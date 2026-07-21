# MindVault v4 — 멀티에이전트 공유 기억 완성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **역할 배분:** 각 Task 제목에 담당 명시 — **[Codex]** = Codex 세션에서 수행, **[Claude]** = Claude Code 세션에서 수행. 서로의 결과물을 교차 검수한다 (Task X1). Codex 세션은 이 대화의 맥락이 없으므로 본 문서만으로 self-contained 하게 작성되어 있다.

**Goal:** Claude Code와 Codex가 MindVault를 대등한 1급 시민으로 사용하게 한다 — 회수 주입(완료)에 이어 ① 에이전트 소스 태깅, ② Codex 세션 자동 추출, ③ Codex close-session 규약까지 채워 v4.0.0으로 릴리스한다.

**Architecture:** 저장소(`~/.claude/projects/-Users-yonghaekim/memory/` = `~/my-folder/knowledge-hub/memory` 심링크)와 recall 엔진(`memory-recall.py`)은 이미 양쪽 공유이며 v3.10.0에서 Codex `UserPromptSubmit` 주입이 연결됐다. v4는 코어를 바꾸지 않고 **경계 어댑터**만 추가한다: env 기반 에이전트 태깅(hot path 비용 0), Codex rollout JSONL → 기존 정규화 메시지 계약으로 변환하는 로더(추출은 hook 밖 비동기 경로), Codex 쪽 저장 규약 정비.

**Tech Stack:** Python 3.10 (`/Library/Frameworks/Python.framework/Versions/3.10`), pytest, Codex CLI 0.144+ hooks(`~/.codex/hooks.json`), Gemma 4 12B(localhost:8080, `enable_thinking: false`), Arctic-ko 임베딩 서버.

## Global Constraints

- **hook hot path 400ms 예산 불변** — `memory-recall.py` `HARD_TIMEOUT_MS = 400`, 이 플랜의 어떤 변경도 hot path에 I/O·연산을 추가하지 않는다 (env 읽기 1회만 허용). 추출·인덱싱은 전부 비동기 경로.
- **게이트 값 변경 금지** — `TOP_K = 1`, `RAW_COSINE_MIN_DEFAULT = 0.32`, `RAW_COSINE_MIN_HINTED = 0.27`, `SCORE_THRESHOLD = 0.50` 그대로. 에이전트별 게이트 튜닝은 v4 범위 밖 (계측 데이터가 쌓인 뒤 별도 스프린트).
- **기존 Claude 경로 무회귀** — 전체 `python3 -m pytest -q` green이 모든 커밋의 전제. 기준선: 935 passed, 2 skipped.
- **`~/.codex/hooks.json`은 `scripts/manage_codex_recall.py` 단일 작성자** — 손 편집 금지. Codex 쪽 hook 변경은 전부 이 스크립트를 확장해서 수행 (기존 Herdr `SessionStart` hook 보존·백업·원자적 저장 로직 재사용).
- **Gemma 호출 규약** — 요청 body에 top-level `"enable_thinking": False` 필수 (12B는 `chat_template_kwargs` 무효). 기존 코드 패턴 (`src/memory_extractor.py:263` 근처) 따를 것.
- **메모리 파일 저장 금지 항목** — 비밀번호·API 키·토큰·개인 식별정보 등 (`~/.codex/AGENTS.md` "Automatic Obsidian memory" 절의 기존 금지 목록 유지).
- **산출물 표기** — 코드·문서·커밋 메시지에는 "Claude Code" / "Codex" 정식 명칭만 사용 (사적 애칭 금지).
- **push 규칙** — 전체 회귀 green 확인 후에만 push. `v4.0.0` 태그는 Task R1의 릴리스 기준 4항 전부 충족 시에만.
- **커밋 메시지** — 기존 관례 유지: `feat(codex): ...`, `fix(gemma): ...` 형식 한국어.

## 배경 사실 (Codex 세션용 — 이 대화 맥락 없이 알아야 할 것)

- repo: `~/my-folder/apps/mindvault-v3` (GitHub `etinpres/mindvault-v3`), 배포본: `~/.claude/scripts/mindvault/` (post-commit hook이 자동 sync).
- recall hook: `/Users/yonghaekim/.claude/hooks/memory-recall.py` — stdin JSON `{"prompt": ...}` 를 읽어 hybrid 검색(FTS5+Arctic-ko vec, RRF) 후 `<system-reminder>` 블록을 stdout으로 출력. Claude·Codex 모두 이 stdout을 컨텍스트로 주입.
- Codex 연동 현황 (v3.10.0, commit fe8c294): `~/.codex/hooks.json` `UserPromptSubmit`에 위 hook이 command `"/Users/yonghaekim/.claude/hooks/memory-recall.py # mindvault-v3-codex-recall"`로 등록됨. e2e 검증 2건 통과.
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
- Produces: `~/.codex/hooks.json`의 UserPromptSubmit command가 `MV3_AGENT=codex /Users/yonghaekim/.claude/hooks/memory-recall.py # mindvault-v3-codex-recall` 이 된다. Task P1은 이 env 값을 읽는다. 값 계약: `"codex"` (소문자 고정).

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
- Consumes: env `MV3_AGENT` (S1이 codex 쪽에 세팅; 미설정 시 `"claude"` 기본값 — Claude 쪽 settings.json은 변경 불필요).
- Produces: `metrics.jsonl`의 kind `recall`/`recall_skip` 레코드에 `"agent": "claude"|"codex"` 필드. debug.log의 `hook-recall:` 라인에 `agent=<value>` 토큰. 이후 통계 스크립트는 이 필드로 에이전트별 hit rate를 집계할 수 있다.

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

def test_agent_field_default_claude(tmp_path):
    env = {"MV3_AGENT": ""}  # 미설정과 동일 취급 확인
    assert _run_hook(tmp_path, env)["agent"] == "claude"
```

(회수 결과가 skip이어도 `recall_skip` 레코드에 agent가 찍히므로 마지막 레코드 검증으로 충분. 빈 문자열 처리는 Step 3 구현의 검증 분기가 담당.)
- [ ] **Step 2: 실패 확인** — Run: `python3 -m pytest <해당 테스트 파일> -q` → FAIL (agent 키 부재).
- [ ] **Step 3: 구현** — `main()` 도입부에서 1회 읽기:

```python
agent = os.environ.get("MV3_AGENT", "claude")
if agent not in ("claude", "codex"):
    agent = "claude"
```

모든 `_metric({...})` 호출 dict에 `"agent": agent` 추가, `_debug(f"hook-recall: ...")` 라인에 `agent={agent}` 추가. **다른 로직 변경 없음** (400ms 예산·게이트 불변).

- [ ] **Step 4: 통과 확인** — Run: 해당 테스트 파일 + `python3 -m pytest -q` 전체 green.
- [ ] **Step 5: 배포 + 실측** — Run: `MV3_SYNC_ONLY=1 ./install.sh` 후, Claude 세션 프롬프트 1회와 Codex 프롬프트 1회를 발생시켜 `tail -2 ~/.claude/mindvault-v3/metrics.jsonl`에서 `"agent": "claude"`와 `"agent": "codex"`가 각각 찍히는 것 확인.
- [ ] **Step 6: Commit** — `git commit -m "feat(recall): metrics/debug에 agent 소스 태깅 (MV3_AGENT env 기반)"`

---

## Phase 2 — Codex 세션 자동 추출 (S2 → P2, S2가 선행 입력)

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

---

## Phase 3 — Codex 저장 규약 정비 (S3)

### Task S3: [Codex] $close-session 스킬 + frontmatter 규약

**Files:**
- Create: Codex 스킬 `close-session` (`~/.agents/skills/close-session/SKILL.md` — Codex 스킬 설치 관례 기준)
- Modify: `~/.codex/AGENTS.md`의 "Automatic Obsidian memory" 절 (frontmatter 규약 추가)

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

- [ ] **Step 1: 스킬 작성** — `$close-session` 호출 시: 현재 세션에서 새로 확정된 사실·결정·완료 사항을 열거 → 위 규약대로 memory/ 에 반영 → 반영 목록을 사용자에게 보고. 자동(무호출) 저장 규칙은 기존 AGENTS.md 체크포인트 규칙 유지.
- [ ] **Step 2: AGENTS.md 갱신** — "Automatic Obsidian memory" 절에 frontmatter 규약(위 블록 그대로)과 인덱스 규칙 ①~④ 추가.
- [ ] **Step 3: 실사용 검증** — 실제 세션 하나를 `$close-session`으로 닫아 신규/갱신 파일이 규약을 지키는지 확인. 이어서 `tail ~/.claude/mindvault-v3/debug.log`에서 mem-indexer가 해당 파일을 인덱싱했는지 확인.
- [ ] **Step 4: 기록** — 변경 사항을 memory/의 MindVault 프로젝트 노트에 [2026-MM-DD] 태그로 기록 (repo 커밋 대상 아님 — AGENTS.md·스킬은 repo 밖 파일).

---

## Phase 4 — 교차 검수 + 릴리스

### Task X1: [공동] 교차 검수

- [ ] **Step 1:** Codex 산출물(S1·S2·S3)을 Claude Code가 검수 — hooks.json 실상태, 스펙 문서 vs 실제 rollout 대조(무작위 세션 2건), fixture redact 누락, close-session 산출 파일 규약 준수.
- [ ] **Step 2:** Claude 산출물(P1·P2)을 Codex가 검수 — `codex review` 비대화 모드로 diff 리뷰 + 자기 런타임에서 주입·계측 실측 (metrics의 `"agent": "codex"` 실기록 확인 포함).
- [ ] **Step 3:** 발견 사항은 각자 담당 영역을 수정 후 재검수. 2회 연속 지적 0건이면 통과.

### Task R1: [Claude] v4.0.0 릴리스

**릴리스 기준 (전부 충족 필수):**
1. metrics.jsonl에 claude/codex 양쪽 agent 필드가 실운영 데이터로 기록됨
2. 실제 Codex 세션 ≥1건이 추출→review 승인까지 통과
3. Codex `$close-session` 실사용 ≥1회, 산출 파일 규약 준수
4. 양방향 주입 e2e 재확인 (Claude 프롬프트 1건 + 신규 Codex 프로세스 1건)

- [ ] **Step 1:** 전체 회귀 green 확인 — Run: `python3 -m pytest -q`.
- [ ] **Step 2:** CHANGELOG·README 갱신 (v4.0.0 — 멀티에이전트 공유 기억: 태깅·Codex 추출·close-session).
- [ ] **Step 3:** Commit + 태그 + push — `git commit -m "docs(release): v4.0.0 — 멀티에이전트 공유 기억 완성" && git tag v4.0.0 && git push origin master --tags`.

---

## 범위 제외 (명시적 YAGNI)

- 에이전트별 cosine 게이트·TOP_K 차등 튜닝 (계측 데이터 축적 후 별도 스프린트)
- cold-start 시 Gemma intent 타임아웃으로 인한 회수 skip 완화 (기존 운영 특성 — 별도 이슈로 추적)
- Codex `SessionStart` 요약 주입, Codex 세션 자동 추출의 **상시 트리거**(Stop hook 연결) — S2가 payload 스키마만 확보해 두고 연결은 v4.1 후보
- repo 리네이밍 (`mindvault-v3` 유지 — 디렉토리·배포 경로 변경은 v4 범위 밖)
