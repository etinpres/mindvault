from __future__ import annotations

import json
from pathlib import Path

import memory_extractor
import extractor_cache


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex_sessions"


def test_incremental_success_commits_cursor(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text((FIXTURES / "interactive.jsonl").read_text())
    monkeypatch.setattr(memory_extractor, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("MV3_LLM_PROVIDER", "codex_cli")
    monkeypatch.setenv("MV3_EXTRACTOR_ALWAYS_FIRE", "1")
    monkeypatch.setattr(extractor_cache, "cache_get", lambda prompt: None)
    monkeypatch.setattr(extractor_cache, "cache_put", lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_extractor, "call_model", lambda prompt: "[]")

    result = memory_extractor.extract_incremental_codex(
        transcript, "session-success"
    )
    assert result.success is True
    assert result.candidates == []
    cursor = json.loads(
        (tmp_path / "data" / "codex_extraction_cursors.json").read_text()
    )
    assert cursor["session-success"]["offset"] == transcript.stat().st_size


def test_incremental_failure_does_not_advance_cursor(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text((FIXTURES / "interactive.jsonl").read_text())
    monkeypatch.setattr(memory_extractor, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("MV3_LLM_PROVIDER", "codex_cli")
    monkeypatch.setenv("MV3_EXTRACTOR_ALWAYS_FIRE", "1")
    monkeypatch.setattr(extractor_cache, "cache_get", lambda prompt: None)
    monkeypatch.setattr(extractor_cache, "cache_put", lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_extractor, "call_model", lambda prompt: None)

    result = memory_extractor.extract_incremental_codex(
        transcript, "session-failure"
    )
    assert result.success is False
    cursor_path = tmp_path / "data" / "codex_extraction_cursors.json"
    assert not cursor_path.exists()
