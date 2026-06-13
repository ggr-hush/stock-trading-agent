"""
tuner.py — 安全范围自动调参
- 周末从 picks + paper_positions 拉本周 stats
- 对 v3 每个 numeric 参数提议新值
- 在 safe_range 内 → 直接写 config.yaml + params_history
- 超出 safe_range → 推飞书确认卡片（proposal 队列）
- 黑名单：基于本周胜率增删（受 max_add / max_remove 约束）
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


from ..llm.reasoner import judge_proposal
from .data_fetcher import load_config, reload_config
from .paper_trader import get_db

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"



def _read_cfg_with_fallback() -> dict:
    """读 CONFIG_PATH: yaml 优先，找不到 PyYAML 时读同名 .json"""
    try:
        import yaml as _yaml  # noqa: PLC0415
        with open(CONFIG_PATH) as f:
            return _yaml.safe_load(f)
    except ImportError:
        json_path = CONFIG_PATH.with_suffix(".json")
        if not json_path.exists():
            raise RuntimeError(f"PyYAML 未安装且 {json_path} 不存在")
        import json as _json  # noqa: PLC0415
        return _json.loads(json_path.read_text())


def _write_cfg_with_fallback(cfg: dict) -> None:
    """写 CONFIG_PATH: yaml 优先，找不到 PyYAML 时写同名 .json"""
    try:
        import yaml as _yaml  # noqa: PLC0415
        with open(CONFIG_PATH, "w") as f:
            _yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        return
    except ImportError:
        json_path = CONFIG_PATH.with_suffix(".json")
        import json as _json  # noqa: PLC0415
        json_path.write_text(_json.dumps(cfg, ensure_ascii=False, indent=2))


# ─────────── 统计 ───────────

def weekly_stats() -> dict[str, Any]:
    """本周（周一到周日）所有 picks + paper 表现"""
    conn = get_db()
    today = datetime.now().date()
    # 本周日 = today, 本周一 = today - timedelta(days=today.weekday())
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")

    rows = conn.execute(
        """
        SELECT p.*, pos.pnl_noon_pct, pos.pnl_open_pct
        FROM picks p
        LEFT JOIN paper_positions pos ON pos.code = p.code AND pos.pick_date = p.pick_date
        WHERE p.pick_date >= ?
        ORDER BY p.pick_date, p.score DESC
        """,
        (week_start,),
    ).fetchall()

    picks = [dict(r) for r in rows]
    closed = [p for p in picks if p.get("pnl_noon_pct") is not None]

    # 按评分分桶
    buckets_score: dict[str, list[float]] = defaultdict(list)
    for p in closed:
        s = p.get("score") or 0
        if s >= 80:
            buckets_score[">=80"].append(p["pnl_noon_pct"])
        elif s >= 75:
            buckets_score["75-80"].append(p["pnl_noon_pct"])
        elif s >= 70:
            buckets_score["70-75"].append(p["pnl_noon_pct"])
        else:
            buckets_score["<70"].append(p["pnl_noon_pct"])

    # 按涨幅分桶
    buckets_chg: dict[str, list[float]] = defaultdict(list)
    for p in closed:
        c = p.get("chg_pct") or 0
        if c >= 4.8:
            buckets_chg[">=4.8"].append(p["pnl_noon_pct"])
        elif c >= 4.0:
            buckets_chg["4.0-4.8"].append(p["pnl_noon_pct"])
        elif c >= 3.5:
            buckets_chg["3.5-4.0"].append(p["pnl_noon_pct"])
        elif c >= 3.0:
            buckets_chg["3.0-3.5"].append(p["pnl_noon_pct"])
        else:
            buckets_chg["<3.0"].append(p["pnl_noon_pct"])

    # 按板块分桶
    buckets_sector: dict[str, list[float]] = defaultdict(list)
    for p in closed:
        sec = p.get("sector") or "(unknown)"
        buckets_sector[sec].append(p["pnl_noon_pct"])

    def _summarize(d: dict[str, list[float]]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for k, vs in d.items():
            if not vs:
                continue
            wins = sum(1 for v in vs if v > 0)
            out[k] = {
                "n": len(vs),
                "avg": round(sum(vs) / len(vs), 2),
                "win_rate": round(wins / len(vs) * 100, 1),
            }
        return out

    # 整体胜率
    overall_pnls = [p["pnl_noon_pct"] for p in closed]
    overall_wins = sum(1 for v in overall_pnls if v > 0)
    overall = {
        "n": len(overall_pnls),
        "avg": round(sum(overall_pnls) / len(overall_pnls), 2) if overall_pnls else 0,
        "win_rate": round(overall_wins / len(overall_pnls) * 100, 1) if overall_pnls else 0,
    }

    return {
        "week_start": week_start,
        "overall": overall,
        "by_score": _summarize(buckets_score),
        "by_chg": _summarize(buckets_chg),
        "by_sector": _summarize(buckets_sector),
    }


# ─────────── 调参决策 ───────────

def _in_safe_range(new_value: float, safe_range: list[float]) -> bool:
    lo, hi = safe_range[0], safe_range[1]
    return lo <= new_value <= hi


def _propose_score_max(stats: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    """评分上限：>=80 段如果胜率 < 60%，提议下调 2 分（最多）"""
    node = config["v3"]["score_max"]
    cur = node["value"]
    safe_lo, safe_hi = node["safe_range"]
    bucket = stats["by_score"].get(">=80")
    if not bucket or bucket["n"] < 3:
        return None
    # >=80 段胜率 < 60% → 建议把上限收紧
    if bucket["win_rate"] < 60:
        new = round(cur - 2, 1)
        new = max(new, safe_lo)
        if abs(new - cur) < 0.1:
            return None
        return {
            "param": "v3.score_max",
            "old": cur,
            "new": new,
            "in_safe_range": _in_safe_range(new, node["safe_range"]),
            "reason": f"本周 >=80 段胜率 {bucket['win_rate']}% (n={bucket['n']}), 历史规律反向指标",
        }
    # >=80 段胜率 > 75% → 可考虑放宽（但保守起见不动）
    return None


def _propose_strong_band(stats: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    """强信号带：3.0-3.5 段胜率显著高于 3.5-4.0 → 收紧上沿"""
    node_lo = config["v3"]["strong_band_lo"]
    node_hi = config["v3"]["strong_band_hi"]
    in_band = stats["by_chg"].get("3.0-3.5")
    above_band = stats["by_chg"].get("3.5-4.0")
    if not in_band or not above_band or in_band["n"] < 3 or above_band["n"] < 3:
        return None
    # in_band 胜率明显更高（>15pp）→ 收紧上沿
    if in_band["win_rate"] - above_band["win_rate"] > 15:
        new_hi = round(node_hi["value"] - 0.1, 2)
        if new_hi < in_band["win_rate"] / 100:  # sanity: 不让上沿降到下沿以下
            return None
        if new_hi == node_hi["value"]:
            return None
        return {
            "param": "v3.strong_band_hi",
            "old": node_hi["value"],
            "new": new_hi,
            "in_safe_range": _in_safe_range(new_hi, node_hi["safe_range"]),
            "reason": f"3.0-3.5 段胜率 {in_band['win_rate']}% >> 3.5-4.0 段 {above_band['win_rate']}%, 收紧上沿",
        }
    return None


def _propose_blacklist(stats: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """黑名单：本周胜率 <30% 且样本 ≥2 的板块加；<0% 移除"""
    by_sector = stats["by_sector"]
    cur = set(config["blacklist"]["sectors"])
    safe = set(config["blacklist"].get("safe_sectors", []))
    max_add = config["blacklist"]["max_add_per_week"]
    max_rm = config["blacklist"]["max_remove_per_week"]
    proposals: list[dict[str, Any]] = []

    # 提议新增
    to_add: list[tuple[str, dict[str, float]]] = []
    for sec, b in by_sector.items():
        if sec in cur or sec in safe or b["n"] < 2:
            continue
        if b["win_rate"] < 30:
            to_add.append((sec, b))
    to_add.sort(key=lambda x: x[1]["win_rate"])  # 最差先加
    for sec, b in to_add[:max_add]:
        proposals.append({
            "param": "blacklist.sectors",
            "action": "add",
            "old": sorted(cur),
            "new": sorted(cur | {sec}),
            "in_safe_range": True,  # 数量约束已保证
            "reason": f"板块 {sec} 本周胜率 {b['win_rate']}% (n={b['n']}), 加入黑名单",
        })

    # 提议移除（在黑名单中且本周胜率 ≥50%）
    to_rm: list[tuple[str, dict[str, float]]] = []
    for sec in cur:
        if sec in safe:
            continue
        b = by_sector.get(sec)
        if b and b["n"] >= 2 and b["win_rate"] >= 50:
            to_rm.append((sec, b))
    to_rm.sort(key=lambda x: -x[1]["win_rate"])
    for sec, b in to_rm[:max_rm]:
        proposals.append({
            "param": "blacklist.sectors",
            "action": "remove",
            "old": sorted(cur),
            "new": sorted(cur - {sec}),
            "in_safe_range": True,
            "reason": f"板块 {sec} 本周胜率 {b['win_rate']}% (n={b['n']}), 表现恢复, 移出黑名单",
        })

    return proposals


# ─────────── 应用 / 提议 ───────────

def apply_proposal(proposal: dict[str, Any], auto: bool) -> bool:
    """应用 proposal：auto=True 表示在 safe_range 内自动改"""
    if auto and not proposal.get("in_safe_range"):
        return False
    param = proposal["param"]
    conn = get_db()
    now = datetime.now().isoformat()
    if param.startswith("v3."):
        # 单值参数
        sub_key = param.split(".")[1]
        cfg = _read_cfg_with_fallback()
        old = cfg["v3"][sub_key]["value"]
        new = proposal["new"]
        cfg["v3"][sub_key]["value"] = new
        with open(CONFIG_PATH, "w") as f:
            _write_cfg_with_fallback(cfg)
        reload_config()
        conn.execute(
            """
            INSERT INTO params_history
            (changed_at, param_name, old_value, new_value, reason, auto_applied)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, param, str(old), str(new), proposal.get("reason", ""), 1 if auto else 0),
        )
    elif param == "blacklist.sectors":
        cfg = _read_cfg_with_fallback()
        old = sorted(cfg["blacklist"]["sectors"])
        new = proposal["new"]
        cfg["blacklist"]["sectors"] = new
        with open(CONFIG_PATH, "w") as f:
            _write_cfg_with_fallback(cfg)
        reload_config()
        conn.execute(
            """
            INSERT INTO params_history
            (changed_at, param_name, old_value, new_value, reason, auto_applied)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, param, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False),
             proposal.get("reason", ""), 1 if auto else 0),
        )
    conn.commit()
    return True


def run_weekly_tune(dry_run: bool = True) -> dict[str, Any]:
    """v12.A.3: dry_run=True 默认只算 proposals 不写库 (--write 才真改)

    Returns:
        {
          stats, preview: [...], applied: [...], pending: [...]
          preview = dry_run 模式下计算出的将应用的 proposals (未真写)
          applied = 真写到 config.yaml + params_history 的 proposals
          pending = 需用户确认的 (out-of-safe-range 或 judge 不通过)
        }
    """
    cfg = load_config()
    stats = weekly_stats()
    proposals: list[dict[str, Any]] = []
    for fn in (_propose_score_max, _propose_strong_band):
        p = fn(stats, cfg)
        if p:
            proposals.append(p)
    proposals.extend(_propose_blacklist(stats, cfg))

    applied: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    # v9.4: LLM-as-judge 开关
    judge_cfg = cfg.get("tuner", {})
    judge_enabled = judge_cfg.get("judge_enabled", False)
    judge_min_score = int(judge_cfg.get("judge_min_score", 60))
    for p in proposals:
        if p.get("in_safe_range"):
            if judge_enabled:
                try:
                    judgment = judge_proposal(p, stats)
                    p["judgment"] = judgment
                    if judgment.get("score", 0) < judge_min_score:
                        # judge 不通过, 推到 pending 让用户确认
                        p["in_safe_range"] = False
                        p["pending_reason"] = f"judge 评分 {judgment.get('score')} < {judge_min_score}: {judgment.get('verdict', '')}"
                        pending.append(p)
                        continue
                except Exception as e:  # noqa: BLE001
                    # judge 调用失败, 走默认通过
                    p["judgment"] = {"error": str(e)}
            # v12.A.3: dry_run 屏障
            if dry_run:
                preview.append(p)
            else:
                apply_proposal(p, auto=True)
                applied.append(p)
        else:
            pending.append(p)
    return {"stats": stats, "preview": preview, "applied": applied, "pending": pending}
