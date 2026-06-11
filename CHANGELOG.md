# Changelog

本项目所有重要变更记录于此。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

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

