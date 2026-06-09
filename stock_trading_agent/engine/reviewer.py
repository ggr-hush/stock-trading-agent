"""
reviewer.py — 日/周复盘 + 历史回测
- run_daily_review(): 当日 picks + paper 表现摘要
- run_weekly_review(): 周胜率 + 调参提议
- backtest(): 用历史 JSON fixtures 重放 picker，输出 PnL 曲线
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .data_fetcher import load_config
from .multi_strategy import _historical_win_rate, _plan_filter, run as multi_strategy_run
from .paper_trader import get_db, get_paper_pnl
from ..llm.reasoner import auto_period_explain
from .tuner import run_weekly_tune, weekly_stats

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures"


# ─────────── 日报 ───────────

def run_daily_review(target_date: str | None = None) -> dict[str, Any]:
    """单日复盘：当日 picks 的 paper 假设表现"""
    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    rows = conn.execute(
        """
        SELECT p.*, pos.pnl_open_pct, pos.pnl_noon_pct
        FROM picks p
        LEFT JOIN paper_positions pos ON pos.code = p.code AND pos.pick_date = p.pick_date
        WHERE p.pick_date = ?
        ORDER BY p.score DESC
        """,
        (target_date,),
    ).fetchall()
    picks = [dict(r) for r in rows]
    paper = get_paper_pnl()
    return {
        "date": target_date,
        "picks": picks,
        "pick_count": len(picks),
        "paper_total": paper,
    }


# ─────────── 周报 + 调参 ───────────

def detect_auto_regression(
    stats: dict[str, Any],
    win_rate_threshold: float = 40.0,
    avg_threshold: float = -0.5,
) -> bool:
    """v7.1: 检测 auto 策略是否表现差到需要诊断

    触发条件 (任一):
      - 胜率 < win_rate_threshold (默认 40%)
      - 平均 PnL < avg_threshold (默认 -0.5%)
    样本数 < 5 时不触发 (避免噪声)。
    """
    overall = (stats or {}).get("overall", {}) or {}
    n = overall.get("n", 0)
    if n < 5:
        return False
    if overall.get("win_rate", 100) < win_rate_threshold:
        return True
    if overall.get("avg", 0) < avg_threshold:
        return True
    return False


def run_weekly_review() -> dict[str, Any]:
    """周日复盘：stats + 自动调参 + 推飞书内容

    v7.1: auto 表现差时, 调 BM25 + LLM 解释根因, 塞到 auto_regression 字段。
    """
    tune_result = run_weekly_tune()
    stats = tune_result.get("stats", {})
    regression = detect_auto_regression(stats)
    explanation = auto_period_explain(stats) if regression else ""
    return {
        "stats": stats,
        "applied": tune_result.get("applied", []),
        "pending": tune_result.get("pending", []),
        "auto_regression": {
            "triggered": regression,
            "explanation": explanation,
        },
    }


# ─────────── 回测 ───────────

def backtest(days: int = 30, fixtures_dir: Path | None = None) -> dict[str, Any]:
    """用历史 JSON fixtures 跑 picker，输出虚拟 PnL 曲线

    fixtures_dir 期望: tests/fixtures/  下有 pick_YYYYMMDD.json 文件
    每个 JSON 应有 filtered_stocks 字段
    """
    if fixtures_dir is None:
        fixtures_dir = FIXTURES_DIR
    if not fixtures_dir.exists():
        return {"error": f"fixtures 目录不存在: {fixtures_dir}"}

    files = sorted(fixtures_dir.glob("pick_*.json"))[-days:]
    if not files:
        return {"error": "没有历史 fixtures 可回测"}

    cfg = load_config()
    initial_cap = cfg["paper"]["initial_capital"]
    cash = initial_cap
    max_concurrent = cfg["paper"]["max_concurrent"]
    max_pos_ratio = cfg["paper"]["max_position_ratio"]

    daily_pnl: list[dict[str, Any]] = []
    open_positions: dict[str, dict[str, Any]] = {}  # code -> {open_price, shares, pick_date, score}

    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        date = f.stem.replace("pick_", "")
        plan = data.get("plan", "C")
        stocks = data.get("filtered_stocks", [])
        env = data.get("market_env", {})
        pos_ratio = env.get("position_ratio", 0)

        # 1) 平掉昨天的仓（假设按 next_noon 价成交）—— fixtures 里已包含
        closed_today = 0
        pnl_today = 0.0
        for code, pos in list(open_positions.items()):
            fill_price = data.get("next_noon_prices", {}).get(code)
            if fill_price is None:
                continue
            pnl_pct = (fill_price - pos["open_price"]) / pos["open_price"]
            pnl_amt = pnl_pct * pos["shares"] * pos["open_price"]
            cash += pos["shares"] * fill_price
            pnl_today += pnl_amt
            closed_today += 1
            del open_positions[code]

        # 2) 开新仓
        opened_today = 0
        if plan in ("A", "B") and pos_ratio > 0:
            n_open = min(len(stocks), max_concurrent)
            if n_open > 0:
                per_position = cash * pos_ratio / n_open
                for s in stocks[:n_open]:
                    price = float(s.get("price", 0) or 0)
                    if price <= 0:
                        continue
                    amount = per_position
                    shares = int(amount / price / 100) * 100
                    if shares <= 0:
                        continue
                    cost = shares * price
                    if cost > cash:
                        continue
                    cash -= cost
                    open_positions[s["code"]] = {
                        "open_price": price,
                        "shares": shares,
                        "pick_date": date,
                        "score": s.get("score", 0),
                    }
                    opened_today += 1

        daily_pnl.append({
            "date": date,
            "plan": plan,
            "opened": opened_today,
            "closed": closed_today,
            "pnl": round(pnl_today, 2),
            "cash": round(cash, 2),
            "open_count": len(open_positions),
        })

    # 剩余未平仓按最后一天 next_noon 价清掉（简化）
    final_cash = cash + sum(
        p["shares"] * p["open_price"] for p in open_positions.values()
    )
    total_pnl = final_cash - initial_cap
    wins = sum(1 for d in daily_pnl if d["pnl"] > 0)

    return {
        "days": len(files),
        "initial_capital": initial_cap,
        "final_cash": round(final_cash, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / initial_cap * 100, 2),
        "win_days": wins,
        "win_rate_pct": round(wins / len(daily_pnl) * 100, 1) if daily_pnl else 0,
        "daily_pnl": daily_pnl,
    }






# ─────────── 指标计算 ───────────

def _compute_metrics(daily_pnl: list[dict], initial_cap: float, config: dict) -> dict[str, Any]:
    """计算 Sharpe / max_dd / annualized_return / Calmar

    Args:
        daily_pnl: [{"pnl": float, "cash": float, ...}, ...]
        initial_cap: 初始资金
        config: config.yaml dict (拿 risk_free_rate / trading_days_per_year)

    Returns:
        {
          sharpe: float,         # 年化 Sharpe
          max_drawdown_pct: float,
          annualized_return_pct: float,
          calmar: float,
          volatility_pct: float,
        }
    """
    bt_cfg = config.get("backtest", {})
    risk_free = bt_cfg.get("risk_free_rate_pct", 2.0)  # 年化 %
    tdp_year = bt_cfg.get("trading_days_per_year", 240)

    if not daily_pnl:
        return {"sharpe": 0, "max_drawdown_pct": 0, "annualized_return_pct": 0,
                "calmar": 0, "volatility_pct": 0, "n_days": 0}

    pnls = [d.get("pnl", 0) for d in daily_pnl]
    # 用 pnl 百分比而不是金额, 便于跨资金规模比较
    # 但这里 daily PnL 是绝对金额, 需先归一化到 % / 资金
    pnl_pcts = []
    cash = initial_cap
    for pnl_amt in pnls:
        if cash > 0:
            pnl_pcts.append(pnl_amt / cash * 100)
        else:
            pnl_pcts.append(0)
        cash += pnl_amt

    n = len(pnl_pcts)
    avg = sum(pnl_pcts) / n
    var = sum((p - avg) ** 2 for p in pnl_pcts) / max(n - 1, 1)
    std = var ** 0.5
    daily_rf = risk_free / tdp_year
    # Sharpe = (avg - daily_rf) / std * sqrt(N)
    if std > 0:
        sharpe = (avg - daily_rf) / std * (tdp_year ** 0.5)
    else:
        sharpe = 0.0

    # 总收益
    final_cash = initial_cap + sum(pnls)
    total_ret_pct = (final_cash - initial_cap) / initial_cap * 100
    # 年化 (按实际交易日数)
    annualized = ((1 + total_ret_pct / 100) ** (tdp_year / max(n, 1)) - 1) * 100

    # Max drawdown: 跑 cumulative cash, 找最大回撤
    peak = initial_cap
    max_dd = 0.0
    cum = initial_cap
    for pnl_amt in pnls:
        cum += pnl_amt
        if cum > peak:
            peak = cum
        dd = (peak - cum) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Calmar = 年化 / max_dd
    calmar = annualized / max_dd if max_dd > 0 else 0.0

    # 年化波动率
    vol = std * (tdp_year ** 0.5)

    # ── Sortino: 只看下行波动率 (用日亏作为分母) ──
    downside = [p for p in pnl_pcts if p < daily_rf]
    if downside:
        downside_var = sum((p - daily_rf) ** 2 for p in downside) / max(len(downside), 1)
        downside_dev = downside_var ** 0.5
        sortino = (avg - daily_rf) / downside_dev * (tdp_year ** 0.5) if downside_dev > 0 else 0.0
    else:
        sortino = 0.0  # 无下行 = 完美 (不无限大)

    # ── Information Ratio: (策略 - 基准) / 跟踪误差, 这里用 0 基准 ──
    # 实际用 (avg - 0) / std 当简化版, 跟 Sharpe 一样; v1 不引入外部基准
    info_ratio = sharpe  # v1 简化

    # ── Max consecutive losses / wins ──
    max_cons_loss = 0
    max_cons_win = 0
    cur_loss = cur_win = 0
    for p in pnl_pcts:
        if p < 0:
            cur_loss += 1
            cur_win = 0
            max_cons_loss = max(max_cons_loss, cur_loss)
        elif p > 0:
            cur_win += 1
            cur_loss = 0
            max_cons_win = max(max_cons_win, cur_win)
        else:
            cur_loss = cur_win = 0

    # ── Profit factor: 总盈利 / 总亏损 ──
    gross_profit = sum(p for p in pnl_pcts if p > 0)
    gross_loss = abs(sum(p for p in pnl_pcts if p < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

    # ── Win rate (按 pnl 算) ──
    wins = sum(1 for p in pnl_pcts if p > 0)
    win_rate = round(wins / n * 100, 1) if n > 0 else 0.0

    return {
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "information_ratio": round(info_ratio, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "annualized_return_pct": round(annualized, 2),
        "calmar": round(calmar, 2),
        "volatility_pct": round(vol, 2),
        "max_consecutive_losses": max_cons_loss,
        "max_consecutive_wins": max_cons_win,
        "profit_factor": profit_factor,
        "win_rate_pct": win_rate,
        "n_days": n,
    }


def _simulate_with_plan(files, plan_override, cfg, multi_strategy_callable=None) -> dict[str, Any]:
    """单策略回测 (拆出来给 backtest_multi 复用)

    Args:
        files: fixture paths
        plan_override: "A" / "B" / "C" / "auto" / None
        cfg: config
        multi_strategy_callable: 用于 "auto" 时调 multi_strategy.run (None = 用实际存的 plan)
    """
    initial_cap = cfg["paper"]["initial_capital"]
    max_concurrent = cfg["paper"]["max_concurrent"]
    cash = initial_cap
    open_positions: dict[str, dict] = {}
    daily: list[dict] = []

    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        date = f.stem.replace("pick_", "")
        stocks = data.get("filtered_stocks", [])
        env = data.get("market_env", {})
        pos_ratio = env.get("position_ratio", 0)
        actual_plan = data.get("plan", "C")

        # 决定当日 plan
        if plan_override == "auto":
            if multi_strategy_callable is not None:
                try:
                    ms_result = multi_strategy_callable(
                        stocks=stocks, market_env=env, hot_sectors=[], config=cfg,
                    )
                    use_plan = ms_result["recommendation"]
                except Exception:
                    use_plan = actual_plan
            else:
                use_plan = actual_plan
        else:
            use_plan = plan_override

        # 平仓
        pnl_today = 0.0
        for code, pos in list(open_positions.items()):
            fill = data.get("next_noon_prices", {}).get(code)
            if fill is None:
                continue
            pnl = (fill - pos["open_price"]) / pos["open_price"] * pos["shares"] * pos["open_price"]
            cash += pos["shares"] * fill
            pnl_today += pnl
            del open_positions[code]

        # 开仓
        opened = 0
        if use_plan in ("A", "B") and pos_ratio > 0:
            final, _stats = _plan_filter(stocks, use_plan, cfg)
            n_open = min(len(final), max_concurrent)
            if n_open > 0:
                per = cash * pos_ratio / n_open
                for s in final[:n_open]:
                    price = float(s.get("price", 0) or 0)
                    if price <= 0:
                        continue
                    shares = int(per / price / 100) * 100
                    if shares <= 0:
                        continue
                    cost = shares * price
                    if cost > cash:
                        continue
                    cash -= cost
                    open_positions[s["code"]] = {
                        "open_price": price, "shares": shares, "pick_date": date,
                    }
                    opened += 1

        daily.append({"date": date, "plan": use_plan, "pnl": round(pnl_today, 2),
                      "cash": round(cash, 2), "open": len(open_positions)})

    final_cash = cash + sum(p["shares"] * p["open_price"] for p in open_positions.values())
    wins = sum(1 for d in daily if d["pnl"] > 0)

    metrics = _compute_metrics(daily, initial_cap, cfg)
    return {
        "n": len(files),
        "final_cash": round(final_cash, 2),
        "total_pnl": round(final_cash - initial_cap, 2),
        "total_pnl_pct": round((final_cash - initial_cap) / initial_cap * 100, 2),
        "win_days": wins,
        "win_rate_pct": round(wins / len(daily) * 100, 1) if daily else 0,
        "metrics": metrics,
        "daily": daily,  # 给 _compute_metrics / debug 用
    }

# ─────────── 多策略回测 ───────────

def backtest_multi(days: int = 30, fixtures_dir: Path | None = None) -> dict[str, Any]:
    """对比 3 种策略在同一组历史数据上的表现:
    - fixed_A: 强制用方案 A
    - fixed_B: 强制用方案 B
    - auto: 用 multi_strategy.run() 投票

    Returns:
        {
          days,
          fixed_A: {n, total_pnl, win_days, win_rate_pct},
          fixed_B: {...},
          auto:    {...},
          recommendation: str
        }
    """
    if fixtures_dir is None:
        fixtures_dir = FIXTURES_DIR
    if not fixtures_dir.exists():
        return {"error": f"fixtures 目录不存在: {fixtures_dir}"}

    files = sorted(fixtures_dir.glob("pick_*.json"))[-days:]
    if not files:
        return {"error": "没有历史 fixtures 可回测"}

    cfg = load_config()
    initial_cap = cfg["paper"]["initial_capital"]
    max_concurrent = cfg["paper"]["max_concurrent"]
    max_pos_ratio = cfg["paper"]["max_position_ratio"]


    rA = _simulate_with_plan(files, "A", cfg)
    rB = _simulate_with_plan(files, "B", cfg)
    rC = _simulate_with_plan(files, "C", cfg)
    rAuto = _simulate_with_plan(files, "auto", cfg, multi_strategy_callable=multi_strategy_run)

    # 选最优 (按 Sharpe 而不是 total_pnl_pct, 更稳健)
    candidates = [("fixed_A", rA), ("fixed_B", rB), ("auto", rAuto)]
    candidates.sort(key=lambda x: -x[1]["metrics"]["sharpe"])
    recommendation = candidates[0][0] if candidates else "auto"

    # 输出不带 daily 列表 (太大)
    def _strip_daily(r):
        out = {k: v for k, v in r.items() if k != "daily"}
        return out

    return {
        "days": len(files),
        "initial_capital": initial_cap,
        "fixed_A": _strip_daily(rA),
        "fixed_B": _strip_daily(rB),
        "fixed_C": _strip_daily(rC),
        "auto": _strip_daily(rAuto),
        "recommendation": recommendation,
        "delta_sharpe_vs_worst": round(
            candidates[0][1]["metrics"]["sharpe"] - candidates[-1][1]["metrics"]["sharpe"], 2
        ),
        "delta_pnl_pct_vs_worst": round(
            candidates[0][1]["total_pnl_pct"] - candidates[-1][1]["total_pnl_pct"], 2
        ),
    }



# ─────────── 累积统计（v1 评估窗口用） ───────────

def cumulative_stats() -> dict[str, Any]:
    """从 picks + paper_positions 算全量胜率"""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT p.pick_date, p.code, p.name, p.score, p.chg_pct, p.sector, p.plan_used,
               pos.pnl_open_pct, pos.pnl_noon_pct
        FROM picks p
        LEFT JOIN paper_positions pos ON pos.code = p.code AND pos.pick_date = p.pick_date
        ORDER BY p.pick_date
        """
    ).fetchall()

    closed_noon = [dict(r) for r in rows if r["pnl_noon_pct"] is not None]
    if not closed_noon:
        return {"n": 0}

    pnls = [r["pnl_noon_pct"] for r in closed_noon]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(closed_noon),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 2),
        "win_rate_pct": round(wins / len(pnls) * 100, 1),
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
    }
