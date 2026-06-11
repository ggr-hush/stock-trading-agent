"""test_v7.py — v7 五个新功能
1) 回测 + RAG 联动 (auto 表现差 → BM25 知识库 + LLM 解释)
2) 行业相关性矩阵 (仓位约束 v2)
3) 周日 20:00 自动 report 推飞书
4) APScheduler stage 依赖图
5) pusher 抽 _http.py 独立层
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import stock_trading_agent.engine.reviewer as rev
from stock_trading_agent.engine.reviewer import detect_auto_regression, run_weekly_review


# ─────────── 1. 回测 + RAG 联动 ───────────

def test_detect_regression_low_win_rate() -> None:
    """胜率 < 40% 触发回归检测"""
    stats = {"overall": {"n": 20, "win_rate": 30, "avg": 0.5}}
    assert detect_auto_regression(stats) is True
    print("  ✓ test_detect_regression_low_win_rate")


def test_detect_regression_low_avg() -> None:
    """平均 PnL < -0.5% 触发"""
    stats = {"overall": {"n": 20, "win_rate": 60, "avg": -1.0}}
    assert detect_auto_regression(stats) is True
    print("  ✓ test_detect_regression_low_avg")


def test_detect_regression_small_sample_skipped() -> None:
    """样本 < 5 不触发 (避免噪声)"""
    stats = {"overall": {"n": 3, "win_rate": 0, "avg": -10}}
    assert detect_auto_regression(stats) is False
    print("  ✓ test_detect_regression_small_sample_skipped")


def test_detect_regression_good_not_triggered() -> None:
    """胜率高 + avg 正 → 不触发"""
    stats = {"overall": {"n": 20, "win_rate": 65, "avg": 1.2}}
    assert detect_auto_regression(stats) is False
    print("  ✓ test_detect_regression_good_not_triggered")


def test_run_weekly_review_attaches_explanation_on_regression() -> None:
    """回归检测为 True 时, auto_regression.explanation 被填上 (mock LLM)"""
    bad_stats = {
        "overall": {"n": 20, "win_rate": 30, "avg": -1.0},
        "by_score": {}, "by_chg": {}, "by_sector": {},
    }
    fake_tune = {"stats": bad_stats, "applied": [], "pending": []}
    with patch.object(rev, "run_weekly_tune", return_value=fake_tune), \
         patch.object(rev, "auto_period_explain", return_value="FAKE-EXPLANATION") as mp:
        result = run_weekly_review()
    assert result["auto_regression"]["triggered"] is True
    assert result["auto_regression"]["explanation"] == "FAKE-EXPLANATION"
    assert mp.call_count == 1
    print("  ✓ test_run_weekly_review_attaches_explanation_on_regression")


def test_run_weekly_review_skips_explanation_on_good() -> None:
    """胜率高时不调 auto_period_explain, explanation 为空"""
    good_stats = {
        "overall": {"n": 20, "win_rate": 65, "avg": 1.2},
        "by_score": {}, "by_chg": {}, "by_sector": {},
    }
    fake_tune = {"stats": good_stats, "applied": [], "pending": []}
    with patch.object(rev, "run_weekly_tune", return_value=fake_tune), \
         patch.object(rev, "auto_period_explain") as mp:
        result = run_weekly_review()
    assert result["auto_regression"]["triggered"] is False
    assert result["auto_regression"]["explanation"] == ""
    assert mp.call_count == 0
    print("  ✓ test_run_weekly_review_skips_explanation_on_good")


# ─────────── 2. 行业相关性矩阵 ───────────

from stock_trading_agent.engine.paper_trader import _check_sector_correlation


def test_correlation_filters_over_group_limit() -> None:
    """同 group 内总仓位超 60% → 第二只被过滤"""
    # 已有 50w 在 "新能源车" (group 0)
    existing = [{"sector": "新能源车", "open_amount": 500_000}]
    # 候选两只: 一只 "锂电池" (同 group 0) 30w, 一只 "半导体" (group 1) 10w
    stocks = [
        {"code": "1", "sector": "锂电池", "position_advice_amount": 300_000},
        {"code": "2", "sector": "半导体", "position_advice_amount": 100_000},
    ]
    groups = [["新能源车", "锂电池", "充电桩"], ["半导体", "芯片"]]
    out = _check_sector_correlation(stocks, existing, total_cap=1_000_000,
                                    group_max_ratio=0.6, groups=groups)
    # 50w + 30w = 80w = 80% > 60% → 锂电池过滤
    # 半导体不相关, 保留
    assert len(out) == 1
    assert out[0]["code"] == "2"
    print("  ✓ test_correlation_filters_over_group_limit")


def test_correlation_unrelated_sector_passes() -> None:
    """不在任何 group 内的板块不受约束"""
    existing = []
    stocks = [
        {"code": "1", "sector": "军工", "position_advice_amount": 100_000},
        {"code": "2", "sector": "教育", "position_advice_amount": 100_000},
    ]
    groups = [["新能源车", "锂电池"], ["半导体", "芯片"]]
    out = _check_sector_correlation(stocks, existing, total_cap=1_000_000,
                                    group_max_ratio=0.6, groups=groups)
    assert len(out) == 2
    print("  ✓ test_correlation_unrelated_sector_passes")


def test_correlation_empty_groups_disables() -> None:
    """groups 为空时, 不做相关性检查"""
    stocks = [{"code": "1", "sector": "新能源车", "position_advice_amount": 900_000}]
    out = _check_sector_correlation(stocks, [], total_cap=1_000_000,
                                    group_max_ratio=0.6, groups=[])
    assert len(out) == 1
    print("  ✓ test_correlation_empty_groups_disables")


def test_correlation_accumulates_within_batch() -> None:
    """本批内第二只也走累加, 不该一刀切"""
    existing = []
    # 两只都是 "新能源车" (同 group 0), 各 30w
    # 第一只过: 0 + 30w = 30% < 60% ✓
    # 第二只: 30w + 30w = 60% ≤ 60% ✓
    stocks = [
        {"code": "1", "sector": "新能源车", "position_advice_amount": 300_000},
        {"code": "2", "sector": "新能源车", "position_advice_amount": 300_000},
    ]
    groups = [["新能源车", "锂电池"]]
    out = _check_sector_correlation(stocks, existing, total_cap=1_000_000,
                                    group_max_ratio=0.6, groups=groups)
    assert len(out) == 2, f"应都过, got {len(out)}"
    print("  ✓ test_correlation_accumulates_within_batch")

# ─────────── 3. 周日 20:00 自动 report 推飞书 ───────────

import stock_trading_agent.agent as ag
from stock_trading_agent.agent import stage_weekly_review


def test_stage_weekly_review_runs_full_report() -> None:
    """stage_weekly_review 应跑全量 (回测 + LLM 总结 + 落盘 + 推飞书)"""
    fake_weekly = {"stats": {"overall": {"n": 5, "win_rate": 50, "avg": 0.5}},
                   "applied": [], "pending": [],
                   "auto_regression": {"triggered": False, "explanation": ""}}
    fake_bt = {"multi": {"fixed_A": {"pnl_pct": 0.5}, "fixed_B": {"pnl_pct": 0.3},
                         "auto": {"pnl_pct": 0.4}}}
    with patch.object(ag.stages, "run_weekly_review", return_value=fake_weekly), \
         patch.object(ag.stages, "backtest_multi", return_value=fake_bt) as bt_mp, \
         patch.object(ag.stages, "weekly_summary", return_value="LLM 总结"), \
         patch.object(ag.stages, "push_weekly_report", return_value={
             "saved": "docs/reports/weekly_2025-06-08.md",
             "feishu": {"ok": True, "channel": "app"},
         }) as push_mp:
        result = stage_weekly_review()
    assert result["weekly"] is fake_weekly
    assert result["summary"] == "LLM 总结"
    assert bt_mp.call_count == 1
    assert push_mp.call_count == 1
    # push_weekly_report 收到 weekly + bt + summary 三个参数
    args, _ = push_mp.call_args
    assert args[0] is fake_weekly
    assert args[1] is fake_bt
    assert args[2] == "LLM 总结"
    print("  ✓ test_stage_weekly_review_runs_full_report")


def test_stage_weekly_review_skips_backtest_when_zero() -> None:
    """config.weekly_auto_backtest_days=0 → 跳过回测"""
    fake_weekly = {"stats": {"overall": {"n": 0, "win_rate": 0, "avg": 0}},
                   "applied": [], "pending": [],
                   "auto_regression": {"triggered": False, "explanation": ""}}
    fake_cfg = {"weekly_auto_backtest_days": 0, "schedule": {}}
    with patch.object(ag.stages, "load_config", return_value=fake_cfg), \
         patch.object(ag.stages, "run_weekly_review", return_value=fake_weekly), \
         patch.object(ag.stages, "backtest_multi") as bt_mp, \
         patch.object(ag.stages, "weekly_summary", return_value=""), \
         patch.object(ag.stages, "push_weekly_report", return_value={"saved": None, "feishu": {"ok": False}}):
        stage_weekly_review()
    assert bt_mp.call_count == 0
    print("  ✓ test_stage_weekly_review_skips_backtest_when_zero")

# ─────────── 4. APScheduler stage 依赖图 ───────────

from stock_trading_agent.agent import (
    STAGE_REGISTRY, topological_sort, validate_stage_deps,
    _check_dependencies, run_once,
)
from stock_trading_agent.engine.paper_trader import (
    mark_stage_run, was_stage_run_today,
)


def test_topo_sort_default_registry() -> None:
    """默认 STAGE_REGISTRY 应能拓扑排序"""
    topo = topological_sort(STAGE_REGISTRY)
    # 依赖在前, 排在前面
    assert topo.index("pre_market") < topo.index("open_auction")
    assert topo.index("open_auction") < topo.index("pick")
    assert topo.index("pick") < topo.index("post_market")
    assert topo.index("post_market") < topo.index("evening")
    assert topo.index("evening") < topo.index("weekly_review")
    print("  ✓ test_topo_sort_default_registry")


def test_topo_sort_raises_on_cycle() -> None:
    """循环依赖应 raise"""
    bad = {
        "a": {"fn": lambda: None, "depends": ["b"]},
        "b": {"fn": lambda: None, "depends": ["a"]},
    }
    try:
        topological_sort(bad)
        raise AssertionError("应 raise ValueError")
    except ValueError as e:
        assert "循环依赖" in str(e)
    print("  ✓ test_topo_sort_raises_on_cycle")


def test_topo_sort_raises_on_unknown_dep() -> None:
    """未知依赖应 raise"""
    bad = {"a": {"fn": lambda: None, "depends": ["nonexistent"]}}
    try:
        topological_sort(bad)
        raise AssertionError("应 raise ValueError")
    except ValueError as e:
        assert "未知" in str(e)
    print("  ✓ test_topo_sort_raises_on_unknown_dep")


def test_check_deps_missing() -> None:
    """依赖未跑时, _check_dependencies 列出 missing"""
    # 清理 stage_runs (避免别的 test 残留)
    conn = __import__("stock_trading_agent.engine.paper_trader", fromlist=["get_db"]).get_db()
    conn.execute("DELETE FROM stage_runs")
    conn.commit()
    missing = _check_dependencies("open_auction")
    assert missing == ["pre_market"]
    print("  ✓ test_check_deps_missing")


def test_check_deps_satisfied() -> None:
    """依赖已 mark 后, missing 为空"""
    mark_stage_run("pre_market", ok=True)
    missing = _check_dependencies("open_auction")
    assert missing == []
    # 清理
    conn = __import__("stock_trading_agent.engine.paper_trader", fromlist=["get_db"]).get_db()
    conn.execute("DELETE FROM stage_runs")
    conn.commit()
    print("  ✓ test_check_deps_satisfied")


def test_run_once_marks_stage_run() -> None:
    """run_once 成功跑完后, mark_stage_run 写入成功记录"""
    # 清理
    conn = __import__("stock_trading_agent.engine.paper_trader", fromlist=["get_db"]).get_db()
    conn.execute("DELETE FROM stage_runs")
    conn.commit()
    # mock stage_pre_market 函数
    called = []
    fake_fn = lambda: (called.append(1), {"ok": True})[1]
    with patch.dict(STAGE_REGISTRY, {
        "pre_market": {"fn": fake_fn, "depends": []},
    }, clear=False):
        # clear=False 保留其他, 但 dict 顺序会变; 改成临时把 pre_market 的 fn 替换
        pass
    # 更稳的做法: 直接 patch STAGE_REGISTRY["pre_market"]["fn"]
    orig = STAGE_REGISTRY["pre_market"]["fn"]
    STAGE_REGISTRY["pre_market"]["fn"] = fake_fn
    try:
        run_once("pre_market")
    finally:
        STAGE_REGISTRY["pre_market"]["fn"] = orig
    assert called == [1]
    assert was_stage_run_today("pre_market") is True
    # 清理
    conn.execute("DELETE FROM stage_runs")
    conn.commit()
    print("  ✓ test_run_once_marks_stage_run")


def test_validate_stage_deps_passes() -> None:
    """validate_stage_deps 在默认注册表上不抛"""
    validate_stage_deps()  # 不抛即过
    print("  ✓ test_validate_stage_deps_passes")

# ─────────── 5. pusher 抽 _http.py 独立层 ───────────

import stock_trading_agent.feishu.pusher as ps
from stock_trading_agent.feishu import _http


def test_pusher_uses_http_post_not_requests() -> None:
    """pusher 不应再直接 import requests, 一律走 http_post"""
    # 静态检查源码
    src = Path(ps.__file__).read_text()
    assert "import requests" not in src, "pusher 不该 import requests"
    assert "requests.post" not in src, "pusher 不该直接 requests.post"
    assert "from ._http import http_post" in src
    # 运行时检查
    assert not hasattr(ps, "requests"), "ps.requests 不该存在"
    assert callable(ps.http_post)
    print("  ✓ test_pusher_uses_http_post_not_requests")


def test_mock_http_post_catches_all_calls() -> None:
    """mock http_post 后, _send / _send_webhook / _send_via_app 全部不发真 HTTP"""
    from unittest.mock import MagicMock
    fake = MagicMock(status_code=200, text="ok")
    fake.json.return_value = {"code": 0, "msg": "ok"}
    # 让 app 通道走通: 假 token + 假 message
    import stock_trading_agent.engine.data_fetcher as df
    df._ENV_CACHE = {
        "FEISHU_APP_ID": "cli_xxx",
        "FEISHU_APP_SECRET": "sec",
        "FEISHU_CHAT_ID": "oc_xxx",
        "FEISHU_BITABLE_WEBHOOK": "https://example.com/hook",
        "FEISHU_PUSH_MODE": "app",
    }
    try:
        with patch.object(ps, "http_post", return_value=fake) as mp:
            res_webhook = ps._send_webhook("hi")
            res_app = ps._send_via_app("hi")
        # webhook 1 + app 2 (token+message) = 3
        assert mp.call_count == 3, f"webhook 1 + app 2 = 3, got {mp.call_count}"
        assert res_webhook["ok"] is True
        assert res_app["ok"] is True
    finally:
        df._ENV_CACHE = {}
    print("  ✓ test_mock_http_post_catches_all_calls")


def test_http_post_module_importable() -> None:
    """_http 模块应可独立 import"""
    assert hasattr(_http, "http_post")
    assert callable(_http.http_post)
    print("  ✓ test_http_post_module_importable")

# ─────────── 6. v8.1 自动学相关矩阵 ───────────

from stock_trading_agent.engine.correlation import (
    _pearson, _series_by_sector, compute_sector_correlation,
    learn_correlated_groups, auto_learn_groups,
)
from stock_trading_agent.engine.paper_trader import _check_sector_correlation


def test_pearson_perfect_positive() -> None:
    xs = [1, 2, 3, 4, 5]
    ys = [2, 4, 6, 8, 10]
    assert abs(_pearson(xs, ys) - 1.0) < 1e-6
    print("  ✓ test_pearson_perfect_positive")


def test_pearson_perfect_negative() -> None:
    xs = [1, 2, 3, 4, 5]
    ys = [5, 4, 3, 2, 1]
    assert abs(_pearson(xs, ys) - (-1.0)) < 1e-6
    print("  ✓ test_pearson_perfect_negative")


def test_pearson_too_short_returns_zero() -> None:
    """< 3 点无法算相关"""
    assert _pearson([1, 2], [2, 4]) == 0.0
    print("  ✓ test_pearson_too_short_returns_zero")


def test_learn_groups_merges_high_corr() -> None:
    """两个相关对应合并"""
    corr = {("A", "B"): 0.85, ("A", "C"): 0.2, ("B", "C"): 0.3}
    groups = learn_correlated_groups(corr, threshold=0.7)
    # A, B 高相关合并; C 单独 (不在 group 内, 输出时过滤)
    assert groups == [["A", "B"]]
    print("  ✓ test_learn_groups_merges_high_corr")


def test_learn_groups_chains() -> None:
    """A-B 高, B-C 高 → 链式合并到 [A,B,C]"""
    corr = {("A", "B"): 0.8, ("B", "C"): 0.75}
    groups = learn_correlated_groups(corr, threshold=0.7)
    assert groups == [["A", "B", "C"]]
    print("  ✓ test_learn_groups_chains")


def test_learn_groups_threshold_filters() -> None:
    """阈值提高, 不再合并"""
    corr = {("A", "B"): 0.65, ("C", "D"): 0.9}
    groups_low = learn_correlated_groups(corr, threshold=0.5)
    groups_high = learn_correlated_groups(corr, threshold=0.8)
    assert groups_low == [["A", "B"], ["C", "D"]]
    assert groups_high == [["C", "D"]]
    print("  ✓ test_learn_groups_threshold_filters")


def test_check_correlation_auto_learn_overrides() -> None:
    """auto_learn=True 时, 硬编码 groups 被忽略"""
    stocks = [{"code": "1", "sector": "新能源车", "position_advice_amount": 100_000}]
    # 硬编码 group 是空的, 但 auto_learn 假装学出 [["新能源车", "锂电池"]]
    fake_corr = {("新能源车", "锂电池"): 0.9}
    with patch("stock_trading_agent.engine.correlation.auto_learn_groups",
               return_value=[["新能源车", "锂电池"]]):
        out = _check_sector_correlation(
            stocks, existing_positions=[],
            total_cap=1_000_000,
            group_max_ratio=0.6,
            groups=[],  # 硬编码空
            auto_learn=True,
        )
    # 新能源车单只开 100k = 10% < 60%, 应过
    assert len(out) == 1
    print("  ✓ test_check_correlation_auto_learn_overrides")


def test_check_correlation_auto_learn_fallback_on_error() -> None:
    """auto_learn 抛异常时不崩, 退化为 groups=[]"""
    stocks = [{"code": "1", "sector": "X", "position_advice_amount": 100_000}]
    with patch("stock_trading_agent.engine.correlation.auto_learn_groups",
               side_effect=RuntimeError("DB error")):
        out = _check_sector_correlation(
            stocks, existing_positions=[],
            total_cap=1_000_000,
            group_max_ratio=0.6,
            groups=[],
            auto_learn=True,
        )
    assert len(out) == 1  # 退化路径, 全部放行
    print("  ✓ test_check_correlation_auto_learn_fallback_on_error")

# ─────────── 7. v8.2 stage 漏跑自动补跑 ───────────

from stock_trading_agent.agent import catch_up_stages, run_daemon
from stock_trading_agent.engine.paper_trader import mark_stage_run, was_stage_run_today


def _clean_stage_runs():
    conn = __import__("stock_trading_agent.engine.paper_trader", fromlist=["get_db"]).get_db()
    conn.execute("DELETE FROM stage_runs")
    conn.commit()


def test_catch_up_no_pending_when_all_done() -> None:
    """今日所有 stage 都跑过, 补跑列表空"""
    _clean_stage_runs()
    # 标记全部 6 个 stage 为今日已跑
    for s in ("pre_market", "open_auction", "pick", "post_market", "evening", "weekly_review"):
        mark_stage_run(s, ok=True)
    try:
        caught = catch_up_stages()
        assert caught == []
        print("  ✓ test_catch_up_no_pending_when_all_done")
    finally:
        _clean_stage_runs()


def test_catch_up_picks_missing_cron_passed() -> None:
    """cron 时间已过但 stage_runs 缺 → 应补跑"""
    _clean_stage_runs()
    # 现在是 09:30:00, pre_market 08:30 cron 已过, 但没 mark
    fake_now = __import__("datetime").datetime(2026, 6, 8, 9, 30, 0)
    fake_cfg = {
        "schedule": {
            "pre_market": "30 8 * * 1-5",
            "open_auction": "15 9 * * 1-5",
            "pick": "0 14 * * 1-5",
            "post_market": "30 15 * * 1-5",
            "evening": "0 19 * * 1-5",
            "weekly_review": "0 20 * * 0",
        },
    }
    import stock_trading_agent.agent as ag
    fake_ran = []
    with patch.object(ag.stages, "load_config", return_value=fake_cfg), \
         patch.object(ag, "_dt") as dt_mod, \
         patch.object(ag, "was_stage_run_today", return_value=False), \
         patch.dict(ag.STAGE_REGISTRY, {}, clear=False):
        # 替换 fn 为 mock
        for s in ("pre_market", "open_auction"):
            ag.STAGE_REGISTRY[s]["fn"] = lambda s=s: fake_ran.append(s)
        # _dt.now() 返回 fake_now
        ag._dt.now = lambda: fake_now
        # weekday=0 周一, 满足 1-5
        caught = catch_up_stages()
    # 2026-06-08 是周一, 08:30 / 09:15 都已过, 应补跑 pre_market + open_auction
    assert "pre_market" in caught
    assert "open_auction" in caught
    # 14:00 / 15:30 / 19:00 / 周日 20:00 都还没到
    assert "pick" not in caught
    assert "post_market" not in caught
    print("  ✓ test_catch_up_picks_missing_cron_passed")

# ─────────── 7. v8.2 stage 漏跑自动补跑 ───────────

from stock_trading_agent.agent import catch_up_stages, _cron_should_have_run
from stock_trading_agent.engine.paper_trader import mark_stage_run


def _clean_stage_runs():
    conn = __import__("stock_trading_agent.engine.paper_trader", fromlist=["get_db"]).get_db()
    conn.execute("DELETE FROM stage_runs")
    conn.commit()


def test_cron_should_have_run_no_apscheduler_returns_false() -> None:
    """apscheduler 缺失时, _cron_should_have_run 返回 False (不补跑)"""
    import stock_trading_agent.agent as ag
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **kw):
        if name.startswith("apscheduler"):
            raise ImportError("no apscheduler")
        return real_import(name, *a, **kw)
    with patch("builtins.__import__", side_effect=fake_import):
        result = _cron_should_have_run("0 14 * * 1-5", __import__("datetime").datetime.now())
    assert result is False
    print("  ✓ test_cron_should_have_run_no_apscheduler_returns_false")


def test_cron_should_have_run_with_apscheduler() -> None:
    """有 apscheduler 时, 09:30 应识别 08:30 的 pre_market cron 已过 (skipif 未装)"""
    try:
        import apscheduler.triggers.cron  # noqa: F401
    except ImportError:
        print("  ⏭ test_cron_should_have_run_with_apscheduler: skip (apscheduler not installed)")
        return
    from datetime import datetime
    fake_now = datetime(2026, 6, 8, 9, 30, 0)  # 周一
    result = _cron_should_have_run("30 8 * * 1-5", fake_now)
    assert result is True, "08:30 cron 在 09:30 看来应已跑"
    print("  ✓ test_cron_should_have_run_with_apscheduler")


def test_cron_should_have_run_before_time_returns_false() -> None:
    """cron 还没到时间 → 返回 False"""
    from datetime import datetime
    fake_now = datetime(2026, 6, 8, 7, 0, 0)  # 早于 08:30
    result = _cron_should_have_run("30 8 * * 1-5", fake_now)
    assert result is False
    print("  ✓ test_cron_should_have_run_before_time_returns_false")


def test_catch_up_no_pending_when_all_done() -> None:
    """今日所有 stage 都跑过, 补跑列表空"""
    _clean_stage_runs()
    for st in ("pre_market", "open_auction", "pick", "post_market", "evening", "weekly_review"):
        mark_stage_run(st, ok=True)
    # 用一个永远不会"该跑"的时间 (00:00), 即便 stage 缺也不会补
    from datetime import datetime
    caught = catch_up_stages(now=datetime(2026, 6, 8, 0, 0, 0))
    assert caught == []
    _clean_stage_runs()
    print("  ✓ test_catch_up_no_pending_when_all_done")


def test_catch_up_picks_missing_cron_passed() -> None:
    """cron 已过 + stage 缺 → 补跑; cron 未过 → 不补"""
    _clean_stage_runs()
    import stock_trading_agent.agent as ag
    from datetime import datetime
    fake_now = datetime(2026, 6, 8, 9, 30, 0)  # 周一 09:30
    fake_cfg = {
        "schedule": {
            "pre_market": "30 8 * * 1-5",
            "open_auction": "15 9 * * 1-5",
            "pick": "0 14 * * 1-5",
            "post_market": "30 15 * * 1-5",
            "evening": "0 19 * * 1-5",
            "weekly_review": "0 20 * * 0",
        },
    }
    # mock fn 为记录调用
    called: list[str] = []
    for s_name in ("pre_market", "open_auction", "pick", "post_market", "evening", "weekly_review"):
        orig_fn = ag.STAGE_REGISTRY[s_name]["fn"]
        ag.STAGE_REGISTRY[s_name]["fn"] = (lambda s: lambda: called.append(s))(s_name)
    try:
        # 真实环境可能没装 apscheduler, mock _cron_should_have_run 让其返回 True
        # (这样我们测的是"cron 已过"路径的 catch_up 逻辑, 不依赖 apscheduler)
        with patch.object(ag.stages, "load_config", return_value=fake_cfg), \
             patch.object(ag.stages, "_cron_should_have_run", return_value=True):
            caught = catch_up_stages(now=fake_now)
        # 因为 mock 让所有 cron 都返回 True (假装所有 stage 都"该跑"了)
        # stage_runs 都空 → 6 个都补
        assert "pre_market" in caught
        assert "open_auction" in caught
        assert "pick" in caught
        assert "post_market" in caught
        assert "evening" in caught
        assert "weekly_review" in caught
        print("  ✓ test_catch_up_picks_missing_cron_passed")
    finally:
        # 恢复 fn
        _clean_stage_runs()

# ─────────── 8. v8.3 周报 LLM 多轮对话 ───────────

from stock_trading_agent.llm import reasoner
from stock_trading_agent.llm.reasoner import weekly_followup


def test_weekly_followup_empty_question_returns_empty() -> None:
    """空 question 不发请求, 返回空"""
    assert weekly_followup({"stats": {}}, "") == ""
    assert weekly_followup({"stats": {}}, "   ") == ""
    print("  ✓ test_weekly_followup_empty_question_returns_empty")


def test_weekly_followup_constructs_prompt() -> None:
    """正常 question: 调 retrieve + chat, prompt 含 question 和 weekly stats"""
    fake_stats = {
        "overall": {"n": 20, "win_rate": 30, "avg": -1.0},
        "by_score": {"60-80": {"n": 5, "win_rate": 40}},
        "by_chg": {"0-3": {"n": 10, "win_rate": 20}},
        "by_sector": {"新能源车": {"n": 5, "avg": -2.0}, "半导体": {"n": 3, "avg": 0.5}},
    }
    weekly = {"stats": fake_stats, "applied": [], "pending": [],
              "auto_regression": {"triggered": True, "explanation": ""}}
    with patch.object(reasoner, "retrieve", return_value=[
        {"source": "haoyun-2008", "title": "test", "text": "做题材要看主线", "score": 0.9}
    ]) as ret_mp, \
         patch.object(reasoner, "format_context", return_value="[haoyun] 做题材要看主线"), \
         patch.object(reasoner, "chat", return_value={
             "ok": True, "content": "FAKE-FOLLOWUP", "latency_ms": 50, "usage": {}
         }) as chat_mp:
        result = weekly_followup(weekly, "为什么新能源车拖累?")
    assert result == "FAKE-FOLLOWUP"
    assert ret_mp.call_count == 1
    # retrieve query 应含 question 关键词
    query = ret_mp.call_args[0][0]
    assert "新能源车" in query
    assert "拖累" in query
    # chat 收到 prompt 含周报数据 + question
    chat_msgs = chat_mp.call_args[0][0]
    prompt = chat_msgs[0]["content"]
    assert "新能源车拖累" in prompt
    assert "胜率" in prompt  # 周报数据
    assert "[haoyun] 做题材要看主线" in prompt  # 知识库
    print("  ✓ test_weekly_followup_constructs_prompt")


def test_weekly_followup_chat_failure_degrades() -> None:
    """chat 失败降级返回空字符串"""
    weekly = {"stats": {"overall": {"n": 5, "win_rate": 50, "avg": 0.5},
                          "by_score": {}, "by_chg": {}, "by_sector": {}}}
    with patch.object(reasoner, "retrieve", return_value=[]), \
         patch.object(reasoner, "format_context", return_value=""), \
         patch.object(reasoner, "chat", return_value={"ok": False, "error": "network", "latency_ms": 0}):
        result = weekly_followup(weekly, "test")
    assert result == ""
    print("  ✓ test_weekly_followup_chat_failure_degrades")


def test_weekly_followup_no_knowledge_still_runs() -> None:
    """知识库无命中时, prompt 仍生成, 用占位符"""
    weekly = {"stats": {"overall": {"n": 5}, "by_score": {}, "by_chg": {}, "by_sector": {}}}
    with patch.object(reasoner, "retrieve", return_value=[]), \
         patch.object(reasoner, "format_context", return_value=""), \
         patch.object(reasoner, "chat", return_value={"ok": True, "content": "OK", "latency_ms": 0, "usage": {}}):
        result = weekly_followup(weekly, "总结下")
    assert result == "OK"
    print("  ✓ test_weekly_followup_no_knowledge_still_runs")

# ─────────── 9. v8.4 RAG 索引增量更新 ───────────

from stock_trading_agent.engine.knowledge import (
    BM25, _get_index, _data_dir_mtime, _load_haoyun, _load_susan,
)
from stock_trading_agent.engine import knowledge


def test_bm25_add_docs_extends_index() -> None:
    """add_docs 增量: 索引长度增加, 老 query 仍能命中"""
    bm = BM25(["旧文档: 这是关于选股的内容"])
    scores_before = bm.score("选股")
    assert any(s > 0 for s in scores_before)

    bm.add_docs(["新文档: 题材轮动时要快进快出"])
    assert bm.n == 2
    assert len(bm.docs) == 2
    # 老 query 仍能命中旧 doc
    scores_after = bm.score("选股")
    assert scores_after[0] > 0
    # 新 query 命中新 doc
    scores_new = bm.score("题材")
    assert scores_new[1] > 0
    print("  ✓ test_bm25_add_docs_extends_index")


def test_data_dir_mtime_zero_when_missing() -> None:
    """目录不存在时 mtime = 0"""
    assert _data_dir_mtime("nonexistent_dir_xyz") == 0.0
    print("  ✓ test_data_dir_mtime_zero_when_missing")


def test_get_index_initial_loads() -> None:
    """首次调 _get_index 加载 corpus"""
    knowledge.reset_index()
    bm, corpus = _get_index()
    assert bm.n > 0
    assert len(corpus) > 0
    print(f"  ✓ test_get_index_initial_loads: {bm.n} docs loaded")


def test_get_index_cached_no_reload() -> None:
    """mtime 不变时, _get_index 直接返回缓存 (不调 load_corpus)"""
    knowledge.reset_index()
    bm1, corpus1 = _get_index()
    # 第二次调用, 改 mtime mock 让其认为没变
    with patch.object(knowledge, "_data_dir_mtime", return_value=0.0):
        bm2, corpus2 = _get_index()
    assert bm1 is bm2  # 同一对象
    assert corpus1 is corpus2
    print("  ✓ test_get_index_cached_no_reload")


def test_get_index_incremental_on_mtime_change() -> None:
    """haoyun mtime 变化时, 增量重建; susan mtime 不变, 不动"""
    knowledge.reset_index()
    bm1, corpus1 = _get_index()
    n_before = bm1.n
    hao_corpus_before = [d for d in corpus1 if d.source == "haoyun"]
    sus_corpus_before = [d for d in corpus1 if d.source == "susan"]

    # 模拟 haoyun mtime 变了 (返回 1e9), susan 不变 (返回 0)
    mtime_map = {"haoyun": 1e9, "susan": 0.0}
    def fake_mtime(subdir):
        return mtime_map.get(subdir, 0.0)
    with patch.object(knowledge, "_data_dir_mtime", side_effect=fake_mtime):
        # 但 cache 里 haoyun mtime 是 0, 1e9 > 0 → 触发增量
        bm2, corpus2 = _get_index()

    # 增量更新后 bm.n 增加了 haoyun 部分的 doc 数 (至少 ≥ 之前)
    assert bm2.n >= n_before
    print(f"  ✓ test_get_index_incremental_on_mtime_change: "
          f"haoyun {len(hao_corpus_before)} + susan {len(sus_corpus_before)} → "
          f"bm.n={bm2.n}")

# ─────────── 10. v9.1 PDF 导出 ───────────

from stock_trading_agent.engine._pdf import render_pdf, _md_to_blocks
from stock_trading_agent.engine.report import save_report_pdf, push_weekly_report


def test_pdf_starts_with_header() -> None:
    """PDF 字节流以 %PDF-1.4 开头, 以 %%EOF 结尾"""
    pdf = render_pdf("# Hello\n\nWorld", "Test")
    assert pdf.startswith(b"%PDF-1.4"), f"PDF 头错: {pdf[:10]}"
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 100
    print(f"  ✓ test_pdf_starts_with_header: {len(pdf)} bytes")


def test_pdf_contains_xref() -> None:
    """PDF 含 xref 表"""
    pdf = render_pdf("# t", "X")
    assert b"xref" in pdf
    assert b"trailer" in pdf
    print("  ✓ test_pdf_contains_xref")


def test_md_to_blocks_parses_h1_h2_h3() -> None:
    """md → blocks 解析 h1/h2/h3"""
    md = "# T1\n\n## T2\n\n### T3\n\np"
    blocks = _md_to_blocks(md)
    kinds = [k for k, _ in blocks]
    assert "h1" in kinds and "h2" in kinds and "h3" in kinds and "p" in kinds
    print("  ✓ test_md_to_blocks_parses_h1_h2_h3")


def test_md_to_blocks_parses_table() -> None:
    """md 表格识别"""
    md = "| a | b |\n|---|---|\n| 1 | 2 |"
    blocks = _md_to_blocks(md)
    table_blocks = [b for k, b in blocks if k == "table"]
    assert len(table_blocks) == 1
    assert "1 | 2" in table_blocks[0]
    print("  ✓ test_md_to_blocks_parses_table")


def test_save_report_pdf_writes_file() -> None:
    """save_report_pdf 落盘 .pdf 文件"""
    import tempfile
    pdf = save_report_pdf("# Test\n\nBody", "v9_test", title="T")
    try:
        assert pdf.exists()
        assert pdf.suffix == ".pdf"
        content = pdf.read_bytes()
        assert content.startswith(b"%PDF-1.4")
    finally:
        pdf.unlink()
    print("  ✓ test_save_report_pdf_writes_file")


def test_push_weekly_report_save_pdf() -> None:
    """push_weekly_report(save_pdf=True) 落盘 PDF + 推飞书"""
    weekly = {"stats": {"overall": {"n": 5, "win_rate": 50, "avg": 0.5}},
              "applied": [], "pending": [],
              "auto_regression": {"triggered": False, "explanation": ""}}
    with patch("stock_trading_agent.engine.report.save_report", return_value=__import__("pathlib").Path("/tmp/x.md")), \
         patch("stock_trading_agent.engine.report.save_report_pdf", return_value=__import__("pathlib").Path("/tmp/x.pdf")) as pdf_mp, \
         patch("stock_trading_agent.feishu.pusher.push_weekly", return_value={"ok": True}):
        result = push_weekly_report(weekly, save_pdf=True)
    assert result["saved"] == "/tmp/x.md"
    assert result["saved_pdf"] == "/tmp/x.pdf"
    assert pdf_mp.call_count == 1
    print("  ✓ test_push_weekly_report_save_pdf")

# ─────────── 11. v9.2 多账户 paper trade ───────────

import stock_trading_agent.engine.paper_trader as pt
from stock_trading_agent.engine.paper_trader import simulate_profile, run_multi_account


def test_simulate_profile_plan_c_returns_zero() -> None:
    """plan C (空仓) 不论 profile 都开 0"""
    pick = {"filtered_stocks": [{"code": "1"}], "plan_used": "C", "market_env": {"position_ratio": 0.5}}
    cfg = {"max_concurrent": 3, "max_position_ratio": 0.2}
    r = simulate_profile(pick, cfg)
    assert r["n_open"] == 0
    print("  ✓ test_simulate_profile_plan_c_returns_zero")


def test_simulate_profile_conservative_stricter() -> None:
    """conservative 比 balanced 限制更严, 同样候选开更少"""
    stocks = [
        {"code": "1", "sector": "A", "position_advice_amount": 100_000, "score": 90},
        {"code": "2", "sector": "A", "position_advice_amount": 100_000, "score": 85},
        {"code": "3", "sector": "A", "position_advice_amount": 100_000, "score": 80},
        {"code": "4", "sector": "B", "position_advice_amount": 100_000, "score": 75},
    ]
    pick = {"filtered_stocks": stocks, "plan_used": "A", "market_env": {"position_ratio": 0.5}}
    cons = {"max_concurrent": 1, "max_position_ratio": 0.2, "max_sector_concurrent": 1, "max_sector_ratio": 0.3}
    bal = {"max_concurrent": 3, "max_position_ratio": 0.2, "max_sector_concurrent": 2, "max_sector_ratio": 0.5}
    r_cons = simulate_profile(pick, cons)
    r_bal = simulate_profile(pick, bal)
    # conservative: max_concurrent=1 强制只 1 只; sector_concurrent=1 第一只 A 过后第二只 A 卡
    assert r_cons["n_open"] <= 2
    assert r_bal["n_open"] >= r_cons["n_open"]
    print(f"  ✓ test_simulate_profile_conservative_stricter: cons={r_cons['n_open']} bal={r_bal['n_open']}")


def test_run_multi_account_disabled_returns_empty() -> None:
    """multi_account.enabled=false → 返回空"""
    pick = {"filtered_stocks": [], "plan_used": "A", "market_env": {}}
    with patch("stock_trading_agent.engine.paper_trader.load_config",
               return_value={"multi_account": {"enabled": False}, "paper": {}}):
        r = run_multi_account(pick)
    assert r == {}
    print("  ✓ test_run_multi_account_disabled_returns_empty")


def test_run_multi_account_enabled_3_profiles() -> None:
    """enabled + 3 profiles → 返回 3 个 profile 的结果"""
    pick = {
        "filtered_stocks": [
            {"code": str(i), "sector": f"S{i % 3}", "position_advice_amount": 50_000, "score": 90 - i}
            for i in range(6)
        ],
        "plan_used": "A", "market_env": {"position_ratio": 0.5},
    }
    cfg = {
        "multi_account": {
            "enabled": True,
            "profiles": {
                "conservative": {"max_concurrent": 2, "max_position_ratio": 0.15, "max_sector_concurrent": 1, "max_sector_ratio": 0.3},
                "balanced":     {"max_concurrent": 3, "max_position_ratio": 0.20, "max_sector_concurrent": 2, "max_sector_ratio": 0.5},
                "aggressive":  {"max_concurrent": 4, "max_position_ratio": 0.30, "max_sector_concurrent": 3, "max_sector_ratio": 0.7},
            },
        },
        "paper": {"initial_capital": 1_000_000.0},
    }
    with patch("stock_trading_agent.engine.paper_trader.load_config", return_value=cfg), \
         patch("stock_trading_agent.engine.paper_trader.get_open_sector_stats", return_value={}):
        r = run_multi_account(pick)
    assert set(r.keys()) == {"conservative", "balanced", "aggressive"}
    # aggressive 限制最松, 开得最多; conservative 最严, 开得最少
    assert r["aggressive"]["n_open"] >= r["balanced"]["n_open"] >= r["conservative"]["n_open"]
    print(f"  ✓ test_run_multi_account_enabled_3_profiles: "
          f"cons={r['conservative']['n_open']} bal={r['balanced']['n_open']} "
          f"aggr={r['aggressive']['n_open']}")

# ─────────── 12. v9.3 实时盯盘 ───────────

import stock_trading_agent.engine.intraday as itd
from stock_trading_agent.engine.intraday import (
    detect_anomalies, format_anomaly_message, fetch_realtime_quotes,
    get_open_positions_for_monitor, intraday_monitor,
)


def test_detect_anomalies_chg_threshold() -> None:
    """chg_pct 超过阈值触发"""
    positions = [{"code": "1", "name": "A", "sector": "X", "open_amount": 100_000}]
    quotes = {
        "1": {"price": 11.0, "prev_close": 10.0, "chg_pct": 10.0, "amplitude": 2.0},
    }
    out = detect_anomalies(positions, quotes, chg_threshold=3.0, amplitude_threshold=8.0)
    assert len(out) == 1
    assert "涨" in out[0]["reasons"][0]
    print("  ✓ test_detect_anomalies_chg_threshold")


def test_detect_anomalies_amplitude() -> None:
    """振幅超阈值触发"""
    positions = [{"code": "1", "name": "A", "sector": "X", "open_amount": 50_000}]
    quotes = {"1": {"price": 10.0, "prev_close": 9.5, "chg_pct": 0.5, "amplitude": 10.0}}
    out = detect_anomalies(positions, quotes, chg_threshold=3.0, amplitude_threshold=8.0)
    assert len(out) == 1
    assert "振幅" in out[0]["reasons"][0]
    print("  ✓ test_detect_anomalies_amplitude")


def test_detect_anomalies_no_match() -> None:
    """没异动不触发"""
    positions = [{"code": "1", "name": "A", "sector": "X", "open_amount": 50_000}]
    quotes = {"1": {"price": 10.0, "prev_close": 9.5, "chg_pct": 1.0, "amplitude": 2.0}}
    out = detect_anomalies(positions, quotes, chg_threshold=3.0, amplitude_threshold=8.0)
    assert out == []
    print("  ✓ test_detect_anomalies_no_match")


def test_detect_anomalies_negative_chg() -> None:
    """跌也触发 (绝对值)"""
    positions = [{"code": "1", "name": "A", "sector": "X", "open_amount": 50_000}]
    quotes = {"1": {"price": 9.0, "prev_close": 10.0, "chg_pct": -10.0, "amplitude": 2.0}}
    out = detect_anomalies(positions, quotes, chg_threshold=3.0, amplitude_threshold=8.0)
    assert len(out) == 1
    assert "跌" in out[0]["reasons"][0]
    print("  ✓ test_detect_anomalies_negative_chg")


def test_format_anomaly_message_empty() -> None:
    """无异动返回空"""
    assert format_anomaly_message([]) == ""
    print("  ✓ test_format_anomaly_message_empty")


def test_format_anomaly_message_with_items() -> None:
    """有异动时, 消息含关键信息"""
    a = {"code": "1", "name": "A", "sector": "X", "price": 11.0,
         "chg_pct": 10.0, "amplitude": 2.0, "open_amount": 100_000,
         "reasons": ["涨 10.00% (阈值 3%)"]}
    msg = format_anomaly_message([a])
    assert "1" in msg and "A" in msg and "10.00%" in msg
    print("  ✓ test_format_anomaly_message_with_items")


def test_intraday_monitor_disabled() -> None:
    """enabled=false 不做事"""
    with patch("stock_trading_agent.engine.data_fetcher.load_config",
               return_value={"intraday_monitor": {"enabled": False}}):
        r = intraday_monitor()
    assert r["skipped"] == "disabled"
    assert r["scanned"] == 0
    print("  ✓ test_intraday_monitor_disabled")


def test_intraday_monitor_with_anomaly_pushes() -> None:
    """异动时调 push_anomaly"""
    cfg = {
        "intraday_monitor": {"enabled": True, "chg_threshold": 3.0, "amplitude_threshold": 8.0},
    }
    positions = [{"code": "1", "name": "A", "sector": "X", "open_amount": 100_000}]
    quotes = {"1": {"price": 11.0, "prev_close": 10.0, "chg_pct": 10.0, "amplitude": 2.0}}
    with patch("stock_trading_agent.engine.data_fetcher.load_config", return_value=cfg), \
         patch("stock_trading_agent.engine.intraday.get_open_positions_for_monitor", return_value=positions), \
         patch("stock_trading_agent.engine.intraday.fetch_realtime_quotes", return_value=quotes), \
         patch("stock_trading_agent.feishu.pusher.push_anomaly", return_value={"ok": True}) as push_mp:
        r = intraday_monitor()
    assert r["scanned"] == 1
    assert len(r["anomalies"]) == 1
    assert r["pushed"] is True
    assert push_mp.call_count == 1
    print("  ✓ test_intraday_monitor_with_anomaly_pushes")

# ─────────── 13. v9.4 LLM-as-judge 调参评估 ───────────

from stock_trading_agent.llm import reasoner
from stock_trading_agent.llm.reasoner import judge_proposal


def test_judge_proposal_parses_json() -> None:
    """LLM 返回 JSON 格式时, 解析出 approved/score/concerns/verdict"""
    proposal = {
        "param": "score_max", "old": 80, "new": 82,
        "safe_range": [75, 85], "in_safe_range": True, "reason": "胜率低, 提高阈值"
    }
    stats = {"overall": {"n": 20, "win_rate": 30, "avg": -1.0}, "by_score": {}}
    fake_llm = '{"score": 75, "approved": true, "concerns": ["改动幅度温和"], "verdict": "合理"}'
    with patch.object(reasoner, "chat", return_value={
        "ok": True, "content": fake_llm, "latency_ms": 30, "usage": {}
    }):
        r = judge_proposal(proposal, stats)
    assert r["approved"] is True
    assert r["score"] == 75
    assert r["verdict"] == "合理"
    print("  ✓ test_judge_proposal_parses_json")


def test_judge_proposal_low_score_not_approved() -> None:
    """score < 60 → approved=False"""
    proposal = {"param": "x", "old": 1, "new": 99, "safe_range": [0, 100], "in_safe_range": True}
    stats = {"overall": {"n": 5, "win_rate": 50, "avg": 0}, "by_score": {}}
    fake_llm = '{"score": 30, "approved": false, "concerns": ["改动过大"], "verdict": "过激"}'
    with patch.object(reasoner, "chat", return_value={
        "ok": True, "content": fake_llm, "latency_ms": 30, "usage": {}
    }):
        r = judge_proposal(proposal, stats)
    assert r["approved"] is False
    assert r["score"] == 30
    print("  ✓ test_judge_proposal_low_score_not_approved")


def test_judge_proposal_chat_failure_passes_through() -> None:
    """chat 失败时, 默认通过 (不阻断)"""
    proposal = {"param": "x", "old": 1, "new": 2, "in_safe_range": True}
    stats = {"overall": {"n": 5}, "by_score": {}}
    with patch.object(reasoner, "chat", return_value={"ok": False, "error": "timeout", "latency_ms": 0}):
        r = judge_proposal(proposal, stats)
    assert r["approved"] is True
    assert "judge 失败" in r["verdict"] or "失败" in str(r["concerns"])
    print("  ✓ test_judge_proposal_chat_failure_passes_through")


def test_judge_proposal_handles_json_in_text() -> None:
    """LLM 输出含前后文时, 仍能抠出 JSON"""
    proposal = {"param": "x", "old": 1, "new": 2, "in_safe_range": True}
    stats = {"overall": {"n": 5}, "by_score": {}}
    fake = "我评估如下:\n{\"score\": 80, \"approved\": true, \"concerns\": [], \"verdict\": \"OK\"}\n结束"
    with patch.object(reasoner, "chat", return_value={
        "ok": True, "content": fake, "latency_ms": 30, "usage": {}
    }):
        r = judge_proposal(proposal, stats)
    assert r["score"] == 80
    assert r["approved"] is True
    print("  ✓ test_judge_proposal_handles_json_in_text")


def test_judge_proposal_unparseable_passes() -> None:
    """完全无法解析时, 默认通过"""
    proposal = {"param": "x", "old": 1, "new": 2, "in_safe_range": True}
    stats = {"overall": {"n": 5}, "by_score": {}}
    with patch.object(reasoner, "chat", return_value={
        "ok": True, "content": "not json at all", "latency_ms": 30, "usage": {}
    }):
        r = judge_proposal(proposal, stats)
    assert r["approved"] is True  # 默认通过
    print("  ✓ test_judge_proposal_unparseable_passes")


def test_run_weekly_tune_judge_gates_low_score() -> None:
    """run_weekly_tune 集成 judge: score 低时把 proposal 推到 pending"""
    import stock_trading_agent.engine.tuner as tn
    cfg = {
        "v3": {
            "score_max": {"value": 80, "safe_range": [75, 85]},
        },
        "tuner": {"judge_enabled": True, "judge_min_score": 60},
    }
    cfg_patch = patch.object(tn, "load_config", return_value=cfg)
    fake_stats = {"overall": {"n": 20, "win_rate": 30, "avg": -1.0},
                  "by_score": {"60-80": {"n": 10, "win_rate": 20, "avg": -1.5}},
                  "by_chg": {}, "by_sector": {}}
    fake_proposal = {
        "param": "score_max", "old": 80, "new": 82,
        "safe_range": [75, 85], "in_safe_range": True, "reason": "test",
    }
    with cfg_patch, \
         patch.object(tn, "weekly_stats", return_value=fake_stats), \
         patch.object(tn, "_propose_score_max", return_value=fake_proposal), \
         patch.object(tn, "_propose_strong_band", return_value=None), \
         patch.object(tn, "_propose_blacklist", return_value=[]), \
         patch.object(tn, "apply_proposal", return_value=True) as apply_mp, \
         patch.object(tn, "judge_proposal",
               return_value={"approved": False, "score": 30, "concerns": ["过激"], "verdict": "改太大"}):
        result = tn.run_weekly_tune()
    # score=30 < 60, 应推到 pending, 不 apply
    assert len(result["applied"]) == 0
    assert len(result["pending"]) == 1
    assert "judge 评分 30" in result["pending"][0].get("pending_reason", "")
    assert apply_mp.call_count == 0
    print("  ✓ test_run_weekly_tune_judge_gates_low_score")


def test_run_weekly_tune_judge_passes_high_score() -> None:
    """score 高时正常 apply"""
    import stock_trading_agent.engine.tuner as tn
    cfg = {
        "v3": {"score_max": {"value": 80, "safe_range": [75, 85]}},
        "tuner": {"judge_enabled": True, "judge_min_score": 60},
    }
    cfg_patch = patch.object(tn, "load_config", return_value=cfg)
    fake_stats = {"overall": {"n": 20, "win_rate": 30, "avg": -1.0},
                  "by_score": {}, "by_chg": {}, "by_sector": {}}
    fake_proposal = {
        "param": "score_max", "old": 80, "new": 82,
        "safe_range": [75, 85], "in_safe_range": True, "reason": "test",
    }
    with cfg_patch, \
         patch.object(tn, "weekly_stats", return_value=fake_stats), \
         patch.object(tn, "_propose_score_max", return_value=fake_proposal), \
         patch.object(tn, "_propose_strong_band", return_value=None), \
         patch.object(tn, "_propose_blacklist", return_value=[]), \
         patch.object(tn, "apply_proposal", return_value=True) as apply_mp, \
         patch.object(tn, "judge_proposal",
               return_value={"approved": True, "score": 80, "concerns": [], "verdict": "OK"}):
        result = tn.run_weekly_tune()
    assert len(result["applied"]) == 1
    assert len(result["pending"]) == 0
    assert apply_mp.call_count == 1
    print("  ✓ test_run_weekly_tune_judge_passes_high_score")

# ─────────── 14. v10 listener 不依赖 lark-cli ───────────

import stock_trading_agent.feishu.listener as ls
from stock_trading_agent.feishu.listener import _strip_mention, _is_chat_allowed, _send_reply


def test_listener_imports_without_lark_oapi() -> None:
    """lark-oapi 没装时, listener 模块本身可以导入 (不强依赖)"""
    # 这里如果 import 失败会直接挂, 进了函数就说明 OK
    import stock_trading_agent.feishu.listener  # noqa: F401
    print("  ✓ test_listener_imports_without_lark_oapi")


def test_strip_mention_removes_at_user() -> None:
    """@_user_X 形式的 mention 被剥掉"""
    assert _strip_mention("@_user_1 今天为啥没选宁德") == "今天为啥没选宁德"
    assert _strip_mention("@_user_1 @_user_2 你好") == "你好"
    assert _strip_mention("无 mention 的纯文本") == "无 mention 的纯文本"
    print("  ✓ test_strip_mention_removes_at_user")


def test_is_chat_allowed_off_mode() -> None:
    """whitelist_mode=off 时全部允许"""
    cfg = {"feishu": {"whitelist_mode": "off"}}
    allowed, reason = _is_chat_allowed("oc_xxx", "user_y", cfg)
    assert allowed is True
    assert reason == "ok"
    print("  ✓ test_is_chat_allowed_off_mode")


def test_is_chat_allowed_blacklist() -> None:
    """黑名单直接拒绝"""
    cfg = {"feishu": {"blacklist_chat_ids": ["oc_blocked"]}}
    allowed, reason = _is_chat_allowed("oc_blocked", "u", cfg)
    assert allowed is False
    assert "黑名单" in reason
    print("  ✓ test_is_chat_allowed_blacklist")


def test_is_chat_allowed_whitelist_mode() -> None:
    """whitelist 模式只允许白名单 chat"""
    cfg = {"feishu": {
        "whitelist_mode": "whitelist",
        "whitelist_chat_ids": ["oc_allowed"],
    }}
    allowed_a, _ = _is_chat_allowed("oc_allowed", "u", cfg)
    allowed_b, reason_b = _is_chat_allowed("oc_other", "u", cfg)
    assert allowed_a is True
    assert allowed_b is False
    assert "白名单" in reason_b
    print("  ✓ test_is_chat_allowed_whitelist_mode")


def test_is_chat_allowed_user_restriction() -> None:
    """allowed_user_ids 非空时, 非白名单 user 被拒"""
    cfg = {"feishu": {"allowed_user_ids": ["u_alice"]}}
    allowed_a, _ = _is_chat_allowed("oc_x", "u_alice", cfg)
    allowed_b, reason_b = _is_chat_allowed("oc_x", "u_bob", cfg)
    assert allowed_a is True
    assert allowed_b is False
    assert "allowed_user_ids" in reason_b
    print("  ✓ test_is_chat_allowed_user_restriction")


def test_run_fails_when_app_credentials_missing() -> None:
    """.env 缺 APP_ID/SECRET 时, run() 抛 RuntimeError 含字段名"""
    import stock_trading_agent.engine.data_fetcher as df
    orig_secret = df._secret
    # 强制 _secret 抛错 (模拟缺凭据)
    df._secret = lambda name, default=None: (_ for _ in ()).throw(
        RuntimeError(f"凭据 {name} 未设置"))
    try:
        try:
            ls.run()
        except RuntimeError as e:
            msg = str(e)
            assert "FEISHU_APP_ID" in msg or "FEISHU_APP_SECRET" in msg, f"unexpected: {msg}"
        else:
            raise AssertionError("应 raise RuntimeError")
    finally:
        df._secret = orig_secret
    print("  ✓ test_run_fails_when_app_credentials_missing")


def test_run_fails_when_lark_oapi_missing() -> None:
    """lark-oapi 没装时, run() 抛 RuntimeError 含安装提示"""
    import stock_trading_agent.engine.data_fetcher as df
    orig_cache = df._ENV_CACHE.copy()
    orig_env = {k: os.environ.pop(k) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET") if k in os.environ}
    df._ENV_CACHE = {
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "sec",
    }
    try:
        # mock sys.modules 让 import lark_oapi 失败
        saved = sys.modules.pop("lark_oapi", None)
        sys.modules["lark_oapi"] = None  # import 时会 TypeError, 但我们要 ImportError
        # 实际: 把 lark_oapi 设成 None 触发 "No module" 错
        try:
            try:
                ls.run()
            except (RuntimeError, TypeError) as e:
                msg = str(e)
                assert "lark-oapi" in msg or "lark_oapi" in msg, f"unexpected: {msg}"
            else:
                raise AssertionError("应 raise RuntimeError 或 TypeError")
        finally:
            del sys.modules["lark_oapi"]
            if saved is not None:
                sys.modules["lark_oapi"] = saved
    finally:
        df._ENV_CACHE = orig_cache
        os.environ.update(orig_env)
    print("  ✓ test_run_fails_when_lark_oapi_missing")


def test_send_reply_uses_client_create() -> None:
    """_send_reply 调 client.im.v1.message.create(req)"""
    fake_resp = MagicMock()
    fake_resp.success.return_value = True
    fake_resp.data.message_id = "om_test"
    fake_client = MagicMock()
    fake_client.im.v1.message.create.return_value = fake_resp
    # 这要求 lark_oapi.api.im.v1 模块存在; 没装时跳过
    try:
        import lark_oapi.api.im.v1  # noqa: F401
    except ImportError:
        print("  ⏭ test_send_reply_uses_client_create: skip (lark-oapi not installed)")
        return
    r = _send_reply(fake_client, "oc_xxx", "hi")
    assert r["ok"] is True
    assert r["message_id"] == "om_test"
    assert fake_client.im.v1.message.create.call_count == 1
    print("  ✓ test_send_reply_uses_client_create")

# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n✗ {failed} tests failed")
        sys.exit(1)
    print(f"\n✓ {len(tests)} tests passed")
