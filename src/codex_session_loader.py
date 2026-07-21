"""Codex rollout JSONL → MindVault 정규화 메시지 로더 (v4 P2).

스펙: docs/specs/codex-rollout-format.md — Codex CLI 0.144.6 스냅샷.
반환 계약은 Claude 경로(memory_extractor.load_tail_messages)와 동일:
{"role": "user"|"assistant", "text": <redact 적용 str>, "bash_commands": list[str]}

스펙의 핵심 규칙:
- 대화 원본은 type=response_item / payload.type=message 만. event_msg 의
  user_message/agent_message 는 UI 용 중복이라 적재하지 않는다.
- role=developer, 그리고 첫 turn_context 이전의 bootstrap user 는 제외.
- assistant 는 phase=final_answer 만 기본 transcript (commentary 제외).
  phase 필드가 없는 assistant 는 포함 (구버전·미래 포맷 fail-open).
- custom_tool_call(name=exec)의 input 은 JS source 문자열 — eval 금지,
  정적인 cmd 문자열 리터럴만 보수적으로 파싱한다.
- compacted / world_state / reasoning / 알 수 없는 record 는 건너뛴다
  (버리되 오류로 처리하지 않음 — 포맷은 안정 인터페이스가 아님).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 스펙 "공통 envelope" 의 top-level type 집합. 첫 유효 레코드가 이 형태면
# Codex rollout 으로 판별한다 (Claude 세션 JSONL 은 type=user/assistant +
# message 키 구조라 겹치지 않음).
_CODEX_TOP_TYPES = {
    "session_meta",
    "turn_context",
    "response_item",
    "event_msg",
    "world_state",
    "compacted",
}

# assistant 텍스트에 섞이는 비대화 블록 — transcript 에서 제거.
_STRIP_BLOCK_RE = re.compile(
    r"<(oai-mem-citation|system-reminder)>.*?</\1>",
    re.DOTALL,
)

# tools.exec_command({cmd: "...", ...}) 의 정적 cmd 문자열 리터럴만 매칭.
# 템플릿 표현식·동적 계산은 매칭되지 않아 자연히 제외된다 (스펙 규칙).
_CMD_LITERAL_RE = re.compile(r'\bcmd\s*:\s*"((?:[^"\\]|\\.)*)"')

_ESCAPES = {"\\\"": "\"", "\\\\": "\\", "\\n": "\n", "\\t": "\t"}


def _unescape_cmd(raw: str) -> str:
    out = raw
    for esc, ch in _ESCAPES.items():
        out = out.replace(esc, ch)
    return out


def _redact(text: str) -> str:
    """memory_extractor.redact 재사용 — import 실패 시 원문 유지 (fail-open)."""
    try:
        from memory_extractor import redact
        return redact(text)
    except Exception:
        return text


def is_codex_rollout(jsonl_path: Path) -> bool:
    """첫 유효 JSON 레코드의 envelope 형태로 Codex rollout 여부 판별."""
    try:
        with Path(jsonl_path).open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    return False
                return (
                    isinstance(d, dict)
                    and d.get("type") in _CODEX_TOP_TYPES
                    and isinstance(d.get("payload"), dict)
                )
    except OSError:
        return False
    return False


def _message_text(content) -> str:
    """content[] 의 input_text/output_text 를 순서 유지로 합침. 그 외 type 은 skip."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("input_text", "output_text"):
            t = item.get("text") or ""
            if t:
                parts.append(t)
    return "\n".join(parts)


def load_tail_messages_codex(jsonl_path: Path, tail_turns: int = 40) -> list[dict]:
    msgs: list[dict] = []
    seen_turn_context = False
    try:
        with Path(jsonl_path).open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                payload = d.get("payload")
                if t == "turn_context":
                    seen_turn_context = True
                    continue
                if t != "response_item" or not isinstance(payload, dict):
                    continue
                pt = payload.get("type")
                if pt == "message":
                    role = payload.get("role")
                    if role == "user":
                        # 첫 turn_context 이전 = bootstrap context (environment
                        # context 등 주입분) — 실제 대화가 아니므로 제외.
                        if not seen_turn_context:
                            continue
                    elif role == "assistant":
                        phase = payload.get("phase")
                        if phase is not None and phase != "final_answer":
                            continue
                    else:
                        # developer 및 미래 role 은 대화 본문에서 제외.
                        continue
                    text = _message_text(payload.get("content"))
                    text = _STRIP_BLOCK_RE.sub("", text).strip()
                    if not text:
                        continue
                    msgs.append(
                        {
                            "role": role,
                            "text": _redact(text),
                            "bash_commands": [],
                        }
                    )
                elif pt == "custom_tool_call" and payload.get("name") == "exec":
                    raw = payload.get("input")
                    if not isinstance(raw, str):
                        continue
                    cmds = [
                        _unescape_cmd(m) for m in _CMD_LITERAL_RE.findall(raw)
                    ]
                    if cmds:
                        msgs.append(
                            {
                                "role": "assistant",
                                "text": "",
                                "bash_commands": cmds,
                            }
                        )
                # 그 외 payload type (reasoning, function_call, tool output 등)
                # 과 top-level compacted/world_state/event_msg 는 적재하지 않음.
    except OSError:
        return []
    return msgs[-tail_turns:]
