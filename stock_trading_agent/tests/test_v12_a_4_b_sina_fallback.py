"""test_v12_a_4_b_sina_fallback.py — v12.A.4.b 指数数据源 fallback 测试

Covers (B 借鉴):
  - _fetch_indices_sina 解析新浪响应
  - get_market_env: qt.gtimg.cn 失败时自动 fallback 新浪
  - 决策卡片数据缺失时加 sentinel 提示 (D 借鉴)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── _fetch_indices_sina 解析 ───────────

SINA_MOCK_RESPONSE = (
    'var hq_str_sh000001="??ָ֤??,4094.2124,4096.4717,4091.8917,4103.9259,4077.8702,0,0,615668296,1369612989674,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-16,15:30:39,00";\n'
    'var hq_str_sz399006="??ҵ??ָ,4065.271,4033.535,4102.942,4142.433,4053.878,0.000,0.000,22590511467,829816569631.400,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,0,0.000,2026-06-16,15:00:03,00";\n'
    'var hq_str_sh000688="?ƴ?50,1751.7052,1748.3264,1758.4244,1766.1375,1729.0368,0,0,15018218,142643106071,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-16,15:00:03,00";\n'
)


def test_get_market_env_uses_tushare_index_daily() -> None:
    """v12.A.4.c: get_market_env 改走 Tushare index_daily (替代 qt + sina)"""
    import pandas as pd
    from stock_trading_agent.engine.data_fetcher import get_market_env
    cfg = {
        "env": {
            "indices": [
                {"code": "sh000001", "name": "上证指数", "weight": 0.4},
                {"code": "sz399006", "name": "创业板指", "weight": 0.35},
                {"code": "sh000688", "name": "科创50", "weight": 0.25},
            ],
            "vol_thresh_hi_yi": 12000,
            "vol_thresh_lo_yi": 9000,
        },
        "position": {
            "full": {"score_min": 75, "ratio": 1.0, "advice": "满仓", "market": "牛市"},
            "heavy": {"score_min": 60, "ratio": 0.8, "advice": "重仓", "market": "牛市"},
            "half": {"score_min": 45, "ratio": 0.5, "advice": "半仓", "market": "震荡"},
            "light": {"score_min": 30, "ratio": 0.2, "advice": "轻仓", "market": "偏弱"},
            "empty": {"score_min": 0, "ratio": 0.0, "advice": "空仓", "market": "熊市"},
        },
    }
    # mock Tushare index_daily 返 3 个指数各 5 天
    def fake_index_daily(ts_code, **kwargs):
        return pd.DataFrame({
            "ts_code": [ts_code]*5,
            "trade_date": ["20260616", "20260615", "20260612", "20260611", "20260610"],
            "close": [4091.89, 4096.47, 4031.5, 4050.0, 4070.0],
            "pre_close": [4096.47, 4031.5, 4050.0, 4070.0, 4060.0],
            "amount": [4.5e8, 4.3e8, 3.8e8, 4.0e8, 4.1e8],
        })
    with patch("stock_trading_agent.engine.tushare_client.get_pro") as mock_get_pro:
        mock_get_pro.return_value.index_daily.side_effect = fake_index_daily
        r = get_market_env(cfg)
    # v12.A.4.c: data_source 应该是 tushare (不是 sina)
    assert r["details"]["data_source"] == "tushare"
    # weighted_chg 应该有值
    assert r["details"]["weighted_chg"] is not None


def test_get_market_env_returns_data_missing_when_tushare_fails() -> None:
    """v12.A.4.c: Tushare 拉不到 → 返 data_missing"""
    import pandas as pd
    from stock_trading_agent.engine.data_fetcher import get_market_env
    cfg = {
        "env": {
            "indices": [{"code": "sh000001", "name": "上证指数", "weight": 1.0}],
            "vol_thresh_hi_yi": 12000,
            "vol_thresh_lo_yi": 9000,
        },
        "position": {
            "full": {"score_min": 75, "ratio": 1.0, "advice": "满仓", "market": "牛市"},
            "heavy": {"score_min": 60, "ratio": 0.8, "advice": "重仓", "market": "牛市"},
            "half": {"score_min": 45, "ratio": 0.5, "advice": "半仓", "market": "震荡"},
            "light": {"score_min": 30, "ratio": 0.2, "advice": "轻仓", "market": "偏弱"},
            "empty": {"score_min": 0, "ratio": 0.0, "advice": "空仓", "market": "熊市"},
        },
    }
    # mock Tushare index_daily 返空 DataFrame
    with patch("stock_trading_agent.engine.tushare_client.get_pro") as mock_get_pro:
        mock_get_pro.return_value.index_daily.return_value = pd.DataFrame()
        r = get_market_env(cfg)
    assert r["flags"] == ["data_missing"]
    assert r["details"]["data_source"] == "none"
    assert "Tushare" in r["details"]["reason"]


# ─────────── 决策卡片 sentinel (D 借鉴) ───────────

def test_decision_card_shows_sentinel_on_data_missing() -> None:
    """v12.A.4.b: env_score=50 + 数据缺失 regime → 卡片顶部加 ⚠️ 提示"""
    from stock_trading_agent.engine.skills import _render_decision_card
    card = _render_decision_card({
        "regime": "Choppy",
        "regime_zh": "震荡 (数据缺失)",
        "env_score": 50,
        "candidate_count": 0,
        "decision": {
            "decisionMode": "WATCH",
            "mode_zh": "谨慎观察",
            "positionMin": 0.0,
            "positionMax": 0.3,
            "allowedActions": ["谨慎观察"],
            "forbiddenActions": ["扩大仓位"],
            "keyReasons": ["市场状态 震荡 (数据缺失)"],
            "switchConditions": [],
        },
    })
    text = card["content"]["text"]["content"]
    assert "⚠️" in text
    assert "指数接口暂时不可用" in text
    assert "qt.gtimg.cn" in text or "hq.sinajs.cn" in text


def test_decision_card_no_sentinel_when_data_ok() -> None:
    """正常数据时不显示 sentinel"""
    from stock_trading_agent.engine.skills import _render_decision_card
    card = _render_decision_card({
        "regime": "RiskOn",
        "regime_zh": "进攻",
        "env_score": 75,
        "candidate_count": 5,
        "decision": {
            "decisionMode": "PROBE",
            "mode_zh": "小仓试探",
            "positionMin": 0.2,
            "positionMax": 0.5,
            "allowedActions": ["观察主线龙头"],
            "forbiddenActions": ["追高"],
            "keyReasons": ["市场状态 进攻"],
            "switchConditions": [],
        },
    })
    text = card["content"]["text"]["content"]
    assert "⚠️" not in text
    assert "指数接口暂时不可用" not in text


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
