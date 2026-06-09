"""
chanlun_rag.py — v12.4: 缠中说禅 108 课 知识源

- 数据落点: data/knowledge/chanlun/*.jsonl (每行 = 一段)
- 字段约定 (与 haoyun/susan 的 Doc 对齐):
  {"id": "L23", "title": "第23课 ...", "text": "...", "tags": ["级别", "中枢"]}
- Doc.source 命名: "chanlun:L23" (跟 susan:art_id 风格一致)
- 走跟 haoyun/susan 完全一样的 BM25 索引; 不引入新依赖
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("engine.chanlun_rag")

# 跟 _load_susan/_load_haoyun 用同一根目录
# 跟 knowledge.py 一样, 优先用 KNOWLEDGE_DIR 变量 (v12.4 monkey-patch 友好)
try:
    from .knowledge import KNOWLEDGE_DIR
    CHANLUN_DIR = KNOWLEDGE_DIR / "chanlun"
except ImportError:
    CHANLUN_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "knowledge" / "chanlun"


class ChanlunRecord:
    """一条 108 课段落"""
    __slots__ = ("id", "title", "text", "tags")

    def __init__(self, id: str, title: str, text: str, tags: list[str] | None = None) -> None:
        self.id = id
        self.title = title
        self.text = text
        self.tags = tags or []

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "text": self.text, "tags": self.tags}

    @classmethod
    def from_dict(cls, d: dict) -> "ChanlunRecord":
        return cls(
            id=str(d.get("id", "")).strip(),
            title=str(d.get("title", "")).strip(),
            text=str(d.get("text", "")),
            tags=list(d.get("tags", []) or []),
        )


def _iter_jsonl(path: Path) -> Iterator[ChanlunRecord]:
    """容错地逐行读 JSONL, 跳过空行和损坏行"""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("[chanlun] %s:%d 跳过损坏 JSON: %s", path.name, lineno, e)
                continue
            if not isinstance(obj, dict):
                continue
            rec = ChanlunRecord.from_dict(obj)
            if not rec.text:
                continue
            yield rec


_LESSON_ID_RE = re.compile(r"^L?(\d{1,3})$")


def normalize_lesson_id(raw_id: str) -> str:
    """"第23课" / "23" / "L23" / "lesson-23" -> "L23" """
    s = (raw_id or "").strip()
    m = _LESSON_ID_RE.match(s)
    if m:
        return f"L{m.group(1).zfill(2)}"
    m2 = re.search(r"(\d{1,3})", s)
    if m2:
        return f"L{m2.group(1).zfill(2)}"
    return "L00"


def split_into_paragraphs(text: str, max_chars: int = 800) -> list[str]:
    """长课文按 800 字 / 段落边界 切段, 用于 RAG 精检索"""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    paras: list[str] = []
    cur = ""
    for p in re.split(r"\n\s*\n", text):
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) + 1 <= max_chars:
            cur = (cur + "\n" + p) if cur else p
        else:
            if cur:
                paras.append(cur)
            if len(p) > max_chars:
                for s in re.split(r"(?<=[。！？!?])\s*", p):
                    s = s.strip()
                    if not s:
                        continue
                    if len(cur) + len(s) + 1 <= max_chars:
                        cur = (cur + s) if cur else s
                    else:
                        if cur:
                            paras.append(cur)
                        cur = s
            else:
                cur = p
    if cur:
        paras.append(cur)
    return paras


def load_records() -> list[ChanlunRecord]:
    """加载 data/knowledge/chanlun/ 下所有 .jsonl 文件的记录"""
    if not CHANLUN_DIR.exists():
        return []
    out: list[ChanlunRecord] = []
    for jsonl in sorted(CHANLUN_DIR.glob("*.jsonl")):
        out.extend(_iter_jsonl(jsonl))
    return out


def load_docs() -> list:
    """导出 BM25 友好的 Doc 列表 (跟 haoyun/susan 的 _load_* 对齐)

    Returns:
        [Doc(source='chanlun:L23', title='第23课 ...', text='...'), ...]
    """
    from .knowledge import Doc  # 局部 import 避免循环
    records = load_records()
    docs = []
    for rec in records:
        source = f"chanlun:{rec.id}"
        # 整课也算一个 doc, 方便问 "什么是中枢" 这种宽问
        docs.append(Doc(source=source, text=rec.text, title=rec.title))
        # 切段用于精检索
        for para in split_into_paragraphs(rec.text):
            docs.append(Doc(source=source, text=para, title=rec.title))
    return docs


# ─────────── CLI ───────────

if __name__ == "__main__":
    recs = load_records()
    print(f"加载 {len(recs)} 条缠论记录")
    for r in recs[:3]:
        print(f"  {r.id} | {r.title[:30]} | tags={r.tags} | {len(r.text)} 字")
