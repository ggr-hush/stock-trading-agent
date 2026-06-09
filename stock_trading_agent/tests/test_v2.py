"""
test_v2.py — v2 三个新功能的测试
1) knowledge.retrieve 检索 + 降级
2) reasoner.with_knowledge / answer_question 降级
3) multi_strategy.run 多方案对比
4) Webhook handler 单元测试
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import stock_trading_agent.engine.knowledge as kmod
import stock_trading_agent.engine.multi_strategy as ms
import stock_trading_agent.engine.paper_trader as pt
from stock_trading_agent.agent import _WebhookHandler
from stock_trading_agent.llm import reasoner as llmr


# ─────────── knowledge ───────────

def test_knowledge_retrieve() -> None:
    """BM25 检索 '一夜持股法' 应该优先返回 haoyun_wisdom"""
    kmod.reset_index()
    r = kmod.retrieve("什么是一夜持股法", k=3)
    assert len(r) > 0, "应至少返回 1 条"
    top = r[0]
    assert top["source"] == "haoyun_wisdom", f"应优先 haoyun_wisdom, got {top['source']}"
    assert top["score"] > 0
    print(f"  ✓ test_knowledge_retrieve: top={top['source']} score={top['score']}")


def test_knowledge_format_context() -> None:
    """format_context 长度限制"""
    r = kmod.retrieve("选股 仓位", k=5)
    ctx = kmod.format_context(r, max_chars=200)
    assert len(ctx) <= 200, f"ctx 长度应 ≤200, got {len(ctx)}"
    print(f"  ✓ test_knowledge_format_context: ctx_len={len(ctx)}")


def test_knowledge_empty_query() -> None:
    """空 query 返回空"""
    r = kmod.retrieve("", k=3)
    assert r == []
    r = kmod.retrieve("   ", k=3)
    assert r == []
    print(f"  ✓ test_knowledge_empty_query: 空查询降级")


# ─────────── reasoner (with RAG) ───────────

def test_reasoner_with_knowledge_degrades() -> None:
    """无 API key 时 with_knowledge / answer_question 返回空"""
    os.environ.pop("MINIMAX_API_KEY", None)
    assert llmr.with_knowledge("任何问题") == ""
    assert llmr.answer_question("任何问题", recent_picks=[], market_env={}) == ""
    print(f"  ✓ test_reasoner_with_knowledge_degrades: 2 个新调用点降级 OK")


# ─────────── multi_strategy ───────────

def _isolated(name: str):
    d = Path(tempfile.mkdtemp(prefix=f"sta_test_v2_{name}_"))
    pt.DB_PATH = d / "quant.db"
    pt.DATA_DIR = d
    return d


def _mk_stock(code: str, chg: float, turnover: float, sector: str = "好板块") -> dict:
    return {"code": code, "name": f"测试{code}", "price": 10.0, "prev_close": 9.7,
            "chg_pct": chg, "turnover": turnover, "total_mv_yi": 200, "amount_yi": 5,
            "high": 10.2, "low": 9.85, "amplitude": 3.5, "sector": sector}


def test_multi_strategy_picks_a_when_both() -> None:
    """A 和 B 都有候选 → 选 A"""
    _isolated("a_over_b")
    pt.init_account()
    import stock_trading_agent.engine.data_fetcher as df
    cfg = json.loads(Path("/Users/alice/Documents/Codex/stock-trading-agent/stock_trading_agent/config.json").read_text())
    df._CONFIG_CACHE = cfg

    stocks = [
        _mk_stock("000001", 3.1, 8.5, "好板块"),  # A
        _mk_stock("000002", 3.2, 7.0, "好板块"),  # B
    ]
    hot = [{"name": "机器人", "chg_pct": 5.0}]
    r = ms.run(stocks=stocks, market_env={"env_score": 50}, hot_sectors=hot, config=cfg)
    assert r["recommendation"] == "A", f"A 优先, got {r['recommendation']}"
    assert r["plans"]["A"]["n"] >= 1
    assert r["plans"]["B"]["n"] >= 1
    print(f"  ✓ test_multi_strategy_picks_a_when_both: A={r['plans']['A']['n']} B={r['plans']['B']['n']}")


def test_multi_strategy_falls_back_to_c() -> None:
    """A B 都无 → 选 C"""
    _isolated("c_only")
    pt.init_account()
    import stock_trading_agent.engine.data_fetcher as df
    cfg = json.loads(Path("/Users/alice/Documents/Codex/stock-trading-agent/stock_trading_agent/config.json").read_text())
    df._CONFIG_CACHE = cfg
    r = ms.run(stocks=[], market_env={}, hot_sectors=[], config=cfg)
    assert r["recommendation"] == "C", f"无候选应选 C, got {r['recommendation']}"
    print(f"  ✓ test_multi_strategy_falls_back_to_c: 推荐 C")


# ─────────── webhook ───────────

def _make_handler(method: str, path: str, body: bytes = b""):
    h = _WebhookHandler.__new__(_WebhookHandler)
    h.command = method
    h.path = path
    h.request_version = "HTTP/1.1"
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)

    class W:
        def __init__(self): self.chunks = []
        def write(self, b): self.chunks.append(b)
        @property
        def body(self): return b''.join(self.chunks)

    w = W()
    h.wfile = w
    class R:
        status = None
        hdr = {}
    r = R()
    h.send_response = lambda s: setattr(r, 'status', s)
    h.send_header = lambda k, v: r.hdr.__setitem__(k, v)
    h.end_headers = lambda: None
    h.log_message = lambda *a, **k: None
    return h, r, w


def test_webhook_health() -> None:
    """GET /health"""
    h, r, w = _make_handler("GET", "/health")
    h.do_GET()
    assert r.status == 200
    assert json.loads(w.body)["ok"] is True
    print(f"  ✓ test_webhook_health: 200 OK")


def test_webhook_chat_no_question() -> None:
    """POST /chat 无 question → 400"""
    h, r, w = _make_handler("POST", "/chat", b'{}')
    h.do_POST()
    assert r.status == 400
    assert "required" in json.loads(w.body)["error"]
    print(f"  ✓ test_webhook_chat_no_question: 400")


def test_webhook_chat_with_question() -> None:
    """POST /chat 有 question → 200 + answer (降级模式无 LLM 时返回 fallback)"""
    os.environ.pop("MINIMAX_API_KEY", None)
    h, r, w = _make_handler("POST", "/chat", json.dumps({"question": "今天怎么没选宁德"}).encode())
    h.do_POST()
    assert r.status == 200
    out = json.loads(w.body)
    assert "answer" in out
    assert out["question"] == "今天怎么没选宁德"
    print(f"  ✓ test_webhook_chat_with_question: 200, answer[:60]={out['answer'][:60]}")


def test_webhook_not_found() -> None:
    """POST /unknown → 404"""
    h, r, w = _make_handler("POST", "/unknown", b'{}')
    h.do_POST()
    assert r.status == 404
    print(f"  ✓ test_webhook_not_found: 404")


# ─────────── runner ───────────

if __name__ == "__main__":
    tests = [
        test_knowledge_retrieve,
        test_knowledge_format_context,
        test_knowledge_empty_query,
        test_reasoner_with_knowledge_degrades,
        test_multi_strategy_picks_a_when_both,
        test_multi_strategy_falls_back_to_c,
        test_webhook_health,
        test_webhook_chat_no_question,
        test_webhook_chat_with_question,
        test_webhook_not_found,
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
