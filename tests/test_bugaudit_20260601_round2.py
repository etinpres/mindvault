"""bug-audit 2026-06-01 round2 회귀 가드 — 교차관점 점검에서 발견·수정한 결함.

  #7 extractor: finish_reason=length 절단 응답을 호출실패로 취급(영구 negative 캐시 방지)
  #7b/#10 Gemma 비-문자열 content .strip()/.splitlines() 크래시 방어 (extractor/alias)
  #8 query_intent: transient 실패(None)는 7일 negative 센티넬 캐시 금지
  #11 reverify: 비-dict sidecar JSON 에 AttributeError 없이 None 반환(자가복구)
  #13/#14/#15 backfill_cli: 음수 --sleep/--limit/--last-hours 거부
"""
from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch


class _Resp:
    def __init__(self, payload: bytes):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def _gemma_payload(content, finish_reason="stop"):
    return json.dumps({
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}]
    }).encode()


class TestExtractorTruncationAndNonStr(unittest.TestCase):
    def test_finish_reason_length_returns_none(self):
        import memory_extractor as me
        payload = _gemma_payload('[{"type":"project","title":"T"', finish_reason="length")
        with patch.object(me.urllib.request, "urlopen", return_value=_Resp(payload)):
            self.assertIsNone(me.call_gemma("p"))

    def test_nonstr_content_returns_none(self):
        import memory_extractor as me
        payload = _gemma_payload([{"type": "text", "text": "x"}])
        with patch.object(me.urllib.request, "urlopen", return_value=_Resp(payload)):
            self.assertIsNone(me.call_gemma("p"))

    def test_normal_content_still_works(self):
        import memory_extractor as me
        payload = _gemma_payload("[]", finish_reason="stop")
        with patch.object(me.urllib.request, "urlopen", return_value=_Resp(payload)):
            self.assertEqual(me.call_gemma("p"), "[]")


class TestAliasNonStrContent(unittest.TestCase):
    def test_list_content_no_crash(self):
        import alias_generator as ag
        payload = _gemma_payload([{"type": "text", "text": "alias1\nalias2"}])
        with patch.object(ag.urllib.request, "urlopen", return_value=_Resp(payload)):
            self.assertEqual(ag._call_gemma("desc", "body"), [])


class TestIntentTransientNegCache(unittest.TestCase):
    def test_transient_failure_not_negative_cached(self):
        import query_intent as qi
        with patch.object(qi, "_gemma_cache_get", return_value=None), \
             patch.object(qi, "_call_gemma_intent", return_value=None), \
             patch.object(qi, "_gemma_cache_put") as put:
            self.assertIsNone(qi.classify_with_gemma("이거 뭐 처리해줘 적당히"))
            put.assert_not_called()

    def test_genuine_other_is_negative_cached(self):
        import query_intent as qi
        with patch.object(qi, "_gemma_cache_get", return_value=None), \
             patch.object(qi, "_call_gemma_intent", return_value="other"), \
             patch.object(qi, "_gemma_cache_put") as put:
            self.assertIsNone(qi.classify_with_gemma("이거 뭐 처리해줘 적당히"))
            put.assert_called_once()


class TestReverifyNonDictSidecar(unittest.TestCase):
    def test_nondict_sidecar_returns_none(self):
        import tempfile
        from pathlib import Path
        import reverify as rv
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "sidecar.json"
            for bad in ("[1, 2, 3]", "42", '"justastring"'):
                sp.write_text(bad, encoding="utf-8")
                with patch.object(rv, "_sidecar_path", return_value=sp):
                    self.assertIsNone(rv._read_sidecar_last_scan())


class TestBackfillNegativeArgs(unittest.TestCase):
    def test_negative_sleep_rejected(self):
        import backfill_cli
        with self.assertRaises(SystemExit):
            backfill_cli.main(["--sleep", "-1"])

    def test_negative_limit_rejected(self):
        import backfill_cli
        with self.assertRaises(SystemExit):
            backfill_cli.main(["--limit", "-5"])

    def test_negative_last_hours_rejected(self):
        import backfill_cli
        with self.assertRaises(SystemExit):
            backfill_cli.main(["--last-hours", "-24"])


class TestSessionVecStaleOnUpdate(unittest.TestCase):
    """#1: 기존 세션 재인덱싱 중 임베드 실패 시 stale vec 를 남기지 말고 삭제."""

    def test_embed_fail_on_update_deletes_vec_row(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        import indexer

        def _vec_sids(db):
            c = sqlite3.connect(str(db))
            try:
                return c.execute("SELECT session_id FROM sessions_vec").fetchall()
            finally:
                c.close()

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = root / "index.db"
            projects = root / "projects"
            (projects / "slot").mkdir(parents=True)
            jp = projects / "slot" / "sess-x.jsonl"
            jp.write_text(json.dumps({
                "type": "user",
                "message": {"content": "최초 세션 본문 충분히 긴 회수 대상 내용입니다"},
                "timestamp": "2026-01-01T00:00:00",
            }) + "\n", encoding="utf-8")
            # 1) embed OK → vec 행 생성
            with patch("memory_indexer.embed_text", side_effect=lambda *a, **k: [0.5] * 1024):
                indexer.incremental_index(projects, db)
            self.assertEqual(len(_vec_sids(db)), 1)
            # 2) 본문(첫 턴) 변경 → size 변동 → 재인덱싱, 이번엔 embed down
            jp.write_text(json.dumps({
                "type": "user",
                "message": {"content": "변경된 세션 본문 — 첫 턴이 달라졌고 길이도 더 길어졌습니다 추가추가추가추가"},
                "timestamp": "2026-01-01T00:00:00",
            }) + "\n", encoding="utf-8")
            with patch("memory_indexer.embed_text", side_effect=lambda *a, **k: None):
                indexer.incremental_index(projects, db)
            # 수정 후: stale vec 대신 행이 삭제(NULL 상태) → backfill 재충전 가능
            self.assertEqual(_vec_sids(db), [], "임베드 실패 시 기존 세션 vec 는 삭제돼야(stale 금지)")



class TestSearchCallGemmaNonStr(unittest.TestCase):
    """#R3: search.call_gemma 도 non-str content 가드(다른 Gemma 호출자와 parity)."""

    def test_list_content_returns_none(self):
        import search
        payload = _gemma_payload([{"type": "text", "text": "x"}])
        with patch.object(search.urllib.request, "urlopen", return_value=_Resp(payload)):
            self.assertIsNone(search.call_gemma("prompt"))


class TestProvenanceMissingDir(unittest.TestCase):
    """#R3: provenance backfill 이 미존재 memory_dir 를 silent 0건 성공으로 처리하지 않는다."""

    def test_missing_dir_nonzero_exit(self):
        import provenance_backfill_cli as pbc
        argv = ["prog", "/nonexistent/path/xyz-mv3-audit"]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as cm:
                pbc.main()
        self.assertNotEqual(cm.exception.code, 0)



class TestCollectMdFilesDedup(unittest.TestCase):
    """#R4-HIGH: dirs 에 동일 dir 중복 시 _collect_md_files 가 같은 .md 를 한 번만 방출
    (dedup merge 자기삭제 데이터유실 방지)."""

    def test_duplicate_dir_emitted_once(self):
        import tempfile
        from pathlib import Path
        import memory_indexer as mi
        with tempfile.TemporaryDirectory() as d:
            memdir = Path(d) / "memory"
            memdir.mkdir()
            (memdir / "topic.md").write_text("---\nname: topic\n---\n본문\n", encoding="utf-8")
            out = mi._collect_md_files([memdir, memdir])  # 동일 dir 중복
            names = [p.name for p in out]
            self.assertEqual(names.count("topic.md"), 1, "중복 dir 여도 .md 는 한 번만 방출돼야")


if __name__ == "__main__":
    unittest.main()
