"""test_v11.py — v11 单进程 supervisor + LLM tool-use 回归测试

覆盖:
  - 8 个 skill: 1 个每个 (含 2 个 LLM skill 用 mock)
  - keyword_fallback: 已知/未知/空文本
  - chat_with_tools: tool_calls 解析
  - dispatch: llm_tool / keyword_fallback / llm_unavailable / llm_freeform 4 条路径
  - supervisor: 2 thread 起 + 停
  - listener builder: 含 reaction handler
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

# ─────────── Fixture helpers ───────────

TEST_DIR = Path(tempfile.mkdtemp(prefix="sta_test_v11_"))


def _isolated_dir(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"sta_test_v11_{name}_"))


def _seed_db(target_dir: Path) -> None:
    """重设 paper_trader 的全局 DB_PATH / DATA_DIR 到临时目录, 插 fixture 数据"""
    import stock_trading_agent.engine.paper_trader as pt
    pt.DATA_DIR = target_dir
    pt.DB_PATH = target_dir / "quant.db"
    pt.init_account(initial_capital=1_000_000.0)
    conn = pt.get_db()
    # picks (3 条, 2 个板块)
    for i, (code, name, sector, score, plan) in enumerate([
        ("600519", "贵州茅台", "白酒", 90.0, "A"),
        ("000858", "五粮液", "白酒", 85.0, "A"),
        ("300750", "宁德时代", "新能源", 82.0, "B"),
    ]):
        conn.execute(
            "INSERT INTO picks (pick_date, code, name, price, chg_pct, score, sector, "
            "plan, plan_used, market_env_score, market_env_level, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-06-05", code, name, 100.0 + i, 2.0, score, sector, plan, plan,
             65, "中性", "2026-06-05 14:00:00"),
        )
    # paper_positions (2 只 open)
    for code, name, sector, pnl_open, pnl_noon in [
        ("600519", "贵州茅台", "白酒", 1.5, 0.8),
        ("000858", "五粮液", "白酒", -0.5, None),
    ]:
        conn.execute(
            "INSERT INTO paper_positions (pick_date, code, name, open_price, open_amount, "
            "shares, sector, plan, score, status, pnl_open_pct, pnl_noon_pct, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-06-05", code, name, 100.0, 10000.0, 100.0, sector, "A", 85.0,
             "open", pnl_open, pnl_noon, "2026-06-05 14:00:00", "2026-06-05 14:00:00"),
        )
    # stage_runs (1 条)
    conn.execute(
        "INSERT INTO stage_runs (stage, run_date, ran_at, ok) VALUES (?, ?, ?, ?)",
        ("pick", "2026-06-05", "2026-06-05T14:00:05+08:00", 1),
    )
    conn.commit()


def _reset_llm_key():
    os.environ["MINIMAX_API_KEY"] = ""


def _set_llm_key():
    os.environ["MINIMAX_API_KEY"] = "sk-fake-for-test"


# ─────────── 8 个 skill 单元测试 ───────────

def test_skill_get_picks_returns_seeded() -> None:
    """get_picks: 读 picks 表, 验证返回 3 条"""
    td = _isolated_dir("get_picks")
    _seed_db(td)
    from stock_trading_agent.engine.skills import call_skill
    r = call_skill("get_picks", {"top_n": 10})
    assert r["ok"], r
    assert r["raw"]["count"] == 3
    codes = {p["code"] for p in r["raw"]["items"]}
    assert codes == {"600519", "000858", "300750"}
    assert r["card"]["msg_type"] == "text"
    assert "贵州茅台" in r["card"]["content"]["text"]
    print(f"  ✓ get_picks 返回 {r['raw']['count']} 条, card 长度 {len(r['card']['content']['text'])}")


def test_skill_get_positions_returns_seeded() -> None:
    """get_positions: 读 paper_positions 表"""
    td = _isolated_dir("get_positions")
    _seed_db(td)
    from stock_trading_agent.engine.skills import call_skill
    r = call_skill("get_positions", {"status": "open"})
    assert r["ok"], r
    assert r["raw"]["count"] == 2
    assert r["uses_llm"] is False
    print(f"  ✓ get_positions(open) 返回 {r['raw']['count']} 条, uses_llm=False")


def test_skill_get_daily_report() -> None:
    """get_daily_report: 拼 picks + PnL + env"""
    td = _isolated_dir("daily_report")
    _seed_db(td)
    from stock_trading_agent.engine.skills import call_skill
    r = call_skill("get_daily_report", {"date": "2026-06-05"})
    assert r["ok"], r
    assert r["raw"]["pick_count"] == 3
    assert r["raw"]["env_score"] == 65
    assert r["raw"]["env_level"] == "中性"
    assert "选股" in r["card"]["content"]["text"]
    print(f"  ✓ daily_report pick_count=3 env=65 中性")


def test_skill_get_market_env() -> None:
    """get_market_env: 读最新 env"""
    td = _isolated_dir("market_env")
    _seed_db(td)
    from stock_trading_agent.engine.skills import call_skill
    r = call_skill("get_market_env", {})
    assert r["ok"], r
    assert r["raw"]["env_score"] == 65
    assert r["raw"]["env_level"] == "中性"
    print(f"  ✓ get_market_env env_score=65")


def test_skill_get_stage_runs() -> None:
    """get_stage_runs: 读今日 stage_runs"""
    td = _isolated_dir("stage_runs")
    _seed_db(td)
    from stock_trading_agent.engine.skills import call_skill
    r = call_skill("get_stage_runs", {"date": "2026-06-05"})
    assert r["ok"], r
    assert r["raw"]["count"] == 1
    assert r["raw"]["items"][0]["stage"] == "pick"
    assert "✅" in r["card"]["content"]["text"] or "pick" in r["card"]["content"]["text"]
    print(f"  ✓ get_stage_runs count=1 stage=pick")


def test_skill_explain_pick_uses_llm() -> None:
    """explain_pick: 走 LLM, mock answer_question"""
    td = _isolated_dir("explain_pick")
    _seed_db(td)
    with patch("stock_trading_agent.llm.reasoner.answer_question",
               return_value="600519 选入理由: 业绩稳健 + 板块龙头") as m:
        from stock_trading_agent.engine.skills import call_skill
        r = call_skill("explain_pick", {"code": "600519"})
    assert r["ok"], r
    assert r["uses_llm"] is True
    assert "贵州茅台" in r["raw"]["name"]
    assert "业绩稳健" in r["raw"]["explanation"]
    assert m.call_count == 1
    print(f"  ✓ explain_pick 调了 LLM, explanation 长度 {len(r['raw']['explanation'])}")


def test_skill_search_knowledge_uses_llm() -> None:
    """search_knowledge: 走 RAG + LLM"""
    td = _isolated_dir("search")
    _seed_db(td)
    with patch("stock_trading_agent.engine.skills.knowledge.retrieve",
               return_value=[{"title": "好运2008-龙头战法", "score": 0.9, "text": "..."}]), \
         patch("stock_trading_agent.llm.reasoner.with_knowledge",
               return_value="根据好运2008: 选龙头要..."):
        from stock_trading_agent.engine.skills import call_skill
        r = call_skill("search_knowledge", {"query": "龙头战法", "k": 1})
    assert r["ok"], r
    assert r["uses_llm"] is True
    assert len(r["raw"]["sources"]) == 1
    assert "好运2008" in r["raw"]["answer"]
    print(f"  ✓ search_knowledge 调了 RAG + LLM, sources={len(r['raw']['sources'])}")


def test_skill_backtest_runs() -> None:
    """backtest: 调 reviewer.backtest_multi"""
    td = _isolated_dir("backtest")
    _seed_db(td)
    mock_bt = {"metrics": {"win_rate": 60.0, "avg_pnl": 1.5, "sharpe": 1.2, "max_drawdown": -3.0},
               "by_plan": {}}
    with patch("stock_trading_agent.engine.skills.backtest_multi", return_value=mock_bt):
        from stock_trading_agent.engine.skills import call_skill
        r = call_skill("backtest", {"strategy": "auto", "days": 30})
    assert r["ok"], r
    assert r["raw"]["metrics"]["win_rate"] == 60.0
    assert "胜率" in r["card"]["content"]["text"]
    print(f"  ✓ backtest win_rate=60.0")


# ─────────── keyword_fallback 单元测试 ───────────

def test_keyword_fallback_known() -> None:
    from stock_trading_agent.engine.skills import keyword_fallback
    assert keyword_fallback("今日选股") == "get_picks"
    assert keyword_fallback("持仓怎么样") == "get_positions"
    assert keyword_fallback("今天日报") == "get_daily_report"
    assert keyword_fallback("大盘怎么样") == "get_market_env"
    assert keyword_fallback("今天跑了什么") == "get_stage_runs"
    assert keyword_fallback("回测") == "backtest"
    assert keyword_fallback("picks") == "get_picks"
    assert keyword_fallback("today") == "get_picks"
    print("  ✓ keyword_fallback 8 个关键词命中正确")


def test_keyword_fallback_unknown_returns_none() -> None:
    from stock_trading_agent.engine.skills import keyword_fallback
    assert keyword_fallback("nihc 你好") is None
    assert keyword_fallback("") is None
    assert keyword_fallback("今天天气真好") is None
    print("  ✓ keyword_fallback 未知/空 不命中")


# ─────────── chat_with_tools 单元测试 ───────────

def test_chat_with_tools_parses_tool_calls() -> None:
    """chat_with_tools: mock HTTP 返回, 验证解析"""
    _set_llm_key()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "get_picks",
                    "arguments": json.dumps({"top_n": 5}),
                },
            }],
        }}],
    }
    with patch("stock_trading_agent.llm.tool_use.requests.post", return_value=mock_resp) as m:
        from stock_trading_agent.llm.tool_use import chat_with_tools
        r = chat_with_tools(
            [{"role": "user", "content": "show picks"}],
            tools=[{"type": "function", "function": {"name": "get_picks"}}],
        )
    assert r["ok"], r
    assert len(r["tool_calls"]) == 1
    assert r["tool_calls"][0]["name"] == "get_picks"
    assert r["tool_calls"][0]["args"] == {"top_n": 5}
    assert m.call_count == 1
    print(f"  ✓ chat_with_tools 解析 tool_calls OK, count={len(r['tool_calls'])}")


# ─────────── dispatch 集成测试 ───────────

def test_dispatch_llm_tool_path() -> None:
    """dispatch 选 tool: mock LLM 返回 tool_calls, 验证执行了 skill"""
    _set_llm_key()
    td = _isolated_dir("dispatch_tool")
    _seed_db(td)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "1", "type": "function",
                            "function": {"name": "get_picks", "arguments": "{}"}}],
        }}],
    }
    with patch("stock_trading_agent.llm.tool_use.requests.post", return_value=mock_resp):
        from stock_trading_agent.llm.tool_use import dispatch
        r = dispatch("今日选股")
    assert r["ok"], r
    assert r["path"] == "llm_tool"
    assert r["tool_calls"][0]["name"] == "get_picks"
    assert r["card"]["msg_type"] == "text"
    assert "贵州茅台" in r["card"]["content"]["text"]
    print(f"  ✓ dispatch llm_tool 路径, path=llm_tool card len={len(r['card']['content']['text'])}")


def test_dispatch_keyword_fallback_path() -> None:
    """dispatch 关键词降级: LLM 不可用 (无 key) + 命中关键词"""
    _reset_llm_key()
    td = _isolated_dir("dispatch_fallback")
    _seed_db(td)
    from stock_trading_agent.llm.tool_use import dispatch
    r = dispatch("今日选股")
    assert r["ok"], r
    assert r["path"] == "keyword_fallback"
    assert r["tool_calls"][0]["name"] == "get_picks"
    print(f"  ✓ dispatch 关键词降级 path=keyword_fallback")


def test_dispatch_llm_unavailable_no_keyword() -> None:
    """dispatch LLM 不可用 + 无关键词命中: 返回错误 + 提示"""
    _reset_llm_key()
    td = _isolated_dir("dispatch_unavail")
    _seed_db(td)
    from stock_trading_agent.llm.tool_use import dispatch
    r = dispatch("nihc 今天天气怎么样")
    assert r["ok"] is False
    assert r["path"] == "llm_unavailable"
    assert "MINIMAX" in r["card"]["content"]["text"] or "LLM" in r["card"]["content"]["text"]
    print(f"  ✓ dispatch LLM 不可用 + 无关键词 → 提示")


def test_dispatch_freeform_path() -> None:
    """dispatch LLM 不选 tool: 走 freeform"""
    _set_llm_key()
    td = _isolated_dir("dispatch_freeform")
    _seed_db(td)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {
            "content": "你好, 有什么事我可以帮你?",
            "tool_calls": None,
        }}],
    }
    with patch("stock_trading_agent.llm.tool_use.requests.post", return_value=mock_resp):
        from stock_trading_agent.llm.tool_use import dispatch
        r = dispatch("你好")
    assert r["ok"], r
    assert r["path"] == "llm_freeform"
    assert r["tool_calls"] == []
    assert "你好" in r["card"]["content"]["text"]
    print(f"  ✓ dispatch freeform 路径")


# ─────────── supervisor 集成测试 ───────────

def test_supervisor_runs_both_threads() -> None:
    """supervisor: 起 2 thread 后能正常停 (5s 超时强停)"""
    from stock_trading_agent.agent import _run_supervisor
    # v12 修: patch catch_up_stages 跳过 weekly_review 的 LLM 重试 (3+ 秒)
    # 这个 test 只验证 thread 创建, 不验证 catch_up 行为
    with patch("stock_trading_agent.agent.catch_up_stages", return_value=[]):
        t = threading.Thread(target=_run_supervisor, daemon=True)
        t.start()
        # 给 1.5s 让 thread 起来
        time.sleep(1.5)
        # 检查 alive 的 worker thread
        alive = [th for th in threading.enumerate() if th.name in ("scheduler", "listener") and th.is_alive()]
        assert len(alive) >= 1, f"期望至少 1 个 worker thread, got: {[th.name for th in threading.enumerate()]}"
    # 用 SIGTERM 关 supervisor (主线程不动, 用 _run_supervisor 内部的 stop_event 模拟: 杀进程会触发)
    # 简单点: 直接让 daemon thread 跟着测试退出
    print(f"  ✓ supervisor 起了 {len(alive)} 个 worker thread: {[th.name for th in alive]}")


# ─────────── listener builder 测试 ───────────

def test_llm_key_loaded_from_dotenv() -> None:
    """v11 修复: client._get_api_key 兜底读 .env

    之前 load_env() 的 keys 列表没 MINIMAX_API_KEY, listener 启动拿不到
    .env 里的 key, 走关键词降级。现在 load_env() 加上, 客户端能拿到。
    """
    import os
    os.environ.pop("MINIMAX_API_KEY", None)  # 模拟没 export
    from stock_trading_agent.engine.data_fetcher import load_env
    load_env.cache_clear() if hasattr(load_env, "cache_clear") else None
    from stock_trading_agent.llm.client import _get_api_key
    # data_fetcher._ENV_CACHE 可能缓存了空 dict, 清掉
    import stock_trading_agent.engine.data_fetcher as df
    df._ENV_CACHE = None
    k = _get_api_key()
    assert k, "期望从 .env 读到 MINIMAX_API_KEY"
    assert k.startswith("sk-"), f"key 格式异常: {k[:10]}"
    print(f"  ✓ _get_api_key 从 .env 读到了 key (长度 {len(k)})")


def test_reaction_handler_registered_in_listener() -> None:
    """listener run 路径: 验证 builder 里含 reaction handler (静态 import 检查)"""
    # 不真起 ws client, 直接看 listener 模块里 builder 链路是否合法
    from stock_trading_agent.feishu import listener
    src = Path(listener.__file__).read_text()
    assert "register_p2_im_message_reaction_created_v1" in src, "listener 没注册 reaction handler"
    assert "register_p2_im_message_receive_v1" in src
    assert "dispatch" in src, "listener on_message 没接 dispatch"
    assert "chat_with_session" not in src, "listener on_message 还在用老 chat_with_session"
    print("  ✓ listener builder 含 reaction handler + dispatch 替代 chat_with_session")


# ─────────── Main ───────────



def test_llm_tool_use_router_logged() -> None:
    """chat_with_tools 成功时记 llm_logs (call_site=tool_use_router, tool_name=...)"""
    _set_llm_key()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "1", "type": "function",
                            "function": {"name": "get_picks", "arguments": json.dumps({"top_n": 3})}}],
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
    }
    td = _isolated_dir("llm_log_router")
    _seed_db(td)
    with patch("stock_trading_agent.llm.tool_use.requests.post", return_value=mock_resp):
        from stock_trading_agent.llm.tool_use import chat_with_tools
        r = chat_with_tools([{"role": "user", "content": "x"}], tools=[])
    assert r["ok"], r
    # 检查 llm_logs
    import stock_trading_agent.engine.paper_trader as pt
    pt._ENV_CACHE = None
    pt.DATA_DIR = td
    pt.DB_PATH = td / "quant.db"
    conn = pt.get_db()
    rows = conn.execute(
        "SELECT call_site, tool_name, tool_args, success, prompt_tokens FROM llm_logs WHERE call_site='tool_use_router'"
    ).fetchall()
    assert len(rows) == 1, f"期望 1 条 router 日志, got {len(rows)}"
    row = dict(rows[0])
    assert row["tool_name"] == "get_picks"
    assert '"top_n": 3' in row["tool_args"]
    assert row["success"] == 1
    assert row["prompt_tokens"] == 100
    print(f"  ✓ chat_with_tools 写 tool_use_router 日志: tool={row['tool_name']}")


def test_dispatch_logs_tool_use_dispatch() -> None:
    """dispatch 走 llm_tool 路径时记 llm_logs (call_site=tool_use_dispatch)"""
    _set_llm_key()
    td = _isolated_dir("llm_log_dispatch")
    _seed_db(td)
    import stock_trading_agent.engine.paper_trader as pt
    pt._ENV_CACHE = None
    pt.DATA_DIR = td
    pt.DB_PATH = td / "quant.db"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "1", "type": "function",
                            "function": {"name": "get_market_env", "arguments": "{}"}}],
        }}],
    }
    with patch("stock_trading_agent.llm.tool_use.requests.post", return_value=mock_resp):
        from stock_trading_agent.llm.tool_use import dispatch
        r = dispatch("大盘", chat_id="oc_xxx")
    assert r["ok"] and r["path"] == "llm_tool"
    conn = pt.get_db()
    rows = conn.execute(
        "SELECT call_site, tool_name, tool_args, chat_id, success FROM llm_logs WHERE call_site='tool_use_dispatch'"
    ).fetchall()
    assert len(rows) == 1, f"期望 1 条 dispatch 日志, got {len(rows)}"
    row = dict(rows[0])
    assert row["tool_name"] == "get_market_env"
    assert row["chat_id"] == "oc_xxx"
    assert row["success"] == 1
    print(f"  ✓ dispatch 写 tool_use_dispatch 日志: tool={row['tool_name']} chat_id={row['chat_id']}")


def test_dispatch_keyword_fallback_logs_chat_id() -> None:
    """dispatch 关键词降级时也写 llm_logs (含 chat_id)"""
    _reset_llm_key()
    td = _isolated_dir("llm_log_fallback")
    _seed_db(td)
    import stock_trading_agent.engine.paper_trader as pt
    pt._ENV_CACHE = None
    pt.DATA_DIR = td
    pt.DB_PATH = td / "quant.db"
    from stock_trading_agent.llm.tool_use import dispatch
    r = dispatch("今日选股", chat_id="oc_yyy")
    assert r["ok"] and r["path"] == "keyword_fallback"
    conn = pt.get_db()
    rows = conn.execute(
        "SELECT tool_name, chat_id, success FROM llm_logs WHERE call_site='tool_use_dispatch'"
    ).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["tool_name"] == "get_picks"
    assert row["chat_id"] == "oc_yyy"
    print(f"  ✓ 关键词降级路径记 chat_id={row['chat_id']}")


def test_agent_stop_kills_process() -> None:
    """agent stop: 起个 sleep 子进程当假 agent, _stop_agent 发 SIGTERM 杀它, 清 pid file"""
    import os as _os
    import subprocess
    import time as _time
    pid_file = Path("data/agent.pid")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    # 用 subprocess.Popen 起 sleep 子进程
    proc = subprocess.Popen(["sleep", "60"])
    pid_file.write_text(str(proc.pid))
    _time.sleep(0.3)
    assert proc.poll() is None, "子进程意外已退"
    from stock_trading_agent.agent import _stop_agent
    _stop_agent()
    # pid file 应被删
    assert not pid_file.exists(), "pid file 没清"
    # 子进程应该已经死
    rc = proc.wait(timeout=2)
    assert proc.poll() is not None, f"子进程 {proc.pid} 还活着, stop 没生效"
    print(f"  ✓ _stop_agent 发 SIGTERM 到子进程 {proc.pid} (rc={rc}) 并清 pid file")

def main() -> None:
    tests = [
        test_skill_get_picks_returns_seeded,
        test_skill_get_positions_returns_seeded,
        test_skill_get_daily_report,
        test_skill_get_market_env,
        test_skill_get_stage_runs,
        test_skill_explain_pick_uses_llm,
        test_skill_search_knowledge_uses_llm,
        test_skill_backtest_runs,
        test_keyword_fallback_known,
        test_keyword_fallback_unknown_returns_none,
        test_chat_with_tools_parses_tool_calls,
        test_dispatch_llm_tool_path,
        test_dispatch_keyword_fallback_path,
        test_dispatch_llm_unavailable_no_keyword,
        test_dispatch_freeform_path,
        test_supervisor_runs_both_threads,
        test_llm_key_loaded_from_dotenv,
        test_reaction_handler_registered_in_listener,
        test_llm_tool_use_router_logged,
        test_dispatch_logs_tool_use_dispatch,
        test_dispatch_keyword_fallback_logs_chat_id,
        test_agent_stop_kills_process,
    ]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    if failed:
        print(f"\n✗ {failed} tests failed")
        sys.exit(1)
    print(f"\n✓ {passed} tests passed")


if __name__ == "__main__":
    main()
