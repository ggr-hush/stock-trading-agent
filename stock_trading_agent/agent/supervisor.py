"""agent/supervisor.py — v12.6 单进程 supervisor

v12.5.2 自重启 + v11 单进程 + v12.3 watchdog 整合。

职责:
  - PID 文件读写 (`_check_already_running` / `_stop_agent` / `_write_pid`)
  - 启动顺序: init → catch-up → scheduler thread + listener watchdog thread
  - v12.5.2: listener 跑完 → `_self_exec_restart` 整进程 os.execv
  - SIGTERM/SIGINT 优雅退出
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
import threading
from pathlib import Path
from typing import NoReturn

from ..engine.paper_trader import init_account
from ..feishu import listener as _listener
from .stages import catch_up_stages, run_daemon

log = logging.getLogger("agent.supervisor")

PID_FILE = Path("data/agent.pid")

# v12.5.2: 自重启计数器 (跨 exec 持久化)
AUTO_RESTART_COUNT_FILE = Path("data/.auto_restart_count")
AUTO_RESTART_WINDOW_S = 3600
AUTO_RESTART_MAX = 10

# v12.5.2: 自重启执行器, 默认是 os.execv, 测试可注入
_restart_executor = os.execv


# ─────────── PID 文件 ───────────

def _write_pid() -> None:
    """写 supervisor 主线程 PID 到 data/agent.pid"""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    log.info("supervisor PID %d 写入 %s", os.getpid(), PID_FILE)


def _check_already_running() -> None:
    """v12.5: 启动前检查 data/agent.pid, 已存在且进程活着 -> 拒绝启动

    失败情形 (pid 文件存在但进程已死): 视为 stale, 删了重起
    """
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        log.warning("[start] pid 文件损坏, 删除重起: %s", PID_FILE)
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return
    try:
        os.kill(old_pid, 0)
    except ProcessLookupError:
        log.warning("[start] pid 文件存在但进程 %d 已死, 删除重起", old_pid)
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return
    except PermissionError:
        log.warning("[start] pid %d 存在但不属于本用户, 仍尝试启动", old_pid)
        return
    log.error("[start] agent 已在运行 (pid=%d), 请先 `agent stop` 或删 %s",
              old_pid, PID_FILE)
    print(f"\n  X agent 已在运行 (pid={old_pid})")
    print(f"     停止: PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent stop")
    print(f"     强杀: rm -f {PID_FILE}  # 确认进程真死了再删")
    sys.exit(1)


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
    import time
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"  ✓ agent stopped (pid={pid})")
            PID_FILE.unlink(missing_ok=True)
            return
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"  ⚠ agent didn't stop gracefully, SIGKILL'd (pid={pid})")
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)


# ─────────── 自重启 (v12.5.2) ───────────

def _read_auto_restart_log() -> list[float]:
    """读 1h 内的自重启时间戳列表, 自动清过期"""
    if not AUTO_RESTART_COUNT_FILE.exists():
        return []
    try:
        import time as _t
        now = _t.time()
        cutoff = now - AUTO_RESTART_WINDOW_S
        lines = AUTO_RESTART_COUNT_FILE.read_text(encoding="utf-8").strip().splitlines()
        ts: list[float] = []
        for ln in lines:
            try:
                t = float(ln.strip())
                if t > cutoff:
                    ts.append(t)
            except ValueError:
                continue
        return ts
    except Exception:
        return []


def _check_auto_restart_budget() -> tuple[bool, int]:
    """检查自重启预算: (是否还有预算, 窗口内已用几次)"""
    used = _read_auto_restart_log()
    return (len(used) < AUTO_RESTART_MAX, len(used))


def _record_auto_restart() -> None:
    """记一次自重启 (append 当前时间戳到文件)"""
    import time as _t
    try:
        AUTO_RESTART_COUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUTO_RESTART_COUNT_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{_t.time():.3f}\n")
    except Exception as e:  # noqa: BLE001
        log.warning("写自重启计数器失败 (忽略): %s", e)


def _self_exec_restart(reason: str) -> NoReturn:
    """v12.5.2: supervisor 用 os.execv 重启自己 (不依赖外层 launchctl)

    1h 限 10 次防止代码 bug 导致无限重启打爆日志。
    """
    has_budget, used = _check_auto_restart_budget()
    if not has_budget:
        log.error("[self-restart] 1h 内已自重启 %d 次 (>= 上限 %d), 不再重启, "
                  "退出 (用户需 `agent stop && agent start` 手动恢复)",
                  used, AUTO_RESTART_MAX)
        sys.exit(1)
    _record_auto_restart()
    log.warning("[self-restart] %s -> 1h 内第 %d 次自重启 (上限 %d), "
                "execv 同一进程启动", reason, used + 1, AUTO_RESTART_MAX)
    _restart_executor(sys.executable,
                      [sys.executable, "-m", "stock_trading_agent.agent"] + sys.argv[1:])


# ─────────── listener watchdog ───────────

def _listener_lifecycle(stop_event: threading.Event) -> None:
    """v12.8.1: listener watchdog — 正常 return 走 sleep 5s 重连, 异常才 execv 自重启

    v12.5.2 设计: listener 跑完一次就 os.execv 整进程自重启, 1h 限 10 次。
    问题是飞书 WebSocket 每 ~16min 一次 keepalive ping 超时是 SDK 正常生命周期,
    每次都重启导致:
      - cron job 状态丢失, 14:00 pick 错过
      - 自重启计数器容易耗尽 → supervisor 死 → 6/11 23:23 之后 24h 没 stage
      - 数据迁移/记账半中间态

    v12.8.1 新设计:
      - 正常 return (ws 断线) → sleep 5s → 再起 listener.run()  (跟 v12.3 一样, 但 v12.5.1 已修 5xx 重投, 不会叠加)
      - 异常 (代码 bug) → 3s 后 execv 重启 supervisor, 1h 限 10 次 (防 bug 死循环)
    """
    log.info("[listener-watchdog] 启动 (v12.8.1: 正常 return sleep 重连, 异常 execv)")
    while not stop_event.is_set():
        try:
            log.info("[listener-watchdog] listener.run() 启动")
            _listener.run(quiet=False)
            # 正常 return (ws 16min 断线/网络抖动) → sleep 重连
            if stop_event.is_set():
                break
            log.info("[listener-watchdog] listener 正常 return, 5s 后重连 (ws 生命周期)")
            time.sleep(5)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("[listener-watchdog] listener 崩了: %s, 3s 后 execv 自重启", e)
            time.sleep(3)
            _self_exec_restart(f"listener 异常: {type(e).__name__}: {str(e)[:80]}")


# ─────────── supervisor 主循环 ───────────

def _run_supervisor() -> None:
    """v12.3: 单进程跑 scheduler + 飞书 ws client (2 个 daemon thread + 1 watchdog)

    主线程: 阻塞等 stop_event, 处理信号
    Thread A: BlockingScheduler 跑 9 个 cron job (7 stage + 2 push)
    Thread B: lark-oapi ws client (由 _listener_lifecycle watchdog 包裹, 崩了自动 restart)
    """
    init_account()
    _write_pid()
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
    t_listen = threading.Thread(
        target=_listener_lifecycle, args=(stop_event,),
        name="listener-watchdog", daemon=True,
    )
    t_listen.start()
    log.info("[supervisor] scheduler thread 启动: %s", t_sched.name)
    log.info("[supervisor] listener watchdog 启动: %s (v12.5.2 自重启模式, 1h 限 10 次)", t_listen.name)
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
