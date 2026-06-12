"""test_v12_8_1_supervisor.py — v12.8.1 修 v12.5.2 自重启设计

Covers:
  - _listener_lifecycle: 正常 return → sleep 5s 重连 (不调 _self_exec_restart)
  - _listener_lifecycle: stop_event.set() 后立即退出
  - _listener_lifecycle: 异常 → 仍走 _self_exec_restart (1h 限 10 次)
  - stage 失败时 mark_stage_run(stage, ok=False) 被调用
  - stage 成功时 mark_stage_run(stage, ok=True) 被调用
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_listener_normal_return_sleeps_5s_no_execv() -> None:
    """listener 正常 return → sleep 5s → 再起, 不调 _self_exec_restart"""
    from stock_trading_agent.agent import supervisor

    stop_event = MagicMock()
    # 让 stop_event.is_set() 第一次返 False, 第二次返 True (退出循环)
    stop_event.is_set.side_effect = [False, False, True]  # 第1次: while 入口; 第2次: 跑完 listener 后; 第3次: break

    call_count = {"n": 0}
    sleep_calls: list[float] = []
    execv_called = {"v": False}

    def fake_listener_run(quiet=False):
        call_count["n"] += 1
        # 第一次正常 return, 第二次也 return (但 stop_event 触发 break)

    def fake_sleep(s):
        sleep_calls.append(s)

    def fake_execv(*a, **kw):
        execv_called["v"] = True
        raise SystemExit(0)  # 防呆

    with patch.object(supervisor._listener, "run", side_effect=fake_listener_run), \
         patch("time.sleep", side_effect=fake_sleep), \
         patch.object(supervisor, "_self_exec_restart", side_effect=fake_execv):
        supervisor._listener_lifecycle(stop_event)

    assert call_count["n"] >= 1, f"listener.run 应至少被调 1 次, 实际 {call_count['n']}"
    assert 5.0 in sleep_calls, f"应有 sleep(5), 实际 {sleep_calls}"
    assert execv_called["v"] is False, "正常 return 不应触发 _self_exec_restart"
    print("  PASS test_listener_normal_return_sleeps_5s_no_execv")


def test_listener_exception_triggers_execv() -> None:
    """listener 抛异常 → _self_exec_restart 仍被调 (防 bug 死循环)"""
    from stock_trading_agent.agent import supervisor

    stop_event = MagicMock()
    stop_event.is_set.return_value = False

    def fake_listener_run(quiet=False):
        raise RuntimeError("listener 炸了")

    execv_args: list[tuple] = []

    def fake_execv(*a, **kw):
        execv_args.append((a, kw))
        raise SystemExit(0)

    with patch.object(supervisor._listener, "run", side_effect=fake_listener_run), \
         patch("time.sleep", return_value=None), \
         patch.object(supervisor, "_self_exec_restart", side_effect=fake_execv):
        try:
            supervisor._listener_lifecycle(stop_event)
        except SystemExit:
            pass

    assert len(execv_args) >= 1, f"异常时应调 _self_exec_restart, 实际 0 次"
    # 第二次 execv 会因超过 1h 限 10 次而 sys.exit(1), 跳出循环
    print("  PASS test_listener_exception_triggers_execv")


def test_stage_failure_writes_stage_runs_ok_false() -> None:
    """stage 抛异常 → mark_stage_run(stage, ok=False) 被调"""
    from stock_trading_agent.agent import stages

    with patch.object(stages, "mark_stage_run") as mock_mark, \
         patch.object(stages, "is_trading_day", return_value=True), \
         patch.object(stages, "load_config", side_effect=RuntimeError("data_fetcher 炸了")):
        result = stages.stage_pick()

    assert result.get("ok") is False
    assert result.get("stage") == "pick"
    assert "RuntimeError" in result.get("error", "")
    # 关键: mark_stage_run("pick", ok=False) 被调
    mock_mark.assert_called_with("pick", ok=False)
    print("  PASS test_stage_failure_writes_stage_runs_ok_false")


def test_stage_success_writes_stage_runs_ok_true() -> None:
    """stage 正常跑完 → mark_stage_run(stage, ok=True) 被调"""
    from stock_trading_agent.agent import stages

    with patch.object(stages, "mark_stage_run") as mock_mark, \
         patch.object(stages, "is_trading_day", return_value=False):
        result = stages.stage_pick()

    # 非交易日返回 {"skipped": "weekend"}, 仍算成功
    assert result.get("skipped") == "weekend"
    mock_mark.assert_called_with("pick", ok=True)
    print("  PASS test_stage_success_writes_stage_runs_ok_true")


def test_stage_decorator_does_not_swallow_return_value() -> None:
    """装饰器不破坏原 stage 返回值"""
    from stock_trading_agent.agent import stages

    with patch.object(stages, "mark_stage_run"), \
         patch.object(stages, "is_trading_day", return_value=False):
        r1 = stages.stage_pre_market()
        r2 = stages.stage_open_auction()
        r3 = stages.stage_pick()

    # 非交易日 stage_pre_market 返 {"skipped": "weekend"}
    assert r1 == {"skipped": "weekend"}
    # stage_open_auction 返 {"open_count": 0, "opens": []}
    assert r2 == {"open_count": 0, "opens": []}
    # stage_pick 同 pre_market
    assert r3 == {"skipped": "weekend"}
    print("  PASS test_stage_decorator_does_not_swallow_return_value")


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            fail += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            fail += 1
    print(f"\n{'✓' if fail == 0 else '✗'} {len(tests) - fail}/{len(tests)} tests passed")
    sys.exit(0 if fail == 0 else 1)
