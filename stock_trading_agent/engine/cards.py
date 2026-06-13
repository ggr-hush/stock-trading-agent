"""engine/cards.py — v12.A.2 飞书 interactive card 工厂

v12.A.2 改名迁移: 原 feishu/card_templates.py
原因: 工厂函数被 engine/skills.py 调用 (做"拼卡片"工作),
     跟 feishu/listener.py / feishu/pusher.py (做"发卡片"工作) 职责不同,
     放 engine/ 下名实相符。

飞书 interactive card JSON 结构 (简化版, 只用 header + elements):
{
  "config": {"wide_screen_mode": true},
  "header": {"title": {"tag": "plain_text", "content": "..."}, "template": "blue"},
  "elements": [
    {"tag": "div", "text": {"tag": "lark_md", "content": "..."}},
    {"tag": "hr"},
    {"tag": "div", "fields": [...]},  # 多列字段
  ]
}
"""
from __future__ import annotations

from datetime import date
from typing import Any


def _card(title: str, elements: list[dict], template: str = "blue") -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


def _field(key: str, val: str) -> dict[str, Any]:
    """单列字段 (key: val)"""
    return {
        "is_short": True,
        "text": {"tag": "lark_md", "content": f"**{key}**: {val}"},
    }


def _bar(pct: float, width: int = 10) -> str:
    """v12.A.2: 盈亏柱状图 (用 ▁▂▃▄▅▆▇█ 8 段 unicode 块)
    正数 → 绿色块, 负数 → 红色块, 长度按 |pct| 算
    """
    if pct == 0:
        return "▌" + "─" * (width - 1)
    blocks = "▁▂▃▄▅▆▇█"
    n = min(width, max(1, int(abs(pct) * width / 10)))
    if pct > 0:
        return "🟩" + "▇" * n
    return "🟥" + "▇" * n


def card_picks(picks: list[dict[str, Any]], date: str = "") -> dict[str, Any]:
    """v12.9.1: 今日选股 interactive card

    输入 picks 字段: code / name / sector / score / chg_pct / price / plan
    """
    title = f"今日选股 · {date}" if date else "今日选股"
    if not picks:
        return _card(title, [
            {"tag": "div", "text": {"tag": "lark_md", "content": "暂无选股 (今天没跑 pick stage)"}},
        ], template="grey")
    elements: list[dict] = []
    for i, p in enumerate(picks[:10]):
        code = p.get("code", "?")
        name = p.get("name", "?")
        sector = p.get("sector", "-")
        score = p.get("score", 0)
        chg = p.get("chg_pct", 0)
        plan = p.get("plan", "?")
        chg_str = f"{chg:+.1f}%" if isinstance(chg, (int, float)) else str(chg)
        line = (
            f"**{i+1}. {code} {name}** · 板块 {sector} · "
            f"评分 **{score:.1f}** · 今日 {chg_str} · 方案 {plan}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
        if i < len(picks) - 1:
            elements.append({"tag": "hr"})
    if len(picks) > 10:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"_(还有 {len(picks)-10} 只未显示)_"}})
    return _card(title, elements)


def _position_holding_days(pick_date: str) -> int:
    """v12.A.2: 持仓天数 (从 pick_date 到今天)"""
    if not pick_date:
        return 0
    try:
        pd = date.fromisoformat(pick_date[:10])
        return (date.today() - pd).days
    except (ValueError, TypeError):
        return 0


def card_positions(positions: list[dict[str, Any]]) -> dict[str, Any]:
    """v12.A.2: 持仓 interactive card + 盈亏柱 + 板块分布 + 持仓天数

    增强: 每只票加持仓天数 + 盈亏柱; 末尾加板块分布汇总
    """
    if not positions:
        return _card("持仓", [
            {"tag": "div", "text": {"tag": "lark_md", "content": "暂无持仓"}},
        ], template="grey")
    total_pnl = 0.0
    open_count = 0
    sector_stats: dict[str, dict[str, Any]] = {}  # sector -> {count, pnl_sum, pnl_n}
    elements: list[dict] = []
    for i, p in enumerate(positions[:10]):
        code = p.get("code", "?")
        name = p.get("name", "?")
        op = p.get("open_price", 0)
        sh = p.get("shares", 0)
        pnl_pct = p.get("pnl_open_pct", 0) or 0
        status = p.get("status", "?")
        sector = p.get("sector", "-") or "-"
        pick_date = p.get("pick_date", "")
        days = _position_holding_days(pick_date) if pick_date else 0
        pnl_emoji = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < 0 else "⚪")
        bar = _bar(pnl_pct)
        if status == "open":
            open_count += 1
            total_pnl += pnl_pct
            if sector != "-":
                if sector not in sector_stats:
                    sector_stats[sector] = {"count": 0, "pnl_sum": 0.0, "pnl_n": 0}
                sector_stats[sector]["count"] += 1
                sector_stats[sector]["pnl_sum"] += pnl_pct
                sector_stats[sector]["pnl_n"] += 1
        day_str = f" · {days}日" if days > 0 else ""
        line = (
            f"**{i+1}. {code} {name}** · 板块 {sector}{day_str} · "
            f"成本 {op:.2f} · {sh:.0f} 股 · "
            f"{pnl_emoji} **{pnl_pct:+.1f}%** {bar} · {status}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
        if i < len(positions) - 1:
            elements.append({"tag": "hr"})
    avg_pnl = (total_pnl / open_count) if open_count else 0
    summary = (
        f"**合计 {len(positions)} 只, 持仓中 {open_count} 只, "
        f"平均盈亏 {avg_pnl:+.1f}%**"
    )
    elements.insert(0, {"tag": "div", "text": {"tag": "lark_md", "content": summary}})
    # v12.A.2 板块分布
    if sector_stats:
        elements.append({"tag": "hr"})
        sector_lines = ["**板块分布:**"]
        for sec, st in sorted(sector_stats.items(), key=lambda x: -x[1]["count"]):
            avg = (st["pnl_sum"] / st["pnl_n"]) if st["pnl_n"] else 0
            mark = "🟢" if avg > 0 else ("🔴" if avg < 0 else "⚪")
            sector_lines.append(
                f"  · {sec}: {st['count']} 只, 平均 {mark} {avg:+.1f}%"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(sector_lines)}})
    return _card("持仓", elements)


def card_explain(code: str, name: str, explanation: str,
                 sources: list[str] | None = None) -> dict[str, Any]:
    """v12.9.1: 解释类卡 — 标题 / 主体 / 来源"""
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": explanation}},
    ]
    if sources:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**来源**: " + " / ".join(sources[:3])},
        })
    template = "blue" if sources else "grey"
    return _card(f"💡 {code} {name}", elements, template=template)


# 包装: 转成飞书发消息的 content dict
def wrap_as_interactive(card: dict[str, Any]) -> dict[str, Any]:
    """返 {msg_type: 'interactive', content: card_dict} 给 _send_reply 用"""
    return {"msg_type": "interactive", "content": card}
