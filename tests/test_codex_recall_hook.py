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

    def test_install_preserves_existing_herdr_hook(self):
        herdr = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "herdr-state session"}]}
                ]
            }
        }
        self._write(herdr)

        self.assertTrue(MANAGER.install(self.config, self.hook))
        data = self._read()
        self.assertEqual(data["hooks"]["SessionStart"], herdr["hooks"]["SessionStart"])
        registered = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertIn(MANAGER.HOOK_MARKER, registered["command"])
        self.assertTrue(registered["command"].startswith("MV3_AGENT=codex "))
        self.assertIn(str(self.hook.resolve()), registered["command"])
        self.assertEqual(registered["timeout"], 2)

    def test_install_is_idempotent(self):
        self._write({"hooks": {}})
        self.assertTrue(MANAGER.install(self.config, self.hook))
        first = self.config.read_text(encoding="utf-8")
        self.assertFalse(MANAGER.install(self.config, self.hook))
        self.assertEqual(self.config.read_text(encoding="utf-8"), first)
        state = MANAGER.status(self.config, self.hook)
        self.assertTrue(state["installed"])
        self.assertEqual(state["matching_handlers"], 1)

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
                    ]
                }
            }
        )

        self.assertTrue(MANAGER.install(self.config, self.hook))
        events = self._read()["hooks"]["UserPromptSubmit"]
        commands = [handler["command"] for event in events for handler in event["hooks"]]
        self.assertEqual(sum(MANAGER.HOOK_MARKER in cmd for cmd in commands), 1)
        self.assertIn("/opt/foreign-hook", commands)

    def test_uninstall_removes_only_managed_handler(self):
        self._write(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "/opt/foreign-hook"}]}
                    ]
                }
            }
        )
        MANAGER.install(self.config, self.hook)

        self.assertTrue(MANAGER.uninstall(self.config, self.hook))
        events = self._read()["hooks"]["UserPromptSubmit"]
        self.assertEqual(
            events,
            [{"hooks": [{"type": "command", "command": "/opt/foreign-hook"}]}],
        )
        self.assertFalse(MANAGER.uninstall(self.config, self.hook))

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

        self.assertTrue(MANAGER.install(self.config, self.hook))
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
            MANAGER.install(self.config, self.hook)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{not-json")

    def test_missing_or_non_executable_hook_is_rejected(self):
        missing = self.hook.with_name("missing.py")
        with self.assertRaises(MANAGER.HookConfigError):
            MANAGER.install(self.config, missing)

        self.hook.chmod(0o644)
        with self.assertRaises(MANAGER.HookConfigError):
            MANAGER.install(self.config, self.hook)

    def test_changed_config_creates_backup_and_valid_json(self):
        original = {"description": "keep me", "hooks": {}}
        self._write(original)
        self.assertTrue(MANAGER.install(self.config, self.hook))
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
