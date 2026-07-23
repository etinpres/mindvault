from __future__ import annotations

import enum
import fcntl
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memory_compiler import GEMMA_MODEL, GEMMA_TIMEOUT, GEMMA_URL


def _runtime_dir() -> Path:
    """런타임 데이터 디렉토리 (debug.log, contradictions.jsonl). env 우선."""
    env = os.environ.get("MV3_RUNTIME_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "mindvault-v3"


CONFIDENCE_THRESHOLD = 0.7  # 미만은 review queue 제외 (false positive 회피)
BODY_EXCERPT_CHARS = 200    # contradictions.jsonl 에 저장할 body 발췌 길이
CLASSIFY_BODY_LIMIT = 1500  # classify prompt 에 넣는 body 당 char 예산 (Gemma 4K context 여유)


_CLASSIFY_PROMPT = """메모리 시스템의 두 항목이 진짜로 충돌하는지 판단하세요.

[기존 항목]
{old}

[새 항목]
{new}

핵심 원칙: 두 항목이 **양립 가능**하면 (둘 다 동시에 참일 수 있으면) 무조건 no_conflict.
충돌은 새 항목이 기존 항목의 **특정 주장을 더 이상 유효하지 않게 만들 때만** 성립한다.
주제가 겹치거나 같은 시스템을 다룬다는 것만으로는 충돌이 아니다.

분류:
- metric_update: 동일 지표의 수치가 갱신되어 옛 값이 무효화됨 (예: "hit rate 65%" → "hit rate 66.3%"). 다른 지표를 새로 추가하는 것은 아님.
- decision_reversal: 같은 사안에서 기존 결정을 명시적으로 뒤집음 (예: "X 도입" → "X 폐기/철회").
- fact_correction: 기존 항목의 특정 사실을 틀렸다고 정정함 (예: "A는 B다" → "아니다 A는 C다"). 새 정보·세부·맥락·교훈을 단순히 추가하는 것은 정정이 아니다.
- no_conflict: 위 셋이 아니면 전부. 보완·추가·심화·예시·적용·서로 다른 측면 관계는 모두 no_conflict.

판단 절차:
1. 새 항목이 기존 항목의 어떤 구체적 문장을 거짓으로 만드는가? 못 찾으면 no_conflict.
2. 둘 다 동시에 참일 수 있는가? 그렇다면 no_conflict.

예시:
- 기존 "프로젝트 X 진행중" / 새 "X 의 버그를 고치는 코딩 패턴은 Y" → no_conflict (교훈 추가, 기존 사실 부정 안 함)
- 기존 "배포는 install.sh 로 sync" / 새 "alias_index 는 SessionEnd 에 동기화" → no_conflict (서로 다른 메커니즘, 양립)
- 기존 "두 항목 다 메모리 오류를 다룸" 류의 주제 유사 → no_conflict
- 기존 "hit rate 66.3%" / 새 "hit rate 12% 로 하락" → metric_update
- 기존 "산출물은 HTML 로 변환" / 새 "HTML 변환 폐기, 마크다운만" → decision_reversal

JSON만 출력. 다른 설명 없음:
{{"kind": "<one of above>", "reason": "<한 문장>", "confidence": <0.0-1.0>}}"""


def _debug(msg: str) -> None:
    """Emit to ~/.claude/mindvault-v3/debug.log (matches memory_search._debug pattern).

    MV3_RUNTIME_DIR 환경변수가 있으면 그 경로 우선, 없으면 default.
    """
    try:
        log_path = _runtime_dir() / "debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] contradiction: {msg}\n")
    except OSError:
        pass  # never raise from logging path


class ContradictionKind(str, enum.Enum):
    METRIC_UPDATE = "metric_update"
    DECISION_REVERSAL = "decision_reversal"
    FACT_CORRECTION = "fact_correction"
    NO_CONFLICT = "no_conflict"


@dataclass
class Contradiction:
    target_path: Path
    target_name: str
    kind: ContradictionKind
    reason: str
    confidence: float
    new_body_excerpt: str = ""
    old_body_excerpt: str = ""


def detect_contradictions(candidate: dict, mem_dir: Path) -> list[Contradiction]:
    """Hybrid recall + Gemma 분류로 candidate 와 mem_dir 안 충돌 후보 검출.

    Args:
        candidate: {
            "slug": str,                    # bare slug, no type prefix
            "title": str,
            "body": str,
            "type": str (optional),
            "path": Path | str (optional), # explicit self-path for exclusion
        }
        mem_dir: memory/*.md 위치 (recall 결과를 이 디렉토리 subtree 로 제한)

    Returns:
        confidence ≥ CONFIDENCE_THRESHOLD 이고 kind != NO_CONFLICT 인 항목만.
        Gemma 호출 실패 / parse 실패 / low confidence / no_conflict 는 모두 silent skip.
    """
    candidates = _recall_candidates(candidate, mem_dir, top_k=5)
    if not candidates:
        return []

    new_body = candidate.get("body", "")
    if not new_body:
        return []

    contradictions: list[Contradiction] = []
    for path, _score in candidates:
        try:
            old_body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        result = _classify_pair(new_body, old_body)
        if not result:
            continue
        if result["kind"] == ContradictionKind.NO_CONFLICT.value:
            continue
        if result["confidence"] < CONFIDENCE_THRESHOLD:
            continue

        contradictions.append(Contradiction(
            target_path=path,
            target_name=path.stem,
            kind=ContradictionKind(result["kind"]),
            reason=result["reason"],
            confidence=result["confidence"],
            new_body_excerpt=new_body[:BODY_EXCERPT_CHARS],
            old_body_excerpt=old_body[:BODY_EXCERPT_CHARS],
        ))
    return contradictions


def _hybrid_search(query: str, mem_dir: Path, top_k: int = 5) -> list[tuple[Path, float]]:
    """memory_search.recall_memory 호출 후 (path, score) tuple 로 정규화.

    mem_dir filter: 결과 path 중 mem_dir subtree 안의 것만 (cross-project 잡음 제거).
    실패 시 빈 list + debug.log 에 사유 기록 (silent loss 방지).
    """
    import memory_search
    try:
        results = memory_search.recall_memory(query, top_k=top_k)
    except Exception as e:
        # Telemetry only — caller already handles []. memory_search.recall_memory
        # 자체가 FATAL+traceback 을 자기 로그에 남기므로 여기는 한 줄 요약으로 충분.
        _debug(f"recall_memory failed: {type(e).__name__}: {e}")
        return []

    mem_root = mem_dir.resolve()
    out: list[tuple[Path, float]] = []
    for r in results:
        p_raw = r.get("path")
        if not p_raw:
            continue
        try:
            p = Path(p_raw).resolve()
        except (OSError, ValueError):
            continue
        try:
            p.relative_to(mem_root)  # raises ValueError if not under mem_root
        except ValueError:
            continue
        out.append((p, float(r.get("score", 0.0))))
    return out


def _recall_candidates(
    candidate: dict, mem_dir: Path, top_k: int = 5,
) -> list[tuple[Path, float]]:
    """candidate.body+title 로 query 만들어 _hybrid_search 호출, self 제외.

    Self-exclusion 우선순위:
    1. candidate["path"] 있으면 path identity 비교 (가장 정확).
    2. 없으면 stem suffix match — production memory 파일이 "<type>_<slug>.md"
       (예: feedback_youtube_metadata_dump.md) 형태라서, slug="youtube-metadata-dump"
       만 가지고 stem 전체와 비교하면 절대 일치 안 함. suffix 매칭으로 보강.
    """
    title = candidate.get("title", "")
    body_excerpt = candidate.get("body", "")[:300]
    query = " ".join(p for p in (title, body_excerpt) if p).strip()
    if not query:
        return []

    results = _hybrid_search(query, mem_dir, top_k=top_k)

    # Self-exclusion. Prefer path identity (most reliable); fall back to stem
    # suffix match (handles "<type>_<slug>" prod naming convention).
    own_path = candidate.get("path")
    if own_path:
        try:
            own_resolved = Path(own_path).resolve()
        except (OSError, ValueError):
            own_resolved = None
    else:
        own_resolved = None

    own_slug = candidate.get("slug", "")
    own_stem_suffix = own_slug.replace("-", "_")  # underscore form

    def is_self(p: Path) -> bool:
        if own_resolved is not None and p == own_resolved:
            return True
        if not own_stem_suffix:
            return False
        # Match if stem == slug (bare) OR ends with "_<slug>" (handles type_ prefix).
        return p.stem == own_stem_suffix or p.stem.endswith("_" + own_stem_suffix)

    return [(p, s) for p, s in results if not is_self(p)]


def _call_gemma_for_classify(prompt: str, max_tokens: int = 1536) -> str | None:
    """Gemma 4 E4B 호출. 실패 시 None (silent, _debug 로깅).

    BaseException 은 통과시킴 (sentinel pattern, hook hard-budget 호환).

    max_tokens 는 reasoning(CoT) 분리 출력 모델 기준. gemma-4-e4b 가 응답을
    message.reasoning + message.content 로 나눠 내므로, budget 이 작으면 reasoning
    (~500-850 tok) 이 다 먹고 content(JSON) 가 빈 채 finish_reason=length 로 잘림.
    """
    body = json.dumps({
        "model": GEMMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        GEMMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError, OSError, TimeoutError) as e:
        # bug-audit 2026-06-01 (gemma-unicodedecode): max_tokens 경계에서 멀티바이트
        # UTF-8 절단 시 json.loads(bytes) 가 UnicodeDecodeError(ValueError, JSONDecodeError 아님)
        # 를 던져 캐치 튜플을 빠져나가 detect 루프를 중단시켰다. 튜플에 추가.
        _debug(f"gemma classify fail: {type(e).__name__}: {e}")
        return None

    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    raw_content = message.get("content")
    if raw_content is not None and not isinstance(raw_content, str):
        # bug-audit 2026-06-01 (gemma-nonstr-content): content-block 리스트 등 비-문자열
        # content 에 .strip() 하면 AttributeError 가 detect 루프 전체를 중단시킨다.
        _debug(f"gemma classify non-str content type={type(raw_content).__name__}")
        return None
    content = (raw_content or "").strip()
    if not content:
        # Reasoning models can burn the whole budget on CoT (message.reasoning)
        # and emit empty content with finish_reason=length. Without this log it
        # looks identical to a clean no-detection and the failure stays invisible.
        _debug(f"gemma classify empty content (finish_reason={choices[0].get('finish_reason')})")
        return None
    return content


def _strip_code_fences(text: str) -> str:
    """```json\\n{...}\\n``` 같은 마크다운 fence 제거."""
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z0-9.]{2,}")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _excerpt_tokens(text: str) -> set[str]:
    """비교용 토큰 집합 — 한글 음절 run + 영숫자 run (length ≥ 2), lowercase."""
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}


def _relevant_excerpt(text: str, query: str, limit: int = CLASSIFY_BODY_LIMIT) -> str:
    """`text` 중 `query` 와 가장 관련된 ≤limit 자 발췌.

    긴 메모리(예: 20K 자 프로젝트 트래커)는 충돌 주장이 head 1500 자 너머에 묻혀
    naive head-truncation 으로는 분류기가 못 본다. query 토큰 겹침이 가장 큰 window 를
    고르고, frontmatter description(요지)을 항상 앞에 붙여 주제 맥락을 잃지 않게 한다.
    순수 Python·결정적. 짧은 text(≤limit)는 그대로 반환 → 기존 동작 보존.
    """
    if len(text) <= limit:
        return text

    qtokens = _excerpt_tokens(query)

    # frontmatter description 을 gist 헤더로 (있으면). 토큰 없거나 매칭 0이면 head fallback.
    header = ""
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        fm, body = m.group(1), text[m.end():]
        dm = re.search(r"^\s*description\s*:\s*(.+)$", fm, re.MULTILINE)
        if dm:
            header = "[description] " + dm.group(1).strip().strip('"')[:300] + "\n\n"

    if not qtokens:
        return (header + body)[:limit]

    # query 토큰을 body 내 희소도로 가중 — 희귀 토큰(예: 특정 수치 "66.3")이 흔한
    # 토큰("hook" 이 트래커에 50번)보다 변별력 크다. 안 그러면 term-dense window 가
    # 정작 충돌 값 없는 곳으로 쏠림 (project_mindvault 실측).
    blow = body.lower()
    weights = {t: 1.0 / c for t in qtokens if (c := blow.count(t)) > 0}
    if not weights:
        return (header + body)[:limit]  # query 토큰이 body 에 전무 → head fallback

    win = max(200, limit - len(header))
    step = max(1, win // 2)
    best_start, best_score = 0, -1.0
    for start in range(0, max(1, len(body)), step):
        chunk = body[start:start + win]
        if not chunk:
            break
        clow = chunk.lower()
        score = sum(w for t, w in weights.items() if t in clow)
        if score > best_score:
            best_score, best_start = score, start
    if best_score <= 0:
        return (header + body)[:limit]  # no overlap → head fallback
    excerpt = body[best_start:best_start + win]
    prefix = "…" if best_start > 0 else ""
    return (header + prefix + excerpt)[:limit]


def _classify_pair(new_body: str, old_body: str) -> dict | None:
    """두 body 비교 후 {'kind', 'reason', 'confidence'} 반환. failure → None.

    각 body 는 상대 body 와 가장 관련된 ≤CLASSIFY_BODY_LIMIT 자 청크로 발췌해 prompt 에
    포함 (긴 메모리의 깊은 충돌도 분류기가 볼 수 있게). 짧은 body 는 전체 그대로.
    """
    old_excerpt = _relevant_excerpt(old_body, new_body)
    new_excerpt = _relevant_excerpt(new_body, old_body)
    prompt = _CLASSIFY_PROMPT.format(old=old_excerpt, new=new_excerpt)
    raw = _call_gemma_for_classify(prompt)
    if not raw:
        return None

    raw = _strip_code_fences(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    valid_kinds = {k.value for k in ContradictionKind}
    kind = parsed.get("kind")
    if not isinstance(kind, str) or kind not in valid_kinds:
        return None

    parsed.setdefault("reason", "")

    # Validate confidence: finite float in [0,1]. nan/inf/out-of-range/bool all
    # fall back to 0.5 (below CONFIDENCE_THRESHOLD, so a hallucinated value won't
    # pass the gate — nan < 0.7 is False, which would silently accept otherwise).
    raw_conf = parsed.get("confidence", 0.5)
    if isinstance(raw_conf, bool):
        conf = 0.5
    else:
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            conf = 0.5
        if not math.isfinite(conf) or not (0.0 <= conf <= 1.0):
            conf = 0.5
    parsed["confidence"] = conf

    return parsed


def append_to_review_queue(
    candidate_slug: str,
    contradictions: list[Contradiction],
    new_path: Path,
) -> Path:
    """Contradiction 항목들을 contradictions.jsonl 에 append.

    동시 write race 회피: fcntl.flock(LOCK_EX) for the batch (parallel SessionEnd
    hooks 가 sibling Conductor workspaces 에서 동시 호출될 수 있음).
    OSError silent skip + _debug log (T5 hook context 에서 traceback 노이즈 회피).
    Timestamp 는 UTC (self_eval.py:135 naive TZ 경고 따름).
    """
    out = _runtime_dir() / "contradictions.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # bug-audit 2026-05-29 (contradiction-jsonl-race-1): append 를 review CLI 의
    # _mark_resolved(read-modify-rewrite)와 같은 lock 파일(<queue>.lock)에서 직렬화.
    # 이전엔 데이터 파일 자체 fd 에 flock 했으나, _mark_resolved 는 tmp+os.replace 로
    # 데이터 파일을 새 inode 로 교체하므로 같은 락을 공유하지 못했다 — append 가 read
    # 와 replace 사이에 끼면 replace 가 그 row 를 덮어 유실시켰다. 별도 lock 파일을
    # 양측이 LOCK_EX 로 잡아 상호 배제한다.
    lock_path = out.with_name(out.name + ".lock")
    try:
        lock_fh = open(lock_path, "w")
    except OSError as e:
        _debug(f"queue lock open fail: {type(e).__name__}: {e}")
        lock_fh = None
    try:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            except OSError as e:
                _debug(f"queue flock fail: {type(e).__name__}: {e}")
                # Continue without lock — better single-writer integrity than full skip
        with out.open("a", encoding="utf-8") as f:
            for c in contradictions:
                f.write(json.dumps({
                    "ts": ts,
                    "new_slug": candidate_slug,
                    "new_path": str(new_path),
                    "target_name": c.target_name,
                    "target_path": str(c.target_path),
                    "kind": c.kind.value,
                    "reason": c.reason,
                    "confidence": c.confidence,
                    "new_excerpt": c.new_body_excerpt,
                    "old_excerpt": c.old_body_excerpt,
                    "resolved": False,
                }, ensure_ascii=False) + "\n")
    except OSError as e:
        _debug(f"queue append fail: {type(e).__name__}: {e}")
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fh.close()

    return out
