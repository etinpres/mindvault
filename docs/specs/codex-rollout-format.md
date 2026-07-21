# Codex rollout JSONL 포맷 스펙 (0.144.6)

## 범위와 근거

이 문서는 Codex CLI `0.144.6`이 2026-07-21에 생성한 실제 rollout을 구조만 보존해 조사한 결과다. 확인한 표본은 다음 세 종류다.

- 대화형 TUI: `originator=codex-tui`, `source=cli`
- 비대화형 실행: `originator=codex_exec`, `source=exec`
- 자동 compact가 두 번 발생한 대화형 TUI

추가로 설치된 `0.144.6` 바이너리의 hook JSON schema와, 격리한 `Stop` probe를 실제 실행해 stdin payload를 교차 확인했다. 원본의 사용자명, 절대 경로, 대화 내용, session id는 fixture에 복사하지 않았다.

Codex 공식 hook 문서는 `transcript_path`가 편의를 위한 필드이며 transcript 포맷은 안정된 인터페이스가 아니라고 명시한다. 따라서 loader는 이 문서를 버전 스냅샷으로 취급하고, 알 수 없는 record나 필드는 버리지 말되 추출에서는 건너뛰어야 한다.

## 공통 envelope

각 줄은 독립 JSON object이며 공통 최상위 형태는 다음과 같다.

```json
{"timestamp":"2026-07-21T00:00:00.000Z","type":"response_item","payload":{}}
```

핵심 top-level `type`은 다음과 같다.

| `type` | 역할 |
| --- | --- |
| `session_meta` | 세션 식별, 실행 surface, CLI 버전, cwd |
| `turn_context` | 새 turn 경계와 turn id, 모델, 권한 정보 |
| `response_item` | 메시지, reasoning, tool call과 tool output의 원본 항목 |
| `event_msg` | UI/event stream용 중복 알림과 상태 이벤트 |
| `world_state` | 세션 상태 snapshot |
| `compacted` | compact 전후 window와 대체 history |

`session_meta.payload.session_id`와 `payload.id`는 관찰 표본에서 같은 세션 UUID였다. surface 판별은 id가 아니라 아래 두 필드를 사용한다.

| surface | `originator` | `source` |
| --- | --- | --- |
| 대화형 CLI | `codex-tui` | `cli` |
| `codex exec` | `codex_exec` | `exec` |

## 메시지 판별과 텍스트 추출

대화 원본은 `type=response_item`, `payload.type=message`인 record다.

```json
{
  "type": "response_item",
  "payload": {
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": "..."}]
  }
}
```

관찰된 조합은 다음과 같다.

| `role` | content item | 의미 |
| --- | --- | --- |
| `developer` | `input_text` | 시스템 및 개발자 지침 |
| `user` | `input_text` | bootstrap context 또는 실제 사용자 입력 |
| `assistant` | `output_text` | assistant 출력 |

assistant message에는 `payload.phase`가 있으며 `commentary`와 `final_answer`를 관찰했다. 기억 추출용 기본 transcript는 `final_answer`를 답변으로 사용하고, `commentary`는 작업 과정이 꼭 필요할 때만 별도 보존한다.

텍스트는 `payload.content`의 순서를 유지해 `input_text` 또는 `output_text` 항목의 `text`를 줄바꿈으로 합친다. 이미지나 이후 추가될 content type은 오류로 처리하지 말고 건너뛴다.

### 실제 사용자 입력과 bootstrap 구분

세션 첫머리에는 `world_state`와 첫 `turn_context`보다 앞선 `role=user` record가 있을 수 있다. 이 항목은 `environment_context` 같은 주입 context를 담으므로 실제 대화로 추출하면 안 된다.

안전한 turn 추출 순서는 다음과 같다.

1. `turn_context`를 새 turn 시작으로 잡는다.
2. 그 뒤 처음 나오는 `response_item/message/role=user`를 해당 turn의 사용자 입력으로 잡는다.
3. 다음 `turn_context` 전까지의 `response_item/message/role=assistant`를 같은 turn의 응답으로 잡는다.
4. `event_msg.user_message`와 `event_msg.agent_message`는 UI용 중복이므로 transcript에 다시 넣지 않는다.
5. `role=developer`와 첫 `turn_context` 이전의 bootstrap `role=user`는 대화 본문에서 제외한다.

`event_msg.user_message.payload.message`와 `event_msg.agent_message.payload.message`는 판별 보조 신호로는 쓸 수 있지만, `response_item`과 함께 적재하면 문장이 중복된다.

## tool call과 Bash 대응

0.144.6 code-mode 표본에서 shell 실행은 다음 두 record로 기록됐다.

- 호출: `response_item`, `payload.type=custom_tool_call`, `payload.name=exec`
- 결과: `response_item`, `payload.type=custom_tool_call_output`, 같은 `call_id`

`custom_tool_call.payload.input`은 JSON object가 아니라 `tools.exec_command({...})`를 포함하는 JavaScript source 문자열이다. loader가 이 문자열을 `eval`해서는 안 된다. Bash 이력을 얻어야 한다면 알려진 `tools.exec_command` 호출에서 정적인 `cmd` 문자열만 보수적으로 파싱하고, 템플릿 표현식·동적 계산·파싱 실패는 raw tool call로 남긴다.

tool output은 관찰 표본에서 `payload.output` 배열이었고, 각 항목은 `{"type":"input_text","text":"..."}` 형태였다. 첫 항목은 실행 상태, 다음 항목은 stdout/stderr 본문일 수 있으므로 순서대로 합친다.

일반 function tool은 다음 형태도 사용한다.

- `payload.type=function_call`: `name`, JSON 문자열 `arguments`, `call_id`
- `payload.type=function_call_output`: 같은 `call_id`, 문자열 `output`

실제 표본에서는 장기 실행 command를 기다리는 `function_call(name=wait)`을 관찰했다. hook matcher에서 unified exec가 `Bash`로 매핑되는 것과 rollout의 `custom_tool_call(name=exec)` 표기는 서로 다른 계층이다.

## 대화형과 exec 차이

두 surface의 record envelope와 turn/message 구조는 같다. 차이는 `session_meta`와 진행 양이다.

| 항목 | 대화형 CLI | `codex exec` |
| --- | --- | --- |
| `originator` / `source` | `codex-tui` / `cli` | `codex_exec` / `exec` |
| turn 수 | 여러 turn 가능 | 보통 한 turn |
| assistant phase | `commentary`, `final_answer` 모두 가능 | 단순 표본은 `final_answer`만 관찰 |
| 종료 표시 | 각 turn마다 `task_complete` | 단일 turn 뒤 `task_complete` |

`task_started`, `task_complete`, `token_count`, `thread_settings_applied`는 상태 이벤트다. 대화 텍스트로 적재하지 않는다.

## compact 포맷

자동 compact 표본에서 다음 두 신호가 함께 나타났다.

- top-level `type=compacted`
- `event_msg.payload.type=context_compacted`

`compacted.payload`의 관찰 필드는 다음과 같다.

```text
first_window_id, previous_window_id, window_id, window_number,
message, replacement_history
```

`replacement_history`는 `message` 항목들과 마지막 `type=compaction` 항목을 담았다. `compaction.encrypted_content`는 암호화 문자열이며 loader가 해석할 수 없다. 관찰된 top-level `message`는 빈 문자열이었다.

`replacement_history`는 새 대화가 아니라 compact 이후 모델에 공급할 대체 history다. 원래 `response_item`과 함께 다시 적재하면 전체 대화가 중복되므로, loader는 `compacted`를 window 경계 및 진단 정보로만 기록한다. compact 직후에도 다음 `turn_context`부터 평소 알고리즘으로 추출을 계속한다.

## Stop hook: Codex의 SessionEnd 대응 이벤트

Codex 0.144.6의 지원 hook 목록에는 `SessionEnd`가 없다. 자동 추출 트리거에 해당하는 이벤트는 turn scope의 `Stop`이다. 즉 프로세스가 완전히 종료될 때 한 번이 아니라, assistant가 turn을 끝낼 때마다 실행된다.

실제 `codex exec --dangerously-bypass-hook-trust` probe에서 `Stop` command stdin은 다음 9개 필드만 포함했다.

| 필드 | 타입 | 비고 |
| --- | --- | --- |
| `session_id` | string | 현재 Codex session id |
| `turn_id` | string | 현재 turn id |
| `transcript_path` | string 또는 null | rollout JSONL 경로 |
| `cwd` | string | session working directory |
| `hook_event_name` | string | 정확히 `Stop` |
| `model` | string | 활성 모델 slug |
| `permission_mode` | enum string | `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions` |
| `stop_hook_active` | boolean | Stop continuation 재진입 여부 |
| `last_assistant_message` | string 또는 null | 마지막 assistant message |

probe에서는 `stop_hook_active=false`, `last_assistant_message="PROBE_OK"`, 문자열 `transcript_path`가 관찰됐다. payload에는 timestamp나 `source`가 없으므로 surface 판별이 필요하면 `transcript_path`의 `session_meta`를 읽는다.

MindVault 자동 추출 handler는 다음 계약을 지켜야 한다.

- stdin JSON을 읽고 `hook_event_name=Stop`만 처리한다.
- `transcript_path=null`, 파일 없음, JSONL 일부 손상은 exit `0`으로 fail-open한다.
- `(session_id, turn_id)`를 idempotency key로 사용해 같은 turn을 중복 추출하지 않는다.
- continuation을 요구하지 않으며 stdout을 비운 채 exit `0`한다.
- hook hot path에서는 transcript 수집과 enqueue만 하고 LLM 추출은 비동기로 넘긴다.
- `stop_hook_active=true`에서도 새 continuation을 만들지 않는다.

## 익명화 fixture

`tests/fixtures/codex_sessions/`의 세 파일은 실제 0.144.6 shape를 재구성한 합성 fixture다.

- `interactive.jsonl`: 두 turn, commentary/final, custom exec와 wait
- `exec.jsonl`: `codex exec` 단일 turn
- `compacted.jsonl`: compact window와 이후 turn

fixture에는 실제 사용자명, 홈 경로, session id, 이메일, 전화번호, 주소, 계정번호, token을 넣지 않는다. 모든 줄은 독립적으로 `json.loads()` 가능한지 검사하고 gitleaks를 통과해야 한다.
