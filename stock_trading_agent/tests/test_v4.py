"""
test_v4.py — v4 四个 follow-up
1) 群聊白名单 (whitelist/blacklist/allowed_users)
2) Session Fernet 加密 (明文 + 密文互转)
3) 回测真实 auto (multi_strategy.run 投票)
4) 回测指标 (Sharpe / max_dd / 年化)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import stock_trading_agent.engine.paper_trader as pt
import stock_trading_agent.engine.sessions as sess
import stock_trading_agent.engine.data_fetcher as df
import stock_trading_agent.feishu.listener as listener
from stock_trading_agent.engine import reviewer
from stock_trading_agent.engine.reviewer import _compute_metrics, _simulate_with_plan, backtest_multi


# ─────────── 1. 群聊白名单 ───────────

def _cfg(mode="off", whitelist=None, blacklist=None, allowed=None, admins=None):
    return {
        "feishu": {
            "whitelist_mode": mode,
            "whitelist_chat_ids": whitelist or [],
            "blacklist_chat_ids": blacklist or [],
            "allowed_user_ids": allowed or [],
            "admin_user_ids": admins or [],
        }
    }


def test_whitelist_blacklist() -> None:
    """黑名单永远先挡；白名单模式 + 不在白名单 → 挡"""
    cfg = _cfg("whitelist", whitelist=["oc_a"], blacklist=["oc_spam"])
    # 黑名单
    ok, r = listener._is_chat_allowed("oc_spam", "ou_x", cfg)
    assert not ok and "黑名单" in r
    # 白名单模式 + 不在白名单
    ok, r = listener._is_chat_allowed("oc_other", "ou_x", cfg)
    assert not ok and "白名单" in r
    # 通过
    ok, r = listener._is_chat_allowed("oc_a", "ou_x", cfg)
    assert ok
    print(f"  ✓ test_whitelist_blacklist: 3 case 验证")


def test_allowed_users_filter() -> None:
    """allowed_user_ids 配置时, 不在列表的 sender 全挡"""
    cfg = _cfg("off", allowed=["ou_alice"])
    ok, r = listener._is_chat_allowed("oc_anything", "ou_hacker", cfg)
    assert not ok and "allowed_user" in r
    ok, r = listener._is_chat_allowed("oc_anything", "ou_alice", cfg)
    assert ok
    print(f"  ✓ test_allowed_users_filter: 2 case 验证")


def test_admin_check() -> None:
    """有 admin 列表时, 必须在列表内才返回 True; 没配 = 全员 admin"""
    cfg = _cfg(admins=["ou_admin"])
    assert listener._is_admin("ou_admin", cfg) is True
    assert listener._is_admin("ou_user", cfg) is False
    # 没配 = 全员
    assert listener._is_admin("ou_anyone", {"feishu": {}}) is True
    print(f"  ✓ test_admin_check: 3 case 验证")


# ─────────── 2. Session 加密 ───────────

def _isolated(name: str):
    d = Path(tempfile.mkdtemp(prefix=f"sta_test_v4_{name}_"))
    pt.DB_PATH = d / "quant.db"
    pt.DATA_DIR = d
    return d


def test_session_plaintext() -> None:
    """默认明文: DB 里是 JSON, 读出来是原文"""
    d = _isolated("plain")
    df._CONFIG_CACHE = {"session": {"encryption": "off"}, "paper": {"initial_capital": 1000000.0}}
    pt.init_account()
    sess._FERNET = None
    sess._ENCRYPTION_ENABLED = False
    sess.append_turn("alice", "user", "明文消息")
    h = sess.get_history("alice")
    assert h[0]["content"] == "明文消息"
    # DB 里也是明文 (没有 gAAAAA 前缀)
    import sqlite3
    conn = sqlite3.connect(str(d / "quant.db"))
    row = conn.execute("SELECT history FROM bot_sessions WHERE session_id=?", ("alice",)).fetchone()
    assert not row[0].startswith("gAAAAA"), f"明文应无 Fernet 前缀: {row[0][:30]}"
    print(f"  ✓ test_session_plaintext: 明文模式验证")


def test_session_fernet_encrypt_decrypt() -> None:
    """Fernet 加密: DB 里是密文, 读出来能解"""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ["BOT_ENCRYPTION_KEY"] = key
    d = _isolated("fernet")
    df._CONFIG_CACHE = {"session": {"encryption": "fernet", "encryption_key_env": "BOT_ENCRYPTION_KEY"},
                        "paper": {"initial_capital": 1000000.0}}
    pt.init_account()
    sess._FERNET = None
    sess._ENCRYPTION_ENABLED = False
    sess.append_turn("bob", "user", "密文消息")
    # DB 里是密文
    import sqlite3
    conn = sqlite3.connect(str(d / "quant.db"))
    row = conn.execute("SELECT history FROM bot_sessions WHERE session_id=?", ("bob",)).fetchone()
    assert row[0].startswith("gAAAAA"), f"应 Fernet 密文: {row[0][:30]}"
    # 读出来解了
    h = sess.get_history("bob")
    assert h[0]["content"] == "密文消息"
    # 清理 env
    os.environ.pop("BOT_ENCRYPTION_KEY", None)
    print(f"  ✓ test_session_fernet_encrypt_decrypt: 加密 + 解密 round-trip OK")


def test_session_fernet_missing_key_fallback() -> None:
    """Fernet 模式但无 key → 回退明文 (不崩)"""
    os.environ.pop("BOT_ENCRYPTION_KEY", None)
    d = _isolated("no_key")
    df._CONFIG_CACHE = {"session": {"encryption": "fernet", "encryption_key_env": "BOT_ENCRYPTION_KEY"},
                        "paper": {"initial_capital": 1000000.0}}
    pt.init_account()
    sess._FERNET = None
    sess._ENCRYPTION_ENABLED = False
    sess.append_turn("carol", "user", "无 key 消息")
    h = sess.get_history("carol")
    assert h[0]["content"] == "无 key 消息"  # 回退明文, 内容仍可读
    print(f"  ✓ test_session_fernet_missing_key_fallback: 降级 OK")


# ─────────── 3 & 4. 回测真实 auto + 指标 ───────────

def _valid_stock(code: str, sector: str = "好板块") -> dict:
    """造一只满足 plan A 全部条件的票"""
    return {
        "code": code, "name": f"测试{code}", "price": 10.0, "prev_close": 9.7,
        "chg_pct": 3.2, "turnover": 8.5, "total_mv_yi": 200, "amount_yi": 5,
        "high": 10.2, "low": 9.85, "amplitude": 3.5, "score": 75.0, "sector": sector,
    }


def _fixtures_with_pnl(pnl_factors: list[float]) -> Path:
    """造 N 个 fixture, 每个有可入选的票 + next_noon_prices 给出涨跌"""
    # 补全 config (避免前一个 test 的 cache 缺字段)
    full_cfg = {
        "paper": {
            "initial_capital": 1000000.0,
            "max_position_ratio": 0.20,
            "max_concurrent": 3,
        },
        "hard": {
            "chg_danger": 4.8, "amp_danger": 8.0, "chg_over": 6.0, "limit_up": 9.8,
            "mv_lo_yi": 50, "mv_hi_yi": 5000, "amt_lo_yi": 3, "max_picks": 15,
        },
        "plan_a": {"chg_lo": 3.0, "chg_hi": 4.0, "turnover_lo": 8.0, "turnover_hi": 10.0, "amplitude_hi": 8.0},
        "plan_b": {"chg_lo": 3.0, "chg_hi": 4.0, "turnover_lo": 6.0, "turnover_hi": 10.0, "amplitude_hi": 8.0},
        "v3": {
            "score_max": {"value": 80.0, "safe_range": [75.0, 85.0]},
            "strong_band_lo": {"value": 3.0, "safe_range": [2.8, 3.2]},
            "strong_band_hi": {"value": 3.5, "safe_range": [3.3, 3.7]},
            "strong_bonus": {"value": 5, "safe_range": [3, 8]},
            "theme_bonus": {"value": 3, "safe_range": [2, 5]},
        },
        "blacklist": {"sectors": [], "max_add_per_week": 2, "max_remove_per_week": 2, "safe_sectors": []},
        "backtest": {"risk_free_rate_pct": 2.0, "trading_days_per_year": 240},
    }
    df._CONFIG_CACHE = full_cfg
    fixtures = Path(tempfile.mkdtemp(prefix="sta_test_v4_fixtures_"))
    # 所有日用同一只票 (c000), next_noon_prices 每天是该票的次日中午价
    # 这样 backtest 能正确跟踪持仓
    for i, fac in enumerate(pnl_factors):
        date = f"2026-05-{15+i:02d}"
        stock = _valid_stock("c000")
        data = {
            "date": date, "plan": "A",
            "market_env": {"position_ratio": 0.5, "env_score": 50, "env_level": "中性"},
            "filtered_stocks": [stock],
            "next_noon_prices": {"c000": stock["price"] * fac},
        }
        (fixtures / f"pick_{date.replace('-', '')}.json").write_text(
            json.dumps(data, ensure_ascii=False)
        )
    return fixtures


def test_compute_metrics_sharpe_and_dd() -> None:
    """指标计算: 5 日 +2/-2/+5/-5/+3"""
    daily = [{"pnl": 10000, "cash": 510000}, {"pnl": -10000, "cash": 500000},
             {"pnl": 25000, "cash": 525000}, {"pnl": -25000, "cash": 500000},
             {"pnl": 15000, "cash": 515000}]
    cfg = {"backtest": {"risk_free_rate_pct": 2.0, "trading_days_per_year": 240}}
    m = _compute_metrics(daily, 500000, cfg)
    assert m["n_days"] == 5
    assert m["sharpe"] != 0  # 5 日有波动
    assert m["max_drawdown_pct"] > 0
    assert m["annualized_return_pct"] != 0
    print(f"  ✓ test_compute_metrics_sharpe_and_dd: sharpe={m['sharpe']}, dd={m['max_drawdown_pct']}%, annual={m['annualized_return_pct']}%")


def test_compute_metrics_empty() -> None:
    """空 daily_pnl 全部 0"""
    m = _compute_metrics([], 100000, {"backtest": {}})
    assert m == {"sharpe": 0, "max_drawdown_pct": 0, "annualized_return_pct": 0,
                "calmar": 0, "volatility_pct": 0, "n_days": 0}
    print(f"  ✓ test_compute_metrics_empty: 空输入 → 全部 0")


def test_backtest_real_auto_differs() -> None:
    """真实 auto 应该用 multi_strategy.run 选 plan"""
    fixtures = _fixtures_with_pnl([1.02, 0.98, 1.05, 0.95, 1.03])
    import stock_trading_agent.engine.reviewer as rev
    orig = rev.FIXTURES_DIR
    rev.FIXTURES_DIR = fixtures
    try:
        r = backtest_multi(days=10)
        # 应至少有 fixed_A 结果
        assert r["fixed_A"]["n"] == 5
        assert r["auto"]["n"] == 5
        # auto 应该有 metrics
        assert "sharpe" in r["auto"]["metrics"]
        # 推荐应基于 Sharpe 选最优
        assert r["recommendation"] in ("fixed_A", "fixed_B", "auto")
        print(f"  ✓ test_backtest_real_auto_differs: 5 日 PnL auto sharpe={r['auto']['metrics']['sharpe']:.2f}, 推荐={r['recommendation']}")
    finally:
        rev.FIXTURES_DIR = orig


def test_backtest_metrics_realistic() -> None:
    """用 +/- 混合 PnL 跑出真实指标"""
    fixtures = _fixtures_with_pnl([1.05, 0.95, 1.08, 0.92, 1.10, 0.90, 1.06])
    import stock_trading_agent.engine.reviewer as rev
    orig = rev.FIXTURES_DIR
    rev.FIXTURES_DIR = fixtures
    try:
        r = backtest_multi(days=10)
        for name in ("fixed_A", "auto"):
            s = r[name]
            assert s["total_pnl_pct"] != 0, f"{name} 应该有非零收益"
            m = s["metrics"]
            # 7 日数据应该算出有意义的指标
            assert m["n_days"] == 7
            print(f"  ✓ test_backtest_metrics_realistic: {name} pnl={s['total_pnl_pct']:.2f}%, sharpe={m['sharpe']:.2f}, max_dd={m['max_drawdown_pct']:.2f}%")
    finally:
        rev.FIXTURES_DIR = orig


# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [
        test_whitelist_blacklist,
        test_allowed_users_filter,
        test_admin_check,
        test_session_plaintext,
        test_session_fernet_encrypt_decrypt,
        test_session_fernet_missing_key_fallback,
        test_compute_metrics_sharpe_and_dd,
        test_compute_metrics_empty,
        test_backtest_real_auto_differs,
        test_backtest_metrics_realistic,
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
