# MindVault

> Claude Code·Codex의 공유 영구 기억 시스템. 5-layer 파이프라인으로 세션 요약 자동 주입 · 자연어 검색 · Memory Compiler · 자동 회수 · 모순 감지까지 — 두 에이전트가 같은 기억을 회수·추출·저장·계측한다.

**v4.0.0** · **멀티에이전트 공유 기억** — Claude Code+Codex가 같은 MindVault를 전 생명주기(회수·추출·저장·계측)로 공유. Codex `UserPromptSubmit` 자동 회수(어댑터 없는 직결) + turn-scope `Stop` 자동 추출(rollout 로더, `(session,turn)` 멱등키) + close-session 저장 규약 + install.sh Codex 자동 통합(감지 시 hook·스킬·AGENTS 규약 배포) · `agent` 태깅 계측(claude/codex/unknown) · 교차 검수 왕복(Codex 검수 5결함 수정 수렴, 실운영 E2E 실증) · 이전: v3.10.0 Codex 회수 연결 · macOS (Apple Silicon) · MIT license · 959 passed + 41 subtests

---

## 무엇인가

Claude Code는 매 세션마다 컨텍스트 윈도우가 초기화됩니다. 어제 결정한 사실, 만들어 둔 CLI 위치, 진행 중인 프로젝트 상태 — 다음 세션을 열면 모두 사라집니다.

MindVault는 그 망각의 빈 자리를 네 축으로 메웁니다:

1. **세션 검색** — 모든 과거 .jsonl 로그를 SQLite FTS5 + 임베딩으로 인덱싱, `/recall` 자연어 검색
2. **메모리 회수** — UserPromptSubmit 마다 hybrid 검색으로 관련 메모리를 system-reminder에 자동 주입
3. **자동 컴파일** — SessionEnd 마다 로컬 Gemma가 그 세션에서 영구로 남길 가치가 있는 결정/노하우/사실을 추출, 검토 후 영구 메모리에 진입
4. **모순 감지 (v3.4+)** — 신규 메모리가 기존과 충돌 (metric 갱신·결정 반전·사실 정정) 시 Gemma 4-way 분류로 검출, 검토 후 신규가 옛 항목 deprecate / 본문 update / dismiss

로컬 Gemma + Arctic-ko 임베딩 서버라 API 비용 0원, 데이터 외부 전송 없음.

## 왜 이렇게 만들었나 (Karpathy LLM-as-Compiler)

> "매 쿼리마다 모든 원문을 LLM에 다시 던지지 말고, LLM을 한 번 컴파일러처럼 써서 지식을 정제한 뒤 그 결과를 작은 메모리로 축적해 가라. 다음부터는 정제된 결과만 조회하면 된다."
> — Andrej Karpathy, [LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) (요약)

```
# 기존 RAG 패턴
질문 → 외부 문서 N개 검색 → 그대로 컨텍스트에 붙임 → LLM이 매번 재해석
  ↑ 매번 비용. 매번 같은 노이즈. 매번 같은 추론.

# LLM-as-Compiler 패턴 (MindVault)
세션 끝 → Gemma가 한 번 정제 → 작은 .md로 저장 → 다음부턴 정제본만 조회
  ↑ 한 번 비용. 추출 후엔 깨끗한 신호. 누적되며 자기 강화.
```

## 핵심 개념 네 가지

| 개념 | 정의 |
|---|---|
| **Session** | Claude Code 한 번의 대화 단위. `~/.claude/projects/*/<uuid>.jsonl` 한 파일이 한 세션. |
| **Memory** | 다음 세션에도 쓸 가치가 있는 사실/결정/노하우. `memory/<kebab-name>.md` 한 파일이 한 메모리. |
| **Recall** | UserPromptSubmit 시 hybrid 검색으로 관련 메모리를 자동 회수해 `system-reminder`로 주입. |
| **Compile** | SessionEnd 시 Gemma가 로그를 읽고 새 메모리 후보를 `_procedural/_staged/` 에 자동 추출. |

## 5-Layer 아키텍처

| Layer | 책임 |
|---|---|
| **L1 — SessionStart 자동 주입** | 최근 5 세션을 Gemma로 요약해 새 세션에 자동 주입. 캐시 히트 ~50ms. **compaction 직후(`source=compact`)엔 요약 대신 현재 세션 관련 메모리만 경량 재주입 (v3.5+)** — 압축으로 사라진 회수 맥락 복원 |
| **L2 — `/recall` 자연어 검색** | JSONL FTS5 + Gemma 재순위 (sessions), Arctic-ko 임베딩 + FTS5 hybrid RRF (memory) |
| **L3 — Memory Compiler** | SessionEnd → Gemma 정제 → `memory/_procedural/_staged/` → `/memory_review` 승인 후 영구 진입 |
| **L4 — UserPromptSubmit hook** | 매 메시지 hybrid 검색 → 관련 메모리 system-reminder 주입. raw cosine 게이트 + query intent classifier가 잡담 차단 (false positive 0%) |
| **L5 — Contradiction Detection (v3.4+)** | 신규 메모리가 기존과 충돌 (metric 갱신·결정 반전·사실 정정) 시 Gemma 4-way 분류로 자동 검출. `python -m src.contradiction_review_cli` 로 검토 후 신규가 옛 항목 deprecate / 본문 update / dismiss. `deprecated_by` 메모리는 L4 회수 시 raw_cosine + score 둘 다 × 0.3 감쇠 |

## v3 본체 (Sprint 13~16 + NEXT-1~20)

| Sprint | 주제 | 상태 |
|---|---|---|
| 13 | Procedural Memory Slot (`memory/_procedural/_staged/`) | ✅ |
| 14 | Memory Compiler (SessionEnd → Gemma 정제 → staged) | ✅ `MV3_AUTO_COMPILE=1` |
| 15 | Self-eval Loop (internal effort · false positive · 자기충족 탐지) | ✅ |
| 16 | Query Intent Classifier + Multi-source 인덱싱 | ✅ |
| NEXT-1~7 | 자동 trigger · embed match · Gemma classifier · type-gate · diff color · slug conflict · scan cache | ✅ |
| NEXT-8 | PROJECTS_ROOT fix (dogfooding gap 해소, LLM-as-Compiler 첫 실증) | ✅ |
| NEXT-10~20 | ACK trigger · backfill · always-fire · cache · stats CLI · launchd 영구화 | ✅ |

**실측 (v3.2.0, 2026-05-25)**: **360 passed + 13 subtests** (v3.1.3 의 340 → 360, 신규 zero-touch install 테스트 20건 추가, 회귀 0). false positive 0.0%, internal effort 0.60, **hook 실효 hit rate picked>0 = 66.3%** (n=3,193, 2026-05-23~25 carry-forward — hook 로직 변경 없음). 자기충족 메모리 탐지 8건, extractor nonzero rate 20% → 47%. v3.2.0 변경은 install.sh / uninstall.sh / 신규 plist·runner·convert 헬퍼만이라 측정 그대로 적용.

**Latency** (n=3,193): **p50=40ms, p95=400ms, p99=471ms**. timeout(≥400ms) skip ~6%. v3.0.x post-ship perf 회귀 (avg 452ms) 는 해소 (NEXT-27/28 fix + alias_index/intent cache 운영 누적).

> 옛 표기 추적:
> - "295 passed" (v3.0.0) → "307" (v3.0.1) → "311" (v3.0.2) → "327" (v3.1.0) → "340" (v3.1.1/v3.1.2/v3.1.3) → "360" (v3.2.0/v3.2.1) → "363" (v3.2.2) → "384" (v3.2.3/v3.2.4) → **"392" (v3.2.5/v3.2.6)**
> - "hit rate 2.6%" (measurement artifact) → "55.7%" (v3.0.2 audit 시점) → "66.3%" (v3.2.0 cohort n=3,193) → **"83.0%"** (v3.2.5 누적 n=6,781)
> - "avg 452ms" (NEXT-27 이전) → **"p50=40ms"** (현재)
> - "Gemma 수동 사전 설치" (v3.0~v3.1.3) → **"install.sh 자동" (v3.2.0)**
> - "Arctic-ko 모델 수동 변환" (v3.0~v3.1.3) → **"install.sh 자동 변환" (v3.2.0)**
> - "Apple Silicon 전용 (v3.2.0~) — v3.3.0 백엔드 추상화 이후 가능" (v3.2.0 README) → **"Intel/Linux/Windows 사용 불가, 로컬 LLM 은 Gemma 4 E4B 만 지원" (v3.2.1 README 정확화, 미래 예고 제거)**

## 요구사항 / 지원 환경

### 운영체제 / 하드웨어

- ✅ **macOS, Apple Silicon (M1 / M2 / M3 / M4)** — 유일한 fully-supported 환경
- ⚠️ **Intel Mac** — `install.sh` 가드가 인프라-only 설치 (`MV3_SKIP_MODELS=1`) 를 옵션으로 제공하지만 MLX 모델은 동작 안 함 → 메모리 회수·Memory Compiler 둘 다 비활성 (FTS5 만)
- ❌ **Linux / Windows** — 사용 불가 (launchd 의존)

### 로컬 LLM

- ✅ **Gemma 4 E4B (`mlx-community/gemma-4-e4b-it-4bit`)** — 유일한 지원 모델
  - **사전 설치 안 된 환경**: `install.sh` 가 자동 다운로드 + launchd 등록 (`com.mindvault.gemma-mlx`, port 8080, ~3GB)
  - **사전 설치된 환경** (예: `com.<user>.gemma-mlx` 가 이미 port 8080 점유 중): `install.sh` 가 자동 감지 후 재사용 (신규 plist 설치 skip)
- ❌ **ollama / LM Studio / llama.cpp / OpenAI API / Qwen / Llama / 기타 LLM** — 사용 불가

### 임베딩 모델

- ✅ **Arctic-ko (`dragonkue/snowflake-arctic-embed-l-v2.0-ko`, MLX 4bit)** — 유일한 지원 모델
  - `install.sh` 가 자동 다운로드 + 4bit 양자화 변환 + launchd 등록 (port 8081, ~322MB)

### 기타

- **Python 3.10+**
- **Claude Code** (hook 등록을 위해)

## 설치

```bash
git clone https://github.com/etinpres/mindvault.git
cd mindvault
./install.sh
```

설치 내용:
- `~/.claude/hooks/` 에 hook 스크립트들 복사
- `~/.claude/scripts/mindvault/` 에 인덱서/검색 모듈 배포
- `~/.claude/commands/` 에 `/recall`, `/memory_review`, `/close-session`, `/cs` 스킬 등록
- `~/.claude/settings.json` 의 hook 배열에 등록 (`.bak` 자동 백업)
- Arctic-ko launchd 서비스 등록 (사용자별 `$HOME` 자동 치환)
- **(v3.2.0) Sprint 4.5 — Arctic-ko 4bit 모델 자동 변환** (원본 ~1.1GB DL + ~322MB 4bit 양자화)
- **(v3.2.0) Sprint 17 — Gemma 자동 설치** (`mlx-lm` pip + 모델 ~3GB DL + launchd `com.mindvault.gemma-mlx`)
- **(v3.2.0) Apple Silicon 가드** — 비 arm64 환경에서는 인프라만 설치 (모델 자동 설치 skip)
- **(v3.2.0) Resumable checkpoint** — 중간 실패 시 step 파일 (`~/.cache/{mv3-gemma,mlx-arctic-ko}/.mv3-step`) 기반으로 재실행 시 이어감
- 초기 인덱싱 1회 실행

첫 실행 소요: **8~12분** (모델 DL/변환). 재실행은 idempotent — 이미 있는 자산 자동 skip.

설치 확인: 새 `claude` 세션을 열면 system-reminder에 `# 지난 세션 요약 (MindVault v3)` 블록이 나타납니다.

### Codex와 기억 공유

`./install.sh`가 `~/.codex/`를 감지하면 Codex 통합을 자동으로 설치합니다 — 회수 훅 등록, `$close-session` 스킬(`~/.agents/skills/close-session/`) 배포, `AGENTS.md`에 MindVault 메모리 규약 삽입(`<!-- MINDVAULT_MEMORY_* -->` 마커 사이 idempotent 갱신, 기존 내용 보존). Codex가 없는 환경에서는 이 단계가 조용히 생략됩니다. 훅만 수동으로 다루려면:

```bash
python3 scripts/manage_codex_recall.py install
```

이 명령은 기존 `~/.codex/hooks.json` 항목을 보존하면서 `UserPromptSubmit`에 배포된 `~/.claude/hooks/memory-recall.py`를 읽기 전용 회수 훅으로 연결합니다. Codex는 최초 실행 전 `/hooks`에서 새 훅을 검토·신뢰해야 하며, 변경 사항은 새 Codex 세션부터 적용됩니다.

```bash
python3 scripts/manage_codex_recall.py status
python3 scripts/manage_codex_recall.py uninstall
```

Codex와 Claude Code가 같은 메모리 디렉토리에 쓰는 환경에서는 이 연결로 양쪽 모두 같은 hybrid FTS5+vector 검색 결과를 자동 주입받습니다. 이는 영구 메모리 공유이며, 아직 저장되지 않은 실시간 대화 전체를 동기화하지는 않습니다.

## 사용

### `/recall` — 자연어 검색

```
/recall 영어 학습 망각곡선
/recall 이메일 SMTP 설정
/recall MindVault 마이그레이션 결정
```

FTS5 (키워드) + Arctic-ko 임베딩 (의미) 두 결과를 RRF로 결합한 뒤 Gemma가 재순위. 관련 세션 + 메모리 동시 검색.

### `/memory_review` — staged 메모리 검토

SessionEnd마다 Gemma가 자동 추출한 메모리 후보는 `memory/_procedural/_staged/` 에 임시 저장됩니다. 자동으로 영구 메모리에 들어가지 않습니다 (silent failure 방지).

```
/memory_review              # staged 후보 목록 (메인 Claude 가 인터랙티브 처리)
```

명령은 인자 없음. 메인 Claude 가 후보 목록을 보여준 뒤 사용자가 번호+코드로 응답:
- `1y` — 1번 항목 승인 (영구 메모리로 진입)
- `2n` — 2번 항목 폐기
- `3e` — 3번 항목 본문 편집 후 승인
- `4s` — 4번 항목 다음 세션으로 건너뜀

내부적으로 `python3 memory_review_cli.py {approve|reject} <file>` 만 호출 (subcommand 직접 입력 불가).

### 자동 회수

UserPromptSubmit hook이 매 메시지마다 hybrid 검색을 돌립니다. 관련 메모리는 `<system-reminder>` 태그로 Claude의 컨텍스트에 자동 주입되며, 사용자가 명령을 내릴 필요가 없습니다.

raw cosine 게이트 (Arctic-ko 기준 default 0.32, 회수 단서어 시 0.27 완화 — NEXT-30.1 운영 누적 후 fine-tune) + query intent classifier (chat/meta/code/recall/unknown) 가 잡담 쿼리에서 무관 메모리 끼는 false positive를 차단합니다. 임계값은 `hooks/memory-recall.py` 의 `RAW_COSINE_MIN_*` 상수.

## 자기-수정 메커니즘

메모리 시스템의 echo chamber (잘못된 메모리가 자기를 강화) 방지 장치:

- **자기충족 메모리 자동 탐지** — `scan_self_affirming_memories` 가 "v1 폐기 / v2 운영" 류 자기-진화 표현을 후보로 표시
- **False positive 측정** — 회수 직후 사용자가 negative cue ("그거 아니야") 발화하는지 추적
- **자기 모순 메모리** — 운영 누적 시 자동 탐지 → 검토 큐 진입
- **Type-gate 분리** — procedural 타입은 raw cosine 게이트 0.05 더 엄격하게 (운영 노이즈 차단)

## 설정

`src/session_memory.py` 상단 상수 섹션:

| 상수 | 기본값 | 설명 |
|---|---|---|
| `MAX_SESSIONS` | 5 | L1 요약 대상 세션 수 |
| `MAX_HEAD_TURNS` | 6 | 각 세션 앞쪽 턴 수 |
| `MAX_TAIL_TURNS` | 6 | 각 세션 뒤쪽 턴 수 |
| `MAX_MSG_CHARS` | 200 | 각 메시지 최대 글자 |
| `GEMMA_MAX_TOKENS` | 2000 | Gemma 응답 토큰 한도 |
| `GEMMA_TIMEOUT` | 45 | Gemma 호출 타임아웃(초) |
| `CACHE_DAYS` | 7 | 캐시 보존 기간 |

### 환경 변수

| 변수 | 기본 | 설명 |
|---|---|---|
| `MV3_AUTO_COMPILE` | 0 | SessionEnd Memory Compiler opt-in |
| `MV3_EXTRACTOR_ALWAYS_FIRE` | 0 | trigger 휴리스틱 없이도 항상 추출 시도 |
| `MV3_GEMMA_INTENT` | 0 | Query intent classifier에 Gemma 보강 opt-in |
| `MV3_EXTRA_MEMORY_DIRS` | (empty) | 추가 메모리 디렉토리 (`:` 구분) |
| `MV3_EXTRACTOR_CACHE_DISABLE` | 0 | extractor 결과 캐시 비활성화 |
| `MV3_PROJECTS_DIR` | derived | primary memory base 디렉토리 (기본: `$HOME` 에서 자동 derive) |

## 테스트

```bash
pytest tests/
```

**470 passed + 25 subtests** (회귀 0, v3.3.0 기준 카운트). functional 전부 OK, v3.0.x 의 e2e perf 회귀 (avg 452ms > 150ms) 는 NEXT-27/28 fix 로 해소 (현재 p50=39ms). 네트워크/Gemma 불필요 (mocked).

## 제거

```bash
./uninstall.sh
```

훅 등록 + 스크립트 + 스킬 + launchd 서비스 일괄 제거:

- 선택 설치한 Codex `UserPromptSubmit` MindVault 회수 훅
- `com.mindvault.gemma-mlx` (v3.2.0+ Gemma)
- `com.mindvault.arctic-ko-mlx` (v3.2.3+ Arctic-ko)
- legacy: `com.yonghaekim.{arctic-ko-mlx,bge-m3-mlx,mv3-env,mv3-gemma-intent,mv3-stats-daily}` (옛 설치자 호환 cleanup)

Settings backup 은 `~/.claude/settings.json.bak` 으로 자동 저장. `install.sh` 가 사용자 personal `~/.claude/skills/{close-session,cs}/` 를 attic 으로 displace 했었다면 `uninstall.sh` 가 manifest 보고 자동 복원합니다 (v3.2.3 #15).

캐시 + 인덱스는 보존 (수동 삭제):
```bash
rm -rf ~/.claude/mindvault-v3
rm -rf ~/.cache/mlx-arctic-ko
```

## 알려진 한계

1. **Apple Silicon Mac 전용** — Intel Mac / Linux / Windows 사용 불가. MLX (`mlx-lm`, `mlx-embeddings`) 가 Apple Silicon 만 지원하고 launchd 도 macOS 전용.
2. **로컬 LLM 은 Gemma 4 E4B (`mlx-community/gemma-4-e4b-it-4bit`) 만 지원** — ollama / LM Studio / llama.cpp / OpenAI API / 기타 LLM 사용 불가. 사전 설치 안 된 환경은 `install.sh` 가 자동 설치, 사전 설치된 환경은 자동 감지.
3. **임베딩은 Arctic-ko (`dragonkue/snowflake-arctic-embed-l-v2.0-ko`, MLX 4bit) 만 지원** — 다른 임베딩 모델 사용 불가.
4. **Gemma 4 E4B는 reasoning 모델** — 내부 사고에 토큰 많이 소비, `GEMMA_MAX_TOKENS` 크게 잡아야 함
5. **한국어 특화 프롬프트** — 프롬프트가 한국어로 최적화. 영어 도메인은 검색 품질 일부 저하 가능
6. **PII 필터는 키 패턴만** — 이메일/전화는 로컬 전용이라 통과
7. **세션 경계 = JSONL 파일** — 한 파일 안에서 주제 바뀌어도 하나로 간주

## 디버깅

```bash
# 훅 실행 로그
tail ~/.claude/mindvault-v3/debug.log

# 캐시
ls ~/.claude/mindvault-v3/cache/

# Arctic-ko 헬스체크
curl http://localhost:8081/health
# {"ok":true,"model":"arctic-ko-mlx-4bit","dim":1024}

# Arctic-ko 재기동
launchctl kickstart -k gui/$(id -u)/com.<author>.arctic-ko-mlx

# 메모리 인덱스 강제 재구축
python3 -c "
import sys; from pathlib import Path
sys.path.insert(0, str(Path.home() / '.claude/scripts/mindvault'))
from memory_indexer import full_rebuild
full_rebuild()
"

# DB 상태
python3 -c "
import sqlite3
from pathlib import Path
db = Path.home() / '.claude/mindvault-v3/index.db'
c = sqlite3.connect(db)
for t in ('sessions','memories','memories_fts','memories_vec'):
    print(t, c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
"
```

## 라이선스

MIT — 자세한 내용은 [LICENSE](LICENSE) 참조 (또는 별도 LICENSE 파일이 없는 경우 표준 MIT 적용).

## 기여 / 영감

이 프로젝트는 [Andrej Karpathy의 LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 의 LLM-as-Compiler 패턴을 Claude Code 환경에서 실증한 결과입니다.

이전에 같은 저자가 만든 [etinpres/mindvault](https://github.com/etinpres/mindvault) (deprecated) 는 이 패턴을 잘못 이해한 채 진행한 첫 시도였습니다. v3는 그 postmortem 교훈을 반영한 재시도입니다 — Gemma가 실제로 메모리를 정제·합성하는 구조로.

이슈와 PR을 환영합니다. macOS 외 플랫폼 포팅에 관심 있다면 환영합니다.
