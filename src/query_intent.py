#!/usr/bin/env python3
"""Deterministic query intent classifier for the 400 ms recall hook.

No LLM is allowed on this hot path. False positives are more costly than false
negatives because an irrelevant memory injection repeats the V1 token-waste
failure mode.
"""
from __future__ import annotations

import re
from typing import NamedTuple


CHAT_RE = re.compile(
    r"(^(?:안녕|안녕하세요|하이|반갑|굿모닝|굿나잇|좋은\s?(?:아침|밤|저녁|하루))|"
    r"^(?:오늘\s?(?:날씨|기분|점심|저녁|뭐|어때)|날씨\s?어때|기분\s?어때)|"
    r"^(?:고마워|감사합니다|땡큐|땡스)|"
    r"^(?:잘자|굿나잇|그럼\s?이만|나중에|또\s?봐))"
)

META_RE = re.compile(
    r"(무슨\s?모델|어떤\s?모델|어떤\s?(?:버전|claude)|claude\s?(?:몇|version)|"
    r"context\s?(?:얼마|남았|window|용량)|토큰\s?(?:얼마|남았|사용)|"
    r"너는\s?(?:누구|뭐|어떤)|당신은\s?(?:누구|뭐)|네\s?이름|"
    r"버전\s?(?:이|확인)|모델\s?(?:이|확인))"
)

META_SELFREF_RE = re.compile(r"(claude\s?code|이\s?세션|현재\s?세션)")
META_AMBIGUOUS_MAX_WORDS = 3

_FILE_EXT_RE = re.compile(
    r"\.(?:py|js|jsx|ts|tsx|md|yml|yaml|toml|json|sh|bash|zsh|c|cc|cpp|h|hpp|rs|go|java|kt|swift|rb|php|sql|html|css|scss)\b",
    re.IGNORECASE,
)
CODE_RE = re.compile(
    r"(이\s?(?:함수|코드|클래스|메서드|메소드|파일|버그|테스트|커밋|브랜치)|"
    r"버그\s?(?:고쳐|수정|fix)|fix\s?(?:bug|this)|"
    r"(?:테스트|test)\s?(?:돌려|실행|run)|돌려\s?봐|run\s?(?:the\s)?test|"
    r"(?:배포|deploy|ship)|commit|커밋|push|머지|merge|pr\s?(?:만들|올려|생성)|"
    r"리팩토링|refactor|reindex|컴파일|build\s?(?:해|돌려)|타입\s?체크|"
    r"실행\s?(?:해|돌려)|실행해\s?봐)",
    re.IGNORECASE,
)

RECALL_RE = re.compile(
    r"(예전에|그때|이전에|지난번|어제|전에|옛날에|저번에|"
    r"기억(?:해|나|안\s?나|에)|뭐였(?:어|지|더|을까)|"
    r"이전\s?(?:대화|세션)|예전\s?(?:대화|일|얘기))"
)

MIN_LEN_CHAT_FALLBACK = 6


class IntentResult(NamedTuple):
    intent: str
    confidence: float
    matched: list[str]


def _matched_terms(regex: re.Pattern, text: str) -> list[str]:
    return [match.group(0) for match in regex.finditer(text)]


def classify(prompt: str) -> IntentResult:
    """Rule-based intent with recall > code > meta > chat priority."""
    if not prompt:
        return IntentResult("unknown", 0.0, [])
    text = prompt.strip()

    recall_hits = _matched_terms(RECALL_RE, text)
    if recall_hits:
        return IntentResult(
            "recall", min(1.0, 0.6 + 0.1 * len(recall_hits)), recall_hits
        )

    code_hits = _matched_terms(CODE_RE, text)
    ext_hits = _matched_terms(_FILE_EXT_RE, text)
    if code_hits or ext_hits:
        merged = code_hits + ext_hits
        return IntentResult(
            "code", min(1.0, 0.6 + 0.1 * len(merged)), merged[:5]
        )

    meta_hits = _matched_terms(META_RE, text)
    if not meta_hits:
        selfref_hits = _matched_terms(META_SELFREF_RE, text)
        if selfref_hits:
            word_count = len(re.findall(r"[가-힣A-Za-z0-9]+", text))
            if word_count <= META_AMBIGUOUS_MAX_WORDS:
                meta_hits = selfref_hits
    if meta_hits:
        return IntentResult(
            "meta", min(1.0, 0.7 + 0.1 * len(meta_hits)), meta_hits
        )

    chat_hits = _matched_terms(CHAT_RE, text)
    if chat_hits:
        return IntentResult(
            "chat", min(1.0, 0.7 + 0.1 * len(chat_hits)), chat_hits
        )
    word_count = len(re.findall(r"[가-힣A-Za-z0-9]+", text))
    if len(text) < MIN_LEN_CHAT_FALLBACK and word_count <= 2:
        return IntentResult("chat", 0.4, ["short-fallback"])
    return IntentResult("unknown", 0.0, [])


def should_skip_recall(intent: IntentResult) -> bool:
    return intent.intent in ("chat", "meta")
