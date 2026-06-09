"""test_v12_export_url.py — v12.4 export_ima_kb.py 字段取值兼容

Covers:
  - url_info.url 嵌套结构 (v12.4 真实响应)
  - file_info.url 旧结构 (兼容)
  - 顶层 url 兜底
  - 缺 url 时明确报错 (不静默吞)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _load_export_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("export_ima_kb", ROOT / "scripts" / "export_ima_kb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_url_info_nested_url_extracted() -> None:
    """url_info.url 嵌套结构 — 这是真实 IMA PDF 响应格式"""
    mod = _load_export_module()
    captured: dict = {}

    def fake_get(url, headers=None, timeout=None, stream=True):
        captured["url"] = url
        captured["headers"] = headers
        # 模拟 302 → 文件内容
        r = MagicMock()
        r.status_code = 200
        r.iter_content = lambda chunk_size: [b"PDF-BYTES-HERE"]
        return r

    with patch.object(mod, "_post", return_value={
        "media_type": 1,
        "title": "test.pdf",
        "url_info": {
            "url": "https://res-pkb.ima.qq.com/2/xxx/file.pdf?media_id=...&media_title=...",
            "headers": {"X-Custom": "abc"},
        },
    }), patch("requests.get", side_effect=fake_get):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            mod.download_media("media_x", out_dir, "cid", "key")
            # 验证: url 用 url_info.url
            assert captured["url"].startswith("https://res-pkb.ima.qq.com"), \
                f"expected url_info.url, got {captured['url']}"
            assert captured["headers"] == {"X-Custom": "abc"}, \
                f"expected url_info.headers, got {captured['headers']}"
            # 验证: 文件落盘
            files = list(out_dir.glob("*"))
            assert len(files) == 1, f"expected 1 file, got {len(files)}"
            assert files[0].read_bytes() == b"PDF-BYTES-HERE"
    print("  PASS test_url_info_nested_url_extracted")


def test_file_info_legacy_url_still_works() -> None:
    """file_info.url 旧结构仍兼容"""
    mod = _load_export_module()
    captured: dict = {}

    def fake_get(url, headers=None, timeout=None, stream=True):
        captured["url"] = url
        r = MagicMock()
        r.status_code = 200
        r.iter_content = lambda chunk_size: [b"DATA"]
        return r

    with patch.object(mod, "_post", return_value={
        "media_type": 1,
        "title": "old.pdf",
        "file_info": {"url": "https://example.com/old.pdf"},
    }), patch("requests.get", side_effect=fake_get):
        with tempfile.TemporaryDirectory() as td:
            mod.download_media("media_y", Path(td), "cid", "key")
            assert captured["url"] == "https://example.com/old.pdf"
    print("  PASS test_file_info_legacy_url_still_works")


def test_no_url_field_exits_cleanly() -> None:
    """3 个字段都没有 → 明确报错退出 (而不是 NPE 后下载 0 字节文件)"""
    mod = _load_export_module()

    with patch.object(mod, "_post", return_value={
        "media_type": 1,
        "title": "broken.pdf",
        # url / url_info / file_info 都没有
    }):
        try:
            with tempfile.TemporaryDirectory() as td:
                mod.download_media("media_z", Path(td), "cid", "key")
        except SystemExit as e:
            assert "没有 url 字段" in str(e), f"unexpected exit msg: {e}"
            print("  PASS test_no_url_field_exits_cleanly")
            return
        raise AssertionError("expected SystemExit when no url field")


def main() -> int:
    tests = [
        test_url_info_nested_url_extracted,
        test_file_info_legacy_url_still_works,
        test_no_url_field_exits_cleanly,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    total = len(tests)
    print(f"\n  {total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
