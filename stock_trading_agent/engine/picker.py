"""
picker.py — 选股引擎（v3 调参后）
- 方案 A 精准 / 方案 B 平衡 / 方案 C 空仓
- 评分（强信号带 + 主线 + v3 上限）
- 板块黑名单
"""
from __future__ import annotations

from typing import Any

from .data_fetcher import get_hot_sectors, get_market_env, get_market_stocks, get_stock_sectors


# ─────────── 评分 ───────────

def _score_stock(s: dict[str, Any], config: dict[str, Any]) -> float:
    """单只票综合评分（v3）"""
    v3 = config["v3"]
    amount_score = min(float(s.get("amount_yi", 0)) / 30, 1) * 30
    turn_score = min(float(s.get("turnover", 0)) / 10, 1) * 30
    chg = float(s.get("chg_pct", 0))
    chg_score = min(abs(chg - 3.0) / 1.0, 1) * 20  # 越接近 3% 越高
    size_score = min(float(s.get("total_mv_yi", 0)) / 300, 1) * 20
    base = amount_score + turn_score + chg_score + size_score
    # 强信号带加分
    if v3["strong_band_lo"]["value"] <= chg <= v3["strong_band_hi"]["value"]:
        base += v3["strong_bonus"]["value"]
    return round(base, 2)


# ─────────── 三档方案 ───────────

def _plan_a(stocks: list[dict[str, Any]], hard_excluded: set[int], config: dict[str, Any]) -> list[dict[str, Any]]:
    pa = config["plan_a"]
    return [
        s for i, s in enumerate(stocks)
        if i not in hard_excluded
        and pa["chg_lo"] <= float(s.get("chg_pct", 0)) < pa["chg_hi"]
        and pa["turnover_lo"] <= float(s.get("turnover", 0)) <= pa["turnover_hi"]
        and float(s.get("amplitude", 0)) < pa["amplitude_hi"]
        and config["hard"]["mv_lo_yi"] <= float(s.get("total_mv_yi", 0)) <= config["hard"]["mv_hi_yi"]
        and float(s.get("amount_yi", 0)) >= config["hard"]["amt_lo_yi"]
    ]


def _plan_b(stocks: list[dict[str, Any]], hard_excluded: set[int], config: dict[str, Any]) -> list[dict[str, Any]]:
    pb = config["plan_b"]
    return [
        s for i, s in enumerate(stocks)
        if i not in hard_excluded
        and pb["chg_lo"] <= float(s.get("chg_pct", 0)) < pb["chg_hi"]
        and pb["turnover_lo"] <= float(s.get("turnover", 0)) <= pb["turnover_hi"]
        and float(s.get("amplitude", 0)) < pb["amplitude_hi"]
        and config["hard"]["mv_lo_yi"] <= float(s.get("total_mv_yi", 0)) <= config["hard"]["mv_hi_yi"]
        and float(s.get("amount_yi", 0)) >= config["hard"]["amt_lo_yi"]
    ]


def _hard_excluded(stocks: list[dict[str, Any]], config: dict[str, Any]) -> set[int]:
    """硬过滤：涨幅 ≥ 危险线 或 振幅 ≥ 危险线"""
    hard = config["hard"]
    return {
        i for i, s in enumerate(stocks)
        if float(s.get("chg_pct", 0)) >= hard["chg_danger"]
        or float(s.get("amplitude", 0)) >= hard["amp_danger"]
    }


# ─────────── 过滤主函数 ───────────

def filter_stocks(
    stocks: list[dict[str, Any]],
    stock_sector_map: dict[str, str] | None = None,
    hot_sector_names: set[str] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """v3 选股主函数

    Args:
        stocks: get_market_stocks() 的输出
        stock_sector_map: {code: sector_name}，可选
        hot_sector_names: set of str，主线板块名集合，可选
        config: config.yaml dict

    Returns:
        (filtered_stocks, stats)
    """
    if config is None:
        from .data_fetcher import load_config
        config = load_config()
    v3 = config["v3"]
    bl = config["blacklist"]
    hard = config["hard"]

    # 计算振幅和日内位置
    for s in stocks:
        try:
            high = float(s.get("high") or 0)
            low = float(s.get("low") or 0)
            price = float(s.get("price") or 0)
            if high > low and high > 0:
                s["amplitude"] = round((high - low) / low * 100, 1)
                s["position"] = round((price - low) / (high - low) * 100, 0)
            else:
                s["amplitude"] = 0.0
                s["position"] = 50.0
        except Exception:
            s["amplitude"] = 0.0
            s["position"] = 50.0

    hard_excl = _hard_excluded(stocks, config)
    df_a = _plan_a(stocks, hard_excl, config)
    df_b = _plan_b(stocks, hard_excl, config) if not df_a else []

    if df_a:
        final, plan_used = df_a, "A"
    elif df_b:
        final, plan_used = df_b, "B"
    else:
        final, plan_used = [], "C"

    # 注入板块
    if stock_sector_map:
        for s in stocks:
            if not s.get("sector"):
                s["sector"] = stock_sector_map.get(s.get("code", ""), "")

    # 评分
    for s in final:
        s["score"] = _score_stock(s, config)

    # 主线加分
    if stock_sector_map and hot_sector_names:
        for s in final:
            in_theme = s.get("sector", "") in hot_sector_names
            s["in_theme"] = in_theme
            if in_theme:
                s["score"] = round(s["score"] + v3["theme_bonus"]["value"], 2)

    final.sort(key=lambda x: x["score"], reverse=True)

    # v3 过滤：评分上限
    pre = len(final)
    score_max = v3["score_max"]["value"]
    final = [s for s in final if s["score"] <= score_max]

    # v3 过滤：板块黑名单
    if final and any(s.get("sector") for s in final):
        before = len(final)
        final = [s for s in final if s.get("sector", "") not in set(bl["sectors"])]
        filtered_out = before - len(final)
        if filtered_out:
            print(f"  [v3过滤] 板块黑名单剔除 {filtered_out} 只")

    stats = {
        "hard_excluded": len(hard_excl),
        "plan_a_count": len(df_a),
        "plan_b_count": len(df_b),
        "plan_used": plan_used,
        "v3_filtered": pre - len(final),
        "final_count": len(final),
    }
    return final, stats


# ─────────── 顶层入口 ───────────

def pick(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """完整选股流程：拉数据 → 拿板块 → 过滤 → 评分

    Returns:
        {
          date, plan_used, market_env, sectors, hot_sector_names,
          stock_sector_map, filtered_stocks, stats
        }
    """
    from datetime import date as _date
    from .data_fetcher import load_config

    if config is None:
        config = load_config()

    env = get_market_env(config)
    sectors = get_hot_sectors(config)
    hot_sector_names = {s["name"] for s in sectors[:8]}  # TOP8 作为主线

    stocks = get_market_stocks(config)
    codes = [s["code"] for s in stocks if s.get("code")]
    stock_sector_map = get_stock_sectors(codes, config)

    filtered, stats = filter_stocks(
        stocks,
        stock_sector_map=stock_sector_map,
        hot_sector_names=hot_sector_names,
        config=config,
    )

    # 截断到 max_picks
    max_picks = config["hard"]["max_picks"]
    filtered = filtered[:max_picks]

    # 写入 paper-trade 仓位建议
    pos_ratio = env["position_ratio"]
    cap = config["paper"]["initial_capital"]
    per_position = cap * config["paper"]["max_position_ratio"]
    n_open = min(len(filtered), config["paper"]["max_concurrent"])
    actual_per = (cap * pos_ratio / n_open) if n_open > 0 else 0

    for i, s in enumerate(filtered):
        s["position_advice_pct"] = round(pos_ratio * 100, 0) if i < n_open else 0
        s["position_advice_amount"] = round(actual_per, 0) if i < n_open else 0

    return {
        "date": _date.today().strftime("%Y-%m-%d"),
        "plan_used": stats["plan_used"],
        "market_env": env,
        "sectors": sectors,
        "hot_sector_names": list(hot_sector_names),
        "stock_sector_map": stock_sector_map,
        "filtered_stocks": filtered,
        "stats": stats,
    }
