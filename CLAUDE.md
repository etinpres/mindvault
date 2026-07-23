# MindVault v3

## Project

MindVault v3 — Claude Code 세션 간 기억 유지 시스템. MindVault v1의 실패 교훈을 반영해 스코프를 극단적으로 좁힌 재시도.

### 풀려는 문제

사용자(비전공 1인 개발자)가 Claude Code에서 세션을 새로 시작하면 이전 맥락을 잃어버려, 세션을 끊지 못하고 컨텍스트 윈도우가 폭발하는 악순환. 새 세션을 시작해도 AI가 "딴소리"하는 일이 잦음.

### 해결 방식 — 4-layer 메모리 아키텍처

1. **자동 주입 (Layer 1)** — SessionStart 훅에서 최근 N개 세션을 Gemma 로컬 서버로 요약해 새 세션에 자동 주입
2. **자연어 검색 (Layer 2)** — FTS5 + Arctic-ko 임베딩 hybrid (RRF) 로 과거 모든 세션·메모리 검색 가능. `/recall` 스킬로 호출
3. **Memory Compiler (Layer 3)** — 세션 종료 시 Gemma 가 결정/노하우/사실을 추출 → `memory/_procedural/_staged/` 임시 저장 → 사용자의 `/memory_review` 승인 후 영구 메모리 진입 (Sprint 13~14)
4. **자동 회수 hook (Layer 4)** — UserPromptSubmit 마다 hybrid 검색으로 관련 메모리를 `system-reminder` 로 자동 주입 (raw cosine 게이트 + query intent classifier 가 잡담 차단, false positive 0%)

## Layer 5: Contradiction Detection (v3.4+)

Memory Compiler 가 신규/업데이트 메모리를 staged 한 *직후* `contradiction_detector` 가 자동 fire (`session_memory_end.py:make_contradiction_aware_writer`).

- **검출**: hybrid recall (FTS5 + Arctic-ko-MLX RRF) top-5 → Gemma 4 E4B (`mlx-community/gemma-4-e4b-it-4bit`, port 8080) 가 4-way 분류 (`metric_update` / `decision_reversal` / `fact_correction` / `no_conflict`)
- **gate**: confidence ≥ 0.7 만 review queue 추가 (false positive 회피)
- **queue**: `~/.claude/mindvault-v3/contradictions.jsonl` (append-only, atomic rewrite via tmp + os.replace + fcntl.flock)
- **review CLI**: `python -m src.contradiction_review_cli list / show / resolve`
- **회수 영향**: `deprecated_by: [name]` frontmatter 가 있는 메모리는 Layer 4 hook 회수 시 raw_cosine + score 모두 × 0.3 감쇠 (sort key 가 primary raw_cosine 이므로 양쪽 감쇠 필요)
- **비용**: Gemma 호출 1~5건 / close-session (~p95 < 4s warm)
- **graceful fail**: detector throw 해도 staged write 흐름 차단 안 함 — 모든 실패는 silent skip + `debug.log` 로 telemetry

**Cost asymmetry**: v3 는 silent abstention 정책이라 confidence < 0.7 의 false negative 를 허용. 보수적 retrieve 가 필요한 도메인 (e.g. CDSS 임상결정지원 fork) 은 threshold 낮춰야 함.

**Inspiration**: 외부 CDSS MindVault fork 의 LLM-detected contradictions (fact-layer 4-type 중 detection 부분만 차용. 4-type 메모리 분류는 이번 범위 밖 (현재 detection 만 구현)).

- **Known limitations**: review CLI resolve 는 single-writer 가정 (동시 resolve / resolve-중-append race 미보호, 드뭄·재검출로 복구). supersede audit-trace 는 staged stem 기록 (promote 후 stale, decay 기능은 정상). backfill `--memory-dir custom` 은 prod index 인덱싱된 dir 만 유효.

### 핵심 원칙 (MindVault v1 실패 교훈)

- **Claude Code 내부에서만 작동** — 별도 서버, 별도 에이전트 없음
- **사용자 행동 제로 전제** — 수동으로 문서 만들지 않아도 자동 수집
- **로컬 MLX 서버로 운영비 제로** — Gemma (요약) `localhost:8080` + Arctic-ko (임베딩) `localhost:8081`
- **점진적 가치 약속** — "토큰 절약" 같은 드라마틱 약속 금지
- **검증 쉬운 목표** — "새 세션에서 딴소리 안 하면 성공"

### 스택

- Python (Claude Code 훅/스크립트 관례 따름)
- SQLite FTS5 + sqlite-vec (풀텍스트 + 벡터 hybrid)
- Gemma 4 E4B MLX (로컬 요약, `http://localhost:8080`, launchd `com.mindvault.gemma-mlx`)
- Arctic-ko v2.0 MLX (한국어 임베딩 1024dim, `http://localhost:8081`, launchd `com.mindvault.arctic-ko-mlx`)
- Claude Code 훅 시스템 (SessionStart / UserPromptSubmit / SessionEnd / Stop)

### 데이터 소스

Claude Code JSONL 로그: `~/.claude/projects/*/` 하위 모든 디렉토리의 `*.jsonl`. 사용자가 `cd` 위치에 따라 별도 projects 폴더(예: cwd=`/Users/<user>` → `-Users-<user>`, cwd=`/Users/<user>/foo` → `-Users-<user>-foo`)가 자동 생성되므로 모두 흡수 (Sprint 6).

### 최종 배포 경로 (완성 시)

- 훅: `~/.claude/hooks/session-memory.py`
- 스크립트: `~/.claude/scripts/mindvault/`
- 스킬: `~/.claude/commands/recall.md`
- 오픈소스 MIT 라이센스로 GitHub 공개 가능성 있음

---

## Three Man Team

Available agents: Arch (Architect), Bob (Builder), Richard (Reviewer)

---

## Token Rules

- Trust skills/memory — skip re-reading files
- No speculative tool calls
- Parallelize independent tool calls
- Route output > 20 lines to subagents
- Never restate what the user already said
