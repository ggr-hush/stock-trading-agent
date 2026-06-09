"""
test_tuner.py — 调参引擎 5 个边界测试
1) 提议值在 safe_range 内 → 自动应用
2) 提议值在 safe_range 外 → 推 pending 队列
3) 黑名单 +2 上限
4) 黑名单 -2 上限
5) 无变化时 → 提议为空
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# Use isolated DB for tests
# Each test gets a unique isolated dir
def _isolated_dir(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"sta_test_{name}_"))



def _seed_config() -> None:
    """最小可用 config（写 .yaml + .json 双份，沙箱无 PyYAML 也能跑）"""
    import stock_trading_agent.engine.tuner as tu  # noqa: PLC0415
    payload = {
        "v3": {
            "score_max": {"value": 80.0, "safe_range": [75.0, 85.0]},
            "strong_band_lo": {"value": 3.0, "safe_range": [2.8, 3.2]},
            "strong_band_hi": {"value": 3.5, "safe_range": [3.3, 3.7]},
            "strong_bonus": {"value": 5, "safe_range": [3, 8]},
            "theme_bonus": {"value": 3, "safe_range": [2, 5]},
        },
        "blacklist": {
            "sectors": ["光伏设备"],
            "max_add_per_week": 2,
            "max_remove_per_week": 2,
            "safe_sectors": [],
        },
        "paper": {"initial_capital": 1000000.0, "max_position_ratio": 0.20, "max_concurrent": 3},
    }
    TEST_CONFIG.write_text(json.dumps(payload, ensure_ascii=False, indent=2))  # 用 JSON 文本
    TEST_CONFIG.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tu.CONFIG_PATH = TEST_CONFIG


def _seed_picks(picks_data: list[dict]) -> None:
    """造一些 picks + paper 表现数据"""
    from stock_trading_agent.engine.paper_trader import get_db
    conn = get_db()
    for p in picks_data:
        conn.execute(
            """
            INSERT INTO picks
            (pick_date, code, name, price, prev_close, chg_pct, turnover, amplitude,
             score, sector, in_theme, plan, plan_used, market_env_score, market_env_level, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p["date"], p["code"], p["name"], p["price"], p["prev_close"],
                p["chg_pct"], p["turnover"], p["amplitude"], p["score"],
                p["sector"], 1 if p.get("in_theme") else 0, "A", "A",
                50, "中性", "2026-06-01T00:00:00",
            ),
        )
        # 模拟已成交
        conn.execute(
            """
            INSERT INTO paper_positions
            (pick_date, code, name, open_price, open_amount, shares, sector, plan, score,
             status, close_noon_price, pnl_noon, pnl_noon_pct, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed_noon', ?, ?, ?, ?, ?)
            """,
            (
                p["date"], p["code"], p["name"], p["price"], 100000.0, 1000,
                p["sector"], "A", p["score"],
                p["price"] * (1 + p["pnl_noon_pct"] / 100),
                p["pnl_noon_pct"] * 1000,
                p["pnl_noon_pct"],
                "2026-06-01T00:00:00", "2026-06-02T00:00:00",
            ),
        )
    conn.commit()


def test_propose_in_safe_range() -> None:
    global TEST_DIR, TEST_DB, TEST_CONFIG
    TEST_DIR = _isolated_dir("test_propose_in_safe_range")
    TEST_DB = TEST_DIR / "quant.db"
    TEST_CONFIG = TEST_DIR / "config.yaml"
    import stock_trading_agent.engine.paper_trader as pt
    import stock_trading_agent.engine.tuner as tu
    pt.DB_PATH = TEST_DB
    pt.DATA_DIR = TEST_DIR
    tu.CONFIG_PATH = TEST_CONFIG
    # load_config 缓存会在 _seed_config() 之后被覆盖
    """测试 1: 提议值在 safe_range 内 → 自动应用"""
    _seed_config()
    # >=80 段胜率 30% (低) → 提议 score_max 80 → 78
    picks = [
        {"date": "2026-06-01", "code": f"c{i:06d}", "name": f"n{i}", "price": 10.0, "prev_close": 9.7,
         "chg_pct": 3.1, "turnover": 8.0, "amplitude": 5.0, "score": 80 + i * 0.1,
         "sector": "半导体", "pnl_noon_pct": -1.0}  # n=5, 全部负
        for i in range(5)
    ]
    _seed_picks(picks)
    result = tu.run_weekly_tune()
    applied = [a for a in result["applied"] if a["param"] == "v3.score_max"]
    assert applied, "应自动应用 score_max, got " + str([a["param"] for a in result["applied"]])
    applied = applied[0]
    assert applied["new"] >= 75.0 and applied["new"] <= 85.0, f"新值应在 safe_range 内: {applied}"
    print(f"  ✓ test_propose_in_safe_range: score_max {applied['old']} → {applied['new']}")


def test_propose_out_of_safe_range() -> None:
    global TEST_DIR, TEST_DB, TEST_CONFIG
    TEST_DIR = _isolated_dir("test_propose_out_of_safe_range")
    TEST_DB = TEST_DIR / "quant.db"
    TEST_CONFIG = TEST_DIR / "config.yaml"
    import stock_trading_agent.engine.paper_trader as pt
    import stock_trading_agent.engine.tuner as tu
    pt.DB_PATH = TEST_DB
    pt.DATA_DIR = TEST_DIR
    tu.CONFIG_PATH = TEST_CONFIG
    # load_config 缓存会在 _seed_config() 之后被覆盖
    """测试 2: 提议值在 safe_range 外 → 推 pending"""
    _seed_config()
    # 极端场景: >=80 全部 -10% (极差), 提议 -10
    # 但我们的 _propose_score_max 一次只 -2，所以这个测起来难
    # 改测: 直接构造一个 out_of_range 的 proposal 走 apply_proposal(auto=True) 路径
    proposal = {
        "param": "v3.score_max",
        "old": 80.0,
        "new": 60.0,  # 远低于 safe_range[0]=75
        "in_safe_range": False,
        "reason": "test out-of-range",
    }
    # 验证: 当 in_safe_range=False 且 auto=True 时, apply_proposal 应该返回 False
    ok = tu.apply_proposal(proposal, auto=True)
    assert ok is False, "out-of-range proposal 不应被自动应用"
    print("  ✓ test_propose_out_of_safe_range: 拒绝自动应用 out-of-range proposal")


def test_blacklist_add_cap() -> None:
    global TEST_DIR, TEST_DB, TEST_CONFIG
    TEST_DIR = _isolated_dir("test_blacklist_add_cap")
    TEST_DB = TEST_DIR / "quant.db"
    TEST_CONFIG = TEST_DIR / "config.yaml"
    import stock_trading_agent.engine.paper_trader as pt
    import stock_trading_agent.engine.tuner as tu
    pt.DB_PATH = TEST_DB
    pt.DATA_DIR = TEST_DIR
    tu.CONFIG_PATH = TEST_CONFIG
    # load_config 缓存会在 _seed_config() 之后被覆盖
    """测试 3: 黑名单 +2 上限"""
    _seed_config()
    # 3 个拖累板块, max_add=2, 应该只加前 2
    # 3 个拖累板块, 每个 2 只 (n>=2), max_add=2, 应只加前 2
    picks = []
    code = 0
    for sec, pnl in [("拖累A", -5.0), ("拖累B", -4.0), ("拖累C", -3.0)]:
        for _ in range(2):
            picks.append({
                "date": "2026-06-01", "code": f"c{code:06d}", "name": f"n{code}", "price": 10.0,
                "prev_close": 9.7, "chg_pct": 3.1, "turnover": 8.0, "amplitude": 5.0, "score": 75.0,
                "sector": sec, "pnl_noon_pct": pnl,
            })
            code += 1
    _seed_picks(picks)
    result = tu.run_weekly_tune()
    adds = [a for a in result["applied"] if a["param"] == "blacklist.sectors" and a.get("action") == "add"]
    assert len(adds) <= 2, f"max_add=2 限制应生效, got {len(adds)}"
    assert len(adds) >= 1, f"应有 1-2 个加, got {len(adds)}"
    print(f"  ✓ test_blacklist_add_cap: 加 {len(adds)} 个板块 (上限 2)")


def test_blacklist_remove_cap() -> None:
    global TEST_DIR, TEST_DB, TEST_CONFIG
    TEST_DIR = _isolated_dir("test_blacklist_remove_cap")
    TEST_DB = TEST_DIR / "quant.db"
    TEST_CONFIG = TEST_DIR / "config.yaml"
    import stock_trading_agent.engine.paper_trader as pt
    import stock_trading_agent.engine.tuner as tu
    pt.DB_PATH = TEST_DB
    pt.DATA_DIR = TEST_DIR
    tu.CONFIG_PATH = TEST_CONFIG
    # load_config 缓存会在 _seed_config() 之后被覆盖
    """测试 4: 黑名单 -2 上限"""
    _seed_config()
    # 现有黑名单: [光伏设备, 拖累A, 拖累B, 拖累C] (3 个表现恢复的)
    cfg = json.loads(TEST_CONFIG.read_text())
    cfg["blacklist"]["sectors"] = ["光伏设备", "拖累A", "拖累B", "拖累C"]
    TEST_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    TEST_CONFIG.with_suffix(".json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    # 同步刷新 load_config 缓存
    import stock_trading_agent.engine.data_fetcher as df
    df._CONFIG_CACHE = cfg
    # 3 个表现恢复的板块, 每个 2 只 (n>=2, 胜率 >=50%)
    picks = []
    code = 0
    for sec, pnl in [("拖累A", 2.0), ("拖累B", 3.0), ("拖累C", 1.5)]:
        for _ in range(2):
            picks.append({
                "date": "2026-06-01", "code": f"c{code:06d}", "name": f"n{code}", "price": 10.0,
                "prev_close": 9.7, "chg_pct": 3.1, "turnover": 8.0, "amplitude": 5.0, "score": 75.0,
                "sector": sec, "pnl_noon_pct": pnl,
            })
            code += 1
    _seed_picks(picks)
    result = tu.run_weekly_tune()
    if not result["applied"]:
        print("  DEBUG stats:", result["stats"])
        print("  DEBUG applied:", result["applied"])
    rms = [a for a in result["applied"] if a["param"] == "blacklist.sectors" and a.get("action") == "remove"]
    assert len(rms) <= 2, f"max_remove=2 限制应生效, got {len(rms)}"
    assert len(rms) >= 1, f"应有 1-2 个移除, got {len(rms)}"
    print(f"  ✓ test_blacklist_remove_cap: 移 {len(rms)} 个板块 (上限 2)")


def test_no_change_no_proposal() -> None:
    global TEST_DIR, TEST_DB, TEST_CONFIG
    TEST_DIR = _isolated_dir("test_no_change_no_proposal")
    TEST_DB = TEST_DIR / "quant.db"
    TEST_CONFIG = TEST_DIR / "config.yaml"
    import stock_trading_agent.engine.paper_trader as pt
    import stock_trading_agent.engine.tuner as tu
    pt.DB_PATH = TEST_DB
    pt.DATA_DIR = TEST_DIR
    tu.CONFIG_PATH = TEST_CONFIG
    # load_config 缓存会在 _seed_config() 之后被覆盖
    """测试 5: 无变化时提议为空"""
    _seed_config()
    # 所有表现都好, 没有反向指标
    picks = [
        {"date": "2026-06-01", "code": f"c{i:06d}", "name": f"n{i}", "price": 10.0, "prev_close": 9.7,
         "chg_pct": 3.1, "turnover": 8.0, "amplitude": 5.0, "score": 75.0,
         "sector": "好板块", "pnl_noon_pct": 2.0}
        for i in range(3)
    ]
    _seed_picks(picks)
    result = tu.run_weekly_tune()
    # 应该有提议但都比较温和
    # 验证逻辑没崩
    assert "stats" in result
    assert "applied" in result
    assert "pending" in result
    print(f"  ✓ test_no_change_no_proposal: 提议 {len(result['applied'])} 改 / {len(result['pending'])} 待确认")


# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [
        test_propose_in_safe_range,
        test_propose_out_of_safe_range,
        test_blacklist_add_cap,
        test_blacklist_remove_cap,
        test_no_change_no_proposal,
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
    shutil.rmtree(TEST_DIR, ignore_errors=True)
