"""test_v12_a_2.py — v12.A.2 体验优化包

Covers (5 改 1 包):
  1) admin_cmd /stage /health 接 handle() 修 BUG
  2) engine/cards.py 持仓卡片增强 (盈亏柱/板块分布/持仓天数)
  3) stages.open_auction push 异常吞掉 (不阻下游 pick)
  4) listener dedup cache 跨进程持久 (文件 fallback)
  5) data/paper_trader.db 从 git 移除 (gitignore)
  6) admin_cmd /picks /positions /env 仍正常 (回归)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ─────────── 1) admin_cmd /stage /health BUG 修 ───────────

def test_admin_stage_cmd() -> None:
    """v12.A.2 修: /stage 不再返 '未知命令'"""
    from stock_trading_agent.feishu import admin_cmd
    card = admin_cmd.handle("/stage", "user1", "chat1", {"feishu": {"admin_user_ids": ["user1"]}})
    assert card is not None
    assert card["msg_type"] == "text"
    # 内容包含 "Stage 记录"
    text = card["content"]["text"]
    assert "Stage" in text or "stage" in text, f"unexpected: {text[:100]}"


def test_admin_health_cmd() -> None:
    """v12.A.2 修: /health 不再返 '未知命令'"""
    from stock_trading_agent.feishu import admin_cmd
    card = admin_cmd.handle("/health", "user1", "chat1", {"feishu": {"admin_user_ids": ["user1"]}})
    assert card is not None
    assert card["msg_type"] == "text"
    text = card["content"]["text"]
    assert "LLM" in text or "调用" in text, f"unexpected: {text[:100]}"


def test_admin_help_lists_all_7() -> None:
    """/help 应列出 /stage /health 不再漏"""
    from stock_trading_agent.feishu import admin_cmd
    assert "/stage" in admin_cmd._HELP
    assert "/health" in admin_cmd._HELP


def test_admin_unknown_cmd() -> None:
    """未知命令仍返未知命令文案 (不挂)"""
    from stock_trading_agent.feishu import admin_cmd
    card = admin_cmd.handle("/xxx_unknown", "user1", "chat1", {"feishu": {"admin_user_ids": ["user1"]}})
    assert card is not None
    assert "未知命令" in card["content"]["text"]


def test_admin_not_admin_blocked() -> None:
    """非 admin 返 '权限不足'"""
    from stock_trading_agent.feishu import admin_cmd
    card = admin_cmd.handle("/stage", "mallory", "chat1", {"feishu": {"admin_user_ids": ["user1"]}})
    assert card is not None
    assert "权限不足" in card["content"]["text"]


# ─────────── 2) engine/cards.py 持仓卡片增强 ───────────

def test_cards_bar_positive() -> None:
    """v12.A.2: 盈亏柱 +5% 返绿色块"""
    from stock_trading_agent.engine.cards import _bar
    out = _bar(5.0)
    assert "🟩" in out
    assert "▇" in out


def test_cards_bar_negative() -> None:
    """v12.A.2: 盈亏柱 -3% 返红色块"""
    from stock_trading_agent.engine.cards import _bar
    out = _bar(-3.0)
    assert "🟥" in out
    assert "▇" in out


def test_cards_bar_zero() -> None:
    """v12.A.2: 盈亏柱 0 返中性"""
    from stock_trading_agent.engine.cards import _bar
    out = _bar(0)
    assert "─" in out


def test_cards_position_holding_days() -> None:
    """v12.A.2: 持仓天数 (从 pick_date 到 today)"""
    from datetime import date, timedelta
    from stock_trading_agent.engine.cards import _position_holding_days
    # 5 天前
    five_days_ago = (date.today() - timedelta(days=5)).isoformat()
    assert _position_holding_days(five_days_ago) == 5
    # 今天
    assert _position_holding_days(date.today().isoformat()) == 0
    # 空
    assert _position_holding_days("") == 0
    # 格式错
    assert _position_holding_days("not-a-date") == 0


def test_cards_position_with_sector_and_days() -> None:
    """v12.A.2: 持仓卡片含板块分布 + 持仓天数"""
    from datetime import date, timedelta
    from stock_trading_agent.engine.cards import card_positions
    five_days_ago = (date.today() - timedelta(days=5)).isoformat()
    items = [
        {"code": "002063", "name": "远光软件", "open_price": 8.5, "shares": 1000,
         "pnl_open_pct": 5.2, "status": "open", "sector": "软件",
         "pick_date": five_days_ago},
        {"code": "601318", "name": "中国平安", "open_price": 45.0, "shares": 500,
         "pnl_open_pct": -2.1, "status": "open", "sector": "保险",
         "pick_date": five_days_ago},
    ]
    card = card_positions(items)
    assert card["header"]["title"]["content"] == "持仓"
    # 板块分布应在 elements 里
    all_text = json.dumps(card, ensure_ascii=False)
    assert "板块分布" in all_text
    assert "软件" in all_text
    assert "保险" in all_text
    assert "5日" in all_text  # 持仓天数


def test_cards_position_empty() -> None:
    """无持仓 → 灰色 card + '暂无持仓'"""
    from stock_trading_agent.engine.cards import card_positions
    card = card_positions([])
    assert "暂无持仓" in card["elements"][0]["text"]["content"]


def test_cards_picks_unchanged() -> None:
    """v12.A.2: picks 卡片逻辑没改 (回归)"""
    from stock_trading_agent.engine.cards import card_picks
    card = card_picks([{"code": "002063", "name": "远光", "sector": "软件",
                         "score": 85.0, "chg_pct": 2.5, "plan": "A"}],
                       date="2026-06-13")
    assert "2026-06-13" in card["header"]["title"]["content"]
    assert "002063" in card["elements"][0]["text"]["content"]


# ─────────── 3) stages.open_auction push 异常吞掉 ───────────

def test_open_auction_push_fail_does_not_raise() -> None:
    """v12.A.2: push_anomaly 抛异常时 stage 仍 ok=True (不阻下游 pick)"""
    from stock_trading_agent.agent import stages
    from stock_trading_agent.agent.stages import stage_open_auction
    # mock pusher.push_anomaly 抛异常, mock get_open_positions 返 1 个持仓
    # 注意: stage_open_auction 已被装饰器包, push 异常被内部 try/except 吞
    with patch.object(stages.pusher, "push_anomaly", side_effect=RuntimeError("webhook挂了")), \
         patch.object(stages, "get_open_positions", return_value=[{"code": "002063", "name": "X"}]):
        result = stage_open_auction()  # 不应抛异常
    assert result["open_count"] == 1
    assert "opens" in result


def test_open_auction_not_in_retryable_stages() -> None:
    """v12.A.2 设计: open_auction 仍是 '非关键' (v12.9.2 分类), 不 retry

    治根因是 push_anomaly 异常被 try/except 吞 (改在 stage 函数体内), 不靠 retry
    """
    from stock_trading_agent.agent.stages import RETRYABLE_STAGES
    assert "open_auction" not in RETRYABLE_STAGES


# ─────────── 4) listener dedup cache 文件 fallback ───────────

def test_dedup_load_from_disk_empty() -> None:
    """v12.A.2: dedup_seen.json 不存在 → 返空 dict (不挂)"""
    import tempfile
    from stock_trading_agent.feishu import listener
    # mock _SEEN_FILE 指向临时空文件
    with patch.object(listener, "_SEEN_FILE", Path(tempfile.mkdtemp()) / "nope.json"):
        result = listener._load_seen_from_disk()
    assert result == {}


def test_dedup_load_from_disk_with_data() -> None:
    """v12.A.2: dedup_seen.json 有数据 → 加载到 _seen_msgs"""
    import tempfile
    from stock_trading_agent.feishu import listener
    tmp = Path(tempfile.mkdtemp()) / "dedup_seen.json"
    now = time.time()
    # 1 个新鲜 + 1 个过期
    tmp.write_text(json.dumps({
        "fresh_id": now,
        "old_id": now - 1000,  # 远大于 TTL 600s
    }))
    with patch.object(listener, "_SEEN_FILE", tmp), \
         patch.object(listener, "_DEDUP_TTL_S", 600):
        result = listener._load_seen_from_disk()
    assert "fresh_id" in result
    assert "old_id" not in result  # 过期被过滤


def test_dedup_mark_seen_writes_to_disk_every_30() -> None:
    """v12.A.2: _mark_seen 每 30 次写一次盘"""
    import tempfile
    from stock_trading_agent.feishu import listener
    tmp = Path(tempfile.mkdtemp()) / "dedup_seen.json"
    with patch.object(listener, "_SEEN_FILE", tmp), \
         patch.object(listener, "_DEDUP_MAX", 10000):
        # 29 次: 不写盘
        for i in range(29):
            assert listener._mark_seen(f"msg_{i}") is True
        assert not tmp.exists()
        # 第 30 次: 写盘
        listener._mark_seen("msg_29")
        assert tmp.exists()
        data = json.loads(tmp.read_text())
        assert "msg_29" in data


# ─────────── 5) data/paper_trader.db gitignore ───────────

def test_paper_trader_db_in_gitignore() -> None:
    """v12.A.2: data/paper_trader.db 加入 .gitignore"""
    gi = (ROOT / ".gitignore").read_text()
    assert "data/paper_trader.db" in gi


# ─────────── 6) admin_cmd 回归 (调 get_picks / positions / env) ───────────

def test_admin_picks_cmd() -> None:
    """/picks 调 get_picks skill"""
    from stock_trading_agent.feishu import admin_cmd
    with patch("stock_trading_agent.engine.skills.call_skill",
               return_value={"ok": True, "card": {"msg_type": "text", "content": {"text": "mocked picks"}}}):
        card = admin_cmd.handle("/picks", "user1", "chat1", {"feishu": {"admin_user_ids": ["user1"]}})
    assert card is not None
    assert "mocked picks" in card["content"]["text"]


def test_admin_positions_cmd() -> None:
    """/positions 调 get_positions skill"""
    from stock_trading_agent.feishu import admin_cmd
    with patch("stock_trading_agent.engine.skills.call_skill",
               return_value={"ok": True, "card": {"msg_type": "text", "content": {"text": "mocked positions"}}}):
        card = admin_cmd.handle("/positions", "user1", "chat1", {"feishu": {"admin_user_ids": ["user1"]}})
    assert card is not None
    assert "mocked positions" in card["content"]["text"]


# ─────────── 7) cards.py 路径迁移回归 ───────────

def test_cards_module_path() -> None:
    """v12.A.2: cards.py 在 engine/ 下, 不在 feishu/ 下"""
    import importlib
    mod = importlib.import_module("stock_trading_agent.engine.cards")
    assert mod.__file__
    assert "engine/cards.py" in mod.__file__
    # 老路径已删
    feishu_cards = ROOT / "stock_trading_agent" / "feishu" / "card_templates.py"
    assert not feishu_cards.exists(), f"老文件还在: {feishu_cards}"


# ─────────── 8) v12.A.2 BUG FIX: date shadow ───────────

def test_fetch_stock_kline_no_date_shadow() -> None:
    """v12.A.2 修: fetch_stock_kline 参数 date (str) 跟 import date (class) 同名
    → shadow 致 AttributeError 'str has no attribute fromisoformat'
    修: import 别名 _date, 函数体内用 _date.today() / _date.fromisoformat()
    """
    from stock_trading_agent.engine.data_fetcher import fetch_stock_kline
    # date='today' 之前会炸 AttributeError, 现在应正常返 (空 dict 也行, 不报异常)
    try:
        r = fetch_stock_kline("002063", "today")
        # 期望: 返 dict, 不抛 AttributeError
        assert isinstance(r, dict), f"expect dict, got {type(r)}"
    except AttributeError as e:
        raise AssertionError(f"date shadow bug 复发: {e}")


def test_fetch_stock_kline_date_str_shadow() -> None:
    """date=YYYY-MM-DD 也走 _date.fromisoformat, 不应抛"""
    from stock_trading_agent.engine.data_fetcher import fetch_stock_kline
    try:
        r = fetch_stock_kline("002063", "2026-06-12")
        assert isinstance(r, dict)
    except AttributeError as e:
        raise AssertionError(f"date shadow 复发 (fromisoformat): {e}")


def test_data_fetcher_date_alias_in_globals() -> None:
    """import 用别名 _date, 防止 shadow"""
    import stock_trading_agent.engine.data_fetcher as df
    assert hasattr(df, "_date"), "data_fetcher 应 import 'date as _date'"
    from datetime import date as _date
    assert df._date is _date


def test_is_trading_day_uses_date_alias() -> None:
    """is_trading_day() 默认参应正常 (无 ref_date) — 之前 date.today() 隐式依赖 class"""
    from stock_trading_agent.engine.data_fetcher import is_trading_day
    # 不应抛 AttributeError
    result = is_trading_day()
    assert isinstance(result, bool)


# ─────────── 9) _run_get_picks empty UX 增强 ───────────

def test_get_picks_empty_shows_stage_reason() -> None:
    """v12.A.2 增强: picks 空时告诉用户 stage 状态 (治'答非所问')"""
    from stock_trading_agent.engine.skills import call_skill
    # picks 表 0 行, stage_runs 也没 pick → "还没跑"
    r = call_skill("get_picks", {})
    assert r["ok"] is True
    assert r["raw"]["count"] == 0
    assert "empty_reason" in r["raw"]
    # stage_runs 里有今天 open_auction 没 pick → 还没跑
    assert "还没跑" in r["raw"]["empty_reason"] or "14:00" in r["raw"]["empty_reason"]
    # 卡片 text 应包含 friendly reason
    assert r["card"]["msg_type"] == "text"
    assert "📭" in r["card"]["content"]["text"]
    assert "14:00" in r["card"]["content"]["text"] or "还没跑" in r["card"]["content"]["text"]


def test_get_picks_with_items_unchanged() -> None:
    """v12.A.2: picks 有数据时仍走 card_picks 路径 (回归)"""
    from stock_trading_agent.engine import paper_trader
    from stock_trading_agent.engine.skills import call_skill
    # 临时插一行
    conn = paper_trader.get_db()
    from datetime import date
    today = date.today().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO picks "
        "(pick_date, code, name, price, prev_close, chg_pct, turnover, amplitude, "
        " score, sector, in_theme, plan, plan_used, market_env_score, market_env_level, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (today, "002063", "远光软件", 8.5, 8.3, 2.4, 1.2, 3.5,
         85.0, "软件", 1, "A", "A", 65.0, "中等", "2026-06-13T10:00:00"),
    )
    conn.commit()
    r = call_skill("get_picks", {})
    assert r["ok"] is True
    assert r["raw"]["count"] >= 1
    assert "empty_reason" not in r["raw"]
    assert r["card"]["msg_type"] == "interactive"  # picks 有数据走 card
    # 清理
    conn.execute("DELETE FROM picks WHERE code=?", ("002063",))
    conn.commit()

if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    fail = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            fail += 1
    print(f"\n{'OK' if fail == 0 else 'FAIL'} {len(tests) - fail}/{len(tests)} tests passed")
    sys.exit(0 if fail == 0 else 1)
