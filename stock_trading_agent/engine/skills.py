"""engine/skills.py — v11 skill 注册表

每个 skill 是纯函数 (args: dict) -> dict, 自带 render_to_card 拼飞书卡片。
设计原则:
  - 5 个只读 skill (picks/positions/daily_report/market_env/stage_runs) 0 LLM
  - 2 个解释性 skill (explain_pick/search_knowledge) 调 LLM
  - 1 个计算性 skill (backtest) 跑本地回测

注册表 SKILL_REGISTRY 暴露给 llm/tool_use.py, 也给 LLM 不可用时的
关键词降级路径直接调。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from .data_fetcher import load_config
from .paper_trader import get_db
from .reviewer import backtest_multi
from . import knowledge

log = logging.getLogger("engine.skills")

# v12 防御性: minimax M3 等推理模型可能在 explanation/answer 里泄漏 <think>...</think>
# dispatch 路径 (_strip_think_tags in tool_use.py) 已剥, skill 渲染再剥一次兜底
_STRIP_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    if not text or "<think>" not in text:
        return text
    cleaned = _STRIP_THINK_RE.sub("", text)
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip()


@dataclass
class Skill:
    name: str
    description: str
    uses_llm: bool
    schema: dict[str, Any]
    run: Callable[[dict[str, Any]], dict[str, Any]]
    render_to_card: Callable[[dict[str, Any]], dict[str, Any]]


def _run_get_picks(args: dict[str, Any]) -> dict[str, Any]:
    target_date: str | None = args.get("date")
    top_n: int = int(args.get("top_n", 10))
    conn = get_db()
    if target_date:
        rows = conn.execute(
            "SELECT pick_date, code, name, price, chg_pct, score, sector, plan_used "
            "FROM picks WHERE pick_date = ? ORDER BY score DESC LIMIT ?",
            (target_date, top_n),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT pick_date, code, name, price, chg_pct, score, sector, plan_used "
            "FROM picks ORDER BY pick_date DESC, score DESC LIMIT ?",
            (top_n,),
        ).fetchall()
    return {
        "date": target_date or (rows[0]["pick_date"] if rows else None),
        "count": len(rows),
        "items": [dict(r) for r in rows],
    }


def _render_picks_card(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items", [])
    if not items:
        return {"msg_type": "text", "content": {"text": "(无选股记录)"}}
    # v12.9.1: 改用 interactive card
    from ..feishu.card_templates import card_picks
    card = card_picks(items, date=result.get("date", ""))
    return {"msg_type": "interactive", "content": card}


def _run_get_positions(args: dict[str, Any]) -> dict[str, Any]:
    status: str | None = args.get("status", "open")
    conn = get_db()
    if status == "all":
        rows = conn.execute(
            "SELECT pick_date, code, name, open_price, shares, status, pnl_open_pct, pnl_noon_pct, sector "
            "FROM paper_positions ORDER BY pick_date DESC LIMIT 50"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT pick_date, code, name, open_price, shares, status, pnl_open_pct, pnl_noon_pct, sector "
            "FROM paper_positions WHERE status = ? ORDER BY pick_date DESC LIMIT 50",
            (status,),
        ).fetchall()
    return {"status": status, "count": len(rows), "items": [dict(r) for r in rows]}


def _render_positions_card(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items", [])
    if not items:
        return {"msg_type": "text", "content": {"text": f"(无 {result.get('status', '')} 持仓)"}}
    # v12.9.1: 改用 interactive card
    from ..feishu.card_templates import card_positions
    card = card_positions(items)
    return {"msg_type": "interactive", "content": card}


def _run_get_daily_report(args: dict[str, Any]) -> dict[str, Any]:
    target_date: str | None = args.get("date")
    if not target_date:
        target_date = date.today().isoformat()
    conn = get_db()
    pick_count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM picks WHERE pick_date = ?", (target_date,)
    ).fetchone()
    pnl_row = conn.execute(
        "SELECT AVG(pnl_open_pct) AS open_avg, AVG(pnl_noon_pct) AS noon_avg, "
        "SUM(CASE WHEN pnl_noon_pct > 0 THEN 1 ELSE 0 END) AS win_n, "
        "COUNT(*) AS total FROM paper_positions WHERE pick_date = ?",
        (target_date,),
    ).fetchone()
    env_row = conn.execute(
        "SELECT market_env_score, market_env_level FROM picks "
        "WHERE pick_date = ? ORDER BY id DESC LIMIT 1",
        (target_date,),
    ).fetchone()
    return {
        "date": target_date,
        "pick_count": pick_count_row["n"] if pick_count_row else 0,
        "open_avg_pct": pnl_row["open_avg"] if pnl_row else None,
        "noon_avg_pct": pnl_row["noon_avg"] if pnl_row else None,
        "win_count": pnl_row["win_n"] if pnl_row else 0,
        "total": pnl_row["total"] if pnl_row else 0,
        "env_score": env_row["market_env_score"] if env_row else None,
        "env_level": env_row["market_env_level"] if env_row else None,
    }


def _render_daily_report_card(result: dict[str, Any]) -> dict[str, Any]:
    text = (
        f"📅 日报 · {result.get('date', '?')}\n\n"
        f"  选股: {result.get('pick_count', 0)} 只\n"
        f"  大盘 env: {result.get('env_score', '?')} ({result.get('env_level', '-')})\n"
        f"  开盘 PnL: {(result.get('open_avg_pct') or 0):+.2f}%\n"
        f"  午盘 PnL: {(result.get('noon_avg_pct') or 0):+.2f}%\n"
        f"  胜率: {result.get('win_count', 0)}/{result.get('total', 0)}"
    )
    return {"msg_type": "text", "content": {"text": text}}


def _run_get_market_env(_args: dict[str, Any]) -> dict[str, Any]:
    """v12.5.1: picks 表空时 (周末/假期) 实时拉一次大盘兜底

    优先级: picks 表 (历史选股时算的 env) > data_fetcher 实时拉 > 失败提示
    """
    conn = get_db()
    row = conn.execute(
        "SELECT market_env_score, market_env_level, pick_date FROM picks "
        "ORDER BY pick_date DESC, id DESC LIMIT 1"
    ).fetchone()
    if row:
        return {
            "env_score": row["market_env_score"],
            "env_level": row["market_env_level"],
            "date": row["pick_date"],
            "source": "picks",
        }

    # picks 空 (周末/假期/刚开项目) -> 实时拉一次
    from datetime import date as _date
    try:
        from .data_fetcher import get_market_env as _fetch_env, load_config as _load_cfg
        cfg = _load_cfg()
        env = _fetch_env(cfg)
        return {
            "env_score": env.get("env_score"),
            "env_level": env.get("env_level"),
            "date": _date.today().isoformat(),
            "source": "realtime",
        }
    except Exception as e:  # noqa: BLE001
        log.warning("get_market_env 实时拉失败 (兜底): %s", e)
        return {
            "env_score": None,
            "env_level": "数据源不可用",
            "date": _date.today().isoformat(),
            "source": "failed",
        }


def _render_market_env_card(result: dict[str, Any]) -> dict[str, Any]:
    """v12.5.1: source 字段加标注, 失败给友好提示"""
    src_label = result.get("source", "?")
    src_emoji = {
        "picks": "📊",      # 📊 选股时算的
        "realtime": "⚡️",  # ⚡️ 刚拉的
        "failed": "⚠️",    # ⚠️ 拉失败
    }.get(src_label, "❓")
    score = result.get("env_score")
    score_str = f"{score}" if score is not None else "?"
    level = result.get("env_level") or "?"
    date_str = result.get("date") or "?"
    text = (
        f"{src_emoji} 大盘 · {date_str} ({src_label})\n"
        f"  env_score: {score_str}\n"
        f"  env_level: {level}"
    )
    return {"msg_type": "text", "content": {"text": text}}


def _run_get_stage_runs(args: dict[str, Any]) -> dict[str, Any]:
    target_date: str | None = args.get("date")
    if not target_date:
        target_date = date.today().isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT stage, ran_at, ok FROM stage_runs WHERE run_date = ? ORDER BY ran_at",
        (target_date,),
    ).fetchall()
    return {"date": target_date, "count": len(rows), "items": [dict(r) for r in rows]}


def _render_stage_runs_card(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items", [])
    if not items:
        return {"msg_type": "text", "content": {"text": f"今日 {result.get('date', '')} 还未跑任何阶段"}}
    lines = [f"⏱ 阶段运行 · {result.get('date', '')}", ""]
    for r in items:
        status = "✅" if r.get("ok") else "❌"
        lines.append(f"  {status} {r.get('stage', '?')}  {r.get('ran_at', '')[-8:]}")
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}


def _build_explain_query(pick: dict[str, Any]) -> str:
    """v12.9: 拼 RAG-friendly query, 让 BM25 能从缠论108课/好运2008 召回相关片段

    之前: f"为什么选 {code} {name} (评分 X, 板块 Y)?" — 问法太口语, 知识库召回率 ~0
    现在: 拼 4 类关键词, 命中知识库多个源:
      - 股票名 + 板块
      - 缠中说禅术语 (买点 / 卖点 / 趋势 / 背驰)
      - 好运2008 术语 (龙头 / 主线 / 量价齐升)
      - 评分维度 (强信号 / 高分)
    """
    name = pick.get("name", "")
    sector = pick.get("sector", "-")
    score = pick.get("score", 0)
    score_band = "强信号" if score >= 75 else ("中等" if score >= 60 else "弱信号")
    return f"{name} {sector} 缠中说禅 选股 买点 趋势 量价齐升 龙头 {score_band}"


def _run_explain_pick(args: dict[str, Any]) -> dict[str, Any]:
    from ..llm.reasoner import answer_question, retrieve
    code: str = args.get("code", "").strip()
    if not code:
        return {"code": code, "explanation": "（请提供股票代码）"}
    conn = get_db()
    row = conn.execute(
        "SELECT pick_date, code, name, price, chg_pct, score, sector, plan_used, market_env_score "
        "FROM picks WHERE code = ? ORDER BY pick_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not row:
        # v12.9.1: picks 找不到 → 拉实时行情兜底
        from .data_fetcher import fetch_realtime_quote
        quote = fetch_realtime_quote(code)
        if not quote:
            return {
                "code": code,
                "explanation": (
                    f"这只票 ({code}) 不在我的选股记录里, 我也没拉到实时数据。\n\n"
                    f"你可以试试:\n"
                    f"1. 说股票名 (例 '茅台怎么样') 或完整 6 位代码\n"
                    f"2. 我帮你从选股记录 / 知识库找"
                ),
                "source": "fallback_empty",
            }
        # 实时拉到 → 让 LLM 用实时数据给一句话解释
        name = quote.get("name") or code
        price = quote.get("price")
        chg = quote.get("chg_pct")
        turnover = quote.get("turnover")
        mktcap = quote.get("mktcap_yi")
        # 拼事实给 LLM
        facts = f"{name}({code}) 最新价 {price}, 今日 {chg}%, 换手 {turnover}%, 总市值 {mktcap}亿"
        explanation = answer_question(
            question=f"用大白话一句话说说 {facts}, 给小白用户看 (≤ 100 字)",
            recent_picks=[],
            market_env={},
        )
        explanation = (explanation or "（实时数据已拉到, LLM 暂不可用）").rstrip()
        explanation += "\n\n[数据源: 东方财富实时]"
        return {
            "code": code,
            "name": name,
            "explanation": explanation,
            "source": "realtime",
            "quote": quote,
        }
    pick = dict(row)

    # v12.9: 先 RAG 检索知识库, 把最相关 k 条来源注入 prompt, 末尾再标注 [来源]
    rag_query = _build_explain_query(pick)
    rag_results = retrieve(rag_query, k=5)
    # 来源标注: 优先 title, 否则 source:text 前 20 字
    rag_sources: list[str] = []
    for r in rag_results[:3]:
        title = r.get("title", "").strip()
        if title:
            rag_sources.append(title)
        else:
            src_short = r.get("source", "?")
            text_short = (r.get("text", "") or "").strip()[:20]
            rag_sources.append(f"{src_short}:{text_short}")

    explanation = answer_question(
        question=rag_query,
        recent_picks=[pick],
        market_env={"env_score": pick.get("market_env_score"), "env_level": "-", "position_advice": "-"},
        preset_results=rag_results,  # v12.9: 复用, 不重复 retrieve
    )
    # 末尾追加 [来源: ...], 即便 LLM 没在正文里标也能让用户知道用了哪些知识
    if explanation and rag_sources:
        explanation = explanation.rstrip() + "\n\n[来源] " + " / ".join(rag_sources)
    return {"code": code, "name": pick.get("name"),
            "explanation": explanation or "（LLM 暂不可用）",
            "rag_sources": rag_sources}
    return {"code": code, "name": pick.get("name"),
            "explanation": explanation or "（LLM 暂不可用）",
            "rag_sources": rag_titles}


def _render_explain_card(result: dict[str, Any]) -> dict[str, Any]:
    explanation = _strip_think(result.get("explanation", ""))
    sources = result.get("rag_sources") or result.get("sources") or []
    # v12.9.1: 改用 interactive card
    from ..feishu.card_templates import card_explain
    card = card_explain(
        code=result.get("code", "?"),
        name=result.get("name", ""),
        explanation=explanation,
        sources=sources,
    )
    return {"msg_type": "interactive", "content": card}


def _run_search_knowledge(args: dict[str, Any]) -> dict[str, Any]:
    from ..llm.reasoner import with_knowledge
    query: str = args.get("query", "").strip()
    k: int = int(args.get("k", 3))
    if not query:
        return {"query": query, "answer": "（请提供问题）", "sources": []}
    results = knowledge.retrieve(query, k=k)
    sources = [{"title": r.get("title", "?"), "score": r.get("score", 0)} for r in results]
    answer = with_knowledge(query, k=k) if results else "（知识库无相关结果）"
    return {"query": query, "answer": answer or "（LLM 暂不可用）", "sources": sources}


def _render_search_knowledge_card(result: dict[str, Any]) -> dict[str, Any]:
    answer = _strip_think(result.get("answer", ""))
    text = f"📚 {result.get('query', '')}\n\n{answer}"
    srcs = result.get("sources", [])
    if srcs:
        text += "\n\n来源: " + " / ".join(s.get("title", "?") for s in srcs[:3])
    return {"msg_type": "text", "content": {"text": text}}


def _run_backtest(args: dict[str, Any]) -> dict[str, Any]:
    strategy: str = args.get("strategy", "auto")
    days: int = int(args.get("days", 30))
    try:
        result = backtest_multi(days=days, fixtures_dir=None)
    except Exception as e:
        return {"strategy": strategy, "days": days, "error": str(e)}
    return {
        "strategy": strategy,
        "days": days,
        "metrics": result.get("metrics", {}),
        "by_plan": result.get("by_plan", {}),
    }


def _render_backtest_card(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return {"msg_type": "text", "content": {"text": f"回测失败: {result['error']}"}}
    m = result.get("metrics", {})
    text = (
        f"📈 回测 · {result.get('strategy', '')} · {result.get('days', 0)}d\n\n"
        f"  胜率: {m.get('win_rate', 0):.1f}%\n"
        f"  平均 PnL: {m.get('avg_pnl', 0):+.2f}%\n"
        f"  Sharpe: {m.get('sharpe', 0):.2f}\n"
        f"  最大回撤: {m.get('max_drawdown', 0):.2f}%"
    )
    return {"msg_type": "text", "content": {"text": text}}


SKILL_REGISTRY: dict[str, Skill] = {
    "get_picks": Skill(
        name="get_picks",
        description="查询选股记录。可指定日期或返回最近 N 条。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "get_picks",
                "description": "查询选股记录 (picks 表)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD, 缺省返回最近"},
                        "top_n": {"type": "integer", "description": "返回数量, 默认 10"},
                    },
                    "required": [],
                },
            },
        },
        run=_run_get_picks,
        render_to_card=_render_picks_card,
    ),
    "get_positions": Skill(
        name="get_positions",
        description="查询 paper 持仓。status: open / closed_open / closed_noon / all。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "get_positions",
                "description": "查询 paper 持仓 (paper_positions 表)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "open | closed_open | closed_noon | all"},
                    },
                    "required": [],
                },
            },
        },
        run=_run_get_positions,
        render_to_card=_render_positions_card,
    ),
    "get_daily_report": Skill(
        name="get_daily_report",
        description="查询某日日报: 选股数 + paper PnL + 大盘 env。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "get_daily_report",
                "description": "查询日报 (选股数 + PnL + 大盘)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD, 缺省今日"},
                    },
                    "required": [],
                },
            },
        },
        run=_run_get_daily_report,
        render_to_card=_render_daily_report_card,
    ),
    "get_market_env": Skill(
        name="get_market_env",
        description="查询最新大盘环境评分 env_score / env_level。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "get_market_env",
                "description": "查询大盘环境 (env_score / env_level)",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        run=_run_get_market_env,
        render_to_card=_render_market_env_card,
    ),
    "get_stage_runs": Skill(
        name="get_stage_runs",
        description="查询某日已跑的 stage 列表 (stage_runs 表)。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "get_stage_runs",
                "description": "查询今日/某日阶段运行记录",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD, 缺省今日"},
                    },
                    "required": [],
                },
            },
        },
        run=_run_get_stage_runs,
        render_to_card=_render_stage_runs_card,
    ),
    "explain_pick": Skill(
        name="explain_pick",
        description="解释某只票为什么被选 (LLM + RAG)。需要 code。",
        uses_llm=True,
        schema={
            "type": "function",
            "function": {
                "name": "explain_pick",
                "description": "LLM 解释某只票为什么入选 (含 RAG 知识库)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "6 位股票代码"},
                    },
                    "required": ["code"],
                },
            },
        },
        run=_run_explain_pick,
        render_to_card=_render_explain_card,
    ),
    "search_knowledge": Skill(
        name="search_knowledge",
        description="搜索知识库 (好运2008 / 苏三) 找最相关的 k 条, LLM 包装。",
        uses_llm=True,
        schema={
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "RAG 检索知识库 (好运2008 + 苏三)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "查询文本"},
                        "k": {"type": "integer", "description": "返回条数, 默认 3"},
                    },
                    "required": ["query"],
                },
            },
        },
        run=_run_search_knowledge,
        render_to_card=_render_search_knowledge_card,
    ),
    "backtest": Skill(
        name="backtest",
        description="跑多策略回测, 返回胜率/Sharpe/最大回撤。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "backtest",
                "description": "回测最近 N 天的多策略对比",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "strategy": {"type": "string", "description": "auto | A | B | C"},
                        "days": {"type": "integer", "description": "回测窗口, 默认 30"},
                    },
                    "required": [],
                },
            },
        },
        run=_run_backtest,
        render_to_card=_render_backtest_card,
    ),
}


# 关键词降级路径 (LLM 不可用时)
# 按"最具体优先"排序, 命中第一个返回 skill 名
_KEYWORD_FALLBACK: list[tuple[list[str], str]] = [
    # v12.9: 知识库关键词放最前 (最具体优先)
    (["缠论", "缠中说禅", "108课", "108 课", "好运2008", "好运 2008", "苏三", "知识库", "知识", "教材", "理论", "心法"], "search_knowledge"),
    (["picks", "选股", "今日选股", "今日推荐", "today", "今日"], "get_picks"),
    (["持仓", "positions", "开了什么", "现在有什么"], "get_positions"),
    (["日报", "daily", "今日战况", "今日怎么样"], "get_daily_report"),
    (["大盘", "市场环境", "env", "市场怎么样", "大盘怎么样"], "get_market_env"),
    (["阶段", "跑了", "stage_runs", "今天跑了什么"], "get_stage_runs"),
    (["回测", "backtest", "复盘"], "backtest"),
]


def keyword_fallback(text: str) -> str | None:
    """LLM 不可用时, 按关键词挑一个只读 skill 名

    只命中 5 个只读 skill + backtest, 不命中返回 None
    (避免 2 个 LLM skill 在降级路径被错误激活)。
    """
    if not text:
        return None
    t = text.lower()
    for keywords, skill_name in _KEYWORD_FALLBACK:
        if any(kw in t for kw in keywords):
            return skill_name
    return None


def call_skill(skill_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """从注册表里找 skill 并调用。

    返回 {"ok": True, "card": {...}, "raw": {...}, "uses_llm": bool, "name": str}
    或 {"ok": False, "error": "...", "name": str}。
    """
    skill = SKILL_REGISTRY.get(skill_name)
    if skill is None:
        return {"ok": False, "error": f"未知 skill: {skill_name}", "name": skill_name}
    try:
        raw = skill.run(args)
        card = skill.render_to_card(raw)
        return {"ok": True, "card": card, "raw": raw, "uses_llm": skill.uses_llm, "name": skill_name}
    except Exception as e:
        log.exception("skill %s 失败: %s", skill_name, e)
        return {"ok": False, "error": str(e), "name": skill_name}


def tool_schemas() -> list[dict[str, Any]]:
    """给 LLM tool-use 用的 schema 列表 (OpenAI 风格)"""
    return [s.schema for s in SKILL_REGISTRY.values()]
