# MindVault v4 — 멀티에이전트 공유 기억 완성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **역할 배분:** 각 Task 제목에 담당 명시 — **[Codex]** = Codex 세션에서 수행, **[Claude]** = Claude Code 세션에서 수행. 서로의 결과물을 교차 검수한다 (Task X1). Codex 세션은 이 대화의 맥락이 없으므로 본 문서만으로 self-contained 하게 작성되어 있다.

**Goal:** Claude Code와 Codex가 MindVault를 대등한 1급 시민으로 사용하게 한다 — 회수 주입(완료)에 이어 ① 에이전트 소스 태깅, ② Codex 세션 자동 추출, ③ Codex close-session 규약까지 채워 v4.0.0으로 릴리스한다.

**Architecture:** 저장소(`~/.claude/projects/<home-slug>/memory/` = `~/my-folder/knowledge-hub/memory` 심링크)와 recall 엔진(`memory-recall.py`)은 이미 양쪽 공유이며 commit fe8c294(v3.10.0 태그 예정)에서 Codex `UserPromptSubmit` 주입이 연결됐다. v4는 코어를 바꾸지 않고 **경계 어댑터**만 추가한다: env 기반 에이전트 태깅(hot path 비용 0), Codex rollout JSONL → 기존 정규화 메시지 계약으로 변환하는 로더(추출은 hook 밖 비동기 경로), Codex 쪽 저장 규약 정비.

**Tech Stack:** Python 3.10 (`/Library/Frameworks/Python.framework/Versions/3.10`), pytest, Codex CLI 0.144+ hooks(`~/.codex/hooks.json`), Gemma 4 12B(localhost:8080, `enable_thinking: false`), Arctic-ko 임베딩 서버.

## Global Constraints

- **hook hot path 400ms 예산 불변** — `memory-recall.py` `HARD_TIMEOUT_MS = 400`, 이 플랜의 어떤 변경도 hot path에 I/O·연산을 추가하지 않는다 (env 읽기 1회만 허용). 추출·인덱싱은 전부 비동기 경로.
- **게이트 값 변경 금지** — `TOP_K = 1`, `RAW_COSINE_MIN_DEFAULT = 0.32`, `RAW_COSINE_MIN_HINTED = 0.27`, `SCORE_THRESHOLD = 0.50` 그대로. 에이전트별 게이트 튜닝은 v4 범위 밖 (계측 데이터가 쌓인 뒤 별도 스프린트).
- **기존 Claude 경로 무회귀** — 전체 `python3 -m pytest -q` green이 모든 커밋의 전제. 기준선: 936 passed, 2 skipped, 41 subtests (2026-07-21 Codex 실측 기준).
- **`~/.codex/hooks.json`은 `scripts/manage_codex_recall.py` 단일 작성자** — 손 편집 금지. Codex 쪽 hook 변경은 전부 이 스크립트를 확장해서 수행 (기존 Herdr `SessionStart` hook 보존·백업·원자적 저장 로직 재사용).
- **Gemma 호출 규약** — 요청 body에 top-level `"enable_thinking": False` 필수 (12B는 `chat_template_kwargs` 무효). 기존 코드 패턴 (`src/memory_extractor.py:263` 근처) 따를 것.
- **메모리 파일 저장 금지 항목** — 비밀번호·API 키·토큰·개인 식별정보 등 (`~/.codex/AGENTS.md` "Automatic Obsidian memory" 절의 기존 금지 목록 유지).
- **산출물 표기** — 코드·문서·커밋 메시지에는 "Claude Code" / "Codex" 정식 명칭만 사용 (사적 애칭 금지).
- **push 규칙** — 전체 회귀 green 확인 후에만 push. `v4.0.0` 태그는 Task R1의 릴리스 기준 4항 전부 충족 시에만.
- **커밋 메시지** — 기존 관례 유지: `feat(codex): ...`, `fix(gemma): ...` 형식 한국어.

## 배경 사실 (Codex 세션용 — 이 대화 맥락 없이 알아야 할 것)

- repo: `~/my-folder/apps/mindvault-v3` (GitHub `etinpres/mindvault-v3`), 배포본: `~/.claude/scripts/mindvault/` (post-commit hook이 자동 sync).
- recall hook: `~/.claude/hooks/memory-recall.py` — stdin JSON `{"prompt": ...}` 를 읽어 hybrid 검색(FTS5+Arctic-ko vec, RRF) 후 `<system-reminder>` 블록을 stdout으로 출력. Claude·Codex 모두 이 stdout을 컨텍스트로 주입.
- Codex 연동 현황 (commit fe8c294 — **v3.10.0 태그는 회귀 green 후 push 시 부여 예정**, 현재 최신 태그는 v3.9.1): `~/.codex/hooks.json` `UserPromptSubmit`에 위 hook이 command `"~/.claude/hooks/memory-recall.py # mindvault-v3-codex-recall"`로 등록됨. e2e 검증 2건 통과.
- Claude 세션 종료 추출 트리거: install.sh가 SessionEnd hook + 비동기 wrapper(백그라운드 detach — 종료 시 SIGTERM 회피, install.sh:234 주변)를 배포·등록하고, `src/session_memory_end.py`가 세션 transcript에서 추출을 수행한다. v4의 Codex 트리거(P3·S4)는 이 구조의 대칭 이식이다.
- 계측 파일: `~/.claude/mindvault-v3/metrics.jsonl` (kind: `recall`/`recall_skip`), `~/.claude/mindvault-v3/debug.log`.
- Claude 세션 추출 계약: `src/memory_extractor.py:173` `load_tail_messages(jsonl_path, tail_turns=40) -> list[dict]` 가 세션 JSONL을 **정규화 메시지** `{"role": "user"|"assistant", "text": <redact() 적용 str>, "bash_commands": list[str]}` 목록(마지막 40턴)으로 만들고, `extract_from_jsonl()` → Gemma 추출 → `memory_review` 승인 파이프라인으로 흐른다.
- Codex 세션 원본: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. 관측된 스키마: 한 줄에 `{"timestamp": ..., "type": "response_item", "payload": {"type": "message", "role": "user"|"assistant"|"developer", "content": [{"type": "input_text"|..., "text": ...}]}}` 형태 + `world_state` 등 비메시지 레코드 혼재. `<oai-mem-citation>` 블록이 assistant 텍스트에 섞임. **정밀 스키마 확정은 Task S2가 담당.**

---

## Phase 1 — 에이전트 소스 태깅 (S1 + P1, 선행)

### Task S1: [Codex] 회수 hook command에 MV3_AGENT env 추가

**Files:**
- Modify: `scripts/manage_codex_recall.py` (등록 command 상수)
- Modify: `tests/test_codex_recall_hook.py` (command 검증 갱신)

**Interfaces:**
- Produces: `~/.codex/hooks.json`의 UserPromptSubmit command가 `MV3_AGENT=codex ~/.claude/hooks/memory-recall.py # mindvault-v3-codex-recall` 이 된다. Task P1은 이 env 값을 읽는다. 값 계약: `"codex"` (소문자 고정).

- [ ] **Step 1: 실패하는 테스트 수정** — `tests/test_codex_recall_hook.py`(unittest 스타일, `TestCodexRecallHookManager` 클래스)에서 command 문자열을 검증하는 기존 케이스의 기대값을 `MV3_AGENT=codex ` prefix 포함으로 바꾸고, 신규 케이스 1개 추가:

```python
def test_install_command_includes_agent_env(self):
    # 클래스의 기존 install 셋업 패턴 재사용
    cfg = json.loads(self.config_path.read_text())
    handlers = cfg["hooks"]["UserPromptSubmit"][0]["hooks"]
    cmd = handlers[0]["command"]
    self.assertTrue(cmd.startswith("MV3_AGENT=codex "))
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m pytest tests/test_codex_recall_hook.py -q` → 신규/수정 케이스 FAIL.
- [ ] **Step 3: 구현** — `scripts/manage_codex_recall.py:103` `_managed_entry()`의 command 조립을 수정:

```python
command = f"MV3_AGENT=codex {shlex.quote(str(recall_hook))} # {HOOK_MARKER}"
```

기존 설치본 감지는 `_is_managed_handler()`가 `HOOK_MARKER` 포함 여부로 판단하므로 그대로 동작한다. `install()`은 이미 관리 핸들러 제거 후 재등록 방식이면 자동 upgrade가 되고, "이미 설치됨"으로 skip하는 방식이면 **command가 신형과 다를 때 재등록**하는 분기를 추가한다 (실제 `install()` 구현을 읽고 해당되는 쪽만 적용).

- [ ] **Step 4: 테스트 통과 확인** — Run: `python3 -m pytest tests/test_codex_recall_hook.py -q` → 전부 PASS.
- [ ] **Step 5: 실적용 + 실측** — Run: `python3 scripts/manage_codex_recall.py install && python3 scripts/manage_codex_recall.py status` → `"installed": true`, command에 `MV3_AGENT=codex` 포함. 이어서 새 Codex 프로세스에서 임의 질문 1회 후 `tail -2 ~/.claude/mindvault-v3/metrics.jsonl`로 hook 발화 확인 (P1 미구현 시점엔 agent 필드가 아직 없음 — 발화만 확인).
- [ ] **Step 6: Commit** — `git add scripts/manage_codex_recall.py tests/test_codex_recall_hook.py && git commit -m "feat(codex): 회수 hook command에 MV3_AGENT=codex 태깅 env 추가"`

### Task P1: [Claude] 계측에 agent 필드 추가

**Files:**
- Modify: `hooks/memory-recall.py` (repo 원본 — install.sh가 `~/.claude/hooks/memory-recall.py`로 배포)
- Test: `tests/test_recall_agent_tag.py` (신규 — 기존 hook 테스트들의 subprocess 실행 + `MV3_DATA_DIR` tmp 격리 패턴 재사용)

**Interfaces:**
- Consumes: env `MV3_AGENT` (S1이 codex 쪽에 세팅, 본 태스크가 Claude 쪽 등록에 세팅).
- Produces: `metrics.jsonl`의 kind `recall`/`recall_skip` 레코드에 `"agent": "claude"|"codex"|"unknown"` 필드. **미설정·비인가 값 = `"unknown"`** — 수동 실행·제3 소비자를 claude로 오분류하지 않기 위한 계측 정직성 규칙 (Codex 리뷰 반영). debug.log의 `hook-recall:` 라인에 `agent=<value>` 토큰. 이후 통계 스크립트는 이 필드로 에이전트별 hit rate를 집계할 수 있다.

- [ ] **Step 1: 실패하는 테스트** — `tests/test_recall_agent_tag.py`에 테스트 2개:

```python
def _run_hook(tmp_path, env_extra):
    env = {**os.environ, "MV3_DATA_DIR": str(tmp_path), **env_extra}
    subprocess.run(
        [sys.executable, "hooks/memory-recall.py"],
        input=json.dumps({"session_id": "t", "prompt": "에이전트 태깅 검증용 프롬프트"}),
        text=True, env=env, timeout=10,
    )
    lines = (tmp_path / "metrics.jsonl").read_text().strip().splitlines()
    return json.loads(lines[-1])

def test_agent_field_codex(tmp_path):
    assert _run_hook(tmp_path, {"MV3_AGENT": "codex"})["agent"] == "codex"

def test_agent_field_claude(tmp_path):
    assert _run_hook(tmp_path, {"MV3_AGENT": "claude"})["agent"] == "claude"

def test_agent_field_unset_is_unknown(tmp_path):
    assert _run_hook(tmp_path, {"MV3_AGENT": ""})["agent"] == "unknown"
```

(회수 결과가 skip이어도 `recall_skip` 레코드에 agent가 찍히므로 마지막 레코드 검증으로 충분. 빈 문자열·비인가 값 처리는 Step 3 구현의 검증 분기가 담당.)
- [ ] **Step 2: 실패 확인** — Run: `python3 -m pytest <해당 테스트 파일> -q` → FAIL (agent 키 부재).
- [ ] **Step 3: 구현** — `main()` 도입부에서 1회 읽기:

```python
agent = os.environ.get("MV3_AGENT", "").strip()
if agent not in ("claude", "codex"):
    agent = "unknown"
```

모든 `_metric({...})` 호출 dict에 `"agent": agent` 추가, `_debug(f"hook-recall: ...")` 라인에 `agent={agent}` 추가. **다른 로직 변경 없음** (400ms 예산·게이트 불변).

- [ ] **Step 3b: Claude 쪽 등록에 env 명시** — `install.sh`의 UserPromptSubmit hook 등록부에서 command를 `MV3_AGENT=claude ~/.claude/hooks/memory-recall.py`로 변경 (S1의 codex 쪽과 대칭). install.sh의 기존 stale-entry 제거 로직이 구형 command를 교체하는지 확인하고, 안 되면 교체 분기 추가.

- [ ] **Step 4: 통과 확인** — Run: 해당 테스트 파일 + `python3 -m pytest -q` 전체 green.
- [ ] **Step 5: 배포 + 실측** — Run: `MV3_SYNC_ONLY=1 ./install.sh` 후, Claude 세션 프롬프트 1회와 Codex 프롬프트 1회를 발생시켜 `tail -2 ~/.claude/mindvault-v3/metrics.jsonl`에서 `"agent": "claude"`와 `"agent": "codex"`가 각각 찍히는 것 확인.
- [ ] **Step 6: Commit** — `git commit -m "feat(recall): metrics/debug에 agent 소스 태깅 (MV3_AGENT env 기반)"`

---

## Phase 2 — Codex 세션 자동 추출 (S2 → P2 → P3 → S4 순서, S2가 선행 입력)

> Codex 리뷰 반영: 자동 실행 트리거(P3·S4)를 v4 범위에 포함한다 — 이게 빠지면 "자동 추출"이 아니라 로더+수동 추출이라 v4.0.0 이름이 과장이 된다.

### Task S2: [Codex] rollout 포맷 스펙 + fixture 제공

**Files:**
- Create: `docs/specs/codex-rollout-format.md`
- Create: `tests/fixtures/codex_sessions/interactive.jsonl`
- Create: `tests/fixtures/codex_sessions/exec.jsonl`
- Create: `tests/fixtures/codex_sessions/compacted.jsonl`

**Interfaces:**
- Produces: P2가 파서를 구현할 때 유일하게 의존하는 스키마 문서와 테스트 입력. 스펙 문서에는 최소한 다음을 **실제 세션에서 확인해** 기술한다:
  1. 메시지 레코드 판별 규칙 (`type`/`payload.type`/`payload.role` 조합; `developer`·`world_state`·도구 호출 레코드는 제외 대상 명시)
  2. 텍스트 추출 규칙 (`content[]`의 type별 text 위치, `<oai-mem-citation>`·`<system-reminder>` 블록 제거 규칙)
  3. bash 명령 상당물 존재 여부 (Codex의 shell 호출 레코드 구조 — 있으면 스키마, 없으면 "없음"을 명시)
  4. 세션 유형별 차이 (interactive vs `codex exec` vs compact 발생 세션)
  5. **Stop/SessionEnd hook payload 스키마** — Codex hook 이벤트가 세션 파일 경로를 어떤 필드로 전달하는지 (향후 자동 트리거 연결용; 0.144 기준 실측)
- Fixture 계약: 각 파일은 실제 rollout에서 발췌·**redact**(경로·이름 외 개인정보 제거)한 15~40줄 축약본. interactive에는 user/assistant 최소 3턴 + 비메시지 레코드 2종 이상 포함.

- [ ] **Step 1: 실제 세션 3종 선정** — `~/.codex/sessions/2026/07/` 이하에서 interactive·exec·compact 각 1건 선정 (compact가 없으면 `"compact"` 문자열 포함 세션 grep으로 탐색; 그래도 없으면 스펙에 "compact 레코드 미관측"으로 기록하고 fixture는 2종).
- [ ] **Step 2: 스펙 문서 작성** — 위 Produces 1~5 항목 구조로 `docs/specs/codex-rollout-format.md` 작성. 각 규칙에 실제 JSON 한 줄 예시 첨부.
- [ ] **Step 3: fixture 작성 + 검증** — 발췌·redact 후 `python3 -c "import json,sys; [json.loads(l) for l in open(sys.argv[1])]" tests/fixtures/codex_sessions/interactive.jsonl` 식으로 3파일 전부 JSONL 유효성 확인.
- [ ] **Step 3b: 비밀·개인정보 스캔 (릴리스 게이트)** — Run: `/opt/homebrew/bin/gitleaks detect --no-git --source tests/fixtures/codex_sessions -v` → Expected: `no leaks found`. 이어서 이메일·전화·주소·계좌 패턴 육안 점검 1회. 검출 시 해당 값 치환 후 재스캔 (Codex 리뷰 반영 — R1 릴리스 기준에도 포함).
- [ ] **Step 4: Commit** — `git add docs/specs/codex-rollout-format.md tests/fixtures/codex_sessions/ && git commit -m "docs(codex): rollout 포맷 스펙 + 추출용 fixture 3종"`

### Task P2: [Claude] Codex rollout 로더 + 추출 라우팅

**Files:**
- Create: `src/codex_session_loader.py`
- Modify: `src/memory_extractor.py` (소스 판별 라우팅 1곳)
- Test: `tests/test_codex_session_loader.py`

**Interfaces:**
- Consumes: S2의 스펙 문서와 fixture. `src/memory_extractor.py:137` `extract_text_from_content`, `:126` `redact`, `:132` `_is_system_reminder` 재사용.
- Produces: `load_tail_messages_codex(jsonl_path: Path, tail_turns: int = 40) -> list[dict]` — 반환 형태는 Claude 경로와 **동일 계약** `{"role": "user"|"assistant", "text": str, "bash_commands": list[str]}` (마지막 tail_turns턴). `extract_from_jsonl()`은 파일 첫 유효 레코드의 스키마(또는 경로가 `~/.codex/sessions/` 하위인지)로 로더를 선택하며, 이후 Gemma 추출·review 파이프라인은 무변경으로 동작한다.

- [ ] **Step 1: 실패하는 테스트** — fixture 3종 각각에 대해: 반환 dict 키·role 집합 검증, `developer`/`world_state` 제외 검증, `<oai-mem-citation>` 제거 검증, tail_turns 절단 검증. exec fixture는 1턴짜리 정상 처리 검증.

```python
def test_interactive_fixture_normalized():
    msgs = load_tail_messages_codex(FIXTURES / "interactive.jsonl")
    assert msgs and set(msgs[0]) == {"role", "text", "bash_commands"}
    assert all(m["role"] in ("user", "assistant") for m in msgs)
    assert all("oai-mem-citation" not in m["text"] for m in msgs)
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m pytest tests/test_codex_session_loader.py -q` → FAIL (모듈 없음).
- [ ] **Step 3: 로더 구현** — S2 스펙대로 파싱, 텍스트에 기존 `redact()` 적용, 인용·리마인더 블록 제거. 스펙에 bash 상당물이 "없음"이면 `bash_commands`는 항상 `[]`.
- [ ] **Step 4: 통과 확인** — Run: `python3 -m pytest tests/test_codex_session_loader.py -q` → PASS.
- [ ] **Step 5: 라우팅 연결 + 통합 테스트** — `extract_from_jsonl()` 도입부에 Codex 판별 → `load_tail_messages_codex` 호출 분기 추가. 통합 테스트 1개: interactive fixture를 `extract_from_jsonl`에 넣어 Gemma 호출 직전 prompt에 fixture의 user 텍스트가 포함되는지 (Gemma는 mock).
- [ ] **Step 6: 실측** — 실제 Codex 세션 1건으로 extractor를 직접 호출해 추출 후보 생성 확인:

```bash
python3 -c "
import sys; sys.path.insert(0, 'src')
from pathlib import Path
from memory_extractor import extract_from_jsonl
cands = extract_from_jsonl(sorted(Path.home().glob('.codex/sessions/2026/*/*/rollout-*.jsonl'))[-1])
print(len(cands), [c.get('title') for c in cands])
"
```

후보가 `/memory_review` 큐에 오르는 것까지 확인 (큐 적재 경로는 Claude 세션과 동일 파이프라인). 후보 품질은 Task X1에서 Codex가 검수.
- [ ] **Step 7: Commit** — `git commit -m "feat(extractor): Codex rollout 세션 로더 + 추출 라우팅 (fixture 기반)"`

### Task P3: [Claude] 세션 종료 추출 트리거 — Codex 수신 경로

**Files:**
- Modify: `src/session_memory_end.py` (Codex transcript 경로 수신 분기)
- Modify: SessionEnd 비동기 wrapper (install.sh가 배포하는 wrapper 스크립트 — Codex 호출 인자 통과 확인)
- Test: `tests/test_codex_session_end.py` (신규)

**Interfaces:**
- Consumes: S2 스펙의 Stop/SessionEnd hook payload 스키마 (Codex가 transcript 경로를 전달하는 필드명), P2의 `load_tail_messages_codex`.
- Produces: `session_memory_end.py`가 Codex rollout 경로를 받으면(payload 필드 또는 `--codex-transcript <path>` 인자) P2 로더로 추출을 수행한다. S4는 이 진입점을 Codex hook에서 호출한다. **Claude 경로 payload 처리는 무변경.**

- [ ] **Step 1: 실패하는 테스트** — S2 payload 스키마를 본뜬 stdin JSON(또는 인자)으로 `session_memory_end.py`를 실행해, interactive fixture 경로가 주어지면 추출 파이프라인이 Codex 로더를 타는지 검증 (Gemma mock, 후보 ≥ 0건 반환·크래시 없음 + 로더 선택 분기 단위 검증).
- [ ] **Step 2: 실패 확인** — Run: `python3 -m pytest tests/test_codex_session_end.py -q` → FAIL.
- [ ] **Step 3: 구현** — 진입부에서 transcript 경로의 소스 판별(P2 Step 5와 동일 판별 재사용) 후 로더 분기. 서브세션 재귀 가드·비동기 detach 등 기존 방어 로직은 그대로 통과시킨다.
- [ ] **Step 4: 통과 확인 + 전체 회귀** — Run: `python3 -m pytest tests/test_codex_session_end.py -q && python3 -m pytest -q` → green.
- [ ] **Step 5: 배포 + Commit** — `MV3_SYNC_ONLY=1 ./install.sh` 후 `git commit -m "feat(extractor): 세션 종료 추출의 Codex transcript 수신 경로"`

### Task S4: [Codex] Stop hook 등록 — 자동 추출 트리거 연결

**Files:**
- Modify: `scripts/manage_codex_recall.py` (Stop/SessionEnd hook 등록 기능 추가 — 기존 install/uninstall/status에 대상 hook 확장)
- Modify: `tests/test_codex_recall_hook.py` (Stop hook 등록·해제·보존 케이스)

**Interfaces:**
- Consumes: S2가 확정한 Stop hook payload 스키마, P3의 진입점 (배포본 `~/.claude/scripts/mindvault/` 하위 session_memory_end 경로 — P3 커밋의 배포 산출물 기준).
- Produces: `~/.codex/hooks.json`에 Stop(또는 Codex의 세션 종료 상당 이벤트) hook이 등록되어 Codex 세션 종료 시 추출이 자동 실행된다. `uninstall`은 recall·stop 두 hook을 함께 해제한다.

- [ ] **Step 1: 실패하는 테스트** — Stop hook 등록 후 hooks.json에 recall(UserPromptSubmit)·stop 두 관리 핸들러가 공존하고 Herdr SessionStart가 보존되는지, uninstall이 둘 다 제거하는지 검증하는 케이스 추가.
- [ ] **Step 2: 실패 확인** — Run: `python3 -m pytest tests/test_codex_recall_hook.py -q` → FAIL.
- [ ] **Step 3: 구현** — 기존 `_managed_entry`/`_is_managed_handler` 패턴을 이벤트별 마커(`mindvault-v3-codex-recall`, `mindvault-v3-codex-session-end`)로 일반화. 실행 command는 P3 진입점 + S2 스키마 기준 인자. 백그라운드 detach가 필요하면 Claude 쪽 wrapper 방식을 따른다 (Codex 종료 시 SIGTERM 여부는 실측으로 확인).
- [ ] **Step 4: 통과 확인** — Run: `python3 -m pytest tests/test_codex_recall_hook.py -q` → PASS.
- [ ] **Step 5: 실적용 + e2e** — `python3 scripts/manage_codex_recall.py install` 후 실제 Codex 세션 하나를 열고 닫아, `~/.claude/mindvault-v3/debug.log`에 session-end 추출 기록과 후보 생성(또는 후보 0건 정상 종료)을 확인.
- [ ] **Step 6: Commit** — `git commit -m "feat(codex): Stop hook 자동 추출 트리거 등록 (manage_codex_recall 확장)"`

---

## Phase 3 — Codex 저장 규약 정비 (S3)

### Task S3: [Codex] $close-session 스킬 + frontmatter 규약

**Files:**
- Create: `codex/close-session/SKILL.md` (repo 템플릿 — 정본)
- Create: `codex/AGENTS-mindvault-snippet.md` (repo 템플릿 — AGENTS.md에 삽입할 규약 절, 마커 주석 `<!-- MINDVAULT_MEMORY_START/END -->` 포함)
- Modify: `~/.agents/skills/close-session/SKILL.md` (템플릿을 자기 환경에 설치 — Codex 스킬 설치 관례 기준)
- Modify: `~/.codex/AGENTS.md`의 "Automatic Obsidian memory" 절 (snippet 반영)

> Codex 리뷰 반영: 산출물을 사용자 홈에만 두면 신규 설치자가 v4 기능을 받지 못한다. **repo 템플릿이 정본**이고 홈 설치본은 배포 결과물이다 — 이후 수정은 템플릿에 하고 P4의 install 경로로 배포한다.

**Interfaces:**
- Consumes: MindVault 메모리 파일 규약 (아래에 전문 명시 — 다른 문서 참조 불필요).
- Produces: Codex가 저장하는 신규 메모리 파일이 Claude 저장분과 동일 규약을 따른다. 규약 전문:

```markdown
---
name: <short-kebab-case-slug>
description: <one-line summary — 회수 시 관련성 판단에 사용>
metadata:
  type: user | feedback | project | reference | procedural
---

<본문. 사실마다 [YYYY-MM-DD] 태그. 관련 메모리는 [[name]] 위키링크.>
```

  추가 규칙: ① 기존 파일 갱신이 신규 생성보다 우선(중복 금지) ② 신규 파일 생성 시 `MEMORY.md`에 한 줄 인덱스 추가 — 단 procedural/feedback 유형은 `MEMORY-PROCEDURAL.md`/`MEMORY-FEEDBACK.md`에 추가 (MEMORY.md 200줄 한계 유지) ③ 기존 금지 목록(비밀정보 등) 유지 ④ 기존에 frontmatter 없이 저장된 Codex 파일들은 **소급 수정하지 않는다** (인덱서가 이미 수용 중).

- [ ] **Step 1: repo 템플릿 작성** — `codex/close-session/SKILL.md`: `$close-session` 호출 시 현재 세션에서 새로 확정된 사실·결정·완료 사항을 열거 → 위 규약대로 memory/ 에 반영 → 반영 목록을 사용자에게 보고. `codex/AGENTS-mindvault-snippet.md`: frontmatter 규약(위 블록 그대로) + 인덱스 규칙 ①~④, 마커 주석 포함. 자동(무호출) 저장 규칙은 기존 AGENTS.md 체크포인트 규칙 유지.
- [ ] **Step 2: 자기 환경 설치** — 템플릿을 `~/.agents/skills/close-session/SKILL.md`로 복사, AGENTS.md에 snippet 삽입.
- [ ] **Step 3: 실사용 검증** — 실제 세션 하나를 `$close-session`으로 닫아 신규/갱신 파일이 규약을 지키는지 확인. 이어서 `tail ~/.claude/mindvault-v3/debug.log`에서 mem-indexer가 해당 파일을 인덱싱했는지 확인.
- [ ] **Step 4: Commit + 기록** — `git add codex/ && git commit -m "feat(codex): close-session 스킬 + AGENTS 메모리 규약 템플릿"`. 변경 사항을 memory/의 MindVault 프로젝트 노트에 [2026-MM-DD] 태그로 기록.

### Task P4: [Claude] install.sh Codex 통합 — 신규 설치 경로

**Files:**
- Modify: `install.sh` (Codex 감지·설치 절 추가)
- Modify: `uninstall.sh` (스킬·snippet 제거 — recall/stop hook 해제는 기존 연동 확인)
- Modify: `README.md` (Codex 연동 설치 안내 절)

**Interfaces:**
- Consumes: S1·S4의 `manage_codex_recall.py` install 명령, S3의 repo 템플릿 (`codex/`).
- Produces: `./install.sh` 실행 시 `~/.codex/`가 존재하면 ① `manage_codex_recall.py install`(recall+stop hook), ② `codex/close-session/SKILL.md` → `~/.agents/skills/close-session/` 복사, ③ AGENTS.md에 snippet을 마커 주석 기준 idempotent 삽입/갱신. `~/.codex/` 부재 시 전부 조용히 skip (Claude 단독 사용자 무영향).

- [ ] **Step 1: 구현** — install.sh에 위 ①~③ 절 추가. AGENTS.md 삽입은 `<!-- MINDVAULT_MEMORY_START/END -->` 마커 사이 교체 방식 (중복 삽입 방지, 사용자 다른 내용 보존).
- [ ] **Step 2: 검증 (신규 설치 시뮬레이션)** — `MV3_CODEX_HOME=<tmp>` 식 오버라이드가 가능하면 tmp로, 아니면 실제 홈에서: hooks.json 백업 후 uninstall → install 왕복으로 hook 2종·스킬·snippet이 생성·제거되는지, Herdr hook·AGENTS 기존 내용이 보존되는지 확인.
- [ ] **Step 3: 전체 회귀 + Commit** — `python3 -m pytest -q` green 후 `git commit -m "feat(install): Codex 자동 연동 — hook·close-session 스킬·AGENTS 규약 배포"`

---

## Phase 4 — 교차 검수 + 릴리스

### Task X1: [공동] 교차 검수

- [ ] **Step 1:** Codex 산출물(S1·S2·S3·S4)을 Claude Code가 검수 — hooks.json 실상태(hook 2종+Herdr 보존), 스펙 문서 vs 실제 rollout 대조(무작위 세션 2건), fixture redact·스캔 누락, close-session 산출 파일 규약 준수.
- [ ] **Step 2:** Claude 산출물(P1·P2·P3·P4)을 Codex가 검수 — `codex review` 비대화 모드로 diff 리뷰 + 자기 런타임에서 주입·계측·세션 종료 추출 실측 (metrics의 `"agent": "codex"` 실기록 확인 포함).
- [ ] **Step 3:** 발견 사항은 각자 담당 영역을 수정 후 재검수. 2회 연속 지적 0건이면 통과.

### Task R1: [Claude] v4.0.0 릴리스

**릴리스 기준 (전부 충족 필수):**
1. metrics.jsonl에 claude/codex 양쪽 agent 필드가 실운영 데이터로 기록됨
2. 실제 Codex 세션 ≥1건이 **자동 트리거(Stop hook)로** 추출→review 승인까지 통과
3. Codex `$close-session` 실사용 ≥1회, 산출 파일 규약 준수
4. 양방향 주입 e2e 재확인 (Claude 프롬프트 1건 + 신규 Codex 프로세스 1건)
5. fixture gitleaks 스캔 `no leaks found` (S2 Step 3b 재실행)
6. 신규 설치 시뮬레이션 통과 (P4 Step 2 왕복 검증 재실행)

- [ ] **Step 1:** 전체 회귀 green 확인 — Run: `python3 -m pytest -q`.
- [ ] **Step 2:** CHANGELOG·README 갱신 (v4.0.0 — 멀티에이전트 공유 기억: 태깅·Codex 추출·close-session).
- [ ] **Step 3:** Commit + 태그 + push — `git commit -m "docs(release): v4.0.0 — 멀티에이전트 공유 기억 완성" && git tag v4.0.0 && git push origin master --tags`.

---

## 범위 제외 (명시적 YAGNI)

- 에이전트별 cosine 게이트·TOP_K 차등 튜닝 (계측 데이터 축적 후 별도 스프린트)
- Codex `SessionStart` 요약 주입 (v4.1 후보)
- LLM 백엔드 교체 (별도 스펙: `docs/specs/2026-07-21-v4.1-llm-backend-abstraction.md`. hot path Gemma intent 폴백은 [2026-07-21] `MV3_GEMMA_INTENT` off로 이미 조치됨)
- repo 리네이밍 (`mindvault-v3` 유지 — 디렉토리·배포 경로 변경은 v4 범위 밖)

## 개정 이력

- [2026-07-21] 초판 작성 (Claude Code) → Codex 리뷰 반영 개정: ① 자동 트리거(P3·S4) v4 편입 ② 신규 설치 경로(P4 + S3 repo 템플릿화) ③ 테스트 기준선 936 정정 ④ v3.10.0 태그 예정 표기 ⑤ `MV3_AGENT` 미설정=unknown + 양측 명시 env ⑥ fixture gitleaks 게이트.
- [2026-07-22] **전 태스크 완료 — v4.0.0 릴리스.** S1~S4(Codex)·P1~P4(Claude Code) 구현, X1 교차 검수 왕복(Codex→P 검수 5결함 → f5c95d5 수정 → 재검 통과, Claude→S 검수 1차 지적 0건). 릴리스 기준: ①agent 양쪽 실기록 ②실 Stop 자동 추출→review 승인 ③close-session 실사용(기존 파일 갱신 경로 — 신규 파일 생성 경로는 미실행, 운영 중 자연 발생 시 확인) ④양방향 주입 재확인 ⑤gitleaks ⑥설치 시뮬 왕복. 최종 회귀 959 passed·2 skipped·41 subtests.
