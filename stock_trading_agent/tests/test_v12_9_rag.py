"""test_v12_9_rag.py — v12.9 explain_pick 接 RAG 知识库

Covers:
  - _build_explain_query: 高分 → 强信号关键词
  - _build_explain_query: 中分 → 中等关键词
  - _build_explain_query: 拼进板块/缠论术语
  - _run_explain_pick: 调 retrieve + 末尾追加 [来源] 标注
  - _run_explain_pick: preset_results 复用 RAG 检索, answer_question 不再二次 retrieve
  - answer_question: preset_results 跳过内部 retrieve
  - keyword_fallback: "缠论" / "108课" / "好运2008" 命中 search_knowledge
  - keyword_fallback: 普通"今日选股" 仍命中 get_picks (兼容性)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_build_explain_query_high_score() -> None:
    """score >= 75 → 拼 "强信号" """
    from stock_trading_agent.engine.skills import _build_explain_query
    q = _build_explain_query({"name": "贵州茅台", "sector": "白酒", "score": 80})
    assert "贵州茅台" in q
    assert "白酒" in q
    assert "强信号" in q
    assert "缠中说禅" in q
    assert "龙头" in q
    print("  PASS test_build_explain_query_high_score")


def test_build_explain_query_mid_score() -> None:
    """score 60-74 → 拼 "中等" """
    from stock_trading_agent.engine.skills import _build_explain_query
    q = _build_explain_query({"name": "测试票", "sector": "电子", "score": 65})
    assert "中等" in q
    assert "测试票" in q
    assert "电子" in q
    print("  PASS test_build_explain_query_mid_score")


def test_explain_pick_appends_sources_and_uses_preset() -> None:
    """_run_explain_pick: 末尾追加 [来源] + 把 retrieve 结果传 preset_results"""
    from stock_trading_agent.engine import skills, knowledge
    from stock_trading_agent.llm import reasoner

    mock_row = MagicMock()
    mock_row.__getitem__ = lambda self, k: {
        'pick_date': '2026-06-12', 'code': '600519', 'name': '贵州茅台',
        'price': 1500, 'chg_pct': 1.2, 'score': 80, 'sector': '白酒',
        'plan_used': 'A', 'market_env_score': 65,
    }[k]
    mock_row.keys.return_value = ['pick_date', 'code', 'name', 'price', 'chg_pct',
                                   'score', 'sector', 'plan_used', 'market_env_score']

    rag_results = [
        {"title": "缠中说禅108课 第17课", "source": "chanlun", "text": "...", "score": 5.2},
        {"title": "好运2008: 龙头战法", "source": "haoyun_wisdom", "text": "...", "score": 4.1},
        {"title": "", "source": "susan:1", "text": "12.28 倒计时周末更新", "score": 3.0},
    ]

    answer_called_with: dict = {}

    def fake_answer(**kwargs):
        answer_called_with.update(kwargs)
        return "因为是龙头, 量价齐升 (mock)"

    with patch.object(skills, 'get_db') as mock_db, \
         patch("stock_trading_agent.llm.reasoner.retrieve", return_value=rag_results) as mock_retrieve, \
         patch.object(reasoner, 'answer_question', side_effect=fake_answer):
        mock_db.return_value.execute.return_value.fetchone.return_value = mock_row
        result = skills._run_explain_pick({"code": "600519"})

    # 1) 解释末尾追加 [来源]
    assert "因为是龙头" in result["explanation"]
    assert "[来源]" in result["explanation"]
    assert "缠中说禅108课 第17课" in result["explanation"]
    assert "好运2008: 龙头战法" in result["explanation"]
    # 第 3 条 title 为空, 用 source:text 前 20 字兜底
    assert "susan:1" in result["explanation"]

    # 2) preset_results 传进去了, retrieve 只被调 1 次
    assert answer_called_with.get("preset_results") is rag_results
    assert mock_retrieve.call_count == 1
    print("  PASS test_explain_pick_appends_sources_and_uses_preset")


def test_answer_question_preset_results_skips_retrieve() -> None:
    """answer_question: 传 preset_results → 内部不调 retrieve"""
    from stock_trading_agent.engine import knowledge
    from stock_trading_agent.llm import reasoner

    with patch.object(knowledge, 'retrieve') as mock_retrieve, \
         patch.object(reasoner, 'chat', return_value={"ok": True, "content": "mock", "latency_ms": 50, "usage": {}}):
        reasoner.answer_question(
            question="测试问题",
            preset_results=[{"title": "预设知识", "source": "x", "text": "y", "score": 1}],
        )
    assert mock_retrieve.call_count == 0, "preset_results 传时, 内部 retrieve 不应被调"


def test_keyword_fallback_chanlun_routes_to_search() -> None:
    """'缠论' / '108课' / '好运2008' / '苏三' 关键词命中 search_knowledge"""
    from stock_trading_agent.engine.skills import keyword_fallback
    assert keyword_fallback("缠论怎么看") == "search_knowledge"
    assert keyword_fallback("讲讲 108课") == "search_knowledge"
    assert keyword_fallback("好运2008 的心法") == "search_knowledge"
    assert keyword_fallback("苏三怎么看") == "search_knowledge"
    assert keyword_fallback("知识库有啥") == "search_knowledge"
    print("  PASS test_keyword_fallback_chanlun_routes_to_search")


def test_keyword_fallback_picks_still_works() -> None:
    """'今日选股' 仍命中 get_picks (向后兼容)"""
    from stock_trading_agent.engine.skills import keyword_fallback
    assert keyword_fallback("今日选股") == "get_picks"
    assert keyword_fallback("持仓") == "get_positions"
    assert keyword_fallback("大盘") == "get_market_env"
    print("  PASS test_keyword_fallback_picks_still_works")


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
