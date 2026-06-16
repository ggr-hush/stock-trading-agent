"""test_v12_a_market_env.py — get_market_env 接 date 参数, 治 LLM '截止日' hallucinate

Covers:
  1) _parse_relative_date: 今天/昨天/明天/周X/下周一/上周五/2段日期/3段日期/无日期
  2) _run_get_market_env: 未来日/过去日/今天-交易日/今天-周末/格式错/无参
  3) keyword_fallback: 触发词含 "行情" / "市场行情" / "盘面"
  4) tool schema description 含 date 参数说明
  5) 集成: dispatch keyword_fallback 透传 date
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock


def _past_trading_day(n_days_back: int = 10) -> datetime.date:
    """返回 n_days_back 天内最近的过去交易日 (避开周末)"""
    from stock_trading_agent.engine.data_fetcher import is_trading_day
    d = datetime.date.today() - datetime.timedelta(days=n_days_back)
    while not is_trading_day(d):
        d -= datetime.timedelta(days=1)
    return d

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── 1) _parse_relative_date ───────────

def test_parse_today() -> None:
    from stock_trading_agent.engine.skills import _parse_relative_date
    today = datetime.date.today().isoformat()
    assert _parse_relative_date("今天行情") == today
    assert _parse_relative_date("今日怎么样") == today


def test_parse_yesterday_tomorrow() -> None:
    from stock_trading_agent.engine.skills import _parse_relative_date
    y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    t = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    assert _parse_relative_date("昨天行情") == y
    assert _parse_relative_date("明天行情") == t


def test_parse_weekday_push_to_future() -> None:
    """单独 '周五' 推下一个周五 (v12.A 修复, 之前会推过去)"""
    from stock_trading_agent.engine.skills import _parse_relative_date
    today = datetime.date.today()
    cur_wd = today.weekday()  # 0=Mon
    fri = 4
    diff = fri - cur_wd
    if diff <= 0:
        diff += 7
    expected = (today + datetime.timedelta(days=diff)).isoformat()
    assert _parse_relative_date("周五行情呢") == expected


def test_parse_next_last_weekday() -> None:
    from stock_trading_agent.engine.skills import _parse_relative_date
    today = datetime.date.today()
    cur_wd = today.weekday()
    mon = 0
    diff = mon - cur_wd
    if diff <= 0:
        diff += 7
    expected_mon = (today + datetime.timedelta(days=diff)).isoformat()
    assert _parse_relative_date("下周一行情") == expected_mon

    fri = 4
    diff = fri - cur_wd
    if diff >= 0:
        diff -= 7
    expected_fri = (today + datetime.timedelta(days=diff)).isoformat()
    assert _parse_relative_date("上周五行情") == expected_fri


def test_parse_explicit_date_3seg() -> None:
    from stock_trading_agent.engine.skills import _parse_relative_date
    assert _parse_relative_date("2025-11-07 那天行情") == "2025-11-07"
    assert _parse_relative_date("2026-1-5 怎样") == "2026-01-05"


def test_parse_explicit_date_2seg() -> None:
    """'11-07' 默认本年"""
    from stock_trading_agent.engine.skills import _parse_relative_date
    today = datetime.date.today()
    expected = today.replace(month=11, day=7).isoformat()
    assert _parse_relative_date("11-07 那天") == expected


def test_parse_no_date_returns_none() -> None:
    from stock_trading_agent.engine.skills import _parse_relative_date
    assert _parse_relative_date("随便问问") is None
    assert _parse_relative_date("") is None


def test_parse_chinese_date() -> None:
    """v12.A.4.c hotfix: 6月16日 / 6月16号 → YYYY-MM-DD (治"6月16日选股" 不识别)"""
    from stock_trading_agent.engine.skills import _parse_relative_date
    today = datetime.date.today()
    # 当年 6月16日: 未来 → 去年, 过去 → 今年
    target = datetime.date(today.year, 6, 16)
    if target > today:
        target = datetime.date(today.year - 1, 6, 16)
    expected = target.isoformat()
    assert _parse_relative_date("6月16日选股") == expected
    assert _parse_relative_date("6月16号行情") == expected
    assert _parse_relative_date("06月16日 K线") == expected


# ─────────── 2) _run_get_market_env with date ───────────

def test_run_future_date_returns_future_label() -> None:
    """未来日 → '未开盘 (未来日)' 友好提示, 不瞎编"""
    from stock_trading_agent.engine.skills import _run_get_market_env
    future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    result = _run_get_market_env({"date": future})
    assert result["env_score"] is None
    assert result["env_level"] == "未开盘 (未来日)"
    assert result["source"] == "future"
    assert result["date"] == future


def test_run_past_date_no_picks_returns_no_history() -> None:
    """过去交易日 + picks 表空 → '历史数据暂无'"""
    from stock_trading_agent.engine.skills import _run_get_market_env
    past = _past_trading_day(10).isoformat()
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = None
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db):
        result = _run_get_market_env({"date": past})
    assert result["env_score"] is None
    assert result["env_level"] == "历史数据暂无 (picks 表当日为空)"
    assert result["source"] == "no_history"


def test_run_past_date_picks_has_returns_picks_source() -> None:
    """过去日 + picks 表有该日数据 → 用 picks"""
    from stock_trading_agent.engine.skills import _run_get_market_env
    past = _past_trading_day(10).isoformat()
    fake_row = {
        "market_env_score": 65,
        "market_env_level": "偏多",
        "pick_date": past,
    }
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = fake_row
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db):
        result = _run_get_market_env({"date": past})
    assert result["env_score"] == 65
    assert result["env_level"] == "偏多"
    assert result["source"] == "picks"


def test_run_today_non_trading_day_returns_friendly() -> None:
    """今天 = 周末 → '周末/节假日不开盘' 友好提示"""
    from stock_trading_agent.engine.skills import _run_get_market_env
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = None
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db), \
         patch("stock_trading_agent.engine.skills.is_trading_day", return_value=False):
        result = _run_get_market_env({"date": "today"})
    assert result["source"] == "non_trading_day"
    assert "不开盘" in result["env_level"]


def test_run_bad_date_format() -> None:
    from stock_trading_agent.engine.skills import _run_get_market_env
    result = _run_get_market_env({"date": "not-a-date"})
    assert result["source"] == "bad_date"
    assert "日期格式错" in result["env_level"]


def test_run_today_no_args_falls_through_to_realtime() -> None:
    """date 未传 + picks 空 → 走实时拉"""
    from stock_trading_agent.engine.skills import _run_get_market_env
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = None
    fake_env = {"env_score": 50, "env_level": "中性", "position_advice": "半仓"}
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db), \
         patch("stock_trading_agent.engine.skills.is_trading_day", return_value=True), \
         patch("stock_trading_agent.engine.data_fetcher.get_market_env", return_value=fake_env), \
         patch("stock_trading_agent.engine.data_fetcher.load_config", return_value={}):
        result = _run_get_market_env({})
    assert result["env_score"] == 50
    assert result["source"] == "realtime"


# ─────────── 3) keyword_fallback triggers ───────────

def test_keyword_fallback_market_env_triggers() -> None:
    """'行情' / '市场行情' / '盘面' 都该路由到 get_market_env"""
    from stock_trading_agent.engine.skills import keyword_fallback
    assert keyword_fallback("今天行情呢") == "get_market_env"
    assert keyword_fallback("市场行情怎么样") == "get_market_env"
    assert keyword_fallback("今天盘面") == "get_market_env"
    assert keyword_fallback("大盘怎么样") == "get_market_env"
    assert keyword_fallback("今天 env 多少") == "get_market_env"


def test_keyword_fallback_unrelated() -> None:
    from stock_trading_agent.engine.skills import keyword_fallback
    # 注意: "今日持仓" 会被 get_picks 截胡 (因 "今日" 在 get_picks 触发词里), 是 v12.9.1 已知行为, 不在 v12.A 修复范围
    assert keyword_fallback("请帮我选股") == "get_picks"
    assert keyword_fallback("查询持仓") == "get_positions"
    assert keyword_fallback("随便问问") is None
    assert keyword_fallback("") is None


# ─────────── 4) tool schema 含 date 参数 ───────────

def test_get_market_env_schema_has_date_param() -> None:
    from stock_trading_agent.engine.skills import SKILL_REGISTRY
    schema = SKILL_REGISTRY["get_market_env"].schema["function"]
    desc = schema["description"]
    assert "date" in desc
    assert "YYYY-MM-DD" in desc
    assert "不瞎编" in desc or "不会编" in desc
    params = schema.get("parameters", {})
    props = params.get("properties", {})
    assert "date" in props
    assert props["date"]["type"] == "string"


# ─────────── 5) 集成: dispatch keyword_fallback 透传 date ───────────

def test_dispatch_keyword_fallback_passes_date_to_get_market_env() -> None:
    """LLM 不可用 + 关键词命中 get_market_env + 文本含 '周五'
    → call_skill 收到的 args 应含 date='YYYY-MM-DD'"""
    from stock_trading_agent.llm import tool_use

    captured_args: dict = {}

    def fake_call_skill(skill_name, args):
        captured_args["skill_name"] = skill_name
        captured_args["args"] = args
        return {
            "ok": True,
            "card": {"msg_type": "text", "content": {"text": "ok"}},
            "raw": {},
            "uses_llm": False,
            "name": skill_name,
        }

    with patch("stock_trading_agent.llm.tool_use.chat_with_tools",
                 return_value={"ok": False, "error": "mock LLM down",
                               "tool_calls": [], "content": ""}), \
         patch("stock_trading_agent.engine.skills.call_skill", side_effect=fake_call_skill), \
         patch("stock_trading_agent.llm.tool_use._empty_response_fallback", return_value="fb"):
        result = tool_use.dispatch("周五行情呢", chat_id="test_chat")

    assert captured_args["skill_name"] == "get_market_env"
    assert "date" in captured_args["args"]
    today = datetime.date.today()
    cur_wd = today.weekday()
    fri = 4
    diff = fri - cur_wd
    if diff <= 0:
        diff += 7
    expected = (today + datetime.timedelta(days=diff)).isoformat()
    assert captured_args["args"]["date"] == expected
    assert result["path"] == "keyword_fallback"


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
