"""P3 (v4): session_memory_end 의 Codex Stop payload 수신 경로.

S2 스펙 계약 (docs/specs/codex-rollout-format.md §Stop hook):
- hook_event_name=Stop 만 Codex 경로로 처리
- transcript_path null·파일 없음은 exit 0 fail-open
- (session_id, turn_id) 멱등키로 같은 turn 중복 추출 금지
- stdout 은 비운 채 exit 0 (continuation 생성 금지)
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import session_memory_end as sme

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex_sessions"


def _stop_payload(transcript, sid="sess-1", turn="turn-1"):
    return {
        "session_id": sid,
        "turn_id": turn,
        "transcript_path": transcript,
        "cwd": "/tmp",
        "hook_event_name": "Stop",
        "model": "fixture-model",
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "ok",
    }


@pytest.fixture()
def run_main(monkeypatch):
    calls = []

    def fake_extract(jsonl_path):
        calls.append(Path(jsonl_path))
        return []  # 후보 0건 → 파이프라인 조기 종료 (staging 없이 경로만 검증)

    monkeypatch.setattr(sme, "extract_from_jsonl", fake_extract)

    def run(payload) -> int:
        monkeypatch.setattr(
            sme.sys, "stdin", io.StringIO(json.dumps(payload))
        )
        return sme.main()

    run.calls = calls
    return run


def test_codex_stop_routes_transcript(run_main):
    rc = run_main(_stop_payload(str(FIXTURES / "interactive.jsonl")))
    assert rc == 0
    assert run_main.calls == [FIXTURES / "interactive.jsonl"]


def test_codex_stop_turn_idempotency(run_main):
    p = _stop_payload(str(FIXTURES / "exec.jsonl"), sid="sess-2", turn="turn-9")
    assert run_main(p) == 0
    assert run_main(p) == 0  # 같은 (sid, turn) 재전달 — stop_hook_active 재진입 등
    assert len(run_main.calls) == 1
    # 다른 turn 은 다시 추출
    assert run_main(_stop_payload(str(FIXTURES / "exec.jsonl"), sid="sess-2", turn="turn-10")) == 0
    assert len(run_main.calls) == 2


def test_codex_stop_null_transcript_fail_open(run_main):
    assert run_main(_stop_payload(None)) == 0
    assert run_main.calls == []


def test_codex_stop_missing_file_fail_open(run_main, tmp_path):
    assert run_main(_stop_payload(str(tmp_path / "nope.jsonl"))) == 0
    assert run_main.calls == []


def test_claude_path_unaffected(run_main):
    # hook_event_name 없는 기존 Claude SessionEnd payload — 세션 jsonl 이 없는
    # 격리 환경이라 "jsonl missing" 조기 종료. Codex 분기로 새지 않아야 한다.
    rc = run_main({"session_id": "claude-sess-1"})
    assert rc == 0
    assert run_main.calls == []


def test_codex_stop_emits_no_stdout(run_main, capsys):
    run_main(_stop_payload(str(FIXTURES / "compacted.jsonl"), turn="turn-out"))
    assert capsys.readouterr().out == ""
