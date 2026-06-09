"""
chanlun_parser.py — v12.4: PDF → 108 课 JSONL

输入:  data/knowledge/chanlun/108lessons.pdf  (脚本 export_ima_kb.py 下载)
输出:  data/knowledge/chanlun/108lessons.jsonl (每行 = 一课)

切章规则 (按"第N课"标题分章, 兜底按页眉):
  1) 整本 PDF 抽文本 (pypdf)
  2) 用正则 ^第\\s*\\d{1,3}\\s*课.*$ 切章
  3) 每章 = 1 条 record: {id: L01, title: 第1课 ..., text: ..., tags: []}
  4) 落 JSONL, 增量: PDF mtime 变了才重跑
  5) tags 暂空 (v12.4 不打, v13 可接 LLM 自动标)

依赖: pypdf (venv 内已装)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("engine.chanlun_parser")

# 跟 haoyun/susan 同位置: stock_trading_agent/data/knowledge/chanlun/
# (v8.4 之后 data 移到 package 内部, 跟 KNOWLEDGE_DIR 一致)
import os
try:
    from .knowledge import KNOWLEDGE_DIR as _KN
    CHANLUN_DIR = _KN / "chanlun"
except ImportError:
    CHANLUN_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge" / "chanlun"
PDF_PATH = CHANLUN_DIR / "108lessons.pdf"
JSONL_PATH = CHANLUN_DIR / "108lessons.jsonl"

# 章节标题: "第1课", "第 23 课", "第108课  xxx"
# 章节标题 2 种格式 (按出现频率排):
#   "教你炒股票 23: ..." (实际 PDF 用的, 108 课正文)
#   "第23课 ..." (网页版 / 老版本)
_LESSON_HEAD_RE = re.compile(
    r"^\s*(?:教你炒股票|第)\s*(\d{1,3})\s*(?:课)?\s*[:：、\.][^\n]*$",
    re.MULTILINE
)
# 兜底: "Lesson 23" / "L23" (英文版 PDF)
_LESSON_HEAD_EN_RE = re.compile(r"^\s*(?:Lesson|L)\s*(\d{1,3})\b[^\n]*$", re.MULTILINE | re.IGNORECASE)

# 段间空白/页眉/页脚噪音清理
_PAGE_HEADER_NOISE_RE = re.compile(
    r"(?m)^[\s]*(?:缠中说禅\s*博客|教你炒股票|www\.|\d{4}[-/]\d{1,2}[-/]\d{1,2})[^\n]*$"
)


def _extract_text(pdf_path: Path) -> str:
    """抽 PDF 全文, 容错 (pypdf 失败时回退空串)"""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        log.error("pypdf 没装, 请 `pip install pypdf`")
        return ""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        log.error("PDF 打开失败: %s", e)
        return ""
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as e:
            log.warning("第 %d 页抽文本失败: %s", i + 1, e)
            t = ""
        pages.append(t)
    return "\n".join(pages)


def _find_first_lesson(text: str, matches: list[tuple[int, str, str]]) -> int:
    """启发: 找第一次"教你炒股票 N"出现在"非目录"段的位置

    目录特征: 该匹配后 80 字符内含 5+ 个 \n (页码) 或全 ASCII 数字+省略号
    正文特征: 该匹配后 200 字符内含 4+ 个中文字
    简化: 找所有 match 中, 第一次后面跟 ≥100 个中文字的
    """
    for pos, _lid, _t in matches:
        snippet = text[pos:pos + 400]
        han_count = sum(1 for c in snippet if "\u4e00" <= c <= "\u9fff")
        if han_count >= 100:
            return pos
    return matches[0][0] if matches else 0


def _split_lessons(full_text: str) -> list[tuple[str, str]]:
    """按"第N课"切章 → [(lesson_id, title), ...] 顺序对
    返回 [(lesson_id, full_text_of_lesson), ...]
    """
    # 不再全文先 sub noise (noise 含 教你炒股票 会误删 108 课标题), 改为逐 chunk 清理
    text = full_text
    # 找所有标题位置
    matches: list[tuple[int, str, str]] = []  # (start_pos, lesson_id, title)
    for m in _LESSON_HEAD_RE.finditer(text):
        num = m.group(1)
        title = m.group(0).strip()
        matches.append((m.start(), f"L{num.zfill(2)}", title))
    # 兜底: 如果 0 命中, 试英文
    if not matches:
        for m in _LESSON_HEAD_EN_RE.finditer(text):
            num = m.group(1)
            title = m.group(0).strip()
            matches.append((m.start(), f"L{num.zfill(2)}", title))

    if not matches:
        log.warning("PDF 未发现'教你炒股票 N'标题, 整本作为 L00 一章")
        return [("L00", "缠中说禅：教你炒股票108课 (未切章)")]

    # 目录过滤: 找第一次出现"教你炒股票 1:"正文 (通常含 200+ 字内容, 不是 ...1\n) 的位置
    # 启发: 目录里 "教你炒股票 N" 后面接 "...\n", 正文里接 "..." (含完整内容)
    first_lesson_pos = _find_first_lesson(text, matches)
    matches = [(p, lid, t) for (p, lid, t) in matches if p >= first_lesson_pos]
    if not matches:
        log.warning("过滤目录后 0 命中, 整本作为 L00 一章")
        return [("L00", "缠中说禅：教你炒股票108课 (未切章)")]

    out: list[tuple[str, str]] = []
    for i, (pos, lid, title) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        chunk = _PAGE_HEADER_NOISE_RE.sub("", text[pos:end]).strip()
        # 标题单独保留, 后面从 chunk 抽正文 (去掉标题那行)
        body = _LESSON_HEAD_RE.sub("", chunk, count=1).strip()
        if not body:
            body = chunk  # 切不出就整块
        out.append((lid, body, title))

    # 去重: 同 ID 多次出现 (目录 + 正文), 只保留 text 最长那条
    by_id: dict[str, tuple[str, str, str]] = {}
    for lid, body, title in out:
        if lid not in by_id or len(body) > len(by_id[lid][1]):
            by_id[lid] = (lid, body, title)
    out = list(by_id.values())
    # 按 ID 排序
    out.sort(key=lambda x: int(x[0].lstrip("L") or "0"))
    # 兼容签名: 返回 [(id, text), ...] (title 后续从 text 第一行拿)
    return [(lid, body) for lid, body, _ in out]


def _extract_title(text: str) -> str:
    """从正文首行/前 30 字内抽标题 (去掉正文里残留的"第N课"前缀)"""
    first = text.split("\n", 1)[0].strip()
    if 4 <= len(first) <= 60:
        return first
    return first[:30] + ("…" if len(first) > 30 else "")


def parse_pdf_to_jsonl(pdf_path: Path = PDF_PATH, jsonl_path: Path = JSONL_PATH) -> int:
    """主入口: PDF → JSONL

    Returns: 写入记录数 (0 表示失败 / 跳过)
    """
    if not pdf_path.exists():
        log.warning("PDF 不存在: %s (跳过)", pdf_path)
        return 0

    # 增量: PDF mtime <= JSONL mtime 跳过
    if jsonl_path.exists():
        if pdf_path.stat().st_mtime <= jsonl_path.stat().st_mtime:
            log.info("[chanlun_parser] PDF 未更新, 跳过 (mtime 增量)")
            return _count_lines(jsonl_path)

    full_text = _extract_text(pdf_path)
    if not full_text.strip():
        log.error("PDF 抽文本为空, 可能是扫描件 (需要 OCR). 试试: pypdf -> pdfplumber")
        return 0

    lessons = _split_lessons(full_text)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with jsonl_path.open("w", encoding="utf-8") as f:
        for lid, body in lessons:
            rec = {
                "id": lid,
                "title": f"第{lid.lstrip('L')}课",  # 占位, 后面用正文首行覆盖
                "text": body,
                "tags": [],
            }
            # 用正文首行尝试拿更准的标题
            rec["title"] = _extract_title(body) or rec["title"]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
    log.info("[chanlun_parser] 写入 %d 课到 %s", written, jsonl_path)
    return written


def _count_lines(p: Path) -> int:
    try:
        return sum(1 for _ in p.open("r", encoding="utf-8"))
    except OSError:
        return 0


# ─────────── CLI ───────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    n = parse_pdf_to_jsonl()
    print(f"\n  写入 {n} 条缠论记录")
    if n:
        # 列前 3 课预览
        for i, line in enumerate(JSONL_PATH.read_text(encoding="utf-8").splitlines()[:3]):
            rec = json.loads(line)
            print(f"  [{rec['id']}] {rec['title'][:40]} ({len(rec['text'])} 字)")
