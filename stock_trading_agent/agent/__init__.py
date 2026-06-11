"""agent 子包 — v12.6

子模块:
  - stages: 6 stage + 2 push + 调度 + 上下文查询 + catch-up
  - supervisor: PID / 启停 / watchdog / 自重启
  - webhook: HTTP 服务 (chat/reset/health)
  - cli: argparse 入口

用法:
  - python -m stock_trading_agent.agent <subcmd>
  - from stock_trading_agent.agent import STAGE_REGISTRY, build_scheduler, ...
"""
from __future__ import annotations

# v7 测试 patch 兼容: `patch.object(ag, "run_weekly_review", ...)` 等
from ..engine.data_fetcher import load_config
from ..engine.report import push_weekly_report
from ..engine.reviewer import backtest_multi, run_weekly_review
from ..llm.reasoner import weekly_summary

from .cli import _handle_memory_cmd, main
from .stages import (
    PUSH_REGISTRY,
    STAGE_REGISTRY,
    _check_dependencies,
    _cron_should_have_run,
    _latest_market_env,
    _recent_picks_for_question,
    backtest_multi,  # re-export
    build_scheduler,
    catch_up_stages,
    run_daemon,
    run_once,
    stage_intraday_monitor,
    stage_weekly_review,
    topological_sort,
    validate_stage_deps,
)
from .supervisor import (
    AUTO_RESTART_COUNT_FILE,
    PID_FILE,
    _check_already_running,
    _listener_lifecycle,
    _restart_executor,
    _run_supervisor,
    _self_exec_restart,
    _stop_agent,
    _write_pid,
)
from .webhook import _WebhookHandler, run_webhook

__all__ = [
    # cli
    "_handle_memory_cmd", "main",
    # stages
    "PUSH_REGISTRY", "STAGE_REGISTRY", "_check_dependencies",
    "_cron_should_have_run", "_latest_market_env", "_recent_picks_for_question",
    "build_scheduler", "catch_up_stages", "run_daemon", "run_once",
    "stage_intraday_monitor", "stage_weekly_review",
    "topological_sort", "validate_stage_deps",
    # supervisor
    "AUTO_RESTART_COUNT_FILE", "PID_FILE", "_check_already_running",
    "_listener_lifecycle", "_restart_executor", "_run_supervisor",
    "_self_exec_restart", "_stop_agent", "_write_pid",
    # webhook
    "_WebhookHandler", "run_webhook",
    # v7 兼容 re-export
    "backtest_multi", "load_config", "push_weekly_report",
    "run_weekly_review", "weekly_summary",
]
