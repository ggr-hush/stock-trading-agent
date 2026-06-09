"""
agent.py — 多阶段常驻 agent 入口

6 个阶段（APScheduler cron）:
  08:30  pre_market      盘前复盘
  09:15  open_auction    集合竞价风控
  14:00  pick            尾盘选股 + paper 开仓
  15:30  post_market     盘后对账
  19:00  evening         晚间日报
  周日20:00 weekly_review 深度复盘 + 调参

子命令:
  daemon            守护进程模式
  run-once --stage  单次跑某个阶段
  webhook           启动 HTTP 服务（飞书 bot 调用）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .engine.data_fetcher import get_latest_trading_day, is_trading_day, load_config
from .engine.paper_trader import (
    fill_noon_prices,
    fill_open_prices,
    get_db,
    init_account,
    mark_stage_run,
    open_positions,
    was_stage_run_today,
)
from .engine.picker import pick
from .engine.reviewer import backtest_multi, run_daily_review, run_weekly_review
from .engine.report import push_weekly_report
from .engine.intraday import intraday_monitor
from .llm.reasoner import weekly_summary
from .feishu import pusher, listener as _listener
from .llm.reasoner import answer_question, chat_with_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent")

PID_FILE = Path("data/agent.pid")


def _write_pid() -> None:
    """写 supervisor 主线程 PID 到 data/agent.pid"""
    DATA_DIR_LOCAL = Path("data")
    DATA_DIR_LOCAL.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    log.info("supervisor PID %d 写入 %s", os.getpid(), PID_FILE)


def _stop_agent() -> None:
    """v11: agent stop 子命令 — 读 pid file, 发 SIGTERM, 等 5s, 不行 SIGKILL"""
    if not PID_FILE.exists():
        print(f"  (no pid file at {PID_FILE}, agent 可能没在跑)")
        return
    pid_str = PID_FILE.read_text().strip()
    try:
        pid = int(pid_str)
    except ValueError:
        print(f"  ✗ pid file 内容非法: {pid_str!r}")
        return
    print(f"  stopping agent (pid={pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"  (no process {pid}, 可能是 zombie pid file)")
        PID_FILE.unlink(missing_ok=True)
        return
    # 等最多 5s 让进程优雅退出
    import time
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)  # 检测进程是否还在
        except ProcessLookupError:
            print(f"  ✓ agent stopped (pid={pid})")
            PID_FILE.unlink(missing_ok=True)
            return
    # 5s 还没退, 强 KILL
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"  ⚠ agent didn't stop gracefully, SIGKILL'd (pid={pid})")
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)


def _install_daemon_signals() -> None:
    """daemon 子命令的信号 handler (主线程)"""
    def _shutdown(signum, _frame):
        log.info("收到信号 %s, 关闭 agent", signum)
        sys.exit(0)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


# v12: memory CLI handler (extract to function for testability)
def _handle_memory_cmd(args) -> None:
    """处理 `agent memory list/clear --chat-id X` 子命令"""
    from .assistant.memory import list_memories as _list_mem, clear_memories as _clear_mem
    from .engine.paper_trader import init_account
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
    from .engine.paper_trader import get_open_positions
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
    """v7.3: 周日 20:00 自动跑全量周报 (回测 + LLM 总结 + 落盘 + 推飞书)

    跟 `agent report` 命令走同一条路径, 但不带 --days 交互参数,
    用 config.weekly_auto_backtest_days (默认 30)。
    """
    cfg = load_config()
    days = int(cfg.get("weekly_auto_backtest_days", 30))
    weekly = run_weekly_review()
    bt = backtest_multi(days=days) if days > 0 else None
    try:
        summary = weekly_summary(weekly)
    except Exception:  # noqa: BLE001
        summary = ""
    result = push_weekly_report(weekly, bt, summary, save_pdf=True)  # v9.1
    log.info("[stage_weekly_review] saved=%s feishu_ok=%s",
             result.get("saved"), result.get("feishu", {}).get("ok"))
    return {"weekly": weekly, "backtest": bt, "summary": summary, "result": result}


# v7.4: stage 依赖图
#  - depends: 跑前必须先跑过的 stage (同日)
#  - day_filter: "all" (默认) / "weekday" / "weekend"
STAGE_REGISTRY: dict[str, dict[str, Any]] = {
    "pre_market":    {"fn": stage_pre_market,    "depends": []},
    "open_auction":  {"fn": stage_open_auction,  "depends": ["pre_market"]},
    "pick":          {"fn": stage_pick,          "depends": ["open_auction"]},
    "post_market":   {"fn": stage_post_market,   "depends": ["pick"]},
    "evening":       {"fn": stage_evening,       "depends": ["post_market"]},
    "weekly_review": {"fn": stage_weekly_review, "depends": ["evening"], "day_filter": "weekend"},
    "intraday_monitor": {"fn": stage_intraday_monitor, "depends": []},  # v9.3: 独立, 盘中每 5 分钟跑
}


# v12: 轻主动推送注册表 (跟 STAGE_REGISTRY 平级, 无依赖)
# 用法: 跟 stage 一样通过 schedule.cron 注册到 BlockingScheduler
# 设计取舍: 不放进 STAGE_REGISTRY 因为它们不参与 stage 依赖图 (推个卡片而已)
def _push_daily_summary() -> dict[str, Any]:
    """v12: 15:35 收盘日报推送 (轻主动)"""
    from .engine.paper_trader import get_open_positions
    from .engine.reviewer import run_daily_review
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
    """v12: 19:05 当日异动复盘推送 (轻主动)

    v12 简化: 不查 intraday_monitor 表 (那个表可能没有 v12 数据), 直接推"今日无异动"。
    v13 再做真实异动数据接入。
    """
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
    """v7.4: 启动时校验, 循环依赖 / 未知依赖 立即 raise"""
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
    from apscheduler.schedulers.blocking import BlockingScheduler  # noqa: PLC0415
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415
    cfg = load_config()
    sched_cfg = cfg["schedule"]
    validate_stage_deps()  # v7.4
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    for stage_name, stage_cfg in STAGE_REGISTRY.items():
        cron = sched_cfg.get(stage_name)
        if not cron:
            log.warning("schedule 缺 %s, 跳过注册", stage_name)
            continue
        sched.add_job(stage_cfg["fn"], CronTrigger.from_crontab(_convert_cron_to_apscheduler(cron)), id=stage_name)
    # v12: PUSH_REGISTRY 跟 STAGE_REGISTRY 走一样的 cron 通道
    for push_name, push_cfg in PUSH_REGISTRY.items():
        cron = sched_cfg.get(push_name)
        if not cron:
            log.info("schedule 缺 %s (轻主动), 跳过注册", push_name)
            continue
        sched.add_job(push_cfg["fn"], CronTrigger.from_crontab(_convert_cron_to_apscheduler(cron)), id=push_name)
        log.info("v12 push 注册: %s cron=%s", push_name, cron)
    return sched


# ─────────── Webhook (Bot HTTP) ───────────

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


class _WebhookHandler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "service": "stock_trading_agent"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._json(400, {"error": f"bad json: {e}"})
            return
        if path == "/chat":
            question = data.get("question", "").strip()
            if not question:
                self._json(400, {"error": "question is required"})
                return
            session_id = data.get("session_id") or self.headers.get("X-Session-Id") or "default"
            log.info("chat Q [%s]: %s", session_id, question[:80])
            try:
                answer = chat_with_session(
                    session_id,
                    question,
                    recent_picks=_recent_picks_for_question(),
                    market_env=_latest_market_env(),
                )
                if not answer:
                    answer = "（LLM 暂不可用, 请检查 MINIMAX_API_KEY; 知识库检索可单跑 python -m stock_trading_agent.engine.knowledge <query>）"
            except Exception as e:
                answer = f"（处理失败: {e}）"
            self._json(200, {"question": question, "session_id": session_id, "answer": answer})
        elif path == "/reset":
            session_id = data.get("session_id") or self.headers.get("X-Session-Id") or "default"
            from .engine.sessions import reset as _reset
            _reset(session_id)
            log.info("reset session: %s", session_id)
            self._json(200, {"session_id": session_id, "reset": True})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        log.info(fmt, *args)


def run_webhook(host: str = "127.0.0.1", port: int = 8765) -> None:
    init_account()
    server = ThreadingHTTPServer((host, port), _WebhookHandler)
    log.info("webhook 启动: http://%s:%d", host, port)
    log.info("  POST /chat {\"question\": \"...\"}")
    log.info("  GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("webhook 关闭")
        server.shutdown()


# ─────────── 入口 ───────────

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
    """把标准 cron (0=Sun) 翻译成 apscheduler 3.11+ (0=Mon) 风格。

    apscheduler 3.11 把 day_of_week 改成了 Python weekday() 编码 (0=Mon..6=Sun),
    与工业标准 cron (0=Sun..6=Sat) 差一格。项目里所有 cron 都按标准写,
    在喂给 CronTrigger 之前先过这个函数, 避免阶段被错位调度。
    """
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
            # 跨周日: e.g. "6-1" (Sat-Mon) → "5-6,0"
            return f"{lo_a}-6,0-{hi_a}"
        return str((int(tok) + 6) % 7)

    parts[4] = remap(dow)
    return " ".join(parts)


def _cron_should_have_run(cron_expr: str, now) -> bool:
    """v8.2: 判断 cron 表达式今天是否已经"该跑" (返回上一次的触发时间是否在今天且 <= now)

    用 apscheduler 解析 cron; 缺失时返回 False (不补跑)。

    注意: apscheduler 3.11+ 移除了 CronTrigger.get_prev_fire_time,
    只剩 get_next_fire_time(previous_fire_time, now)。这里从 now-1d 开始
    反查 "now-1d 之后的第一次触发" 即 "now 之前的最近一次触发"。
    """
    try:
        from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415
    except ImportError:
        log.warning("apscheduler 缺失, _cron_should_have_run 不可用")
        return False
    try:
        trigger = CronTrigger.from_crontab(_convert_cron_to_apscheduler(cron_expr))
        from datetime import timedelta
        # apscheduler 3.11+ 返回 aware datetime; 测试和 catch-up 经常传 naive now,
        # 统一在 trigger.timezone 里比较, 然后 drop tz 再 .date() 比对。
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
    """v8.2: 漏跑 stage 自动补跑

    逻辑: 遍历 STAGE_REGISTRY, 对每个 stage 用 cron 表达式判断今天是否已经"该跑"过
    (即 cron 今天的最近一次触发时间 < now), 若是但 stage_runs 表里没记录, 串行跑。

    Args:
        now: 测试用注入的当前时间; 生产默认 _dt.now()

    Returns: 补跑成功的 stage 列表
    """
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
    # 信号注册留给主线程 (signal.signal 只能在 main thread)
    sched.start()


def _install_daemon_signals() -> None:
    """daemon 子命令的信号 handler (主线程)"""
    def _shutdown(signum, _frame):
        log.info("收到信号 %s, 关闭 agent", signum)
        sys.exit(0)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


def _listener_lifecycle(
    stop_event: threading.Event,
    *,
    max_restarts: int = 5,
    window_s: int = 300,
    backoff_s: float = 2.0,
) -> None:
    """v12.3: listener 自愈 watchdog — 单次挂掉就 restart, 不让整个 supervisor 死

    设计动机:
      - lark-oapi WebSocket 约 16 分钟断一次 ("no close frame"), reconnect 过程偶尔崩
      - v11/v12.1 老设计: listener 死 → stop_event.set() → 整个 supervisor 退出
      - 结果: 一次断线就让 agent 失联, 必须用户手动 stop+start
      - v12.3 修法: listener 挂掉自动 restart, 限流 (5 分钟内最多 5 次), 仍 fail-safe

    Args:
        stop_event: supervisor 主线程的停止信号 (设了就退出)
        max_restarts: window_s 时间内最多重启几次
        window_s: 限流窗口 (秒)
        backoff_s: 每次 restart 前等几秒
    """
    restart_times: list[float] = []
    restart_n = 0
    log.info("[listener-watchdog] 启动 (max=%d/%ds, backoff=%.1fs)",
             max_restarts, window_s, backoff_s)
    while not stop_event.is_set():
        try:
            log.info("[listener-watchdog] listener.run() 第 %d 次启动", restart_n + 1)
            _listener.run(quiet=False)
            # _listener.run() 正常 return (不是异常退出) — 也视作挂掉
            log.warning("[listener-watchdog] listener.run() 正常 return, 准备 restart")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("[listener-watchdog] listener 崩了: %s", e)

        if stop_event.is_set():
            break

        # 限流: 滚动窗口, 超 max_restarts 就不再重试
        import time as _t
        now = _t.time()
        restart_times = [t for t in restart_times if now - t < window_s]
        restart_times.append(now)
        restart_n = len(restart_times)
        if restart_n > max_restarts:
            log.error("[listener-watchdog] %ds 内崩 %d 次, 放弃 restart, "
                      "让 supervisor 退出 (fail-safe)", window_s, restart_n)
            stop_event.set()
            break
        log.info("[listener-watchdog] %ds 后 restart (第 %d 次, 窗口内累计)",
                 backoff_s, restart_n)
        stop_event.wait(backoff_s)  # 支持中断退出
    log.info("[listener-watchdog] 退出 (共 restart %d 次)", restart_n)


def _run_supervisor() -> None:
    """v12.3: 单进程跑 scheduler + 飞书 ws client (2 个 daemon thread + 1 watchdog)

    主线程: 阻塞等 stop_event, 处理信号
    Thread A: BlockingScheduler 跑 9 个 cron job (7 stage + 2 push)
    Thread B: lark-oapi ws client (由 _listener_lifecycle watchdog 包裹, 崩了自动 restart)
    """
    init_account()
    _write_pid()  # v11: 写 pid 给 agent stop 用
    caught = catch_up_stages()
    if caught:
        log.info("[supervisor] catch-up 已补跑: %s", caught)
    else:
        log.info("[supervisor] catch-up 无需补跑")

    stop_event = threading.Event()
    thread_errors: list[Exception] = []

    def _run_scheduler() -> None:
        try:
            run_daemon(catch_up=False)
        except Exception as e:
            log.exception("scheduler thread 失败: %s", e)
            thread_errors.append(e)
            stop_event.set()

    t_sched = threading.Thread(target=_run_scheduler, name="scheduler", daemon=True)
    t_sched.start()
    # v12.3: listener 走 watchdog 包裹, 单次崩不挂整个 supervisor
    t_listen = threading.Thread(
        target=_listener_lifecycle, args=(stop_event,),
        name="listener-watchdog", daemon=True,
    )
    t_listen.start()
    log.info("[supervisor] scheduler thread 启动: %s", t_sched.name)
    log.info("[supervisor] listener watchdog 启动: %s (崩了自动 restart, 5min 限 5 次)", t_listen.name)
    log.info("[supervisor] 主线程阻塞等信号, Ctrl+C 退出")

    def _shutdown(signum, _frame) -> None:
        log.info("supervisor 收到信号 %s, 关停", signum)
        stop_event.set()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        log.info("supervisor 收到 Ctrl+C")
    finally:
        if thread_errors:
            log.error("supervisor 退出 (有 thread 异常: %d)", len(thread_errors))
        else:
            log.info("supervisor 退出")


def main() -> None:
    parser = argparse.ArgumentParser(description="stock_trading_agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="v11: 单进程启动 scheduler + 飞书监听 (2 worker thread)")
    p_stop = sub.add_parser("stop", help="v11: 停掉 agent start 起的进程 (读 data/agent.pid, 发 SIGTERM)")
    p_daemon = sub.add_parser("daemon", help="常驻进程模式 (仅 scheduler, 不含飞书)")
    p_daemon.add_argument("--catch-up", action="store_true",
                          help="v8.2: 启动时补跑今日已到时间但未跑的 stage")
    p_run_once = sub.add_parser("run-once", help="单次跑某个阶段")
    p_run_once.add_argument("--stage", required=True, choices=list(STAGE_REGISTRY))
    p_webhook = sub.add_parser("webhook", help="启动 HTTP 服务（bot）")
    p_webhook.add_argument("--host", default=os.environ.get("WEBHOOK_HOST", "127.0.0.1"))
    p_webhook.add_argument("--port", type=int, default=int(os.environ.get("WEBHOOK_PORT", "8765")))
    p_listen = sub.add_parser("listen", help="飞书事件订阅（@bot 自动回复, 直接 lark-oapi WebSocket 长连）")
    p_listen.add_argument("--stop-after", type=int, default=None, help="处理 N 条后退出（调试用）")
    p_listen.add_argument("--quiet", action="store_true", help="静默 lark-oapi 自身日志")
    p_report = sub.add_parser("report", help="生成周报 + 多策略回测 + 推飞书")
    p_report.add_argument("--days", type=int, default=30, help="回测窗口")
    p_report.add_argument("--no-push", action="store_true", help="只保存文件, 不推飞书")
    p_report.add_argument("--no-backtest", action="store_true", help="跳过回测对比")
    p_report.add_argument("--pdf", action="store_true", help="v9.1: 同时保存 PDF 版周报")
    # v12: memory 管理 CLI
    p_memory = sub.add_parser("memory", help="v12: 用户记忆 (偏好 + 情景记忆) 管理")
    p_memory.add_argument("action", choices=["list", "clear"], help="list 列出 / clear 清空")
    p_memory.add_argument("--chat-id", default="default", help="chat_id (默认 'default' = 单测/CLI 场景)")
    args = parser.parse_args()

    if args.cmd == "start":
        _run_supervisor()
        return
    if args.cmd == "stop":
        _stop_agent()
        return
    if args.cmd == "memory":
        _handle_memory_cmd(args)
        return
    if args.cmd == "daemon":
        _install_daemon_signals()
        run_daemon(catch_up=getattr(args, "catch_up", False))
    elif args.cmd == "run-once":
        result = run_once(args.stage)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "webhook":
        run_webhook(args.host, args.port)
    elif args.cmd == "listen":
        _listener.run(stop_after=args.stop_after, quiet=args.quiet)
    elif args.cmd == "report":
        from .engine.report import push_weekly_report
        from .engine.reviewer import run_weekly_review, backtest_multi
        from .llm.reasoner import weekly_summary
        weekly = run_weekly_review()
        bt = None if args.no_backtest else backtest_multi(days=args.days)
        try:
            summary = weekly_summary(weekly)
        except Exception:
            summary = ""
        if args.no_push:
            content = render_weekly(weekly, bt, summary) if False else None  # noqa
            from .engine.report import render_weekly as _rw
            content = _rw(weekly, bt, summary)
            print(content)
        else:
            result = push_weekly_report(weekly, bt, summary, save_pdf=args.pdf)
            out = {"saved": result["saved"], "feishu_ok": result["feishu"].get("ok")}
            if result.get("saved_pdf"):
                out["saved_pdf"] = result["saved_pdf"]
            print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
