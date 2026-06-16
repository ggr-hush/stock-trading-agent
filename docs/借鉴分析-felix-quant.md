# 借鉴分析: XFX-939/felix-quant (向复兴 / Quant Research Terminal)

> 调研时间: 2026-06-16
> 调研仓库: https://github.com/XFX-939/felix-quant
> 调研方式: clone --depth 1 到 `docs/raw/felix-quant/`, 通读 22 张表 + 18 个 service + 决策引擎
> 调研目的: 看有没有可借鉴的设计 / 实施 / 数据维度
> 项目定位: **本地个人量化研究与复盘终端** (Next.js + FastAPI + SQLite + AKShare + APScheduler)
> 与本项目关系: 范式相近 (A 股 / Python / SQLite / APScheduler), 偏向不同 (他做"个人决策仪表盘", 我们做"agent 飞书对话接口")

## 核心定位差异

| 维度 | felix-quant | stock-trading-agent (本项目) |
|---|---|---|
| 用户入口 | Web 仪表盘 (Next.js) | 飞书对话 (WebSocket) |
| 决策模式 | 结构化 (regime × decision_mode 决策树) | LLM 自由路由 (tool-use) |
| 策略评估 | 4 个硬条件 + 5 因子加权 → 候选/观察/拒绝 | auto/A/B/C 拍脑袋 + LLM 解释 |
| 风控 | 规则表 (risk_rules) + 行业集中度 + 高风险池 | 单一 max_position_ratio |
| 市场状态 | 5 档 (Panic/RiskOff/Choppy/RiskOn/Recovery) | env_score 0-100 数字 |
| 情绪数据 | 涨停/跌停/炸板/封板率/最高板/3连板/昨涨停溢价 | 0 |
| 复盘 | reviews 表 (date/stock/signal_id/action/reason/result/summary/tags) | weekly review markdown 报告 |
| 数据源 | AKShare (真实 A 股, 全市场 180 天日线) | 东方财富 push2 (实时 + K 线, 单股单日) |
| 任务调度 | 9 阶段 (prewarm/midday/after_close + dashboard 兜底) | 7 阶段 + 2 push |
| 持久化 | 22 张表 (sentiment/industry_heat/snapshots 全有) | 6 张表 (picks/positions/memory/stage 等) |

**结论**: 不是替代关系, 是 **互补 + 大幅可借鉴**。他做"完整决策仪表盘", 我们做"飞书对话 agent"。最大差距在 **数据维度 + 结构化决策 + 风控精细度**。

---

## 5 大可借鉴点 (按价值/成本排序)

### ⭐⭐⭐⭐⭐ 1. 市场状态 5 档分类 + 决策模式决策树

**felix 做法** (`backend/app/services/classic_quant.py:market_regime_model` + `decision_engine.py:build_daily_decision`):

- **5 档市场状态** (基于量化阈值):
  - `Panic` — 20 日跌 < -6% + 回撤 < -8% + 跌停 > 50
  - `RiskOff` — 20 日跌 < -3% + 上涨家数 < 45% + 跌停 > 20
  - `Recovery` — 20 日涨 + 但 60 日跌 + 上涨 ≥ 50% + 跌停 ≤ 20
  - `RiskOn` — 20 日涨 > 3% + 上涨家数 > 55% + 涨停 > 50 + 跌停 < 10
  - `Choppy` — 其他震荡
- **5 档决策模式** (regime → mode 映射):
  - `WAIT` (Panic): 仓位 0/0, 禁止短线追涨
  - `DEFENSIVE_OBSERVE` (RiskOff): 仓位 0/0.2, 允许低波防御
  - `WATCH` (Choppy/Recovery): 仓位 0/0.3
  - `PROBE` (RiskOn+条件): 仓位 0.2/0.5
- **4 维仓位上限** (取 min):
  - base_risk_limit / market_limit / strategy_quality_limit / decision_mode_limit
  - final = min(4 维) + 各种 reason 解释
- **每档都给**: `positionDecision` / `allowedActions` / `forbiddenActions` / `keyReasons` / `switchConditions`

**本项目现状**:
- 单一 `env_score` 0-100 数字 + 简单分级 (偏多/中性/偏空)
- 选股阶段不参考市场状态 (auto/A/B/C 三选一硬拍)
- 没有"模式"概念 (仓位/动作/禁止 都没结构化)

**借鉴方案 v12.A.4**:
- 新建 `engine/market_regime.py` 实现 5 档分类 (基于东方财富已有数据可大致估算, 跌停/涨停数需新加)
- 改 `engine/skills.py` 的 `_run_get_market_env` 改返 `regime` 字段, 而不只是数字
- 新建 `engine/decision_engine.py`: regimme → mode → position (min/max) → allowed/forbidden
- 改 `agent/stages.py:stage_pick` / `stage_open_auction` 调用 decision_engine 决定仓位, 不再硬拍 A/B/C
- 新增 skill `get_daily_decision` (默认飞书问"今天能买吗"触发)
- 改 `engine/cards.py` 加决策卡片 (regime + mode + position + 理由)
- **预计**: 1 个新 module + 1 个新 skill + 2-3 个改动 + ~15 测试

### ⭐⭐⭐⭐ 2. 情绪数据维度 (涨停/跌停/炸板/封板率/最高板/3连板/昨涨停溢价)

**felix 做法**:
- 表 `market_sentiment_daily`: 涨停数/跌停数/炸板数/封板率/最高板数/3连板+/昨涨停溢价/指数趋势分/市场情绪分/市场状态 (退潮/修复/高潮)
- service `limit_up_strategy_service.py` 把这些加权重算 0-100 分 → 4 档 (强情绪/可交易/弱分歧/退潮)
- 表 `industry_heat_daily`: 行业级别热度

**本项目现状**: 0。完全没拉这些数据, 也不存这些表。

**借鉴方案 v12.A.4**:
- 新建 `engine/sentiment_fetcher.py` 从东方财富拉涨停/跌停/炸板统计 (Tushare `limit_list_d` 需 token, 东方财富有免费接口)
- 新建 SQLite 表 `market_sentiment_daily` (仿 felix schema)
- 新增 stage `stage_sentiment_refresh` (15:35 跑, 写 sentiment 表)
- 改 `market_regime` 把 sentiment 状态作为补充输入
- 新增 skill `get_market_sentiment` (飞书问"今天情绪怎么样"触发)
- **预计**: 1 fetcher + 1 张表 + 1 stage + 1 skill + 5 测试
- **风险**: 东方财富炸板数据接口稳定性差, 需 fallback (TODO.md 提到此点)

### ⭐⭐⭐⭐ 3. 结构化复盘 (reviews 表 + action_taken + reason/result/summary/tags)

**felix 做法**:
- 表 `reviews` (date/stock_code/signal_id/action_taken/reason/result/summary/tags)
- signal_id 反向关联回 `signals` 表 (能 join 当时选股理由)
- tags 是 JSON 数组 (["止盈","早盘冲高","题材退潮"])
- action_taken 是 boolean (区分"实操"vs"看戏")

**本项目现状**:
- 复盘 = 每周 `weekly_review` 生成 markdown 报告 (`docs/reports/`)
- 没有按 (date, stock) 的结构化复盘
- v12.A.3 加了 `temporal_facts` JSONL, 但只存"选股/复盘/作废"事件, 不存"复盘内容"

**借鉴方案 v12.A.4**:
- 新建 SQLite 表 `reviews` (仿 felix)
- 新增 skill `add_review` (飞书说"加复盘: 002063 止盈 2.5% 早盘冲高 题材退潮" 自动解析)
- 新增 skill `query_reviews` (按 date/code/tag 查)
- `bot_sessions` 的 type=system 复用 + `temporal_facts` 新增 `REVIEWED` predicate
- **预计**: 1 张表 + 2 skill + 1 索引 + 8 测试
- **价值**: 用户能"积累复盘经验", 后续 LLM 推荐股票时引用历史复盘

### ⭐⭐⭐ 4. 结构化风控 (risk_rules 表 + 行业集中度 + 高风险池 + 多维仓位上限)

**felix 做法**:
- 表 `risk_rules` (name/threshold/enabled/description) — 规则可配
- `risk_overview()`: 单票上限 (0.2) / 总仓位建议 / 行业集中度 / 高风险池 / warnings
- 4 维仓位上限 (base + market + strategy_quality + decision_mode) 取 min
- 多种 warning 触发 (高风险 / 集中度 > 0.35 / 回撤 > 0.18)

**本项目现状**:
- `paper.json` 配的 `max_position_ratio` 单一阈值
- `tuner.py` 调参只动 `v3.score_max` / `strong_band` / `blacklist`
- 没有行业集中度计算
- 没有"市场状态触发的动态仓位"

**借鉴方案 v12.A.4**:
- 新建 `engine/risk.py` 仿 `risk_service`
- 新表 `risk_rules` (4-6 条: 单票上限/总仓位/行业集中度/高风险比例/连板数)
- 改 `_run_get_positions` 算行业集中度, 拼到持仓卡片
- `decision_engine` 集成 risk_rules 算 final_position
- 飞书新增 `/risk` admin 命令
- **预计**: 1 service + 1 表 + 1 admin cmd + 6 测试

### ⭐⭐⭐ 5. 数据快照 (dashboard_snapshots + 增量计算)

**felix 做法**:
- 表 `dashboard_snapshots` 缓存 dashboard 拼装结果
- 表 `market_snapshots_daily` (trade_date/stock_code 行情快照)
- 表 `daily_prices` (全 A 股 180 天日线, UNIQUE(stock_code, date))
- `data_sync_status` / `stock_sync_state` / `failed_sync_records` 跟踪同步状态
- 增量 + 失败补抓队列

**本项目现状**:
- 行情按需拉, 不缓存
- 没有"全市场快照"概念
- `picks` 表只存当天选股, 历史 K 线不存

**借鉴方案 v12.A.4 (低优先级, 可推 v13)**:
- 增量缓存常见股票 K 线 (本地 DB, 减少东方财富调用)
- 涨跌停快照每天存 (跟 sentiment_fetcher 配合)
- 失败补抓队列 (减轻 API 限流压力)
- **预计**: 大改, 1-2 周工作量

---

## 数据维度对照表 (本项目 vs felix)

| 维度 | felix | 本项目 | 差距 |
|---|---|---|---|
| 全市场 K 线 (180 天) | ✅ daily_prices | ❌ 按需拉 | 大 |
| 涨停/跌停/炸板统计 | ✅ market_sentiment_daily | ❌ 无 | 大 |
| 行业热度 | ✅ industry_heat_daily | ❌ 无 | 大 |
| 指数多档 (sh/sz/cyb/kc50/bse50) | ✅ market_snapshot | ❌ sh/sz | 中 |
| 涨跌家数 | ✅ snapshot | ❌ 无 | 中 |
| 题材/概念板块 | ⚠️ TODO (用行业近似) | ❌ 无 | 中 |
| 复盘 reviews | ✅ 11 字段 | ❌ weekly md | 中 |
| 信号回链 | ✅ signal_id | ❌ temporal_facts JSONL | 小 |
| 策略源 (source) | ✅ strategy_source_service | ❌ 无 | 小 |
| 风险规则可配 | ✅ risk_rules | ❌ 硬编码 | 中 |
| 多策略健康评估 | ✅ strategy_health | ❌ 无 | 大 |
| 候选分层 (main/defensive/hotspot) | ✅ 3 层 | ❌ 单层 | 大 |
| 决策模式 (WAIT/WATCH/PROBE) | ✅ 4 档 | ❌ 无 | 大 |

---

## 借鉴路径建议 (3 阶段)

### 阶段 1 — v12.A.4 (1-2 周)
- 借鉴点 #1 (5 档 market regime + decision mode 决策树) — **最优先**
- 借鉴点 #3 (结构化复盘 reviews 表) — 跟 v12.A.3 temporal_facts 衔接

### 阶段 2 — v12.A.5 (2-3 周)
- 借鉴点 #2 (情绪数据) — 需新数据源, 工作量大
- 借鉴点 #4 (结构化风控) — 跟 v12.A.4 决策引擎一起做更顺

### 阶段 3 — v13 (长期)
- 借鉴点 #5 (数据快照 + 增量) — 大改, 需本地 DB 升级 + 增量同步

---

## 不学的部分
- Web 仪表盘 (我们是飞书 bot, 走对话)
- 财务因子 (PE/PB/PS 等, 跟短线选股关系弱, 需 token)
- Tushare (收费, 我们走东方财富免费路线)
- 多策略组合权重 (单策略已够用)
- AI 自动复盘 (TODO 提到, 我们用 LLM 解释复盘就行)
- 桌面 app (用户确认不学)

---

## 调研心得
- felix 是"个人决策仪表盘"范式, 我们是"对话 agent"范式 — 互相补
- 5 档市场状态 + 决策模式 是核心, 比 LLM 自由回答**确定性高、可解释强、风控硬**
- 22 张表里我们最该补的是: `market_sentiment_daily` + `reviews` + `risk_rules`
- 数据源从 AKShare → 我们继续用东方财富 (免费 + 实时)
- "策略评估用结构化条件 (4 硬条件 + 5 因子)" 也可借鉴, 替换我们 v3 拍脑袋
- TODO.md 里提的"全 A 股 / 概念板块 / 分时 / VWAP" 我们**暂不学**, 复杂度高
