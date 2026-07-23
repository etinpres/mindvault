"""P2 (v4): Codex rollout JSONL 로더 — 정규화 메시지 계약 검증.

스펙: docs/specs/codex-rollout-format.md (0.144.6 스냅샷).
계약: Claude 경로(load_tail_messages)와 동일한
{"role": "user"|"assistant", "text": str, "bash_commands": list[str]}.
"""
from __future__ import annotations

import json
from pathlib import Path

from codex_session_loader import (
    is_codex_rollout,
    load_incremental_messages_codex,
    load_tail_messages_codex,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex_sessions"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_interactive_fixture_normalized():
    msgs = load_tail_messages_codex(FIXTURES / "interactive.jsonl")
    assert msgs
    assert all(set(m) == {"role", "text", "bash_commands"} for m in msgs)
    assert all(m["role"] in ("user", "assistant") for m in msgs)
    # developer·bootstrap user(첫 turn_context 이전)는 제외
    joined = " ".join(m["text"] for m in msgs)
    assert "Follow repository instructions" not in joined
    assert "Bootstrap context" not in joined
    assert "environment_context" not in joined
    # commentary phase 는 기본 transcript 에서 제외, final_answer 만
    assert "검증을 시작할게요." not in joined
    texts = [m["text"] for m in msgs if m["text"]]
    assert texts == [
        "검증 명령을 실행해줘.",
        "검증이 통과했어요.",
        "결과만 다시 알려줘.",
        "2개 테스트가 모두 통과했어요.",
    ]


def test_interactive_bash_commands_conservative_parse():
    msgs = load_tail_messages_codex(FIXTURES / "interactive.jsonl")
    bash = [m for m in msgs if m["bash_commands"]]
    assert len(bash) == 1
    assert bash[0]["role"] == "assistant"
    assert bash[0]["bash_commands"] == ["python3 -m pytest -q"]
    # 첫 user 발화 뒤, 해당 turn 의 final_answer 앞 위치 (순서 보존)
    roles_texts = [(m["role"], m["text"]) for m in msgs]
    assert roles_texts.index(("assistant", "")) == 1


def test_exec_fixture_single_turn():
    msgs = load_tail_messages_codex(FIXTURES / "exec.jsonl")
    assert [(m["role"], m["text"]) for m in msgs] == [
        ("user", "Reply exactly EXEC_OK."),
        ("assistant", "EXEC_OK"),
    ]


def test_compacted_fixture_both_windows_no_duplication():
    msgs = load_tail_messages_codex(FIXTURES / "compacted.jsonl")
    texts = [m["text"] for m in msgs]
    assert texts == [
        "첫 번째 결정을 기억해줘.",
        "첫 번째 결정을 확인했어요.",
        "compact 이후 다음 작업을 시작해줘.",
        "다음 작업을 시작했어요.",
    ]


def test_tail_turns_truncation():
    msgs = load_tail_messages_codex(FIXTURES / "interactive.jsonl", tail_turns=2)
    assert [m["text"] for m in msgs] == [
        "결과만 다시 알려줘.",
        "2개 테스트가 모두 통과했어요.",
    ]


def test_incremental_loader_only_returns_delta_plus_overlap():
    rollout = FIXTURES / "interactive.jsonl"
    first = load_incremental_messages_codex(
        rollout, cursor_offset=0, bootstrap_tail=2
    )
    assert first.success is True
    assert first.has_delta is True
    assert [m["text"] for m in first.messages] == [
        "결과만 다시 알려줘.",
        "2개 테스트가 모두 통과했어요.",
    ]
    assert first.end_offset == rollout.stat().st_size

    unchanged = load_incremental_messages_codex(
        rollout, cursor_offset=first.end_offset, overlap=2
    )
    assert unchanged.success is True
    assert unchanged.has_delta is False
    assert unchanged.messages == []


def test_incremental_loader_append_includes_small_overlap(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    original = (FIXTURES / "exec.jsonl").read_text()
    rollout.write_text(original)
    cursor = rollout.stat().st_size
    appended = [
        {"type": "turn_context", "payload": {"turn_id": "t2"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "앞으로 Luna를 써줘."}]}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "그렇게 할게요."}]}},
    ]
    with rollout.open("a") as f:
        f.write("\n" + "\n".join(json.dumps(d) for d in appended) + "\n")

    delta = load_incremental_messages_codex(
        rollout, cursor_offset=cursor, overlap=1
    )
    assert delta.has_delta is True
    assert [m["text"] for m in delta.messages] == [
        "EXEC_OK",
        "앞으로 Luna를 써줘.",
        "그렇게 할게요.",
    ]
    assert delta.delta_message_count == 2


def test_citation_and_reminder_blocks_stripped(tmp_path):
    rollout = tmp_path / "rollout-synthetic.jsonl"
    lines = [
        {"type": "turn_context", "payload": {"turn_id": "t1"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "질문이야"}]}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text",
                         "text": "답이야\n<oai-mem-citation>\nx\n</oai-mem-citation>"}]}},
    ]
    rollout.write_text("\n".join(json.dumps(d) for d in lines))
    msgs = load_tail_messages_codex(rollout)
    assert [m["text"] for m in msgs] == ["질문이야", "답이야"]


def test_malformed_lines_fail_open(tmp_path):
    src = (FIXTURES / "exec.jsonl").read_text()
    broken = tmp_path / "rollout-broken.jsonl"
    broken.write_text("{not json}\n" + src + "\n{\"type\": \"future_type\"}\n")
    msgs = load_tail_messages_codex(broken)
    assert [m["text"] for m in msgs] == ["Reply exactly EXEC_OK.", "EXEC_OK"]


def test_bash_commands_redacted(tmp_path):
    """X1-R1 High: Claude loader 와 대칭으로 cmd 문자열에도 redact() 적용."""
    secret = "sk-abcdefghijklmnopqrstuvwx"
    rollout = tmp_path / "rollout-secret.jsonl"
    lines = [
        {"type": "turn_context", "payload": {"turn_id": "t1"}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c1",
            "input": 'const r = await tools.exec_command({cmd: "curl -H '
                     f'{secret}", workdir: "/w"}})',
        }},
    ]
    rollout.write_text("\n".join(json.dumps(d) for d in lines))
    msgs = load_tail_messages_codex(rollout)
    assert len(msgs) == 1
    assert secret not in msgs[0]["bash_commands"][0]
    assert "[REDACTED_KEY]" in msgs[0]["bash_commands"][0]


def test_installer_deploys_loader_module():
    """X1-R1 High: 배포 누락 회귀 가드 — install.sh 배포 목록에 로더 포함.

    실운영에서 ModuleNotFoundError → Claude fallback 으로 자동 추출이 조용히
    무력화됐던 결함. 파일명이 배포 배열에서 빠지면 즉시 실패해야 한다.
    """
    install_sh = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "codex_session_loader.py" in install_sh


def test_is_codex_rollout_skips_leading_malformed(tmp_path):
    """X1-R1 Low: '첫 유효 레코드' 계약 — 선행 malformed 줄은 건너뛰고 판별."""
    src = (FIXTURES / "exec.jsonl").read_text()
    broken = tmp_path / "rollout-lead-broken.jsonl"
    broken.write_text("{not json}\n\n" + src)
    assert is_codex_rollout(broken) is True


def test_is_codex_rollout_detection(tmp_path):
    assert is_codex_rollout(FIXTURES / "interactive.jsonl") is True
    claude = tmp_path / "claude-session.jsonl"
    claude.write_text(json.dumps({
        "type": "user",
        "message": {"content": [{"type": "text", "text": "안녕"}]},
    }) + "\n")
    assert is_codex_rollout(claude) is False
    assert is_codex_rollout(tmp_path / "missing.jsonl") is False


def test_extract_from_jsonl_routes_codex(monkeypatch):
    import memory_extractor

    captured = {}

    def fake_gemma(prompt, max_tokens=1500):
        captured["prompt"] = prompt
        return "[]"

    monkeypatch.setattr(memory_extractor, "call_gemma", fake_gemma)
    monkeypatch.setenv("MV3_EXTRACTOR_ALWAYS_FIRE", "1")
    import extractor_cache
    monkeypatch.setattr(extractor_cache, "cache_get", lambda prompt: None)
    monkeypatch.setattr(extractor_cache, "cache_put", lambda *a, **k: None)

    result = memory_extractor.extract_from_jsonl(FIXTURES / "interactive.jsonl")
    assert result == []
    assert "검증 명령을 실행해줘." in captured["prompt"]
    assert "Bootstrap context" not in captured["prompt"]
