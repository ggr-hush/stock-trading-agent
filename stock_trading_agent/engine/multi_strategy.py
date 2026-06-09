"""
multi_strategy.py — A/B/C 多策略并行 + 本周最优投票
- 同时跑 A 精准 / B 平衡 / C 空仓保护
- 计算每个方案的"本周基线胜率"（从 params_history 查最近一次手动跑的数据）
- 给出本周"该用哪个"的建议
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from .data_fetcher import load_config
from .paper_trader import get_db
from .picker import filter_stocks


def _plan_filter(stocks: list[dict], plan: str, config: dict[str, Any]) -> tuple[list[dict], dict]:
    """单方案过滤 (复用 picker 的内部函数)"""
    if plan == "C":
        return [], {"plan_used": "C", "n": 0}
    if plan == "A":
        from .picker import _hard_excluded, _plan_a
        hard_excl = _hard_excluded(stocks, config)
        df = _plan_a(stocks, hard_excl, config)
    elif plan == "B":
        from .picker import _hard_excluded, _plan_b
        hard_excl = _hard_excluded(stocks, config)
        df = _plan_b(stocks, hard_excl, config)
    else:
        raise ValueError(f"未知 plan: {plan}")
    # 应用 v3 过滤
    from .picker import _score_stock
    v3 = config["v3"]
    bl = set(config["blacklist"]["sectors"])
    final: list[dict] = []
    for s in df:
        s["score"] = _score_stock(s, config)
        if s["score"] > v3["score_max"]["value"]:
            continue
        if s.get("sector", "") in bl:
            continue
        final.append(s)
    final.sort(key=lambda x: x["score"], reverse=True)
    return final, {"plan_used": plan, "n": len(final)}


def _historical_win_rate(plan: str) -> dict[str, Any] | None:
    """查最近 30 天某方案的胜率（来自 paper_positions + picks）"""
    conn = get_db()
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT pos.pnl_noon_pct
        FROM paper_positions pos
        JOIN picks p ON p.code = pos.code AND p.pick_date = pos.pick_date
        WHERE pos.pick_date >= ? AND p.plan_used = ?
        """,
        (since, plan),
    ).fetchall()
    pnls = [r["pnl_noon_pct"] for r in rows if r["pnl_noon_pct"] is not None]
    if len(pnls) < 3:
        return None
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(pnls),
        "avg_pnl": round(sum(pnls) / len(pnls), 2),
        "win_rate": round(wins / len(pnls) * 100, 1),
    }


def run(stocks: list[dict] | None = None,
        market_env: dict | None = None,
        hot_sectors: list[dict] | None = None,
        config: dict[str, Any] | None = None) -> dict[str, Any]:
    """多策略并行 + 投票推荐

    Args:
        stocks: 全市场候选 (None 则空仓)
        market_env: 大盘环境
        hot_sectors: 热门板块
        config: config.yaml dict

    Returns:
        {
          plans: {
            A: {n, codes, stats},
            B: {n, codes, stats},
            C: {n=0, codes=[]},
          },
          recommendation: "A" | "B" | "C",
          reasoning: str,
          historical: {A: {n, avg, win_rate}, B: {...}, C: null}
        }
    """
    if config is None:
        config = load_config()
    if stocks is None:
        stocks = []
    if market_env is None:
        market_env = {}
    if hot_sectors is None:
        hot_sectors = []
    hot_sector_names = {s["name"] for s in hot_sectors[:8]}

    plans: dict[str, Any] = {}
    for plan in ("A", "B", "C"):
        if plan == "C":
            plans["C"] = {"n": 0, "codes": [], "stats": {"plan_used": "C"}}
        else:
            try:
                final, stats = _plan_filter(stocks, plan, config)
                codes = [s["code"] for s in final]
                plans[plan] = {"n": len(final), "codes": codes, "stats": stats}
            except Exception as e:
                plans[plan] = {"n": 0, "codes": [], "error": str(e)}

    # 投票逻辑：
    # 1) 如果 A 和 B 都有候选，取 A (A 优先)
    # 2) 如果只有 B 有，用 B
    # 3) 如果都没有，用 C
    # 4) 同时参考历史胜率：如果 A 历史胜率 < B 的一半，自动倾向 B
    hist = {
        "A": _historical_win_rate("A"),
        "B": _historical_win_rate("B"),
    }
    reasoning_parts: list[str] = []
    if plans["A"]["n"] > 0 and plans["B"]["n"] > 0:
        recommendation = "A"
        reasoning_parts.append(f"方案 A 有 {plans['A']['n']} 只, 方案 B 有 {plans['B']['n']} 只, 默认 A 优先")
    elif plans["B"]["n"] > 0:
        recommendation = "B"
        reasoning_parts.append(f"方案 A 无候选, 方案 B 有 {plans['B']['n']} 只, 用 B")
    else:
        recommendation = "C"
        reasoning_parts.append("A/B 均无候选, 空仓 C")

    # 历史胜率修正
    if recommendation in ("A", "B") and hist[recommendation]:
        h = hist[recommendation]
        if h["n"] >= 5 and h["win_rate"] < 30:
            # 胜率 < 30% 强制降级
            other = "B" if recommendation == "A" else "A"
            if plans[other]["n"] > 0:
                reasoning_parts.append(
                    f"⚠️ {recommendation} 历史 30 日胜率 {h['win_rate']}% (n={h['n']}) 低于 30%, "
                    f"自动改用 {other}"
                )
                recommendation = other
            else:
                reasoning_parts.append(
                    f"⚠️ {recommendation} 历史胜率低, 但 {other} 无候选, 维持 {recommendation}"
                )

    return {
        "plans": plans,
        "recommendation": recommendation,
        "reasoning": " | ".join(reasoning_parts),
        "historical": hist,
        "market_env": market_env,
        "hot_sectors_top3": [s["name"] for s in hot_sectors[:3]],
    }
