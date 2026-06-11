# Changelog Archive (v12.4 ~ v1)

本文件归档老版本变更, 最近 5 个版本 (v12.7 ~ v12.5 + Unreleased) 见 [CHANGELOG.md](CHANGELOG.md)。

---

## [v12.4] - 2026-06-09

### Added
- 缠中说禅 108 课 RAG 接入
  - `engine/chanlun_rag.py`: ChanlunRecord / load_records / load_docs / split_into_paragraphs / normalize_lesson_id
  - `engine/chanlun_parser.py`: PDF → 按"第N课"分章 → 落 JSONL (pypdf, 增量 mtime 感知)
  - `scripts/export_ima_kb.py`: 通用 IMA KB 导出器 (`--list` / `--list-contents` / `--media-id` / `--type` 批量)
- 测试覆盖: `tests/test_v12_chanlun.py` 7 个 + `tests/test_v12_export_url.py` 3 个

### Changed
- `engine/knowledge.py`: 增量感知加 chanlun (load_corpus / mtime / changed 应用 3 处)
- `engine/knowledge.py::_data_dir_mtime`: 走 KNOWLEDGE_DIR 变量 (方便测试 monkey-patch)

### Security
- 3 份付费 KB 源加入 `.gitignore` (haoyun_wisdom.md / susan_all.json / chanlun/**), 不上传 GitHub
- `data/knowledge/README.md` 文档化 3 份数据自备流程

## [v12.3] - 2026-06-09

### Added
- listener watchdog: 异常 auto-restart (5/5min), max 5 次后停服, 避免静默挂掉

## [v12.1] - 2026-06-08

### Fixed
- minimax M3 `<think>` 标签泄漏修复
  - `tool_use._strip_think_tags` 3 路径自动剥除
  - pusher 入口也加剥除
  - 不再泄漏 think 标签给飞书用户

## [v12] - 2026-06-08

### Added
- 选股助手进化 (灵魂 + 记忆 + 主动 + 深聊 4 层基础)
  - `config/persona.yaml` SOUL 式 system prompt (identity / tone_rules / context_preamble)
  - `assistant/persona.py::load_persona`
  - `assistant/memory.py` (user_profile + memories 2 张表 + signal 正则检测)
  - `tool_use._build_system_prompt(chat_id)` 拼 persona + tools + memory
  - `dispatch` 注入 `sessions.get_history` last 5 turns
  - `listener.on_message` 写 user/assistant 两条 + 检测 memory 信号
  - `feishu/pusher.push_daily_summary` + `push_anomaly_recap` 2 新推送
  - `agent.PUSH_REGISTRY` 平级注册 (15:35 + 19:05 cron)
- `agent memory list/clear` CLI 子命令

### Changed
- `engine/paper_trader.SCHEMA` 增 2 表 2 索引 (老库 ALTER 兼容)

## [v11.1] - 2026-06-07

### Added
- LLM 路由日志入 `llm_logs` 表
  - 加 `tool_name` / `tool_args` / `chat_id` 3 列 (ALTER 老库兼容)
  - `_log_call` 升级支持新列
  - `chat_with_tools` 记 `tool_use_router`
  - `dispatch` 记 `tool_use_dispatch` (4 条路径)
- `agent start` 写 `data/agent.pid`
- `agent stop` 读 pid 发 SIGTERM 优雅关停

## [v11] - 2026-06-07

### Added
- 单进程 supervisor + LLM tool-use 重构
  - `agent._run_supervisor` 2-thread (scheduler + lark ws) 一键 `start` 替代双 launchd
  - `engine/skills.py` 8 个 skill (5 只读 + 2 解释 + 1 回测)
  - `call_skill` / `llm/tool_use.py::chat_with_tools` 注入 OpenAI 风格 schema
  - `dispatch` 一站式 (LLM 路由 → 调 skill → 拼卡片, 3 条降级路径)
- listener `on_message` 改用 dispatch + 注册 reaction 空 handler 去噪

## [v8-v10] - 2026-05-30 ~ 2026-06-05

### Added
- 增量索引 (`_data_dir_mtime` + 增量 build)
- 数据源 fallback (东方财富 → 新浪 → 腾讯)
- 涨跌停硬约束 (主板 ±10%, 创业板/科创板 ±20%, ST ±5%)
- 同源去重 (susan/chanyun 同一文章多段不重复召回)

## [v7] - 2026-05-25

### Added
- 飞书 WebSocket 长连 (lark-oapi v10)
- 群聊白名单 4 级控制 (whitelist/blacklist/allowed_user_ids/admin_user_ids)
- reaction 噪音处理 (im.message.reaction.created_v1)

## [v5] - 2026-05-15

### Added
- 真实回测 (`tests/fixtures/pick_*.json` 12 个真数据快照)
- auto 策略投票 (按历史胜率选 A/B/C)

## [v4] - 2026-05-10

### Added
- 11 指标回测 (Sharpe / Sortino / 信息比率 / max_dd / 年化 / Calmar / 波动率 / 最大连亏/连盈 / 盈亏比 / 胜率)
- 周日深度复盘

## [v3] - 2026-05-05

### Added
- 调参引擎 (`safe_range` 自动调参)
- 板块黑名单 (每周 `max_add_per_week` / `max_remove_per_week` 限流)
- 4 个 LLM 调用点 (pick_intro / risk_explain / param_reason / weekly_summary)

## [v2] - 2026-04-25

### Added
- 知识融合 RAG (haoyun + susan BM25)
- HTTP Bot (`webhook` 子命令, POST /chat 接受问答)
- 多策略 (A/B/C) + 多策略回测
- 多轮会话 (SQLite session, 24h TTL, 40 条上限)
- Session 加密 (Fernet, 可选)
- 报告导出 (`report` 子命令, 周报 Markdown 渲染)
- 行业集中度约束 (max_sector_ratio + max_sector_concurrent)

## [v1] - 2026-04-15

### Added
- 基础选股: 尾盘涨幅 3-4% + 换手率 8-10% + 振幅 < 8%
- 飞书推送 (webhook 通道)
- 单方案策略 (A)
- 7 阶段调度框架 (盘前/竞价/选股/盘后/日报/周报/盘中监控)
- Paper-trade 虚拟账户 (SQLite)
- 多源行情 (东方财富 + 腾讯 + 新浪)

[Unreleased]: https://github.com/ggr-hush/stock-trading-agent/compare/v12.5...HEAD
[v12.5]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v12.5
[v12.4]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v12.4
[v12.3]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v12.3
[v12.1]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v12.1
[v12]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v12
[v11.1]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v11.1
[v11]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v11
[v8-v10]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v10
[v7]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v7
[v5]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v5
[v4]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v4
[v3]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v3
[v2]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v2
[v1]: https://github.com/ggr-hush/stock-trading-agent/releases/tag/v1
