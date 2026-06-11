"""agent.py — v12.6 兼容 shim

新逻辑拆分到 stock_trading_agent/agent/ 子包:
  - stages.py   6 stage + 2 push + 调度
  - supervisor.py  PID/启停/watchdog/自重启
  - webhook.py  HTTP 服务
  - cli.py      argparse 入口

本文件仅为向后兼容: 所有原 import 路径 (`from stock_trading_agent.agent import X`)
依然能用, 转发到子包对应位置。

`python -m stock_trading_agent.agent <subcmd>` 走 agent/cli.py:main
"""
from __future__ import annotations

import logging
import sys

# 顶部 basicConfig 兼容 (老代码会调 logging.getLogger("agent") 拿 logger)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# v7 测试 patch 兼容: 老的 patch.object(ag, "run_weekly_review", ...) 模式
from ..engine.data_fetcher import load_config
from ..engine.report import push_weekly_report
from ..engine.reviewer import backtest_multi, run_weekly_review
from ..llm.reasoner import weekly_summary

# 子包 re-export
# 注: 下面这一坨是兼容 shim — 老的测试 `from stock_trading_agent.agent import X` / 
# `patch("stock_trading_agent.agent.X", ...)` 仍然能用
from .agent import (  # noqa: E402, F401
    AUTO_RESTART_COUNT_FILE,
    PID_FILE,
    PUSH_REGISTRY,
    STAGE_REGISTRY,
    _WebhookHandler,
    _check_already_running,
    _check_dependencies,
    _cron_should_have_run,
    _latest_market_env,
    _listener_lifecycle,
    _recent_picks_for_question,
    _restart_executor,
    _run_supervisor,
    _self_exec_restart,
    _stop_agent,
    _write_pid,
    build_scheduler,
    catch_up_stages,
    main,
    run_daemon,
    run_once,
    run_webhook,
    stage_intraday_monitor,
    stage_weekly_review,
    validate_stage_deps,
)

__all__ = [
    "AUTO_RESTART_COUNT_FILE", "PID_FILE", "PUSH_REGISTRY", "STAGE_REGISTRY",
    "_WebhookHandler", "_check_already_running", "_check_dependencies",
    "_cron_should_have_run", "_latest_market_env", "_listener_lifecycle",
    "_recent_picks_for_question", "_restart_executor", "_run_supervisor",
    "_self_exec_restart", "_stop_agent", "_write_pid",
    "build_scheduler", "catch_up_stages", "main", "run_daemon", "run_once",
    "run_webhook", "stage_intraday_monitor", "stage_weekly_review",
    "topological_sort", "validate_stage_deps",
    # v7 测试 patch 兼容 (老 agent.py 顶层 re-export 的内部引用)
    "run_weekly_review", "backtest_multi", "load_config",
    "weekly_summary", "push_weekly_report",
]


if __name__ == "__main__":
    sys.exit(main() or 0)
