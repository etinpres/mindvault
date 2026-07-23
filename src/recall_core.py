#!/usr/bin/env python3
"""MindVault v3 — 회수 게이트 상수·포맷터의 single source of truth.

Layer 4 (``hooks/memory-recall.py``, UserPromptSubmit) 와 compact 재주입
(``src/session_memory.py``, SessionStart source=compact) 이 **같은 게이트로**
동작하도록 임계값을 한 곳에 둔다.

memory-recall.py 는 회귀 흉터가 많은 hot-path 라 자체 literal 을 그대로 유지한다
(이 모듈을 import 하지 않음 — 무손상). 대신 ``tests/test_recall_core_parity.py``
가 두 값의 동등성과 포맷터 byte-동일성을 강제해 silent skew 를 차단한다.
(feedback: pattern-parity-guard — 한쪽만 바뀌는 조용한 드리프트 금지)
"""
from __future__ import annotations

import re

# --- 게이트 상수 (memory-recall.py literal 과 parity 테스트로 동기) ---------
SCORE_THRESHOLD = 0.50
TOP_K = 1
RAW_COSINE_MIN_DEFAULT = 0.32
RAW_COSINE_MIN_HINTED = 0.27
MIN_PROMPT_LEN = 4
# 회수 의도 명확 키워드 (있으면 raw cosine 임계값 ↓)
RECALL_HINTS = ("예전에", "그때", "이전에", "지난번", "어제", "전에", "옛날에", "저번에")

# --- compact 재주입 전용 -----------------------------------------------------
# 세션 1개 분량 맥락이라 TOP_K=1 은 부족. 다만 v1 토큰낭비 회피 위해 소량만.
COMPACT_TOP_K = 3

# Layer 4 _format_output 과 byte-동일하게 유지할 라벨/계약 (parity 테스트가 강제)
DEFAULT_INTRO = "MEMORY CONTEXT (다음 fact 를 본 답변 reasoning 에 반드시 통합):"
CONTRACT = (
    "위 메모리는 답변 reasoning 에 내부적으로만 통합하고, 메모리 회수 사실·"
    "점수·출처를 사용자 답변에 별도로 표시하지 말 것. 회수 fact 와 사용자의 "
    "최신 직접 진술이 모순되면 최신 진술을 우선한다. 옵션·권장·다음 단계 "
    "제시 시 위 회수 메모리에 명시된 룰·제약과 충돌하는 항목은 제거한다. "
    "사용자 판단에 꼭 필요한 실제 충돌만 메모리 용어 없이 설명할 것."
)

# v3.2.6 H1: memory 본문에 '</system-reminder>' literal 이 들어가면 hook 출력이
# early-close 되고 뒤 내용이 system context 밖으로 누출. zero-width space 삽입
# 으로 visually 동일하지만 Claude Code parser 의 close tag 매칭은 차단.
_ZWSP = "​"  # zero-width space (memory-recall._sanitize 와 동일 codepoint)
_CLOSE_TAG_RE = re.compile(r"</(\s*)(system-reminder)(\s*)>", re.IGNORECASE)


def sanitize(text: str) -> str:
    """``</system-reminder>`` close-tag 무력화 (memory-recall._sanitize 와 동일 계약)."""
    if not text:
        return text
    return _CLOSE_TAG_RE.sub(
        lambda m: f"</{m.group(1)}{_ZWSP}{m.group(2)}{m.group(3)}>", text
    )


def has_recall_hint(text: str) -> bool:
    return any(h in (text or "") for h in RECALL_HINTS)


def format_memory_context(
    results: list,
    *,
    intro: str = DEFAULT_INTRO,
    wrap_system_reminder: bool = True,
) -> str:
    """``recall_memory`` 결과를 회수 컨텍스트 텍스트로 포맷.

    항목 구조(name/score/source/desc/발췌) + silent integration 계약은 Layer 4
    ``_format_output`` 과 동일. 기본 intro + wrap=True 면 그 출력과 byte-동일.
    compact 경로는 다른 intro 를 넘겨 SessionStart additionalContext 에 싣는다.

    빈 results 에 헤더만 박혀 LLM 이 false self-report 하는 시나리오를 차단하기
    위해 빈 입력은 빈 문자열을 반환한다 (helper 자체 invariant).
    """
    if not results:
        return ""
    # intro 도 sanitize — 현재는 하드코딩 상수라 비악용이나, </system-reminder> 누출
    # 방지 계약을 모든 출력 텍스트에 일관 적용 (defense-in-depth). 상수엔 close-tag 가
    # 없어 byte 출력 불변 → Layer 4 parity 유지.
    body = [sanitize(intro), ""]
    for r in results:
        # source 는 항상 list(recall_memory)지만, 스칼라 'vec' 가 오면 "+".join 이
        # 'v+e+c' 로 글자단위 분해된다. isinstance 가드(or [] 만으론 truthy 비-list 못 막음).
        src_val = r.get("source")
        src_list = src_val if isinstance(src_val, list) else ([] if not src_val else [str(src_val)])
        srcs = sanitize("+".join(src_list))
        # name 안 ']' 가 들어가면 회수노트 추출 regex 가 첫 ']' 에서 끊김 → ')' escape
        raw_name = r.get("name") or "(unnamed)"
        name = sanitize(raw_name.replace("]", ")"))
        desc = sanitize(r.get("description") or "")
        snippet = sanitize(r.get("snippet") or "")
        score = r.get("score", 0)
        body.append(f"- [{name}] (score {score:.2f}, {srcs}) — {desc}")
        prov = r.get("provenance") or {}
        if prov.get("source_type") and prov["source_type"] != "unknown":
            # session 은 UUID 8자 prefix, url/commit/file 등은 전체(120 캡) — audit:
            # [:8] 고정이면 url 이 'https://' 로만 잘려 모든 url provenance 가 비구별.
            _sr = str(prov.get("source_ref") or "")
            ref = sanitize(_sr[:8] if prov["source_type"] == "session" else _sr[:120])
            cap = sanitize(str(prov.get("captured_at") or "")[:10])
            body.append(f"  출처: {sanitize(str(prov['source_type']))} {ref} {cap}".rstrip())
        rv = r.get("reverify")
        rv = rv if isinstance(rv, dict) else {}
        if rv.get("status") == "stale":
            note = sanitize(str(rv.get("note") or "")).strip()
            tail = f"{note} " if note else ""
            body.append(f"  재검증 필요: {tail}(현행 코드/사실 대조 후 신뢰)")
        if snippet:
            body.append(f"  발췌: {snippet}")
    body.append("")
    body.append(CONTRACT)
    text = "\n".join(body)
    if wrap_system_reminder:
        text = "<system-reminder>\n" + text + "\n</system-reminder>"
    return text + "\n"
