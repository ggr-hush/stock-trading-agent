"""test_v12_5_1_dedup.py — v12.5.1 飞书消息去重 + 大盘兜底

Covers:
  - _mark_seen: 同 message_id 第二次返 False (去重)
  - _mark_seen: 不同 message_id 各返 True (不误伤)
  - _mark_seen: TTL 过期后能重新处理 (monkeypatch time)
  - _mark_seen: LRU 软上限, 满了丢最旧
  - _run_get_market_env: picks 有数据 -> source=picks
  - _run_get_market_env: picks 空 + 实时拉 -> source=realtime
  - _run_get_market_env: picks 空 + 实时拉抛 -> source=failed
  - _render_market_env_card: 三种 source 出对应 emoji
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── _mark_seen 去重 ───────────

def test_mark_seen_first_time_returns_true() -> None:
    from stock_trading_agent.feishu.listener import _mark_seen, _seen_msgs
    _seen_msgs.clear()
    assert _mark_seen("msg_001") is True
    print("  PASS test_mark_seen_first_time_returns_true")


def test_mark_seen_duplicate_returns_false() -> None:
    from stock_trading_agent.feishu.listener import _mark_seen, _seen_msgs
    _seen_msgs.clear()
    assert _mark_seen("msg_002") is True
    assert _mark_seen("msg_002") is False
    assert _mark_seen("msg_002") is False
    print("  PASS test_mark_seen_duplicate_returns_false")


def test_mark_seen_distinct_msg_ids_both_true() -> None:
    from stock_trading_agent.feishu.listener import _mark_seen, _seen_msgs
    _seen_msgs.clear()
    assert _mark_seen("msg_A") is True
    assert _mark_seen("msg_B") is True
    assert _mark_seen("msg_C") is True
    assert len(_seen_msgs) == 3
    print("  PASS test_mark_seen_distinct_msg_ids_both_true")


def test_mark_seen_ttl_expires() -> None:
    """注入 fake time, 让所有 message 都过期 -> 应重新接受"""
    from stock_trading_agent.feishu import listener
    listener._seen_msgs.clear()
    fake_now = [1_000_000.0]
    real_time = listener.time.time

    def fake_time():
        return fake_now[0]
    with patch.object(listener.time, "time", fake_time):
        assert listener._mark_seen("msg_ttl_1") is True
        # 跳到 TTL 边界外
        fake_now[0] += listener._DEDUP_TTL_S + 1
        # 此时 _mark_seen 会先做清理 (if len > MAX: 清过期) 但 len=1 < MAX, 不会清
        # 所以"过期但未触发清理" 仍会返 False -- 这是预期 (LRU 懒清理)
        # 真实场景: 新消息涌入到 > _DEDUP_MAX 时会清理, 或者下次超 MAX
        # 这里直接验证 _mark_seen 返 False (缓存没自动清, 行为正确)
        assert listener._mark_seen("msg_ttl_1") is False
    # 强插第 _DEDUP_MAX+1 条触发清理
    fake_now[0] += listener._DEDUP_TTL_S + 10
    for i in range(listener._DEDUP_MAX + 5):
        with patch.object(listener.time, "time", fake_time):
            listener._mark_seen(f"flood_{i}")
    # 此时原 msg_ttl_1 已过期被清, 重新接受
    with patch.object(listener.time, "time", fake_time):
        assert listener._mark_seen("msg_ttl_1") is True, "TTL 过期后应能重新接受"
    listener._seen_msgs.clear()
    print("  PASS test_mark_seen_ttl_expires")


def test_mark_seen_lru_evicts_oldest() -> None:
    """超 _DEDUP_MAX 时, 最旧应被踢出"""
    from stock_trading_agent.feishu import listener
    listener._seen_msgs.clear()
    fake_now = [2_000_000.0]

    def fake_time():
        return fake_now[0]

    with patch.object(listener.time, "time", fake_time):
        # 灌满
        for i in range(listener._DEDUP_MAX + 10):
            listener._mark_seen(f"lru_{i}")
        # 此时最旧的 lru_0 ~ lru_9 已被踢
        assert "lru_0" not in listener._seen_msgs
        assert f"lru_{listener._DEDUP_MAX + 9}" in listener._seen_msgs
        assert len(listener._seen_msgs) <= listener._DEDUP_MAX
    listener._seen_msgs.clear()
    print("  PASS test_mark_seen_lru_evicts_oldest")


def test_mark_seen_thread_safe() -> None:
    """并发跑 N 个线程, 同一个 message_id 应只一个返 True"""
    import threading
    from stock_trading_agent.feishu import listener
    listener._seen_msgs.clear()

    results: list[bool] = []
    lock = threading.Lock()

    def worker(mid: str) -> None:
        r = listener._mark_seen(mid)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=("race_msg",)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1, f"应只 1 个 True, 实际 {results.count(True)}"
    assert results.count(False) == 19
    listener._seen_msgs.clear()
    print("  PASS test_mark_seen_thread_safe")


# ─────────── _run_get_market_env 三分支 ───────────

def test_market_env_picks_path() -> None:
    from stock_trading_agent.engine import skills
    real_row = MagicMock()
    real_row.__getitem__.side_effect = lambda k: {
        "market_env_score": 72, "market_env_level": "偏多", "pick_date": "2026-06-10",
    }[k]
    real_row.keys.return_value = ["market_env_score", "market_env_level", "pick_date"]
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = real_row
    with patch.object(skills, "get_db", return_value=fake_conn):
        r = skills._run_get_market_env({})
    assert r["source"] == "picks"
    assert r["env_score"] == 72
    assert r["env_level"] == "偏多"
    print("  PASS test_market_env_picks_path")


def test_market_env_realtime_path() -> None:
    from stock_trading_agent.engine import skills, data_fetcher
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None
    with patch.object(skills, "get_db", return_value=fake_conn), \
         patch.object(data_fetcher, "curl_get", return_value=""):
        r = skills._run_get_market_env({})
    assert r["source"] == "realtime", f"应走 realtime, 实际 {r}"
    # env_fetcher 在没数据时返 env_score=50
    assert r["env_score"] is not None
    print("  PASS test_market_env_realtime_path")


def test_market_env_failed_path() -> None:
    """v12.A.4.c: get_market_env 抛异常 → source=failed"""
    from stock_trading_agent.engine import skills
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None
    with patch.object(skills, "get_db", return_value=fake_conn), \
         patch("stock_trading_agent.engine.data_fetcher.get_market_env",
               side_effect=ConnectionError("net down")):
        r = skills._run_get_market_env({})
    assert r["source"] == "failed"
    assert r["env_level"] == "数据源不可用"
    print("  PASS test_market_env_failed_path")


# ─────────── _render_market_env_card 三种 source ───────────

def test_render_market_env_card_three_sources() -> None:
    from stock_trading_agent.engine.skills import _render_market_env_card
    for src, expected_emoji in [
        ("picks", "📊"),
        ("realtime", "⚡"),
        ("failed", "⚠"),
    ]:
        r = {"env_score": 65, "env_level": "偏多", "date": "2026-06-11", "source": src}
        card = _render_market_env_card(r)
        assert card["content"]["text"].startswith(expected_emoji), \
            f"source={src} 期望首字符 {expected_emoji}, 实际: {card['content']['text'][:30]}"
    # 失败 + score None -> 卡片里 score 显示 ?
    r = {"env_score": None, "env_level": "数据源不可用", "date": "2026-06-11", "source": "failed"}
    card = _render_market_env_card(r)
    assert "env_score: ?" in card["content"]["text"]
    assert "数据源不可用" in card["content"]["text"]
    print("  PASS test_render_market_env_card_three_sources")


# ─────────── 集成: on_message 同 message_id 第二次跳过 ───────────

def test_on_message_dedup_skips_second_call() -> None:
    """直接调 _make_handler 注册的 on_message, mock _send_card 计数"""
    from unittest.mock import MagicMock
    from stock_trading_agent.feishu import listener
    listener._seen_msgs.clear()

    fake_client = MagicMock()
    fake_client.im.v1.message.create.return_value = MagicMock(
        success=MagicMock(return_value=True),
        data=MagicMock(message_id="m_out_1"),
    )
    captured: dict = {"send_count": 0}

    def fake_send_card(client, chat_id, text, msg_type="text"):
        captured["send_count"] += 1
        captured.setdefault("texts", []).append(text)
        return {"ok": True, "message_id": "m_out_x"}

    fake_get_config = lambda: {}

    with patch.object(listener, "_send_card", fake_send_card), \
         patch("stock_trading_agent.engine.paper_trader.init_account", lambda: None), \
         patch("stock_trading_agent.llm.tool_use.dispatch") as mock_dispatch, \
         patch("stock_trading_agent.assistant.memory.detect_memory_signal", return_value=None), \
         patch("stock_trading_agent.engine.sessions.append_turn", lambda *a, **k: None):
        mock_dispatch.return_value = {
            "ok": True, "path": "llm_freeform",
            "card": {"msg_type": "text", "content": {"text": "hi"}},
            "tool_calls": [],
        }
        on_message = listener._make_handler(fake_client, fake_get_config)
        ev1 = MagicMock()
        ev1.event.message = MagicMock(
            message_type="text", chat_id="oc_test_chat", message_id="msg_integration_dup",
            content='{"text":"hello"}', mentions=[],
        )
        ev1.event.sender = MagicMock()
        ev1.event.sender.sender_id = MagicMock(open_id="ou_x", user_id="", union_id="")
        # 同 message_id 调 2 次
        on_message(ev1)
        on_message(ev1)

    # dispatch 应只被调 1 次
    assert mock_dispatch.call_count == 1, f"dispatch 应只 1 次, 实际 {mock_dispatch.call_count}"
    assert captured["send_count"] == 1, f"_send_card 应只 1 次, 实际 {captured['send_count']}"
    listener._seen_msgs.clear()
    print("  PASS test_on_message_dedup_skips_second_call")


# ─────────── 入口 ───────────

def main() -> int:
    tests = [
        test_mark_seen_first_time_returns_true,
        test_mark_seen_duplicate_returns_false,
        test_mark_seen_distinct_msg_ids_both_true,
        test_mark_seen_ttl_expires,
        test_mark_seen_lru_evicts_oldest,
        test_mark_seen_thread_safe,
        test_market_env_picks_path,
        test_market_env_realtime_path,
        test_market_env_failed_path,
        test_render_market_env_card_three_sources,
        test_on_message_dedup_skips_second_call,
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
