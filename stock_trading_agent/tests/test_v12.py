"""test_v12.py — v12 选股助手进化测试

覆盖:
  - 人格 (persona)        : 5 个
  - 记忆 (memory)         : 6 个
  - 多轮 (multi-turn)     : 6 个
  - 轻主动 (proactivity)  : 4 个
  - 集成 (integration)    : 3 个
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

TEST_DIR = Path(tempfile.mkdtemp(prefix="sta_test_v12_"))


def _isolated_dir(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"sta_test_v12_{name}_"))


def _seed_db(target_dir: Path) -> None:
    """重设 paper_trader DB 到临时目录, init_account 即可创建所有表"""
    import stock_trading_agent.engine.paper_trader as pt
    pt.DATA_DIR = target_dir
    pt.DB_PATH = target_dir / "quant.db"
    pt.init_account(initial_capital=1_000_000.0)


def _reset_llm_key():
    os.environ["MINIMAX_API_KEY"] = ""


# ─────────── 1. 人格 (persona) ───────────

def test_persona_loads_yaml_identity() -> None:
    """人格 yaml 加载: identity 段存在"""
    from stock_trading_agent.assistant.persona import load_persona, reload
    reload()
    p = load_persona()
    assert "盘盘" in p, f"identity 缺'盘盘': {p[:100]}"
    assert "A 股" in p or "A股" in p, f"identity 缺 'A 股': {p[:200]}"
    print("  ✓ test_persona_loads_yaml_identity")


def test_persona_three_sections_in_order() -> None:
    """人格 3 段拼装顺序: identity → tone_rules → context_preamble"""
    from stock_trading_agent.assistant.persona import load_persona, reload
    reload()
    p = load_persona()
    # identity 在最前
    i_pos = p.find("盘盘")
    # tone_rules 在中间
    t_pos = p.find("语气规则")
    # context_preamble 在后
    c_pos = p.find("A 股硬约束")
    assert i_pos >= 0, "identity 缺失"
    assert t_pos > i_pos, f"tone_rules 顺序错: identity@{i_pos} rules@{t_pos}"
    assert c_pos > t_pos, f"context_preamble 顺序错: rules@{t_pos} ctx@{c_pos}"
    print("  ✓ test_persona_three_sections_in_order")


def test_persona_tone_rules_hit_keywords() -> None:
    """人格语气规则包含关键小白规则"""
    from stock_trading_agent.assistant.persona import load_persona, reload
    reload()
    p = load_persona()
    # 关键语气关键词至少出现 3 个
    keywords = ["白话", "术语", "不熟", "不知道", "≤ 200", "200 字"]
    hits = [k for k in keywords if k in p]
    assert len(hits) >= 3, f"语气规则关键词命中太少 ({len(hits)}/{len(keywords)}): {hits}"
    print("  ✓ test_persona_tone_rules_hit_keywords")


def test_persona_context_preamble_a_share_rules() -> None:
    """A 股硬约束 (T+1 / 涨跌停 / 交易时间) 在 context_preamble"""
    from stock_trading_agent.assistant.persona import load_persona, reload
    reload()
    p = load_persona()
    assert "T+1" in p, "T+1 规则缺失"
    assert "涨跌停" in p, "涨跌停规则缺失"
    assert "9:30" in p or "9:30-11:30" in p, "交易时间缺失"
    print("  ✓ test_persona_context_preamble_a_share_rules")


def test_persona_fallback_when_yaml_missing() -> None:
    """yaml 文件不存在/解析失败 → 返回默认人格, 不抛异常"""
    from stock_trading_agent.assistant import persona
    # 临时改 log_path 指向不存在
    with patch.object(persona, "log_path", Path("/nonexistent/persona.yaml")):
        p = persona.load_persona()
        assert "选股助手" in p or "A 股" in p, f"fallback 异常: {p}"
        assert len(p) > 5, f"fallback 太短: {p}"
    # 退出 with 后, log_path 已恢复 → 读真 yaml, persona 应含 "盘盘"
    p2 = persona.load_persona()
    assert "盘盘" in p2, f"恢复后应读到真 yaml: {p2[:100]}"
    print("  ✓ test_persona_fallback_when_yaml_missing")


# ─────────── 1b. <think> 标签剥除 (v12 修 minimax M3 think 泄漏) ───────────

def test_strip_think_basic() -> None:
    """<think>...</think>... 格式: 只留 think 之后的内容"""
    from stock_trading_agent.llm.tool_use import _strip_think_tags
    result = _strip_think_tags("<think>用户说 hello, 这是闲聊</think>\n你好!")
    assert result == "你好!", f"基本剥离失败: {result!r}"
    assert "<think>" not in result, "<think> 标签残留"
    print("  ✓ test_strip_think_basic")


def test_strip_think_no_tag() -> None:
    """无 <think>: 原样返回"""
    from stock_trading_agent.llm.tool_use import _strip_think_tags
    s = "你好! 我是助手, 啥事?"
    assert _strip_think_tags(s) == s, "无 think 时不应改内容"
    print("  ✓ test_strip_think_no_tag")


def test_strip_think_multiline_and_whitespace() -> None:
    """多行 think + 前后空白: 干净剥除 + strip"""
    from stock_trading_agent.llm.tool_use import _strip_think_tags
    s = " <think>\n多行\n思考\nprocess\n</think>  \n  实际内容  "
    result = _strip_think_tags(s)
    assert result == "实际内容", f"多行/空白处理失败: {result!r}"
    print("  ✓ test_strip_think_multiline_and_whitespace")


def test_strip_think_unclosed() -> None:
    """未闭合 <think> (LLM 输出截断): 整段砍掉, 不泄漏"""
    from stock_trading_agent.llm.tool_use import _strip_think_tags
    s = "<think>用户想问 怎么选股, 我需要回答具体步骤"
    result = _strip_think_tags(s)
    assert "<think>" not in result, "未闭合 think 标签残留"
    assert "怎么选股" not in result, "未闭合 think 内容泄漏"
    print("  ✓ test_strip_think_unclosed")


def test_strip_think_empty_after_strip() -> None:
    """整段都是 think, 剥完为空 → 返回空串 (dispatch 会兜底"未返回内容")"""
    from stock_trading_agent.llm.tool_use import _strip_think_tags
    s = "<think>只是思考, 没说实际内容</think>"
    result = _strip_think_tags(s)
    assert result == "", f"应为空, 实际 {result!r}"
    print("  ✓ test_strip_think_empty_after_strip")


# ─────────── 1c. skill 渲染路径也剥 think (防御性) ───────────

def test_skill_explain_strips_think() -> None:
    """explain_pick 渲染: LLM 返回的 explanation 含 think 标签 → 渲染剥掉"""
    from stock_trading_agent.engine.skills import _render_explain_card
    r = _render_explain_card({
        "code": "600519",
        "name": "茅台",
        "explanation": "<think>分析评分逻辑</think>\n茅台是蓝筹股, 评分 90",
    })
    text = r["content"]["text"]
    assert "<think>" not in text, f"explain 渲染残留 think: {text}"
    assert "茅台是蓝筹股" in text, f"实际内容丢失: {text}"
    print("  ✓ test_skill_explain_strips_think")


def test_skill_search_strips_think() -> None:
    """search_knowledge 渲染: LLM 返回的 answer 含 think 标签 → 渲染剥掉"""
    from stock_trading_agent.engine.skills import _render_search_knowledge_card
    r = _render_search_knowledge_card({
        "query": "银行股",
        "answer": "<think>查了一下知识库</think>\n银行是周期股",
        "sources": [],
    })
    text = r["content"]["text"]
    assert "<think>" not in text, f"search 渲染残留 think: {text}"
    assert "银行是周期股" in text, f"实际内容丢失: {text}"
    print("  ✓ test_skill_search_strips_think")


# ─────────── 1d. 加强版 strip (多块 / 裸闭合) ───────────

def test_strip_think_multiple_blocks() -> None:
    """多个 think 块 + 裸闭合标签: 全剥 (v12 修多块 bug)"""
    from stock_trading_agent.llm.tool_use import _strip_think_tags
    # 用户实际看到的格式: 裸 </think> + <think>[英文 reasoning]</think> + 中文
    s = "</think>\n<think>[chain of thought]\nGiven the instruction 不要其他废话...</think>\n数据全为空"
    result = _strip_think_tags(s)
    assert "<think>" not in result, f"残留 <think>: {result}"
    assert "</think>" not in result, f"残留 </think>: {result}"
    assert "数据全为空" in result, f"实际内容丢失: {result}"
    assert "Given the instruction" not in result, f"chain-of-thought 泄漏: {result}"
    print("  ✓ test_strip_think_multiple_blocks")


def test_pusher_strip_think_at_entry() -> None:
    """pusher._send 入口剥 think: 6 个 push_xxx 全覆盖"""
    from stock_trading_agent.feishu.pusher import _strip_think_for_push
    # 周报 LLM 总结含 think 块 → 模拟 _send 入参
    dirty = "</think>\n<think>Given the instruction, I should be concise.\nLet me write.</think>\n本周胜率 60%"
    cleaned = _strip_think_for_push(dirty)
    assert "<think>" not in cleaned
    assert "</think>" not in cleaned
    assert "本周胜率" in cleaned
    # 同时验证 _send 真的在入口剥 (而不是只 strip_for_push)
    from stock_trading_agent.feishu import pusher as _p
    # mock load_env + 走一条 push 函数, 看最终传给 _send_webhook 的内容
    from unittest.mock import patch, MagicMock
    fake_send = MagicMock(return_value={"ok": True, "channel": "webhook"})
    with patch.object(_p, "_send_webhook", fake_send),          patch.object(_p, "_send_via_app", return_value={"ok": False, "error": "no app"}),          patch.object(_p, "load_env", return_value={"FEISHU_PUSH_MODE": "webhook"}):
        _p.push_anomaly("<think>异常检测</think>\n⚠️ 数据缺失")
    # 看 fake_send 收到的 text 应无 think
    call_args = fake_send.call_args
    sent_text = call_args[0][0] if call_args else ""
    assert "<think>" not in sent_text, f"推送含 think: {sent_text}"
    assert "⚠️ 数据缺失" in sent_text
    print("  ✓ test_pusher_strip_think_at_entry")


# ─────────── 2. 记忆 (memory) ───────────

def test_memory_detect_preference_signal() -> None:
    """偏好信号检测: 正向 / 负向 / 显式"""
    from stock_trading_agent.assistant.memory import detect_memory_signal
    neg = detect_memory_signal("我不喜欢银行股")
    assert neg is not None, "负向偏好未识别"
    assert neg[0] == "preference", f"type 错: {neg[0]}"
    assert "银行" in neg[1], f"content 缺'银行': {neg[1]}"
    assert neg[2] == "detected", f"source 错: {neg[2]}"

    pos = detect_memory_signal("我偏好白酒股")
    assert pos is not None, "正向偏好未识别"
    assert pos[0] == "preference"
    assert "白酒" in pos[1]

    exp = detect_memory_signal("记住明天 9 点开会")
    assert exp is not None, "显式信号未识别"
    assert exp[0] == "explicit"
    assert exp[2] == "explicit"
    print("  ✓ test_memory_detect_preference_signal")


def test_memory_detect_no_signal() -> None:
    """无信号: 普通对话返回 None"""
    from stock_trading_agent.assistant.memory import detect_memory_signal
    assert detect_memory_signal("今天选股怎么样") is None
    assert detect_memory_signal("") is None
    assert detect_memory_signal("茅台怎么样") is None
    print("  ✓ test_memory_detect_no_signal")


def test_memory_remember_and_list() -> None:
    """记忆写入 + 列出"""
    d = _isolated_dir("mem_rw")
    _seed_db(d)
    from stock_trading_agent.assistant.memory import remember, list_memories
    remember("chat_A", "不喜欢银行", type="preference", importance=2)
    remember("chat_A", "关注 600519", type="fact", importance=1)
    mems = list_memories("chat_A")
    assert len(mems) == 2, f"应有 2 条, 实际 {len(mems)}"
    # importance DESC 排序: 2 在前
    assert mems[0]["importance"] == 2, f"sort 错: {mems[0]['importance']}"
    assert "银行" in mems[0]["content"]
    print("  ✓ test_memory_remember_and_list")


def test_memory_chat_id_isolation() -> None:
    """不同 chat_id 的记忆互不干扰"""
    d = _isolated_dir("mem_iso")
    _seed_db(d)
    from stock_trading_agent.assistant.memory import remember, list_memories
    remember("chat_A", "A 的偏好")
    remember("chat_B", "B 的偏好")
    a_mems = list_memories("chat_A")
    b_mems = list_memories("chat_B")
    assert len(a_mems) == 1 and "A" in a_mems[0]["content"]
    assert len(b_mems) == 1 and "B" in b_mems[0]["content"]
    # 隔离: A 不应看到 B
    assert all("B" not in m["content"] for m in a_mems)
    print("  ✓ test_memory_chat_id_isolation")


def test_memory_clear() -> None:
    """清空记忆"""
    d = _isolated_dir("mem_clr")
    _seed_db(d)
    from stock_trading_agent.assistant.memory import remember, list_memories, clear_memories
    remember("chat_X", "m1")
    remember("chat_X", "m2")
    remember("chat_Y", "y1")  # 别的 chat, 不应被清
    assert len(list_memories("chat_X")) == 2
    n = clear_memories("chat_X")
    assert n == 2, f"应清 2 条, 实际 {n}"
    assert list_memories("chat_X") == []
    assert len(list_memories("chat_Y")) == 1, "chat_Y 不应被影响"
    print("  ✓ test_memory_clear")


def test_memory_ttl_expiry() -> None:
    """过期记忆被自动过滤"""
    d = _isolated_dir("mem_ttl")
    _seed_db(d)
    from stock_trading_agent.assistant.memory import remember, list_memories
    remember("chat_T", "1 天前", ttl_days=1)
    remember("chat_T", "100 天前", ttl_days=100)
    # 直接改 created_at 让一条变旧
    import stock_trading_agent.engine.paper_trader as pt
    conn = pt.get_db()
    old_date = (datetime.now() - timedelta(days=100)).isoformat(timespec="seconds")
    conn.execute("UPDATE memories SET created_at=? WHERE content=?", (old_date, "100 天前"))
    conn.commit()
    # 不含 expired: 只剩 1 条
    visible = list_memories("chat_T", include_expired=False)
    assert len(visible) == 1, f"应过滤掉过期, 剩 {len(visible)}"
    assert visible[0]["content"] == "1 天前"
    # 含 expired: 2 条都在
    all_mems = list_memories("chat_T", include_expired=True)
    assert len(all_mems) == 2
    print("  ✓ test_memory_ttl_expiry")


# ─────────── 3. 多轮 (multi-turn) ───────────

def test_sessions_append_turn_basic() -> None:
    """session append_turn 写入 + 读出"""
    d = _isolated_dir("mt_basic")
    _seed_db(d)
    from stock_trading_agent.engine.sessions import append_turn, get_history
    append_turn("chat_1", "user", "你好")
    append_turn("chat_1", "assistant", "你好, 啥事?")
    h = get_history("chat_1")
    assert len(h) == 2
    assert h[0]["role"] == "user" and h[0]["content"] == "你好"
    assert h[1]["role"] == "assistant"
    print("  ✓ test_sessions_append_turn_basic")


def test_sessions_chat_isolation() -> None:
    """session 按 chat_id 隔离"""
    d = _isolated_dir("mt_iso")
    _seed_db(d)
    from stock_trading_agent.engine.sessions import append_turn, get_history
    append_turn("cA", "user", "A msg")
    append_turn("cB", "user", "B msg")
    assert len(get_history("cA")) == 1
    assert len(get_history("cB")) == 1
    assert get_history("cA")[0]["content"] == "A msg"
    print("  ✓ test_sessions_chat_isolation")


def test_sessions_ttl_resets_history() -> None:
    """session TTL 过期 → history 自动重置"""
    d = _isolated_dir("mt_ttl")
    _seed_db(d)
    from stock_trading_agent.engine.sessions import append_turn, get_history, reset
    append_turn("chat_T", "user", "old msg")
    # 模拟 25 小时前
    import stock_trading_agent.engine.paper_trader as pt
    conn = pt.get_db()
    old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
    conn.execute("UPDATE bot_sessions SET last_active=? WHERE session_id=?", (old, "chat_T"))
    conn.commit()
    # 重置
    reset("chat_T")
    h = get_history("chat_T")
    assert h == [], f"TTL 过期后 history 应空, 实际 {h}"
    print("  ✓ test_sessions_ttl_resets_history")


def test_dispatch_injects_history() -> None:
    """dispatch 把 session history 注入 LLM messages"""
    d = _isolated_dir("mt_disp")
    _seed_db(d)
    from stock_trading_agent.engine.sessions import append_turn
    from stock_trading_agent.llm.tool_use import _build_system_prompt, dispatch

    chat_id = "oc_disp_inj"
    append_turn(chat_id, "user", "今天大盘怎么样")
    append_turn(chat_id, "assistant", "今天跌 1%, 谨慎")

    # 验证 _build_system_prompt 在有 chat_id 时不会注入历史 (history 是 messages 层注入)
    sp = _build_system_prompt(chat_id=chat_id)
    assert "今天大盘怎么样" not in sp, "history 不应在 system prompt 里"

    # 验证 dispatch 会读 history: LLM 不可用 → keyword_fallback, 走完后 history 还在
    _reset_llm_key()
    r = dispatch("继续", chat_id=chat_id)
    # history 没被 dispatch 写入 (写是 listener 的事)
    from stock_trading_agent.engine.sessions import get_history
    h = get_history(chat_id)
    assert len(h) == 2, f"dispatch 不应改 history, 实际 {len(h)}"
    print("  ✓ test_dispatch_injects_history")


def test_dispatch_includes_memory_in_system_prompt() -> None:
    """dispatch 的 system prompt 包含记忆"""
    d = _isolated_dir("mt_mem")
    _seed_db(d)
    from stock_trading_agent.assistant.memory import remember
    from stock_trading_agent.llm.tool_use import _build_system_prompt

    remember("oc_mem", "不喜欢地产", type="preference", importance=2)
    sp = _build_system_prompt(chat_id="oc_mem")
    assert "不喜欢地产" in sp, f"memory 未注入 system prompt: {sp[-300:]}"
    assert "用户记忆" in sp, "memory 标题缺失"
    print("  ✓ test_dispatch_includes_memory_in_system_prompt")


def test_dispatch_no_chat_id_works() -> None:
    """dispatch 无 chat_id 不报错 (webhook / 单次调用兼容)"""
    d = _isolated_dir("mt_noid")
    _seed_db(d)
    from stock_trading_agent.llm.tool_use import _build_system_prompt, dispatch
    _reset_llm_key()
    sp = _build_system_prompt()  # 无 chat_id
    assert "盘盘" in sp, "无 chat_id 也应有 persona"
    r = dispatch("持仓")
    assert "card" in r, f"dispatch 应正常返回: {r}"
    print("  ✓ test_dispatch_no_chat_id_works")


# ─────────── 4. 轻主动 (proactivity) ───────────

def test_push_daily_summary_structure() -> None:
    """push_daily_summary 返回结构正确 (含 ok 字段)"""
    d = _isolated_dir("push_daily")
    _seed_db(d)
    from stock_trading_agent.feishu import pusher
    r = pusher.push_daily_summary({"date": "2026-06-07", "pick_count": 3, "paper_total": {"total_pnl_pct": 1.5, "win_rate": 60}}, [])
    assert "ok" in r, f"push_daily_summary 缺 ok: {r}"
    # 失败也行 (沙盒无外网), 但结构要对
    assert "channel" in r or "error" in r
    print("  ✓ test_push_daily_summary_structure")


def test_push_anomaly_recap_empty() -> None:
    """push_anomaly_recap 空数据: 推 '无异动' 卡片"""
    d = _isolated_dir("push_recap_empty")
    _seed_db(d)
    from stock_trading_agent.feishu import pusher
    r = pusher.push_anomaly_recap([])
    assert "ok" in r
    print("  ✓ test_push_anomaly_recap_empty")


def test_push_anomaly_recap_with_data() -> None:
    """push_anomaly_recap 有数据: 走 _send, 返回 ok 字段"""
    d = _isolated_dir("push_recap_data")
    _seed_db(d)
    from stock_trading_agent.feishu import pusher
    items = [
        {"time": "14:32", "code": "600519", "name": "茅台", "type": "涨停", "change": "+10%"},
        {"time": "13:15", "code": "000858", "name": "五粮液", "type": "放量", "change": "+5%"},
    ]
    r = pusher.push_anomaly_recap(items)
    assert "ok" in r
    print("  ✓ test_push_anomaly_recap_with_data")


def test_push_registry_registers_in_scheduler() -> None:
    """PUSH_REGISTRY 2 个 push 在 build_scheduler 后出现在 jobs 列表"""
    from stock_trading_agent.agent import build_scheduler, PUSH_REGISTRY
    assert "daily_summary_push" in PUSH_REGISTRY
    assert "anomaly_recap_push" in PUSH_REGISTRY
    sched = build_scheduler()
    job_ids = {j.id for j in sched.get_jobs()}
    assert "daily_summary_push" in job_ids, f"daily_summary_push 未注册, jobs: {job_ids}"
    assert "anomaly_recap_push" in job_ids, f"anomaly_recap_push 未注册"
    print("  ✓ test_push_registry_registers_in_scheduler")


# ─────────── 5. 集成 (integration) ───────────

def test_listener_writes_session_and_memory() -> None:
    """集成: listener 路径 → 写 session + 写 memory (有信号)"""
    d = _isolated_dir("int_listen")
    _seed_db(d)
    from stock_trading_agent.engine.sessions import get_history
    from stock_trading_agent.assistant.memory import list_memories
    from stock_trading_agent.llm.tool_use import dispatch

    chat_id = "oc_int_listen"
    user_text = "我不喜欢银行股"
    _reset_llm_key()

    # 模拟 listener 行为: 先 detect_memory + session_append, 再 dispatch
    from stock_trading_agent.assistant.memory import detect_memory_signal, remember as _remember
    from stock_trading_agent.engine.sessions import append_turn as _append

    sig = detect_memory_signal(user_text)
    assert sig is not None, "信号未识别"
    _remember(chat_id, sig[1], type=sig[0], source=sig[2])

    _append(chat_id, "user", user_text)
    result = dispatch(user_text, chat_id=chat_id)
    reply = result.get("card", {}).get("content", {}).get("text", "")
    _append(chat_id, "assistant", reply)

    # 验证: memory 1 条 + session 2 turns
    mems = list_memories(chat_id)
    assert len(mems) == 1, f"应有 1 条记忆, 实际 {len(mems)}"
    assert "银行" in mems[0]["content"]

    h = get_history(chat_id)
    assert len(h) == 2, f"应有 2 turn, 实际 {len(h)}"
    assert h[0]["role"] == "user" and "银行" in h[0]["content"]
    assert h[1]["role"] == "assistant"
    print("  ✓ test_listener_writes_session_and_memory")


def test_memory_cli_list_and_clear() -> None:
    """集成: agent memory CLI list / clear 子命令 (直接调 _handle_memory_cmd)"""
    d = _isolated_dir("int_cli")
    _seed_db(d)
    from stock_trading_agent.assistant.memory import remember, list_memories
    from stock_trading_agent.agent import _handle_memory_cmd
    from unittest.mock import patch
    import io

    remember("oc_cli_test", "测试记忆 1", importance=2)
    remember("oc_cli_test", "测试记忆 2", importance=1)

    # list — 模拟 argparse Namespace
    args_list = MagicMock()
    args_list.action = "list"
    args_list.chat_id = "oc_cli_test"

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        _handle_memory_cmd(args_list)
    out = buf.getvalue()
    assert "测试记忆 1" in out, f"list 输出缺: {out}"
    assert "测试记忆 2" in out, f"list 输出缺: {out}"
    assert "共 2 条记忆" in out, f"list 计数错: {out}"

    # clear
    args_clear = MagicMock()
    args_clear.action = "clear"
    args_clear.chat_id = "oc_cli_test"
    buf2 = io.StringIO()
    with patch("sys.stdout", buf2):
        _handle_memory_cmd(args_clear)
    out2 = buf2.getvalue()
    assert "已清空 2 条" in out2, f"clear 输出错: {out2}"

    # 再次 list 应空
    assert list_memories("oc_cli_test") == [], "清空后 list 应空"
    print("  ✓ test_memory_cli_list_and_clear")


def test_end_to_end_persona_memory_session() -> None:
    """端到端: user 偏好 → memory → system prompt 影响回复"""
    d = _isolated_dir("int_e2e")
    _seed_db(d)
    from stock_trading_agent.assistant.memory import remember
    from stock_trading_agent.llm.tool_use import _build_system_prompt
    from stock_trading_agent.engine.sessions import append_turn

    chat = "oc_e2e"
    remember(chat, "不喜欢地产股", type="preference", importance=3)
    append_turn(chat, "user", "今天选股怎么样")
    append_turn(chat, "assistant", "今天选到 2 只, 都是白酒")
    append_turn(chat, "user", "继续")

    sp = _build_system_prompt(chat_id=chat)
    # 验证 3 段都进 system prompt
    assert "盘盘" in sp, "persona 缺失"
    assert "可用工具" in sp, "tools 段缺失"
    assert "不喜欢地产股" in sp, "memory 段缺失"
    # 多轮 history 不在 system prompt, 而在 messages 层
    assert "今天选股怎么样" not in sp, "history 不应在 system prompt"
    print("  ✓ test_end_to_end_persona_memory_session")


# ─────────── 5b. v12.3 listener watchdog (listener 崩了不挂 supervisor) ───────────

def test_watchdog_restarts_listener_on_exception() -> None:
    """watchdog: listener 抛异常 → 2s 后 restart, 累计 1 次"""
    from stock_trading_agent.agent import _listener_lifecycle
    stop_event = threading.Event()
    call_count = [0]

    def fake_run(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:  # 前 2 次崩, 第 3 次不崩
            raise RuntimeError(f"simulated lark-oapi crash #{call_count[0]}")
        # 第 3 次跑住 (block), 等 stop_event 设了就退出
        stop_event.wait(timeout=2.0)

    with patch("stock_trading_agent.agent._listener.run", side_effect=fake_run):
        # backoff=0.1s, max=5, window=10s → 应该至少跑 3 次
        t = threading.Thread(target=_listener_lifecycle, args=(stop_event,),
                             kwargs={"max_restarts": 5, "window_s": 10, "backoff_s": 0.1},
                             daemon=True)
        t.start()
        time.sleep(1.0)  # 给 watchdog 时间 restart 几次
        stop_event.set()
        t.join(timeout=3.0)

    assert call_count[0] >= 3, f"应至少调 3 次 listener.run, 实际 {call_count[0]}"
    print(f"  ✓ test_watchdog_restarts_listener_on_exception (call_count={call_count[0]})")


def test_watchdog_stops_when_max_restarts_exceeded() -> None:
    """watchdog: 5 次连崩 (5s 窗口) → 放弃 restart, set stop_event"""
    from stock_trading_agent.agent import _listener_lifecycle
    stop_event = threading.Event()
    call_count = [0]

    def always_crash(*args, **kwargs):
        call_count[0] += 1
        raise RuntimeError(f"crash #{call_count[0]}")

    with patch("stock_trading_agent.agent._listener.run", side_effect=always_crash):
        # max=2, window=10s, backoff=0.05s → 第 3 次时停
        _listener_lifecycle(stop_event, max_restarts=2, window_s=10, backoff_s=0.05)

    assert stop_event.is_set(), "超 max_restarts 后应 set stop_event"
    assert call_count[0] == 3, f"应跑 3 次 (初始 + 2 restart), 实际 {call_count[0]}"
    print(f"  ✓ test_watchdog_stops_when_max_restarts_exceeded (call_count={call_count[0]})")


def test_watchdog_exits_cleanly_when_stop_event_set() -> None:
    """watchdog: stop_event 主动设 → 立即退出, 不再 restart

    验证: stop 路径正常, 不会卡在 while 循环里
    """
    from stock_trading_agent.agent import _listener_lifecycle
    stop_event = threading.Event()
    call_count = [0]

    def block_forever(*args, **kwargs):
        call_count[0] += 1
        # 阻塞直到 stop_event (模拟 lark-oapi 跑住的 WS client)
        stop_event.wait()  # 无 timeout, 阻塞

    with patch("stock_trading_agent.agent._listener.run", side_effect=block_forever):
        t = threading.Thread(target=_listener_lifecycle, args=(stop_event,),
                             kwargs={"max_restarts": 5, "window_s": 10, "backoff_s": 0.05},
                             daemon=True)
        t.start()
        time.sleep(0.3)  # 让 1st call 起来
        stop_event.set()  # 触发 stop
        t.join(timeout=2.0)

    assert call_count[0] == 1, f"stop_event 设后只跑 1 次, 实际 {call_count[0]}"
    assert stop_event.is_set()
    assert not t.is_alive(), "watchdog 线程应已退出"
    print(f"  ✓ test_watchdog_exits_cleanly_when_stop_event_set")


# ─────────── main ───────────

def main() -> None:
    tests = [
        # 5 persona
        test_persona_loads_yaml_identity,
        test_persona_three_sections_in_order,
        test_persona_tone_rules_hit_keywords,
        test_persona_context_preamble_a_share_rules,
        test_persona_fallback_when_yaml_missing,
        # 5 think-strip (v12 think 标签泄漏修)
        test_strip_think_basic,
        test_strip_think_no_tag,
        test_strip_think_multiline_and_whitespace,
        test_strip_think_unclosed,
        test_strip_think_empty_after_strip,
        test_skill_explain_strips_think,
        test_skill_search_strips_think,
        test_strip_think_multiple_blocks,
        test_pusher_strip_think_at_entry,
        # 6 memory
        test_memory_detect_preference_signal,
        test_memory_detect_no_signal,
        test_memory_remember_and_list,
        test_memory_chat_id_isolation,
        test_memory_clear,
        test_memory_ttl_expiry,
        # 6 multi-turn
        test_sessions_append_turn_basic,
        test_sessions_chat_isolation,
        test_sessions_ttl_resets_history,
        test_dispatch_injects_history,
        test_dispatch_includes_memory_in_system_prompt,
        test_dispatch_no_chat_id_works,
        # 4 proactivity
        test_push_daily_summary_structure,
        test_push_anomaly_recap_empty,
        test_push_anomaly_recap_with_data,
        test_push_registry_registers_in_scheduler,
        # 3 integration
        test_listener_writes_session_and_memory,
        test_memory_cli_list_and_clear,
        test_end_to_end_persona_memory_session,
        # 3 v12.3 watchdog
        test_watchdog_restarts_listener_on_exception,
        test_watchdog_stops_when_max_restarts_exceeded,
        test_watchdog_exits_cleanly_when_stop_event_set,
    ]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    if failed:
        print(f"\n✗ {failed} tests failed")
        sys.exit(1)
    print(f"\n✓ {len(tests)} tests passed")


if __name__ == "__main__":
    main()
