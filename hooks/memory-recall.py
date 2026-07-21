#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — UserPromptSubmit hook.

매 사용자 메시지마다 memory/*.md hybrid 검색 결과를 system-reminder로 주입.
모든 실패는 silent → exit 0 빈 출력. 사용자 메시지 처리 절대 블로킹 X.
"""
from __future__ import annotations

import os as _os_bootstrap
import sys as _sys_bootstrap

# numpy/sqlite_vec 보유한 python interpreter로 자동 재실행 (NEXT-26).
# launchd/Claude hook 컨텍스트에서 PATH가 numpy 없는 /usr/bin/python3 를 잡아
# "numpy._core.multiarray failed to import" FATAL 폭주하던 회귀 차단.
if "MV3_HOOK_REEXEC" not in _os_bootstrap.environ:
    try:
        import numpy as _probe_numpy  # noqa: F401
    except ImportError:
        for _cand in (
            "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ):
            if _os_bootstrap.path.exists(_cand) and _os_bootstrap.path.realpath(_cand) != _os_bootstrap.path.realpath(_sys_bootstrap.executable):
                _os_bootstrap.environ["MV3_HOOK_REEXEC"] = "1"
                try:
                    _os_bootstrap.execv(_cand, [_cand, __file__] + _sys_bootstrap.argv[1:])
                except OSError:
                    continue
        # 못 찾으면 silent exit — hook은 절대 블로킹 금지
        _sys_bootstrap.exit(0)

import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
import os as _os_paths
DATA_DIR = Path(_os_paths.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
DEBUG_LOG = DATA_DIR / "debug.log"
METRICS_LOG = DATA_DIR / "metrics.jsonl"
MIN_PROMPT_LEN = 4  # 너무 짧은 키워드는 skip. 잡담은 raw cosine 게이트가 차단.
HARD_TIMEOUT_MS = 400
# NEXT-30.3 (2026-05-24): score_threshold 게이트가 실제로 recall_memory
# 본체에 적용됨 — memory_search.py:546-551 참조. 이전 dead-param 코멘트는
# stale 이라 제거. picked=0 의 주 원인은 여전히 vec+fts no_candidates
# (debug.log 870 건) 임 — RRF 게이트가 아니라 임베딩/FTS sparsity.
# 0.65 → 0.50 (NEXT-29 doc-correctness) 기준치 그대로 유지.
SCORE_THRESHOLD = 0.50
TOP_K = 1  # 절대 우수한 1건만. 매번 3건 회수는 V1 토큰 낭비 패턴.
# NEXT-30.1 (2026-05-24): 0.40 → 0.32 / 0.32 → 0.27. 측정: cohort weak
# correct 20% → 33% (+13pp), FP 0/16 → 1/16 (오직 "ok" len=2 — hook
# MIN_PROMPT_LEN=4 가 어차피 차단). strong correct 60% 그대로 유지.
RAW_COSINE_MIN_DEFAULT = 0.32  # Sprint 9 Arctic-ko 재튜닝 + NEXT-30.1 완화
RAW_COSINE_MIN_HINTED = 0.27   # 회수 단서어 ("예전에" 등) 있을 때 사용자 의도 명확 → 추가 완화

# 회수 의도 명확 키워드 (있으면 임계값 ↓)
RECALL_HINTS = ("예전에", "그때", "이전에", "지난번", "어제", "전에", "옛날에", "저번에")
# Claude Code 가 cwd 마다 별도 projects 슬롯을 만들기 때문에 런타임 glob 으로
# 모든 슬롯의 memory 디렉토리를 흡수한다. (~/.claude/projects/*/memory)
def _discover_memory_dirs() -> list[Path]:
    # v3.2.7: env var 우선 — module-level DATA_DIR 패턴과 일관성.
    import os as _os
    root = Path(_os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/memory") if p.is_dir())


MEMORY_DIRS = _discover_memory_dirs()
# Sprint 11: env var `MV3_EXTRA_MEMORY_DIRS=path1:path2` — _mtime_changed가 이
# 디렉토리들도 watch해야 indexer trigger 일관. _spawn_reindex가 부모 env 보존하므로
# indexer 본체는 자체적으로 같은 env 읽어 처리.
import os as _os_envread
_seen_dirs = {str(d) for d in MEMORY_DIRS}
_extra = _os_envread.environ.get("MV3_EXTRA_MEMORY_DIRS", "").strip()
if _extra:
    for _piece in _extra.split(":"):
        _piece = _piece.strip()
        if _piece:
            _p = Path(_piece).expanduser()
            if str(_p) not in _seen_dirs:
                _seen_dirs.add(str(_p))
                MEMORY_DIRS.append(_p)
# Sprint 16: sources.json (영구 등록) 도 mtime watch 대상에 포함
_SOURCES_CFG = DATA_DIR / "sources.json"
try:
    if _SOURCES_CFG.is_file():
        import json as _json
        _cfg = _json.loads(_SOURCES_CFG.read_text(encoding="utf-8"))
        for _s in (_cfg.get("sources") or []):
            if isinstance(_s, str) and _s:
                _p = Path(_s).expanduser()
                if str(_p) not in _seen_dirs:
                    _seen_dirs.add(str(_p))
                    MEMORY_DIRS.append(_p)
except Exception:
    pass
INDEX_DB = DATA_DIR / "index.db"

# T8 (v3.4 contradiction): deprecated_by 감쇠 primitives 를 hook 모듈 namespace
# 에 re-export — tests/test_memory_recall_deprecated.py 가 hooks/memory-recall.py
# 를 직접 spec_from_file_location 로 로드하기 때문. 실제 로직은 memory_search.py
# 안의 recall_memory pipeline 에서 호출된다 (single source of truth).
try:
    for _d in (
        Path(_os_paths.environ.get("MV3_SCRIPTS_DIR", "~/.claude/scripts/mindvault")).expanduser(),
        Path(__file__).resolve().parent.parent / "src",
    ):
        if _d.is_dir() and str(_d) not in sys.path:
            sys.path.insert(0, str(_d))
    from memory_search import (  # noqa: E402
        DEPRECATED_DECAY,
        _apply_deprecation_decay,
        _is_deprecated,
    )
except Exception:
    # silent fallback — hook 은 절대 import 단계에서 깨지지 않는다.
    DEPRECATED_DECAY = 0.3
    def _is_deprecated(path):  # type: ignore[no-redef]
        return False
    def _apply_deprecation_decay(path, original_score):  # type: ignore[no-redef]
        return original_score

# import 경로 — production(배포본) + dev(repo) 둘 다 지원.
# v3.2.7: MV3_SCRIPTS_DIR env var 우선 (테스트 격리 + 사용자 override).
_HOOK_FILE = Path(__file__).resolve()
SCRIPTS_DIRS = [
    Path(_os_paths.environ.get("MV3_SCRIPTS_DIR", "~/.claude/scripts/mindvault")).expanduser(),
    _HOOK_FILE.parent.parent / "src",
]


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] hook-recall: {msg}\n")
    except Exception:
        pass


def _metric(payload: dict) -> None:
    try:
        with METRICS_LOG.open("a") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _mtime_changed() -> bool:
    try:
        db_mt = INDEX_DB.stat().st_mtime
    except FileNotFoundError:
        return True
    for d in MEMORY_DIRS:
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime > db_mt:
                return True
            for p in d.glob("*.md"):
                if p.stat().st_mtime > db_mt:
                    return True
            # Sprint 13: _procedural/ 하위 .md 변경도 reindex trigger
            proc = d / "_procedural"
            if proc.is_dir():
                if proc.stat().st_mtime > db_mt:
                    return True
                for p in proc.glob("*.md"):
                    if p.stat().st_mtime > db_mt:
                        return True
        except OSError:
            continue
    return False


# NEXT-27 (2026-05-24): _spawn_reindex throttle.
# 이전: _mtime_changed 가 True 인 동안 매 hook 호출마다 reindex child 를
# subprocess.Popen 으로 fork 했음. 메모리 .md 가 자주 갱신되는 상황에서
# concurrent reindex 100건이 동시에 떠 e2e perf 회귀(avg 452ms) 가중.
# 매 spawn 전에 lock 파일의 mtime 을 보고 SPAWN_THROTTLE_SEC 이내면 skip —
# child 가 이미 도는 중이면 굳이 새로 띄울 필요 없음.
SPAWN_THROTTLE_SEC = 30
SPAWN_LOCK = DATA_DIR / "reindex-spawn.lock"
# audit-2026-05-24 Fix #9: _spawn_reindex 가 사용하므로 함수 정의 전에 둠.
RECURSION_GUARD_ENV = "MV3_HOOK_RECURSION_GUARD"


def _spawn_reindex() -> None:
    """incremental_index를 백그라운드로 분리 spawn. 결과 안 기다림.

    NEXT-27 throttle: SPAWN_LOCK 의 mtime 이 SPAWN_THROTTLE_SEC 이내면 skip.
    audit-2026-05-24: TOCTOU race 차단 — `fcntl.flock(LOCK_EX|LOCK_NB)` 로
    atomic 화. 동시 hook burst 시 한 process 만 lock 획득해 spawn,
    나머지는 즉시 빠짐. spawn 완료 시 lock 파일에 1바이트 마커를 써서
    "이전에 한 번이라도 spawn 됨" 을 표시 (size=0 이면 첫 호출이라 throttle 우회).
    """
    import os  # noqa: WPS433  부모 env 보존 + lock fd
    import fcntl  # noqa: WPS433  POSIX (macOS)
    try:
        SPAWN_LOCK.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(SPAWN_LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        _debug(f"spawn lock open fail: {e}")
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # 다른 hook 이 spawn 처리 중 — 양보
        try:
            st = os.fstat(fd)
            # size=0 placeholder = 첫 spawn 이라 throttle 우회. size>0 + mtime
            # 30s 이내면 최근 spawn 됐으니 skip.
            if st.st_size > 0 and (time.time() - st.st_mtime) < SPAWN_THROTTLE_SEC:
                return
        except OSError:
            pass

        scripts_path = ":".join(str(d) for d in SCRIPTS_DIRS if d.is_dir())
        code = (
            "import sys, os;"
            "sys.path[:0] = os.environ.get('MV3_SCRIPTS_PATH','').split(':');"
            "from memory_indexer import incremental_index;"
            "incremental_index()"
        )
        env = {"MV3_SCRIPTS_PATH": scripts_path, "PATH": ""}
        env.update({k: v for k, v in os.environ.items() if k not in env})
        env["MV3_SCRIPTS_PATH"] = scripts_path
        # Fix #6 (audit-2026-05-24): child 에 RECURSION_GUARD 전파 — 향후
        # indexer 안에서 hook trigger 코드가 추가돼도 무한 재귀 차단.
        env[RECURSION_GUARD_ENV] = "1"
        # Fix #10 (audit-2026-05-24): marker 를 Popen 시도 *전* 에 박음 —
        # Popen 이 OSError 로 실패해도 throttle 유지 (이전 동작: marker 안
        # 박혀 size=0 유지 → 다음 hook 마다 재시도 폭주). 실패 후 30s 뒤
        # 다음 시도 시 재진입 OK.
        try:
            os.pwrite(fd, b"1", 0)
        except OSError:
            pass
        try:
            subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except OSError as e:
            _debug(f"spawn reindex Popen fail: {e}")
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


class _Timeout(BaseException):
    """Hook hard-budget sentinel.

    BaseException 상속이라 호출 chain 어디서도 `except Exception` 에
    swallow 되지 않는다. KeyboardInterrupt/SystemExit 와 동일 패턴.
    이전엔 Exception 상속이라 `memory_search.recall_memory` 의 broad
    `except Exception` 가 잡아 "recall FATAL: _Timeout" + traceback 을
    debug.log 에 51건 누적시켰고, 실제로는 정상 hook budget timeout 인데
    panic 레벨로 보였다. BaseException 로 바뀐 뒤엔 hook 외 어떤 핸들러도
    잡지 않으므로 호출 stack 을 깔끔히 unwind → outer hook 의
    `except _Timeout` 만 silent skip 처리.
    """


def _alarm_handler(_signum, _frame):
    raise _Timeout()


# v3.2.6 H1: memory 본문에 '</system-reminder>' literal 이 들어가면 hook 출력이
# early-close 되고 뒤 내용이 system context 밖으로 누출. zero-width space 삽입
# 으로 visually 동일하지만 Claude Code parser 의 close tag 매칭은 차단.
_CLOSE_TAG_RE = re.compile(r"</(\s*)(system-reminder)(\s*)>", re.IGNORECASE)


def _sanitize(text: str) -> str:
    if not text:
        return text
    return _CLOSE_TAG_RE.sub(r"</\1​\2\3>", text)


def _format_output(results: list[dict]) -> str:
    """NEXT-37 Phase 2 (Step 2) — Zep "MEMORY CONTEXT:" 라벨 + Chain-of-Note
    self-report 강제 prompt.

    배경 (3-agent 자료조사 수렴): 회수된 메모리를 LLM 답변에 강제 통합하는
    유일한 검증된 메커니즘 = "답변에 활용 흔적을 explicit 출력하라"는
    contract. Zep production 검증 포맷 + Chain-of-Note EM +7.9 효과 + Cursor
    positive instruction 패턴 결합.

    옛 format ("# 메모리 회수 (Layer 4 hybrid)") 은 self_eval.py 의
    RECALL_INJECTION_HEADERS 가 backward compat 으로 retroactive 분석 보존.
    """
    # Round 1 fix L2: 빈 results 직접 호출 시 header+contract 만 박혀
    # LLM 이 false self-report 하는 시나리오 차단. main() 도 이미 가드
    # 하지만 helper 자체 invariant 보장.
    if not results:
        return ""
    lines = [
        "<system-reminder>",
        "MEMORY CONTEXT (다음 fact 를 본 답변 reasoning 에 반드시 통합):",
        "",
    ]
    for r in results:
        # Round 1 fix L1: source label 도 _sanitize — sanitization contract
        # 일관성. 현재는 internal label (vec/fts/alias) 만이라 exploitability
        # 낮지만 contract 누락은 닫는다.
        # source 는 항상 list 지만 스칼라 'vec' 가 오면 "+".join 이 'v+e+c' 로 분해된다.
        # isinstance 가드 — recall_core.format_memory_context 와 parity (or [] 만으론
        # truthy 비-list 못 막음).
        _src_val = r.get("source")
        _src_list = _src_val if isinstance(_src_val, list) else ([] if not _src_val else [str(_src_val)])
        srcs = _sanitize("+".join(_src_list))
        # Round 1 fix L3: name 안 ']' 가 들어가면 RECALLED_NAME_RE 가 첫 ']'
        # 에서 끊겨 추출 실패. 안전한 escape 로 차단.
        raw_name = r.get("name") or "(unnamed)"
        name = _sanitize(raw_name.replace("]", ")"))
        desc = _sanitize(r.get("description") or "")
        snippet = _sanitize(r.get("snippet") or "")
        score = r.get("score", 0)
        lines.append(f"- [{name}] (score {score:.2f}, {srcs}) — {desc}")
        prov = r.get("provenance") or {}
        if prov.get("source_type") and prov["source_type"] != "unknown":
            # session 은 UUID 8자 prefix, url/commit/file 등은 전체(120 캡) — audit:
            # [:8] 고정이면 url 이 'https://' 로만 잘려 모든 url provenance 가 비구별.
            _sr = str(prov.get("source_ref") or "")
            ref = _sanitize(_sr[:8] if prov["source_type"] == "session" else _sr[:120])
            cap = _sanitize(str(prov.get("captured_at") or "")[:10])
            lines.append(f"  출처: {_sanitize(str(prov['source_type']))} {ref} {cap}".rstrip())
        rv = r.get("reverify")
        rv = rv if isinstance(rv, dict) else {}
        if rv.get("status") == "stale":
            note = _sanitize(str(rv.get("note") or "")).strip()
            tail = f"{note} " if note else ""
            lines.append(f"  재검증 필요: {tail}(현행 코드/사실 대조 후 신뢰)")
        if snippet:
            lines.append(f"  발췌: {snippet}")
    lines.append("")
    lines.append(
        "답변 시작 전 한 줄로 \"회수 노트: <위 메모리가 본 질문과 어떻게 "
        "관련되는가, 무관하면 '무관'>\" 명시 출력 의무. 회수 fact 와 답변이 "
        "모순되면 즉시 표기. 옵션·권장·다음 단계 제시 시 위 회수 메모리에 "
        "명시된 룰·제약과 충돌하는 항목은 제거하거나 \"회수 메모리 <이름> 위반 "
        "가능성\"으로 표기."
    )
    lines.append("</system-reminder>")
    return "\n".join(lines) + "\n"


def main() -> int:
    # sub-session의 hook 즉시 skip (자기 자신 발동에서 또 호출되는 무한 재귀 차단)
    import os as _os
    if _os.environ.get(RECURSION_GUARD_ENV) == "1":
        try:
            sys.stdin.read()
        except Exception:
            pass
        return 0
    t0 = time.time()
    # 이전 핸들러 저장→finally 복원: 안 하면 _alarm_handler 가 프로세스에 남아 이후
    # stray SIGALRM 이 무관 코드에서 _Timeout(BaseException)을 던진다 (단발 hook 은
    # 무해하나 장수 인터프리터=pytest 에서 flaky). compact 경로와 동일 패턴.
    _prev_sigalrm = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, HARD_TIMEOUT_MS / 1000.0)

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return 0
        if not isinstance(payload, dict):
            return 0

        prompt = (payload.get("prompt") or "").strip()
        if len(prompt) < MIN_PROMPT_LEN:
            return 0

        # v4 P1: 발화 에이전트 태깅 — hook 등록 command 가 MV3_AGENT 를 명시
        # (claude=install.sh, codex=manage_codex_recall.py). 미설정·비인가 값은
        # "unknown" — 수동 실행·제3 소비자를 claude 로 오분류하지 않는다.
        agent = _os.environ.get("MV3_AGENT", "").strip()
        if agent not in ("claude", "codex"):
            agent = "unknown"

        # NEXT-31 (2026-05-24): _mtime_changed() 자체가 500+ stat 호출 (메모리
        # 디렉토리 5×100건 + _procedural). SPAWN_LOCK age 가 SPAWN_THROTTLE_SEC
        # 이내라면 이미 reindex spawn 직후라 어차피 throttle 로 skip 되므로
        # mtime check 자체를 건너뛰어 hot-path latency 절약 (~5-30ms).
        _skip_mtime = False
        try:
            if SPAWN_LOCK.exists():
                if (time.time() - SPAWN_LOCK.stat().st_mtime) < SPAWN_THROTTLE_SEC:
                    _skip_mtime = True
        except OSError:
            pass
        if not _skip_mtime and _mtime_changed():
            _spawn_reindex()

        for d in SCRIPTS_DIRS:
            if d.is_dir() and str(d) not in sys.path:
                sys.path.insert(0, str(d))

        # Sprint 16: query intent classifier — chat/meta 는 회수 강제 skip.
        # rule-based 라 latency 추가 ~0. 미import 실패 시 fallback (skip 없음).
        intent_label = "unknown"
        intent_match: list[str] = []
        try:
            from query_intent import (  # noqa: WPS433
                classify,
                classify_with_gemma,
                gemma_intent_enabled,
                should_skip_recall,
            )
            intent_obj = classify(prompt)
            # Sprint NEXT-3: rule-based unknown 보강 — opt-in env 일 때만, 짧은 query 만.
            if intent_obj.intent == "unknown" and gemma_intent_enabled():
                gemma_obj = classify_with_gemma(prompt)
                if gemma_obj is not None:
                    intent_obj = gemma_obj
            intent_label = intent_obj.intent
            intent_match = list(intent_obj.matched)
            if should_skip_recall(intent_obj):
                _metric({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "kind": "recall_skip",
                    "agent": agent,
                    "reason": f"intent:{intent_label}",
                    "intent": intent_label,
                    "matched": intent_match,
                    "query_len": len(prompt),
                })
                _debug(
                    f"skip recall agent={agent} intent={intent_label} "
                    f"match={intent_match!r}"
                )
                return 0
        except _Timeout:
            # post-ship: SIGALRM 의 _Timeout 은 outer 핸들러로 전파 — hook 의
            # HARD_TIMEOUT_MS budget 보장. 이전 broad `except Exception` 가
            # _Timeout 까지 swallow 해 첫 cold Gemma 호출 시 641ms 까지 늘었던
            # 회귀(20:17 debug.log) 차단.
            raise
        except Exception as e:
            _debug(f"intent classify skipped: {type(e).__name__}: {e}")

        from memory_search import recall_memory  # noqa: WPS433

        # 회수 단서어 있으면 임계값 완화 (사용자 의도 명확)
        has_hint = (
            intent_label == "recall"
            or any(h in prompt for h in RECALL_HINTS)
        )
        raw_min = RAW_COSINE_MIN_HINTED if has_hint else RAW_COSINE_MIN_DEFAULT

        results = recall_memory(
            prompt,
            top_k=TOP_K,
            score_threshold=SCORE_THRESHOLD,
            raw_cosine_min=raw_min,
        )

        elapsed_ms = int((time.time() - t0) * 1000)
        max_score = results[0]["score"] if results else 0.0
        raw_top = results[0].get("raw_cosine", 0.0) if results else 0.0
        _metric({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "recall",
            "agent": agent,
            "query_len": len(prompt),
            "elapsed_ms": elapsed_ms,
            "picked": len(results),
            "max_score": max_score,
            "raw_top1_cosine": raw_top,
            "raw_min": raw_min,
            "has_hint": has_hint,
            "intent": intent_label,
            "intent_matched": intent_match[:3],
            "recalled_ids": [
                (r.get("name") or r.get("path", "")) for r in results
            ],
        })
        _debug(
            f"agent={agent} query_len={len(prompt)} "
            f"picked={len(results)} elapsed_ms={elapsed_ms}"
        )

        if not results:
            return 0

        sys.stdout.write(_format_output(results))
        return 0
    except _Timeout:
        _debug(f"timeout {HARD_TIMEOUT_MS}ms — skip")
        return 0
    except Exception as e:
        _debug(f"FATAL {type(e).__name__}: {e}")
        return 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        # 이전 SIGALRM 핸들러 복원 (누수 차단). _prev None 이면 SIG_DFL.
        try:
            signal.signal(
                signal.SIGALRM,
                _prev_sigalrm if _prev_sigalrm is not None else signal.SIG_DFL,
            )
        except (TypeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    sys.exit(main())
