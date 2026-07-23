#!/usr/bin/env python3
"""MindVault v3 Sprint 3 — Gemma 기반 기억 후보 추출기.

트리거 키워드 감지 → Gemma에 구조화 프롬프트 → JSON 배열 파싱 → 유효 항목 반환.
"""
from __future__ import annotations

import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
DEBUG_LOG = DATA_DIR / "debug.log"
GEMMA_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_MODEL = "mlx-community/gemma-4-12B-it-4bit"
GEMMA_TIMEOUT = 45
MAX_BODY_CHARS = 200
MAX_TITLE_CHARS = 50
# Sprint 13: procedural type 추가 — 명령어 syntax·workflow·환경 설정.
# 새 trigger 그룹으로 신호 받아 Gemma 가 type=procedural 후보 생성하면
# session_memory_end 가 _procedural/_staged/ 슬롯에 저장.
VALID_TYPES = ("feedback", "project", "procedural")

TRIGGER_RE = re.compile(
    r"(기억해|잊지\s?마|잊지말아|결정[:：]|정했[어다]|앞으로는|다음부턴|"
    r"이 프로젝트는|원칙[:：]|규칙[:：]|"
    # Sprint 13 procedural triggers — 명령어·문법·workflow·환경설정 자동 추출
    r"이 명령어|이 명령은|이렇게\s?(?:쓰면|하면|입력하면|실행하면)|"
    r"syntax|문법|이\s?(?:패턴|workflow|플로우|순서|절차)는?|"
    r"외워둬|기억해둬|반복(?:해서|적으로)\s?(?:쓰|할|사용|실행)|"
    r"환경설정|환경\s?변수|셋업|setup|이\s?(?:flag|옵션|플래그))"
)

# Sprint NEXT-1 자동 trigger 휴리스틱 — assistant 의 Bash tool_use 안 명령어가
# 특수 binary 또는 non-trivial 한 모양인데, 직후 user 가 다음 액션을 지시하면
# (한국어 사용자 패턴상 "ok/굿" 같은 confirmation 보다 "진행/적용/켜줘" 사용자가 흔하다)
# Gemma 가 보고 procedural candidate 만들 가치 있다고 본다. trigger ON.
SPECIAL_BIN_RE = re.compile(
    r"\b(?:launchctl|sqlite3|ffprobe|ffmpeg|yt-dlp|higgsfield|kubectl|gcloud|"
    r"hyperframes|jq|awk)\b|sed\s+-i\b|gh\s+api\b|"
    r"claude\s+(?:--bg|-c\b|--resume|-r\b)|"
    r"git\s+worktree\b"
)
NEXT_ACTION_RE = re.compile(
    r"(진행|해결|적용|켜줘|실행|영구화|반영|배포|sync|push|land|merge|commit|"
    r"ship|다음|이어서|계속)"
)

# Sprint NEXT-10 — ACK 휴리스틱. NEXT-1 (다음 액션 지시) 과 보완 관계:
# NEXT-1 은 "진행/적용/켜줘" 같은 NEXT_ACTION 시그널을 잡고, NEXT-10 은
# "좋아/OK/ㅇㅇ/굿" 같은 단순 confirmation 을 잡는다. 사용자의 한국어 응답 패턴상
# 한 줄 ACK 가 가장 흔한 결정 confirmation 시그널 — backfill 24/1 hit ratio
# (NEXT-8 BUILD-LOG §5) 가 노출한 extractor recall 한계의 1차 해소책.
#
# 안전선:
# 1) 직전 assistant 가 의미있는 변경 시그널 (Bash tool_use 있음 OR text ≥ 200자)
# 2) user turn 짧음 (≤ 30자) — 잡담 긴 답변 차단
# 3) MV3_EXTRACTOR_ACK_TRIGGER=0 으로 끌 수 있음 (false positive 측정 후 튜닝)
ACK_RE = re.compile(
    r"^(좋[아네아으]|굳|굿|good|nice|perfect|훌륭|ㅇㅇ|ㅇㅋ|어|네|예|"
    r"OK|ok|오케이|콜|땡큐|thx|thanks|감사|👍|✓|✔|💯|확인|"
    r"맞[아네어]|그래|그러게|그렇네|좋아요|좋다|완벽|아주\s?좋)"
    r"[\.!~ㅋㅎ\s]{0,15}$",
    re.IGNORECASE,
)
SIGNIFICANT_ASSISTANT_TEXT_LEN = 200
ACK_TRIGGER_ENABLED = os.environ.get("MV3_EXTRACTOR_ACK_TRIGGER", "1") == "1"


def _is_significant_assistant(m: dict) -> bool:
    """assistant turn 이 변경/결정 시그널을 가지는지.

    bash_commands 가 있거나 텍스트가 길면 (≥ SIGNIFICANT_ASSISTANT_TEXT_LEN)
    significant. NEXT-10 ACK 휴리스틱의 1차 게이트.
    """
    if m.get("bash_commands"):
        return True
    text = m.get("text", "") or ""
    return len(text) >= SIGNIFICANT_ASSISTANT_TEXT_LEN


def _is_non_trivial_bash(cmd: str) -> bool:
    """길이 100 이상, 또는 pipe/redirect 2+ 회 — '복잡한 한 줄'."""
    if not cmd:
        return False
    if len(cmd) >= 100:
        return True
    if cmd.count(" | ") >= 2:
        return True
    # Codex review fix: 기존 `cmd.count(">") + cmd.count(">>")` 는 `>>` 안의 `>` 가
    # 두 번 카운트돼 `echo x >> file` 같은 trivial append 가 합 3 으로 잘못 잡혔다.
    # findall 로 `>>` 또는 `>` 를 토큰 단위로 카운트 — 의도된 "2+ redirect 연산자" 정확.
    if len(re.findall(r">>|>", cmd)) >= 2:
        return True
    return False


def _is_special_bash(cmd: str) -> bool:
    if not cmd:
        return False
    return bool(SPECIAL_BIN_RE.search(cmd))

SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS]"),
]


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] extractor: {msg}\n")
    except Exception:
        pass


def redact(text: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _is_system_reminder(text: str) -> bool:
    head = text.lstrip()[:50]
    return head.startswith("<system-reminder>") or head.startswith("<command-")


def extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return "" if _is_system_reminder(content) else content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        text_val = block.get("text")
        if btype == "text" or (btype is None and text_val is not None):
            t = str(text_val or "")
            if _is_system_reminder(t):
                continue
            parts.append(t)
    return "\n".join(p for p in parts if p)


def extract_bash_from_content(content) -> list[str]:
    """assistant message 안의 Bash tool_use command 문자열만 수집."""
    if not isinstance(content, list):
        return []
    cmds: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use" or block.get("name") != "Bash":
            continue
        inp = block.get("input") or {}
        cmd = inp.get("command") if isinstance(inp, dict) else None
        if isinstance(cmd, str) and cmd.strip():
            cmds.append(redact(cmd.strip()))
    return cmds


def load_tail_messages(jsonl_path: Path, tail_turns: int = 40) -> list[dict]:
    msgs: list[dict] = []
    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                text = extract_text_from_content(content).strip()
                bash_cmds = (
                    extract_bash_from_content(content) if t == "assistant" else []
                )
                if not text and not bash_cmds:
                    continue
                if text.startswith("# 지난 세션 요약"):
                    continue
                msgs.append(
                    {
                        "role": t,
                        "text": redact(text),
                        "bash_commands": bash_cmds,
                    }
                )
    except OSError as e:
        _debug(f"read fail {jsonl_path.name}: {e}")
        return []
    return msgs[-tail_turns:]


def has_trigger(messages: list[dict]) -> bool:
    """3 layer trigger 결합:

    1. TRIGGER_RE — 명시 키워드 (기억해/결정:/외워둬/이 명령어 …). 항상 ON.
    2. NEXT-1 휴리스틱 — special/non_trivial bash + 직후 user NEXT_ACTION. 항상 ON.
    3. NEXT-10 ACK 휴리스틱 — significant assistant + 직후 user 짧은 ACK.
       MV3_EXTRACTOR_ACK_TRIGGER=0 으로 OFF 가능.

    Gemma 가 최종 판별. trigger 는 "이 세션에 procedural/decision 후보가 있을 가능성"
    1차 필터. 분기된 trigger 사유는 _debug 로 가시화 (self_eval 측정 인프라).
    """
    prev_bash_signal = False
    prev_significant = False
    for m in messages:
        role = m.get("role")
        text = m.get("text", "") or ""
        if role == "assistant":
            cmds = m.get("bash_commands") or []
            # 한 user turn 사이의 assistant 분할(tool_use → text) 을 흡수 — 한 번이라도
            # special/non_trivial 가 보였으면 signal 누적
            if any(_is_special_bash(c) or _is_non_trivial_bash(c) for c in cmds):
                prev_bash_signal = True
            if _is_significant_assistant(m):
                prev_significant = True
            continue
        if role != "user":
            continue
        if TRIGGER_RE.search(text):
            _debug("trigger=keyword")
            return True
        if (
            prev_bash_signal
            and len(text) <= 50
            and NEXT_ACTION_RE.search(text)
        ):
            _debug("trigger=next1-action")
            return True
        if (
            ACK_TRIGGER_ENABLED
            and prev_significant
            and len(text.strip()) <= 30
            and ACK_RE.search(text.strip())
        ):
            _debug(f"trigger=next10-ack text={text.strip()[:20]!r}")
            return True
        # user turn 마침 → 다음 사이클 위해 reset
        prev_bash_signal = False
        prev_significant = False
    return False


def call_gemma(prompt: str, max_tokens: int = 1500) -> str | None:
    body = json.dumps(
        {
            "model": GEMMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            # mem-fan-fix 2026-06-19: thinking off (top-level, 12B 전용). peak 폭증 방지.
            "enable_thinking": False,
        }
    ).encode()
    req = urllib.request.Request(
        GEMMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return None
        choice0 = choices[0]
        # bug-audit 2026-06-01 (extractor-truncation-negcache): finish_reason=length 면
        # 응답이 중간 절단돼 JSON 배열이 안 닫힌다. 이전엔 truncation 도 non-empty 절단
        # 문자열로 반환돼 parse=[] → 빈 결과가 영구 negative 캐시(extractor-negcache-1
        # 가드 우회). 절단은 '호출 실패'로 취급해 None 반환 → 빈 결과 캐시 차단·재시도.
        if choice0.get("finish_reason") == "length":
            _debug("gemma finish_reason=length (truncated) — treat as call fail")
            return None
        # non-str content(content-block 리스트 등) 방어 — .strip() AttributeError 회피.
        raw = (choice0.get("message") or {}).get("content")
        content = raw if isinstance(raw, str) else ""
        return content.strip() or None
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as e:
        # audit-2026-05-24: BaseException/_Timeout 은 의도적으로 전파.
        _debug(f"gemma fail: {type(e).__name__} {e}")
        return None


def _provider() -> str:
    """Extraction backend. Public default is local Gemma; cloud is opt-in."""
    if os.environ.get("MV3_PRIVACY", "").strip().lower() == "local":
        return "gemma"
    return os.environ.get("MV3_LLM_PROVIDER", "gemma").strip().lower()


def call_model(prompt: str, max_tokens: int = 1500) -> str | None:
    """Route one extraction call without silently multiplying paid/cloud calls."""
    provider = _provider()
    # A redaction marker means the source contained credentials. Keep that prompt
    # local even though the literal secret has already been removed.
    if provider == "gemma" or "[REDACTED_" in prompt or "Bearer [REDACTED]" in prompt:
        return call_gemma(prompt, max_tokens=max_tokens)
    if provider != "codex_cli":
        _debug(f"unknown LLM provider={provider!r}")
        return None
    try:
        from llm_backend import call_codex_extractor
        out = call_codex_extractor(prompt)
    except Exception as e:
        _debug(f"codex-cli backend fail: {type(e).__name__}: {e}")
        out = None
    if out is None and os.environ.get("MV3_LLM_FALLBACK", "").strip() == "gemma":
        _debug("codex-cli unavailable; explicit gemma fallback")
        return call_gemma(prompt, max_tokens=max_tokens)
    return out


def build_prompt(messages: list[dict]) -> str:
    excerpt_parts = []
    for m in messages:
        role = m.get("role")
        prefix = "U" if role == "user" else "A"
        text = m.get("text") or ""
        if text:
            excerpt_parts.append(f"{prefix}: {text[:600]}")
        # assistant 의 Bash command 도 Gemma 가 보도록 별도 라인으로 첨부
        for cmd in (m.get("bash_commands") or [])[:5]:
            excerpt_parts.append(f"{prefix}:bash: {cmd[:300]}")
    excerpt = "\n".join(excerpt_parts)
    return (
        "아래는 AI 코딩 세션 대화의 새 구간이다. 사용자가 '영구 기억'으로 남기려고 한 "
        "사실만 추출하라. 단순 진행 보고나 일회성 대화는 제외, 단 sprint 진척·결정·"
        "마일스톤 같은 누적 메타데이터는 project 로 적극 추출.\n\n"
        "출력은 JSON 배열만. 각 항목 형식:\n"
        '{"type":"feedback|project|procedural","title":"한 줄 50자 이내","body":"본문 200자 이내",'
        '"reason":"저장 이유 10자 이내","evidence":"원문 인용 30자"}\n\n'
        "type 가이드:\n"
        "- feedback: 사용자의 작업 방식·선호·금지사항 (예: '커밋 분리해라', '머지 직접 금지', "
        "  '자의적 멈춤 권고 금지').\n"
        "- project: 프로젝트의 누적 상태·결정·진척·인물·외부 자원·milestone.\n"
        "  모델·백엔드·인프라 선택과 운영 기본값 결정도 project.\n"
        "  예: 'X v3.0 ship 2026-05', 'master HEAD abc123 NEXT-N fix 완료', "
        "  'Sprint 14 Memory Compiler 운영 fire 시작', '책임자 Y'.\n"
        "  NEXT-11: 진척 메타데이터도 적극 — 한 sprint 결과·운영 측정 수치·게이트 통과 등은\n"
        "  영구 기록 가치. 같은 주제 기존 메모리 있으면 update 자동 매칭됨 (NEXT-2).\n"
        "- procedural: 명령어·syntax·workflow·환경 설정. body 는 실행 예시 1줄 + 한 줄 설명.\n"
        "  예: body='claude --bg \"prompt\" # 백그라운드 세션 시작. 결과는 jobs 디렉토리에 저장.'\n"
        "  단순 실행 보고 (예: 'commit 완료', 'test 통과') 자체는 procedural 아님.\n\n"
        "후보가 없으면 빈 배열 []. 해설·마크다운 코드펜스 금지. JSON만.\n\n"
        "---대화---\n"
        f"{excerpt}\n"
        "---끝---"
    )


def _iter_balanced_arrays(text: str):
    """text 안의 모든 balanced [...] 후보를 파싱 시도, list 인 것만 yield.

    bug-audit 2026-05-29 (extractor-greedy-json-1): 이전 `re.search(r"\\[[\\s\\S]*\\]")`
    는 greedy 라 배열 밖 산문의 대괄호까지 삼켜(예: "[note]: [{...}] (참고 [x])")
    전체 매칭이 깨지면 valid 후보를 통째로 버렸다. 후보 span 을 하나씩 균형 매칭해
    파싱 가능한 list 만 골라낸다 — 문자열 내부 대괄호/이스케이프도 정확히 처리.
    """
    i, n = 0, len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        depth = 0
        in_str = esc = False
        end = -1
        for j in range(i, n):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end < 0:
            # bug-audit 2026-06-01 (extractor-balanced-array-early-return): 선행 불균형
            # '[' 뒤에 유효 배열이 올 수 있으므로 종료 대신 이 한 글자만 건너뛰고 계속.
            i += 1
            continue
        try:
            val = json.loads(text[i:end + 1])
            if isinstance(val, list):
                yield val
        except json.JSONDecodeError:
            # bug-audit 2026-06-02 (R4): 산문 머리말 + 끝쉼표 조합이면 여기 span 이
            # 끝쉼표로 파싱 실패해 yield 안 돼 유효 후보가 유실됐다(끝쉼표 복구가
            # whole-string/fence 경로에만 배선됨). string-aware 복구 후 1회 재시도 —
            # 스캐너가 문자열 내부 쉼표를 보존하므로 산문 안 가짜 후보 날조 위험 없음.
            try:
                val = json.loads(_strip_trailing_commas(text[i:end + 1]))
                if isinstance(val, list):
                    yield val
            except json.JSONDecodeError:
                pass
        i = end + 1


def _strip_trailing_commas(s: str) -> str:
    """문자열 리터럴 *밖*의 끝쉼표(다음 비공백이 ] 또는 })만 제거.

    bug-audit 2026-06-02 (codex R2): 이전 blind `re.sub(r",(\\s*[}\\]])", ...)` 는
    JSON 구조를 몰라 body/title 문자열 값 안의 ', ]' / ', }'(예: 한국어 procedural
    body '순서는 [빌드, 테스트, 배포,] 로 한다')의 쉼표까지 삭제해 저장 콘텐츠를
    무음 손상시켰다. in_str/esc 상태기로 문자열 내부 쉼표는 보존하고 구조적 끝쉼표만
    제거한다 (_iter_balanced_arrays 의 스캐너와 동일 원리).
    """
    out_chars: list[str] = []
    in_str = esc = False
    n = len(s)
    for idx, c in enumerate(s):
        if in_str:
            out_chars.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            out_chars.append(c)
            continue
        if c == ",":
            k = idx + 1
            while k < n and s[k] in " \t\r\n":
                k += 1
            if k < n and s[k] in "]}":
                continue  # 구조적 끝쉼표 → drop (뒤따르는 공백·괄호는 그대로 유지)
        out_chars.append(c)
    return "".join(out_chars)


def _parse_gemma_json_ex(out: str) -> tuple[list[dict], bool]:
    """Gemma 응답 파싱 → (valid_candidates, parse_failed).

    bug-audit 2026-06-02 (#5): 이전 parse_gemma_json 은 '진짜 빈 배열'과 '파싱
    실패'를 둘 다 [] 로 반환해 호출자가 구분 못 했다. 그래서 흔한 LLM JSON 결함
    (끝쉼표 등)으로 유효 후보가 담긴 응답이 [] 로 처리되고 negative cache 에
    영구 저장돼 그 세션의 기억 후보가 영영 유실됐다(MindVault 핵심 목적 훼손).
    이제 (a) 끝쉼표를 관대하게 복구해 가장 흔한 결함을 살리고, (b) 비-empty
    응답에서 유효 배열을 못 뽑으면 parse_failed=True 를 돌려 호출자가
    negative-cache 를 건너뛰게 한다.
    """
    if not out:
        return [], False
    arr = None
    # 1순위: 코드펜스만 벗긴 뒤 직접 파싱 (모델이 JSON 만 낸 정상 케이스).
    stripped = out.strip()
    if not stripped:
        return [], False
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", stripped)
    if fence:
        stripped = fence.group(1).strip()
    try:
        cand = json.loads(stripped)
        if isinstance(cand, list):
            arr = cand
    except json.JSONDecodeError:
        arr = None
    # 1.5순위: 끝쉼표(`[{...},]`/`{...,}`) 관대 복구 후 재파싱 — 가장 흔한 LLM 결함.
    # string-aware 제거(문자열 값 내부 쉼표 보존) — codex R2 손상 회귀 차단.
    if arr is None:
        repaired = _strip_trailing_commas(stripped)
        if repaired != stripped:
            try:
                cand = json.loads(repaired)
                if isinstance(cand, list):
                    arr = cand
            except json.JSONDecodeError:
                arr = None
    # 2순위: 산문이 섞였으면 balanced [...] 후보 중 dict 를 담은 list 우선 선택.
    if arr is None:
        for cand in _iter_balanced_arrays(out):
            if any(isinstance(x, dict) for x in cand):
                arr = cand
                break
            if arr is None:
                arr = cand  # dict 없는 list 라도 첫 후보 보관 (대개 [])
    if not isinstance(arr, list):
        # 비-empty 응답인데 유효 배열을 못 뽑음 = malformed → parse_failed.
        _debug("json parse fail: no balanced array")
        return [], True
    valid = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        title = (item.get("title") or "").strip()
        body = (item.get("body") or "").strip()
        if t not in VALID_TYPES or not title or not body:
            continue
        valid.append(
            {
                "type": t,
                "title": title[:MAX_TITLE_CHARS],
                "body": body[:MAX_BODY_CHARS],
                "reason": (item.get("reason") or "").strip()[:30],
                "evidence": (item.get("evidence") or "").strip()[:60],
            }
        )
    return valid, False


def parse_gemma_json(out: str) -> list[dict]:
    return _parse_gemma_json_ex(out)[0]


def _retries() -> int:
    """Backend-aware additional attempts.

    Luna/Codex is one-shot by default. Gemma keeps the legacy two retries for
    compatibility and recall. MV3_EXTRACTOR_RETRIES overrides both; the old
    MV3_EXTRACTOR_GEMMA_RETRIES remains accepted for local deployments.
    """
    explicit = os.environ.get("MV3_EXTRACTOR_RETRIES")
    if explicit is None and _provider() == "gemma":
        explicit = os.environ.get("MV3_EXTRACTOR_GEMMA_RETRIES", "2")
    if explicit is None:
        explicit = "0"
    try:
        return max(0, int(explicit))
    except ValueError:
        return 2 if _provider() == "gemma" else 0


def _tail_turns() -> int:
    """MV3_EXTRACTOR_TAIL_TURNS — load_tail_messages 가 읽는 마지막 turn 수.

    NEXT-15 진단: trigger 60% miss 의 일부는 tail 40 안에 trigger 패턴이 없어서.
    window 늘리면 (default 80) Gemma prompt 길어지지만 trigger 와 컨텍스트 모두 풍부.
    """
    try:
        return max(10, int(os.environ.get("MV3_EXTRACTOR_TAIL_TURNS", "80")))
    except ValueError:
        return 80


def _always_fire() -> bool:
    """MV3_EXTRACTOR_ALWAYS_FIRE=1 — has_trigger 결과 무시하고 항상 Gemma 호출.

    NEXT-15 진단: trigger 게이트 통과 못 한 세션 60% 가 사실은 영구 기억 가치 있을
    수 있음. opt-in 으로 게이트 우회. Gemma fire 비용 ↑ but recall 폭 본질 해결.
    candidates 0 면 부담 거의 없음 (latency 만 1회 추가).
    """
    return os.environ.get("MV3_EXTRACTOR_ALWAYS_FIRE", "0") == "1"


def _union_by_title(*lists: list[dict]) -> list[dict]:
    """여러 retry 결과를 title 기준 dedup union. 첫 등장 우선."""
    seen: set[str] = set()
    merged: list[dict] = []
    for lst in lists:
        for c in lst:
            t = (c.get("title") or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            merged.append(c)
    return merged


def _load_messages_routed(jsonl_path: Path) -> list[dict]:
    """v4 P2: 파일 스키마로 Claude/Codex 로더 라우팅.

    경로가 아니라 첫 레코드의 envelope 로 판별 — fixture·백필처럼
    ~/.codex/sessions/ 밖에 있는 rollout 도 정상 인식. 로더 import 실패는
    Claude 경로로 fail-open.
    """
    try:
        from codex_session_loader import is_codex_rollout, load_tail_messages_codex
        if is_codex_rollout(jsonl_path):
            return load_tail_messages_codex(jsonl_path, tail_turns=_tail_turns())
    except Exception as e:
        _debug(f"codex loader unavailable ({type(e).__name__}: {e}); claude fallback")
    return load_tail_messages(jsonl_path, tail_turns=_tail_turns())


class ExtractionResult:
    """Small dependency-free status object (also supports dynamic module loads)."""

    __slots__ = ("candidates", "success")

    def __init__(self, candidates: list[dict], success: bool):
        self.candidates = candidates
        self.success = success

    def __repr__(self) -> str:
        return (
            f"ExtractionResult(candidates={self.candidates!r}, "
            f"success={self.success!r})"
        )


def _extract_messages(messages: list[dict], source_name: str) -> ExtractionResult:
    """Extract one bounded message chunk and report transport/parse success."""
    try:
        if not messages:
            return ExtractionResult([], True)
        if not has_trigger(messages):
            if not _always_fire():
                _debug(f"no trigger in {source_name}, skip")
                return ExtractionResult([], True)
            _debug(f"always-fire bypass for {source_name}")
        prompt = build_prompt(messages)
        # NEXT-16: prompt SHA256 캐시 hit → Gemma 호출 건너뜀 (deterministic).
        # jsonl 변하면 prompt 가 달라져 hash 도 자동 invalidate. opt-out env 있음.
        try:
            from extractor_cache import cache_get, cache_put
            cached = cache_get(prompt)
        except Exception as e:
            _debug(f"cache_get fail (graceful): {type(e).__name__}: {e}")
            cached = None
            cache_put = None  # type: ignore
        if cached is not None:
            _debug(
                f"extract cache hit for {source_name}: "
                f"{len(cached)} candidates"
            )
            return ExtractionResult(cached, True)
        # NEXT-14b: Gemma 멱등성 보강 — 0건이면 retry, union 으로 candidates 모음.
        # 첫 호출 비-empty 면 즉시 반환 (latency 최소화). 0건일 때만 retry.
        attempts = 1 + _retries()
        results: list[list[dict]] = []
        any_call_failed = False  # bug-audit 2026-05-29 (extractor-negcache-1)
        any_parse_failed = False  # bug-audit 2026-06-02 (#5)
        any_valid_response = False
        for i in range(attempts):
            out = call_model(prompt)
            if out is None:
                # 전송 실패/빈 응답 (서버 다운·timeout·finish_reason=length). legit
                # "후보 없음" 은 Gemma 가 "[]" 를 반환하므로 None 과 구분된다.
                any_call_failed = True
            if out:
                parsed, parse_failed = _parse_gemma_json_ex(out)
                if parse_failed:
                    any_parse_failed = True
                else:
                    any_valid_response = True
            else:
                parsed = []
            results.append(parsed)
            if parsed:
                _debug(
                    f"extract attempt={i + 1}/{attempts} candidates={len(parsed)}"
                )
                # 첫 hit 이후엔 한 번 더 시도해 union 으로 recall 보강.
                # 이미 다음 시도가 cost 보다 가치 큼 (NEXT-15 측정: 같은 input
                # 도 다른 candidates 추출 — 정보 누적 효과).
                if i + 1 < attempts and i == 0:
                    continue
                break
            _debug(f"extract attempt={i + 1}/{attempts} candidates=0 (retry)")
        merged = _union_by_title(*results)
        if not merged:
            _debug(f"extract all attempts 0 candidates for {source_name}")
        elif sum(len(r) for r in results) != len(merged):
            _debug(
                f"extract union merged={len(merged)} from "
                f"{[len(r) for r in results]}"
            )
        # NEXT-16: 결과 캐시 저장 — 빈 list 도 저장 (다음 호출 재시도 비용 회피).
        # bug-audit 2026-05-29 (extractor-negcache-1): 단, Gemma 호출 자체가 실패한
        # 빈 결과(서버 다운)는 캐시하지 않는다 — 캐시하면 서버 복구 후에도 같은 세션이
        # 영구히 추출 스킵돼 데이터가 유실된다. "서버가 응답했으나 후보 0건"(out="[]")
        # 은 정상 빈 결과라 그대로 캐시한다.
        # bug-audit 2026-06-02 (#5): malformed-but-present 응답(파싱 실패)도
        # negative-cache 회피 — 캐시하면 그 세션 후보가 영구 유실된다.
        skip_negative_cache = (any_call_failed or any_parse_failed) and not merged
        if cache_put is not None and not skip_negative_cache:
            try:
                cache_put(prompt, merged)
            except Exception as e:
                _debug(f"cache_put fail (graceful): {type(e).__name__}: {e}")
        elif skip_negative_cache:
            _debug(
                f"skip caching empty result (gemma call failed={any_call_failed} "
                f"parse failed={any_parse_failed}) for {source_name}"
            )
        return ExtractionResult(merged, any_valid_response)
    except Exception as e:
        _debug(f"extract FATAL: {e}\n{traceback.format_exc()}")
        return ExtractionResult([], False)


def _message_weight(message: dict) -> int:
    return min(600, len(message.get("text") or "")) + sum(
        min(300, len(c)) for c in (message.get("bash_commands") or [])[:5]
    ) + 16


def _max_chunk_chars() -> int:
    try:
        return max(2000, int(os.environ.get("MV3_EXTRACTOR_MAX_CHARS", "16000")))
    except ValueError:
        return 16000


def _chunk_messages(messages: list[dict], overlap: int = 2) -> list[list[dict]]:
    """Bound cloud input; large idle-period deltas are processed sequentially."""
    if not messages:
        return []
    budget = _max_chunk_chars()
    chunks: list[list[dict]] = []
    start = 0
    while start < len(messages):
        used = 0
        end = start
        while end < len(messages):
            weight = _message_weight(messages[end])
            if end > start and used + weight > budget:
                break
            used += weight
            end += 1
        chunks.append(messages[start:end])
        if end >= len(messages):
            break
        start = max(start + 1, end - max(0, overlap))
    return chunks


def extract_from_jsonl(jsonl_path: Path) -> list[dict]:
    """Legacy full-tail extractor for Claude SessionEnd and backfill."""
    result = _extract_messages(_load_messages_routed(jsonl_path), jsonl_path.name)
    return result.candidates


_CURSOR_FILE = "codex_extraction_cursors.json"


def _cursor_state(session_id: str, transcript: Path) -> int:
    path = DATA_DIR / _CURSOR_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        item = data.get(session_id)
        if not isinstance(item, dict):
            return 0
        if item.get("transcript") != str(transcript.resolve()):
            return 0
        return max(0, int(item.get("offset", 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _commit_cursor(session_id: str, transcript: Path, offset: int) -> bool:
    path = DATA_DIR / _CURSOR_FILE
    lock_path = path.with_suffix(".lock")
    try:
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, json.JSONDecodeError):
                data = {}
            data[session_id] = {
                "transcript": str(transcript.resolve()),
                "offset": int(offset),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        return True
    except Exception as e:
        _debug(f"cursor commit fail: {type(e).__name__}: {e}")
        return False


def extract_incremental_codex(
    jsonl_path: Path, session_id: str
) -> ExtractionResult:
    """Extract only new Codex messages and advance cursor after valid output."""
    try:
        from codex_session_loader import load_incremental_messages_codex

        cursor = _cursor_state(session_id, jsonl_path)
        loaded = load_incremental_messages_codex(
            jsonl_path,
            cursor,
            overlap=3,
            bootstrap_tail=12,
        )
        if not loaded.success:
            return ExtractionResult([], False)
        if not loaded.has_delta:
            return ExtractionResult([], True)
        # File bookkeeping with no new user/assistant message still advances safely.
        if not loaded.messages:
            ok = _commit_cursor(session_id, jsonl_path, loaded.end_offset)
            return ExtractionResult([], ok)

        results: list[list[dict]] = []
        chunks = _chunk_messages(loaded.messages)
        for idx, chunk in enumerate(chunks, start=1):
            result = _extract_messages(
                chunk, f"{jsonl_path.name} delta {idx}/{len(chunks)}"
            )
            results.append(result.candidates)
            if not result.success:
                _debug(
                    f"incremental extraction failed; cursor retained at {cursor}"
                )
                return ExtractionResult(_union_by_title(*results), False)
        merged = _union_by_title(*results)
        if not _commit_cursor(session_id, jsonl_path, loaded.end_offset):
            return ExtractionResult(merged, False)
        return ExtractionResult(merged, True)
    except Exception as e:
        _debug(f"incremental FATAL: {e}\n{traceback.format_exc()}")
        return ExtractionResult([], False)
