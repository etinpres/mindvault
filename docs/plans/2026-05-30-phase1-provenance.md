# Phase 1 — Provenance (출처 추적) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 영구기억에 출처(`source_type`/`source_ref`)를 보존하고 회수 시 출처 라벨을 동반 표시해, over-trust(검증 없이 stale 믿음)·under-integration(반영 안 함)의 공통 뿌리인 "출처 없는 주장"을 해소한다.

**Architecture:** `write_staged`가 source 필드를 frontmatter에 기록(기존 `staged_from_session` 확장). `recall_memory`가 결과 path의 frontmatter를 재파싱해 provenance를 부착(indexer DB 스키마 불변 — 안전, TOP_K=1이라 파싱 1회). `_format_output`이 출처 라벨을 출력. 기존 171개 메모리는 backfill CLI로 소급.

**Tech Stack:** Python, pytest, YAML frontmatter(`yaml.safe_load`), SQLite(불변). 제약: CC 내부 전용 / 운영비 0 / v1 토큰낭비 금지(출처 라벨은 짧게, 원문은 필요시 Read).

**Scope:** Phase 1의 ①provenance 축만. ②효과적회수 self-check 계약, ③stale 감지는 별도 plan(②③은 ①의 source 필드에 의존).

---

## File Structure

- Modify: `src/session_memory_end.py` (`write_staged` — source 필드 기록)
- Modify: `src/memory_search.py` (`recall_memory` — 결과에 provenance 부착)
- Modify: `hooks/memory-recall.py` (`_format_output` — 출처 라벨 출력)
- Create: `src/provenance_backfill_cli.py` (기존 메모리 소급)
- Test: `tests/test_provenance.py`

각 Task는 독립 testable. TDD: 실패 테스트 → 확인 → 구현 → 통과 → 커밋.

---

### Task 1: write_staged 가 source_type/source_ref 를 frontmatter 에 기록

**Files:**
- Modify: `src/session_memory_end.py:122-154` (`write_staged`)
- Test: `tests/test_provenance.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_provenance.py
import importlib.util
from pathlib import Path

def _load(monkeypatch, tmp_path):
    # 기존 test 관례: MV3_DATA_DIR / 메모리 디렉토리 env 격리
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MV3_MEMORY_DIR", str(tmp_path / "memory"))
    spec = importlib.util.spec_from_file_location(
        "sme", Path(__file__).parent.parent / "src" / "session_memory_end.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_write_staged_records_source(monkeypatch, tmp_path):
    sme = _load(monkeypatch, tmp_path)
    item = {"type": "project", "title": "t", "reason": "r",
            "evidence": "e", "body": "b"}
    path = sme.write_staged(item, session_id="abcd1234-5678-90ab-cdef-111122223333")
    assert path is not None
    text = path.read_text()
    assert "source_type: session" in text
    assert "source_ref: abcd1234-5678-90ab-cdef-111122223333" in text
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_provenance.py::test_write_staged_records_source -v`
Expected: FAIL (`source_type` not in text)

- [ ] **Step 3: write_staged 시그니처·fm_lines 수정**

`def write_staged(` 시그니처에 파라미터 추가 (기본값으로 기존 호출부 무변경):

```python
def write_staged(
    item: dict, session_id: str, slug_override: str | None = None,
    source_type: str = "session", source_ref: str | None = None,
) -> Path | None:
```

`fm_lines` 리스트(현재 `reason`/`evidence` 다음)에 source 라인 2개 추가:

```python
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
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_provenance.py::test_write_staged_records_source -v`
Expected: PASS

- [ ] **Step 5: 회귀 확인 + 커밋**

Run: `pytest tests/test_session_memory_end_integration.py tests/test_memory_review_cli.py -q`
Expected: PASS (frontmatter 라인 추가가 기존 파서·review CLI 깨지 않음)

```bash
git add src/session_memory_end.py tests/test_provenance.py
git commit -m "feat(provenance): write_staged 가 source_type/source_ref 기록"
```

---

### Task 2: recall_memory 가 결과에 provenance 부착

**Files:**
- Modify: `src/memory_search.py:504-` (`recall_memory` 반환 직전)
- Test: `tests/test_provenance.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_provenance.py 에 추가
from src import memory_search

def test_recall_attaches_provenance(monkeypatch, tmp_path):
    # 메모리 1개를 source_type/source_ref 포함해 쓰고 인덱싱한 fixture 위에서
    # recall_memory 결과 dict 에 provenance 키가 있는지 확인.
    # (기존 test_memory_search.py 의 인덱스 빌드 fixture 패턴 재사용)
    results = memory_search.recall_memory("쿼리", db_path=_BUILT_DB)  # fixture
    assert results, "후보 없음 — fixture 확인"
    assert "provenance" in results[0]
    assert results[0]["provenance"]["source_type"] in ("session", "url", "unknown")
```

> 주: `_BUILT_DB`는 기존 `tests/test_memory_search.py`가 쓰는 인덱스 빌드 헬퍼를 따른다. Task 2 착수 시 `tests/test_memory_search.py`의 fixture 빌드 패턴을 먼저 읽고 동일 방식으로 source 필드 포함 메모리를 인덱싱한다.

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_provenance.py::test_recall_attaches_provenance -v`
Expected: FAIL (`'provenance'` KeyError)

- [ ] **Step 3: recall_memory 반환 직전에 provenance 부착**

`recall_memory`가 결과 리스트(`results`)를 만든 뒤 반환(`return results`) 직전에, 각 결과의 `path` frontmatter를 재파싱해 provenance를 부착한다. 파일 상단에 import 추가:

```python
from .memory_indexer import parse_frontmatter
```

반환 직전 루프:

```python
    for r in results:
        prov = {"source_type": "unknown", "source_ref": None, "captured_at": None}
        try:
            fm, _ = parse_frontmatter(Path(r["path"]).read_text(encoding="utf-8"))
            prov["source_type"] = fm.get("source_type", "unknown")
            prov["source_ref"] = fm.get("source_ref")
            prov["captured_at"] = fm.get("staged_at") or fm.get("captured_at")
        except (OSError, KeyError):
            pass
        r["provenance"] = prov
    return results
```

> indexer DB 스키마는 건드리지 않는다. TOP_K=1이라 파싱은 결과당 1회 — hot-path 영향 무시 가능.

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_provenance.py::test_recall_attaches_provenance -v`
Expected: PASS

- [ ] **Step 5: 회귀 확인 + 커밋**

Run: `pytest tests/test_memory_search.py tests/test_memory_recall_deprecated.py -q`
Expected: PASS

```bash
git add src/memory_search.py tests/test_provenance.py
git commit -m "feat(provenance): recall_memory 결과에 provenance 부착 (path frontmatter 재파싱)"
```

---

### Task 3: _format_output 이 출처 라벨을 출력

**Files:**
- Modify: `hooks/memory-recall.py:299-343` (`_format_output`)
- Test: `tests/test_provenance.py`

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_provenance.py 에 추가
import importlib.util
def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "mrecall", Path(__file__).parent.parent / "hooks" / "memory-recall.py"
    )
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def test_format_output_shows_source_label():
    hook = _load_hook()
    results = [{
        "name": "x", "description": "d", "snippet": "", "score": 0.9,
        "source": ["vec"],
        "provenance": {"source_type": "session", "source_ref": "abcd1234-...",
                       "captured_at": "2026-05-30T10:00:00"},
    }]
    out = hook._format_output(results)
    assert "출처:" in out
    assert "session" in out
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_provenance.py::test_format_output_shows_source_label -v`
Expected: FAIL (`'출처:'` not in out)

- [ ] **Step 3: _format_output 에 출처 라벨 추가**

`_format_output`의 결과 루프(현재 `score = r.get("score", 0)` 다음, `lines.append(f"- [{name}] ...")` 직후)에 provenance 라벨 1줄 추가. 짧게(토큰낭비 금지) — source_type + ref 앞 8자 + captured_at 날짜만:

```python
        prov = r.get("provenance") or {}
        if prov.get("source_type") and prov["source_type"] != "unknown":
            ref = _sanitize(str(prov.get("source_ref") or "")[:8])
            cap = _sanitize(str(prov.get("captured_at") or "")[:10])
            lines.append(f"  출처: {_sanitize(prov['source_type'])} {ref} {cap}".rstrip())
```

> Chain-of-Note 계약 문구(맨 끝 "회수 노트:" 의무)는 그대로 둔다. 출처 라벨은 회수 fact의 검증 경로를 제공해 over-trust를 누른다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_provenance.py::test_format_output_shows_source_label -v`
Expected: PASS

- [ ] **Step 5: 회귀 확인 + 커밋**

Run: `pytest tests/test_memory_hook.py -q`
Expected: PASS (출처 라벨이 RECALLED_NAME 추출·sanitize 계약 안 깸)

```bash
git add hooks/memory-recall.py tests/test_provenance.py
git commit -m "feat(provenance): 회수 출력에 출처 라벨 동반 표시"
```

---

### Task 4: 기존 메모리 backfill CLI

**Files:**
- Create: `src/provenance_backfill_cli.py`
- Test: `tests/test_provenance.py`

기존 171개 메모리 중 `source_type`이 없는 것에 소급 부여. 우선순위: `staged_from_session`이 있으면 `source_type: session` + `source_ref`=그 값. 없으면 `source_type: unknown`(억측 금지 — git blame 등 추론은 오탐 위험이라 하지 않음).

- [ ] **Step 1: 실패 테스트 작성**

```python
# tests/test_provenance.py 에 추가
def test_backfill_adds_source_from_staged_session(monkeypatch, tmp_path):
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "feedback_x.md"
    p.write_text("---\nname: x\ntype: feedback\nstaged_from_session: abcd1234\n---\n\nbody\n")
    n = bf.backfill_dir(mem, dry_run=False)
    assert n == 1
    text = p.read_text()
    assert "source_type: session" in text
    assert "source_ref: abcd1234" in text

def test_backfill_unknown_when_no_session(monkeypatch, tmp_path):
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "reference_y.md"
    p.write_text("---\nname: y\ntype: reference\n---\n\nbody\n")
    bf.backfill_dir(mem, dry_run=False)
    assert "source_type: unknown" in p.read_text()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `pytest tests/test_provenance.py -k backfill -v`
Expected: FAIL (module not found)

- [ ] **Step 3: backfill CLI 구현**

```python
# src/provenance_backfill_cli.py
"""기존 메모리에 source_type/source_ref 소급 부여. 억측 금지: staged_from_session
있으면 session, 없으면 unknown. atomic write (tmp + os.replace)."""
from __future__ import annotations
import argparse, os, re, sys
from pathlib import Path
from .memory_indexer import parse_frontmatter

def _has(fm: dict, key: str) -> bool:
    return key in fm and fm[key] not in (None, "")

def backfill_file(path: Path, dry_run: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if not fm or _has(fm, "source_type"):
        return False
    if _has(fm, "staged_from_session"):
        st, ref = "session", str(fm["staged_from_session"])
    else:
        st, ref = "unknown", ""
    # frontmatter 끝('---')의 닫는 줄 앞에 두 라인 삽입 — 라인 기반 일관.
    lines = text.split("\n")
    # 첫 '---' 다음부터 두 번째 '---' 사이가 frontmatter
    close = lines.index("---", 1)
    inject = [f"source_type: {st}"]
    if ref:
        inject.append(f"source_ref: {ref}")
    new = lines[:close] + inject + lines[close:]
    out = "\n".join(new)
    if dry_run:
        return True
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, path)
    return True

def backfill_dir(d: Path, dry_run: bool) -> int:
    n = 0
    for p in sorted(d.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        if backfill_file(p, dry_run):
            n += 1
    return n

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("memory_dir")
    ap.add_argument("--apply", action="store_true", help="실제 쓰기 (기본 dry-run)")
    a = ap.parse_args()
    n = backfill_dir(Path(a.memory_dir), dry_run=not a.apply)
    print(f"{'적용' if a.apply else 'dry-run'}: {n}건")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_provenance.py -k backfill -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/provenance_backfill_cli.py tests/test_provenance.py
git commit -m "feat(provenance): 기존 메모리 source backfill CLI (억측 금지, unknown fallback)"
```

---

### Task 5: end-to-end 통합 테스트 + 실제 백필 dry-run 확인

**Files:**
- Test: `tests/test_provenance.py`

- [ ] **Step 1: end-to-end 테스트 작성**

```python
# tests/test_provenance.py 에 추가
def test_e2e_staged_to_recall_label(monkeypatch, tmp_path):
    """write_staged → 인덱싱 → recall_memory → _format_output 에 출처 라벨까지."""
    # 기존 test_memory_search.py 인덱스 빌드 헬퍼로 source 포함 메모리 1개 인덱싱,
    # recall_memory 결과를 _format_output 에 넣어 '출처:' + 'session' 동시 확인.
    # (구체 fixture 는 Task 2 에서 확정한 빌드 헬퍼 재사용)
    ...
```

- [ ] **Step 2~4: 작성 → 실패 → fixture 연결 → 통과**

Run: `pytest tests/test_provenance.py -v`
Expected: 전체 PASS

- [ ] **Step 5: 실 메모리 백필 dry-run (사용자 확인용)**

Run: `python -m src.provenance_backfill_cli ~/.claude/projects/<home-slug>/memory`
Expected: `dry-run: N건` 출력. 사용자가 N 확인 후 `--apply` 결정.

- [ ] **Step 6: 전체 회귀 + 커밋**

Run: `pytest -q`
Expected: 기존 599 + 신규 통과

```bash
git add tests/test_provenance.py
git commit -m "test(provenance): end-to-end 통합 + 백필 dry-run 절차"
```

---

## 완료 게이트 (spec §4.3 ① 대응)

- [ ] 신규 영구기억 출처 추적률 100% (Task 1 — write_staged 항상 기록)
- [ ] 회수 출력에 출처 라벨 동반 (Task 3)
- [ ] 기존 171개 백필 (Task 4·5, `--apply`는 사용자 승인 후)

## 비범위 (별도 plan)

- ②효과적 회수 self-check 계약 강화 (CLAUDE.md/hook 프롬프트 계약 + self_eval strict cited 목표치)
- ③stale 자동 감지 (코드/모델명/버전 참조 메모리 재검증, BGE→Arctic 회귀 케이스)
