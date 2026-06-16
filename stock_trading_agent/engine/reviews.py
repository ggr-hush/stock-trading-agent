"""engine/reviews.py — v12.A.4 结构化复盘

借鉴 felix-quant `backend/app/services/review_service.py`

公共 API:
  - add_review(date, stock_code, *, stock_name=None, signal_id=None,
               action_taken=False, reason="", result="", summary="",
               tags=[]) -> int (新 id)
  - query_reviews(date=None, stock_code=None, tag=None, action_taken=None,
                  limit=20) -> list[dict]
  - get_review(review_id) -> dict | None
  - update_review(review_id, payload) -> dict | None
  - tag_count(days=30) -> dict[tag, count]  高频复盘标签
  - parse_natural_review(text) -> dict  "加复盘: 002063 止盈 2.5% 早盘冲高" → dict
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from .paper_trader import get_db

_NOW_ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(_NOW_ISO_FMT)


def add_review(
    date: str,
    stock_code: str,
    *,
    stock_name: str | None = None,
    signal_id: int | None = None,
    action_taken: bool = False,
    reason: str = "",
    result: str = "",
    summary: str = "",
    tags: list[str] | None = None,
) -> int:
    """写一条复盘, 返新 id"""
    if not date or not stock_code:
        return 0
    tags_list = tags or []
    conn = get_db()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO reviews
            (date, stock_code, stock_name, signal_id, action_taken, reason, result, summary, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date, stock_code, stock_name, signal_id,
            1 if action_taken else 0,
            reason, result, summary,
            json.dumps(tags_list, ensure_ascii=False),
            now, now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _loads_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def query_reviews(
    date: str | None = None,
    stock_code: str | None = None,
    tag: str | None = None,
    action_taken: bool | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """按 (date, stock_code, tag, action_taken) 查复盘, 按 date DESC + id DESC"""
    clauses: list[str] = []
    params: list[Any] = []
    if date:
        clauses.append("date = ?")
        params.append(date)
    if stock_code:
        clauses.append("stock_code = ?")
        params.append(stock_code)
    if action_taken is not None:
        clauses.append("action_taken = ?")
        params.append(1 if action_taken else 0)
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%\"{tag}\"%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    conn = get_db()
    rows = conn.execute(
        f"SELECT * FROM reviews {where} ORDER BY date DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["tags"] = _loads_tags(d.get("tags"))
        d["action_taken"] = bool(d.get("action_taken"))
        out.append(d)
    return out


def get_review(review_id: int) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["tags"] = _loads_tags(d.get("tags"))
    d["action_taken"] = bool(d.get("action_taken"))
    return d


def update_review(review_id: int, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    current = get_review(review_id)
    if not current:
        return None
    tags = payload.get("tags", current["tags"])
    if isinstance(tags, list):
        tags_json = json.dumps(tags, ensure_ascii=False)
    else:
        tags_json = str(tags or "[]")
    conn = get_db()
    conn.execute(
        """
        UPDATE reviews SET
            reason = ?, result = ?, summary = ?, tags = ?,
            action_taken = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            payload.get("reason", current.get("reason", "")),
            payload.get("result", current.get("result", "")),
            payload.get("summary", current.get("summary", "")),
            tags_json,
            1 if payload.get("action_taken", current.get("action_taken")) else 0,
            _now(), review_id,
        ),
    )
    conn.commit()
    return get_review(review_id)


def tag_count(days: int = 30) -> dict[str, int]:
    """统计最近 N 天高频复盘标签, 返 {tag: count}"""
    from datetime import date as _date, timedelta
    start = (_date.today() - timedelta(days=days)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT tags FROM reviews WHERE date >= ?", (start,)
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for t in _loads_tags(r["tags"]):
            counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


# ─────────── 自然语言解析 ───────────

# 触发: "加复盘: 002063 止盈 2.5% 早盘冲高"
_RE_ADD_REVIEW = re.compile(r"加\s*复\s*盘\s*[:：]\s*(.+)", re.IGNORECASE)
# 提取 6 位股票代码
_RE_CODE = re.compile(r"\b(\d{6})\b")
# 提取盈亏百分比
_RE_PNL = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
# 常见动作词
_ACTION_WORDS = {
    "止盈": "止盈", "止盈出局": "止盈", "出局": "止盈",
    "止损": "止损", "割肉": "止损", "止损出局": "止损",
    "持有": "持有", "继续持有": "持有",
    "加仓": "加仓", "补仓": "加仓",
    "减仓": "减仓",
    "买入": "买入", "建仓": "建仓",
    "卖出": "卖出", "清仓": "清仓",
    "看戏": "观察", "没买": "观察", "未操作": "观察",
}


def parse_natural_review(text: str) -> dict[str, Any] | None:
    """解析 '加复盘: 002063 止盈 2.5% 早盘冲高' → 落库 dict

    返 None 表示解析失败 (让 caller 给友好提示)
    """
    if not text:
        return None
    m = _RE_ADD_REVIEW.search(text.strip())
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None
    # 提取 code
    code_m = _RE_CODE.search(body)
    code = code_m.group(1) if code_m else ""
    if not code:
        return {"error": "未识别股票代码 (需 6 位数字)"}
    # 提取盈亏
    pnl_m = _RE_PNL.search(body)
    pnl_str = f"{pnl_m.group(1)}%" if pnl_m else ""
    # 提取动作 + 标签
    action: str | None = None
    tags: list[str] = []
    for keyword, label in _ACTION_WORDS.items():
        if keyword in body:
            action = label
            tags.append(label)
            break
    # 提取 2-4 字短语作为 tags (粗略: 切分逗号/空格)
    parts = re.split(r"[,，;；\s]+", body)
    for p in parts:
        p = p.strip()
        if p and 2 <= len(p) <= 8 and p != action and not _RE_CODE.match(p) and not _RE_PNL.match(p):
            # 过滤 "止盈" 之类已加的
            if p not in tags and not any(k in p for k in _ACTION_WORDS):
                tags.append(p)
    tags = list(dict.fromkeys(tags))[:6]  # 去重 + 限 6 个
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "stock_code": code,
        "action_taken": action not in (None, "观察"),
        "result": pnl_str,
        "summary": body,
        "tags": tags,
    }
