"""test_v12_9_2_ops.py — v12.9.2 仪表盘 + 可观测 + retry

Covers:
  - #15 admin_cmd /stage 返 stage_runs 今日时间线 (含失败)
  - #18 admin_cmd /health 返 llm_logs 今日统计 (成功率/延迟/Token)
  - #18 admin_cmd /health 今日 0 调用返友好提示
  - #19 关键 stage 失败 retry 1 次
  - #19 非关键 stage 失败不 retry
  - #19 重试仍失败 → 记 ok=False 且 result.retried=True
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _make_conn_with_tables():
    """建一个内存 sqlite, 含 stage_runs + llm_logs + paper_account (mark_stage_run 也要)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE stage_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage TEXT NOT NULL,
        run_date TEXT NOT NULL,
        ran_at TEXT NOT NULL,
        ok INTEGER NOT NULL,
        UNIQUE(stage, run_date)
    );
    CREATE TABLE llm_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_at TEXT NOT NULL,
        call_site TEXT NOT NULL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        latency_ms INTEGER,
        success INTEGER NOT NULL,
        error TEXT,
        tool_name TEXT,
        tool_args TEXT,
        chat_id TEXT
    );
    CREATE TABLE paper_account (
        id INTEGER PRIMARY KEY, initial_capital REAL NOT NULL, cash REAL NOT NULL, updated_at TEXT NOT NULL
    );
    """)
    return conn


# ─────────── #15 /stage ───────────

def test_stage_payload_shows_today_runs() -> None:
    """_stage_payload 返今日 stage 时间线"""
    from stock_trading_agent.feishu import admin_cmd
    conn = _make_conn_with_tables()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("INSERT INTO stage_runs(stage, run_date, ran_at, ok) VALUES (?, ?, ?, ?)",
                 ("pick", today, f"{today}T14:00:00", 1))
    conn.execute("INSERT INTO stage_runs(stage, run_date, ran_at, ok) VALUES (?, ?, ?, ?)",
                 ("evening", today, f"{today}T19:00:00", 1))
    conn.commit()
    with patch("stock_trading_agent.engine.paper_trader.get_db", return_value=conn):
        r = admin_cmd._stage_payload()
    text = r["content"]["text"]
    assert "**Stage 记录**" in text
    assert "总数: **2**" in text
    assert "pick" in text
    assert "evening" in text
    print("  PASS test_stage_payload_shows_today_runs")


def test_stage_payload_handles_empty() -> None:
    """今日 0 stage 返友好提示"""
    from stock_trading_agent.feishu import admin_cmd
    conn = _make_conn_with_tables()
    with patch("stock_trading_agent.engine.paper_trader.get_db", return_value=conn):
        r = admin_cmd._stage_payload()
    text = r["content"]["text"]
    assert "暂无 stage 跑过" in text
    print("  PASS test_stage_payload_handles_empty")


# ─────────── #18 /health ───────────

def test_health_payload_aggregates_today() -> None:
    """_health_payload 统计今日 llm_logs: 总数/成功率/平均延迟/按 site 拆"""
    from stock_trading_agent.feishu import admin_cmd
    conn = _make_conn_with_tables()
    today = datetime.now().strftime("%Y-%m-%d")
    # 3 success + 1 fail
    for site, ok, lat in [("tool_use_router", 1, 200), ("tool_use_router", 1, 300),
                          ("answer_question", 1, 1500), ("answer_question", 0, 50)]:
        conn.execute(
            "INSERT INTO llm_logs(call_at, call_site, latency_ms, success, prompt_tokens, completion_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"{today}T10:00:00", site, lat, ok, 100, 50),
        )
    conn.commit()
    with patch("stock_trading_agent.engine.paper_trader.get_db", return_value=conn):
        r = admin_cmd._health_payload()
    text = r["content"]["text"]
    assert "总调用: **4**" in text
    assert "成功 3 / 失败 1" in text
    assert "成功率: **75%**" in text
    assert "answer_question" in text
    assert "tool_use_router" in text
    print("  PASS test_health_payload_aggregates_today")


def test_health_payload_handles_empty() -> None:
    """今日 0 调用返友好提示"""
    from stock_trading_agent.feishu import admin_cmd
    conn = _make_conn_with_tables()
    with patch("stock_trading_agent.engine.paper_trader.get_db", return_value=conn):
        r = admin_cmd._health_payload()
    text = r["content"]["text"]
    assert "暂无 LLM 调用" in text
    print("  PASS test_health_payload_handles_empty")


# ─────────── #19 stage retry ───────────

def test_stage_retry_on_failure() -> None:
    """关键 stage 失败时 retry 1 次, 第二次成功 → ok=True"""
    from stock_trading_agent.agent import stages
    import time

    call_count = {"n": 0}

    def fake_stage():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("network 抖")
        return {"ok": True}

    conn = _make_conn_with_tables()
    with patch.object(stages, "is_trading_day", return_value=True), \
         patch.object(stages, "load_config", return_value={}), \
         patch("time.sleep", return_value=None) as mock_sleep, \
         patch("stock_trading_agent.engine.paper_trader.get_db", return_value=conn), \
         patch("stock_trading_agent.agent.stages.mark_stage_run") as mock_mark:
        # 包装饰器
        wrapped = stages._with_stage_run_logging("pick")(fake_stage)
        result = wrapped()

    assert call_count["n"] == 2, f"应调 2 次, 实际 {call_count['n']}"
    assert result == {"ok": True}
    assert mock_sleep.called, "应 sleep 30s 重试"
    assert mock_sleep.call_args[0][0] == 30
    mock_mark.assert_called_with("pick", ok=True)
    print("  PASS test_stage_retry_on_failure")


def test_stage_no_retry_for_non_critical() -> None:
    """非关键 stage (open_auction) 失败不 retry"""
    from stock_trading_agent.agent import stages

    call_count = {"n": 0}

    def fake_stage():
        call_count["n"] += 1
        raise RuntimeError("boom")

    with patch("time.sleep", return_value=None) as mock_sleep:
        wrapped = stages._with_stage_run_logging("open_auction")(fake_stage)
        result = wrapped()

    assert call_count["n"] == 1, f"非关键 stage 不应 retry, 实际 {call_count['n']}"
    assert not mock_sleep.called
    assert result["ok"] is False
    assert result.get("retried") is False
    print("  PASS test_stage_no_retry_for_non_critical")


def test_stage_retry_still_fails_records_ok_false() -> None:
    """关键 stage 重试仍失败 → ok=False + retried=True"""
    from stock_trading_agent.agent import stages

    def fake_stage():
        raise RuntimeError("永久失败")

    with patch("time.sleep", return_value=None), \
         patch("stock_trading_agent.agent.stages.mark_stage_run") as mock_mark:
        wrapped = stages._with_stage_run_logging("evening")(fake_stage)
        result = wrapped()

    assert result["ok"] is False
    assert result["retried"] is True
    assert "永久失败" in result["error"]
    mock_mark.assert_called_with("evening", ok=False)
    print("  PASS test_stage_retry_still_fails_records_ok_false")


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            fail += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            fail += 1
    print(f"\n{'✓' if fail == 0 else '✗'} {len(tests) - fail}/{len(tests)} tests passed")
    sys.exit(0 if fail == 0 else 1)
