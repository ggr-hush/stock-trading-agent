"""feishu/_http.py — 飞书 / OpenAPI 公共 HTTP 客户端

v7.5: 把 pusher 对 requests 的直接依赖收敛到这一层, 测试 / mock 只 patch 公共入口,
避免 `patch("requests.post")` 这种全局脆弱路径。
"""
from __future__ import annotations

from typing import Any

import requests


def http_post(url: str, *, json: dict[str, Any] | None = None,
              params: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None,
              timeout: float = 10.0) -> requests.Response:
    """统一的 POST 包装。失败时仍抛 (调用方负责 try/except 转 ok=False 协议)。"""
    return requests.post(url, json=json, params=params, headers=headers, timeout=timeout)
