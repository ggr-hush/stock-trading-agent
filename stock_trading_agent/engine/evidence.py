"""engine/evidence.py — v12.A.3 证据编号统一工具

借鉴 trading-review-wiki: 每个证据用简短编号 (R1, S1, W1) + kind 标签
让 LLM 答案"逐条引用"而不是自由发挥, 用户能看清每个判断来自哪

约定:
  R  Knowledge (RAG 知识库) — haoyun/susan/chanlun
  S  SQL/picks/data (本地数据库 / 行情接口)
  M  Memory (用户记忆)
  F  Facts (temporal facts 时序账本)
  L  Live quote (实时行情)
  K  Skill (skill 自身计算结果)

公共 API:
  - make_evidence_id(kind, idx) -> str   编号生成: "R1", "S2"
  - format_evidence_for_prompt(items)    给 LLM prompt 看的多行字符串
  - render_evidence_section(items)       给飞书卡片底部看的 "📚 证据" 段
"""
from __future__ import annotations

from typing import Any


# kind → 编号前缀 (单字母, 跟 trading-review-wiki 的 W/R/G/F/M/S 一致)
KIND_PREFIX = {
    "rag": "R",        # 知识库 BM25 检索
    "knowledge": "R",  # 别名
    "sql": "S",        # 本地 SQLite / picks / paper_positions
    "memory": "M",     # 用户记忆 (memories 表)
    "facts": "F",      # 时序事实 (temporal_facts)
    "live": "L",       # 实时行情 (东方财富 push2)
    "kline": "K",      # K 线行情
    "skill": "K",      # skill 自身
}


def make_evidence_id(kind: str, idx: int) -> str:
    """生成证据编号, e.g. ('rag', 1) -> 'R1'"""
    prefix = KIND_PREFIX.get(kind, "E")  # 未知 kind 用 E
    return f"{prefix}{idx}"


def format_evidence_for_prompt(evidence: list[dict[str, Any]]) -> str:
    """给 LLM prompt 用的证据块, e.g.
        [R1] 缠论 108 课第 23 课: 教你炒股票...
        [R2] 好运2008 心法: 龙头股战法...
    空列表返 ""
    """
    if not evidence:
        return ""
    lines: list[str] = []
    for ev in evidence:
        eid = ev.get("id", "?")
        title = (ev.get("title") or "").strip()[:50]
        snippet = (ev.get("snippet") or "").strip()[:120]
        if title and snippet:
            lines.append(f"[{eid}] {title}: {snippet}")
        elif title:
            lines.append(f"[{eid}] {title}")
        elif snippet:
            lines.append(f"[{eid}] {snippet}")
    return "\n".join(lines)


def render_evidence_section(
    evidence: list[dict[str, Any]],
    max_items: int = 3,
) -> dict[str, Any]:
    """给飞书卡片底部看的 "📚 证据" 段 (返回 card 元素 dict)

    空列表返 {"tag": "div", "text": {"tag": "lark_md", "content": ""}}
    """
    if not evidence:
        return {"tag": "div", "text": {"tag": "lark_md", "content": ""}}
    items = evidence[:max_items]
    lines = ["**📚 证据:**"]
    for ev in items:
        eid = ev.get("id", "?")
        title = (ev.get("title") or "").strip()[:40]
        lines.append(f"- `[{eid}]` {title}" if title else f"- `[{eid}]`")
    more = len(evidence) - len(items)
    if more > 0:
        lines.append(f"_(还有 {more} 条未显示)_")
    return {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}


def build_evidence_from_rag(rag_results: list[dict[str, Any]], max_items: int = 3) -> list[dict[str, Any]]:
    """从 RAG 检索结果构造 evidence 列表 (kind='rag')

    rag_results: [{source, title, text, score}, ...] (engine.knowledge.retrieve 返的结构)
    """
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rag_results[:max_items], start=1):
        out.append({
            "id": make_evidence_id("rag", i),
            "kind": "rag",
            "title": r.get("title") or r.get("source", "?"),
            "snippet": (r.get("text") or "")[:120],
        })
    return out


def build_evidence_from_sql(table_name: str, count: int, sample: str = "") -> list[dict[str, Any]]:
    """从 SQL/data 查询结果构造 evidence 列表 (kind='sql')"""
    snippet = sample[:80] if sample else f"{count} 条记录"
    return [{
        "id": make_evidence_id("sql", 1),
        "kind": "sql",
        "title": f"{table_name} 表 ({count} 行)",
        "snippet": snippet,
    }]


def build_evidence_from_live(name: str, price: Any, chg_pct: Any) -> list[dict[str, Any]]:
    """从实时行情构造 evidence 列表 (kind='live')"""
    return [{
        "id": make_evidence_id("live", 1),
        "kind": "live",
        "title": f"东方财富实时: {name}",
        "snippet": f"价 {price} 涨跌 {chg_pct}%",
    }]


def build_evidence_from_facts(facts: list[dict[str, Any]], max_items: int = 3) -> list[dict[str, Any]]:
    """从 temporal facts 构造 evidence 列表 (kind='facts')"""
    out: list[dict[str, Any]] = []
    for i, f in enumerate(facts[:max_items], start=1):
        out.append({
            "id": make_evidence_id("facts", i),
            "kind": "facts",
            "title": f"{f.get('predicate', '?')} {f.get('object', '')}".strip(),
            "snippet": (f.get("claim") or "")[:120],
        })
    return out
