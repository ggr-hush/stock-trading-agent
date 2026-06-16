"""
paper_trader.py — 虚拟账户 + 模拟成交
- 14:00 按收盘价开仓（不超过仓位/并发上限）
- 次日 09:30 按回填开盘价模拟成交
- 次日 12:00 按回填中午价模拟成交
- 跟踪 PnL，跟实盘手单对账（discrepancy_note）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from .data_fetcher import load_config

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = DATA_DIR / "quant.db"


# ─────────── DB Schema ───────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY,
    initial_capital REAL NOT NULL,
    cash REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    open_price REAL NOT NULL,
    open_amount REAL NOT NULL,
    shares REAL NOT NULL,
    sector TEXT,
    plan TEXT,
    score REAL,
    status TEXT NOT NULL DEFAULT 'open',  -- open / closed_open / closed_noon
    close_open_price REAL,
    close_noon_price REAL,
    pnl_open REAL,
    pnl_noon REAL,
    pnl_open_pct REAL,
    pnl_noon_pct REAL,
    actual_buy_price REAL,
    actual_sell_price REAL,
    discrepancy_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    prev_close REAL,
    chg_pct REAL,
    turnover REAL,
    amplitude REAL,
    score REAL,
    sector TEXT,
    in_theme INTEGER,
    plan TEXT NOT NULL,
    plan_used TEXT,
    market_env_score INTEGER,
    market_env_level TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(pick_date, code)
);

CREATE TABLE IF NOT EXISTS params_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    param_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    auto_applied INTEGER NOT NULL DEFAULT 0,  -- 1=在 safe_range 内自动改，0=人工确认
    weekly_win_rate REAL,
    weekly_avg_pnl REAL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_date TEXT NOT NULL UNIQUE,
    pick_count INTEGER,
    plan_a_count INTEGER,
    plan_b_count INTEGER,
    plan_c_count INTEGER,
    env_score INTEGER,
    paper_pnl_open REAL,
    paper_pnl_noon REAL,
    paper_pnl_open_pct REAL,
    paper_pnl_noon_pct REAL,
    win_rate_noon REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS bot_sessions (
    session_id TEXT PRIMARY KEY,
    last_active TEXT NOT NULL,
    history TEXT NOT NULL DEFAULT '[]',
    turn_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_at TEXT NOT NULL,
    call_site TEXT NOT NULL,  -- pick_intro / risk_explain / param_reason / weekly_summary / empty_day / anomaly / tool_use_router / tool_use_dispatch
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    latency_ms INTEGER,
    success INTEGER NOT NULL,
    error TEXT,
    -- v11: tool-use 路由日志新增列 (老库用 ALTER TABLE 兼容)
    tool_name TEXT,
    tool_args TEXT,
    chat_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_pick_date ON paper_positions(pick_date);
CREATE INDEX IF NOT EXISTS idx_positions_code ON paper_positions(code);
CREATE INDEX IF NOT EXISTS idx_picks_date ON picks(pick_date);
CREATE INDEX IF NOT EXISTS idx_params_changed_at ON params_history(changed_at);

-- v12.A.4: 结构化复盘 (借鉴 felix reviews 表)
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                 -- 复盘日期 YYYY-MM-DD
    stock_code TEXT NOT NULL,           -- 股票代码
    stock_name TEXT,                    -- 股票名称 (冗余便于读)
    signal_id INTEGER,                  -- 关联的 signal (可选)
    action_taken INTEGER NOT NULL DEFAULT 0,  -- 是否实操 0/1
    reason TEXT NOT NULL DEFAULT '',    -- 操作理由 / 买入理由
    result TEXT NOT NULL DEFAULT '',    -- 结果 (盈亏 % / 备注)
    summary TEXT NOT NULL DEFAULT '',   -- 总结 / 反思
    tags TEXT NOT NULL DEFAULT '[]',    -- JSON 数组 ["止盈","早盘冲高","题材退潮"]
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_date ON reviews(date);
CREATE INDEX IF NOT EXISTS idx_reviews_stock_code ON reviews(stock_code);
CREATE INDEX IF NOT EXISTS idx_reviews_action_taken ON reviews(action_taken);

-- v7.4: stage 依赖图运行记录
CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    run_date TEXT NOT NULL,    -- YYYY-MM-DD
    ran_at TEXT NOT NULL,      -- ISO8601
    ok INTEGER NOT NULL,       -- 1=成功, 0=失败
    UNIQUE(stage, run_date)
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_date ON stage_runs(run_date);
CREATE INDEX IF NOT EXISTS idx_stage_runs_stage ON stage_runs(stage);

-- v12: 用户偏好 (稳定) + 情景记忆 (带 TTL)
CREATE TABLE IF NOT EXISTS user_profile (
    chat_id TEXT PRIMARY KEY,
    prefs_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    type TEXT NOT NULL,         -- preference | fact | decision | interaction
    content TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 1,  -- 1-3, 默认 1
    created_at TEXT NOT NULL,
    ttl_days INTEGER NOT NULL DEFAULT 90,
    source TEXT                 -- user / detected / explicit
);
CREATE INDEX IF NOT EXISTS idx_memories_chat ON memories(chat_id);
CREATE INDEX IF NOT EXISTS idx_memories_chat_created ON memories(chat_id, created_at);
"""


def get_db() -> sqlite3.Connection:
    """连接 SQLite，自动建表 + 兼容老库 ALTER"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # v11: 老库 (在 v11 之前创建的 quant.db) 缺 3 列, 同步加上
    for col, decl in (
        ("tool_name", "TEXT"),
        ("tool_args", "TEXT"),
        ("chat_id", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE llm_logs ADD COLUMN {col} {decl}")
        except Exception:
            pass  # 已存在, 忽略
    conn.commit()
    return conn


def mark_stage_run(stage: str, ok: bool = True) -> None:
    """v7.4: 记录某 stage 当日已跑过 (用于依赖图检查)"""
    from datetime import datetime as _dt
    conn = get_db()
    today = _dt.now().strftime("%Y-%m-%d")
    now = _dt.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO stage_runs(stage, run_date, ran_at, ok) VALUES (?, ?, ?, ?)
           ON CONFLICT(stage, run_date) DO UPDATE SET ran_at=excluded.ran_at, ok=excluded.ok""",
        (stage, today, now, 1 if ok else 0),
    )
    conn.commit()


def was_stage_run_today(stage: str) -> bool:
    """v7.4: 检查某 stage 今天是否跑过 (依赖图用)"""
    from datetime import datetime as _dt
    conn = get_db()
    today = _dt.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT 1 FROM stage_runs WHERE stage=? AND run_date=? AND ok=1",
        (stage, today),
    ).fetchone()
    return row is not None


# ─────────── 账户 ───────────

def init_account(initial_capital: float | None = None) -> dict[str, Any]:
    """初始化虚拟账户（幂等）"""
    if initial_capital is None:
        cfg = load_config()
        initial_capital = cfg["paper"]["initial_capital"]
    conn = get_db()
    row = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    if row:
        return dict(row)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO paper_account (id, initial_capital, cash, updated_at) VALUES (1, ?, ?, ?)",
        (initial_capital, initial_capital, now),
    )
    conn.commit()
    return {"id": 1, "initial_capital": initial_capital, "cash": initial_capital, "updated_at": now}


def get_account() -> dict[str, Any]:
    return init_account()


# ─────────── 开仓 ───────────



def _check_sector_concentration(stocks_to_open: list[dict], existing_positions: list[dict],
                                 max_sector_ratio: float, max_sector_concurrent: int,
                                 pos_ratio: float, total_cap: float) -> list[dict]:
    """检查行业集中度, 过滤超限的票

    规则:
    1. max_sector_concurrent: 同一板块已有 N 只持仓, 则不再开
    2. max_sector_ratio: 同一板块总仓位 (历史+新) / 总资金 > max_sector_ratio, 则不开

    Args:
        stocks_to_open: 候选 stock 列表 [{code, name, sector, ...}, ...]
        existing_positions: 已有持仓 [{sector, open_amount}, ...]
        max_sector_ratio: 板块最大仓位比 (0.5 = 50%)
        max_sector_concurrent: 板块最大同时持仓数
        pos_ratio: 今日仓位系数
        total_cap: 总资金

    Returns:
        过滤后的 stocks 列表 (符合集中度约束)
    """
    if not stocks_to_open:
        return []
    if max_sector_ratio <= 0 and max_sector_concurrent <= 0:
        return stocks_to_open
    # 已有板块统计
    sector_amount: dict[str, float] = {}
    sector_count: dict[str, int] = {}
    for p in existing_positions:
        sec = p.get("sector", "") or "(unknown)"
        sector_amount[sec] = sector_amount.get(sec, 0) + (p.get("open_amount") or 0)
        sector_count[sec] = sector_count.get(sec, 0) + 1
    out: list[dict] = []
    for s in stocks_to_open:
        sec = s.get("sector", "") or "(unknown)"
        # 1) 并发上限
        if max_sector_concurrent > 0 and sector_count.get(sec, 0) >= max_sector_concurrent:
            continue
        # 2) 仓位上限: 假设这只开 amount, 加进板块后总和 / total_cap
        if max_sector_ratio > 0:
            add_amount = (s.get("position_advice_amount") or 0)
            if (sector_amount.get(sec, 0) + add_amount) / max(total_cap, 1) > max_sector_ratio:
                continue
        out.append(s)
        # 累加 (用于后续票判断)
        sector_amount[sec] = sector_amount.get(sec, 0) + (s.get("position_advice_amount") or 0)
        sector_count[sec] = sector_count.get(sec, 0) + 1
    return out


def _check_sector_correlation(
    stocks_to_open: list[dict],
    existing_positions: list[dict],
    total_cap: float,
    group_max_ratio: float,
    groups: list[list[str]] | None = None,
    auto_learn: bool = False,
    learn_window_days: int = 60,
    learn_threshold: float = 0.7,
) -> list[dict]:
    """v7.2: 板块相关性矩阵约束 (防因子集中)

    同一 group 内的板块视为高相关 (e.g. ["新能源车", "锂电池", "充电桩"]),
    该 group 内 (已有持仓 + 本批) 的总仓位 / total_cap 不能超 group_max_ratio。

    Args:
        stocks_to_open: 已过 _check_sector_concentration 的候选
        existing_positions: 已有持仓 [{sector, open_amount}, ...]
        total_cap: 总资金
        group_max_ratio: 任一 group 最大仓位比 (0.6 = 60%)
        groups: 相关性 group 列表, 每个 group 是板块名 list

    Returns:
        过滤后的 stocks 列表
    """
    if not stocks_to_open or group_max_ratio <= 0:
        return stocks_to_open

    # v8.1: auto_learn 优先, 用历史 pnl 学 group
    if auto_learn:
        try:
            from .correlation import auto_learn_groups
            groups = auto_learn_groups(window_days=learn_window_days,
                                        threshold=learn_threshold)
        except Exception:
            groups = []

    if not groups:
        return stocks_to_open

    # 把每个板块映到它所在的 group index (-1 = 不在任何 group)
    sec_to_group: dict[str, int] = {}
    for gi, g in enumerate(groups):
        for sec in g:
            sec_to_group[sec] = gi

    # 累加已有持仓按 group 维度
    group_amount: dict[int, float] = {}
    for p in existing_positions:
        sec = p.get("sector", "") or "(unknown)"
        gi = sec_to_group.get(sec, -1)
        if gi < 0:
            continue
        group_amount[gi] = group_amount.get(gi, 0) + (p.get("open_amount") or 0)

    out: list[dict] = []
    for s in stocks_to_open:
        sec = s.get("sector", "") or "(unknown)"
        gi = sec_to_group.get(sec, -1)
        if gi < 0:
            # 不在任何相关 group 里, 不受此约束
            out.append(s)
            continue
        add_amount = s.get("position_advice_amount") or 0
        if (group_amount.get(gi, 0) + add_amount) / max(total_cap, 1) > group_max_ratio:
            # 超 group 上限, 过滤
            continue
        out.append(s)
        # 累加 (用于本批后续票)
        group_amount[gi] = group_amount.get(gi, 0) + add_amount
    return out


def simulate_profile(pick_result: dict[str, Any], profile_cfg: dict[str, Any]) -> dict[str, Any]:
    """v9.2: 用指定 profile 模拟开仓, 返回 (会开几只 + 哪些)

    不真写 DB, 只跑过滤逻辑。给多账户对比用。
    """
    stocks = pick_result.get("filtered_stocks", [])
    plan_used = pick_result.get("plan_used", "C")
    env = pick_result.get("market_env", {})
    pos_ratio = env.get("position_ratio", 0)
    if plan_used == "C" or pos_ratio <= 0 or not stocks:
        return {"plan": plan_used, "n_open": 0, "stocks": []}

    cap = profile_cfg.get("initial_capital", 1_000_000.0)
    existing = [{"sector": k, "open_amount": v["amount"]} for k, v in get_open_sector_stats().items()]
    cand = _check_sector_concentration(
        stocks, existing,
        max_sector_ratio=profile_cfg.get("max_sector_ratio", 0.5),
        max_sector_concurrent=profile_cfg.get("max_sector_concurrent", 2),
        pos_ratio=pos_ratio, total_cap=cap,
    )
    cand = _check_sector_correlation(
        cand, existing,
        total_cap=cap,
        group_max_ratio=profile_cfg.get("correlated_group_max_ratio", 0.0),
        groups=profile_cfg.get("correlated_sector_groups", []),
        auto_learn=profile_cfg.get("auto_learn_correlated_groups", False),
    )
    max_c = profile_cfg.get("max_concurrent", 3)
    final = cand[:max_c]
    return {
        "plan": plan_used,
        "n_open": len(final),
        "n_filtered": len(stocks) - len(final),
        "stocks": [{"code": s.get("code"), "name": s.get("name"),
                    "sector": s.get("sector"), "score": s.get("score")}
                   for s in final],
    }


def run_multi_account(pick_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """v9.2: 用 config.multi_account.profiles 跑多个 profile, 横向对比"""
    cfg = load_config()
    ma = cfg.get("multi_account", {})
    if not ma.get("enabled", False):
        return {}
    profiles = ma.get("profiles", {})
    if not profiles:
        return {}
    base_cap = cfg.get("paper", {}).get("initial_capital", 1_000_000.0)
    out: dict[str, dict[str, Any]] = {}
    for name, prof in profiles.items():
        prof_with_cap = {**prof, "initial_capital": base_cap}
        out[name] = simulate_profile(pick_result, prof_with_cap)
    return out


def get_open_sector_stats() -> dict[str, dict[str, float]]:
    """当前 open 持仓的板块统计 {sector: {amount, count}}"""
    conn = get_db()
    rows = conn.execute(
        "SELECT sector, open_amount FROM paper_positions WHERE status='open'"
    ).fetchall()
    stats: dict[str, dict[str, float]] = {}
    for r in rows:
        sec = r["sector"] or "(unknown)"
        if sec not in stats:
            stats[sec] = {"amount": 0.0, "count": 0}
        stats[sec]["amount"] += r["open_amount"] or 0
        stats[sec]["count"] += 1
    return stats


def open_positions(pick_result: dict[str, Any], config: dict[str, Any] | None = None) -> int:
    """14:00 选股完成后调用：按 position_advice_amount 开 paper 仓

    Returns:
        开仓笔数
    """
    if config is None:
        config = load_config()
    account = init_account(config["paper"]["initial_capital"])
    cap = account["initial_capital"]
    conn = get_db()
    pick_date = pick_result["date"]
    plan_used = pick_result.get("plan_used", "C")
    env = pick_result.get("market_env", {})
    now = datetime.now().isoformat()

    # 幂等：同一天已开过则跳过
    existing = conn.execute(
        "SELECT COUNT(*) AS c FROM paper_positions WHERE pick_date=? AND status='open'",
        (pick_date,),
    ).fetchone()["c"]
    if existing:
        return 0

    # 写入 picks 表（所有方案 A/B 候选，便于复盘）
    for s in pick_result.get("filtered_stocks", []):
        conn.execute(
            """
            INSERT OR REPLACE INTO picks
            (pick_date, code, name, price, prev_close, chg_pct, turnover, amplitude,
             score, sector, in_theme, plan, plan_used, market_env_score, market_env_level, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pick_date, s.get("code"), s.get("name"), s.get("price"),
                s.get("prev_close"), s.get("chg_pct"), s.get("turnover"),
                s.get("amplitude"), s.get("score"), s.get("sector"),
                1 if s.get("in_theme") else 0,
                s.get("plan_advice", "A"),
                plan_used,
                env.get("env_score"),
                env.get("env_level"),
                now,
            ),
        )

    # 实际开仓：按 plan_used，只取 N 只
    max_concurrent = config["paper"]["max_concurrent"]
    pos_ratio = env.get("position_ratio", 0)
    if pos_ratio <= 0 or plan_used == "C":
        conn.commit()
        return 0

    paper_cfg = config["paper"]
    raw_candidates = [s for s in pick_result.get("filtered_stocks", [])
                     if s.get("position_advice_amount", 0) > 0]
    # ── 行业集中度约束 (v1) ──
    existing = get_open_sector_stats()
    existing_list = [{"sector": k, "open_amount": v["amount"]} for k, v in existing.items()]
    candidates = _check_sector_concentration(
        raw_candidates, existing_list,
        max_sector_ratio=paper_cfg.get("max_sector_ratio", 0.5),
        max_sector_concurrent=paper_cfg.get("max_sector_concurrent", 2),
        pos_ratio=pos_ratio, total_cap=cap,
    )
    # ── 板块相关性约束 (v7.2 + v8.1 auto-learn) ──
    candidates = _check_sector_correlation(
        candidates, existing_list,
        total_cap=cap,
        group_max_ratio=paper_cfg.get("correlated_group_max_ratio", 0.0),
        groups=paper_cfg.get("correlated_sector_groups", []),
        auto_learn=paper_cfg.get("auto_learn_correlated_groups", False),
        learn_window_days=paper_cfg.get("correlation_window_days", 60),
        learn_threshold=paper_cfg.get("correlation_threshold", 0.7),
    )
    candidates = candidates[:max_concurrent]
    n = len(candidates)
    if n == 0:
        conn.commit()
        return 0

    per_position = cap * pos_ratio / n

    opened = 0
    for s in candidates:
        price = float(s.get("price") or 0)
        if price <= 0:
            continue
        amount = round(per_position, 0)
        shares = round(amount / price, 0)  # 整百股
        if shares <= 0 or amount > account["cash"]:
            continue
        conn.execute(
            """
            INSERT INTO paper_positions
            (pick_date, code, name, open_price, open_amount, shares, sector, plan, score, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                pick_date, s.get("code"), s.get("name"), price, amount, shares,
                s.get("sector", ""), plan_used, s.get("score"), now, now,
            ),
        )
        conn.execute(
            "UPDATE paper_account SET cash = cash - ?, updated_at = ? WHERE id=1",
            (amount, now),
        )
        opened += 1

    conn.commit()
    return opened


# ─────────── 模拟成交 ───────────

def fill_open_prices(next_day: str, price_map: dict[str, float]) -> int:
    """次日 09:30 调用：按开盘价模拟成交（开盘卖）

    Args:
        next_day: YYYY-MM-DD（实际是次一交易日）
        price_map: {code: open_price}

    Returns:
        成交笔数
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM paper_positions WHERE status='open' AND pick_date < ?",
        (next_day,),
    ).fetchall()
    now = datetime.now().isoformat()
    filled = 0
    for r in rows:
        code = r["code"]
        if code not in price_map:
            continue
        open_price = float(price_map[code])
        pnl = round((open_price - r["open_price"]) * r["shares"], 2)
        pnl_pct = round((open_price - r["open_price"]) / r["open_price"] * 100, 2) if r["open_price"] > 0 else 0
        conn.execute(
            """
            UPDATE paper_positions
            SET status='closed_open', close_open_price=?, pnl_open=?, pnl_open_pct=?, updated_at=?
            WHERE id=?
            """,
            (open_price, pnl, pnl_pct, now, r["id"]),
        )
        filled += 1
    conn.commit()
    return filled


def fill_noon_prices(next_day: str, price_map: dict[str, float]) -> int:
    """次日 12:00 调用：按中午价模拟成交（中午卖）"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM paper_positions WHERE status IN ('open', 'closed_open') AND pick_date < ?",
        (next_day,),
    ).fetchall()
    now = datetime.now().isoformat()
    filled = 0
    for r in rows:
        code = r["code"]
        if code not in price_map:
            continue
        noon_price = float(price_map[code])
        pnl = round((noon_price - r["open_price"]) * r["shares"], 2)
        pnl_pct = round((noon_price - r["open_price"]) / r["open_price"] * 100, 2) if r["open_price"] > 0 else 0
        # 现金回账户
        amount_back = round(noon_price * r["shares"], 2)
        conn.execute(
            """
            UPDATE paper_positions
            SET status='closed_noon', close_noon_price=?, pnl_noon=?, pnl_noon_pct=?, updated_at=?
            WHERE id=?
            """,
            (noon_price, pnl, pnl_pct, now, r["id"]),
        )
        conn.execute(
            "UPDATE paper_account SET cash = cash + ?, updated_at = ? WHERE id=1",
            (amount_back, now),
        )
        filled += 1
    conn.commit()
    return filled


# ─────────── 实际手单对账 ───────────

def record_actual_trade(
    code: str,
    pick_date: str,
    actual_buy_price: float,
    actual_sell_price: float,
    note: str = "",
) -> int:
    """用户手动录入实际手单，写入 discrepancy_note 便于复盘"""
    conn = get_db()
    now = datetime.now().isoformat()
    cur = conn.execute(
        """
        UPDATE paper_positions
        SET actual_buy_price=?, actual_sell_price=?, discrepancy_note=?, updated_at=?
        WHERE code=? AND pick_date=?
        """,
        (actual_buy_price, actual_sell_price, note, now, code, pick_date),
    )
    conn.commit()
    return cur.rowcount


# ─────────── 选股记录 (v3.1) ───────────

def record_picks(pick_date: str, stocks: list[dict[str, Any]], plan_used: str,
                 market_env_score: int | None = None, market_env_level: str | None = None) -> int:
    """v3.1: stage_pick 末尾调用, 把今日选股结果写入 picks 表

    之前: stage_pick 跑完只 push_pick 推飞书, picks 表永远空, 飞书问"今日选股" 看不到
    现在: 推飞书前先 INSERT INTO picks, 飞书再问就能拿到同一份数据

    Returns:
        写入的票数
    """
    from datetime import datetime as _dt
    conn = get_db()
    now = _dt.now().isoformat(timespec="seconds")
    written = 0
    for s in stocks or []:
        try:
            conn.execute(
                """
                INSERT INTO picks(
                    pick_date, code, name, price, prev_close, chg_pct,
                    turnover, amplitude, score, sector, in_theme, plan,
                    plan_used, market_env_score, market_env_level, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pick_date, code) DO UPDATE SET
                    price=excluded.price,
                    chg_pct=excluded.chg_pct,
                    turnover=excluded.turnover,
                    score=excluded.score,
                    sector=excluded.sector,
                    plan_used=excluded.plan_used,
                    market_env_score=excluded.market_env_score,
                    market_env_level=excluded.market_env_level
                """,
                (
                    pick_date,
                    str(s.get("code", "")),
                    str(s.get("name", "")),
                    float(s.get("price", 0) or 0),
                    float(s.get("prev_close", 0) or 0),
                    float(s.get("chg_pct", 0) or 0),
                    float(s.get("turnover", 0) or 0),
                    float(s.get("amplitude", 0) or 0),
                    float(s.get("score", 0) or 0),
                    str(s.get("sector", "") or ""),
                    1 if s.get("in_theme") else 0,
                    plan_used,
                    plan_used,
                    market_env_score,
                    market_env_level,
                    now,
                ),
            )
            written += 1
        except Exception as e:  # noqa: BLE001
            log.warning("record_picks 写 %s 失败: %s", s.get("code"), e)
    conn.commit()
    return written


# ─────────── 查询 ───────────

def get_open_positions() -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY pick_date DESC").fetchall()
    return [dict(r) for r in rows]


def get_paper_pnl() -> dict[str, Any]:
    """当前累计 PnL"""
    conn = get_db()
    rows = conn.execute("SELECT * FROM paper_positions WHERE status='closed_noon'").fetchall()
    account = get_account()
    total_pnl = sum(r["pnl_noon"] for r in rows)
    wins = sum(1 for r in rows if r["pnl_noon"] > 0)
    return {
        "closed_count": len(rows),
        "win_count": wins,
        "win_rate": round(wins / len(rows) * 100, 1) if rows else 0,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / account["initial_capital"] * 100, 2),
        "current_cash": account["cash"],
    }
