"""agent/dedup_cli.py — v12.8 dup skip 计数器查询/重置

CLI: agent dedup stats / reset
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

_STATS_PATH = Path(__file__).parent.parent.parent / "data" / "dedup_stats.json"


def _load_stats() -> dict:
    if not _STATS_PATH.exists():
        return {"date": datetime.now().strftime("%Y-%m-%d"), "today_count": 0, "recent_5min": []}
    try:
        return json.loads(_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"date": datetime.now().strftime("%Y-%m-%d"), "today_count": 0, "recent_5min": []}


def cmd_stats() -> int:
    """打印今日累计 + 最近 5min 窗口 dup skip 计数"""
    s = _load_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    if s.get("date") != today:
        # 跨天自动归零
        s = {"date": today, "today_count": 0, "recent_5min": []}
    recent_5min = len(s.get("recent_5min", []))
    print(json.dumps({
        "date": s.get("date"),
        "today_total": s.get("today_count", 0),
        "last_5min": recent_5min,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_reset() -> int:
    """清空计数器"""
    _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh = {"date": datetime.now().strftime("%Y-%m-%d"), "today_count": 0, "recent_5min": []}
    _STATS_PATH.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    print("✅ dedup stats reset")
    return 0


def dispatch(action: str) -> int:
    if action == "stats":
        return cmd_stats()
    if action == "reset":
        return cmd_reset()
    print(f"未知 action: {action}, 支持: stats / reset", file=sys.stderr)
    return 2
