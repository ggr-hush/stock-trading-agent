"""test_v12_a_1_stock_quote.py — v12.A.1 个股 K 线 skill

Covers:
  1) fetch_stock_kline: 代码错/未来日/历史日都返空 dict
  2) _run_get_stock_quote: 6 位 code + 未来/过去/今天 4 个分支
  3) _render_stock_quote_card: 4 种 source 卡片格式
  4) keyword_fallback: 6 位代码优先 → get_stock_quote / explain_pick
  5) tool schema: get_stock_quote 在 SKILL_REGISTRY + 参数正确
  6) _run_explain_pick picks 找不到时走 K 线 (mock fetch_stock_kline)
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── 1) fetch_stock_kline 边界 ───────────

def test_fetch_stock_kline_bad_code() -> None:
    from stock_trading_agent.engine.data_fetcher import fetch_stock_kline
    assert fetch_stock_kline("") == {}
    assert fetch_stock_kline("123") == {}        # 不足 6 位
    assert fetch_stock_kline("abcdef") == {}    # 非数字
    assert fetch_stock_kline("1234567") == {}   # 7 位


def test_fetch_stock_kline_bad_date() -> None:
    """日期格式错 → _run_get_stock_quote 返 bad_date; fetcher 拉 1 年 K 让接口自己选"""
    from stock_trading_agent.engine.skills import _run_get_stock_quote
    r = _run_get_stock_quote({"code": "603063", "date": "not-a-date"})
    assert r["source"] == "bad_date"


def test_fetch_stock_kline_future_date() -> None:
    """未来日 → source=future"""
    from stock_trading_agent.engine.skills import _run_get_stock_quote
    future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    r = _run_get_stock_quote({"code": "603063", "date": future})
    assert r["source"] == "future"
    assert "未开盘" in r["env_level"]


# ─────────── 2) _run_get_stock_quote 主路径 (mock fetch_stock_kline) ───────────

def test_run_get_stock_quote_with_kline_data() -> None:
    """K 线拉到 → 返 close/chg/turnover 全字段"""
    from stock_trading_agent.engine.skills import _run_get_stock_quote
    fake_kline = {
        "code": "603063", "name": "禾望电气", "date": "2026-06-12",
        "open": 33.10, "close": 33.45, "high": 33.80, "low": 33.10,
        "chg_pct": 0.50, "chg_amt": 0.17,
        "volume": 12345678, "amount_yi": 1.23, "amplitude": 2.10,
        "turnover": 2.15, "source": "东方财富K线",
    }
    with patch("stock_trading_agent.engine.data_fetcher.fetch_stock_kline", return_value=fake_kline):
        r = _run_get_stock_quote({"code": "603063", "date": "2026-06-12"})
    assert r["close"] == 33.45
    assert r["chg_pct"] == 0.50
    assert r["source"] == "东方财富K线"
    assert r["date"] == "2026-06-12"


def test_run_get_stock_quote_empty_kline() -> None:
    """K 线拉不到 (节假日/接口挂) → source=empty 友好提示"""
    from stock_trading_agent.engine.skills import _run_get_stock_quote
    with patch("stock_trading_agent.engine.data_fetcher.fetch_stock_kline", return_value={}):
        r = _run_get_stock_quote({"code": "603063", "date": "2025-12-31"})
    assert r["source"] == "empty"
    assert "拉不到" in r["env_level"]


# ─────────── 3) _render_stock_quote_card 4 种 source ───────────

def test_render_stock_quote_card_kline_source() -> None:
    """K 线数据卡片: 含收盘/涨跌/换手"""
    from stock_trading_agent.engine.skills import _render_stock_quote_card
    r = _render_stock_quote_card({
        "code": "603063", "name": "禾望电气", "date": "2026-06-12",
        "close": 33.45, "chg_pct": 0.50, "chg_amt": 0.17,
        "open": 33.10, "high": 33.80, "low": 33.10,
        "amplitude": 2.10, "turnover": 2.15, "amount_yi": 1.23,
        "source": "kline",
    })
    text = r["content"]["text"]
    assert "603063" in text
    assert "禾望电气" in text
    assert "33.45" in text
    assert "+0.50%" in text
    assert "换手" in text
    assert r["msg_type"] == "text"


def test_render_stock_quote_card_future_source() -> None:
    """未来日 → 友好提示"""
    from stock_trading_agent.engine.skills import _render_stock_quote_card
    r = _render_stock_quote_card({
        "code": "603063", "name": "禾望电气", "date": "2027-01-01",
        "env_level": "未开盘 (未来日)", "source": "future",
    })
    text = r["content"]["text"]
    assert "未开盘" in text
    assert "603063" in text


# ─────────── 4) keyword_fallback 标的优先 ───────────

def test_keyword_fallback_stock_code_priority() -> None:
    """6 位代码 → get_stock_quote (有 '行情') 或 explain_pick (无)"""
    from stock_trading_agent.engine.skills import keyword_fallback
    # 有 '行情' → get_stock_quote
    assert keyword_fallback("603063 行情") == "get_stock_quote"
    assert keyword_fallback("603063 周五行情") == "get_stock_quote"
    # 没 '行情' → explain_pick
    assert keyword_fallback("603063 怎么样") == "explain_pick"
    # 沪市 6 开头
    assert keyword_fallback("600519") == "explain_pick"
    # 深市 0 开头
    assert keyword_fallback("000001") == "explain_pick"


def test_keyword_fallback_position_before_picks() -> None:
    """'今日持仓' 优先到 get_positions (不再被 '今日' 截胡到 get_picks)"""
    from stock_trading_agent.engine.skills import keyword_fallback
    assert keyword_fallback("今日持仓") == "get_positions"
    assert keyword_fallback("我的仓位") == "get_positions"
    assert keyword_fallback("查一下持仓") == "get_positions"


# ─────────── 5) tool schema 校验 ───────────

def test_get_stock_quote_schema() -> None:
    from stock_trading_agent.engine.skills import SKILL_REGISTRY
    schema = SKILL_REGISTRY["get_stock_quote"].schema["function"]
    desc = schema["description"]
    assert "code" in desc
    assert "date" in desc
    assert "K 线" in desc or "K线" in desc
    params = schema.get("parameters", {})
    props = params.get("properties", {})
    assert "code" in props
    assert "date" in props
    assert params.get("required", []) == ["code"]


# ─────────── 6) _run_explain_pick picks 找不到时走 K 线 ───────────

def test_explain_pick_falls_back_to_kline_when_date_given() -> None:
    """picks 找不到 + date 传了 + K 线拉到 → source='kline' (不是 'realtime')"""
    from stock_trading_agent.engine.skills import _run_explain_pick
    from stock_trading_agent.engine.paper_trader import get_db
    from stock_trading_agent.llm import reasoner
    import sqlite3

    # 确保 picks 表里没 999999 这只
    conn = get_db()
    conn.execute("DELETE FROM picks WHERE code = ?", ("999999",))
    conn.commit()

    fake_kline = {
        "code": "999999", "name": "测试股", "date": "2026-06-12",
        "open": 10.0, "close": 10.5, "high": 11.0, "low": 9.8,
        "chg_pct": 5.0, "chg_amt": 0.5, "turnover": 1.5, "amount_yi": 0.5,
        "amplitude": 12.0, "source": "东方财富K线",
    }
    with patch("stock_trading_agent.engine.data_fetcher.fetch_stock_kline", return_value=fake_kline), \
         patch.object(reasoner, "answer_question", return_value="这是一只测试股"):
        r = _run_explain_pick({"code": "999999", "date": "2026-06-12"})
    assert r["source"] == "kline"
    assert "K线" in r["explanation"]
    assert "测试股" in r["explanation"]


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            fail += 1
    print(f"\n{'OK' if fail == 0 else 'FAIL'} {len(tests) - fail}/{len(tests)} tests passed")
    sys.exit(0 if fail == 0 else 1)
