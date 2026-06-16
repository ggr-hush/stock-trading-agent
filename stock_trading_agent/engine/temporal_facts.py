"""engine/temporal_facts.py — v12.A.3 时序事实账本

借鉴 trading-review-wiki: 交易决策 (SELECTED / VALIDATED / SUPERSEDED / ...) 用
append-only JSONL 持久化, 每次写带 sha1 幂等 ID, 后续 supersede/invalidate 走状态机

存储: data/facts/stock_events.jsonl (一行一事实, gitignore)

公共 API:
  - record(subject, predicate, object, claim, status="active", **meta) -> str (fact_id)
  - supersede(old_fact_id, new_fact_id) -> None
  - invalidate(fact_id, reason) -> None
  - query_active(subject=None) -> list[dict]
  - query_all(subject=None, include_invalidated=False) -> list[dict]
  - get_fact(fact_id) -> dict | None
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("engine.temporal_facts")

# 默认存储路径: data/facts/stock_events.jsonl (相对 cwd, 项目根)
DEFAULT_FACTS_PATH = Path("data/facts/stock_events.jsonl")

# v1 词表 (跟 trading-review-wiki 对齐)
PREDICATE_VOCAB = {
    "SELECTED",      # 选股
    "VALIDATED",     # 复盘验证
    "REVIEWED",      # v12.A.4: 用户主动复盘
    "SUPERSEDED",    # 被新事实替代
    "INVALIDATED",   # 作废
    "TUNED",         # 调参
}

# 状态机: 允许的状态转移
VALID_STATUS = {"active", "superseded", "invalidated"}


def _facts_path() -> Path:
    """解析 facts 路径, 允许环境变量覆盖 (测试用)"""
    override = os.environ.get("STOCK_AGENT_FACTS_PATH")
    if override:
        return Path(override)
    p = DEFAULT_FACTS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _fact_id(subject: str, predicate: str, object_: str) -> str:
    """确定性 fact_id = sha1(subject|predicate|object)[:12]

    同样 (subject, predicate, object) 多次 record → 同一 fact_id (幂等)
    """
    h = hashlib.sha1(f"{subject}|{predicate}|{object_}".encode("utf-8")).hexdigest()
    return h[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_all() -> list[dict[str, Any]]:
    """读全部 facts, 缺文件返 []"""
    p = _facts_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception as e:  # noqa: BLE001
            log.warning("facts 解析行失败: %s | %s", line[:80], e)
    return out


def _append(fact: dict[str, Any]) -> None:
    """append 一行 JSON 到 facts 文件"""
    p = _facts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(fact, ensure_ascii=False) + "\n")


def _rewrite(facts: list[dict[str, Any]]) -> None:
    """整体重写 (用于 supersede/invalidate 后的状态修正)

    因为 JSONL append-only 但状态可改, 用读 → 改 → 写回方式维护
    """
    p = _facts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in facts) + "\n", encoding="utf-8")


def record(
    subject: str,
    predicate: str,
    object_: str,
    claim: str,
    *,
    status: str = "active",
    source: str = "agent",
    **meta: Any,
) -> str:
    """记一条事实, 幂等 (同 fact_id 已存在且 status=active → 跳过, 不重复写)

    Args:
        subject: 主体 (e.g. "002063")
        predicate: 谓词 (e.g. "SELECTED"), 必须在 PREDICATE_VOCAB
        object_: 客体 (e.g. "plan:A")
        claim: 自然语言描述 (e.g. "今日选股 A 方案, 评分 85")
        status: active | superseded | invalidated
        source: agent | user | system
        **meta: 额外元数据 (e.g. score=85, pnl_pct=2.3)

    Returns:
        fact_id (12 字符 sha1 前缀)
    """
    if predicate not in PREDICATE_VOCAB:
        raise ValueError(f"predicate 必须在 v1 词表: {sorted(PREDICATE_VOCAB)}")
    if status not in VALID_STATUS:
        raise ValueError(f"status 必须在 {sorted(VALID_STATUS)}")
    fid = _fact_id(subject, predicate, object_)
    # 幂等检查
    existing = get_fact(fid)
    if existing and existing.get("status") == "active" and status == "active":
        log.debug("fact 幂等跳过: %s", fid)
        return fid
    fact = {
        "id": fid,
        "subject": subject,
        "predicate": predicate,
        "object": object_,
        "claim": claim,
        "status": status,
        "source": source,
        "created_at": _now(),
        **meta,
    }
    _append(fact)
    return fid


def supersede(old_fact_id: str, new_fact_id: str) -> None:
    """把 old 标 superseded (不删, 留 audit trail)

    注意: caller 应先 record(new) 拿到 new_fact_id 再调这个
    """
    facts = _read_all()
    changed = False
    for f in facts:
        if f.get("id") == old_fact_id and f.get("status") == "active":
            f["status"] = "superseded"
            f["superseded_by"] = new_fact_id
            f["superseded_at"] = _now()
            changed = True
    if changed:
        _rewrite(facts)


def invalidate(fact_id: str, reason: str = "") -> None:
    """把一条 fact 标 invalidated"""
    facts = _read_all()
    changed = False
    for f in facts:
        if f.get("id") == fact_id and f.get("status") == "active":
            f["status"] = "invalidated"
            f["invalidated_at"] = _now()
            f["invalidated_reason"] = reason
            changed = True
    if changed:
        _rewrite(facts)


def get_fact(fact_id: str) -> Optional[dict[str, Any]]:
    """按 id 查一条"""
    for f in _read_all():
        if f.get("id") == fact_id:
            return f
    return None


def query_active(subject: Optional[str] = None) -> list[dict[str, Any]]:
    """查 active facts, 可按 subject 过滤"""
    out: list[dict[str, Any]] = []
    for f in _read_all():
        if f.get("status") != "active":
            continue
        if subject is None or f.get("subject") == subject:
            out.append(f)
    return out


def query_all(
    subject: Optional[str] = None,
    include_invalidated: bool = False,
) -> list[dict[str, Any]]:
    """查全部 facts (含 superseded/invalidated 时显式 opt-in)"""
    out: list[dict[str, Any]] = []
    for f in _read_all():
        if subject is not None and f.get("subject") != subject:
            continue
        if not include_invalidated and f.get("status") != "active":
            continue
        out.append(f)
    return out
