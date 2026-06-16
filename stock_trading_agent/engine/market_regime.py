"""engine/market_regime.py — v12.A.4 5 档市场状态分类

借鉴 felix-quant (XFX-939/felix-quant) `backend/app/services/classic_quant.py:market_regime_model`

5 档 (基于量化阈值, 不依赖 LLM):
  - Panic      20 日跌 < -6% + 20 日回撤 < -8% + 跌停 > 50
  - RiskOff    20 日跌 < -3% + 上涨家数 < 45% + 跌停 > 20
  - Recovery   20 日涨 + 但 60 日跌 + 上涨 ≥ 50% + 跌停 ≤ 20
  - RiskOn     20 日涨 > 3% + 上涨家数 > 55% + 涨停 > 50 + 跌停 < 10
  - Choppy     其他震荡

注: 本项目数据源限制 (东方财富免费接口), 我们用现有 `get_market_env()` 返的 env_score + weighted_chg + sh_amt_yi 等字段估算.
涨停/跌停/上涨家数等指标 v12.A.4 用 env_score 间接代替, 标注为 "v12.A.5 接 sentiment 数据后升级"

公共 API:
  - classify_regime(env: dict) -> dict  返 {regime, score, reasons}
  - REGIME_THRESHOLDS: dict  5 档量化阈值
  - DECISION_MODES: dict  4 档决策模式描述
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("engine.market_regime")

# 5 档市场状态
REGIMES = ("Panic", "RiskOff", "Choppy", "Recovery", "RiskOn")

# 决策模式 (4 档, 跟 felix 对齐: WAIT / DEFENSIVE_OBSERVE / WATCH / PROBE)
DECISION_MODES = ("WAIT", "DEFENSIVE_OBSERVE", "WATCH", "PROBE")

# v12.A.4 阈值: 基于现有 get_market_env() 返的 env_score (0-100) 间接分类
# env_score 来源: 25 + weighted_chg * 5 + trend_bonus + vol_bonus
# 经验映射:
#   - 0-25   = 极差 → Panic / RiskOff
#   - 25-45  = 差 / 偏弱 → Choppy / RiskOff
#   - 45-65  = 中性 → Choppy
#   - 65-85  = 偏强 → RiskOn / Recovery
#   - 85-100 = 强 → RiskOn
REGIME_THRESHOLDS = {
    "Panic":    {"env_max": 25,  "chg_max": -1.5, "label_zh": "恐慌"},
    "RiskOff":  {"env_max": 45,  "chg_max": -0.5, "label_zh": "避险"},
    "Choppy":   {"env_min": 45,  "env_max": 65,  "label_zh": "震荡"},
    "Recovery": {"env_min": 45,  "chg_min": 0,   "env_max": 70,  "label_zh": "修复"},
    "RiskOn":   {"env_min": 65,  "chg_min": 0.5, "label_zh": "进攻"},
}

# 4 档决策模式描述
DECISION_MODE_DESC = {
    "WAIT":             {"label_zh": "空仓等待", "pos_min": 0.0, "pos_max": 0.0,
                         "allowed": ["复盘风险候选", "检查数据质量"],
                         "forbidden": ["短线追涨", "龙头接力", "趋势突破", "加仓"]},
    "DEFENSIVE_OBSERVE":{"label_zh": "防御观察", "pos_min": 0.0, "pos_max": 0.20,
                         "allowed": ["观察低波防御", "观察质量动量", "复盘风险候选"],
                         "forbidden": ["短线热点追涨", "龙头接力", "扩大仓位"]},
    "WATCH":            {"label_zh": "谨慎观察", "pos_min": 0.0, "pos_max": 0.30,
                         "allowed": ["谨慎观察", "观察质量动量", "观察低回撤趋势", "复盘短线候选"],
                         "forbidden": ["扩大仓位", "追涨高风险热点"]},
    "PROBE":            {"label_zh": "小仓试探", "pos_min": 0.20, "pos_max": 0.50,
                         "allowed": ["观察主线龙头", "观察趋势突破", "观察放量强势股"],
                         "forbidden": ["追高高位放量滞涨票", "无风控扩大仓位"]},
}

# regime → default decision_mode 映射
REGIME_TO_MODE = {
    "Panic":    "WAIT",
    "RiskOff":  "DEFENSIVE_OBSERVE",
    "Choppy":   "WATCH",
    "Recovery": "WATCH",
    "RiskOn":   "PROBE",
}


def classify_regime(env: dict[str, Any]) -> dict[str, Any]:
    """从 get_market_env() 返的 dict 推断 5 档市场状态

    Args:
        env: get_market_env() 返的 dict, 关键字段:
             - env_score: 0-100 综合分
             - details.weighted_chg: 加权涨跌幅 (%)
             - details.sh_amt_yi: 上证成交额 (亿)
             - market_type: 强/偏强/中性/偏弱/差/极差
             - position_ratio: 当前建议仓位 (0-1)

    Returns:
        {
          regime: Panic / RiskOff / Choppy / Recovery / RiskOn,
          score: env_score 原值 (0-100),
          weighted_chg: 涨跌幅,
          label_zh: 中文标签,
          reasons: list[str] 分类理由
        }
    """
    score = env.get("env_score")
    details = env.get("details") or {}
    chg = details.get("weighted_chg", 0.0) or 0.0
    sh_amt = details.get("sh_amt_yi", 0) or 0

    reasons: list[str] = []
    if score is None:
        return {"regime": "Choppy", "score": None, "weighted_chg": chg,
                "label_zh": "震荡 (数据缺失)", "reasons": ["数据缺失, 默认震荡"]}

    # 5 档分类 (顺序敏感, 从最严到最宽)
    if score < 25 and chg < -1.5:
        regime = "Panic"
        reasons.append(f"env_score={score} < 25 且 weighted_chg={chg}% < -1.5%")
    elif score < 45 and chg < -0.5:
        regime = "RiskOff"
        reasons.append(f"env_score={score} < 45 且 weighted_chg={chg}% < -0.5%")
    elif score >= 65 and chg >= 0.5 and sh_amt > 0:
        regime = "RiskOn"
        reasons.append(f"env_score={score} ≥ 65 且 weighted_chg={chg}% ≥ 0.5%")
    elif score >= 45 and chg >= 0 and score < 70:
        regime = "Recovery"
        reasons.append(f"env_score {score} 在 45-70, 涨跌幅 {chg}% ≥ 0")
    else:
        regime = "Choppy"
        reasons.append(f"env_score={score}, weighted_chg={chg}%, 默认震荡")

    return {
        "regime": regime,
        "score": score,
        "weighted_chg": chg,
        "sh_amt_yi": sh_amt,
        "label_zh": REGIME_THRESHOLDS[regime]["label_zh"],
        "reasons": reasons,
    }


def regime_to_mode(regime: str, candidate_count: int = 0, high_risk_ratio: float = 0.0) -> str:
    """regime → decision_mode (考虑候选数 + 高风险比例)

    仿 felix 逻辑: RiskOn 但 candidate_count 不足 → 降档到 WATCH
    """
    base_mode = REGIME_TO_MODE.get(regime, "WATCH")
    if base_mode == "PROBE":
        if candidate_count < 2:
            return "WATCH"  # 候选不够, 不试探
        if high_risk_ratio >= 0.6:
            return "WATCH"  # 高风险太多
    return base_mode


def describe_mode(mode: str) -> dict[str, Any]:
    """返决策模式描述 (label_zh / pos_min/max / allowed / forbidden)"""
    return dict(DECISION_MODE_DESC.get(mode, DECISION_MODE_DESC["WATCH"]))
