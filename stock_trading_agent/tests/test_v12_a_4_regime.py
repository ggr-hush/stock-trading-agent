"""test_v12_a_4_regime.py — v12.A.4 market regime + decision engine 单元测试

Covers (借鉴 #1):
  - market_regime.classify_regime 5 档分类
  - market_regime.regime_to_mode 决策模式降档 (候选不足/高风险)
  - decision_engine.build_daily_decision 4 维仓位上限
  - skills.get_daily_decision skill (smoke)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── classify_regime 5 档分类 ───────────

def test_classify_panic_when_env_lt_25_chg_lt_neg_1_5() -> None:
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 20, "details": {"weighted_chg": -2.0, "sh_amt_yi": 3000}}
    r = classify_regime(env)
    assert r["regime"] == "Panic"
    assert r["label_zh"] == "恐慌"


def test_classify_riskoff_when_env_lt_45_chg_lt_neg_0_5() -> None:
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 40, "details": {"weighted_chg": -0.8, "sh_amt_yi": 4000}}
    r = classify_regime(env)
    assert r["regime"] == "RiskOff"
    assert r["label_zh"] == "避险"


def test_classify_choppy_when_env_45_to_65_no_clear_direction() -> None:
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 55, "details": {"weighted_chg": 0.0, "sh_amt_yi": 5000}}
    r = classify_regime(env)
    assert r["regime"] in ("Choppy", "Recovery")


def test_classify_recovery_when_env_45_to_70_positive_chg() -> None:
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 60, "details": {"weighted_chg": 0.3, "sh_amt_yi": 5000}}
    r = classify_regime(env)
    assert r["regime"] == "Recovery"


def test_classify_riskon_when_env_ge_65_chg_ge_0_5() -> None:
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 75, "details": {"weighted_chg": 1.2, "sh_amt_yi": 8000}}
    r = classify_regime(env)
    assert r["regime"] == "RiskOn"
    assert r["label_zh"] == "进攻"


def test_classify_data_missing_returns_choppy() -> None:
    from stock_trading_agent.engine.market_regime import classify_regime
    r = classify_regime({"env_score": None, "details": {}})
    assert r["regime"] == "Choppy"
    assert "数据缺失" in r["label_zh"]


# ─────────── regime_to_mode 降档逻辑 ───────────

def test_riskon_with_enough_candidates_returns_probe() -> None:
    from stock_trading_agent.engine.market_regime import regime_to_mode
    assert regime_to_mode("RiskOn", candidate_count=5, high_risk_ratio=0.1) == "PROBE"


def test_riskon_with_too_few_candidates_downgrades_to_watch() -> None:
    from stock_trading_agent.engine.market_regime import regime_to_mode
    assert regime_to_mode("RiskOn", candidate_count=1, high_risk_ratio=0.1) == "WATCH"


def test_riskon_with_high_risk_downgrades_to_watch() -> None:
    from stock_trading_agent.engine.market_regime import regime_to_mode
    assert regime_to_mode("RiskOn", candidate_count=5, high_risk_ratio=0.7) == "WATCH"


# ─────────── build_daily_decision 4 维仓位上限 ───────────

def test_decision_panic_yields_wait_zero_position() -> None:
    from stock_trading_agent.engine.decision_engine import build_daily_decision
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 18, "details": {"weighted_chg": -2.5, "sh_amt_yi": 3000}}
    regime_info = classify_regime(env)
    d = build_daily_decision(regime_info, candidate_count=0)
    assert d["decisionMode"] == "WAIT"
    assert d["positionMax"] == 0.0
    assert "短线追涨" in d["forbiddenActions"]


def test_decision_riskon_probe_yields_20_to_50() -> None:
    from stock_trading_agent.engine.decision_engine import build_daily_decision
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 75, "details": {"weighted_chg": 1.2, "sh_amt_yi": 8000}}
    regime_info = classify_regime(env)
    d = build_daily_decision(regime_info, candidate_count=5, high_risk_ratio=0.1)
    assert d["decisionMode"] == "PROBE"
    assert d["positionMin"] == 0.20
    assert d["positionMax"] == 0.50


def test_decision_riskon_with_high_risk_caps_at_0_20() -> None:
    from stock_trading_agent.engine.decision_engine import build_daily_decision
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 75, "details": {"weighted_chg": 1.2, "sh_amt_yi": 8000}}
    regime_info = classify_regime(env)
    d = build_daily_decision(regime_info, candidate_count=5, high_risk_ratio=0.7)
    # strategy_quality_limit = 0.20, final_max = min(0.65, 0.70, 0.20, 0.50) = 0.20
    assert d["positionMax"] == 0.20
    assert "高风险候选比例" in str(d["keyReasons"])


def test_decision_choppy_yields_watch_0_to_30() -> None:
    from stock_trading_agent.engine.decision_engine import build_daily_decision
    from stock_trading_agent.engine.market_regime import classify_regime
    env = {"env_score": 50, "details": {"weighted_chg": 0.1, "sh_amt_yi": 5000}}
    regime_info = classify_regime(env)
    d = build_daily_decision(regime_info, candidate_count=3)
    assert d["decisionMode"] == "WATCH"
    assert d["positionMax"] == 0.30


# ─────────── get_daily_decision skill smoke ───────────

def test_get_daily_decision_skill_runs() -> None:
    """v12.A.4: 飞书问'今天能买吗' 触发的 skill 真能跑通"""
    from stock_trading_agent.engine.skills import _run_get_daily_decision, _render_decision_card
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = {"c": 3}
    with patch("stock_trading_agent.engine.data_fetcher.load_config", return_value={}), \
         patch("stock_trading_agent.engine.data_fetcher.get_market_env", return_value={
             "env_score": 60, "env_level": "偏弱", "details": {"weighted_chg": 0.3, "sh_amt_yi": 5000}
         }), \
         patch("stock_trading_agent.engine.paper_trader.get_db", return_value=fake_db):
        result = _run_get_daily_decision({})
    assert "regime" in result
    assert "decision" in result
    assert result["candidate_count"] == 3
    # 渲染卡片
    card = _render_decision_card(result)
    assert card["msg_type"] == "interactive"
    # lark_md card 结构: {"content": {"tag": "div", "text": {"tag": "lark_md", "content": "..."}}}
    text = card["content"]["text"]["content"]
    assert "市场状态" in text, f"missing 市场状态: {text[:200]}"
    assert "决策模式" in text
    assert "建议仓位" in text


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    pass_n = 0
    fail_n = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            pass_n += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
            fail_n += 1
    print(f"\n{'OK' if fail_n == 0 else 'FAIL'} {pass_n}/{pass_n+fail_n} tests passed")
    sys.exit(0 if fail_n == 0 else 1)
