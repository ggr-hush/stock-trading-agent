"""test_v12_9_3_realtime.py — picks 找到时也拉实时, 治"问实时答 RAG"

Covers:
  - picks 找到 + 实时拉到 → 末尾追加 [实时 N 元 · 今日 N% · 换手 N%]
  - picks 找到 + 实时拉不到 → 优雅降级, 返 realtime={}, 不影响 RAG 路径
  - rag_query 被实时 facts 叠加 (LLM 真收到)
"""
from __future__ import annotations

import sys
from unittest.mock import patch, MagicMock

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _make_pick_row():
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        'pick_date': '2026-06-13', 'code': '603063', 'name': '禾望电气',
        'price': 32.5, 'chg_pct': 1.2, 'score': 75, 'sector': '风电设备',
        'plan_used': 'A', 'market_env_score': 65,
    }[k]
    row.keys.return_value = ['pick_date', 'code', 'name', 'price', 'chg_pct',
                              'score', 'sector', 'plan_used', 'market_env_score']
    return row


def test_explain_pick_with_realtime_appends_quote() -> None:
    """picks 找到 + 实时拉到 → 末尾追加实时价/涨跌/换手"""
    from stock_trading_agent.engine import skills
    from stock_trading_agent.engine import data_fetcher as df
    from stock_trading_agent.llm import reasoner

    row = _make_pick_row()
    captured_question: dict = {}

    def fake_answer(question, **kwargs):
        captured_question["question"] = question
        return "RAG 分析: 数据中心电源是新故事"

    with patch.object(skills, 'get_db') as mock_db, \
         patch.object(df, 'fetch_realtime_quote',
                      return_value={"code": "603063", "name": "禾望电气",
                                    "price": 33.5, "chg_pct": 2.8,
                                    "turnover": 1.5, "mktcap_yi": 95,
                                    "source": "东方财富实时"}), \
         patch.object(reasoner, "retrieve", return_value=[
             {"title": "苏三 12.18", "source": "susan", "text": "...", "score": 5.0}
         ]), \
         patch.object(reasoner, 'answer_question', side_effect=fake_answer):
        mock_db.return_value.execute.return_value.fetchone.return_value = row
        result = skills._run_explain_pick({"code": "603063"})

    # 1) 末尾有 [实时 ... 元 · 今日 ...% · 换手 ...%]
    assert "[实时 33.5 元 · 今日 2.8% · 换手 1.5%]" in result["explanation"], \
        f"应有时效实时价行, got: {result['explanation']}"
    # 2) rag_query 叠加了实时 facts
    q = captured_question["question"]
    assert "今日实时" in q
    assert "33.5" in q
    # 3) realtime 字段在结果里
    assert result["realtime"]["price"] == 33.5
    # 4) RAG 来源也保留
    assert result["rag_sources"] == ["苏三 12.18"]
    print("  PASS test_explain_pick_with_realtime_appends_quote")


def test_explain_pick_realtime_failure_graceful() -> None:
    """picks 找到 + 实时拉不到 → 优雅降级, 不影响 RAG 路径"""
    from stock_trading_agent.engine import skills
    from stock_trading_agent.engine import data_fetcher as df
    from stock_trading_agent.llm import reasoner

    row = _make_pick_row()

    with patch.object(skills, 'get_db') as mock_db, \
         patch.object(df, 'fetch_realtime_quote', return_value={}), \
         patch.object(reasoner, "retrieve", return_value=[]), \
         patch.object(reasoner, 'answer_question', return_value="RAG 分析: 板块龙头"):
        mock_db.return_value.execute.return_value.fetchone.return_value = row
        result = skills._run_explain_pick({"code": "603063"})

    # 1) 实时行不出现
    assert "[实时" not in result["explanation"]
    # 2) realtime 字段是空 dict
    assert result["realtime"] == {}
    # 3) RAG 路径正常返
    assert "RAG 分析" in result["explanation"]
    print("  PASS test_explain_pick_realtime_failure_graceful")


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
