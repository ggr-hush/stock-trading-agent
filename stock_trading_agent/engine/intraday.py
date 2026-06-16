"""engine/intraday.py — v9.3 实时盯盘

扫当前 open paper 持仓, 拉实时行情, 异动推飞书告警。
异动规则 (config.intraday_monitor 配):
  - chg_pct 绝对值 > 3%
  - 振幅 > 8%
"""
from __future__ import annotations

import re
from typing import Any

from .tushare_client import get_pro, to_ts_code, rate_limit_sleep
from .paper_trader import get_db


def get_open_positions_for_monitor() -> list[dict[str, Any]]:
    """从 DB 拉 status='open' 的 paper 持仓, 返回 [{code, name, sector, open_price, prev_close}]"""
    conn = get_db()
    rows = conn.execute(
        """SELECT code, name, sector, open_price, open_amount
           FROM paper_positions
           WHERE status='open'
           ORDER BY open_amount DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_realtime_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """v12.A.4.c: Tushare daily + daily_basic 拉实时行情

    返回 {code: {price, prev_close, chg_pct, amplitude}}
    失败 / 拉不到 → 该 code 不在结果里 (让上层当成"无行情"处理)

    实现:
      1) 查最近 1 个交易日的 daily, 拿 close / pre_close / pct_chg
      2) 同一日 daily_basic 拿 amplitude (Tushare daily 也有 amplitude 字段)
      3) 单只票查询 (持仓不会太多, 不走全市场)
    """
    if not codes:
        return {}
    from datetime import date as _d, timedelta
    pro = get_pro()
    today = _d.today()
    # 找最近 1 个交易日 (T+1 兼容)
    end_d = today.strftime("%Y%m%d")
    beg_d = (today - timedelta(days=10)).strftime("%Y%m%d")
    out: dict[str, dict[str, Any]] = {}
    for code in codes:
        ts_code = to_ts_code(code)
        if "." not in ts_code:
            continue
        try:
            df = pro.daily(ts_code=ts_code, start_date=beg_d, end_date=end_d,
                           fields="ts_code,trade_date,close,pre_close,pct_chg,high,low")
            if df is None or df.empty:
                continue
            row = df.sort_values("trade_date", ascending=False).iloc[0]
            close = float(row.get("close") or 0)
            pre_close = float(row.get("pre_close") or 0)
            pct_chg = float(row.get("pct_chg") or 0)
            high = float(row.get("high") or 0)
            low = float(row.get("low") or 0)
            # 振幅 = (high - low) / pre_close * 100
            amplitude = round((high - low) / pre_close * 100, 2) if pre_close > 0 else 0.0
            if close <= 0 or pre_close <= 0:
                continue
            out[code] = {
                "price": close,
                "prev_close": pre_close,
                "chg_pct": pct_chg,
                "amplitude": amplitude,
            }
            rate_limit_sleep(0.05)
        except Exception as e:
            print(f"[intraday] {code} 拉取失败: {e}", file=__import__("sys").stderr)
            continue
    return out


def detect_anomalies(
    positions: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    chg_threshold: float = 3.0,
    amplitude_threshold: float = 8.0,
) -> list[dict[str, Any]]:
    """对比持仓 vs 实时行情, 返回异动列表"""
    anomalies: list[dict[str, Any]] = []
    for pos in positions:
        code = pos["code"]
        q = quotes.get(code)
        if not q:
            continue
        reasons: list[str] = []
        if abs(q["chg_pct"]) >= chg_threshold:
            direction = "涨" if q["chg_pct"] > 0 else "跌"
            reasons.append(f"{direction} {q['chg_pct']:.2f}% (阈值 {chg_threshold}%)")
        if q["amplitude"] >= amplitude_threshold:
            reasons.append(f"振幅 {q['amplitude']:.2f}% (阈值 {amplitude_threshold}%)")
        if reasons:
            anomalies.append({
                "code": code,
                "name": pos.get("name", ""),
                "sector": pos.get("sector", ""),
                "price": q["price"],
                "chg_pct": q["chg_pct"],
                "amplitude": q["amplitude"],
                "reasons": reasons,
                "open_amount": pos.get("open_amount", 0),
            })
    return anomalies


def format_anomaly_message(anomalies: list[dict[str, Any]]) -> str:
    """异动列表 → 推送消息文本"""
    if not anomalies:
        return ""
    lines = [f"## ⚠️ 实时盯盘异动 · {len(anomalies)} 只"]
    for a in anomalies:
        lines.append(
            f"- **{a['code']} {a['name']}** ({a.get('sector', '')}) "
            f"现价 {a['price']:.2f} 涨幅 {a['chg_pct']:+.2f}% 振幅 {a['amplitude']:.2f}%\n"
            f"  - {', '.join(a['reasons'])}"
        )
    return "\n".join(lines)


def intraday_monitor() -> dict[str, Any]:
    """v9.3: 扫持仓 + 拉行情 + 异动推飞书

    Returns: {"scanned": n, "anomalies": [...], "pushed": bool}
    """
    from ..feishu import pusher
    from .data_fetcher import load_config
    cfg = load_config()
    mon_cfg = cfg.get("intraday_monitor", {})
    if not mon_cfg.get("enabled", False):
        return {"scanned": 0, "anomalies": [], "pushed": False, "skipped": "disabled"}

    positions = get_open_positions_for_monitor()
    if not positions:
        return {"scanned": 0, "anomalies": [], "pushed": False}

    codes = [p["code"] for p in positions]
    quotes = fetch_realtime_quotes(codes)
    anomalies = detect_anomalies(
        positions, quotes,
        chg_threshold=mon_cfg.get("chg_threshold", 3.0),
        amplitude_threshold=mon_cfg.get("amplitude_threshold", 8.0),
    )

    pushed = False
    if anomalies:
        msg = format_anomaly_message(anomalies)
        try:
            pusher.push_anomaly(msg)
            pushed = True
        except Exception:
            pushed = False

    return {
        "scanned": len(positions),
        "anomalies": anomalies,
        "pushed": pushed,
    }
