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

from .data_fetcher import load_config, is_trading_day, get_market_env
from .paper_trader import get_db
from .reviewer import backtest_multi
from . import knowledge
from .evidence import (
    build_evidence_from_rag,
    build_evidence_from_sql,
    build_evidence_from_live,
    build_evidence_from_facts,
    format_evidence_for_prompt,
    render_evidence_section,
)

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
    """v12.A.2 改: picks 空时返 stage 状态 (治 '无选股记录' 答非所问体感)

    之前 picks 空 → 只返 {items: []} → 卡片 "无选股记录", 用户不知道为啥没
    现在 picks 空 → 查 stage_runs 看今天 pick 跑没跑, 给用户友好提示
    """
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
    result: dict[str, Any] = {
        "date": target_date or (rows[0]["pick_date"] if rows else None),
        "count": len(rows),
        "items": [dict(r) for r in rows],
    }
    # v12.A.3: evidence 字段 (rows 是 sqlite3.Row, 没 .get(), 用 dict 包装)
    if rows:
        first = dict(rows[0])
        result["evidence"] = build_evidence_from_sql(
            f"picks 表 ({target_date or '最近'})",
            len(rows),
            sample=f"{first.get('code', '')} {first.get('name', '')} 评分 {first.get('score', 0):.1f}",
        )
    # v12.A.2: picks 空时查 stage 状态
    if not rows:
        try:
            from datetime import datetime as _dt
            today = _dt.now().strftime("%Y-%m-%d")
            stage_row = conn.execute(
                "SELECT stage, ran_at, ok FROM stage_runs WHERE run_date=? AND stage='pick'",
                (today,),
            ).fetchone()
            if stage_row is None:
                result["empty_reason"] = f"今天 ( {today} ) pick stage 还没跑 (计划 14:00 跑)"
            elif stage_row["ok"] == 0:
                result["empty_reason"] = f"今天 pick stage 跑失败 ( {stage_row['ran_at'] } ), 详见 /stage"
            else:
                result["empty_reason"] = f"今天 pick 跑过但没出候选 (大盘/筛选太严, 详见 /stage)"
        except Exception as e:  # noqa: BLE001
            log.debug("_run_get_picks empty_reason 查 stage_runs 失败: %s", e)
    return result


def _render_picks_card(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items", [])
    if not items:
        # v12.A.2: picks 空时用 empty_reason 告诉用户为啥没 (治 '答非所问')
        reason = result.get("empty_reason", "无选股记录")
        return {"msg_type": "text", "content": {"text": f"📭 {reason}"}}
    # v12.9.1: 改用 interactive card (v12.A.3 +evidence 段)
    from .cards import card_picks
    card = card_picks(items, date=result.get("date", ""),
                       evidence=result.get("evidence"))
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
    result = {"status": status, "count": len(rows), "items": [dict(r) for r in rows]}
    if rows:
        first = dict(rows[0])
        result["evidence"] = build_evidence_from_sql(
            f"paper_positions 表 (status={status})",
            len(rows),
            sample=f"{first.get('code', '')} {first.get('name', '')}",
        )
    return result


def _render_positions_card(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items", [])
    if not items:
        return {"msg_type": "text", "content": {"text": f"(无 {result.get('status', '')} 持仓)"}}
    # v12.9.1: 改用 interactive card (v12.A.3 +evidence 段)
    from .cards import card_positions
    card = card_positions(items, evidence=result.get("evidence"))
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


def _run_get_market_env(args: dict[str, Any]) -> dict[str, Any]:
    """v12.A: 接 date 参数, 治 LLM '截止最新运行日' hallucinate

    优先级:
      1) date 显式传入 + picks 表有该日数据 → 用 picks
      2) date 显式传入 + picks 表无 → 友好提示 (不瞎编)
      3) date 未传 + picks 表非空 → 用最近一行 (向后兼容)
      4) date 未传 + picks 空 → 实时拉 (v12.5.1 老逻辑)

    date 语义:
      YYYY-MM-DD (今天/过去日) | "today" (默认)
      未来日 → 返 '未开盘'
      周末/节假日 → 返 '不开盘'
      过去交易日 + 无数据 → 返 '历史数据暂无'
    """
    from datetime import date as _date, datetime as _dt
    # 1) 解析 date 参数
    #    没传 date → target_date=None, 跳过未来/非交易日检查, 走"最近一行 picks"或实时拉
    #    date="today" → target_date=today, 走交易日判断
    #    date="YYYY-MM-DD" → target_date=对应日
    raw_date = (args or {}).get("date")
    today = _date.today()
    if raw_date is None or raw_date == "":
        target_date: _date | None = None
    elif raw_date == "today":
        target_date = today
    else:
        try:
            target_date = _dt.strptime(str(raw_date), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return {
                "env_score": None,
                "env_level": "日期格式错 (要 YYYY-MM-DD)",
                "date": str(raw_date),
                "source": "bad_date",
            }

    # 2) 未来日 → 未开盘 (明确告诉用户, 避免 LLM hallucinate)
    if target_date and target_date > today:
        return {
            "env_score": None,
            "env_level": "未开盘 (未来日)",
            "date": target_date.isoformat(),
            "source": "future",
        }

    # 3) 非交易日 → 周末/节假日
    if target_date and not is_trading_day(target_date):
        wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][target_date.weekday()]
        return {
            "env_score": None,
            "env_level": f"{wd}不开盘 (周末/节假日)",
            "date": target_date.isoformat(),
            "source": "non_trading_day",
        }

    # 4) 查 picks 表 (精确匹配 date 优先, 否则取最近一行)
    conn = get_db()
    if target_date:
        row = conn.execute(
            "SELECT market_env_score, market_env_level, pick_date FROM picks "
            "WHERE pick_date = ? ORDER BY id DESC LIMIT 1",
            (target_date.isoformat(),),
        ).fetchone()
    else:
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
            "evidence": build_evidence_from_sql(
                f"picks 表 ({row['pick_date']})", 1,
                sample=f"env_score={row['market_env_score']} level={row['market_env_level']}",
            ),
        }

    # 5) 过去交易日 + picks 无 → 明确说'历史数据暂无', 不瞎编截止日
    if target_date and target_date < today:
        return {
            "env_score": None,
            "env_level": "历史数据暂无 (picks 表当日为空)",
            "date": target_date.isoformat(),
            "source": "no_history",
            "evidence": build_evidence_from_sql(
                f"picks 表 ({target_date}, 空)", 0, sample="该日无选股/无 env 记录",
            ),
        }

    # 6) date=today + picks 空 → 实时拉 (v12.5.1 老逻辑, 保持不变)
    try:
        from .data_fetcher import get_market_env as _fetch_env, load_config as _load_cfg
        cfg = _load_cfg()
        env = _fetch_env(cfg)  # v12.A.3: 我之前改 evidence 误删了, 补回
        return {
            "env_score": env.get("env_score"),
            "env_level": env.get("env_level"),
            "date": today.isoformat(),
            "source": "realtime",
            "evidence": build_evidence_from_sql(
                "东方财富 push2 实时大盘", 1,
                sample=f"env_score={env.get('env_score')} level={env.get('env_level')}",
            ),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("get_market_env 实时拉失败 (兜底): %s", e)
        return {
            "env_score": None,
            "env_level": "数据源不可用",
            "date": today.isoformat(),
            "source": "failed",
            "evidence": [],
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
    # v12.A.3: 末尾加 evidence 段
    evidence = result.get("evidence") or []
    if evidence:
        ev_lines = ["", "**📚 证据:**"]
        for ev in evidence[:3]:
            eid = ev.get("id", "?")
            title = (ev.get("title") or "").strip()[:40]
            ev_lines.append(f"- `[{eid}]` {title}" if title else f"- `[{eid}]`")
        text += "\n" + "\n".join(ev_lines)
    return {"msg_type": "text", "content": {"text": text}}


def _run_get_stock_quote(args: dict[str, Any]) -> dict[str, Any]:
    """v12.A.1: 个股某日 K 线 (push2his.kline)

    优先级: args.date → date=今天 → 实时接口
    失败 (代码错/网络挂/节假日无数据) → 返 source=empty
    """
    from .data_fetcher import fetch_stock_kline, is_trading_day
    code: str = (args or {}).get("code", "").strip()
    raw_date = (args or {}).get("date")
    today = date.today()
    if not raw_date or raw_date == "today":
        target_date = today
    else:
        try:
            from datetime import datetime as _dt
            target_date = _dt.strptime(str(raw_date), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return {
                "code": code, "date": str(raw_date),
                "env_level": "日期格式错 (要 YYYY-MM-DD)",
                "source": "bad_date",
            }
    if not code or len(code) != 6 or not code.isdigit():
        return {
            "code": code, "date": target_date.isoformat(),
            "env_level": "请提供 6 位股票代码",
            "source": "bad_code",
        }
    # 未来日
    if target_date > today:
        return {
            "code": code, "date": target_date.isoformat(),
            "env_level": "未开盘 (未来日)",
            "source": "future",
        }
    # 拉 K 线
    kline = fetch_stock_kline(code, target_date.isoformat())
    if not kline:
        return {
            "code": code, "date": target_date.isoformat(),
            "env_level": "拉不到该日 K 线 (代码错 / 节后无数据 / 接口挂)",
            "source": "empty",
        }
    # K 线接口总是返最近一根, 如果不是用户问的那天 → 标 mismatch
    fetched_date = kline.get("date")
    if fetched_date and fetched_date != target_date.isoformat():
        kline["date"] = fetched_date  # 用接口实际返的 (最近交易日)
        kline["requested_date"] = target_date.isoformat()
    # v12.A.3: evidence 字段
    kline["evidence"] = build_evidence_from_sql(
        f"东方财富 push2his.kline ({fetched_date or target_date})", 1,
        sample=f"{kline.get('name', code)} 收盘 {kline.get('close')} 涨跌 {kline.get('chg_pct')}%",
    )
    return kline


def _render_stock_quote_card(result: dict[str, Any]) -> dict[str, Any]:
    """v12.A.1: 个股 K 线行情卡片"""
    code = result.get("code", "?")
    name = result.get("name") or code
    src = result.get("source", "kline")
    src_emoji = {
        "kline": "📈", "东方财富K线": "📈",
        "future": "⏳", "bad_date": "❓", "bad_code": "❓", "empty": "⚠️",
    }.get(src, "📊")
    if src in ("future", "bad_date", "bad_code", "empty"):
        text = (
            f"{src_emoji} {code} {name} · {result.get('date', '?')}\n"
            f"  {result.get('env_level', '?')}"
        )
        return {"msg_type": "text", "content": {"text": text}}
    close = result.get("close")
    chg = result.get("chg_pct")
    chg_amt = result.get("chg_amt")
    turnover = result.get("turnover")
    amount_yi = result.get("amount_yi")
    amplitude = result.get("amplitude")
    high = result.get("high")
    low = result.get("low")
    open_p = result.get("open")
    date_str = result.get("date", "?")
    req_date = result.get("requested_date")
    date_label = date_str
    if req_date and req_date != date_str:
        date_label = f"{date_str} (问 {req_date}, 该日无数据, 取最近交易日)"
    # chg 用 + 标识涨/跌
    chg_str = f"{chg:+.2f}%" if chg is not None else "?"
    chg_amt_str = f"{chg_amt:+.2f}元" if chg_amt is not None else "?"
    text = (
        f"{src_emoji} {code} {name} \u00b7 {date_label}\n"
        f"  \u6536\u76d8: {close} ({chg_str}, {chg_amt_str})\n"
        f"  \u5f00/\u9ad8/\u4f4e: {open_p} / {high} / {low}\n"
        f"  \u632f\u5e45: {amplitude}%\n"
        f"  \u6210\u4ea4\u989d: {amount_yi}\u4ebf\n"
        f"  \u6362\u624b: {turnover}%"
    )
    # v12.A.3: 末尾加 evidence 段
    evidence = result.get("evidence") or []
    if evidence:
        ev_lines = ["", "**📚 证据:**"]
        for ev in evidence[:3]:
            eid = ev.get("id", "?")
            title = (ev.get("title") or "").strip()[:40]
            ev_lines.append(f"- `[{eid}]` {title}" if title else f"- `[{eid}]`")
        text += "\n" + "\n".join(ev_lines)
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
    code: str = (args or {}).get("code", "").strip()
    if not code:
        return {"code": code, "explanation": "（请提供股票代码）"}
    conn = get_db()
    row = conn.execute(
        "SELECT pick_date, code, name, price, chg_pct, score, sector, plan_used, market_env_score "
        "FROM picks WHERE code = ? ORDER BY pick_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not row:
        # v12.A.1: 优先用 date 拉 K 线 (治 "禾望电气周五行情" 类)
        from .data_fetcher import fetch_stock_kline, fetch_realtime_quote
        kline_date = (args or {}).get("date")
        kline = fetch_stock_kline(code, kline_date) if kline_date else {}
        if not kline or not kline.get("close"):
            # 没 date 或 K 线拉不到 → 走实时
            kline = fetch_realtime_quote(code)
            if not kline:
                return {
                    "code": code,
                    "explanation": (
                        f"这只票 ({code}) 不在我的选股记录里, 我也没拉到行情数据。\n\n"
                        f"你可以试试:\n"
                        f"1. 说股票名 (例 '茅台怎么样') 或完整 6 位代码\n"
                        f"2. 我帮你从选股记录 / 知识库找"
                    ),
                    "source": "fallback_empty",
                }
        # K 线/实时 拉到 → 让 LLM 用数据给一句话解释
        name = kline.get("name") or code
        if "close" in kline and kline.get("date"):
            # K 线数据 (有 date 字段)
            price = kline.get("close")
            chg = kline.get("chg_pct")
            chg_amt = kline.get("chg_amt")
            turnover = kline.get("turnover")
            amount_yi = kline.get("amount_yi")
            k_date = kline.get("date")
            data_tag = f"[数据源: 东方财富K线 · {k_date}]"
            facts = f"{name}({code}) {k_date} 收盘 {price}元, 涨跌 {chg}% ({chg_amt:+.2f}元), 换手 {turnover}%, 成交额 {amount_yi}亿"
        else:
            # 实时数据
            price = kline.get("price")
            chg = kline.get("chg_pct")
            turnover = kline.get("turnover")
            mktcap = kline.get("mktcap_yi")
            data_tag = "[数据源: 东方财富实时]"
            facts = f"{name}({code}) 最新价 {price}, 今日 {chg}%, 换手 {turnover}%, 总市值 {mktcap}亿"
        explanation = answer_question(
            question=f"用大白话一句话说说 {facts}, 给小白用户看 (≤ 100 字)",
            recent_picks=[],
            market_env={},
        )
        explanation = (explanation or "（行情数据已拉到, LLM 暂不可用）").rstrip()
        explanation += f"\n\n{data_tag}"
        return {
            "code": code,
            "name": name,
            "explanation": explanation,
            "source": "kline" if "close" in kline else "realtime",
            "quote": kline,
        }
    pick = dict(row)

    # v12.9.3: picks 找到时也尝试拉一次实时 (容错, 失败不影响) — 用户问"实时"时也能答上
    realtime: dict[str, Any] = {}
    try:
        from .data_fetcher import fetch_realtime_quote
        realtime = fetch_realtime_quote(code)
    except Exception:  # noqa: BLE001
        realtime = {}

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

    # v12.9.3: 实时价/涨跌 拼成 facts, 喂给 LLM
    if realtime:
        rt_facts = (
            f"今日实时: {realtime.get('name') or code} "
            f"最新价 {realtime.get('price')}, "
            f"今日 {realtime.get('chg_pct')}%, "
            f"换手 {realtime.get('turnover')}%, "
            f"总市值 {realtime.get('mktcap_yi')}亿"
        )
        rag_query = f"{rag_query} | {rt_facts}"
    explanation = answer_question(
        question=rag_query,
        recent_picks=[pick],
        market_env={"env_score": pick.get("market_env_score"), "env_level": "-", "position_advice": "-"},
        preset_results=rag_results,  # v12.9: 复用, 不重复 retrieve
    )
    # 末尾追加 [来源: ...], 即便 LLM 没在正文里标也能让用户知道用了哪些知识
    if explanation and rag_sources:
        explanation = explanation.rstrip() + "\n\n[来源] " + " / ".join(rag_sources)
    # v12.9.3: 实时拉到 → 末尾追加 [实时数据: 价/涨跌] 一行, 让用户明确看到 bot 用了实时
    if realtime and explanation:
        rt_line = (
            f"\n\n[实时 {realtime.get('price')} 元 · "
            f"今日 {realtime.get('chg_pct')}% · "
            f"换手 {realtime.get('turnover')}%]"
        )
        explanation = explanation.rstrip() + rt_line
    # v12.A.3: evidence 字段 (RAG + 实时一起, 用于卡片底部)
    evidence: list[dict[str, Any]] = []
    evidence.extend(build_evidence_from_rag(rag_results, max_items=3))
    if realtime:
        evidence.extend(build_evidence_from_live(
            realtime.get("name") or code,
            realtime.get("price"),
            realtime.get("chg_pct"),
        ))
    return {"code": code, "name": pick.get("name"),
            "explanation": explanation or "（LLM 暂不可用）",
            "rag_sources": rag_sources,
            "realtime": realtime,
            "evidence": evidence}


def _render_explain_card(result: dict[str, Any]) -> dict[str, Any]:
    explanation = _strip_think(result.get("explanation", ""))
    sources = result.get("rag_sources") or result.get("sources") or []
    # v12.9.1: 改用 interactive card (v12.A.3 evidence 段)
    from .cards import card_explain
    card = card_explain(
        code=result.get("code", "?"),
        name=result.get("name", ""),
        explanation=explanation,
        sources=sources,
        evidence=result.get("evidence"),
    )
    return {"msg_type": "interactive", "content": card}


def _run_search_knowledge(args: dict[str, Any]) -> dict[str, Any]:
    from ..llm.reasoner import with_knowledge
    query: str = args.get("query", "").strip()
    k: int = int(args.get("k", 3))
    if not query:
        return {"query": query, "answer": "（请提供问题）", "sources": [], "evidence": []}
    results = knowledge.retrieve(query, k=k)
    sources = [{"title": r.get("title", "?"), "score": r.get("score", 0)} for r in results]
    answer = with_knowledge(query, k=k) if results else "（知识库无相关结果）"
    # v12.A.3: evidence 字段
    evidence = build_evidence_from_rag(results, max_items=3)
    return {"query": query, "answer": answer or "（LLM 暂不可用）", "sources": sources, "evidence": evidence}


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




def _run_get_stock_lifecycle(args: dict[str, Any]) -> dict[str, Any]:
    """v12.A.3: 查某只票的 temporal facts 时间线

    Args:
        code: 股票代码 (e.g. "002063")
        include_invalidated: 是否含 superseded/invalidated (默认 False 只 active)
    """
    from .temporal_facts import query_active, query_all
    code: str = (args or {}).get("code", "").strip()
    include_invalidated: bool = bool((args or {}).get("include_invalidated", False))
    if not code:
        return {"code": code, "events": [], "error": "（请提供股票代码）"}
    if include_invalidated:
        events = query_all(subject=code, include_invalidated=True)
    else:
        events = query_active(subject=code)
    # 按时间倒序
    events.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {
        "code": code,
        "events": events,
        "count": len(events),
        "include_invalidated": include_invalidated,
    }


def _render_stock_lifecycle_card(result: dict[str, Any]) -> dict[str, Any]:
    code = result.get("code", "?")
    events = result.get("events", [])
    if not events:
        return {"msg_type": "text", "content": {"text": f"📅 {code} 暂无时序记录"}}
    lines = [f"📅 **{code} 时序账本** ({result.get('count', 0)} 条)"]
    for e in events[:10]:
        status_icon = {"active": "🟢", "superseded": "🟡", "invalidated": "🔴"}.get(
            e.get("status", "?"), "⚪")
        pred = e.get("predicate", "?")
        claim = (e.get("claim") or "")[:50]
        date = (e.get("created_at", "")[:10]) or "?"
        lines.append(f"{status_icon} `{date}` **{pred}** {claim}")
    if len(events) > 10:
        lines.append(f"_(还有 {len(events) - 10} 条未显示)_")
    return {"msg_type": "interactive", "content": {
        "tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}
    }}


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
                "description": "查询大盘环境 (env_score / env_level)。"
                               " 支持传 date=YYYY-MM-DD 回看历史日 (返友好提示, 不会编'截止日'幻觉);"
                               " 传 date='today' 或省略 = 今天 (实时拉)。"
                               " 未来日/周末/节假日/过去无数据日 都返明确文案, 不瞎编。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "目标日期 YYYY-MM-DD, 或 'today' (默认今天)",
                        },
                    },
                    "required": [],
                },
            },
        },
        run=_run_get_market_env,
        render_to_card=_render_market_env_card,
    ),
    "get_stock_quote": Skill(
        name="get_stock_quote",
        description="查询个股某日行情 (开高低收/涨跌/换手)。需要 code, 可选 date。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "get_stock_quote",
                "description": "查询个股某日 K 线行情 (东方财富 push2his.kline)。"
                               " 必须传 code (6 位); 可选 date=YYYY-MM-DD (默认今天)。"
                               " 治 '禾望电气周五行情' 类: 周五=6-12 时返 6-12 收盘价, 不幻觉。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "6 位股票代码"},
                        "date": {"type": "string", "description": "YYYY-MM-DD 或 'today' (默认今天)"},
                    },
                    "required": ["code"],
                },
            },
        },
        run=_run_get_stock_quote,
        render_to_card=_render_stock_quote_card,
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
    "get_stock_lifecycle": Skill(
        name="get_stock_lifecycle",
        description="查某只票的时序账本 (选股/复盘/作废) 时间线。",
        uses_llm=False,
        schema={
            "type": "function",
            "function": {
                "name": "get_stock_lifecycle",
                "description": "查个股时序事实 (active / 含 invalidated)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "6 位股票代码"},
                        "include_invalidated": {
                            "type": "boolean",
                            "description": "是否含 superseded/invalidated (默认 false 只 active)",
                        },
                    },
                    "required": ["code"],
                },
            },
        },
        run=_run_get_stock_lifecycle,
        render_to_card=_render_stock_lifecycle_card,
    ),
}


# 关键词降级路径 (LLM 不可用时)
# 按"最具体优先"排序, 命中第一个返回 skill 名
_KEYWORD_FALLBACK: list[tuple[list[str], str]] = [
    # v12.A.1: 优先级: 知识库 > 个股 (含 6 位代码) > 持仓 > 选股 > 大盘 > 阶段 > 回测
    #          治 "今日持仓" 截胡到 get_picks; 治 "它今天行情" 漏到 get_market_env
    (["缠论", "缠中说禅", "108课", "108 课", "好运2008", "好运 2008", "苏三", "知识库", "知识", "教材", "理论", "心法"], "search_knowledge"),
    (["持仓", "positions", "开了什么", "现在有什么", "仓位", "我的股"], "get_positions"),
    (["个股", "股价", "股票", "这个股", "这个票", "只股", "只票"], "explain_pick"),
    (["picks", "选股", "今日选股", "今日推荐", "today", "今日"], "get_picks"),
    (["日报", "daily", "今日战况", "今日复盘"], "get_daily_report"),
    (["大盘", "市场环境", "env", "市场怎么样", "大盘怎么样", "行情", "市场行情", "盘面"], "get_market_env"),
    (["阶段", "跑了", "stage_runs", "今天跑了什么"], "get_stage_runs"),
    (["回测", "backtest", "复盘"], "backtest"),
    # v12.A.3: 时序账本
    (["时序", "生命周期", "lifecycle", "历史决策", "这票历史"], "get_stock_lifecycle"),
]


_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_relative_date(text: str) -> str | None:
    """v12.A: 解析 '周五' / '下周一' / '昨天' / '今天' → 'YYYY-MM-DD'

    只解析 4 种; 解析不到返 None (让上游走默认 today)
    """
    import re
    from datetime import date, timedelta

    today = date.today()
    t = text.strip()

    if "今天" in t or "今日" in t:
        return today.isoformat()
    if "昨天" in t or "昨日" in t:
        return (today - timedelta(days=1)).isoformat()
    if "明天" in t or "明日" in t:
        return (today + timedelta(days=1)).isoformat()

    # 显式 2025-11-07 (3 段)
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    # 显式 11-07 (2 段, 默认本年)
    m2 = re.search(r"\b(\d{1,2})-(\d{1,2})\b", t)
    if m2:
        try:
            return date(today.year, int(m2.group(1)), int(m2.group(2))).isoformat()
        except ValueError:
            return None

    # 周X: "下周五" 强推下周; "上周五" 强推上周; 单独 "周五" 默认下一个
    for i, wd in enumerate(_WEEKDAY_CN):
        if wd in t:
            cur_wd = today.weekday()  # 0=Mon
            diff = i - cur_wd
            # 显式 "下周" / "下个周X" / "下X" → 至少下周
            if "下周" in t or "下个" in t or "下" + wd in t:
                if diff <= 0:
                    diff += 7
            # 显式 "上周" / "上X" → 至少上周
            elif "上周" in t or "上个" in t or "上" + wd in t:
                if diff >= 0:
                    diff -= 7
            else:
                # 单独 "周五" → 默认下一个 (用户没指定过去)
                if diff <= 0:
                    diff += 7
            return (today + timedelta(days=diff)).isoformat()
    return None


_STOCK_CODE_RE = re.compile(r"\b\d{6}\b")  # 6 位代码 (沪 6xxxxx, 深 0xxxxx, 北 4/8xxxxx)


def keyword_fallback(text: str) -> str | None:
    """v12.A.1: LLM 不可用时, 按优先级挑一个 skill

    优先级 (v12.A.1):
      1) 含 6 位股票代码 (\d{6}) → get_stock_quote (有 date 时) 或 explain_pick
      2) 知识库关键词 → search_knowledge
      3) 持仓/仓位关键词 → get_positions  (提前到选股前面, 治 "今日持仓" 截胡)
      4) "个股/股价/股票/这个股" → explain_pick
      5) 选股/今日/picks → get_picks
      6) 日报/复盘 → get_daily_report
      7) 大盘/行情/市场 → get_market_env  (没标的)
      8) 阶段 → get_stage_runs
      9) 回测/复盘 → backtest
    """
    if not text:
        return None
    t = text.lower()

    # 1) 6 位代码 → 个股 skill
    m = _STOCK_CODE_RE.search(t)
    if m:
        code = m.group()
        # 有 date / 行情 词 → get_stock_quote (拉 K 线); 否则 explain_pick (解释/RAG)
        if any(kw in t for kw in ["行情", "价", "k线", "涨跌"]) or _parse_relative_date(t):
            return "get_stock_quote"
        return "explain_pick"

    # 2-N) 顺序匹配 _KEYWORD_FALLBACK
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
