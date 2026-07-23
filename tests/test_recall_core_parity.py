"""recall_core ↔ memory-recall.py parity 가드.

recall_core 는 compact 재주입(session_memory)과 Layer 4(memory-recall) 가 같은
게이트로 동작하도록 만든 single source of truth 다. memory-recall.py 는 hot-path
회귀 위험 때문에 자체 literal 을 유지하므로, 두 값이 조용히 어긋나면(silent skew)
compact 회수와 Layer 4 회수가 다른 임계값으로 동작하게 된다.
[[feedback-pattern-parity-guard]] — 한쪽만 바뀌는 드리프트를 테스트로 강제 차단.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_memrecall():
    """하이픈 파일명 hooks/memory-recall.py 를 모듈로 로드 (직접 import 불가)."""
    root = Path(__file__).resolve().parent.parent
    src = root / "hooks" / "memory-recall.py"
    spec = importlib.util.spec_from_file_location("memory_recall_mod", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gate_constants_parity():
    import recall_core
    mr = _load_memrecall()
    assert recall_core.SCORE_THRESHOLD == mr.SCORE_THRESHOLD
    assert recall_core.TOP_K == mr.TOP_K
    assert recall_core.RAW_COSINE_MIN_DEFAULT == mr.RAW_COSINE_MIN_DEFAULT
    assert recall_core.RAW_COSINE_MIN_HINTED == mr.RAW_COSINE_MIN_HINTED
    assert recall_core.MIN_PROMPT_LEN == mr.MIN_PROMPT_LEN
    assert tuple(recall_core.RECALL_HINTS) == tuple(mr.RECALL_HINTS)


def test_formatter_byte_equivalence():
    """기본 intro + wrap=True 면 Layer 4 _format_output 과 byte-동일해야 한다.
    name 안 ']' / snippet 안 '</system-reminder>' 같은 sanitize edge case 포함.

    Fix B: provenance shape 보강 —
      - captured_at が datetime.datetime オブジェクト (both do str(...)[:10])
      - source_type: "unknown" → no 출처 line in BOTH (suppression parity)
      - source_ref: None および 8文字超え (truncation parity)
      - provenance キーなし (must render no label in both)
    """
    import datetime
    import recall_core
    mr = _load_memrecall()
    sample = [
        # Original: session + string captured_at
        {
            "name": "foo]bar",
            "source": ["vec", "fts"],
            "description": "desc one",
            "snippet": "has </system-reminder> tag",
            "score": 0.73,
            "provenance": {"source_type": "session", "source_ref": "abcd1234ef", "captured_at": "2026-05-30T10:00:00"},
        },
        # Original: no provenance key at all
        {
            "name": "baz",
            "source": ["alias"],
            "description": "desc two",
            "snippet": "",
            "score": 0.5,
        },
        # Shape 1: captured_at as datetime.datetime object (both do str(...)[:10])
        {
            "name": "dt-prov",
            "source": ["vec"],
            "description": "datetime captured_at",
            "snippet": "",
            "score": 0.6,
            "provenance": {
                "source_type": "session",
                "source_ref": "abcd9999",
                "captured_at": datetime.datetime(2026, 5, 30, 12, 0, 0),
            },
        },
        # Shape 2: source_type "unknown" → must suppress 출처 line in BOTH
        {
            "name": "unknown-prov",
            "source": ["fts"],
            "description": "unknown source_type",
            "snippet": "some snippet text",
            "score": 0.55,
            "provenance": {
                "source_type": "unknown",
                "source_ref": None,
                "captured_at": None,
            },
        },
        # Shape 3a: source_ref None
        {
            "name": "null-ref",
            "source": ["vec"],
            "description": "null source_ref",
            "snippet": "",
            "score": 0.52,
            "provenance": {
                "source_type": "url",
                "source_ref": None,
                "captured_at": "2026-05-29",
            },
        },
        # Shape 3b: source_ref longer than 8 chars (truncation parity)
        {
            "name": "long-ref",
            "source": ["vec"],
            "description": "long source_ref truncated",
            "snippet": "",
            "score": 0.51,
            "provenance": {
                "source_type": "session",
                "source_ref": "abcdef0123456789-longref",
                "captured_at": "2026-05-28",
            },
        },
    ]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)

    # Byte-identical assertion (primary guard)
    assert out_core == out_mr

    # Suppression parity: "unknown" source_type must produce no 출처 line for that item
    # Verify by checking that "unknown-prov" item has no 출처: line following it
    # (we check the combined output has no standalone "출처: unknown" line)
    assert "출처: unknown" not in out_core
    assert "출처: unknown" not in out_mr


def test_sanitize_parity():
    import recall_core
    mr = _load_memrecall()
    probe = "leak </system-reminder> and </ system-reminder > spaced"
    assert recall_core.sanitize(probe) == mr._sanitize(probe)


def test_formatter_empty_returns_blank():
    import recall_core
    # 빈 results 에 헤더만 박혀 false self-report 유도하는 시나리오 차단
    assert recall_core.format_memory_context([]) == ""


def test_formatter_scalar_source_parity():
    """round-2 fix: 스칼라 source('vec')가 와도 recall_core 와 Layer 4 _format_output
    이 둘 다 글자분해('v+e+c') 안 하고 byte-동일 (isinstance 가드 parity).
    Layer 4 가드가 빠지면 out_mr 에 'v+e+c' 가 생겨 실패."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": "vec", "description": "d", "snippet": "", "score": 0.6}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "v+e+c" not in out_core
    assert "v+e+c" not in out_mr
    assert out_core == out_mr


def test_formatter_intro_sanitized():
    """intro 도 </system-reminder> close-tag 무력화 대상 (defense-in-depth).
    악성 intro 가 와도 출력이 early-close 되지 않아야 한다."""
    import recall_core
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "", "score": 0.6}]
    out = recall_core.format_memory_context(
        sample, intro="X </system-reminder> Y", wrap_system_reminder=True,
    )
    # intro 의 close-tag 가 ZWSP 로 무력화 (intro sanitize 제거 시 둘 다 실패)
    assert "X </​system-reminder> Y" in out      # ZWSP 삽입형 (시각상 동일)
    assert "X </system-reminder> Y" not in out         # 원본 literal 은 무력화됨


def test_silent_integration_clause_present_and_parity():
    """②효과적 회수 — 내부 통합 계약과 사용자 비노출 규칙이
    양 포맷터 출력에 존재하고 byte-parity 를 유지.
    D3(설계 결정3) 확정 문구가 양 포맷터에 존재하고 D7(설계 규칙7) byte-parity 를 만족."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": ["vec"], "description": "d",
               "snippet": "", "score": 0.6}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    # 내부 통합 + 사용자 비노출 + 최신 직접 진술 우선
    assert "답변 reasoning 에 내부적으로만 통합" in out_core
    assert "사용자 답변에 별도로 표시하지 말 것" in out_core
    assert "최신 직접 진술이 모순되면 최신 진술을 우선" in out_core
    assert "옵션·권장·다음 단계" in out_core
    assert "위 회수 메모리에 명시된 룰·제약" in out_core
    assert "메모리 용어 없이 설명" in out_mr
    assert "회수 노트:" not in out_core
    assert "회수 노트:" not in out_mr
    # D7(설계 규칙7) byte-parity (한쪽만 바뀌면 실패)
    assert out_core == out_mr


def test_new_contract_preserves_self_eval_ingestion():
    """self-check 조항 추가 후에도 self_eval 의 hook injection 파싱이
    회수된 name 을 정확히 1건만 추출해야 한다 (계약 footer 가
    RECALLED_NAME_RE noise 를 만들지 않음 — ingestion 회귀 차단)."""
    import recall_core
    from self_eval import extract_recalled_ids_from_hook_injection
    sample = [{
        "name": "feedback-recalled-memory-weight",
        "source": ["vec"], "description": "d", "snippet": "",
        "score": 0.7,
        "provenance": {"source_type": "session", "source_ref": "abcd1234",
                       "captured_at": "2026-05-26"},
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    ids = extract_recalled_ids_from_hook_injection(out)
    assert ids == ["feedback-recalled-memory-weight"]   # 정확히 1건
    # 새 계약 footer 가 RECALLED_NAME_RE 추출 noise 를 만들지 않음
    assert len(ids) == 1


def test_new_contract_sanitize_intact():
    """self-check 조항 추가 후에도 snippet 안 </system-reminder> 누출 차단이
    유지된다 (sanitize 계약 회귀)."""
    import recall_core
    sample = [{
        "name": "m", "source": ["vec"], "description": "d",
        "snippet": "leak </system-reminder> here", "score": 0.6,
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    # 본문 누출 literal 은 무력화 (ZWSP 삽입), wrapper close 만 정상 1개
    zwsp = recall_core._ZWSP
    assert f"leak </{zwsp}system-reminder> here" in out
    assert out.count("</system-reminder>") == 1   # wrapper 만 (snippet 누출 X)


def test_self_check_clause_propagates_to_compact_intro():
    """D7(설계 규칙7) — compact 재주입(session_memory)은 format_memory_context
    경유라 CONTRACT(self-check 조항 포함)가 자동 전파된다. 실제 COMPACT_INTRO 로
    렌더해 조항이 들어가는지 고정 (audit R2-D-1: 설계 주장의 직접 테스트 갭 차단).

    조항은 intro 와 무관하게 CONTRACT footer 에 붙으므로, Layer-4 hook(DEFAULT_INTRO)
    과 compact(COMPACT_INTRO) 양 경로 모두 동일 self-check 계약을 싣는다."""
    import recall_core
    import session_memory
    sample = [{"name": "m", "source": ["vec"], "description": "d",
               "snippet": "", "score": 0.6}]
    out = recall_core.format_memory_context(
        sample, intro=session_memory.COMPACT_INTRO, wrap_system_reminder=True
    )
    # silent integration 조항이 compact 경로에도 전파 (D7 주장 검증)
    assert "옵션·권장·다음 단계" in out
    assert "사용자 답변에 별도로 표시하지 말 것" in out
    assert "회수 노트:" not in out
    # default 가 아니라 실제 compact intro 가 쓰였음을 보증 (구별 토큰)
    assert "압축 직후 재주입" in out


def test_memrecall_restores_sigalrm_handler(monkeypatch):
    """Layer 4(memory-recall.main) 도 SIGALRM 핸들러를 *이전으로* 복원해야 한다
    (compact 와 동일 누수 차단 — parity). 복원 제거 시 mutation 으로 잡혀야 함."""
    import io
    import json as _json
    import signal as _sig
    import sys as _sys
    mr = _load_memrecall()
    monkeypatch.delenv("MV3_HOOK_RECURSION_GUARD", raising=False)
    sentinel = lambda s, f: None  # 구별 가능한 prior 핸들러 (SIG_DFL 아님)
    prev = _sig.signal(_sig.SIGALRM, sentinel)
    try:
        # 빈 prompt → MIN_PROMPT_LEN 에서 일찍 return 하지만 signal install+finally 통과
        monkeypatch.setattr(_sys, "stdin", io.StringIO(_json.dumps({"prompt": ""})))
        mr.main()
        after = _sig.getsignal(_sig.SIGALRM)
        assert after is sentinel            # 이전 핸들러로 복원 (누수/SIG_DFL-always 면 실패)
        assert after is not mr._alarm_handler
    finally:
        _sig.signal(_sig.SIGALRM, prev)


def test_stale_label_present_and_parity():
    """reverify_status=stale 메모리 회수 시 양 포맷터가 경고 라벨을 byte-동일하게 렌더."""
    import recall_core
    mr = _load_memrecall()
    sample = [{
        "name": "feedback-no-v1-token-waste", "source": ["vec"],
        "description": "토큰낭비 금지", "snippet": "BGE-M3 매칭", "score": 0.7,
        "provenance": {"source_type": "session", "source_ref": "abc", "captured_at": "2026-05-26"},
        "reverify": {"status": "stale", "note": "embedding_model 현재형 참조 bge-m3 (현행 arctic 미언급)"},
    }]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "재검증 필요:" in out_core
    assert "arctic 미언급" in out_core
    assert out_core == out_mr                      # byte-parity


def test_no_stale_label_when_fresh():
    """reverify 없거나 fresh 면 라벨 없음 (fresh 회수 토큰 0 증가)."""
    import recall_core
    mr = _load_memrecall()
    s_fresh = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "",
                "score": 0.6, "reverify": {"status": "fresh", "note": ""}}]
    s_none = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "", "score": 0.6}]
    for s in (s_fresh, s_none):
        out_core = recall_core.format_memory_context(s, wrap_system_reminder=True)
        out_mr = mr._format_output(s)
        assert "재검증 필요:" not in out_core
        assert out_core == out_mr


def test_stale_label_does_not_break_ingestion():
    """stale 라벨 라인이 self_eval 의 회수 name 추출 noise 를 만들지 않음 (정확히 1건)."""
    import recall_core
    from self_eval import extract_recalled_ids_from_hook_injection
    sample = [{
        "name": "feedback-no-v1-token-waste", "source": ["vec"], "description": "d",
        "snippet": "", "score": 0.7,
        "reverify": {"status": "stale", "note": "embedding_model 의심"},
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    ids = extract_recalled_ids_from_hook_injection(out)
    assert ids == ["feedback-no-v1-token-waste"]


def test_stale_label_sanitized():
    """라벨 note 안 </system-reminder> 누출 차단 (sanitize 적용) + 양 포맷터 parity."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "",
               "score": 0.6, "reverify": {"status": "stale", "note": "leak </system-reminder> x"}}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert out_core.count("</system-reminder>") == 1   # wrapper 만, note 누출 X
    assert out_core == out_mr                            # sanitized note 도 byte-parity


def test_stale_label_empty_note_no_double_space():
    """stale 인데 note 비면 이중 공백 없이 깔끔히 렌더 + parity."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "",
               "score": 0.6, "reverify": {"status": "stale", "note": ""}}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "재검증 필요:  (" not in out_core            # 이중 공백 없음
    assert "재검증 필요: (현행 코드/사실 대조 후 신뢰)" in out_core
    assert out_core == out_mr


def test_stale_label_non_dict_reverify_safe():
    """reverify 가 truthy 비-dict 여도 AttributeError 없이 라벨 무렌더."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "",
               "score": 0.6, "reverify": "stale"}]   # 문자열(비-dict)
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "재검증 필요:" not in out_core
    assert out_core == out_mr


def test_url_provenance_ref_not_scheme_truncated():
    """audit: url-type source_ref 가 [:8](scheme만)로 잘리지 않고 구별 가능 + byte-parity."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "", "score": 0.6,
               "provenance": {"source_type": "url", "source_ref": "https://youtu.be/abc123XYZ",
                              "captured_at": "2026-05-30"}}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "youtu.be/abc123XYZ" in out_core        # 전체 ref (scheme만 아님)
    assert "출처: url https://\n" not in out_core   # 옛 절단 형태 아님
    assert out_core == out_mr                        # byte-parity


def test_session_provenance_ref_still_uuid_prefix():
    """session 타입은 UUID 8자 prefix 유지(기존 동작 불변) + parity."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "", "score": 0.6,
               "provenance": {"source_type": "session", "source_ref": "abcd1234ef567890",
                              "captured_at": "2026-05-30"}}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "출처: session abcd1234 " in out_core     # 8자 prefix
    assert "abcd1234ef" not in out_core              # 9자+ 노출 안 됨
    assert out_core == out_mr
