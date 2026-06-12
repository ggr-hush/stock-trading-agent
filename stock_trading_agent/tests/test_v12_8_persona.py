"""test_v12_8_persona.py — v12.8 persona 加厚 5 段

Covers:
  - yaml 加载 8 段都拼出来 (含 v12.8 新 5 段)
  - 缺新键 (老 yaml) 走默认空 list 不报错
  - few_shots 列表里混非字符串时容错
  - glossary 长度 0 / 正常 / 超长都正常拼
  - boundary_rules 顺序固定 (identity → tone_rules → context_preamble → few_shots → ... → style_examples)
  - fallback_phrases 3 句时随机选 1
"""
from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _reload_persona_module():
    """测试间清空 module cache, 避免 _load_yaml 缓存假数据"""
    import importlib
    from stock_trading_agent.assistant import persona
    importlib.reload(persona)
    return persona


def test_load_full_yaml_8_sections() -> None:
    """完整 yaml → 8 段都拼出来"""
    persona = _reload_persona_module()
    text = persona.load_persona()
    assert "盘盘" in text, "identity 段缺失"
    assert "语气规则" in text, "tone_rules 段缺失"
    assert "A 股硬约束" in text, "context_preamble 段缺失"
    # v12.8 5 段
    assert "对话范例" in text, "few_shots 缺失"
    assert "术语" in text, "glossary 缺失"
    assert "边界规则" in text, "boundary_rules 缺失"
    assert "好回复" in text, "style_examples 缺失"
    print("  PASS test_load_full_yaml_8_sections")


def test_missing_new_keys_compatible() -> None:
    """老 yaml (只有 3 老段) → v12.8 加载不报错, 仍能拼出 3 老段"""
    persona = _reload_persona_module()
    # 模拟老 yaml: 临时覆盖 yaml 文件
    old_yaml = persona.log_path
    try:
        persona.log_path = Path(tempfile.mkdtemp()) / "persona.yaml"
        persona.log_path.parent.mkdir(exist_ok=True)
        persona.log_path.write_text(
            "identity: |\n  老 identity\n"
            "tone_rules:\n  - 老规则 1\n"
            "context_preamble: |\n  老 preamble\n",
            encoding="utf-8",
        )
        text = persona.load_persona()
        assert "老 identity" in text
        assert "老规则 1" in text
        assert "老 preamble" in text
        # 5 个新段全部缺失, 不该有 "对话范例" 之类的字样
        assert "对话范例" not in text
    finally:
        persona.log_path = old_yaml
    print("  PASS test_missing_new_keys_compatible")


def test_few_shots_mixed_types_tolerant() -> None:
    """few_shots 里混非字符串时跳过非字符串, 不抛异常"""
    persona = _reload_persona_module()
    old_yaml = persona.log_path
    try:
        persona.log_path = Path(tempfile.mkdtemp()) / "persona.yaml"
        persona.log_path.parent.mkdir(exist_ok=True)
        persona.log_path.write_text(
            "identity: |\n  i\n"
            "few_shots:\n  - '正常条目'\n  - 123\n  - ''\n  - null\n  - '另一条'\n",
            encoding="utf-8",
        )
        text = persona.load_persona()
        assert "正常条目" in text
        assert "另一条" in text
        # 非字符串不能泄漏
        assert "123" not in text.split("对话范例")[1] or "对话范例" not in text
    finally:
        persona.log_path = old_yaml
    print("  PASS test_few_shots_mixed_types_tolerant")


def test_glossary_lengths() -> None:
    """glossary 0 条 / 正常 / 超长都正常拼"""
    persona = _reload_persona_module()
    old_yaml = persona.log_path
    try:
        # 0 条
        persona.log_path = Path(tempfile.mkdtemp()) / "persona.yaml"
        persona.log_path.parent.mkdir(exist_ok=True)
        persona.log_path.write_text("identity: |\n  i\n", encoding="utf-8")
        text = persona.load_persona()
        assert "术语" not in text

        # 正常
        persona.log_path.write_text(
            "identity: |\n  i\n"
            "glossary:\n  - 'PE: 市盈率'\n  - '换手率: 比例'\n",
            encoding="utf-8",
        )
        text = persona.load_persona()
        assert "PE: 市盈率" in text
        assert "换手率: 比例" in text

        # 超长 (20 条)
        big = "\n".join([f"  - '术语{i}: 释义{i}'" for i in range(20)])
        persona.log_path.write_text(
            f"identity: |\n  i\nglossary:\n{big}\n",
            encoding="utf-8",
        )
        text = persona.load_persona()
        assert "术语0:" in text
        assert "术语19:" in text
    finally:
        persona.log_path = old_yaml
    print("  PASS test_glossary_lengths")


def test_section_order_fixed() -> None:
    """段顺序固定: identity → tone_rules → context_preamble → few_shots → glossary → boundary_rules → style_examples"""
    persona = _reload_persona_module()
    text = persona.load_persona()
    idx_identity = text.find("盘盘")
    idx_tone = text.find("语气规则")
    idx_context = text.find("A 股硬约束")
    idx_few = text.find("对话范例")
    idx_glossary = text.find("常用术语白话注释")
    idx_boundary = text.find("边界规则:")
    idx_style = text.find("好回复 vs 坏回复")
    assert 0 < idx_identity < idx_tone < idx_context < idx_few < idx_glossary < idx_boundary < idx_style, (
        f"段顺序错: identity={idx_identity} tone={idx_tone} context={idx_context} "
        f"few={idx_few} glossary={idx_glossary} boundary={idx_boundary} style={idx_style}"
    )
    print("  PASS test_section_order_fixed")


def test_pick_fallback_phrase_deterministic_with_seed() -> None:
    """fallback_phrases 3 句时, 注入 random.Random(seed=42) 应可复现"""
    persona = _reload_persona_module()
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    a = persona.pick_fallback_phrase(rng=rng1)
    b = persona.pick_fallback_phrase(rng=rng2)
    assert a == b, f"相同 seed 应返相同结果, got {a!r} vs {b!r}"
    assert a and isinstance(a, str)
    print("  PASS test_pick_fallback_phrase_deterministic_with_seed")


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            fail += 1
    print(f"\n{'✓' if fail == 0 else '✗'} {len(tests) - fail}/{len(tests)} tests passed")
    sys.exit(0 if fail == 0 else 1)
