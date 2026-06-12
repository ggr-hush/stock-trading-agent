"""
llm/reasoner.py — 6 个 LLM 调用点统一入口
- 失败降级：返回空字符串 + 写 llm_logs
- 6 个调用点: pick_intro / risk_explain / param_reason / weekly_summary / empty_day / anomaly
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from ..engine.paper_trader import get_db
from ..engine.knowledge import format_context, retrieve
from ..engine.sessions import append_turn, get_history
from .client import chat

log = logging.getLogger("llm.reasoner")

PROMPTS_DIR = Path(__file__).parent / "prompts"
_env = Environment(loader=FileSystemLoader(str(PROMPTS_DIR)), autoescape=False)


def _render(template_name: str, **ctx: Any) -> str:
    """渲染 jinja2 模板"""
    tpl = _env.get_template(template_name)
    return tpl.render(**ctx)


def _log_call(call_site: str, success: bool, latency_ms: int, error: str = "",
              prompt_tokens: int = 0, completion_tokens: int = 0,
              tool_name: str | None = None, tool_args: str | None = None,
              chat_id: str | None = None) -> None:
    """写 llm_logs 表（失败也不抛异常）

    v11: 新增 tool_name / tool_args / chat_id 3 列, 兼容老库
    (老库缺列 → 静默走 fallback 路径, 只写老字段)。
    """
    try:
        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO llm_logs
                (call_at, call_site, prompt_tokens, completion_tokens, latency_ms, success, error,
                 tool_name, tool_args, chat_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (datetime.now().isoformat(), call_site, prompt_tokens, completion_tokens,
                 latency_ms, 1 if success else 0, error,
                 tool_name, tool_args, chat_id),
            )
        except Exception:
            # 老库缺新列, 走 fallback
            conn.execute(
                """
                INSERT INTO llm_logs
                (call_at, call_site, prompt_tokens, completion_tokens, latency_ms, success, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (datetime.now().isoformat(), call_site, prompt_tokens, completion_tokens,
                 latency_ms, 1 if success else 0, error),
            )
        conn.commit()
    except Exception as e:
        log.warning("写 llm_logs 失败: %s", e)


# ─────────── 6 个调用点 ───────────

def pick_intro(pick_result: dict[str, Any]) -> str:
    """14:00 选股卡片开头 1-2 句"""
    env = pick_result.get("market_env", {})
    sectors = pick_result.get("sectors", [])
    plan = pick_result.get("plan_used", "C")
    n_picks = len(pick_result.get("filtered_stocks", []))
    prompt = _render("pick_intro.j2",
                     date=pick_result.get("date", ""),
                     plan_used=plan,
                     env_level=env.get("env_level", ""),
                     env_score=env.get("env_score", 0),
                     position_advice=env.get("position_advice", ""),
                     hot_sectors_top5=[s["name"] for s in sectors[:5]],
                     n_picks=n_picks,
                     blacklist_sectors=pick_result.get("blacklist_sectors", []),
                     env_flags=env.get("flags", []))
    resp = chat([{"role": "user", "content": prompt}], max_tokens=120)
    _log_call("pick_intro", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def risk_explain(excluded_stocks: list[dict[str, Any]]) -> str:
    """14:00 硬过滤解释"""
    if not excluded_stocks:
        return ""
    reasons = []
    for s in excluded_stocks[:10]:
        reasons.append(
            f"{s.get('code', '')} {s.get('name', '')}: "
            f"涨幅 {s.get('chg_pct', 0):.1f}% ≥ 危险线 OR 振幅 {s.get('amplitude', 0):.1f}% ≥ 8%, 历史上胜率为 0"
        )
    prompt = _render("risk_explain.j2", exclusion_reasons="; ".join(reasons))
    resp = chat([{"role": "user", "content": prompt}], max_tokens=200)
    _log_call("risk_explain", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def param_reason(proposal: dict[str, Any], stats: dict[str, Any]) -> str:
    """调参理由信"""
    prompt = _render("param_reason.j2",
                     param=proposal.get("param", ""),
                     old=proposal.get("old", ""),
                     new=proposal.get("new", ""),
                     in_safe_range="是" if proposal.get("in_safe_range") else "否",
                     stats_summary=_stats_summary_text(stats))
    resp = chat([{"role": "user", "content": prompt}], max_tokens=200)
    _log_call("param_reason", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def judge_proposal(proposal: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    """v9.4: LLM-as-judge 调参评估

    评估单个调参 proposal 是否合理, 返回:
      {"approved": bool, "score": int (0-100), "concerns": [str], "verdict": str}

    失败降级: 失败时 approved=True (不阻断主流程), 跟其他 reasoner 一致。
    """
    import json as _json
    import re as _re
    overall = stats.get("overall", {}) if isinstance(stats, dict) else {}
    by_score = stats.get("by_score", {}) if isinstance(stats, dict) else {}
    safe_range = proposal.get("safe_range") or [proposal.get("old"), proposal.get("new")]
    prompt = _render("proposal_judge.j2",
                     param=proposal.get("param", ""),
                     old=proposal.get("old", ""),
                     new=proposal.get("new", ""),
                     safe_range=safe_range,
                     reason=proposal.get("reason", ""),
                     in_safe_range="是" if proposal.get("in_safe_range") else "否",
                     stats_overall_n=overall.get("n", 0),
                     stats_overall_win_rate=overall.get("win_rate", 0),
                     stats_overall_avg=overall.get("avg", 0),
                     by_score=by_score)
    resp = chat([{"role": "user", "content": prompt}], max_tokens=300)
    _log_call("proposal_judge", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    if not resp["ok"]:
        # 失败降级: 默认通过, 不阻断
        return {"approved": True, "score": 0, "concerns": [f"judge 失败: {resp.get('error', '')}"],
                "verdict": "judge 失败, 走默认通过"}
    content = resp.get("content", "").strip()
    # 尝试从 LLM 输出里抠 JSON
    m = _re.search(r"\{.*\}", content, _re.DOTALL)
    if not m:
        return {"approved": True, "score": 0, "concerns": ["无法解析 judge 输出"],
                "verdict": "解析失败, 走默认通过"}
    try:
        data = _json.loads(m.group(0))
        score = int(data.get("score", 0))
        return {
            "approved": bool(data.get("approved", score >= 60)),
            "score": score,
            "concerns": list(data.get("concerns", [])),
            "verdict": str(data.get("verdict", ""))[:100],
        }
    except (ValueError, TypeError) as e:
        return {"approved": True, "score": 0, "concerns": [f"JSON 解析失败: {e}"],
                "verdict": "解析失败, 走默认通过"}


def weekly_followup(weekly: dict[str, Any], question: str, k: int = 3, max_chars: int = 1000) -> str:
    """v8.3: 用户对周报追问 (如 "为什么这个板块拖累"), 走 RAG + LLM 回答

    复用 retrieve() + format_context(), 跟 with_knowledge / auto_period_explain 一致。
    失败降级: 返回 "" (跟其他 reasoner 函数一致)。
    """
    if not question or not question.strip():
        return ""
    stats = weekly.get("stats", {}) if isinstance(weekly, dict) else {}
    overall = stats.get("overall", {}) if isinstance(stats, dict) else {}
    by_score = stats.get("by_score", {}) if isinstance(stats, dict) else {}
    by_chg = stats.get("by_chg", {}) if isinstance(stats, dict) else {}
    by_sector = stats.get("by_sector", {}) if isinstance(stats, dict) else {}

    sectors_sorted = sorted(by_sector.items(), key=lambda x: x[1].get("avg", 0)) if by_sector else []
    worst = sectors_sorted[:3]
    best = sectors_sorted[-3:][::-1]

    # 检索时把 question + 周报关键数字拼起来, 让 BM25 命中相关心法
    query = f"{question} 胜率 {overall.get('win_rate', 0)}% 拖累板块 {[(n, round(b.get('avg', 0), 2)) for n, b in worst]}"
    hits = retrieve(query, k=k)
    knowledge = format_context(hits, max_chars=max_chars)
    if not knowledge:
        knowledge = "(知识库无相关命中)"

    prompt = _render("weekly_followup.j2",
                     weekly_overall_n=overall.get("n", 0),
                     weekly_overall_win_rate=overall.get("win_rate", 0),
                     weekly_overall_avg=overall.get("avg", 0),
                     worst_sectors=[(n, round(b.get("avg", 0), 2)) for n, b in worst],
                     best_sectors=[(n, round(b.get("avg", 0), 2)) for n, b in best],
                     by_score=by_score,
                     by_chg=by_chg,
                     knowledge=knowledge,
                     question=question)
    resp = chat([{"role": "user", "content": prompt}], max_tokens=250)
    _log_call("weekly_followup", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def auto_period_explain(stats: dict[str, Any], k: int = 3, max_chars: int = 1200) -> str:
    """v7.1: auto 表现差时, 拉 BM25 知识库 + LLM 诊断

    复用 retrieve() + format_context(), 跟 weekly_summary / with_knowledge 走同一套。
    失败降级: 返回 "" (跟其他 reasoner 函数一致)。
    """
    overall = stats.get("overall", {}) if isinstance(stats, dict) else {}
    by_score = stats.get("by_score", {}) if isinstance(stats, dict) else {}
    by_chg = stats.get("by_chg", {}) if isinstance(stats, dict) else {}
    by_sector = stats.get("by_sector", {}) if isinstance(stats, dict) else {}

    # 拖累板块 TOP3 (按 avg pnl 升序)
    sectors_sorted = sorted(by_sector.items(), key=lambda x: x[1].get("avg", 0)) if by_sector else []
    worst = sectors_sorted[:3]

    # 构造多角度 query, 让 BM25 命中不同知识源
    query = (
        f"为什么 auto 策略最近胜率 {overall.get('win_rate', 0)}% "
        f"平均 PnL {overall.get('avg', 0):.2f}% "
        f"拖累板块 {[(n, round(b.get('avg', 0), 2)) for n, b in worst]} "
        f"评分分桶 {by_score} 涨幅分桶 {by_chg}"
    )
    hits = retrieve(query, k=k)
    knowledge = format_context(hits, max_chars=max_chars)
    if not knowledge:
        knowledge = "(知识库无相关命中)"

    prompt = _render("auto_period_explain.j2",
                     knowledge=knowledge,
                     overall_n=overall.get("n", 0),
                     overall_win_rate=overall.get("win_rate", 0),
                     overall_avg=overall.get("avg", 0),
                     by_score=by_score,
                     by_chg=by_chg,
                     worst_sectors=[(n, round(b.get("avg", 0), 2)) for n, b in worst])
    resp = chat([{"role": "user", "content": prompt}], max_tokens=300)
    _log_call("auto_period_explain", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def weekly_summary(weekly: dict[str, Any]) -> str:
    """周报 3 句话 + 洞察"""
    stats = weekly.get("stats", {})
    overall = stats.get("overall", {})
    by_score = stats.get("by_score", {})
    by_chg = stats.get("by_chg", {})
    by_sector = stats.get("by_sector", {})

    sectors_sorted = sorted(by_sector.items(), key=lambda x: x[1].get("avg", 0))
    worst = sectors_sorted[:3]
    best = sectors_sorted[-3:][::-1]

    prompt = _render("weekly_summary.j2",
                     overall_n=overall.get("n", 0),
                     overall_win_rate=overall.get("win_rate", 0),
                     overall_avg=overall.get("avg", 0),
                     by_score=by_score,
                     by_chg=by_chg,
                     worst_sectors=[(n, b) for n, b in worst],
                     best_sectors=[(n, b) for n, b in best])
    resp = chat([{"role": "user", "content": prompt}], max_tokens=400)
    _log_call("weekly_summary", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def empty_day(env: dict[str, Any], n_candidates: int = 0) -> str:
    """空仓日解释"""
    trigger_reason = (
        f"涨幅 3-4% 区间共 {n_candidates} 只, "
        f"均不满足换手/振幅/市值/成交额门槛"
    )
    prompt = _render("empty_day.j2",
                     env_level=env.get("env_level", ""),
                     env_score=env.get("env_score", 0),
                     position_advice=env.get("position_advice", ""),
                     n_candidates=n_candidates,
                     trigger_reason=trigger_reason)
    resp = chat([{"role": "user", "content": prompt}], max_tokens=80)
    _log_call("empty_day", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def anomaly(anomaly_type: str, detail: str) -> str:
    """异常推送解释"""
    prompt = _render("anomaly.j2",
                     anomaly_type=anomaly_type,
                     anomaly_detail=detail,
                     anomaly_time=datetime.now().strftime("%Y-%m-%d %H:%M"))
    resp = chat([{"role": "user", "content": prompt}], max_tokens=120)
    _log_call("anomaly", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""




def with_knowledge(question: str, k: int = 3, max_chars: int = 800) -> str:
    """通用 RAG 问答：先用 BM25 检索知识库，再调 LLM 回答"""
    results = retrieve(question, k=k)
    knowledge = format_context(results, max_chars=max_chars)
    prompt = _render("with_knowledge.j2", knowledge=knowledge, question=question)
    resp = chat([{"role": "user", "content": prompt}], max_tokens=400)
    _log_call("with_knowledge", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""


def answer_question(question: str, recent_picks: list[dict] | None = None,
                    market_env: dict | None = None, k: int = 3,
                    preset_results: list[dict] | None = None) -> str:
    """对话式问答：给 bot 用，结合近期选股 + 大盘 + 知识库

    v12.9: preset_results 允许外部预检索 (避免重复 BM25, 也能拿到 RAG 来源做标注)
            不传 → 内部 retrieve(question, k=k) 自己搜
    """
    if recent_picks is None:
        recent_picks = []
    if market_env is None:
        market_env = {}

    # 摘要近期 picks (避免 prompt 过大)
    picks_text = "\n".join(
        f"- {p.get('date', '')} {p.get('code', '')} {p.get('name', '')} "
        f"评分={p.get('score', 0):.1f} 板块={p.get('sector', '')}"
        for p in recent_picks[:10]
    ) or "（无近期选股）"

    env_text = (
        f"env_score={market_env.get('env_score', '?')}, "
        f"level={market_env.get('env_level', '?')}, "
        f"position={market_env.get('position_advice', '?')}"
    ) if market_env else "（无今日大盘数据）"

    # v12.9: preset 优先, 否则内部 retrieve
    results = preset_results if preset_results is not None else retrieve(question, k=k)
    knowledge = format_context(results, max_chars=800)
    prompt = _render("advisor.j2",
                     recent_picks=picks_text,
                     market_env=env_text,
                     knowledge=knowledge,
                     question=question)
    resp = chat([{"role": "user", "content": prompt}], max_tokens=500)
    _log_call("answer_question", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))
    return resp.get("content", "") if resp["ok"] else ""




def chat_with_session(
    session_id: str,
    question: str,
    recent_picks: list[dict] | None = None,
    market_env: dict | None = None,
    k: int = 3,
) -> str:
    """带 session 记忆的对话

    流程:
    1. 读 session 历史
    2. 拼 messages: [system? no, ...history, current_question]
    3. 调 LLM
    4. 写入 user + assistant 到 history
    """
    from ..engine.knowledge import format_context, retrieve  # 避免循环
    from .client import chat as llm_chat

    history = get_history(session_id)
    # 摘要近期 picks
    picks_text = "\n".join(
        f"- {p.get('date', '')} {p.get('code', '')} {p.get('name', '')} "
        f"score={p.get('score', 0):.1f} sector={p.get('sector', '')}"
        for p in (recent_picks or [])[:10]
    ) or "（无近期选股）"
    env_text = (
        f"env_score={market_env.get('env_score', '?')}, "
        f"level={market_env.get('env_level', '?')}, "
        f"position={market_env.get('position_advice', '?')}"
    ) if market_env else "（无大盘数据）"
    # 知识库
    results = retrieve(question, k=k)
    knowledge = format_context(results, max_chars=600)

    system_prompt = (
        "你是量化选股 agent 的对话助手。\n\n"
        f"【今日 / 近期选股】\n{picks_text}\n\n"
        f"【今日大盘】{env_text}\n\n"
        f"【知识库片段】\n{knowledge}\n\n"
        "要求：简洁回答（≤200字），引用知识库时标注 [来源]。涉及具体数据时直接列数字。不确定就说'不确定'。"
    )

    # 拼 messages: system + 历史 + 当前
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})

    resp = llm_chat(messages, max_tokens=500)
    _log_call("chat_with_session", resp["ok"], resp["latency_ms"], resp.get("error", ""),
              resp.get("usage", {}).get("prompt_tokens", 0),
              resp.get("usage", {}).get("completion_tokens", 0))

    if not resp["ok"]:
        return ""
    answer = resp["content"]
    # 写入历史
    append_turn(session_id, "user", question)
    append_turn(session_id, "assistant", answer)
    return answer


# ─────────── helper ───────────

def _stats_summary_text(stats: dict[str, Any]) -> str:
    overall = stats.get("overall", {})
    by_score = stats.get("by_score", {})
    by_chg = stats.get("by_chg", {})
    lines = [
        f"整体: n={overall.get('n', 0)}, 胜率 {overall.get('win_rate', 0)}%, 平均 {overall.get('avg', 0):.2f}%",
        "按评分: " + ", ".join(f"{k}({v['n']}只,胜率{v['win_rate']}%)" for k, v in by_score.items()),
        "按涨幅: " + ", ".join(f"{k}({v['n']}只,胜率{v['win_rate']}%)" for k, v in by_chg.items()),
    ]
    return "\n".join(lines)
