"""feishu/listener.py — 飞书事件订阅监听 (v10: 不依赖 lark-cli)

直接用 lark-oapi Python SDK 走 WebSocket 长连接:
  - .env 里有 FEISHU_APP_ID/SECRET 就启动
  - 不依赖 lark-cli / keychain / 任何 CLI 工具
  - 装一次: pip install lark-oapi
"""
from __future__ import annotations

import json
import logging
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("feishu.listener")

EVENT_KEY = "im.message.receive_v1"
# ────────── v12.8: dup skip 可观测化 ──────────
# 写 bot_sessions system note + 计数器 JSON, 不阻塞主流程
_DEDUP_STATS_PATH = Path(__file__).parent.parent.parent / "data" / "dedup_stats.json"


def _record_dup_skip(chat_id: str, message_id: str, text: str) -> None:
    """v12.8: dup skip 时落 2 处:
      1. bot_sessions 写 system note "[dup skip] {text}" (历史可查)
      2. data/dedup_stats.json 计数器 +1 (今日累计 + 5min 窗口)
    失败 → 静默, 不影响主流程
    """
    # 1) session note
    try:
        from ..engine.sessions import append_turn as _session_append
        _session_append(chat_id, "system", f"[dup skip] {text[:100]}")
    except Exception as e:  # noqa: BLE001
        log.debug("dup skip session note 失败 (忽略): %s", e)
    # 2) 计数器
    try:
        import json as _json
        from datetime import datetime as _dt
        now = _dt.now()
        stats: dict = {}
        if _DEDUP_STATS_PATH.exists():
            try:
                stats = _json.loads(_DEDUP_STATS_PATH.read_text(encoding="utf-8"))
            except Exception:
                stats = {}
        today = now.strftime("%Y-%m-%d")
        if stats.get("date") != today:
            stats = {"date": today, "today_count": 0, "recent_5min": []}
        stats["today_count"] = stats.get("today_count", 0) + 1
        # recent_5min: 滚动 5min 内时间戳列表
        recent = [t for t in stats.get("recent_5min", [])
                  if (now.timestamp() - t) < 300]
        recent.append(now.timestamp())
        stats["recent_5min"] = recent
        _DEDUP_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DEDUP_STATS_PATH.write_text(_json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("dup skip 计数器写失败 (忽略): %s", e)

# ────────── 消息去重缓存 (v12.5.1 + v12.A.2) ──────────
# 根因: lark-oapi 5xx / WebSocket reconnect 会重投同 message_id 的事件
# 解决: 模块级 LRU + TTL 10min, 覆盖飞书 5xx 重投 + 16min 断线回放窗口
# v12.A.2: 加文件 fallback, 跨进程持久 (agent 重启不丢)
_DEDUP_TTL_S = 600
_DEDUP_MAX = 2000
_seen_msgs: dict = {}
_seen_lock = threading.Lock()
_SEEN_FILE = _DEDUP_STATS_PATH.parent / "dedup_seen.json"


def _load_seen_from_disk() -> dict[str, float]:
    """v12.A.2: 启动时从文件加载 seen_msgs, 跨进程持久"""
    if not _SEEN_FILE.exists():
        return {}
    try:
        import json as _json
        cutoff = time.time() - _DEDUP_TTL_S
        data = _json.loads(_SEEN_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if v > cutoff}
    except Exception as e:  # noqa: BLE001
        log.debug("load seen file 失败 (忽略): %s", e)
        return {}


def _save_seen_to_disk() -> None:
    """v12.A.2: 周期性落盘, 跨重启保留 dedup 状态
    不在每次 mark_seen 都写 (磁盘 IO 抖动), 改成每 30 条写一次
    """
    try:
        import json as _json
        _SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 写前先清过期
        cutoff = time.time() - _DEDUP_TTL_S
        fresh = {k: v for k, v in _seen_msgs.items() if v > cutoff}
        _SEEN_FILE.write_text(_json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("save seen file 失败 (忽略): %s", e)


# v12.A.2: 启动时从磁盘加载 (一次, 避免每次 process 都重头开始)
_seen_msgs.update(_load_seen_from_disk())
_seen_write_counter = 0


def _mark_seen(message_id: str) -> bool:
    """返回 True 表示新消息应处理, False 表示重复应跳过

    v12.A.2: 每 30 次写一次文件, 跨重启保留 dedup 状态
    """
    global _seen_write_counter
    now = time.time()
    with _seen_lock:
        if len(_seen_msgs) > _DEDUP_MAX:
            cutoff = now - _DEDUP_TTL_S
            for mid in [k for k, t in list(_seen_msgs.items()) if t < cutoff]:
                _seen_msgs.pop(mid, None)
        if message_id in _seen_msgs:
            return False
        _seen_msgs[message_id] = now
        if len(_seen_msgs) > _DEDUP_MAX:
            try:
                oldest = min(_seen_msgs, key=_seen_msgs.get)
                _seen_msgs.pop(oldest, None)
            except ValueError:
                pass
        # v12.A.2: 每 30 次写一次盘, 避免 IO 抖动
        _seen_write_counter += 1
        if _seen_write_counter >= 30:
            _seen_write_counter = 0
            _save_seen_to_disk()
        return True




# ─────────── 文本提取 / mention 处理 ───────────

def _extract_text(content: str, message_type: str) -> str:
    if message_type != "text":
        return ""
    try:
        parsed = json.loads(content)
        return parsed.get("text", "").strip()
    except Exception:
        return content.strip()


def _strip_mention(text: str) -> str:
    """旧 lark-cli 风格的 @_user_X mention 剥除"""
    return re.sub(r"@_user_\d+\s*", "", text).strip()


def _strip_mention_lark(text: str, mentions) -> str:
    """v10: 用 lark-oapi mentions 列表剥除 @ 提及

    mentions: List[MentionEvent] (每个含 .key)
    把 text 里 "@key" 形式的提及剥掉。
    """
    if not mentions:
        return text.strip()
    for m in mentions:
        key = getattr(m, "key", None)
        if key and key in text:
            text = text.replace(f"@{key}", "", 1)
    return text.strip()


# ─────────── 白名单 / 黑名单 / admin ───────────

def _is_chat_allowed(chat_id: str, sender_id: str, config: dict) -> tuple[bool, str]:
    feishu_cfg = config.get("feishu", {})
    mode = feishu_cfg.get("whitelist_mode", "off")
    whitelist = set(feishu_cfg.get("whitelist_chat_ids", []))
    blacklist = set(feishu_cfg.get("blacklist_chat_ids", []))
    allowed_users = set(feishu_cfg.get("allowed_user_ids", []))

    if chat_id in blacklist:
        return False, f"chat {chat_id} 在黑名单"
    if mode == "whitelist":
        if chat_id not in whitelist:
            return False, f"chat {chat_id} 不在白名单 (mode=whitelist)"
    if allowed_users and sender_id not in allowed_users:
        return False, f"sender {sender_id} 不在 allowed_user_ids"
    return True, "ok"


def _is_admin(sender_id: str, config: dict) -> bool:
    admins = set(config.get("feishu", {}).get("admin_user_ids", []))
    return sender_id in admins if admins else True


# ─────────── 推回原 chat (用 lark-oapi client) ───────────

def _send_reply(client, chat_id: str, text_or_card: Any,
                 msg_type: str = "text") -> dict[str, Any]:
    """用 lark-oapi client 把文本/卡片推回 chat_id

    msg_type="text" → text_or_card 是 str
    msg_type="interactive" → text_or_card 是 card dict
    """
    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if msg_type == "interactive" and isinstance(text_or_card, dict):
            content = text_or_card  # card dict 整段当 content
        else:
            content = {"text": text_or_card if isinstance(text_or_card, str) else str(text_or_card)}
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type(msg_type) \
            .content(json.dumps(content, ensure_ascii=False)) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()
        resp = client.im.v1.message.create(req)
        if not resp.success():
            return {"ok": False, "error": f"code={resp.code} msg={resp.msg}"}
        return {"ok": True, "message_id": resp.data.message_id}
    except Exception as e:
        log.warning("_send_reply 失败: %s: %s", type(e).__name__, str(e)[:200])
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ─────────── 兼容层: 旧 lark-cli 风格 _handle_event (test_v3 用) ───────────

def _handle_event(event: dict, lark_cli: str = "/dev/null") -> dict:
    """v10 之前的 lark-cli 风格 handler, 给 test_v3 兼容用

    内部把 dict event 当成原始 lark-cli NDJSON 处理。
    _send_reply 走 lark-cli 路径 (subprocess), 实际不再用, 但保留签名让 v3 测试不破。
    """
    if event.get("type") != EVENT_KEY:
        return {"skipped": "not message event"}
    msg_type = event.get("message_type", "")
    if msg_type != "text":
        return {"skipped": f"unsupported message_type: {msg_type}"}
    chat_id = event.get("chat_id", "")
    message_id = event.get("message_id") or event.get("id", "")
    sender_id = event.get("sender_id", "")
    text = _strip_mention(_extract_text(event.get("content", ""), msg_type))
    if not text:
        return {"skipped": "empty text after mention strip"}
    if not chat_id or not message_id:
        return {"skipped": "missing chat_id or message_id"}

    from ..engine.data_fetcher import load_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    allowed, reason = _is_chat_allowed(chat_id, sender_id, cfg)
    if not allowed:
        return {"skipped": f"blocked: {reason}"}

    from ..engine.paper_trader import init_account
    from ..llm.tool_use import dispatch
    init_account()
    try:
        from ..agent import _recent_picks_for_question, _latest_market_env
        result = dispatch(
            text,
            recent_picks=_recent_picks_for_question(),
            market_env=_latest_market_env(),
        )
        answer = result.get("card", {}).get("content", {}).get("text", "") or "（无回复）"
    except Exception as e:
        answer = f"（处理失败: {e}）"
    send_result = _send_reply_legacy(message_id, answer, lark_cli)
    return {"chat_id": chat_id, "message_id": message_id, "answer_len": len(answer), "send": send_result}


def _send_card(client, chat_id: str, text_or_card: Any, msg_type: str = "text") -> dict[str, Any]:
    """v11/v12.9.1: 发送 card

    msg_type="text" → text_or_card 是 str, 发纯文本
    msg_type="interactive" → text_or_card 是 card dict (飞书 interactive card JSON)
    """
    return _send_reply(client, chat_id, text_or_card, msg_type=msg_type)


def _send_reply_legacy(message_id: str, text: str, lark_cli: str) -> dict:
    """旧 subprocess 版, 保留给 test_v3 的 mock 用 (实际不调真 lark-cli)"""
    return {"ok": True, "mocked": True, "text_preview": text[:50]}


# ─────────── 事件 handler (lark-oapi 风格) ───────────

def _make_handler(client, get_config):
    """注册 lark-oapi 的 on_message 回调"""
    from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

    def on_message(data: P2ImMessageReceiveV1) -> None:
        event = data.event
        if event is None or event.message is None:
            return
        msg = event.message
        msg_type = msg.message_type
        if msg_type != "text":
            log.debug("skip non-text msg_type=%s", msg_type)
            return
        chat_id = msg.chat_id
        message_id = msg.message_id
        text = _strip_mention_lark(_extract_text(msg.content or "", msg_type), msg.mentions or [])
        sender_id = ""
        if event.sender and event.sender.sender_id:
            sid = event.sender.sender_id
            sender_id = (sid.open_id or sid.user_id or sid.union_id or "")

        if not text or not chat_id or not message_id:
            return

        # v12.5.1: message_id 去重 (lark-oapi 5xx / reconnect 重投同一事件)
        # v12.8: 升级 warning + 写 bot_sessions system note + 计数器 +1
        if not _mark_seen(message_id):
            log.info("dup skip: chat=%s msg=%s text=%r",
                        chat_id, message_id, text[:60])
            _record_dup_skip(chat_id, message_id, text)
            return

        cfg = get_config() or {}
        allowed, reason = _is_chat_allowed(chat_id, sender_id, cfg)
        if not allowed:
            log.info("blocked by whitelist: %s", reason)
            return

        log.info("event: chat=%s sender=%s msg=%s text=%r",
                 chat_id, sender_id, message_id, text[:60])

        # v12: 检测"想被记住"信号 (写入记忆, 在 dispatch 前, 不阻塞主流程)
        try:
            from ..assistant.memory import detect_memory_signal, remember as _remember
            sig = detect_memory_signal(text)
            if sig:
                mem_type, mem_content, mem_source = sig
                _remember(chat_id, mem_content, type=mem_type, source=mem_source,
                          importance=2 if mem_type == "preference" else 1)
                log.info("memory: chat=%s type=%s content=%s", chat_id, mem_type, mem_content[:40])
        except Exception as e:  # noqa: BLE001
            log.debug("memory detect/write 失败 (忽略): %s", e)

        # v12: 写 user 消息到 session (在 dispatch 前, 失败不影响主流程)
        try:
            from ..engine.sessions import append_turn as _session_append
            _session_append(chat_id, "user", text)
        except Exception as e:  # noqa: BLE001
            log.debug("session append_turn(user) 失败 (忽略): %s", e)

        # v12.9.1: admin 斜杠命令早返回, 不进 LLM dispatch (省 token + 快)
        if text.startswith("/"):
            from .admin_cmd import handle as _admin_handle
            admin_card = _admin_handle(text, sender_id, chat_id, cfg or {})
            if admin_card is not None:
                card = admin_card
                text_reply = card.get("content", {}).get("text", "")
                if not text_reply and card.get("msg_type") == "interactive":
                    text_reply = "(interactive card)"  # 占位, 实际用 _send_card
                send_result = _send_card(client, chat_id, card.get("content", card), card.get("msg_type", "text"))
                log.info("admin cmd replied: ok=%s cmd=%s", send_result.get("ok"), text[:30])
                return  # 早返回, 不走 dispatch

        from ..engine.paper_trader import init_account
        from ..llm.tool_use import dispatch
        init_account()
        try:
            from ..agent import _recent_picks_for_question, _latest_market_env
            result = dispatch(
                text,
                recent_picks=_recent_picks_for_question(),
                market_env=_latest_market_env(),
                chat_id=chat_id,
            )
        except Exception as e:
            log.exception("dispatch 失败: %s", e)
            result = {"ok": False, "path": "exception", "card": {"msg_type": "text", "content": {"text": f"（处理失败: {e}）"}}, "error": str(e)}

        log.info("dispatch path=%s ok=%s tool_calls=%d", result.get("path"), result.get("ok"), len(result.get("tool_calls", [])))
        card = result.get("card") or {"msg_type": "text", "content": {"text": "（无回复）"}}
        text_reply = card.get("content", {}).get("text", "") or "（无回复）"
        msg_type = card.get("msg_type", "text")
        send_result = _send_card(client, chat_id, text_reply, msg_type)
        log.info("replied: ok=%s err=%s path=%s", send_result.get("ok"), send_result.get("error", ""), result.get("path"))

        # v12: 写 assistant 回复到 session (在 _send_card 之后, 失败不影响主流程)
        try:
            from ..engine.sessions import append_turn as _session_append
            _session_append(chat_id, "assistant", text_reply)
        except Exception as e:  # noqa: BLE001
            log.debug("session append_turn(assistant) 失败 (忽略): %s", e)

    return on_message


# ─────────── 入口 ───────────

def run(stop_after: int | None = None, quiet: bool = False) -> None:
    """启飞书 WebSocket 长连接, 阻塞直到 Ctrl+C

    v10: 不再依赖 lark-cli / keychain。直接读 .env APP_ID/SECRET 调
    lark-oapi SDK 起 WebSocket 长连接, 启动即连接。
    """
    from ..engine.paper_trader import init_account
    from ..engine.data_fetcher import _secret
    init_account()

    try:
        app_id = _secret("FEISHU_APP_ID")
        app_secret = _secret("FEISHU_APP_SECRET")
    except RuntimeError as e:
        raise RuntimeError(f"FEISHU_APP_ID / FEISHU_APP_SECRET 未设 (检查 .env): {e}") from e
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET 未设 (检查 .env)")

    try:
        import lark_oapi as lark
    except ImportError:
        raise RuntimeError(
            "lark-oapi SDK 未装。请先:\n"
            "  pip install lark-oapi\n"
            "然后再跑 agent listen。"
        )

    from lark_oapi.core.enum import LogLevel
    log_level = LogLevel.WARNING if quiet else LogLevel.INFO

    client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(log_level) \
        .build()

    from ..engine.data_fetcher import load_config
    def get_config():
        try:
            return load_config()
        except Exception:
            return {}

    on_message = _make_handler(client, get_config)

    # v11: 非 receive 事件统一走 noop handler, 消除 "processor not found" ERROR 日志
    def _noop(_data) -> None:
        log.debug("event ignored (noop)")

    handler = lark.EventDispatcherHandler.builder(
        encrypt_key="",
        verification_token="",
        level=log_level,
    ).register_p2_im_message_receive_v1(on_message) \
     .register_p2_im_message_reaction_created_v1(_noop) \
     .register_p2_im_message_reaction_deleted_v1(_noop) \
     .register_p2_im_message_message_read_v1(_noop) \
     .register_p2_im_message_recalled_v1(_noop) \
     .build()

    ws_client = lark.ws.Client(
        app_id, app_secret,
        event_handler=handler,
        log_level=log_level,
    )

    # 信号注册留给主线程 (signal.signal 只能在 main thread)
    # 主线程 (agent._run_supervisor 或 agent start) 会先注册 SIGINT/SIGTERM

    log.info("启动飞书事件监听 (lark-oapi WebSocket): app_id=%s, key=%s",
             app_id[:8] + "***", EVENT_KEY)
    log.info("按 Ctrl+C 退出")
    ws_client.start()
