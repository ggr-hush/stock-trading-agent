"""persona.py — v12 人格加载器

职责:
  - 读 config/persona.yaml
  - 拼成 system prompt 顶部段 (identity + tone_rules + context_preamble + 5 段 v12.8 加厚)
  - 文件不存在/解析失败 → 返回最小默认人格 (不让整个 LLM 路径挂)

设计取舍:
  - 不用 dataclass/class 包装: 直接 string, 调用方拼到 system prompt 顶部即可
  - 不做 lru_cache: 改 yaml 后下次 load 自动重读 (操作员改完等下次 dispatch 生效, 不用重启)
  - 不暴露编辑 CLI: 改 yaml + 下条消息生效 (避免运行时热加载不一致)

v12.8 加厚:
  - few_shots: 用户问→盘盘答 对话范例
  - glossary: 常用术语白话注释
  - boundary_rules: 边界规则
  - style_examples: 好/坏回复对比
  - fallback_phrases: LLM 不可用/空响应时随机挑 1
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Callable

import yaml

log_path = Path(__file__).parent.parent.parent / "config" / "persona.yaml"

DEFAULT_PERSONA = (
    "你是 A 股选股助手, 跟用户用白话聊, "
    "不熟金融术语就解释, 不确定就说不知道。"
)

# v12.8: fallback_phrases 默认兜底 (yaml 缺时用)
DEFAULT_FALLBACK_PHRASES = [
    "我没拿到这个数据, 换个说法或加个关键词试试?",
    "这个问题我还在学, 试试问我 今日选股 / 持仓 / 日报 看看?",
    "我没理解你的意思, 描述具体点? (比如 '今日选股' / '茅台怎么样')",
]


def _load_yaml() -> dict[str, Any]:
    """读 yaml; 文件不在或解析失败 → 返回空 dict (走默认人格)"""
    try:
        if not log_path.exists():
            return {}
        with log_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _format_list_section(parts: list[str], key: str, header: str, formatter: Callable[[str], str] | None = None) -> None:
    """v12.8: 通用 list[str] 段格式化器 (容错非字符串/空字符串)

    formatter: 可选, 接受单条 string 返回格式化的 string
    """
    items = data_get(_load_yaml(), key, default=[])  # noqa: F821 - 占位, 实际下面用闭包
    if not items or not isinstance(items, list):
        return
    lines: list[str] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            if formatter:
                lines.append(formatter(it.strip()))
            else:
                lines.append(f"- {it.strip()}")
    if lines:
        parts.append(header + "\n" + "\n".join(lines))


# 上面是早期思考, 改用下面这个干净实现
def _format_list(parts: list[str], data: dict[str, Any], key: str, header: str) -> None:
    """v12.8: 通用 list[str] 段格式化器

    - data 缺 key → 跳过 (向后兼容老 yaml)
    - list 里非字符串/空字符串 → 跳过 (容错)
    """
    items = data.get(key)
    if not items or not isinstance(items, list):
        return
    lines = [f"- {it.strip()}" for it in items if isinstance(it, str) and it.strip()]
    if lines:
        parts.append(header + "\n" + "\n".join(lines))


def load_persona() -> str:
    """读 yaml 拼成 system prompt 顶部段 (≤ ~1200 tokens)

    拼装顺序 (固定):
      1. identity          — 身份
      2. tone_rules        — 语气规则
      3. context_preamble  — A 股上下文 + 工具集 + 行为模式
      4. few_shots         — 对话范例
      5. glossary          — 术语白话
      6. boundary_rules    — 边界
      7. style_examples    — 好/坏对比
      # 注意: fallback_phrases 不进 system prompt, 是给 dispatch 兜底用
    """
    data = _load_yaml()
    if not data:
        return DEFAULT_PERSONA

    parts: list[str] = []

    # 1) identity (必有)
    identity = data.get("identity")
    if isinstance(identity, str) and identity.strip():
        parts.append(identity.strip())

    # 2) tone_rules (list[str])
    rules = data.get("tone_rules")
    if isinstance(rules, list) and rules:
        rule_lines = [f"- {r}" for r in rules if isinstance(r, str) and r.strip()]
        if rule_lines:
            parts.append("语气规则:\n" + "\n".join(rule_lines))

    # 3) context_preamble (可选)
    preamble = data.get("context_preamble")
    if isinstance(preamble, str) and preamble.strip():
        parts.append(preamble.strip())

    # 4) few_shots (v12.8 新增)
    _format_list(parts, data, "few_shots", "对话范例 (模仿这种口吻):")

    # 5) glossary (v12.8 新增)
    _format_list(parts, data, "glossary", "常用术语白话注释:")

    # 6) boundary_rules (v12.8 新增)
    _format_list(parts, data, "boundary_rules", "边界规则:")

    # 7) style_examples (v12.8 新增)
    _format_list(parts, data, "style_examples", "好回复 vs 坏回复 (学好的):")

    if not parts:
        return DEFAULT_PERSONA
    return "\n\n".join(parts)


def load_fallback_phrases() -> list[str]:
    """v12.8: 读 yaml.fallback_phrases, 给 dispatch() 空响应兜底用

    yaml 缺 / 解析失败 / 全是非字符串 → 走 DEFAULT_FALLBACK_PHRASES
    """
    data = _load_yaml()
    if not data:
        return list(DEFAULT_FALLBACK_PHRASES)
    items = data.get("fallback_phrases")
    if not isinstance(items, list):
        return list(DEFAULT_FALLBACK_PHRASES)
    cleaned = [it.strip() for it in items if isinstance(it, str) and it.strip()]
    return cleaned if cleaned else list(DEFAULT_FALLBACK_PHRASES)


def pick_fallback_phrase(rng: random.Random | None = None) -> str:
    """v12.8: 随机选 1 句 fallback 话术

    rng: 注入的 random.Random 实例, 测试时固定 seed 验证
    """
    phrases = load_fallback_phrases()
    r = rng or random
    return r.choice(phrases)


def reload() -> None:
    """v12: 占位, 给历史 API 兼容 (实际不再用 lru_cache, 改文件后下次 load 自动重读)"""
    pass
