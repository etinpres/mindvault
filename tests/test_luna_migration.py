from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_sensitive_marker_skips_codex_subprocess(monkeypatch):
    import llm_backend

    def must_not_run(*args, **kwargs):
        raise AssertionError("sensitive prompt reached subprocess")

    monkeypatch.setattr(llm_backend.subprocess, "run", must_not_run)
    assert llm_backend.call_codex_text(
        "compile", "token=[REDACTED_KEY]"
    ) is None


def test_session_search_batches_rerank_and_summary(monkeypatch):
    import llm_backend
    import search

    calls = []

    def fake(prompt):
        calls.append(prompt)
        return [
            {"index": 2, "summary": "세 번째"},
            {"index": 0, "summary": "첫 번째"},
            {"index": 2, "summary": "중복"},
            {"index": 99, "summary": "범위 밖"},
        ]

    monkeypatch.setattr(llm_backend, "call_codex_search", fake)
    candidates = [{"session_id": str(i), "first_ts": ""} for i in range(3)]
    rows = search.luna_enrich(
        "질의", candidates, ["a", "b", "c"], k=3
    )
    assert [row["index"] for row in rows] == [2, 0]
    assert len(calls) == 1


def test_contradictions_are_classified_in_one_call(monkeypatch):
    import contradiction_detector
    import llm_backend

    calls = []

    def fake(prompt):
        calls.append(prompt)
        return [
            {
                "index": 0,
                "kind": "metric_update",
                "reason": "값 갱신",
                "confidence": 0.91,
            },
            {
                "index": 1,
                "kind": "no_conflict",
                "reason": "양립",
                "confidence": 0.99,
            },
        ]

    monkeypatch.setattr(llm_backend, "call_codex_contradictions", fake)
    rows = contradiction_detector._classify_pairs(
        "새 값 2", ["기존 값 1", "다른 주제"]
    )
    assert rows[0]["kind"] == "metric_update"
    assert rows[1]["kind"] == "no_conflict"
    assert len(calls) == 1


def test_runtime_has_no_local_gemma_endpoint():
    roots = [
        ROOT / "src",
        ROOT / "hooks",
        ROOT / "scripts",
        ROOT / "plist",
    ]
    offenders = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py", ".sh", ".plist", ".json"
            }:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if (
                "http://localhost:8080" in text
                or "http://127.0.0.1:8080" in text
            ):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_retired_model_assets_are_absent():
    assert not (ROOT / "scripts/gemma_server_runner.sh").exists()
    assert not (ROOT / "plist/com.mindvault.gemma-mlx.plist").exists()
