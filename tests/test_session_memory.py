"""Unit tests for MindVault v3 session_memory hook."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import session_memory as sm  # noqa: E402


class TestRedact(unittest.TestCase):
    def test_openai_key(self):
        t = "my key is sk-abcdefghij1234567890xyz please"
        self.assertIn("[REDACTED_KEY]", sm.redact(t))
        self.assertNotIn("sk-abcdefg", sm.redact(t))

    def test_github_token(self):
        t = "token ghp_abcdefghij1234567890abcdef"
        self.assertIn("[REDACTED_KEY]", sm.redact(t))

    def test_bearer(self):
        t = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxxxxxxxxxx"
        self.assertIn("Bearer [REDACTED]", sm.redact(t))

    def test_no_match(self):
        t = "just a normal sentence"
        self.assertEqual(sm.redact(t), t)


class TestExtractContent(unittest.TestCase):
    def test_string_content(self):
        self.assertEqual(sm.extract_text_from_content("hello"), "hello")

    def test_text_block_list(self):
        content = [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}]
        self.assertEqual(sm.extract_text_from_content(content), "hi\nthere")

    def test_skips_thinking_and_tool_use(self):
        content = [
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": "visible"},
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]
        self.assertEqual(sm.extract_text_from_content(content), "visible")

    def test_empty(self):
        self.assertEqual(sm.extract_text_from_content(None), "")
        self.assertEqual(sm.extract_text_from_content([]), "")

    def test_regression_m1_keeps_user_text_alongside_system_reminder(self):
        """M1 회귀: 시스템 리마인더 블록 + 진짜 사용자 텍스트 블록 공존 시 사용자 텍스트 보존."""
        content = [
            {"type": "text", "text": "<system-reminder>some reminder</system-reminder>"},
            {"type": "text", "text": "실제 사용자 질문입니다"},
        ]
        result = sm.extract_text_from_content(content)
        self.assertIn("실제 사용자 질문입니다", result)
        self.assertNotIn("system-reminder", result)

    def test_regression_m1_string_content_that_is_system_reminder(self):
        """순수 문자열 콘텐츠가 통째로 시스템 리마인더면 빈 문자열."""
        content = "<system-reminder>all reminder</system-reminder>"
        self.assertEqual(sm.extract_text_from_content(content), "")

    def test_regression_m1_command_name_block_filtered(self):
        """<command-name>, <command-message> 등 command-* 블록도 필터링."""
        content = [
            {"type": "text", "text": "<command-name>clear</command-name>"},
            {"type": "text", "text": "이건 실제 메시지"},
        ]
        result = sm.extract_text_from_content(content)
        self.assertNotIn("command-name", result)
        self.assertIn("이건 실제 메시지", result)


class TestExtractMessages(unittest.TestCase):
    def _write_jsonl(self, rows: list[dict]) -> Path:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl", mode="w")
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.close()
        return Path(f.name)

    def test_head_tail_window(self):
        rows = []
        for i in range(50):
            rows.append({
                "type": "user",
                "message": {"role": "user", "content": f"msg {i}"},
            })
        p = self._write_jsonl(rows)
        msgs = sm.extract_messages(p, head_turns=6, tail_turns=6)
        self.assertEqual(len(msgs), 12)
        self.assertEqual(msgs[0]["text"], "msg 0")
        self.assertEqual(msgs[-1]["text"], "msg 49")

    def test_skips_non_user_assistant(self):
        rows = [
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "user", "message": {"content": "hello"}},
            {"type": "permission-mode", "permissionMode": "bypass"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        ]
        p = self._write_jsonl(rows)
        msgs = sm.extract_messages(p)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")

    def test_skips_signature_block(self):
        rows = [
            {"type": "user", "message": {"content": f"{sm.SIGNATURE}\n\nprevious"}},
            {"type": "user", "message": {"content": "real message"}},
        ]
        p = self._write_jsonl(rows)
        msgs = sm.extract_messages(p)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "real message")

    def test_truncates_long_text(self):
        long_text = "x" * 500
        rows = [{"type": "user", "message": {"content": long_text}}]
        p = self._write_jsonl(rows)
        msgs = sm.extract_messages(p)
        self.assertEqual(len(msgs[0]["text"]), sm.MAX_MSG_CHARS)

    def test_redacts_secrets(self):
        rows = [{"type": "user", "message": {"content": "key sk-abcdef1234567890abcdef"}}]
        p = self._write_jsonl(rows)
        msgs = sm.extract_messages(p)
        self.assertIn("[REDACTED_KEY]", msgs[0]["text"])

    def test_regression_m2_redact_before_truncate(self):
        """M2 회귀: 비밀이 MAX_MSG_CHARS 경계를 걸쳐도 먼저 redact되어 단편이 남지 않음."""
        prefix = "x" * (sm.MAX_MSG_CHARS - 20)
        secret = " sk-SECRETKEY12345678901234567890ABCDEF"
        rows = [{"type": "user", "message": {"content": prefix + secret}}]
        p = self._write_jsonl(rows)
        msgs = sm.extract_messages(p)
        out = msgs[0]["text"]
        self.assertNotIn("sk-SECRETKEY", out)
        self.assertNotIn("sk-", out.split("[REDACTED_KEY]")[-1][:5] if "[REDACTED_KEY]" in out else out)

    def test_ignores_malformed_lines(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl", mode="w")
        f.write('{"type": "user", "message": {"content": "ok"}}\n')
        f.write("not json at all\n")
        f.write('{"type": "user", "message": {"content": "still ok"}}\n')
        f.close()
        msgs = sm.extract_messages(Path(f.name))
        self.assertEqual(len(msgs), 2)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._original = sm.CACHE_DIR
        sm.CACHE_DIR = Path(self.tmp)

    def tearDown(self):
        sm.CACHE_DIR = self._original

    def test_key_deterministic(self):
        p1 = Path(tempfile.NamedTemporaryFile(delete=False).name)
        p2 = Path(tempfile.NamedTemporaryFile(delete=False).name)
        k1 = sm.cache_key([p1, p2])
        k2 = sm.cache_key([p2, p1])
        self.assertEqual(k1, k2, "cache key must be order-independent")

    def test_key_changes_on_mtime(self):
        p = Path(tempfile.NamedTemporaryFile(delete=False).name)
        k1 = sm.cache_key([p])
        time.sleep(0.01)
        p.touch()
        k2 = sm.cache_key([p])
        self.assertNotEqual(k1, k2)

    def test_get_set_roundtrip(self):
        sm.cache_set("abc", "hello summary")
        self.assertEqual(sm.cache_get("abc"), "hello summary")

    def test_get_missing(self):
        self.assertIsNone(sm.cache_get("does-not-exist"))

    def test_cache_set_atomic_no_tmp_leftover(self):
        """v3.2.6 Round 2 LR1: cache_set 가 tmp + os.replace 패턴 — 성공 시
        .tmp 잔존 없고 source 에 os.replace 명시."""
        sm.cache_set("atomic", "value")
        leftover = list(Path(self.tmp).glob("*.tmp"))
        self.assertEqual(leftover, [])
        import inspect
        src = inspect.getsource(sm.cache_set)
        self.assertIn("os.replace", src)
        self.assertIn(".tmp", src)


class TestGemmaClientErrorHandling(unittest.TestCase):
    """call_gemma 의 실패 경로 — 함수명은 legacy 지만 내부 구현은 `subprocess.run`
    으로 `claude -p` CLI 호출. 따라서 mock 대상은 urllib.urlopen 이 아니라
    `session_memory.subprocess.run`. 이전 stale test 가 잘못된 target 잡아 실제
    claude CLI 가 호출되며 fail 했던 결함을 본 패치로 해소 (Sprint 11 BUILD-LOG
    §"미해결" 4번)."""

    def test_returns_none_on_timeout(self):
        import subprocess
        with patch(
            "session_memory.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5),
        ):
            self.assertIsNone(sm.call_gemma("anything"))

    def test_returns_none_on_binary_missing(self):
        """claude CLI 가 없을 때 (FileNotFoundError) 도 graceful None."""
        with patch(
            "session_memory.subprocess.run",
            side_effect=FileNotFoundError("claude"),
        ):
            self.assertIsNone(sm.call_gemma("anything"))

    def test_returns_none_on_nonzero_exit(self):
        """exit code != 0 면 stderr 메시지 무관하게 None."""
        from types import SimpleNamespace
        fake = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with patch("session_memory.subprocess.run", return_value=fake):
            self.assertIsNone(sm.call_gemma("anything"))


class TestEmitOutput(unittest.TestCase):
    def test_emits_valid_json_with_signature(self):
        import io

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            sm.emit_output("my summary body")
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(sm.SIGNATURE, parsed["hookSpecificOutput"]["additionalContext"])
        self.assertIn("my summary body", parsed["hookSpecificOutput"]["additionalContext"])


class TestGetRecentSessionsHeuristic(unittest.TestCase):
    def test_excludes_most_recent_when_no_session_id(self):
        """E2 휴리스틱: sessionId 없으면 가장 최근 mtime 파일 1개 배제."""
        tmp = tempfile.mkdtemp()
        paths = []
        for i in range(3):
            p = Path(tmp) / f"sess{i}.jsonl"
            p.write_text("{}\n")
            import os as _os
            _os.utime(p, (1000000 + i, 1000000 + i))
            paths.append(p)
        original = sm.PROJECTS_DIR
        sm.PROJECTS_DIR = Path(tmp)
        try:
            result = sm.get_recent_sessions(None)
            result_names = [r.name for r in result]
            self.assertNotIn("sess2.jsonl", result_names, "most recent excluded as current-session heuristic")
            self.assertIn("sess1.jsonl", result_names)
            self.assertIn("sess0.jsonl", result_names)
        finally:
            sm.PROJECTS_DIR = original

    def test_respects_explicit_exclude(self):
        """sessionId 주어지면 휴리스틱 미적용, 지정된 것만 제외."""
        tmp = tempfile.mkdtemp()
        for i in range(3):
            p = Path(tmp) / f"sess{i}.jsonl"
            p.write_text("{}\n")
            import os as _os
            _os.utime(p, (1000000 + i, 1000000 + i))
        original = sm.PROJECTS_DIR
        sm.PROJECTS_DIR = Path(tmp)
        try:
            result = sm.get_recent_sessions("sess0")
            result_names = [r.name for r in result]
            self.assertNotIn("sess0.jsonl", result_names)
            self.assertIn("sess2.jsonl", result_names, "most recent kept since explicit exclude given")
            self.assertIn("sess1.jsonl", result_names)
        finally:
            sm.PROJECTS_DIR = original


if __name__ == "__main__":
    unittest.main()
