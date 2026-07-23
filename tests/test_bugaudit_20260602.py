"""bug-audit 2026-06-02 회귀 가드 — 전체 시스템 적대적 점검 R1 에서 확인·수정한 결함들.

각 테스트는 수정 전 코드에서 FAIL, 수정 후 PASS 하도록 설계. 발견 번호(#N)는
점검 워크플로 confirmed findings 와 대응.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class TestEmbedTextNonFiniteGuard(unittest.TestCase):
    """#1 임베딩 서버가 토큰 한도 초과로 all-NaN 벡터를 200 으로 반환해도,
    embed_text 가 NaN/Inf 벡터를 거부(None)해 embed_cache/memories_vec 에
    영구 저장되지 않아야 한다."""

    def test_nan_vector_rejected(self):
        from memory_indexer import embed_text
        with patch("memory_indexer._embed_cache_get", return_value=None), \
             patch("memory_indexer._embed_cache_put") as mock_put, \
             patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            # NaN 은 json.dumps/loads 를 그대로 왕복한다 (allow_nan 기본 True).
            mock_resp.read.return_value = json.dumps(
                {"vector": [float("nan")] * 1024}
            ).encode()
            self.assertIsNone(embed_text("hello"))
            mock_put.assert_not_called()  # NaN 은 절대 캐시 저장 금지

    def test_inf_vector_rejected(self):
        from memory_indexer import embed_text
        with patch("memory_indexer._embed_cache_get", return_value=None), \
             patch("memory_indexer._embed_cache_put") as mock_put, \
             patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            vec = [0.1] * 1024
            vec[7] = float("inf")
            mock_resp.read.return_value = json.dumps({"vector": vec}).encode()
            self.assertIsNone(embed_text("hello"))
            mock_put.assert_not_called()

    def test_finite_vector_still_accepted(self):
        from memory_indexer import embed_text
        with patch("memory_indexer._embed_cache_get", return_value=None), \
             patch("memory_indexer._embed_cache_put"), \
             patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            mock_resp.read.return_value = json.dumps({"vector": [0.1] * 1024}).encode()
            vec = embed_text("hello")
            self.assertEqual(len(vec), 1024)


class _MockRow:
    def __init__(self, d):
        self._d = d
    def __getitem__(self, k):
        return self._d[k]


class _MockConn:
    def __init__(self, rows):
        self._rows = rows
    def execute(self, _q, *a):
        rows = self._rows
        class _C:
            def __iter__(self):
                return iter(rows)
            def fetchall(self):
                return list(rows)
        return _C()


class TestVecRowCorruptionResilience(unittest.TestCase):
    """#4/#8/#15 — 4의 배수가 아닌 손상 blob(또는 차원 불일치 row) 한 건이
    np.frombuffer/matmul ValueError 로 전체 검색을 0건/None 으로 만들지 않아야 한다.
    손상 행만 skip 하고 나머지 유효 행으로 계속 검색."""

    def _vecs(self, seed=7):
        import numpy as np
        rng = np.random.RandomState(seed)
        return (rng.rand(1024).astype("float32"), rng.rand(1024).astype("float32"))

    def test_search_vec_candidates_skips_corrupt_blob(self):
        import search
        import sqlite3
        v0, v1 = self._vecs()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE sessions_vec (session_id TEXT, embedding BLOB)")
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT, first_ts TEXT, last_ts TEXT, turn_count INT)"
        )
        conn.executemany(
            "INSERT INTO sessions_vec(session_id, embedding) VALUES (?,?)",
            [
                ("s0", v0.tobytes()),
                ("s_bad", b"\x00\x01\x02"),  # 3바이트 (비-4배수 손상)
                ("s1", v1.tobytes()),
            ],
        )
        conn.executemany(
            "INSERT INTO sessions(session_id, first_ts, last_ts, turn_count) VALUES (?,?,?,?)",
            [("s0", "t", "t", 1), ("s1", "t", "t", 1)],
        )
        conn.commit()
        results, raw_map = search.vec_candidates(conn, v0.tolist())
        conn.close()
        # 손상 행에도 불구하고 유효 행으로 검색 성공해야 한다.
        self.assertTrue(results)
        self.assertIn("s0", raw_map)
        self.assertNotIn("s_bad", raw_map)

    def test_memory_search_vec_top_k_skips_corrupt_blob(self):
        from memory_search import _vec_top_k
        v0, v1 = self._vecs()
        rows = [
            _MockRow({"path": "/p0", "kind": "body", "embedding": v0.tobytes()}),
            _MockRow({"path": "/p_bad", "kind": "body", "embedding": b"\x01\x02\x03\x04\x05"}),  # 5바이트
            _MockRow({"path": "/p1", "kind": "body", "embedding": v1.tobytes()}),
        ]
        conn = _MockConn(rows)
        results, raw_map = _vec_top_k(conn, v0.tolist(), limit=3)
        self.assertTrue(results)
        self.assertIn("/p0", raw_map)
        self.assertNotIn("/p_bad", raw_map)

    def test_memory_compiler_embedding_skips_dim_mismatch_and_corrupt(self):
        import memory_compiler
        import memory_indexer
        import indexer
        import numpy as np
        import sqlite3
        d = Path(tempfile.mkdtemp())
        (d / "topic_one.md").write_text(
            "---\nname: claude-bg-syntax\ndescription: x\n---\nclaude --bg 명령 사용법",
            encoding="utf-8",
        )
        vec = (np.ones(1024, dtype=np.float32) / np.float32(np.sqrt(1024))).astype("float32")
        path_str = str((d / "topic_one.md").resolve())
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE memories_vec (path TEXT, kind TEXT, embedding BLOB)")
        conn.executemany(
            "INSERT INTO memories_vec(path, kind, embedding) VALUES (?,?,?)",
            [
                ("/stale_768", "passage", np.ones(768, dtype=np.float32).tobytes()),  # 차원 불일치
                ("/corrupt", "passage", b"\x00\x01\x02"),  # 비-4배수
                (path_str, "passage", vec.tobytes()),  # 유효 매칭
            ],
        )
        conn.commit()
        cand = {"title": "백그라운드 세션 시작", "body": "claude --bg 으로 백그라운드 실행"}
        with patch.object(memory_indexer, "embed_text", lambda text, kind="passage": vec.tolist()), \
             patch.object(indexer, "open_db", lambda: conn):
            try:
                out = memory_compiler._find_existing_memory(cand, [d])
            finally:
                conn.close()
        # stale/corrupt row 가 스캔을 중단시키지 않고 유효 매칭을 찾아야 한다.
        self.assertIsNotNone(out)
        self.assertEqual(out["path"].name, "topic_one.md")


def _fake_embed(_text, kind="passage"):
    return [0.5] * 1024


class _AliasRecallBase(unittest.TestCase):
    """fixtures/memory 를 tmp 에 복사·인덱싱한 실 DB 하니스 (TestRecallMemory 와 동형)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self.tmp_dir.name) / "test.db"
        src_fixture = Path(__file__).parent / "fixtures" / "memory"
        self.fixture_dir = Path(self.tmp_dir.name) / "memory"
        shutil.copytree(src_fixture, self.fixture_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _index(self, embed=_fake_embed):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=embed):
            incremental_index([self.fixture_dir], db_path=self.tmp_db)


class TestAliasIndexDictGuard(_AliasRecallBase):
    """#10 — alias_index.json 이 비-dict valid JSON 이면 recall 전체가
    AttributeError 로 죽지 않고 graceful 빈 결과여야 한다."""

    def test_non_dict_alias_index_does_not_crash_recall(self):
        import memory_search
        bad = Path(self.tmp_dir.name) / "alias_index.json"
        bad.write_text(json.dumps(["a", "b", "c"]))  # truthy 비-dict
        with patch("memory_search.ALIAS_INDEX_PATH", bad), \
             patch("memory_search._ALIAS_INDEX_CACHE", None), \
             patch("memory_search._ALIAS_INDEX_MTIME", 0.0):
            # _alias_boost_paths 가 idx.items() 에서 터지지 않아야 한다.
            paths = memory_search._alias_boost_paths("아무 토큰 쿼리")
            self.assertEqual(paths, set())


class TestAliasGhostFilter(_AliasRecallBase):
    """#11 — alias_index 에만 있고 memories 테이블엔 없는 stale path 는
    빈 name/desc 의 ghost 결과(score 1.0)로 주입되면 안 된다."""

    def test_stale_alias_path_produces_no_ghost(self):
        from memory_search import recall_memory
        self._index()
        ghost = str(self.fixture_dir / "deleted_or_moved_memory.md")  # DB 에 없음
        with patch("memory_search.embed_text", return_value=None), \
             patch("memory_search.DB_PATH", self.tmp_db), \
             patch("memory_search._alias_boost_paths", return_value={ghost}):
            results = recall_memory(
                "zzzqqq 전혀무관 외계어 토큰",
                top_k=3,
                score_threshold=0.50,
                db_path=self.tmp_db,
            )
        paths = [r["path"] for r in results]
        self.assertNotIn(ghost, paths, "stale alias path 가 ghost 로 회수됨")
        # 빈 name 결과(ghost 흔적)가 없어야 한다.
        self.assertTrue(all(r["name"] for r in results), "빈 name ghost 결과 존재")


class TestAliasBoostNestedTypeGuard(_AliasRecallBase):
    """#10/codex R2 — alias_index 의 per-entry 값이 비-dict 거나 alias 가 비-str 여도
    _alias_boost_paths 가 AttributeError 없이 graceful 동작."""

    def test_malformed_entries_no_crash(self):
        import memory_search
        bad = Path(self.tmp_dir.name) / "alias_index.json"
        bad.write_text(json.dumps({
            "/a.md": [],                       # 비-dict 값
            "/b.md": {"aliases": [42, "정상토큰"]},  # 비-str alias 혼재
            "/c.md": {"aliases": ["프린터재시작"]},
            "/d.md": {"aliases": 42},          # R5: 비-iterable aliases (for 에서 TypeError)
            "/e.md": {"aliases": "문자열"},     # str 도 iterable 이지만 글자단위 분해 방지
        }))
        with patch("memory_search.ALIAS_INDEX_PATH", bad), \
             patch("memory_search._ALIAS_INDEX_CACHE", None), \
             patch("memory_search._ALIAS_INDEX_MTIME", 0.0):
            # 크래시 없이 set 반환 (정상 항목만 매칭).
            paths = memory_search._alias_boost_paths("프린터재시작 절차")
            self.assertIsInstance(paths, set)


class TestVecReadNonFiniteSkip(unittest.TestCase):
    """codex R2 — memories_vec/sessions_vec 에 pre-existing NaN 행이 있어도
    cosine 순위를 오염시키지 않고 skip 되어야 한다 (read-side resilience)."""

    def _vecs(self, seed=11):
        import numpy as np
        rng = np.random.RandomState(seed)
        return rng.rand(1024).astype("float32"), rng.rand(1024).astype("float32")

    def test_memory_search_skips_nan_row(self):
        import numpy as np
        from memory_search import _vec_top_k
        v0, v1 = self._vecs()
        nan_vec = np.full(1024, np.nan, dtype=np.float32)
        rows = [
            _MockRow({"path": "/p0", "kind": "body", "embedding": v0.tobytes()}),
            _MockRow({"path": "/p_nan", "kind": "body", "embedding": nan_vec.tobytes()}),
            _MockRow({"path": "/p1", "kind": "body", "embedding": v1.tobytes()}),
        ]
        results, raw_map = _vec_top_k(_MockConn(rows), v0.tolist(), limit=3)
        self.assertIn("/p0", raw_map)
        self.assertNotIn("/p_nan", raw_map)
        # NaN 이 순위를 오염시키지 않아 raw 값이 모두 유한해야 한다.
        import math
        self.assertTrue(all(math.isfinite(v) for v in raw_map.values()))


class TestEmbedCacheInvalidGuard(unittest.TestCase):
    """codex R2 — embed_cache 에 NaN/손상 벡터가 있어도 _embed_cache_get 이
    크래시 없이 None(cache miss) 반환."""

    def test_nan_and_corrupt_cache_return_none(self):
        import numpy as np
        import memory_indexer as mi
        # NaN 캐시
        with patch.object(mi.sqlite3, "connect") as mc:
            nan_blob = np.full(1024, np.nan, dtype=np.float32).tobytes()
            mc.return_value.execute.return_value.fetchone.return_value = (nan_blob,)
            self.assertIsNone(mi._embed_cache_get("q", "passage"))
        # 비-4배수 손상 blob
        with patch.object(mi.sqlite3, "connect") as mc:
            mc.return_value.execute.return_value.fetchone.return_value = (b"\x00\x01\x02",)
            self.assertIsNone(mi._embed_cache_get("q", "passage"))
        # R5: vector 컬럼에 TEXT/str 값 (SQLite 타입 비-strict) → frombuffer TypeError
        with patch.object(mi.sqlite3, "connect") as mc:
            mc.return_value.execute.return_value.fetchone.return_value = ("corrupt-text-value",)
            self.assertIsNone(mi._embed_cache_get("q", "passage"))


class TestReverifyFlockUnsupportedFs(unittest.TestCase):
    """codex R2 — flock 미지원 FS(ENOLCK 등)에서 reverify 가 영구 skip 하지 않고
    단독 스캔으로 폴백."""

    def test_enolck_falls_back_to_scan(self):
        import errno
        import reverify
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memdir = Path(tmp.name) / "memory"
        memdir.mkdir()
        sidecar = Path(tmp.name) / "reverify_state.json"
        scanned = {"ran": False}

        def fake_scan(_d):
            scanned["ran"] = True
            return {"scanned": 0}

        def fake_flock(fd, op):
            raise OSError(errno.ENOLCK, "no locks available")

        with patch.object(reverify, "_sidecar_path", lambda: sidecar), \
             patch.object(reverify, "scan_memories", fake_scan), \
             patch("fcntl.flock", fake_flock):
            result = reverify.maybe_scan_due(memdir)
        self.assertTrue(scanned["ran"], "flock 미지원 FS 에서 스캔이 영구 skip 됨")
        self.assertIsNotNone(result)


class TestAliasMtimeIncremental(unittest.TestCase):
    """#12 — 내용이 바뀐(mtime 변경) 메모리는 alias 가 재생성돼야 한다."""

    def test_mtime_change_triggers_regen(self):
        import alias_generator as ag
        import os as _os
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memdir = Path(tmp.name) / "memory"
        memdir.mkdir()
        mf = memdir / "status.md"
        mf.write_text("---\nname: status\ndescription: v1\n---\n본문 v1", encoding="utf-8")
        idx = Path(tmp.name) / "alias_index.json"
        calls = []

        def fake_gemma(desc, body):
            calls.append(desc)
            return ["alias-" + str(len(calls))]

        with patch.object(ag, "INDEX_PATH", idx), \
             patch.object(ag, "discover_memory_dirs", lambda: [memdir]), \
             patch.object(ag, "_call_gemma", fake_gemma):
            s1 = ag.generate(provider="gemma")
            self.assertEqual(s1["generated"], 1)
            # 변경 없음 → skip (mtime 일치)
            s2 = ag.generate(provider="gemma")
            self.assertEqual(s2["generated"], 0)
            self.assertEqual(s2["skipped"], 1)
            # 내용·mtime 변경 → 재생성
            mt = mf.stat().st_mtime_ns
            mf.write_text("---\nname: status\ndescription: v2\n---\n본문 v2 변경됨", encoding="utf-8")
            _os.utime(mf, ns=(mt + 1_000_000_000, mt + 1_000_000_000))
            s3 = ag.generate(provider="gemma")
            self.assertEqual(s3["generated"], 1, "내용 변경된 메모리가 재생성 안 됨 (stale alias)")

    def test_non_dict_entry_does_not_crash(self):
        # codex R2: 손상된 비-dict per-entry 값에 .get() AttributeError 없이 재생성.
        import alias_generator as ag
        import json as _json
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memdir = Path(tmp.name) / "memory"
        memdir.mkdir()
        mf = memdir / "status.md"
        mf.write_text("---\nname: status\ndescription: v1\n---\n본문", encoding="utf-8")
        idx = Path(tmp.name) / "alias_index.json"
        idx.write_text(_json.dumps({str(mf): []}))  # 손상 엔트리 (비-dict)
        with patch.object(ag, "INDEX_PATH", idx), \
             patch.object(ag, "discover_memory_dirs", lambda: [memdir]), \
             patch.object(ag, "_call_gemma", lambda d, b: ["alias-1"]):
            s = ag.generate(provider="gemma")  # 크래시 없이 완료
        self.assertEqual(s["generated"], 1, "비-dict 손상 엔트리가 재생성으로 교정 안 됨")
        fixed = _json.loads(idx.read_text())
        self.assertIsInstance(fixed[str(mf)], dict)
        self.assertIn("mtime_ns", fixed[str(mf)])


class TestAliasBomTolerance(unittest.TestCase):
    """#13 — 선두 BOM 메모리도 alias 메타 추출 성공해야 한다 (인덱서/회수와 일관)."""

    def test_bom_prefixed_memory_parsed(self):
        import alias_generator as ag
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "bom.md"
        p.write_text(
            "﻿---\nname: foo\ndescription: bar baz\n---\n\nbody",
            encoding="utf-8",
        )
        meta = ag._extract_memory_meta(p)
        self.assertIsNotNone(meta, "BOM 메모리가 alias 메타 추출에서 거부됨")
        self.assertEqual(meta[0], "foo")


class TestQueryIntentNonDictGemma(unittest.TestCase):
    """#21 — Gemma intent 응답이 비-dict valid JSON 이어도 _call_gemma_intent 가
    AttributeError 없이 None 을 반환해야 한다 (hook 핫패스 보호)."""

    def test_non_dict_json_bodies_return_none(self):
        import query_intent
        for payload in (
            b"[]", b"42", b'"error"', b"null", b'{"choices":["s"]}',
            # R3: choices 가 truthy 비-list(dict/int)인 변종도 raise 없이 None.
            b'{"choices": {"x":1}}', b'{"choices": 123}',
            b'{"choices": {"message":"oops"}}',
        ):
            with patch("query_intent.urllib.request.urlopen") as mo:
                mo.return_value.__enter__.return_value.read.return_value = payload
                # AttributeError/TypeError/KeyError 가 전파되면 테스트 실패. None 이어야 한다.
                self.assertIsNone(query_intent._call_gemma_intent("짧은쿼리"), payload)


class TestMetaSelfRefNotMidSentence(unittest.TestCase):
    """#22 — 'claude code'·'현재 세션' 등 모호 토큰이 긴 작업 쿼리 중간에 있으면
    meta 로 오분류돼 회수가 silent 차단되면 안 된다. 짧은 메타 질의는 그대로 meta."""

    def test_long_work_queries_not_meta(self):
        from query_intent import classify, should_skip_recall
        for q in (
            "claude code 로 만든 프로젝트의 매출 추이 분석해줘",
            "현재 세션 동안 진행한 youtube 작업 요약해줘",
            # codex R2: 4단어 작업 쿼리도 meta 가 아니어야 한다 (≤3 으로 강화).
            "claude code 프로젝트 분석",
            "현재 세션 작업 요약",
        ):
            r = classify(q)
            self.assertNotEqual(r.intent, "meta", f"{q!r} → {r}")
            self.assertFalse(should_skip_recall(r), f"{q!r} 회수 차단됨")

    def test_short_meta_still_classified(self):
        from query_intent import classify
        # 기존 test_meta 가 못박은 짧은 메타는 유지.
        self.assertEqual(classify("현재 세션 정보").intent, "meta")
        self.assertEqual(classify("claude code 버전이 뭐야").intent, "meta")


class TestWriteStagedPidUniqueTmp(unittest.TestCase):
    """#6 — write_staged 의 tmp 파일이 PID-고유여야 동시 same-slug SessionEnd 에서
    lost update 가 안 난다 (reverify/alias_generator 와 동일 패턴)."""

    def test_tmp_name_is_pid_unique(self):
        import inspect
        import session_memory_end
        src = inspect.getsource(session_memory_end.write_staged)
        self.assertIn("getpid()", src, "write_staged tmp 가 PID-고유가 아님")
        self.assertNotIn('path.with_suffix(path.suffix + ".tmp")', src)


class TestRecallCliExemptsRawGate(unittest.TestCase):
    """#9 — /recall 명시 검색은 raw_cosine_min=0.0 으로 raw 게이트도 면제해야 한다."""

    def test_search_memory_passes_raw_cosine_min_zero(self):
        import memory_search
        import recall_cli
        captured = {}

        def fake_recall(query, **kw):
            captured.update(kw)
            return []

        with patch.object(memory_search, "recall_memory", fake_recall):
            recall_cli._search_memory("아무 질의")
        self.assertEqual(captured.get("raw_cosine_min"), 0.0)
        self.assertEqual(captured.get("score_threshold"), 0.0)


class TestCompilerEmptyTitleGuard(unittest.TestCase):
    """#16 — 빈/공백 title 후보는 slug 'memory' 폴백으로 오매칭되지 않고 None."""

    def test_blank_title_returns_none(self):
        import memory_compiler
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (d / "memory.md").write_text(
            "---\nname: real\ndescription: z\n---\n본문", encoding="utf-8"
        )
        self.assertIsNone(memory_compiler._find_existing_memory({"title": "   "}, [d]))
        self.assertIsNone(memory_compiler._find_existing_memory({"title": ""}, [d]))

    def test_punctuation_only_title_not_matched_to_memory_md(self):
        # codex R2 (#16 완성): 구두점-only title 은 slugify 'memory' 폴백으로
        # memory.md 와 slug 오매칭되면 안 된다.
        import memory_compiler
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (d / "memory.md").write_text(
            "---\nname: real\ndescription: z\n---\n본문", encoding="utf-8"
        )
        res = memory_compiler._find_existing_memory({"title": "!!!"}, [d])
        self.assertFalse(
            res is not None and res["path"].name == "memory.md",
            "구두점-only title 이 memory.md 와 slug 오매칭됨",
        )


class TestPurgeStagedBothDirsAndOverride(unittest.TestCase):
    """#17/#18 — 자동 staged 청소가 MV3_MEMORY_DIR 를 honor 하고 _staged +
    _procedural/_staged 양쪽을 청소한다."""

    def test_purges_both_staged_dirs_under_override(self):
        import os as _os
        import session_memory
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mem = Path(tmp.name) / "memory"
        staged = mem / "_staged"
        proc_staged = mem / "_procedural" / "_staged"
        staged.mkdir(parents=True)
        proc_staged.mkdir(parents=True)
        old = time.time() - 40 * 86400
        f1 = staged / "old1.md"
        f1.write_text("x")
        _os.utime(f1, (old, old))
        f2 = proc_staged / "old2.md"
        f2.write_text("x")
        _os.utime(f2, (old, old))
        fresh = staged / "fresh.md"
        fresh.write_text("x")  # 최근 → 보존
        with patch.dict(_os.environ, {"MV3_MEMORY_DIR": str(mem)}, clear=False):
            session_memory.purge_staged_memory()
        self.assertFalse(f1.exists(), "_staged 오래된 파일 미청소")
        self.assertFalse(f2.exists(), "_procedural/_staged 오래된 파일 미청소")
        self.assertTrue(fresh.exists(), "최근 파일이 잘못 청소됨")


class TestProvenanceBackfillNoDupRef(unittest.TestCase):
    """#25 — source_ref 가 이미 있고 source_type 만 없는 파일에 source_ref 가
    중복 주입돼 원본 출처가 덮어쓰이면 안 된다."""

    def test_existing_source_ref_preserved(self):
        import provenance_backfill_cli as pb
        from memory_indexer import parse_frontmatter
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        p = d / "legacy.md"
        p.write_text(
            "---\nname: x\ndescription: y\nsource_ref: existing-ref-value\n"
            "staged_from_session: abcd1234\n---\n본문",
            encoding="utf-8",
        )
        self.assertTrue(pb.backfill_file(p, dry_run=False))
        text = p.read_text(encoding="utf-8")
        self.assertEqual(text.count("source_ref:"), 1, "source_ref 중복 주입")
        fm, _ = parse_frontmatter(text)
        self.assertEqual(fm.get("source_ref"), "existing-ref-value", "원본 출처 덮어쓰임")
        self.assertEqual(fm.get("source_type"), "session")


class TestContradictionUnclosedFlowListRefused(unittest.TestCase):
    """#20 — 미닫힌 flow-list(`key: [a, b`)는 mutate 거부(중복 키 append 금지)."""

    def test_unclosed_flow_list_not_duplicated(self):
        import contradiction_review_cli as cc
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        p = d / "old.md"
        p.write_text("---\nname: old\ndeprecated_by: [x, y\n---\n본문", encoding="utf-8")
        # 거부돼야 한다 (return False), 파일 변경 없음.
        self.assertFalse(cc._patch_frontmatter_list(p, "deprecated_by", "z"))
        self.assertFalse(cc._can_patch_frontmatter_list(p, "deprecated_by"))
        text = p.read_text(encoding="utf-8")
        self.assertEqual(text.count("deprecated_by:"), 1, "중복 키 append (YAML 손상)")

    def test_valid_flow_list_still_patches(self):
        import contradiction_review_cli as cc
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        p = d / "old.md"
        p.write_text("---\nname: old\ndeprecated_by: [x]\n---\n본문", encoding="utf-8")
        self.assertTrue(cc._patch_frontmatter_list(p, "deprecated_by", "z"))
        text = p.read_text(encoding="utf-8")
        self.assertEqual(text.count("deprecated_by:"), 1)
        self.assertIn("z", text)


class TestExtractorStatsRobustness(unittest.TestCase):
    """#23/#24 — no-trigger SessionEnd 이중카운트 제거 + 손상 타임스탬프 graceful."""

    def _run(self, log_text):
        import extractor_stats_cli as esc
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        log = d / "debug.log"
        log.write_text(log_text)
        orig = esc.DEBUG_LOG
        esc.DEBUG_LOG = log
        try:
            return esc.parse_debug(None)
        finally:
            esc.DEBUG_LOG = orig

    def test_no_trigger_session_counted_once(self):
        # 한 no-trigger SessionEnd = no-trigger 라인 + no-candidates 라인.
        log = (
            "[2026-06-02 10:00:00] extractor: no trigger in foo-sid.jsonl, skip\n"
            "[2026-06-02 10:00:00] session-end: no candidates for a2522ffa\n"
        )
        d = self._run(log)
        self.assertEqual(d["session_end_total"], 1, "no-trigger SessionEnd 이중 카운트")

    def test_corrupt_timestamp_line_skipped(self):
        log = (
            "[2026-13-45 99:99:99] extractor: no trigger in bad.jsonl, skip\n"
            "[2026-06-02 10:00:00] session-end: no candidates for a2522ffa\n"
        )
        d = self._run(log)  # ValueError 로 죽지 않아야 한다
        self.assertEqual(d["session_end_total"], 1)


class TestReverifyScanLockSerializes(unittest.TestCase):
    """#26 — maybe_scan_due 가 flock 으로 직렬화: 락이 이미 잡혀 있으면 skip(None)."""

    def test_held_lock_causes_skip(self):
        import fcntl
        import reverify
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memdir = Path(tmp.name) / "memory"
        memdir.mkdir()
        sidecar = Path(tmp.name) / "reverify_state.json"
        lock_path = sidecar.with_suffix(".lock")
        with patch.object(reverify, "_sidecar_path", lambda: sidecar):
            # 다른 holder 가 락을 잡고 있는 상황 시뮬레이션 (별도 open file description).
            held = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                # sidecar 없음 → due 판정이지만 락 점유 중이라 skip 되어야 한다.
                result = reverify.maybe_scan_due(memdir)
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                os.close(held)
        self.assertIsNone(result, "락 점유 중인데도 중복 스캔 진입")


class TestUninstallGitUnwireAndDriftHook(unittest.TestCase):
    """#2/#7 — uninstall.sh 가 (a) git core.hooksPath 를 unset 하고 .repo-path 를
    제거하며(다음 커밋의 post-commit 자동 재배포 차단), (b) deploy_drift_check.py
    SessionStart 훅 잔재를 settings.json 에서 제거한다."""

    REPO_DIR = Path(__file__).resolve().parents[1]

    def test_uninstall_unwires_git_and_removes_drift_hook(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        # 가짜 deploy repo (git checkout) — core.hooksPath=.githooks 설정.
        repo = Path(tmp.name) / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".githooks"], check=True)
        # 배포 상태 구성
        mv3 = home / ".claude" / "mindvault-v3"
        scripts = home / ".claude" / "scripts" / "mindvault"
        mv3.mkdir(parents=True)
        scripts.mkdir(parents=True)
        (scripts / "deploy_drift_check.py").write_text("# drift\n")
        (mv3 / ".repo-path").write_text(str(repo) + "\n")
        settings = home / ".claude" / "settings.json"
        settings.write_text(json.dumps({
            "hooks": {
                "SessionStart": [
                    {"matcher": "*", "hooks": [
                        {"type": "command", "command": str(scripts / "deploy_drift_check.py")},
                    ]},
                ],
                "UserPromptSubmit": [
                    {"matcher": "*", "hooks": [
                        {"type": "command", "command": "/keep/telegram-guard.sh"},
                    ]},
                ],
            }
        }, indent=2))
        # conftest 가 os.environ 에 박은 MV3_* (특히 MV3_SCRIPTS_DIR=tmp) 를 제거 —
        # 그대로 상속되면 uninstall 의 SCRIPTS_DIR 가 fake HOME 과 어긋나 drift 경로
        # 매칭이 깨진다. 실제 사용자 환경(MV3_SCRIPTS_DIR 미설정)을 재현.
        env = {k: v for k, v in os.environ.items() if not k.startswith("MV3_")}
        env.update({
            "HOME": str(home),
            "MV3_LAUNCH_AGENTS": str(Path(tmp.name) / "LaunchAgents"),
            "MV3_GEMMA_CACHE": str(Path(tmp.name) / "gemma-cache"),
            "MV3_UNINSTALL_DRY_LAUNCHCTL": "1",
        })
        r = subprocess.run(
            ["bash", str(self.REPO_DIR / "uninstall.sh")],
            capture_output=True, env=env,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode())
        # #2: git core.hooksPath unset + .repo-path 제거
        hp = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
            capture_output=True,
        )
        self.assertNotEqual(hp.stdout.decode().strip(), ".githooks", "core.hooksPath 미해제")
        self.assertFalse((mv3 / ".repo-path").exists(), ".repo-path 마커 미제거")
        # #7: settings.json 의 drift SessionStart 훅 제거 (telegram-guard 는 보존)
        data = json.loads(settings.read_text())
        ss = data.get("hooks", {}).get("SessionStart", [])
        cmds = [h.get("command", "") for e in ss for h in e.get("hooks", [])]
        self.assertFalse(any("deploy_drift_check.py" in c for c in cmds), "drift 훅 잔재")
        ups = [h.get("command", "") for e in data["hooks"].get("UserPromptSubmit", []) for h in e.get("hooks", [])]
        self.assertTrue(any("telegram-guard" in c for c in ups), "무관 훅이 잘못 제거됨")

    def _clean_env(self, home, tmpname, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("MV3_")}
        env.update({
            "HOME": str(home),
            "MV3_LAUNCH_AGENTS": str(Path(tmpname) / "LaunchAgents"),
            "MV3_GEMMA_CACHE": str(Path(tmpname) / "gemma-cache"),
            "MV3_UNINSTALL_DRY_LAUNCHCTL": "1",
        })
        env.update(extra)
        return env

    def test_uninstall_does_not_delete_cwd_drift_file(self):
        """R3: bare 'deploy_drift_check.py' 가 rm 루프에 들어가 cwd 동명 파일을
        삭제하면 안 된다 (settings 매칭 전용으로 분리)."""
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        (home / ".claude").mkdir(parents=True)
        # uninstall 을 실행할 cwd 에 동명 파일 배치 (repo scripts/ 시나리오 모사).
        cwd = Path(tmp.name) / "scripts"
        cwd.mkdir()
        victim = cwd / "deploy_drift_check.py"
        victim.write_text("# real source — must survive\n")
        env = self._clean_env(home, tmp.name)
        r = subprocess.run(
            ["bash", str(self.REPO_DIR / "uninstall.sh")],
            capture_output=True, env=env, cwd=str(cwd),
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode())
        self.assertTrue(victim.exists(), "uninstall 이 cwd 의 deploy_drift_check.py 를 삭제함 (footgun)")

    def test_uninstall_restores_prior_hookspath(self):
        """R3: install 이전 사용자 custom core.hooksPath 가 기록돼 있으면 uninstall 이
        unset 이 아니라 복원해야 한다."""
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        repo = Path(tmp.name) / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        # install 이 .githooks 로 clobber 한 상태 + prior 값 기록돼 있음.
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ".githooks"], check=True)
        mv3 = home / ".claude" / "mindvault-v3"
        mv3.mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
        (mv3 / ".repo-path").write_text(str(repo) + "\n")
        (mv3 / ".prior-hookspath").write_text(".myhooks\n")  # 사용자 원래 값
        env = self._clean_env(home, tmp.name)
        r = subprocess.run(
            ["bash", str(self.REPO_DIR / "uninstall.sh")],
            capture_output=True, env=env,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode())
        hp = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
            capture_output=True,
        )
        self.assertEqual(hp.stdout.decode().strip(), ".myhooks", "원래 hooksPath 복원 실패")
        self.assertFalse((mv3 / ".prior-hookspath").exists(), ".prior-hookspath 마커 미제거")


class TestExtractorMalformedJson(unittest.TestCase):
    """#5 — malformed-but-recoverable Gemma JSON(끝쉼표)은 복구되고, 복구
    불가한 malformed 는 negative-cache 되지 않아야 한다(후보 영구 유실 방지)."""

    def test_trailing_comma_recovered(self):
        from memory_extractor import parse_gemma_json
        out = ('[{"type":"feedback","title":"T","body":"B",'
               '"reason":"r","evidence":"e"},]')  # 끝쉼표
        cands = parse_gemma_json(out)
        self.assertEqual(len(cands), 1, "끝쉼표 JSON 복구 실패")
        self.assertEqual(cands[0]["title"], "T")

    def _run(self, gemma_return):
        import memory_extractor as me
        import extractor_cache
        from unittest.mock import MagicMock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        jsonl = Path(tmp.name) / "sess.jsonl"
        jsonl.write_text(
            json.dumps({"type": "user", "message": {"content": "이건 영구 기억해줘: 커밋은 논리 단위로 분리"}, "timestamp": "2026-01-01T00:00:00Z"}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": "알겠습니다."}, "timestamp": "2026-01-01T00:00:01Z"}) + "\n"
        )
        put = MagicMock()
        with patch.object(me, "_always_fire", return_value=True), \
             patch.object(me, "call_gemma", return_value=gemma_return), \
             patch.object(extractor_cache, "cache_get", return_value=None), \
             patch.object(extractor_cache, "cache_put", put):
            out = me.extract_from_jsonl(jsonl)
        return out, put

    def test_unrecoverable_malformed_not_negative_cached(self):
        # 작은따옴표 JSON — 복구 불가. 후보가 있었지만 파싱 실패.
        out, put = self._run("[{'type':'feedback','title':'t','body':'b'}]")
        self.assertEqual(out, [])
        put.assert_not_called()  # malformed → negative-cache 회피

    def test_recoverable_trailing_comma_cached(self):
        valid = ('[{"type":"feedback","title":"커밋 분리","body":"커밋은 논리 단위로 분리",'
                 '"reason":"r","evidence":"e"},]')  # 끝쉼표 (복구 가능)
        out, put = self._run(valid)
        self.assertEqual(len(out), 1)
        put.assert_called_once()

    def test_legit_empty_still_cached(self):
        # 회귀: 진짜 빈 배열은 여전히 캐시된다(retry 비용 회피).
        out, put = self._run("[]")
        self.assertEqual(out, [])
        put.assert_called_once()

    def test_body_internal_comma_preserved(self):
        # codex R2: string-aware 복구 — body 안 '[..,]' 의 쉼표를 삭제하지 않아야 한다.
        from memory_extractor import parse_gemma_json
        out = ('[{"type":"project","title":"x",'
               '"body":"순서는 [빌드, 테스트, 배포,] 로 한다"},]')  # 끝쉼표 + body 내부 ',]'
        c = parse_gemma_json(out)
        self.assertEqual(len(c), 1, "끝쉼표 복구 실패")
        self.assertEqual(c[0]["body"], "순서는 [빌드, 테스트, 배포,] 로 한다", "body 내부 쉼표 손상")

    def test_prose_preamble_with_trailing_comma_recovered(self):
        # R4: 산문 머리말 + 끝쉼표 조합도 balanced 경로에서 복구돼야 한다(recall miss 차단).
        from memory_extractor import _parse_gemma_json_ex
        out = '결과:\n[{"type":"project","title":"t","body":"b",}]'
        cands, failed = _parse_gemma_json_ex(out)
        self.assertEqual(len(cands), 1, "산문+끝쉼표 복구 실패 (recall miss)")
        self.assertFalse(failed)

    def test_prose_fake_brackets_not_fabricated(self):
        # R4 가드: 산문 안 가짜 대괄호는 후보로 날조되면 안 된다 (over-recover 방지).
        from memory_extractor import _parse_gemma_json_ex
        cands, failed = _parse_gemma_json_ex("항목들 [a, b,] 입니다")
        self.assertEqual(len(cands), 0, "산문 대괄호에서 가짜 후보 날조")


class TestVecAvailableCapturedBeforeAlias(_AliasRecallBase):
    """codex R2 — vec 서버 다운(embed None) + alias 매칭이 동시에 있어도,
    vec_available 가 alias sentinel 로 오염되지 않아 fts-only fallback 게이트 면제가
    유지되어야 한다 (사용자 키워드 메모리 회수 보존)."""

    def test_fts_fallback_survives_with_alias_when_vec_down(self):
        from memory_search import recall_memory
        self._index()
        alias_hit = str(self.fixture_dir / "feedback_test_html.md")  # DB 실재
        with patch("memory_search.embed_text", return_value=None), \
             patch("memory_search.DB_PATH", self.tmp_db), \
             patch("memory_search._alias_boost_paths", return_value={alias_hit}):
            results = recall_memory(
                "메일",  # test-mail 의 fts 매칭
                top_k=5,
                score_threshold=0.0,
                db_path=self.tmp_db,
            )
        names = [r["name"] for r in results]
        self.assertIn("test-mail", names,
                      "vec 다운+alias 시 fts fallback 이 게이트로 차단됨 (vec_available 오염)")


class TestAliasGeneratorTopLevelNonDict(unittest.TestCase):
    """codex R2 (#10 완성) — 비-dict alias_index.json 이어도 generate() 가
    크래시 없이 빈 dict 로 취급해 재생성한다."""

    def test_array_index_no_crash(self):
        import alias_generator as ag
        import json as _json
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        memdir = Path(tmp.name) / "memory"
        memdir.mkdir()
        (memdir / "m.md").write_text("---\nname: m\ndescription: d\n---\n본문", encoding="utf-8")
        idx = Path(tmp.name) / "alias_index.json"
        idx.write_text(_json.dumps(["a", "b"]))  # top-level 배열 (비-dict)
        with patch.object(ag, "INDEX_PATH", idx), \
             patch.object(ag, "discover_memory_dirs", lambda: [memdir]), \
             patch.object(ag, "_call_gemma", lambda d, b: ["alias-1"]):
            s = ag.generate(provider="gemma")  # 크래시 없이 완료
        self.assertEqual(s["generated"], 1)


class TestVecCandidatesInfQueryGuard(unittest.TestCase):
    """codex R2 — search.vec_candidates 가 비유한(inf) 쿼리 벡터에 빈 결과 반환."""

    def test_inf_query_returns_empty(self):
        import search
        import sqlite3
        import numpy as np
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE sessions_vec (session_id TEXT, embedding BLOB)")
        conn.execute("CREATE TABLE sessions (session_id TEXT, first_ts TEXT, last_ts TEXT, turn_count INT)")
        v = np.random.RandomState(3).rand(1024).astype("float32")
        conn.execute("INSERT INTO sessions_vec VALUES (?,?)", ("s0", v.tobytes()))
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", ("s0", "t", "t", 1))
        conn.commit()
        inf_q = [float("inf")] * 1024
        results, raw_map = search.vec_candidates(conn, inf_q)
        conn.close()
        self.assertEqual(results, [])
        self.assertEqual(raw_map, {})


if __name__ == "__main__":
    unittest.main()
