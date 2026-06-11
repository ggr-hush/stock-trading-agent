"""agent/webhook.py — v12.6 飞书 webhook HTTP 服务

原 agent.py:_WebhookHandler + run_webhook。
chat 路径用 chat_with_session (reasoner 里的 session-aware LLM 入口)。
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from ..engine.paper_trader import init_account
from ..llm.reasoner import chat_with_session
from .stages import _latest_market_env, _recent_picks_for_question

log = logging.getLogger("agent.webhook")


class _WebhookHandler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "service": "stock_trading_agent"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._json(400, {"error": f"bad json: {e}"})
            return
        if path == "/chat":
            question = data.get("question", "").strip()
            if not question:
                self._json(400, {"error": "question is required"})
                return
            session_id = data.get("session_id") or self.headers.get("X-Session-Id") or "default"
            log.info("chat Q [%s]: %s", session_id, question[:80])
            try:
                answer = chat_with_session(
                    session_id,
                    question,
                    recent_picks=_recent_picks_for_question(),
                    market_env=_latest_market_env(),
                )
                if not answer:
                    answer = "（LLM 暂不可用, 请检查 MINIMAX_API_KEY; 知识库检索可单跑 python -m stock_trading_agent.engine.knowledge <query>）"
            except Exception as e:
                answer = f"（处理失败: {e}）"
            self._json(200, {"question": question, "session_id": session_id, "answer": answer})
        elif path == "/reset":
            session_id = data.get("session_id") or self.headers.get("X-Session-Id") or "default"
            from ..engine.sessions import reset as _reset
            _reset(session_id)
            log.info("reset session: %s", session_id)
            self._json(200, {"session_id": session_id, "reset": True})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        log.info(fmt, *args)


def run_webhook(host: str = "127.0.0.1", port: int = 8765) -> None:
    init_account()
    server = ThreadingHTTPServer((host, port), _WebhookHandler)
    log.info("webhook 启动: http://%s:%d", host, port)
    log.info("  POST /chat {\"question\": \"...\"}")
    log.info("  GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("webhook 关闭")
        server.shutdown()
