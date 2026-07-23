#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — memory/*.md → BLOB-기반 vec + FTS5 이중 인덱서.

설계 결정:
- vec 저장은 sqlite-vec 대신 일반 BLOB 컬럼 + numpy float32 (macOS 시스템
  Python sqlite3의 enable_load_extension 미지원 회피). 메모리 ~100개 규모라
  성능 차이 무시 가능.
- 이중 임베딩: body 전체 + frontmatter description 각각. description은 정수만
  박혀있어 매칭 정밀도가 높아 검색 시 1.5x 가중.
- 변경 감지: mtime_ns 비교. 같으면 skip, 다르면 재임베딩 + DB upsert.
- 동시성: flock(LOCK_NB)로 동시 indexer 실행 차단.
- path traversal: symlink resolve 후 allowed_roots 하위 확인.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import yaml

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
DB_PATH = DATA_DIR / "index.db"
DEBUG_LOG = DATA_DIR / "debug.log"
LOCK_PATH = DATA_DIR / "memory-indexer.lock"
EMBED_URL = "http://localhost:8081/embed"
EMBED_TIMEOUT = 5  # seconds — 인덱싱 시점은 hook과 별개라 여유
EMBED_DIM = 1024

# ── Contextual Retrieval (CR, gbrain `contextual-retrieval-service.ts` 차용) ──
# body 를 임베딩하기 전에 맥락 한 줄(name + synopsis)을 선붙여 별 벡터(embedding_ctx)로
# 저장. 짧은/coined-name 메모리가 문서 맥락을 벡터에 담는다. 원본 embedding 은 불변
# (off 모드 회귀 0). 비용은 전부 index-time — 회수 query-time 0ms 증가.
CR_MODES = ("off", "title", "synopsis")
CR_MODE = os.environ.get("MV3_CR_MODE", "off")  # index-time 기본 off
SYNOPSIS_PROMPT_VERSION = 1
WRAPPER_VERSION = 1
CR_EMBED_MODEL_TAG = "arctic-ko-v2"  # corpus_generation 해시 입력(임베딩 모델 식별)
CTX_SYNOPSIS_CAP = 300  # synopsis/description 맥락 줄 cap(자)
# synopsis tier 전용 로컬 Gemma(zero-cost — 클라우드 금지). index-time 이라 관대한 timeout.
# enable_thinking=False 로 reasoning 생략(3~7배 빠름, 품질 동일 — CLAUDE.md gemma 규약).
GEMMA_SYNOPSIS_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_SYNOPSIS_MODEL = "mlx-community/gemma-4-12B-it-4bit"
GEMMA_SYNOPSIS_TIMEOUT = 8.0
GEMMA_SYNOPSIS_MAX_TOKENS = 120
# Sprint 9: BGE-M3 → Arctic-Embed-L v2.0 KO 교체. CLS pooling + L2 normalized.
# 서버가 "kind" 필드를 사용 (query → "query: " prefix 자동 부착).
# Claude Code 가 cwd 마다 별도 projects 슬롯을 만들기 때문에
# (예: cwd=`/Users/<user>` → `~/.claude/projects/-Users-<user>/`,
# cwd=`/Users/<user>/foo` → `~/.claude/projects/-Users-<user>-foo/`) 런타임 glob 으로
# 모든 슬롯의 memory 디렉토리를 흡수한다. 사용자가 직접 슬러그를 입력할 필요 없음.
def _discover_memory_dirs() -> list[Path]:
    # v3.2.7: env var 우선 — module-level DATA_DIR 패턴과 일관성.
    import os as _os
    root = Path(_os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/memory") if p.is_dir())


DEFAULT_MEMORY_DIRS = _discover_memory_dirs()
# Sprint 11: env var `MV3_EXTRA_MEMORY_DIRS=path1:path2` 로 추가 indexing 디렉토리.
# 예: handoff/ 폴더에 sprint 별 brief/build-log 두는 환경에서 그 콘텐츠를 회수
# 가능하게. hook의 _spawn_reindex가 부모 env 보존하므로 shell rc에 export 1회면
# indexer + hook 양쪽에 자동 적용.
ENV_EXTRA_DIRS = "MV3_EXTRA_MEMORY_DIRS"


SOURCES_CONFIG = DATA_DIR / "sources.json"
# Sprint 16: env var(MV3_EXTRA_MEMORY_DIRS) + config file(sources.json) union.
# env 는 shell session 한정, config 는 영구. sources_cli.py 로 add/remove/list.


def _config_memory_dirs() -> list[Path]:
    """sources.json 의 sources 항목 → Path 리스트. 실패 시 빈 리스트."""
    if not SOURCES_CONFIG.is_file():
        return []
    try:
        import json as _json
        data = _json.loads(SOURCES_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    srcs = data.get("sources")
    if not isinstance(srcs, list):
        return []
    out: list[Path] = []
    for s in srcs:
        if not isinstance(s, str) or not s:
            continue
        out.append(Path(s).expanduser())
    return out


def _extra_memory_dirs() -> list[Path]:
    """env var + config file dirs union (env 우선·dedup)."""
    out: list[Path] = []
    seen: set[str] = set()
    raw = os.environ.get(ENV_EXTRA_DIRS, "").strip()
    if raw:
        for piece in raw.split(":"):
            piece = piece.strip()
            if not piece:
                continue
            p = Path(piece).expanduser()
            key = str(p)
            if key not in seen:
                seen.add(key)
                out.append(p)
    for p in _config_memory_dirs():
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out

# 선행 BOM(﻿) 허용 (audit R3): BOM 메모리에서 reverify._FM_RE 는 stale 을 읽는데
# 이 canonical parser 가 비관용이면 recall 의 '재검증 필요' 경고·provenance 가 통째
# 누락돼 list↔recall trust state 가 어긋난다. ﻿? 로 정렬.
FRONTMATTER_RE = re.compile(r"^﻿?---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# 같은 디렉토리의 indexer.py(Sprint 1~3)에서 secret 마스킹 + open_db 재사용
sys.path.insert(0, str(Path(__file__).parent))
from indexer import redact, open_db  # noqa: E402


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] mem-indexer: {msg}\n")
    except Exception:
        pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """마크다운에서 YAML frontmatter dict + 본문 분리. 실패 시 ({}, text)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            return {}, text
    except yaml.YAMLError:
        return {}, text
    return fm, text[m.end():]


def _embed_cache_get(query: str, kind: str) -> list[float] | None:
    """sqlite embed_cache 조회. cache key는 (kind, text) 묶음 — query/passage 분리.

    Sprint 10: 메인 indexer 트랜잭션이 짧아졌으므로 timeout 기본값(5s) 복구.
    이전엔 timeout=0.1로 self-deadlock 회피했으나 짧은 트랜잭션에선 BUSY 대기로 충분.
    """
    h = hashlib.sha256(f"{kind}\x00{query}".encode("utf-8")).hexdigest()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute(
                "SELECT vector FROM embed_cache WHERE query_hash=?", (h,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    # bug-audit 2026-06-02 (codex R2): 캐시 측 검증. (1) 비-4배수 손상 blob 은
    # frombuffer ValueError 가 embed_text 의 try 블록 밖(cache lookup)에서 터져
    # 호출자(indexer/recall) 크래시. (2) pre-existing NaN/Inf 캐시 행은 embed_text
    # finiteness 가드(#1)가 cache 이후라 그대로 재서빙돼 memories_vec 로 전파.
    # 손상·비유한·차원불일치는 None(cache miss) → 서버 재요청(가드된 경로)로 흘린다.
    try:
        arr = np.frombuffer(row[0], dtype=np.float32)
    except (ValueError, TypeError):
        # ValueError: 비-4배수 길이. TypeError: SQLite 는 컬럼 타입 strict 가 아니라
        # vector 컬럼에 TEXT/str 값이 들어갈 수 있는데 frombuffer 는 bytes-like 만
        # 받는다(non-buffer → TypeError). 둘 다 cache miss 로 흘려 서버 재요청.
        return None
    if arr.shape != (EMBED_DIM,) or not np.isfinite(arr).all():
        return None
    return arr.tolist()


def _embed_cache_put(query: str, kind: str, vector: list[float]) -> None:
    """sqlite embed_cache 저장. cache key는 (kind, text) 묶음.

    Sprint 10: timeout 기본값(5s) 복구 (위 _embed_cache_get 주석 참고).
    """
    h = hashlib.sha256(f"{kind}\x00{query}".encode("utf-8")).hexdigest()
    try:
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embed_cache(query_hash, vector, created_at) VALUES(?,?,?)",
                (h, blob, time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        _debug(f"embed cache put fail: {e}")


def embed_text(text: str, kind: str = "passage") -> list[float] | None:
    """임베딩 서버 호출 → 1024차원 dense 벡터.

    kind="passage" (기본): 문서/메모리 본문 임베딩.
    kind="query": 검색 쿼리 임베딩. Arctic-Embed-L v2.0 KO 학습 설정상 서버가
    "query: " prefix를 자동 부착한다.

    Sprint 8 sqlite embed_cache 유지 — cache key는 (kind, text) 묶음이라 같은
    텍스트라도 query/passage 분리 저장. 모델 교체 시 embed_cache truncate 필요.
    """
    text = (text or "").strip()
    if not text:
        return None
    if kind not in ("query", "passage"):
        raise ValueError(f"kind must be 'query' or 'passage', got {kind!r}")
    cached = _embed_cache_get(text, kind)
    if cached is not None:
        return cached
    body = json.dumps({"input": text, "kind": kind}).encode("utf-8")
    req = urllib.request.Request(
        EMBED_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
        vec = data.get("vector")
        if not isinstance(vec, list) or len(vec) != EMBED_DIM:
            _debug(
                f"embed bad shape: type={type(vec).__name__} "
                f"len={len(vec) if isinstance(vec, list) else '?'}"
            )
            return None
        # bug-audit 2026-06-02 (#1): NaN/Inf 벡터 거부. 임베딩 서버가 토큰
        # 한도 초과 입력에 대해 크래시 대신 all-NaN 벡터를 200 으로 반환할 수
        # 있는데(arctic_ko_server token-truncation 부재), NaN 은 json.dumps/loads
        # 를 그대로 왕복하고 len==EMBED_DIM 도 통과해 embed_cache·memories_vec 에
        # 영구 저장된다 → cosine 검색에서 sims=NaN 으로 해당 메모리 영구 미회수 +
        # top-k 순위 오염. None 반환 시 embed_failed defer 경로(아래)가 재시도하게
        # 둔다. (서버측 token truncation·500 가드는 arctic_ko_server.py 에서 별도 차단.)
        if not np.isfinite(np.asarray(vec, dtype=np.float64)).all():
            _debug(f"embed non-finite vector rejected (kind={kind}, len={len(vec)})")
            return None
        _embed_cache_put(text, kind, vec)
        return vec
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        _debug(f"embed fail: {type(e).__name__}: {e}")
        return None


def _safe_memory_path(path: Path, allowed_roots: list[Path]) -> bool:
    """path가 allowed_roots 중 하나의 하위인지 (symlink resolve 포함).

    audit-2026-05-24: strict=True 로 변경 — dangling symlink (등록 후 target
    삭제) 가 traversal check 를 우회하던 잠재 회귀 차단. allowed_roots 의
    `resolve` 는 (sources.json 등록 시 is_dir 확인됐다는 신뢰 하에) 관용적으로
    strict=False 유지.
    """
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except (ValueError, OSError):
            continue
    return False


PROCEDURAL_SUBDIR = "_procedural"
# Sprint 13: 절차적 메모리(명령어 syntax·workflow·환경 설정)는 결정 메모리와
# 분리해 `_procedural/` 하위 디렉토리에 저장. 디렉토리 단위 분리로 grep·인벤토리·
# 백업이 단순해진다. 회수 시점에는 동일 게이트로 검색 — type 분리는 저장 슬롯의
# 의미만 가지고, 검색 우선순위는 raw cosine 그대로 따른다.


def _collect_md_files(dirs: list[Path]) -> list[Path]:
    """memory/ 디렉토리에서 .md 수집. _staged/, MEMORY.md(index 파일), symlink outside 제외.

    Sprint 13: root 직속 + `_procedural/` 하위까지 수집. `_procedural/_staged/` 는
    `_staged` 부분 일치로 제외.
    """
    out: list[Path] = []
    # bug-audit 2026-06-01 (collect-md-dup-path-dataloss): dirs 에 동일 dir 가 중복
    # (sources.json 에 DEFAULT 자동발견 슬롯을 add 하면 _extra_memory_dirs 가 DEFAULT
    # 와의 교집합을 제거하지 않아 발생)되면 같은 .md 가 두 번 방출된다. 그러면
    # dedup_cli._scan 이 단일 파일을 자기 자신과 'name-dup'으로 보고하고 cmd_merge 가
    # canonical 을 자기삭제(영구 데이터 유실, ok:True). resolved path 로 단일 방출 보장.
    seen: set = set()
    for d in dirs:
        if not d.is_dir():
            continue
        candidates: list[Path] = list(d.glob("*.md"))
        proc_dir = d / PROCEDURAL_SUBDIR
        if proc_dir.is_dir():
            candidates.extend(proc_dir.glob("*.md"))
        for p in candidates:
            if any(part == "_staged" for part in p.parts):
                continue
            if p.name == "MEMORY.md":
                # MEMORY.md는 다른 메모리들의 인덱스(목차)일 뿐. 본문이 한국어
                # 일반 키워드로 가득해 무관 쿼리에 fts hit으로 끼는 노이즈 원인.
                continue
            if not _safe_memory_path(p, dirs):
                _debug(f"unsafe path skip: {p}")
                continue
            try:
                rp = p.resolve()
            except OSError:
                rp = p
            if rp in seen:
                continue
            seen.add(rp)
            out.append(p)
    return out


def _vec_to_blob(vec: list[float]) -> bytes:
    """list[float] → float32 little-endian bytes (numpy)."""
    return np.asarray(vec, dtype=np.float32).tobytes()


# ── Contextual Retrieval 헬퍼 (gbrain `embedding-context.ts`·`page-summary.ts` 이식) ──
def active_cr_mode() -> str:
    """런타임 CR 모드 — env 우선(setenv 호환), 미설정 시 import-time CR_MODE 기본.
    유효하지 않은 값은 off 로 폴백."""
    m = os.environ.get("MV3_CR_MODE", CR_MODE)
    return m if m in CR_MODES else "off"


_CTX_TAG_RE = re.compile(r"</?\s*context\s*>", re.IGNORECASE)


def _sanitize_ctx(s: str, cap: int = CTX_SYNOPSIS_CAP) -> str:
    """맥락 줄 정제 — context 태그 strip(대소문자·공백 변형 포함, 주입 방지),
    공백 축약, cap(code-point 슬라이스라 멀티바이트 안전)."""
    if not s:
        return ""
    s = _CTX_TAG_RE.sub(" ", s)
    s = " ".join(s.split())
    return s[:cap]


def build_contextual_prefix(name: str, synopsis: str | None) -> str | None:
    """`<context>{name}\\n{synopsis}</context>\\n` 접두 생성. synopsis 없으면
    name-only(title). name·synopsis 둘 다 비면 None."""
    name_s = _sanitize_ctx(name or "", cap=120)
    syn_s = _sanitize_ctx(synopsis or "", cap=CTX_SYNOPSIS_CAP)
    if not name_s and not syn_s:
        return None
    if syn_s:
        return f"<context>{name_s}\n{syn_s}</context>\n"
    return f"<context>{name_s}</context>\n"


def wrap_body_for_embedding(body: str, prefix: str | None) -> str:
    """contextual 임베딩용 wrapped 문자열. prefix None 이면 body 그대로(원본 불변)."""
    return f"{prefix}{body}" if prefix else body


def compute_corpus_generation(mode: str) -> str:
    """16-char 해시 — mode/prompt_ver/gemma_model/wrapper_ver/embed_model 입력 변경
    감지(stale → 재임베딩 트리거). mtime 만으로 못 잡는 '모드/프롬프트 변경' 재임베딩."""
    raw = f"{mode}|{SYNOPSIS_PROMPT_VERSION}|{GEMMA_SYNOPSIS_MODEL}|{WRAPPER_VERSION}|{CR_EMBED_MODEL_TAG}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def generate_synopsis_gemma(
    name: str, description: str, body: str
) -> tuple[str | None, str]:
    """로컬 Gemma 로 1줄 한국어 synopsis 생성(검색 맥락용). enable_thinking=False.
    성공 → (synopsis, "ok"); 거부/빈/타임아웃/다운 → (None, reason)."""
    snippet = (body or "").strip()[:1200]
    prompt = (
        "다음 메모리를 검색 인덱싱용으로 한국어 한 문장(15~30단어)으로 요약하라. "
        "이 메모리가 '무엇에 관한 것인지' 핵심 주제·대상·맥락만 담고, 따옴표·머리말·군더더기 "
        "없이 문장만 출력하라.\n\n"
        f"<name>{name}</name>\n<description>{description}</description>\n<body>{snippet}</body>"
    )
    payload = json.dumps({
        "model": GEMMA_SYNOPSIS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": GEMMA_SYNOPSIS_MAX_TOKENS,
        "temperature": 0.2,
        "enable_thinking": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        GEMMA_SYNOPSIS_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_SYNOPSIS_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError, OSError, TimeoutError) as e:
        _debug(f"cr synopsis gemma fail: {type(e).__name__}: {e}")
        return None, "gemma_unavailable"
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None, "no_choices"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None, "no_message"
    raw = message.get("content")
    if raw is not None and not isinstance(raw, str):
        return None, "nonstr_content"
    syn = _sanitize_ctx(raw or "", cap=CTX_SYNOPSIS_CAP)
    if not syn:
        return None, "empty"
    return syn, "ok"


def compute_contextual_embedding(
    name: str, description: str, body: str, mode: str
) -> tuple[bytes | None, str | None, str]:
    """주어진 모드로 contextual 임베딩 생성. body 가 있는 메모리 전용.

    반환: (embedding_ctx_blob | None, cr_synopsis | None, effective_mode).
    - title : description 을 맥락으로(LLM 0). description 빈약하면 name-only.
    - synopsis : 로컬 Gemma 1줄 생성 → 실패 시 title 로 강등(R2).
    - ctx 임베딩 자체 실패(서버 다운 등) → off 로 완전 폴백(인덱싱 무중단, raw 사용).
    """
    if mode == "off" or not body.strip():
        return None, None, "off"
    synopsis = None
    effective = mode
    if mode == "synopsis":
        synopsis, _reason = generate_synopsis_gemma(name, description, body)
        effective = "synopsis" if synopsis else "title"  # 강등
    ctx_line = synopsis or (description if description.strip() else None)
    prefix = build_contextual_prefix(name, ctx_line)
    if prefix is None:
        return None, None, "off"
    wrapped = wrap_body_for_embedding(body, prefix)
    vec_ctx = embed_text(wrapped)
    if vec_ctx is None:
        return None, None, "off"  # ctx 임베딩 실패 → raw 폴백
    return _vec_to_blob(vec_ctx), ctx_line, effective


def _parse_memory_file(path: Path) -> tuple[dict, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        _debug(f"read fail {path}: {e}")
        return None
    fm, body = parse_frontmatter(text)
    return fm, redact(body)


def _lock_path_for(db_path: Path | None) -> Path:
    """db_path 와 같은 디렉토리에 lock 위치 — production 은 DATA_DIR/index.db
    페어로 DATA_DIR/memory-indexer.lock 유지, 테스트는 tmp_db 페어로 tmp 격리."""
    if db_path is None or Path(db_path).resolve() == DB_PATH.resolve():
        return LOCK_PATH
    return Path(db_path).parent / "memory-indexer.lock"


def _acquire_lock(db_path: Path | None = None):
    """flock(LOCK_NB) — 동시 실행 차단. lock 못 잡으면 None.
    post-ship: db_path 인자 추가 — 테스트가 tmp_db 사용 시 lock 도 tmp 로
    분리되어 production ~/.claude/mindvault-v3/ 오염 차단.
    """
    lock_path = _lock_path_for(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (BlockingIOError, OSError):
        # bug-audit 2026-06-01 (lock-fd-leak): flock 는 BlockingIOError 외 OSError
        # (EINTR/ENOLCK, advisory-lock 미지원 FS 의 EINVAL/EOPNOTSUPP)도 던진다.
        # 그 경우에도 fh 를 닫아 fd 누수를 막는다(indexer._acquire_session_lock 과 대칭).
        fh.close()
        return None


def _release_lock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# Sprint 10: 매 .md 처리 후 conn.commit() — long-running write transaction을
# 짧은 트랜잭션 묶음으로 쪼개 hook(memory-recall.py 등) 동시 실행 시 lock 충돌 회피.
# 추가로 embed_text(sub-conn embed_cache write 호출)는 메인 conn write 전에 수행하되,
# 매 iter 끝에 commit해 다음 iter 시작 시 메인 idle → sub-conn cache_put BUSY 회피.
# WAL 모드라 commit 부하 무시 (~100 memory 규모 풀 리빌드 단위 시간 미만).


def incremental_index(
    memory_dirs: list[Path] | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    """변경된 .md만 재임베딩. 반환: {"updated", "skipped", "removed"}.

    lock 못 잡으면 즉시 0으로 반환 (다른 indexer가 작업 중).
    """
    if memory_dirs is None:
        memory_dirs = DEFAULT_MEMORY_DIRS + _extra_memory_dirs()
    if db_path is None:
        db_path = DB_PATH

    counts = {"updated": 0, "skipped": 0, "removed": 0}
    lock = _acquire_lock(db_path)
    if lock is None:
        _debug("lock busy — skip")
        return counts

    try:
        conn = open_db(db_path)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        cr_mode_active = active_cr_mode()  # off 기본 — 회귀 0
        try:
            existing = {
                r["path"]: r["mtime_ns"]
                for r in conn.execute("SELECT path, mtime_ns FROM memories")
            }
            present_files = _collect_md_files(memory_dirs)
            present_paths = {str(p) for p in present_files}

            # 1) 삭제된 파일 정리 — 각 stale 처리 후 즉시 commit해 hook write 대기 최소화.
            for stale_path in existing.keys() - present_paths:
                conn.execute("DELETE FROM memories WHERE path=?", (stale_path,))
                conn.execute(
                    "DELETE FROM memories_fts WHERE path=?", (stale_path,)
                )
                conn.execute(
                    "DELETE FROM memories_vec WHERE path=?", (stale_path,)
                )
                counts["removed"] += 1
                conn.commit()

            # 2) 신규/변경 파일 처리
            for p in present_files:
                try:
                    st = p.stat()
                except OSError:
                    continue
                sp = str(p)
                if existing.get(sp) == st.st_mtime_ns:
                    counts["skipped"] += 1
                    continue

                parsed = _parse_memory_file(p)
                if parsed is None:
                    continue
                fm, body = parsed
                # bug-audit 2026-06-01 (indexer-nonstr-frontmatter-crash): description/name
                # 이 비-문자열(YAML int/float/list/dict)이면 아래 .strip() 가 AttributeError
                # 로 per-file 가드 없는 루프를 뚫고 인덱서 run 전체를 중단시킨다. str 강제.
                name = str(fm.get("name") or p.stem)
                description = str(fm.get("description") or "")

                # Sprint 10: 임베딩(sub-conn embed_cache write 포함)을 메인 conn write 전에 수행.
                # 이전 iter의 commit 직후라 메인 conn이 idle → sub-conn cache_put BUSY 회피.
                vec_body = embed_text(body) if body.strip() else None
                vec_desc = embed_text(description) if description.strip() else None

                # bug-audit 2026-05-29 (embeddings-alias-1 / embeddings-alias-6):
                # 임베딩 서버 일시 장애로 embed_text 가 None 을 반환했는데도 아래에서
                # mtime_ns 를 갱신해버리면, 다음 incremental_index 가 mtime 일치로 이
                # 메모리를 skip 해 vec 가 영구히 비게 되고 semantic recall 에서 영구
                # 누락된다. 본문/description 이 실제로 있는데 vec 가 비었으면(=embed
                # 실패) 이번 건을 통째로 건너뛰어 mtime/FTS/vec 어느 것도 건드리지
                # 않는다 → 파일 mtime 이 stored mtime 과 계속 달라 다음 run 이 재시도.
                # (본문이 원래 비어 vec 가 None 인 정상 케이스는 embed 실패가 아니므로 제외.)
                # 신규 메모리는 outage 동안 FTS 에도 안 들어가는 트레이드오프가 있으나,
                # 서버 복구 후 다음 run 에서 정상 인덱싱되며 영구 누락은 막는다.
                embed_failed = (
                    (bool(body.strip()) and vec_body is None)
                    or (bool(description.strip()) and vec_desc is None)
                )
                if embed_failed:
                    _debug(f"embed unavailable, defer reindex: {p.name}")
                    continue

                # Contextual Retrieval — body 임베딩에 맥락(name+synopsis) 선붙여 별
                # 벡터(embedding_ctx) 생성. off 모드는 (None,None,"off") → 원본 불변.
                # ctx 임베딩 실패는 off 폴백(파일 skip 아님 — raw 로 정상 인덱싱).
                embedding_ctx, cr_synopsis, effective_mode = compute_contextual_embedding(
                    name, description, body, cr_mode_active
                )
                # corpus_generation 은 *실제 달성 tier*(effective_mode) 기준 — 설정모드가
                # 아니라 achieved tier 로 마킹해야 강등이 가짜 converged 되지 않는다.
                # adversarial review 2026-06-17 (R12): 설정 synopsis 인데 Gemma 일시중단으로
                # title 강등(effective="title", Arctic 정상이라 ctx 는 생성됨)되면, 설정모드
                # 기준 gen("synopsis") 마킹 시 Gemma 복구 후에도 백필이 영영 제외(영구 title
                # 고정). effective 기준이면 gen("title")≠gen("synopsis") 라 백필 후보로 남아
                # 재시도된다. effective="off"(임베딩 실패/빈body)→gen("off")로 R5 가드도 subsume.
                # 단 *빈 body*(effective="off" & body 없음 — ctx 구조적 불가)는 설정모드
                # 기준 수렴 마킹(cr_backfill FIX 와 정합) — R13: gen("off")≠gen(설정) 이라
                # 빈-body 가 매 백필 재선정되는 무한 no-op 방지. 비-빈 effective="off"(임베딩
                # 실패)는 gen("off")로 후보 유지(R5).
                corpus_generation = (
                    compute_corpus_generation(cr_mode_active)
                    if (effective_mode == "off" and not body.strip())
                    else compute_corpus_generation(effective_mode)
                )

                # off 모드는 기존 ctx *벡터* 만 보존(파괴 금지), 단 cr_mode/
                # corpus_generation 은 off 기본 유지. adversarial review 2026-06-17:
                #  R1 — off-mode 재인덱싱이 백필 embedding_ctx 를 NULL 로 덮어써 파괴 →
                #       embedding_ctx/cr_synopsis carry-forward 로 파괴 방지.
                #  R2 — corpus_generation 까지 보존하면(gen=title) 다음 백필이 stale ctx
                #       를 skip(gen 일치) → 영구 미갱신. 따라서 generation 은 off 기본
                #       (gen("off") ≠ gen("title")) 으로 둬 다음 백필이 재처리·refresh.
                # 즉 body 변경 후 ctx 는 잠시 stale 하나(off 회수는 raw 사용이라 무해),
                # 백필 후보로 남아 다음 백필이 새 body 로 갱신한다(영구 stale 아님).
                if cr_mode_active == "off":
                    prev_v = conn.execute(
                        "SELECT embedding_ctx, cr_synopsis FROM memories_vec "
                        "WHERE path=? AND kind='body'", (sp,)
                    ).fetchone()
                    # body 가 바뀌었으면 기존 ctx 는 stale → carry-forward 안 함(codex
                    # 2-track R11: use_ctx 검색이 stale ctx 벡터로 랭킹하며 새 raw row 를
                    # 반환하는 오염 차단). body 동일(메타데이터/touch 재인덱싱)일 때만 보존
                    # (R1: 백필 결과 파괴 금지). stale 시 embedding_ctx 는 None(off compute)
                    # 유지 + corpus_generation=gen("off") → 다음 백필이 새 body 로 refresh.
                    old_fts = conn.execute(
                        "SELECT body FROM memories_fts WHERE path=?", (sp,)
                    ).fetchone()
                    body_unchanged = old_fts is not None and old_fts["body"] == body
                    if body_unchanged and prev_v is not None and prev_v["embedding_ctx"] is not None:
                        embedding_ctx = prev_v["embedding_ctx"]
                        cr_synopsis = prev_v["cr_synopsis"]

                conn.execute(
                    "DELETE FROM memories_fts WHERE path=?", (sp,)
                )
                conn.execute(
                    "DELETE FROM memories_vec WHERE path=?", (sp,)
                )
                conn.execute(
                    """
                    INSERT INTO memories(path, name, description, mtime_ns, indexed_at,
                                         cr_mode, corpus_generation)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        name=excluded.name,
                        description=excluded.description,
                        mtime_ns=excluded.mtime_ns,
                        indexed_at=excluded.indexed_at,
                        cr_mode=excluded.cr_mode,
                        corpus_generation=excluded.corpus_generation
                    """,
                    (sp, name, description, st.st_mtime_ns, now, effective_mode, corpus_generation),
                )
                conn.execute(
                    "INSERT INTO memories_fts(path, body) VALUES(?,?)",
                    (sp, body),
                )
                if vec_body is not None:
                    conn.execute(
                        "INSERT INTO memories_vec(path, kind, embedding, embedding_ctx, cr_synopsis) "
                        "VALUES(?,?,?,?,?)",
                        (sp, "body", _vec_to_blob(vec_body), embedding_ctx, cr_synopsis),
                    )
                if vec_desc is not None:
                    conn.execute(
                        "INSERT INTO memories_vec(path, kind, embedding) "
                        "VALUES(?,?,?)",
                        (sp, "description", _vec_to_blob(vec_desc)),
                    )
                counts["updated"] += 1
                conn.commit()
        finally:
            conn.close()
    finally:
        _release_lock(lock)

    _debug(f"incremental: {counts}")
    return counts


def full_rebuild(
    memory_dirs: list[Path] | None = None,
    db_path: Path | None = None,
) -> int:
    """memories_* 데이터 비우고 재인덱싱 (sessions_* 보존)."""
    if db_path is None:
        db_path = DB_PATH
    conn = open_db(db_path)
    try:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM memories_fts")
        conn.execute("DELETE FROM memories_vec")
        conn.commit()
    finally:
        conn.close()
    return incremental_index(memory_dirs, db_path)["updated"]


def main() -> int:
    t0 = time.time()
    try:
        c = incremental_index()
        _debug(f"main: {c} in {time.time()-t0:.2f}s")
    except Exception as e:
        _debug(f"FATAL: {e}\n{traceback.format_exc()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
