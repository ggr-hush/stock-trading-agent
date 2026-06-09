"""
feishu/bot.py — 飞书机器人（v2 stub）
v1 不启用；预留路由接口供 v2 接入
"""
from __future__ import annotations

from typing import Any


def handle_message(chat_id: str, text: str) -> dict[str, Any]:
    """v2: 接收飞书消息 → LLM 路由 → 返回响应

    v1 stub: 直接 echo
    """
    return {"chat_id": chat_id, "text": f"[bot stub] 收到: {text}"}
