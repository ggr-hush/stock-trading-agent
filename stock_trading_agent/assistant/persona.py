"""persona.py — v12 人格加载器

职责:
  - 读 config/persona.yaml
  - 拼成 system prompt 顶部段 (identity + tone_rules + context_preamble)
  - 文件不存在/解析失败 → 返回最小默认人格 (不让整个 LLM 路径挂)

设计取舍:
  - 不用 dataclass/class 包装: 直接 string, 调用方拼到 system prompt 顶部即可
  - cache: 模块级 lru_cache, 改 yaml 需重启 (避免运行时热加载的不一致)
  - 不暴露编辑 CLI: 改 yaml + 重启 supervisor 生效
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

log_path = Path(__file__).parent.parent.parent / "config" / "persona.yaml"

DEFAULT_PERSONA = (
    "你是 A 股选股助手, 跟用户用白话聊, "
    "不熟金融术语就解释, 不确定就说不知道。"
)


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


def load_persona() -> str:
    """读 yaml 拼成 system prompt 顶部段 (≤ ~600 tokens)"""
    data = _load_yaml()
    if not data:
        return DEFAULT_PERSONA

    parts: list[str] = []
    # identity 段 (必有)
    identity = data.get("identity")
    if isinstance(identity, str) and identity.strip():
        parts.append(identity.strip())

    # tone_rules 段 (list[str])
    rules = data.get("tone_rules")
    if isinstance(rules, list) and rules:
        rule_lines = [f"- {r}" for r in rules if isinstance(r, str) and r.strip()]
        if rule_lines:
            parts.append("语气规则:\n" + "\n".join(rule_lines))

    # context_preamble 段 (可选)
    preamble = data.get("context_preamble")
    if isinstance(preamble, str) and preamble.strip():
        parts.append(preamble.strip())

    if not parts:
        return DEFAULT_PERSONA
    return "\n\n".join(parts)


def reload() -> None:
    """v12: 占位, 给历史 API 兼容 (实际不再用 lru_cache, 改文件后下次 load 自动重读)"""
    pass
