#!/usr/bin/env python3
"""MindVault v3 Sprint 16 — Query Intent Classifier (rule-based).

memory-recall.py 의 hook 게이트 보강. raw_cosine 단일 게이트는 잡담·메타가
우연 단어 매칭으로 mid-cosine zone (0.30~0.40) 진입할 때 차단 불완전.
intent 가 chat/meta 면 회수 0건 강제 — V3-PLAN §3.D 의 mid-cosine zone discriminator.

규칙 기반 우선 (Gemma 미사용):
- hook latency 추가 0 (regex 매칭은 마이크로초)
- 잡담·메타 한국어 패턴은 규칙적 → 휴리스틱 충분
- Gemma 호출은 옵션으로 별도 추가 가능 (현재 sprint 미포함)

분류 결과:
- chat: 인사·짧은 잡담
- meta: Claude/세션 메타 대화
- code: 코드·파일·실행 요청
- recall: 명시적 회수 의도 ("예전에", "기억나")
- unknown: 위 어디에도 안 잡힘 — hook 기본 게이트 그대로
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

# Sprint NEXT-3 — Gemma 보강 classifier. rule-based 가 unknown 으로 떨어진
# 짧은 query 에 한해 Gemma 가 chat/meta 분류 보강. opt-in 환경변수로 default off.
GEMMA_INTENT_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_INTENT_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
# Hook hard-budget (hooks/memory-recall.py HARD_TIMEOUT_MS=400) 기준으로
# 산정. 실측: warm 247ms / cold 558ms (2026-05-24). 2.0s → 0.30s 로 단축해
# cold 는 즉시 silent skip(None) → rule-based fallback, warm 만 통과.
# 이전 2.0s 때엔 cold 호출 1건이 hook SIGALRM 400ms 에 잡혀 debug.log 에
# "intent classify skipped: _Timeout" 60건 누적 (13:45~20:17 burst).
GEMMA_INTENT_TIMEOUT = 0.30
GEMMA_INTENT_MAX_LEN = 40  # 그 이상 query 는 Gemma 호출 안 함 (cost / latency)
ENABLE_GEMMA_INTENT_ENV = "MV3_GEMMA_INTENT"

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
_MV3_DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
_DEBUG_LOG = _MV3_DATA_DIR / "debug.log"


def _debug(msg: str) -> None:
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a") as f:
            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] intent: {msg}\n"
            )
    except Exception:
        pass

# 각 카테고리 regex. compile 1회.
# 우선순위: recall > code > meta > chat > unknown.
# 같은 query 가 여러 카테고리 매칭하면 우선순위 높은 쪽 채택.

CHAT_RE = re.compile(
    r"(^(?:안녕|안녕하세요|하이|반갑|굿모닝|굿나잇|좋은\s?(?:아침|밤|저녁|하루))|"
    r"^(?:오늘\s?(?:날씨|기분|점심|저녁|뭐|어때)|날씨\s?어때|기분\s?어때)|"
    r"^(?:고마워|감사합니다|땡큐|땡스)|"
    r"^(?:잘자|굿나잇|그럼\s?이만|나중에|또\s?봐))"
)

# 강한 meta — 그 자체로 Claude/세션/모델/토큰을 묻는 표현. 문장 어디서 매칭돼도 meta.
META_RE = re.compile(
    r"(무슨\s?모델|어떤\s?모델|어떤\s?(?:버전|claude)|claude\s?(?:몇|version)|"
    r"context\s?(?:얼마|남았|window|용량)|토큰\s?(?:얼마|남았|사용)|"
    r"너는\s?(?:누구|뭐|어떤)|당신은\s?(?:누구|뭐)|네\s?이름|"
    r"버전\s?(?:이|확인)|모델\s?(?:이|확인))"
)

# bug-audit 2026-06-02 (#22): 모호한 자기참조 토큰. 'claude code'·'이 세션'·
# '현재 세션' 은 일반 작업 쿼리에도 흔히 등장하는 명사구라(예: 'claude code 로
# 만든 프로젝트 분석', '현재 세션 동안 진행한 youtube 작업 요약') 앵커 없이
# 문장 중간 매칭하면 정당한 작업 쿼리의 회수를 silent 차단한다. 쿼리가 짧아
# 이 구절이 주제일 때(≤ META_AMBIGUOUS_MAX_WORDS 단어)만 meta 로 인정한다.
META_SELFREF_RE = re.compile(r"(claude\s?code|이\s?세션|현재\s?세션)")
# codex R2: ≤4 는 'claude code 프로젝트 분석'·'현재 세션 작업 요약'(둘 다 4단어) 같은
# 정당한 작업 쿼리를 meta 로 오분류해 회수를 silent 차단했다. ≤3 으로 강화 —
# 기존 test_meta('현재 세션 정보'=3단어)는 유지되고 4단어 작업 쿼리는 회수 진행.
# 비대칭성: false-meta(작업쿼리 회수차단)는 해롭고 false-non-meta(메타쿼리 회수)는
# raw_cosine 게이트가 걸러 무해 → meta 로 분류하지 않는 쪽으로 보수적 설정.
META_AMBIGUOUS_MAX_WORDS = 3

# code intent — 명확한 코드 작업 키워드 또는 파일 경로/확장자
_FILE_EXT_RE = re.compile(
    r"\.(?:py|js|jsx|ts|tsx|md|yml|yaml|toml|json|sh|bash|zsh|c|cc|cpp|h|hpp|rs|go|java|kt|swift|rb|php|sql|html|css|scss)\b",
    re.IGNORECASE,
)
_PATH_HINT_RE = re.compile(r"(?:^|\s)([/\\]?[\w.-]+/[\w./-]+)")
CODE_RE = re.compile(
    r"(이\s?(?:함수|코드|클래스|메서드|메소드|파일|버그|테스트|커밋|브랜치)|"
    r"버그\s?(?:고쳐|수정|fix)|fix\s?(?:bug|this)|"
    r"(?:테스트|test)\s?(?:돌려|실행|run)|돌려\s?봐|run\s?(?:the\s)?test|"
    r"(?:배포|deploy|ship)|commit|커밋|push|머지|merge|pr\s?(?:만들|올려|생성)|"
    r"리팩토링|refactor|reindex|컴파일|build\s?(?:해|돌려)|타입\s?체크|"
    r"실행\s?(?:해|돌려)|실행해\s?봐)",
    re.IGNORECASE,  # PR/Test/Commit 등 대문자 case 포용
)

RECALL_RE = re.compile(
    r"(예전에|그때|이전에|지난번|어제|전에|옛날에|저번에|"
    r"기억(?:해|나|안\s?나|에)|뭐였(?:어|지|더|을까)|"
    r"이전\s?(?:대화|세션)|예전\s?(?:대화|일|얘기))"
)

MIN_LEN_CHAT_FALLBACK = 6
# 짧은 인삿말 외 길이도 너무 짧고 단어 1~2개면 chat fallback


class IntentResult(NamedTuple):
    intent: str
    confidence: float
    matched: list[str]


def _matched_terms(regex: re.Pattern, text: str) -> list[str]:
    return [m.group(0) for m in regex.finditer(text)]


def classify(prompt: str) -> IntentResult:
    """rule-based intent. 우선순위: recall > code > meta > chat > unknown."""
    if not prompt:
        return IntentResult("unknown", 0.0, [])
    p = prompt.strip()

    # recall: 가장 강력한 신호 (raw cosine 게이트 완화 의도)
    recall_hits = _matched_terms(RECALL_RE, p)
    if recall_hits:
        return IntentResult("recall", min(1.0, 0.6 + 0.1 * len(recall_hits)), recall_hits)

    # code: file/extension 또는 코드 키워드
    code_hits = _matched_terms(CODE_RE, p)
    ext_hits = _matched_terms(_FILE_EXT_RE, p)
    if code_hits or ext_hits:
        merged = code_hits + ext_hits
        return IntentResult(
            "code", min(1.0, 0.6 + 0.1 * len(merged)), merged[:5]
        )

    # meta: Claude/세션 메타
    meta_hits = _matched_terms(META_RE, p)
    if not meta_hits:
        # 모호한 자기참조 토큰은 쿼리가 짧아 주제일 때만 meta (#22).
        selfref_hits = _matched_terms(META_SELFREF_RE, p)
        if selfref_hits:
            word_count = len(re.findall(r"[가-힣A-Za-z0-9]+", p))
            if word_count <= META_AMBIGUOUS_MAX_WORDS:
                meta_hits = selfref_hits
    if meta_hits:
        return IntentResult(
            "meta", min(1.0, 0.7 + 0.1 * len(meta_hits)), meta_hits
        )

    # chat: 인사·잡담 + 짧은 query 휴리스틱
    chat_hits = _matched_terms(CHAT_RE, p)
    if chat_hits:
        return IntentResult(
            "chat", min(1.0, 0.7 + 0.1 * len(chat_hits)), chat_hits
        )
    # 짧고 단어 적으면 chat fallback — 의도가 약함
    word_count = len(re.findall(r"[가-힣A-Za-z0-9]+", p))
    if len(p) < MIN_LEN_CHAT_FALLBACK and word_count <= 2:
        return IntentResult("chat", 0.4, ["short-fallback"])

    return IntentResult("unknown", 0.0, [])


def should_skip_recall(intent: IntentResult) -> bool:
    """intent 가 chat/meta 면 회수 skip. 게이트 통과해도 차단."""
    return intent.intent in ("chat", "meta")


_GEMMA_INTENT_PROMPT = (
    "다음 질문을 한 단어로 분류해라. 분류 라벨 한 단어만 즉시 출력. "
    "thinking, 해설, 설명 모두 금지. 첫 토큰부터 라벨.\n"
    "chat: 인사 잡담 날씨 기분\n"
    "meta: Claude 세션 모델 버전 토큰 자기참조\n"
    "code: 코드 파일 실행 디버그 테스트 빌드\n"
    "recall: 과거 대화 결정 메모리 회수\n"
    "other: 그 외 작업 지시\n\n"
    "질문: {q}\n"
    "라벨:"
)

_VALID_GEMMA_LABELS = {"chat", "meta", "code", "recall", "other"}


def gemma_intent_enabled() -> bool:
    """env-based opt-in. session_memory_end / hook 이 사용."""
    return os.environ.get(ENABLE_GEMMA_INTENT_ENV, "").strip() == "1"


def _call_gemma_intent(prompt_text: str) -> str | None:
    """Gemma 한 줄 분류 호출. 실패하면 None."""
    body = json.dumps(
        {
            "model": GEMMA_INTENT_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": _GEMMA_INTENT_PROMPT.format(q=prompt_text),
                }
            ],
            "max_tokens": 8,
            "temperature": 0.0,
            # Gemma 3 의 thinking 모드를 끄지 않으면 max_tokens 안에 라벨이
            # 도달 못하고 reasoning trace ("Thinking Process: ...") 만 출력됨.
            # mlx_lm.server chat template 인자로 비활성 — content 에 즉시 라벨.
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode()
    req = urllib.request.Request(
        GEMMA_INTENT_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Codex review fix: 광범위 except Exception 은 hook 의 SIGALRM _Timeout 까지
    # swallow → UserPromptSubmit hook 의 400ms budget 보장 깨짐. 네트워크·디코드
    # 계열만 명시적으로 잡고 BaseException (KeyboardInterrupt 등) 과 hook 의
    # _Timeout 은 통과시켜 hook outer try/except 가 잡게 한다.
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_INTENT_TIMEOUT) as resp:
            data = json.loads(resp.read())
        # bug-audit 2026-06-02 (#21): 비-dict valid JSON(서버 오류 래퍼/프록시가
        # []/숫자/문자열/null 을 200 으로 반환)이면 data.get / choices[0].get 가
        # except 튜플 밖 AttributeError 로 hook 핫패스를 뚫고 negative-cache 도
        # 못 박혀 매 turn 재호출. contradiction_detector._call_gemma_for_classify
        # 의 isinstance 가드와 동일 관례로 컨테이너 타입을 먼저 검증.
        if not isinstance(data, dict):
            return None
        # bug-audit 2026-06-02 (R3, #21 완성): choices 자체가 truthy 비-list(dict/int)
        # 면 `not choices` 를 통과한 뒤 choices[0] 가 TypeError/KeyError(except 튜플 밖)
        # → negative-cache 우회로 매 turn 재호출. list 타입을 먼저 검증.
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        msg = choices[0].get("message")
        if not isinstance(msg, dict):
            return None
        # mlx_lm.server 의 thinking-mode 응답은 message.content 가 빈 문자열이고
        # raw trace 가 message.reasoning 필드에만 들어간다 (max_tokens 가 작아
        # 라벨 도달 전에 토큰 소진되는 케이스). content 비면 reasoning fallback.
        # bug-audit 2026-06-01 (gemma-nonstr-content sibling): content/reasoning 이
        # 비-문자열이면 .strip() 이 AttributeError(except 튜플에 없음)로 hook 핫패스를
        # 뚫는다. str 만 통과.
        raw = msg.get("content")
        if not isinstance(raw, str) or not raw:
            raw = msg.get("reasoning")
        content = raw if isinstance(raw, str) else ""
        return content.strip() or None
    except (
        TimeoutError,
        urllib.error.URLError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as e:
        _debug(f"gemma intent fail: {type(e).__name__} {e}")
        return None


_LABEL_TOKEN_RE = re.compile(r"[a-zA-Z]+")


def _normalize_gemma_label(raw: str | None) -> str | None:
    """Gemma 응답에서 첫 유효 라벨 토큰 추출. reasoning trace 안에 'Thinking',
    'The', 'I' 같은 비라벨 토큰이 먼저 등장해도 무시하고 valid label 만 잡는다.
    유효 라벨이 하나도 없으면 None.
    """
    if not raw:
        return None
    for m in _LABEL_TOKEN_RE.finditer(raw):
        label = m.group(0).lower()
        if label in _VALID_GEMMA_LABELS:
            return label
    return None


_GEMMA_CACHE_DB = _MV3_DATA_DIR / "intent_cache.db"
_GEMMA_CACHE_TTL_SEC = 7 * 24 * 3600  # 7일
_GEMMA_CACHE_DISABLE_ENV = "MV3_GEMMA_INTENT_CACHE_DISABLE"
_gemma_cache_initialized = False


def _gemma_cache_enabled() -> bool:
    return os.environ.get(_GEMMA_CACHE_DISABLE_ENV, "0") != "1"


def _gemma_cache_init() -> None:
    """idempotent — sqlite WAL 로 hook 동시 호출 안전."""
    global _gemma_cache_initialized
    if _gemma_cache_initialized:
        return
    try:
        _GEMMA_CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = __import__("sqlite3").connect(str(_GEMMA_CACHE_DB), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gemma_intent_cache (
                    prompt_hash TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        _gemma_cache_initialized = True
    except Exception as e:
        _debug(f"gemma cache init fail (graceful): {type(e).__name__} {e}")


def _gemma_cache_get(prompt_text: str) -> str | None:
    """label 또는 None. miss/error 는 None — 무한 fallback."""
    if not _gemma_cache_enabled():
        return None
    _gemma_cache_init()
    try:
        h = __import__("hashlib").sha256(prompt_text.encode("utf-8")).hexdigest()
        conn = __import__("sqlite3").connect(str(_GEMMA_CACHE_DB), timeout=1.0)
        try:
            row = conn.execute(
                "SELECT label, created_at FROM gemma_intent_cache WHERE prompt_hash=?",
                (h,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        label, created_at = row
        if time.time() - float(created_at) > _GEMMA_CACHE_TTL_SEC:
            return None
        return label
    except Exception as e:
        _debug(f"gemma cache get fail (graceful): {type(e).__name__} {e}")
        return None


def _gemma_cache_put(prompt_text: str, label: str) -> None:
    if not _gemma_cache_enabled():
        return
    _gemma_cache_init()
    try:
        h = __import__("hashlib").sha256(prompt_text.encode("utf-8")).hexdigest()
        conn = __import__("sqlite3").connect(str(_GEMMA_CACHE_DB), timeout=1.0)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO gemma_intent_cache(prompt_hash,label,created_at) VALUES(?,?,?)",
                (h, label, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _debug(f"gemma cache put fail (graceful): {type(e).__name__} {e}")


# label sentinel for "Gemma 호출했으나 유효 라벨 없음" — 다시 호출 안 함.
_GEMMA_NEGATIVE_SENTINEL = "__none__"


def classify_with_gemma(prompt: str) -> IntentResult | None:
    """rule-based 가 unknown 으로 떨어진 짧은 query 를 Gemma 가 보강 분류.

    호출 조건은 caller (hook) 가 판단 — 본 함수는 opt-in 체크만 추가로 안 함.
    유효 라벨(`chat`/`meta`/`code`/`recall`/`other`) 만 반환. 그 외엔 None.
    실패(timeout, 서버 다운, parse fail)는 None 반환 — rule-based 결과로 폴백.

    post-ship perf fix (2026-05-24): file-backed cache. 같은 prompt 는 1회만
    Gemma 호출, 이후 7일간 hit. perf test 100회 호출 시 8 unique × 350ms 첫
    호출 + 92 × ~1ms cache hit 으로 avg 478ms → ~50ms 회복.
    """
    p = (prompt or "").strip()
    if not p or len(p) > GEMMA_INTENT_MAX_LEN:
        return None
    # cache 우선
    cached = _gemma_cache_get(p)
    if cached == _GEMMA_NEGATIVE_SENTINEL:
        return None
    if cached and cached in _VALID_GEMMA_LABELS and cached != "other":
        return IntentResult(cached, 0.6, [f"gemma:{cached}"])
    # miss → 실제 호출
    raw = _call_gemma_intent(p)
    if raw is None:
        # bug-audit 2026-06-01 (intent-transient-negcache): timeout/서버다운/parse-fail/
        # 빈응답(=call 미성공)을 'Gemma 가 other 라 응답'과 합쳐 7일 negative 캐시하면
        # 일시 장애가 해당 prompt 의 Gemma 분류를 7일간 배제한다. 실패는 캐시 말고 재시도.
        # genuine non-empty 응답(아래)만 negative 캐시 → perf 캐시 이점은 유지.
        return None
    label = _normalize_gemma_label(raw)
    if label is None or label == "other":
        _gemma_cache_put(p, _GEMMA_NEGATIVE_SENTINEL)
        return None
    _gemma_cache_put(p, label)
    _debug(f"gemma label={label} q={p[:40]!r}")
    return IntentResult(label, 0.6, [f"gemma:{label}"])
