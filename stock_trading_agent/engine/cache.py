"""cache.py — v12.A.4.c 启动预热缓存层

设计:
  - 文件缓存, 路径 data/cache/<name>_<date>.json
  - 跨天自动失效 (文件名带日期)
  - 预热在 agent start 启动时一次性完成
  - 业务函数 (get_all_stocks 等) 内部 lazy load, 缓存命中直接返
  - 写入时支持同步锁避免并发写撞车 (agent 单进程, 写并发概率低, 主要是防御)

缓存 key:
  stock_basic_<YYYYMMDD>.json  — 行业 + name 一次拉, ~5000 行, 启动预热
  daily_<YYYYMMDD>.json        — 全市场当日 daily 行情, ~5400 行, 启动预热 + 收盘后刷
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import date as _date
from pathlib import Path
from typing import Any, Optional

CACHE_DIR = Path("data") / "cache"
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    """每个缓存名一个锁, 避免并发写撞车"""
    with _LOCKS_GUARD:
        if name not in _WRITE_LOCKS:
            _WRITE_LOCKS[name] = threading.Lock()
        return _WRITE_LOCKS[name]


def _path_for(name: str, ref_date: Optional[_date] = None) -> Path:
    """data/cache/<name>_<YYYYMMDD>.json"""
    d = ref_date or _date.today()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}_{d.strftime('%Y%m%d')}.json"


def read_cache(name: str, ref_date: Optional[_date] = None) -> Optional[Any]:
    """读缓存, 命中返内容 (list[dict] / dict), 未命中或解析失败返 None"""
    path = _path_for(name, ref_date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[cache] 读 {path.name} 失败: {e}", file=sys.stderr)
        return None


def write_cache(name: str, data: Any, ref_date: Optional[_date] = None) -> Path:
    """写缓存, 返写入路径"""
    path = _path_for(name, ref_date)
    lock = _lock_for(name)
    with lock:
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[cache] 写 {path.name} 失败: {e}", file=sys.stderr)
    return path


def cache_exists(name: str, ref_date: Optional[_date] = None) -> bool:
    """缓存是否存在"""
    return _path_for(name, ref_date).exists()


def cache_age_days(name: str, ref_date: Optional[_date] = None) -> Optional[int]:
    """缓存距今天数, 不存在返 None"""
    path = _path_for(name, ref_date)
    if not path.exists():
        return None
    import os
    mtime = path.stat().st_mtime
    import time
    return (time.time() - mtime) / 86400


def warm_up() -> dict[str, Any]:
    """v12.A.4.c: 启动预热 (在 agent start 调一次)

    拉 2 个:
      1. stock_basic 行业表 (list[dict])
      2. daily 全市场当日 (list[dict])

    拉不到 / 失败: 返 {stock_basic: False, daily: False}, 不抛异常
    """
    from stock_trading_agent.engine.tushare_client import (
        get_pro, to_ts_code, df_to_dicts, rate_limit_sleep,
    )
    result = {"stock_basic": False, "daily": False, "errors": []}
    try:
        pro = get_pro()
    except Exception as e:
        result["errors"].append(f"get_pro 失败: {e}")
        return result

    # 1) stock_basic
    try:
        df = pro.stock_basic(
            list_status="L",
            fields="ts_code,name,industry,exchange,list_date",
        )
        items = df_to_dicts(df)
        if items:
            write_cache("stock_basic", items)
            result["stock_basic"] = True
            result["stock_basic_count"] = len(items)
        else:
            result["errors"].append("stock_basic 返空")
    except Exception as e:
        result["errors"].append(f"stock_basic 失败: {e}")
    rate_limit_sleep(0.1)

    # 2) daily (最近 1 个交易日)
    try:
        from datetime import timedelta
        end_d = _date.today()
        beg_d = end_d - timedelta(days=10)
        # 找最近有数据的 1 天 (T+1 兼容: 今天可能没数据, 试到第 5 天)
        df = None
        for back in range(0, 10):
            d = end_d - timedelta(days=back)
            ds = d.strftime("%Y%m%d")
            df_try = pro.daily(
                trade_date=ds,
                fields="ts_code,name,close,pre_close,pct_chg,vol,amount",
            )
            if df_try is not None and not df_try.empty:
                df = df_try
                result["daily_trade_date"] = ds
                break
            rate_limit_sleep(0.05)
        if df is not None and not df.empty:
            items = df_to_dicts(df)
            write_cache("daily", items, ref_date=end_d)
            result["daily"] = True
            result["daily_count"] = len(items)
        else:
            result["errors"].append("daily 最近 10 天都无数据")
    except Exception as e:
        result["errors"].append(f"daily 失败: {e}")

    return result


if __name__ == "__main__":
    # 单独跑: python -m stock_trading_agent.engine.cache
    import json
    print(json.dumps(warm_up(), ensure_ascii=False, indent=2))
