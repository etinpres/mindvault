"""One-shot backfill: pair every memory/*.md against every other,
queue contradictions for review.

USAGE
    python scripts/contradiction_backfill.py
    python scripts/contradiction_backfill.py --memory-dir /custom/path
    python scripts/contradiction_backfill.py --dry-run   # show stats, don't append
    python scripts/contradiction_backfill.py --limit 10  # process first N files

This script calls Gemma ~N*top_k times where N is the memory file count.
At ~3s per Gemma call, expect ~10 minutes per 200 files.

After running, review the queue:
    python -m src.contradiction_review_cli list

Apply resolutions interactively:
    python -m src.contradiction_review_cli show <idx>
    python -m src.contradiction_review_cli resolve <idx> --action <action> --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _add_repo_to_path() -> None:
    """Allow `from contradiction_detector import ...` when running from repo root."""
    repo = Path(__file__).resolve().parent.parent
    src = repo / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _default_memory_dir() -> Path:
    """Derive memory dir from $HOME — matches session_memory_end._default_memory_dir.

    Public-ship: never hardcode a user slug. Honor the same env overrides the
    hook honors so the backfill script runs in the same slot as production.
    Precedence: MV3_MEMORY_DIR → MV3_PROJECTS_DIR/memory → PROJECTS_ROOT/home_slug/memory.
    """
    mem_override = os.environ.get("MV3_MEMORY_DIR", "").strip()
    if mem_override:
        return Path(mem_override).expanduser()
    proj_override = os.environ.get("MV3_PROJECTS_DIR", "").strip()
    if proj_override:
        return Path(proj_override).expanduser() / "memory"
    projects_root = Path(
        os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")
    ).expanduser()
    home_slug = "-" + str(Path.home()).strip("/").replace("/", "-")
    return projects_root / home_slug / "memory"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contradiction_backfill")
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="Memory directory to sweep (default: derived from $HOME via MV3_PROJECTS_ROOT/home_slug/memory; respects MV3_MEMORY_DIR / MV3_PROJECTS_DIR overrides).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print contradiction counts but do NOT append to review queue.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N memory files (for testing).",
    )
    args = parser.parse_args(argv)

    _add_repo_to_path()
    from contradiction_detector import (
        detect_contradictions,
        append_to_review_queue,
    )

    default_dir = _default_memory_dir()
    mem_dir = args.memory_dir if args.memory_dir else default_dir
    if not mem_dir.exists():
        print(f"memory dir not found: {mem_dir}", file=sys.stderr)
        return 1

    # Defect I-memdir: detect_contradictions → _hybrid_search → recall_memory
    # reads the PRODUCTION index DB (memory_indexer.DB_PATH), NOT this directory.
    # _hybrid_search then filters recall hits to paths under mem_dir. So if
    # mem_dir's files are not in the prod index DB, every hit is filtered out and
    # detection silently reports ZERO contradictions. For the default dir (which
    # IS indexed) this works; a custom --memory-dir looks like it works but
    # detects nothing. Warn loudly rather than fail (the dir may legitimately be
    # indexed via MV3_EXTRA_MEMORY_DIRS / sources.json).
    try:
        differs = mem_dir.resolve(strict=False) != default_dir.resolve(strict=False)
    except OSError:
        differs = str(mem_dir) != str(default_dir)
    if differs:
        print(
            f"WARNING: --memory-dir {mem_dir} differs from the indexed memory dir "
            f"{default_dir}. recall_memory reads the production index DB; files "
            f"under {mem_dir} that aren't indexed will produce zero contradictions. "
            f"Re-index that dir first (MV3_EXTRA_MEMORY_DIRS / sources.json) or use "
            f"the default.",
            file=sys.stderr,
        )

    files = sorted(mem_dir.glob("*.md"))
    # Skip MEMORY.md (index, no frontmatter)
    files = [f for f in files if f.name != "MEMORY.md"]
    if args.limit is not None:  # bug-audit 2026-06-01: --limit 0 falsy 가드 → 전체 sweep 방지
        files = files[: args.limit]

    print(f"sweep target: {len(files)} memory files in {mem_dir}")
    if args.dry_run:
        print("(dry-run — no queue append)")

    total_contradictions = 0
    for i, path in enumerate(files, 1):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[{i}/{len(files)}] {path.name}: read error: {e}", file=sys.stderr)
            continue

        # Extract slug + body. Treat each existing memory as the "new" candidate
        # paired against the rest.
        candidate = {
            "slug": path.stem,
            "title": path.stem.replace("_", " ").replace("-", " "),
            "body": text,
            "path": path,
        }

        contradictions = detect_contradictions(candidate, mem_dir)
        if contradictions:
            total_contradictions += len(contradictions)
            print(
                f"[{i}/{len(files)}] {path.name}: "
                f"{len(contradictions)} contradiction(s)"
            )
            if not args.dry_run:
                append_to_review_queue(
                    path.stem, contradictions, new_path=path
                )

    print()
    print(f"=== Total: {total_contradictions} contradictions across {len(files)} files ===")
    if not args.dry_run and total_contradictions > 0:
        print("Review:  python -m src.contradiction_review_cli list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
