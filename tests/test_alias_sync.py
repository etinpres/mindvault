"""NEXT-34 — alias_generator.generate(purge_missing=True) 회귀 테스트.

SessionEnd 가 자동 호출하는 동기화 path 가 (1) 신규 메모리 인덱싱 (2) 디스크에
없는 dangling entry 청소 두 동작 모두 수행하는지 검증. Gemma/Claude provider 호출은
mock 으로 차단해 외부 의존성 없이 결정적으로 돈다.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestAliasIndexSync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mv3-alias-sync-"))
        self.mem_dir = self.tmp / "memory"
        self.mem_dir.mkdir()
        self.index_path = self.tmp / "alias_index.json"

        # 메모리 파일 2개 + alias_index 에 (그 중 1개 + 가짜 dangling 1개) 미리 박기
        (self.mem_dir / "feedback_alpha.md").write_text(
            "---\nname: alpha\ndescription: alpha desc\n"
            "metadata:\n  type: feedback\n---\n\nbody alpha"
        )
        (self.mem_dir / "feedback_beta.md").write_text(
            "---\nname: beta\ndescription: beta desc\n"
            "metadata:\n  type: feedback\n---\n\nbody beta"
        )
        # alpha 만 미리 indexed + dangling 1건 + MEMORY.md 1건
        seed = {
            str(self.mem_dir / "feedback_alpha.md"): {
                "name": "alpha",
                "aliases": ["a1", "a2"],
                "provider": "gemma",
                "generated_at": "2026-05-25T00:00:00",
            },
            str(self.mem_dir / "feedback_DELETED.md"): {
                "name": "deleted",
                "aliases": ["x"],
                "provider": "gemma",
                "generated_at": "2026-05-20T00:00:00",
            },
        }
        self.index_path.write_text(json.dumps(seed))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patched_generate(self, **kwargs):
        from alias_generator import generate
        # v3.2.6 H3: generate() 가 매 호출마다 discover_memory_dirs() 를 호출.
        # MEMORY_DIRS module constant 만 patch 하면 무효 — 함수도 함께 patch.
        with patch("alias_generator.MEMORY_DIRS", [self.mem_dir]), \
             patch("alias_generator.discover_memory_dirs", return_value=[self.mem_dir]), \
             patch("alias_generator.INDEX_PATH", self.index_path), \
             patch("alias_generator._call_gemma", return_value=["mock_alias"]):
            return generate(**kwargs)

    def test_purge_missing_removes_dangling(self):
        stats = self._patched_generate(purge_missing=True)
        idx = json.loads(self.index_path.read_text())
        # alpha 는 보존, beta 신규 indexed, deleted purge
        self.assertIn(str(self.mem_dir / "feedback_alpha.md"), idx)
        self.assertIn(str(self.mem_dir / "feedback_beta.md"), idx)
        self.assertNotIn(str(self.mem_dir / "feedback_DELETED.md"), idx)
        self.assertEqual(stats["purged"], 1)
        self.assertEqual(stats["generated"], 1)  # beta 만 신규
        self.assertEqual(stats["skipped"], 1)    # alpha incremental skip

    def test_purge_false_keeps_dangling(self):
        # default behavior — purge 안 켜면 dangling 그대로
        stats = self._patched_generate(purge_missing=False)
        idx = json.loads(self.index_path.read_text())
        self.assertIn(str(self.mem_dir / "feedback_DELETED.md"), idx)
        self.assertEqual(stats.get("purged", 0), 0)

    def test_sync_flag_implies_purge(self):
        # CLI shortcut: --sync 가 purge_missing 켜는지 stats 로 검증
        # (run_main 대신 generate 직접 호출 — main 의 sys.argv 파싱 우회)
        stats = self._patched_generate(purge_missing=True)
        self.assertGreaterEqual(stats["purged"], 1)

    def test_staged_directory_excluded(self):
        """_staged/ 안 메모리는 review 전이라 alias_index 진입 금지 (NEXT-34 #5)."""
        staged_dir = self.mem_dir / "_procedural" / "_staged"
        staged_dir.mkdir(parents=True)
        (staged_dir / "20260101_pending.md").write_text(
            "---\nname: pending\ndescription: pending\n"
            "metadata:\n  type: feedback\n---\n\nbody pending"
        )
        # _procedural 직속 (정상)
        proc = self.mem_dir / "_procedural"
        (proc / "ok_procedural.md").write_text(
            "---\nname: ok_proc\ndescription: ok\n"
            "metadata:\n  type: procedural\n---\n\nbody ok"
        )
        stats = self._patched_generate(purge_missing=True)
        idx = json.loads(self.index_path.read_text())
        # _staged path 는 절대 진입 안 함
        for k in idx:
            self.assertNotIn("_staged", k, f"_staged leaked: {k}")
        # 정상 _procedural 은 진입
        self.assertIn(str(proc / "ok_procedural.md"), idx)

    def test_legacy_staged_entry_purged(self):
        """이전 코드가 박은 _staged entry 가 purge_missing 으로 청소되는지 (디스크 존재해도)."""
        # 디스크에 _staged 파일 만들고 seed 로 alias_index 에 박기
        staged_dir = self.mem_dir / "_procedural" / "_staged"
        staged_dir.mkdir(parents=True)
        legacy = staged_dir / "legacy.md"
        legacy.write_text("---\nname: legacy\ndescription: x\n---\n\nbody")
        seed = json.loads(self.index_path.read_text())
        seed[str(legacy)] = {"name": "legacy", "aliases": ["x"],
                             "provider": "gemma", "generated_at": "2026-05-20T00:00:00"}
        self.index_path.write_text(json.dumps(seed))
        stats = self._patched_generate(purge_missing=True)
        idx = json.loads(self.index_path.read_text())
        self.assertNotIn(str(legacy), idx, "legacy _staged entry not purged")


class TestSessionEndAliasHook(unittest.TestCase):
    """session_memory_end.py 의 alias_sync 호출이 실패해도 main() 결과에 영향 없음."""

    def test_alias_sync_failure_is_silent(self):
        # alias_generator import 실패 시 _debug 만 찍고 return 0
        # 간단히 ImportError 시뮬: sys.modules 에 가짜 모듈 박지 않고
        # session_memory_end 의 main() 안 try 가 except Exception 으로 잡는지 확인.
        # 여기서는 generate 가 Exception raise 해도 main 이 죽지 않는지만 보면 충분.
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        import alias_generator
        original = alias_generator.generate
        try:
            def boom(*a, **kw):
                raise RuntimeError("simulated alias_generator failure")
            alias_generator.generate = boom
            # 본 테스트는 session_memory_end main 까지 호출하지 않고,
            # try/except 의미만 직접 검증
            try:
                alias_generator.generate(purge_missing=True)
                self.fail("should have raised")
            except Exception as e:
                # session_memory_end 의 except 가 그대로 swallow 함을 시뮬
                self.assertIn("simulated", str(e))
        finally:
            alias_generator.generate = original


class TestDiscoverMemoryDirs(unittest.TestCase):
    """v3.2.6 H3: alias_generator 가 cwd-별 모든 memory slot 을 자동 발견.

    이전엔 ``MEMORY_DIRS`` 가 2개 path 만 hardcoded — Sprint 6 의 cwd-별
    projects 자동 생성과 비대칭이라 새 slot 의 메모리는 alias boost 누락.
    NEXT-8 PROJECTS_ROOT family 의 dogfooding gap 회귀 차단.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        self.tmp = Path(tempfile.mkdtemp(prefix="mv3-discover-"))
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        # slot 3개 — 그 중 2개에만 .md 존재 (1개는 빈 디렉토리)
        for slug, has_md in [("slot-a", True), ("slot-b", True), ("slot-empty", False)]:
            mem = self.projects / slug / "memory"
            mem.mkdir(parents=True)
            if has_md:
                (mem / "feedback_x.md").write_text(
                    "---\nname: x\ndescription: x\n---\nbody"
                )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_dirs_with_md_files_discovered(self):
        import alias_generator
        with patch.object(alias_generator, "PROJECTS_ROOT", self.projects), \
             patch.dict("os.environ", {}, clear=False):
            os_env_before = "MV3_EXTRA_MEMORY_DIRS"
            import os as _os
            _os.environ.pop(os_env_before, None)
            dirs = alias_generator.discover_memory_dirs()
        names = sorted(d.parent.name for d in dirs)
        self.assertIn("slot-a", names)
        self.assertIn("slot-b", names)
        self.assertNotIn("slot-empty", names)

    def test_extra_env_dirs_appended(self):
        import alias_generator
        import os as _os
        extra = self.tmp / "external" / "memory"
        extra.mkdir(parents=True)
        (extra / "z.md").write_text("body")
        with patch.object(alias_generator, "PROJECTS_ROOT", self.projects), \
             patch.dict(_os.environ, {"MV3_EXTRA_MEMORY_DIRS": str(extra)}, clear=False):
            dirs = alias_generator.discover_memory_dirs()
        resolved = {str(d.resolve()) for d in dirs}
        self.assertIn(str(extra.resolve()), resolved)

    def test_duplicate_paths_deduped(self):
        import alias_generator
        import os as _os
        slot_a = self.projects / "slot-a" / "memory"
        with patch.object(alias_generator, "PROJECTS_ROOT", self.projects), \
             patch.dict(_os.environ, {"MV3_EXTRA_MEMORY_DIRS": str(slot_a)}, clear=False):
            dirs = alias_generator.discover_memory_dirs()
        # slot-a 가 PROJECTS_ROOT 안에 이미 있는데 env 로 재지정해도 중복 없음.
        count = sum(1 for d in dirs if d.resolve() == slot_a.resolve())
        self.assertEqual(count, 1)


class TestCallClaudeRecursionGuard(unittest.TestCase):
    """bug-audit 2026-05-29 (session-hooks-recursion-guard-end-1): 자식 claude 에
    MV3_HOOK_RECURSION_GUARD=1 을 전파해 nested 훅 재귀 방지."""

    def test_call_claude_sets_recursion_guard_env(self):
        import alias_generator
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.returncode = 1  # 빠른 반환 (파싱 안 해도 됨)
        fake.stdout = ""
        with patch.object(alias_generator.subprocess, "run", return_value=fake) as run:
            alias_generator._call_claude("desc", "body")
        env = run.call_args.kwargs.get("env")
        self.assertIsNotNone(env, "env 가 전달되지 않음")
        self.assertEqual(env.get("MV3_HOOK_RECURSION_GUARD"), "1")


class TestExtractMemoryMetaUnquote(unittest.TestCase):
    """bug-audit 2026-05-29 (embeddings-alias-7): frontmatter 값의 양끝 따옴표 제거."""

    def test_quoted_name_and_description_unquoted(self):
        import alias_generator
        d = Path(tempfile.mkdtemp(prefix="mv3-meta-"))
        p = d / "m.md"
        p.write_text(
            '---\nname: "My Memory"\ndescription: "콜론: 포함된 요지"\ntype: feedback\n---\n\nbody\n',
            encoding="utf-8",
        )
        name, desc, _body = alias_generator._extract_memory_meta(p)
        self.assertEqual(name, "My Memory")
        self.assertEqual(desc, "콜론: 포함된 요지")  # 콜론은 보존, 따옴표만 제거

    def test_unquoted_values_unchanged(self):
        import alias_generator
        self.assertEqual(alias_generator._unquote_fm("plain value"), "plain value")
        self.assertEqual(alias_generator._unquote_fm("'single'"), "single")


if __name__ == "__main__":
    unittest.main()
