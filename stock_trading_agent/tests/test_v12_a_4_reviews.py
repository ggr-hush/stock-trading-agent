"""test_v12_a_4_reviews.py — v12.A.4 reviews 表 + skill 单元测试

Covers (借鉴 #3):
  - reviews.add_review / query_reviews / get_review / update_review / tag_count
  - reviews.parse_natural_review 自然语言解析
  - skills.add_review / query_reviews skill 集成
  - temporal_facts REVIEWED predicate
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── reviews 基础 CRUD ───────────

def test_add_review_returns_id_and_persists() -> None:
    from stock_trading_agent.engine import reviews
    rid = reviews.add_review(
        date="2026-06-16", stock_code="002063", stock_name="亚光科技",
        action_taken=True, reason="早盘冲高", result="2.5%",
        summary="止盈出局", tags=["止盈", "早盘冲高"],
    )
    assert rid > 0
    r = reviews.get_review(rid)
    assert r["stock_code"] == "002063"
    assert r["action_taken"] is True
    assert r["tags"] == ["止盈", "早盘冲高"]


def test_query_reviews_by_date() -> None:
    from stock_trading_agent.engine import reviews
    # 清理之前的
    today = "2026-06-16"
    for r in reviews.query_reviews(date=today, limit=100):
        if r["stock_code"] == "600519":
            reviews.update_review(r["id"], {"summary": r["summary"]})  # 留个空 trigger
    # 写一条
    reviews.add_review(date=today, stock_code="600519",
                      action_taken=False, summary="看戏")
    items = reviews.query_reviews(date=today, stock_code="600519")
    assert any(i["stock_code"] == "600519" for i in items)


def test_query_reviews_by_tag() -> None:
    from stock_trading_agent.engine import reviews
    today = "2026-06-16"
    reviews.add_review(date=today, stock_code="000001", tags=["题材退潮", "早盘冲高"])
    items = reviews.query_reviews(tag="题材退潮", limit=50)
    assert any("题材退潮" in i["tags"] for i in items)


def test_tag_count_returns_freq_dict() -> None:
    from stock_trading_agent.engine import reviews
    today = "2026-06-16"
    reviews.add_review(date=today, stock_code="000002", tags=["止盈"])
    reviews.add_review(date=today, stock_code="000003", tags=["止盈", "题材退潮"])
    counts = reviews.tag_count(days=7)
    assert "止盈" in counts
    assert counts["止盈"] >= 2


def test_update_review_modifies_fields() -> None:
    from stock_trading_agent.engine import reviews
    rid = reviews.add_review(date="2026-06-16", stock_code="000004", summary="旧摘要")
    updated = reviews.update_review(rid, {"summary": "新摘要", "result": "5%"})
    assert updated["summary"] == "新摘要"
    assert updated["result"] == "5%"


# ─────────── parse_natural_review ───────────

def test_parse_natural_review_basic() -> None:
    from stock_trading_agent.engine.reviews import parse_natural_review
    r = parse_natural_review("加复盘: 002063 止盈 2.5% 早盘冲高")
    assert r["stock_code"] == "002063"
    assert r["action_taken"] is True
    assert "2.5%" in r["result"]
    assert "止盈" in r["tags"]


def test_parse_natural_review_observation() -> None:
    """'没买' / '看戏' → action_taken=False"""
    from stock_trading_agent.engine.reviews import parse_natural_review
    r = parse_natural_review("加复盘: 600519 看戏 白酒板块退潮")
    assert r["stock_code"] == "600519"
    assert r["action_taken"] is False
    assert "观察" in r["tags"]


def test_parse_natural_review_missing_code_returns_error() -> None:
    from stock_trading_agent.engine.reviews import parse_natural_review
    r = parse_natural_review("加复盘: 今天行情不错")
    assert r is not None
    assert "error" in r


def test_parse_natural_review_invalid_returns_none() -> None:
    from stock_trading_agent.engine.reviews import parse_natural_review
    assert parse_natural_review("今天行情怎么样") is None
    assert parse_natural_review("") is None
    assert parse_natural_review("加复盘:") is None


# ─────────── skill 集成 ───────────

def test_run_add_review_skill() -> None:
    """v12.A.4: 飞书 '加复盘: ...' 触发的 add_review skill"""
    from stock_trading_agent.engine.skills import _run_add_review, _render_reviews_card
    import os
    os.environ["STOCK_AGENT_FACTS_PATH"] = f"{tempfile.mkdtemp()}/facts.jsonl"
    result = _run_add_review({"text": "加复盘: 002063 止盈 2.5% 早盘冲高"})
    assert result["ok"] is True
    assert result["stock_code"] == "002063"
    assert "止盈" in result["tags"]
    card = _render_reviews_card(result)
    assert card["msg_type"] == "text"  # add_review 返 text (短消息)
    assert "复盘已记录" in card["content"]["text"]


def test_run_query_reviews_skill() -> None:
    from stock_trading_agent.engine.skills import _run_query_reviews, _render_reviews_card
    result = _run_query_reviews({"date": "2026-06-16", "limit": 5})
    assert "items" in result
    assert "count" in result
    card = _render_reviews_card(result)
    assert card["msg_type"] == "interactive"
    text = card["content"]["text"]["content"]
    assert "复盘列表" in text


# ─────────── temporal_facts REVIEWED predicate ───────────

def test_temporal_facts_reviewed_predicate() -> None:
    """v12.A.4: REVIEWED 加入 PREDICATE_VOCAB, 可正常 record"""
    os.environ["STOCK_AGENT_FACTS_PATH"] = f"{tempfile.mkdtemp()}/facts.jsonl"
    from stock_trading_agent.engine import temporal_facts
    fid = temporal_facts.record(
        "002063", "REVIEWED", "review:1", "止盈 2.5%", source="user_review",
    )
    assert fid
    f = temporal_facts.get_fact(fid)
    assert f["predicate"] == "REVIEWED"
    assert f["status"] == "active"


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
