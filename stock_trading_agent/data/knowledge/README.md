# 知识库数据源

本目录存放 3 份知识库源文件, 用于 `engine/knowledge.py` 的 BM25 RAG 检索.

## 文件清单

| 文件 | 大小 | 来源 | 状态 |
|---|---|---|---|
| `haoyun_wisdom.md` | 4.7 KB | 好运2008 (淘股吧 ID:6630) 227 篇帖子提炼 | 🔒 私有 (不传 GitHub) |
| `susan_all.json` | 264 KB | 苏三离了家 49 篇 A 股点评 | 🔒 私有 (不传 GitHub) |
| `chanlun/108lessons.jsonl` | 850 KB | 缠中说禅 108 课 (IMA KB "至今配图最全"版) | 🔒 私有 (不传 GitHub) |
| `stock_synonyms.json` | 1.5 KB | 金融术语同义词 (项目自带) | ✅ 公开 (随仓库) |

## 为什么这 3 份是私有的

- **haoyun_wisdom**: 从 Hermes Agent 的 `haoyun-2008-wisdom` skill 二次提炼, 含个人批注
- **susan_all**: 苏三离了家 公众号内容整理, 部分来自付费星球
- **chanlun/108lessons**: 用户 IMA 知识库里的"至今配图最全"版, 是付费资源

## clone 后怎么补齐

### haoyun + susan (2 个文件)

直接问 alice 要, 或从 Hermes 公开版迁移:
- haoyun: 复制 `~/.codex/skills/haoyun-2008-wisdom/SKILL.md` 的内容到 `haoyun_wisdom.md`
- susan: 跑 `python3 -m stock_trading_agent.engine.knowledge` 触发自动 fallback, 或手动放

### chanlun (1 个文件 + 1 个脚本)

需要 IMA 凭据 (`~/.config/ima/{client_id, api_key}`).

```bash
# 1) 下载 PDF (从你 IMA KB 拉)
PYTHONPATH=. .venv/bin/python scripts/export_ima_kb.py \\
  --kb-id wFh3ADpvEIh1Nrex8IOYsyUKdJcIwBcJnImh7ruxFA0= \\
  --media-id pdf_25ebff4742c2ee9d0244406ac0580d23_33c0affcd45be65698eab34819df575d001a5dffcc0040e2 \\
  --out stock_trading_agent/data/knowledge/chanlun/108lessons.pdf

# 2) 解析 + 切章
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.engine.chanlun_parser

# 3) 验证 (期望 108 条)
wc -l stock_trading_agent/data/knowledge/chanlun/108lessons.jsonl
```

## 验证

跑测试, 期望 7/7 全绿:

```bash
PYTHONPATH=. .venv/bin/python -m stock_trading_agent.tests.test_v12_chanlun
```

跑真实搜索, 期望 top-3 都是 chanlun 命中:

```bash
PYTHONPATH=. .venv/bin/python -c "
from stock_trading_agent.engine import knowledge as kn
kn.reset_index()
for q in ['背驰', '中枢', 'MACD', '第一类买点']:
    results = kn.retrieve(q, k=3)
    print(f'Q: {q!r} → {len(results)} 命中, 源: {set(r[\"source\"].split(\":\")[0] for r in results)}')
"
```

## 故障排查

| 现象 | 原因 | 修法 |
|---|---|---|
| `corpus 分布` 没有 chanlun | JSONL 缺失或路径错 | 跑 step 2 重新生成 |
| 搜索返回空 | jieba 没装, 走 char n-gram 兜底 | `pip install jieba` (可选, 但显著提升中文分词) |
| `parse_pdf_to_jsonl` 报"PDF 抽文本为空" | PDF 是扫描件 | 用 `pdfplumber` 替代 `pypdf`, 或 OCR |
| `chanlun/108lessons.jsonl` 在 git status 出现 | .gitignore 没生效 | 检查 `.gitignore` 第 24-26 行, 确保 `chanlun/**` 在 |
