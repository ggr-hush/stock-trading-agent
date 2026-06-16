"""llm/tool_use.py — v11 LLM tool-use 路由

封装 minimax M3 (OpenAI 兼容) 的 tool_calls 调用, 给 listener 用:

  1. chat_with_tools(messages, tools) -> LLM 返回的 tool_calls + content
  2. dispatch(text) -> 一个函数, 完成"消息→路由→调 skill→拼回复"全流程

降级策略:
  - LLM 失败 (key 缺/网络挂)  → 关键词 fallback 到 5 个只读 skill
  - LLM 成功但没返回 tool_call → 走 chat_with_session 自由问答
  - tool_call 解析失败          → 降级到关键词
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import requests

from .reasoner import _log_call

log = logging.getLogger("llm.tool_use")


# ─────────── 线程安全: 共享 LLM client (Lock 串行化请求) ───────────
# minimax M3 限流比较敏感, 串行化能避免触发 429

_LLM_LOCK = threading.Lock()


# ─────────── v12.8: freeform 空响应兜底 + 60s 缓存 ───────────
# 避免同 chat_id 短时间内反复触发 LLM 空响应 → 浪费 quota
_EMPTY_CACHE: dict[str, tuple[float, str]] = {}  # chat_id → (ts, fallback_text)
_EMPTY_CACHE_TTL_S = 60
_EMPTY_CACHE_MAX = 500


def _empty_response_fallback(text: str, chat_id: str | None) -> str:
    """v12.8: LLM 返空 content 时的兜底话术

    - 60s 内同 chat_id 重复问 → 走缓存 (避免反复 LLM 调用)
    - chat_id=None (webhook) → 不缓存, 每次走随机
    """
    from ..assistant.persona import pick_fallback_phrase
    if not chat_id:
        return pick_fallback_phrase()
    now = time.time()
    # 清理过期 + LRU 上限
    if len(_EMPTY_CACHE) > _EMPTY_CACHE_MAX:
        cutoff = now - _EMPTY_CACHE_TTL_S
        for k in [k for k, (t, _) in list(_EMPTY_CACHE.items()) if t < cutoff]:
            _EMPTY_CACHE.pop(k, None)
    cached = _EMPTY_CACHE.get(chat_id)
    if cached and (now - cached[0]) < _EMPTY_CACHE_TTL_S:
        return cached[1]
    fb = pick_fallback_phrase()
    _EMPTY_CACHE[chat_id] = (now, fb)
    return fb


def _llm_payload(messages: list[dict[str, str]], tools: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
    """构造 OpenAI 兼容的 chat/completions payload (含 tools)"""
    from .client import _get_config
    cfg = _get_config()
    payload: dict[str, Any] = {
        "model": cfg.get("model", "MiniMax-M3"),
        "messages": messages,
        "temperature": kwargs.get("temperature", 0.4),
        "max_tokens": kwargs.get("max_tokens", 600),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _parse_tool_calls(choice_message: dict[str, Any]) -> list[dict[str, Any]]:
    """从 LLM 返回的 message 里抽 tool_calls, 解析 arguments JSON"""
    raw_calls = choice_message.get("tool_calls") or []
    parsed: list[dict[str, Any]] = []
    for tc in raw_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("tool_call %s arguments 解析失败: %s args=%r", name, e, args_str[:100])
            args = {}
        parsed.append({"name": name, "args": args, "id": tc.get("id", "")})
    return parsed


def chat_with_tools(
    messages: list[dict[str, str]],
    tools: list[dict[str, Any]] | None = None,
    *,
    temperature: float = 0.4,
    max_tokens: int = 600,
) -> dict[str, Any]:
    """调 LLM 一次, 解析 tool_calls

    Returns:
        {"ok": True,  "tool_calls": [{"name", "args"}], "content": str}
        {"ok": False, "error": str, "tool_calls": [], "content": ""}
    """
    from .client import _get_api_key, _get_config

    api_key = _get_api_key()
    if not api_key:
        return {"ok": False, "error": "MINIMAX_API_KEY not set",
                "tool_calls": [], "content": ""}

    cfg = _get_config()
    api_base = cfg.get("api_base", "https://api.minimax.chat/v1")
    model = cfg.get("model", "MiniMax-M3")
    timeout = cfg.get("timeout_s", 30)
    max_retries = cfg.get("max_retries", 2)
    url = f"{api_base}/chat/completions"
    payload = _llm_payload(messages, tools or [], temperature=temperature, max_tokens=max_tokens)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    started = time.time()
    last_err: str | None = None
    with _LLM_LOCK:  # 串行化 minimax 调用
        for attempt in range(max_retries + 1):
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=timeout)
                latency_ms = int((time.time() - started) * 1000)
                if r.status_code == 200:
                    data = r.json()
                    msg = data["choices"][0]["message"]
                    content = msg.get("content") or ""
                    tool_calls = _parse_tool_calls(msg)
                    usage = data.get("usage", {})
                    primary_name = tool_calls[0].get("name") if tool_calls else None
                    primary_args = json.dumps(tool_calls[0].get("args"), ensure_ascii=False) if tool_calls else None
                    _log_call(
                        "tool_use_router", True, latency_ms,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        tool_name=primary_name, tool_args=primary_args,
                    )
                    return {
                        "ok": True,
                        "tool_calls": tool_calls,
                        "content": content,
                        "latency_ms": latency_ms,
                    }
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                log.warning("LLM tool-use 失败 (attempt %d): %s", attempt + 1, last_err)
            except Exception as e:
                latency_ms = int((time.time() - started) * 1000)
                last_err = f"{type(e).__name__}: {e}"
                log.warning("LLM tool-use 异常 (attempt %d): %s", attempt + 1, last_err)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    _log_call("tool_use_router", False, int((time.time() - started) * 1000),
              error=last_err or "unknown")
    return {
        "ok": False,
        "error": last_err or "unknown",
        "tool_calls": [],
        "content": "",
        "latency_ms": int((time.time() - started) * 1000),
    }


# ─────────── 高层 dispatch: 一步到位 ───────────

def _build_system_prompt(chat_id: str | None = None) -> str:
    """v12: 拼装 system prompt = 人格 + 工具集 + 用户记忆

    优先级 (top → bottom):
      1. persona (来自 config/persona.yaml) — 身份/语气/A 股上下文
      2. 工具集 + 调度要求 — 跟 v11 一样
      3. 用户记忆 (来自 bot_sessions + user_profile + memories) — 短时+长时偏好

    chat_id=None: 跳过记忆注入 (webhook / 单次调用场景)
    """
    from ..engine.skills import SKILL_REGISTRY
    from ..assistant.persona import load_persona

    parts: list[str] = []

    # 1) 人格
    persona = load_persona()
    if persona:
        parts.append(persona)

    # 2) 工具集 + 调度要求 (v11 老逻辑)
    tool_lines = [f"- {s.name}: {s.description}" for s in SKILL_REGISTRY.values()]
    tool_section = (
        "你是量化选股 agent 的对话助手。\n\n"
        "可用工具:\n" + "\n".join(tool_lines) + "\n\n"
        "要求:\n"
        "1. 用户问简单数据查询 (今日选股/持仓/日报/大盘/阶段/回测) → 调用对应 tool\n"
        "2. 用户问解释/为什么/怎么看 → 用 explain_pick / search_knowledge\n"
        "3. 用户闲聊 → 直接回答, 不调 tool\n"
        "4. 简洁回答, ≤200 字, 引用知识库标注 [来源]"
    )
    parts.append(tool_section)

    # 3) 用户记忆 (v12 新增)
    if chat_id:
        try:
            from ..assistant.memory import build_memory_context
            mem_ctx = build_memory_context(chat_id)
            if mem_ctx:
                parts.append(mem_ctx)
        except Exception as e:  # noqa: BLE001
            log.debug("build_memory_context 失败 (忽略): %s", e)

    return "\n\n".join(parts)


def dispatch(
    text: str,
    *,
    recent_picks: list[dict[str, Any]] | None = None,
    market_env: dict[str, Any] | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """一步式: 消息 → LLM 路由 → 调 skill → 拼回复

    Returns:
        {
          "ok": bool,
          "path": "llm_tool" | "keyword_fallback" | "llm_freeform",
          "card": {"msg_type": "text", "content": {"text": "..."}},  # 给飞书发的
          "tool_calls": [...],   # LLM 选的工具
          "raw": dict,           # skill 返回的 raw 数据
          "error": str | None,
        }
    """
    from ..engine.skills import tool_schemas, call_skill, keyword_fallback, _parse_relative_date

    if not text or not text.strip():
        return {"ok": False, "path": "empty", "card": _empty_card("（空消息）"),
                "tool_calls": [], "raw": {}, "error": "empty text"}

    # v12.8: 60s 窗口内同 chat_id 重复 → 直接走上次兜底, 跳过 LLM
    if chat_id:
        now = time.time()
        if len(_EMPTY_CACHE) > _EMPTY_CACHE_MAX:
            cutoff = now - _EMPTY_CACHE_TTL_S
            for k in [k for k, (t, _) in list(_EMPTY_CACHE.items()) if t < cutoff]:
                _EMPTY_CACHE.pop(k, None)
        cached = _EMPTY_CACHE.get(chat_id)
        if cached and (now - cached[0]) < _EMPTY_CACHE_TTL_S:
            log.info("freeform 60s 命中缓存: chat=%s (跳过 LLM)", chat_id)
            return {
                "ok": True,
                "path": "llm_freeform_empty_cached",
                "card": {"msg_type": "text", "content": {"text": cached[1]}},
                "tool_calls": [],
                "raw": {"content": ""},
                "error": None,
            }

    # v12.A.1: 入口 1 次解析 date, 3 路径共享 (LLM tool / keyword_fallback / freeform 提示)
    parsed_date = _parse_relative_date(text)

    # 拼系统 prompt + 上下文
    sys_prompt = _build_system_prompt(chat_id=chat_id)
    context_lines = []
    if recent_picks:
        for p in recent_picks[:3]:
            context_lines.append(
                f"- {p.get('date', '')} {p.get('code', '')} {p.get('name', '')} 评分 {p.get('score', 0):.1f}"
            )
    if market_env:
        context_lines.append(
            f"今日 env: {market_env.get('env_score', '?')} ({market_env.get('env_level', '?')})"
        )
    if context_lines:
        sys_prompt += "\n\n当前上下文:\n" + "\n".join(context_lines)

    # v12: 注入多轮历史 (来自 bot_sessions)
    history_messages: list[dict[str, str]] = []
    if chat_id:
        try:
            from ..engine.sessions import get_history
            history = get_history(chat_id)
            # 只取 user/assistant 角色, 跳过 tool_calls (避免污染)
            for h in history[-10:]:  # 多取一点, 拼到 system prompt 之前
                role = h.get("role", "")
                content = h.get("content", "")
                if role in ("user", "assistant") and content:
                    history_messages.append({"role": role, "content": content})
        except Exception as e:  # noqa: BLE001
            log.debug("get_history 失败 (忽略): %s", e)

    messages: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    messages.extend(history_messages)
    messages.append({"role": "user", "content": text.strip()})

    # 1) LLM 路由
    # v12.8: temperature / max_tokens 走 config (默认 0.7 / 800, 治自由回答干瘪)
    from .client import _get_config
    _llm_cfg = _get_config()
    _llm_temp = _llm_cfg.get("temperature", 0.7)
    _llm_max = _llm_cfg.get("max_tokens", 800)
    resp = chat_with_tools(messages, tools=tool_schemas(),
                           temperature=_llm_temp, max_tokens=_llm_max)
    if not resp["ok"]:
        # LLM 不可用 → 关键词降级
        log.info("LLM 不可用, 走关键词降级: %s", resp.get("error", "")[:80])
        skill_name = keyword_fallback(text)
        if skill_name is None:
            _log_call("tool_use_dispatch", False, 0,
                      error="no keyword match", chat_id=chat_id)
            return {
                "ok": False,
                "path": "llm_unavailable",
                "card": _empty_card("（LLM 暂不可用, 且无关键词命中; 请检查 MINIMAX_API_KEY 或重述问题）"),
                "tool_calls": [],
                "raw": {},
                "error": resp.get("error", "LLM unavailable"),
            }
        # v12.A.1: 复用 dispatch 入口的 parsed_date (避免重复解析)
        skill_args = {"date": parsed_date} if parsed_date else {}
        result = call_skill(skill_name, skill_args)
        _log_call("tool_use_dispatch", result.get("ok", False), 0,
                  tool_name=skill_name, tool_args=json.dumps(skill_args, ensure_ascii=False)[:80], chat_id=chat_id,
                  error=result.get("error"))
        return {
            "ok": result.get("ok", False),
            "path": "keyword_fallback",
            "card": result.get("card", _empty_card(f"（{skill_name} 失败）")),
            "tool_calls": [{"name": skill_name, "args": {}}],
            "raw": result.get("raw", {}),
            "error": result.get("error"),
        }

    # 2) LLM 成功, 检查 tool_calls
    tool_calls = resp.get("tool_calls", [])
    if not tool_calls:
        # v12.A.4.c: LLM 没选 tool → 先尝试 keyword_fallback 救场 (治 "今日选股" 兜底成空响应)
        # 之前: LLM 不调 tool → 走 freeform 兜底, 用户体感"没识别意图"
        # 现在: 先看能不能命中关键词 → 调 skill, 调不中再走 freeform
        try:
            from ..engine.skills import keyword_fallback as _kf
            _kb_skill = _kf(text)
        except Exception:
            _kb_skill = None
        if _kb_skill:
            log.info("LLM 未选 tool, keyword_fallback 救场: %s", _kb_skill)
            from ..engine.skills import call_skill as _cs
            _args = {"date": parsed_date} if parsed_date else {}
            _r = _cs(_kb_skill, _args)
            if _r.get("ok"):
                _log_call("tool_use_dispatch", True, resp.get("latency_ms", 0),
                          tool_name=_kb_skill, chat_id=chat_id)
                return {
                    "ok": True,
                    "path": "llm_tool_rescued",
                    "card": _r.get("card", _empty_card("（skill 无卡片）")),
                    "tool_calls": [{"name": _kb_skill, "args": _args, "source": "keyword_fallback"}],
                    "raw": _r.get("raw", {}),
                    "error": None,
                    "uses_llm": _r.get("uses_llm", False),
                }
        # v12.A.1: freeform 路径把 date 拼到 messages (治 LLM hallucinate "周五" → 6-12)
        if parsed_date:
            messages = list(messages)
            messages.insert(1, {
                "role": "system",
                "content": f"[时间提示] 用户文本中提到的日期 → {parsed_date} (用此回答, 不再编造日期)"
            })
        # LLM 选了不调 tool → 自由回答 (兼容老 chat_with_session 行为)
        raw_content = resp.get("content", "").strip()
        content = _strip_think_tags(raw_content)
        if not content:
            # v12.8: 空响应兜底 — 走 persona.fallback_phrases 随机选 1, 不再静默
            #        同 chat_id 60s 窗口内直接走缓存, 避免重复 LLM 浪费
            fb = _empty_response_fallback(text, chat_id)
            log.warning("freeform 空响应: chat=%s text=%r content_len=%d fallback=%r",
                        chat_id, text[:50], len(raw_content), fb[:40])
            _log_call("tool_use_dispatch", False, resp.get("latency_ms", 0),
                      chat_id=chat_id, error="empty content (fallback used)")
            return {
                "ok": True,
                "path": "llm_freeform_empty",
                "card": {"msg_type": "text", "content": {"text": fb}},
                "tool_calls": [],
                "raw": {"content": ""},
                "error": None,
            }
        _log_call("tool_use_dispatch", True, resp.get("latency_ms", 0),
                  chat_id=chat_id)
        return {
            "ok": True,
            "path": "llm_freeform",
            "card": {"msg_type": "text", "content": {"text": content}},
            "tool_calls": [],
            "raw": {"content": content},
            "error": None,
        }

    # 3) LLM 选了 tool, 执行第一个 (支持多个但只取第一个)
    primary = tool_calls[0]
    skill_name = primary.get("name", "")
    args = primary.get("args", {})
    log.info("LLM 路由: %s(%s)", skill_name, json.dumps(args, ensure_ascii=False)[:80])
    result = call_skill(skill_name, args)
    if not result.get("ok"):
        _log_call("tool_use_dispatch", False, resp.get("latency_ms", 0),
                  tool_name=skill_name, tool_args=json.dumps(args, ensure_ascii=False),
                  chat_id=chat_id, error=result.get("error"))
        return {
            "ok": False,
            "path": "llm_tool",
            "card": _empty_card(f"（{skill_name} 失败: {result.get('error', '?')}）"),
            "tool_calls": tool_calls,
            "raw": result.get("raw", {}),
            "error": result.get("error"),
        }
    _log_call("tool_use_dispatch", True, resp.get("latency_ms", 0),
              tool_name=skill_name, tool_args=json.dumps(args, ensure_ascii=False),
              chat_id=chat_id)
    return {
        "ok": True,
        "path": "llm_tool",
        "card": result.get("card", _empty_card("（skill 无卡片）")),
        "tool_calls": tool_calls,
        "raw": result.get("raw", {}),
        "error": None,
        "uses_llm": result.get("uses_llm", False),
    }


# v12: 剥掉 <think>...</think> 块 (minimax M3 等推理模型会泄漏 chain-of-thought)
# 例: "<think>用户说 hello, 闲聊</think>\n你好!" → "你好!"
# 边界 case 全处理: 多个块 / 裸闭合标签 / 未闭合 / 嵌套
import re
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_BARE_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
_BARE_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)


def _strip_think_tags(text: str) -> str:
    """剥掉 <think>...</think> 块 + 裸闭合标签, 多余空白也清掉

    边界 case 全覆盖:
      - 无 think 标签 → 原样返回
      - 正常闭合 → 整块剥掉
      - 多个块 → 全部剥掉
      - 只有 <think> 没 </think> → 整段砍掉
      - 裸 </think> (没匹配的 <think) → 也剥掉
      - 混合 "some text</think>actual" → "actual"
    """
    if not text:
        return text
    # 1) 正常闭合块
    if "<think>" in text:
        cleaned = _THINK_RE.sub("", text)
        # 兜底: 残留 <think> 没闭合 → 整段砍掉
        if "<think>" in cleaned:
            cleaned = cleaned.split("<think>", 1)[0]
    else:
        cleaned = text
    # 2) 裸闭合标签 (常见于多个 think 块截断残留)
    if "</think>" in cleaned:
        cleaned = _BARE_CLOSE_RE.sub("", cleaned)
    return cleaned.strip()


def _empty_card(text: str) -> dict[str, Any]:
    return {"msg_type": "text", "content": {"text": text}}
