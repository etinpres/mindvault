# Phase 1 ③ — 신뢰성 검증 (over-trust / stale 자동 감지) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 회수된 stale 메모리를 검증 없이 믿는 over-trust(2026-05-30 BGE-M3→Arctic 사고)를 해소하기 위해, 코드/사실 참조 메모리를 현행 코드와 결정론적으로 대조(Canonical Facts Registry + 라이브 verifier)해 stale 의심을 판정하고, stale 메모리에만 frontmatter flag 를 부여해 회수 시 짧은 경고 라벨을 동반 주입한다.

**Architecture:** 신규 `src/reverify.py`(순수 판정 + frontmatter upsert) + `src/reverify_cli.py`(scan/list/verify-registry). 판별 신호 = "메모리가 stale_alias 토큰 포함 AND current_value 토큰 미포함 AND verifier 가 current_value 라이브 확인" → stale. 회수 라벨은 `recall_memory`(memory_search) 가 frontmatter 의 `reverify_status` 를 결과 dict 에 부착하고 양 포맷터(`recall_core.format_memory_context` ↔ `hooks/memory-recall.py:_format_output`)가 `provenance` 라인과 동형의 conditional 라인으로 렌더(byte-parity). 트리거 = SessionEnd best-effort 증분(sidecar `last_scan` 기반). Layer 5 모순감지(`contradiction_detector`)는 무손상.

**Tech Stack:** Python, pytest/unittest, 기존 hook 시스템. 제약: CC 내부 전용 / 운영비 0(결정론 grep, LLM 없음) / v1 토큰낭비 금지(경고 라벨 1줄·stale 메모리에만) / 두 포맷터 byte-parity 불변 / hot-path(`memory-recall.py`) 회귀 흉터 보호 / 사용자 메모리 frontmatter atomic·body 보존.

**설계 단일 진실원천:** `docs/specs/2026-05-31-phase1-reliability-design.md` (D1~D10), 상위 `docs/specs/2026-05-30-second-brain-roadmap-design.md` §4.2③/§4.3-3/§4.4/§5.

**Scope:** Phase 1 ③(신뢰성 검증)만. ①provenance(v3.6.0)·②효과적회수(v3.7.0)는 완료. 자동 게이트 조정·메모리 본문 auto-edit·Gemma adjudication·file:line 내용 대조는 미구현(D8/§8). install.sh 재배포(트리거 활성화)·GitHub push 는 형 승인 영역(비범위).

---

## File Structure

- Create: `src/reverify.py` — Canonical Facts Registry + verifier + `check_memory_staleness` + frontmatter upsert + `scan_memories`/`maybe_scan_due` (sidecar).
- Create: `src/reverify_cli.py` — `scan` / `list` / `verify-registry` 서브커맨드.
- Modify: `src/memory_search.py` (`recall_memory` prov 부착 블록 ~677-689 — `reverify` 필드 동시 부착).
- Modify: `src/recall_core.py` (`format_memory_context` — stale 경고 라벨 conditional 라인).
- Modify: `hooks/memory-recall.py` (`_format_output` — 동일 라벨, byte-parity).
- Modify: `src/session_memory_end.py` (`main()` best-effort 사슬 끝 — reverify 증분 step).
- Test: `tests/test_reverify.py` (판정 게이트 + BGE 회귀 + upsert + scan + sidecar).
- Test: `tests/test_reverify_cli.py` (CLI 3 서브커맨드).
- Test: `tests/test_recall_core_parity.py` (stale 라벨 parity + ingestion/sanitize 회귀).

각 Task 는 독립 testable. TDD: 실패 테스트 → 확인 → 구현 → 통과 → 회귀 → 커밋.

**테스트 import 관례(확인됨):** `tests/conftest.py` 가 worktree `src/` 를 `sys.path` 최우선 + env 격리. 따라서 `import reverify`, `from reverify import ...` 직접 import 가능. 하이픈 파일 `hooks/memory-recall.py` 는 `tests/test_recall_core_parity.py` 의 기존 `_load_memrecall()` 헬퍼로 로드.

**현행 코드 ground truth(확인됨):** `src/memory_indexer.py:37` `EMBED_URL="http://localhost:8081/embed"`, `:40/:192` 'Arctic' 표기 → embedding_model/port verifier 라이브 통과. stale 토큰 'bge' 는 `src/eval_arctic_ko_ab.py`(HISTORICAL)에만 잔존하나 verifier grep 대상(memory_indexer.py)과 무관.

---

### Task 1: `reverify.py` 코어 — Registry + verifier + `check_memory_staleness` (BGE→Arctic 회귀 게이트)

**Files:**
- Create: `src/reverify.py`
- Test: `tests/test_reverify.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_reverify.py` 생성:

```python
"""Phase 1③ 신뢰성 검증 — stale 자동 감지 테스트."""
import pytest

from reverify import (
    CanonicalFact,
    CANONICAL_FACTS,
    check_memory_staleness,
    verify_registry,
    default_root,
)


def _fake_root(tmp_path):
    """현행 코드 ground truth 모사: arctic 라이브, 8081 라이브, bge 없음."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "memory_indexer.py").write_text(
        'EMBED_URL = "http://localhost:8081/embed"\n# Arctic-Embed-L v2.0 KO\n',
        encoding="utf-8",
    )
    return tmp_path


# --- 핵심 판별 신호 (설계 §2): injected root 로 결정론 ---
def test_stale_when_alias_present_and_current_absent(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("BGE-M3 임베딩이 형 메시지 어디든 0.7+ 매칭한다.", root)
    assert v.status == "stale"
    assert "embedding_model" in v.note


def test_fresh_history_when_both_tokens_present(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("임베딩: arctic-ko v2.0 (Sprint 9 BGE-M3 → 교체)", root)
    assert v.status == "fresh"


def test_fresh_when_only_current_token(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("arctic-ko 8081 정상 동작 확인.", root)
    assert v.status == "fresh"


def test_fresh_when_no_canonical_tokens(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("오늘 카드뉴스 4건 렌더 완료.", root)
    assert v.status == "fresh"


def test_stale_port_8765(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("Arctic-ko 포트는 8765 입니다.", root)
    # 8765 alias 포함 + 8081 미포함 → stale (단 'arctic' 현행토큰 동반이면 면제)
    # 'Arctic-ko' 가 embedding_model current(arctic) 동반이라 embedding_model 면제,
    # 단 embedding_port fact 는 current=8081 미언급이라 stale.
    assert v.status == "stale"
    assert "embedding_port" in v.note


def test_port_8081_not_matched_inside_18081(tmp_path):
    root = _fake_root(tmp_path)
    # 18081 안의 8081 이 current 토큰으로 오매칭되면 안 됨 (word-boundary)
    v = check_memory_staleness("eval 서버는 18081 (BGE-M3 별도 spin-up).", root)
    # current 8081 미포함(18081 은 boundary 로 불일치) + 8765 미포함 → embedding_port no-op
    # bge-m3 포함 + arctic 미포함 → embedding_model stale
    assert v.status == "stale"
    assert "embedding_model" in v.note


def test_verifier_fail_skips_fact(tmp_path):
    # arctic 이 라이브에 없는 root → embedding_model verifier False → 그 fact 판정 skip
    src = tmp_path / "src"
    src.mkdir()
    (src / "memory_indexer.py").write_text("EMBED_URL = nothing here\n", encoding="utf-8")
    v = check_memory_staleness("BGE-M3 임베딩 사용", tmp_path)
    # embedding_model verifier fail → skip, embedding_port verifier 도 8081 없음 → skip
    assert v.status == "fresh"  # 판정 불가 fact 는 stale 로 몰지 않음


# --- 레지스트리 self-check: 실제 repo 코드에서 모든 verifier 통과 (registry 정직성) ---
def test_verify_registry_all_live_on_real_repo():
    """CANONICAL_FACTS 의 모든 current_value 가 라이브 코드에 실재해야 한다.
    실패 = 코드가 또 바뀌었는데 registry 미갱신 (registry stale)."""
    failed = verify_registry(default_root())
    assert failed == [], f"registry stale — verifier fail: {failed}"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_reverify.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'reverify'`)

- [ ] **Step 3: `src/reverify.py` 코어 구현**

`src/reverify.py` 생성:

```python
#!/usr/bin/env python3
"""MindVault v3 Phase 1 ③ — 신뢰성 검증 (stale 자동 감지, over-trust 해소).

메모리의 코드/사실 참조(모델명·포트)를 현행 코드와 결정론적으로 대조해 stale
의심을 판정한다. Layer 5 모순감지(memory vs memory)와 달리 ③은 memory vs 현행
코드. Gemma 미사용 — 운영비 0, 결정론, CI pin 가능.

판별 신호(설계 §2): 메모리가 stale_alias 토큰을 포함하면서 current_value 토큰을
미포함하면 stale 의심. 현행 값을 함께 언급하는 이력 메모리는 면제. verifier 가
current_value 가 라이브 코드에 실재하는지 확인 → registry 자체의 메타-staleness 차단.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


def default_root() -> Path:
    """현행 코드 ground truth root. MV3_REVERIFY_ROOT env 우선, 기본 = repo root."""
    env = os.environ.get("MV3_REVERIFY_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent  # src/reverify.py → repo root


def _grep_present(root: Path, rel_path: str, pattern: str) -> bool:
    """root/rel_path 에 pattern(정규식, 대소문자 무시)이 존재하면 True (없으면 False)."""
    try:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(pattern, text, re.IGNORECASE))


@dataclass(frozen=True)
class CanonicalFact:
    key: str
    current_value: str            # 현행 진실 토큰 (회수 메모리가 이걸 언급하면 면제)
    stale_aliases: tuple          # 현재처럼 주장되면 stale 인 옛 토큰들
    verifier: Callable            # (root: Path) -> bool : current_value 가 라이브?
    description: str = ""


# 초기 facts — 실측 stale 위험 + verifier 라이브 통과 확인 (설계 D3).
# 확장: 형이 summarizer 포트·버전·파일경로 등 한 줄씩 추가 (단 verifier 라이브 통과 필수).
CANONICAL_FACTS = (
    CanonicalFact(
        key="embedding_model",
        current_value="arctic",
        stale_aliases=("bge-m3", "bge_m3"),
        verifier=lambda root: _grep_present(root, "src/memory_indexer.py", r"arctic"),
        description="임베딩 모델 (Sprint 9/14 BGE-M3 → Arctic-ko 교체)",
    ),
    CanonicalFact(
        key="embedding_port",
        current_value="8081",
        stale_aliases=("8765",),
        verifier=lambda root: _grep_present(root, "src/memory_indexer.py", r"(?<!\d)8081(?!\d)"),
        description="임베딩 서버 포트 (Arctic-ko :8081)",
    ),
)


def _contains_token(text: str, token: str) -> bool:
    """대소문자 무시 토큰 포함 검사. 숫자 토큰은 word-boundary(18081 안 8081 오매칭 차단)."""
    t = text.lower()
    tok = token.lower()
    if tok.isdigit():
        return re.search(rf"(?<!\d){re.escape(tok)}(?!\d)", t) is not None
    return tok in t


@dataclass
class StaleVerdict:
    status: str                   # "stale" | "fresh"
    note: str = ""
    findings: List[str] = field(default_factory=list)


def check_memory_staleness(
    text: str, root: Optional[Path] = None, facts=CANONICAL_FACTS
) -> StaleVerdict:
    """메모리 텍스트(frontmatter+body)를 현행 코드와 대조해 stale 판정 (설계 §2).

    각 fact 에 대해: verifier(root) 가 current_value 라이브 확인 못 하면 skip
    (registry stale 의심 → verify_registry). current_value 토큰 동반이면 면제(이력).
    stale_alias 토큰만 있으면 → finding 누적. finding 있으면 stale.
    """
    if root is None:
        root = default_root()
    findings: List[str] = []
    for fact in facts:
        if not fact.verifier(root):
            continue  # current_value 라이브 확인 불가 → 이 fact 로 판정 안 함
        if _contains_token(text, fact.current_value):
            continue  # 현행 값 동반 → 정당한 이력/현행, 면제
        hit = next((a for a in fact.stale_aliases if _contains_token(text, a)), None)
        if hit:
            findings.append(
                f"{fact.key}: '{hit}' 현재형 참조, 현행 {fact.current_value} 미언급"
            )
    if findings:
        return StaleVerdict(status="stale", note="; ".join(findings), findings=findings)
    return StaleVerdict(status="fresh")


def verify_registry(root: Optional[Path] = None, facts=CANONICAL_FACTS) -> List[dict]:
    """각 fact 의 current_value 가 라이브 코드에 실재하는지 self-check.

    반환: verifier fail 한 fact 들 [{key, description}] — registry stale 경고용.
    빈 리스트 = 레지스트리 정상.
    """
    if root is None:
        root = default_root()
    return [
        {"key": f.key, "description": f.description}
        for f in facts
        if not f.verifier(root)
    ]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_reverify.py -v`
Expected: PASS (8건)

- [ ] **Step 5: 회귀 + 커밋**

Run: `python3 -m pytest tests/test_reverify.py -q`
Expected: PASS

```bash
git add src/reverify.py tests/test_reverify.py
git commit -m "feat(reliability): reverify 코어 — Canonical Facts Registry + verifier + check_memory_staleness (BGE→Arctic 회귀 게이트, 결정론)"
```

---

### Task 2: frontmatter upsert (atomic·idempotent·cleanup) + `scan_memories` + sidecar

**Files:**
- Modify: `src/reverify.py` (upsert/write-back/scan/sidecar 추가)
- Test: `tests/test_reverify.py` (append)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_reverify.py` 맨 끝에 추가:

```python
from reverify import (
    upsert_reverify_frontmatter,
    write_back_verdict,
    scan_memories,
    maybe_scan_due,
)
from reverify import StaleVerdict as _SV  # noqa


# --- upsert 순수 함수 ---
def test_upsert_adds_keys_no_frontmatter():
    out = upsert_reverify_frontmatter("본문만 있음", "stale", "n1", "2026-05-31")
    assert out.startswith("---\n")
    assert "reverify_status: stale" in out
    assert "reverify_note: n1" in out
    assert "본문만 있음" in out


def test_upsert_preserves_body_and_existing_keys():
    text = "---\nname: m\ntype: feedback\n---\n\n본문 줄1\n본문 줄2\n"
    out = upsert_reverify_frontmatter(text, "stale", "note", "2026-05-31")
    assert "name: m" in out and "type: feedback" in out
    assert "본문 줄1" in out and "본문 줄2" in out
    assert "reverify_status: stale" in out


def test_upsert_replaces_existing_reverify_keys():
    text = "---\nname: m\nreverify_status: stale\nreverify_note: old\nreverify_checked: 2026-01-01\n---\n\nbody\n"
    out = upsert_reverify_frontmatter(text, "stale", "new", "2026-05-31")
    assert out.count("reverify_status:") == 1
    assert "reverify_note: new" in out
    assert "old" not in out


def test_upsert_note_oneline():
    out = upsert_reverify_frontmatter("body", "stale", "줄1\n줄2", "2026-05-31")
    assert "reverify_note: 줄1 줄2" in out
    assert out.count("reverify_note:") == 1


# --- write-back: atomic, idempotent, cleanup ---
def test_write_back_flags_stale(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("---\nname: m\n---\n\nBGE-M3 임베딩\n", encoding="utf-8")
    wrote = write_back_verdict(p, _SV(status="stale", note="x"), "2026-05-31")
    assert wrote is True
    assert "reverify_status: stale" in p.read_text(encoding="utf-8")


def test_write_back_idempotent(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("---\nname: m\n---\n\nBGE-M3\n", encoding="utf-8")
    write_back_verdict(p, _SV(status="stale", note="x"), "2026-05-31")
    first = p.read_text(encoding="utf-8")
    wrote2 = write_back_verdict(p, _SV(status="stale", note="x"), "2026-06-09")  # 날짜 달라도
    assert wrote2 is False                       # status/note 불변 → no-write
    assert p.read_text(encoding="utf-8") == first  # checked churn 없음


def test_write_back_cleans_up_when_fresh(tmp_path):
    p = tmp_path / "m.md"
    p.write_text(
        "---\nname: m\nreverify_status: stale\nreverify_note: x\nreverify_checked: 2026-05-31\n---\n\nbody\n",
        encoding="utf-8",
    )
    wrote = write_back_verdict(p, _SV(status="fresh"), "2026-06-09")
    assert wrote is True
    txt = p.read_text(encoding="utf-8")
    assert "reverify_status" not in txt   # stale→fresh 전이 시 키 제거
    assert "name: m" in txt and "body" in txt


def test_write_back_noop_when_fresh_and_no_flag(tmp_path):
    p = tmp_path / "m.md"
    orig = "---\nname: m\n---\n\narctic-ko 정상\n"
    p.write_text(orig, encoding="utf-8")
    wrote = write_back_verdict(p, _SV(status="fresh"), "2026-06-09")
    assert wrote is False
    assert p.read_text(encoding="utf-8") == orig  # fresh 메모리 무손상


# --- scan_memories + sidecar ---
def _fake_root_for_scan(tmp_path):
    src = tmp_path / "code" / "src"
    src.mkdir(parents=True)
    (src / "memory_indexer.py").write_text(
        'EMBED_URL = "http://localhost:8081/embed"\n# Arctic\n', encoding="utf-8"
    )
    return tmp_path / "code"


def test_scan_flags_only_stale(tmp_path, monkeypatch):
    root = _fake_root_for_scan(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "stale.md").write_text("---\nname: s\n---\n\nBGE-M3 임베딩\n", encoding="utf-8")
    (mem / "fresh.md").write_text("---\nname: f\n---\n\narctic-ko 동작\n", encoding="utf-8")
    (mem / "hist.md").write_text("---\nname: h\n---\n\nBGE-M3 → arctic 교체\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("index\n", encoding="utf-8")
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    stats = scan_memories(mem, root=root, checked="2026-05-31")
    assert stats["flagged"] == 1
    assert "reverify_status: stale" in (mem / "stale.md").read_text(encoding="utf-8")
    assert "reverify_status" not in (mem / "fresh.md").read_text(encoding="utf-8")
    assert "reverify_status" not in (mem / "hist.md").read_text(encoding="utf-8")


def test_maybe_scan_due_first_run_then_skips(tmp_path, monkeypatch):
    root = _fake_root_for_scan(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "s.md").write_text("---\nname: s\n---\n\nBGE-M3\n", encoding="utf-8")
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MV3_REVERIFY_ROOT", str(root))
    s1 = maybe_scan_due(mem, interval_days=7)
    assert s1 is not None and s1["flagged"] == 1   # 첫 실행 → scan
    s2 = maybe_scan_due(mem, interval_days=7)
    assert s2 is None                               # sidecar 최신 → skip
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_reverify.py -k "upsert or write_back or scan or maybe_scan" -v`
Expected: FAIL (`ImportError: cannot import name 'upsert_reverify_frontmatter'`)

- [ ] **Step 3: I/O + scan + sidecar 구현**

`src/reverify.py` 끝에 추가 (import 에 `import json`, `import time` 추가):

```python
import json
import time

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_REVERIFY_KEYS = ("reverify_status", "reverify_checked", "reverify_note")
REVERIFY_INTERVAL_DAYS = 7


def _data_dir() -> Path:
    return Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()


def _sidecar_path() -> Path:
    return _data_dir() / "reverify_state.json"


def _oneline(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\r", " ").replace("\n", " ")).strip()


def upsert_reverify_frontmatter(text: str, status: str, note: str, checked: str) -> str:
    """frontmatter 에 reverify_* 키 upsert (순수 함수). 본문·기존 키 보존, reverify_* 만 교체.

    frontmatter 없으면 생성. note 는 단일 라인 정규화 (라인 파서 호환).
    """
    new_lines = [f"reverify_status: {status}", f"reverify_checked: {checked}"]
    note1 = _oneline(note)
    if note1:
        new_lines.append(f"reverify_note: {note1}")
    m = _FM_RE.match(text)
    if not m:
        return "---\n" + "\n".join(new_lines) + "\n---\n\n" + text
    kept = [
        ln for ln in m.group(1).split("\n")
        if not any(ln.lstrip().startswith(k + ":") for k in _REVERIFY_KEYS)
    ]
    merged = "\n".join(kept + new_lines)
    return "---\n" + merged + "\n---\n" + text[m.end():]


def _strip_reverify_frontmatter(text: str) -> str:
    """frontmatter 에서 reverify_* 키 제거 (stale→fresh cleanup). frontmatter 없으면 원본."""
    m = _FM_RE.match(text)
    if not m:
        return text
    kept = [
        ln for ln in m.group(1).split("\n")
        if not any(ln.lstrip().startswith(k + ":") for k in _REVERIFY_KEYS)
    ]
    return "---\n" + "\n".join(kept) + "\n---\n" + text[m.end():]


def _current_reverify_status(text: str) -> Optional[str]:
    m = _FM_RE.match(text)
    if not m:
        return None
    mm = re.search(r"^reverify_status:\s*(\S+)", m.group(1), re.MULTILINE)
    return mm.group(1) if mm else None


def _current_reverify_note(text: str) -> str:
    m = _FM_RE.match(text)
    if not m:
        return ""
    mm = re.search(r"^reverify_note:\s*(.*)$", m.group(1), re.MULTILINE)
    return mm.group(1).strip() if mm else ""


def _atomic_write(path: Path, content: str) -> bool:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_back_verdict(path: Path, verdict: StaleVerdict, checked: str) -> bool:
    """판정 결과를 파일 frontmatter 에 atomic 반영. 반환: 실제로 썼으면 True.

    - stale: status/note 변화 있을 때만 upsert (idempotent — 불변이면 checked churn 없이 skip).
    - fresh: 기존 flag 있으면 제거(cleanup), 없으면 no-op (fresh 메모리 무손상).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    cur_status = _current_reverify_status(text)
    if verdict.status == "stale":
        if cur_status == "stale" and _current_reverify_note(text) == _oneline(verdict.note):
            return False  # idempotent
        return _atomic_write(
            path, upsert_reverify_frontmatter(text, "stale", verdict.note, checked)
        )
    # fresh
    if cur_status is None:
        return False  # 무flag fresh → no-op
    return _atomic_write(path, _strip_reverify_frontmatter(text))


def _collect_memory_files(mem_dir: Path) -> List[Path]:
    """*.md + _procedural/*.md, MEMORY.md·_staged 제외 (provenance_backfill 와 동일 범위)."""
    files: List[Path] = []
    for base in (mem_dir, mem_dir / "_procedural"):
        if not base.is_dir():
            continue
        for p in base.glob("*.md"):
            if p.name == "MEMORY.md" or any(part == "_staged" for part in p.parts):
                continue
            files.append(p)
    return sorted(files)


def scan_memories(
    mem_dir: Path, root: Optional[Path] = None, checked: Optional[str] = None
) -> dict:
    """mem_dir 의 모든 메모리를 현행 코드와 대조 + frontmatter flag 갱신.

    반환: {flagged, cleared, checked(=처리 파일수), total}. sidecar last_scan 갱신.
    """
    if root is None:
        root = default_root()
    if checked is None:
        checked = time.strftime("%Y-%m-%d")
    flagged = cleared = processed = 0
    files = _collect_memory_files(mem_dir)
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        processed += 1
        verdict = check_memory_staleness(text, root)
        had_flag = _current_reverify_status(text) is not None
        wrote = write_back_verdict(p, verdict, checked)
        if verdict.status == "stale" and wrote:
            flagged += 1
        elif verdict.status == "fresh" and had_flag and wrote:
            cleared += 1
    _write_sidecar()
    return {"flagged": flagged, "cleared": cleared, "checked": processed, "total": len(files)}


def _read_sidecar_last_scan() -> Optional[float]:
    try:
        d = json.loads(_sidecar_path().read_text(encoding="utf-8"))
        return float(d.get("last_scan_epoch"))
    except (OSError, ValueError, TypeError):
        return None


def _write_sidecar() -> None:
    sc = _sidecar_path()
    try:
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(
            json.dumps(
                {"last_scan_epoch": time.time(), "last_scan": time.strftime("%Y-%m-%dT%H:%M:%S")}
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def maybe_scan_due(mem_dir: Path, interval_days: int = REVERIFY_INTERVAL_DAYS) -> Optional[dict]:
    """sidecar last_scan 이 interval 보다 오래됐(또는 부재)으면 scan, 아니면 None.

    SessionEnd best-effort 트리거용 — 사실상 주 1회.
    """
    last = _read_sidecar_last_scan()
    if last is not None and (time.time() - last) < interval_days * 86400:
        return None
    return scan_memories(mem_dir)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_reverify.py -v`
Expected: PASS (전체 ~19건)

- [ ] **Step 5: 회귀 + 커밋**

Run: `python3 -m pytest tests/test_reverify.py -q`
Expected: PASS

```bash
git add src/reverify.py tests/test_reverify.py
git commit -m "feat(reliability): frontmatter upsert(atomic·idempotent·cleanup) + scan_memories + sidecar 증분 (flag-only-stale)"
```

---

### Task 3: `reverify_cli.py` — scan / list / verify-registry

**Files:**
- Create: `src/reverify_cli.py`
- Test: `tests/test_reverify_cli.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_reverify_cli.py` 생성:

```python
"""reverify_cli 서브커맨드 테스트."""
import json

from reverify_cli import main as cli_main


def _setup(tmp_path, monkeypatch):
    code = tmp_path / "code"
    (code / "src").mkdir(parents=True)
    (code / "src" / "memory_indexer.py").write_text(
        'EMBED_URL = "http://localhost:8081/embed"\n# Arctic\n', encoding="utf-8"
    )
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "stale.md").write_text("---\nname: s\n---\n\nBGE-M3 임베딩\n", encoding="utf-8")
    (mem / "fresh.md").write_text("---\nname: f\n---\n\narctic-ko\n", encoding="utf-8")
    monkeypatch.setenv("MV3_REVERIFY_ROOT", str(code))
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    return mem


def test_cli_scan_flags(tmp_path, monkeypatch, capsys):
    mem = _setup(tmp_path, monkeypatch)
    rc = cli_main(["scan", str(mem), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["flagged"] == 1
    assert "reverify_status: stale" in (mem / "stale.md").read_text(encoding="utf-8")


def test_cli_list_shows_stale(tmp_path, monkeypatch, capsys):
    mem = _setup(tmp_path, monkeypatch)
    cli_main(["scan", str(mem)])
    capsys.readouterr()
    rc = cli_main(["list", str(mem)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stale.md" in out
    assert "fresh.md" not in out


def test_cli_verify_registry_ok_on_real_repo(monkeypatch, capsys):
    monkeypatch.delenv("MV3_REVERIFY_ROOT", raising=False)  # 실제 repo root 사용
    rc = cli_main(["verify-registry"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out   # capsys 는 1회만 호출 (버퍼 소비)


def test_cli_verify_registry_detects_stale_registry(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad"
    (bad / "src").mkdir(parents=True)
    (bad / "src" / "memory_indexer.py").write_text("nothing\n", encoding="utf-8")
    monkeypatch.setenv("MV3_REVERIFY_ROOT", str(bad))
    rc = cli_main(["verify-registry"])
    assert rc == 1   # verifier fail → 비정상 exit
    assert "embedding_model" in capsys.readouterr().out
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_reverify_cli.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'reverify_cli'`)

- [ ] **Step 3: `src/reverify_cli.py` 구현**

`src/reverify_cli.py` 생성:

```python
#!/usr/bin/env python3
"""Phase 1③ 신뢰성 검증 CLI — stale 재검증 scan / list / 레지스트리 self-check.

usage:
  python -m src.reverify_cli scan <memory_dir> [--json]
  python -m src.reverify_cli list <memory_dir>
  python -m src.reverify_cli verify-registry
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reverify import (  # noqa: E402
    scan_memories,
    verify_registry,
    default_root,
    _collect_memory_files,
    _current_reverify_status,
    _current_reverify_note,
)


def _cmd_scan(args) -> int:
    stats = scan_memories(Path(args.memory_dir).expanduser())
    if args.json:
        json.dump(stats, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(
            f"scan: flagged={stats['flagged']} cleared={stats['cleared']} "
            f"checked={stats['checked']}/{stats['total']}"
        )
    return 0


def _cmd_list(args) -> int:
    n = 0
    for p in _collect_memory_files(Path(args.memory_dir).expanduser()):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _current_reverify_status(text) == "stale":
            n += 1
            print(f"{p.name}: {_current_reverify_note(text)}")
    if n == 0:
        print("stale 메모리 없음")
    return 0


def _cmd_verify_registry(args) -> int:
    failed = verify_registry(default_root())
    if not failed:
        print("registry verify-registry: OK (모든 fact 라이브 통과)")
        return 0
    print("registry STALE — verifier fail:")
    for f in failed:
        print(f"  - {f['key']}: {f['description']}")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reverify_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="메모리 stale 재검증 + frontmatter flag 갱신")
    p_scan.add_argument("memory_dir")
    p_scan.add_argument("--json", action="store_true")
    p_scan.set_defaults(func=_cmd_scan)

    p_list = sub.add_parser("list", help="현재 stale flag 된 메모리 나열")
    p_list.add_argument("memory_dir")
    p_list.set_defaults(func=_cmd_list)

    p_vr = sub.add_parser("verify-registry", help="레지스트리 self-check (current_value 라이브?)")
    p_vr.set_defaults(func=_cmd_verify_registry)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_reverify_cli.py -v`
Expected: PASS (4건)

- [ ] **Step 5: 회귀 + 커밋**

Run: `python3 -m pytest tests/test_reverify.py tests/test_reverify_cli.py -q`
Expected: PASS

```bash
git add src/reverify_cli.py tests/test_reverify_cli.py
git commit -m "feat(reliability): reverify_cli — scan / list / verify-registry (레지스트리 self-check)"
```

---

### Task 4: 회수 경고 라벨 (양 포맷터 byte-parity) + `recall_memory` reverify 부착

**Files:**
- Modify: `src/memory_search.py` (prov 부착 블록 ~677-689)
- Modify: `src/recall_core.py` (`format_memory_context` ~93-100)
- Modify: `hooks/memory-recall.py` (`_format_output` ~338-345)
- Test: `tests/test_recall_core_parity.py` (append)

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_recall_core_parity.py` 맨 끝에 추가:

```python
def test_stale_label_present_and_parity():
    """reverify_status=stale 메모리 회수 시 양 포맷터가 경고 라벨을 byte-동일하게 렌더."""
    import recall_core
    mr = _load_memrecall()
    sample = [{
        "name": "feedback-no-v1-token-waste", "source": ["vec"],
        "description": "토큰낭비 금지", "snippet": "BGE-M3 매칭", "score": 0.7,
        "provenance": {"source_type": "session", "source_ref": "abc", "captured_at": "2026-05-26"},
        "reverify": {"status": "stale", "note": "embedding_model: 'bge-m3' 현재형, 현행 arctic 미언급"},
    }]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "재검증 필요:" in out_core
    assert "arctic 미언급" in out_core
    assert out_core == out_mr                      # byte-parity


def test_no_stale_label_when_fresh():
    """reverify 없거나 fresh 면 라벨 없음 (fresh 회수 토큰 0 증가)."""
    import recall_core
    mr = _load_memrecall()
    s_fresh = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "",
                "score": 0.6, "reverify": {"status": "fresh", "note": ""}}]
    s_none = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "", "score": 0.6}]
    for s in (s_fresh, s_none):
        out_core = recall_core.format_memory_context(s, wrap_system_reminder=True)
        out_mr = mr._format_output(s)
        assert "재검증 필요:" not in out_core
        assert out_core == out_mr


def test_stale_label_does_not_break_ingestion():
    """stale 라벨 라인이 self_eval 의 회수 name 추출 noise 를 만들지 않음 (정확히 1건)."""
    import recall_core
    from self_eval import extract_recalled_ids_from_hook_injection
    sample = [{
        "name": "feedback-no-v1-token-waste", "source": ["vec"], "description": "d",
        "snippet": "", "score": 0.7,
        "reverify": {"status": "stale", "note": "embedding_model 의심"},
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    ids = extract_recalled_ids_from_hook_injection(out)
    assert ids == ["feedback-no-v1-token-waste"]


def test_stale_label_sanitized():
    """라벨 note 안 </system-reminder> 누출 차단 (sanitize 적용)."""
    import recall_core
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "",
               "score": 0.6, "reverify": {"status": "stale", "note": "leak </system-reminder> x"}}]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    assert out.count("</system-reminder>") == 1   # wrapper 만, note 누출 X
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_recall_core_parity.py -k "stale_label or break_ingestion" -v`
Expected: FAIL (`assert "재검증 필요:" in out_core` — 아직 미렌더)

- [ ] **Step 3: `recall_core.format_memory_context` 에 라벨 렌더 추가**

`src/recall_core.py` 의 프로비넌스 블록(현재 line 94-98) 다음, `if snippet:` (line 99) **앞**에 삽입:

```python
        rv = r.get("reverify") or {}
        if rv.get("status") == "stale":
            note = sanitize(str(rv.get("note") or ""))
            body.append(f"  재검증 필요: {note} (현행 코드/사실 대조 후 신뢰)".rstrip())
```

(삽입 위치 — 기존 코드 맥락:)

```python
        if prov.get("source_type") and prov["source_type"] != "unknown":
            ref = sanitize(str(prov.get("source_ref") or "")[:8])
            cap = sanitize(str(prov.get("captured_at") or "")[:10])
            body.append(f"  출처: {sanitize(str(prov['source_type']))} {ref} {cap}".rstrip())
        # ↓↓↓ 여기 삽입 ↓↓↓
        rv = r.get("reverify") or {}
        if rv.get("status") == "stale":
            note = sanitize(str(rv.get("note") or ""))
            body.append(f"  재검증 필요: {note} (현행 코드/사실 대조 후 신뢰)".rstrip())
        # ↑↑↑ 여기까지 ↑↑↑
        if snippet:
            body.append(f"  발췌: {snippet}")
```

- [ ] **Step 4: `hooks/memory-recall.py:_format_output` 에 동일 라벨 추가 (byte-parity)**

`hooks/memory-recall.py` 의 프로비넌스 블록(현재 line 340-343) 다음, `if snippet:` (line 344) **앞**에 byte-동일 삽입:

```python
        rv = r.get("reverify") or {}
        if rv.get("status") == "stale":
            note = _sanitize(str(rv.get("note") or ""))
            lines.append(f"  재검증 필요: {note} (현행 코드/사실 대조 후 신뢰)".rstrip())
```

> ⚠ byte-parity: `recall_core` 는 `sanitize`/`body.append`, hook 은 `_sanitize`/`lines.append` 만 다르고 **출력 문자열 리터럴은 완전 동일**해야 한다. `"  재검증 필요: {note} (현행 코드/사실 대조 후 신뢰)"` (앞 공백 2칸, 토큰 동일). parity 테스트가 한쪽만 바뀌면 잡는다.

- [ ] **Step 5: `recall_memory` 가 frontmatter 의 reverify_status 를 결과에 부착**

`src/memory_search.py` 의 prov 부착 루프(현재 line 677-689)를 다음으로 교체:

```python
        for r in results:
            prov = {"source_type": "unknown", "source_ref": None, "captured_at": None}
            reverify = {"status": None, "note": None}
            try:
                fm, _ = parse_frontmatter(Path(r["path"]).read_text(encoding="utf-8"))
                prov["source_type"] = fm.get("source_type") or "unknown"
                _sr = fm.get("source_ref")
                prov["source_ref"] = _sr.isoformat() if hasattr(_sr, "isoformat") else (str(_sr) if _sr not in (None, "") else None)
                _cap = fm.get("staged_at") or fm.get("captured_at")
                prov["captured_at"] = _cap.isoformat() if hasattr(_cap, "isoformat") else (str(_cap) if _cap else None)
                _rvs = fm.get("reverify_status")
                if _rvs:
                    reverify["status"] = str(_rvs)
                    _note = fm.get("reverify_note")
                    reverify["note"] = str(_note) if _note not in (None, "") else None
            except (OSError, UnicodeDecodeError, KeyError):
                pass
            r["provenance"] = prov
            r["reverify"] = reverify
        return results
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_recall_core_parity.py -v`
Expected: PASS (기존 + 신규 4건)

- [ ] **Step 7: 회귀 + 커밋**

Run: `python3 -m pytest tests/test_recall_core_parity.py tests/test_memory_hook.py tests/test_memory_search.py tests/test_compact_reinjection.py tests/test_memory_recall_deprecated.py -q`
Expected: PASS (기존 prov-only 샘플은 `reverify` 부재 → `.get` 으로 라벨 무렌더 → byte-parity 유지; compact 재주입은 `format_memory_context` 경유라 라벨 자동 전파, system-reminder 블록 스킵만 검사)

```bash
git add src/memory_search.py src/recall_core.py hooks/memory-recall.py tests/test_recall_core_parity.py
git commit -m "feat(reliability): 회수 시 stale 경고 라벨 (양 포맷터 byte-parity) + recall_memory reverify 부착"
```

---

### Task 5: SessionEnd 증분 트리거 (best-effort) + e2e + 전체 회귀

**Files:**
- Modify: `src/session_memory_end.py` (`main()` best-effort 사슬 끝 ~378)
- Test: `tests/test_reverify.py` (e2e append)

- [ ] **Step 1: 실패 테스트 작성 (e2e + 트리거 silent-fail)**

`tests/test_reverify.py` 맨 끝에 추가:

```python
def test_e2e_bge_memory_flagged_then_recall_warns(tmp_path, monkeypatch):
    """완료 게이트 e2e: BGE 주장 메모리 → scan flag → 회수 출력에 경고 라벨."""
    import recall_core
    root = _fake_root_for_scan(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    bge = mem / "feedback_no_v1_token_waste.md"
    bge.write_text("---\nname: no-v1-token-waste\n---\n\nBGE-M3 임베딩이 0.7+ 매칭.\n", encoding="utf-8")
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    scan_memories(mem, root=root, checked="2026-05-31")
    # 회수 결과를 모사 (frontmatter 에서 reverify_status 읽힌 상태)
    fm_text = bge.read_text(encoding="utf-8")
    assert "reverify_status: stale" in fm_text
    sample = [{
        "name": "no-v1-token-waste", "source": ["vec"], "description": "d",
        "snippet": "", "score": 0.7,
        "reverify": {"status": "stale", "note": "embedding_model: 'bge-m3' 현재형, 현행 arctic 미언급"},
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    assert "재검증 필요:" in out and "arctic 미언급" in out


def test_session_end_reverify_step_silent_on_error(monkeypatch):
    """SessionEnd 의 reverify step 이 예외에도 main 을 죽이지 않음 (best-effort)."""
    import importlib
    sme = importlib.import_module("session_memory_end")
    # maybe_scan_due 가 던져도 main 의 try/except 가 삼켜야 한다 — 구조 검증.
    assert hasattr(sme, "main")
```

- [ ] **Step 2: 테스트 실패/구조 확인**

Run: `python3 -m pytest tests/test_reverify.py -k "e2e or session_end" -v`
Expected: `test_e2e...` PASS (Task2/4 구현이 이미 올바르면 — characterization), `test_session_end...` PASS (구조). e2e 가 FAIL 이면 Task2/4 회귀.

- [ ] **Step 3: SessionEnd 증분 step 배선**

`src/session_memory_end.py` 의 `main()` 에서 alias_sync 블록(현재 line 369-378) 다음, `return 0`(line 379) **앞**에 삽입:

```python
        # Phase 1③ (reliability, 2026-05-31): stale 재검증 증분. sidecar last_scan
        # 이 REVERIFY_INTERVAL_DAYS 보다 오래됐을 때만 결정론 grep scan (LLM 없음 —
        # 운영비 0). best-effort silent-fail — recall hot-path 무관, 다음 SessionEnd
        # 재시도 가능. flag-only-stale 이라 fresh 메모리 무손상.
        try:
            from reverify import maybe_scan_due
            rstat = maybe_scan_due(MEMORY_DIR)
            if rstat is not None:
                _debug(
                    f"reverify scan flagged={rstat.get('flagged', 0)} "
                    f"cleared={rstat.get('cleared', 0)} "
                    f"checked={rstat.get('checked', 0)}/{rstat.get('total', 0)}"
                )
        except Exception as e:
            _debug(f"reverify skipped: {type(e).__name__}: {e}")
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_reverify.py tests/test_session_memory_end_integration.py -v`
Expected: PASS

- [ ] **Step 5: 전체 회귀 확인**

Run: `python3 -m pytest -q`
Expected: 기존 baseline **678 passed, 1 skipped, 25 subtests** + 신규 ≈ **708+ passed** (Task1 8 + Task2 ~11 + Task3 4 + Task4 4 + Task5 2 = ~29건). `contradiction_detector`(Layer 5) 회귀 0.

> `test_e2e_4_hook_performance`(avg<150ms)는 동시부하 시 flake. FAIL 시 격리 재실행:
> `python3 -m pytest tests/test_e2e.py -k hook_performance -v` (격리 통과 = 코드 회귀 아님)

- [ ] **Step 6: 커밋**

```bash
git add src/session_memory_end.py tests/test_reverify.py
git commit -m "feat(reliability): SessionEnd 증분 reverify 트리거 (best-effort sidecar) + e2e BGE→Arctic 게이트"
```

---

## 완료 게이트 (설계 §5 / spec §4.3-3 대응)

- [ ] **BGE→Arctic 회귀 (1건 이상 사전 차단)**: `check_memory_staleness` — BGE-as-current → stale / Arctic-fresh → fresh / 이력(양 토큰) → 면제 (Task 1 + e2e Task 5)
- [ ] **회수 end-to-end**: stale 메모리 회수 시 양 포맷터 경고 라벨 + byte-parity + ②self-check·"회수 노트:"·sanitize·RECALLED_NAME_RE 회귀 무손상 (Task 4)
- [ ] **메커니즘**: `reverify_cli` scan(atomic·idempotent·cleanup) / list / verify-registry(레지스트리 self-check) (Task 2/3)
- [ ] **회귀 무손상**: 전체 pytest 통과(678 + 신규) + Layer 5 contradiction 무손상 (Task 5)

## 운영 절차 (배포 후 — 배포는 형 승인 영역)

활성화(install.sh 재배포 — 형 승인) 후:

```bash
# 1) 레지스트리 정직성 확인 (current_value 가 라이브?)
python3 src/reverify_cli.py verify-registry

# 2) 메모리 전체 stale 재검증 + flag
python3 src/reverify_cli.py scan ~/.claude/projects/<home-slug>/memory --json

# 3) 현재 stale flag 된 메모리 확인
python3 src/reverify_cli.py list ~/.claude/projects/<home-slug>/memory
```

SessionEnd 증분(주 1회)이 활성화되면 (2)는 자동. 형은 (3)으로 stale 메모리를 보고 본문 수정 → 다음 scan 이 flag 제거(cleanup).

## 비범위 (별도 / 형 승인)

- Gemma adjudication(history-vs-current 모호 케이스) — §2 결정론 신호로 충분, deferred.
- file:line 내용 대조 — 광범위 false-positive, Phase 2+.
- 회수 임계값 auto-tune (D8) — 영구 미구현.
- 메모리 본문 auto-edit / auto-delete (D8) — 미구현(경고만, reverify_* 메타만 기록).
- `src/memory_search.py:516` docstring 의 stale "BGE-M3" 표기 정정 — 코드 주석 staleness, ③ 범위 밖(형 지시 시 1줄 fix).
- install.sh 재배포(트리거 활성화)·GitHub push/tag/release — 형 승인 영역.
- 모순감지(Layer 5) 변경 — 무손상 유지.
