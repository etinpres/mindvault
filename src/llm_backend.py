#!/usr/bin/env python3
"""MindVault memory extraction LLM backends.

The Codex CLI backend intentionally runs as an isolated, ephemeral, read-only
sub-session. ``MV3_HOOK_RECURSION_GUARD`` is mandatory: without it, the nested
Codex session's Stop hook can recursively start another extraction.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


_SCHEMA = {
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


def _timeout() -> int:
    try:
        return max(10, int(os.environ.get("MV3_LLM_TIMEOUT", "60")))
    except ValueError:
        return 60


def call_codex_extractor(prompt: str, timeout: int | None = None) -> str | None:
    """Run one Luna/low structured extraction and return a legacy JSON array."""
    model = os.environ.get("MV3_LLM_MODEL", "gpt-5.6-luna").strip()
    effort = os.environ.get("MV3_LLM_EFFORT", "low").strip()
    codex_bin = os.environ.get("MV3_CODEX_BIN", "codex").strip() or "codex"
    env = os.environ.copy()
    env["MV3_HOOK_RECURSION_GUARD"] = "1"

    schema_fd, schema_name = tempfile.mkstemp(
        prefix="mv3-extractor-schema-", suffix=".json"
    )
    output_fd, output_name = tempfile.mkstemp(
        prefix="mv3-extractor-output-", suffix=".json"
    )
    os.close(schema_fd)
    os.close(output_fd)
    schema_path = Path(schema_name)
    output_path = Path(output_name)
    try:
        schema_path.write_text(
            json.dumps(_SCHEMA, ensure_ascii=False), encoding="utf-8"
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
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
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
            timeout=timeout if timeout is not None else _timeout(),
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            return None
        try:
            obj = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        memories = obj.get("memories") if isinstance(obj, dict) else None
        if not isinstance(memories, list):
            return None
        # Existing validation and length bounds remain centralized in
        # memory_extractor._parse_gemma_json_ex.
        return json.dumps(memories, ensure_ascii=False)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ):
        return None
    finally:
        for path in (schema_path, output_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
