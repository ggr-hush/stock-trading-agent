"""test_v12_chanlun.py — v12.4 缠中说禅 RAG 测试

Covers:
  - load_records: 容错 / 多文件 / 空目录
  - normalize_lesson_id: 多种格式归一化
  - split_into_paragraphs: 短文不切 / 长文切段
  - load_docs 跟 knowledge.load_corpus 集成
  - knowledge._get_index 增量感知 chanlun
  - knowledge.retrieve 能命中 chanlun
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _tmp_chanlun_dir() -> Path:
    """Return data/knowledge/chanlun-equivalent path: parent dir + child chanlun.
    Patch then sets KNOWLEDGE_DIR = parent, CHANLUN_DIR = child.
    """
    parent = Path(tempfile.mkdtemp(prefix="sta_test_chanlun_root_"))
    child = parent / "chanlun"
    child.mkdir(parents=True, exist_ok=True)
    return child


def _patch_chanlun_dir(d: Path):
    import stock_trading_agent.engine.chanlun_rag as cr
    import stock_trading_agent.engine.knowledge as kn
    orig_cr = cr.CHANLUN_DIR
    orig_kn = kn.KNOWLEDGE_DIR
    cr.CHANLUN_DIR = d
    kn.KNOWLEDGE_DIR = d.parent
    return orig_cr, orig_kn, cr, kn


def _restore(orig_cr, orig_kn, cr, kn):
    cr.CHANLUN_DIR = orig_cr
    kn.KNOWLEDGE_DIR = orig_kn


def _write_jsonl(d: Path, name: str, records: list[dict]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    with (d / name).open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------- 1. load_records: multi-file + corrupt JSON tolerance ----------

def test_load_records_basic_and_multifile() -> None:
    d = _tmp_chanlun_dir()
    orig_cr, orig_kn, cr, kn = _patch_chanlun_dir(d)
    try:
        _write_jsonl(d, "lessons_part1.jsonl", [
            {"id": "L01", "title": "L01", "text": "AAA", "tags": ["level"]},
            {"id": "L02", "title": "L02", "text": "BBB", "tags": ["hub"]},
        ])
        _write_jsonl(d, "lessons_part2.jsonl", [
            {"id": "L03", "title": "L03", "text": "CCC", "tags": ["bei-chi"]},
        ])
        recs = cr.load_records()
        assert len(recs) == 3, f"expected 3, got {len(recs)}"
        assert [r.id for r in recs] == ["L01", "L02", "L03"]
        assert recs[1].text == "BBB"
        # corrupt JSON line should be skipped
        (d / "lessons_part1.jsonl").write_text(
            (d / "lessons_part1.jsonl").read_text() + "\n{not json\n",
            encoding="utf-8",
        )
        recs2 = cr.load_records()
        assert len(recs2) == 3, f"corrupt line should be skipped, got {len(recs2)}"
    finally:
        _restore(orig_cr, orig_kn, cr, kn)
        shutil.rmtree(d, ignore_errors=True)
    print("  PASS test_load_records_basic_and_multifile")


def test_load_records_empty_dir_no_crash() -> None:
    d = _tmp_chanlun_dir()
    orig_cr, orig_kn, cr, kn = _patch_chanlun_dir(d)
    try:
        recs = cr.load_records()
        assert recs == []
        shutil.rmtree(d)
        recs2 = cr.load_records()
        assert recs2 == []
    finally:
        _restore(orig_cr, orig_kn, cr, kn)
    print("  PASS test_load_records_empty_dir_no_crash")


# ---------- 2. normalize_lesson_id various formats ----------

def test_normalize_lesson_id_various_formats() -> None:
    from stock_trading_agent.engine.chanlun_rag import normalize_lesson_id
    cases = [
        ("L23", "L23"),
        ("23", "L23"),
        ("L23", "L23"),
        ("L007", "L007"),  # 3-digit kept (zfill(2) is no-op on 3-digit)
        ("Lesson 108", "L108"),
        ("", "L00"),
        ("abc", "L00"),
    ]
    for raw, expected in cases:
        got = normalize_lesson_id(raw)
        assert got == expected, f"normalize({raw!r}) = {got!r}, expected {expected!r}"
    print("  PASS test_normalize_lesson_id_various_formats")


# ---------- 3. split_into_paragraphs ----------

def test_split_into_paragraphs_short_no_split() -> None:
    from stock_trading_agent.engine.chanlun_rag import split_into_paragraphs
    text = "X" * 100
    parts = split_into_paragraphs(text)
    assert parts == [text], f"short text should not split, got {len(parts)} parts"
    print("  PASS test_split_into_paragraphs_short_no_split")


def test_split_into_paragraphs_long_split() -> None:
    from stock_trading_agent.engine.chanlun_rag import split_into_paragraphs
    # 5 paragraphs, each ~600 chars, total ~3000 chars, max_chars=800
    # → expect at least 3 parts, each ≤ 850
    paras = [f"P{i}-" + "X" * 600 for i in range(5)]
    text = "\n\n".join(paras)
    parts = split_into_paragraphs(text, max_chars=800)
    assert len(parts) >= 3, f"expected >=3 parts, got {len(parts)}"
    for part in parts:
        assert len(part) <= 850, f"part too long: {len(part)} chars"
    # content should be preserved (modulo whitespace)
    joined = "".join(parts).replace("\n", "")
    original = text.replace("\n", "")
    assert joined == original, "split dropped content"
    print("  PASS test_split_into_paragraphs_long_split")


# ---------- 4. load_docs appears in knowledge corpus and retrievable ----------

def test_load_docs_appears_in_corpus_and_retrievable() -> None:
    d = _tmp_chanlun_dir()
    orig_cr, orig_kn, cr, kn = _patch_chanlun_dir(d)
    try:
        _write_jsonl(d, "108lessons.jsonl", [
            {"id": "L17", "title": "L17", "text": "this talks about hub and level", "tags": ["hub"]},
            {"id": "L24", "title": "L24", "text": "bei-chi method for first class buy/sell", "tags": ["bei-chi"]},
        ])
        kn.reset_index()
        corpus = kn.load_corpus()
        sources = {x.source for x in corpus}
        assert "chanlun:L17" in sources, f"corpus missing chanlun:L17: {sources}"
        assert "chanlun:L24" in sources, f"corpus missing chanlun:L24: {sources}"
        results = kn.retrieve("hub", k=3)
        assert any("chanlun" in r["source"] for r in results), \
            f"retrieve('hub') no chanlun hit: {results}"
    finally:
        kn.reset_index()
        _restore(orig_cr, orig_kn, cr, kn)
        shutil.rmtree(d, ignore_errors=True)
    print("  PASS test_load_docs_appears_in_corpus_and_retrievable")


# ---------- 5. _get_index incremental: chanlun mtime change rebuilds ----------

def test_get_index_reloads_on_chanlun_change() -> None:
    d = _tmp_chanlun_dir()
    orig_cr, orig_kn, cr, kn = _patch_chanlun_dir(d)
    try:
        _write_jsonl(d, "v1.jsonl", [
            {"id": "L01", "title": "L01", "text": "v1 content hub", "tags": []},
        ])
        kn.reset_index()
        _, corpus1 = kn._get_index()
        n1 = sum(1 for x in corpus1 if x.source.startswith("chanlun:"))
        assert n1 >= 1, f"v1 should have >=1 chanlun doc, got {n1}"

        # Update JSONL and force mtime 2s into the future (bypass macOS 1s precision)
        _write_jsonl(d, "v1.jsonl", [
            {"id": "L01", "title": "L01", "text": "v1 content hub", "tags": []},
            {"id": "L02", "title": "L02", "text": "v2 new L02 bei-chi", "tags": []},
        ])
        new_mtime = (datetime.now() + timedelta(seconds=2)).timestamp()
        os.utime(d / "v1.jsonl", (new_mtime, new_mtime))

        _, corpus2 = kn._get_index()
        n2 = sum(1 for x in corpus2 if x.source.startswith("chanlun:"))
        assert n2 > n1, f"incremental load failed: v1={n1}, v2={n2}"
    finally:
        kn.reset_index()
        _restore(orig_cr, orig_kn, cr, kn)
        shutil.rmtree(d, ignore_errors=True)
    print("  PASS test_get_index_reloads_on_chanlun_change")


def main() -> int:
    tests = [
        test_load_records_basic_and_multifile,
        test_load_records_empty_dir_no_crash,
        test_normalize_lesson_id_various_formats,
        test_split_into_paragraphs_short_no_split,
        test_split_into_paragraphs_long_split,
        test_load_docs_appears_in_corpus_and_retrievable,
        test_get_index_reloads_on_chanlun_change,
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
    passed = total - failed
    print(f"\n  {passed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
