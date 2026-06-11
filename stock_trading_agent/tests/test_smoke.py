"""
test_smoke.py — 烟雾测试
1) LLM 调用无 API key 时优雅降级
2) 选股端到端（mock 行情接口）
3) CLI 单次跑（run-once --stage pick）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import stock_trading_agent.engine.paper_trader as pt
import stock_trading_agent.llm.client as llmc
import stock_trading_agent.llm.reasoner as llmr


def test_llm_degrades_without_key() -> None:
    """无 MINIMAX_API_KEY 且 .env 也没 → 返回 ok=False, 不抛异常"""
    os.environ.pop("MINIMAX_API_KEY", None)
    # v11 修复: client 兜底会读 .env, 测试时 mock 掉
    with patch("stock_trading_agent.engine.data_fetcher.load_env", return_value={}):
        r = llmc.chat([{"role": "user", "content": "hi"}])
        assert r["ok"] is False
        assert "MINIMAX_API_KEY" in r["error"]
        print(f"  ✓ test_llm_degrades_without_key: 降级返回 {r['error'][:40]}")


def test_reasoner_returns_empty_on_failure() -> None:
    """LLM 失败时 reasoner 各调用点返回空字符串"""
    os.environ.pop("MINIMAX_API_KEY", None)
    with patch("stock_trading_agent.engine.data_fetcher.load_env", return_value={}):
        # 6 个调用点
        pick_result = {
            "date": "2026-06-01", "plan_used": "A",
        "market_env": {"env_level": "中性", "env_score": 50, "position_advice": "半仓", "flags": []},
        "sectors": [{"name": "机器人"}, {"name": "航天"}], "filtered_stocks": [],
    }
    assert llmr.pick_intro(pick_result) == ""
    assert llmr.risk_explain([]) == ""
    assert llmr.param_reason({}, {}) == ""
    assert llmr.weekly_summary({}) == ""
    assert llmr.empty_day({}, 0) == ""
    assert llmr.anomaly("test", "details") == ""
    print(f"  ✓ test_reasoner_returns_empty_on_failure: 6 个调用点全部优雅降级")


def test_cli_run_once_pick() -> None:
    """CLI: python -m stock_trading_agent.agent run-once --stage pick（mock 行情）"""
    # 准备一个隔离的测试目录
    tmp = Path(tempfile.mkdtemp(prefix="sta_test_cli_"))
    db = tmp / "quant.db"
    pt.DB_PATH = db
    pt.DATA_DIR = tmp

    # Mock 所有外部调用
    def mock_get_market_env(*a, **k):
        return {
            "env_score": 50, "env_level": "中性", "position_advice": "半仓50%",
            "position_ratio": 0.5, "market_type": "震荡", "flags": ["can_trade"],
            "details": {},
        }
    def mock_get_hot_sectors(*a, **k):
        return [{"name": "机器人", "chg_pct": 5.0, "_type": "concept"}]
    def mock_get_market_stocks(*a, **k):
        return [
            {"code": "600001", "name": "测试A", "price": 10.0, "prev_close": 9.7,
             "chg_pct": 3.1, "turnover": 8.5, "total_mv_yi": 200.0, "amount_yi": 5.0,
             "high": 10.2, "low": 9.85, "amplitude": 3.5, "limit_up_days": 0,
             "sector": "机器人", "position": 50.0},
        ]
    def mock_get_stock_sectors(codes, cfg):
        return {"600001": "机器人"}
    def mock_get_sina_ut():
        return "test_ut"
    def mock_is_trading_day(*a, **k):
        return True
    def mock_open_positions(*a, **k):
        return 1  # mock 1 开仓
    def mock_pusher(*a, **k):
        return {"ok": True, "mocked": True}

    with patch("stock_trading_agent.engine.picker.get_market_env", mock_get_market_env), \
         patch("stock_trading_agent.engine.picker.get_hot_sectors", mock_get_hot_sectors), \
         patch("stock_trading_agent.engine.picker.get_market_stocks", mock_get_market_stocks), \
         patch("stock_trading_agent.engine.picker.get_stock_sectors", mock_get_stock_sectors), \
         patch("stock_trading_agent.engine.data_fetcher.get_sina_ut", mock_get_sina_ut), \
         patch("stock_trading_agent.engine.data_fetcher.is_trading_day", mock_is_trading_day), \
         patch("stock_trading_agent.agent.stages.open_positions", mock_open_positions), \
         patch("stock_trading_agent.feishu.pusher._send_webhook", mock_pusher):
        from stock_trading_agent.agent import run_once
        result = run_once("pick")
        assert result["plan"] in ("A", "B", "C")
        print(f"  ✓ test_cli_run_once_pick: 跑通了, plan={result['plan']}, 开仓={result.get('n_open', 0)}")


# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [
        test_llm_degrades_without_key,
        test_reasoner_returns_empty_on_failure,
        test_cli_run_once_pick,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"  ✗ {t.__name__}: EXCEPTION {type(e).__name__}: {e}")
            sys.exit(1)
    print(f"\n✓ {len(tests)} tests passed")
