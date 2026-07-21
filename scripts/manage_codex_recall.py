#!/usr/bin/env python3
"""Install or remove MindVault recall and session extraction from Codex hooks.

Codex accepts the same ``prompt`` input field as Claude Code and treats a
successful hook's non-JSON stdout as model-visible additional context.  The
existing ``memory-recall.py`` hook can therefore be reused directly.  Codex
uses the turn-scoped ``Stop`` event as its SessionEnd equivalent; the deployed
async wrapper forwards that payload to MindVault without blocking Codex.

This module is the single writer for both managed entries in
``~/.codex/hooks.json`` and preserves every unrelated hook.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any


RECALL_HOOK_MARKER = "mindvault-v3-codex-recall"
SESSION_END_HOOK_MARKER = "mindvault-v3-codex-session-end"
RECALL_EVENT_NAME = "UserPromptSubmit"
SESSION_END_EVENT_NAME = "Stop"

# Backward-compatible aliases used by existing tests and callers.
HOOK_MARKER = RECALL_HOOK_MARKER
EVENT_NAME = RECALL_EVENT_NAME


class HookConfigError(RuntimeError):
    """Raised when the Codex hook configuration cannot be safely changed."""


def default_config_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return codex_home / "hooks.json"


def default_recall_hook_path() -> Path:
    override = os.environ.get("MV3_CODEX_RECALL_HOOK", "").strip()
    if override:
        return Path(override).expanduser()
    return Path("~/.claude/hooks/memory-recall.py").expanduser()


def default_session_end_hook_path() -> Path:
    override = os.environ.get("MV3_CODEX_SESSION_END_HOOK", "").strip()
    if override:
        return Path(override).expanduser()
    return Path("~/.claude/hooks/session-memory-end-async.sh").expanduser()


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise HookConfigError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HookConfigError(f"top-level hook config must be an object: {path}")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HookConfigError(f"'hooks' must be an object: {path}")
    return data


def _is_managed_handler(handler: Any, hook_path: Path, marker: str) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    if marker in command:
        return True
    try:
        argv = shlex.split(command, comments=True)
    except ValueError:
        return False
    return bool(argv) and argv[0] == str(hook_path)


def _remove_managed_handlers(
    data: dict[str, Any], event_name: str, hook_path: Path, marker: str
) -> tuple[int, list[dict[str, Any]]]:
    hooks = data["hooks"]
    events = hooks.get(event_name, [])
    if not isinstance(events, list):
        raise HookConfigError(f"hooks.{event_name} must be an array")

    removed = 0
    kept_events: list[dict[str, Any]] = []
    for entry in events:
        if not isinstance(entry, dict):
            raise HookConfigError(f"hooks.{event_name} entries must be objects")
        handlers = entry.get("hooks", [])
        if not isinstance(handlers, list):
            raise HookConfigError(f"hooks.{event_name}[].hooks must be an array")
        kept_handlers = []
        for handler in handlers:
            if _is_managed_handler(handler, hook_path, marker):
                removed += 1
            else:
                kept_handlers.append(handler)
        if kept_handlers:
            updated_entry = dict(entry)
            updated_entry["hooks"] = kept_handlers
            kept_events.append(updated_entry)
    return removed, kept_events


def _managed_entry(hook_path: Path, marker: str, env_prefix: str = "") -> dict[str, Any]:
    prefix = f"{env_prefix} " if env_prefix else ""
    command = f"{prefix}{shlex.quote(str(hook_path))} # {marker}"
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 2,
            }
        ]
    }


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    json.loads(serialized)

    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass


def _validate_executable(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise HookConfigError(f"{label} not found: {resolved}")
    if not os.access(resolved, os.X_OK):
        raise HookConfigError(f"{label} is not executable: {resolved}")
    return resolved


def install(
    config_path: Path, recall_hook: Path, session_end_hook: Path | None = None
) -> bool:
    recall_hook = _validate_executable(recall_hook, "recall hook")
    session_end_hook = _validate_executable(
        session_end_hook or default_session_end_hook_path(), "session-end hook"
    )

    data = _load_config(config_path)
    specs = (
        (
            RECALL_EVENT_NAME,
            recall_hook,
            RECALL_HOOK_MARKER,
            "MV3_AGENT=codex",
        ),
        (
            SESSION_END_EVENT_NAME,
            session_end_hook,
            SESSION_END_HOOK_MARKER,
            "",
        ),
    )
    updates: dict[str, list[dict[str, Any]]] = {}
    for event_name, hook_path, marker, env_prefix in specs:
        _removed, kept_events = _remove_managed_handlers(
            data, event_name, hook_path, marker
        )
        desired_events = kept_events + [
            _managed_entry(hook_path, marker, env_prefix)
        ]
        if data["hooks"].get(event_name, []) != desired_events:
            updates[event_name] = desired_events
    if not updates:
        return False
    data["hooks"].update(updates)
    _atomic_write(config_path, data)
    return True


def uninstall(
    config_path: Path, recall_hook: Path, session_end_hook: Path | None = None
) -> bool:
    if not config_path.exists():
        return False
    recall_hook = recall_hook.expanduser().resolve()
    session_end_hook = (
        session_end_hook or default_session_end_hook_path()
    ).expanduser().resolve()
    data = _load_config(config_path)
    total_removed = 0
    for event_name, hook_path, marker in (
        (RECALL_EVENT_NAME, recall_hook, RECALL_HOOK_MARKER),
        (SESSION_END_EVENT_NAME, session_end_hook, SESSION_END_HOOK_MARKER),
    ):
        removed, kept_events = _remove_managed_handlers(
            data, event_name, hook_path, marker
        )
        total_removed += removed
        if kept_events:
            data["hooks"][event_name] = kept_events
        else:
            data["hooks"].pop(event_name, None)
    if total_removed == 0:
        return False
    _atomic_write(config_path, data)
    return True


def _matching_handlers(
    data: dict[str, Any], event_name: str, hook_path: Path, marker: str
) -> int:
    events = data["hooks"].get(event_name, [])
    matches = 0
    if isinstance(events, list):
        for entry in events:
            if not isinstance(entry, dict):
                continue
            handlers = entry.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            matches += sum(
                _is_managed_handler(handler, hook_path, marker)
                for handler in handlers
            )
    return matches


def status(
    config_path: Path, recall_hook: Path, session_end_hook: Path | None = None
) -> dict[str, Any]:
    recall_hook = recall_hook.expanduser().resolve()
    session_end_hook = (
        session_end_hook or default_session_end_hook_path()
    ).expanduser().resolve()
    data = _load_config(config_path)
    recall_matches = _matching_handlers(
        data, RECALL_EVENT_NAME, recall_hook, RECALL_HOOK_MARKER
    )
    session_end_matches = _matching_handlers(
        data, SESSION_END_EVENT_NAME, session_end_hook, SESSION_END_HOOK_MARKER
    )
    return {
        "installed": recall_matches == 1 and session_end_matches == 1,
        "matching_handlers": recall_matches + session_end_matches,
        "recall_installed": recall_matches == 1,
        "recall_matching_handlers": recall_matches,
        "session_end_installed": session_end_matches == 1,
        "session_end_matching_handlers": session_end_matches,
        "config_path": str(config_path),
        "recall_hook": str(recall_hook),
        "recall_hook_exists": recall_hook.is_file(),
        "recall_hook_executable": os.access(recall_hook, os.X_OK),
        "session_end_hook": str(session_end_hook),
        "session_end_hook_exists": session_end_hook.is_file(),
        "session_end_hook_executable": os.access(session_end_hook, os.X_OK),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage MindVault Codex recall and Stop session extraction hooks."
    )
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--hook", type=Path, default=default_recall_hook_path())
    parser.add_argument(
        "--session-end-hook", type=Path, default=default_session_end_hook_path()
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.action == "install":
            changed = install(args.config, args.hook, args.session_end_hook)
            print("installed" if changed else "already installed")
        elif args.action == "uninstall":
            changed = uninstall(args.config, args.hook, args.session_end_hook)
            print("uninstalled" if changed else "not installed")
        else:
            print(
                json.dumps(
                    status(args.config, args.hook, args.session_end_hook),
                    ensure_ascii=False,
                )
            )
        return 0
    except HookConfigError as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
