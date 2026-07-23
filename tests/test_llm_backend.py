from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import llm_backend


def test_codex_cli_uses_luna_low_structured_output(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        out_path = Path(args[args.index("-o") + 1])
        out_path.write_text(json.dumps({
            "memories": [{
                "type": "project",
                "title": "Luna 전환",
                "body": "추출 백엔드를 Luna로 전환한다.",
                "reason": "운영 결정",
                "evidence": "Luna를 써줘",
            }]
        }))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(llm_backend.subprocess, "run", fake_run)
    monkeypatch.setenv("MV3_LLM_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("MV3_LLM_EFFORT", "low")

    raw = llm_backend.call_codex_extractor("prompt", timeout=17)
    parsed = json.loads(raw)
    assert parsed[0]["title"] == "Luna 전환"
    args = captured["args"]
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert "--sandbox" in args and "read-only" in args
    assert args[args.index("-m") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="low"' in args
    assert "--output-schema" in args
    assert captured["kwargs"]["env"]["MV3_HOOK_RECURSION_GUARD"] == "1"
    assert captured["kwargs"]["timeout"] == 17


def test_codex_cli_failure_returns_none(monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stderr="auth failed", stdout="")

    monkeypatch.setattr(llm_backend.subprocess, "run", fake_run)
    assert llm_backend.call_codex_extractor("prompt") is None


def test_extractor_provider_has_no_luna_retry(monkeypatch):
    import memory_extractor

    monkeypatch.setenv("MV3_LLM_PROVIDER", "codex_cli")
    monkeypatch.delenv("MV3_EXTRACTOR_RETRIES", raising=False)
    assert memory_extractor._retries() == 0
    monkeypatch.setenv("MV3_LLM_PROVIDER", "gemma")
    assert memory_extractor._retries() == 0
