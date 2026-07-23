"""Sprint 4 Task 7 — install/uninstall settings.json 변형 검증."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# install.sh 4.7 섹션의 idempotent append 로직을 재현 (스크립트 분리 테스트용)
INSTALL_REGISTER_CODE = '''
import json, sys
from pathlib import Path

hook_cmd = sys.argv[1]
settings_path = Path(sys.argv[2])
data = json.loads(settings_path.read_text())
hooks = data.setdefault("hooks", {})
ups = hooks.setdefault("UserPromptSubmit", [])

new_hook = {
    "matcher": "*",
    "hooks": [{"type": "command", "command": hook_cmd}],
}
already = any("memory-recall.py" in json.dumps(h) for h in ups)
if not already:
    ups.append(new_hook)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\\n")
    print("appended")
else:
    print("skipped")
'''


class TestSettingsJsonInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_install(self, hook_cmd: str = "/home/test/hooks/memory-recall.py"):
        r = subprocess.run(
            [sys.executable, "-c", INSTALL_REGISTER_CODE, hook_cmd, str(self.settings)],
            capture_output=True,
            check=True,
        )
        return r.stdout.decode().strip()

    def test_appends_to_existing_hooks(self):
        """기존 telegram-guard 같은 hook 보존하면서 추가."""
        self.settings.write_text(json.dumps({
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "/existing/telegram-guard.sh"}]}
                ]
            }
        }, indent=2))
        self.assertEqual(self._run_install(), "appended")
        data = json.loads(self.settings.read_text())
        ups = data["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(ups), 2)
        # 둘 다 보존
        self.assertTrue(any("telegram-guard" in json.dumps(h) for h in ups))
        self.assertTrue(any("memory-recall" in json.dumps(h) for h in ups))

    def test_idempotent(self):
        """두 번 실행해도 hook은 1개만."""
        self.settings.write_text(json.dumps({"hooks": {}}, indent=2))
        self.assertEqual(self._run_install(), "appended")
        self.assertEqual(self._run_install(), "skipped")
        data = json.loads(self.settings.read_text())
        ups = data["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(ups), 1)

    def test_creates_hooks_key_if_missing(self):
        """빈 settings.json도 처리."""
        self.settings.write_text("{}")
        self.assertEqual(self._run_install(), "appended")
        data = json.loads(self.settings.read_text())
        self.assertIn("UserPromptSubmit", data["hooks"])

    def test_install_script_syntax(self):
        """install.sh / uninstall.sh 문법 검증."""
        repo = Path(__file__).parent.parent
        for sh in ("install.sh", "uninstall.sh"):
            r = subprocess.run(
                ["bash", "-n", str(repo / sh)],
                capture_output=True,
            )
            self.assertEqual(r.returncode, 0, f"{sh}: {r.stderr.decode()}")


class TestCloseSessionSkillDeploy(unittest.TestCase):
    """audit-2026-05-25: close-session/cs 스킬을 install.sh 가 deploy 하는지
    + uninstall.sh 가 정리하는지 + skill 본문 자체 회귀 가드."""

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_skill_source_files_exist(self):
        """skill/close-session.md + skill/cs.md repo 에 존재."""
        self.assertTrue((self.repo / "skill" / "close-session.md").exists())
        self.assertTrue((self.repo / "skill" / "cs.md").exists())

    def test_install_wires_close_session(self):
        """install.sh 가 두 skill target 을 모두 명시."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("close-session.md", sh)
        self.assertIn("cs.md", sh)
        # COMMANDS_DIR 경유로 deploy 되는지
        self.assertIn("CLOSE_SESSION_SKILL_TARGET", sh)
        self.assertIn("CS_SKILL_TARGET", sh)

    def test_uninstall_removes_close_session(self):
        """uninstall.sh 가 두 skill 모두 제거."""
        sh = (self.repo / "uninstall.sh").read_text()
        self.assertIn("close-session", sh)
        # for-loop 한 줄에 두 스킬 다 잡히는지
        self.assertIn("close-session cs", sh)

    def test_skill_has_no_hardcoded_user_path(self):
        """audit Critical #1 회귀 가드 — hardcoded user-specific path 금지.

        skill 본문은 일반 사용자도 작동해야 하므로 특정 사용자 home/slug 박지 말 것.
        """
        body = (self.repo / "skill" / "close-session.md").read_text()
        # 특정 사용자명 leak 차단
        forbidden = ["yonghaekim", "/Users/yonghaekim", "dr.ocean", "vibe1977"]
        for tok in forbidden:
            self.assertNotIn(
                tok, body,
                f"skill/close-session.md 안에 personal identifier '{tok}' — sanitize 필요"
            )

    def test_skill_supports_procedural_type(self):
        """audit High #4 회귀 가드 — 5개 type 모두 enum 에 포함."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        for type_name in ("user", "feedback", "project", "reference", "procedural"):
            self.assertIn(type_name, body, f"type '{type_name}' 누락")

    def test_skill_has_concurrency_guard(self):
        """audit codex Medium 회귀 가드 — atomic mkdir lock + atomic rename 가드 명시.

        v3.2.3 (#14): flock 은 macOS 기본 미포함이라 silent skip 됐던 결함을
        mkdir-atomic 패턴으로 교체. 본문에 "flock" 은 옛 패턴 설명용으로 등장.
        """
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("mkdir", body, "atomic mkdir lock 가드 누락")
        self.assertIn("LOCK_DIR", body, "lock 디렉토리 변수 누락")
        self.assertIn("mktemp", body, "atomic rename pattern 누락")

    def test_skill_has_slug_sanitize(self):
        """audit codex High 회귀 가드 — slug sanitize regex 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("[a-z]", body, "slug sanitize regex 누락")
        self.assertIn("realpath", body, "path traversal defense in-depth 누락")

    def test_cs_alias_redirects_to_close_session(self):
        """cs 는 close-session 본문 Read 만 하는 single-source-of-truth 패턴."""
        cs = (self.repo / "skill" / "cs.md").read_text()
        self.assertIn("close-session.md", cs)
        self.assertIn("Read", cs)

    def test_skill_validates_mem_dir_boundary(self):
        """codex round-2 N5 가드 — MV3_MEMORY_DIR override 시 PROJECTS_ROOT 하위 강제."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("_validate_mem_dir", body, "boundary validator 함수 누락")
        self.assertIn("PROJECTS_ROOT", body, "PROJECTS_ROOT 상수 누락")

    def test_skill_uses_nul_separated_find(self):
        """codex round-2 N6 가드 — find -print0 패턴 (whitespace path 안전)."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("-print0", body, "NUL-separated find 누락")
        self.assertIn("xargs -0", body, "xargs -0 누락")

    def test_skill_has_lock_cleanup(self):
        """codex round-2 N1 가드 — lock 디렉토리 정리 (trap).

        v3.2.3 (#14): flock+`rm -f $LOCK_FILE` → mkdir-atomic+`rmdir $LOCK_DIR`.
        디렉토리 자체가 lock 상태라 EXIT trap 에서 반드시 제거.
        """
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("trap", body, "trap (EXIT) lock cleanup 누락")
        self.assertIn('rmdir "$LOCK_DIR"', body, "lock 디렉토리 정리 누락")

    def test_skill_detects_slug_race(self):
        """codex round-2 N2 가드 — 같은 슬러그 race 시 silent loss 안 함."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("conflict-", body, "slug race rescue 패턴 누락")
        self.assertNotIn("mv -n ", body, "mv -n (silent loss) 패턴 잔존")

    def test_install_uses_defensive_deploy_skill(self):
        """v3.2.2 가드 — install.sh 가 deploy_skill 헬퍼로 cp 사이트 통일.

        옛 SKILL_TRIPLES 배열 + 직접 cp 패턴은 cp fail 시 .bak 만 남고 .md 잃을 위험.
        v3.2.2 의 deploy_skill 헬퍼는 cp fail 시 .bak 자동 복원 보장.
        """
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("deploy_skill()", sh, "deploy_skill 헬퍼 정의 누락")
        # 4개 skill 모두 헬퍼 호출 — recall, memory_review, close-session, cs
        for label in ("/recall", "/memory_review", "/close-session", "/cs"):
            self.assertIn(f'deploy_skill ', sh, f"{label} deploy_skill 호출 누락")
        # 옛 SKILL_TRIPLES 배열 패턴은 제거됨
        self.assertNotIn("SKILL_TRIPLES=(", sh, "옛 SKILL_TRIPLES 배열 잔존 (deploy_skill 로 교체됐어야)")

    def test_uninstall_uses_unique_marker(self):
        """codex round-3 D 가드 — uninstall sentinel 이 unique `[mv3-skill]` 마커.

        round-2 의 'MindVault v3' 매칭은 사용자 customize 본문에도 자주 박혀 false-positive
        delete 위험. round-3 에서 frontmatter description 첫머리 `[mv3-skill]` 토큰으로 교체.
        """
        sh = (self.repo / "uninstall.sh").read_text()
        self.assertIn("[mv3-skill]", sh, "uninstall unique marker 매칭 누락")
        # 두 skill frontmatter 에 marker 실존
        for name in ("close-session.md", "cs.md"):
            body = (self.repo / "skill" / name).read_text()
            self.assertIn("[mv3-skill]", body, f"{name} 에 [mv3-skill] marker 없음")

    def test_validator_rejects_projects_root_itself(self):
        """codex round-3 C 가드 — _validate_mem_dir 가 PROJECTS_ROOT 자체는 거부."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        # root 자체 거부 명시
        self.assertIn('[ "$abs" = "$root" ] && return 1', body,
                      "PROJECTS_ROOT 자체 거부 가드 누락")


class TestV3_1_1PostShipFixes(unittest.TestCase):
    """v3.1.1 audit-2026-05-25 post-ship dogfood + codex round-5 fix 회귀 가드."""

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_install_handles_personal_skill_conflict(self):
        """CRITICAL #1 가드 — install.sh 가 옛 personal `~/.claude/skills/{cs,close-session}/`
        디렉토리를 발견하면 sentinel 검사 후 백업."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("skills/close-session", sh, "personal skill conflict 처리 누락")
        self.assertIn("skills.attic", sh, "백업 디렉토리 패턴 누락")

    def test_cwd_slot_activity_heuristic(self):
        """HIGH #2 가드 — cwd 슬롯 priority 2 가 .md ≥ 5 휴리스틱 통과 시에만 채택."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("md_count", body, "활성 슬롯 휴리스틱 누락")
        self.assertIn("-ge 5", body, ".md >= 5 게이트 누락")

    def test_dry_run_arg_parsing_spec(self):
        """HIGH #3 가드 — --dry-run 인자 surface 명세 추가."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("$ARGUMENTS", body, "args surface 명세 누락")
        self.assertIn("DRY_RUN=1", body, "DRY_RUN flag 누락")

    def test_flock_edit_tool_gap_documented(self):
        """HIGH #4 가드 — flock vs Edit/Write tool gap 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("bash subshell", body, "lock 범위 한계 명시 누락")

    def test_korean_slug_conversion_rule(self):
        """MED #5 가드 — 한국어 → kebab 변환 deterministic rule 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("한국어 → 영어 의미 번역", body, "kebab 변환 rule 누락")

    def test_cs_alias_path_fallback(self):
        """MED #6 가드 — cs.md 가 tilde expansion fail 시 fallback path 명시."""
        body = (self.repo / "skill" / "cs.md").read_text()
        self.assertIn("$HOME", body, "absolute path fallback 누락")

    def test_recall_latency_not_stale(self):
        """LOW #8 가드 — /recall 메트릭이 갱신 (200ms → 40ms)."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertNotIn("실측 200ms", body, "/recall 200ms 표기 잔존")
        self.assertIn("p50~40ms", body, "/recall 최신 p50 표기 누락")

    def test_type_tiebreaker_rule(self):
        """LOW #9 가드 — 5 카테고리 tiebreaker rule 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("tiebreaker", body, "type 결정 tiebreaker 누락")

    def test_install_handles_corrupt_skill_dir(self):
        """codex round-6 A 가드 — SKILL.md 부재 시 corrupt install 보존."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("SKILL.md 없음", sh, "corrupt install guard 누락")
        self.assertIn("[ ! -f \"$skill_md\" ]", sh, "SKILL.md 부재 검사 누락")

    def test_install_backup_uses_pid_suffix(self):
        """codex round-6 B 가드 — backup 디렉토리에 $$ PID 추가 (parallel collision 차단)."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("mv3-skill-conflict-$(date", sh)
        self.assertIn("-$$", sh, "PID suffix 누락 — 같은 초 parallel install collision")

    def test_slug_reuse_existing_before_translation(self):
        """codex round-6 D 가드 — slug 결정 시 기존 메모리 매칭 우선."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("기존 메모리에 비슷한 주제가 있으면 그 slug 재사용", body,
                      "slug consistency 강화 누락")

    def test_edit_write_tools_excluded(self):
        """v3.2.3 (#24) — Edit/Write 도구는 allowed-tools 에서 제외.

        옛 codex round-6 E 의 INITIAL_MTIME alt mode 는 Edit/Write 가 lock 우회하는
        race 위험을 완화하기 위한 차선책이었는데, v3.2.3 에서 Edit/Write 자체를
        제거 (#24) 하면서 alt mode 도 불필요해짐. 대신 frontmatter 에서 두 tool
        부재를 검증.
        """
        body = (self.repo / "skill" / "close-session.md").read_text()
        # frontmatter 영역 (--- 두 번 사이) 만 검사 — 본문 안 "Edit/Write" 설명 허용
        head = body.split("---", 2)[1]
        # tool 항목으로 등록되지 않아야 함
        self.assertNotIn("- Edit", head, "Edit 가 allowed-tools 에 잔존")
        self.assertNotIn("- Write", head, "Write 가 allowed-tools 에 잔존")

    def test_cs_warns_on_stale_content(self):
        """codex round-6 F 가드 — cs.md 가 stale content 가능성 안내."""
        body = (self.repo / "skill" / "cs.md").read_text()
        self.assertIn("stale", body, "stale content 경고 누락")


class TestUninstallV320(unittest.TestCase):
    """v3.2.0 — Gemma plist + cache 정리 (com.mindvault.gemma-mlx)."""

    REPO_DIR = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.launch_agents = Path(self.tmp.name) / "LaunchAgents"
        self.launch_agents.mkdir()
        self.cache = Path(self.tmp.name) / "mv3-gemma"
        self.cache.mkdir()
        (self.cache / ".mv3-step").write_text("deps-ok\n")
        self.plist = self.launch_agents / "com.mindvault.gemma-mlx.plist"
        self.plist.write_text("<plist/>")

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_gemma_plist_and_cache(self):
        env = os.environ.copy()
        env.update({
            "MV3_LAUNCH_AGENTS": str(self.launch_agents),
            "MV3_GEMMA_CACHE": str(self.cache),
            "MV3_UNINSTALL_GEMMA_ONLY": "1",
            "MV3_UNINSTALL_DRY_LAUNCHCTL": "1",
        })
        r = subprocess.run(
            ["bash", str(self.REPO_DIR / "uninstall.sh")],
            capture_output=True, env=env,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode())
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.cache.exists())


class TestV3_2_3FixSweep(unittest.TestCase):
    """v3.2.3 — 26건 fix sweep 회귀 가드.

    fresh 사용자 fatal (#1 Python 3.10 절대경로, #13 LaunchAgents mkdir 누락),
    data loss 위험 (#4 SessionEnd broken register, #15 personal SKILL 미복원,
    #17 settings.json non-atomic), race 가드 (#14 flock macOS 부재) 등 26건.
    """

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent

    # ── fresh-install fatal 가드 (#1, #2, #3, #6, #13) ────────────────────

    def test_arctic_runner_wrapper_exists(self):
        """#1 #6 — Arctic-ko 도 Gemma 처럼 wrapper 로 Python resolve."""
        runner = self.repo / "scripts" / "arctic_ko_server_runner.sh"
        self.assertTrue(runner.exists(), "arctic_ko_server_runner.sh 누락")
        body = runner.read_text()
        # 절대경로 Python 박힘 차단
        self.assertNotIn("/Library/Frameworks/Python.framework", body,
                         "wrapper 안에 Python 3.10 절대경로 잔존")
        # 가변 PATH 로 resolve
        self.assertIn("export PATH", body, "PATH 자동 보완 누락")

    def test_arctic_plist_uses_mindvault_namespace(self):
        """#2 — Arctic-ko plist 가 com.mindvault.* 로 sanitize."""
        new_plist = self.repo / "plist" / "com.mindvault.arctic-ko-mlx.plist"
        self.assertTrue(new_plist.exists(), "com.mindvault.arctic-ko-mlx.plist 누락")
        body = new_plist.read_text()
        # personal namespace 잔존 차단
        self.assertNotIn("yonghaekim", body, "plist Label 에 personal namespace 잔존")
        self.assertIn("com.mindvault.arctic-ko-mlx", body, "Label sanitize 누락")
        # Python 절대경로 박힘 차단 (#1)
        self.assertNotIn("/Library/Frameworks/Python.framework", body,
                         "plist 안에 Python 3.10 절대경로 잔존")
        # wrapper 호출
        self.assertIn("arctic_ko_server_runner.sh", body, "runner wrapper 호출 누락")

    def test_install_migrates_legacy_arctic_plist(self):
        """#3 — install.sh 가 옛 com.yonghaekim.arctic-ko-mlx 도 cleanup."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("com.yonghaekim.arctic-ko-mlx", sh,
                      "옛 yonghaekim arctic-ko plist legacy migration 누락")
        self.assertIn("LEGACY_LAUNCHD_LABELS", sh, "legacy label 배열 누락")

    def test_install_creates_launch_agents_dir(self):
        """#13 — fresh macOS 사용자는 ~/Library/LaunchAgents 부재 가능.
        첫 plist deploy 전 mkdir -p 필요.
        """
        sh = (self.repo / "install.sh").read_text()
        self.assertIn('mkdir -p "$GEMMA_LAUNCH_AGENTS"', sh,
                      "LaunchAgents 디렉토리 자동 생성 누락")

    # ── HIGH UX 가드 (#4, #5) ─────────────────────────────────────────

    def test_install_guards_session_end_register(self):
        """#4 — SessionEnd wrapper 미배포 시 register skip (broken path 등록 차단)."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("END_WRAPPER_DEPLOYED", sh, "wrapper 배포 상태 추적 누락")
        # cmd empty 면 register skip
        self.assertIn("register skip", sh, "broken path register 차단 안내 누락")

    def test_install_arctic_ready_uses_file_check(self):
        """#5 — ARCTIC_MODEL_READY 는 step file 대신 model.safetensors 존재로."""
        sh = (self.repo / "install.sh").read_text()
        # 직접 파일 검증 패턴
        self.assertIn('[ -f "$ARCTIC_TARGET/model.safetensors" ]', sh,
                      "model.safetensors 존재 검증 누락")
        # 검증 후 ARCTIC_MODEL_READY=1
        self.assertIn("ARCTIC_MODEL_READY=1", sh, "모델 검증 후 ready flag 설정 누락")

    # ── MEDIUM 가드 (#14, #15, #16, #17, #18, #19, #20, #21, #24) ─────

    def test_skill_uses_atomic_mkdir_lock(self):
        """#14 — flock 의존성 0, macOS-native mkdir-atomic lock."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn('mkdir "$LOCK_DIR"', body, "atomic mkdir lock 패턴 누락")
        self.assertIn("LOCK_DIR", body, "lock 디렉토리 변수 누락")
        # rmdir trap cleanup
        self.assertIn('rmdir "$LOCK_DIR"', body, "rmdir trap cleanup 누락")

    def test_install_records_displaced_personal_skill(self):
        """#15 — install 이 personal SKILL displace 시 manifest 기록."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("personal_skill_displaced", sh,
                      "manifest displace 기록 누락")
        self.assertIn("INSTALL_MANIFEST", sh, "install manifest 변수 누락")

    def test_uninstall_restores_displaced_personal_skill(self):
        """#15 — uninstall 이 manifest 보고 personal SKILL 복원."""
        sh = (self.repo / "uninstall.sh").read_text()
        self.assertIn("personal_skill_displaced", sh,
                      "uninstall 의 manifest restore 패턴 누락")
        self.assertIn("INSTALL_MANIFEST", sh, "manifest 변수 누락")

    def test_install_skill_deploy_no_silent_swallow(self):
        """#16 — `|| true` 가 skill cp 실패 swallow 했던 결함 fix.
        실패 누적 후 끝에서 alarm.
        """
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("DEPLOY_FAILURES", sh, "deploy 실패 누적 변수 누락")
        # 끝에서 명시 경고
        self.assertIn("deploy failure", sh, "deploy 실패 종합 경고 누락")

    def test_install_settings_atomic_write(self):
        """#17 — settings.json 쓰기는 atomic (tmp + os.replace)."""
        sh = (self.repo / "install.sh").read_text()
        # round-trip 검증 + os.replace 패턴
        self.assertIn("os.replace", sh, "atomic os.replace 누락")
        self.assertIn("json.loads(serialized)", sh, "JSON 재검증 누락")

    def test_install_plist_loaded_always_refresh(self):
        """#18 — plist-loaded step 은 cheap step. content drift 차단 위해 항상 refresh."""
        sh = (self.repo / "install.sh").read_text()
        # step entry 정리 후 재기록
        self.assertIn('grep -v "^plist-loaded$"', sh,
                      "plist-loaded step 정리 후 재기록 누락")

    def test_install_ups_register_cleanup(self):
        """#19 — UserPromptSubmit 도 SessionStart/End 같은 cleanup-then-register 패턴."""
        sh = (self.repo / "install.sh").read_text()
        # stale memory-recall.py 도 cleanup
        self.assertIn("stale memory-recall entries", sh,
                      "UPS stale entries cleanup 누락")

    def test_skill_validates_basename_memory(self):
        """#20 — _validate_mem_dir 가 basename 'memory' 정확 매칭."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn('"$(basename "$abs")" = "memory"', body,
                      "basename memory 검증 누락")

    def test_skill_dry_run_exact_token_match(self):
        """#21 — --dry-run 은 shell-like token exact match (substring 아님)."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        # 본문에 token 정확 매칭 안내
        self.assertIn("token 으로 정확 매칭", body, "exact token match 명시 누락")

    def test_skill_allowed_tools_excludes_edit_write(self):
        """#24 — Edit/Write 는 race 우회 위험으로 allowed-tools 에서 제외."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        head = body.split("---", 2)[1]
        self.assertNotIn("- Edit", head, "Edit 가 allowed-tools 에 잔존")
        self.assertNotIn("- Write", head, "Write 가 allowed-tools 에 잔존")
        # cs.md alias 도 동일
        cs_body = (self.repo / "skill" / "cs.md").read_text()
        cs_head = cs_body.split("---", 2)[1]
        self.assertNotIn("- Edit", cs_head, "cs.md Edit 잔존")
        self.assertNotIn("- Write", cs_head, "cs.md Write 잔존")

    # ── Documentation drift 가드 (#23, #25, #26) ──────────────────────

    def test_readme_uninstall_list_includes_gemma(self):
        """#26 — README uninstall 섹션이 com.mindvault.gemma-mlx 명시."""
        body = (self.repo / "README.md").read_text()
        # 어디든 com.mindvault.gemma-mlx 가 uninstall 컨텍스트로 등장
        idx = body.find("uninstall")
        self.assertTrue(idx >= 0, "uninstall 섹션 부재")
        # 그 이후 본문에 gemma plist label 등장
        self.assertIn("com.mindvault.gemma-mlx", body[idx:],
                      "uninstall list 에 Gemma plist label 누락")

    def test_readme_memory_review_no_phantom_subcommands(self):
        """#23 — README 가 /memory_review 의 가상 subcommand (diff/approve/discard)
        를 더 이상 명시하지 않음. 실제 skill 은 인터랙티브 listing 만.
        """
        body = (self.repo / "README.md").read_text()
        # subcommand 형식이 본문에서 빠졌는지 확인 (`/memory_review approve <slug>` 같은)
        self.assertNotIn("/memory_review approve <slug>", body,
                         "phantom subcommand 잔존")
        self.assertNotIn("/memory_review discard <slug>", body,
                         "phantom subcommand 잔존")


class TestV3_2_4PythonBinResolve(unittest.TestCase):
    """v3.2.4 — wrapper PATH 와 install python3 mismatch 로 인한 mlx ImportError
    fix. install.sh 가 자기 python3 의 bin dir 을 wrapper PATH 맨 앞에 prepend.

    v3.2.3 의 #1 #6 fix (Python.framework 절대경로 박힘 제거) 가 도입한 fix-the-fix —
    wrapper PATH 의 첫 매칭 python3 가 install.sh 의 pip --user 가 설치한 인터프리터
    와 다를 경우 ImportError. install.sh 가 사용한 python3 의 dirname 을 명시 prepend.
    """

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent

    def test_wrappers_have_python_bin_placeholder(self):
        """wrapper 본문에 __INSTALL_PYTHON_BIN__ placeholder 가 있어야 함."""
        for name in ("arctic_ko_server_runner.sh", "gemma_server_runner.sh"):
            body = (self.repo / "scripts" / name).read_text()
            self.assertIn("__INSTALL_PYTHON_BIN__", body,
                          f"{name}: placeholder 누락")
            # PATH 맨 앞에 있는지 확인
            for line in body.splitlines():
                if line.startswith("export PATH="):
                    self.assertIn('"__INSTALL_PYTHON_BIN__:', line,
                                  f"{name}: placeholder 가 PATH 맨 앞 아님")
                    break
            else:
                self.fail(f"{name}: 'export PATH=' 라인 부재")

    def test_install_defines_deploy_runner(self):
        """install.sh 가 deploy_runner 헬퍼 정의 + Python placeholder 치환."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("deploy_runner()", sh, "deploy_runner 헬퍼 정의 누락")
        self.assertIn("__INSTALL_PYTHON_BIN__", sh,
                      "install.sh 안 placeholder 치환 로직 누락")
        # 두 wrapper 다 deploy_runner 로 호출
        self.assertIn('deploy_runner "$GEMMA_RUNNER_SRC"', sh,
                      "Gemma runner deploy_runner 호출 누락")
        self.assertIn('deploy_runner "$ARCTIC_RUNNER_SRC"', sh,
                      "Arctic runner deploy_runner 호출 누락")

    def test_deploy_runner_uses_command_v(self):
        """deploy_runner 가 command -v python3 로 install python3 resolve."""
        sh = (self.repo / "install.sh").read_text()
        # dirname + command -v python3 패턴
        self.assertIn('dirname "$(command -v python3)"', sh,
                      "install python3 bin dir 자동 추출 패턴 누락")


class TestV34ContradictionDeploy(unittest.TestCase):
    """v3.4 Layer 5 T10 회귀 가드 — install.sh 가 contradiction modules 를 deploy 하는지.

    T5 의 session_memory_end.py 가 `from contradiction_detector import ...` 를
    runtime 에 import. install.sh 가 이 모듈을 deploy 하지 않으면 hook 이
    silent ModuleNotFoundError (broad except 안에서 묻힘) → Layer 5 무동작.
    """

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_install_sh_deploys_contradiction_modules(self):
        """install.sh must deploy contradiction_detector.py and contradiction_review_cli.py."""
        install_sh = self.repo / "install.sh"
        content = install_sh.read_text(encoding="utf-8")
        # Either explicit list mention OR wildcard pattern that catches them
        has_detector = (
            "contradiction_detector.py" in content
            or "src/*.py" in content
            or "cp -r src" in content
            or "cp src/" in content  # any cp of src/ would catch them
        )
        has_review_cli = (
            "contradiction_review_cli.py" in content
            or "src/*.py" in content
            or "cp -r src" in content
            or "cp src/" in content
        )
        self.assertTrue(has_detector, "install.sh must deploy contradiction_detector.py")
        self.assertTrue(has_review_cli, "install.sh must deploy contradiction_review_cli.py")

    def test_contradiction_source_files_exist(self):
        """repo 에 src/contradiction_detector.py + src/contradiction_review_cli.py 존재."""
        self.assertTrue((self.repo / "src" / "contradiction_detector.py").exists())
        self.assertTrue((self.repo / "src" / "contradiction_review_cli.py").exists())


class TestCompactReinjectionDeploy(unittest.TestCase):
    """compact 재주입(SessionStart source=compact) 회귀 가드 — install.sh 가
    recall_core.py 를 deploy 하는지.

    session_memory.py 의 handle_compact_reinjection 이 runtime 에 `import recall_core`
    한다. install.sh RUNTIME_EXTRA_SRC 에서 빠지면 compact 경로가 silent
    ImportError(broad except 안에서 묻힘) → compact 재주입 무동작.
    (TestV34ContradictionDeploy 와 동일 failure class.)
    """

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_install_sh_deploys_recall_core(self):
        content = (self.repo / "install.sh").read_text(encoding="utf-8")
        has_recall_core = (
            "recall_core.py" in content
            or "src/*.py" in content
            or "cp -r src" in content
            or "cp src/" in content
        )
        self.assertTrue(has_recall_core, "install.sh must deploy recall_core.py (compact 경로 의존)")

    def test_recall_core_source_file_exists(self):
        self.assertTrue((self.repo / "src" / "recall_core.py").exists())


class TestReverifyDeploy(unittest.TestCase):
    """Phase 1③ 신뢰성 회귀 가드 — install.sh 가 reverify 모듈을 deploy 하는지.

    session_memory_end.py 가 `from reverify import maybe_scan_due` 를 runtime import.
    install.sh RUNTIME_EXTRA_SRC 에서 빠지면 hook 이 silent ModuleNotFoundError
    (broad except 안에서 묻힘) → ③ stale 자동 감지 전체 무동작 + reverify_cli 부재.
    (TestV34ContradictionDeploy 와 동일 failure class — audit sweep R1 발견.)
    """

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_install_sh_deploys_reverify_modules(self):
        content = (self.repo / "install.sh").read_text(encoding="utf-8")
        for mod in ("reverify.py", "reverify_cli.py"):
            ok = (
                mod in content
                or "src/*.py" in content
                or "cp -r src" in content
                or "cp src/" in content
            )
            self.assertTrue(ok, f"install.sh must deploy {mod} (③ 신뢰성 런타임 import)")

    def test_reverify_source_files_exist(self):
        self.assertTrue((self.repo / "src" / "reverify.py").exists())
        self.assertTrue((self.repo / "src" / "reverify_cli.py").exists())


if __name__ == "__main__":
    unittest.main()
