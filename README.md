# stock-trading-agent

多阶段量化炒股 agent，从通用 agent 跑的 `daily-stock-picker` 技能升级而来。

v11 起单进程 supervisor 跑调度 + 飞书 WebSocket; v12 加人格/记忆/主动/深聊 4 层; v12.4 接入缠中说禅 108 课 RAG。

## 核心特性

- **多阶段** (APScheduler 单进程): 盘前复盘 → 集合竞价风控 → 尾盘选股 → 盘后对账 → 晚间日报 → 周日深度复盘
- **多策略**: 方案 A 精准 / B 平衡 / C 空仓保护, 历史投票自动选
- **v3 调参**: 评分上限、强信号带、板块黑名单 (基于 06-06 深度复盘)
- **自动调参**: 周末按 `safe_range` 自动改 config.yaml, 超范围推飞书确认卡片
- **Paper-trade 引擎**: 跟实盘一致的虚拟账户, 便于复盘对账
- **RAG 知识库**: 好运2008 (227 篇) + 苏三离了家 (49 篇) + 缠中说禅 108 课, BM25 检索
- **失败降级**: LLM 失败不阻塞主流程, 卡片走"无解释"模板

## 快速开始

### 1. 建虚拟环境 + 装依赖

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` 含 6 个包: lark-oapi / requests / jinja2 / cryptography / pyyaml / apscheduler。pyyaml 和 apscheduler 是 `agent start` 必需, 只跑 `run-once` / 测试可不装。

### 2. 配置 .env

```bash
cp .env.example .env
# 填 4 个必填 key: MINIMAX_API_KEY, FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID
# 不设 MINIMAX_API_KEY 也行, 6 个 LLM 调用点会优雅降级
```

详细见 [环境变量](#环境变量) 章节。

### 3. 跑起来

```bash
# 单次跑某个阶段
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent run-once --stage pick

# 单进程 supervisor (v11+): 调度 + 飞书 WebSocket 一个进程
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent start

# 优雅关停
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent stop
```

`agent start` 后跑在后台, 飞书消息来即回复, 6 阶段按 cron 自动跑。

### 4. 验证

```bash
# 静态: 检查 .env 必填项
python3 scripts/check_env.py

# 动态: 跑全部测试 (12 套件 / 209 测试)
for t in test_pusher test_picker test_tuner test_paper_trader test_smoke \
         test_v2 test_v3 test_v4 test_v5 test_v7 test_v11 test_v12 \
         test_v12_chanlun test_v12_export_url; do
  printf "  %-22s " "$t"
  PYTHONPATH=. .venv/bin/python -m stock_trading_agent.tests.$t 2>&1 | tail -1
done
```

## 目录结构

```
stock-trading-agent/
├── README.md                         # 本文件
├── .env.example                      # 环境变量模板
├── .gitignore                        # 排除 .env + 3 份付费 KB
├── config/
│   └── persona.yaml                  # v12 人格 (SOUL 式 system prompt)
├── docs/
│   ├── v3-tuning.md                  # 调参依据完整文档
│   └── reports/                      # 周报产物 (6/7 旧报告归 archive/)
├── scripts/
│   ├── check_env.py                  # .env 校验
│   ├── export_ima_kb.py              # v12.4 通用 IMA KB 导出器
│   ├── push_wiki.py
│   └── yaml_to_json.py
└── stock_trading_agent/              # Python 包
    ├── agent.py                      # 入口 (v11: supervisor, 2 thread)
    ├── assistant/                    # v12: persona + memory
    ├── engine/                       # 业务核心 (picker / paper_trader / knowledge / skills ...)
    ├── feishu/                       # 飞书监听 + 推送
    ├── llm/                          # minimax M3 客户端 + tool-use 路由
    ├── data/                         # 知识库 (haoyun/susan/chanlun) + SQLite + picks fixtures
    └── tests/                        # 13 个 test_*.py, 209 测试全绿
```

开发者看内部细节: `engine/skills.py` (8 个 skill 注册表) / `assistant/persona.py` / `llm/tool_use.py`。

## 知识库

3 份 RAG 源在 `stock_trading_agent/data/knowledge/`, 详见 [data/knowledge/README.md](stock_trading_agent/data/knowledge/README.md):

| 源 | 大小 | 状态 |
|---|---|---|
| `haoyun_wisdom.md` | 4.7 KB | 🔒 私有 (不在 GitHub) |
| `susan_all.json` | 264 KB | 🔒 私有 |
| `chanlun/108lessons.jsonl` | 850 KB | 🔒 私有 |
| `stock_synonyms.json` | 1.5 KB | ✅ 公开 (随仓库) |

clone 后前 3 份**得自备**。`chanlun` 那份跑 `scripts/export_ima_kb.py` 从 IMA KB 拉。

## 环境变量

`.env` 已在项目根创建 (git 忽略), 含 9 个 key:

| Key | 必填 | 来源 |
|---|---|---|
| `MINIMAX_API_KEY` | 推荐 | 你的 MiniMax 控制台 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 必填 | 飞书开放平台 → 应用凭证 |
| `FEISHU_CHAT_ID` | 必填 | 飞书群 chat_id (`oc_xxx`) |
| `FEISHU_DASH_APP_TOKEN` / `FEISHU_DASH_DASHBOARD_ID` | 必填 | 多维表格 token + dashboard |
| `FEISHU_BITABLE_WEBHOOK` | 推荐 | 多维表格 webhook URL (webhook 模式通道) |
| `FEISHU_SINA_UT` | 自动 | 东方财富 ut token (已自动: 硬编码默认 + 启动时拉一次) |
| `BOT_ENCRYPTION_KEY` | 已内置 | Session Fernet 加密 |

**3 个加载源 (优先级 高→低)**:
1. 进程 env (`export KEY=value`)
2. `~/.hermes/.env` (跨项目共享)
3. `<project_root>/.env` (项目本地, 已 git 忽略)

**FEISHU_SINA_UT 说明**: 不是用户凭据, 是东财 `push2delay.eastmoney.com` 接口的公开 token. 硬编码默认 `bd1d9ddb04089700cf9c27f6f7426281` + 首次调用抓 `https://data.eastmoney.com/` 解析, 失败回退默认. 东财轮换时手动填 env 即可. 关自动刷新: `UT_AUTO_REFRESH=0`.

**上线三步** (默认 `FEISHU_PUSH_MODE=auto`):
1. 静态检查: `python3 scripts/check_env.py` 输出 `0 to-fill`
2. 试跑一次: `PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent run-once --stage pick`, 确认飞书收到卡片
3. 进守护: `PYTHONPATH=. .venv/bin/python -m stock_trading_agent.agent start`

## 6 个阶段时间线

| 阶段 | cron | 作用 |
|---|---|---|
| `pre_market` | 工作日 8:30 | 盘前复盘 + 异动预警 |
| `open_auction` | 工作日 9:15 | 集合竞价风控 |
| `pick` | 工作日 14:00 | 尾盘选股 (核心) |
| `post_market` | 工作日 15:30 | 盘后对账 |
| `evening` | 工作日 19:00 | 晚间日报 |
| `weekly_review` | 周日 20:00 | 深度复盘 + 调参 + 回测 |
| `intraday_monitor` | 工作日 9-15 每 5 分钟 | 盘中异动 (v9.3+) |

## 调参

所有阈值在 `stock_trading_agent/config.yaml` (项目自带) + `config/persona.yaml` (人格), 改这两文件 + 重启 `agent start` 生效.

**3 类参数**:
- **硬阈值** (`hard:` 段): v1 不可自动改, 改完需人工 review
- **v3 调参** (`v3:` 段): tuner 只能在 `safe_range` 内动, 超出推飞书确认
- **板块黑名单** (`blacklist:` 段): 每周 `max_add_per_week` / `max_remove_per_week` 限流

详细依据见 [docs/v3-tuning.md](docs/v3-tuning.md)。

## 跟原 daily-stock-picker 的区别

| 维度 | daily-stock-picker (原 skill) | stock-trading-agent (本项目) |
|---|---|---|
| 形态 | 通用 agent 跑一次性 skill | 独立 daemon 长进程 |
| 调度 | 手动触发 | APScheduler 7 阶段 cron |
| 策略 | 单方案 (尾盘涨幅 3-4%) | 3 方案 A/B/C + auto 投票 |
| 调参 | 手动 | 周日自动 (`safe_range` 内) |
| 复盘 | 无 | 日报 + 周报 + 回测 (11 指标) |
| 持仓跟踪 | 无 | paper-trade + 盘中异动监控 |
| 知识库 | 无 | 3 源 BM25 RAG (好运2008/苏三/缠论) |
| 交互 | 一次性输出 | 飞书 bot 多轮对话 (v12 4 层人格化) |

## 演进记录 (v1 → v12.4)

| 版本 | 主题 | 关键改动 | 测试 |
|---|---|---|---|
| v1 | 基础选股 | 尾盘涨幅 3-4% + 换手率 8-10% + 振幅 < 8% | 6 |
| v2 | 知识融合 + Bot | RAG (haoyun/susan) + HTTP Bot + 多策略 + Fernet 加密 | 12 |
| v3 | 调参引擎 | `safe_range` 自动调参 + 板块黑名单 + 4 个 LLM 调用点 | 14 |
| v4 | 周报 + 回测 | 11 指标 (Sharpe/Sortino/Calmar) + 周日深度复盘 | 18 |
| v5 | 真实回测 | `tests/fixtures/pick_*.json` 12 个真数据快照 + auto 策略投票 | 10 |
| v7 | 飞书 WebSocket | lark-oapi 长连 + 群聊白名单 4 级控制 + reaction 噪音处理 | 23 |
| v8-v10 | 体验优化 | 增量索引 / 数据源 fallback / 涨跌停硬约束 | 51 |
| v11 | 单进程 supervisor + LLM tool-use | `agent._run_supervisor` 2-thread (scheduler + lark ws) 一键 `start` / `engine/skills.py` 8 个 skill / `llm/tool_use.py::chat_with_tools` / `dispatch` 一站式 (LLM 路由 → 调 skill → 拼卡片) | 17 |
| v11.1 | LLM 路由日志 + agent stop | `llm_logs` 加 3 列 (tool_name/args/chat_id) / `agent start` 写 pid file / `agent stop` 发 SIGTERM | 4 |
| v12 | 选股助手进化 (灵魂+记忆+主动+深聊) | `config/persona.yaml` SOUL / `assistant/persona.py` / `assistant/memory.py` 2 张表 / `tool_use._build_system_prompt` 拼 persona+tools+memory / 5 turns session 注入 / `feishu/pusher.push_daily_summary` + `push_anomaly_recap` 2 推送 / `agent memory list/clear` CLI | 24 |
| v12.1 | think 标签泄漏修复 | `tool_use._strip_think_tags` 3 路径自动剥除 minimax `` 标签 | 5 |
| v12.3 | listener watchdog | 5/5min auto-restart, max 5 次后停服 | 5 |
| v12.4 | 缠中说禅 108 课 RAG | `engine/chanlun_rag.py` / `engine/knowledge.py` 增量感知加 chanlun / `engine/chanlun_parser.py` PDF → "第N课"切章 / `scripts/export_ima_kb.py` 通用 IMA 导出器 | 10 |

**当前**: 14 套件 / 209 测试全绿. `agent start` 后台跑 supervisor + 飞书监听 (4 维人格化: persona + memory + session + proactivity); `agent stop` 优雅关停; `agent memory list/clear` 查清用户记忆.

## 后续候选 (v13+)

1. **飞书交互卡片**: v9.1 推的是 markdown 文本, 改成 v2 卡片 (含按钮 / 折叠块 / 链接), 用户在卡片里直接确认调参/补跑
2. **实盘 broker 对接**: 把 paper 改成实盘 (雪球/华泰/同花顺条件单), 严格风控
3. **跨市场扩展**: 港股 / 美股 (改 push2 接口 + 板块映射)
4. **Web UI dashboard**: 实时显示持仓/PnL/异动, 取代纯飞书卡片
