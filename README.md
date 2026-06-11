# stock-trading-agent

A 股量化选股 + paper-trade 自动化 agent。6 阶段 cron + 飞书 bot 多轮对话 + 3 源 RAG 知识库。

## 快速开始

```bash
# 1. 装环境
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配 .env (4 个必填 + 5 个推荐)
cp .env.example .env
# 必填: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID
# 推荐: MINIMAX_API_KEY, FEISHU_BITABLE_WEBHOOK

# 3. 校验 + 试跑
python3 scripts/check_env.py
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent run-once --stage pick

# 4. 后台跑 (单进程 scheduler + 飞书 WebSocket, v12.5.2 自重启)
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent start
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent stop
```

## 6 个阶段 (工作日)

| 阶段 | 时间 | 作用 |
|---|---|---|
| `pre_market` | 8:30 | 盘前复盘 + 异动预警 |
| `open_auction` | 9:15 | 集合竞价风控 |
| `pick` | 14:00 | 尾盘选股 (3 方案 A/B/C + auto 投票) |
| `post_market` | 15:30 | 盘后对账 |
| `evening` | 19:00 | 晚间日报 |
| `weekly_review` | 周日 20:00 | 深度复盘 + 调参 + 回测 |
| `intraday_monitor` | 9-15 每 5 分钟 | 盘中异动 |

cron 在 `stock_trading_agent/config.yaml` 改，`agent start` 重启生效。

## 飞书交互

启动 `agent start` 后，私聊或群里 @bot 即可对话。LLM 路由 (v11 tool-use) 自动判别：

- 问"今日选股/持仓/日报/大盘" → 查 SQLite 拼卡片
- 问"为什么选 X / 缠论怎么看" → RAG 检索 + LLM 包装
- 闲聊 / 解释 / 复盘 → LLM 自由回答 (minimax M3)

## 知识库 (RAG)

3 份 BM25 源在 `stock_trading_agent/data/knowledge/`:

| 源 | 来源 | 状态 |
|---|---|---|
| `haoyun_wisdom.md` | 好运2008 (淘股吧 ID:6630) 227 篇心法 | 🔒 私有 (git 忽略) |
| `susan_all.json` | 苏三离了家 49 篇市场点评 | 🔒 私有 |
| `chanlun/108lessons.jsonl` | 缠中说禅教你炒股票 108 课 | 🔒 私有 |
| `stock_synonyms.json` | 股票代码→名称映射 | ✅ 公开 |

clone 后前 3 份**自备**。`chanlun/` 跑 `scripts/export_ima_kb.py` 从 IMA KB 拉。详见 `data/knowledge/README.md`。

## 目录结构

```
stock-trading_agent/
├── agent/                   # v12.6 拆分: stages / supervisor / webhook / cli
├── assistant/               # v12: persona + memory
├── engine/                  # 业务: picker / paper_trader / knowledge / skills
├── feishu/                  # 飞书监听 + 推送
├── llm/                     # minimax M3 client + tool-use 路由
├── data/                    # 知识库 + SQLite + 测试 fixtures
├── tests/                   # 16 套件 / 232 测试
└── config/                  # config.yaml + persona.yaml
```

文档:
- `docs/v3-tuning.md` — 调参依据
- `CHANGELOG.md` — 最近 5 个版本
- `CHANGELOG_ARCHIVE.md` — v1 ~ v12.5 的老版本

## 关键设计

- **多策略**: 方案 A 精准 / B 平衡 / C 空仓保护, 自动投票选
- **v3 调参**: 评分上限 / 强信号带 / 板块黑名单, 周末自动在 `safe_range` 内调
- **Paper-trade 引擎**: 跟实盘一致的虚拟账户, 便于复盘对账
- **失败降级**: LLM 失败不阻塞主流程, 卡片走"无解释"模板
- **v12.5.2 自重启**: 飞书 WebSocket 16min 断线时, supervisor 自动 `os.execv` 整进程重启 (1h 限 10 次)

## 开发者

```bash
# 跑全部测试
for t in test_pusher test_picker test_tuner test_paper_trader test_smoke \
         test_v2 test_v3 test_v4 test_v5 test_v7 test_v11 test_v12 \
         test_v12_chanlun test_v12_export_url test_v12_start_guard test_v12_5_1_dedup; do
  printf "  %-22s " "$t"
  PYTHONPATH=. .venv/bin/python -m stock_trading_agent.tests.$t 2>&1 | tail -1
done

# 加新 stage: stock_trading_agent/agent/stages.py + STAGE_REGISTRY
# 加新 push: stock_trading_agent/agent/stages.py + PUSH_REGISTRY
# 改启停/watchdog: stock_trading_agent/agent/supervisor.py
# 加 HTTP endpoint: stock_trading_agent/agent/webhook.py
# 加新 subcommand: stock_trading_agent/agent/cli.py
```
