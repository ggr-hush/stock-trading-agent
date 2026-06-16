"""v12.A.4.c — 启动预热缓存层测试

覆盖:
  - read_cache / write_cache / cache_exists 基础 CRUD
  - 跨天自动失效 (文件名带日期)
  - warm_up 调 Tushare + 写两个缓存
  - 失败不抛异常 (graceful degradation)
"""
import json
import os
import sys
import unittest
from datetime import date as _date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_trading_agent.engine import cache as _cache


class TestReadWriteCache(unittest.TestCase):
    """基础 CRUD"""

    def setUp(self):
        # 清理所有可能的旧文件 (test_key / stock_basic / daily)
        d = _date.today()
        for name in ("test_key", "stock_basic", "daily"):
            p = _cache._path_for(name, d)
            if p.exists():
                p.unlink()

    def test_write_and_read(self):
        data = [{"a": 1}, {"a": 2}]
        _cache.write_cache("test_key", data)
        out = _cache.read_cache("test_key")
        self.assertEqual(out, data)

    def test_cache_exists(self):
        self.assertFalse(_cache.cache_exists("test_key"))
        _cache.write_cache("test_key", [1, 2, 3])
        self.assertTrue(_cache.cache_exists("test_key"))

    def test_read_missing_returns_none(self):
        out = _cache.read_cache("definitely_not_exists_key")
        self.assertIsNone(out)

    def test_文件名带日期(self):
        """data/cache/<name>_<YYYYMMDD>.json"""
        d = _date.today()
        expected = _cache.CACHE_DIR / f"test_key_{d.strftime('%Y%m%d')}.json"
        _cache.write_cache("test_key", [1])
        self.assertTrue(expected.exists())

    def test_跨天读不命中(self):
        """今天的缓存不能读明天的 (文件名带日期, 明天没文件)"""
        _cache.write_cache("test_key", [1])
        # 读明天 (ref_date=明天) → 找不到 → None
        from datetime import timedelta
        tomorrow = _date.today() + timedelta(days=1)
        out = _cache.read_cache("test_key", ref_date=tomorrow)
        self.assertIsNone(out)


class TestWarmUp(unittest.TestCase):
    """warm_up 调 Tushare + 写两个缓存"""

    def setUp(self):
        d = _date.today()
        for name in ("test_key", "stock_basic", "daily"):
            p = _cache._path_for(name, d)
            if p.exists():
                p.unlink()

    def test_全通_写两个缓存(self):
        mock_pro = MagicMock()
        mock_pro.stock_basic.return_value = pd.DataFrame({
            "ts_code": ["000001.SZ", "600000.SH"],
            "name": ["平安银行", "浦发银行"],
            "industry": ["银行", "银行"],
        })
        mock_pro.daily.return_value = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "name": ["平安银行"],
            "close": [10.0],
            "pre_close": [9.5],
            "pct_chg": [5.0],
            "vol": [1000000.0],
            "amount": [50000.0],
        })
        with patch("stock_trading_agent.engine.tushare_client.get_pro", return_value=mock_pro):
            r = _cache.warm_up()
        self.assertTrue(r["stock_basic"])
        self.assertTrue(r["daily"])
        self.assertEqual(r["stock_basic_count"], 2)
        self.assertEqual(r["daily_count"], 1)
        # 缓存写成功
        self.assertTrue(_cache.cache_exists("stock_basic"))
        self.assertTrue(_cache.cache_exists("daily"))

    def test_降级_get_pro_失败(self):
        """get_pro 抛异常 → 返 errors, 不抛"""
        with patch("stock_trading_agent.engine.tushare_client.get_pro", side_effect=RuntimeError("token 无效")):
            r = _cache.warm_up()
        self.assertFalse(r["stock_basic"])
        self.assertFalse(r["daily"])
        self.assertTrue(len(r["errors"]) > 0)
        self.assertIn("token 无效", r["errors"][0])

    def test_降级_daily_拉不到_不影响_stock_basic(self):
        """daily 拉空 + stock_basic 拉成功 → 半成功"""
        mock_pro = MagicMock()
        mock_pro.stock_basic.return_value = pd.DataFrame({
            "ts_code": ["000001.SZ"], "name": ["平安银行"], "industry": ["银行"],
        })
        mock_pro.daily.return_value = pd.DataFrame()  # 拉空
        with patch("stock_trading_agent.engine.tushare_client.get_pro", return_value=mock_pro):
            r = _cache.warm_up()
        self.assertTrue(r["stock_basic"])
        self.assertFalse(r["daily"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
