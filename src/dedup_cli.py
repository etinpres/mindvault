#!/usr/bin/env python3
"""MindVault v3 — Memory dedup CLI.

Sprint 11/12/15 BUILD-LOG §"미해결" 의 duplicate memory 우려 해소.

핵심 인식: **stem 동일 ≠ 의미 dup**. 같은 파일명이라도 frontmatter `name` 다르면
별개 메모리. 따라서 두 종류 group 구분 보고:

  - name-dup: frontmatter `name` (lowercase strip) 동일. 진짜 의미 dup.
              Memory Compiler 로 본문 통합 후 1건만 유지 권장.
  - stem-collision: 파일명(stem) 동일·name 다름. 사용자가 같은 슬러그로 별개 메모리
                    만든 경우. 본문 비교 후 rename 또는 둘 다 보존.

자동 삭제는 안 함 — list/merge/rename 모두 명시 호출. .bak 백업 후 작업.

명령:
  python3 dedup_cli.py list                  # name-dup + stem-collision JSON 출력
  python3 dedup_cli.py merge <name>          # name-dup 그룹의 Gemma 통합 + canonical 선택
  python3 dedup_cli.py rename <path> <new_stem>  # stem-collision 해소: 파일명만 변경
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# memory_indexer 의 디렉토리 정책 공유
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


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] dedup: {msg}\n")
    except Exception:
        pass


def _scan(memory_dirs: list[Path] | None = None) -> dict:
    """모든 indexed memory path 를 stem 과 frontmatter name 별로 group.

    반환:
      {
        "files": [{path, stem, name, mtime, size}],
        "name_dups": [{key, files: [...]}],     # 진짜 의미 dup
        "stem_collisions": [{key, files: [...]}],  # 같은 파일명, 다른 name
      }
    """
    if memory_dirs is None:
        memory_dirs = DEFAULT_MEMORY_DIRS + _extra_memory_dirs()
    files = []
    by_stem: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for p in _collect_md_files(memory_dirs):
        try:
            text = p.read_text(encoding="utf-8")
            st = p.stat()
        except OSError:
            continue
        fm, _body = parse_frontmatter(text)
        name = (fm.get("name") or "").strip()
        entry = {
            "path": str(p),
            "stem": p.stem,
            "name": name,
            "mtime": st.st_mtime,
            "size": st.st_size,
        }
        files.append(entry)
        by_stem[p.stem].append(entry)
        if name:
            by_name[name.lower()].append(entry)

    name_dups = []
    for k, entries in by_name.items():
        if len(entries) < 2:
            continue
        name_dups.append({"key": k, "files": _sort_by_freshness(entries)})

    stem_collisions = []
    for k, entries in by_stem.items():
        if len(entries) < 2:
            continue
        names = {(e["name"] or "").lower() for e in entries}
        # name 도 같으면 name-dup 으로만 분류, stem-collision 에서 제외
        if len(names) <= 1 and "" not in names:
            continue
        stem_collisions.append(
            {"key": k, "files": _sort_by_freshness(entries)}
        )

    name_dups.sort(key=lambda g: g["key"])
    stem_collisions.sort(key=lambda g: g["key"])
    return {
        "files": files,
        "name_dups": name_dups,
        "stem_collisions": stem_collisions,
    }


def _sort_by_freshness(entries: list[dict]) -> list[dict]:
    """mtime 최신 우선, 동률이면 size 큰 쪽 우선. canonical 후보가 [0]."""
    return sorted(entries, key=lambda e: (e["mtime"], e["size"]), reverse=True)


def _serialize_fm_value(v) -> str:
    """frontmatter 값을 메모리 컨벤션 형식 문자열로 직렬화 (dedup-merge-2).

    - list → flow-style `[a, b]` (supersedes/deprecated_by 가 쓰는 형식. 항목은
      stem slug 라 공백/콤마/따옴표 없음).
    - dict → yaml flow `{k: v}` (metadata 등 nested).
    - scalar → str(v).
    줄바꿈은 frontmatter 라인 구조를 깨므로 공백으로 치환.
    """
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    if isinstance(v, dict):
        import yaml as _yaml
        return _yaml.safe_dump(
            v, default_flow_style=True, allow_unicode=True
        ).strip()
    return str(v).replace("\n", " ").strip()


def cmd_list() -> int:
    result = _scan()
    summary = {
        "total_indexed": len(result["files"]),
        "name_dup_groups": len(result["name_dups"]),
        "stem_collision_groups": len(result["stem_collisions"]),
        "name_dups": result["name_dups"],
        "stem_collisions": result["stem_collisions"],
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _backup(path: Path) -> Path:
    # v3.2.6 Round 3 NR2: atomic backup — partial .bak 잔류 시 향후 복구 시도
    # 실패. 본 dedup 가 canonical overwrite 직전에 backup 만들기 때문에 critical.
    # v3.2.8: finally — KeyboardInterrupt 도 tmp orphan 차단.
    bak = path.with_suffix(path.suffix + ".bak")
    tmp = path.with_suffix(path.suffix + ".bak.tmp")
    # bug-audit 2026-05-29 (dedup-backup-3): 기존 .bak 이 있으면 덮어쓰기 전에
    # 타임스탬프 사본으로 회전해 직전(유일) 복구본 소실을 막는다. 첫 백업(.bak 부재)은
    # 그대로 .bak 에 착지 → 기존 동작·테스트 계약 유지. 같은 초 재호출 시 회전본이
    # 이미 있으면 회전 스킵(드문 edge — 같은 path 동일초 중복 백업).
    if bak.exists():
        rotated = bak.parent / (bak.name + "." + time.strftime("%Y%m%d-%H%M%S"))
        try:
            if not rotated.exists():
                os.replace(bak, rotated)
        except OSError:
            pass
    content = path.read_text(encoding="utf-8")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, bak)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return bak


def cmd_merge(name_key: str, dry_run: bool = False) -> int:
    """name-dup 그룹의 본문을 Memory Compiler 로 통합, canonical 만 유지.

    canonical = mtime 최신 + size 큰 쪽. 나머지는 .bak 백업 후 삭제. canonical 본문은
    Gemma 가 통합한 결과로 overwrite (memory_compiler._compile_update 재활용).
    """
    result = _scan()
    matched = [g for g in result["name_dups"] if g["key"] == name_key.lower()]
    if not matched:
        sys.stdout.write(json.dumps({
            "ok": False, "error": f"no name-dup group for {name_key!r}"
        }) + "\n")
        return 1
    group = matched[0]["files"]
    canonical = Path(group[0]["path"])
    others = [Path(g["path"]) for g in group[1:]]

    canon_text = canonical.read_text(encoding="utf-8")
    canon_fm, canon_body = parse_frontmatter(canon_text)

    # 다른 path 본문들 차례로 compile → canonical body 누적 정제
    from memory_compiler import _compile_update  # noqa: WPS433
    merged_body = canon_body
    compile_log = []
    # bug-audit 2026-05-29 (dedup-merge-1): compile 에 성공해 canonical 본문으로
    # 병합된 other 만 삭제 대상에 넣는다. compile 실패한 other 의 본문은 canonical
    # 에 들어가지 않았으므로 삭제하면 영구 유실 (이전엔 실패해도 전부 삭제했음).
    merged_others: list[Path] = []
    for other in others:
        try:
            other_text = other.read_text(encoding="utf-8")
        except OSError as e:
            compile_log.append({"path": str(other), "ok": False, "error": str(e)})
            continue
        _, other_body = parse_frontmatter(other_text)
        candidate = {
            "title": canon_fm.get("name", canonical.stem),
            "body": other_body,
        }
        new_body = _compile_update(merged_body, candidate)
        if not new_body:
            compile_log.append({
                "path": str(other), "ok": False, "error": "gemma compile failed",
            })
            continue
        merged_body = new_body
        merged_others.append(other)
        compile_log.append({"path": str(other), "ok": True})

    plan = {
        "canonical": str(canonical),
        "drop": [str(p) for p in merged_others],
        "compile_log": compile_log,
        "merged_body_chars": len(merged_body),
    }
    if dry_run:
        plan["dry_run"] = True
        json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    # bug-audit 2026-05-29 (dedup-merge-1 / dedup-merge-empty-6): 데이터 유실 가드.
    # 통합 성공한 other 가 하나도 없거나(예: Gemma 다운으로 전부 실패) merged_body 가
    # 비면 canonical overwrite/삭제를 시작하지 않고 중단한다. 이전엔 Gemma 가 죽어도
    # canonical 을 빈/원본 본문으로 덮고 others 를 전부 지워 그룹이 통째로 유실됐다.
    if not merged_others or not merged_body.strip():
        plan.update({
            "ok": False,
            "error": "merge aborted: no successful compile (data-loss guard)",
        })
        json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 1

    # canonical 도 .bak 백업 후 overwrite (Compiler 와 동일 안전 패턴)
    _backup(canonical)
    # bug-audit 2026-05-29 (dedup-merge-2): name/description/type 외의 frontmatter
    # (supersedes / deprecated_by / metadata 등 audit link)를 보존해 재방출. 이전엔
    # 세 키만 다시 써서 canonical 의 deprecation/supersede 링크가 통째로 소실됐다.
    # parse_frontmatter 가 yaml 기반이라 list 는 Python list 로 들어오므로 flow-style
    # ([a, b]) 로 직렬화 — 이 메모리를 읽는 memory_indexer(yaml)·_is_deprecated(regex)
    # ·contradiction_review_cli 가 기대하는 형식과 일치.
    _core_keys = {"name", "description", "type"}
    extra_lines = "".join(
        f"{k}: {_serialize_fm_value(v)}\n"
        for k, v in canon_fm.items()
        if k not in _core_keys and v is not None
    )
    final_fm = (
        "---\n"
        # bug-audit 2026-06-01 (dedup-multiline-fm-corrupt): name/description 이 멀티라인
        # YAML 블록 스칼라면 raw 보간 시 frontmatter 라인이 깨진다. _serialize_fm_value 로
        # 단일라인 정규화(memory_review_cli 의 _fm_oneline 과 동일 취지).
        f"name: {_serialize_fm_value(canon_fm.get('name', canonical.stem))}\n"
        f"description: {_serialize_fm_value(canon_fm.get('description', canon_fm.get('name', canonical.stem)))}\n"
        f"type: {_serialize_fm_value(canon_fm.get('type', 'project'))}\n"  # bug-audit 2026-06-01: type 도 단일라인 정규화(name/desc sibling)
        f"{extra_lines}"
        "---\n\n"
        f"{merged_body.rstrip()}\n"
    )
    # v3.2.6 Round 3 NR2: canonical overwrite atomic — merged 영구 메모리 write.
    # v3.2.8: finally — KeyboardInterrupt 도 tmp orphan 차단.
    _canon_tmp = canonical.with_suffix(canonical.suffix + ".tmp")
    try:
        _canon_tmp.write_text(final_fm, encoding="utf-8")
        os.replace(_canon_tmp, canonical)
    finally:
        try:
            _canon_tmp.unlink(missing_ok=True)
        except OSError:
            pass

    dropped = []
    for o in merged_others:
        _backup(o)
        try:
            o.unlink()
            dropped.append(str(o))
        except OSError as e:
            _debug(f"unlink fail {o}: {e}")

    # reindex (실패해도 dedup 성공)
    reindex_info: dict = {}
    try:
        from memory_indexer import incremental_index  # noqa: WPS433
        reindex_info = incremental_index()
    except Exception as e:
        reindex_info = {"error": str(e)}

    plan.update({
        "ok": True,
        "canonical_backup": str(canonical) + ".bak",
        "dropped": dropped,
        "reindex": reindex_info,
    })
    json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_rename(path_str: str, new_stem: str) -> int:
    """stem-collision 해소: 파일명만 변경 (frontmatter·본문 보존)."""
    src = Path(path_str).expanduser().absolute()
    if not src.is_file() or src.suffix != ".md":
        sys.stdout.write(json.dumps({
            "ok": False, "error": "invalid source path",
        }) + "\n")
        return 1
    # new_stem safety — path traversal 차단
    safe = new_stem.replace("/", "").replace("\\", "").replace("..", "")
    if not safe or safe != new_stem:
        sys.stdout.write(json.dumps({
            "ok": False, "error": "invalid new_stem (special chars)",
        }) + "\n")
        return 1
    dst = src.with_name(f"{safe}.md")
    if dst.exists():
        sys.stdout.write(json.dumps({
            "ok": False, "error": "target exists", "target": str(dst),
        }) + "\n")
        return 1
    src.rename(dst)

    reindex_info: dict = {}
    try:
        from memory_indexer import incremental_index  # noqa: WPS433
        reindex_info = incremental_index()
    except Exception as e:
        reindex_info = {"error": str(e)}

    sys.stdout.write(json.dumps({
        "ok": True, "from": str(src), "to": str(dst), "reindex": reindex_info,
    }, ensure_ascii=False) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_merge = sub.add_parser("merge")
    p_merge.add_argument("name")
    p_merge.add_argument("--dry-run", action="store_true")
    p_rename = sub.add_parser("rename")
    p_rename.add_argument("path")
    p_rename.add_argument("new_stem")
    args = parser.parse_args()

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "merge":
        return cmd_merge(args.name, dry_run=args.dry_run)
    if args.cmd == "rename":
        return cmd_rename(args.path, args.new_stem)
    return 1


if __name__ == "__main__":
    sys.exit(main())
