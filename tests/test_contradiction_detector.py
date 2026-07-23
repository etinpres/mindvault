import pytest
from pathlib import Path
from src.contradiction_detector import (
    Contradiction, ContradictionKind, detect_contradictions,
)

def test_contradiction_kind_enum_values():
    assert ContradictionKind.METRIC_UPDATE.value == "metric_update"
    assert ContradictionKind.DECISION_REVERSAL.value == "decision_reversal"
    assert ContradictionKind.FACT_CORRECTION.value == "fact_correction"
    assert ContradictionKind.NO_CONFLICT.value == "no_conflict"

def test_contradiction_dataclass_minimal():
    c = Contradiction(
        target_path=Path("/x/a.md"),
        target_name="a",
        kind=ContradictionKind.METRIC_UPDATE,
        reason="hit rate 65 -> 66.3",
        confidence=0.85,
    )
    assert c.kind == ContradictionKind.METRIC_UPDATE
    assert 0 <= c.confidence <= 1
    assert c.new_body_excerpt == ""

def test_detect_returns_empty_when_mem_dir_empty(tmp_path):
    candidate = {"slug": "x", "title": "X", "body": "no related"}
    result = detect_contradictions(candidate, tmp_path)
    assert result == []


def test_recall_candidates_excludes_self_slug(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import _recall_candidates

    own = write_memory(tmp_path, "new_metric.md",
                       "name: new-metric\ntype: feedback", "hit rate 66.3%")
    other = write_memory(tmp_path, "old_metric.md",
                         "name: old-metric\ntype: feedback", "hit rate 65%")

    def fake_hybrid(query, mem_dir, top_k):
        return [(own, 0.95), (other, 0.85)]
    monkeypatch.setattr("src.contradiction_detector._hybrid_search", fake_hybrid)

    candidate = {"slug": "new-metric", "title": "회수율", "body": "hit rate 66.3%"}
    found = _recall_candidates(candidate, tmp_path, top_k=5)

    paths = [p for p, _ in found]
    assert own not in paths
    assert other in paths


def test_recall_candidates_excludes_self_by_explicit_path(tmp_path, write_memory, monkeypatch):
    """When candidate carries 'path', exclude by path identity (most reliable)."""
    from src.contradiction_detector import _recall_candidates

    own = write_memory(tmp_path, "feedback_new_metric.md",
                       "name: new-metric\ntype: feedback", "hit 66.3%")
    other = write_memory(tmp_path, "feedback_old_metric.md",
                         "name: old-metric\ntype: feedback", "hit 65%")

    monkeypatch.setattr(
        "src.contradiction_detector._hybrid_search",
        lambda q, m, top_k: [(own, 0.95), (other, 0.85)],
    )

    # Note: slug="new-metric" but stem is "feedback_new_metric" (type prefix).
    # The current stem-only logic would FAIL to exclude own. Path identity catches it.
    candidate = {"slug": "new-metric", "title": "회수율", "body": "hit 66.3%",
                 "path": own}
    found = _recall_candidates(candidate, tmp_path)
    paths = [p for p, _ in found]
    assert own not in paths
    assert other in paths


def test_recall_candidates_fallback_stem_suffix_match(tmp_path, write_memory, monkeypatch):
    """When no 'path' in candidate, fall back to stem suffix match (handles <type>_<slug>)."""
    from src.contradiction_detector import _recall_candidates

    own = write_memory(tmp_path, "procedural_my_thing.md",
                       "name: my-thing\ntype: procedural", "body")
    other = write_memory(tmp_path, "feedback_other.md",
                         "name: other\ntype: feedback", "body")

    monkeypatch.setattr(
        "src.contradiction_detector._hybrid_search",
        lambda q, m, top_k: [(own, 0.95), (other, 0.85)],
    )

    candidate = {"slug": "my-thing", "title": "t", "body": "b"}  # no "path"
    found = _recall_candidates(candidate, tmp_path)
    paths = [p for p, _ in found]
    assert own not in paths, f"stem suffix match failed: own={own.stem} should match slug=my-thing"
    assert other in paths


def test_hybrid_search_logs_debug_on_recall_failure(tmp_path, monkeypatch):
    """recall_memory raising → _debug log + return [] (no propagation)."""
    from src.contradiction_detector import _hybrid_search

    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    def boom(query, top_k):
        raise RuntimeError("simulated DB down")
    monkeypatch.setattr("memory_search.recall_memory", boom)

    result = _hybrid_search("any query", tmp_path)
    assert result == []

    log = tmp_path / "debug.log"
    assert log.exists()
    contents = log.read_text(encoding="utf-8")
    assert "recall_memory failed" in contents
    assert "simulated DB down" in contents


def test_classify_metric_update(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "metric_update", "reason": "65% → 66.3%", "confidence": 0.92}',
    )
    result = _classify_pair("hit rate 66.3% (n=3,193)", "hit rate 65% (n=2,397)")
    assert result["kind"] == "metric_update"
    assert result["confidence"] >= 0.8


def test_classify_no_conflict_unrelated(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "no_conflict", "reason": "주제 다름", "confidence": 0.95}',
    )
    assert _classify_pair("python tip", "scanner CLI")["kind"] == "no_conflict"


def test_classify_handles_gemma_failure(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: None,
    )
    assert _classify_pair("a", "b") is None


def test_classify_handles_malformed_json(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: "not json {{",
    )
    assert _classify_pair("a", "b") is None


def test_classify_strips_code_fences(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '```json\n{"kind": "fact_correction", "reason": "r", "confidence": 0.8}\n```',
    )
    assert _classify_pair("a", "b")["kind"] == "fact_correction"


def test_classify_strips_fences_without_trailing_newline(monkeypatch):
    """Gemma sometimes emits ```json\\n{...}``` (no \\n before closing fence)."""
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '```json\n{"kind": "metric_update", "reason": "r", "confidence": 0.8}```',
    )
    result = _classify_pair("a", "b")
    assert result is not None, "should strip fence even without trailing newline"
    assert result["kind"] == "metric_update"


def test_classify_strips_inline_fences(monkeypatch):
    """Inline single-line fence: ```{...}```"""
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '```{"kind": "fact_correction", "reason": "r", "confidence": 0.9}```',
    )
    result = _classify_pair("a", "b")
    assert result is not None, "should strip inline fence"
    assert result["kind"] == "fact_correction"


def test_detect_end_to_end_metric_drift(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import detect_contradictions, ContradictionKind

    old = write_memory(tmp_path, "feedback_old_metric.md",
                       "name: old-metric\ndescription: 회수율\ntype: feedback",
                       "hit rate 65% (n=2,397)")

    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.85)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: {"kind": "metric_update", "reason": "65 → 66.3", "confidence": 0.9},
    )

    candidate = {"slug": "new-metric", "title": "회수율 재측",
                 "body": "hit rate 66.3% (n=3,193)"}
    result = detect_contradictions(candidate, tmp_path)

    assert len(result) == 1
    assert result[0].kind == ContradictionKind.METRIC_UPDATE
    assert result[0].confidence == 0.9
    assert "65" in result[0].old_body_excerpt
    assert "66.3" in result[0].new_body_excerpt
    # excerpt is BODY_EXCERPT_CHARS-bounded
    assert len(result[0].new_body_excerpt) <= 200
    assert len(result[0].old_body_excerpt) <= 200


def test_detect_filters_low_confidence(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import detect_contradictions
    old = write_memory(tmp_path, "x.md", "name: x\ntype: feedback", "old text")
    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.7)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: {"kind": "fact_correction", "reason": "?", "confidence": 0.5},
    )
    assert detect_contradictions({"slug": "y", "body": "new", "title": "Y"}, tmp_path) == []


def test_detect_filters_no_conflict(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import detect_contradictions
    old = write_memory(tmp_path, "x.md", "name: x\ntype: feedback", "old text")
    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.85)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: {"kind": "no_conflict", "reason": "주제 다름", "confidence": 0.95},
    )
    assert detect_contradictions({"slug": "y", "body": "new", "title": "Y"}, tmp_path) == []


def test_detect_handles_classify_failure(tmp_path, write_memory, monkeypatch):
    """_classify_pair returning None → skip that candidate, don't crash."""
    from src.contradiction_detector import detect_contradictions
    old = write_memory(tmp_path, "x.md", "name: x\ntype: feedback", "old text")
    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.85)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: None,
    )
    assert detect_contradictions({"slug": "y", "body": "new", "title": "Y"}, tmp_path) == []


def test_append_to_review_queue_writes_jsonl(tmp_path, monkeypatch):
    from src.contradiction_detector import (
        append_to_review_queue, Contradiction, ContradictionKind,
    )
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    c = [Contradiction(
        target_path=tmp_path / "a.md", target_name="a",
        kind=ContradictionKind.METRIC_UPDATE, reason="r", confidence=0.9,
        new_body_excerpt="new ex", old_body_excerpt="old ex",
    )]
    out = append_to_review_queue("new-x", c, new_path=tmp_path / "new_x.md")

    assert out.exists()
    import json
    line = json.loads(out.read_text(encoding="utf-8").strip())
    assert line["new_slug"] == "new-x"
    assert line["new_path"].endswith("new_x.md")
    assert line["kind"] == "metric_update"
    assert line["resolved"] is False
    assert line["confidence"] == 0.9
    assert line["target_name"] == "a"


def test_append_to_review_queue_appends_multiple_calls(tmp_path, monkeypatch):
    """Multiple calls append to same file, don't overwrite."""
    from src.contradiction_detector import (
        append_to_review_queue, Contradiction, ContradictionKind,
    )
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    c1 = [Contradiction(target_path=tmp_path/"a.md", target_name="a",
                        kind=ContradictionKind.METRIC_UPDATE, reason="r1", confidence=0.9)]
    c2 = [Contradiction(target_path=tmp_path/"b.md", target_name="b",
                        kind=ContradictionKind.FACT_CORRECTION, reason="r2", confidence=0.8)]
    append_to_review_queue("x", c1, new_path=tmp_path / "x.md")
    out = append_to_review_queue("y", c2, new_path=tmp_path / "y.md")

    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_append_to_review_queue_swallows_oserror(tmp_path, monkeypatch):
    """OSError from open should be swallowed + logged via _debug, not propagated."""
    from src.contradiction_detector import (
        append_to_review_queue, Contradiction, ContradictionKind,
    )
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    # Force open to raise OSError. Use a directory where the jsonl file path
    # collides with a sub-directory (PermissionError/IsADirectoryError variant).
    target = tmp_path / "contradictions.jsonl"
    target.mkdir()  # path is a directory, so open() will fail

    c = [Contradiction(target_path=tmp_path/"a.md", target_name="a",
                       kind=ContradictionKind.METRIC_UPDATE, reason="r",
                       confidence=0.9)]

    # Should not raise, should return Path
    result = append_to_review_queue("x", c, new_path=tmp_path / "x.md")
    assert result is not None  # path returned


def test_append_to_review_queue_uses_utc_timestamp(tmp_path, monkeypatch):
    """ts field is UTC with Z suffix (or +0000), not naive local time."""
    import json
    from src.contradiction_detector import (
        append_to_review_queue, Contradiction, ContradictionKind,
    )
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    c = [Contradiction(target_path=tmp_path/"a.md", target_name="a",
                       kind=ContradictionKind.METRIC_UPDATE, reason="r",
                       confidence=0.9)]
    out = append_to_review_queue("x", c, new_path=tmp_path / "x.md")

    line = json.loads(out.read_text(encoding="utf-8").strip())
    ts = line["ts"]
    # Must end with Z or +0000 to indicate UTC
    assert ts.endswith("Z") or ts.endswith("+0000") or ts.endswith("+00:00"), \
        f"ts {ts!r} is not UTC-tagged — TZ drift risk per self_eval:135 warning"


def test_append_to_review_queue_uses_flock(tmp_path, monkeypatch):
    """flock LOCK_EX is acquired during write (mock the call to verify)."""
    import fcntl
    from src.contradiction_detector import (
        append_to_review_queue, Contradiction, ContradictionKind,
    )
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    flock_calls = []
    real_flock = fcntl.flock
    def tracking_flock(fd, op):
        flock_calls.append(op)
        return real_flock(fd, op)
    monkeypatch.setattr(fcntl, "flock", tracking_flock)

    c = [Contradiction(target_path=tmp_path/"a.md", target_name="a",
                       kind=ContradictionKind.METRIC_UPDATE, reason="r",
                       confidence=0.9)]
    append_to_review_queue("x", c, new_path=tmp_path / "x.md")

    # LOCK_EX should have been called
    assert fcntl.LOCK_EX in flock_calls, f"flock LOCK_EX not acquired (calls={flock_calls})"


def test_classify_rejects_list_kind(monkeypatch):
    """Gemma returning {"kind": ["metric_update"]} must return None, not crash."""
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": ["metric_update"], "reason": "r", "confidence": 0.9}',
    )
    # Must NOT raise TypeError: unhashable type: 'list'
    assert _classify_pair("a", "b") is None


def test_classify_rejects_dict_kind(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": {"x": 1}, "reason": "r", "confidence": 0.9}',
    )
    assert _classify_pair("a", "b") is None


def test_classify_nan_confidence_falls_back(monkeypatch):
    """nan confidence must NOT pass the gate (nan < 0.7 is False — silent accept)."""
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "metric_update", "reason": "r", "confidence": "NaN"}',
    )
    result = _classify_pair("a", "b")
    assert result is not None
    assert result["confidence"] == 0.5  # fell back, below threshold


def test_classify_inf_confidence_falls_back(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "metric_update", "reason": "r", "confidence": "inf"}',
    )
    result = _classify_pair("a", "b")
    assert result is not None
    assert result["confidence"] == 0.5


def test_classify_out_of_range_confidence_falls_back(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "metric_update", "reason": "r", "confidence": 999}',
    )
    result = _classify_pair("a", "b")
    assert result is not None
    assert result["confidence"] == 0.5


def test_classify_valid_confidence_preserved(monkeypatch):
    """Regression: a valid confidence still passes through unchanged."""
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "metric_update", "reason": "r", "confidence": 0.92}',
    )
    result = _classify_pair("a", "b")
    assert result is not None
    assert result["confidence"] == 0.92


# --- sweep round2: _call_gemma_for_classify must guard non-dict choices/message ---
# monkeypatch target: src.contradiction_detector.urllib.request.urlopen
# (module uses `import urllib.request`, so urlopen is attribute of urllib.request)

class _FakeResp:
    """Minimal context-manager stub for urlopen returning controlled JSON."""

    def __init__(self, payload):
        import json as _json
        self._raw = _json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_call_gemma_handles_nondict_choice(monkeypatch):
    """choices[0] being a non-dict (str) must return None, not raise AttributeError."""
    from src import contradiction_detector
    monkeypatch.setattr(
        contradiction_detector.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp({"choices": ["just a string"]}),
    )
    assert contradiction_detector._call_gemma_for_classify("prompt") is None


def test_call_gemma_handles_null_choice(monkeypatch):
    """choices[0] being null must return None, not raise."""
    from src import contradiction_detector
    monkeypatch.setattr(
        contradiction_detector.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp({"choices": [None]}),
    )
    assert contradiction_detector._call_gemma_for_classify("prompt") is None


def test_call_gemma_handles_nondict_message(monkeypatch):
    """message being a non-dict (str) must return None, not raise."""
    from src import contradiction_detector
    monkeypatch.setattr(
        contradiction_detector.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp({"choices": [{"message": "not a dict"}]}),
    )
    assert contradiction_detector._call_gemma_for_classify("prompt") is None


def test_call_gemma_valid_response_still_works(monkeypatch):
    """Regression: a normal valid response still returns trimmed content."""
    from src import contradiction_detector
    monkeypatch.setattr(
        contradiction_detector.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp({"choices": [{"message": {"content": "  hello  "}}]}),
    )
    assert contradiction_detector._call_gemma_for_classify("prompt") == "hello"


# --- _relevant_excerpt (long-body truncation fix) ---

def test_relevant_excerpt_short_text_unchanged():
    """≤limit 텍스트는 그대로 반환 (기존 짧은 메모리 동작 보존)."""
    from src.contradiction_detector import _relevant_excerpt
    t = "짧은 메모리 본문입니다. hit rate 66.3%."
    assert _relevant_excerpt(t, query="hit rate", limit=1500) == t


def test_relevant_excerpt_picks_relevant_chunk_over_head():
    """긴 본문에서 query 토큰이 몰린 뒤쪽 청크를 head 대신 선택."""
    from src.contradiction_detector import _relevant_excerpt
    head = "맥락 없는 머리말. " * 200          # ~2000+ chars, query 무관
    tail = "Layer 4 hook 의 picked hit rate 는 66.3% 이다."
    text = head + tail
    out = _relevant_excerpt(text, query="hit rate 66.3 picked", limit=800)
    assert "66.3%" in out
    assert len(out) <= 800


def test_relevant_excerpt_includes_frontmatter_description():
    """frontmatter description 이 gist 헤더로 항상 포함."""
    from src.contradiction_detector import _relevant_excerpt
    text = ('---\nname: project-x\ndescription: "MindVault v3 진척 트래커"\n---\n'
            + "본문 채우기. " * 400 + " 특정지표 alias_index 동기화 patterns.")
    out = _relevant_excerpt(text, query="alias_index 동기화", limit=900)
    assert "MindVault v3 진척 트래커" in out
    assert "alias_index" in out


def test_relevant_excerpt_no_overlap_falls_back_to_head():
    """query 토큰 겹침 0 이면 head fallback (silent loss 방지)."""
    from src.contradiction_detector import _relevant_excerpt
    text = "시작부분 표식 ABCSTART. " + ("무관 본문. " * 400)
    out = _relevant_excerpt(text, query="zzz 전혀없는토큰 qqqq", limit=500)
    assert "ABCSTART" in out  # head 유지
    assert len(out) <= 500


def test_relevant_excerpt_deep_metric_now_visible():
    """동기 사례: 21K자 트래커 후반의 metric 이 head-truncation 너머라도 발췌됨."""
    from src.contradiction_detector import _relevant_excerpt
    filler = "프로젝트 진척 노트 라인. " * 1500   # ~20K chars
    deep = "\n[2026-05-25] Layer 4 hook hit rate = 66.3%, p50 latency 40ms."
    text = filler + deep
    assert len(text) > 20000
    out = _relevant_excerpt(text, query="Layer 4 hook hit rate latency", limit=1500)
    assert "66.3%" in out and "latency" in out
