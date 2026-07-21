#!/usr/bin/env python3
"""Install or remove MindVault recall from Codex UserPromptSubmit hooks.

Codex accepts the same ``prompt`` input field as Claude Code and treats a
successful hook's non-JSON stdout as model-visible additional context.  The
existing ``memory-recall.py`` hook can therefore be reused directly; this
module only manages ``~/.codex/hooks.json`` safely and idempotently.
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


HOOK_MARKER = "mindvault-v3-codex-recall"
EVENT_NAME = "UserPromptSubmit"


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


def _is_managed_handler(handler: Any, recall_hook: Path) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    if HOOK_MARKER in command:
        return True
    try:
        argv = shlex.split(command, comments=True)
    except ValueError:
        return False
    return bool(argv) and argv[0] == str(recall_hook)


def _remove_managed_handlers(
    data: dict[str, Any], recall_hook: Path
) -> tuple[int, list[dict[str, Any]]]:
    hooks = data["hooks"]
    events = hooks.get(EVENT_NAME, [])
    if not isinstance(events, list):
        raise HookConfigError(f"hooks.{EVENT_NAME} must be an array")

    removed = 0
    kept_events: list[dict[str, Any]] = []
    for entry in events:
        if not isinstance(entry, dict):
            raise HookConfigError(f"hooks.{EVENT_NAME} entries must be objects")
        handlers = entry.get("hooks", [])
        if not isinstance(handlers, list):
            raise HookConfigError(f"hooks.{EVENT_NAME}[].hooks must be an array")
        kept_handlers = []
        for handler in handlers:
            if _is_managed_handler(handler, recall_hook):
                removed += 1
            else:
                kept_handlers.append(handler)
        if kept_handlers:
            updated_entry = dict(entry)
            updated_entry["hooks"] = kept_handlers
            kept_events.append(updated_entry)
    return removed, kept_events


def _managed_entry(recall_hook: Path) -> dict[str, Any]:
    command = f"MV3_AGENT=codex {shlex.quote(str(recall_hook))} # {HOOK_MARKER}"
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


def install(config_path: Path, recall_hook: Path) -> bool:
    recall_hook = recall_hook.expanduser().resolve()
    if not recall_hook.is_file():
        raise HookConfigError(f"recall hook not found: {recall_hook}")
    if not os.access(recall_hook, os.X_OK):
        raise HookConfigError(f"recall hook is not executable: {recall_hook}")

    data = _load_config(config_path)
    _removed, kept_events = _remove_managed_handlers(data, recall_hook)
    desired_entry = _managed_entry(recall_hook)
    existing_events = data["hooks"].get(EVENT_NAME, [])
    desired_events = kept_events + [desired_entry]
    if existing_events == desired_events:
        return False
    data["hooks"][EVENT_NAME] = desired_events
    _atomic_write(config_path, data)
    return True


def uninstall(config_path: Path, recall_hook: Path) -> bool:
    if not config_path.exists():
        return False
    recall_hook = recall_hook.expanduser().resolve()
    data = _load_config(config_path)
    removed, kept_events = _remove_managed_handlers(data, recall_hook)
    if removed == 0:
        return False
    if kept_events:
        data["hooks"][EVENT_NAME] = kept_events
    else:
        data["hooks"].pop(EVENT_NAME, None)
    _atomic_write(config_path, data)
    return True


def status(config_path: Path, recall_hook: Path) -> dict[str, Any]:
    recall_hook = recall_hook.expanduser().resolve()
    data = _load_config(config_path)
    events = data["hooks"].get(EVENT_NAME, [])
    matches = 0
    if isinstance(events, list):
        for entry in events:
            if not isinstance(entry, dict):
                continue
            handlers = entry.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            matches += sum(_is_managed_handler(handler, recall_hook) for handler in handlers)
    return {
        "installed": matches == 1,
        "matching_handlers": matches,
        "config_path": str(config_path),
        "recall_hook": str(recall_hook),
        "recall_hook_exists": recall_hook.is_file(),
        "recall_hook_executable": os.access(recall_hook, os.X_OK),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage MindVault read-only recall for Codex UserPromptSubmit."
    )
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--hook", type=Path, default=default_recall_hook_path())
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.action == "install":
            changed = install(args.config, args.hook)
            print("installed" if changed else "already installed")
        elif args.action == "uninstall":
            changed = uninstall(args.config, args.hook)
            print("uninstalled" if changed else "not installed")
        else:
            print(json.dumps(status(args.config, args.hook), ensure_ascii=False))
        return 0
    except HookConfigError as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
