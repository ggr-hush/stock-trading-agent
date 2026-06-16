"""engine/decision_engine.py — v12.A.4 决策引擎

借鉴 felix-quant `backend/app/services/decision_engine.py:build_daily_decision` + `build_position_decision`

职责:
  - 接收 market_regime 分类 + 候选统计 → 算出当日决策
  - 4 维仓位上限 (base / market / strategy_quality / decision_mode) 取 min
  - 返 {decisionMode, positionMin/Max, allowedActions, forbiddenActions, keyReasons, switchConditions}

公共 API:
  - build_daily_decision(regime_info: dict, candidate_count: int = 0,
                          high_risk_ratio: float = 0.0, base_risk_limit: float = 0.65) -> dict
"""
from __future__ import annotations

import logging
from typing import Any

from .market_regime import (
    REGIME_TO_MODE,
    DECISION_MODE_DESC,
    describe_mode,
    regime_to_mode,
)

log = logging.getLogger("engine.decision_engine")

# 4 维仓位上限之 market 维度
MARKET_REGIME_LIMIT = {
    "RiskOn": 0.70,
    "Recovery": 0.50,
    "Choppy": 0.30,
    "RiskOff": 0.20,
    "Panic": 0.0,
}


def build_daily_decision(
    regime_info: dict[str, Any],
    candidate_count: int = 0,
    high_risk_ratio: float = 0.0,
    base_risk_limit: float = 0.65,
) -> dict[str, Any]:
    """从市场状态 + 候选统计算出当日决策

    Args:
        regime_info: classify_regime() 返的 dict
        candidate_count: 今日选股候选数 (供 PROBE 模式判断)
        high_risk_ratio: 高风险候选比例 (0-1)
        base_risk_limit: 基础风控上限 (默认 0.65)

    Returns:
        {
          regime, regime_zh, decisionMode, mode_zh,
          positionMin, positionMax,
          allowedActions, forbiddenActions,
          keyReasons, switchConditions,
          marketLimit, decisionModeLimit, finalLimit,
          explanation
        }
    """
    regime = regime_info.get("regime", "Choppy")
    regime_zh = regime_info.get("label_zh", "震荡")
    mode = regime_to_mode(regime, candidate_count=candidate_count,
                          high_risk_ratio=high_risk_ratio)
    mode_desc = describe_mode(mode)
    mode_zh = mode_desc["label_zh"]

    # 4 维仓位上限
    market_limit = MARKET_REGIME_LIMIT.get(regime, 0.30)
    decision_min = mode_desc["pos_min"]
    decision_max = mode_desc["pos_max"]

    # strategy_quality_limit (简化版: 高风险 > 0.5 降到 0.20)
    if high_risk_ratio > 0.5:
        strategy_quality_limit = 0.20
    else:
        strategy_quality_limit = base_risk_limit

    # final = min(base, market, strategy_quality, decision_mode)
    final_max = min(base_risk_limit, market_limit, strategy_quality_limit, decision_max)
    final_min = min(decision_min, final_max)

    # key reasons
    key_reasons: list[str] = []
    if regime in ("Panic", "RiskOff"):
        key_reasons.append(f"市场状态 {regime_zh}, 风险偏好收缩")
    elif regime == "RiskOn":
        key_reasons.append(f"市场状态 {regime_zh}, 风险偏好恢复")
    elif regime == "Recovery":
        key_reasons.append(f"市场状态 {regime_zh}, 仍需确认持续性")
    else:
        key_reasons.append(f"市场状态 {regime_zh}, 优先质量动量 + 低回撤趋势")
    if candidate_count == 0 and mode == "PROBE":
        key_reasons.append("试探条件不足 (候选 < 2), 降档为 WATCH")
    if high_risk_ratio > 0.5:
        key_reasons.append(f"高风险候选比例 {high_risk_ratio:.0%} > 50%, 策略质量上限降至 20%")

    # switch conditions (regime 切换提示)
    switch_conditions = _switch_conditions(regime, mode)

    return {
        "regime": regime,
        "regime_zh": regime_zh,
        "decisionMode": mode,
        "mode_zh": mode_zh,
        "positionMin": round(final_min, 4),
        "positionMax": round(final_max, 4),
        "allowedActions": list(mode_desc["allowed"]),
        "forbiddenActions": list(mode_desc["forbidden"]),
        "keyReasons": key_reasons[:5],
        "switchConditions": switch_conditions,
        "marketLimit": round(market_limit, 4),
        "decisionModeLimit": (round(decision_min, 4), round(decision_max, 4)),
        "finalLimit": round(final_max, 4),
        "explanation": (
            f"市场 {regime_zh} → 决策 {mode_zh} → 仓位 "
            f"{final_min:.0%}-{final_max:.0%} (base={base_risk_limit:.0%}, "
            f"market={market_limit:.0%}, mode={decision_max:.0%})"
        ),
    }


def _switch_conditions(regime: str, current_mode: str) -> list[str]:
    """返 regime 切换提示"""
    if regime == "Panic":
        return ["20 日涨跌幅 > -3% 且跌停 < 20 → RiskOff",
                "上涨家数 > 45% → RiskOff"]
    if regime == "RiskOff":
        return ["20 日涨跌幅 > 3% 且上涨家数 > 55% → RiskOn",
                "市场进入修复期 → Recovery"]
    if regime == "Choppy":
        return ["放量上涨 + 涨停 > 50 → RiskOn",
                "持续下跌 + 跌停 > 20 → RiskOff"]
    if regime == "Recovery":
        return ["放量确认 + 涨停 > 50 → RiskOn",
                "回踩失败 + 跌停 > 20 → RiskOff"]
    if regime == "RiskOn":
        return ["缩量 + 涨跌幅 < 0.5% → Choppy",
                "大跌 + 跌停 > 20 → RiskOff"]
    return []
