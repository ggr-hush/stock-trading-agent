"""
knowledge.py — RAG over haoyun-2008 + susan-commentary + chanlun (v12.4)
- 简单 BM25 (字符 trigram + IDF), 无外部依赖
- 加载 data/knowledge/ 下的源文件
- retrieve(query, k=3) 返回 [(source, score, text), ...]
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

log = logging.getLogger("engine.knowledge")
from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).parent.parent / "data" / "knowledge"

# ─── 可选 jieba (中文分词); 没装时回退 char n-gram ───
try:
    import jieba  # type: ignore
    _HAS_JIEBA = True
    jieba.setLogLevel(20)  # 静默
except ImportError:
    _HAS_JIEBA = False


def _load_synonyms() -> dict[str, list[str]]:
    """加载同义词 dict; 用于 query 扩展"""
    syn_path = KNOWLEDGE_DIR / "stock_synonyms.json"
    if not syn_path.exists():
        return {}
    try:
        import json
        return json.loads(syn_path.read_text())
    except Exception:
        return {}


_SYNONYMS = _load_synonyms()


def expand_query(query: str) -> str:
    """用同义词扩展 query: 把 "题材龙头" → "题材龙头 主线 热点 龙头 风口 概念 核心标的"

    注意: 同义词 dict 里的词都是金融术语, 加进去能显著提高 BM25 召回
    """
    if not query or not _SYNONYMS:
        return query
    expanded = [query]
    for term, syns in _SYNONYMS.items():
        if term in query:
            for s in syns:
                if s not in query:
                    expanded.append(s)
    return " ".join(expanded)


# ─────────── 文档模型 ───────────

@dataclass
class Doc:
    source: str          # "haoyun_wisdom" / "susan:7437167697623103"
    text: str
    title: str = ""


def _split_sentences(text: str) -> list[str]:
    """中文句子切分 (用标点 + 段落)"""
    # 先按段落
    paras = re.split(r"\n\s*\n", text)
    sents: list[str] = []
    for p in paras:
        p = p.strip()
        if not p:
            continue
        # 按中英文标点切
        parts = re.split(r"(?<=[。！？!?])\s*", p)
        for s in parts:
            s = s.strip()
            if len(s) >= 8:  # 至少 8 个字
                sents.append(s)
    return sents


def _load_haoyun() -> list[Doc]:
    """好运2008 的 SKILL.md 整篇 + 切段"""
    p = KNOWLEDGE_DIR / "haoyun_wisdom.md"
    if not p.exists():
        return []
    text = p.read_text()
    # 整篇也算一个 doc, 方便问"什么是术/法/道"
    docs: list[Doc] = [Doc(source="haoyun_wisdom", text=text, title="好运2008交易心法(全文)")]
    # 切段用于精检索
    for s in _split_sentences(text):
        docs.append(Doc(source="haoyun_wisdom", text=s, title=""))
    return docs


def _load_susan() -> list[Doc]:
    """苏三离了家 49 篇"""
    p = KNOWLEDGE_DIR / "susan_all.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except Exception:
        return []
    results = (data or {}).get("results", {})
    docs: list[Doc] = []
    for art_id, full_text in results.items():
        title_match = re.search(r"^([^\n]{2,40})", full_text)
        title = title_match.group(1) if title_match else ""
        docs.append(Doc(source=f"susan:{art_id}", text=full_text, title=title))
        for s in _split_sentences(full_text):
            docs.append(Doc(source=f"susan:{art_id}", text=s, title=title))
    return docs


def load_corpus() -> list[Doc]:
    """加载所有知识源 (v12.4: 加 chanlun)"""
    from .chanlun_rag import load_docs as _load_chanlun
    return _load_haoyun() + _load_susan() + _load_chanlun()


# ─────────── BM25 索引 ───────────

class BM25:
    """极简 BM25 (字符 n-gram tokenization)"""

    def __init__(self, docs: list[str], ngram: int = 2, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs
        self.n = len(docs)
        self.doc_lens = [len(self._tokenize(d)) for d in docs]
        self.avgdl = sum(self.doc_lens) / max(self.n, 1)
        # term -> df
        self.df: dict[str, int] = defaultdict(int)
        # doc_idx -> Counter(term -> tf)
        self.tfs: list[Counter] = []
        for d in docs:
            tokens = self._tokenize(d)
            tf = Counter(tokens)
            self.tfs.append(tf)
            for term in tf:
                self.df[term] += 1

    @staticmethod
    def _tokenize(text: str, ngram: int = 2) -> list[str]:
        """中文分词: jieba 优先, 降级 char n-gram

        jieba 模式: 词级 + 单字 (单字保留避免漏词)
        降级模式: 字符 n-gram + 单字
        """
        text = re.sub(r"\s+", " ", text.lower())
        if _HAS_JIEBA:
            tokens = list(jieba.cut(text))
            # 过滤标点 + 空白
            tokens = [t for t in tokens if re.search(r"[\u4e00-\u9fff\w]", t)]
            # 加单字 (兜底)
            chars = re.findall(r"[\u4e00-\u9fff]", text)
            return tokens + chars
        # 降级: 字符 n-gram
        chars = re.findall(r"[\u4e00-\u9fff\w]", text)
        if len(chars) < ngram:
            return chars
        grams = [chars[i:i + ngram] for i in range(len(chars) - ngram + 1)]
        return ["".join(g) for g in grams] + chars

    def add_docs(self, new_docs: list[str]) -> None:
        """v8.4: 增量添加 docs, 重建 df / tfs / doc_lens (n 小时全表)"""
        for d in new_docs:
            tokens = self._tokenize(d)
            tf = Counter(tokens)
            self.tfs.append(tf)
            self.doc_lens.append(len(tokens))
            for term in tf:
                self.df[term] += 1
        self.docs.extend(new_docs)
        self.n = len(self.docs)
        self.avgdl = sum(self.doc_lens) / max(self.n, 1)

    def score(self, query: str) -> list[float]:
        """返回每个 doc 的 BM25 分数"""
        q_tokens = self._tokenize(query)
        scores = [0.0] * self.n
        for q in q_tokens:
            if q not in self.df:
                continue
            idf = math.log((self.n - self.df[q] + 0.5) / (self.df[q] + 0.5) + 1)
            for i, tf in enumerate(self.tfs):
                f = tf.get(q, 0)
                if f == 0:
                    continue
                dl = self.doc_lens[i]
                denom = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                scores[i] += idf * f * (self.k1 + 1) / denom
        return scores


# ─────────── 全局索引 ───────────

_INDEX: tuple[BM25, list[Doc]] | None = None


def _data_dir_mtime(data_subdir: str) -> float:
    """v8.4: 取数据目录下所有文件的最新 mtime (v12.4 改走 KNOWLEDGE_DIR 变量, 方便测试 monkey-patch)"""
    d = KNOWLEDGE_DIR / data_subdir.replace("knowledge/", "", 1)
    if not d.exists():
        return 0.0
    mtimes = []
    for f in d.rglob("*"):
        if f.is_file():
            try:
                mtimes.append(f.stat().st_mtime)
            except OSError:
                continue
    return max(mtimes) if mtimes else 0.0


_INDEX: tuple[BM25, list[Doc], dict[str, float]] | None = None


def _get_index() -> tuple[BM25, list[Doc]]:
    """v8.4: 增量感知索引

    检查 haoyun / susan 两个 source 的 mtime, 哪个变了就只重 build 那个 source 的 docs,
    通过 BM25.add_docs() 增量合并, 不全表重建。
    """
    global _INDEX
    hao_mtime = _data_dir_mtime("knowledge/haoyun")
    sus_mtime = _data_dir_mtime("knowledge/susan")
    chl_mtime = _data_dir_mtime("knowledge/chanlun")
    current = {"haoyun": hao_mtime, "susan": sus_mtime, "chanlun": chl_mtime}

    if _INDEX is None:
        corpus = load_corpus()
        if not corpus:
            raise RuntimeError("知识库为空, 请检查 data/knowledge/")
        bm = BM25([d.text for d in corpus])
        _INDEX = (bm, corpus, current)
        return _INDEX[0], _INDEX[1]

    bm, corpus, cached_mtime = _INDEX
    changed: list[str] = []
    for src, mt in current.items():
        if mt > cached_mtime.get(src, 0.0):
            changed.append(src)

    if not changed:
        return bm, corpus

    # 有 source 变了, 只重读那部分
    log.info("[knowledge] 增量更新: %s (cache: %s → now: %s)",
             changed, cached_mtime, current)
    new_corpus = list(corpus)
    existing = len(corpus)
    if "haoyun" in changed:
        new_corpus = _load_haoyun() + [d for d in new_corpus if d.source != "haoyun"]
    if "susan" in changed:
        new_corpus = [d for d in new_corpus if d.source != "susan"] + _load_susan()
    if "chanlun" in changed:
        from .chanlun_rag import load_docs as _load_chanlun
        new_corpus = [d for d in new_corpus if not d.source.startswith("chanlun:")] + _load_chanlun()
    new_texts = [d.text for d in new_corpus[existing:]]
    if new_texts:
        bm.add_docs(new_texts)
    _INDEX = (bm, new_corpus, current)
    return bm, new_corpus


def reset_index() -> None:
    """强制重建索引 (数据更新后调用)"""
    global _INDEX
    _INDEX = None


# ─────────── 检索 ───────────

def retrieve(query: str, k: int = 3, expand: bool = True) -> list[dict[str, Any]]:
    """检索 top-k 文档片段

    Args:
        query: 用户问题
        k: top-k
        expand: 是否用同义词扩展 query (默认开)

    Returns:
        [{"source": str, "title": str, "text": str, "score": float}, ...]
    """
    if not query or not query.strip():
        return []
    try:
        bm, corpus = _get_index()
    except RuntimeError:
        return []
    # 同义词扩展
    q = expand_query(query) if expand else query
    scores = bm.score(q)
    # 取 top-k
    indexed = sorted(enumerate(scores), key=lambda x: -x[1])
    out: list[dict[str, Any]] = []
    seen_sources: dict[str, int] = {}  # 同一 source 最多 2 段
    for idx, sc in indexed:
        if sc <= 0:
            break
        d = corpus[idx]
        if seen_sources.get(d.source, 0) >= 2:
            continue
        seen_sources[d.source] = seen_sources.get(d.source, 0) + 1
        out.append({
            "source": d.source,
            "title": d.title,
            "text": d.text,
            "score": round(float(sc), 3),
        })
        if len(out) >= k:
            break
    return out


def format_context(results: list[dict[str, Any]], max_chars: int = 800) -> str:
    """把检索结果格式化成 prompt 上下文"""
    if not results:
        return ""
    parts: list[str] = []
    total = 0
    for r in results:
        snippet = r["text"][:300]  # 截断每段
        block = f"[{r['source']}] {snippet}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


# ─────────── CLI ───────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m stock_trading_agent.engine.knowledge <query>")
        sys.exit(1)
    q = sys.argv[1]
    print(f"Q: {q}\n")
    for i, r in enumerate(retrieve(q, k=5), 1):
        print(f"[{i}] {r['source']} (score={r['score']})")
        print(f"    {r['text'][:150]}...")
        print()
