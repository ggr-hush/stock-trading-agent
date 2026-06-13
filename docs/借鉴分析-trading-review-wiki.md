# 借鉴分析: trading-review-wiki (杰哥 / ymj0418)

> 调研时间: 2026-06-13
> 调研仓库: https://github.com/ymj8903668-droid/trading-review-wiki
> 调研目的: 看有没有可借鉴的设计思想 / 实施模式
> 仓库定位: **交易复盘 wiki 知识库** (TypeScript + Tauri 桌面 app + Codex CLI), 不是选股 agent
> 与本项目关系: 同圈子 (A 股量化), 不同范式 (他做"知识库维护", 我们做"agent 自动决策")

---

## 核心定位差异

| 维度 | trading-review-wiki | stock-trading-agent (本项目) |
|---|---|---|
| 用户交互 | LLM 维护个人 wiki, 人类审阅 | agent 主动调度, 飞书对话触发 |
| 知识形态 | 持久 markdown wiki + temporal facts | SQLite + RAG BM25 + persona + memory |
| 部署形态 | Tauri 桌面 + Web + CLI 工具集 | macOS launchd 后台 + 飞书 bot |
| LLM 角色 | 编译/维护知识库 | 路由 + 解释 + 卡片渲染 |
| 自动化 | Codex CLI 调度 (ingest / ask / daily-loop) | APScheduler cron + 飞书 WebSocket |

**结论**: 不是替代关系, 是**互补**。他做"个人交易研究", 我们做"agent 自动选股+纸面交易"。

---

## 5 个可借鉴点 (按价值/成本排序)

### ⭐⭐⭐ 1. 多源 RAG + 证据编号

**他的做法**:
- 6 类源: `wiki_pages / raw_text / wiki_graph / facts_jsonl / brain_memory / stock_daily_sql`
- 每个证据编号: `W` (wiki) / `R` (raw) / `G` (graph) / `F` (facts) / `M` (memory) / `S` (SQL)
- 答案必须**逐条引用**: `[W1] 缠论 108 课说...` / `[R1] 6/12 复盘提到...`
- 固定 6 段式: 结论 / 证据链 / 分歧反证 / 后续验证 / 交易含义 / 引用来源

**我们的现状** (engine/skills.py `_run_explain_pick` / `_run_search_knowledge`):
- 调 LLM 自由回答, 末尾"来源: xxx"贴 1 行
- 没有强制引用, 用户不知道哪个判断来自哪个证据
- LLM 容易 hallucinate, 引用编号治"瞎编"

**借鉴做法 (v13 优先)**:
1. `engine/skills.py` 改造 `_run_explain_pick` / `_run_search_knowledge` 的 result:
   - 返 `{"explanation": "...", "evidence": [{"id": "R1", "kind": "knowledge", "title": "缠论 108 课", "snippet": "..."}]}`
2. `_render_*_card` 卡片底部加"📚 证据" 列表, 强制 LLM 引用编号
3. 增量: dispatch prompt 加 system 提示"每个判断必须引用 [R1]/[W1] 等编号"

**成本**: 1-2 小时 + 2-3 个测试
**价值**: 立竿见影治"瞎编", 用户信任度提升

---

### ⭐⭐⭐ 2. Brain memory 5 类型

**他的做法** (`data/brain/*.jsonl`):
| type | 用途 | 示例 |
|---|---|---|
| `correction` | 纠错 | "高开接盘必须看承接, 不允许把热度当买点" |
| `prediction` | 预测 | 用于事后验证 |
| `validation` | 验证结果 | success/fail 记录 |
| `preference` | 偏好 | "我不喜欢银行股" |
| `guardrail` | 卫语句 | 防 agent 重复犯同样错 |

**我们的现状** (v12 `memories` 表): `type` 字段有但没真正分类使用, `assistant/memory.py` 只简单 detect 几个 keyword。

**借鉴做法 (v12.A.3 顺手做)**:
1. `memories.type` 限定 5 个 enum + 校验
2. `assistant/memory.py` 改 detect_memory_signal 返回 5 类之一
3. dispatch system prompt 注入时按 type 分组, 类似:
   ```
   [用户偏好]
   - 用户不喜欢银行股
   [用户纠正]
   - 高开接盘必须看承接
   [agent 卫语句]
   - 不准用"显然/肯定/必然" 推仓位建议
   ```

**成本**: 1 小时 + 几个 enum 校验
**价值**: 你已经有 memories 表, 改字段约束 + 注入逻辑, persona 注入点直接复用

---

### ⭐⭐ 3. 时序事实账本 (Temporal Facts)

**他的做法** (`data/facts/temporal_edges.jsonl`):
```json
{
  "subject": "三孚新科",
  "predicate": "HAS_ORDER",
  "claim": "mSAP 电镀设备订单尚未确认",
  "status": "active",     // active / superseded / invalidated / expired
  "validAt": "2026-05-29",
  "supersedes": []
}
```

**核心机制**:
- "会过期、会被证伪"的事实从普通 wiki 拆出来
- 默认 `ask` 只查 `active` facts, 避免旧事实污染答案
- `--include-invalidated` 审计模式可看历史
- `supersedes` 链追踪"谁替代了谁"

**我们的现状**: v12 没有"事件状态机"概念
- `picks` 表存当日选股, 7 天后还在
- "上周选的 X 怎么样了" 类查询 → picks 全量返回, 不区分 active/historical
- "禾望电气周五成交 11.2 元" 是瞬时事实, 没过期机制

**借鉴做法 (v14+)**:
1. 新建 `data/facts/stock_events.jsonl`:
   - `pick` stage 选股时写 `SELECTED` fact
   - `post_market` 复盘时写 `VALIDATED` fact
   - `weekly_review` 调参时标记 `SUPERSEDED`
2. 问"上周 X 怎么样" → 只查 active events
3. 问"X 整个生命周期" → 全量 + status 标注

**成本**: 半天 + 改 pick / post_market 写库
**价值**: 治"时间错乱"类查询, 高

---

### ⭐⭐ 4. 写入边界纪律 (dry-run / apply --write)

**他的做法**:
```sh
npm run codex:ingest -- apply --manifest changes.json         # dry-run, 不改
npm run codex:ingest -- apply --manifest changes.json --write  # 真改
```
- `apply` 默认 dry-run, 必须显式 `--write` 才真写 wiki
- `raw/**` 永远不改写
- `wiki/**` 只能 apply 写
- 边界清晰: "程序能写什么/不能写什么" 列在 `directory boundaries` 表

**我们的现状**:
- `stage_pick` 直接写 `picks` 表, 没 dry-run
- `weekly_review` 调参直接 commit, 没预览
- 如果 stage 跑一半崩了 / 调参代码 bug → 脏数据
- README 没说"agent 写什么 / 不写什么", 边界靠 git log 维护

**借鉴做法 (v13+)**:
1. `agent/stages.py` 每个 stage 加 `--dry-run` 模式, 默认 dry-run
2. `tuner.py` 加 `apply --write` 显式写入
3. CHANGELOG / README 加"写入边界"段
4. v12 已有 `_write_pid` 等边界, 顺势整理

**成本**: 半天 + 回归测试 dry-run 路径
**价值**: 生产稳定性 +100%, 但对你(单用户)感知不强

---

### ⭐ 5. 桌面 / Web / CLI 三端 + 自动更新

**他的做法**:
- Tauri 桌面 app (macOS / Windows / Linux)
- Web (Vite + React)
- Codex CLI 工具集 (Node.js scripts)
- 自动更新: GitHub Releases + Sparkle (macOS) / Squirrel (Windows)

**我们的现状**:
- 纯 macOS 后台服务 + 飞书 bot
- 迭代方式: 改代码 → `bash deploy/install_launchd.sh restart` 5s 生效
- 没有桌面 app, 不需要 (你说过不打包)

**借鉴做法**:
- **不学** (你刚明确说不要打包成 .app)
- 唯一可借鉴: GitHub Releases 自动化 (`scripts/patch-release.py` / `upload-release-asset.py`)
- 但你 git push 已经够用, 不必自动化

---

## 一句话总结

**3 件值得立刻动手**:
1. **证据编号** (多源 RAG 答案逐条引用) — 立竿见影治"瞎编"
2. **brain memory 5 类型 enum** — 你已有 memories 表, 1 小时填字段
3. **时序事实账本** — 治"上周 X 怎样"类查询, 但要等 picks 数据积累

**不学**:
- Tauri 桌面 app (你刚说不打包)
- 自动更新 (单用户自用不需要)
- 大型 multi-source RAG (你 3 源够用)

**实施建议 (优先级)**:
- v13: #1 证据编号 (1-2h, 用户立刻能感觉到)
- v12.A.3 顺手: #2 memory 5 类型 (1h, 跟 persona 注入点复用)
- v14+: #3 temporal facts (半天, 等数据沉淀)
- 暂不做: #4 dry-run (单用户感知弱) / #5 桌面 app (你明确拒绝)

---

## 参考链接

- 项目主页: https://github.com/ymj8903668-droid/trading-review-wiki
- README: [README.md](https://github.com/ymj8903668-droid/trading-review-wiki/blob/main/README.md)
- 多源 RAG 流程: [docs/多源检索RAG完整流程.md](https://github.com/ymj8903668-droid/trading-review-wiki/blob/main/docs/多源检索RAG完整流程.md)
- Temporal Facts 设计: [docs/temporal-facts-v1.md](https://github.com/ymj8903668-droid/trading-review-wiki/blob/main/docs/temporal-facts-v1.md)
- Schema 模板: [docs/交易复盘Schema参考模板.md](https://github.com/ymj8903668-droid/trading-review-wiki/blob/main/docs/交易复盘Schema参考模板.md)
- 核心范式: [llm-wiki.md](https://github.com/ymj8903668-droid/trading-review-wiki/blob/main/llm-wiki.md) (LLM 持续维护个人 wiki 的通用模式)
