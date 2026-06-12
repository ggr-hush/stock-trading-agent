# Changelog

本项目所有重要变更记录于此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

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

