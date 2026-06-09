"""engine/correlation.py — 行业相关性矩阵 (v8.1)

从 paper_positions 历史 pnl_open_pct 算板块间 Pearson 相关,
按阈值贪心聚合成 group, 替代 config.yaml 硬编码的 correlated_sector_groups。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def _series_by_sector(window_days: int = 60) -> dict[str, dict[str, float]]:
    """拉最近 window_days 的板块日均 pnl_pct 序列

    Returns:
        {sector: {date: avg_pnl_pct, ...}, ...}
    """
    from .paper_trader import get_db
    conn = get_db()
    rows = conn.execute(
        """SELECT pick_date, sector, AVG(pnl_open_pct) as pnl
           FROM paper_positions
           WHERE pnl_open_pct IS NOT NULL AND pick_date >= date('now', ?)
           GROUP BY pick_date, sector
           ORDER BY pick_date""",
        (f"-{window_days} day",),
    ).fetchall()
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        d = r["pick_date"]
        sec = r["sector"] or "(unknown)"
        out[sec][d] = float(r["pnl"] or 0)
    return dict(out)


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def compute_sector_correlation(window_days: int = 60) -> dict[tuple[str, str], float]:
    """算两两板块的 Pearson 相关

    只算"同一天都有数据"的 (date, sector) 对, 没数据的日期跳过。
    """
    series = _series_by_sector(window_days)
    sectors = sorted(series.keys())
    out: dict[tuple[str, str], float] = {}
    for i, a in enumerate(sectors):
        for b in sectors[i + 1:]:
            common = sorted(set(series[a].keys()) & set(series[b].keys()))
            if len(common) < 3:
                continue
            xs = [series[a][d] for d in common]
            ys = [series[b][d] for d in common]
            out[(a, b)] = round(_pearson(xs, ys), 3)
    return out


def learn_correlated_groups(
    corr: dict[tuple[str, str], float],
    threshold: float = 0.7,
) -> list[list[str]]:
    """贪心聚合: corr > threshold 的 (a, b) 合并到同一 group

    步骤:
      1. 收集所有出现在 corr 里的 sector
      2. 用 union-find 把 > threshold 的对合并
      3. 输出每个连通分量的 sector 列表 (按字母序)
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    sectors: set[str] = set()
    for (a, b), c in corr.items():
        sectors.update([a, b])
        if c >= threshold:
            union(a, b)

    groups: dict[str, list[str]] = defaultdict(list)
    for s in sorted(sectors):
        groups[find(s)].append(s)
    # 只保留 >= 2 个 sector 的 group (单 sector 不算 group)
    return sorted([g for g in groups.values() if len(g) >= 2])


def auto_learn_groups(window_days: int = 60, threshold: float = 0.7) -> list[list[str]]:
    """一站式: 算相关 + 贪心聚合"""
    corr = compute_sector_correlation(window_days=window_days)
    return learn_correlated_groups(corr, threshold=threshold)
