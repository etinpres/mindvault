#!/usr/bin/env python3
"""MindVault v3 Sprint 3 — SessionEnd 훅.

세션 종료 시 마지막 턴에 '영구 기억' 트리거가 있으면 Gemma로 후보 추출 →
memory/_staged/*.md 로 저장. 실제 memory/ 파일은 절대 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

# import path 보강: hook (~/.claude/hooks/)에 배포돼도 memory_extractor는
# ~/.claude/scripts/mindvault/에 있음. dev/repo는 src/ 옆에 같이 있음.
# 자기 자신이 있는 디렉토리에 memory_extractor.py 가 같이 있으면 dev/repo 또는
# 정상 배포된 production — 그쪽만 sys.path 에 등록한다. 없을 때만(hooks/ 만 배포된 경우)
# production fallback 을 추가. 이렇게 안 하면 worktree 테스트 시 production 코드가
# 우선 잡혀 새 함수가 안 보임.
_HOOK_FILE = Path(__file__).resolve()
_HOOK_DIR = _HOOK_FILE.parent
if (_HOOK_DIR / "memory_extractor.py").is_file():
    if str(_HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOK_DIR))
else:
    # v3.2.7: MV3_SCRIPTS_DIR env var 우선.
    _PROD = Path(os.environ.get("MV3_SCRIPTS_DIR", "~/.claude/scripts/mindvault")).expanduser()
    if _PROD.is_dir() and str(_PROD) not in sys.path:
        sys.path.insert(0, str(_PROD))

RECURSION_GUARD_ENV = "MV3_HOOK_RECURSION_GUARD"
# sub-session의 SessionEnd 즉시 skip
if os.environ.get(RECURSION_GUARD_ENV) == "1":
    sys.exit(0)

# NEXT-19 final fix (2026-05-24): Claude Code hook subprocess 가 본체 env 안 inherit —
# launchctl plist / .zshrc / .zshenv / wrapper export 모두 fail.
# wrapper 의 setsid 는 macOS 미지원, settings.json 직접 호출 path 는 wrapper 우회.
# 영구화는 코드 안에 setdefault — 외부 명시 override 가능 (테스트), default 는 항상 ON.
os.environ.setdefault("MV3_AUTO_COMPILE", "1")
os.environ.setdefault("MV3_EXTRACTOR_ALWAYS_FIRE", "1")

from memory_extractor import extract_from_jsonl  # type: ignore  # noqa: E402

# v3.2.7: production state pollution 방지. MV3_DATA_DIR / MV3_PROJECTS_ROOT
# env var 가 set 됐으면 우선 (테스트 conftest 가 tmp 로 강제). default 는 production.
_MV3_DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
PROJECTS_ROOT = Path(os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()
# memory 저장 base 는 현재 사용자 $HOME 에서 파생된 Claude Code 프로젝트 슬롯
# (단일 원천 of truth). 예: HOME=/Users/alice → 슬러그 `-Users-alice`.
# v3.2.8: env override 우선순위 — close-session.md skill 과 같은 변수 인식해 slot
# divergence 차단. (1) `MV3_MEMORY_DIR` — skill 과 동일 변수, 이게 MEMORY_DIR
# 자체. (2) `MV3_PROJECTS_DIR` — 슬롯 root. (3) home_slug default.
# jsonl 탐색은 PROJECTS_ROOT 의 모든 하위 슬롯에서 — Sprint 6 indexer 와 동일.
def _default_memory_dir() -> Path:
    mem_override = os.environ.get("MV3_MEMORY_DIR", "").strip()
    if mem_override:
        return Path(mem_override).expanduser()
    proj_override = os.environ.get("MV3_PROJECTS_DIR", "").strip()
    if proj_override:
        return Path(proj_override).expanduser() / "memory"
    home_slug = "-" + str(Path.home()).strip("/").replace("/", "-")
    return PROJECTS_ROOT / home_slug / "memory"


MEMORY_DIR = _default_memory_dir()
PROJECTS_DIR = MEMORY_DIR.parent
STAGED_DIR = MEMORY_DIR / "_staged"
# Sprint 13: procedural type 후보는 _procedural/_staged/ 슬롯에 저장. 결정 메모리와
# 분리해 indexer + grep·인벤토리 시 한눈에 구분 가능. memory_review_cli 가
# 양쪽 staged 모두 스캔.
PROCEDURAL_DIR = MEMORY_DIR / "_procedural"
PROCEDURAL_STAGED_DIR = PROCEDURAL_DIR / "_staged"
DEBUG_LOG = _MV3_DATA_DIR / "debug.log"


def staged_dir_for(memory_type: str) -> Path:
    """type 별 staged 슬롯. procedural 만 _procedural/_staged/, 나머지는 기존 슬롯."""
    if memory_type == "procedural":
        return PROCEDURAL_STAGED_DIR
    return STAGED_DIR


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] session-end: {msg}\n")
    except Exception:
        pass


def slugify(title: str) -> str:
    slug = re.sub(r"\s+", "_", title.strip())
    slug = re.sub(r"[^\w가-힣\-]", "", slug)
    return slug[:30] or "memory"


def _fm_oneline(value) -> str:
    """frontmatter 스칼라 값을 단일 라인으로 정규화 (session-hooks-frontmatter-1).

    줄바꿈/캐리지리턴을 공백으로 치환하고 연속 공백을 하나로 접은 뒤 strip. 라인
    기반 frontmatter 가 LLM 값의 줄바꿈으로 깨지는 것을 막는다. 따옴표는 쓰지 않아
    (콜론 포함 값도) naive first-colon split 파서와 호환.
    """
    return re.sub(r"\s+", " ", str(value).replace("\r", " ").replace("\n", " ")).strip()


def existing_slugs() -> set[str]:
    slugs: set[str] = set()
    for d in (MEMORY_DIR, STAGED_DIR, PROCEDURAL_DIR, PROCEDURAL_STAGED_DIR):
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            slugs.add(f.stem.split("_", 2)[-1] if "_" in f.stem else f.stem)
    return slugs


def write_staged(
    item: dict, session_id: str, slug_override: str | None = None,
    source_type: str = "session", source_ref: str | None = None,
) -> Path | None:
    """staged 파일 작성. slug_override 로 충돌 회피 suffix 부여 가능 (Sprint NEXT-6)."""
    staged_dir = staged_dir_for(item["type"])
    staged_dir.mkdir(parents=True, exist_ok=True)
    slug = slug_override if slug_override is not None else slugify(item["title"])
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{ts}_{item['type']}_{slug}.md"
    path = staged_dir / filename
    # bug-audit 2026-05-29 (session-hooks-frontmatter-1): LLM 이 만든 값에 줄바꿈이
    # 섞이면 라인 기반 frontmatter 구조가 깨진다 — 값 안의 '\n' 이 가짜 키 라인을
    # 만들거나, 값이 '---' 를 포함하면 frontmatter 가 조기 종료돼 다음 /memory_review
    # 파서가 본문/메타를 오독한다. 각 값을 단일 라인으로 정규화한다. 콜론은 라인
    # 파서가 first-colon split 이라 값에 남아도 안전하므로 따옴표로 감싸지 않는다
    # (따옴표를 쓰면 naive 파서가 따옴표를 값에 포함시켜 새 artifact 발생).
    title = _fm_oneline(item["title"])
    fm_lines = [
        f"name: {title}",
        f"description: {title}",
        f"type: {_fm_oneline(item['type'])}",
        f"staged_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"staged_from_session: {session_id[:8]}",
        f"reason: {_fm_oneline(item['reason'])}",
        f"evidence: {_fm_oneline(item['evidence'])}",
        f"source_type: {_fm_oneline(source_type)}",
        f"source_ref: {_fm_oneline(source_ref or session_id)}",
    ]
    # Sprint 14: memory compiler 가 부착한 update 메타 보존. review CLI 가
    # update_of 보고 diff/approve 분기.
    if item.get("update_of"):
        fm_lines.append(f"update_of: {_fm_oneline(item['update_of'])}")
    if item.get("diff_summary"):
        fm_lines.append(f"diff_summary: {_fm_oneline(item['diff_summary'])}")
    frontmatter = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + f"{item['body']}\n"
    # v3.2.6 H2: atomic write — tmp + os.replace 로 partial markdown 차단.
    # crash 직전 write_text 가 절반만 flush 되면 다음 /memory_review 가
    # broken frontmatter parse 에 실패. alias_generator 의 동일 패턴 따름.
    # v3.2.8: finally — KeyboardInterrupt 도 tmp orphan 차단.
    # bug-audit 2026-06-02 (#6): PID-고유 tmp. 고정 ".tmp" 는 동시 same-slug
    # SessionEnd(sibling Conductor workspaces)에서 한 프로세스의 finally tmp.unlink
    # 가 다른 프로세스의 tmp 를 지워 os.replace FileNotFoundError → 후보 lost update.
    # reverify.py / alias_generator.py / contradiction_review_cli.py 와 동일 패턴.
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        try:
            tmp_path.write_text(frontmatter, encoding="utf-8")
            os.replace(tmp_path, path)
            return path
        except OSError as e:
            _debug(f"write fail {filename}: {e}")
            return None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def make_contradiction_aware_writer(base_writer, mem_dir: Path):
    """v3.4 T5: wrap a write_staged-style writer so each successful staged write
    triggers contradiction detection against already-promoted memories.

    Args:
        base_writer: callable (item, session_id, slug_override=None) -> Path | None
        mem_dir: directory of promoted memories (NOT _staged/). detect_contradictions
                 filters recall results to this subtree.

    Returns:
        Wrapped writer with the same signature. Best-effort detection: any failure
        in detect/append is swallowed (logged via _debug) and the staged path is
        still returned unchanged. write_staged returning None (dedup skip) → no
        detection fires.

    Critical contract: candidate dict carries 'path' = staged_path so
    _recall_candidates can do path-identity self-exclusion (T2 contract — closes
    the stem-suffix false-positive gap for short slugs like 'metric', 'fix').

    Opt-out: set MV3_CONTRADICTION_DISABLE=1 to bypass detection (return base_writer
    output unchanged). Use for ops emergency disable without uninstall — the staged
    write itself is never blocked, only the detect/append step is skipped.
    """

    def wrapped(item, session_id, slug_override=None, **kwargs):
        staged_path = base_writer(item, session_id, slug_override=slug_override, **kwargs)
        if not staged_path:
            return staged_path  # dedup skip or write failure — no detection

        # Kill switch: env-based opt-out for ops emergency. Checked AFTER staged
        # write so the write itself is never blocked, only detection is skipped.
        if os.environ.get("MV3_CONTRADICTION_DISABLE", "").strip() == "1":
            _debug("contradiction detection disabled by MV3_CONTRADICTION_DISABLE=1")
            return staged_path

        try:
            from contradiction_detector import (
                detect_contradictions,
                append_to_review_queue,
            )
            slug = slug_override or item.get("slug") or ""
            candidate = {
                "slug": slug,
                "title": item.get("title", ""),
                "body": item.get("body", ""),
                "type": item.get("type", ""),
                "path": staged_path,  # T2 contract: path-identity self-exclusion
            }
            contradictions = detect_contradictions(candidate, mem_dir)
            if contradictions:
                append_to_review_queue(slug, contradictions, new_path=staged_path)
                _debug(f"contradiction: {len(contradictions)} found for {slug}")
        except Exception as e:
            # best-effort — staged write already succeeded
            _debug(
                f"contradiction detect/append error for "
                f"{item.get('slug')}: {type(e).__name__}: {e}"
            )

        return staged_path

    return wrapped


def _stage_with_conflict_resolution(
    candidates: list[dict],
    existing_slugs_set: set,
    session_id: str,
    writer,
) -> int:
    """Sprint NEXT-6: session 안 동일 slug 다중 candidate 처리.

    - 기존 memory 와 slug 충돌 + update_of 없음 → skip (file overwrite 방지)
    - session 안 동일 slug + body 완전 동일 → skip (dedup, 정보 손실 아님)
    - session 안 동일 slug + body 다름 → `_2`, `_3` suffix 로 모두 살림
    writer(item, session_id, slug_override=...) -> Path | None 콜백.
    """
    # Codex review fix: 같은 session 에 base='same' 와 base='same_2' 가 동시에
    # 있으면 첫 'same' suffix 가 'same_2' 가 되어 자연 slug 'same_2' 와 collision.
    # base 별 body 카운터 외에 final generated slug 도 글로벌 추적 — 충돌 시
    # 다음 suffix 로 skip.
    session_slug_bodies: dict[str, list[str]] = {}
    used_final_slugs: set[str] = set()
    written = 0
    for item in candidates:
        s_base = slugify(item["title"])
        body = (item.get("body") or "").strip()
        if s_base in existing_slugs_set and not item.get("update_of"):
            _debug(f"dup slug vs existing {s_base}, skip")
            continue
        prev_bodies = session_slug_bodies.setdefault(s_base, [])
        if body and body in prev_bodies:
            _debug(f"dup body in session {s_base}, skip")
            continue
        # base 자연 형태부터 시도, 충돌하면 _2, _3 ... 으로 증분
        idx = len(prev_bodies)
        while True:
            candidate_slug = s_base if idx == 0 else f"{s_base}_{idx + 1}"
            if (
                candidate_slug not in used_final_slugs
                and candidate_slug not in existing_slugs_set
            ):
                break
            idx += 1
        s_final = candidate_slug
        prev_bodies.append(body)
        if writer(item, session_id, slug_override=s_final):
            written += 1
            used_final_slugs.add(s_final)
            # NOTE: existing_slugs_set 에 추가하지 않음 — session 안 추적은
            # session_slug_bodies + used_final_slugs 가 담당. existing_slugs_set 는
            # "file system 의 기존 memory" 만 의미해야, 동일 session 안의 다음
            # candidate 가 잘못 "기존 충돌" 로 skip 되지 않는다.
    return written


# v4 P3: Codex Stop turn 멱등키 저장소 (S2 스펙 §Stop — stop_hook_active
# 재진입·중복 전달에서 같은 turn 의 이중 추출 방지).
_CODEX_TURNS_SEEN = _MV3_DATA_DIR / "codex_turns_seen.jsonl"
_CODEX_TURNS_CAP = 1000   # 초과 시 트림
_CODEX_TURNS_KEEP = 500   # 트림 후 보존 개수


def _codex_turn_seen_and_mark(sid: str, turn: str) -> bool:
    """(session_id, turn_id) 멱등키 체크 후 기록. True = 이미 처리한 turn.

    실패는 False 로 fail-open — 최악이 중복 추출인데 extractor 의 prompt
    SHA256 캐시가 흡수한다. flock 으로 동시 Stop 발화 경합 보호.
    """
    key = f"{sid}\t{turn}"
    try:
        import fcntl
        _CODEX_TURNS_SEEN.parent.mkdir(parents=True, exist_ok=True)
        with _CODEX_TURNS_SEEN.open("a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            lines = f.read().splitlines()
            if key in lines:
                return True
            lines.append(key)
            if len(lines) > _CODEX_TURNS_CAP:
                tmp = _CODEX_TURNS_SEEN.with_suffix(".tmp")
                tmp.write_text(
                    "\n".join(lines[-_CODEX_TURNS_KEEP:]) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, _CODEX_TURNS_SEEN)
            else:
                f.seek(0, 2)
                f.write(key + "\n")
            return False
    except Exception as e:
        _debug(f"codex turn-seen store fail (fail-open): {type(e).__name__}: {e}")
        return False


def _codex_stop_transcript(payload: dict) -> Path | None:
    """Codex Stop payload → 추출 대상 transcript 경로. None = skip (fail-open).

    S2 계약: transcript_path null·파일 없음·중복 turn 전부 exit 0 경로.
    """
    tp = payload.get("transcript_path")
    if not tp or not isinstance(tp, str):
        _debug("codex-stop: transcript_path 없음 — skip")
        return None
    p = Path(tp).expanduser()
    if not p.is_file():
        _debug(f"codex-stop: transcript 파일 없음 — skip ({p.name})")
        return None
    sid = str(payload.get("session_id") or "")
    turn = str(payload.get("turn_id") or "")
    if sid and turn and _codex_turn_seen_and_mark(sid, turn):
        _debug(f"codex-stop: turn 중복 — skip ({sid[:8]}/{turn[:8]})")
        return None
    _debug(f"codex-stop: extract {p.name} ({sid[:8]}/{turn[:8]})")
    return p


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        sid = os.environ.get("CLAUDE_SESSION_ID", "")
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict):
            sid = payload.get("sessionId") or payload.get("session_id") or sid

        # v4 P3: Codex Stop 분기 — transcript 경로를 payload 가 직접 주므로
        # PROJECTS_ROOT glob 을 타지 않는다. 이후 파이프라인(추출→reverify→
        # compile→staged→index/alias)은 Claude 경로와 완전 공유.
        jsonl: Path | None = None
        if (
            isinstance(payload, dict)
            and payload.get("hook_event_name") == "Stop"
            and "transcript_path" in payload
        ):
            jsonl = _codex_stop_transcript(payload)
            if jsonl is None:
                return 0
            sid = str(payload.get("session_id") or "codex-unknown")

        if jsonl is None:
            if not sid:
                _debug("no session id; skip")
                return 0
            matches = sorted(PROJECTS_ROOT.glob(f"*/{sid}.jsonl"))
            if not matches:
                _debug(f"jsonl missing for {sid[:8]}")
                return 0
            jsonl = matches[0]
            if len(matches) > 1:
                _debug(f"jsonl multi-hit for {sid[:8]}: picked {jsonl.parent.name}")

        candidates = extract_from_jsonl(jsonl)

        # Phase 1③ (reliability): stale 재검증 증분 — candidates 유무와 무관하게 매
        # SessionEnd 시도(maybe_scan_due 가 sidecar 로 7일 self-throttle). no-candidate
        # 세션(흔함)이 early-return 하기 전에 호출해 '주1회' cadence 가 staging 발생
        # 여부에 결합되지 않게 한다(audit sweep R1). best-effort silent-fail.
        try:
            from reverify import maybe_scan_due
            rstat = maybe_scan_due(MEMORY_DIR)
            if rstat is not None:
                _debug(
                    f"reverify scan flagged={rstat.get('flagged', 0)} "
                    f"cleared={rstat.get('cleared', 0)} "
                    f"processed={rstat.get('processed', 0)}/{rstat.get('total', 0)}"
                )
        except Exception as e:
            _debug(f"reverify skipped: {type(e).__name__}: {e}")

        if not candidates:
            _debug(f"no candidates for {sid[:8]}")
            return 0

        # Sprint 14: opt-in auto compile — 기존 memory 와 매칭되는 후보는
        # Gemma 가 정제해 update_of 메타 부착. env MV3_AUTO_COMPILE=1 일 때만.
        # 정제 실패는 silent — 원본 candidate 그대로 staged 처리.
        try:
            from memory_compiler import auto_compile_enabled, compile_candidates
            if auto_compile_enabled():
                before = sum(1 for c in candidates if not c.get("update_of"))
                candidates = compile_candidates(candidates)
                updates = sum(1 for c in candidates if c.get("update_of"))
                _debug(
                    f"compiled session={sid[:8]} updates={updates}/{before}"
                )
        except Exception as e:
            _debug(f"compile skipped: {type(e).__name__}: {e}")

        slugs = existing_slugs()
        # T5 (v3.4): wrap write_staged so each staged write triggers contradiction
        # detection against promoted memories. mem_dir = MEMORY_DIR (not _staged/).
        contradiction_writer = make_contradiction_aware_writer(
            write_staged, MEMORY_DIR
        )
        written = _stage_with_conflict_resolution(
            candidates, slugs, sid, contradiction_writer
        )
        _debug(f"session {sid[:8]}: staged {written}/{len(candidates)}")

        # NEXT-35 (2026-05-26): memories 테이블 자동 sync. NEXT-34 alias_index
        # 자동 동기화의 짝꿍 단계 — alias_index 는 sync 되는데 그 위에 깔린
        # memories/_fts/_vec 테이블은 install.sh 1회 실행 의존이라 새 .md 추가
        # 후 stale 됐던 결함(v3.2.9 fix). incremental: mtime 비교로 변경·신규
        # 만 처리, lock 못 잡으면 즉시 0 반환. alias_generator 보다 *먼저*
        # 호출해야 memories 테이블 row 가 새로 들어간 상태에서 alias 생성.
        try:
            from memory_indexer import incremental_index as _index_memories
            ic = _index_memories()
            _debug(
                f"index_sync updated={ic.get('updated', 0)} "
                f"skipped={ic.get('skipped', 0)} "
                f"removed={ic.get('removed', 0)}"
            )
        except Exception as e:
            _debug(f"index_sync skipped: {type(e).__name__}: {e}")

        # NEXT-34 (2026-05-25): alias_index 자동 동기화. SessionEnd 가 이미
        # nohup detach 컨텍스트라 추가 비용 OK. Gemma 호출은 incremental —
        # 새 메모리 파일이 없으면 거의 즉시 끝. purge_missing 으로 삭제된
        # 메모리 dangling entry 도 함께 청소. 실패는 silent (recall hot
        # path 와 무관, 다음 SessionEnd 에서 재시도 가능).
        try:
            from alias_generator import generate as _alias_generate
            stats = _alias_generate(purge_missing=True)
            _debug(
                f"alias_sync generated={stats.get('generated', 0)} "
                f"purged={stats.get('purged', 0)} "
                f"failed={stats.get('failed', 0)}"
            )
        except Exception as e:
            _debug(f"alias_sync skipped: {type(e).__name__}: {e}")
        return 0
    except Exception as e:
        _debug(f"FATAL {e}\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
