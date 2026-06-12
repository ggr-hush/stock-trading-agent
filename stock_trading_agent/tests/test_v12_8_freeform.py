"""test_v12_8_freeform.py — v12.8 freeform 空响应兜底 + 调参

Covers:
  - LLM 返空 content → 走 fallback_phrases + log.warning
  - 60s 窗口同 chat_id 不重调 LLM
  - log.warning 真的打了
  - temperature=0.7 / max_tokens=800 真的传到 payload
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _clear_empty_cache() -> None:
    from stock_trading_agent.llm.tool_use import _EMPTY_CACHE
    _EMPTY_CACHE.clear()


def test_freeform_empty_content_uses_fallback() -> None:
    """LLM 返 ok=True 但 content="" → 走 fallback_phrases"""
    from stock_trading_agent.llm import tool_use
    _clear_empty_cache()

    with patch.object(tool_use, "chat_with_tools",
                      return_value={"ok": True, "tool_calls": [], "content": "", "latency_ms": 500}), \
         patch("stock_trading_agent.engine.skills.tool_schemas", return_value=[]):
        result = tool_use.dispatch("随便问个啥", chat_id="chat_empty_test")

    assert result["ok"] is True
    assert result["path"] == "llm_freeform_empty"
    text = result["card"]["content"]["text"]
    assert text and "?" in text or "。" in text, f"应该是中文兜底话术, got {text!r}"
    print("  PASS test_freeform_empty_content_uses_fallback")


def test_60s_cache_skips_repeat() -> None:
    """同 chat_id 60s 内重复问 → 第二次不重调 LLM, 直接用缓存"""
    from stock_trading_agent.llm import tool_use
    _clear_empty_cache()

    call_count = {"n": 0}

    def fake_chat(messages, tools=None, **kwargs):
        call_count["n"] += 1
        return {"ok": True, "tool_calls": [], "content": "", "latency_ms": 100}

    with patch.object(tool_use, "chat_with_tools", side_effect=fake_chat), \
         patch("stock_trading_agent.engine.skills.tool_schemas", return_value=[]):
        r1 = tool_use.dispatch("x", chat_id="chat_60s")
        r2 = tool_use.dispatch("x", chat_id="chat_60s")
        r3 = tool_use.dispatch("x", chat_id="chat_60s")

    assert call_count["n"] == 1, f"应只调 1 次 LLM, 实际 {call_count['n']} 次"
    # 3 次返回的 fallback text 应该一样
    t1 = r1["card"]["content"]["text"]
    t3 = r3["card"]["content"]["text"]
    assert t1 == t3, "同 chat_id 60s 内应返相同兜底话术"
    print("  PASS test_60s_cache_skips_repeat")


def test_warning_log_emitted() -> None:
    """空响应时应打 log.warning"""
    from stock_trading_agent.llm import tool_use
    _clear_empty_cache()

    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    h = _ListHandler()
    tool_use.log.addHandler(h)
    tool_use.log.setLevel(logging.WARNING)

    try:
        with patch.object(tool_use, "chat_with_tools",
                          return_value={"ok": True, "tool_calls": [], "content": "", "latency_ms": 200}), \
             patch("stock_trading_agent.engine.skills.tool_schemas", return_value=[]):
            tool_use.dispatch("hello", chat_id="chat_warn")
    finally:
        tool_use.log.removeHandler(h)

    warns = [r for r in captured if r.levelno == logging.WARNING]
    assert any("freeform 空响应" in r.getMessage() for r in warns), (
        f"应打 'freeform 空响应' warning, 实际: {[r.getMessage() for r in warns]}"
    )
    print("  PASS test_warning_log_emitted")


def test_temperature_and_max_tokens_from_config() -> None:
    """dispatch() 调的 chat_with_tools 应带上 config 里的 temperature / max_tokens"""
    from stock_trading_agent.llm import tool_use

    captured_kwargs: dict = {}

    def fake_chat(messages, tools=None, **kwargs):
        captured_kwargs.update(kwargs)
        return {"ok": True, "tool_calls": [{"name": "get_picks", "args": {}}], "content": "", "latency_ms": 100}

    # 强制 config 走 paper_trader 的 load_config
    from stock_trading_agent.engine.paper_trader import get_db
    with patch.object(tool_use, "chat_with_tools", side_effect=fake_chat), \
         patch("stock_trading_agent.engine.skills.tool_schemas", return_value=[]), \
         patch("stock_trading_agent.llm.client._get_config",
                      return_value={"temperature": 0.7, "max_tokens": 800}):
        # 走 tool 路径, 不走 freeform 路径
        with patch("stock_trading_agent.engine.skills.call_skill",
                   return_value={"ok": True, "card": {"msg_type": "text", "content": {"text": "ok"}},
                                 "raw": {}}):
            try:
                tool_use.dispatch("今日选股", chat_id="chat_tt")
            except Exception:
                pass

    assert captured_kwargs.get("temperature") == 0.7, f"temp 应为 0.7, got {captured_kwargs}"
    assert captured_kwargs.get("max_tokens") == 800, f"max_tokens 应为 800, got {captured_kwargs}"
    print("  PASS test_temperature_and_max_tokens_from_config")


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
