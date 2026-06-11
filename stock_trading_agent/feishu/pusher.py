"""
feishu/pusher.py — 飞书卡片推送
6 个节点的卡片模板 + webhook / chat API 封装
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

from ..engine.data_fetcher import _secret, is_placeholder, load_env
from ._http import http_post

log = logging.getLogger("feishu.pusher")
# v12: 防御性 strip <think>...</think> 块 (minimax M3 等推理模型在 weekly_summary / pick_intro 等注入)
# 在 _send 入口剥一次, 6 个 push_xxx 都自动覆盖
import re as _re
_PUSHER_THINK_RE = _re.compile(r"<think>.*?</think>", _re.DOTALL)
_PUSHER_BARE_CLOSE_RE = _re.compile(r"</think>", _re.IGNORECASE)


def _strip_think_for_push(text: str) -> str:
    """pusher 专用 think 块剥除 (处理多个块 / 裸闭合 / 未闭合)"""
    if not text or ("<think>" not in text and "</think>" not in text):
        return text
    cleaned = _PUSHER_THINK_RE.sub("", text)
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>", 1)[0]
    cleaned = _PUSHER_BARE_CLOSE_RE.sub("", cleaned)
    return cleaned.strip()


# ─────────── 发送通道 ───────────

def _send_webhook(text: str) -> dict[str, Any]:
    """通过 BITABLE_WEBHOOK 发送 markdown 文本

    缺凭据时返回 ok=False (而不是抛异常), 跟 _send_via_app 行为一致,
    让 _send 分发器可以平滑回退。
    """
    env = load_env()
    url = env.get("FEISHU_BITABLE_WEBHOOK", "")
    if is_placeholder(url):
        url = ""
    if not url:
        return {"ok": False, "error": "FEISHU_BITABLE_WEBHOOK 未设", "channel": "webhook"}
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        r = http_post(url, json=payload, timeout=10)
        return {"ok": r.status_code == 200, "status": r.status_code, "body": r.text[:200], "channel": "webhook"}
    except Exception as e:
        log.warning("_send_webhook 失败: %s: %s", type(e).__name__, str(e)[:200])
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "channel": "webhook"}



def _send_via_app(text: str, msg_type: str = "text", chat_id: Optional[str] = None) -> dict[str, Any]:
    """通过 OpenAPI app (tenant_access_token) 发文本到 chat_id

    区别于 _send_webhook: 走 im/v1/messages, 可发到任意 chat, 支持多种 msg_type。
    """
    env = load_env()
    app_id = env.get("FEISHU_APP_ID", "")
    app_secret = env.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        return {"ok": False, "error": "FEISHU_APP_ID/SECRET 未设, 无法走 app 通道", "channel": "app"}
    if not chat_id:
        chat_id = env.get("FEISHU_CHAT_ID", "")
    if not chat_id:
        return {"ok": False, "error": "FEISHU_CHAT_ID 未设, 无法走 app 通道", "channel": "app"}
    try:
        tok_r = http_post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=8,
        )
        tok_data = tok_r.json()
        if tok_data.get("code") != 0:
            return {"ok": False, "error": f"tenant_access_token 失败: {tok_data.get('msg')}", "channel": "app"}
        token = tok_data.get("tenant_access_token", "")
        r = http_post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            timeout=10,
        )
        resp = r.json() if r.text else {}
        ok = r.status_code == 200 and resp.get("code", 0) == 0
        return {"ok": ok, "status": r.status_code, "channel": "app",
                "body": (resp.get("msg") or r.text[:200])}
    except Exception as e:
        log.warning("_send_via_app 失败: %s: %s", type(e).__name__, str(e)[:200])
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "channel": "app"}


def _send(text: str, msg_type: str = "text", chat_id: Optional[str] = None) -> dict[str, Any]:
    """统一分发器: 按 FEISHU_PUSH_MODE 选通道。

    mode:
      - "app"     : 强制走 app OpenAPI (要 APP_ID/SECRET/CHAT_ID)
      - "webhook" : 强制走 BITABLE_WEBHOOK (老的自定机器人)
      - "auto"    : 优先 app, 缺任一自动回退 webhook (默认)

    返回: {"ok": bool, "channel": "app"|"webhook", ...}

    v12: 入口剥 <think>...</think> 块, 6 个 push_xxx 自动覆盖 (避免 LLM chain-of-thought 泄漏)
    """
    if isinstance(text, str):
        text = _strip_think_for_push(text)
    mode = load_env().get("FEISHU_PUSH_MODE", "auto").lower()
    if mode not in ("app", "webhook", "auto"):
        mode = "auto"

    if mode == "webhook":
        res = _send_webhook(text)
        res["channel"] = "webhook"
        return res

    if mode in ("app", "auto"):
        env = load_env()
        app_ready = all(not is_placeholder(env.get(k)) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"))
        if app_ready:
            return _send_via_app(text, msg_type=msg_type, chat_id=chat_id)
        if mode == "app":
            return {"ok": False, "error": "FEISHU_PUSH_MODE=app 但 APP_ID/SECRET/CHAT_ID 未齐",
                    "channel": "app"}
        # auto 模式无 app → 回退 webhook

    res = _send_webhook(text)
    res["channel"] = "webhook"
    return res


# ─────────── 6 个节点卡片 ───────────

def push_pre_market(yesterday_summary: dict[str, Any]) -> dict[str, Any]:
    """08:30 盘前复盘卡片"""
    text = (
        f"## 🌅 盘前复盘 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"- 昨日选股数: {yesterday_summary.get('pick_count', 0)}\n"
        f"- Paper 累计 PnL: {yesterday_summary.get('paper_total', {}).get('total_pnl_pct', 0):.2f}%\n"
        f"- Paper 胜率: {yesterday_summary.get('paper_total', {}).get('win_rate', 0)}%\n"
    )
    return _send(text)


def push_pick(pick_result: dict[str, Any], intro: str = "") -> dict[str, Any]:
    """14:00 选股卡片"""
    date = pick_result["date"]
    plan = pick_result["plan_used"]
    env = pick_result["market_env"]
    stocks = pick_result["filtered_stocks"]
    lines = [
        f"## 🌙 尾盘选股 · {date}",
        "",
        f"> {intro}" if intro else "",
        f"- 方案: **{plan}** ({'空仓' if plan == 'C' else f'{len(stocks)} 只'})",
        f"- 大盘: {env['env_level']} (env_score={env['env_score']}, 仓位 {env['position_advice']})",
        f"- 主线 TOP3: " + ", ".join(s["name"] for s in pick_result["sectors"][:3]),
        "",
        "| 代码 | 名称 | 涨幅 | 换手 | 评分 | 板块 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for s in stocks[:10]:
        lines.append(
            f"| {s['code']} | {s['name']} | {s.get('chg_pct', 0):.2f}% "
            f"| {s.get('turnover', 0):.1f}% | {s.get('score', 0):.1f} | {s.get('sector', '')} |"
        )
    return _send("\n".join(l for l in lines if l is not None))


def push_risk_explain(excluded: list[dict[str, Any]], explain: str = "") -> dict[str, Any]:
    """14:00 硬过滤/风控解释"""
    lines = [
        f"## 🛡 风控报告 · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"今日硬过滤剔除 {len(excluded)} 只",
    ]
    if explain:
        lines += ["", f"> {explain}"]
    return _send("\n".join(lines))


def push_empty_day(reason: str, env: dict[str, Any]) -> dict[str, Any]:
    """14:00 空仓日"""
    text = (
        f"## 🔴 空仓日 · {datetime.now().strftime('%Y-%m-%d')}\n\n"
        f"**原因**: {reason}\n\n"
        f"- 大盘: {env['env_level']} (score={env['env_score']})\n"
        f"- 仓位建议: {env['position_advice']}\n"
    )
    return _send(text)


def push_evening(daily_review: dict[str, Any], summary: str = "") -> dict[str, Any]:
    """19:00 晚间日报"""
    text = (
        f"## 📊 晚间日报 · {daily_review['date']}\n\n"
        f"> {summary}" if summary else "",
        f"- 选股数: {daily_review['pick_count']}",
        f"- Paper 累计: PnL {daily_review['paper_total']['total_pnl_pct']:.2f}%, "
        f"胜率 {daily_review['paper_total']['win_rate']}%",
    )
    return _send("\n".join(t for t in text if t))


def push_post_market(filled_count: int, fill_type: str, picks: list[dict[str, Any]]) -> dict[str, Any]:
    """15:30 盘后对账（实盘手单对账提醒）"""
    text = (
        f"## 💰 盘后对账 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"- 今日 {fill_type} 模拟成交: {filled_count} 笔\n"
        f"- 请检查你的实盘手单是否一致（不一致请回复此消息记录差异）\n"
    )
    if picks:
        text += "\n| 代码 | 名称 | 模拟价 | 建议实盘 | 差异 |\n|---|---|---:|---:|---:|\n"
        for s in picks[:10]:
            text += f"| {s.get('code', '')} | {s.get('name', '')} | - | - | - |\n"
    return _send(text)


def push_weekly(weekly: dict[str, Any], llm_summary: str = "") -> dict[str, Any]:
    """周日 20:00 周报"""
    stats = weekly.get("stats", {})
    overall = stats.get("overall", {})
    applied = weekly.get("applied", [])
    pending = weekly.get("pending", [])
    lines = [
        f"## 📅 周报 · {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"> {llm_summary}" if llm_summary else "",
        f"### 整体表现",
        f"- 样本: {overall.get('n', 0)}",
        f"- 胜率: {overall.get('win_rate', 0)}%",
        f"- 平均 PnL: {overall.get('avg', 0):.2f}%",
        "",
        f"### 调参",
    ]
    if applied:
        lines.append(f"✅ 自动应用 {len(applied)} 项：")
        for a in applied:
            lines.append(f"- `{a['param']}`: {a.get('old', '?')} → {a.get('new', '?')} ({a.get('reason', '')})")
    if pending:
        lines.append(f"⏸ 待确认 {len(pending)} 项（请回复确认）:")
        for p in pending:
            lines.append(f"- `{p['param']}`: {p.get('old', '?')} → {p.get('new', '?')} ({p.get('reason', '')})")
    if not applied and not pending:
        lines.append("本周无参数变动")
    return _send("\n".join(l for l in lines if l is not None))


def push_anomaly(message: str) -> dict[str, Any]:
    """异常推送（数据缺失/接口失败等）"""
    text = f"## ⚠️ 异常 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{message}"
    return _send(text)


# ─────────── v12: 轻主动推送 ───────────

def push_daily_summary(daily_stats: dict[str, Any], paper_positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """v12: 15:30 收盘日报推送 (轻主动)

    Args:
        daily_stats: 来自 run_daily_review(today) 的统计 (paper_total / pick_count 等)
        paper_positions: 当前 paper 持仓列表 (可选, 用于展示今日持仓表现)
    """
    paper_total = daily_stats.get("paper_total", {}) if daily_stats else {}
    pick_count = daily_stats.get("pick_count", 0) if daily_stats else 0
    date_str = daily_stats.get("date", datetime.now().strftime("%Y-%m-%d")) if daily_stats else datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"## 📊 收盘日报 · {date_str}",
        "",
        f"- 今日选股: **{pick_count}** 只",
        f"- Paper 累计 PnL: **{paper_total.get('total_pnl_pct', 0):.2f}%**",
        f"- Paper 胜率: **{paper_total.get('win_rate', 0)}%**",
    ]
    if paper_positions:
        lines += ["", "### 当前持仓"]
        lines += ["| 代码 | 名称 | 状态 | 当日 PnL |", "|---|---|---|---:|"]
        for p in paper_positions[:10]:
            pnl_pct = p.get("pnl_open_pct") or p.get("pnl_noon_pct") or 0
            lines.append(
                f"| {p.get('code', '')} | {p.get('name', '')} "
                f"| {p.get('status', '')} | {pnl_pct:.2f}% |"
            )
    lines += ["", "_(纯数据日报, v13 会加记忆联动)_"]
    return _send("\n".join(lines))


def push_anomaly_recap(intraday_anomalies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """v12: 19:00 当日异动复盘推送 (轻主动)

    Args:
        intraday_anomalies: 盘中监控记录的异动列表 (e.g. [{"code": "600519", "type": "涨停", "time": "..."}])
                           空或 None 时推一句"今日无异动"
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"## 🌙 当日异动复盘 · {date_str}", ""]
    if not intraday_anomalies:
        lines += ["今日无异动, 平稳收盘 ✓"]
    else:
        lines += [f"共 **{len(intraday_anomalies)}** 条异动:", ""]
        lines += ["| 时间 | 代码 | 名称 | 类型 | 幅度 |", "|---|---|---|---|---:|"]
        for a in intraday_anomalies[:20]:
            lines.append(
                f"| {a.get('time', '')} | {a.get('code', '')} "
                f"| {a.get('name', '')} | {a.get('type', '')} "
                f"| {a.get('change', '')} |"
            )
    lines += ["", "_(每日 19:00 固定推送, v13 会加记忆联动)_"]
    return _send("\n".join(lines))
