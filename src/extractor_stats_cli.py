#!/usr/bin/env python3
"""MindVault v3 NEXT-17 — extractor latency + cache hit rate 측정.

debug.log 의 extractor / session-end 라인 + extractor_cache.cache_stats() 결합.
ship 결정에 필요한 데이터:
- ALWAYS_FIRE 영구화 시 SessionEnd latency 분포 (p50/p95)
- cache hit rate (NEXT-16 효과)
- trigger layer 별 분기 비율 (keyword / next1-action / next10-ack / always-fire)
- staged 통과율 (extractor candidate → 실제 영구 등록 비율)

사용 예:
    python3 extractor_stats_cli.py                  # 최근 168h
    python3 extractor_stats_cli.py --last-hours 24
    python3 extractor_stats_cli.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import os as _os_stats

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
_MV3_DATA_DIR = Path(_os_stats.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
DEBUG_LOG = _MV3_DATA_DIR / "debug.log"


def _default_projects_dir() -> Path:
    override = _os_stats.environ.get("MV3_PROJECTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home_slug = "-" + str(Path.home()).strip("/").replace("/", "-")
    return Path(_os_stats.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser() / home_slug


_PROJECTS_DIR = _default_projects_dir()
STAGED_DIRS = (
    _PROJECTS_DIR / "memory" / "_staged",
    _PROJECTS_DIR / "memory" / "_procedural" / "_staged",
)
_HOOK_DIR = Path(__file__).resolve().parent
if (_HOOK_DIR / "memory_extractor.py").is_file():
    if str(_HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOK_DIR))
else:
    # v3.2.7: MV3_SCRIPTS_DIR env var 우선.
    PROD = Path(_os_stats.environ.get("MV3_SCRIPTS_DIR", "~/.claude/scripts/mindvault")).expanduser()
    if str(PROD) not in sys.path:
        sys.path.insert(0, str(PROD))

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(.*)$")

# extractor / session-end / compiler 패턴
TRIGGER_RE = re.compile(r"extractor: trigger=(\S+)(?:\s+text=.*)?$")
NO_TRIGGER_RE = re.compile(r"extractor: no trigger in (\S+),")
ALWAYS_FIRE_RE = re.compile(r"extractor: always-fire bypass for (\S+)")
ATTEMPT_RE = re.compile(
    r"extractor: extract attempt=(\d+)/(\d+) candidates=(\d+)"
)
CACHE_HIT_RE = re.compile(
    r"extractor: extract cache hit for (\S+): (\d+) candidates"
)
NO_CANDIDATES_RE = re.compile(r"session-end: no candidates for (\w{8})")
COMPILED_RE = re.compile(
    r"session-end: compiled session=(\w{8}) updates=(\d+)/(\d+)"
)
STAGED_RE = re.compile(r"session-end: session (\w{8}): staged (\d+)/(\d+)")
JSONL_MISSING_RE = re.compile(r"session-end: jsonl missing for (\w{8})")


def parse_debug(last_hours: float | None) -> dict:
    """debug.log scan → 통계 dict."""
    cutoff = (
        datetime.now() - timedelta(hours=last_hours) if last_hours else None
    )
    counts: Counter = Counter()
    trigger_layers: Counter = Counter()
    attempts_seen: list[int] = []
    candidates_per_session: list[int] = []
    cache_hit_count = 0
    staged_pairs: list[tuple[int, int]] = []  # (staged, total)
    compiled_pairs: list[tuple[int, int]] = []  # (updates, before)
    session_end_total = 0
    jsonl_missing = 0

    if not DEBUG_LOG.is_file():
        return {"error": "debug.log not found"}

    with DEBUG_LOG.open() as f:
        for line in f:
            m = TS_RE.match(line.rstrip())
            if not m:
                continue
            # bug-audit 2026-06-02 (#24): TS_RE 는 \d{2} 만 봐 달력상 불가능한
            # 타임스탬프('2026-13-45 99:99:99')도 매칭한다. strptime 이 미가드면
            # ValueError 로 parse_debug 전체가 죽어 부분 결과도 못 낸다. 손상 라인만 skip.
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if cutoff and ts < cutoff:
                continue
            body = m.group(2)

            if mt := TRIGGER_RE.match(body):
                trigger_layers[mt.group(1)] += 1
                counts["trigger_fired"] += 1
            elif NO_TRIGGER_RE.match(body):
                # bug-audit 2026-06-02 (#23): no_trigger 는 SessionEnd 의 extractor
                # 서브이벤트일 뿐, 같은 SessionEnd 가 뒤이어 'no candidates' 종결
                # 라인도 남긴다. 여기서 session_end_total 을 올리면 한 SessionEnd 가
                # 2로 집계돼 분모(ship 결정 지표)가 부풀고 파생 비율이 왜곡된다.
                # 종결 라인(no_candidates/staged/jsonl_missing)만 분모에 포함.
                counts["no_trigger"] += 1
            elif ALWAYS_FIRE_RE.match(body):
                trigger_layers["always-fire"] += 1
                counts["always_fire_bypass"] += 1
            elif ma := ATTEMPT_RE.match(body):
                attempts_seen.append(int(ma.group(1)))
                candidates_per_session.append(int(ma.group(3)))
            elif mc := CACHE_HIT_RE.match(body):
                cache_hit_count += 1
                candidates_per_session.append(int(mc.group(2)))
            elif mnc := NO_CANDIDATES_RE.match(body):
                counts["no_candidates"] += 1
                session_end_total += 1
            elif mcc := COMPILED_RE.match(body):
                compiled_pairs.append((int(mcc.group(2)), int(mcc.group(3))))
            elif ms := STAGED_RE.match(body):
                staged_pairs.append((int(ms.group(2)), int(ms.group(3))))
                session_end_total += 1
            elif JSONL_MISSING_RE.match(body):
                jsonl_missing += 1
                session_end_total += 1

    # 통계 계산
    out: dict = {
        "session_end_total": session_end_total,
        "jsonl_missing": jsonl_missing,
        "no_trigger": counts.get("no_trigger", 0),
        "trigger_fired": counts.get("trigger_fired", 0),
        "always_fire_bypass": counts.get("always_fire_bypass", 0),
        "no_candidates_after_trigger": counts.get("no_candidates", 0),
        "cache_hit_count": cache_hit_count,
        "trigger_layers": dict(trigger_layers),
    }

    if attempts_seen:
        out["attempts"] = {
            "min": min(attempts_seen),
            "max": max(attempts_seen),
            "avg": round(statistics.mean(attempts_seen), 2),
        }

    if candidates_per_session:
        nz = [c for c in candidates_per_session if c > 0]
        out["candidates_per_extract"] = {
            "zero_count": len(candidates_per_session) - len(nz),
            "nonzero_count": len(nz),
            "max": max(candidates_per_session),
        }
        if nz:
            out["candidates_per_extract"]["nonzero_avg"] = round(
                statistics.mean(nz), 2
            )

    if staged_pairs:
        ratios = [s / t if t else 0 for s, t in staged_pairs]
        out["staged_pass_rate"] = {
            "sessions": len(staged_pairs),
            "avg_pass_rate": round(statistics.mean(ratios), 2),
        }

    if compiled_pairs:
        total_updates = sum(u for u, _ in compiled_pairs)
        total_before = sum(b for _, b in compiled_pairs)
        out["compiler"] = {
            "sessions": len(compiled_pairs),
            "total_candidates": total_before,
            "total_updates": total_updates,
            "update_rate": round(total_updates / total_before, 2)
            if total_before else 0,
        }

    return out


def cache_stats_or_empty() -> dict:
    try:
        import extractor_cache
        return extractor_cache.cache_stats()
    except Exception as e:
        return {"error": str(e)}


def staged_type_distribution() -> dict:
    """NEXT-11 측정 — staged 후보 type 분포 (frontmatter `type:` 읽기)."""
    counts: Counter = Counter()
    for d in STAGED_DIRS:
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = re.search(r"^type:\s*(\w+)\s*$", text, re.MULTILINE)
            counts[m.group(1) if m else "unknown"] += 1
    return dict(counts)


def fmt_human(d: dict, hours: float | None) -> str:
    lines = []
    span = f"최근 {hours}h" if hours else "전체"
    lines.append(f"=== extractor stats ({span}) ===")
    lines.append(f"SessionEnd 호출 총수:       {d.get('session_end_total', 0)}")
    lines.append(f"  - jsonl missing:           {d.get('jsonl_missing', 0)}")
    lines.append(f"  - no_trigger (skip):       {d.get('no_trigger', 0)}")
    lines.append(f"  - no candidates 후 fire:   {d.get('no_candidates_after_trigger', 0)}")
    lines.append(f"  - always-fire bypass:      {d.get('always_fire_bypass', 0)}")
    lines.append(f"trigger fired:              {d.get('trigger_fired', 0)}")
    layers = d.get("trigger_layers", {})
    for layer, n in sorted(layers.items(), key=lambda x: -x[1]):
        lines.append(f"  - {layer:<20} {n}")

    if "attempts" in d:
        a = d["attempts"]
        lines.append(
            f"Gemma attempts: min={a['min']} max={a['max']} avg={a['avg']}"
        )

    if "candidates_per_extract" in d:
        c = d["candidates_per_extract"]
        lines.append(
            f"candidates/extract: zero={c['zero_count']} "
            f"nonzero={c['nonzero_count']} max={c['max']}"
            + (f" nonzero_avg={c['nonzero_avg']}" if "nonzero_avg" in c else "")
        )

    if "staged_pass_rate" in d:
        s = d["staged_pass_rate"]
        lines.append(
            f"staged pass rate: {s['avg_pass_rate']} "
            f"(over {s['sessions']} sessions)"
        )

    if "compiler" in d:
        c = d["compiler"]
        lines.append(
            f"Memory Compiler: {c['sessions']} sessions, "
            f"{c['total_updates']}/{c['total_candidates']} updates "
            f"(rate={c['update_rate']})"
        )

    lines.append(f"cache hits 누적: {d.get('cache_hit_count', 0)}")

    cs = d.get("cache", {})
    lines.append(
        f"cache entries: {cs.get('entries', 0)}, "
        f"total_hits: {cs.get('total_hits', 0)}, "
        f"total_candidates: {cs.get('total_candidates', 0)}"
    )

    st = d.get("staged_types", {})
    if st:
        lines.append("staged type 분포 (NEXT-11):")
        for t, n in sorted(st.items(), key=lambda x: -x[1]):
            lines.append(f"  - {t:<12} {n}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--last-hours",
        type=float,
        default=168.0,
        help="debug.log scan 시간 범위 (default 168 = 1주일)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="debug.log 전체 스캔 (--last-hours 무시)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="JSON 출력 (default human-readable)",
    )
    args = ap.parse_args(argv)

    hours = None if args.all else args.last_hours
    d = parse_debug(hours)
    d["cache"] = cache_stats_or_empty()
    d["staged_types"] = staged_type_distribution()

    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(fmt_human(d, hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
