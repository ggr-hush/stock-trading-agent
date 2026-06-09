"""
report.py — 周报 / 日报 Markdown 渲染 + 飞书推送
- render_weekly(weekly_review, backtest_result, llm_summary) -> str
- render_daily(daily_review) -> str
- push_report_weekly(...) 同时推飞书
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..feishu import pusher


def _fmt_pct(v: Any) -> str:
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_num(v: Any, decimals: int = 2) -> str:
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return "-"


def render_weekly(
    weekly: dict[str, Any],
    backtest: dict[str, Any] | None = None,
    llm_summary: str = "",
) -> str:
    """渲染周报 Markdown

    Args:
        weekly: reviewer.run_weekly_review() 输出
        backtest: backtest_multi() 输出 (可选)
        llm_summary: LLM 生成的 3 句话总结 (可选)
    """
    stats = weekly.get("stats", {})
    overall = stats.get("overall", {})
    applied = weekly.get("applied", [])
    pending = weekly.get("pending", [])

    lines: list[str] = [
        f"# 📅 量化周报 · {datetime.now().strftime('%Y-%m-%d')}",
        "",
    ]
    if llm_summary:
        lines += ["## 摘要", "", llm_summary, ""]

    # 整体 stats
    lines += [
        "## 整体表现",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| 样本数 | {overall.get('n', 0)} |",
        f"| 胜率 | {_fmt_pct(overall.get('win_rate', 0))} |",
        f"| 平均 PnL | {_fmt_pct(overall.get('avg', 0))} |",
        "",
    ]

    # 分桶
    by_score = stats.get("by_score", {})
    by_chg = stats.get("by_chg", {})
    by_sector = stats.get("by_sector", {})
    if by_score:
        lines += ["### 按评分分桶", "",
                  "| 评分区间 | 样本 | 平均 PnL | 胜率 |",
                  "|---|---:|---:|---:|"]
        for k, v in by_score.items():
            lines.append(f"| {k} | {v.get('n', 0)} | {_fmt_pct(v.get('avg', 0))} | {_fmt_pct(v.get('win_rate', 0))} |")
        lines.append("")
    if by_chg:
        lines += ["### 按选股日涨幅分桶", "",
                  "| 涨幅区间 | 样本 | 平均 PnL | 胜率 |",
                  "|---|---:|---:|---:|"]
        for k, v in by_chg.items():
            lines.append(f"| {k} | {v.get('n', 0)} | {_fmt_pct(v.get('avg', 0))} | {_fmt_pct(v.get('win_rate', 0))} |")
        lines.append("")
    if by_sector:
        lines += ["### 板块表现 (按 PnL 排序)", "",
                  "| 板块 | 样本 | 平均 PnL | 胜率 |",
                  "|---|---:|---:|---:|"]
        sorted_sectors = sorted(by_sector.items(), key=lambda x: x[1].get('avg', 0))
        for sec, v in sorted_sectors[:10]:
            lines.append(f"| {sec} | {v.get('n', 0)} | {_fmt_pct(v.get('avg', 0))} | {_fmt_pct(v.get('win_rate', 0))} |")
        lines.append("")

    # 调参
    lines += ["## 调参记录", ""]
    if applied:
        lines += [f"✅ 自动应用 {len(applied)} 项：", ""]
        for a in applied:
            lines.append(f"- `{a.get('param', '?')}`: {a.get('old', '?')} → {a.get('new', '?')}")
            lines.append(f"  - 理由: {a.get('reason', '')}")
        lines.append("")
    if pending:
        lines += [f"⏸ 待确认 {len(pending)} 项（请在飞书回复确认）:", ""]
        for p in pending:
            lines.append(f"- `{p.get('param', '?')}`: {p.get('old', '?')} → {p.get('new', '?')}")
            lines.append(f"  - 理由: {p.get('reason', '')}")
        lines.append("")
    if not applied and not pending:
        lines += ["本周无参数变动", ""]

    # 回测对比 (如果有)
    if backtest and "error" not in backtest:
        lines += ["## 多策略回测对比", "",
                  f"样本: {backtest.get('days', 0)} 个交易日", "",
                  "| 策略 | 总 PnL | 胜率 | Sharpe | Sortino | 最大回撤 | 年化 | 连续亏损 | 盈亏比 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for name in ("fixed_A", "fixed_B", "auto", "fixed_C"):
            if name not in backtest:
                continue
            s = backtest[name]
            m = s.get("metrics", {})
            lines.append(
                f"| {name} | {_fmt_pct(s.get('total_pnl_pct', 0))} "
                f"| {_fmt_pct(s.get('win_rate_pct', 0))} "
                f"| {_fmt_num(m.get('sharpe'))} "
                f"| {_fmt_num(m.get('sortino'))} "
                f"| {_fmt_pct(m.get('max_drawdown_pct', 0))} "
                f"| {_fmt_pct(m.get('annualized_return_pct', 0))} "
                f"| {m.get('max_consecutive_losses', 0)} "
                f"| {_fmt_num(m.get('profit_factor'))} |"
            )
        rec = backtest.get("recommendation", "-")
        lines += ["", f"**推荐: {rec}** (按 Sharpe 选最优)", ""]

    lines += ["", "---", f"_生成时间: {datetime.now().isoformat()}_"]
    return "\n".join(lines)


def render_daily(daily: dict[str, Any]) -> str:
    """渲染日报"""
    today = daily.get("date", datetime.now().strftime("%Y-%m-%d"))
    picks = daily.get("picks", [])
    paper = daily.get("paper_total", {})
    lines = [
        f"# 📊 日报 · {today}",
        "",
        f"- 选股数: {len(picks)}",
        f"- Paper 累计: PnL {_fmt_pct(paper.get('total_pnl_pct', 0))}, 胜率 {paper.get('win_rate', 0)}%",
        f"- 已成交: {paper.get('closed_count', 0)} 笔",
        "",
    ]
    if picks:
        lines += ["## 选股明细", "",
                  "| 代码 | 名称 | 评分 | 板块 |",
                  "|---|---|---:|---|"]
        for s in picks[:15]:
            lines.append(f"| {s.get('code', '')} | {s.get('name', '')} | {_fmt_num(s.get('score', 0), 1)} | {s.get('sector', '')} |")
    return "\n".join(lines)


def save_report(content: str, name: str = "weekly") -> Path:
    """保存到 docs/reports/"""
    from pathlib import Path
    out_dir = Path(__file__).parent.parent.parent / "docs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    p = out_dir / fname
    p.write_text(content, encoding="utf-8")
    return p


def save_report_pdf(md_content: str, name: str = "weekly",
                    title: str = "量化周报") -> Path:
    """v9.1: Markdown → PDF, 落到 docs/reports/ 下 .pdf

    用纯 stdlib 的 engine._pdf.render_pdf, 无需 reportlab / weasyprint 依赖。
    """
    from pathlib import Path
    from ._pdf import render_pdf
    out_dir = Path(__file__).parent.parent.parent / "docs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    p = out_dir / fname
    p.write_bytes(render_pdf(md_content, title=title))
    return p


def push_weekly_report(weekly: dict[str, Any],
                       backtest: dict[str, Any] | None = None,
                       llm_summary: str = "",
                       save: bool = True,
                       save_pdf: bool = False) -> dict[str, Any]:
    """渲染 + 保存 + 推飞书 (一站式)

    Args:
        save: 保存 Markdown
        save_pdf: v9.1: 同时保存 PDF 版
    """
    content = render_weekly(weekly, backtest, llm_summary)
    saved_path = None
    pdf_path = None
    if save:
        try:
            saved_path = save_report(content, "weekly")
        except Exception as e:
            saved_path = f"save failed: {e}"
    if save_pdf:
        try:
            pdf_path = save_report_pdf(content, "weekly")
        except Exception as e:
            pdf_path = f"pdf save failed: {e}"
    push_result = pusher.push_weekly(weekly, llm_summary=llm_summary)
    return {
        "content": content,
        "saved": str(saved_path) if saved_path else None,
        "saved_pdf": str(pdf_path) if pdf_path else None,
        "feishu": push_result,
    }
