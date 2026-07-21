"""P1 (v4): memory-recall hook 의 agent 소스 태깅 검증.

MV3_AGENT env 로 회수 계측(metrics.jsonl)에 어느 에이전트가 발화했는지 기록한다.
미설정·비인가 값은 "unknown" — 수동 실행·제3 소비자를 claude 로 오분류하지 않는다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "memory-recall.py"


def _run_hook(tmp_path: Path, env_extra: dict) -> dict:
    """hook 을 subprocess 로 실행하고 마지막 metrics 레코드를 반환.

    내부 400ms hard timeout 에 걸리면 metric 이 안 남을 수 있어(부하 flake)
    최대 3회 재시도한다.
    """
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "MV3_DATA_DIR": str(tmp_path),
        "MV3_PROJECTS_ROOT": str(projects),
        "MV3_GEMMA_INTENT": "",
        **env_extra,
    }
    metrics = tmp_path / "metrics.jsonl"
    for _ in range(3):
        subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(
                {"session_id": "agent-tag-test", "prompt": "에이전트 태깅 검증용 프롬프트"}
            ),
            text=True,
            env=env,
            timeout=30,
            capture_output=True,
        )
        if metrics.exists() and metrics.read_text().strip():
            break
    lines = metrics.read_text().strip().splitlines()
    return json.loads(lines[-1])


def test_agent_field_codex(tmp_path):
    assert _run_hook(tmp_path, {"MV3_AGENT": "codex"})["agent"] == "codex"


def test_agent_field_claude(tmp_path):
    assert _run_hook(tmp_path, {"MV3_AGENT": "claude"})["agent"] == "claude"


def test_agent_field_unset_is_unknown(tmp_path):
    assert _run_hook(tmp_path, {"MV3_AGENT": ""})["agent"] == "unknown"


def test_agent_field_invalid_is_unknown(tmp_path):
    assert _run_hook(tmp_path, {"MV3_AGENT": "gemini"})["agent"] == "unknown"
