"""
test_v5.py — v5 四个新功能
1) 真实 fixtures (12 个, 来自 5/7 ~ 6/2 真实数据)
2) 更多风险指标 (Sortino / info / cons_loss / profit_factor)
3) 报告导出 (Markdown 渲染 + 推飞书)
4) 行业集中度约束 (max_sector_ratio / max_sector_concurrent)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import stock_trading_agent.engine.paper_trader as pt
import stock_trading_agent.engine.reviewer as rev
import stock_trading_agent.engine.data_fetcher as df
from stock_trading_agent.engine.paper_trader import _check_sector_concentration
from stock_trading_agent.engine.report import render_weekly, render_daily, save_report
from stock_trading_agent.engine.reviewer import backtest_multi, _compute_metrics


# ─────────── 1. 真实 fixtures ───────────

def test_real_fixtures_exist() -> None:
    """tests/fixtures/ 下应有 12 个 pick_*.json"""
    fixtures_dir = Path("/Users/alice/Documents/Codex/stock-trading-agent/stock_trading_agent/tests/fixtures")
    files = list(fixtures_dir.glob("pick_*.json"))
    assert len(files) >= 10, f"应 ≥ 10 个 fixture, got {len(files)}"
    # 验证格式
    f = files[0]
    data = json.loads(f.read_text())
    assert "date" in data and "plan" in data and "filtered_stocks" in data
    print(f"  ✓ test_real_fixtures_exist: {len(files)} 个 fixture OK")


# ─────────── 2. 更多风险指标 ───────────

def test_metrics_extended() -> None:
    """11 个指标都应计算"""
    daily = [
        {"pnl": 50000, "cash": 1050000}, {"pnl": -30000, "cash": 1020000},
        {"pnl": 60000, "cash": 1080000}, {"pnl": -20000, "cash": 1060000},
        {"pnl": 40000, "cash": 1100000}, {"pnl": 30000, "cash": 1130000},
        {"pnl": -40000, "cash": 1090000}, {"pnl": 50000, "cash": 1140000},
    ]
    cfg = {"backtest": {"risk_free_rate_pct": 2.0, "trading_days_per_year": 240}}
    m = _compute_metrics(daily, 1000000, cfg)
    expected_keys = {
        "sharpe", "sortino", "information_ratio", "max_drawdown_pct",
        "annualized_return_pct", "calmar", "volatility_pct",
        "max_consecutive_losses", "max_consecutive_wins", "profit_factor",
        "win_rate_pct", "n_days",
    }
    assert set(m.keys()) == expected_keys, f"keys mismatch: {set(m.keys()) ^ expected_keys}"
    # 数据验证
    assert m["n_days"] == 8
    assert m["sharpe"] != 0
    assert m["sortino"] != 0
    assert m["max_consecutive_losses"] >= 1  # 有 2 连亏 (-30000, -20000)
    assert m["max_consecutive_wins"] >= 1
    assert m["profit_factor"] > 0
    assert 0 < m["win_rate_pct"] < 100
    print(f"  ✓ test_metrics_extended: 11 个指标, sharpe={m['sharpe']}, sortino={m['sortino']}, PF={m['profit_factor']}")


def test_metrics_sortino_higher_when_less_downside() -> None:
    """下行少时 Sortino > Sharpe (因为分母小)"""
    daily_pos = [{"pnl": 1000, "cash": 1010000} for _ in range(5)] + [{"pnl": -500, "cash": 1005000}]
    cfg = {"backtest": {"risk_free_rate_pct": 2.0, "trading_days_per_year": 240}}
    m = _compute_metrics(daily_pos, 1000000, cfg)
    if m["sharpe"] > 0 and m["sortino"] > 0:
        assert m["sortino"] >= m["sharpe"], f"Sortino 应 >= Sharpe: {m['sortino']} vs {m['sharpe']}"
    print(f"  ✓ test_metrics_sortino_higher: sharpe={m['sharpe']}, sortino={m['sortino']}")


# ─────────── 3. 报告导出 ───────────

def test_render_weekly_minimal() -> None:
    """空 weekly + 空 backtest 也应能渲染"""
    out = render_weekly({"stats": {"overall": {}}, "applied": [], "pending": []}, backtest=None, llm_summary="")
    assert "# 📅 量化周报" in out
    assert "## 整体表现" in out
    assert "## 调参记录" in out
    print(f"  ✓ test_render_weekly_minimal: 长度 {len(out)}")


def test_render_weekly_with_backtest() -> None:
    """带 backtest 时应有对比表"""
    weekly = {"stats": {"overall": {"n": 5, "win_rate": 40, "avg": 1.5}}, "applied": [], "pending": []}
    bt = {
        "days": 12, "recommendation": "fixed_A",
        "fixed_A": {"total_pnl_pct": 5.24, "win_rate_pct": 8.3,
                    "metrics": {"sharpe": 1.7, "sortino": 4.38, "max_drawdown_pct": 30.71,
                                "annualized_return_pct": 177.93, "max_consecutive_losses": 1,
                                "profit_factor": 1.75}},
    }
    out = render_weekly(weekly, backtest=bt, llm_summary="本周胜率 35%")
    assert "多策略回测对比" in out
    assert "fixed_A" in out
    assert "Sortino" in out
    assert "推荐: fixed_A" in out
    print(f"  ✓ test_render_weekly_with_backtest: {len(out)} 字符, 含对比表")


def test_render_daily() -> None:
    daily = {
        "date": "2026-06-08",
        "picks": [
            {"code": "600001", "name": "测试A", "score": 75.0, "sector": "机器人"},
            {"code": "600002", "name": "测试B", "score": 70.0, "sector": "航天"},
        ],
        "paper_total": {"total_pnl_pct": 1.5, "win_rate": 60, "closed_count": 5},
    }
    out = render_daily(daily)
    assert "# 📊 日报" in out
    assert "测试A" in out
    print(f"  ✓ test_render_daily: {len(out)} 字符")


def test_save_report() -> None:
    """保存到 docs/reports/"""
    p = save_report("# test\n\nhello", "test")
    assert p.exists()
    assert p.read_text().startswith("# test")
    # 清理
    p.unlink()
    print(f"  ✓ test_save_report: saved to {p}")


# ─────────── 4. 行业集中度约束 ───────────

def test_sector_concurrent_limit() -> None:
    """max_sector_concurrent=2 时, 第 3 只同板块被拒"""
    stocks = [
        {"code": "1", "sector": "半导体", "position_advice_amount": 100000},
        {"code": "2", "sector": "半导体", "position_advice_amount": 100000},
        {"code": "3", "sector": "半导体", "position_advice_amount": 100000},
        {"code": "4", "sector": "机器人", "position_advice_amount": 100000},
    ]
    out = _check_sector_concentration(stocks, [], max_sector_ratio=1.0, max_sector_concurrent=2, pos_ratio=0.6, total_cap=1_000_000)
    # 半导体: 2 通过, 第 3 个被拒
    # 机器人: 通过
    assert len(out) == 3
    codes = {s["code"] for s in out}
    assert codes == {"1", "2", "4"}
    print(f"  ✓ test_sector_concurrent_limit: 3 只通过 (2 半导体 + 1 机器人)")


def test_sector_ratio_limit() -> None:
    """max_sector_ratio 太严 → 拒"""
    stocks = [
        {"code": "1", "sector": "半导体", "position_advice_amount": 100000},
        {"code": "2", "sector": "半导体", "position_advice_amount": 100000},
    ]
    # 200k / 1M = 20% > 5% → 拒
    out = _check_sector_concentration(stocks, [], max_sector_ratio=0.05, max_sector_concurrent=10, pos_ratio=0.6, total_cap=1_000_000)
    assert len(out) == 0
    print(f"  ✓ test_sector_ratio_limit: 0 只通过 (200k/1M > 5%)")


def test_sector_existing_aggregation() -> None:
    """existing 已有 1 只, 新开 1 只 (共 2 只) + 1 只机器人"""
    existing = [{"sector": "半导体", "open_amount": 200000}]  # 已有 1 只, 20%
    stocks = [
        {"code": "1", "sector": "半导体", "position_advice_amount": 100000},  # 新开 1
        {"code": "2", "sector": "半导体", "position_advice_amount": 100000},  # 拒 (并发)
        {"code": "3", "sector": "机器人", "position_advice_amount": 100000},  # 通过
    ]
    out = _check_sector_concentration(stocks, existing, max_sector_ratio=0.5, max_sector_concurrent=2, pos_ratio=0.6, total_cap=1_000_000)
    assert len(out) == 2
    codes = {s["code"] for s in out}
    assert codes == {"1", "3"}
    print(f"  ✓ test_sector_existing_aggregation: 2 只通过")


# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [
        test_real_fixtures_exist,
        test_metrics_extended,
        test_metrics_sortino_higher_when_less_downside,
        test_render_weekly_minimal,
        test_render_weekly_with_backtest,
        test_render_daily,
        test_save_report,
        test_sector_concurrent_limit,
        test_sector_ratio_limit,
        test_sector_existing_aggregation,
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
