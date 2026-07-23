from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import stop_scheduler


def _payload(turn: str) -> bytes:
    return json.dumps({
        "session_id": "session-1",
        "turn_id": turn,
        "hook_event_name": "Stop",
        "transcript_path": "/tmp/session.jsonl",
    }).encode()


def test_enqueue_coalesces_latest_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(stop_scheduler, "QUEUE_DIR", tmp_path)
    launches = []
    monkeypatch.setattr(
        stop_scheduler.subprocess,
        "Popen",
        lambda args, **kwargs: launches.append((args, kwargs)) or SimpleNamespace(),
    )

    assert stop_scheduler.enqueue(io.BytesIO(_payload("turn-1"))) == 0
    assert stop_scheduler.enqueue(io.BytesIO(_payload("turn-2"))) == 0

    pending = list(tmp_path.glob("*.pending.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text())["turn_id"] == "turn-2"
    assert len(launches) == 2  # lock 을 실제 획득하는 worker 하나만 처리


def test_wrapper_routes_through_scheduler():
    wrapper = (
        Path(__file__).resolve().parents[1]
        / "hooks"
        / "session-memory-end-async.sh"
    ).read_text()
    assert "stop_scheduler.py" in wrapper
    assert 'MV3_LLM_PROVIDER="${MV3_LLM_PROVIDER:-gemma}"' in wrapper
    assert "command -v codex" not in wrapper
