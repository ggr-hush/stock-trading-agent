"""feishu/admin_cmd.py — v12.9.1 飞书 admin 斜杠命令

不走 LLM, 直接调现有 skill / 查 stage_runs, 节省 token + 响应快。

支持:
  /help      - 帮助列表
  /picks     - 今日选股 (调 get_picks skill)
  /positions - 当前持仓 (调 get_positions skill)
  /status    - agent 运行状态 + 今日 stage 跑过几个
  /reset     - 清空当前 chat 的 session 记忆
  /env       - 大盘情况 (调 get_market_env skill)

权限: sender_id 必须在 config.feishu.admin_user_ids 里
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("feishu.admin_cmd")


# 帮助文本
_HELP = (
        "🤖 **stock-trading-agent 指令**\n\n"
        "/help  帮助\n"
        "/picks 今日选股\n"
        "/positions 当前持仓\n"
        "/env 大盘情况\n"
        "/status 今日 stage 跑过几个 + agent uptime\n"
        "/stage 今日 stage 详情 (含失败信息)\n"
        "/health 今日 LLM 调用统计 (成功率/延迟/Token)\n"
        "/reset 清空本会话记忆\n\n"
        "非 admin 也能: '今日选股' / '禾望电气怎么样' / '缠论怎么看买点' (走 LLM)"
    )


def _is_admin(sender_id: str, config: dict[str, Any]) -> bool:
    admins = set(config.get("feishu", {}).get("admin_user_ids", []) or [])
    if not admins:
        # 配置空 → 宽松模式: 不限制
        return True
    return sender_id in admins


def _status_payload() -> dict[str, Any]:
    """agent uptime + 今日 stage 跑过几个"""
    from ..engine.paper_trader import get_db
    today = datetime.now().strftime("%Y-%m-%d")
    # pid 文件 mtime 当作启动时间
    pid_path = Path("data/agent.pid")
    started_at = "?"
    uptime_s = 0
    if pid_path.exists():
        try:
            started_ts = pid_path.stat().st_mtime
            started_at = datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S")
            uptime_s = int(time.time() - started_ts)
        except Exception:
            pass
    # 今日 stage 跑过
    stages: list[dict] = []
    try:
        conn = get_db()
        for r in conn.execute(
            "SELECT stage, ran_at, ok FROM stage_runs WHERE run_date=? ORDER BY ran_at",
            (today,),
        ).fetchall():
            stages.append({"stage": r[0], "ran_at": r[1], "ok": r[2]})
    except Exception as e:  # noqa: BLE001
        log.warning("stage_runs 查询失败: %s", e)
    ok_count = sum(1 for s in stages if s["ok"])
    fail_count = sum(1 for s in stages if not s["ok"])
    lines = [
        f"**Agent 状态** (今天 {today})",
        f"- 启动时间: {started_at}",
        f"- Uptime: {uptime_s // 3600}h {(uptime_s % 3600) // 60}m",
        f"- Stage 跑过: {len(stages)} 个 (成功 {ok_count}, 失败 {fail_count})",
    ]
    if stages:
        lines.append("\n详情:")
        for s in stages:
            mark = "✅" if s["ok"] else "❌"
            lines.append(f"  {mark} {s['stage']} @ {s['ran_at']}")
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}


def _reset_session(chat_id: str) -> dict[str, Any]:
    """清空当前 chat 的 bot_sessions 多轮记忆"""
    try:
        from ..engine.sessions import reset as _session_reset
        _session_reset(chat_id)
        return {"msg_type": "text", "content": {"text": f"已清空 chat {chat_id[:12]}... 的会话记忆"}}
    except Exception as e:  # noqa: BLE001
        log.warning("session reset 失败: %s", e)
        return {"msg_type": "text", "content": {"text": f"清空失败: {e}"}}


def handle(text: str, sender_id: str, chat_id: str,
           config: dict[str, Any]) -> dict[str, Any] | None:
    """v12.9.1: 处理斜杠命令, 返 card/text dict, 非命令返 None

    返 None 表示不是命令, 让 listener 走原 LLM dispatch 路径
    """
    if not text or not text.startswith("/"):
        return None
    cmd = text.strip().split()[0].lower()  # /picks?date=... → /picks
    # 权限检查
    if not _is_admin(sender_id, config):
        return {"msg_type": "text", "content": {"text": "权限不足 (admin 才能用斜杠命令)"}}
    log.info("admin_cmd: chat=%s sender=%s cmd=%s", chat_id, sender_id, cmd)
    if cmd in ("/help", "/h", "/?"):
        return {"msg_type": "text", "content": {"text": _HELP}}
    if cmd == "/picks":
        from ..engine.skills import call_skill
        return call_skill("get_picks", {}).get("card") or {"msg_type": "text", "content": {"text": "(无结果)"}}
    if cmd == "/positions":
        from ..engine.skills import call_skill
        return call_skill("get_positions", {"status": "open"}).get("card") or {"msg_type": "text", "content": {"text": "(无结果)"}}
    if cmd == "/env":
        from ..engine.skills import call_skill
        return call_skill("get_market_env", {}).get("card") or {"msg_type": "text", "content": {"text": "(无结果)"}}
    if cmd == "/status":
        return _status_payload()
    if cmd == "/reset":
        return _reset_session(chat_id)
    return {"msg_type": "text", "content": {"text": f"未知命令: {cmd} (打 /help 看支持列表)"}}


# ─────────── v12.9.2: /health 和 /stage 命令 ───────────

def _health_payload() -> dict[str, Any]:
    """v12.9.2: 今日 LLM 调用统计 (从 llm_logs 读)

    返: 总调用 / 成功 / 失败 / 平均延迟 / 按 call_site 拆
    """
    from datetime import datetime as _dt
    from ..engine.paper_trader import get_db
    today = _dt.now().strftime("%Y-%m-%d")
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT call_site, success, latency_ms, prompt_tokens, completion_tokens "
            "FROM llm_logs WHERE date(call_at) = ?",
            (today,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("llm_logs 查询失败: %s", e)
        return {"msg_type": "text", "content": {"text": f"读 llm_logs 失败: {e}"}}
    if not rows:
        return {"msg_type": "text",
                "content": {"text": f"**LLM 健康** · {today}\n\n今日暂无 LLM 调用记录"}}
    total = len(rows)
    success = sum(1 for r in rows if r["success"])
    fail = total - success
    avg_latency = sum(r["latency_ms"] or 0 for r in rows) // total if total else 0
    total_prompt = sum(r["prompt_tokens"] or 0 for r in rows)
    total_completion = sum(r["completion_tokens"] or 0 for r in rows)
    # 按 call_site 拆
    site_stats: dict[str, dict[str, int]] = {}
    for r in rows:
        s = r["call_site"] or "?"
        if s not in site_stats:
            site_stats[s] = {"n": 0, "ok": 0, "fail": 0}
        site_stats[s]["n"] += 1
        if r["success"]:
            site_stats[s]["ok"] += 1
        else:
            site_stats[s]["fail"] += 1
    lines = [
        f"**LLM 健康** · {today}",
        f"",
        f"- 总调用: **{total}** 次 (成功 {success} / 失败 {fail})",
        f"- 成功率: **{success * 100 // total if total else 0}%**",
        f"- 平均延迟: **{avg_latency}ms**",
        f"- Token 用量: prompt {total_prompt} + completion {total_completion} = {total_prompt + total_completion}",
        f"",
        f"**按 call_site 拆:**",
    ]
    for s, st in sorted(site_stats.items(), key=lambda x: -x[1]["n"]):
        rate = st["ok"] * 100 // st["n"] if st["n"] else 0
        mark = "✅" if st["fail"] == 0 else "❌"
        lines.append(f"  {mark} {s}: {st['n']} 次 (成功 {st['ok']}, 失败 {st['fail']}, 成功率 {rate}%)")
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}


def _stage_payload() -> dict[str, Any]:
    """v12.9.2: 今日 stage_runs 详情 (含失败 stage 的 error 上下文)
    """
    from datetime import datetime as _dt
    from ..engine.paper_trader import get_db
    today = _dt.now().strftime("%Y-%m-%d")
    try:
        conn = get_db()
        # stage_runs 没有 error 列 (老 schema), 通过 llm_logs 间接看 fail 时调了啥
        runs = conn.execute(
            "SELECT stage, ran_at, ok FROM stage_runs WHERE run_date = ? ORDER BY ran_at",
            (today,),
        ).fetchall()
    except Exception as e:  # noqa: BLE1
        log.warning("stage_runs 查询失败: %s", e)
        return {"msg_type": "text", "content": {"text": f"读 stage_runs 失败: {e}"}}
    if not runs:
        return {"msg_type": "text",
                "content": {"text": f"**Stage 记录** · {today}\n\n今日暂无 stage 跑过"}}
    ok_n = sum(1 for r in runs if r["ok"])
    fail_n = len(runs) - ok_n
    lines = [
        f"**Stage 记录** · {today}",
        f"",
        f"- 总数: **{len(runs)}** (成功 {ok_n}, 失败 {fail_n})",
        f"",
        f"**时间线:**",
    ]
    for r in runs:
        mark = "✅" if r["ok"] else "❌"
        ts = r["ran_at"][-8:] if r["ran_at"] else "?"  # HH:MM:SS
        lines.append(f"  {mark} `{ts}` {r['stage']}")
    # 失败详情: 拉 llm_logs 找 fail 时调了啥
    if fail_n > 0:
        fail_stages = [r["stage"] for r in runs if not r["ok"]]
        try:
            fail_logs = conn.execute(
                "SELECT call_site, error, call_at FROM llm_logs "
                "WHERE date(call_at) = ? AND success = 0 ORDER BY call_at DESC LIMIT 5",
                (today,),
            ).fetchall()
        except Exception:
            fail_logs = []
        if fail_logs:
            lines.append(f"\n**最近 5 条 LLM 失败:**")
            for fl in fail_logs:
                err = (fl["error"] or "")[:60]
                ts = fl["call_at"][-8:] if fl["call_at"] else "?"
                lines.append(f"  ❌ `{ts}` {fl['call_site']}: {err}")
        lines.append(f"\n(失败 stages: {', '.join(fail_stages)})")
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}
