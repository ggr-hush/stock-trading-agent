"""agent/stages.py — v12.6 阶段定义 + 调度

v12.6 拆出来: 6 stage + 2 push + STAGE_REGISTRY + PUSH_REGISTRY + 拓扑排序
+ build_scheduler + catch_up_stages + _check_dependencies + _convert_cron。

stages.py 是纯业务层, 不依赖 supervisor/cli/webhook。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from ..engine.data_fetcher import is_trading_day, load_config
from ..engine.intraday import intraday_monitor
from ..engine.paper_trader import (
    get_db,
    get_open_positions,
    init_account,
    mark_stage_run,
    open_positions,
    was_stage_run_today,
)
from ..engine.picker import pick
from ..engine.reviewer import (
    backtest_multi,
    run_daily_review,
    run_weekly_review,
)
from ..engine.report import push_weekly_report
from ..feishu import pusher
from ..llm.reasoner import weekly_summary

log = logging.getLogger("agent.stages")

PID_FILE = None  # 占位, supervisor.PID_FILE 才是真 (re-export)


# ─────────── 6 个阶段 ───────────

def stage_pre_market() -> dict[str, Any]:
    if not is_trading_day():
        log.info("非交易日, 跳过盘前复盘")
        return {"skipped": "weekend"}
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    summary = run_daily_review(yesterday)
    pusher.push_pre_market(summary)
    log.info("盘前复盘推送: %s", yesterday)
    return summary


def stage_open_auction() -> dict[str, Any]:
    opens = get_open_positions()
    if opens:
        pusher.push_anomaly(f"今日开盘需关注 paper 持仓 {len(opens)} 只")
    return {"open_count": len(opens), "opens": opens}


def stage_pick() -> dict[str, Any]:
    if not is_trading_day():
        log.info("非交易日, 跳过选股")
        return {"skipped": "weekend"}
    cfg = load_config()
    result = pick(cfg)
    n_open = open_positions(result, cfg)
    if result["plan_used"] == "C":
        env = result["market_env"]
        reason = (
            f"方案 A/B 均无候选（涨幅 3-4% 区间共 "
            f"{result['stats'].get('plan_a_count', 0) + result['stats'].get('plan_b_count', 0)} 只, "
            f"均不满足换手/振幅/市值/成交额门槛）"
        )
        pusher.push_empty_day(reason, env)
    else:
        pusher.push_pick(result)
    log.info("选股完成: 方案=%s, 候选=%d, 开仓=%d",
             result["plan_used"], len(result["filtered_stocks"]), n_open)
    return {"plan": result["plan_used"], "n_open": n_open}


def stage_post_market() -> dict[str, Any]:
    pusher.push_post_market(0, "占位", [])
    return {"ok": True}


def stage_evening() -> dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    summary = run_daily_review(today)
    pusher.push_evening(summary)
    return summary


def stage_intraday_monitor() -> dict[str, Any]:
    """v9.3: 盘中盯盘, 异动推飞书告警"""
    return intraday_monitor()


def stage_weekly_review() -> dict[str, Any]:
    """v7.3: 周日 20:00 自动跑全量周报"""
    cfg = load_config()
    days = int(cfg.get("weekly_auto_backtest_days", 30))
    weekly = run_weekly_review()
    bt = backtest_multi(days=days) if days > 0 else None
    try:
        summary = weekly_summary(weekly)
    except Exception:  # noqa: BLE001
        summary = ""
    result = push_weekly_report(weekly, bt, summary, save_pdf=True)
    log.info("[stage_weekly_review] saved=%s feishu_ok=%s",
             result.get("saved"), result.get("feishu", {}).get("ok"))
    return {"weekly": weekly, "backtest": bt, "summary": summary, "result": result}


# v7.4: stage 依赖图
STAGE_REGISTRY: dict[str, dict[str, Any]] = {
    "pre_market":    {"fn": stage_pre_market,    "depends": []},
    "open_auction":  {"fn": stage_open_auction,  "depends": ["pre_market"]},
    "pick":          {"fn": stage_pick,          "depends": ["open_auction"]},
    "post_market":   {"fn": stage_post_market,   "depends": ["pick"]},
    "evening":       {"fn": stage_evening,       "depends": ["post_market"]},
    "weekly_review": {"fn": stage_weekly_review, "depends": ["evening"], "day_filter": "weekend"},
    "intraday_monitor": {"fn": stage_intraday_monitor, "depends": []},
}


# v12: 轻主动推送注册表
def _push_daily_summary() -> dict[str, Any]:
    """v12: 15:35 收盘日报推送 (轻主动)"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        stats = run_daily_review(today) or {}
        positions = get_open_positions() or []
        result = pusher.push_daily_summary(stats, positions)
        mark_stage_run("daily_summary_push", ok=result.get("ok", False))
        return result
    except Exception as e:  # noqa: BLE001
        log.error("daily_summary_push 失败: %s", e)
        try:
            mark_stage_run("daily_summary_push", ok=False)
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


def _push_anomaly_recap() -> dict[str, Any]:
    """v12: 19:05 当日异动复盘推送 (轻主动)"""
    try:
        result = pusher.push_anomaly_recap(intraday_anomalies=[])
        mark_stage_run("anomaly_recap_push", ok=result.get("ok", False))
        return result
    except Exception as e:  # noqa: BLE001
        log.error("anomaly_recap_push 失败: %s", e)
        try:
            mark_stage_run("anomaly_recap_push", ok=False)
        except Exception:
            pass
        return {"ok": False, "error": str(e)}


PUSH_REGISTRY: dict[str, dict[str, Any]] = {
    "daily_summary_push": {"fn": _push_daily_summary},
    "anomaly_recap_push": {"fn": _push_anomaly_recap},
}


# ─────────── 依赖图 + 调度 ───────────

def topological_sort(stages: dict[str, dict[str, Any]]) -> list[str]:
    """v7.4: 拓扑排序, 循环依赖抛 ValueError"""
    visited: set[str] = set()
    order: list[str] = []
    in_stack: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in in_stack:
            raise ValueError(f"循环依赖: {' -> '.join(path + [node])}")
        if node in visited:
            return
        in_stack.add(node)
        for dep in stages.get(node, {}).get("depends", []):
            if dep not in stages:
                raise ValueError(f"{node} 依赖未知 stage: {dep}")
            visit(dep, path + [node])
        in_stack.discard(node)
        visited.add(node)
        order.append(node)

    for s_name in stages:
        visit(s_name, [])
    return order


def validate_stage_deps() -> None:
    """v7.4: 启动时校验"""
    topo = topological_sort(STAGE_REGISTRY)
    log.info("stage 依赖图校验通过, 拓扑序: %s", topo)


def _check_dependencies(stage: str) -> list[str]:
    """v7.4: 返回该 stage 当日未跑的依赖, 列表为空表示 OK"""
    cfg = STAGE_REGISTRY.get(stage, {})
    missing: list[str] = []
    for dep in cfg.get("depends", []):
        if not was_stage_run_today(dep):
            missing.append(dep)
    return missing


def build_scheduler():
    """v7.4: 构造 BlockingScheduler, 注册 STAGE_REGISTRY + PUSH_REGISTRY 全部 cron"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    cfg = load_config()
    sched_cfg = cfg["schedule"]
    validate_stage_deps()
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    for stage_name, stage_cfg in STAGE_REGISTRY.items():
        cron = sched_cfg.get(stage_name)
        if not cron:
            log.warning("schedule 缺 %s, 跳过注册", stage_name)
            continue
        sched.add_job(stage_cfg["fn"], CronTrigger.from_crontab(_convert_cron_to_apscheduler(cron)), id=stage_name)
    for push_name, push_cfg in PUSH_REGISTRY.items():
        cron = sched_cfg.get(push_name)
        if not cron:
            log.info("schedule 缺 %s (轻主动), 跳过注册", push_name)
            continue
        sched.add_job(push_cfg["fn"], CronTrigger.from_crontab(_convert_cron_to_apscheduler(cron)), id=push_name)
        log.info("v12 push 注册: %s cron=%s", push_name, cron)
    return sched


# ─────────── 上下文查询 (给 LLM dispatch 用) ───────────

def _recent_picks_for_question(n: int = 10) -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT pick_date, code, name, score, sector, plan_used FROM picks ORDER BY pick_date DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_market_env() -> dict[str, Any]:
    conn = get_db()
    row = conn.execute(
        "SELECT market_env_score, market_env_level, pick_date FROM picks ORDER BY pick_date DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    return {
        "env_score": row["market_env_score"],
        "env_level": row["market_env_level"],
        "position_advice": "未知",
    }


# ─────────── catch-up + run-once ───────────

def run_once(stage: str) -> dict[str, Any]:
    if stage not in STAGE_REGISTRY:
        raise ValueError(f"未知 stage: {stage}; 可选: {list(STAGE_REGISTRY)}")
    missing = _check_dependencies(stage)
    if missing:
        log.warning("[%s] 依赖 stage 今日未跑: %s (仍继续, run-once 不强制)",
                    stage, missing)
    log.info("run-once: %s", stage)
    init_account()
    try:
        result = STAGE_REGISTRY[stage]["fn"]()
        mark_stage_run(stage, ok=True)
        return result
    except Exception as e:
        mark_stage_run(stage, ok=False)
        raise


def _convert_cron_to_apscheduler(cron_expr: str) -> str:
    """把标准 cron (0=Sun) 翻译成 apscheduler 3.11+ (0=Mon) 风格。"""
    parts = cron_expr.split()
    if len(parts) != 5:
        return cron_expr
    dow = parts[4]
    if dow == "*":
        return cron_expr

    def remap(tok: str) -> str:
        if tok == "*":
            return tok
        if "/" in tok:
            base, step = tok.split("/", 1)
            return f"{remap(base)}/{step}"
        if "," in tok:
            return ",".join(remap(x) for x in tok.split(","))
        if "-" in tok:
            lo, hi = tok.split("-", 1)
            lo_a, hi_a = (int(lo) + 6) % 7, (int(hi) + 6) % 7
            if lo_a <= hi_a:
                return f"{lo_a}-{hi_a}"
            return f"{lo_a}-6,0-{hi_a}"
        return str((int(tok) + 6) % 7)

    parts[4] = remap(dow)
    return " ".join(parts)


def _cron_should_have_run(cron_expr: str, now) -> bool:
    """v8.2: 判断 cron 表达式今天是否已经"该跑" """
    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("apscheduler 缺失, _cron_should_have_run 不可用")
        return False
    try:
        trigger = CronTrigger.from_crontab(_convert_cron_to_apscheduler(cron_expr))
        from datetime import timedelta
        now_aware = now if now.tzinfo else now.replace(tzinfo=trigger.timezone)
        earliest = now_aware - timedelta(days=1)
        prev = trigger.get_next_fire_time(earliest, now_aware)
        if prev is None or prev > now_aware:
            return False
        prev_naive = prev.replace(tzinfo=None)
        return prev_naive.date() == now.date()
    except Exception as e:
        log.warning("cron 解析失败 (%s): %s", cron_expr, e)
        return False


def catch_up_stages(now=None) -> list[str]:
    """v8.2: 漏跑 stage 自动补跑"""
    from datetime import datetime as _dt
    if now is None:
        now = _dt.now()
    cfg = load_config()
    sched_cfg = cfg.get("schedule", {})
    caught: list[str] = []
    for stage_name, stage_cfg in STAGE_REGISTRY.items():
        if was_stage_run_today(stage_name):
            continue
        cron_expr = sched_cfg.get(stage_name)
        if not cron_expr:
            continue
        if not _cron_should_have_run(cron_expr, now):
            continue
        missing = _check_dependencies(stage_name)
        if missing:
            log.warning("[catch-up %s] 依赖未跑: %s (仍继续)", stage_name, missing)
        log.info("[catch-up] 补跑 %s (now=%s)", stage_name, now)
        try:
            stage_cfg["fn"]()
            mark_stage_run(stage_name, ok=True)
            caught.append(stage_name)
        except Exception as e:
            mark_stage_run(stage_name, ok=False)
            log.error("[catch-up %s] 失败: %s", stage_name, e)
    return caught


def run_daemon(catch_up: bool = False) -> None:
    """v7: 阻塞跑 BlockingScheduler (单独 subcommand, 不带飞书)"""
    init_account()
    if catch_up:
        caught = catch_up_stages()
        if caught:
            log.info("[catch-up] 已补跑: %s", caught)
        else:
            log.info("[catch-up] 无需补跑")
    sched = build_scheduler()
    log.info("agent 启动, 调度注册:")
    for job in sched.get_jobs():
        log.info("  - %s: %s", job.id, job.trigger)
    sched.start()
