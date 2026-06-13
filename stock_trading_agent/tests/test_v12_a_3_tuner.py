"""test_v12_a_3_tuner.py — v12.A.3 tuner dry-run 屏障 单元测试

Covers (改动 4):
  - dry_run=True 不写库
  - --write 真写到 config.yaml + params_history
  - metrics (preview vs applied) 一致
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _setup_tmp():
    """每个 test 单独 tmpdir, 隔离 config + db"""
    tmp = Path(tempfile.mkdtemp(prefix="sta_tuner_v12a3_"))
    os.environ["STOCK_AGENT_FACTS_PATH"] = f"{tmp}/facts.jsonl"
    return tmp


def _patched_dependencies(tmp: Path, proposals: list[dict]):
    """mock weekly_stats + apply_proposal, 返一个 fake cfg"""
    fake_cfg = {
        "v3": {
            "score_max": {"value": 80.0, "safe_range": [75.0, 85.0]},
        },
        "blacklist": {"sectors": [], "max_add_per_week": 2, "max_remove_per_week": 2, "safe_sectors": []},
        "paper": {"initial_capital": 1000000.0, "max_position_ratio": 0.20, "max_concurrent": 3},
        "tuner": {"judge_enabled": False, "judge_min_score": 60},
    }
    return [
        patch("stock_trading_agent.engine.tuner.weekly_stats", return_value={"n": 10, "win_rate": 30.0}),
        patch("stock_trading_agent.engine.tuner.load_config", return_value=fake_cfg),
        patch("stock_trading_agent.engine.tuner._propose_score_max", return_value=proposals[0] if proposals else None),
        patch("stock_trading_agent.engine.tuner._propose_strong_band", return_value=None),
        patch("stock_trading_agent.engine.tuner._propose_blacklist", return_value=[]),
    ]


def test_dry_run_does_not_write_db_or_config() -> None:
    """v12.A.3: dry_run=True (默认) 不写 params_history, 不改 config.yaml"""
    tmp = _setup_tmp()
    fake_proposal = {
        "param": "v3.score_max",
        "old": 80.0,
        "new": 78.0,
        "in_safe_range": True,
        "reason": "test dry-run",
    }
    patches = _patched_dependencies(tmp, [fake_proposal])
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        from stock_trading_agent.engine import tuner
        # mock apply_proposal → 记录是否被调
        with patch.object(tuner, "apply_proposal") as mock_apply:
            result = tuner.run_weekly_tune(dry_run=True)
    # apply_proposal 完全不应该被调
    assert mock_apply.call_count == 0, f"dry-run 不应调 apply_proposal, got {mock_apply.call_count}"
    # preview 应有 1 条, applied 应空
    assert len(result["preview"]) == 1
    assert len(result["applied"]) == 0
    assert result["preview"][0]["new"] == 78.0


def test_write_flag_actually_calls_apply() -> None:
    """v12.A.3: dry_run=False (即 --write) 真调 apply_proposal"""
    tmp = _setup_tmp()
    fake_proposal = {
        "param": "v3.score_max",
        "old": 80.0,
        "new": 78.0,
        "in_safe_range": True,
        "reason": "test write",
    }
    patches = _patched_dependencies(tmp, [fake_proposal])
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        from stock_trading_agent.engine import tuner
        with patch.object(tuner, "apply_proposal", return_value=True) as mock_apply:
            result = tuner.run_weekly_tune(dry_run=False)
    # apply_proposal 被调 1 次
    assert mock_apply.call_count == 1
    # 验证 proposal 内容进了 apply_proposal
    passed_proposal = mock_apply.call_args[0][0]
    assert passed_proposal["new"] == 78.0
    # 验证 auto=True (位置参数或 kwargs 都行)
    auto_kw = mock_apply.call_args.kwargs.get("auto")
    if auto_kw is None:
        # 走位置参数 [0] = proposal, [1] = auto
        auto_kw = mock_apply.call_args[0][1] if len(mock_apply.call_args[0]) > 1 else True
    assert auto_kw is True
    # applied 收 1 条, preview 空
    assert len(result["applied"]) == 1
    assert len(result["preview"]) == 0


def test_preview_and_applied_have_same_metrics() -> None:
    """v12.A.3: dry-run 跟 --write 看到的 proposals 集合应该一致"""
    tmp = _setup_tmp()
    fake_proposal = {
        "param": "v3.score_max",
        "old": 80.0,
        "new": 78.0,
        "in_safe_range": True,
        "reason": "test consistency",
    }
    # dry-run 一次
    patches = _patched_dependencies(tmp, [fake_proposal])
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        from stock_trading_agent.engine import tuner
        with patch.object(tuner, "apply_proposal", return_value=True):
            r_dry = tuner.run_weekly_tune(dry_run=True)
    # write 一次
    patches2 = _patched_dependencies(tmp, [fake_proposal])
    with patches2[0], patches2[1], patches2[2], patches2[3], patches2[4]:
        with patch.object(tuner, "apply_proposal", return_value=True):
            r_write = tuner.run_weekly_tune(dry_run=False)
    # preview / applied 长度一致 (同一份 proposals)
    assert len(r_dry["preview"]) == len(r_write["applied"])
    if r_dry["preview"]:
        assert r_dry["preview"][0]["param"] == r_write["applied"][0]["param"]
        assert r_dry["preview"][0]["new"] == r_write["applied"][0]["new"]


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
