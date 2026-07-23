"""v3.2.7 회귀 — production state pollution 방지.

v3.2.6 sweep 에서 debug.log 에 가짜 "disk full" 7건 박힌 사례 (test_procedural_slot
mock OSError 가 module-level production DEBUG_LOG 로 leak). 본 회귀가:

1. conftest 가 env var 를 tmp 로 set 했는지 verify
2. **worktree** src 모듈의 path constant 가 env var 를 따르는지 verify
3. 실제 _debug() 호출이 production log 가 아닌 tmp log 로 가는지 verify

검증 대상은 **worktree 본** — full pytest run 중 다른 테스트가 production deploy
경로 (`~/.claude/scripts/mindvault`) 를 sys.modules 에 캐싱할 수 있으므로
spec_from_file_location 으로 명시 로드. (메모리 [[feedback-test-production-path-pollution]]
의 동일 패턴)
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parent.parent
_WORKTREE_SRC = _WORKTREE_ROOT / "src"
_WORKTREE_HOOKS = _WORKTREE_ROOT / "hooks"


def _load_worktree_module(name: str, src_dir: Path = _WORKTREE_SRC):
    """worktree 본을 명시 로드. sys.modules 캐싱 무관."""
    path = src_dir / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_worktree_isolation_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_env_var_set_by_conftest():
    """conftest.py 가 path env var 를 tmp 로 강제 (MV3_MEMORY_DIR 포함 — Phase 1③ audit)."""
    for var in (
        "MV3_DATA_DIR", "MV3_PROJECTS_ROOT", "MV3_HOOKS_DIR", "MV3_SCRIPTS_DIR",
        "MV3_MEMORY_DIR", "MV3_PROJECTS_DIR",
    ):
        val = os.environ.get(var, "")
        assert val, f"{var} not set"
        assert "mv3-pytest-isolation" in val, f"{var}={val} is not tmp"
    # MV3_EXTRA_MEMORY_DIRS 는 비어 있어야 함 (사용자 셸의 handoff 등 실제 dir 누출 차단)
    assert os.environ.get("MV3_EXTRA_MEMORY_DIRS", "") == "", (
        "MV3_EXTRA_MEMORY_DIRS 가 격리 안 됨 — 실제 dir 누출 위험"
    )
    # 서버 게이트 env hermetic — 사용자 셸의 MV3_GEMMA_INTENT=1 누출 시 통합 hook
    # subprocess 테스트가 실제 Gemma 호출. conftest 가 "0" 으로 강제해야 한다.
    assert os.environ.get("MV3_GEMMA_INTENT") == "0", (
        "MV3_GEMMA_INTENT 가 격리 안 됨 — 테스트가 실제 Gemma(:8080) 호출 위험"
    )


def test_memory_dir_isolated_from_real_memory():
    """Phase 1③ audit 회귀 — 테스트 중 session_memory_end.MEMORY_DIR 가 **실제**
    메모리 dir 이면 안 된다. 사용자 셸의 MV3_MEMORY_DIR=<real> export 를 conftest 가
    격리하지 못하면, SessionEnd reverify scan 을 타는 테스트(test_index_sync 등)가
    실제 메모리 frontmatter 를 변경한다(2026-05-31 실측 사고).
    """
    real_memory = Path("~/.claude/projects/-Users-yonghaekim/memory").expanduser()
    tmp_memory = Path(os.environ["MV3_MEMORY_DIR"])

    sme = _load_worktree_module("session_memory_end")
    assert sme.MEMORY_DIR == tmp_memory, (
        f"session_memory_end.MEMORY_DIR={sme.MEMORY_DIR} != 격리 tmp {tmp_memory}"
    )
    assert sme.MEMORY_DIR != real_memory, (
        f"session_memory_end.MEMORY_DIR 가 실제 메모리 dir 을 가리킴 — 테스트가 "
        f"사용자 데이터를 변경할 수 있다 ({sme.MEMORY_DIR})"
    )
    # 실제 메모리 dir 의 부모 슬롯도 아니어야 (STAGED_DIR 등 하위 쓰기 방지)
    assert real_memory not in sme.MEMORY_DIR.parents and sme.MEMORY_DIR not in real_memory.parents


def test_src_modules_honor_env_var():
    """worktree src 모듈의 path constant 가 MV3_DATA_DIR env var 를 따른다."""
    expected_base = Path(os.environ["MV3_DATA_DIR"])

    for name, attrs in [
        ("session_memory_end", ["DEBUG_LOG"]),
        ("session_memory", ["DEBUG_LOG", "CACHE_DIR"]),
        ("memory_search", ["DB_PATH", "DEBUG_LOG", "ALIAS_INDEX_PATH"]),
        ("extractor_cache", ["CACHE_DB", "DEBUG_LOG"]),
        ("turns_cache", ["CACHE_DB", "DEBUG_LOG"]),
        ("query_intent", ["_DEBUG_LOG", "_GEMMA_CACHE_DB"]),
        ("memory_review_cli", ["DEBUG_LOG"]),
        ("memory_indexer", ["DEBUG_LOG", "DB_PATH"]),
        ("alias_generator", ["DEBUG_LOG", "INDEX_PATH"]),
        ("extractor_stats_cli", ["DEBUG_LOG"]),
        ("dedup_cli", ["DEBUG_LOG"]),
        ("memory_compiler", ["DEBUG_LOG"]),
        ("memory_extractor", ["DEBUG_LOG"]),
        ("sources_cli", ["CONFIG_PATH"]),
        ("indexer", ["DEBUG_LOG", "DB_PATH"]),
        ("search", ["DB_PATH", "DEBUG_LOG"]),
        ("self_eval", ["DEFAULT_METRICS", "DEBUG_LOG"]),
        ("eval_top3_domain", ["DB_PATH"]),
    ]:
        mod = _load_worktree_module(name)
        for attr in attrs:
            path = getattr(mod, attr)
            assert path.parent == expected_base, (
                f"worktree {name}.{attr}={path} parent != {expected_base}"
            )


def test_src_modules_honor_projects_root_env():
    """PROJECTS_ROOT 도 MV3_PROJECTS_ROOT env var 를 따른다."""
    expected_projects = Path(os.environ["MV3_PROJECTS_ROOT"])

    for name, attr in [
        ("session_memory_end", "PROJECTS_ROOT"),
        ("indexer", "PROJECTS_ROOT"),
        ("alias_generator", "PROJECTS_ROOT"),
        ("backfill_cli", "PROJECTS_ROOT"),
        ("self_eval", "DEFAULT_PROJECTS_ROOT"),
    ]:
        mod = _load_worktree_module(name)
        path = getattr(mod, attr)
        assert path == expected_projects, (
            f"worktree {name}.{attr}={path} != {expected_projects}"
        )


def test_discover_memory_dirs_honors_env_var():
    """v3.2.7 codex Cat 7 — _discover_memory_dirs 가 module-level hardcoded
    가 아닌 env var 를 참조해야 함. memory_indexer / memory-recall hook
    동일 패턴."""
    expected_projects = Path(os.environ["MV3_PROJECTS_ROOT"])

    mi = _load_worktree_module("memory_indexer")
    # DEFAULT_MEMORY_DIRS 는 import 시점 evaluation — env var 가 set 됐을 때
    # tmp projects 가 비어있으므로 empty list 일 수 있다. 함수 자체가 env var 를
    # 보는지가 핵심.
    dirs = mi._discover_memory_dirs()
    assert isinstance(dirs, list)
    # tmp/projects 에는 *memory* 하위 디렉토리 없으므로 빈 list. production
    # projects 를 잡지 않았다는 게 핵심.
    for d in dirs:
        assert str(d).startswith(str(expected_projects)), (
            f"memory_indexer._discover_memory_dirs() returned {d} outside MV3_PROJECTS_ROOT"
        )
    # (bge_m3 정리 2026-06-02: eval_arctic_ko_ab.py 삭제 — 같은 검증 블록 제거.)


def test_scripts_dir_env_var_used_in_bootstrap_fallback():
    """v3.2.7 codex Cat 1 — session_memory.py / session_memory_end.py /
    extractor_stats_cli.py 의 bootstrap fallback path 가 MV3_SCRIPTS_DIR
    env var 우선 참조. (테스트 환경에서 production scripts 가리키면 안 됨)"""
    expected_scripts = Path(os.environ["MV3_SCRIPTS_DIR"])

    # session_memory.py 는 import 시 indexer subprocess Popen 만 — module
    # level 에서 보이지는 않으므로 source code 검증.
    sm_src = (_WORKTREE_SRC / "session_memory.py").read_text()
    assert 'os.environ.get("MV3_SCRIPTS_DIR"' in sm_src, (
        "session_memory.py 가 MV3_SCRIPTS_DIR env var 참조 안 함"
    )

    sme_src = (_WORKTREE_SRC / "session_memory_end.py").read_text()
    assert 'os.environ.get("MV3_SCRIPTS_DIR"' in sme_src

    esc_src = (_WORKTREE_SRC / "extractor_stats_cli.py").read_text()
    assert 'environ.get("MV3_SCRIPTS_DIR"' in esc_src

    # hooks/memory-recall.py 의 SCRIPTS_DIRS 도 env var 따름
    mr = _load_worktree_module("memory-recall", src_dir=_WORKTREE_HOOKS)
    # SCRIPTS_DIRS 첫 항목이 MV3_SCRIPTS_DIR 이어야 함
    assert mr.SCRIPTS_DIRS[0] == expected_scripts, (
        f"memory-recall.SCRIPTS_DIRS[0]={mr.SCRIPTS_DIRS[0]} != {expected_scripts}"
    )


def test_production_debug_log_not_touched_by_worktree_debug_call():
    """worktree 모듈의 _debug() 호출이 production log 가 아닌 tmp log 로 간다."""
    production_log = Path("~/.claude/mindvault-v3/debug.log").expanduser()
    if not production_log.exists():
        pytest.skip("production debug.log 없음")

    before_mtime = production_log.stat().st_mtime
    before_size = production_log.stat().st_size

    sme = _load_worktree_module("session_memory_end")
    sme._debug("v3.2.7 isolation regression: session_memory_end")

    qi = _load_worktree_module("query_intent")
    qi._debug("v3.2.7 isolation regression: query_intent")

    after_mtime = production_log.stat().st_mtime
    after_size = production_log.stat().st_size
    assert before_mtime == after_mtime, (
        f"production debug.log mtime changed: {before_mtime} → {after_mtime} "
        f"(다른 hook 동시 실행 의심)"
    )
    assert before_size == after_size, (
        f"production debug.log size changed: {before_size} → {after_size}"
    )

    tmp_log = Path(os.environ["MV3_DATA_DIR"]) / "debug.log"
    assert tmp_log.exists(), "tmp debug.log not created"
    content = tmp_log.read_text()
    assert "v3.2.7 isolation regression: session_memory_end" in content
    assert "v3.2.7 isolation regression: query_intent" in content


def test_worktree_alias_index_path_isolated():
    """worktree memory_search 의 ALIAS_INDEX_PATH 가 tmp 로 격리."""
    expected_base = Path(os.environ["MV3_DATA_DIR"])
    ms = _load_worktree_module("memory_search")
    assert ms.ALIAS_INDEX_PATH.parent == expected_base
    assert ms.ALIAS_INDEX_PATH != Path("~/.claude/mindvault-v3/alias_index.json").expanduser()
