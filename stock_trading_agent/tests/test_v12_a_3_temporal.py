"""test_v12_a_3_temporal.py — v12.A.3 时序账本 单元测试

Covers (改动 3):
  - engine/temporal_facts.py 5 个核心 (record 幂等 / supersede / invalidate / query_active / query_all)
  - get_stock_lifecycle skill (active 过滤 + include_invalidated)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── engine/temporal_facts 5 个核心 ───────────

def _setup_facts_tmp():
    """每个 test 单独 tmpdir 隔离"""
    tmp = tempfile.mkdtemp()
    os.environ["STOCK_AGENT_FACTS_PATH"] = f"{tmp}/facts.jsonl"
    return tmp


def test_record_idempotent() -> None:
    """同 (subject, predicate, object) 多次 record → 同一 fact_id, 不重复写"""
    _setup_facts_tmp()
    from stock_trading_agent.engine import temporal_facts
    fid1 = temporal_facts.record("002063", "SELECTED", "plan:A", "选 A")
    fid2 = temporal_facts.record("002063", "SELECTED", "plan:A", "选 A 又一次")
    assert fid1 == fid2
    all_f = temporal_facts.query_all(include_invalidated=True)
    assert len([f for f in all_f if f["id"] == fid1]) == 1


def test_supersede_marks_old_inactive() -> None:
    """supersede 标 old 为 superseded, 写 superseded_by / superseded_at"""
    _setup_facts_tmp()
    from stock_trading_agent.engine import temporal_facts
    old_id = temporal_facts.record("002063", "SELECTED", "plan:A", "选 A")
    new_id = temporal_facts.record("002063", "SELECTED", "plan:B", "改选 B")
    temporal_facts.supersede(old_id, new_id)
    old = temporal_facts.get_fact(old_id)
    assert old["status"] == "superseded"
    assert old.get("superseded_by") == new_id
    assert "superseded_at" in old
    # new 仍 active
    assert temporal_facts.get_fact(new_id)["status"] == "active"


def test_invalidate_marks_inactive_with_reason() -> None:
    """invalidate 标 status=invalidated + 写 reason"""
    _setup_facts_tmp()
    from stock_trading_agent.engine import temporal_facts
    fid = temporal_facts.record("600519", "VALIDATED", "pnl:-2.5", "亏损出局")
    temporal_facts.invalidate(fid, reason="止损")
    f = temporal_facts.get_fact(fid)
    assert f["status"] == "invalidated"
    assert f.get("invalidated_reason") == "止损"


def test_query_active_excludes_superseded_invalidated() -> None:
    """query_active 默认只返 status=active"""
    _setup_facts_tmp()
    from stock_trading_agent.engine import temporal_facts
    a = temporal_facts.record("002063", "SELECTED", "plan:A", "A")
    b = temporal_facts.record("002063", "SELECTED", "plan:B", "B")
    temporal_facts.supersede(a, b)
    temporal_facts.invalidate(b, reason="测试")
    active = temporal_facts.query_active()
    assert all(f["status"] == "active" for f in active)
    assert not any(f["id"] == a for f in active)
    assert not any(f["id"] == b for f in active)


def test_query_all_include_invalidated() -> None:
    """query_all(include_invalidated=True) 返全量"""
    _setup_facts_tmp()
    from stock_trading_agent.engine import temporal_facts
    a = temporal_facts.record("002063", "SELECTED", "plan:A", "A")
    b = temporal_facts.record("002063", "SELECTED", "plan:B", "B")
    temporal_facts.invalidate(b, reason="测试")
    all_f = temporal_facts.query_all(include_invalidated=True)
    assert len(all_f) == 2
    # 默认 (不传 include_invalidated) 返 active only → 1 条 (a)
    default_f = temporal_facts.query_all()
    assert len(default_f) == 1
    assert default_f[0]["id"] == a
    assert default_f[0]["status"] == "active"


# ─────────── get_stock_lifecycle skill (2 个) ───────────

def test_get_stock_lifecycle_active_filter() -> None:
    """默认只返 active events"""
    _setup_facts_tmp()
    from stock_trading_agent.engine import temporal_facts, skills
    fid_a = temporal_facts.record("002063", "SELECTED", "plan:A", "A")
    fid_b = temporal_facts.record("002063", "SELECTED", "plan:B", "B")
    temporal_facts.supersede(fid_a, fid_b)
    result = skills._run_get_stock_lifecycle({"code": "002063"})
    assert result["count"] == 1
    assert result["events"][0]["id"] == fid_b
    assert result["events"][0]["status"] == "active"


def test_get_stock_lifecycle_include_invalidated() -> None:
    """include_invalidated=True 返全量 (含 superseded/invalidated)"""
    _setup_facts_tmp()
    from stock_trading_agent.engine import temporal_facts, skills
    fid_a = temporal_facts.record("002063", "SELECTED", "plan:A", "A")
    fid_b = temporal_facts.record("002063", "SELECTED", "plan:B", "B")
    temporal_facts.supersede(fid_a, fid_b)
    result = skills._run_get_stock_lifecycle({"code": "002063", "include_invalidated": True})
    assert result["count"] == 2
    statuses = {e["status"] for e in result["events"]}
    assert "superseded" in statuses
    assert "active" in statuses


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    pass_n = 0
    fail_n = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            pass_n += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
            fail_n += 1
    print(f"\n{'OK' if fail_n == 0 else 'FAIL'} {pass_n}/{pass_n+fail_n} tests passed")
    sys.exit(0 if fail_n == 0 else 1)
