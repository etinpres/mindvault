"""v3.2.0 — Zero-Touch Install 검증.

install.sh 안의 핵심 헬퍼 (do_step / print_next_step / Apple Silicon guard) 와
checkpoint resume 동작을 격리 테스트.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_DIR / "install.sh"


class TestAppleSiliconGuard(unittest.TestCase):
    """install.sh 가 non-arm64 환경에서 경고 + 분기하는지."""

    def test_non_arm64_prints_warning(self):
        """ARCH_OVERRIDE=x86_64 환경변수로 가드 강제 트리거."""
        env = os.environ.copy()
        env["ARCH_OVERRIDE"] = "x86_64"
        env["MV3_GUARD_ONLY"] = "1"
        r = subprocess.run(
            ["bash", str(INSTALL_SH)],
            capture_output=True,
            env=env,
            input=b"n\n",
        )
        out = r.stdout + r.stderr
        self.assertIn(b"Apple Silicon", out)
        # v3.2.6 H4: 미래 release 예고 ("v3.3.0 예정") 제거됨.
        # 형 [[feedback-no-future-release-predictions]] 규칙 회귀 차단.
        self.assertNotIn(b"v3.3.0", out)
        self.assertIn(b"\xeb\xaf\xb8\xec\xa7\x80\xec\x9b\x90", out)  # "미지원"
        self.assertNotEqual(r.returncode, 0)

    def test_arm64_skips_warning(self):
        """ARCH_OVERRIDE=arm64 면 가드 통과."""
        env = os.environ.copy()
        env["ARCH_OVERRIDE"] = "arm64"
        env["MV3_GUARD_ONLY"] = "1"
        r = subprocess.run(
            ["bash", str(INSTALL_SH)],
            capture_output=True,
            env=env,
        )
        self.assertNotIn(b"Apple Silicon", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0)


class TestDoStep(unittest.TestCase):
    """do_step 헬퍼의 idempotent + resume 동작."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.step_file = Path(self.tmp.name) / ".mv3-step"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, snippet: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c",
             f"export MV3_SOURCE_HELPERS_ONLY=1 ARCH_OVERRIDE=arm64; "
             f"source {INSTALL_SH}; {snippet}"],
            capture_output=True,
        )

    def test_do_step_records_on_success(self):
        r = self._run(f'do_step myname {self.step_file} "true"')
        self.assertEqual(r.returncode, 0)
        self.assertIn("myname\n", self.step_file.read_text())

    def test_do_step_skips_if_already_done(self):
        self.step_file.write_text("myname\n")
        r = self._run(f'do_step myname {self.step_file} "echo SHOULD_NOT_RUN"')
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(b"SHOULD_NOT_RUN", r.stdout)
        self.assertIn(b"already done", r.stdout)

    def test_do_step_no_record_on_failure(self):
        r = self._run(f'do_step myname {self.step_file} "false"')
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.step_file.exists() and "myname" in self.step_file.read_text())

    def test_print_next_step_known_step(self):
        r = self._run('print_next_step downloaded')
        self.assertIn(b"Next:", r.stdout)
        self.assertIn(b"huggingface_hub", r.stdout)


class TestConvertArcticKo(unittest.TestCase):
    """scripts/convert_arctic_ko.py 의 idempotent + cleanup-on-failure."""

    CONVERT_SCRIPT = REPO_DIR / "scripts" / "convert_arctic_ko.py"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.model_dir = Path(self.tmp.name) / "arctic-ko"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *extra_args, env_extra=None):
        env = os.environ.copy()
        env["MV3_CONVERT_DRY_RUN"] = env_extra.get("dry_run", "1") if env_extra else "1"
        if env_extra:
            for k, v in env_extra.items():
                if k != "dry_run":
                    env[k] = v
        return subprocess.run(
            [sys.executable, str(self.CONVERT_SCRIPT), "--target", str(self.model_dir), *extra_args],
            capture_output=True, env=env,
        )

    def test_skip_if_model_exists(self):
        """target/model.safetensors 이미 있으면 즉시 success exit."""
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "model.safetensors").write_bytes(b"fake")
        r = self._run()
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"already present", r.stdout)

    def test_dry_run_creates_marker(self):
        """MV3_CONVERT_DRY_RUN=1 + 모델 없으면 marker 만 만들고 success."""
        r = self._run()
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.model_dir / "model.safetensors").exists())

    def test_partial_cleanup_on_failure(self):
        """변환 도중 fail 시 model_dir 내부 정리 (corrupt 차단)."""
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "partial.bin").write_bytes(b"partial")
        r = self._run(env_extra={"dry_run": "1", "MV3_CONVERT_FAIL": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.model_dir / "partial.bin").exists())

    def test_real_path_missing_mlx_embeddings(self):
        """MV3_CONVERT_DRY_RUN 미설정 + mlx_embeddings 막힘 시 exit 1 + 안내 메시지."""
        env = os.environ.copy()
        env.pop("MV3_CONVERT_DRY_RUN", None)
        empty = Path(self.tmp.name) / "empty_pp"
        empty.mkdir()
        pkg = empty / "mlx_embeddings"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("raise ImportError('blocked by test')\n")
        env["PYTHONPATH"] = str(empty)
        r = subprocess.run(
            [sys.executable, str(self.CONVERT_SCRIPT), "--target", str(self.model_dir)],
            capture_output=True, env=env,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"mlx_embeddings", r.stderr)


class TestDeploySkill(unittest.TestCase):
    """v3.2.2 — deploy_skill 헬퍼의 defensive cp + .bak 자동 복원."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "skill.md"
        self.target = Path(self.tmp.name) / "target.md"
        self.bak = Path(self.tmp.name) / "target.md.bak"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, snippet: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c",
             f"export MV3_SOURCE_HELPERS_ONLY=1 ARCH_OVERRIDE=arm64; "
             f"source {INSTALL_SH}; {snippet}"],
            capture_output=True,
        )

    def test_missing_src_aborts_target_preserved(self):
        """src 부재 시 deploy_skill return 1, 기존 target 그대로."""
        self.target.write_text("original content")
        # src 미생성
        r = self._run(f'deploy_skill {self.src} {self.target} "/test"')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"skill source missing", r.stderr)
        self.assertEqual(self.target.read_text(), "original content")
        self.assertFalse(self.bak.exists())

    def test_successful_deploy_removes_bak(self):
        """정상 cp 후 .bak 정리 (cruft 누적 방지)."""
        self.src.write_text("new content")
        self.target.write_text("old content")
        r = self._run(f'deploy_skill {self.src} {self.target} "/test"')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.target.read_text(), "new content")
        self.assertFalse(self.bak.exists())   # 성공 시 .bak 정리됨

    def test_first_install_no_existing_target(self):
        """target 처음 설치 시 .bak 없음, cp 정상."""
        self.src.write_text("new content")
        r = self._run(f'deploy_skill {self.src} {self.target} "/test"')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.target.read_text(), "new content")
        self.assertFalse(self.bak.exists())


class TestGemmaRunner(unittest.TestCase):
    """scripts/gemma_server_runner.sh 가 mlx_lm.server 호출하는 명령 형식 검증."""

    RUNNER = REPO_DIR / "scripts" / "gemma_server_runner.sh"

    def test_dry_run_prints_expected_command(self):
        """MV3_RUNNER_DRY_RUN=1 시 실제 실행 안 하고 명령 echo."""
        env = os.environ.copy()
        env["MV3_RUNNER_DRY_RUN"] = "1"
        r = subprocess.run(
            ["bash", str(self.RUNNER)],
            capture_output=True, env=env,
        )
        self.assertEqual(r.returncode, 0)
        out = r.stdout.decode()
        self.assertIn("mlx_lm.server", out)
        self.assertIn("mlx-community/gemma-4-e4b-it-4bit", out)
        self.assertIn("127.0.0.1", out)
        self.assertIn("8080", out)

    def test_runner_exists_and_executable(self):
        self.assertTrue(self.RUNNER.exists())
        self.assertTrue(os.access(self.RUNNER, os.X_OK))


class TestSprint45ArcticKoConvert(unittest.TestCase):
    """Sprint 4.5 — 모델 부재 → install.sh 실행 → 모델 marker 생성 시나리오."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "arctic"
        self.step_file = Path(self.tmp.name) / ".mv3-step"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_sprint45(self, env_extra=None):
        env = os.environ.copy()
        env.update({
            "ARCH_OVERRIDE": "arm64",
            "MV3_SPRINT45_ONLY": "1",
            "MV3_ARCTIC_TARGET": str(self.target),
            "MV3_ARCTIC_STEP_FILE": str(self.step_file),
            "MV3_CONVERT_DRY_RUN": "1",
        })
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", str(INSTALL_SH)],
            capture_output=True, env=env,
        )

    def test_clean_run_creates_marker(self):
        """모델 없는 상태에서 4.5 실행 → marker 생성 + step 파일 4줄."""
        r = self._run_sprint45()
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode())
        self.assertTrue((self.target / "model.safetensors").exists())
        steps = self.step_file.read_text().splitlines()
        self.assertIn("deps-ok", steps)
        self.assertIn("converted", steps)
        self.assertIn("verified", steps)

    def test_resume_from_downloaded(self):
        """step 파일에 deps-ok + downloaded 있을 때 → converted/verified 만 실행."""
        self.step_file.write_text("deps-ok\ndownloaded\n")
        r = self._run_sprint45()
        self.assertEqual(r.returncode, 0)
        steps = self.step_file.read_text().splitlines()
        self.assertEqual(steps.count("deps-ok"), 1)
        self.assertEqual(steps.count("downloaded"), 1)
        self.assertIn("converted", steps)

    def test_fully_done_skips_all(self):
        """모든 step 완료 + 모델 marker 있으면 4.5 가 noop."""
        self.target.mkdir(parents=True)
        (self.target / "model.safetensors").write_bytes(b"existing")
        self.step_file.write_text("deps-ok\ndownloaded\nconverted\nverified\n")
        r = self._run_sprint45()
        self.assertEqual(r.returncode, 0)
        self.assertGreaterEqual(r.stdout.decode().count("already done"), 4)

    def test_convert_failure_no_step_recorded(self):
        """MV3_CONVERT_FAIL 트리거 시 converted/verified step 미기록."""
        self.step_file.write_text("deps-ok\ndownloaded\n")
        r = self._run_sprint45(env_extra={"MV3_CONVERT_FAIL": "1"})
        self.assertNotEqual(r.returncode, 0)
        steps = self.step_file.read_text().splitlines()
        self.assertNotIn("converted", steps)


class TestSprint17GemmaInstall(unittest.TestCase):
    """Sprint 17 — Gemma plist + cache 설치 + 기존 서비스 충돌 감지."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "mv3-gemma"
        self.step_file = self.cache_dir / ".mv3-step"
        self.launch_agents = Path(self.tmp.name) / "LaunchAgents"
        self.launch_agents.mkdir()
        self.scripts_dir = Path(self.tmp.name) / "mindvault-scripts"
        self.scripts_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run_sprint17(self, env_extra=None):
        env = os.environ.copy()
        env.update({
            "ARCH_OVERRIDE": "arm64",
            "MV3_SPRINT17_ONLY": "1",
            "MV3_GEMMA_CACHE": str(self.cache_dir),
            "MV3_GEMMA_STEP_FILE": str(self.step_file),
            "MV3_LAUNCH_AGENTS": str(self.launch_agents),
            "MV3_SCRIPTS_DIR": str(self.scripts_dir),
            "MV3_GEMMA_DRY_RUN": "1",
            "MV3_EXISTING_GEMMA": "",
        })
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", str(INSTALL_SH)],
            capture_output=True, env=env,
        )

    def test_clean_install_creates_plist_and_steps(self):
        r = self._run_sprint17()
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode())
        self.assertTrue((self.launch_agents / "com.mindvault.gemma-mlx.plist").exists())
        self.assertTrue((self.scripts_dir / "gemma_server_runner.sh").exists())
        steps = self.step_file.read_text().splitlines()
        for s in ("deps-ok", "downloaded", "plist-loaded", "healthy"):
            self.assertIn(s, steps)

    def test_existing_gemma_service_skips_plist(self):
        r = self._run_sprint17(env_extra={"MV3_EXISTING_GEMMA": "com.yonghaekim.gemma-mlx"})
        self.assertEqual(r.returncode, 0)
        out = r.stdout.decode()
        self.assertIn("기존 Gemma launchd 서비스 감지됨", out)
        self.assertIn("com.yonghaekim.gemma-mlx", out)
        self.assertFalse((self.launch_agents / "com.mindvault.gemma-mlx.plist").exists())

    def test_resume_from_plist_loaded(self):
        self.step_file.parent.mkdir(parents=True, exist_ok=True)
        self.step_file.write_text("deps-ok\ndownloaded\nplist-loaded\n")
        (self.launch_agents / "com.mindvault.gemma-mlx.plist").write_text("<existing/>")
        r = self._run_sprint17()
        self.assertEqual(r.returncode, 0)
        out = r.stdout.decode()
        self.assertGreaterEqual(out.count("already done"), 3)
        steps = self.step_file.read_text().splitlines()
        self.assertIn("healthy", steps)


if __name__ == "__main__":
    unittest.main()
