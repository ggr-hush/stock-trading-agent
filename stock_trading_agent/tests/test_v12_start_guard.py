"""test_v12_start_guard.py — v12.5 start 防多进程

覆盖:
  - pid 文件不存在: 正常通过
  - pid 文件存在 + 进程活着: 拒绝启动 (SystemExit 1)
  - pid 文件存在 + 进程已死 (stale): 删 pid 后正常通过
  - pid 文件损坏: 删 pid 后正常通过
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _spawn_sleep_for(seconds: int = 30) -> int:
    """起一个会睡 N 秒的真实子进程, 返回 PID (用来模拟'agent 已在跑')"""
    p = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return p.pid


def test_start_no_pid_file_passes() -> None:
    """无 pid 文件: _check_already_running 不抛, 直接 return"""
    from stock_trading_agent.agent import _check_already_running, PID_FILE
    if PID_FILE.exists():
        PID_FILE.unlink()
    _check_already_running()  # 不应抛
    print("  PASS test_start_no_pid_file_passes")


def test_start_alive_pid_rejects() -> None:
    """有 pid 文件 + 进程活着: 应该 SystemExit 1"""
    from stock_trading_agent.agent import _check_already_running, PID_FILE
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 起一个会睡 30 秒的子进程
    child_pid = _spawn_sleep_for(30)
    try:
        PID_FILE.write_text(str(child_pid))
        try:
            _check_already_running()
        except SystemExit as e:
            assert e.code == 1, f"expected exit code 1, got {e.code}"
            print("  PASS test_start_alive_pid_rejects (SystemExit(1))")
            return
        raise AssertionError("expected SystemExit when another agent is running")
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass
        if PID_FILE.exists():
            PID_FILE.unlink()


def test_start_stale_pid_cleans_up() -> None:
    """有 pid 文件 + 进程已死 (stale): 应该清掉 pid 文件, 不抛"""
    from stock_trading_agent.agent import _check_already_running, PID_FILE
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 用一个不存在的 pid (大于当前最大 pid 1 亿一定不存在)
    fake_pid = 999_999_999
    PID_FILE.write_text(str(fake_pid))
    _check_already_running()  # 不应抛
    assert not PID_FILE.exists(), f"stale pid file should be removed, still: {PID_FILE}"
    print("  PASS test_start_stale_pid_cleans_up")


def test_start_corrupted_pid_cleans_up() -> None:
    """pid 文件内容损坏: 应该清掉, 不抛"""
    from stock_trading_agent.agent import _check_already_running, PID_FILE
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text("not-a-number\n")
    _check_already_running()  # 不应抛
    assert not PID_FILE.exists(), f"corrupted pid file should be removed, still: {PID_FILE}"
    print("  PASS test_start_corrupted_pid_cleans_up")


def main() -> int:
    tests = [
        test_start_no_pid_file_passes,
        test_start_alive_pid_rejects,
        test_start_stale_pid_cleans_up,
        test_start_corrupted_pid_cleans_up,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    total = len(tests)
    print(f"\n  {total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
