"""tushare_client.py — v12.A.4.c Tushare 代理薄封装

全量数据源统一走 Tushare 代理 (TUSHARE_PROXY_URL 从 .env 读, 默认 null),
替代旧的东方财富 push2 / push2delay / 腾讯 qt.gtimg.cn / 新浪 hq.sinajs.cn 抓网页路径。

⚠️ 代理地址属敏感信息, 不在代码里硬编码; .env 必填, 否则启动时报错。

设计原则:
  - 单例 pro 实例 (避免重复握手 + 限流)
  - 凭据从 .env (TUSHARE_TOKEN + TUSHARE_PROXY_URL) 走 load_env() 拿
  - ts_code 格式统一: 上交所 .SH / 深交所 .SZ (Tushare 原生)
  - 项目内常用 code 格式: 'sh600000' / 'sz000001' / '600000' / '000001' 兼容
  - 所有 fetch 函数失败返空 (DataFrame 空 / 字段缺失), 不抛异常 (跟旧 fetch_* 风格一致)
  - 错误处理: 走 sys.stderr 打 warning, 上层安全降级

接口对应:
  get_market_env        → index_daily    (大盘指数)
  fetch_realtime_quote  → daily_basic    (个股实时, PE/PB/换手/市值)
  fetch_stock_kline     → daily + pro_bar(个股 K 线, 治'周五行情')
  get_all_stocks        → daily 全市场   (TOP200 改排序)
  get_stock_sectors     → stock_basic    (个股行业, 改 industry 字段)
  get_hot_sectors       → sector         (板块排行, 走概念指数)
  is_trading_day        → trade_cal      (交易日历 + 调休补班)
"""
from __future__ import annotations

import sys
import time
from typing import Any, Optional

# pandas 在 tushare 依赖里, 这里只 type hint
import pandas as pd  # type: ignore

# ─────────── 凭据加载 ───────────
_TOKEN: Optional[str] = None
_PROXY_URL: Optional[str] = None
_PRO = None  # 懒加载单例


def _load_token() -> str:
    """从 .env 拿 TUSHARE_TOKEN, 拿不到报错"""
    global _TOKEN
    if _TOKEN is not None:
        return _TOKEN
    try:
        from stock_trading_agent.engine.data_fetcher import load_env
        env = load_env()
    except Exception:
        env = {}
    token = env.get("TUSHARE_TOKEN")
    if not token or token.startswith("<"):
        raise RuntimeError(
            "TUSHARE_TOKEN 未配置或为占位符, 请在 .env 里设置: "
            "TUSHARE_TOKEN=<your-token>"
        )
    _TOKEN = token
    return _TOKEN


def _load_proxy_url() -> str:
    """从 .env 拿 TUSHARE_PROXY_URL, 默认值"""
    global _PROXY_URL
    if _PROXY_URL is not None:
        return _PROXY_URL
    try:
        from stock_trading_agent.engine.data_fetcher import load_env
        env = load_env()
    except Exception:
        env = {}
    _PROXY_URL = env.get("TUSHARE_PROXY_URL")
    if not _PROXY_URL or _PROXY_URL.startswith("<"):
        raise RuntimeError(
            "TUSHARE_PROXY_URL 未配置或为占位符, 请在 .env 里设置: "
            "TUSHARE_PROXY_URL=<your-proxy-url>"
        )
    return _PROXY_URL


def get_pro():
    """拿 Tushare pro 单例 (懒加载 + 替换 _DataApi__http_url 走代理)

    用法:
        from stock_trading_agent.engine.tushare_client import get_pro
        pro = get_pro()
        df = pro.stock_basic(list_status='L')
    """
    global _PRO
    if _PRO is not None:
        return _PRO
    import tushare as ts  # type: ignore
    token = _load_token()
    proxy = _load_proxy_url()
    pro = ts.pro_api(token)
    # 走代理 (官方文档标准做法, name-mangle 改私有属性)
    pro._DataApi__http_url = proxy
    _PRO = pro
    return _PRO


def reset_pro() -> None:
    """重置 pro 单例 (测试 / token 轮换时用)"""
    global _PRO, _TOKEN, _PROXY_URL
    _PRO = None
    _TOKEN = None
    _PROXY_URL = None


# ─────────── ts_code 转换 ───────────
# Tushare 用 '600000.SH' / '000001.SZ'
# 项目里用 'sh600000' / 'sz000001' / '600000' / '000001' / '000001.SH' 都见过


def to_ts_code(code: str) -> str:
    """'600000' / 'sh600000' / '600000.SH' → '600000.SH'

    指数特殊: 'sh000001' (上证) / 'sh000688' (科创 50) → .SH
              'sz399006' (创业板指) / 'sz399001' (深证成指) → .SZ
    股票:  6/9 开头  → .SH
           0/3 开头  → .SZ
           4 开头    → .BJ (北交所)
    已经是 ts_code 格式 (含 .) → 原样返
    """
    if not code:
        return code
    c = code.strip().lower()
    # 已经含 . (ts_code 格式)
    if "." in c and (c.endswith(".sh") or c.endswith(".sz") or c.endswith(".bj")):
        return code.strip().upper()
    # 记录原始前缀, 决定交易所
    prefix = ""
    if c.startswith(("sh", "sz", "bj")):
        prefix = c[:2]
        c = c[2:]
    if not c.isdigit():
        return code  # 非数字不转换, 让上层报错
    # 指数判断: 前缀显式 sh/sz → 按前缀 (000xxx/399xxx 都是指数, 不会跟股票冲突)
    if prefix == "sh":
        return f"{c}.SH"
    if prefix == "sz":
        return f"{c}.SZ"
    if prefix == "bj":
        return f"{c}.BJ"
    # 没前缀: 股票规则
    if c[0] in ("6", "9"):
        return f"{c}.SH"
    if c[0] in ("0", "3"):
        return f"{c}.SZ"
    if c[0] in ("4", "8"):  # 北交所 (4/8 开头)
        return f"{c}.BJ"
    return code


def from_ts_code(ts_code: str) -> str:
    """'600000.SH' → 'sh600000' (项目内原格式)

    主要给 is_trading_day 之类需要跟原 code 互转的地方用
    """
    if not ts_code or "." not in ts_code:
        return ts_code
    code, market = ts_code.split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market.upper(), "")
    return f"{prefix}{code}" if prefix else ts_code


# ─────────── 通用 fetch 封装 ───────────


def _safe_df(api_call, *args, label: str = "", **kwargs) -> pd.DataFrame:
    """包一层 try/except + log, 失败返空 DataFrame"""
    try:
        pro = get_pro()
        df = api_call(pro, *args, **kwargs)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] {label} 失败: {e}", file=sys.stderr)
        return pd.DataFrame()


def df_to_dicts(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → list[dict], 处理 NaN (None) + 时间戳转 str"""
    if df is None or df.empty:
        return []
    out = df.copy()
    # 时间戳列转 str
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y%m%d")
    # NaN / NaT → None (json 友好)
    out = out.replace({float("nan"): None, pd.NaT: None})
    return out.to_dict(orient="records")


def rate_limit_sleep(seconds: float = 0.1) -> None:
    """限流 sleep (Tushare 200 次/分钟)"""
    time.sleep(seconds)


# ─────────── 自检 ───────────


def health_check() -> dict[str, Any]:
    """连通性 + 凭据 + 代理全检

    返 {ok, pro_ok, stock_basic_ok, sample_ts_code, error}
    """
    result: dict[str, Any] = {"ok": False, "pro_ok": False, "stock_basic_ok": False, "error": None}
    try:
        pro = get_pro()
        result["pro_ok"] = True
        df = pro.stock_basic(list_status="L", limit=2, fields="ts_code,name,industry")
        if not df.empty:
            result["stock_basic_ok"] = True
            row = df.iloc[0].to_dict()
            result["sample_ts_code"] = row.get("ts_code")
        result["ok"] = result["pro_ok"] and result["stock_basic_ok"]
    except Exception as e:
        result["error"] = str(e)
    return result


if __name__ == "__main__":
    # 单独跑: python -m stock_trading_agent.engine.tushare_client
    import json
    h = health_check()
    print(json.dumps(h, ensure_ascii=False, indent=2))
