"""Codex UserPromptSubmit registration for shared MindVault recall."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "manage_codex_recall.py"
SPEC = importlib.util.spec_from_file_location("manage_codex_recall", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MANAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANAGER)


class TestCodexRecallHookManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / ".codex" / "hooks.json"
        self.hook = root / ".claude" / "hooks" / "memory-recall.py"
        self.hook.parent.mkdir(parents=True)
        self.hook.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.hook.chmod(0o755)
        self.session_end_hook = self.hook.with_name("session-memory-end-async.sh")
        self.session_end_hook.write_text("#!/bin/sh\n", encoding="utf-8")
        self.session_end_hook.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _read(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def _install(self):
        return MANAGER.install(self.config, self.hook, self.session_end_hook)

    def _uninstall(self):
        return MANAGER.uninstall(self.config, self.hook, self.session_end_hook)

    def _status(self):
        return MANAGER.status(self.config, self.hook, self.session_end_hook)

    def test_install_preserves_existing_herdr_hook(self):
        herdr = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "herdr-state session"}]}
                ]
            }
        }
        self._write(herdr)

        self.assertTrue(self._install())
        data = self._read()
        self.assertEqual(data["hooks"]["SessionStart"], herdr["hooks"]["SessionStart"])
        registered = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertIn(MANAGER.HOOK_MARKER, registered["command"])
        self.assertTrue(registered["command"].startswith("MV3_AGENT=codex "))
        self.assertIn(str(self.hook.resolve()), registered["command"])
        self.assertEqual(registered["timeout"], 2)
        stop = data["hooks"]["Stop"][0]["hooks"][0]
        self.assertIn(MANAGER.SESSION_END_HOOK_MARKER, stop["command"])
        self.assertIn(str(self.session_end_hook.resolve()), stop["command"])
        self.assertEqual(stop["timeout"], 2)

    def test_install_is_idempotent(self):
        self._write({"hooks": {}})
        self.assertTrue(self._install())
        first = self.config.read_text(encoding="utf-8")
        self.assertFalse(self._install())
        self.assertEqual(self.config.read_text(encoding="utf-8"), first)
        state = self._status()
        self.assertTrue(state["installed"])
        self.assertEqual(state["matching_handlers"], 2)
        self.assertTrue(state["recall_installed"])
        self.assertTrue(state["session_end_installed"])
        self.assertEqual(state["recall_matching_handlers"], 1)
        self.assertEqual(state["session_end_matching_handlers"], 1)

    def test_install_replaces_stale_duplicate_and_preserves_foreign_handler(self):
        self._write(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{self.hook} # {MANAGER.HOOK_MARKER}",
                                },
                                {"type": "command", "command": "/opt/foreign-hook"},
                            ]
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{self.hook} # {MANAGER.HOOK_MARKER}",
                                }
                            ]
                        },
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        f"{self.session_end_hook} "
                                        f"# {MANAGER.SESSION_END_HOOK_MARKER}"
                                    ),
                                },
                                {"type": "command", "command": "/opt/foreign-stop"},
                            ]
                        }
                    ],
                }
            }
        )

        self.assertTrue(self._install())
        events = self._read()["hooks"]["UserPromptSubmit"]
        commands = [handler["command"] for event in events for handler in event["hooks"]]
        self.assertEqual(sum(MANAGER.HOOK_MARKER in cmd for cmd in commands), 1)
        self.assertIn("/opt/foreign-hook", commands)
        stop_events = self._read()["hooks"]["Stop"]
        stop_commands = [
            handler["command"]
            for event in stop_events
            for handler in event["hooks"]
        ]
        self.assertEqual(
            sum(MANAGER.SESSION_END_HOOK_MARKER in cmd for cmd in stop_commands),
            1,
        )
        self.assertIn("/opt/foreign-stop", stop_commands)

    def test_uninstall_removes_only_managed_handler(self):
        self._write(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "/opt/foreign-hook"}]}
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/opt/foreign-stop"}]}
                    ],
                }
            }
        )
        self._install()

        self.assertTrue(self._uninstall())
        hooks = self._read()["hooks"]
        events = hooks["UserPromptSubmit"]
        self.assertEqual(
            events,
            [{"hooks": [{"type": "command", "command": "/opt/foreign-hook"}]}],
        )
        self.assertEqual(
            hooks["Stop"],
            [{"hooks": [{"type": "command", "command": "/opt/foreign-stop"}]}],
        )
        self.assertFalse(self._uninstall())

    def test_similar_path_is_not_treated_as_managed(self):
        lookalike = f"{self.hook}-foreign"
        self._write(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": lookalike}]}
                    ]
                }
            }
        )

        self.assertTrue(self._install())
        commands = [
            handler["command"]
            for event in self._read()["hooks"]["UserPromptSubmit"]
            for handler in event["hooks"]
        ]
        self.assertIn(lookalike, commands)

    def test_invalid_json_fails_closed(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(MANAGER.HookConfigError):
            self._install()
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{not-json")

    def test_missing_or_non_executable_hook_is_rejected(self):
        missing = self.hook.with_name("missing.py")
        with self.assertRaises(MANAGER.HookConfigError):
            MANAGER.install(self.config, missing, self.session_end_hook)

        self.hook.chmod(0o644)
        with self.assertRaises(MANAGER.HookConfigError):
            self._install()

        self.hook.chmod(0o755)
        self.session_end_hook.chmod(0o644)
        with self.assertRaises(MANAGER.HookConfigError):
            self._install()

    def test_missing_session_end_hook_leaves_config_unchanged(self):
        original = {"hooks": {"SessionStart": [{"hooks": []}]}}
        self._write(original)
        missing = self.session_end_hook.with_name("missing-session-end.sh")

        with self.assertRaises(MANAGER.HookConfigError):
            MANAGER.install(self.config, self.hook, missing)

        self.assertEqual(self._read(), original)

    def test_changed_config_creates_backup_and_valid_json(self):
        original = {"description": "keep me", "hooks": {}}
        self._write(original)
        self.assertTrue(self._install())
        backup = self.config.with_suffix(".json.bak")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)
        json.loads(self.config.read_text(encoding="utf-8"))

    def test_main_uninstall_unwires_optional_codex_hook(self):
        uninstall_script = SCRIPT.parent.parent / "uninstall.sh"
        body = uninstall_script.read_text(encoding="utf-8")
        self.assertIn("manage_codex_recall.py", body)
        self.assertIn('python3 "$CODEX_HOOK_MANAGER" uninstall', body)


if __name__ == "__main__":
    unittest.main()
