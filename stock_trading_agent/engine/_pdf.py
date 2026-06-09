"""engine/_pdf.py — v9.1 极简 PDF 生成器 (纯 stdlib)

不走 reportlab/weasyprint, 直接构造 PDF 1.4 二进制。
支持: 标题 / 段落 / 简单表格 (用 monospace font 对齐)。
中文处理: 用 WinAnsi (latin1) 不行, 我们走 UTF-16BE + Identity-H encoding (CIDFont Type0 简化版)
   - 简化起见, v9.1 只保证 ASCII 内容渲染正确, 含中文的内容用 UTF-16BE 编码 + Identity-H 标识
   - 大多数 reader (Preview, Adobe) 会要求 embedded CMap; 我们的简化版用 /Encoding /WinAnsiEncoding,
     中文会变成空白。生产环境建议装 reportlab 后用真实 CJK 字体, 这里只走通"接口 + 字节"
"""
from __future__ import annotations

from typing import Iterable


def _escape_pdf_text(s: str) -> str:
    """转义 PDF 文本特殊字符"""
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap_line(line: str, max_chars: int = 90) -> list[str]:
    """简单按字符数换行 (中英文都按 1 字符算, ASCII 简化)"""
    if len(line) <= max_chars:
        return [line]
    return [line[i:i + max_chars] for i in range(0, len(line), max_chars)]


def _md_to_blocks(md: str) -> list[tuple[str, str]]:
    """Markdown → [(kind, text), ...] 块序列
    kind ∈ {"h1","h2","h3","p","table","blank"}
    """
    blocks: list[tuple[str, str]] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            blocks.append(("blank", ""))
            i += 1
            continue
        if line.startswith("### "):
            blocks.append(("h3", line[4:].strip()))
        elif line.startswith("## "):
            blocks.append(("h2", line[3:].strip()))
        elif line.startswith("# "):
            blocks.append(("h1", line[2:].strip()))
        elif line.startswith("|") and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|"):
            # 表格: 收集直到非表格行
            tbl: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                tbl.append(lines[i].strip())
                i += 1
            blocks.append(("table", "\n".join(tbl)))
            continue
        else:
            # 段落: 把后续非空行拼一起
            para = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not (
                lines[i].startswith("#") or
                (lines[i].lstrip().startswith("|") and i + 1 < len(lines)
                 and lines[i + 1].lstrip().startswith("|"))
            ):
                para.append(lines[i].strip())
                i += 1
            blocks.append(("p", " ".join(para)))
            continue
        i += 1
    return blocks


def render_pdf(md: str, title: str = "Report") -> bytes:
    """Markdown 文本 → PDF 二进制

    简化实现: 标题 + 段落流式排版, 表格用 monospace font 对齐。
    页面: A4, 边距 50pt, 单倍行距。
    """
    # 页面几何
    page_w, page_h = 595, 842  # A4 in pt
    margin_x, margin_y = 50, 50
    line_h = 14
    title_h = 24
    h2_h, h3_h = 20, 18
    max_y = page_h - margin_y
    body_fs = 10
    title_fs = 18
    h2_fs, h3_fs = 14, 12

    content_stream: list[str] = []
    y = page_h - margin_y

    def new_page() -> None:
        # 关闭当前页
        content_stream.append("BT /F1 10 Tf ET")
        content_stream.append("")

    def ensure_space(need: float) -> None:
        nonlocal y
        if y - need < margin_y:
            new_page()
            y = page_h - margin_y

    # 标题
    ensure_space(title_h)
    content_stream.append("q")
    content_stream.append(f"BT /F1 {title_fs} Tf {margin_x} {y - title_fs} Td ({_escape_pdf_text(title)}) Tj ET")
    content_stream.append("Q")
    y -= title_h + 6

    blocks = _md_to_blocks(md)
    for kind, text in blocks:
        if kind == "blank":
            y -= line_h // 2
            continue
        if kind == "h1":
            ensure_space(h2_h)
            content_stream.append(
                f"BT /F1 {h2_fs} Tf {margin_x} {y - h2_fs} Td ({_escape_pdf_text(text)}) Tj ET"
            )
            y -= h2_h
        elif kind == "h2":
            ensure_space(h3_h)
            content_stream.append(
                f"BT /F1 {h2_fs} Tf {margin_x} {y - h2_fs} Td ({_escape_pdf_text(text)}) Tj ET"
            )
            y -= h3_h
        elif kind == "h3":
            ensure_space(line_h)
            content_stream.append(
                f"BT /F1 {h3_fs} Tf {margin_x} {y - body_fs} Td ({_escape_pdf_text(text)}) Tj ET"
            )
            y -= line_h
        elif kind == "p":
            for ln in _wrap_line(text, max_chars=85):
                ensure_space(line_h)
                content_stream.append(
                    f"BT /F1 {body_fs} Tf {margin_x} {y - body_fs} Td ({_escape_pdf_text(ln)}) Tj ET"
                )
                y -= line_h
        elif kind == "table":
            # 表格用 Courier (F2) 渲染, 列宽按 | 切分
            for row in text.splitlines():
                # 去掉首尾 | 分割
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                if all(set(c) <= set("-: ") for c in cells):
                    continue  # 分隔行
                line = " | ".join(cells)
                for ln in _wrap_line(line, max_chars=95):
                    ensure_space(line_h)
                    content_stream.append(
                        f"BT /F2 {body_fs} Tf {margin_x} {y - body_fs} Td ({_escape_pdf_text(ln)}) Tj ET"
                    )
                    y -= line_h
        y -= 4  # 块间隔

    # ─────────── 拼装 PDF 对象 ───────────
    objs: list[bytes] = []

    def add_obj(content: str) -> int:
        objs.append(content.encode("latin-1", errors="replace"))
        return len(objs)

    # 1: Catalog
    catalog_id = 1
    add_obj("<< /Type /Catalog /Pages 2 0 R >>")

    # 2: Pages
    add_obj("<< /Type /Pages /Kids [3 0 R] /Count 1 >>")

    # 3: Page
    content_id = 4
    add_obj(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] "
        f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents {content_id} 0 R >>"
    )

    # 4: Content stream
    stream = "\n".join(content_stream)
    add_obj(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")

    # 5: Font Helvetica
    add_obj("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # 6: Font Courier (表格用)
    add_obj("<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    # 串接 PDF: header + body + xref + trailer
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"
    xref_pos = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n".encode("latin-1")
    pdf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        pdf += f"{off:010d} 00000 n \n".encode("latin-1")
    pdf += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    return pdf
