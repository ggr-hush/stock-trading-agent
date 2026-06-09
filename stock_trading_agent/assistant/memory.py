"""memory.py — v12 用户记忆层

职责:
  - user_profile: 稳定偏好 (chat_id → prefs_json), 永久
  - memories    : 情景记忆 (chat_id → [{type, content, importance, ...}]), 带 TTL

写入触发 (regex 起步, v12 不调 LLM 二分类):
  - "记住" "别忘" "记一下" → explicit
  - "我不喜欢" "我不要" "别推" → preference
  - "我偏好" "我喜欢" "以后默认" → preference
  - "我刚买了" "我关注" "我持有" → decision/fact

读取:
  - build_memory_context(chat_id) → 一段拼到 system prompt 末尾的 string
  - 默认 90 天 TTL, 按 chat_id 过滤

公共 API:
  - remember(chat_id, content, type=...) → bool
  - get_profile(chat_id) → dict
  - set_pref(chat_id, key, value) → None
  - list_memories(chat_id, limit=20) → list[dict]
  - clear_memories(chat_id) → int (清掉几条)
  - build_memory_context(chat_id) → str (拼成 system prompt 段)
  - detect_memory_signal(text) → tuple[type, content] | None
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from ..engine.paper_trader import get_db

DEFAULT_TTL_DAYS = 90
DEFAULT_IMPORTANCE = 1

# ─────────── 偏好信号检测 (regex) ───────────

# 显式记忆信号
_RE_EXPLICIT = re.compile(r"(记住|别忘|记一下|记着|备忘)", re.IGNORECASE)
# 偏好信号 (负向)
_RE_PREF_NEG = re.compile(r"我(不喜欢?|不要|不想|别推|避开?)([^。.!?\\n,，]{1,40})", re.IGNORECASE)
# 偏好信号 (正向)
_RE_PREF_POS = re.compile(r"我(偏好?|喜欢|常?看|常?买)([^。.!?\\n,，]{1,40})", re.IGNORECASE)
# 决策/事实 (持仓/关注)
_RE_DECISION = re.compile(r"我(刚?[买持有关注]|持有?了?|关注?了?|买了?)([^。.!?\\n,，]{1,40})", re.IGNORECASE)


def detect_memory_signal(text: str) -> Optional[tuple[str, str, str]]:
    """检测文本里有没有"想被记住"的意思

    Returns:
        None                           → 没检测到
        (type, content, source)        → type ∈ {preference, decision, fact, explicit}
        source ∈ {explicit, detected}
    """
    if not text or not text.strip():
        return None
    s = text.strip()
    # 显式: "记住 XXX"
    if _RE_EXPLICIT.search(s):
        return ("explicit", s, "explicit")
    # 偏好
    m = _RE_PREF_NEG.search(s)
    if m:
        return ("preference", f"不喜欢{m.group(2).strip()}", "detected")
    m = _RE_PREF_POS.search(s)
    if m:
        return ("preference", f"偏好{m.group(2).strip()}", "detected")
    # 决策/事实
    m = _RE_DECISION.search(s)
    if m:
        verb = m.group(1)
        obj = m.group(2).strip()
        if any(k in verb for k in ("关注",)):
            return ("fact", f"关注{obj}", "detected")
        return ("decision", f"{verb}{obj}", "detected")
    return None


# ─────────── 读写 ───────────

def remember(
    chat_id: str,
    content: str,
    *,
    type: str = "fact",
    importance: int = DEFAULT_IMPORTANCE,
    ttl_days: int = DEFAULT_TTL_DAYS,
    source: str = "explicit",
) -> int:
    """写一条 memory, 返回新 id

    Args:
        chat_id: 飞书 chat_id (群/私聊)
        content: 记忆内容 (e.g. "不喜欢银行股")
        type: preference | fact | decision | interaction | explicit
        importance: 1-3, 默认 1
        ttl_days: 默认 90 天
        source: user | detected | explicit
    """
    if not chat_id or not content or not content.strip():
        return 0
    conn = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO memories (chat_id, type, content, importance, created_at, ttl_days, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, type, content.strip(), max(1, min(3, importance)), now, ttl_days, source),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_memories(chat_id: str, limit: int = 20, include_expired: bool = False) -> list[dict[str, Any]]:
    """列一个 chat_id 的记忆 (按 importance DESC + created_at DESC)

    include_expired=False: 自动过滤 ttl > ttl_days 的
    """
    if not chat_id:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE chat_id=? ORDER BY importance DESC, created_at DESC LIMIT ?",
        (chat_id, limit * 2),  # 多取一倍, 客户端再 filter
    ).fetchall()
    out: list[dict[str, Any]] = []
    now = datetime.now()
    for r in rows:
        d = dict(r)
        if not include_expired:
            try:
                created = datetime.fromisoformat(d["created_at"])
                if (now - created) > timedelta(days=d.get("ttl_days", DEFAULT_TTL_DAYS)):
                    continue
            except Exception:
                pass
        out.append(d)
        if len(out) >= limit:
            break
    return out


def clear_memories(chat_id: str) -> int:
    """清掉一个 chat_id 的所有 memory, 返回删了几条"""
    if not chat_id:
        return 0
    conn = get_db()
    cur = conn.execute("DELETE FROM memories WHERE chat_id=?", (chat_id,))
    conn.commit()
    return cur.rowcount


def get_profile(chat_id: str) -> dict[str, Any]:
    """读 user_profile.prefs_json, 不存在返回 {}"""
    if not chat_id:
        return {}
    conn = get_db()
    row = conn.execute(
        "SELECT prefs_json FROM user_profile WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if not row:
        return {}
    raw = row["prefs_json"]
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def set_pref(chat_id: str, key: str, value: Any) -> None:
    """写一条 stable pref (e.g. set_pref(chat_id, "no_sectors", ["银行"]))"""
    if not chat_id or not key:
        return
    conn = get_db()
    profile = get_profile(chat_id)
    profile[key] = value
    now = datetime.now().isoformat(timespec="seconds")
    blob = json.dumps(profile, ensure_ascii=False)
    conn.execute(
        "INSERT INTO user_profile (chat_id, prefs_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET prefs_json=excluded.prefs_json, updated_at=excluded.updated_at",
        (chat_id, blob, now),
    )
    conn.commit()


def build_memory_context(chat_id: str, max_chars: int = 1200) -> str:
    """拼成 system prompt 末尾的 "用户记忆" 段

    结构:
      用户记忆 (90 天内):
      - [preference] 不喜欢银行股
      - [fact] 关注 600519
      ...

    超出 max_chars 自动截断
    """
    if not chat_id:
        return ""
    mems = list_memories(chat_id, limit=30)
    if not mems:
        return ""
    lines = ["用户记忆 (近 90 天, 重要度从高到低):"]
    total = len(lines[0])
    for m in mems:
        line = f"- [{m.get('type', '?')}] {m.get('content', '')}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
