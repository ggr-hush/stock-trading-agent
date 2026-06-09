"""
sessions.py — Bot 多轮对话 session 记忆
- session_id = chat_id (飞书群/私聊) 或 user-defined
- history = [{role, content, ts}, ...], 最大 20 条
- TTL = 24h 不活跃清空
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .data_fetcher import load_config
from .paper_trader import get_db

MAX_HISTORY_TURNS = 20
TTL_HOURS = 24

# ─── 可选 Fernet 加密 (从 BOT_ENCRYPTION_KEY env 读) ───
_FERNET = None
_ENCRYPTION_ENABLED = False


def _init_encryption() -> None:
    """懒加载 Fernet, 根据 config.session.encryption 决定是否启用"""
    global _FERNET, _ENCRYPTION_ENABLED
    if _FERNET is not None:
        return
    try:
        cfg = load_config()
        sess_cfg = cfg.get("session", {})
    except Exception:
        sess_cfg = {}
    if sess_cfg.get("encryption", "off") != "fernet":
        return
    key_env = sess_cfg.get("encryption_key_env", "BOT_ENCRYPTION_KEY")
    key = os.environ.get(key_env)
    if not key:
        log.warning("session.encryption=fernet 但 %s 未设置, 回退明文", key_env)
        return
    try:
        from cryptography.fernet import Fernet  # type: ignore
        _FERNET = Fernet(key.encode() if isinstance(key, str) else key)
        _ENCRYPTION_ENABLED = True
        log.info("session 加密已启用 (Fernet)")
    except ImportError:
        log.warning("cryptography 未装, session 回退明文")
    except Exception as e:
        log.warning("Fernet 初始化失败 (%s), session 回退明文", e)


def _encrypt(text: str) -> str:
    _init_encryption()
    if not _ENCRYPTION_ENABLED or _FERNET is None:
        return text
    return _FERNET.encrypt(text.encode()).decode()


def _decrypt(blob: str) -> str:
    _init_encryption()
    if not _ENCRYPTION_ENABLED or _FERNET is None or not blob:
        return blob
    # 判断是否 Fernet 格式 (base64 以 gAAAAA 开头)
    if not blob.startswith("gAAAAA"):
        return blob
    try:
        return _FERNET.decrypt(blob.encode()).decode()
    except Exception:
        return blob  # 失败时回退明文 (兼容老数据)


import os  # 上面用了 os.environ
import logging
log = logging.getLogger("engine.sessions")


def _parse_history(raw: Any) -> list[dict[str, str]]:
    """SQLite 存的是 JSON 字符串, 但 row_factory 可能已经 parse 成 list; 兼容两种"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def _get_or_create(session_id: str) -> dict[str, Any]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM bot_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    now = datetime.now().isoformat()
    if row:
        # 检查 TTL
        try:
            last = datetime.fromisoformat(row["last_active"])
        except Exception:
            last = datetime.now()
        # 读 ttl_hours 从 config (向后兼容)
        try:
            cfg = load_config()
            ttl_h = cfg.get("session", {}).get("ttl_hours", TTL_HOURS)
        except Exception:
            ttl_h = TTL_HOURS
        if datetime.now() - last > timedelta(hours=ttl_h):
            conn.execute(
                "UPDATE bot_sessions SET history='[]', turn_count=0, last_active=? WHERE session_id=?",
                (now, session_id),
            )
            conn.commit()
            return {"session_id": session_id, "history": [], "turn_count": 0, "last_active": now}
        d = dict(row)
        raw_history = d.get("history")
        d["history"] = _parse_history(_decrypt(raw_history) if isinstance(raw_history, str) else raw_history)
        return d
    conn.execute(
        "INSERT INTO bot_sessions (session_id, last_active, history, turn_count) VALUES (?, ?, '[]', 0)",
        (session_id, now),
    )
    conn.commit()
    return {"session_id": session_id, "history": [], "turn_count": 0, "last_active": now}


def get_history(session_id: str) -> list[dict[str, str]]:
    """读历史 (不含当前轮)"""
    s = _get_or_create(session_id)
    return _parse_history(s.get("history"))


def append_turn(session_id: str, role: str, content: str) -> list[dict[str, str]]:
    """追加一轮 (user/assistant), 自动 trim 到 MAX_HISTORY_TURNS"""
    s = _get_or_create(session_id)
    history = _parse_history(s.get("history"))
    history.append({"role": role, "content": content, "ts": datetime.now().isoformat()})
    # Trim 用 config 里的 max_history_turns
    try:
        cfg = load_config()
        max_turns = cfg.get("session", {}).get("max_history_turns", MAX_HISTORY_TURNS)
    except Exception:
        max_turns = MAX_HISTORY_TURNS
    if len(history) > max_turns * 2:  # 一轮 = user+assistant 两条
        history = history[-(max_turns * 2):]
    now = datetime.now().isoformat()
    conn = get_db()
    blob = _encrypt(json.dumps(history, ensure_ascii=False))
    conn.execute(
        "UPDATE bot_sessions SET history=?, turn_count=turn_count+1, last_active=? WHERE session_id=?",
        (blob, now, session_id),
    )
    conn.commit()
    return history


def reset(session_id: str) -> None:
    """清空 session"""
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE bot_sessions SET history='[]', turn_count=0, last_active=? WHERE session_id=?",
        (now, session_id),
    )
    conn.commit()


def list_active(limit: int = 50) -> list[dict[str, Any]]:
    """列出活跃 sessions (调试用)"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM bot_sessions ORDER BY last_active DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
