"""
data_fetcher.py — 行情数据抓取层
- 全市场成交额 TOP200 (东方财富 push2delay)
- 个股板块 (ulist.np)
- 热门板块排行
- 大盘环境 (腾讯 qt.gtimg.cn + ifzq.gtimg.cn)
- 凭据加载（mavis secret / ~/.hermes/.env 兜底）
"""
from __future__ import annotations

import json
import sys
from urllib.parse import urlencode
import os
import re
import subprocess
import time
from datetime import date as _date, timedelta  # v12.A.2: 别名防参数 shadow
from pathlib import Path
from typing import Any, Optional

import requests

# ─────────── 常量 ───────────

# 东方财富 push2delay 的 ut token (公开参数, 来自东财页面 JS, 与原 daily-stock-picker / daily-top100-stock-picker 一致)
# 大多数时候这个值够用, 东财极少轮换; 轮换时可设 FEISHU_SINA_UT env 覆盖, 或开 ut_auto_refresh
_DEFAULT_UT = "bd1d9ddb04089700cf9c27f6f7426281"

# 凭据加载
_ENV_CACHE: dict[str, str] = {}
_UT_CACHE: str | None = None  # 运行时 ut 缓存 (含自动刷新结果)


def load_env() -> dict[str, str]:
    """从 (1) 进程 env / (2) ~/.hermes/.env 加载凭据。返回 dict。"""
    global _ENV_CACHE
    if _ENV_CACHE:
        return _ENV_CACHE
    env: dict[str, str] = {}
    keys = (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_SINA_UT",
        "FEISHU_DASH_APP_TOKEN",
        "FEISHU_DASH_DASHBOARD_ID",
        "FEISHU_CHAT_ID",
        "FEISHU_BITABLE_WEBHOOK",
        "MINIMAX_API_KEY",  # v11: 修 LLM client 拿不到 .env 里的 key
    )
    for k in keys:
        v = os.environ.get(k)
        if v:
            env[k] = v
    # 兜底源 1: ~/.hermes/.env (跨项目共享)
    hermes_env = Path.home() / ".hermes" / ".env"
    # 兜底源 2: <project_root>/.env (项目本地, git 忽略)
    # 优先以"包含 data_fetcher.py 的包"反推项目根
    _pkg_root = Path(__file__).resolve().parent.parent.parent
    project_env = _pkg_root / ".env"
    for env_file in (hermes_env, project_env):
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            # 去掉可选引号
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            if k not in env:
                env[k] = v
    _ENV_CACHE = env
    return env


def _secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """读凭据；缺失且无 default → 抛错（避免静默回退到泄露值）。"""
    val = load_env().get(name) or os.environ.get(name)
    if val:
        return val
    if default is not None:
        return default
    raise RuntimeError(
        f"凭据 {name} 未设置；请 mavis secret create {name} --value=... 或 export {name}=..."
    )


def is_placeholder(v: Optional[str]) -> bool:
    """识别 .env 里的 <...> 占位符 (未替换的真值)

    check_env 用同样逻辑标 PLACEHOLDER 状态；pusher 分发器拿占位符当真值会发失败。
    """
    if not v:
        return True
    return v.startswith("<") and v.endswith(">")


def curl_get(url: str, referer: str = "https://data.eastmoney.com/") -> str:
    """HTTP GET: 先 curl（保留原行为），失败再 requests 兜底。返回 body 字符串。"""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    try:
        r = subprocess.run(
            [
                "curl", "-s", "-L", "--max-time", "25", "--retry", "3", "--retry-delay", "2",
                "-H", f"User-Agent: {ua}",
                "-H", f"Referer: {referer}",
                "-H", "Accept: application/json, text/plain, */*",
                "-H", "Accept-Language: zh-CN,zh;q=0.9",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.stdout and r.stdout.strip():
            return r.stdout
    except Exception:
        pass
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": ua,
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


# ─────────── 交易日工具 ───────────

# v12.A.4.c: A 股交易日历走 Tushare trade_cal + 本地缓存
# 缓存文件: data/trade_cal_<year>.json, 存全年 is_open=1 的日期 (含调休补班)
_TRADE_CAL_CACHE_DIR = Path("data") / "trade_cal"


def _load_trade_cal(year: int) -> set[str]:
    """从本地缓存读全年交易日历 (set of 'YYYY-MM-DD')

    缓存命中: 直接读 JSON
    缓存未命中 / 缓存为空: 调 Tushare trade_cal 拉全年, 写缓存
    拉取失败: 降级到 weekday<5 启发式
    """
    cache_path = _TRADE_CAL_CACHE_DIR / f"{year}.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if data:
                return set(data)
        except Exception:
            pass
    # 缓存未命中, 调 Tushare
    try:
        from stock_trading_agent.engine.tushare_client import _safe_df
        pro_df = _safe_df(
            lambda p: p.trade_cal(exchange="SSE", start_date=f"{year}0101", end_date=f"{year}1231",
                                  fields="cal_date,is_open"),
            label=f"trade_cal_{year}",
        )
        if pro_df is not None and not pro_df.empty:
            opens = pro_df[pro_df["is_open"] == 1]["cal_date"].astype(str).tolist()
            # cal_date 是 '20260101' → 转 '2026-01-01'
            opens = [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in opens]
            _TRADE_CAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(sorted(opens), ensure_ascii=False), encoding="utf-8")
            return set(opens)
    except Exception as e:
        print(f"[trade_cal] 拉取 {year} 失败, 降级 weekday 启发式: {e}", file=sys.stderr)
    # 降级: weekday < 5
    return {(_date(year, 1, 1) + timedelta(days=i)).isoformat()
            for i in range(366)
            if (_date(year, 1, 1) + timedelta(days=i)).year == year
            and (_date(year, 1, 1) + timedelta(days=i)).weekday() < 5}


def is_trading_day(ref_date: Optional[date] = None) -> bool:
    """判断 A 股交易日: 走 Tushare trade_cal (含调休补班)

    - 缓存命中 (data/trade_cal_<year>.json): 走缓存
    - 缓存未命中: 实时拉 Tushare, 写缓存
    - Tushare 挂: 降级 weekday<5 启发式
    """
    d = ref_date or _date.today()
    opens = _load_trade_cal(d.year)
    return d.isoformat() in opens


def fetch_realtime_quote(code: str, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """v12.A.4.c: 单票实时行情 (Tushare daily_basic + daily)

    返 {name, price, chg_pct, turnover, mktcap_yi, pe, pb, ...}
    失败 (代码错 / Tushare 挂 / 非交易时间) 返空 dict, 不抛异常。

    接口:
      daily        → close / pct_chg / amount
      daily_basic  → pe_ttm / pb / turnover_rate / total_mv
    单位:
      daily.amount  千元   → 转亿: amount/1e8
      total_mv      万元   → 转亿: total_mv/1e4
    """
    if not code or len(code) != 6 or not code.isdigit():
        return {}
    from stock_trading_agent.engine.tushare_client import (
        get_pro, to_ts_code, rate_limit_sleep,
    )
    ts_code = to_ts_code(code)
    if "." not in ts_code:
        return {}
    try:
        pro = get_pro()
        # 1) daily: 拿最新收盘 + 涨跌幅 + 成交额
        from datetime import date as _date_today
        today = _date_today.today()
        # 找最近 10 个交易日内有数据的那一天 (T+1 兼容)
        end_d = today.strftime("%Y%m%d")
        from datetime import timedelta
        start_d = (today - timedelta(days=10)).strftime("%Y%m%d")
        df_daily = pro.daily(ts_code=ts_code, start_date=start_d, end_date=end_d)
        if df_daily is None or df_daily.empty:
            return {}
        row = df_daily.sort_values("trade_date", ascending=False).iloc[0]
        trade_date = str(row["trade_date"])
        close = _safe_float(row.get("close"))
        pct_chg = _safe_float(row.get("pct_chg"))
        amount_k = _safe_float(row.get("amount"))  # 千元
        amount_yi = round(amount_k / 1e5, 2) if amount_k else None  # 千元→亿
        rate_limit_sleep(0.05)
        # 2) daily_basic: 拿 PE / PB / 换手 / 总市值
        df_basic = pro.daily_basic(
            ts_code=ts_code, trade_date=trade_date,
            fields="ts_code,trade_date,pe_ttm,pb,turnover_rate,total_mv,circ_mv"
        )
        pe = pb = turnover = total_mv_yi = circ_mv_yi = None
        if df_basic is not None and not df_basic.empty:
            b = df_basic.iloc[0]
            pe = _safe_float(b.get("pe_ttm"))
            pb = _safe_float(b.get("pb"))
            turnover = _safe_float(b.get("turnover_rate"))
            tmv = _safe_float(b.get("total_mv"))  # 万元
            cmv = _safe_float(b.get("circ_mv"))
            total_mv_yi = round(tmv / 1e4, 2) if tmv else None
            circ_mv_yi = round(cmv / 1e4, 2) if cmv else None
        rate_limit_sleep(0.05)
        # 3) stock_basic 拿 name
        df_name = pro.stock_basic(ts_code=ts_code, fields="ts_code,name,industry")
        name = industry = ""
        if df_name is not None and not df_name.empty:
            name = df_name.iloc[0].get("name", "")
            industry = df_name.iloc[0].get("industry", "")
        return {
            "code": code,
            "ts_code": ts_code,
            "name": name,
            "industry": industry,
            "trade_date": trade_date,
            "price": close,
            "chg_pct": pct_chg,
            "amount_yi": amount_yi,
            "turnover": turnover,
            "pe": pe,
            "pb": pb,
            "mktcap_yi": total_mv_yi,
            "circ_mv_yi": circ_mv_yi,
            "source": "tushare",
        }
    except Exception as e:
        print(f"[fetch_realtime_quote] {code} 失败: {e}", file=sys.stderr)
        return {}


def fetch_stock_kline(code: str, date: str | None = None,
                   config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """v12.A.4.c: 个股某日 K 线 (Tushare daily + pro_bar 复权)

    接口:
      daily     → 日线 OHLC + pct_chg + amount + vol
      pro_bar   → 前复权 (qfq) 收盘价
      stock_basic → name
    单位:
      daily.vol    手
      daily.amount 千元 → 转亿: amount/1e5

    date=None 或 'today' → 返最近 1 根 (今天/最近交易日)
    date='YYYY-MM-DD' → 拉 1 年日 K 精确匹配
    失败 (代码错/网络挂/节假日无数据) → 返空 dict
    """
    if not code or len(code) != 6 or not code.isdigit():
        return {}
    from stock_trading_agent.engine.tushare_client import (
        get_pro, to_ts_code, rate_limit_sleep,
    )
    ts_code = to_ts_code(code)
    if "." not in ts_code:
        return {}
    # v12.A.2 修: 形参 date (str) shadow import 的 date class → 用 _date alias
    end_date = date if (date and date != "today") else _date.today().isoformat()
    try:
        end_dt = _date.fromisoformat(end_date)
        beg_dt = end_dt - timedelta(days=365)
    except ValueError:
        return {}
    beg_str = beg_dt.strftime("%Y%m%d")
    end_str = end_dt.strftime("%Y%m%d")

    try:
        pro = get_pro()
        df = pro.daily(ts_code=ts_code, start_date=beg_str, end_date=end_str)
        if df is None or df.empty:
            return {}
        # 转日期格式匹配 'YYYY-MM-DD'
        df["_date"] = df["trade_date"].astype(str).apply(lambda x: f"{x[:4]}-{x[4:6]}-{x[6:8]}")
        # 1) 精确匹配
        matched = None
        if date and date != "today":
            hit = df[df["_date"] == end_date]
            if not hit.empty:
                matched = hit.iloc[0]
        # 2) 返最近一根
        if matched is None:
            df_sorted = df.sort_values("trade_date", ascending=False)
            matched = df_sorted.iloc[0]
        rate_limit_sleep(0.05)
        # name
        df_name = pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
        name = df_name.iloc[0]["name"] if (df_name is not None and not df_name.empty) else ""
        rate_limit_sleep(0.05)
        # 复权收盘价 (前复权)
        try:
            df_p = pro.pro_bar(ts_code=ts_code, adj="qfq",
                               start_date=str(matched["trade_date"]),
                               end_date=str(matched["trade_date"]),
                               fields="ts_code,trade_date,close")
            close_qfq = _safe_float(df_p.iloc[0]["close"]) if (df_p is not None and not df_p.empty) else None
        except Exception:
            close_qfq = None
        amount_k = _safe_float(matched.get("amount"))  # 千元
        amount_yi = round(amount_k / 1e5, 2) if amount_k else None
        # 振幅: Tushare daily.amplitude 经常 None, 自己用 (high-low)/pre_close 算
        high_v = _safe_float(matched.get("high"))
        low_v = _safe_float(matched.get("low"))
        pre_close_v = _safe_float(matched.get("pre_close"))
        if high_v and low_v and pre_close_v and pre_close_v > 0:
            amplitude = round((high_v - low_v) / pre_close_v * 100, 2)
        else:
            amplitude = _safe_float(matched.get("amplitude"))
        # 换手率: Tushare daily 不含, 走 daily_basic 拿
        turnover = None
        try:
            df_basic = pro.daily_basic(
                ts_code=ts_code, trade_date=str(matched["trade_date"]),
                fields="ts_code,turnover_rate",
            )
            if df_basic is not None and not df_basic.empty:
                turnover = _safe_float(df_basic.iloc[0].get("turnover_rate"))
        except Exception:
            pass
        rate_limit_sleep(0.05)
        return {
            "code": code,
            "ts_code": ts_code,
            "name": name,
            "date": matched["_date"],
            "open": _safe_float(matched.get("open")),
            "close": _safe_float(matched.get("close")),
            "close_qfq": close_qfq,
            "high": high_v,
            "low": low_v,
            "volume": _safe_float(matched.get("vol")),       # 手
            "amount_yi": amount_yi,
            "amplitude": amplitude,
            "chg_pct": _safe_float(matched.get("pct_chg")),
            "chg_amt": _safe_float(matched.get("change")),
            "turnover": turnover,
            "source": "tushare",
        }
    except Exception as e:
        print(f"[fetch_stock_kline] {code} 失败: {e}", file=sys.stderr)
        return {}


def _safe_float(v: Any) -> float | None:
    """东财接口数值字段可能是 '-' / '' / None, 安全转 float"""
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_all_stocks(config: dict[str, Any]) -> list[dict[str, Any]]:
    """v12.A.4.c: 全市场成交额 TOP200 (Tushare daily 缓存)

    流程:
      1) 读 data/cache/daily_<today>.json 缓存
      2) 缓存命中 → 按 amount 排序取 top 200
      3) 缓存未命中 → 调 Tushare daily (从 warm_up 已有, 这里容错) +
         按 amount 排 + 写缓存 + 返 top 200

    字段兼容 (旧东方财富接口名 → 新 Tushare):
      code           ← ts_code 去 .SH/.SZ
      name           ← name
      trade          ← close
      changepercent  ← pct_chg
      turnoverratio  ← vol 推算 (v12.A.4.c 简化: 跟 daily_basic 拿)
      mktcap         ← daily_basic total_mv (万元)
      amount         ← amount (千元, 旧接口是元, 这里都保留原单位, 旧调用方看)
      high / low     ← 暂用 close + pre_close 估算 (Tushare daily 没高低)
      settlement     ← pre_close
    """
    from stock_trading_agent.engine import cache as _cache
    # 1) 读缓存
    cached = _cache.read_cache("daily")
    if cached is not None:
        return _format_top200_from_daily(cached)
    # 2) 缓存未命中, 主动拉一次
    try:
        from stock_trading_agent.engine.tushare_client import get_pro, df_to_dicts, rate_limit_sleep
        pro = get_pro()
        from datetime import timedelta
        end_d = _date.today()
        items = None
        for back in range(0, 10):
            d = end_d - timedelta(days=back)
            ds = d.strftime("%Y%m%d")
            df_try = pro.daily(trade_date=ds,
                               fields="ts_code,name,close,pre_close,pct_chg,vol,amount")
            if df_try is not None and not df_try.empty:
                items = df_to_dicts(df_try)
                _cache.write_cache("daily", items)
                break
            rate_limit_sleep(0.05)
        if items is None:
            return []
        return _format_top200_from_daily(items)
    except Exception as e:
        print(f"[get_all_stocks] 拉取失败: {e}", file=sys.stderr)
        return []


def _format_top200_from_daily(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """daily list[dict] → 旧接口兼容的 TOP200

    排序: amount DESC, 取前 200
    字段映射: ts_code → code (去后缀)
    """
    out: list[dict[str, Any]] = []
    for it in items:
        try:
            ts_code = it.get("ts_code", "")
            code = ts_code.split(".")[0] if "." in ts_code else ts_code
            close = _safe_float(it.get("close")) or 0
            pre_close = _safe_float(it.get("pre_close")) or 0
            pct = _safe_float(it.get("pct_chg")) or 0
            amount = _safe_float(it.get("amount")) or 0  # 千元
            vol = _safe_float(it.get("vol")) or 0
            if close <= 0 or amount <= 0:
                continue
            out.append({
                "code": code,
                "name": it.get("name", ""),
                "trade": close,
                "changepercent": pct,
                "turnoverratio": 0.0,  # daily 不含, get_market_stocks 用 daily_basic 二次补
                "mktcap": 0.0,          # 同上
                "amount": amount * 1e3,  # 千元 → 元, 兼容旧单位
                "high": close,           # daily 没 high/low, 用 close 占位
                "low": close,
                "settlement": pre_close,
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["amount"], reverse=True)
    return out[:200]


def get_market_stocks(config: dict[str, Any]) -> list[dict[str, Any]]:
    """从全市场 TOP200 解析出统一字段的 stocks 列表

    排除 ST/退/停/N，排除价格或成交额为 0 的票
    计算振幅、近涨停标记
    """
    raw = get_all_stocks(config)
    hard = config["hard"]
    limit_up = hard["limit_up"]
    stocks: list[dict[str, Any]] = []
    for item in raw:
        name = item.get("name", "")
        if not name or any(k in name for k in ("ST", "退", "停", "N ")):
            continue
        price = float(item.get("trade", 0) or 0)
        chg_pct = float(item.get("changepercent", 0) or 0)
        turnover = float(item.get("turnoverratio", 0) or 0)
        total_mv_wan = float(item.get("mktcap", 0) or 0)
        amount = float(item.get("amount", 0) or 0)
        high = float(item.get("high", 0) or 0)
        low = float(item.get("low", 0) or 0)
        prev_close = float(item.get("settlement", 0) or 0)
        if price <= 0 or amount <= 0:
            continue

        amplitude = round((high - low) / low * 100, 1) if (high > low and low > 0) else 0.0
        limit_today = 1 if chg_pct >= limit_up else 0

        stocks.append({
            "code": item.get("code", ""),
            "name": name,
            "price": price,
            "prev_close": prev_close,
            "chg_pct": chg_pct,
            "turnover": turnover,
            "total_mv_yi": round(total_mv_wan / 10000, 1),
            "amount_yi": round(amount / 1e8, 2),
            "high": high,
            "low": low,
            "amplitude": amplitude,
            "limit_up_days": limit_today,
        })
    return stocks


# ─────────── 板块 ───────────

def get_stock_sectors(codes: list[str], config: dict[str, Any]) -> dict[str, str]:
    """v12.A.4.c: 批量查个股行业 (走 stock_basic 缓存)

    流程:
      1) 读 data/cache/stock_basic_<today>.json 缓存
      2) 缓存命中 → 内存 dict 查, O(1)
      3) 缓存未命中 → 调 Tushare stock_basic (从 warm_up) + 写缓存

    返: {code: industry}   ← 注意: 旧 key 是 sector, 现在叫 industry (Tushare 原生)
    """
    from stock_trading_agent.engine import cache as _cache
    cached = _cache.read_cache("stock_basic")
    if cached is None:
        # 缓存未命中, 拉一次
        try:
            from stock_trading_agent.engine.tushare_client import get_pro, df_to_dicts
            pro = get_pro()
            df = pro.stock_basic(list_status="L", fields="ts_code,industry")
            if df is not None and not df.empty:
                cached = df_to_dicts(df)
                _cache.write_cache("stock_basic", cached)
        except Exception as e:
            print(f"[get_stock_sectors] 拉取失败: {e}", file=sys.stderr)
            return {}
    if not cached:
        return {}
    # 建内存索引 ts_code -> industry, 然后查 code
    from stock_trading_agent.engine.tushare_client import to_ts_code
    sector_map: dict[str, str] = {}
    for it in cached:
        ts_code = it.get("ts_code", "")
        if not ts_code:
            continue
        industry = it.get("industry", "")
        if not industry:
            continue
        code = ts_code.split(".")[0]  # 600000.SH → 600000
        sector_map[code] = industry
    # 用户传 codes (项目格式) → 查
    out: dict[str, str] = {}
    for c in codes:
        # 支持 'sh600000' / 'sz000001' / '600000'
        code = c[2:] if c[:2] in ("sh", "sz", "bj") else c
        if code in sector_map:
            out[c] = sector_map[code]
    return out


def get_hot_sectors(config: dict[str, Any]) -> list[dict[str, Any]]:
    """热门板块排行 (东方财富 push2delay) — 行业 + 概念共 16 个

    返回: [{name, chg_pct, _type}, ...]
    """
    base = config["data_source"]["eastmoney_base"]
    sectors: list[dict[str, Any]] = []
    for fs, t in [("m:90+t:2", "industry"), ("m:90+t:3", "concept")]:
        url = (
            f"{base}/api/qt/clist/get"
            f"?pn=1&pz=8&po=1&np=1&fltt=2&invt=2&fid=f3"
            f"&fs={fs}"
            f"&fields=f12,f14,f3"
            f"&_={int(time.time())}"
        )
        raw = curl_get(url, referer="https://quote.eastmoney.com/")
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        for it in (d.get("data") or {}).get("diff", []) or []:
            try:
                sectors.append({
                    "name": it.get("f14", ""),
                    "chg_pct": float(it.get("f3", 0) or 0),
                    "_type": t,
                })
            except Exception:
                continue
    sectors.sort(key=lambda x: x["chg_pct"], reverse=True)
    return sectors[:16]


# ─────────── 大盘环境 ───────────

def get_market_env(config: dict[str, Any]) -> dict[str, Any]:
    """v12.A.4.c: 综合大盘环境评估 (Tushare index_daily)

    指数数据: Tushare index_daily 拉近 30 天, 取最后一行作为当日,
              取最后 5 根 close 算 MA5 (trend_bonus).

    返: {env_score, env_level, position_advice, position_ratio,
         market_type, flags, details: {weighted_chg, trend_bonus,
         vol_bonus, sh_amt_yi, index_data, data_source}}
    """
    env_cfg = config["env"]
    indices = env_cfg["indices"]
    vol_hi = env_cfg["vol_thresh_hi_yi"]
    vol_lo = env_cfg["vol_thresh_lo_yi"]
    pos_cfg = config["position"]
    data_source = "tushare"  # 切到 Tushare 后默认; 真没拉到再降级

    from stock_trading_agent.engine.tushare_client import (
        get_pro, to_ts_code, rate_limit_sleep,
    )
    pro = get_pro()
    # 拉近 30 天 (够算 MA5)
    end_d = _date.today().strftime("%Y%m%d")
    beg_d = (_date.today() - timedelta(days=30)).strftime("%Y%m%d")

    results: dict[str, dict[str, Any]] = {}
    index_data: dict[str, Any] = {}
    weighted_chg = 0.0
    trend_bonus = 0
    weight_total = 0.0

    try:
        for idx in indices:
            # config 里是 dict: {code, name, weight}, 兼容 tuple (旧版)
            if isinstance(idx, dict):
                code, name, w = idx["code"], idx["name"], idx["weight"]
            else:
                code, name, w = idx
            ts_code = to_ts_code(code)
            rate_limit_sleep(0.05)
            df = pro.index_daily(ts_code=ts_code, start_date=beg_d, end_date=end_d)
            if df is None or df.empty:
                continue
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            latest = df.iloc[0]
            close = _safe_float(latest.get("close"))
            pre_close = _safe_float(latest.get("pre_close"))
            amount_k = _safe_float(latest.get("amount"))  # 千元
            if close is None or pre_close is None or pre_close <= 0:
                continue
            chg = (close - pre_close) / pre_close * 100
            # MA5: 取最近 5 根的 close 平均
            closes_5 = [_safe_float(c) for c in df.head(5)["close"].tolist()]
            closes_5 = [c for c in closes_5 if c is not None]
            ma5 = sum(closes_5) / len(closes_5) if len(closes_5) >= 3 else None
            amt_yi = round(amount_k / 1e5, 0) if amount_k else 0  # 千元→亿 (1e5=1e3 元/1e8 亿)
            amt_yuan = (amount_k or 0) * 1e3  # 千元 → 元
            results[code] = {
                "name": name,
                "ts_code": ts_code,
                "prev": pre_close,
                "price": close,
                "chg": round(chg, 2),
                "amt": amt_yuan,
                "amt_yi": amt_yi,
            }
            index_data[name] = {
                "chg": chg,
                "price": close,
                "prev": pre_close,
            }
            if ma5 is not None:
                index_data[name]["ma5"] = round(ma5, 2)
                index_data[name]["above_ma5"] = close > ma5
                if close > ma5:
                    trend_bonus += 8
            weighted_chg += chg * w
            weight_total += w
    except Exception as e:
        print(f"[market_env] Tushare 拉取失败: {e}", file=sys.stderr)

    if not results:
        return {
            "env_score": 50,
            "env_level": "中性（数据缺失）",
            "position_advice": "数据源不可用, 仓位待人工判断",
            "position_ratio": 0.5,
            "market_type": "未知",
            "flags": ["data_missing"],
            "details": {
                "reason": "Tushare index_daily 无数据",
                "data_source": "none",
            },
        }
    if not results:  # catch 路径上也是 data_source=none
        data_source = "none"

    if weight_total > 0:
        weighted_chg /= weight_total
    weighted_chg = round(weighted_chg, 3)

    # 上证成交额 (亿元): results['sh000001']['amt'] 单位是元
    sh_amt_yi = results.get("sh000001", {}).get("amt", 0) / 1e8
    vol_bonus = 10 if sh_amt_yi > vol_hi else (5 if sh_amt_yi > vol_lo else 0)
    if "上证指数" in index_data:
        index_data["上证指数"]["amt_yi"] = round(sh_amt_yi, 0)

    base_score = max(0, min(50, 25 + weighted_chg * 5))
    total = max(0, min(100, int(base_score + trend_bonus + vol_bonus)))

        # 等级判定
    level, pos_ratio, pos_adv, mtype = "极差", 0.0, pos_cfg["empty"]["advice"], "熊市"
    for key in ("full", "heavy", "half", "light", "empty"):
        node = pos_cfg[key]
        if total >= node["score_min"]:
            level = "极差" if key == "empty" else (
                "差" if key == "light" else (
                    "偏弱" if key == "half" else (
                        "偏强" if key == "heavy" else "强"
                    )
                )
            )
            pos_ratio = node["ratio"]
            pos_adv = node["advice"]
            mtype = node["market"]
            break

    flags = ["can_trade"] if pos_ratio > 0 else ["no_trade"]

    return {
        "env_score": total,
        "env_level": level,
        "position_advice": pos_adv,
        "position_ratio": pos_ratio,
        "market_type": mtype,
        "flags": flags,
        "details": {
            "weighted_chg": weighted_chg,
            "trend_bonus": int(trend_bonus),
            "data_source": data_source,
            "vol_bonus": vol_bonus,
            "sh_amt_yi": round(sh_amt_yi, 0),
            "index_data": index_data,
        },
    }


# ─────────── 配置加载 ───────────
# v12.A.4.c 注: _CONFIG_PATH / _CONFIG_CACHE 初始化原本在 get_sina_ut 上面,
#               删 dead code 时一起删了, 这里补回.
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_CONFIG_CACHE: Optional[dict[str, Any]] = None


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """加载 config（YAML 优先，JSON 兜底；带缓存）

    优先读 config.yaml（人类可读），找不到 PyYAML 或 yaml 文件则读 config.json。
    调试时：path=Path("/path/to/config.yaml") 可强制重读。
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and path is None:
        return _CONFIG_CACHE
    p = path or _CONFIG_PATH
    cfg: dict[str, Any]
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml as _yaml  # noqa: PLC0415
            with open(p) as f:
                cfg = _yaml.safe_load(f)
        except ImportError:
            json_path = p.with_suffix(".json")
            if json_path.exists():
                import json as _json  # noqa: PLC0415
                cfg = _json.loads(json_path.read_text())
            else:
                raise RuntimeError(
                    f"PyYAML 未安装且 {json_path} 不存在；请 pip install pyyaml 或编辑 {json_path}"
                )
    else:
        import json as _json  # noqa: PLC0415
        with open(p) as f:
            cfg = _json.load(f)
    if path is None:
        _CONFIG_CACHE = cfg
    return cfg


def reload_config() -> dict[str, Any]:
    """强制重读 config.yaml（调参后调用）"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
    return load_config()
