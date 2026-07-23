#!/usr/bin/env python3
"""Shared Codex Luna backend for MindVault's asynchronous LLM tasks.

Every call runs in an isolated, ephemeral, read-only Codex subprocess with a
strict JSON schema. The hot-path recall hook never imports this module.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


DATA_DIR = Path(
    os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")
).expanduser()
DEBUG_LOG = DATA_DIR / "debug.log"
METRICS_LOG = DATA_DIR / "metrics.jsonl"
RECURSION_GUARD_ENV = "MV3_HOOK_RECURSION_GUARD"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_EFFORT = "low"
SENSITIVE_MARKERS = (
    "[REDACTED_KEY]",
    "[REDACTED_AWS]",
    "Bearer [REDACTED]",
)


EXTRACTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["feedback", "project", "procedural"],
                    },
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "type", "title", "body", "reason", "evidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}

TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

ALIASES_SCHEMA = {
    "type": "object",
    "properties": {
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        }
    },
    "required": ["aliases"],
    "additionalProperties": False,
}

CONTRADICTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "metric_update",
                            "decision_reversal",
                            "fact_correction",
                            "no_conflict",
                        ],
                    },
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["index", "kind", "reason", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "required": ["index", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _debug(message: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"llm-backend: {message}\n"
            )
    except OSError:
        pass


def _metric(task: str, elapsed_ms: int, ok: bool, reason: str = "") -> None:
    try:
        METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "llm_call",
            "task": task,
            "provider": "codex_cli",
            "model": _model(task),
            "effort": _effort(task),
            "elapsed_ms": elapsed_ms,
            "ok": ok,
        }
        if reason:
            row["reason"] = reason
        with METRICS_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _task_env(prefix: str, task: str, default: str) -> str:
    task_key = task.upper().replace("-", "_")
    return (
        os.environ.get(f"{prefix}_{task_key}")
        or os.environ.get(prefix)
        or default
    ).strip()


def _model(task: str) -> str:
    return _task_env("MV3_LLM_MODEL", task, DEFAULT_MODEL)


def _effort(task: str) -> str:
    return _task_env("MV3_LLM_EFFORT", task, DEFAULT_EFFORT)


def _timeout(task: str) -> int:
    raw = _task_env("MV3_LLM_TIMEOUT", task, "90")
    try:
        return max(10, int(raw))
    except ValueError:
        return 90


def contains_sensitive_marker(prompt: str) -> bool:
    """True when local redaction found a secret; such prompts are not uploaded."""
    return any(marker in prompt for marker in SENSITIVE_MARKERS)


def call_codex_json(
    task: str,
    prompt: str,
    schema: dict[str, Any],
    *,
    timeout: int | None = None,
) -> dict[str, Any] | None:
    """Run one schema-constrained Luna call and return its object."""
    if not prompt.strip():
        return None
    if contains_sensitive_marker(prompt):
        _debug(f"{task} skipped=sensitive-marker")
        _metric(task, 0, False, "sensitive_marker")
        return None
    if os.environ.get(RECURSION_GUARD_ENV) == "1":
        _debug(f"{task} skipped=recursion-guard")
        return None

    codex_bin = os.environ.get("MV3_CODEX_BIN", "codex").strip() or "codex"
    env = os.environ.copy()
    env[RECURSION_GUARD_ENV] = "1"
    started = time.monotonic()

    schema_fd, schema_name = tempfile.mkstemp(
        prefix=f"mv3-{task}-schema-", suffix=".json"
    )
    output_fd, output_name = tempfile.mkstemp(
        prefix=f"mv3-{task}-output-", suffix=".json"
    )
    os.close(schema_fd)
    os.close(output_fd)
    schema_path = Path(schema_name)
    output_path = Path(output_name)
    try:
        schema_path.write_text(
            json.dumps(schema, ensure_ascii=False), encoding="utf-8"
        )
        args = [
            codex_bin,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            tempfile.gettempdir(),
            "-m",
            _model(task),
            "-c",
            f'model_reasoning_effort="{_effort(task)}"',
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-",
        ]
        completed = subprocess.run(
            args,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout if timeout is not None else _timeout(task),
            env=env,
            check=False,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if completed.returncode != 0:
            reason = f"exit_{completed.returncode}"
            _debug(f"{task} fail={reason}")
            _metric(task, elapsed_ms, False, reason)
            return None
        try:
            obj = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            _debug(f"{task} fail=invalid-output")
            _metric(task, elapsed_ms, False, "invalid_output")
            return None
        if not isinstance(obj, dict):
            _debug(f"{task} fail=non-object")
            _metric(task, elapsed_ms, False, "non_object")
            return None
        _metric(task, elapsed_ms, True)
        return obj
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _debug(f"{task} fail=timeout")
        _metric(task, elapsed_ms, False, "timeout")
        return None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        reason = type(exc).__name__
        _debug(f"{task} fail={reason}")
        _metric(task, elapsed_ms, False, reason)
        return None
    finally:
        for path in (schema_path, output_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def call_codex_extractor(prompt: str, timeout: int | None = None) -> str | None:
    """Return a legacy JSON array after one Luna/low structured extraction."""
    obj = call_codex_json(
        "extraction", prompt, EXTRACTOR_SCHEMA, timeout=timeout
    )
    memories = obj.get("memories") if isinstance(obj, dict) else None
    if not isinstance(memories, list):
        return None
    return json.dumps(memories, ensure_ascii=False)


def call_codex_text(task: str, prompt: str) -> str | None:
    obj = call_codex_json(task, prompt, TEXT_SCHEMA)
    raw = obj.get("text") if isinstance(obj, dict) else None
    text = raw.strip() if isinstance(raw, str) else ""
    return text or None


def call_codex_aliases(prompt: str) -> list[str]:
    obj = call_codex_json("aliases", prompt, ALIASES_SCHEMA)
    raw = obj.get("aliases") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        return []
    aliases: list[str] = []
    for item in raw:
        alias = str(item).strip().strip("\"'`")
        if alias and len(alias) <= 30 and alias not in aliases:
            aliases.append(alias)
        if len(aliases) == 5:
            break
    return aliases


def call_codex_contradictions(prompt: str) -> list[dict[str, Any]]:
    obj = call_codex_json(
        "contradiction", prompt, CONTRADICTIONS_SCHEMA
    )
    raw = obj.get("results") if isinstance(obj, dict) else None
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def call_codex_search(prompt: str) -> list[dict[str, Any]]:
    obj = call_codex_json("session_search", prompt, SEARCH_SCHEMA)
    raw = obj.get("results") if isinstance(obj, dict) else None
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
