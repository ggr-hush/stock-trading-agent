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
from urllib.parse import urlencode
import os
import re
import subprocess
import time
from datetime import date, timedelta
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


def get_sina_ut(allow_auto_refresh: bool = True) -> str:
    """东方财富 push2delay 的 ut 参数。

    解析顺序:
      1) 进程 env / .env 里显式设了 FEISHU_SINA_UT  → 用你的值 (最优先, 用于东财轮换后快速覆盖)
      2) 模块常量 _DEFAULT_UT (原 daily-stock-picker 一直用的硬编码值)
      3) 可选: 抓 https://data.eastmoney.com/ 页面里的 ut 字符串 (东财极少轮换, 默认尝试一次)

    注意: 这是公开 token, 不是 cookie/凭据, 正常不需要用户配。
    """
    global _UT_CACHE
    if _UT_CACHE is not None:
        return _UT_CACHE
    # 1) env 优先: 走 load_env() (含进程 env / ~/.hermes/.env / 项目 .env 三源)
    env_val = load_env().get("FEISHU_SINA_UT", "").strip()
    if env_val:
        _UT_CACHE = env_val
        return _UT_CACHE
    # 2) 硬编码默认
    _UT_CACHE = _DEFAULT_UT
    # 3) 自动刷新 (一次性, 失败静默回退默认)
    if allow_auto_refresh and os.environ.get("UT_AUTO_REFRESH", "1") != "0":
        fresh = _auto_fetch_ut()
        if fresh:
            _UT_CACHE = fresh
    return _UT_CACHE


def _auto_fetch_ut() -> str | None:
    """从东财主页 / 行情中心 JS 里抓 ut token, 失败返回 None。"""
    urls = [
        "https://data.eastmoney.com/",
        "https://quote.eastmoney.com/center/gridlist.html",
    ]
    # ut 的典型形态: 16-40 位 [a-f0-9], 偶尔夹 i (看历史).
    pattern = re.compile(r"""ut\s*[:=]\s*["']([a-f0-9i]{16,40})["']""", re.I)
    for u in urls:
        try:
            r = requests.get(
                u,
                timeout=6,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            if r.status_code != 200 or not r.text:
                continue
            m = pattern.search(r.text)
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


def reset_ut_cache() -> None:
    """测试 / 调参用: 清掉 ut 缓存, 下次调用会重新解析。"""
    global _UT_CACHE
    _UT_CACHE = None


# ─────────── HTTP ───────────

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

# v12.9.1: A 股 2026 法定节假日常量 (元旦 1 / 春节 7 / 清明 3 / 劳动 3 / 端午 3 / 中秋 3 / 国庆 7 = 27 天)
# 调休补班 (周末调成工作日) 暂不处理, 留 v12.10 接 tushare trade_cal
_A_SHARE_HOLIDAYS_2026: set[str] = {
    "2026-01-01", "2026-01-02", "2026-01-03",  # 元旦
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",  # 春节
    "2026-04-04", "2026-04-05", "2026-04-06",  # 清明
    "2026-05-01", "2026-05-02", "2026-05-03",  # 劳动
    "2026-06-19", "2026-06-20", "2026-06-21",  # 端午
    "2026-09-25", "2026-09-26", "2026-09-27",  # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆
}


def is_trading_day(ref_date: Optional[date] = None) -> bool:
    """判断 A 股交易日: weekday < 5 (Mon-Fri) 且不在法定节假日"""
    d = ref_date or date.today()
    if d.weekday() >= 5:
        return False
    if d.isoformat() in _A_SHARE_HOLIDAYS_2026:
        return False
    return True


def get_latest_trading_day(ref_date: Optional[date] = None) -> date:
    d = ref_date or date.today()
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d - timedelta(days=2)
    return d


def get_previous_trading_day(ref_date: Optional[date] = None) -> date:
    d = ref_date or date.today()
    if d.weekday() == 0:
        return d - timedelta(days=3)
    if d.weekday() in (5, 6):
        return d - timedelta(days=(d.weekday() - 4))
    return d - timedelta(days=1)


# ─────────── 全市场 TOP200 ───────────

def _build_secid(code: str) -> str:
    if code.startswith(("6", "9")):
        return "1." + code
    return "0." + code


def fetch_realtime_quote(code: str, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """v12.9.1: 单票实时行情 (东方财富 push2delay)

    返 {name, price, chg_pct, turnover, volume, sector, mktcap} 7 字段。
    失败 (非交易时间 / 网络挂 / 代码错) 返空 dict, 不抛异常。

    接口: push2delay 的 secid 单票查询
    字段: f12=代码 f14=名称 f2=最新价 f3=涨跌幅% f5=成交额 f6=换手率%
          f8=换手率(另一口径) f9=市盈率 f10=量比 f20=总市值 f21=流通市值
    """
    if not code or len(code) != 6 or not code.isdigit():
        return {}
    try:
        cfg = config or load_config()
        base = cfg["data_source"]["eastmoney_base"]
    except Exception:
        return {}
    secid = _build_secid(code)
    url = f"{base}/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f12,f14,f2,f3,f5,f6,f8,f10,f20,f21",
        "invt": "2",
        "fltt": "2",
        "_": str(int(time.time() * 1000)),
    }
    try:
        text = curl_get(url + "?" + urlencode(params))
        if not text:
            return {}
        import json as _json
        data = _json.loads(text)
        if not data or str(data.get("rc", "")) not in ("0", ""):
            return {}
        d = data.get("data") or {}
        if not d:
            return {}
        return {
            "code": d.get("f12", code),
            "name": d.get("f14", ""),
            "price": _safe_float(d.get("f2")),
            "chg_pct": _safe_float(d.get("f3")),
            "amount_yi": _safe_float(d.get("f5")),  # 成交额(亿, 原始是元)
            "turnover": _safe_float(d.get("f6")),    # 换手率%
            "volume_ratio": _safe_float(d.get("f10")),
            "mktcap_yi": _safe_float(d.get("f20")),  # 总市值(亿)
            "source": "东方财富实时",
        }
    except Exception:
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
    """全市场成交额 TOP200 (东方财富 push2delay)

    字段映射到原新浪接口兼容名: code/name/trade/changepercent/turnoverratio/mktcap/amount/high/low/settlement
    """
    base = config["data_source"]["eastmoney_base"]
    all_stocks: list[dict[str, Any]] = []
    for page in range(1, 3):
        url = (
            f"{base}/api/qt/clist/get"
            f"?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f6"
            f"&fs=m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23,m:1+t:A,m:0+t:7+f:!50,m:1+t:3+f:!50"
            f"&fields=f12,f14,f2,f3,f4,f5,f6,f8,f9,f10,f15,f16,f17,f18,f20,f23"
            f"&_={int(time.time())}"
        )
        raw = curl_get(url, referer="https://quote.eastmoney.com/")
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        diff = (d.get("data") or {}).get("diff") or []
        for it in diff:
            try:
                price = float(it.get("f2", 0) or 0)
                amount = float(it.get("f6", 0) or 0)
                if price <= 0 or amount <= 0:
                    continue
                all_stocks.append({
                    "code": it.get("f12", ""),
                    "name": it.get("f14", ""),
                    "trade": price,
                    "changepercent": float(it.get("f3", 0) or 0),
                    "turnoverratio": float(it.get("f8", 0) or 0),
                    "mktcap": float(it.get("f20", 0) or 0),       # 万
                    "amount": amount,                              # 元
                    "high": float(it.get("f15", 0) or 0),
                    "low": float(it.get("f16", 0) or 0),
                    "settlement": float(it.get("f4", 0) or 0),
                })
            except Exception:
                continue
        time.sleep(0.5)
    return all_stocks


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
    """批量查个股行业板块 (东方财富 ulist.np)

    返回: {code: sector_name}
    每批 10 只
    """
    if not codes:
        return {}
    base = config["data_source"]["eastmoney_base"]
    ut = get_sina_ut()
    sector_map: dict[str, str] = {}
    for i in range(0, len(codes), 10):
        batch = codes[i:i + 10]
        secids = ",".join(_build_secid(c) for c in batch)
        url = (
            f"{base}/api/qt/ulist.np/get"
            f"?fltt=2&invt=2&fields=f12,f14,f100"
            f"&secids={secids}&ut={ut}"
        )
        raw = curl_get(url)
        if raw and len(raw) > 20:
            try:
                data = json.loads(raw)
                for item in (data.get("data") or {}).get("diff", []) or []:
                    code = item.get("f12", "")
                    sector = item.get("f100", "")
                    if code and sector and sector != "-":
                        sector_map[code] = sector
            except Exception:
                pass
        time.sleep(0.1)
    return sector_map


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
    """综合大盘环境评估

    返回: {
      env_score, env_level, position_advice, position_ratio, market_type, flags,
      details: {weighted_chg, trend_bonus, vol_bonus, sh_amt_yi, index_data}
    }
    """
    env_cfg = config["env"]
    indices = env_cfg["indices"]
    vol_hi = env_cfg["vol_thresh_hi_yi"]
    vol_lo = env_cfg["vol_thresh_lo_yi"]
    pos_cfg = config["position"]

    joined = ",".join(c for c, _, _ in indices)
    raw = curl_get(f"https://qt.gtimg.cn/q={joined}")

    results: dict[str, dict[str, Any]] = {}
    for line in raw.strip().splitlines():
        m = re.search(r'v_(\w+)="([^"]+)"', line)
        if not m:
            continue
        code = m.group(1)
        parts = m.group(2).split("~")
        if len(parts) < 10:
            continue
        try:
            results[code] = {
                "name": parts[1],
                "open": float(parts[5]),
                "prev": float(parts[4]),
                "price": float(parts[3]),
                "high": float(parts[33]) if len(parts) > 33 else float(parts[3]),
                "low": float(parts[34]) if len(parts) > 34 else float(parts[3]),
                "vol": float(parts[6]) if len(parts) > 6 else 0,
                "amt": float(parts[37]) if len(parts) > 37 else 0,
            }
        except Exception:
            continue

    index_data: dict[str, Any] = {}
    weighted_chg = 0.0
    trend_bonus = 0
    weight_total = 0.0

    for code, name, w in indices:
        if code not in results:
            continue
        r = results[code]
        chg = (r["price"] - r["prev"]) / r["prev"] * 100 if r["prev"] > 0 else 0
        r["chg"] = round(chg, 2)
        index_data[name] = {"chg": chg, "price": r["price"], "prev": r["prev"]}
        weighted_chg += chg * w
        weight_total += w
        time.sleep(0.1)

        kl_url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={code},day,,,8,qfq"
        )
        kl_raw = curl_get(kl_url, referer="https://gu.qq.com/")
        try:
            kl_json = json.loads(kl_raw)
            day_data = kl_json.get("data", {}).get(code, {}).get("day", [])
            closes = [float(k[2]) for k in day_data if len(k) >= 3]
            if len(closes) >= 5:
                ma5 = sum(closes[-5:]) / 5
                index_data[name]["ma5"] = round(ma5, 2)
                index_data[name]["above_ma5"] = r["price"] > ma5
                if r["price"] > ma5:
                    trend_bonus += 8
        except Exception:
            pass

    if not results:
        return {
            "env_score": 50,
            "env_level": "中性（数据缺失）",
            "position_advice": "数据源不可用, 仓位待人工判断",
            "position_ratio": 0.5,
            "market_type": "未知",
            "flags": ["data_missing"],
            "details": {"reason": "腾讯 qt.gtimg.cn 无返回"},
        }

    if weight_total > 0:
        weighted_chg /= weight_total
    weighted_chg = round(weighted_chg, 3)

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
            "vol_bonus": vol_bonus,
            "sh_amt_yi": round(sh_amt_yi, 0),
            "index_data": index_data,
        },
    }


# ─────────── 板块动能（基于本地 sectors_YYYYMMDD.json） ───────────

def get_sector_momentum(days: int = 3, sectors_dir: Optional[Path] = None) -> dict[str, Any]:
    """识别持续强势的主线板块

    sectors_dir 默认 ~/.hermes/stock_picker/sectors/（向后兼容）
    """
    if sectors_dir is None:
        sectors_dir = Path.home() / ".hermes" / "stock_picker" / "sectors"
    sector_files = sorted(sectors_dir.glob("sectors_*.json")) if sectors_dir.exists() else []

    history: dict[str, list[tuple[str, float]]] = {}
    for sf in sector_files[-days * 3:]:
        try:
            data = json.loads(sf.read_text())
        except Exception:
            continue
        day = sf.stem.replace("sectors_", "")
        for s in data:
            name = s.get("name", "")
            try:
                chg = float(s.get("chg_pct", 0))
            except Exception:
                continue
            history.setdefault(name, []).append((day, chg))

    rising: list[dict[str, Any]] = []
    for name, records in sorted(history.items()):
        records.sort()
        recent = records[-days:]
        if len(recent) == days and all(chg > 0.5 for _, chg in recent):
            total = sum(chg for _, chg in recent)
            rising.append({"name": name, "total_chg": round(total, 2), "days": days, "recent": recent})

    rising.sort(key=lambda x: x["total_chg"], reverse=True)

    today_file = sectors_dir / f"sectors_{date.today().strftime('%Y%m%d')}.json"
    new_sectors: list[dict[str, Any]] = []
    rising_names = {r["name"] for r in rising}
    if today_file.exists():
        try:
            new_sectors = [
                s for s in json.loads(today_file.read_text())
                if float(s.get("chg_pct", 0)) > 3 and s.get("name", "") not in rising_names
            ]
        except Exception:
            pass

    return {"rising_sectors": rising, "new_sectors": new_sectors, "rising_count": len(rising)}


# ─────────── 配置加载 ───────────

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
