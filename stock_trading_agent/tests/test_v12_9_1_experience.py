"""test_v12_9_1_experience.py — v12.9.1 体验优化包

Covers:
  - #1 explain_pick 实时行情兜底 (picks 找不到 → fetch_realtime_quote)
  - #2 stage_post_market 真实计算 (picks+positions)
  - #3 is_trading_day 节假日常量
  - #4 card 模板 (picks/positions/explain 都返 interactive)
  - #5 admin 斜杠命令 (handle 分发 + 权限)
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── #3 节假日常量 ───────────

def test_is_trading_day_weekday_no_holiday() -> None:
    from stock_trading_agent.engine.data_fetcher import is_trading_day
    assert is_trading_day(date(2026, 6, 12)) is True, "周五非假日应返 True"
    print("  PASS test_is_trading_day_weekday_no_holiday")


def test_is_trading_day_holidays_blocked() -> None:
    """v12.A.4.c: 走 Tushare trade_cal, mock Tushare trade_cal 返真实 holiday 数据"""
    from unittest.mock import patch, MagicMock
    import pandas as pd
    from stock_trading_agent.engine.data_fetcher import is_trading_day
    # 模拟 Tushare 返的 trade_cal 数据 (is_open=0 是节假日)
    fake_trade_cal = pd.DataFrame({
        "cal_date": ["20261001", "20260217", "20260619"],  # 国庆/春节/端午
        "is_open": [0, 0, 0],
    })
    # _load_trade_cal 会调 _safe_df 内部调 tushare trade_cal, 返 DataFrame
    # mock _safe_df 返 fake (因为 _load_trade_cal 内部 from ... import _safe_df)
    with patch("stock_trading_agent.engine.tushare_client._safe_df", return_value=fake_trade_cal):
        # 还得 mock 缓存写入, 用 _TRADE_CAL_CACHE_DIR 临时目录
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("stock_trading_agent.engine.data_fetcher._TRADE_CAL_CACHE_DIR", Path(tmpdir)):
                # 第一次调用会调 _safe_df, 写缓存, 返 set
                assert is_trading_day(date(2026, 10, 1)) is False, "国庆应返 False"
                assert is_trading_day(date(2026, 2, 17)) is False, "春节应返 False"
                assert is_trading_day(date(2026, 6, 19)) is False, "端午应返 False"
    print("  PASS test_is_trading_day_holidays_blocked")


def test_is_trading_day_weekend_blocked() -> None:
    from stock_trading_agent.engine.data_fetcher import is_trading_day
    assert is_trading_day(date(2026, 6, 13)) is False, "周六应返 False"
    assert is_trading_day(date(2026, 6, 14)) is False, "周日应返 False"
    print("  PASS test_is_trading_day_weekend_blocked")


# ─────────── #1 explain_pick 兜底 ───────────

def test_explain_pick_fallback_to_realtime_quote() -> None:
    """picks 找不到 + 实时拉到 → LLM 用实时数据回答 + [数据源: 东方财富实时]"""
    from stock_trading_agent.engine import skills
    from stock_trading_agent.engine import data_fetcher as df

    # 1) picks 表找不到
    with patch.object(skills, 'get_db') as mock_db, \
         patch.object(df, 'fetch_realtime_quote',
                      return_value={"code": "603063", "name": "禾望电气",
                                    "price": 32.5, "chg_pct": 1.2,
                                    "turnover": 2.5, "mktcap_yi": 80,
                                    "source": "tushare"}), \
         patch("stock_trading_agent.llm.reasoner.answer_question",
               return_value="禾望电气今天涨 1.2%, 板块光伏设备活跃, 走势温和."):
        mock_db.return_value.execute.return_value.fetchone.return_value = None
        result = skills._run_explain_pick({"code": "603063"})

    assert result["source"] == "realtime"
    assert result["name"] == "禾望电气"
    assert "[数据源: Tushare" in result["explanation"]
    print("  PASS test_explain_pick_fallback_to_realtime_quote")


def test_explain_pick_fallback_empty_returns_friendly_message() -> None:
    """picks 找不到 + 实时也拉不到 → 友好引导"""
    from stock_trading_agent.engine import skills
    from stock_trading_agent.engine import data_fetcher as df

    with patch.object(skills, 'get_db') as mock_db, \
         patch.object(df, 'fetch_realtime_quote', return_value={}):
        mock_db.return_value.execute.return_value.fetchone.return_value = None
        result = skills._run_explain_pick({"code": "000001"})

    assert result["source"] == "fallback_empty"
    assert "000001" in result["explanation"]
    assert "换个" in result["explanation"] or "试试" in result["explanation"]
    print("  PASS test_explain_pick_fallback_empty_returns_friendly_message")


# ─────────── #2 stage_post_market 真实 ───────────

def test_stage_post_market_counts_filled() -> None:
    """stage_post_market 算 filled_count = picks 中已开仓的"""
    from stock_trading_agent.agent import stages
    from stock_trading_agent.engine import paper_trader

    # mock DB 用 sqlite3.Row (paper_trader 设了 row_factory=sqlite3.Row, dict(r) 才能工作)
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE picks(pick_date TEXT, code TEXT, name TEXT, price REAL, chg_pct REAL, score REAL, sector TEXT, plan_used TEXT)")
    conn.execute("CREATE TABLE paper_positions(pick_date TEXT, code TEXT, name TEXT, open_price REAL, shares REAL, status TEXT)")
    # v12.A.4.c: 改用今天日期 (代码里 _date.today().isoformat() 决定 WHERE pick_date)
    from datetime import date as _date
    today = _date.today().isoformat()
    for row in [
        (today, "600519", "茅台", 1500, 1.0, 80, "白酒", "A"),
        (today, "600036", "招行", 35, 0.5, 75, "银行", "A"),
        (today, "002594", "比亚迪", 250, 2.0, 78, "汽车", "B"),
    ]:
        conn.execute("INSERT INTO picks VALUES (?,?,?,?,?,?,?,?)", row)
    for row in [
        (today, "600519", "茅台", 1499, 100, "open"),
        (today, "600036", "招行", 35, 1000, "open"),
    ]:
        conn.execute("INSERT INTO paper_positions VALUES (?,?,?,?,?,?)", row)
    conn.commit()

    pushed: list[tuple] = []
    def fake_push(filled, fill_type, picks_list):
        pushed.append((filled, fill_type, picks_list))

    with patch.object(paper_trader, 'get_db', return_value=conn), \
         patch.object(stages, 'pusher') as mock_pusher, \
         patch.object(stages, 'log'):
        mock_pusher.push_post_market.side_effect = fake_push
        result = stages.stage_post_market()

    assert result["filled_count"] == 2
    assert result["picks_count"] == 3
    assert len(pushed) == 1
    assert pushed[0][0] == 2  # filled_count
    assert pushed[0][1] == "模拟成交"
    # picks_with_status 标记了 is_filled
    is_filled_codes = {p["code"] for p in pushed[0][2] if p.get("is_filled")}
    assert is_filled_codes == {"600519", "600036"}
    print("  PASS test_stage_post_market_counts_filled")


def test_stage_post_market_empty_picks_skips() -> None:
    """picks 空 → filled_count=0, 不 push"""
    from stock_trading_agent.agent import stages
    from stock_trading_agent.engine import paper_trader

    import sqlite3
    conn_empty = sqlite3.connect(":memory:")
    conn_empty.row_factory = sqlite3.Row
    conn_empty.execute("CREATE TABLE picks(pick_date TEXT, code TEXT, name TEXT, price REAL, chg_pct REAL, score REAL, sector TEXT, plan_used TEXT)")
    conn_empty.execute("CREATE TABLE paper_positions(pick_date TEXT, code TEXT, name TEXT, open_price REAL, shares REAL, status TEXT)")
    conn_empty.commit()
    with patch.object(paper_trader, 'get_db', return_value=conn_empty), \
         patch.object(stages, 'pusher') as mock_pusher:
        result = stages.stage_post_market()

    assert result["skipped"] == "no picks"
    assert result["filled_count"] == 0
    mock_pusher.push_post_market.assert_not_called()
    print("  PASS test_stage_post_market_empty_picks_skips")


# ─────────── #4 card 模板 ───────────

def test_render_picks_card_returns_interactive() -> None:
    from stock_trading_agent.engine.skills import _render_picks_card
    result = _render_picks_card({
        "date": "2026-06-12",
        "count": 2,
        "items": [
            {"code": "600519", "name": "茅台", "score": 80, "chg_pct": 1.0, "sector": "白酒", "plan": "A"},
            {"code": "002594", "name": "比亚迪", "score": 75, "chg_pct": 2.0, "sector": "汽车", "plan": "B"},
        ],
    })
    assert result["msg_type"] == "interactive"
    assert "elements" in result["content"]
    print("  PASS test_render_picks_card_returns_interactive")


def test_render_positions_card_returns_interactive() -> None:
    from stock_trading_agent.engine.skills import _render_positions_card
    result = _render_positions_card({
        "status": "open",
        "count": 1,
        "items": [{"code": "600519", "name": "茅台", "open_price": 1500,
                    "shares": 100, "pnl_open_pct": 1.5, "status": "open"}],
    })
    assert result["msg_type"] == "interactive"
    print("  PASS test_render_positions_card_returns_interactive")


def test_render_explain_card_returns_interactive() -> None:
    from stock_trading_agent.engine.skills import _render_explain_card
    result = _render_explain_card({
        "code": "600519",
        "name": "茅台",
        "explanation": "龙头股, 板块强势",
        "rag_sources": ["缠中说禅108课 第17课", "好运2008: 龙头战法"],
    })
    assert result["msg_type"] == "interactive"
    text = str(result["content"])
    assert "龙头股" in text
    assert "缠中说禅" in text
    print("  PASS test_render_explain_card_returns_interactive")


# ─────────── #5 admin 斜杠命令 ───────────

def test_admin_cmd_help() -> None:
    from stock_trading_agent.feishu.admin_cmd import handle
    result = handle("/help", "ou_admin", "oc_chat", {})
    assert result is not None
    assert "/picks" in result["content"]["text"]
    print("  PASS test_admin_cmd_help")


def test_admin_cmd_picks() -> None:
    """/picks 调 get_picks skill, 不进 LLM"""
    from stock_trading_agent.feishu import admin_cmd
    from stock_trading_agent.engine import skills

    with patch.object(skills, 'call_skill',
                      return_value={"ok": True,
                                    "card": {"msg_type": "interactive",
                                             "content": {"elements": []}}}) as mock_call:
        result = admin_cmd.handle("/picks", "ou_admin", "oc_chat", {})

    assert result is not None
    assert result["msg_type"] == "interactive"
    mock_call.assert_called_with("get_picks", {})
    print("  PASS test_admin_cmd_picks")


def test_admin_cmd_non_admin_blocked() -> None:
    """非 admin 返 '权限不足', 不进 skill"""
    from stock_trading_agent.feishu import admin_cmd
    config = {"feishu": {"admin_user_ids": ["ou_admin"]}}
    result = admin_cmd.handle("/picks", "ou_stranger", "oc_chat", config)
    assert result is not None
    assert "权限不足" in result["content"]["text"]
    print("  PASS test_admin_cmd_non_admin_blocked")


def test_admin_cmd_non_slash_returns_none() -> None:
    """非 / 开头返 None, 让 listener 走 LLM 路径"""
    from stock_trading_agent.feishu import admin_cmd
    assert admin_cmd.handle("今日选股", "ou_x", "oc_x", {}) is None
    assert admin_cmd.handle("hi", "ou_x", "oc_x", {}) is None
    print("  PASS test_admin_cmd_non_slash_returns_none")


def test_admin_cmd_unknown_cmd() -> None:
    from stock_trading_agent.feishu import admin_cmd
    result = admin_cmd.handle("/foo", "ou_admin", "oc_chat", {})
    assert result is not None
    assert "未知命令" in result["content"]["text"]
    print("  PASS test_admin_cmd_unknown_cmd")


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
