"""v12.A.4.c — 选股策略 v3.1 测试 (5 改 1 包)

覆盖:
  - A: 评分公式方向感知 (跌=0, 涨 3% 满 20, 偏离越远越低)
  - B: plan_a/b 阈值放宽 (涨幅 2% 换手 4% 进 plan_a)
  - C: plan_c 兜底 score >= 60
  - D: stage_pick 写 picks 表
  - E: 整体回归 (3 改 1 包不破坏老行为)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# 准备测试 config (跟 test_picker.py 一样的 PAYLOAD, 但 v3.1 阈值)
TEST_DIR = Path(tempfile.mkdtemp(prefix="sta_test_picker_v31_"))
TEST_CONFIG = TEST_DIR / "config.yaml"
TEST_JSON = TEST_DIR / "config.json"

PAYLOAD = {
    "hard": {
        "chg_danger": 4.8, "amp_danger": 8.0, "chg_over": 6.0, "limit_up": 9.8,
        "mv_lo_yi": 50, "mv_hi_yi": 5000, "amt_lo_yi": 3, "max_picks": 15,
    },
    # v3.1: 阈值放宽
    "plan_a": {"chg_lo": 1.5, "chg_hi": 5.0, "turnover_lo": 3.0, "turnover_hi": 15.0, "amplitude_hi": 8.0},
    "plan_b": {"chg_lo": 0.5, "chg_hi": 7.0, "turnover_lo": 1.0, "turnover_hi": 20.0, "amplitude_hi": 8.0},
    "v3": {
        "score_max": {"value": 80.0, "safe_range": [75.0, 85.0]},
        "strong_band_lo": {"value": 3.0, "safe_range": [2.8, 3.2]},
        "strong_band_hi": {"value": 3.5, "safe_range": [3.3, 3.7]},
        "strong_bonus": {"value": 5, "safe_range": [3, 8]},
        "theme_bonus": {"value": 3, "safe_range": [2, 5]},
    },
    "blacklist": {
        "sectors": ["光伏设备", "电子化学品Ⅱ"],
        "max_add_per_week": 2, "max_remove_per_week": 2, "safe_sectors": [],
    },
    "env": {
        "indices": [
            {"code": "sh000001", "name": "上证指数", "weight": 0.4},
            {"code": "sz399006", "name": "创业板指", "weight": 0.3},
            {"code": "sh000688", "name": "科创50", "weight": 0.3},
        ],
        "vol_thresh_hi_yi": 12000, "vol_thresh_lo_yi": 9000,
    },
    "position": {
        "full": {"score_min": 75, "ratio": 1.0, "advice": "满仓", "market": "牛市"},
        "heavy": {"score_min": 60, "ratio": 0.8, "advice": "重仓", "market": "牛市"},
        "half": {"score_min": 45, "ratio": 0.5, "advice": "半仓", "market": "震荡"},
        "light": {"score_min": 30, "ratio": 0.2, "advice": "轻仓", "market": "震荡"},
        "empty": {"score_min": 0, "ratio": 0.0, "advice": "空仓", "market": "熊市"},
    },
    "paper": {"initial_capital": 1000000.0, "max_position_ratio": 0.20, "max_concurrent": 3},
    "schedule": {},
    "data_source": {
        "eastmoney_base": "https://push2delay.eastmoney.com",
        "sina_kline": "", "tencent_quote": "", "tencent_kline": "",
    },
    "llm": {},
}
TEST_CONFIG.write_text(json.dumps(PAYLOAD, ensure_ascii=False, indent=2))
TEST_JSON.write_text(json.dumps(PAYLOAD, ensure_ascii=False, indent=2))

# Monkey-patch
import stock_trading_agent.engine.picker as pk
import stock_trading_agent.engine.data_fetcher as df
df._CONFIG_PATH = TEST_CONFIG
df._CONFIG_CACHE = PAYLOAD
pk.config = PAYLOAD


def _mk_stock(code: str, chg: float, turnover: float, amp: float = 5.0,
              mv: float = 200.0, amount: float = 5.0, sector: str = "好板块",
              high: float = 0, low: float = 0, price: float = 10.0) -> dict:
    if high == 0:
        high = price * (1 + chg / 100 + amp / 200)
    if low == 0:
        low = price * (1 + chg / 100 - amp / 200)
    return {
        "code": code, "name": f"测试{code}",
        "price": price, "prev_close": price / (1 + chg / 100),
        "chg_pct": chg, "turnover": turnover,
        "total_mv_yi": mv, "amount_yi": amount,
        "high": high, "low": low, "amplitude": amp,
        "limit_up_days": 0, "sector": sector,
    }


# ─────────── A: 评分公式方向感知 ───────────

class TestScoreDirectional(unittest.TestCase):
    """v3.1: chg_score 改方向感知"""

    def test_falling_stock_zero(self):
        """跌 5% 票 chg_score=0 (v3 旧版 abs() 会给高分, 是 bug)"""
        s = _mk_stock("000001", chg=-5.0, turnover=8.0)
        score = pk._score_stock(s, PAYLOAD)
        # base: amount(5/30*30=5) + turn(8/10*30=24) + chg(0) + size(200/300*20=13.33) = 42.33
        # 不在强信号带, 不加分
        self.assertAlmostEqual(score, 42.33, places=2)
        print(f"  ✓ 跌 5% 票 score≈42.33 (chg_score=0)")

    def test_peak_around_3pct(self):
        """涨 3% 票 chg_score=20 满 20 分"""
        s = _mk_stock("000002", chg=3.0, turnover=8.0)
        score = pk._score_stock(s, PAYLOAD)
        # base: 5 + 24 + 20 + 13.33 = 62.33, +5 强信号带 = 67.33
        # chg=3.0 在 [3.0, 3.5] 强信号带内
        self.assertAlmostEqual(score, 67.33, places=2)
        print(f"  ✓ 涨 3% 票 score≈67.33 (chg_score=20, 强信号带+5)")

    def test_deviating_stock_decreases(self):
        """涨 6% 票 chg_score=0 (偏离 3% 太远)"""
        s = _mk_stock("000003", chg=6.0, turnover=8.0)
        score = pk._score_stock(s, PAYLOAD)
        # 涨 6% 偏离 3% = 3, chg_score = max(20 - 3*10, 0) = 0
        # base: 5 + 24 + 0 + 13.33 = 42.33
        self.assertAlmostEqual(score, 42.33, places=2)
        print(f"  ✓ 涨 6% 票 score≈42.33 (chg_score=0, 偏离超限)")


# ─────────── B: plan_a/b 阈值放宽 ───────────

class TestPlanThresholdsRelaxed(unittest.TestCase):
    """v3.1: 涨 2% 换手 4% 进 plan_a (旧版 3-4% + 8-10% 太严)"""

    def test_relaxed_chg_2pct_turnover_4pct_lands_in_a(self):
        """涨 2% 换手 4% 应该进 plan_a (v3 旧版 plan_a=[] 因为 chg_lo=3.0)"""
        stocks = [_mk_stock("000001", chg=2.0, turnover=4.0)]
        final, stats = pk.filter_stocks(stocks, config=PAYLOAD)
        # 新阈值: chg_lo=1.5, chg_hi=5.0, turnover_lo=3.0
        # 涨 2% 换手 4% 在 plan_a 范围内
        self.assertEqual(stats["plan_used"], "A")
        self.assertEqual(len(final), 1)
        print(f"  ✓ 涨 2% 换手 4% 进 plan_a, final={len(final)} 只")

    def test_chg_1pct_turnover_2pct_lands_in_b(self):
        """涨 1% 换手 2% 应该进 plan_b (不在 plan_a: chg<1.5)"""
        stocks = [_mk_stock("000002", chg=1.0, turnover=2.0)]
        final, stats = pk.filter_stocks(stocks, config=PAYLOAD)
        # plan_a: chg<1.5 不入 → []
        # plan_b: chg_lo=0.5, turnover_lo=1.0 → 进入
        self.assertEqual(stats["plan_used"], "B")
        self.assertEqual(len(final), 1)
        print(f"  ✓ 涨 1% 换手 2% 进 plan_b")


# ─────────── C: plan_c 兜底 score >= 60 ───────────

class TestPlanCFiltersLowScore(unittest.TestCase):
    """v3.1: plan_c 兜底只取 score >= 60"""

    def test_plan_c_filters_low_score(self):
        """plan_c 路径下低分票被过滤掉 (3 只都不在 plan_a/b 范围)"""
        # 关键: 既要不在 plan_a/b, 又不能被 hard_excluded (chg>=4.8 或 amp>=8)
        # plan_a: chg [1.5, 5.0), turn [3, 15]
        # plan_b: chg [0.5, 7.0), turn [1, 20]
        # → chg<0.5, turn>20 (同时不 hard_excluded) → 涨 0.3% 换手 25 OK
        # 000001 涨 0.3% 换手 0.5 amount=0.5: 低 amount+低 turn → 低分
        #   chg_score=0, base = 0.5+1.5+0+13.33 = 15.33
        # 000002 涨 0.3% 换手 25 amount=50: 高 amount+高 turn → 高分
        #   chg_score=0, base = 30+30+0+13.33 = 73.33
        # 000003 涨 0.3% 换手 0.5 amount=50: 高 amount 但 turn<1 (out plan_b)
        #   chg_score=0, base = 30+1.5+0+13.33 = 44.83 < 60
        stocks = [
            _mk_stock("000001", chg=0.3, turnover=0.5, amount=0.5),     # 低分
            _mk_stock("000002", chg=0.3, turnover=25.0, amount=50.0),   # 高分
            _mk_stock("000003", chg=0.3, turnover=0.5, amount=50.0),    # 中分 (< 60)
        ]
        final, stats = pk.filter_stocks(stocks, config=PAYLOAD)
        self.assertEqual(stats["plan_used"], "C")
        codes = [s["code"] for s in final]
        # 000001 + 000003 应被过滤 (score < 60)
        self.assertNotIn("000001", codes)
        self.assertNotIn("000003", codes)
        # 000002 应保留 (score >= 60)
        self.assertIn("000002", codes)
        for s in final:
            self.assertGreaterEqual(s["score"], 60.0)
        print(f"  ✓ plan_c 兜底过滤后 final={len(final)} 只, 全部 score>=60, 低分被剔除")


# ─────────── D: stage_pick 写 picks 表 ───────────

class TestStagePickWritesPicksTable(unittest.TestCase):
    """v3.1: stage_pick 末尾 INSERT INTO picks"""

    def test_record_picks_basic(self):
        """record_picks 写库 OK"""
        from stock_trading_agent.engine.paper_trader import record_picks, get_db
        from datetime import date as _date
        # 先清理今日 picks
        conn = get_db()
        conn.execute("DELETE FROM picks WHERE pick_date=?", (_date.today().isoformat(),))
        conn.commit()
        # 写 2 只
        stocks = [
            {"code": "603063", "name": "禾望电气", "price": 51.5, "prev_close": 49.3,
             "chg_pct": 4.5, "turnover": 5.6, "amplitude": 6.0, "score": 75.0,
             "sector": "光伏设备", "in_theme": False},
            {"code": "000001", "name": "平安银行", "price": 12.0, "prev_close": 11.8,
             "chg_pct": 1.7, "turnover": 1.0, "amplitude": 3.0, "score": 62.0,
             "sector": "银行", "in_theme": True},
        ]
        n = record_picks(_date.today().isoformat(), stocks, plan_used="A",
                         market_env_score=62, market_env_level="偏强")
        self.assertEqual(n, 2)
        rows = conn.execute("SELECT code, name, score, plan_used, market_env_score FROM picks WHERE pick_date=?",
                            (_date.today().isoformat(),)).fetchall()
        self.assertEqual(len(rows), 2)
        codes = {r["code"] for r in rows}
        self.assertEqual(codes, {"603063", "000001"})
        print(f"  ✓ record_picks 写 2 只 OK, picks 表非空")

    def test_stage_pick_invokes_record_picks(self):
        """stage_pick 跑完调 record_picks (mock 校验调用)"""
        from stock_trading_agent.agent import stages
        with patch.object(stages, "pick") as mock_pick, \
             patch.object(stages, "open_positions", return_value=0), \
             patch.object(stages, "is_trading_day", return_value=True), \
             patch.object(stages, "record_picks") as mock_record, \
             patch("stock_trading_agent.feishu.pusher.push_pick"), \
             patch("stock_trading_agent.feishu.pusher.push_empty_day"):
            mock_pick.return_value = {
                "date": "2026-06-17",
                "plan_used": "A",
                "market_env": {"score": 62, "level": "偏强"},
                "filtered_stocks": [
                    {"code": "603063", "name": "禾望电气", "score": 75.0,
                     "sector": "光伏设备", "in_theme": False}
                ],
                "stats": {"plan_a_count": 1, "plan_b_count": 0, "plan_used": "A"},
            }
            stages.stage_pick()
            self.assertTrue(mock_record.called, "stage_pick 应该调 record_picks")
            args, kwargs = mock_record.call_args
            self.assertEqual(kwargs["plan_used"], "A")
            self.assertEqual(kwargs["pick_date"], "2026-06-17")
            self.assertEqual(kwargs["market_env_score"], 62)
            print(f"  ✓ stage_pick 调 record_picks OK (plan=A, date=2026-06-17)")





# ─────────── F: freeform 路径 keyword_fallback 救场 ───────────

class TestFreeformKeywordRescue(unittest.TestCase):
    """v12.A.4.c: LLM 不选 tool → 先尝试 keyword_fallback 救场 (治"今日选股"空响应)"""

    def test_keyword_fallback_rescues_today_picks(self):
        """LLM 返回空 content + '今日选股' → 走 keyword_fallback 调 get_picks"""
        from unittest.mock import patch, MagicMock
        from stock_trading_agent.llm import tool_use

        # Mock chat_with_tools: 返 ok=True 但 tool_calls=[]
        mock_resp = {
            "ok": True, "content": "", "tool_calls": [],
            "latency_ms": 100, "error": None,
        }
        with patch.object(tool_use, "chat_with_tools", return_value=mock_resp), \
             patch.object(tool_use, "_empty_response_fallback", return_value="兜底"), \
             patch("stock_trading_agent.engine.skills.call_skill") as mock_call:
            mock_call.return_value = {
                "ok": True,
                "card": {"msg_type": "text", "content": {"text": "今日选股卡片"}},
                "raw": {"count": 0, "items": []},
            }
            result = tool_use.dispatch("今日选股", chat_id="test_chat_1")
            # 应该走 keyword_fallback 救场 (path=llm_tool_rescued)
            self.assertEqual(result["path"], "llm_tool_rescued")
            self.assertIn("tool_calls", result)
            # call_skill 至少被调一次 (get_picks)
            self.assertTrue(mock_call.called)
            print(f"  ✓ LLM 不选 tool → keyword_fallback 救场 get_picks OK")

    def test_no_keyword_match_falls_through_to_freeform(self):
        """LLM 空 + 无关键词命中 → 走 freeform 兜底 (老行为保留)"""
        from unittest.mock import patch
        from stock_trading_agent.llm import tool_use

        mock_resp = {
            "ok": True, "content": "", "tool_calls": [],
            "latency_ms": 100, "error": None,
        }
        with patch.object(tool_use, "chat_with_tools", return_value=mock_resp), \
             patch.object(tool_use, "_empty_response_fallback", return_value="自由回答兜底"), \
             patch("stock_trading_agent.engine.skills.call_skill") as mock_call:
            result = tool_use.dispatch("随便聊聊", chat_id="test_chat_2")
            # 无关键词命中, 走 freeform empty 兜底
            self.assertIn(result["path"], ("llm_freeform_empty",))
            self.assertEqual(result["card"]["content"]["text"], "自由回答兜底")
            # call_skill 不该被调
            self.assertFalse(mock_call.called)
            print(f"  ✓ 无关键词命中 → 走 freeform 兜底 (老行为保留)")


# ─────────── runner (放在最末, 让所有 class 都已定义) ───────────

def _run_all() -> None:
    """直接 python 执行时的入口"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    _run_all()
