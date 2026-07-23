#!/usr/bin/env python3
"""MindVault v3 Sprint 14 — Memory Compiler.

세션 종료 시 extractor 가 뽑은 후보(candidate) 와 기존 memory 파일의 매칭을 자동
판단해, 동일 주제 메모리가 이미 있으면 Luna 로 정제·통합한 update 후보로 변환한다.
Karpathy LLM-as-compiler 패턴의 핵심 구현체.

opt-in: session_memory_end 가 환경변수 `MV3_AUTO_COMPILE=1` 일 때만 호출.
실패는 silent — 정제 실패 시 원본 candidate 그대로 통과해 기존 v2.9.2 흐름 유지.

매칭 규칙:
1. frontmatter `name` 완전 일치 (case-insensitive)
2. 안 되면 slugify(title) == _candidate_slug(stem) (timestamp/type prefix 제거)

update 후보 메타:
- `update_of`: 기존 메모리 absolute path
- `diff_summary`: +added -removed 짧은 요약

검토는 /memory_review 의 `diff <filename>` 으로. approve 시 기존 파일 .bak 백업 후 overwrite.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

# memory_indexer 의 디렉토리 정책·frontmatter 파서 공유
sys.path.insert(0, str(Path(__file__).parent))
from memory_indexer import (  # noqa: E402
    DEFAULT_MEMORY_DIRS,
    _collect_md_files,
    _extra_memory_dirs,
    parse_frontmatter,
)

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
DEBUG_LOG = DATA_DIR / "debug.log"
COMPILE_BODY_LIMIT = 500  # update 결과 본문 최대 글자 수 (soft hint)
COMPILE_BODY_HARD_LIMIT = 1200  # 실제 trim 한계
ENABLE_AUTO_COMPILE_ENV = "MV3_AUTO_COMPILE"
# Sprint NEXT-2 — embedding fallback. name exact·slug 매칭이 모두 실패했을 때
# candidate body 임베딩과 memories_vec cosine top-1 이 이 임계값 이상이면
# 같은 주제로 본다. 0.75 는 memory_search 의 raw_cosine 게이트(0.40/0.32) 보다
# 한참 엄격 — false-merge 로 무관 메모리 overwrite 되는 위험을 막기 위한 보수치.
EMBED_MATCH_THRESHOLD = 0.75

# session_memory_end 의 slugify 와 동등 — 매칭 일관성. unit test 가 동등성 보장.
SLUG_CHAR_RE = re.compile(r"[^\w가-힣\-]")
STAGED_STEM_RE = re.compile(r"^\d{8}-\d{6}_[a-z]+_(.+)$")


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] compiler: {msg}\n")
    except Exception:
        pass


def slugify(title: str) -> str:
    """session_memory_end.slugify 와 동일 룰. 매칭 일관성 필수."""
    slug = re.sub(r"\s+", "_", (title or "").strip())
    slug = SLUG_CHAR_RE.sub("", slug)
    return slug[:30] or "memory"


def _candidate_slug(stem: str) -> str:
    """staged 파일명 (20YYMMDD-HHMMSS_<type>_<slug>.md) 에서 본질 slug 추출."""
    m = STAGED_STEM_RE.match(stem)
    return m.group(1) if m else stem


def _call_gemma(prompt: str, max_tokens: int = 800) -> str | None:
    """Legacy alias: compile through one schema-constrained Luna call."""
    del max_tokens
    try:
        from llm_backend import call_codex_text
        return call_codex_text("compile", prompt)
    except Exception as e:
        _debug(f"luna fail: {type(e).__name__} {e}")
        return None


def _find_existing_memory(
    candidate: dict, memory_dirs: list[Path]
) -> dict | None:
    """candidate 와 매칭되는 기존 memory 파일 1건 반환.

    매칭 우선순위:
    1. frontmatter name 완전 일치 (lowercase strip)
    2. slugify(title) == _candidate_slug(stem)
    3. embedding cosine top-1 ≥ EMBED_MATCH_THRESHOLD (의미 매칭, Sprint NEXT-2)
    """
    # bug-audit 2026-06-02 (#16): 빈/공백 title 가드. slugify('')='memory' 폴백
    # 때문에 아래 `if not new_slug` 가드가 dead code 였다 — 빈 title 후보가
    # slug 'memory' 로 promoted memory.md(소문자 stem) 와 오매칭돼 approve 시
    # 무관 파일을 overwrite 할 위험. raw title 로 먼저 거른다.
    _raw_title = (candidate.get("title") or "").strip()
    if not _raw_title:
        return None
    new_name = _raw_title.lower()
    new_slug = slugify(candidate.get("title") or "")
    if not new_slug:
        return None
    # codex R2 (#16 완성): 구두점-only title("!!!" 등)은 strip 후 비어있지 않아
    # 위 가드를 통과하지만 slugify 가 'memory' 폴백을 반환해 memory.md(소문자
    # stem) 와 slug 오매칭된다. slug 문자가 실제로 비었으면(폴백) slug 매칭 자체를
    # 건너뛰고 name-exact/embedding 으로만 매칭한다.
    _slug_content = SLUG_CHAR_RE.sub("", re.sub(r"\s+", "_", _raw_title))[:30]
    _slug_is_fallback = not _slug_content
    # _collect_md_files 가 staged 디렉토리 제외 + _procedural/ 포함.
    # promoted memory 만 대상 — staged 끼리의 update 매칭은 의미 없음.
    fallback_match: dict | None = None
    for p in _collect_md_files(memory_dirs):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = parse_frontmatter(text)
        fm_name = (fm.get("name") or "").strip().lower()
        existing_slug = _candidate_slug(p.stem)
        # 1순위: name exact (가장 신뢰도 높음)
        if new_name and fm_name and new_name == fm_name:
            return {"path": p, "frontmatter": fm, "body": body}
        # 2순위: slug 일치 (fallback). 단, new_slug 가 'memory' 폴백이면 skip (#16).
        if existing_slug == new_slug and not _slug_is_fallback and fallback_match is None:
            fallback_match = {"path": p, "frontmatter": fm, "body": body}
    if fallback_match is not None:
        return fallback_match
    # 3순위: embedding 의미 매칭. 임베딩 서버·numpy·DB 미가용 시 silently None.
    try:
        return _find_by_embedding(candidate, memory_dirs)
    except Exception as e:
        _debug(f"embed match fail: {type(e).__name__} {e}")
        return None


def _find_by_embedding(
    candidate: dict, memory_dirs: list[Path]
) -> dict | None:
    """candidate body 임베딩과 memories_vec cosine top-1 매칭.

    threshold EMBED_MATCH_THRESHOLD 미달이면 None — false-merge 차단.
    embed_text 또는 DB 호출 실패는 None 으로 폴백 (Sprint 11 패턴).
    """
    body = (candidate.get("body") or "").strip()
    if not body:
        return None
    title = (candidate.get("title") or "").strip()
    query_text = f"{title}\n{body}" if title else body
    # lazy import — memory_indexer/numpy 가 없는 환경(테스트) 에서 모듈 import 자체는
    # 깨지지 않게 함수 안에서 시도. 모듈 attribute 호출 패턴이라
    # 테스트에서 `patch.object(memory_indexer, 'embed_text', ...)` 자연스럽게 적용.
    try:
        import memory_indexer  # type: ignore  # noqa: E402
        import indexer  # type: ignore  # noqa: E402
        import numpy as np  # type: ignore  # noqa: E402
    except Exception as e:
        _debug(f"embed deps unavailable: {e}")
        return None
    qvec = memory_indexer.embed_text(query_text, kind="passage")
    if not qvec:
        return None
    conn = indexer.open_db()
    try:
        rows = list(
            conn.execute("SELECT path, kind, embedding FROM memories_vec")
        )
    finally:
        conn.close()
    if not rows:
        return None
    q = np.asarray(qvec, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0:
        return None
    q = q / q_norm
    best_path: str | None = None
    best_sim = -1.0
    for r in rows:
        # bug-audit 2026-06-02 (#15): 손상(비-4배수) blob 은 frombuffer 에서,
        # 차원 불일치(예: 모델 교체 후 stale 768-dim) row 는 matmul 에서 각각
        # ValueError 를 던져 전체 스캔을 중단시킨다 → 뒤쪽 유효 후보까지 매칭
        # 무력화(중복 메모리 양산). 형제 read 경로(memory_search/search)와 동일하게
        # 손상·불일치 행만 skip 하고 스캔 지속.
        try:
            arr = np.frombuffer(r["embedding"], dtype=np.float32)
        except ValueError:
            continue
        if arr.size == 0:
            continue
        if arr.shape != q.shape:
            continue
        a_norm = float(np.linalg.norm(arr))
        if a_norm == 0:
            continue
        sim = float((arr / a_norm) @ q)
        if sim > best_sim:
            best_sim = sim
            best_path = r["path"]
    if best_path is None or best_sim < EMBED_MATCH_THRESHOLD:
        return None
    p = Path(best_path)
    if not p.is_file():
        return None
    # path traversal 방어 — memory_dirs 루트 안 path 만 허용
    if not any(_is_within(p, d) for d in memory_dirs):
        _debug(f"embed match path outside roots: {p}")
        return None
    try:
        text_content = p.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body_ex = parse_frontmatter(text_content)
    _debug(f"embed match cosine={best_sim:.3f} path={p}")
    return {
        "path": p,
        "frontmatter": fm,
        "body": body_ex,
        "match_kind": "embedding",
        "cosine": best_sim,
    }


def _is_within(p: Path, root: Path) -> bool:
    """p 가 root 디렉토리 내부 (또는 동일) 인지. is_relative_to 폴리필."""
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _build_compile_prompt(existing_body: str, candidate: dict) -> str:
    return (
        "당신은 기존 wiki 페이지를 update 하는 편집자다.\n"
        "기존 본문 + 새로 들어온 사실(fact) 을 받아 다음 원칙으로 통합한다:\n"
        "- 기존 본문의 핵심·예시·수치 보존\n"
        "- outdated 된 사실(새 fact 와 모순) 은 새 fact 로 교체\n"
        "- 새 fact 가 기존을 정밀화하면 합쳐서 더 정확하게\n"
        "- 형식·문체 일관 유지, 한국어 우선\n"
        f"- 본문 {COMPILE_BODY_LIMIT}자 이내, plain text (markdown fence 금지)\n\n"
        "기존 본문:\n"
        "---\n"
        f"{existing_body[:COMPILE_BODY_HARD_LIMIT]}\n"
        "---\n\n"
        f"새 fact (title: {candidate.get('title', '')}):\n"
        f"{(candidate.get('body') or '')[:COMPILE_BODY_HARD_LIMIT]}\n\n"
        "수정된 본문만 출력. 해설·markdown fence·frontmatter 금지."
    )


_FENCE_HEAD_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n?")
_FENCE_TAIL_RE = re.compile(r"\n?```$")


def _strip_fences(text: str) -> str:
    text = _FENCE_HEAD_RE.sub("", text)
    text = _FENCE_TAIL_RE.sub("", text)
    return text.strip()


def _compile_update(existing_body: str, candidate: dict) -> str | None:
    """기존 body 와 새 fact 를 Luna 로 통합 → 정제 body. 실패 시 None.

    응답에서 markdown fence 제거 + hard limit trim 후 반환.
    """
    prompt = _build_compile_prompt(existing_body, candidate)
    out = _call_gemma(prompt, max_tokens=800)
    if not out:
        return None
    out = _strip_fences(out)
    if not out:
        return None
    if len(out) > COMPILE_BODY_HARD_LIMIT:
        out = out[:COMPILE_BODY_HARD_LIMIT].rstrip()
    return out


def diff_summary(old: str, new: str) -> str:
    """매우 짧은 +added -removed 요약. review CLI 표시용."""
    if not old and not new:
        return ""
    old_lines = old.splitlines() or [""]
    new_lines = new.splitlines() or [""]
    diff = list(difflib.unified_diff(old_lines, new_lines, n=0, lineterm=""))
    added = sum(
        1 for l in diff if l.startswith("+") and not l.startswith("+++")
    )
    removed = sum(
        1 for l in diff if l.startswith("-") and not l.startswith("---")
    )
    return f"+{added} -{removed} ({len(new)}자 ← {len(old)}자)"


def unified_diff_text(old: str, new: str, context: int = 2) -> str:
    """human-readable unified diff. review CLI `diff` 서브커맨드용."""
    old_lines = (old or "").splitlines(keepends=False)
    new_lines = (new or "").splitlines(keepends=False)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="existing",
        tofile="compiled",
        n=context,
        lineterm="",
    )
    return "\n".join(diff)


def compile_candidates(
    candidates: list[dict], memory_dirs: list[Path] | None = None
) -> list[dict]:
    """candidate 각각에 대해 기존 memory 매칭 → Luna 정제 → update 메타 부착.

    Luna 호출 실패하면 해당 candidate 는 원본 그대로 통과.
    """
    if not candidates:
        return []
    if memory_dirs is None:
        memory_dirs = DEFAULT_MEMORY_DIRS + _extra_memory_dirs()
    out: list[dict] = []
    for c in candidates:
        try:
            existing = _find_existing_memory(c, memory_dirs)
        except Exception as e:
            _debug(f"find existing fail: {e}")
            existing = None
        if existing is None:
            out.append(c)
            continue
        compiled = _compile_update(existing["body"], c)
        if not compiled:
            _debug(f"compile fail (kept new) title={c.get('title')!r}")
            out.append(c)
            continue
        merged = dict(c)
        merged["body"] = compiled
        merged["update_of"] = str(existing["path"])
        merged["diff_summary"] = diff_summary(existing["body"], compiled)
        out.append(merged)
        _debug(
            f"compiled title={c.get('title')!r} update_of={existing['path']}"
        )
    return out


def auto_compile_enabled() -> bool:
    """env var 기반 opt-in 체크. session_memory_end 가 사용."""
    return os.environ.get(ENABLE_AUTO_COMPILE_ENV, "").strip() == "1"


def main() -> int:
    """stdin JSON candidates 받아 compile_candidates 결과 stdout 출력 — CLI/디버그용."""
    try:
        raw = sys.stdin.read()
        candidates = json.loads(raw) if raw.strip() else []
        if not isinstance(candidates, list):
            print("[]")
            return 0
        out = compile_candidates(candidates)
        json.dump(out, sys.stdout, ensure_ascii=False)
        return 0
    except Exception as e:
        _debug(f"main FATAL: {e}\n{traceback.format_exc()}")
        print("[]")
        return 0


if __name__ == "__main__":
    sys.exit(main())
