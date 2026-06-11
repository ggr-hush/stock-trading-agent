"""
feishu/bot.py — 飞书机器人 v1 stub (DEPRECATED, v12.5.2)

真实消息处理已统一在 feishu/listener.py 的 on_message 路径 (v10+)。
本文件保留仅为兼容早期 import, 调用时会打 DeprecationWarning。

v13 计划删除。
"""
from __future__ import annotations

import warnings
from typing import Any


def handle_message(chat_id: str, text: str) -> dict[str, Any]:
    """v1 stub: 调用方应迁移到 feishu.listener._make_handler 路径"""
    warnings.warn(
        "feishu.bot.handle_message 已废弃, v13 删除。"
        "请用 stock_trading_agent.feishu.listener + llm.tool_use.dispatch",
        DeprecationWarning,
        stacklevel=2,
    )
    return {"chat_id": chat_id, "text": f"[bot stub] 收到: {text}"}
