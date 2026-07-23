#!/usr/bin/env python3
"""MindVault v3 — /recall CLI 진입점.

JSONL 세션(FTS5 + Gemma 재순위/요약, Sprint 2) 또는 memory/*.md(hybrid RRF, Sprint 4)
또는 둘 다 검색.

usage:
    recall_cli.py <query> [--source memory|sessions|both]

기본 --source=both. /recall 슬래시 스킬과 호환.

stdout: 한 줄 JSON
exit: 항상 0 (실패 시 빈 결과)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _search_sessions(query: str, top_k: int = 3) -> list[dict]:
    """Sprint 2: JSONL FTS5 + Gemma 재순위/요약."""
    from search import recall as session_recall  # noqa: WPS433
    return session_recall(query, top_k=top_k)


def _search_memory(query: str, top_k: int = 5) -> list[dict]:
    """Sprint 4: memory/*.md hybrid RRF. 명시 호출이라 게이트 0.

    bug-audit 2026-06-02 (#9): score_threshold 만 0 으로 풀고 raw_cosine_min 을
    안 넘겨 기본 0.32 게이트가 그대로 적용됐다 — 사용자가 직접 친 /recall 인데
    약-관련 메모리가 silent drop. 자동주입 hook(0.32 게이트, V1 토큰낭비 회피)과
    달리 명시 검색은 "관련된 건 다 보여줘"가 의도(top_k=5, score_threshold=0 도
    같은 신호)라 raw 게이트도 면제한다.
    """
    from memory_search import recall_memory  # noqa: WPS433
    return recall_memory(query, top_k=top_k, score_threshold=0.0, raw_cosine_min=0.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="MindVault v3 recall CLI")
    parser.add_argument("query", help="검색 쿼리 (공백 포함 시 따옴표로 감싸기)")
    parser.add_argument(
        "--source",
        choices=["memory", "sessions", "both"],
        default="both",
        help="검색 대상 (기본: both)",
    )
    args = parser.parse_args()

    query = (args.query or "").strip()
    out: dict = {"query": query}
    if not query:
        if args.source in ("memory", "both"):
            out["memory"] = []
        if args.source in ("sessions", "both"):
            out["sessions"] = []
        sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
        return 0

    # bug-audit 2026-06-01 (recall-cli-exit0-contract): docstring(line 13) 계약은
    # "exit 항상 0, 실패 시 빈 결과". 그러나 검색 호출은 호출 시점에 memory_search
    # (→numpy) 를 lazy import 하므로 numpy 없는 인터프리터(배포 /recall 가 Claude
    # Bash 의 numpy-less python3 로 실행되는, hook 이 re-exec 로 방어하는 바로 그
    # 컨텍스트)에서 ImportError 가 전파돼 비-0 + traceback 종료된다. source 별로
    # 감싸 실패는 빈 결과로 흡수하고 계약대로 항상 0 을 보장한다.
    if args.source in ("memory", "both"):
        try:
            out["memory"] = _search_memory(query, top_k=5)
        except Exception:  # noqa: BLE001
            out["memory"] = []
    if args.source in ("sessions", "both"):
        try:
            out["sessions"] = _search_sessions(query, top_k=3)
        except Exception:  # noqa: BLE001
            out["sessions"] = []

    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
