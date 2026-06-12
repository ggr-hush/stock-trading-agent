"""feishu/card_templates.py — v12.9.1 飞书 interactive card 模板

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


def card_positions(positions: list[dict[str, Any]]) -> dict[str, Any]:
    """v12.9.1: 持仓 interactive card + 总盈亏

    输入 positions 字段: code / name / open_price / shares / pnl_open_pct / status
    """
    if not positions:
        return _card("持仓", [
            {"tag": "div", "text": {"tag": "lark_md", "content": "暂无持仓"}},
        ], template="grey")
    total_pnl = 0.0
    open_count = 0
    elements: list[dict] = []
    for i, p in enumerate(positions[:10]):
        code = p.get("code", "?")
        name = p.get("name", "?")
        op = p.get("open_price", 0)
        sh = p.get("shares", 0)
        pnl_pct = p.get("pnl_open_pct", 0) or 0
        status = p.get("status", "?")
        pnl_emoji = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < 0 else "⚪")
        if status == "open":
            open_count += 1
            total_pnl += pnl_pct
        line = (
            f"**{i+1}. {code} {name}** · 成本 {op:.2f} · "
            f"持仓 {sh:.0f} 股 · {pnl_emoji} **{pnl_pct:+.1f}%** · {status}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
        if i < len(positions) - 1:
            elements.append({"tag": "hr"})
    summary = (
        f"**合计 {len(positions)} 只, 持仓中 {open_count} 只, "
        f"平均盈亏 {total_pnl/open_count:+.1f}%**"
    ) if open_count else f"**合计 {len(positions)} 只**"
    elements.insert(0, {"tag": "div", "text": {"tag": "lark_md", "content": summary}})
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
