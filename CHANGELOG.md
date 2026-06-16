# Changelog

本项目所有重要变更记录于此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [v12.A.4.c] - 2026-06-17

**v3.1 选股策略: 治"今日选股 5 只票很垃圾, 有 -6% 票"**。A+B+C+D+E 一起做, 4 改 1 包。

### 根因
- **A 评分公式 bug**: `chg_score = min(abs(chg - 3.0) / 1.0, 1) * 20` 用了 `abs()`, 让跌 5% 票跟涨 3% 票同分 (跌 5% 偏离 8 → min(8,1)=1, * 20 = 20 分, **满分**)
- **B 阈值太严**: plan_a/b 要求 3-4% 涨 + 8-10% 换手, 全市场 5000 只满足的不到 10 只, 几乎永远走 plan_c 兜底
- **C plan_c 兜底无下限**: 任何 top200 票都进, 推低分冷门小盘股
- **D stage_pick 不写 picks 表**: 飞书问"今日选股" 走 fallback 路径, 看不到推过的票

### 4 改 1 包

**A. 评分公式方向感知** (`engine/picker.py:_score_stock`)
- 跌 (chg < 0) → chg_score = 0
- 涨且 |chg-3| ≤ 1 → chg_score = 20 (满 20 分)
- 偏离 3% 越远越低: `chg_score = max(20 - |chg-3| * 10, 0)`

**B. plan_a/b 阈值放宽** (`config.yaml`)
- plan_a: chg [1.5, 5.0) + 换手 [3, 15] (原 [3, 4) + [8, 10])
- plan_b: chg [0.5, 7.0) + 换手 [1, 20] (原 [3, 4) + [6, 10])

**C. plan_c 兜底收紧** (`engine/picker.py`)
- 之前 plan_c = [] (空仓)
- 现在 plan_c 路径: 从全池(非 hard_excl) 拿评分 ≥ 60 的高分票
- 防止推低分冷门小盘股

**D. stage_pick 写 picks 表** (`agent/stages.py:stage_pick` + `engine/paper_trader.py:record_picks`)
- 之前: stage_pick 跑完只 push_pick 推飞书, picks 表永远空
- 现在: 先 INSERT INTO picks (ON CONFLICT UPDATE), 再推飞书
- 飞书问"今日选股" 能从 picks 表拿到跟推送一致的数据

### Test
- **新文件** `tests/test_v12_a_4_c_picker_v31.py` (8 个全绿): 评分方向感知 3 + 阈值放宽 2 + plan_c 兜底 1 + 写 picks 表 2
- **picker 老 6 测试不退化** (test_picker.py 6/6 全绿)
- **v12.A.* 全套**: regime 14/14 + reviews 12/12 + cache 8/8 + tushare 26/26 + picker_v31 8/8 = **68/68 全绿**
- **paper_trader 4/4**: plan_c 不开仓仍然 OK (open_positions 在 plan_used=="C" 时早返 0)

### 改动文件
- **改 3 个**: `engine/picker.py` (_score_stock + plan_c 兜底) / `config.yaml` (plan_a/b 阈值) / `agent/stages.py` (stage_pick 调 record_picks) / `engine/paper_trader.py` (新加 record_picks 函数)
- **新 1 个**: `tests/test_v12_a_4_c_picker_v31.py` (8 测试)

### 端到端验证 (用户跑)
- `bash deploy/install_launchd.sh restart`
- 飞书发"今日选股" → 看到 score >= 60 的高分票, 不再有 -6% 票
- 飞书发"今日选股" 多次 → 数据一致 (走 picks 表, 不再 fallback)


### hotfix-1: 中文日期解析 + freeform 救场
**问题**: 飞书发"6月16日选股" / "今日选股" 都回"没识别到您的意图"。

- **A. 中文日期解析** (`engine/skills.py:_parse_relative_date`): 加正则 `(\d{1,2})月(\d{1,2})[日号]`, 支持 "6月16日" / "6月16号" / "06月16日"。未来日期自动减一年 (避免 6-16 现在报"还没到")
- **B. freeform 救场** (`llm/tool_use.py:dispatch`): LLM 不选 tool 时, 先尝试 `keyword_fallback(text)` 救场, 命中就走 skill (新 path=`llm_tool_rescued`); 救不中才走老 freeform 兜底
  - 治 "今日选股" LLM 偶尔不调 get_picks 工具 → 走 freeform 空响应 → 兜底成"没识别意图"
- **C. 新 3 测试**: `test_parse_chinese_date` + `test_keyword_fallback_rescues_today_picks` + `test_no_keyword_match_falls_through_to_freeform`
- **D. picks 表清理**: 清掉 test 残留 (整数 75.0/62.0/80.0), 实际 Mac 端会重写

### hotfix-2: 5 只"很垃圾"票的根因
**现象**: 飞书推的 5 只 (301376/301215/002195/301138/002488) chg_pct 全是 -0.7% ~ +2.3%, 全部是跌/微涨。

**根因**: v3 plan_a/b 阈值 (chg 3-4% + 换手 8-10%) 全市场满足的票 < 10 只, 永远走 plan_c 兜底。plan_c 之前无下限, 任何 top200 票都进。

**修复**: v3.1 plan_c 兜底收紧 — 只保留 score >= 60 的高分票。这 5 只票在 v3.1 下 score 全部 < 60, 全部被过滤 → 推"空仓"卡片。

**Mac 端验证**: `bash deploy/install_launchd.sh restart` 后, 14:00 stage_pick 跑出来 plan_c = [] (无 ≥60 分票), 推 🔴 空仓日 卡片, 用户能立刻看到"今天没票"而不是"垃圾票 5 只"。

---

## [v12.A.4] - 2026-06-16

借鉴 `docs/借鉴分析-felix-quant.md` #1 (5 档市场状态 + 决策模式) + #3 (结构化复盘)。**不学** #2 情绪数据 / #4 风险规则 / #5 数据快照 (推 v12.A.5+).

### 借鉴 #1: Market Regime 5 档 + Decision Mode 决策树
- **新文件** `engine/market_regime.py` (~148 行): `classify_regime(env)` 5 档 (Panic/RiskOff/Choppy/Recovery/RiskOn), `regime_to_mode` 候选数/高风险降档
- **新文件** `engine/decision_engine.py` (~141 行): `build_daily_decision` 4 维仓位上限 (base/market/strategy_quality/decision_mode) 取 min, 返 4 档 mode (WAIT/DEFENSIVE_OBSERVE/WATCH/PROBE)
- **改** `engine/skills.py:_run_get_market_env`: realtime 分支加 `regime` / `regime_zh` / `regime_reasons` 字段
- **新 skill** `get_daily_decision`: 飞书问"今天能买吗" 触发, 返 5 档 regime + 4 档 mode + 仓位区间 + 允许/禁止动作 + 切换条件
- **改** `agent/stages.py:stage_pick`: 跑选股前先调 decision engine, Panic/WAIT 模式直接 return (不跑选股)
- **测试** `tests/test_v12_a_4_regime.py` (14 个全绿): 5 档分类 + 降档 + 4 维仓位 + skill 集成

### 借鉴 #3: Reviews 表 + Skill + REVIEWED Fact
- **新文件** `engine/reviews.py` (~240 行): `add_review` / `query_reviews` / `get_review` / `update_review` / `tag_count` / `parse_natural_review`
- **改** `engine/paper_trader.py`: 新表 `reviews` (date/stock_code/stock_name/signal_id/action_taken/reason/result/summary/tags) + 3 索引
- **改** `engine/temporal_facts.py`: PREDICATE_VOCAB 加 `REVIEWED`
- **新 skill** `add_review`: 飞书 "加复盘: 002063 止盈 2.5% 早盘冲高" → 自然语言解析 → 落库 + 写 REVIEWED fact
- **新 skill** `query_reviews`: 按 date/code/tag 查
- **新 CLI** `agent review list` (轻量)
- **测试** `tests/test_v12_a_4_reviews.py` (12 个全绿): CRUD + 自然语言解析 + skill 集成 + REVIEWED fact

### 改动文件
- **新增 4 个**: `engine/market_regime.py` / `engine/decision_engine.py` / `engine/reviews.py` + 2 测试套件
- **改 5 个**: `engine/skills.py` (realtime 加 regime + 3 新 skill) / `engine/paper_trader.py` (新表) / `engine/temporal_facts.py` (REVIEWED predicate) / `agent/stages.py` (stage_pick 屏障) / `agent/cli.py` (review subcommand)

### 端到端验证
- Choppy (env=50) 模式 → stage_pick 正常跑选股 (mode=WATCH, 0/30% 仓位)
- Panic (env=18) 模式 → stage_pick 跳过, return `{"skipped": "regime_wait", "regime": "Panic", ...}`
- 飞书 "今天能买吗" → 返决策卡片 (regime + mode + 仓位 + 理由)
- 飞书 "加复盘: 002063 止盈 2.5% 早盘冲高" → 落 reviews 表 #N + 写 REVIEWED fact
- 飞书 "今日复盘" → 返今日 reviews 列表

### Test
- **v12.A.4 新增 26 测试全绿**: regime 14 + reviews 12
- **老 234 测试 287/289 通过** (2 pre-existing 数据累积, 跟 v12.A.4 无关)

### 不做的事 (推 v12.A.5+)
- ❌ 情绪数据 (涨停/跌停/炸板) — 需 Tushare 付费
- ❌ 风险规则表 (risk_rules) — 跟决策引擎一起做更顺
- ❌ 数据快照 (dashboard_snapshots) — 大改, 推 v13
- ❌ 财务因子 (PE/PB) — 跟短线选股关系弱

---

## [v12.A.3] - 2026-06-13

借鉴 `docs/借鉴分析-trading-review-wiki.md` 4 个可借鉴点, 一包发 v12.A.3。**不学** #5 桌面 app / 自动更新。

### 借鉴 #1: 证据编号 (折中方案)
**目标**: LLM 答案"逐条引用"而不是自由发挥, 用户能看清每个判断来自哪。
- **新文件** `engine/evidence.py` (~130 行): `make_evidence_id` / `format_evidence_for_prompt` / `render_evidence_section` / 4 个 builder (RAG/SQL/live/facts)
  - 编号约定: R (RAG) / S (SQL) / L (Live) / F (Facts) / M (Memory) / K (Kline)
- **`engine/skills.py`**: 5 个 `_run_*` 都加 `evidence: [{id, kind, title, snippet}]` 字段
  - `_run_explain_pick`: RAG + live 合并
  - `_run_search_knowledge`: RAG
  - `_run_get_picks` / `_run_get_positions`: SQL (`first = dict(rows[0])` 防 sqlite3.Row 无 .get())
  - `_run_get_market_env`: 4 分支 (picks/realtime/no_history/failed) 各带 evidence
  - `_run_get_stock_quote`: K 线源
- **`engine/cards.py`**: `card_picks` / `card_positions` / `card_explain` 加 evidence 参数 → 卡片底部插入 "📚 证据" 段
- **修 `_run_get_market_env` BUG**: v12.A.3 evidence 改造时误删了 5 分支的 `if target_date < today:` 条件, 导致 past + picks 空 会走到 6 分支 realtime。已补回。
- **3 个 j2 模板** (`advisor.j2` / `with_knowledge.j2` / `auto_period_explain.j2`) 末尾加引用编号要求
- **测试** `tests/test_v12_a_3_evidence.py` (11 个): 工具函数 3 + skill 字段 5 + 卡片底部 2 + j2 smoke 1

### 借鉴 #2: memory 4 类分组注入
**目标**: `build_memory_context` 按 type 分组, 每类 limit 2, 总 ≤ 800 字。
- **`assistant/memory.py:build_memory_context`** 重构成 4 类分组渲染: 偏好 → 事实 → 决策 → 卫语句 → 其它
  - 顺序固定, importance DESC + created_at DESC 兜底
  - 渲染格式:
    ```
    用户记忆 (按类型分组):
    [偏好]
    - 不喜欢银行股
    [事实]
    - 关注 600519
    ```
- **`detect_memory_signal`** 加 guardrail 识别: `"记住 [规则] XXX"` / `"记住 [卫语句] XXX"` → type=guardrail
- **测试** `tests/test_v12_a_3_memory.py` (5 个): 4 类分组各 1 + 空 memory 1 + guardrail 1

### 借鉴 #3: temporal facts 时序账本
**目标**: 选股/复盘/调参走 append-only JSONL, sha1 幂等, active/invalidated 状态机。
- **新文件** `engine/temporal_facts.py` (~210 行): `record` / `supersede` / `invalidate` / `query_active` / `query_all` / `get_fact`
  - 存储: `data/facts/stock_events.jsonl` (gitignore)
  - fact_id = sha1(subject|predicate|object)[:12] 幂等
  - PREDICATE_VOCAB v1: SELECTED / VALIDATED / SUPERSEDED / INVALIDATED / TUNED
- **`agent/stages.py`**: 3 个写点
  - `stage_pick`: 给每只候选写 SELECTED (subject=code, object=plan:?)
  - `stage_post_market`: 给已开仓的写 VALIDATED
  - `stage_weekly_review`: 写 TUNED, 并标本周 SELECTED 为 invalidated
- **新 skill** `get_stock_lifecycle` (engine/skills.py): 查某只票的 active facts 时间线, `include_invalidated=true` 看完整历史
- **关键词降级**: 时序/生命周期/lifecycle → `get_stock_lifecycle`
- **测试** `tests/test_v12_a_3_temporal.py` (7 个) + `tests/test_v12_a_3_stages.py` (3 个)

### 借鉴 #4: tuner apply --write 屏障
**目标**: 调参默认 dry-run, --write 才真改 config.yaml + params_history。
- **`engine/tuner.py:run_weekly_tune(dry_run=True)`**: 默认只算 proposals 不写库, 返 `preview` 列表让 admin 卡片展示
  - dry_run=True → preview 不写; dry_run=False → applied 真写
- **`agent/cli.py`**: 新增 `agent weekly-review` subcommand
  - `--write` flag (默认不看, 干跑预览)
  - `--json` 输出结构化 JSON (admin 卡片用)
  - 人类格式输出 preview/applied/pending 数量
- **测试** `tests/test_v12_a_3_tuner.py` (3 个): dry-run 不写 / --write 真调 / metrics 一致

### 改动文件
- **新增**: `engine/evidence.py` / `engine/temporal_facts.py` / 5 个 test 套件
- **改**: `engine/skills.py` (5 个 _run + 5 个 _render + new skill + keyword) / `engine/cards.py` (3 card 接受 evidence) / `assistant/memory.py` (4 类分组 + guardrail) / `agent/stages.py` (3 个写点) / `agent/cli.py` (weekly-review subcommand) / `engine/tuner.py` (dry_run 参数) / 3 个 j2 模板
- **改**: `.gitignore` (加 `data/facts/*.jsonl`)

### Migration
- 老库 `quant.db` 不需改 (memories.type 已有 5 候选, 4 类分组渲染向后兼容)
- `data/facts/` 目录运行时创建 (gitignore 加了 `data/facts/*.jsonl`)
- **tuner 行为变更**: 默认 dry-run, 真跑要 `agent weekly-review --write` (Mac launchd 不动, 不会自动改)
- **stage_runs 表**: 装饰器 `_with_stage_run_logging` 自动记账, 不动

### Test
- **v12.A.3 新增 29 测试全绿**: evidence 11 / memory 5 / temporal 7 / stages 3 / tuner 3
- **v12.A.2 旧 27 测试** 26/27 (1 pre-existing: stage_runs 表累积导致 picks-empty 测试与现实状态不一致, 数据问题非代码 regression)
- **老 test_tuner.py 1 pre-existing fail** (plan 已标注)
- **目标 264 测试 / 14 套件, 实际 263/265 通过**

### 假设 / 默认
- **temporal_facts 存 JSONL 不入 SQLite**: 跟 trading-review-wiki 一致, append-only + sha1 幂等, 简单且够用
- **fact predicate 用 v1 默认词表**: SELECTED / VALIDATED / SUPERSEDED / INVALIDATED / TUNED
- **guardrail 复用 memory.explicit**: "记住 [规则] XXX" 写 type=guardrail, 不引入新表
- **stage 默认真写不阻塞**: 跟用户确认, launchd 不动
- **tuner dry-run 默认开启**: 跟用户确认, 用户手动管
- **不引入 LangChain / LlamaIndex / 外部依赖**: JSONL / sha1 / sqlite 全部 stdlib + 已有
- **不学 #5 桌面 app / 自动更新**: 用户已确认

---

## [v12.A.2] - 2026-06-13

### Fixed (体验优化包: 5 改 1 包 + 1 顺手)
- **🐛 BUG 修: `/stage` `/health` 命令已接通** (feishu/admin_cmd.py)
  - 之前 v12.9.2 写好 `_stage_payload` `_health_payload` 但漏接 `handle()` 分发, 返 '未知命令'
  - 改: handle() 末尾加 2 行 `if cmd == "/stage": return _stage_payload()` / `if cmd == "/health": ...`
  - 测试: test_admin_stage_cmd + test_admin_health_cmd + test_admin_help_lists_all_7

- **⭐ 持仓卡片丰富** (engine/cards.py)
  - 新增 `_bar(pct)`: 盈亏柱 (🟩/🟥 + ▇ unicode 块)
  - `card_positions` 增强: 每只票加 `持仓天数` (从 pick_date 算到 today)
  - 末尾加 `板块分布` 汇总 (按 sector 统计数量 + 平均盈亏)
  - 顺手: feishu/card_templates.py 改名迁移 → engine/cards.py (名实相符, 工厂跟 engine 同侧)
  - skills.py 3 个 import 改 `from .cards import ...`
  - 测试: test_cards_bar_positive/negative/zero + test_cards_position_holding_days + test_cards_position_with_sector_and_days

- **⭐ picks 表空修复 (open_auction push 异常吞掉)** (agent/stages.py)
  - 根因: `pusher.push_anomaly` 失败 → open_auction stage 失败 → pick 依赖检查失败 → pick 永远没跑 → picks 表空
  - 改: `stage_open_auction` 内部把 `push_anomaly` 包 try/except, 失败仅 log.warning
  - 不动 RETRYABLE_STAGES (保留 v12.9.2 分类: open_auction 仍是非关键 stage)
  - 测试: test_open_auction_push_fail_does_not_raise + test_open_auction_not_in_retryable_stages

- **🧹 dedup cache 跨进程持久** (feishu/listener.py)
  - 之前: `_seen_msgs` in-memory, agent 重启全丢
  - 现在: 每 30 次 `_mark_seen` 写一次 `data/dedup_seen.json`; 启动时 `_load_seen_from_disk()` 恢复
  - TTL 600s 过期自动清 (避免文件无限增长)
  - 测试: test_dedup_load_from_disk_empty / with_data + test_dedup_mark_seen_writes_to_disk_every_30

- **🧹 清理历史遗留空文件** (.gitignore)
  - `data/paper_trader.db` (0 bytes, 22:43 创建没人写) 实际无引用 (DB_PATH 指 `stock_trading_agent/data/quant.db`)
  - 加 `data/paper_trader.db` 到 .gitignore
  - 注: sandbox 跑 `git rm --cached` 需要 Mac 端跑, 同步见 Migration

### 架构清理 (this PR)
- 单一卡片工厂路径: feishu/card_templates.py (跨 boundary) → engine/cards.py (名实相符)
- dedup 单一文件位置: data/dedup_stats.json (counter) + data/dedup_seen.json (cache) 都在 data/
- skills.py import 3 处收敛到 `from .cards import`

### Migration
- 重启 supervisor 生效: `bash deploy/install_launchd.sh restart`
- Mac 端跑 (sandbox git index lock):
  ```
  git rm --cached data/paper_trader.db
  git add -A && git commit -m "v12.A.2 体验优化包"
  ```

### Tests
- `tests/test_v12_a_2.py` 27 个 (admin BUG 5 + cards 增强 7 + stages 2 + dedup 3 + gitignore 1 + 回归 3 + **date shadow regression 4** + **picks empty UX 2**)
- 跑回归: 12 个套件 (v12.5.1 dedup + v12.8 supervisor/dedup/freeform/persona + v12.9.1/2/3 + v12.9_rag + v12.A market_env/stock_quote + v12.A.2) 全过
- v12.9.2 套件 7/7 不退化 (验证 RETRYABLE_STAGES 没动)

### Hotfix-2 (v12.A.2 收尾 — picks empty UX)
- **问题**: 用户问 '今日选股' / '禾望电气为什么没入选' → picks 表 0 行 → 返 '(无选股记录)', 用户不知道为啥没
- **根因**:
  1. picks 表 0 行的真因是 stage_pick 还没到 cron 时间 (14:00), catch_up 不会补跑未来时间
  2. 但 UX 上不告诉用户原因, 只说"无选股记录" → 答非所问
- **修法** (engine/skills.py):
  - `_run_get_picks`: picks 空时查 `stage_runs` 表, 返 `empty_reason` 字段 (3 种)
    - stage 还没跑 → "今天 ( YYYY-MM-DD ) pick stage 还没跑 (计划 14:00 跑)"
    - stage 跑了但失败 → "今天 pick stage 跑失败 ( 时间 ), 详见 /stage"
    - stage 跑了但没出候选 → "今天 pick 跑过但没出候选 (大盘/筛选太严)"
  - `_render_picks_card`: picks 空时用 `empty_reason` 拼 "📭 {reason}" 卡片
- **回归测试** (test_v12_a_2.py 新加 2 个):
  - test_get_picks_empty_shows_stage_reason
  - test_get_picks_with_items_unchanged
- **Mac 端验证**: 必须先 `bash deploy/install_launchd.sh restart` 让 picks UX 改的代码生效, 旧 agent 进程 (pid 4519) 还跑着 v12.A.2 改前代码

### Hotfix (v12.A.2 收尾 — date shadow BUG)
- **根因**: `engine/data_fetcher.py:fetch_stock_kline` 形参 `date: str` 跟 module-level `from datetime import date` 同名 → Python 解释器优先解析为形参 str, 导致 `date.today()` 实际是 `str.today()` → `AttributeError: 'str' object has no attribute 'today'`
- **影响**: get_stock_quote / explain_pick 走 K 线分支时必炸 (mac 端真实跑东方财富才暴露, sandbox mock 测不出)
- **修法**:
  - `from datetime import date, timedelta` → `from datetime import date as _date, timedelta`
  - fetch_stock_kline 内 `date.today()` / `date.fromisoformat()` → `_date.today()` / `_date.fromisoformat()`
  - 其他 4 处 `date.today()` (is_trading_day / get_latest_trading_day / get_previous_trading_day / get_market_sector_change_rising) 同步改 `_date.today()` (虽然没 shadow, 统一风格防踩)
- **回归测试** (test_v12_a_2.py 新加 4 个):
  - test_fetch_stock_kline_no_date_shadow: date='today' 不抛
  - test_fetch_stock_kline_date_str_shadow: date='2026-06-12' 不抛
  - test_data_fetcher_date_alias_in_globals: _date is datetime.date
  - test_is_trading_day_uses_date_alias: 默认参不抛

## [v12.A.1] - 2026-06-13

### Fixed (治 "禾望电气周五行情" 类 + 架构合并)
- **`fetch_stock_kline(code, date)` 新增** (engine/data_fetcher.py)
  - 调东方财富 push2his.eastmoney.com/api/qt/stock/kline/get
  - 返 11 字段: date/open/close/high/low/volume/amount_yi/amplitude/chg_pct/chg_amt/turnover
  - 支持 date=YYYY-MM-DD 回看历史日 (push2 跟 push2delay 是兄弟域名, 同 family)
- **`get_stock_quote` skill 新增** (engine/skills.py)
  - SKILL_REGISTRY 第 9 个 skill, uses_llm=False, schema 含 code (required) + date (optional)
  - 6 个分支: 未来/格式错/代码错/空数据/K线拉到/没date走实时
- **`_run_explain_pick` picks 找不到分支改 K 线/实时** (engine/skills.py)
  - 优先用 args.date 拉 K 线, 没 date 或拉不到 → 走 fetch_realtime_quote
  - 末尾 source 区分 'kline' / 'realtime' (前端能看到数据源)
- **`keyword_fallback` 改 v12.A.1 优先** (engine/skills.py)
  - 1) 6 位代码 regex → get_stock_quote (有'行情') 或 explain_pick
  - 2) 持仓关键词提前到选股前面 (治 "今日持仓" 截胡到 get_picks)
  - 3) 新增 "个股/股价/股票/这个股" 触发 explain_pick
- **`dispatch` 入口 1 个 date 解析点** (llm/tool_use.py)
  - 旧: 3 个路径各调 1 次 _parse_relative_date
  - 新: 入口解析 1 次, 3 路径共享; freeform 路径把 date 拼到 messages (治 LLM 自由回答 hallucinate "周五" → 6-12)
- **persona boundary_rules +3 条** (config/persona.yaml)
  - "用户提到某只具体票 (6 位代码/股票名) + 任何时间 → 必调 get_stock_quote/explain_pick, 不要当市场行情"
  - "代词 (它/这只/他) → 翻 sessions 找最近 code, 找不到就主动追问"
  - "日期 (今天/周五/11-07) 是 query 的 date 参数, 不再凭印象编"
- **修 `_render_explain_card` think 剥除测试** (tests/test_v12.py)
  - v12.9.1 改 interactive card 后老测试 r["content"]["text"] 找不到
  - 改测试适配 r["content"]["elements[0].text.content"]

### 架构合并 (this PR)
- date 解析 1 个入口 (替代 3 处重复调用)
- 持仓关键词前置 (治 "今日持仓" 截胡老 bug, v12.9.1 已知遗留)
- 6 位代码 regex 直路由 (替代 LLM hallucinate "持仓" 误判)
- 真实存在的接口 (push2his.kline) 现在用上 (之前 Hermes 时代能用)

### Migration
- 无 schema 改动
- 重启 supervisor 生效: `bash deploy/install_launchd.sh restart`

### Tests
- `tests/test_v12_a_1_stock_quote.py` 11 个 (fetch_stock_kline 3 + _run_get_stock_quote 2 + _render 2 + keyword 2 + schema 1 + explain_pick 1)
- 修 `test_v12.py: test_skill_explain_strips_think` 适配 interactive card 结构
- 全套件回归通过 (23 个套件, 285/286, 仅 tuner 1 个 pre-existing fail)
## [v12.A] - 2026-06-13

### Fixed (治 LLM "截止最新运行日" hallucinate)
- **`_run_get_market_env` 接 `date` 参数** (engine/skills.py)
  - 之前: 不接受日期, picks 表空 → 实时拉, picks 表非空 → 取最近一行
  - 现在: 接受 `date=YYYY-MM-DD` / `date="today"`, 6 个分支:
    - 未来日 → "未开盘 (未来日)" 友好提示
    - 周末/节假日 → "X周X不开盘 (周末/节假日)"
    - 过去交易日 + picks 无数据 → "历史数据暂无 (picks 表当日为空)"
    - 过去交易日 + picks 有 → 用 picks
    - 今天 + picks 无 → 实时拉 (v12.5.1 老逻辑)
    - 格式错 → "日期格式错 (要 YYYY-MM-DD)"
  - 治用户问"周五行情" → 模型不再瞎编"截止最新运行日 11-04"
- **`_parse_relative_date` helper** (engine/skills.py)
  - 解析 "今天/今天/昨天/明天" + "周X" (默认推下一个, 显式下X/上X 推对应周) + "YYYY-MM-DD" + "MM-DD" (默认本年)
  - dispatch keyword_fallback 路径透传给 `call_skill(args={"date": "..."})`
- **keyword_fallback 触发词** 加 "行情" / "市场行情" / "盘面"
- **`get_market_env` tool schema description** 教 LLM: 支持 date 参数, 未来/周末/过去无数据 返明确文案, 不瞎编
- **persona.yaml boundary_rules** +2 条: 涉及日期+行情必须调 get_market_env; 不要编造"数据截止日"

### Migration
- 无 schema 改动 (老 .db 兼容)
- 重启 supervisor 生效: `agent stop && agent start`

### Tests
- `tests/test_v12_a_market_env.py` 17 个
  - 7 个 _parse_relative_date (今天/昨天/明天/周X推未来/下周一/上周五/3段日期/2段日期/无日期)
  - 6 个 _run_get_market_env (未来日/过去无picks/过去有picks/今天非交易日/格式错/无参实时拉)
  - 2 个 keyword_fallback (新触发词 + 不冲突)
  - 1 个 tool schema 校验
  - 1 个 dispatch keyword_fallback 集成 (验 date 透传)

## [v12.9.3] - 2026-06-13

### Fixed (picks 找到时也拉实时)
- **`_run_explain_pick` picks 找到分支补拉实时行情** (engine/skills.py)
  - 之前: picks 表找到 → 只走 RAG + picks 历史数据 → 用户问"实时"时 bot 用 RAG 知识答, 体感"实时拿不到"
  - 现在: picks 找到时也调 `fetch_realtime_quote(code)` (容错, 失败不影响)
  - 实时数据叠加进 LLM prompt (作为 fact: 价/涨跌幅/换手/市值)
  - 末尾追加 `[实时 N 元 · 今日 N% · 换手 N%]` 一行, 让用户明确看到 bot 用了实时
- **fallback_empty 文案**: 保留, 不变

### Migration
- 无 (单函数内部加 ~15 行)
- 重启 supervisor 生效: `agent stop && agent start`

### Tests
- `tests/test_v12_9_3_realtime.py` 2 个
- 273/274 通过 (tuner 1 个 pre-existing fail 不动)

## [v12.9.2] - 2026-06-13

### Added (运维包: 仪表盘 + 可观测 + retry)
- **#15 admin `/stage` 命令** (feishu/admin_cmd.py)
  - 读 stage_runs 今日全表, 返时间线 (HH:MM:SS 排序)
  - 失败 stage 在末尾提示 + 拉 llm_logs 最近 5 条 fail
- **#18 admin `/health` 命令** (feishu/admin_cmd.py)
  - 读 llm_logs 今日: 总调用 / 成功率 / 平均延迟 / Token 用量
  - 按 call_site 拆: tool_use_router / answer_question / pick_intro 等
  - 0 调用返 "今日暂无 LLM 调用记录"
- **#19 关键 stage 失败 retry 1 次** (agent/stages.py)
  - `RETRYABLE_STAGES = {pre_market, pick, evening, weekly_review}`
  - 失败时 sleep 30s 重试, 仍失败才 mark_stage_run(ok=False) + 返回 retried=True
  - open_auction / intraday_monitor / post_market 不重试 (轻量 stage, 失败就跳过)
  - 治偶发网络挂导致整天 0 stage 的老问题

### Migration
- 无 (纯加命令 + 改装饰器)
- 重启 supervisor 生效: `agent stop && agent start`

### Tests
- `tests/test_v12_9_2_ops.py` 7 个 (覆盖 3 个改动)
- 271/272 通过 (tuner 1 个 pre-existing fail 不动)

## [v12.9.1] - 2026-06-13

### Added (体验优化包: 治"答非所问" + 视觉升级 + 管理命令)
- **#1 `explain_pick` 实时行情兜底** (engine/skills.py + engine/data_fetcher.py)
  - 新建 `fetch_realtime_quote(code)` 调东方财富 push2delay 单票接口
  - picks 找不到 → 拉实时数据 → LLM 用事实给一句话解释 + 末尾 `[数据源: 东方财富实时]`
  - 实时也拉不到 → 友好引导 "你可以试试: 1. 说股票名 2. 我帮你从选股记录/知识库找"
- **#2 `stage_post_market` 真实实现** (agent/stages.py)
  - 之前: 永远 `push_post_market(0, "占位", [])` 假数据
  - 现在: 读今日 picks + paper_positions 算 filled_count, 标 is_filled, push 真数据
- **#3 A 股节假日常量** (engine/data_fetcher.py)
  - 2026 一年 7 个法定节假日 (元旦/春节/清明/劳动/端午/中秋/国庆) 写死常量
  - `is_trading_day` 同时检查 weekday < 5 且不在节假日列表
  - 调休补班 (周末调成工作日) 暂不处理, 留 v12.10 接 tushare
- **#4 飞书 Interactive Card 模板** (feishu/card_templates.py 新建)
  - `card_picks(items, date)` / `card_positions(items)` / `card_explain(code, name, explanation, sources)`
  - 3 个 `_render_*_card` (picks/positions/explain) 改用 `msg_type: "interactive"`
  - `_send_reply` 加 `msg_type` 参数支持 interactive
  - `_send_card` 透传 interactive card dict
- **#5 Admin 斜杠命令** (feishu/admin_cmd.py 新建 + feishu/listener.py 入口)
  - `/help` `/picks` `/positions` `/env` `/status` `/reset`
  - listener on_message 入口早返回, 不进 LLM dispatch (省 token + 响应快)
  - 权限: `sender_id in config.feishu.admin_user_ids`, 配置空时宽松

### Migration
- 重启 supervisor 生效: `agent stop && agent start`
- 老 LLM 路径完全兼容 (admin 命令是早返回旁路, 不改 dispatch)

### Tests
- `tests/test_v12_9_1_experience.py` 15 个 (覆盖 5 个改动)
- `tests/test_v11.py` 2 处 msg_type 断言更新 (picks/explain card 改 interactive)
- 271/272 通过 (tuner 1 个 pre-existing fail 不动)

## [v12.9] - 2026-06-12

### Added (RAG 解释更聪明)
- **`_run_explain_pick` 用 RAG-friendly query** (engine/skills.py)
  - 之前: `f"为什么选 {code} {name} (评分 X, 板块 Y)?"` — BM25 在缠论108课/好运2008 里召回率 ~0
  - 现在: 拼 4 类关键词 (股票名+板块 / 缠论术语 / 好运2008 术语 / 评分维度)
  - 例: `"贵州茅台 白酒 缠中说禅 选股 买点 趋势 量价齐升 龙头 强信号"`
  - 末尾自动追加 `[来源] 缠中说禅108课 第17课 / 好运2008: 龙头战法` 标注
- **`answer_question` 加 `preset_results` 参数** (llm/reasoner.py)
  - 允许外部预检索, 避免重复 BM25 + 让 explain_pick 拿到来源做标注
  - 不传 → 走内部 `retrieve(question, k=k)` (兼容)
- **`keyword_fallback` 加知识库关键词** (engine/skills.py)
  - "缠论" / "108课" / "好运2008" / "苏三" / "知识库" 等 → 直接调 `search_knowledge`
  - 优先级最高 (最具体优先)

### Migration
- 无 (新增 query 构造 + preset_results 参数, 老调用方仍兼容)
- 重启 supervisor 生效: `agent stop && agent start`

### Tests
- `tests/test_v12_9_rag.py` 6 个:
  - _build_explain_query 高分拼"强信号"
  - _build_explain_query 中分拼"中等"
  - _run_explain_pick 末尾追加 [来源] + preset_results 复用
  - answer_question preset_results 跳过内部 retrieve
  - keyword_fallback 缠论/108课/好运2008/苏三 命中 search_knowledge
  - keyword_fallback 今日选股/持仓/大盘 仍命中对应 skill (兼容)
- 256/257 通过 (tuner 1 个 pre-existing fail 不动)

## [v12.8.1] - 2026-06-12

### Fixed (修 v12.5.2 自重启设计 + stage 失败记账)
- **supervisor listener 正常 return → sleep 5s 重连** (不再 os.execv):
  - v12.5.2 设计: listener 跑完一次就整进程 execv 自重启, 1h 限 10 次
  - 问题: 飞书 ws 16min 一次 keepalive ping 超时是 SDK 正常生命周期, 每次重启导致 cron job 状态丢失 + 自重启计数器耗尽 → 6/11 23:23 之后 24h 没 stage 跑
  - 修法: 正常 return → sleep 5s → 再起 listener.run(); 只有**真崩了**才走 execv 1h 限 10 次
  - v12.5.1 已修 5xx 重投, 不会跟 sleep 重连叠加
- **stage 失败强制记账 stage_runs**:
  - 7 个 stage 函数 (pre_market / open_auction / pick / post_market / evening / intraday_monitor / weekly_review) 加 `_with_stage_run_logging` 装饰器
  - 成功 → `mark_stage_run(name, ok=True)`; 失败 → `mark_stage_run(name, ok=False)` + log.exception
  - 不再静默 except, "今天为啥没选股"能直接查 stage_runs 表
- **`_listener_lifecycle` 顶层 import time** (原本函数内 import, 不可 patch)

### Migration
- 无 (纯 supervisor / stage 行为修复)
- 重启 supervisor 生效: `agent stop && agent start`

### Tests
- `tests/test_v12_8_1_supervisor.py` 5 个:
  - 正常 return 走 sleep 5s 重连 (不 execv)
  - 异常仍走 execv (防 bug 死循环)
  - stage 失败 → mark_stage_run(ok=False)
  - stage 成功 → mark_stage_run(ok=True)
  - 装饰器不破坏原 stage 返回值
- 250/251 通过 (tuner 1 个 pre-existing fail 不动)

## [v12.8] - 2026-06-11

### Added (治"回复很憨 / 漏回"三件套)
- **A. persona 加厚 5 段**: `config/persona.yaml` 加 few_shots / glossary / boundary_rules / style_examples / fallback_phrases
  - `assistant/persona.py:load_persona()` 拼装顺序固定: identity → tone_rules → context_preamble → few_shots → glossary → boundary_rules → style_examples (fallback_phrases 不入 prompt, 给 dispatch 兜底用)
  - `pick_fallback_phrase(rng=None)` 随机选 1 句, 测试时可注入 seed
- **B. 调参 + freeform 兜底升级**: 
  - `config.yaml:llm` 加 `temperature: 0.7` / `max_tokens: 800` (覆盖 client.py 默认 0.4/600)
  - `llm/tool_use.py:dispatch()` 调 chat_with_tools 时改用 config 参数 (不再覆盖回 400)
  - freeform 空响应分支改走 `pick_fallback_phrase()`, 60s 内同 chat_id 不重调 LLM
  - 空响应打 `log.warning` (chat_id / text / content 长度)
- **C. 飞书 5xx 重投可观测化**:
  - `feishu/listener.py:dup skip` 升级 `log.warning`
  - 写 `bot_sessions` system note "[dup skip] {text}" (历史可查)
  - 计数器 `data/dedup_stats.json` (今日累计 + 5min 窗口滚动)
  - 新 CLI: `agent dedup stats / reset` (集成到 `agent/cli.py`)

### Migration
- 无 (纯 persona 调参 + 兜底, 不改 SQLite schema 不改 subcommand 协议)
- 重启 supervisor 生效: `agent stop && agent start`

### Tests
- `tests/test_v12_8_persona.py` 6 个
- `tests/test_v12_8_freeform.py` 4 个
- `tests/test_v12_8_dedup.py` 4 个
- 245/246 通过 (tuner 1 个 pre-existing fail 不动)

## [v12.7] - 2026-06-11

### Changed (项目整理)
- README 重写: 182 → 105 行, 删版本表 / 后续候选 / 重复调参说明
- CHANGELOG 压缩: 18 个版本 → 留最近 5 个, 老版本归 `CHANGELOG_ARCHIVE.md`
- `docs/reports/` 归档: 19 个旧周报移入 `archive/reports/`, 主目录只留最近 2 周

### Migration
- 用户无感 (纯文档/文件归档, 无代码 / 配置变更)
- 看老版本变更: `cat CHANGELOG_ARCHIVE.md`
- 看老周报: `ls docs/reports/archive/`

## [v12.6] - 2026-06-11

### Changed (架构整理)
- 800+ 行 `agent.py` 拆成 4 个子模块, 加 shim 保持向后兼容:
  - `agent/stages.py` (372 行) — 6 stage + 2 push + STAGE_REGISTRY + 拓扑排序 + catch-up + run_daemon + 上下文查询
  - `agent/supervisor.py` (256 行) — PID / 启停 / watchdog / v12.5.2 自重启
  - `agent/webhook.py` (88 行) — HTTP 服务 (chat/reset/health)
  - `agent/cli.py` (131 行) — argparse 入口
  - `agent/__init__.py` 重导出公共 API, 老 `from stock_trading_agent.agent import X` 全不破
  - `agent/__main__.py` 让 `python -m stock_trading_agent.agent <subcmd>` 工作
  - `agent.py` 缩成 67 行 shim, 仅 re-export + 转发到 `cli.main()`
- 测试 patch 路径相应改: `patch("ag.X")` → `patch("ag.stages.X")` / `patch("ag.supervisor.X")`
  - 因为 shim 跟子模块是不同的 Python module, patch shim 属性不影响子模块
  - 改动: v7 (5 处 patch) + v11 (1 处) + v12 (4 处) + smoke (1 处) 共 11 处

### Migration
- 用户无感: `agent start` / `agent stop` / `agent daemon` / `agent run-once --stage X` / `agent report` / `agent memory list` 行为完全不变
- 开发者: 新增功能优先放对应子模块
  - 加新 stage → `stages.py` + `STAGE_REGISTRY`
  - 加新 push 卡片 → `stages.py` + `PUSH_REGISTRY`
  - 改启停/watchdog → `supervisor.py`
  - 加 HTTP endpoint → `webhook.py`
  - 加新 subcommand → `cli.py`

### Regression
- 231/232 测试通过 (tuner 1 个 pre-existing 失败, 跟 v12.6 无关, plan 规则不修)
- 零行为改变, 纯架构整理

## [v12.5.2] - 2026-06-11

### Fixed
- watchdog supervisor 退出后不自启的根因 (用户实际体验到的"飞书失联 2h")
  - 根因: v12.5.1 把"listener 正常 return"也当 supervisor 退出, 但用户没 `agent start` 就失联到下次手动重启
  - 修法: supervisor 改用 `os.execv` 整进程自重启, 1h 限 10 次 (`data/.auto_restart_count` 持久化计数), 不依赖 launchctl 外层兜底
  - 抽 `_restart_executor = os.execv` 变量, 测试可注入 mock (避免测试里真 execv 跑出子进程)
- `_send_reply` / `_send_via_app` / `_send_webhook` 3 处吞 `Exception as e` 但不 log
  - 修法: 加 `log.warning("...失败: %s: %s", type(e).__name__, str(e)[:200])`, error 字段也带 type 前缀 + 限 200 字符 (避免飞书 API 拒绝)
- `engine/sessions.py` 顶部 import 缺失 (`import os` / `import logging` 散在文件中间)
  - 修法: 挪到顶部标准位置, 跟 PEP 8 一致

### Changed
- `feishu/bot.py` v1 stub 加 `DeprecationWarning`, v13 删除
- watchdog 接口签名 `_listener_lifecycle(stop_event, max_restarts=, window_s=, backoff_s=)` → `_listener_lifecycle(stop_event)`, 老 kwargs 全删 (旧限流代码是死代码, 永远走不到)

### Added
- `agent._self_exec_restart(reason)` — 整进程自重启
- `agent._check_auto_restart_budget()` / `_record_auto_restart()` — 1h 10 次预算
- 4 个新 watchdog 测试: 正常 return / 异常 / 预算耗尽 / stop_event 接口
  - `tests/test_v12.py` 从 33 涨到 37 个

### Regression
- 231/232 测试通过 (tuner 1 个 pre-existing 失败, 跟 v12.5.2 无关, plan 规则不修)
- v12 36 → 37 (新增 watchdog)
- 其余测试集零退化

## [v12.5.1] - 2026-06-11

### Fixed
- 飞书消息被回 2 次 / 偶发不回复
  - 根因: lark-oapi WebSocket 在 `processor.do(data)` 抛异常时返 5xx, 飞书 30s 内重投同一 message_id; 加上 16min keepalive 超时重连会回放断线期事件。我们 on_message 没有去重, 导致 1 条问 -> 2 次 on_message -> 飞书收到 2 条 (UI 去重所以用户看到 1 条)
  - 修法: `feishu/listener.py` 加模块级 `_seen_msgs: dict[str, float]`, on_message 入口 `_mark_seen(message_id)` 检查, LRU(2000) + TTL 10min 覆盖飞书 5xx 重投 (30s) + 16min 断线回放
- 问"今天行情" / "大盘怎么样" 偶尔返空内容
  - 根因: `get_market_env` 只查 picks 表, 周末/节后无选股数据, 返 `env_score: None` 卡片里就显示 `env_score: ?` 用户体感"没回复"
  - 修法: `engine/skills.py:_run_get_market_env` 三段式 picks 表 (历史) > `data_fetcher.get_market_env` 实时拉 > 失败给友好提示。卡片加 source 标注 (📊 picks / ⚡ realtime / ⚠ failed)
- watchdog 正常 return 反复 restart 叠加飞书重投
  - 根因: `agent.py:_listener_lifecycle` 把"listener.run() 正常 return"也当崩溃 restart, 跟飞书 5xx 重投叠加放大"问 1 回 2"
  - 修法: 正常 return -> 退 watchdog, supervisor 整体退出, 用户 `agent stop && agent start` 重起 (v11 老行为, 但避免了叠加)

### Added
- 测试覆盖: `tests/test_v12_5_1_dedup.py` 11 个 (mark_seen × 6 / market_env × 3 / render × 1 / on_message 集成 × 1)

### Regression
- v2(6) + v3(13) + v4(10) + v5(10) + v7(78) + v11(22) + v12(36) + v12_chanlun(7) + v12_export_url(3) + v12_start_guard(4) + smoke(3) + pusher(9) + tuner(6) + picker(4) + paper_trader(10) = 221 测试全绿, 加 v12.5.1 11 个共 232 测试

## [v12.5] - 2026-06-11

### Added
- `agent start` 防多进程防护
  - 启动前读 `data/agent.pid`, 已存在且进程活着则拒绝启动 (SystemExit 1)
  - stale pid (进程已死) / 损坏 pid 自动清理
  - 修"重复 `agent start` 起 3 个进程导致飞书消息被推 3 遍"的隐患
- 测试覆盖: `tests/test_v12_start_guard.py` 4 个 (pass / alive / stale / corrupt)

## [Unreleased]

### 计划中
- 飞书交互卡片 (post schema, 按钮/折叠块/链接, 替代 markdown 文本)
- 实盘 broker 对接 (雪球/华泰/同花顺条件单)
- 跨市场扩展 (港股/美股)
- Web UI dashboard (替代纯飞书卡片)
- GitHub Actions 自动跑 14 套件测试

本文件归档老版本变更, 最近 5 个版本见 [CHANGELOG.md](CHANGELOG.md)。

