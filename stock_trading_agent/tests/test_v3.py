"""
test_v3.py — v3 四个新功能的测试
1) RAG synonym 扩展确实提升分数
2) Bot 多轮 session: 创建/历史/reset/TTL
3) 多策略回测: 接口存在 + 无 fixtures 时优雅降级
4) 飞书 listener: mention 解析 + event dispatch (mocked lark-cli)
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import stock_trading_agent.engine.knowledge as kmod
import stock_trading_agent.engine.paper_trader as pt
import stock_trading_agent.engine.sessions as sess
import stock_trading_agent.feishu.listener as listener
from stock_trading_agent.agent import _WebhookHandler
from stock_trading_agent.engine import reviewer


# ─────────── RAG synonym 扩展 ───────────

def test_synonym_expand_improves_score() -> None:
    """'题材龙头' 加同义词后分数应提升"""
    kmod.reset_index()
    r_no = kmod.retrieve("题材龙头", k=1, expand=False)
    r_yes = kmod.retrieve("题材龙头", k=1, expand=True)
    assert r_no and r_yes
    assert r_yes[0]["score"] > r_no[0]["score"], \
        f"同义词扩展应提升分数: {r_no[0]['score']} -> {r_yes[0]['score']}"
    print(f"  ✓ test_synonym_expand_improves_score: {r_no[0]['score']} → {r_yes[0]['score']}")


def test_expand_query_adds_synonyms() -> None:
    """expand_query 应在原文基础上加同义词"""
    q = kmod.expand_query("题材龙头")
    assert "主线" in q or "热点" in q, f"应包含同义词: {q}"
    print(f"  ✓ test_expand_query_adds_synonyms: '{q[:60]}'")


# ─────────── Bot session ───────────

def _isolated_sessions(name: str):
    d = Path(tempfile.mkdtemp(prefix=f"sta_test_sess_{name}_"))
    pt.DB_PATH = d / "quant.db"
    pt.DATA_DIR = d
    return d


def test_session_create_get() -> None:
    d = _isolated_sessions("create")
    pt.init_account()
    s = sess._get_or_create("alice")
    assert s["history"] == []
    assert s["turn_count"] == 0
    h = sess.get_history("alice")
    assert h == []
    print(f"  ✓ test_session_create_get: 新 session 空历史 OK")


def test_session_append_and_history() -> None:
    d = _isolated_sessions("append")
    pt.init_account()
    sess.append_turn("alice", "user", "今天怎么样")
    sess.append_turn("alice", "assistant", "行情中性")
    h = sess.get_history("alice")
    assert len(h) == 2
    assert h[0]["role"] == "user" and h[0]["content"] == "今天怎么样"
    assert h[1]["role"] == "assistant" and h[1]["content"] == "行情中性"
    print(f"  ✓ test_session_append_and_history: 2 轮历史写入正确")


def test_session_reset() -> None:
    d = _isolated_sessions("reset")
    pt.init_account()
    sess.append_turn("alice", "user", "hi")
    assert len(sess.get_history("alice")) == 1
    sess.reset("alice")
    assert len(sess.get_history("alice")) == 0
    print(f"  ✓ test_session_reset: 清空 OK")


def test_session_ttl() -> None:
    """TTL 过期后应自动清空"""
    d = _isolated_sessions("ttl")
    pt.init_account()
    sess.append_turn("alice", "user", "hi")
    # 手动改 last_active 为 25h 之前
    conn = pt.get_db()
    past = (datetime.now() - timedelta(hours=25)).isoformat()
    conn.execute("UPDATE bot_sessions SET last_active=? WHERE session_id=?", (past, "alice"))
    conn.commit()
    # 触发 _get_or_create 检查 TTL
    h = sess.get_history("alice")
    assert h == [], f"TTL 过期应清空, got {h}"
    print(f"  ✓ test_session_ttl: 25h 前数据自动清空")


def test_session_trim() -> None:
    """历史超过 MAX_HISTORY_TURNS * 2 应被截断"""
    d = _isolated_sessions("trim")
    pt.init_account()
    # 加 50 轮 (100 条)
    for i in range(50):
        sess.append_turn("alice", "user", f"q{i}")
        sess.append_turn("alice", "assistant", f"a{i}")
    h = sess.get_history("alice")
    assert len(h) <= sess.MAX_HISTORY_TURNS * 2, f"应 ≤ {sess.MAX_HISTORY_TURNS*2}, got {len(h)}"
    # 最新 2 条应该是 q49, a49
    assert h[-1]["content"] == "a49"
    print(f"  ✓ test_session_trim: 50 轮 → {len(h)} 条 (上限 {sess.MAX_HISTORY_TURNS*2})")


# ─────────── 多策略回测 ───────────

def test_backtest_multi_no_fixtures() -> None:
    """无 fixtures 时返回 error (不崩)"""
    # 临时指向空目录, 避免被 v5 真实 fixtures 影响
    import stock_trading_agent.engine.reviewer as rev
    orig = rev.FIXTURES_DIR
    rev.FIXTURES_DIR = Path(tempfile.mkdtemp(prefix="sta_test_empty_"))
    try:
        r = reviewer.backtest_multi()
        assert "error" in r
        print(f"  ✓ test_backtest_multi_no_fixtures: 优雅降级 → {r['error']}")
    finally:
        rev.FIXTURES_DIR = orig


def test_backtest_multi_with_fixtures() -> None:
    """造 3 个 fixture 跑 backtest_multi"""
    fixtures = Path(tempfile.mkdtemp(prefix="sta_test_fixtures_"))
    for i, (date, plan) in enumerate([
        ("2026-05-15", "A"), ("2026-05-16", "B"), ("2026-05-17", "C"),
    ]):
        stocks = []
        if plan == "A":
            stocks = [
                {"code": f"c{i:03d}001", "name": f"a{i}", "price": 10.0, "score": 75.0, "sector": "好"},
            ]
        elif plan == "B":
            stocks = [
                {"code": f"c{i:03d}002", "name": f"b{i}", "price": 20.0, "score": 70.0, "sector": "好"},
            ]
        data = {
            "date": date, "plan": plan,
            "market_env": {"position_ratio": 0.5 if plan != "C" else 0.0},
            "filtered_stocks": stocks,
            "next_noon_prices": {s["code"]: s["price"] * 1.02 for s in stocks},
        }
        (fixtures / f"pick_{date.replace('-', '')}.json").write_text(json.dumps(data, ensure_ascii=False))
    # Patch FIXTURES_DIR
    import stock_trading_agent.engine.reviewer as rev
    orig = rev.FIXTURES_DIR
    rev.FIXTURES_DIR = fixtures
    try:
        r = reviewer.backtest_multi(days=10)
        assert r["days"] == 3
        assert "fixed_A" in r and "fixed_B" in r and "auto" in r
        assert "recommendation" in r
        print(f"  ✓ test_backtest_multi_with_fixtures: 3 个 fixture, 推荐={r['recommendation']}")
    finally:
        rev.FIXTURES_DIR = orig


# ─────────── 飞书 listener ───────────

def test_strip_mention() -> None:
    assert listener._strip_mention("@_user_1 hello") == "hello"
    assert listener._strip_mention("@_user_1 @_user_2  今天") == "今天"
    assert listener._strip_mention("no mention") == "no mention"
    print(f"  ✓ test_strip_mention: @_user_1 已剥离")


def test_extract_text() -> None:
    assert listener._extract_text('{"text": "hello"}', "text") == "hello"
    assert listener._extract_text("plain", "text") == "plain"
    assert listener._extract_text("img", "image") == ""  # 非 text
    print(f"  ✓ test_extract_text: 正确解析 text/post")


def test_handle_event_skipped() -> None:
    """非 message event / 非 text type 应跳过"""
    r1 = listener._handle_event({"type": "other"}, "/fake")
    assert "skipped" in r1
    r2 = listener._handle_event({"type": "im.message.receive_v1", "message_type": "image"}, "/fake")
    assert "skipped" in r2
    print(f"  ✓ test_handle_event_skipped: 正确跳过非 text event")


def test_handle_event_full() -> None:
    """完整 event: 提取 + 调 chat + 调 send_reply (mocked)"""
    d = _isolated_sessions("listener")
    pt.init_account()
    evt = {
        "type": "im.message.receive_v1",
        "chat_id": "oc_test_listener",
        "message_id": "om_test_listener",
        "message_type": "text",
        "content": json.dumps({"text": "@_user_1 什么是一夜持股法"}),
    }
    listener._send_reply = lambda mid, txt, cli: {"ok": True, "mocked": True, "text_preview": txt[:50]}
    os.environ.pop("MINIMAX_API_KEY", None)  # 强制降级
    r = listener._handle_event(evt, "/fake/lark-cli")
    assert r["chat_id"] == "oc_test_listener"
    assert r["message_id"] == "om_test_listener"
    # LLM 降级时 answer 是 fallback 文案
    assert r["answer_len"] > 0
    assert r["send"]["ok"] is True
    print(f"  ✓ test_handle_event_full: chat={r['chat_id']}, answer_len={r['answer_len']}")


# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [
        test_synonym_expand_improves_score,
        test_expand_query_adds_synonyms,
        test_session_create_get,
        test_session_append_and_history,
        test_session_reset,
        test_session_ttl,
        test_session_trim,
        test_backtest_multi_no_fixtures,
        test_backtest_multi_with_fixtures,
        test_strip_mention,
        test_extract_text,
        test_handle_event_skipped,
        test_handle_event_full,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"  ✗ {t.__name__}: EXCEPTION {type(e).__name__}: {e}")
            sys.exit(1)
    print(f"\n✓ {len(tests)} tests passed")
