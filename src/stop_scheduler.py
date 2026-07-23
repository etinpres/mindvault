#!/usr/bin/env python3
"""Quiet-period coalescer for MindVault SessionEnd/Stop hooks.

Enqueue returns immediately. A detached worker keeps only the newest payload per
session, waits for a short quiet period, then serializes extraction globally.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO


DATA_DIR = Path(
    os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")
).expanduser()
QUEUE_DIR = DATA_DIR / "stop-queue"
GLOBAL_LOCK = QUEUE_DIR / "global-extraction.lock"


def _key(payload: dict) -> str:
    raw = str(
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("transcript_path")
        or "unknown"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _pending(key: str) -> Path:
    return QUEUE_DIR / f"{key}.pending.json"


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def enqueue(stream: BinaryIO | None = None) -> int:
    """Store latest payload and launch a best-effort detached worker."""
    if os.environ.get("MV3_HOOK_RECURSION_GUARD") == "1":
        return 0
    source = stream if stream is not None else sys.stdin.buffer
    try:
        body = source.read()
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return 0
        key = _key(payload)
        _atomic_write(_pending(key), body)
        env = os.environ.copy()
        env["MV3_HOOK_RECURSION_GUARD"] = "0"
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker", key],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return 0


def _debounce_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("MV3_STOP_DEBOUNCE_SECONDS", "20")))
    except ValueError:
        return 20.0


def _session_end_script() -> Path:
    override = os.environ.get("MV3_SESSION_END_SCRIPT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path("~/.claude/hooks/session-memory-end.py").expanduser()


def _wait_until_quiet(path: Path) -> bool:
    quiet = _debounce_seconds()
    while path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        remaining = quiet - age
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 1.0))
    return False


def worker(key: str) -> int:
    """Only one worker per session; only one extraction globally."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = QUEUE_DIR / f"{key}.worker.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        pending = _pending(key)
        while _wait_until_quiet(pending):
            try:
                body = pending.read_bytes()
            except OSError:
                return 0
            digest = hashlib.sha256(body).digest()
            script = _session_end_script()
            if not script.is_file():
                return 0
            with GLOBAL_LOCK.open("a+") as global_lock:
                fcntl.flock(global_lock, fcntl.LOCK_EX)
                env = os.environ.copy()
                env["MV3_HOOK_RECURSION_GUARD"] = "0"
                try:
                    subprocess.run(
                        [sys.executable, str(script)],
                        input=body,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=max(90, int(_debounce_seconds()) + 90),
                        env=env,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError, ValueError):
                    return 0
            try:
                current = pending.read_bytes()
            except OSError:
                return 0
            if hashlib.sha256(current).digest() == digest:
                try:
                    pending.unlink()
                except OSError:
                    pass
                return 0
            # A newer Stop arrived while extraction ran; debounce the new payload.
        return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["worker"] and len(args) == 2:
        return worker(args[1])
    return enqueue()


if __name__ == "__main__":
    raise SystemExit(main())
