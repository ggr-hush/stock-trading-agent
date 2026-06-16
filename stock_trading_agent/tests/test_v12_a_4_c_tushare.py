"""v12.A.4.c — Tushare 客户端 + ts_code 转换 + 单例测试

本测试不连外网, 全部 mock tushare SDK.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# 确保可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_trading_agent.engine import tushare_client as tc


class TestToTsCode(unittest.TestCase):
    """ts_code 转换 (项目内常见 4 种格式 → Tushare 标准 .SH/.SZ/.BJ)"""

    def test_股票_6开头_沪市(self):
        self.assertEqual(tc.to_ts_code("600000"), "600000.SH")
        self.assertEqual(tc.to_ts_code("sh600000"), "600000.SH")
        self.assertEqual(tc.to_ts_code("600000.SH"), "600000.SH")

    def test_股票_9开头_沪市B股(self):
        self.assertEqual(tc.to_ts_code("900901"), "900901.SH")
        self.assertEqual(tc.to_ts_code("sh900901"), "900901.SH")

    def test_股票_0开头_深市主板(self):
        self.assertEqual(tc.to_ts_code("000001"), "000001.SZ")
        self.assertEqual(tc.to_ts_code("sz000001"), "000001.SZ")

    def test_股票_3开头_创业板(self):
        self.assertEqual(tc.to_ts_code("300750"), "300750.SZ")

    def test_股票_6开头_科创板(self):
        self.assertEqual(tc.to_ts_code("688981"), "688981.SH")

    def test_股票_8开头_北交所(self):
        self.assertEqual(tc.to_ts_code("830799"), "830799.BJ")
        self.assertEqual(tc.to_ts_code("430047"), "430047.BJ")

    def test_指数_sh前缀(self):
        self.assertEqual(tc.to_ts_code("sh000001"), "000001.SH")  # 上证指数
        self.assertEqual(tc.to_ts_code("sh000688"), "000688.SH")  # 科创 50
        self.assertEqual(tc.to_ts_code("sh000300"), "000300.SH")  # 沪深 300

    def test_指数_sz前缀(self):
        self.assertEqual(tc.to_ts_code("sz399006"), "399006.SZ")  # 创业板指
        self.assertEqual(tc.to_ts_code("sz399001"), "399001.SZ")  # 深证成指

    def test_空和非法输入(self):
        self.assertEqual(tc.to_ts_code(""), "")
        self.assertEqual(tc.to_ts_code("invalid"), "invalid")  # 非数字原样返

    def test_大小写不敏感(self):
        self.assertEqual(tc.to_ts_code("SH600000"), "600000.SH")
        self.assertEqual(tc.to_ts_code("Sh600000"), "600000.SH")


class TestFromTsCode(unittest.TestCase):
    """Tushare ts_code → 项目内原格式"""

    def test_股票(self):
        self.assertEqual(tc.from_ts_code("600000.SH"), "sh600000")
        self.assertEqual(tc.from_ts_code("000001.SZ"), "sz000001")
        self.assertEqual(tc.from_ts_code("830799.BJ"), "bj830799")

    def test_指数(self):
        self.assertEqual(tc.from_ts_code("000001.SH"), "sh000001")
        self.assertEqual(tc.from_ts_code("399006.SZ"), "sz399006")

    def test_原样返(self):
        self.assertEqual(tc.from_ts_code("invalid"), "invalid")
        self.assertEqual(tc.from_ts_code(""), "")


class TestGetProSingleton(unittest.TestCase):
    """pro 单例 + 代理 URL 替换"""

    def setUp(self):
        tc.reset_pro()

    def tearDown(self):
        tc.reset_pro()

    def test_get_pro_返回单例(self):
        """多次 get_pro() 返同一个对象"""
        with patch("tushare.pro_api") as mock_pro_api:
            mock_pro = MagicMock()
            mock_pro_api.return_value = mock_pro
            p1 = tc.get_pro()
            p2 = tc.get_pro()
            self.assertIs(p1, p2)
            # pro_api 只调一次
            self.assertEqual(mock_pro_api.call_count, 1)

    def test_get_pro_替换代理url(self):
        """必须把 _DataApi__http_url 改成 TUSHARE_PROXY_URL"""
        with patch("tushare.pro_api") as mock_pro_api:
            mock_pro = MagicMock()
            mock_pro._DataApi__http_url = "https://api.tushare.pro"  # 模拟原始 URL
            mock_pro_api.return_value = mock_pro
            p = tc.get_pro()
            # 验证 name-mangle 后属性被改
            from stock_trading_agent.engine.tushare_client import _PROXY_URL
            self.assertEqual(p._DataApi__http_url, _PROXY_URL)

    def test_get_pro_无token报错(self):
        """TUSHARE_TOKEN 缺失时应该 raise"""
        with patch.dict(os.environ, {}, clear=False):
            # 强制 load_env 拿不到
            with patch.object(tc, "_load_token", side_effect=RuntimeError("TUSHARE_TOKEN 未配置")):
                with self.assertRaises(RuntimeError) as ctx:
                    tc.get_pro()
                self.assertIn("TUSHARE_TOKEN", str(ctx.exception))

    def test_reset_pro清状态(self):
        """reset_pro 后下次 get_pro 重新实例化"""
        mock_instances = [MagicMock(name=f"pro_{i}") for i in range(2)]
        with patch("tushare.pro_api", side_effect=mock_instances) as mock_pro_api:
            p1 = tc.get_pro()
            tc.reset_pro()
            p2 = tc.get_pro()
            self.assertIsNot(p1, p2)
            self.assertIs(p1, mock_instances[0])
            self.assertIs(p2, mock_instances[1])
            self.assertEqual(mock_pro_api.call_count, 2)


class TestSafeDf(unittest.TestCase):
    """_safe_df 包 try/except + 返空 DataFrame"""

    def setUp(self):
        tc.reset_pro()

    def tearDown(self):
        tc.reset_pro()

    def test_正常返_dataframe(self):
        import pandas as pd
        fake_df = pd.DataFrame({"a": [1, 2]})
        api_call = MagicMock(return_value=fake_df)
        with patch.object(tc, "get_pro", return_value=MagicMock()):
            result = tc._safe_df(api_call, "arg1", label="test_call")
        self.assertEqual(len(result), 2)

    def test_异常返空_dataframe(self):
        api_call = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(tc, "get_pro", return_value=MagicMock()):
            result = tc._safe_df(api_call, label="will_fail")
        import pandas as pd
        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    def test_返非dataframe_转空(self):
        api_call = MagicMock(return_value="not a df")
        with patch.object(tc, "get_pro", return_value=MagicMock()):
            result = tc._safe_df(api_call, label="returns_str")
        import pandas as pd
        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)


class TestDfToDicts(unittest.TestCase):
    """DataFrame → list[dict]"""

    def test_空_dataframe_返空列表(self):
        import pandas as pd
        self.assertEqual(tc.df_to_dicts(pd.DataFrame()), [])
        self.assertEqual(tc.df_to_dicts(None), [])

    def test_正常转换_nan变none(self):
        import pandas as pd
        df = pd.DataFrame({"a": [1, None], "b": ["x", None]})
        out = tc.df_to_dicts(df)
        self.assertEqual(out, [{"a": 1, "b": "x"}, {"a": None, "b": None}])

    def test_时间戳列转字符串(self):
        import pandas as pd
        df = pd.DataFrame({"trade_date": pd.to_datetime(["20260616", "20260615"])})
        out = tc.df_to_dicts(df)
        self.assertEqual(out[0]["trade_date"], "20260616")


class TestHealthCheck(unittest.TestCase):
    """health_check 集成 (mock 整个 pro)"""

    def setUp(self):
        tc.reset_pro()

    def tearDown(self):
        tc.reset_pro()

    def test_全通(self):
        import pandas as pd
        mock_pro = MagicMock()
        mock_pro.stock_basic.return_value = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "name": ["平安银行"],
            "industry": ["银行"],
        })
        with patch.object(tc, "get_pro", return_value=mock_pro):
            h = tc.health_check()
        self.assertTrue(h["ok"])
        self.assertTrue(h["pro_ok"])
        self.assertTrue(h["stock_basic_ok"])
        self.assertEqual(h["sample_ts_code"], "000001.SZ")
        self.assertIsNone(h["error"])

    def test_pro抛异常(self):
        with patch.object(tc, "get_pro", side_effect=RuntimeError("token 无效")):
            h = tc.health_check()
        self.assertFalse(h["ok"])
        self.assertFalse(h["pro_ok"])
        self.assertIn("token 无效", h["error"])

    def test_stock_basic_空(self):
        import pandas as pd
        mock_pro = MagicMock()
        mock_pro.stock_basic.return_value = pd.DataFrame()
        with patch.object(tc, "get_pro", return_value=mock_pro):
            h = tc.health_check()
        self.assertFalse(h["ok"])
        self.assertTrue(h["pro_ok"])
        self.assertFalse(h["stock_basic_ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
