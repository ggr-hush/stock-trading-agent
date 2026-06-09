"""engine/intraday.py — v9.3 实时盯盘

扫当前 open paper 持仓, 拉实时行情, 异动推飞书告警。
异动规则 (config.intraday_monitor 配):
  - chg_pct 绝对值 > 3%
  - 振幅 > 8%
"""
from __future__ import annotations

import re
from typing import Any

from .data_fetcher import curl_get
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
    """从 qt.gtimg.cn 拉实时行情, 返回 {code: {price, prev_close, chg_pct, amplitude}}"""
    if not codes:
        return {}
    # qt.gtimg.cn 用 sh+code / sz+code 前缀
    prefixed = []
    for c in codes:
        if c.startswith("6"):
            prefixed.append(f"sh{c}")
        elif c.startswith(("0", "3")):
            prefixed.append(f"sz{c}")
        else:
            prefixed.append(c)  # 兼容
    joined = ",".join(prefixed)
    raw = curl_get(f"https://qt.gtimg.cn/q={joined}")
    out: dict[str, dict[str, Any]] = {}
    for line in raw.strip().splitlines():
        m = re.search(r'v_(\w+)="([^"]+)"', line)
        if not m:
            continue
        full = m.group(1)
        code = full[2:] if len(full) > 2 else full  # 去 sh/sz 前缀
        parts = m.group(2).split("~")
        if len(parts) < 10:
            continue
        try:
            price = float(parts[3]) if parts[3] else 0.0
            prev_close = float(parts[4]) if parts[4] else 0.0
            chg_pct = float(parts[32]) if len(parts) > 32 and parts[32] else 0.0
            amplitude = float(parts[37]) if len(parts) > 37 and parts[37] else 0.0
        except (ValueError, IndexError):
            continue
        if price <= 0 or prev_close <= 0:
            continue
        out[code] = {
            "price": price,
            "prev_close": prev_close,
            "chg_pct": chg_pct,
            "amplitude": amplitude,
        }
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
