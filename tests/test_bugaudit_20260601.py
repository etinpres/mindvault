"""bug-audit 2026-06-01 회귀 가드 — 전체 시스템 점검에서 발견·수정한 결함들.

각 테스트는 수정 전 코드에서 FAIL, 수정 후 PASS 하도록 설계.
  - FTS5 예약어(AND/OR/NOT/NEAR) bareword 누수 (운영 15건+, 5/28~)
  - _iter_balanced_arrays 선행 불균형 '[' 조기 종료로 유효 배열 유실
  - gemma_rerank 중복 LLM 인덱스 미dedup
  - recall_cli main() 검색 예외 전파 → "exit 항상 0" 계약 위반
  - 인덱서 비-문자열 frontmatter(.strip()) 크래시로 run 전체 중단
  - contradiction_detector 비-문자열 Gemma content .strip() 크래시
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestFts5ReservedKeywords(unittest.TestCase):
    """AND/OR/NOT/NEAR 가 포함된 쿼리도 FTS5 MATCH 가 받아야 한다."""

    def _parses(self, fts_q: str) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(body, tokenize='unicode61')")
        conn.execute("INSERT INTO t(body) VALUES ('alpha not beta and gamma')")
        conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (fts_q,)).fetchone()
        conn.close()

    def test_memory_search_keywords_parse(self):
        from memory_search import _fts_escape
        for q in ["alpha NOT beta", "x AND y", "p OR q", "a NEAR b", "AND OR NOT NEAR"]:
            with self.subTest(query=q):
                self._parses(_fts_escape(q))

    def test_search_keywords_parse(self):
        from search import fts_escape
        for q in ["alpha NOT beta", "x AND y", "p OR q", "a NEAR b"]:
            with self.subTest(query=q):
                self._parses(fts_escape(q))

    def test_parity_preserved(self):
        from memory_search import _fts_escape
        from search import fts_escape
        for q in ["alpha NOT beta", "스캐너 AND 동작", "hello world", "next-33 진행"]:
            with self.subTest(query=q):
                self.assertEqual(_fts_escape(q), fts_escape(q))

    def test_keyword_quoted_nonkeyword_bare(self):
        from memory_search import _fts_escape
        out = _fts_escape("alpha NOT beta")
        self.assertIn('"NOT"*', out)
        self.assertIn("alpha*", out)


class TestBalancedArraysSkipUnbalanced(unittest.TestCase):
    def test_leading_unbalanced_then_valid(self):
        from memory_extractor import _iter_balanced_arrays
        s = 'noise [ unclosed then later [{"type":"project","title":"T","body":"B"}]'
        arrs = list(_iter_balanced_arrays(s))
        self.assertEqual(len(arrs), 1)
        self.assertEqual(arrs[0][0]["type"], "project")

    def test_multiple_valid_arrays_no_infinite_loop(self):
        from memory_extractor import _iter_balanced_arrays
        s = '[{"a":1}] junk [{"b":2}]'
        self.assertEqual(len(list(_iter_balanced_arrays(s))), 2)


class TestGemmaRerankDedup(unittest.TestCase):
    def test_duplicate_indices_deduped(self):
        import search
        cands = [{"session_id": str(i)} for i in range(4)]
        with patch.object(search, "call_gemma", return_value="[2, 2, 2]"):
            self.assertEqual(search.gemma_rerank("q", cands, k=3), [2])

    def test_order_preserved_dedup(self):
        import search
        cands = [{"session_id": str(i)} for i in range(4)]
        with patch.object(search, "call_gemma", return_value="[3, 1, 3, 0]"):
            self.assertEqual(search.gemma_rerank("q", cands, k=3), [3, 1, 0])


class TestRecallCliExit0Contract(unittest.TestCase):
    def test_search_exception_yields_empty_exit0(self):
        import recall_cli
        argv = ["recall_cli.py", "some query", "--source", "both"]
        written = []
        with patch.object(recall_cli, "_search_memory", side_effect=ImportError("no numpy")), \
             patch.object(recall_cli, "_search_sessions", side_effect=RuntimeError("boom")), \
             patch.object(sys, "argv", argv), \
             patch.object(sys.stdout, "write", side_effect=lambda s: written.append(s)):
            rc = recall_cli.main()
        self.assertEqual(rc, 0)
        out = json.loads("".join(written))
        self.assertEqual(out["memory"], [])
        self.assertEqual(out["sessions"], [])


class TestIndexerNonStringFrontmatter(unittest.TestCase):
    def test_int_frontmatter_does_not_crash_run(self):
        from memory_indexer import incremental_index
        with tempfile.TemporaryDirectory() as d:
            dp = Path(d)
            memdir = dp / "memory"
            memdir.mkdir()
            (memdir / "bad.md").write_text(
                "---\nname: 2026\ndescription: 1234\n---\n본문 내용\n", encoding="utf-8")
            (memdir / "good.md").write_text(
                "---\nname: good\ndescription: 정상 설명\n---\n정상 본문\n", encoding="utf-8")
            db = dp / "t.db"
            with patch("memory_indexer.embed_text", side_effect=lambda *a, **k: [0.5] * 1024):
                incremental_index([memdir], db_path=db)  # 수정 전: AttributeError 로 run 중단
            conn = sqlite3.connect(str(db))
            n = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(n, 2)


class TestClassifyNonStrContent(unittest.TestCase):
    def test_list_content_returns_none_no_crash(self):
        import contradiction_detector as cd

        class _Resp:
            def __init__(self, payload): self._p = payload
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self._p

        payload = json.dumps(
            {"choices": [{"message": {"content": [{"type": "text", "text": "x"}]}}]}
        ).encode()
        with patch.object(cd.urllib.request, "urlopen", return_value=_Resp(payload)):
            self.assertIsNone(cd._call_gemma_for_classify("prompt"))


class TestBlockListRegexSpaces(unittest.TestCase):
    """#11: 공백 포함 블록리스트 항목도 탐지해 mutation 거부 가드가 동작해야 한다."""

    def test_block_list_with_spaces_detected(self):
        import contradiction_review_cli as c
        for fm in [
            "supersedes:\n  - some old memory\n  - another one\n",
            "supersedes:\n  - some old memory",
            "type: x\nsupersedes:\n  - old one here\n  - two\ndescription: y\n",
        ]:
            with self.subTest(fm=fm):
                self.assertTrue(c._BLOCK_LIST_RE("supersedes").search(fm))

    def test_flow_list_and_scalar_not_blocked(self):
        import contradiction_review_cli as c
        self.assertFalse(c._BLOCK_LIST_RE("supersedes").search("supersedes: [a, b]\n"))
        self.assertFalse(c._BLOCK_LIST_RE("supersedes").search("supersedes: scalarval\n"))

    def test_can_patch_refuses_block_with_spaces(self):
        import contradiction_review_cli as c
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.md"
            p.write_text(
                "---\nname: x\nsupersedes:\n  - some old memory\n  - another\n---\nbody\n",
                encoding="utf-8")
            # 공백 포함 블록리스트는 mutation 거부(False) 여야 — 수정 전엔 True 로 통과해 손상
            self.assertFalse(c._can_patch_frontmatter_list(p, "supersedes"))


class TestClassifyUnicodeDecodeError(unittest.TestCase):
    """#10: 깨진 UTF-8 응답에 UnicodeDecodeError 가 detect 루프를 뚫지 않아야 한다."""

    def test_invalid_utf8_returns_none(self):
        import contradiction_detector as cd

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"\xff\xfe not valid utf8 \x80"

        with patch.object(cd.urllib.request, "urlopen", return_value=_Resp()):
            self.assertIsNone(cd._call_gemma_for_classify("prompt"))



if __name__ == "__main__":
    unittest.main()
