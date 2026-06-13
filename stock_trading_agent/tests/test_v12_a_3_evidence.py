"""test_v12_a_3_evidence.py — v12.A.3 证据编号 单元测试

Covers (改动 1):
  - engine/evidence.py 工具函数 (3 个)
  - 5 个 _run_* skill 返 evidence 字段
  - 2 个 _render_*_card 卡片底部含 "📚 证据" 段
  - 3 个 j2 模板含引用编号要求 (render smoke test)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── engine/evidence.py 工具 (3 个) ───────────

def test_make_evidence_id() -> None:
    from stock_trading_agent.engine.evidence import make_evidence_id
    assert make_evidence_id("rag", 1) == "R1"
    assert make_evidence_id("sql", 2) == "S2"
    assert make_evidence_id("live", 1) == "L1"
    assert make_evidence_id("facts", 3) == "F3"
    assert make_evidence_id("unknown_kind", 1) == "E1"  # 未知 kind fallback


def test_format_evidence_for_prompt() -> None:
    from stock_trading_agent.engine.evidence import format_evidence_for_prompt
    out = format_evidence_for_prompt([
        {"id": "R1", "title": "缠论 23 课", "snippet": "教你炒股票..."},
        {"id": "R2", "title": "好运心法", "snippet": "龙头股战法..."},
    ])
    assert "[R1] 缠论 23 课" in out
    assert "[R2] 好运心法" in out
    assert "龙头股战法" in out
    # 空列表
    assert format_evidence_for_prompt([]) == ""


def test_render_evidence_section() -> None:
    from stock_trading_agent.engine.evidence import render_evidence_section
    out = render_evidence_section([
        {"id": "R1", "title": "缠论 23 课", "snippet": "..."},
        {"id": "R2", "title": "好运心法", "snippet": "..."},
    ])
    assert out["tag"] == "div"
    assert "📚 证据" in out["text"]["content"]
    assert "[R1]" in out["text"]["content"]
    assert "[R2]" in out["text"]["content"]
    # 空 evidence
    empty = render_evidence_section([])
    assert empty["text"]["content"] == ""


# ─────────── 5 个 _run_* skill evidence 字段 (5 个) ───────────

def test_run_get_picks_has_evidence() -> None:
    """get_picks 返 evidence 字段 (SQL 源)"""
    import sqlite3
    from stock_trading_agent.engine.skills import _run_get_picks
    # 用真实 sqlite3.Row 风格的 dict (skills 里 first = dict(rows[0]))
    fake_row = {
        "pick_date": "2025-11-07", "code": "002063", "name": "亚光科技",
        "price": 12.5, "chg_pct": 5.2, "score": 85.5,
        "sector": "半导体", "plan_used": "A",
    }
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchall.return_value = [fake_row]
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db):
        result = _run_get_picks({"top_n": 3})
    assert "evidence" in result
    ev = result["evidence"]
    assert len(ev) == 1
    assert ev[0]["id"] == "S1"
    assert ev[0]["kind"] == "sql"
    assert "002063" in ev[0]["snippet"]


def test_run_get_positions_has_evidence() -> None:
    """get_positions 返 evidence 字段"""
    from stock_trading_agent.engine.skills import _run_get_positions
    fake_row = {
        "code": "600519", "name": "贵州茅台", "status": "open",
        "pick_date": "2025-11-01", "open_price": 1500.0,
    }
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchall.return_value = [fake_row]
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db):
        result = _run_get_positions({"status": "open"})
    assert "evidence" in result
    assert result["evidence"][0]["id"] == "S1"
    assert "paper_positions" in result["evidence"][0]["title"]


def test_run_get_market_env_picks_has_evidence() -> None:
    """get_market_env picks 分支 evidence"""
    from stock_trading_agent.engine.skills import _run_get_market_env
    fake_row = {"market_env_score": 65, "market_env_level": "偏多", "pick_date": "2025-11-07"}
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = fake_row
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db):
        result = _run_get_market_env({})
    assert "evidence" in result
    assert result["evidence"][0]["id"] == "S1"
    assert "picks 表" in result["evidence"][0]["title"]


def test_run_explain_pick_has_evidence() -> None:
    """explain_pick evidence: RAG (R1, R2...)"""
    from stock_trading_agent.engine.skills import _run_explain_pick
    fake_pick_row = {
        "pick_date": "2025-11-07", "code": "002063", "name": "亚光科技",
        "price": 12.5, "chg_pct": 5.2, "score": 85,
        "sector": "半导体", "plan_used": "A", "market_env_score": 65,
    }
    rag_results = [
        {"title": "缠论 23 课", "source": "chanlun", "text": "教你炒股票..."},
        {"title": "好运心法", "source": "haoyun", "text": "龙头股战法..."},
    ]
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = fake_pick_row
    # answer_question / retrieve 来自 ..llm.reasoner, patch 源模块
    with patch("stock_trading_agent.engine.skills.get_db", return_value=fake_db), \
         patch("stock_trading_agent.llm.reasoner.answer_question", return_value="基于缠论看..."), \
         patch("stock_trading_agent.llm.reasoner.retrieve", return_value=rag_results):
        result = _run_explain_pick({"code": "002063"})
    assert "evidence" in result
    ev_ids = [e["id"] for e in result["evidence"]]
    assert "R1" in ev_ids
    assert "R2" in ev_ids


def test_run_get_stock_quote_has_evidence() -> None:
    """get_stock_quote 返 evidence 字段 (K 线源)"""
    from stock_trading_agent.engine.skills import _run_get_stock_quote
    fake_kline = {
        "code": "002063", "name": "亚光科技",
        "date": "2025-11-07", "close": 12.5, "chg_pct": 5.2,
        "open": 12.0, "high": 12.8, "low": 11.9,
        "volume": 1000000, "amount": 12500000, "turnover": 3.5,
    }
    # fetch_stock_kline 是在 _run_get_stock_quote 函数体内 from-import, patch 源模块
    with patch("stock_trading_agent.engine.data_fetcher.fetch_stock_kline", return_value=fake_kline):
        result = _run_get_stock_quote({"code": "002063", "date": "2025-11-07"})
    assert "evidence" in result
    assert result["evidence"][0]["kind"] == "sql"
    title = result["evidence"][0]["title"]
    assert ("K 线" in title) or ("kline" in title.lower())


# ─────────── _render_*_card 底部含证据 (2 个) ───────────

def test_render_market_env_card_has_evidence_section() -> None:
    from stock_trading_agent.engine.skills import _render_market_env_card
    card = _render_market_env_card({
        "env_score": 65, "env_level": "偏多",
        "date": "2025-11-07", "source": "picks",
        "evidence": [{"id": "S1", "title": "picks 表 (2025-11-07)", "snippet": "..."}],
    })
    text = card["content"]["text"]
    assert "📚 证据" in text
    assert "[S1]" in text


def test_render_stock_quote_card_has_evidence_section() -> None:
    from stock_trading_agent.engine.skills import _render_stock_quote_card
    card = _render_stock_quote_card({
        "code": "002063", "name": "亚光科技",
        "date": "2025-11-07", "close": 12.5, "chg_pct": 5.2,
        "open": 12.0, "high": 12.8, "low": 11.9,
        "volume": 1000000, "amount": 12500000, "turnover": 3.5,
        "evidence": [{"id": "S1", "title": "东方财富 K 线 (2025-11-07)", "snippet": "..."}],
    })
    text = card["content"]["text"]
    assert "📚 证据" in text
    assert "[S1]" in text


# ─────────── j2 模板含引用编号要求 (1 个 smoke) ───────────

def test_j2_templates_have_evidence_rule() -> None:
    """3 个 j2 模板末尾含 v12.A.3 引用编号要求"""
    base = Path("stock_trading_agent/llm/prompts")
    for name in ("advisor.j2", "with_knowledge.j2", "auto_period_explain.j2"):
        content = (base / name).read_text(encoding="utf-8")
        assert "v12.A.3" in content, f"{name} 缺 v12.A.3 标记"
        assert "[R" in content, f"{name} 缺证据编号示例"


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
