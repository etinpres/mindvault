# Phase 1 ③ — 신뢰성 검증 (over-trust 해소 / stale 자동 감지) 설계 결정

- **작성일**: 2026-05-31
- **상태**: Draft (사용자 검토 대기 — bg 세션 작성, 형 판단 플래그 §7 참조)
- **상위 문서(단일 진실원천)**: `docs/specs/2026-05-30-second-brain-roadmap-design.md` §4.2③, §4.3-3, §4.4, §5
- **선행 작업**: Phase 1 ①Provenance (v3.6.0, 출처 라벨 부착), ②효과적 회수 (v3.7.0, self-check 계약) — 둘 다 출시·머지·배포 완료
- **기준 버전**: master `0a28fea` (v3.7.0 태그, origin 동기)

> 이 문서는 로드맵 spec §4.2③이 남긴 설계 결정(재검증 트리거·대상 선별·stale 판정 방식·flag frontmatter 형태·회수 시 처리·Layer 5 관계)을 구현 가능한 형태로 확정하는 **focused 결정 기록**이다. 비전·로드맵의 단일 진실원천은 상위 문서이고, 본 문서는 그 §4.2③를 좁힌다. ③은 Phase 1(토대: 신뢰 가능한 효과적 회수)을 닫는 마지막 축이다.

---

## 0. 한 줄 요약

회수된 stale 메모리를 검증 없이 믿는 **over-trust** 를 해소하기 위해, 코드/사실 참조(모델명·포트·버전)를 담은 메모리를 **현행 코드와 결정론적으로 대조**(Canonical Facts Registry + 라이브 verifier)해 stale 의심을 자동 판정하고, 그 결과를 메모리 frontmatter 의 flat `reverify_*` 키로 기록하며, **회수 시 stale 의심 메모리에 짧은 경고 라벨**을 동반 주입한다. 2026-05-30 "BGE-M3 → Arctic-ko" stale 사고를 end-to-end 회귀 케이스로 고정한다(완료 게이트).

---

## 1. 문제 (실측 근거)

- **over-trust 사고 (2026-05-30, 본 로드맵 작성 대화에서 재현)**: 어시스턴트가 회수된 메모리의 stale 한 "BGE-M3" 임베딩 표기를 믿고 시스템 상태를 틀리게 답함. 실제 현행은 Arctic-ko v2.0 (Sprint 9/14 교체).
- **실측 grep (`~/.claude/projects/<home-slug>/memory/`)** — stale 주장 vs 정당한 이력의 경계가 데이터에 그대로 드러남:
  - **stale 주장 (현재형, 현행 값 미언급)**: `feedback_no_v1_token_waste.md:15` "BGE-M3 임베딩이 형 메시지 어디든 0.7+ 매칭…" / `feedback_no_api_default.md:21` "임베딩처럼 … 로컬(fastembed/BGE-M3)으로". 모델명이 현행과 다른데 'arctic' 언급 없음.
  - **정당한 이력 (현행 값 동반 명시)**: `project_mindvault.md:17` "임베딩: …arctic… (Sprint 9 BGE-M3 → 교체)" / `project_mindvault_v1v2_history.md` "v2 BGE-M3 → v3 Arctic-ko 교체". 'BGE-M3' 와 'arctic' 을 **함께** 담아 전환을 명시 → stale 아님.
  - **(부수 발견) 코드 주석 stale**: `src/memory_search.py:516` docstring "BGE-M3는 잡담에도 0.6-0.75 매칭" — 코드 내 stale 표기. 메모리 scan 범위 밖이라 ③에서는 보고만(§7).
- **뿌리**: spec §1.2 — under-integration(②가 해결)과 over-trust(③)는 같은 뿌리("출처 없는 주장")에서 나온다. ①Provenance 가 출처 라벨로 검증 *경로*를 깔았고, ③은 그 위에서 **현행 대조로 stale 을 능동 검출·경고**한다.

---

## 2. 핵심 판별 신호 (설계의 심장)

stale 자동 감지의 난점은 "현재형 stale 주장"과 "정당한 역사 기록"을 구분하는 것이다(둘 다 'BGE-M3' 토큰을 포함). 실측 데이터에서 검증된 결정론적 판별 신호:

> **메모리가 `stale_alias` 토큰을 포함하면서 동시에 `current_value` 토큰을 포함하지 않으면 stale 의심.** (현행 값을 언급하지 않고 옛 값을 참조 = "전환을 모르는 채 옛 사실을 현재처럼 진술")

- stale 메모리(`feedback_no_v1_token_waste`)는 'BGE-M3' 포함 + 'arctic' 미포함 → **flag**.
- 이력 메모리(`project_mindvault*`)는 'BGE-M3' 포함 + 'arctic' 포함 → **면제**.
- 무관 메모리('arctic' 만, 'BGE' 없음)는 stale 후보 아님 → 대상 외.

이 신호는 (a) 결정론적(LLM 불필요, 운영비 0), (b) 실측 4종 메모리에 모두 정확, (c) verifier 로 "현행 값이 진짜 current 인가"를 라이브 확인해 hardcoded blocklist 의 메타-staleness 까지 차단한다.

---

## 3. 설계 결정 (D1~D10)

### D1. Layer 5 모순감지와 **별도 모듈** (`src/reverify.py`)

모순(Layer 5, `contradiction_detector`)과 stale(③)은 *비교 상대*가 다르다:
- **모순** = memory **vs** memory (두 기억이 서로 충돌) → Gemma LLM 4-way 분류, confidence ≥ 0.7 게이트, review queue + resolve CLI.
- **stale** = memory **vs 현행 코드/사실** (기억이 ground truth 와 충돌) → 결정론적 코드 대조, frontmatter status 기록 + recall 라벨.

- **근거**: ground truth(다른 기억 vs 라이브 코드)와 메커니즘(LLM vs grep)이 다르므로 한 모듈에 섞으면 양쪽 다 흐려진다. 별도 모듈로 두면 `contradiction_detector` 는 **무손상**(spec 제약: "Layer 5 유지·강화")이고, ③은 Layer 6(신뢰성/stale)로 독립 진화 가능.
- **재사용**: contradiction 의 *검증된 패턴*(jsonl 직렬화·atomic rewrite·silent-fail·CLI ergonomics)은 차용하되 코드는 공유하지 않는다.
- **대안 (기각)**: `contradiction_detector` 에 stale-kind 추가. → 결정론 검사를 LLM 파이프라인에 끼워 넣으면 confidence 게이트 의미가 깨지고(코드 대조는 0/1), Layer 5 회귀 위험.

### D2. 판정 = **결정론적 Canonical Facts Registry + 라이브 verifier** (Gemma 미사용)

stale 판정은 코드 대조 기반 결정론으로 한다. Gemma LLM 판정은 쓰지 않는다.

- **근거**: (a) **운영비 0** 가장 강하게 준수(메모리당 LLM 호출 없음), (b) 완료 게이트(BGE 회귀)가 결정론이라 CI 에서 정확히 pin 가능, (c) LLM 의 비결정·false-positive 없음. §2 판별 신호가 실측에 정확하므로 LLM 불요.
- **대안 (기각/보류)**: Gemma 가 "이 주장이 현행과 일치하나" 판정. → 운영비 발생·비결정. history-vs-current 모호 케이스의 adjudicator 로는 가치 있으나, §2 신호가 이미 그 경계를 결정론적으로 처리하므로 **Phase 1 ③ 비범위**(deferred enhancement §8).
- **대안 (기각)**: 순수 정규식 blocklist("BGE 나오면 stale"). → blocklist 자체가 stale 됨(코드가 또 바뀌면?). verifier 가 이를 막는다(D3).

### D3. Registry 구조 + **verifier self-check** (registry 메타-staleness 방지)

`reverify.py` 에 버전관리되는 canonical facts 레지스트리를 둔다. 각 fact:

```python
CanonicalFact(
    key="embedding_model",
    current_value="arctic",            # 현행 진실(토큰)
    stale_aliases=["bge-m3", "bge"],   # 현재처럼 주장되면 stale 인 옛 토큰
    verifier=lambda root: _grep_present(root, "src/memory_indexer.py", r"arctic"),
    description="임베딩 모델 (Sprint 9/14 BGE-M3 → Arctic-ko 교체)",
)
```

초기 facts (실측 stale 위험 + 결정론적 검증 가능 — verifier 가 라이브 통과 확인됨):
- `embedding_model`: `arctic` (verifier: `src/memory_indexer.py` 에 'arctic' 존재 — line 40/192 ✓) ↔ stale {bge-m3, bge_m3}
- `embedding_port`: `8081` (verifier: `src/memory_indexer.py:37` `EMBED_URL=…:8081` ✓) ↔ stale {8765} (실측 `feedback_systematic_debugging_code_review.md` 옛 포트 오기)
- (확장 가능 — §7 형이 summarizer 포트·버전·파일경로 등 fact 한 줄씩 추가 지정)

> 주의: stale 토큰 'bge' 는 `src/eval_arctic_ko_ab.py` 에 HISTORICAL 로 잔존하나, ③ scan 은 **메모리** 만 보고 verifier 는 `memory_indexer.py`/`memory_search.py` 만 grep 하므로 그 코드 파일과 간섭 없음.

- **verifier 의 역할**: 매 reverify 실행 시 각 fact 의 `current_value` 가 **여전히 코드에 존재**하는지 grep 으로 확인한다. verifier 가 fail 하면(예: 코드가 arctic 을 또 교체) **레지스트리 자체를 stale 로 경고** — registry 가 조용히 거짓말하는 것을 차단(self-check, `verify-registry` CLI). banned-words 리스트보다 낫다(리스트가 또 stale 되는 것을 verifier 가 막음).
- **verifier 의 한계(솔직히)**: grep 은 "토큰이 파일에 등장"만 확인하므로 **휴리스틱**이다 — 현행 임베딩 모델명 'arctic' 은 `memory_indexer.py` 의 주석/docstring(line 40/192)에 등장하고 코드 statement 가 아니다(모델은 서버사이드 endpoint). 미래에 모델을 또 교체하면서 'arctic' 주석만 남겨두면 verifier 가 속을 수 있다. `embedding_port`(`EMBED_URL=…:8081`)는 코드 statement 라 더 강하다. `verify-registry` 가 사람-검토 안전판(주기적). 즉 "라이브 ground truth 대조"는 강한 표현이고, 정확히는 "현행 코드 파일이 그 토큰을 아직 담고 있는지"의 휴리스틱 검증이다.
- **근거**: spec §4.2③ "현행 코드와 대조".

### D4. 대상 선별 = **전체 메모리 content scan + §2 판별 신호** (opt-in 태그 불요)

모든 메모리(provenance_backfill `_collect_files` 와 동일 범위: `*.md` + `_procedural/*.md`, `MEMORY.md`·`_staged` 제외)를 scan 한다. 각 메모리에 대해 각 fact 의 §2 신호(`contains(stale_alias) AND NOT contains(current_value) AND verifier_confirms`)를 적용한다.

- **근거**: §2 신호가 "현행 값 동반 여부"로 history/current 를 결정론 구분하므로, 어떤 메모리가 "코드/사실 참조"인지 **사전 opt-in 태그가 불필요**하다. 대상 선별 = "canonical stale_alias 토큰을 현행 값 없이 포함하는 메모리"로 자연 도출. frontmatter 는 *선별*이 아니라 *결과 저장*에만 쓴다(D5).
- **대안 (기각)**: frontmatter `reverify:` opt-in 태그로만 선별. → 누가 태그? 171개 기존 메모리 backfill 필요 + Memory Compiler 변경. content scan 이 그 단계를 없앤다(단순·완전).
- **대안 (기각)**: 역사 qualifier 어휘 목록("교체|→|v1|v2|이력") 면제. → §2 의 "현행 값 토큰 포함" 검사가 이를 subsume(이력은 항상 현행 값을 함께 언급). 어휘 목록은 fragile, 불요.
- **토큰 매칭 정밀도(구현 확정)**: `_contains_token` 은 라틴 영숫자 경계 `(?<![A-Za-z0-9])tok(?![A-Za-z0-9])`(대소문자 무시)로 매칭한다 — `subarctic`/`18081` 안의 부분어는 차단하되 한국어 인접(`arctic임베딩`)·하이픈(`arctic-ko`)은 허용. `\b` 는 한국어를 `\w` 로 봐 `arctic임` 인접을 잘못 끊으므로 미사용. stale_aliases 는 full token(`bge-m3`,`bge_m3`,`bge m3`; bare `bge` 미사용 — 오매칭 회피). 숫자 토큰은 동일 경계로 `8081 ≠ 18081`. (한계: `bge-m3.5` 처럼 alias 를 접두로 갖는 가상 후속 토큰은 매칭됨 — 경고는 soft 라 저비용, §6.)

### D5. flag frontmatter = **flat 키, stale 메모리에만** + 스캔 주기는 **sidecar**

stale 로 판정된 메모리에만 평면 키를 기록한다(fresh 메모리는 무손상 — 171개 전체 오염 회피):

```yaml
reverify_status: stale        # stale 일 때만 존재 (부재 = 미flag)
reverify_checked: 2026-05-31
reverify_note: embedding_model 현재형 참조 bge-m3 (현행 arctic 미언급)
```

> note 값은 **콜론 없는 YAML plain scalar** 로 쓴다(audit fix). `reverify_note` 에 `: `(콜론+공백)가 들어가면 `yaml.safe_load` 가 깨져 `parse_frontmatter` 가 `{}` 를 반환 → 회수 라벨 미렌더. 따라서 finding 문구는 콜론 없는 형태로 생성하고, §7 레지스트리 확장 시 fact 토큰도 YAML-plain-safe(콜론 금지) 여야 한다.

- **flag-only-stale**: stale → `reverify_*` upsert. 이전에 stale 이었으나 이제 fresh(형이 메모리 수정) → 키 **제거**(cleanup). fresh → no-op(파일 무변경). 즉 frontmatter 에 `reverify_status` 가 있으면 곧 "현재 stale 의심" — 회수 라벨이 바로 이 키로 동작.
- **스캔 주기는 sidecar**: 마지막 scan 시각은 per-file `reverify_checked` 가 아니라 런타임 sidecar `~/.claude/mindvault-v3/reverify_state.json` (`{"last_scan": "<iso>"}`)에 둔다. 증분 트리거(D7)가 이 값으로 "scan due?" 판단 — 매 SessionEnd 마다 171 파일을 다시 안 건드림.
- **근거**: (a) `write_staged` 라인 파서·`parse_frontmatter`(yaml)·hot-path cheap regex(`_is_deprecated` 식) **세 경로 모두 호환** — 중첩 dict 는 라인 파서를 깸. (b) `reverify_note` 가 회수 경고 라벨 본문 제공. (c) `deprecated_by` regex 검출 패턴 미러. (d) flag-only-stale 는 사용자 메모리 churn 을 최소화(§4.4 "본문 auto-edit 안 함" 정신 — 메타도 필요한 곳만).
- **대안 (기각)**: 모든 메모리에 `reverify_status: fresh` 기록 + per-file `reverify_checked`. → 171 파일 전부 frontmatter 변경(git/메모리 노이즈 + 라인파서 위험 확대), churn 큼. sidecar + flag-only-stale 가 더 깨끗.
- **대안 (기각)**: 중첩 `reverify: {status, checked, findings}`. → 라인 파서 비호환 + hot-path yaml 전체 파싱 강요.

### D6. 회수 시 처리 = **per-memory 경고 라벨** (감쇠 아님)

`reverify_status: stale` 인 메모리가 회수되면, 그 항목 바로 아래에 짧은 경고 라벨 1줄을 동반 주입한다(`출처:` 라인과 동형의 conditional 라인). 감쇠(score×factor)는 하지 않는다.

- **근거**:
  - **over-trust 직접 해소**: 사고는 "메모리를 *보고도* 검증 없이 믿음"이다. 라벨이 어시스턴트에게 "이 메모리의 코드/사실 참조는 현행과 다를 수 있음 — 확인 후 신뢰"를 명시 → 검증 단계 강제. 이것이 사고의 정확한 fix.
  - **TOP_K=1 정보손실 회피**: 감쇠는 유일한 회수 결과를 떨궈 유용한 정보를 통째로 잃을 수 있다. stale 의심 ≠ 무가치(옛 모델명이라도 교훈은 유효할 수 있음). 숨기지 말고 경고한다.
  - **false-positive 저비용**: 라벨은 "현행 확인하라"이고, history 메모리에 잘못 붙어도 그 조언은 옳고 해롭지 않다. 반면 감쇠 false-positive 는 정보를 숨긴다.
  - **감쇠는 이미 deprecated_by 담당**: confirmed-supersede(모순 review resolve) 메모리는 `deprecated_by` 가 score×0.3. stale 의심은 그보다 약한 신호라 경고가 적정.
- **라벨 제약 (v1 토큰낭비 금지)**: stale 메모리에만, 1줄, ≤~60자. 비-stale 회수엔 토큰 0 증가. 라벨에 `[name]` 대괄호 없음 → `RECALLED_NAME_RE` 오추출 차단. `</system-reminder>` sanitize 적용.
- **대안 (기각)**: score×factor 감쇠. → TOP_K=1 정보손실 + false-positive 가 정보 은폐. (형 승인 시 confirmed-stale 한정 병행 가능 — §7.)
- **결과 dict 부착**: recall 결과에 `provenance` 를 frontmatter 에서 읽어 붙이는 기존 블록(`memory_search.py:677-688`)에 `reverify`(status/note)를 동일 방식으로 추가. 양 포맷터는 `provenance` conditional 라인과 동형으로 라벨 렌더 → parity 패턴 무변경.

### D7. 트리거 = **best-effort SessionEnd 증분 + 수동 CLI**

- **메커니즘(핵심)**: `reverify.py` 의 순수 검사 함수 + `reverify_cli.py`(scan/list/verify-registry). 항상 수동 호출 가능, 완전 단위테스트 가능 — 완료 게이트가 pin 하는 대상.
- **트리거(주기적)**: `session_memory_end.py:main()` 의 best-effort step 사슬(compile→index_sync→alias_sync, 각 try/except silent-fail) 끝에 reverify 증분 step 추가. **증분 조건**: sidecar(D5) `last_scan` 이 `REVERIFY_INTERVAL_DAYS`(default 7) 보다 오래됐을 때만 scan → 사실상 주 1회, silent-fail. SessionEnd 는 이미 nohup detach 컨텍스트라 추가 비용 OK(결정론 grep, LLM 없음).
- **근거**: spec §4.2③ "주기적으로 대조" 충족 + **CC 내부 전용**(새 launchd cron 불요, 기존 async 인프라 재사용). 활성화는 install.sh 재배포(형 승인) 후 발효 — 코드 머지까지가 본 작업 범위.
- **대안 (보류)**: 별도 launchd cron(`mv3-stats-daily` 선례 있음). → 새 배포 surface. 형이 원하면 후속 선택(§7).
- **대안 (기각)**: 회수 시점 lazy 검증(매 UserPromptSubmit 코드 grep). → hot-path latency 예산(p95~400ms, hard timeout) 위반. ✗

### D8. 자동화 경계 (spec §4.4 준수)

- **자동화 OK**: `reverify_status`/`reverify_note` 자동 기록 + 회수 경고 라벨 자동 렌더. (§4.4: "재검증 후 플래그링은 자동화 범위 포함 가능".)
- **자동화 금지**: (a) 회수 raw_cosine/score 임계값 **auto-tune 안 함**(§4.4 잘못 학습된 loop 위험). (b) 메모리 **본문 auto-edit 안 함**(reverify_* 메타만 기록 — 사용자 데이터 보존). (c) stale 메모리 **auto-delete/auto-supersede 안 함**(경고만 — resolve 는 형/후속 모순 파이프라인 몫).
- **근거**: 경계를 명확히 — ③은 "현행 대조로 flag·경고"까지가 자동화, 그 이상의 회수 동작 변경은 형 판단.

### D9. 메모리 파일 frontmatter rewrite = **atomic·body 보존·idempotent**

reverify scan 은 기존 사용자 메모리 파일을 *변경*(reverify_* 키 추가/갱신)하므로 안전 계약:
- **atomic**: tmp + `os.replace` (`write_staged`·`provenance_backfill` 동일 패턴).
- **보존**: 본문 + 기존 frontmatter 키를 정확히 보존, reverify_* 키만 upsert. close-fence tolerant 검출(`provenance_backfill.backfill_file:59` 패턴).
- **idempotent**: 같은 입력 재실행 시 동일 출력(이미 같은 status 면 no-write).
- **pure function**: `upsert_reverify_frontmatter(text, status, note, checked) -> new_text` 분리 → characterization 테스트(no-frontmatter / 기존 키 갱신 / body 보존 / idempotent).
- **대상 범위**: prod 메모리 dir(`MV3_MEMORY_DIR` resolver) — 테스트는 tmp dir + 주입 root 로 prod 무오염.

### D10. parity 불변식 유지 (제약)

- 회수 경고 라벨을 `hooks/memory-recall.py:_format_output` ↔ `src/recall_core.py:format_memory_context` **양쪽 동형**으로 추가(provenance 라인과 같은 conditional 패턴).
- `tests/test_recall_core_parity.py::test_formatter_byte_equivalence`(byte 동일), `RECALLED_NAME_RE` 추출, `sanitize`(`</system-reminder>` 누출 차단), "회수 노트:" + ②self-check 계약 회귀 모두 통과 유지.
- compact 재주입(`session_memory.py` → `format_memory_context`)은 라벨 자동 전파(의도된 일관 전파 — compact 회수도 경고).
- 새 ingestion 회귀 테스트: 라벨 라인이 `extract_recalled_ids_from_hook_injection` 의 name 추출 noise 를 만들지 않음을 고정(②의 `test_new_contract_preserves_self_eval_ingestion` 패턴).

---

## 4. 아키텍처 요약 (데이터 흐름)

```
[메모리 작성/이력]                       [라이브 코드 ground truth]
        │                                        │
        ▼   (SessionEnd 증분 / 수동 CLI)           ▼
  reverify.scan_memories(root) ── 각 메모리 × 각 CanonicalFact ──▶ verifier(root) 확인
        │                                        │
        │  §2 신호: contains(alias) & !contains(current) & verifier_ok
        ▼
  upsert_reverify_frontmatter(status, note, checked)  ◀── atomic, body 보존 (D9)
        │
        ▼  (다음 회수 시)
  recall_memory() ── frontmatter 읽어 r["reverify"] 부착 (provenance 옆)
        │
        ▼  (양 포맷터, byte-parity)
  _format_output / format_memory_context ── stale 시 "  재검증 필요: <note>" 1줄
        │
        ▼
  <system-reminder> 회수 출력 — 어시스턴트가 경고 보고 현행 확인 후 신뢰 (over-trust 차단)
```

컴포넌트 경계(brainstorming isolation 원칙):
- `reverify.py`: 순수 판정 + frontmatter upsert. 의존 = 라이브 코드 root, 메모리 텍스트. 부수효과 = frontmatter 쓰기(atomic).
- `reverify_cli.py`: scan/list/verify-registry. 의존 = reverify.py + 메모리 dir resolver.
- recall 라벨: `memory_search`(부착) + 양 포맷터(렌더). 의존 = frontmatter `reverify_status`.
- SessionEnd 트리거: 기존 best-effort 사슬에 1 step.

---

## 5. 완료 게이트 (검증 가능)

1. **BGE→Arctic 회귀 케이스 (1건 이상 사전 차단)**: `check_memory_staleness` 가
   - BGE-as-current 메모리(`'bge-m3'` 포함, `'arctic'` 미포함) → **stale**
   - Arctic-fresh 메모리 → **fresh**
   - 이력 메모리(`'bge-m3'` + `'arctic'` 동반) → **면제(fresh)**
   세 케이스 모두 통과 (spec §4.3-3 완료 게이트).
2. **회수 end-to-end**: `reverify_status: stale` 메모리 회수 시 양 포맷터 출력에 경고 라벨 렌더 + byte-parity 유지 + ②self-check·"회수 노트:"·sanitize·RECALLED_NAME_RE 회귀 무손상.
3. **메커니즘**: `reverify_cli` scan(frontmatter atomic·idempotent upsert) / list / verify-registry(레지스트리 self-check) 동작.
4. **회귀 무손상**: 전체 pytest 통과(baseline 678 passed + 신규), `contradiction_detector`(Layer 5) 무손상.

---

## 6. 솔직한 한계 (spec §4.4 계승)

- **회수 통합은 결국 메인 Claude 행동**: 경고 라벨은 prompt-level 신호일 뿐 코드 강제가 아니다. 모델이 라벨을 무시하면 over-trust 가 재발할 수 있다(②와 동일 한계). 시스템 보장 = *검출 + flag + 경고*까지.
- **레지스트리 커버리지**: 초기 facts 는 {embedding model, embedding port} 2건으로 한정(summarizer 등은 §7 확장). file:line 내용 대조·임의 버전 staleness 는 비범위(§8). 즉 ③은 "열거된 canonical facts" 에 대해서만 stale 을 잡는다 — 모든 stale 을 잡는다고 약속하지 않는다.
- **판별 신호의 보수성(양방향)**: §2 신호는 "현행 값 토큰을 함께 언급하면 면제". (a) **false-negative**: stale 메모리가 우연히 현행 값을 다른 맥락에서 언급하면 놓침. (b) **false-positive**: 옛 토큰만 담고 현행 토큰을 안 적은 순수 이력 메모리(예: BGE-M3 만 언급, arctic 미언급)는 flag 됨 — 단 경고는 "현행과 대조하라"는 soft 라벨이라 이력 문서에 붙어도 저비용. over-trust 도메인 특성상 false-negative 를 일부 허용(경고 누락 ≤ 잘못된 hard 차단)하되, 본 설계의 처리(감쇠 아닌 경고)라 false-positive 도 저비용. 보수 retrieve 필요 도메인(CDSS fork)은 신호를 완화해야 함.
- **★ metadata 면제 false-negative (audit R3, 의도된 D4 결과)**: scan 은 full-content(frontmatter+body, reverify_* 만 strip)를 보므로, `name`/`description`/`tags` 같은 metadata 가 우연히 current_value 토큰(예: `name: arctic-server-debug`)을 담으면, body 가 stale 주장(`bge-m3 사용 중`)을 해도 면제돼 놓친다. 이는 D4 의 "current_value 동반=이력" 휴리스틱이 metadata 까지 적용된 직접 결과(soft heuristic + false-negative 허용 정책 내). **실측 178개 메모리 전부에서 full-text vs body-only scan 결과 차이 0건**(실제 이력 메모리는 body 에 현행 값을 함께 적음)이라 운영 영향 없음 — 의도된 tradeoff 로 유지(body-only scan 으로 좁히면 description 의 stale 주장은 놓치는 반대 trade). 형이 원하면 후속에서 면제 범위를 body 로 한정 가능.
- **코드 주석 stale 비범위**: `memory_search.py:516` 같은 코드 내 stale 표기는 메모리 scan 밖. 별도 보고(§7).
- **트리거 활성화는 형 승인**: SessionEnd 증분 코드는 머지되나 install.sh 재배포 전엔 미발효.

---

## 7. 형이 검토할 설계 결정 (사용자 판단 플래그)

bg 세션이라 합리적 기본값을 정했으나, 형의 취향·위험선호로 조정 가능한 항목:

- **★ 경고 vs 감쇠 (D6)**: 기본 = 경고 라벨만. confirmed-stale 에 한해 `deprecated_by` 류 감쇠 병행을 원하면 형 승인 시 추가(TOP_K=1 정보손실 trade-off 고지).
- **★ 트리거 (D7)**: 기본 = SessionEnd 증분(주 1회). 별도 launchd cron 선호 시 형 선택(배포 surface 추가).
- **★ Canonical facts 범위 (D3)**: 초기 {embedding, summarizer}. 형이 감시할 fact(포트·버전·파일경로 등) 추가 지정 가능 — 레지스트리에 한 줄씩.
- **★ 튜닝 상수**: `REVERIFY_INTERVAL_DAYS=7`, 경고 라벨 문구, stale_alias 토큰 매칭 정밀도 — 형 조정 가능(plan 에서 상수/CLI 인자로 노출).
- **★ 코드 주석 stale (부수 발견)**: `src/memory_search.py:516` docstring 의 "BGE-M3" → "Arctic-ko" 정정 여부 — ③ 범위 밖이나 같은 사고의 잔재. 형 지시 시 별도 1줄 fix.

---

## 8. 비범위 (별도 / 형 승인)

- **Gemma adjudication** (history-vs-current 모호 케이스 LLM 판정) — §2 결정론 신호로 충분, deferred enhancement.
- **file:line 내용 대조** (참조 라인의 내용까지 검증) — 광범위 false-positive, Phase 2+. (파일 존재 체크는 저위험이라 plan 에서 옵션 검토.)
- **회수 임계값 auto-tune** (D8) — 영구 미구현.
- **메모리 본문 auto-edit / auto-delete** (D8) — 미구현(경고만).
- **install.sh 재배포(트리거 활성화)·GitHub push/tag/release** — 형 승인 영역.
- **모순감지(Layer 5) 변경** — 무손상 유지(차용만, 코드 미공유).

---

## 9. 출처 & 근거

- 상위 설계: `docs/specs/2026-05-30-second-brain-roadmap-design.md` §4.2③/§4.3-3/§4.4/§5
- ②선행: `docs/specs/2026-05-31-phase1-effective-recall-design.md` (parity·CONTRACT·ingestion 계약 — 라벨이 깨면 안 되는 불변식)
- ①선행: `docs/plans/2026-05-30-phase1-provenance.md` (provenance frontmatter·atomic rewrite 패턴)
- 사고 근거: `feedback_no_v1_token_waste.md`(stale 주장) vs `project_mindvault.md`(정당 이력) 실측 grep
- 차용 패턴: `src/contradiction_detector.py`(jsonl/CLI/silent-fail), `src/provenance_backfill_cli.py`(frontmatter atomic rewrite), `src/memory_search.py`(`deprecated_by` decay·prov 부착 블록)

---

## 10. Audit 후속 정정 (2026-05-31, 구현 후 4-렌즈 적대적 audit)

구현 완료 후 4개 독립 렌즈(검출 correctness / hot-path·parity·ingestion / 데이터 안전성 / 통합·spec·gate) 적대적 audit 수행. byte-parity·sanitize·self_eval ingestion·Layer 5·§4.4 경계·완료게이트는 **결함 0**. 아래 수정 반영(전체 회귀 678→**721 passed**).

- **CRITICAL — scan 진동 차단**: `check_memory_staleness` 가 전체 파일(frontmatter 포함)을 받는데, stale flag 시 쓰는 `reverify_note`("…현행 arctic 미언급")가 current_value 토큰 'arctic' 을 담아, 다음 scan 에서 self-exempt → flag strip → 재flag 무한 진동. **fix**: `scan_memories` 가 `_strip_reverify_frontmatter(text)` 로 자기 reverify_* 키를 제거한 뒤 판정(`test_scan_no_oscillation` 가드).
- **frontmatter 안전성**: `_FM_RE` 를 `^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?` 로 — 본문 구분 빈 줄 보존(옛 `\s*\n?` 가 먹던 BUG), 선택적 BOM 인식(이중 FM·이름 소실 차단), CRLF 허용. + `write_back_verdict` 가 frontmatter 미인식 파일엔 stale flag prepend 안 함(안전 skip). + 키 제거 시 `lstrip` 제거로 `metadata:` 하위 들여쓴 동명 키 오삭제 방지.
- **배포 무력화 차단**: `_grep_present` 가 `root/rel_path` 다음 `root/<basename>` 도 시도 → flat 배포 layout(`~/.claude/scripts/mindvault/` 에 `memory_indexer.py` 평면 배치)에서도 verifier 통과. `default_root` env 는 `.resolve()` 로 상대경로 절대화.
- **방어 가드**: `check_memory_staleness(None/"")` → fresh(크래시 방지), `_contains_token(_, "")` → False(빈 토큰 매칭 차단), note `[:300]` 캡.
- **doc 정정**: §D5 note 예시를 콜론 없는 실제 형식으로(콜론은 yaml.safe_load 깨짐), D3 verifier 를 "휴리스틱(주석 매칭 가능)" 으로 솔직화, D4 토큰 경계를 구현 확정값으로, §6 false-positive 방향 명시.
- **검증된 not-a-bug**: 적대적 finding 중 "`project_mindvault_v1v2_history.md` 가 arctic 미포함이라 오flag" 주장은 **실측 반증**(파일에 arctic 포함 → 정상 면제). 적대적 finding 도 검증 필수임을 재확인.
- **수용된 tradeoff(미수정)**: `bge-m3.5` 류 접두 매칭(soft 경고라 저비용), 순수 이력 false-positive(저비용), verifier 주석 매칭 한계(verify-registry 사람 안전판), 코드 주석 stale(`memory_search.py:516`, ③ 범위 밖 — §7 형 판단).

### 10.2 다회차 sweep (2026-05-31, `/goal` 워크플로 — find→adversarial verify, loop-until-dry)

위 4-렌즈 audit 후, 워크플로 오케스트레이션(렌즈별 find → refute-by-default verify)으로 Phase 1 ①②③ 전체를 **새 결함 0건까지 반복** 점검. 4 라운드로 수렴(R1 8 → R2 2 → R3 4 → **R4 0**). 누적 13건 수정 + 1건 문서화:

- **★ CRITICAL (별도 발견) — 테스트 격리 누락**: `conftest.py` 가 `MV3_PROJECTS_ROOT`/`DATA_DIR` 는 tmp 격리하나 사용자 셸이 export 하는 `MV3_MEMORY_DIR=<실제 메모리 dir>` 는 미격리. `_default_memory_dir()` 가 이를 1순위로 봐 테스트 중 `MEMORY_DIR`=실제 dir → SessionEnd reverify scan 을 타는 테스트(test_index_sync)가 **형의 실제 메모리 파일을 변경**(②까지는 메모리에 쓰는 코드 부재라 무해, ③ reverify 가 노출). fix: conftest 가 `MV3_MEMORY_DIR`/`PROJECTS_DIR`/`EXTRA_MEMORY_DIRS`/`GEMMA_INTENT` 도 격리 + `test_path_isolation` 가드 2건. (오염된 실제 메모리 3건은 원복.)
- **R1 (8, med/high)**: ① install.sh 가 reverify.py/reverify_cli.py 미배포 → ③ prod silent no-op(HIGH) ② default_root flat-배포 overshoot ③ reverify_note 미인용 YAML(미래 fact ': ' alias 시 frontmatter 소실) ④ provenance source_ref 미인용 YAML ⑤ 주1회 reverify 가 no-candidate early-return 에 갇힘 ⑥ 출처 ref [:8] 가 url provenance 파괴 ⑦ stale write-back 의 CRLF→LF 본문 무단변환 ⑧ conftest 격리(위 CRITICAL).
- **R2 (2, low)**: lone-CR frontmatter 미flag + scan/list 불일치 / `_atomic_write` 고정 tmp → 동시 SessionEnd race.
- **R3 (4)**: maybe_scan_due 미래-timestamp 영구 SKIP(med) / `_is_deprecated` 2KB cap 이 Phase1 frontmatter 성장 시 fence 놓쳐 Layer5 감쇠 누락(low) / FRONTMATTER_RE BOM 비관용(low) / D4 metadata 면제 false-negative(문서화, 실측 0건).
- **R4: 0건** — 1 라운드 완전 dry, 수렴 확정.
- 전 과정 회귀 유지(678→741 passed), 실제 메모리 0 오염 재확인, Layer 5(contradiction) 무손상. 상세는 `git log`(fix(reliability) sweep R1~R3 커밋).
