"""
llm/client.py — minimax M3 客户端
- 单例、retry、限流
- 失败降级（返回 None，不抛异常阻塞主流程）
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

from ..engine import data_fetcher
from ..engine.data_fetcher import load_config

log = logging.getLogger("llm.client")


class LLMUnavailable(Exception):
    """LLM 不可用（key 缺失 / 网络失败 / 重试耗尽）"""


def _get_api_key() -> Optional[str]:
    # 优先 os.environ (用户 export), 兜底 load_env() 读 .env
    v = os.environ.get("MINIMAX_API_KEY")
    if v:
        return v
    try:
        # v11: 用 data_fetcher.load_env 形式访问, 便于 unittest.mock patch
        return data_fetcher.load_env().get("MINIMAX_API_KEY")
    except Exception:
        return None


def _get_config() -> dict[str, Any]:
    try:
        return load_config().get("llm", {})
    except Exception:
        return {}


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
    max_tokens: int = 600,
) -> dict[str, Any]:
    """调 minimax M3 聊天接口

    Args:
        messages: [{"role": "user", "content": "..."}]
        temperature, max_tokens: 调参

    Returns:
        {"ok": True, "content": "...", "usage": {prompt_tokens, completion_tokens, total_tokens}, "latency_ms": int}
        或 {"ok": False, "error": "...", "latency_ms": int}
    """
    cfg = _get_config()
    api_base = cfg.get("api_base", "https://api.minimax.chat/v1")
    model = cfg.get("model", "MiniMax-M3")
    timeout = cfg.get("timeout_s", 30)
    max_retries = cfg.get("max_retries", 2)

    api_key = _get_api_key()
    if not api_key:
        return {"ok": False, "error": "MINIMAX_API_KEY not set", "latency_ms": 0}

    url = f"{api_base}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    started = time.time()
    last_err: Optional[str] = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            latency_ms = int((time.time() - started) * 1000)
            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return {
                    "ok": True,
                    "content": content,
                    "usage": {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                    "latency_ms": latency_ms,
                }
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                log.warning("LLM 调用失败 (attempt %d): %s", attempt + 1, last_err)
        except Exception as e:
            latency_ms = int((time.time() - started) * 1000)
            last_err = f"{type(e).__name__}: {e}"
            log.warning("LLM 调用异常 (attempt %d): %s", attempt + 1, last_err)
        if attempt < max_retries:
            time.sleep(2 ** attempt)
    return {"ok": False, "error": last_err or "unknown", "latency_ms": int((time.time() - started) * 1000)}
