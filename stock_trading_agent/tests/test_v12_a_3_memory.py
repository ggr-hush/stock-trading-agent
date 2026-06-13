"""test_v12_a_3_memory.py — v12.A.3 memory 4 类分组 单元测试

Covers (改动 2):
  - build_memory_context 按 4 类分组 (4 个)
  - detect_memory_signal guardrail 识别 (1 个)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── build_memory_context 按 4 类分组 (4 个) ───────────

def test_build_memory_context_groups_preference() -> None:
    """偏好类单独成段"""
    from stock_trading_agent.assistant import memory
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(memory, "list_memories") as mock_list:
            mock_list.return_value = [
                {"type": "preference", "content": "不喜欢银行股", "importance": 3,
                 "created_at": "2025-11-01", "ttl_days": 90, "source": "detected"},
            ]
            ctx = memory.build_memory_context("chat1", max_chars=800)
        assert "[偏好]" in ctx
        assert "不喜欢银行股" in ctx
        # 没有事实/决策/卫语句段
        assert "[事实]" not in ctx
        assert "[决策]" not in ctx


def test_build_memory_context_groups_fact() -> None:
    """事实类单独成段"""
    from stock_trading_agent.assistant import memory
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(memory, "list_memories") as mock_list:
            mock_list.return_value = [
                {"type": "fact", "content": "关注 600519", "importance": 2,
                 "created_at": "2025-11-01", "ttl_days": 90, "source": "explicit"},
            ]
            ctx = memory.build_memory_context("chat1", max_chars=800)
        assert "[事实]" in ctx
        assert "关注 600519" in ctx


def test_build_memory_context_groups_decision() -> None:
    """决策类单独成段"""
    from stock_trading_agent.assistant import memory
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(memory, "list_memories") as mock_list:
            mock_list.return_value = [
                {"type": "decision", "content": "刚买 002063", "importance": 3,
                 "created_at": "2025-11-01", "ttl_days": 90, "source": "explicit"},
            ]
            ctx = memory.build_memory_context("chat1", max_chars=800)
        assert "[决策]" in ctx
        assert "刚买 002063" in ctx


def test_build_memory_context_no_memories_returns_empty() -> None:
    """没有 memory 时返空字符串"""
    from stock_trading_agent.assistant import memory
    with patch.object(memory, "list_memories", return_value=[]):
        ctx = memory.build_memory_context("chat1", max_chars=800)
    assert ctx == ""


# ─────────── detect_memory_signal guardrail 识别 (1 个) ───────────

def test_detect_guardrail_signal() -> None:
    """v12.A.3: '记住 [规则] XXX' / '记住 [卫语句] XXX' 识别为 guardrail"""
    from stock_trading_agent.assistant.memory import detect_memory_signal
    # 1) [规则] 前缀
    r1 = detect_memory_signal("记住 [规则] 不准用显然 推仓位")
    assert r1 is not None
    assert r1[0] == "guardrail"
    assert "不准用显然" in r1[1]
    # 2) [卫语句] 前缀
    r2 = detect_memory_signal("记住 [卫语句] 不要在卡片里用 emoji 太多")
    assert r2 is not None
    assert r2[0] == "guardrail"
    # 3) 普通 explicit 不带前缀 → 仍走 explicit (向下兼容)
    r3 = detect_memory_signal("记住 我喜欢半导体")
    assert r3 is not None
    assert r3[0] == "explicit"


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    pass_n = 0
    fail_n = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            pass_n += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
            fail_n += 1
    print(f"\n{'OK' if fail_n == 0 else 'FAIL'} {pass_n}/{pass_n+fail_n} tests passed")
    sys.exit(0 if fail_n == 0 else 1)
