#!/usr/bin/env python3
"""MindVault v3 Sprint 2 → Sprint 5 — sessions hybrid 검색.

Sprint 5 변경:
- FTS5 단독 → FTS5 + Arctic-ko vec hybrid (RRF 결합).
- raw cosine 절대 게이트 (DEFAULT_RAW_COSINE_MIN) 도입 → V1 토큰 낭비 패턴 회피.
- Gemma rerank/summarize는 게이트 통과 candidates에만 적용 (Gemma 호출량 절감).
- 회수 단서어 감지 시 게이트 완화 (RAW_COSINE_MIN_RELAXED).

memory_search.py와 같은 게이트 철학:
  vec-only hit + raw < min → 차단. fts hit은 면제 (BM25 정확 키워드 보장).
"""
from __future__ import annotations

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

sys.path.insert(0, str(Path(__file__).parent))

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
DB_PATH = DATA_DIR / "index.db"
DEBUG_LOG = DATA_DIR / "debug.log"
GEMMA_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_MODEL = "mlx-community/gemma-4-12B-it-4bit"
GEMMA_TIMEOUT = 45

RRF_K = 60
EMBED_DIM = 1024
FTS_LIMIT = 10
VEC_LIMIT = 10
# Sprint 5 게이트 — memory_search.py와 같은 철학.
# sessions 본문은 잡담·메타 노이즈가 많아 memory보다 살짝 보수적으로 시작.
DEFAULT_RAW_COSINE_MIN = 0.32  # Sprint 9 Arctic-ko 분포에 맞춰 비례 조정 (구 0.62)
RAW_COSINE_MIN_RELAXED = 0.30  # 회수 단서어 시 완화 (구 0.58)
RECALL_HINT_PATTERN = re.compile(
    r"(예전에|그때|이전에|지난번|어제|전에|옛날에|저번에|지난 ?세션)"
)


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] search: {msg}\n")
    except Exception:
        pass


_FTS_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9_]+")


# bug-audit 2026-06-01 (fts5-reserved-keyword-leak): memory_search._fts_escape 와
# parity — AND/OR/NOT/NEAR FTS5 연산자 bareword 누수 차단 (운영 15건+, 5/28~).
_FTS5_RESERVED = frozenset({"AND", "OR", "NOT", "NEAR"})


def fts_escape(query: str) -> str:
    """사용자 쿼리를 FTS5 MATCH 안전 문자열로 변환.

    NEXT-30.2 (2026-05-24) 동기화 — `memory_search._fts_escape` 와 동일 정책:
    한글/영문/숫자 화이트리스트, 단독 숫자 제외, 2자 이상, prefix wildcard, OR 결합.
    이전 `[^\\s\"'`*:()]+` AND 결합은 `.~?/-` 와 숫자를 흘려보내 sessions
    검색 경로에서 silent FTS5 0건 회귀. raw_cosine + RRF 게이트가 false
    positive 차단.
    """
    words = _FTS_TOKEN_RE.findall(query)
    pat = [
        (f'"{w}"*' if w.upper() in _FTS5_RESERVED else f"{w}*")
        for w in words
        if len(w) >= 2 and not w.isdigit()
    ]
    if not pat:
        return '""'
    return " OR ".join(pat)


def call_gemma(prompt: str, max_tokens: int = 1500) -> str | None:
    body = json.dumps(
        {
            "model": GEMMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            # mem-fan-fix 2026-06-19: thinking ON 시 reasoning 토큰 생성으로 KV/activation
            # peak 폭증(gemma 서버 peak 22GB 관측). 단순 변환이라 off 가 품질 동일·3~7배 빠름.
            # 12B 는 top-level 키만 먹힘(chat_template_kwargs 무효 — CLAUDE.md gemma 규약).
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
        # bug-audit 2026-06-01 (gemma-nonstr-content sibling): content-block 리스트 등
        # 비-문자열 content 에 .strip() → AttributeError 가 recall() 의 broad except 에
        # 'recall FATAL' 로 잡혀 sessions 회수가 통째로 0건이 된다(rerank/summary graceful
        # degrade 무력화). str 만 통과 — contradiction_detector/memory_extractor 등과 parity.
        raw = (choices[0].get("message") or {}).get("content")
        content = (raw if isinstance(raw, str) else "").strip()
        return content or None
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as e:
        # audit-2026-05-24: BaseException(KeyboardInterrupt/_Timeout) 은
        # 의도적으로 전파.
        _debug(f"gemma fail: {type(e).__name__} {e}")
        return None


def _embed_query(query: str) -> list[float] | None:
    """memory_indexer.embed_text 재사용. Sprint 9: Arctic-ko query prefix 적용."""
    from memory_indexer import embed_text  # noqa: WPS433
    return embed_text(query, kind="query")


def fts_candidates(
    conn: sqlite3.Connection, query: str, limit: int = FTS_LIMIT
) -> list[dict]:
    """FTS5 BM25 top-K. snippet + bm25 점수 포함."""
    fts_q = fts_escape(query)
    try:
        rows = conn.execute(
            """
            SELECT s.session_id, s.first_ts, s.last_ts, s.turn_count,
                   snippet(sessions_fts, 1, '[', ']', '...', 24) AS snip,
                   bm25(sessions_fts) AS score
            FROM sessions_fts JOIN sessions s USING(session_id)
            WHERE sessions_fts MATCH ?
            ORDER BY score LIMIT ?
            """,
            (fts_q, limit),
        ).fetchall()
    except sqlite3.DatabaseError as e:
        _debug(f"fts query fail: {e}")
        return []
    return [dict(r) for r in rows]


def vec_candidates(
    conn: sqlite3.Connection,
    qvec: list[float],
    limit: int = VEC_LIMIT,
) -> tuple[list[dict], dict[str, float]]:
    """sessions_vec 전체 → numpy cosine top-K.

    반환: (candidates, raw_cosine_map). candidate dict는 fts_candidates와 형태
    맞춤 (snip은 빈 문자열로).
    """
    rows = list(conn.execute("SELECT session_id, embedding FROM sessions_vec"))
    if not rows:
        return [], {}
    mat = np.zeros((len(rows), EMBED_DIM), dtype=np.float32)
    sids: list[str] = []
    valid = 0
    for r in rows:
        emb = r["embedding"]
        # NEXT-28 sentinel(빈 bytes) — 메타-only jsonl 무한 재시도 차단용.
        # 의도된 빈 row 이므로 silent skip, log noise 제거.
        if not emb:
            continue
        # bug-audit 2026-06-02 (#4): 4의 배수가 아닌 손상 blob 은 frombuffer 가
        # shape 가드 도달 전에 ValueError 를 던져 recall 전체를 0건으로 만든다.
        # 손상 행만 skip 하고 나머지로 검색 지속 (NEXT-36 row-resilience 의도 일관).
        try:
            arr = np.frombuffer(emb, dtype=np.float32)
        except ValueError:
            _debug(f"skip corrupt vec bytes len={len(emb)} sid={r['session_id']}")
            continue
        if arr.shape != (EMBED_DIM,):
            _debug(f"skip bad vec dim {arr.shape} sid={r['session_id']}")
            continue
        # bug-audit 2026-06-02 (codex R2): 비유한(NaN/Inf) 행은 cosine 순위 오염 +
        # raw 게이트 NaN-비교 우회. 읽기 측에서 skip (embed_text 가드는 신규 저장만 차단).
        if not np.isfinite(arr).all():
            _debug(f"skip non-finite vec sid={r['session_id']}")
            continue
        mat[valid] = arr
        sids.append(r["session_id"])
        valid += 1
    if valid == 0:
        return [], {}
    mat = mat[:valid]
    q = np.asarray(qvec, dtype=np.float32)
    # bug-audit 2026-06-02 (codex R2): 비유한(NaN/Inf) 쿼리 벡터는 sims 를 NaN 으로
    # 만들어 raw-cosine 게이트(raw < threshold)를 NaN-비교로 우회한다. embed_text
    # finiteness 가드(#1)가 1차 차단하지만 호출자 경로 방어(defense-in-depth).
    if not np.isfinite(q).all():
        return [], {}
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return [], {}
    q = q / q_norm
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat_norm = mat / norms
    sims = mat_norm @ q  # (N,) raw cosine [-1..1] (Arctic-ko는 보통 0..1)
    idx_sorted = np.argsort(-sims)[:limit]
    picked_sids = [sids[i] for i in idx_sorted]
    raw_map = {sids[i]: float(sims[i]) for i in idx_sorted}

    if not picked_sids:
        return [], raw_map

    placeholders = ",".join("?" * len(picked_sids))
    meta_rows = conn.execute(
        f"""
        SELECT session_id, first_ts, last_ts, turn_count
        FROM sessions WHERE session_id IN ({placeholders})
        """,
        picked_sids,
    ).fetchall()
    meta_map = {r["session_id"]: dict(r) for r in meta_rows}
    out: list[dict] = []
    for sid in picked_sids:
        meta = meta_map.get(sid, {})
        out.append(
            {
                "session_id": sid,
                "first_ts": meta.get("first_ts") or "",
                "last_ts": meta.get("last_ts") or "",
                "turn_count": meta.get("turn_count") or 0,
                "snip": "",
            }
        )
    return out, raw_map


def rrf_combine(
    fts_list: list[dict],
    vec_list: list[dict],
    k: int = RRF_K,
) -> tuple[list[str], dict[str, dict]]:
    """RRF로 결합. session_id별 ranked list와 source 추적.

    info_map[sid] = {"score","source","cand"}. cand는 fts 우선(snippet 보존).
    """
    info: dict[str, dict] = {}
    for rank, c in enumerate(fts_list, start=1):
        sid = c["session_id"]
        entry = info.setdefault(
            sid,
            {"score": 0.0, "source": [], "cand": c},
        )
        entry["score"] += 1.0 / (k + rank)
        if "fts" not in entry["source"]:
            entry["source"].append("fts")
    for rank, c in enumerate(vec_list, start=1):
        sid = c["session_id"]
        entry = info.setdefault(
            sid,
            {"score": 0.0, "source": [], "cand": c},
        )
        entry["score"] += 1.0 / (k + rank)
        if "vec" not in entry["source"]:
            entry["source"].append("vec")
    ranked = sorted(info.keys(), key=lambda s: info[s]["score"], reverse=True)
    return ranked, info


def gemma_rerank(query: str, candidates: list[dict], k: int = 3) -> list[int]:
    if not candidates:
        return []
    blocks = []
    for i, c in enumerate(candidates):
        sid = (c["session_id"] or "")[:8]
        ts = (c.get("first_ts") or "")[:16]
        snip = c.get("snip") or "(snippet 없음)"
        blocks.append(f"[{i}] session={sid} ts={ts}\nsnippet: {snip}")
    prompt = (
        f"아래는 과거 Claude Code 세션 {len(candidates)}개의 snippet이다. "
        f"사용자 질의 '{query}'에 가장 관련 높은 세션 {k}개의 인덱스를 "
        "순서대로 JSON 배열로만 출력하라.\n"
        "형식 엄수: [0, 3, 7] 같은 정수 배열만. 해설·마크다운 금지.\n\n"
        + "\n\n".join(blocks)
    )
    out = call_gemma(prompt, max_tokens=200)
    fallback = list(range(min(k, len(candidates))))
    if not out:
        return fallback
    m = re.search(r"\[([0-9,\s]+)\]", out)
    if not m:
        return fallback
    try:
        idxs = [int(x.strip()) for x in m.group(1).split(",") if x.strip() != ""]
    except ValueError:
        return fallback
    # bug-audit 2026-06-01 (gemma-rerank-dup-index): LLM 이 [2,2,2] 같은 중복 인덱스를
    # 내면 동일 세션이 중복 회수·중복 Gemma 요약된다. 순서 보존 dedup 후 절단.
    idxs = list(dict.fromkeys(i for i in idxs if 0 <= i < len(candidates)))[:k]
    return idxs or fallback


def fetch_body(conn: sqlite3.Connection, session_id: str) -> str:
    row = conn.execute(
        "SELECT body FROM sessions_fts WHERE session_id=?", (session_id,)
    ).fetchone()
    if not row:
        return ""
    return row["body"] or ""


def gemma_summarize(body: str, query: str, max_chars: int = 400) -> str | None:
    body = body[:6000]
    prompt = (
        f"다음 과거 Claude Code 세션 내용을 사용자 질의 '{query}' 관점에서 "
        f"핵심만 한국어로 {max_chars}자 이내 요약하라. 불릿 3~5개. "
        "세션에 실제 언급된 내용만. 추측 금지.\n\n"
        f"---세션 본문---\n{body}\n---끝---"
    )
    out = call_gemma(prompt, max_tokens=800)
    if not out:
        return None
    return out.strip()[: max_chars * 2]


def recall(query: str, top_k: int = 3) -> list[dict]:
    """Sprint 5 hybrid 검색. FTS + vec → RRF → raw cosine 게이트 → Gemma rerank/요약.

    raw cosine 게이트: vec-only hit + raw < min → 차단. fts hit은 면제.
    회수 단서어(예전에, 그때 등) 감지 시 게이트 완화.
    """
    if not DB_PATH.is_file():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError as e:
        _debug(f"db open fail: {e}")
        return []
    try:
        t0 = time.time()
        fts_list = fts_candidates(conn, query, limit=FTS_LIMIT)

        qvec = _embed_query(query)
        vec_list: list[dict] = []
        raw_cosine_map: dict[str, float] = {}
        if qvec is not None:
            vec_list, raw_cosine_map = vec_candidates(conn, qvec, limit=VEC_LIMIT)

        if not fts_list and not vec_list:
            _debug(f"no candidates query={query!r}")
            return []

        ranked_sids, info = rrf_combine(fts_list, vec_list)

        min_threshold = (
            RAW_COSINE_MIN_RELAXED
            if RECALL_HINT_PATTERN.search(query)
            else DEFAULT_RAW_COSINE_MIN
        )
        kept_sids: list[str] = []
        for sid in ranked_sids:
            entry = info[sid]
            has_fts = "fts" in entry["source"]
            raw = raw_cosine_map.get(sid, 0.0)
            if not has_fts and raw < min_threshold:
                continue
            kept_sids.append(sid)

        top1_raw = max(raw_cosine_map.values()) if raw_cosine_map else 0.0
        _debug(
            f"query={query!r} fts={len(fts_list)} vec={len(vec_list)} "
            f"merged={len(ranked_sids)} kept={len(kept_sids)} "
            f"top1_raw={top1_raw:.3f} threshold={min_threshold:.2f}"
        )

        if not kept_sids:
            return []

        kept_candidates = [info[sid]["cand"] for sid in kept_sids]
        picked = gemma_rerank(query, kept_candidates, k=top_k)

        results = []
        for i in picked:
            c = kept_candidates[i]
            sid = c["session_id"]
            body = fetch_body(conn, sid)
            summary = gemma_summarize(body, query) if body else None
            results.append(
                {
                    "session_id": sid,
                    "first_ts": c.get("first_ts") or "",
                    "last_ts": c.get("last_ts") or "",
                    "turn_count": c.get("turn_count") or 0,
                    "summary": summary,
                    "raw_snippet": c.get("snip") or (body[:200] if body else ""),
                }
            )
        _debug(
            f"recall picked={len(results)} elapsed_ms={int((time.time()-t0)*1000)}"
        )
        return results
    except Exception as e:
        _debug(f"recall FATAL: {e}\n{traceback.format_exc()}")
        return []
    finally:
        conn.close()
