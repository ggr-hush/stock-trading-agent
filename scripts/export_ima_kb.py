#!/usr/bin/env python3
"""
export_ima_kb.py — v12.4: 通用 IMA 知识库导出器

功能: 从 IMA KB 下载指定文件到本地 (默认: PDF → 落到 data/knowledge/<subdir>/)
- 支持单个文件 (--media-id) 或整 KB 浏览模式
- 通用: 任意 KB, 任意文件类型 (PDF/Word/PPT/笔记) 都用同一份
- 凭证读取顺序: 环境变量 > ~/.config/ima/{client_id, api_key}

用法:
  # 1) 拉 KB 列表 (--list)
  PYTHONPATH=. .venv/bin/python scripts/export_ima_kb.py --list

  # 2) 下载指定 PDF (108 课那一条)
  PYTHONPATH=. .venv/bin/python scripts/export_ima_kb.py \\
    --kb-id wFh3ADpvEIh1Nrex8IOYsyUKdJcIwBcJnImh7ruxFA0= \\
    --media-id pdf_25ebff4742c2ee9d0244406ac0580d23_33c0affcd45be65698eab34819df575d001a5dffcc0040e2 \\
    --out data/knowledge/chanlun/108lessons.pdf

  # 3) 下载 KB 内所有 PDF 文件 (批量模式)
  PYTHONPATH=. .venv/bin/python scripts/export_ima_kb.py \\
    --kb-id <kb_id> --type pdf --out data/knowledge/<subdir>/
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# 找凭证
def _load_creds() -> tuple[str, str]:
    cid = os.environ.get("IMA_CLIENT_ID") or os.environ.get("IMA_OPENAPI_CLIENTID", "")
    key = os.environ.get("IMA_API_KEY") or os.environ.get("IMA_OPENAPI_APIKEY", "")
    if not cid or not key:
        cfg = Path.home() / ".config" / "ima"
        if (cfg / "client_id").exists():
            cid = cid or (cfg / "client_id").read_text().strip()
        if (cfg / "api_key").exists():
            key = key or (cfg / "api_key").read_text().strip()
    if not cid or not key:
        sys.exit("❌ IMA 凭证缺失: 设环境变量 IMA_CLIENT_ID/IMA_API_KEY 或 ~/.config/ima/{client_id, api_key}")
    return cid, key


# IMA API base
IMA_BASE = "https://ima.qq.com"


def _post(path: str, body: dict, cid: str, key: str) -> dict:
    import requests
    url = f"{IMA_BASE}{path}"
    headers = {
        "ima-openapi-clientid": cid,
        "ima-openapi-apikey": key,
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(url, headers=headers, json=body, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"❌ 网络失败: {e}")
    if r.status_code != 200:
        sys.exit(f"❌ HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("code", 0) != 0:
        sys.exit(f"❌ IMA 错误: {data.get('msg')} (code={data.get('code')})")
    return data.get("data", {})


def list_kb(cid: str, key: str, query: str = "", limit: int = 20) -> list[dict]:
    """列 KB"""
    data = _post("/openapi/wiki/v1/search_knowledge_base",
                 {"query": query, "cursor": "", "limit": limit}, cid, key)
    return data.get("info_list", [])


def list_kb_contents(kb_id: str, cid: str, key: str, limit: int = 50) -> list[dict]:
    """列 KB 内全部条目 (翻页)"""
    out: list[dict] = []
    cursor = ""
    while True:
        data = _post("/openapi/wiki/v1/get_knowledge_list",
                     {"knowledge_base_id": kb_id, "cursor": cursor, "limit": limit}, cid, key)
        out.extend(data.get("knowledge_list", []))
        if data.get("is_end"):
            break
        cursor = data.get("next_cursor", "")
    return out


def get_media_info(media_id: str, cid: str, key: str) -> dict:
    """拿条目详情 (含下载 URL)"""
    return _post("/openapi/wiki/v1/get_media_info",
                 {"media_id": media_id}, cid, key)


# MediaType → 扩展名 / 子目录
MEDIA_TYPE_EXT = {
    1: ".pdf", 3: ".docx", 4: ".pptx", 5: ".xlsx", 7: ".md",
    9: ".png", 13: ".txt", 14: ".xmind", 15: ".m4a", 19: ".m4a",
}


def _safe_filename(title: str, media_type: int) -> str:
    base = re.sub(r"[\\/:*?\"<>|]", "_", title).strip()[:80]
    ext = MEDIA_TYPE_EXT.get(media_type, "")
    if not base.lower().endswith(ext):
        base = f"{base}{ext}"
    return base


import re  # noqa: E402


def download_media(media_id: str, out_dir: Path, cid: str, key: str) -> Path:
    """下载单个条目到 out_dir, 命名 = title + ext"""
    info = get_media_info(media_id, cid, key)
    # IMA 响应 url 可能在 url_info.url (在线资源) 或 file_info.url (本地已上传)
    url = (info.get("url_info") or {}).get("url") or (info.get("file_info") or {}).get("url") or info.get("url")
    if not url:
        sys.exit(f"❌ media_id={media_id} 没有 url 字段, 响应: {json.dumps(info, ensure_ascii=False)[:300]}")
    title = info.get("title") or media_id
    media_type = int(info.get("media_type", 0))
    fname = _safe_filename(title, media_type)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname
    if out_path.exists():
        logging.info("已存在, 跳过: %s", out_path)
        return out_path
    import requests
    headers = (info.get("url_info") or {}).get("headers") or (info.get("file_info") or {}).get("headers") or {}
    r = requests.get(url, headers=headers, timeout=60, stream=True)
    if r.status_code != 200:
        sys.exit(f"❌ 下载失败 HTTP {r.status_code}")
    with out_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    logging.info("✅ 下载: %s (%.1f KB)", out_path, out_path.stat().st_size / 1024)
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description="通用 IMA KB 导出器 (v12.4)")
    p.add_argument("--list", action="store_true", help="列所有可见 KB")
    p.add_argument("--list-contents", metavar="KB_ID", help="列 KB 内全部条目")
    p.add_argument("--kb-id", help="知识库 ID (跟 --media-id 一起用)")
    p.add_argument("--media-id", help="条目 ID (单个下载)")
    p.add_argument("--type", type=int, help="按 media_type 过滤批量下载 (e.g. 1=PDF)")
    p.add_argument("--out", help="输出文件 (单文件) 或目录 (批量)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    cid, key = _load_creds()

    if args.list:
        kbs = list_kb(cid, key)
        print(f"\n共 {len(kbs)} 个知识库:\n")
        for kb in kbs:
            print(f"  • {kb.get('kb_name','?')}")
            print(f"    id       : {kb.get('kb_id','?')}")
            print(f"    内容数   : {kb.get('content_count','?')}")
            print(f"    描述     : {(kb.get('description') or '').strip()[:80]}")
            print()
        return

    if args.list_contents:
        items = list_kb_contents(args.list_contents, cid, key)
        print(f"\nKB {args.list_contents} 共 {len(items)} 条:\n")
        for it in items:
            mt = it.get("media_type")
            ext = MEDIA_TYPE_EXT.get(mt, "?")
            print(f"  [{mt:>2} {ext:>5}] {it.get('title','?')[:50]}")
            print(f"           {it.get('media_id','?')}")
        return

    if args.kb_id and args.media_id:
        # 单文件下载
        out = Path(args.out) if args.out else Path("data/knowledge/chanlun/108lessons.pdf")
        if out.suffix == "" or out.is_dir():
            # 当目录处理
            download_media(args.media_id, out, cid, key)
        else:
            # 当文件: 先下到临时目录再 rename
            tmp_dir = out.parent
            tmp_dir.mkdir(parents=True, exist_ok=True)
            dl = download_media(args.media_id, tmp_dir, cid, key)
            if dl != out:
                dl.rename(out)
                logging.info("重命名: %s → %s", dl, out)
        return

    if args.kb_id and args.type is not None and args.out:
        # 批量: 按 type 过滤
        out_dir = Path(args.out)
        items = list_kb_contents(args.kb_id, cid, key)
        hits = [it for it in items if it.get("media_type") == args.type]
        logging.info("命中 %d 条 type=%d, 开始下载", len(hits), args.type)
        for it in hits:
            download_media(it["media_id"], out_dir, cid, key)
        return

    p.print_help()


if __name__ == "__main__":
    main()
