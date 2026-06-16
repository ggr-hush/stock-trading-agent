"""agent/cli.py — v12.6 CLI 入口

main() 解析 argparse, 根据 subcommand 调对应模块:
  - start/stop → supervisor
  - daemon / run-once → stages
  - webhook → webhook
  - listen → feishu.listener
  - report → engine.report / reviewer
  - memory → assistant.memory
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from typing import Any

log = logging.getLogger("agent.cli")


def _handle_review_cmd(args) -> None:
    """v12.A.4: 处理 `agent review list` 子命令 (轻量)"""
    from ..engine.reviews import query_reviews
    from ..engine.paper_trader import init_account
    init_account()
    if args.action == "list":
        items = query_reviews(limit=50)
        if not items:
            print("  (无复盘)")
        else:
            print(f"  复盘共 {len(items)} 条:")
            for r in items:
                action = "✅" if r.get("action_taken") else "👀"
                print(f"    {action} [{r.get('date', '?')}] {r.get('stock_code', '?')} "
                      f"{r.get('result', '')} "
                      f"tags={r.get('tags', [])}  ({(r.get('summary', '') or '')[:40]})")


def _handle_memory_cmd(args) -> None:
    """处理 `agent memory list/clear --chat-id X` 子命令"""
    from ..assistant.memory import list_memories as _list_mem, clear_memories as _clear_mem
    from ..engine.paper_trader import init_account
    init_account()
    if args.action == "list":
        mems = _list_mem(args.chat_id, limit=50)
        if not mems:
            print(f"  (chat_id={args.chat_id} 无记忆)")
        else:
            print(f"  chat_id={args.chat_id} 共 {len(mems)} 条记忆:")
            for m in mems:
                print(f"    [{m.get('type', '?')}] imp={m.get('importance', 1)} "
                      f"{m.get('content', '')}  (源: {m.get('source', '?')}, "
                      f"at: {m.get('created_at', '?')})")
    elif args.action == "clear":
        n = _clear_mem(args.chat_id)
        print(f"  ✓ 已清空 {n} 条记忆 (chat_id={args.chat_id})")


def _install_daemon_signals() -> None:
    """daemon 子命令的信号 handler (主线程)"""
    def _shutdown(signum, _frame):
        log.info("收到信号 %s, 关闭 agent", signum)
        sys.exit(0)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


def main() -> None:
    parser = argparse.ArgumentParser(description="stock_trading_agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="v11: 单进程启动 scheduler + 飞书监听")
    p_stop = sub.add_parser("stop", help="v11: 停掉 agent start 起的进程")
    p_daemon = sub.add_parser("daemon", help="常驻进程模式 (仅 scheduler, 不含飞书)")
    p_daemon.add_argument("--catch-up", action="store_true",
                          help="v8.2: 启动时补跑今日已到时间但未跑的 stage")
    p_run_once = sub.add_parser("run-once", help="单次跑某个阶段")
    p_run_once.add_argument("--stage", required=True)
    p_webhook = sub.add_parser("webhook", help="启动 HTTP 服务 (bot)")
    p_webhook.add_argument("--host", default=os.environ.get("WEBHOOK_HOST", "127.0.0.1"))
    p_webhook.add_argument("--port", type=int, default=int(os.environ.get("WEBHOOK_PORT", "8765")))
    p_listen = sub.add_parser("listen", help="飞书事件订阅 (lark-oapi WebSocket)")
    p_listen.add_argument("--stop-after", type=int, default=None)
    p_listen.add_argument("--quiet", action="store_true")
    p_report = sub.add_parser("report", help="生成周报 + 多策略回测 + 推飞书")
    p_report.add_argument("--days", type=int, default=30)
    p_report.add_argument("--no-push", action="store_true")
    p_report.add_argument("--no-backtest", action="store_true")
    p_report.add_argument("--pdf", action="store_true")
    p_memory = sub.add_parser("memory", help="v12: 用户记忆管理")
    p_dedup = sub.add_parser("dedup", help="v12.8: 飞书重投去重计数器")
    p_dedup.add_argument("action", choices=["stats", "reset"])
    p_memory.add_argument("action", choices=["list", "clear"])
    p_memory.add_argument("--chat-id", default="default")
    # v12.A.3: tuner dry-run 屏障
    p_review = sub.add_parser("review", help="v12.A.4: 复盘管理 (list)")
    p_review.add_argument("action", choices=["list"])
    p_weekly_review = sub.add_parser("weekly-review", help="v12.A.3: 调参 (默认 dry-run, --write 真改)")
    p_weekly_review.add_argument("--write", action="store_true",
                                  help="真写到 config.yaml + params_history (默认只看 preview)")
    p_weekly_review.add_argument("--json", action="store_true",
                                  help="输出 JSON (admin 卡片用)")

    args = parser.parse_args()

    if args.cmd == "start":
        from .supervisor import _check_already_running, _run_supervisor
        _check_already_running()
        _run_supervisor()
        return
    if args.cmd == "stop":
        from .supervisor import _stop_agent
        _stop_agent()
        return
    if args.cmd == "memory":
        _handle_memory_cmd(args)
        return
    if args.cmd == "review":
        _handle_review_cmd(args)
        return
    if args.cmd == "dedup":
        from .dedup_cli import dispatch as _dedup_dispatch
        sys.exit(_dedup_dispatch(args.action))
    if args.cmd == "weekly-review":
        from ..engine.tuner import run_weekly_tune
        from ..engine.paper_trader import init_account
        init_account()
        dry_run = not args.write
        result = run_weekly_tune(dry_run=dry_run)
        if args.json:
            print(json.dumps({
                "dry_run": dry_run,
                "preview_count": len(result.get("preview", [])),
                "applied_count": len(result.get("applied", [])),
                "pending_count": len(result.get("pending", [])),
                "preview": result.get("preview", []),
                "applied": result.get("applied", []),
                "pending": result.get("pending", []),
                "stats": result.get("stats", {}),
            }, ensure_ascii=False, indent=2, default=str))
        else:
            mode = "🔍 dry-run" if dry_run else "✍️ APPLY"
            print(f"{mode} weekly-tune 完毕:")
            print(f"  preview/applied: {len(result.get('preview' if dry_run else 'applied', []))}")
            print(f"  pending (待用户确认): {len(result.get('pending', []))}")
            for p in (result.get("preview", []) if dry_run else result.get("applied", [])):
                print(f"    · {p.get('param', '?')}: {p.get('old', '?')} → {p.get('new', '?')}")
        return
    if args.cmd == "daemon":
        from .stages import run_daemon
        _install_daemon_signals()
        run_daemon(catch_up=getattr(args, "catch_up", False))
    elif args.cmd == "run-once":
        from .stages import run_once
        result = run_once(args.stage)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "webhook":
        from .webhook import run_webhook
        run_webhook(args.host, args.port)
    elif args.cmd == "listen":
        from ..feishu import listener as _listener
        _listener.run(stop_after=args.stop_after, quiet=args.quiet)
    elif args.cmd == "report":
        from ..engine.report import push_weekly_report, render_weekly
        from ..engine.reviewer import run_weekly_review, backtest_multi
        from ..llm.reasoner import weekly_summary
        from .stages import stage_weekly_review
        weekly = run_weekly_review()
        bt = None if args.no_backtest else backtest_multi(days=args.days)
        try:
            summary = weekly_summary(weekly)
        except Exception:
            summary = ""
        if args.no_push:
            content = render_weekly(weekly, bt, summary)
            print(content)
        else:
            result = push_weekly_report(weekly, bt, summary, save_pdf=args.pdf)
            out = {"saved": result["saved"], "feishu_ok": result["feishu"].get("ok")}
            if result.get("saved_pdf"):
                out["saved_pdf"] = result["saved_pdf"]
            print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
