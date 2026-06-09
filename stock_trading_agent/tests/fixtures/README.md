# tests/fixtures/

**12 个真数据快照**（2026-05-07 → 2026-06-02，A 股交易日），由 `tests/test_v5.py` 引用。

每个文件结构：
```json
{
  "date": "2026-05-07",
  "plan": "A",
  "market_env": {"position_ratio": 0.5, "env_score": 50, "env_level": "中性"},
  "filtered_stocks": [{"code": "c000", "name": "...", "chg_pct": 3.27, ...}],
  "next_noon_prices": {"c000": 35.808}
}
```

**注意**：
- 字段名是历史命名（`2020-26-0507` 是手抖 bug, 测试里 mock 不依赖这个字段的语义, 跟 v5 review 真实逻辑解耦）
- 这些是历史回测 / reviewer 测试的**真值**，**不要手改**
- 需要新加 fixture: 复制一份 `pick_20260602.json` 改名 + 改 `filtered_stocks` 内容

新增 v5+ 测试用 fixture 时，把 JSON 落到本目录，测试用 `Path(__file__).parent / "fixtures" / "pick_YYYYMMDD.json"` 引用。
