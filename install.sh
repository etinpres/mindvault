#!/usr/bin/env bash
# MindVault v3 installer — deploys SessionStart hook to ~/.claude/.
# Idempotent; safe to re-run. Creates settings.json.bak before edit.

set -euo pipefail

# v3.2.0 Task 1 — Apple Silicon 가드 + 헬퍼.
# ARCH_OVERRIDE 환경변수는 테스트 전용 (tests/test_install_v320.py).
_ARCH="${ARCH_OVERRIDE:-$(uname -m)}"
_OS="$(uname -s)"

# v3.x — MV3_SYNC_ONLY: 코드/훅/스킬 파일만 빠르게 재배포하는 경량 경로.
# 모델 다운로드/변환·pip·plist(launchctl)·헬스체크 대기·인덱싱·pre-warm 을 전부
# skip 하고 deploy_exec 파일복사 + settings.json 등록만 수행 → sub-second.
# git post-commit/post-merge 훅이 호출 — repo 수정이 배포 경로에 즉시 반영돼
# "repo 는 고쳤는데 배포본은 stale" 인 배포 지연(2026-05-28→06-01)을 차단한다.
# 파일 src→target 매핑은 아래 본문 deploy 호출을 그대로 재사용(단일 진실원본).
if [ "${MV3_SYNC_ONLY:-0}" = "1" ]; then
  export MV3_SKIP_MODELS=1   # Sprint 4.5/17 모델 단계 재사용 게이트로 skip
fi

# 파일 복사만 하는 sync 모드는 arch 무관 — 비arm64 인터랙티브 프롬프트 우회.
if [ "$_ARCH" != "arm64" ] || [ "$_OS" != "Darwin" ]; then
 if [ "${MV3_SYNC_ONLY:-0}" != "1" ]; then
  echo "⚠ MindVault v3 의 MLX 백엔드는 Apple Silicon Mac 에서만 동작합니다."
  echo "  현재 환경: $_OS $_ARCH"
  echo "  Linux/Intel Mac 미지원 (MLX 백엔드 Apple Silicon 전용)."
  # non-interactive (CI, curl|bash, no stdin) 에선 read 가 EOF로 즉시 fail —
  # set -e 로 silent abort 되니, 명시 처리해서 user-facing 메시지 보장.
  if [ ! -t 0 ]; then
    echo "  설치 취소 (non-interactive 환경 — stdin 없음). 인프라만 설치하려면 MV3_SKIP_MODELS=1 ./install.sh"
    exit 1
  fi
  if ! read -r -p "  계속 진행 시 모델 자동 설치(Sprint 4.5/17)는 건너뜁니다. 인프라만 설치하시겠습니까? [y/N] " _resp; then
    echo "  설치 취소 (입력 읽기 실패)."
    exit 1
  fi
  case "${_resp:-N}" in
    y|Y) ;;
    *)   echo "  설치 취소."; exit 1 ;;
  esac
  export MV3_SKIP_MODELS=1
 fi
fi

if [ "${MV3_GUARD_ONLY:-0}" = "1" ]; then
  exit 0
fi

# v3.2.0 Task 1 — checkpoint 헬퍼.
# do_step <name> <step_file> <action_command>
do_step() {
  local name="$1" step_file="$2" action="$3"
  # defensive: 빈 step_file argument 시 cryptic mkdir error 대신 명확한 메시지.
  if [ -z "${step_file:-}" ]; then
    echo "  ✗ do_step: empty step_file for '$name' (caller bug)" >&2
    return 1
  fi
  if grep -q "^${name}$" "$step_file" 2>/dev/null; then
    echo "  ✓ ${name} (already done, skipping)"
    return 0
  fi
  echo "→ ${name} ..."
  if eval "$action"; then
    mkdir -p "$(dirname "$step_file")"
    echo "$name" >> "$step_file"
    echo "  ✓ ${name}"
    return 0
  else
    echo "  ✗ ${name} FAILED"
    print_next_step "$name"
    return 1
  fi
}

print_next_step() {
  case "$1" in
    deps-ok)      echo "  Next: pip 환경 확인 (python3 -m pip --version) 후 ./install.sh 재실행" ;;
    downloaded)   echo "  Next: 네트워크 확인 후 ./install.sh 재실행 (huggingface_hub 가 partial 캐시 자동 활용)" ;;
    converted)    echo "  Next: 디스크 1.5GB 확보 후 ./install.sh 재실행" ;;
    plist-loaded) echo "  Next: ls -la ~/Library/LaunchAgents/ 권한 확인 후 ./install.sh 재실행" ;;
    healthy)      echo "  Next: tail ~/Library/Logs/{gemma,arctic-ko}-mlx.err 진단 후 ./install.sh 재실행" ;;
    verified)     echo "  Next: ls ~/.cache/mlx-arctic-ko/model.safetensors 확인 후 ./install.sh 재실행" ;;
    *)            echo "  Next: ./install.sh 재실행 또는 install.sh debug 로그 확인 (unknown step: $1)" ;;
  esac
}

# v3.2.2 — defensive skill/file deploy 헬퍼.
# 옛 패턴은 `.bak` 백업 후 `cp $src $target`. cp 가 fail 하면 set -e 가 abort —
# `.bak` 만 남고 `.md` 부재. 사용자가 skill 잃음 (Claude Code resolution fail).
# 새 패턴: src 부재 검증 → backup → cp → cp fail 시 .bak 자동 복원 → 성공 시 .bak 제거.
# 어떤 fail 시나리오에서도 target 보존 보장.
deploy_skill() {
  local src="$1" target="$2" label="$3"
  if [ ! -f "$src" ]; then
    echo "  ✗ ${label}: skill source missing ($src)" >&2
    return 1
  fi
  if [ -f "$target" ]; then
    cp "$target" "$target.bak"
  fi
  if ! cp "$src" "$target"; then
    if [ -f "$target.bak" ]; then
      mv "$target.bak" "$target"
      echo "  ✗ ${label}: cp failed, restored from .bak" >&2
    else
      echo "  ✗ ${label}: cp failed, no backup to restore" >&2
    fi
    return 1
  fi
  rm -f "$target.bak"
  echo "  ✓ installed ${label} skill at $target"
}

# v3.2.4 — runner wrapper deploy (Python placeholder 치환).
# install.sh 가 사용한 python3 의 bin dir 을 __INSTALL_PYTHON_BIN__ 치환.
# wrapper PATH 첫 항목 = install python3 → launchd 환경에서 mlx 모듈 일치 보장.
# (v3.2.3 의 wrapper PATH 가 install python3 와 다른 인터프리터 가리켜 ImportError
# 발생했던 결함 fix.) deploy_plist 패턴 차용 — Python heredoc 으로 metachar 안전.
deploy_runner() {
  local src="$1" target="$2" label="${3:-$(basename "$target")}"
  if [ ! -f "$src" ]; then
    echo "  ✗ ${label}: source missing ($src)" >&2
    return 1
  fi
  local pybin
  pybin="$(dirname "$(command -v python3)")"
  if [ -f "$target" ]; then
    cp "$target" "$target.bak"
  fi
  if ! python3 - "$src" "$target" "$pybin" <<'PY'
import os, sys
src, target, pybin = sys.argv[1], sys.argv[2], sys.argv[3]
content = open(src).read().replace("__INSTALL_PYTHON_BIN__", pybin)
tmp = target + ".tmp"
open(tmp, "w").write(content)
os.replace(tmp, target)
PY
  then
    if [ -f "$target.bak" ]; then
      mv "$target.bak" "$target"
      echo "  ✗ ${label}: template failed, restored from .bak" >&2
    else
      echo "  ✗ ${label}: template failed, no backup" >&2
    fi
    return 1
  fi
  rm -f "$target.bak"
  chmod +x "$target"
}

# v3.2.3 — generic file deploy (executable). hook/wrapper/server script 용.
# deploy_skill 과 동일 .bak 복원 패턴 + chmod +x.
deploy_exec() {
  local src="$1" target="$2" label="${3:-$(basename "$target")}"
  if [ ! -f "$src" ]; then
    echo "  ✗ ${label}: source missing ($src)" >&2
    return 1
  fi
  if [ -f "$target" ]; then
    cp "$target" "$target.bak"
  fi
  if ! cp "$src" "$target"; then
    if [ -f "$target.bak" ]; then
      mv "$target.bak" "$target"
      echo "  ✗ ${label}: cp failed, restored from .bak" >&2
    else
      echo "  ✗ ${label}: cp failed, no backup to restore" >&2
    fi
    return 1
  fi
  rm -f "$target.bak"
  chmod +x "$target"
}

# v3.2.3 — plist 템플릿 deploy. sed 의 replacement metachar (`&`, `\`) 안전을
# 위해 Python 으로 처리. __USER_HOME__ → $HOME 단일 치환 후 LaunchAgents 에 쓰기.
# launchctl unload/load 까지 처리. set -e 환경에서 부분 실패 깔끔히.
deploy_plist() {
  local src="$1" target="$2" label="${3:-$(basename "$target" .plist)}"
  if [ ! -f "$src" ]; then
    echo "  ✗ ${label}: plist source missing ($src)" >&2
    return 1
  fi
  python3 - "$src" "$target" <<'PY' || return 1
import os, sys
src, target = sys.argv[1], sys.argv[2]
home = os.environ.get("HOME") or os.path.expanduser("~")
content = open(src).read().replace("__USER_HOME__", home)
tmp = target + ".tmp"
open(tmp, "w").write(content)
os.replace(tmp, target)
PY
  if [ "${MV3_PLIST_SKIP_LAUNCHCTL:-0}" != "1" ]; then
    launchctl unload "$target" 2>/dev/null || true
    # v3.2.7: load 실패를 silent 로 두지 말 것. 실패 시 stderr 로 warning,
    # 그리고 launchctl list 로 실제 load 됐는지 verify. fail 시 return 1
    # 으로 caller (do_step 등) 가 인지.
    if ! launchctl load -w "$target" 2>/dev/null; then
      echo "  ⚠ launchctl load failed: $target" >&2
      return 1
    fi
    # v3.2.7: launchctl load -w 가 0 반환했으면 그것 신뢰. 추가 list verify
    # 는 race window (큰 모델 load 시 수 초 지연) 때문에 false negative 가
    # 흔해 의미가 없음 — retry 도 단순 informational 이라 제거.
    # 사용자가 직접 검증하려면 README 의 troubleshooting 참조.
  fi
}

if [ "${MV3_SOURCE_HELPERS_ONLY:-0}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO_DIR/src/session_memory.py"
HOOKS_DIR="$HOME/.claude/hooks"
TARGET="$HOOKS_DIR/session-memory.py"
SETTINGS="$HOME/.claude/settings.json"
# 훅 스크립트 shebang(#!/usr/bin/env python3)과 chmod +x에 의존.
HOOK_CMD="$TARGET"

# Sprint 2 추가 자산
SCRIPTS_DIR="$HOME/.claude/scripts/mindvault"
COMMANDS_DIR="$HOME/.claude/commands"
SPRINT2_SRC=("$REPO_DIR/src/indexer.py" "$REPO_DIR/src/search.py" "$REPO_DIR/src/recall_cli.py")
RECALL_SKILL_SRC="$REPO_DIR/skill/recall.md"
RECALL_SKILL_TARGET="$COMMANDS_DIR/recall.md"

# Sprint 3 추가 자산
END_SRC="$REPO_DIR/src/session_memory_end.py"
END_TARGET="$HOOKS_DIR/session-memory-end.py"
END_CMD="$END_TARGET"
# NEXT-23/24 (2026-05-24): async wrapper (macOS 호환 nohup+&+disown).
# wrapper 가 SessionEnd hook 를 백그라운드로 detach — Claude Code 종료 시 SIGTERM 회피.
# setsid 사용한 옛 버전은 macOS 에 setsid 없어서 첫 줄 fail.
END_WRAPPER_SRC="$REPO_DIR/hooks/session-memory-end-async.sh"
END_WRAPPER_TARGET="$HOOKS_DIR/session-memory-end-async.sh"
SPRINT3_SRC=("$REPO_DIR/src/memory_extractor.py" "$REPO_DIR/src/memory_review_cli.py")
MEMORY_SKILL_SRC="$REPO_DIR/skill/memory_review.md"
MEMORY_SKILL_TARGET="$COMMANDS_DIR/memory_review.md"

# close-session 명시 closer (자동 hook narrative 보완용)
CLOSE_SESSION_SKILL_SRC="$REPO_DIR/skill/close-session.md"
CLOSE_SESSION_SKILL_TARGET="$COMMANDS_DIR/close-session.md"
CS_SKILL_SRC="$REPO_DIR/skill/cs.md"
CS_SKILL_TARGET="$COMMANDS_DIR/cs.md"

# ── Sprint 4.5 (v3.2.0) — Arctic-ko 4bit 자동 변환 ────────────────────────────
# 옛 4.2 "수동 변환 안내" 블록을 대체. do_step checkpoint 로 4 단계 진행.
# 위치: 다른 사이드이펙트(mkdir, cp, plist load) 전에 두어 MV3_SPRINT45_ONLY=1
# 테스트가 빨리 exit 하도록.
ARCTIC_TARGET="${MV3_ARCTIC_TARGET:-$HOME/.cache/mlx-arctic-ko}"
ARCTIC_STEP_FILE="${MV3_ARCTIC_STEP_FILE:-$ARCTIC_TARGET/.mv3-step}"
CONVERT_SCRIPT="$REPO_DIR/scripts/convert_arctic_ko.py"
ARCTIC_MODEL_READY=0

if [ "${MV3_SKIP_MODELS:-0}" = "1" ]; then
  echo "→ Sprint 4.5 (Arctic-ko 모델 자동 변환) skip — non-arm64 또는 사용자 선택"
else
  echo ""
  echo "── Sprint 4.5 — Arctic-ko 4bit 자동 변환 ─────────────────────────────────"
  mkdir -p "$ARCTIC_TARGET"
  do_step "deps-ok"    "$ARCTIC_STEP_FILE" "python3 -m pip install --user --quiet mlx_embeddings huggingface_hub" || exit 1
  do_step "downloaded" "$ARCTIC_STEP_FILE" "python3 -c \"from huggingface_hub import snapshot_download; snapshot_download('dragonkue/snowflake-arctic-embed-l-v2.0-ko')\"" || exit 1
  do_step "converted"  "$ARCTIC_STEP_FILE" "python3 '$CONVERT_SCRIPT' --target '$ARCTIC_TARGET'" || exit 1
  do_step "verified"   "$ARCTIC_STEP_FILE" "[ -f '$ARCTIC_TARGET/model.safetensors' ]" || exit 1
fi

# v3.2.3 (#5) — ARCTIC_MODEL_READY 는 step file 대신 실제 파일 존재 검증.
# MV3_SKIP_MODELS=1 환경에서도 사용자가 직접 모델 변환을 끝낸 후 install.sh
# 재실행하면 초기 인덱싱이 자동으로 진행되도록.
if [ -f "$ARCTIC_TARGET/model.safetensors" ]; then
  ARCTIC_MODEL_READY=1
fi

if [ "${MV3_SPRINT45_ONLY:-0}" = "1" ]; then
  exit 0
fi

# ── Sprint 17 (v3.2.0) — Gemma 자동 설치 ───────────────────────────────────────
# launchd 로 com.mindvault.gemma-mlx 서비스 띄움. 기존 다른 이름의 gemma-mlx
# 서비스 (예: com.yonghaekim.gemma-mlx) 가 살아있으면 충돌 회피 — 새 plist 설치
# skip, 기존 port 8080 점유 그대로 재사용.
GEMMA_CACHE="${MV3_GEMMA_CACHE:-$HOME/.cache/mv3-gemma}"
GEMMA_STEP_FILE="${MV3_GEMMA_STEP_FILE:-$GEMMA_CACHE/.mv3-step}"
GEMMA_LAUNCH_AGENTS="${MV3_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
GEMMA_SCRIPTS_DIR="${MV3_SCRIPTS_DIR:-$HOME/.claude/scripts/mindvault}"
GEMMA_PLIST_SRC="$REPO_DIR/plist/com.mindvault.gemma-mlx.plist"
GEMMA_PLIST_TARGET="$GEMMA_LAUNCH_AGENTS/com.mindvault.gemma-mlx.plist"
GEMMA_RUNNER_SRC="$REPO_DIR/scripts/gemma_server_runner.sh"
GEMMA_RUNNER_TARGET="$GEMMA_SCRIPTS_DIR/gemma_server_runner.sh"
GEMMA_MODEL_ID="mlx-community/gemma-4-e4b-it-4bit"

# v3.2.3 (#13) — fresh macOS 는 ~/Library/LaunchAgents 부재 가능.
# 첫 plist deploy 직전 명시 생성 — set -e 가 ENOENT 로 abort 회피.
mkdir -p "$GEMMA_LAUNCH_AGENTS" "$GEMMA_SCRIPTS_DIR"

if [ "${MV3_SKIP_MODELS:-0}" = "1" ]; then
  echo "→ Sprint 17 (Gemma 자동 설치) skip — non-arm64 또는 사용자 선택"
else
  echo ""
  echo "── Sprint 17 — Gemma 자동 설치 ────────────────────────────────────────────"

  # (a) 기존 Gemma launchd 서비스 감지 (예: com.yonghaekim.gemma-mlx).
  # MV3_EXISTING_GEMMA 가 명시적으로 set 됐으면 (empty 포함) 그 값 사용 — test 격리.
  # unset 일 때만 launchctl list 스캔.
  if [ "${MV3_EXISTING_GEMMA+set}" = "set" ]; then
    EXISTING_GEMMA="$MV3_EXISTING_GEMMA"
  else
    EXISTING_GEMMA="$(launchctl list 2>/dev/null | awk '/gemma-mlx/ {print $3}' | grep -v '^com.mindvault.gemma-mlx$' | head -1 || true)"
  fi

  # (b) 의존성 + 모델 DL — 충돌 여부와 무관.
  mkdir -p "$GEMMA_CACHE"
  if [ "${MV3_GEMMA_DRY_RUN:-0}" = "1" ]; then
    do_step "deps-ok"    "$GEMMA_STEP_FILE" "true" || exit 1
    do_step "downloaded" "$GEMMA_STEP_FILE" "true" || exit 1
  else
    do_step "deps-ok"    "$GEMMA_STEP_FILE" "python3 -m pip install --user --quiet mlx-lm" || exit 1
    do_step "downloaded" "$GEMMA_STEP_FILE" "python3 -c \"from huggingface_hub import snapshot_download; snapshot_download('$GEMMA_MODEL_ID')\"" || exit 1
  fi

  # (c) plist 설치 — 기존 서비스 감지 시 skip.
  # v3.2.3 (#18): plist-loaded 는 cheap step 이라 do_step 캐시 없이 항상 refresh —
  # template 변경이 silent 로 묻히는 idempotency 버그 차단. step entry 도 cleanup
  # 후 재기록 (upgrade 일관성).
  if [ -n "$EXISTING_GEMMA" ]; then
    echo "  ✓ 기존 Gemma launchd 서비스 감지됨 ($EXISTING_GEMMA, port 8080 점유 중)"
    echo "    MindVault v3.2.x 의 신규 plist 설치 skip — 기존 서비스 재사용"
    echo "    (옵션: 기존 plist 제거 후 ./install.sh 재실행하면 com.mindvault.gemma-mlx 사용)"
    # step entry 정리 후 재기록 (upgrade 시 멱등)
    if [ -f "$GEMMA_STEP_FILE" ]; then
      grep -v "^plist-loaded$" "$GEMMA_STEP_FILE" > "$GEMMA_STEP_FILE.tmp" || true
      mv "$GEMMA_STEP_FILE.tmp" "$GEMMA_STEP_FILE"
    fi
    echo "plist-loaded" >> "$GEMMA_STEP_FILE"
  else
    deploy_runner "$GEMMA_RUNNER_SRC" "$GEMMA_RUNNER_TARGET" "gemma_server_runner" || exit 1

    if [ "${MV3_GEMMA_DRY_RUN:-0}" = "1" ]; then
      MV3_PLIST_SKIP_LAUNCHCTL=1 deploy_plist "$GEMMA_PLIST_SRC" "$GEMMA_PLIST_TARGET" "gemma plist" || exit 1
    else
      deploy_plist "$GEMMA_PLIST_SRC" "$GEMMA_PLIST_TARGET" "gemma plist" || exit 1
    fi
    # step entry 정리 후 재기록 (always refresh, content drift 차단)
    if [ -f "$GEMMA_STEP_FILE" ]; then
      grep -v "^plist-loaded$" "$GEMMA_STEP_FILE" > "$GEMMA_STEP_FILE.tmp" || true
      mv "$GEMMA_STEP_FILE.tmp" "$GEMMA_STEP_FILE"
    fi
    echo "plist-loaded" >> "$GEMMA_STEP_FILE"
    echo "  ✓ plist-loaded (always refresh)"
  fi

  # (d) 헬스체크 — 60초 콜드 스타트 대기 (degraded mode 허용).
  if [ "${MV3_GEMMA_DRY_RUN:-0}" = "1" ]; then
    grep -q "^healthy$" "$GEMMA_STEP_FILE" 2>/dev/null || echo "healthy" >> "$GEMMA_STEP_FILE"
  else
    HEALTH_OK=0
    for i in $(seq 1 30); do
      if curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/v1/models 2>/dev/null | grep -q "200"; then
        HEALTH_OK=1
        break
      fi
      sleep 2
    done
    if [ "$HEALTH_OK" = "1" ]; then
      grep -q "^healthy$" "$GEMMA_STEP_FILE" 2>/dev/null || echo "healthy" >> "$GEMMA_STEP_FILE"
      echo "  ✓ Gemma health: OK"
    else
      echo "  ⚠ Gemma 헬스체크 60초 timeout — install.sh 는 success exit (degraded mode)"
      print_next_step "healthy"
    fi
  fi
fi

if [ "${MV3_SPRINT17_ONLY:-0}" = "1" ]; then
  exit 0
fi

if [ ! -f "$SRC" ]; then
  echo "error: $SRC not found" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not on PATH" >&2
  exit 1
fi
# v3.2.7: MLX (mlx-lm, mlx_embeddings) 3.10+ 필수. 3.9 는 import error.
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
  echo "error: Python 3.10+ required (MLX 호환). 현재: $(python3 --version 2>&1)" >&2
  echo "       brew install python@3.10 또는 https://www.python.org/downloads/" >&2
  exit 1
fi

mkdir -p "$HOOKS_DIR" "$HOME/.claude/mindvault-v3/cache" "$SCRIPTS_DIR" "$COMMANDS_DIR"

# v3.2.3 (#7) — 누락된 skill·hook 을 silent skip 하지 않고 누적 manifest 에 기록.
# 설치 끝에서 누락 건수 확인 + 사용자에게 명확 보고. uninstall.sh 가 이 manifest 를
# 보고 personal SKILL 백업 복원 (#15) 도 같은 메커니즘 사용.
INSTALL_MANIFEST_DIR="$HOME/.claude/mindvault-v3"
INSTALL_MANIFEST="$INSTALL_MANIFEST_DIR/.install-manifest"
# v3.2.7: manifest atomic build — .tmp 에 쓰고 install 끝에서 mv. 중간 fail
# 시 기존 manifest 보존 (uninstall.sh 가 stale 일지언정 비어있지 않음).
INSTALL_MANIFEST_TMP="$INSTALL_MANIFEST_DIR/.install-manifest.tmp"
mkdir -p "$INSTALL_MANIFEST_DIR"
: > "$INSTALL_MANIFEST_TMP"  # tmp 만 truncate, 실제 manifest 는 install 끝에 mv
DEPLOY_FAILURES=0

manifest_record() {
  # type=path 형식, 한 줄. v3.2.7: .tmp 에 기록.
  echo "$1=$2" >> "$INSTALL_MANIFEST_TMP"
}

deploy_exec "$SRC" "$TARGET" "session-memory hook" || { DEPLOY_FAILURES=$((DEPLOY_FAILURES+1)); }
manifest_record "hook" "$TARGET"
echo "✓ copied hook to $TARGET"

# Sprint 2: scripts/mindvault/ 배포
for f in "${SPRINT2_SRC[@]}"; do
  if [ -f "$f" ]; then
    deploy_exec "$f" "$SCRIPTS_DIR/$(basename "$f")" "$(basename "$f")" \
      || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
    manifest_record "script" "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
echo "✓ deployed Sprint 2 scripts to $SCRIPTS_DIR"

# Sprint 2: /recall 스킬 배포 (v3.2.2 — defensive deploy_skill).
# v3.2.3 (#16): 옛 `|| true` 가 cp 실패 swallow → "Installation complete" 였는데
# 실제로는 skill 미설치. 이제 실패 누적 후 끝에서 alarm.
deploy_skill "$RECALL_SKILL_SRC" "$RECALL_SKILL_TARGET" "/recall" \
  || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
manifest_record "skill" "$RECALL_SKILL_TARGET"

# Sprint 3: SessionEnd 훅 + 추가 스크립트 + /memory review 스킬
END_HOOK_DEPLOYED=0
if [ -f "$END_SRC" ]; then
  if deploy_exec "$END_SRC" "$END_TARGET" "session-memory-end hook"; then
    manifest_record "hook" "$END_TARGET"
    echo "✓ copied SessionEnd hook to $END_TARGET"
    END_HOOK_DEPLOYED=1
  else
    DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  fi
fi
# v3.2.3 (#4): async wrapper 가 src 부재 시 silent skip 후에도 register 항상 호출 →
# settings.json 에 broken path 등록. 이제 wrapper deploy 성공 여부를 추적해서
# register 단계에서 가드.
END_WRAPPER_DEPLOYED=0
if [ -f "$END_WRAPPER_SRC" ]; then
  if deploy_exec "$END_WRAPPER_SRC" "$END_WRAPPER_TARGET" "session-memory-end-async wrapper"; then
    manifest_record "hook" "$END_WRAPPER_TARGET"
    echo "✓ copied SessionEnd async wrapper to $END_WRAPPER_TARGET"
    END_WRAPPER_DEPLOYED=1
  else
    DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  fi
fi
for f in "${SPRINT3_SRC[@]}"; do
  if [ -f "$f" ]; then
    deploy_exec "$f" "$SCRIPTS_DIR/$(basename "$f")" "$(basename "$f")" \
      || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
    manifest_record "script" "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
echo "✓ deployed Sprint 3 scripts to $SCRIPTS_DIR"
# Sprint 3: /memory_review 스킬 배포 (v3.2.2 — defensive deploy_skill).
deploy_skill "$MEMORY_SKILL_SRC" "$MEMORY_SKILL_TARGET" "/memory_review" \
  || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
manifest_record "skill" "$MEMORY_SKILL_TARGET"

# /close-session + /cs alias 배포 (v3.2.2 — defensive deploy_skill).
# 자동 hook 의 narrative 보완용 명시 closer.
deploy_skill "$CLOSE_SESSION_SKILL_SRC" "$CLOSE_SESSION_SKILL_TARGET" "/close-session" \
  || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
manifest_record "skill" "$CLOSE_SESSION_SKILL_TARGET"
deploy_skill "$CS_SKILL_SRC" "$CS_SKILL_TARGET" "/cs" \
  || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
manifest_record "skill" "$CS_SKILL_TARGET"

# v3.1.1 (audit-2026-05-25 post-ship CRITICAL): 옛 personal SKILL 디렉토리
# `~/.claude/skills/{close-session,cs}/` 가 새 deploy 본을 가릴 수 있음.
# round-6 A,B 가드 추가: SKILL.md 부재 시 corrupt install 로 간주 → 보존,
# backup 디렉토리에 PID 추가 (같은 초 parallel install collision 차단).
# v3.2.3 (#15) — displace 시 manifest 에 [원본 경로 → 백업 경로] 기록.
# uninstall.sh 가 이 manifest 를 보고 사용자 personal SKILL 자동 복원.
PERSONAL_SKILLS_BACKUP="$HOME/.claude/skills.attic/mv3-skill-conflict-$(date +%Y%m%d-%H%M%S)-$$"
for personal in "$HOME/.claude/skills/close-session" "$HOME/.claude/skills/cs"; do
  if [ -d "$personal" ]; then
    skill_md="$personal/SKILL.md"
    if [ ! -f "$skill_md" ]; then
      # round-6 A: SKILL.md 없는 corrupt/empty 디렉토리 — 사용자 의도 불명, 보존
      echo "↷ $personal 는 SKILL.md 없음 (corrupt 가능) — 보존, 수동 정리 권장"
    elif grep -qF '[mv3-skill]' "$skill_md" 2>/dev/null; then
      # 이미 v3 deploy 본의 변형 — 그대로 두고 새 본이 commands/ 에서 작동
      echo "↷ $personal 는 v3 변형 (sentinel ✓) — 보존, $CLOSE_SESSION_SKILL_TARGET 우선 매칭"
    else
      # 사용자 personal — 백업 후 제거, manifest 에 restore 매핑 기록
      mkdir -p "$PERSONAL_SKILLS_BACKUP"
      mv "$personal" "$PERSONAL_SKILLS_BACKUP/"
      manifest_record "personal_skill_displaced" "$personal=>$PERSONAL_SKILLS_BACKUP/$(basename "$personal")"
      echo "⚠️  $personal 를 $PERSONAL_SKILLS_BACKUP/ 로 백업 (옛 personal SKILL 이 v3 본을 가리지 않도록)"
      echo "    uninstall 시 자동 복원됨 (manifest: $INSTALL_MANIFEST)"
    fi
  fi
done

if [ ! -f "$SETTINGS" ]; then
  echo '{"hooks":{}}' > "$SETTINGS"
  echo "✓ created $SETTINGS"
fi

cp "$SETTINGS" "$SETTINGS.bak"
echo "✓ backup at $SETTINGS.bak"

# v3.2.3 (#4): SessionEnd register 는 wrapper 가 실제 deploy 되었을 때만 호출.
# wrapper 부재 시 register 호출하면 settings.json 에 broken path 박힘.
# (#17): atomic write — tmp 파일에 쓰고 JSON 검증 후 os.replace. 실패 시 .bak 자동 복원.
SESSION_END_REGISTER=""
if [ "$END_WRAPPER_DEPLOYED" = "1" ]; then
  SESSION_END_REGISTER="$END_WRAPPER_TARGET"
fi

python3 - "$SETTINGS" "$HOOK_CMD" "$TARGET" "$SESSION_END_REGISTER" <<'PY'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])
start_cmd = sys.argv[2]
start_target = sys.argv[3]
end_wrapper_cmd = sys.argv[4]  # empty string 이면 register skip

try:
    raw = path.read_text() if path.stat().st_size else "{}"
    data = json.loads(raw)
except (json.JSONDecodeError, OSError) as e:
    bak = path.with_suffix(path.suffix + ".bak")
    msg = (f"⚠️  {path} 가 invalid JSON ({e}).\n"
           f"   .bak 에서 복원 시도: {bak}\n"
           f"   복원 후 다시 install.sh 실행 권장.")
    print(msg, file=sys.stderr)
    if bak.exists():
        try:
            data = json.loads(bak.read_text())
            path.write_text(bak.read_text())
            print(f"   ✓ restored from {bak}", file=sys.stderr)
        except Exception as e2:
            print(f"   ✗ .bak 도 invalid ({e2}) — 수동 복구 필요", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit(1)

hooks = data.setdefault("hooks", {})


def register(event_name, cmd, match_targets):
    """match_targets 의 어떤 substring 라도 매칭되는 기존 hook 모두 cleanup 후 cmd 단일 등록.

    NEXT-25: 옛 SessionEnd 가 wrapper(.sh) + 직접 py 두 hook 동시 등록 →
    매 fire 마다 always-fire bypass 두 번 + cache race. 둘 다 cleanup 후 wrapper 단일.
    """
    events = hooks.setdefault(event_name, [])
    cleaned = 0
    kept_events = []
    for entry in events:
        kept = []
        for h in entry.get("hooks", []):
            command = h.get("command") or ""
            if any(t in command for t in match_targets):
                cleaned += 1
                continue
            kept.append(h)
        if kept:
            entry["hooks"] = kept
            kept_events.append(entry)
    if cleaned:
        print(f"  removed {cleaned} stale MindVault entries from {event_name}")
    if cmd:  # empty cmd 이면 register skip — cleanup 만 수행 (v3.2.3 #4)
        kept_events.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": cmd}],
        })
        hooks[event_name] = kept_events
        print(f"✓ registered {event_name} hook")
    else:
        # 빈 list 면 key 자체 제거 (uninstall 의 cleanup 패턴과 일관)
        if kept_events:
            hooks[event_name] = kept_events
        else:
            hooks.pop(event_name, None)
        print(f"↷ {event_name} register skipped — wrapper 미배포, broken path 등록 회피")


register("SessionStart", start_cmd, [start_target])
# NEXT-25: SessionEnd 는 wrapper 만 단일 등록 — 옛 직접 py path 도 같이 cleanup.
# v3.2.3 (#4): end_wrapper_cmd 가 empty 이면 cleanup 만 하고 register skip.
register("SessionEnd", end_wrapper_cmd,
         ["session-memory-end.py", "session-memory-end-async.sh"])

# v3.2.3 (#17): atomic write via tmp + JSON 재검증 + os.replace.
serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
json.loads(serialized)  # round-trip 검증 (corruption 차단)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(serialized)
os.replace(tmp, path)
PY

if [ "${MV3_SYNC_ONLY:-0}" != "1" ]; then
echo ""
echo "→ Pre-warming Gemma cache (first-session 지연 방지, 최대 45초)..."
if "$TARGET" </dev/null >/dev/null 2>&1; then
  echo "✓ pre-warm complete"
else
  echo "  (pre-warm skipped: Gemma 서버 미응답. 첫 세션에서 실제 요약 시도됨)"
fi

echo ""
echo "→ Building FTS5 search index (Sprint 2)..."
if python3 "$SCRIPTS_DIR/indexer.py" >/dev/null 2>&1; then
  cnt=$(sqlite3 "$HOME/.claude/mindvault-v3/index.db" "SELECT COUNT(*) FROM sessions" 2>/dev/null || echo "?")
  echo "✓ indexed $cnt sessions"
else
  echo "  (index build skipped)"
fi
fi  # MV3_SYNC_ONLY — pre-warm + FTS index skip

echo ""
echo ""
echo "── Sprint 4 — Layer 4 Memory Recall (Arctic-ko hybrid RRF) ─────────────────────"

# Sprint 4 추가 자산
ARCTIC_SERVER_SRC="$REPO_DIR/scripts/arctic_ko_server.py"
ARCTIC_SERVER_TARGET="$SCRIPTS_DIR/arctic_ko_server.py"
# v3.2.3 (#1, #6) — Arctic-ko 도 wrapper 로 Python resolve. plist Python 3.10
# 절대경로 박힘 → 다른 사용자 fail 차단.
ARCTIC_RUNNER_SRC="$REPO_DIR/scripts/arctic_ko_server_runner.sh"
ARCTIC_RUNNER_TARGET="$SCRIPTS_DIR/arctic_ko_server_runner.sh"
# v3.2.3 (#2) — com.yonghaekim.arctic-ko-mlx → com.mindvault.arctic-ko-mlx
# (Gemma 와 동일 네임스페이스 통일, public ship sanitize).
ARCTIC_PLIST_SRC="$REPO_DIR/plist/com.mindvault.arctic-ko-mlx.plist"
ARCTIC_PLIST_TARGET="$HOME/Library/LaunchAgents/com.mindvault.arctic-ko-mlx.plist"
MEMORY_HOOK_SRC="$REPO_DIR/hooks/memory-recall.py"
MEMORY_HOOK_TARGET="$HOOKS_DIR/memory-recall.py"
SPRINT4_SRC=("$REPO_DIR/src/memory_indexer.py" "$REPO_DIR/src/memory_search.py")
ARCTIC_MODEL_DIR="$HOME/.cache/mlx-arctic-ko"

# 4.1 Python 의존성
if [ "${MV3_SYNC_ONLY:-0}" != "1" ]; then
echo "→ Installing Python dependencies (sqlite-vec mlx-embeddings pyyaml numpy huggingface_hub)..."
if python3 -m pip install --user --quiet sqlite-vec mlx-embeddings pyyaml numpy huggingface_hub 2>&1 | tail -3; then
  echo "✓ dependencies installed"
else
  echo "  (warning: dependency install had warnings — Sprint 4 may not work)"
fi
fi  # MV3_SYNC_ONLY — pip 의존성 설치 skip

# 4.2 — Arctic-ko 모델 자동 변환은 위 Sprint 4.5 (v3.2.0) 가 처리.
# (옛 수동 변환 안내 블록은 v3.2.0 에서 제거됨)

# 4.3 스크립트 + 서버 배포 (v3.2.3: deploy_exec 통일)
for f in "${SPRINT4_SRC[@]}"; do
  if [ -f "$f" ]; then
    deploy_exec "$f" "$SCRIPTS_DIR/$(basename "$f")" "$(basename "$f")" \
      || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
    manifest_record "script" "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
if [ -f "$ARCTIC_SERVER_SRC" ]; then
  deploy_exec "$ARCTIC_SERVER_SRC" "$ARCTIC_SERVER_TARGET" "arctic_ko_server.py" \
    || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  manifest_record "script" "$ARCTIC_SERVER_TARGET"
fi
# v3.2.3 (#1) — Arctic-ko runner wrapper deploy.
if [ -f "$ARCTIC_RUNNER_SRC" ]; then
  deploy_runner "$ARCTIC_RUNNER_SRC" "$ARCTIC_RUNNER_TARGET" "arctic_ko_server_runner" \
    || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  manifest_record "script" "$ARCTIC_RUNNER_TARGET"
fi
echo "✓ deployed Sprint 4 scripts to $SCRIPTS_DIR"

# Sprint 4: memory_review_cli도 reindex 트리거 포함된 새 버전으로 재배포
if [ -f "$REPO_DIR/src/memory_review_cli.py" ]; then
  deploy_exec "$REPO_DIR/src/memory_review_cli.py" "$SCRIPTS_DIR/memory_review_cli.py" \
    "memory_review_cli.py" || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  manifest_record "script" "$SCRIPTS_DIR/memory_review_cli.py"
fi

# post-ship: 런타임 import dependencies — 이전 install.sh 가 명시 누락한 채
# 옛 매뉴얼 cp 로만 production 에 존재하던 파일들. 신규 환경에서 install.sh
# 만 실행하면 hook 이 ImportError 로 silent fail 했음.
#
# 분류:
#  - hook 직접 import: query_intent (memory-recall.py),
#                      recall_core (memory-recall + session_memory compact 재주입 공용 게이트)
#  - extractor 체인: extractor_cache, memory_compiler (session-memory-end)
#  - turn 분할 캐시: turns_cache (recall_cli / search)
#  - Sprint 16+: sources_cli (영구 source 등록), backfill_cli (vec 백필),
#                dedup_cli (중복 정리), extractor_stats_cli (관측)
#  - NEXT-31~33: alias_generator (alias_index 자산 생성)
#  - v3.4 Layer 5 (T10): contradiction_detector (session-memory-end import),
#                        contradiction_review_cli (사용자 review CLI)
#  - Phase 1③ 신뢰성 (v3.x): reverify (session-memory-end 의 maybe_scan_due import),
#                            reverify_cli (수동 scan/list/verify-registry CLI)
RUNTIME_EXTRA_SRC=(
  "$REPO_DIR/src/query_intent.py"
  "$REPO_DIR/src/recall_core.py"
  "$REPO_DIR/src/extractor_cache.py"
  "$REPO_DIR/src/memory_compiler.py"
  "$REPO_DIR/src/turns_cache.py"
  "$REPO_DIR/src/sources_cli.py"
  "$REPO_DIR/src/backfill_cli.py"
  "$REPO_DIR/src/dedup_cli.py"
  "$REPO_DIR/src/extractor_stats_cli.py"
  "$REPO_DIR/src/alias_generator.py"
  "$REPO_DIR/src/contradiction_detector.py"
  "$REPO_DIR/src/contradiction_review_cli.py"
  "$REPO_DIR/src/reverify.py"
  "$REPO_DIR/src/reverify_cli.py"
)
for f in "${RUNTIME_EXTRA_SRC[@]}"; do
  if [ -f "$f" ]; then
    deploy_exec "$f" "$SCRIPTS_DIR/$(basename "$f")" "$(basename "$f")" \
      || DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
    manifest_record "script" "$SCRIPTS_DIR/$(basename "$f")"
  fi
done
echo "✓ deployed runtime extras ($(echo "${RUNTIME_EXTRA_SRC[@]}" | wc -w | tr -d ' ') files) to $SCRIPTS_DIR"

# 4.4a v3.2.3 (#3) — legacy plist migration (com.yonghaekim.*):
# - com.yonghaekim.bge-m3-mlx (Sprint 9 이전 설치자)
# - com.yonghaekim.arctic-ko-mlx (v3.2.0~v3.2.2 설치자 → v3.2.3 sanitize 후 rename)
# v3.x: SYNC 모드에선 launchctl 재시작(매 커밋마다 임베딩 서버 reload) 회피 위해 skip.
if [ "${MV3_SYNC_ONLY:-0}" != "1" ]; then
LEGACY_LAUNCHD_LABELS=(
  "com.yonghaekim.bge-m3-mlx"
  "com.yonghaekim.arctic-ko-mlx"
)
for label in "${LEGACY_LAUNCHD_LABELS[@]}"; do
  legacy_plist="$HOME/Library/LaunchAgents/${label}.plist"
  if [ -f "$legacy_plist" ]; then
    launchctl unload "$legacy_plist" >/dev/null 2>&1 || true
    rm -f "$legacy_plist"
    echo "✓ migrated: removed legacy plist ($label)"
  fi
done

# 4.4 Arctic-ko launchd plist (v3.2.3: deploy_plist 헬퍼 — Python 기반 안전 치환)
if [ -f "$ARCTIC_PLIST_SRC" ]; then
  deploy_plist "$ARCTIC_PLIST_SRC" "$ARCTIC_PLIST_TARGET" "arctic-ko plist" || exit 1
  echo "✓ Arctic-ko launchd service loaded (port 8081)"
fi
fi  # MV3_SYNC_ONLY — legacy plist 마이그레이션 + Arctic-ko plist 재시작 skip

# 4.5 hook 배포
MEMORY_HOOK_DEPLOYED=0
if [ -f "$MEMORY_HOOK_SRC" ]; then
  if deploy_exec "$MEMORY_HOOK_SRC" "$MEMORY_HOOK_TARGET" "memory-recall hook"; then
    manifest_record "hook" "$MEMORY_HOOK_TARGET"
    echo "✓ memory-recall hook at $MEMORY_HOOK_TARGET"
    MEMORY_HOOK_DEPLOYED=1
  else
    DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  fi
fi

# 4.6 헬스체크 (Arctic-ko 모델 로딩 대기 ~10초)
# v3.x: HEALTH_OK 는 SYNC 가드 밖에서 0 초기화 — set -u 안전 + sync 모드에선 0 으로
# 남아 아래 초기 인덱싱(HEALTH_OK=1 게이트)도 자동 skip.
HEALTH_OK=0
if [ "${MV3_SYNC_ONLY:-0}" != "1" ]; then
echo "→ Waiting for Arctic-ko to load (up to 30s)..."
for i in $(seq 1 15); do
  if curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/health 2>/dev/null | grep -q "200"; then
    HEALTH_OK=1
    break
  fi
  sleep 2
done
if [ "$HEALTH_OK" = "1" ]; then
  DIM=$(curl -sS http://127.0.0.1:8081/health | python3 -c "import json,sys; print(json.load(sys.stdin)['dim'])" 2>/dev/null || echo "?")
  echo "✓ Arctic-ko health: OK (dim=$DIM)"
else
  echo "  ✗ Arctic-ko health check failed — hook will silently no-op"
  echo "  diagnose: tail ~/Library/Logs/arctic-ko-mlx.err"
fi
fi  # MV3_SYNC_ONLY — Arctic-ko 헬스 대기 skip

# 4.7 settings.json UserPromptSubmit hook 등록.
# v3.2.3 (#4): hook 미배포 시 register skip (broken path 등록 차단).
# v3.2.3 (#17): atomic write — tmp + JSON 재검증 + os.replace.
# v3.2.3 (#19): stale memory-recall.py path entries 도 cleanup 후 새 target 등록 —
# 옛 path 잔존 시 새 hook 등록이 막혔던 idempotency 결함 fix.
if [ "$MEMORY_HOOK_DEPLOYED" = "1" ]; then
  # v4 P1: MV3_AGENT=claude 명시 — 회수 계측의 에이전트 소스 태깅 (codex 쪽은 manage_codex_recall.py 가 대칭 태깅)
  python3 - "MV3_AGENT=claude $MEMORY_HOOK_TARGET" "$SETTINGS" <<'PY'
import json, os, sys
from pathlib import Path

hook_cmd = sys.argv[1]
settings_path = Path(sys.argv[2])

try:
    data = json.loads(settings_path.read_text())
except (json.JSONDecodeError, OSError) as e:
    bak = settings_path.with_suffix(settings_path.suffix + ".bak")
    print(f"⚠️  {settings_path} invalid JSON ({e}). Try restore from {bak}.", file=sys.stderr)
    if bak.exists():
        data = json.loads(bak.read_text())
        settings_path.write_text(bak.read_text())
    else:
        sys.exit(1)

hooks = data.setdefault("hooks", {})
ups = hooks.setdefault("UserPromptSubmit", [])

# Stale memory-recall.py entries cleanup (SessionStart/SessionEnd 패턴 일관).
cleaned = 0
kept_events = []
for entry in ups:
    kept = [h for h in entry.get("hooks", [])
            if "memory-recall.py" not in (h.get("command") or "")]
    cleaned += len(entry.get("hooks", [])) - len(kept)
    if kept:
        entry["hooks"] = kept
        kept_events.append(entry)
if cleaned:
    print(f"  removed {cleaned} stale memory-recall entries from UserPromptSubmit")
kept_events.append({
    "matcher": "*",
    "hooks": [{"type": "command", "command": hook_cmd}],
})
hooks["UserPromptSubmit"] = kept_events
print("✓ registered UserPromptSubmit hook (memory-recall)")

# atomic write
serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
json.loads(serialized)
tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
tmp.write_text(serialized)
os.replace(tmp, settings_path)
PY
else
  echo "↷ UserPromptSubmit register skipped — memory-recall hook 미배포"
fi

# 4.8 초기 인덱싱 (모델 + health 둘 다 확보돼야 진행)
if [ "$HEALTH_OK" = "1" ] && [ "$ARCTIC_MODEL_READY" = "1" ]; then
  echo "→ Initial memory indexing (may take ~30-60s for ~100 memories)..."
  if python3 -c "
import sys
sys.path.insert(0, '$SCRIPTS_DIR')
from memory_indexer import full_rebuild
n = full_rebuild()
print(f'✓ indexed {n} memories')
" 2>&1 | tail -3; then
    :
  else
    echo "  (initial indexing skipped — run later with: python3 $SCRIPTS_DIR/memory_indexer.py)"
  fi

  # v3.2.7: Arctic-ko cold inference 미리 trigger — 첫 indexer batch 의 5s
  # embed timeout 8건 누적 회피. health check (200 OK) 는 통과해도 첫 inference
  # 가 cold (모델 로딩 후 첫 forward pass) 라 indexer 가 일부 잡힘.
  echo "→ Pre-warming Arctic-ko (cold inference)..."
  curl -sS -X POST http://127.0.0.1:8081/embed \
    -H "Content-Type: application/json" \
    -d '{"text":"warmup","kind":"document"}' \
    --max-time 60 >/dev/null 2>&1 || true

  # 4.9 hook warmup — cold-start latency 미리 지불 (200ms→150ms)
  echo "→ Pre-warming hook (cold start mitigation)..."
  echo '{"prompt":"warmup"}' | python3 "$MEMORY_HOOK_TARGET" >/dev/null 2>&1 || true
  echo "✓ hook pre-warmed"
fi

# ── v3.x — 배포 후크화: git hook self-wire + drift 백스톱 ───────────────────────
# (1) repo 경로 기록 — deploy_drift_check.py 가 src/ 원본을 찾는 데 사용.
# (2) git post-commit/post-merge 훅 self-wire (core.hooksPath=.githooks) — repo
#     커밋/머지가 배포 경로에 자동 반영돼 "repo 는 고쳤는데 배포는 stale" 차단.
# (3) deploy_drift_check SessionStart 훅 배포·등록 — 훅 우회(--no-verify)/외부
#     동기화 누락 시 세션 시작에 stale 배포 경고 (감사된 session_memory.py 무수정).
# MV3_SKIP_GIT_WIRE=1 — 테스트 격리용 (실제 repo git config 비변경).
if [ -d "$REPO_DIR/.git" ] && [ "${MV3_SKIP_GIT_WIRE:-0}" != "1" ]; then
  echo "$REPO_DIR" > "$INSTALL_MANIFEST_DIR/.repo-path"
  # bug-audit 2026-06-02 (R3): 덮어쓰기 전에 사용자의 기존 core.hooksPath 를 기록 →
  # uninstall 이 unset 이 아니라 복원할 수 있게 한다. 미기록 시 사용자가 install 전부터
  # 쓰던 custom hooksPath 가 uninstall 후 영구 소실된다.
  PRIOR_HP="$(git -C "$REPO_DIR" config --get core.hooksPath 2>/dev/null || true)"
  if [ -n "$PRIOR_HP" ] && [ "$PRIOR_HP" != ".githooks" ]; then
    printf '%s\n' "$PRIOR_HP" > "$INSTALL_MANIFEST_DIR/.prior-hookspath"
  fi
  if git -C "$REPO_DIR" config core.hooksPath .githooks 2>/dev/null; then
    echo "✓ git hooks wired (core.hooksPath=.githooks — post-commit/post-merge 자동 sync)"
  else
    echo "  ⚠ git hooksPath 설정 실패 (수동: git -C $REPO_DIR config core.hooksPath .githooks)"
  fi
fi

DRIFT_SRC="$REPO_DIR/scripts/deploy_drift_check.py"
DRIFT_TARGET="$SCRIPTS_DIR/deploy_drift_check.py"
if [ -f "$DRIFT_SRC" ]; then
  if deploy_exec "$DRIFT_SRC" "$DRIFT_TARGET" "deploy-drift-check hook"; then
    manifest_record "hook" "$DRIFT_TARGET"
    python3 - "$DRIFT_TARGET" "$SETTINGS" <<'PY'
import json, os, sys
from pathlib import Path

cmd = sys.argv[1]
sp = Path(sys.argv[2])
try:
    data = json.loads(sp.read_text())
except (json.JSONDecodeError, OSError):
    data = {"hooks": {}}
hooks = data.setdefault("hooks", {})
ss = hooks.setdefault("SessionStart", [])
# 기존 drift 등록 cleanup 후 단일 재등록 (멱등 — install.sh register() 패턴과 일관).
kept = []
for entry in ss:
    h = [x for x in entry.get("hooks", [])
         if "deploy_drift_check.py" not in (x.get("command") or "")]
    if h:
        entry["hooks"] = h
        kept.append(entry)
kept.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})
hooks["SessionStart"] = kept
serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
json.loads(serialized)  # round-trip 검증 (corruption 차단)
tmp = sp.with_suffix(sp.suffix + ".tmp")
tmp.write_text(serialized)
os.replace(tmp, sp)
print("✓ registered SessionStart hook (deploy-drift-check)")
PY
  else
    DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  fi
fi

# ── v4 P4: Codex 통합 — ~/.codex 존재 시 hook·close-session 스킬·AGENTS 규약 배포 ──
# Claude 단독 사용자는 조용히 skip (신규 설치자 경로 — Codex 리뷰 반영).
# 시뮬레이션 override: MV3_CODEX_HOME / MV3_CODEX_SKILLS_DIR / MV3_CODEX_AGENTS_MD.
CODEX_HOME="${MV3_CODEX_HOME:-$HOME/.codex}"
if [ -d "$CODEX_HOME" ]; then
  # ① recall hook 등록 — hooks.json 은 manage_codex_recall.py 가 단일 작성자
  #    (S4 이후 stop hook 등록도 같은 스크립트가 담당).
  if python3 "$REPO_DIR/scripts/manage_codex_recall.py" install \
      --config "$CODEX_HOME/hooks.json" >/dev/null 2>&1; then
    echo "✓ Codex recall hook registered ($CODEX_HOME/hooks.json)"
  else
    echo "⚠️  Codex recall hook 등록 실패 — 수동: python3 scripts/manage_codex_recall.py install"
    DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
  fi
  # ② close-session 스킬 배포 (repo 템플릿 codex/ 가 정본)
  CODEX_SKILL_SRC="$REPO_DIR/codex/close-session/SKILL.md"
  CODEX_SKILLS_DIR="${MV3_CODEX_SKILLS_DIR:-$HOME/.agents/skills}"
  if [ -f "$CODEX_SKILL_SRC" ]; then
    if mkdir -p "$CODEX_SKILLS_DIR/close-session" \
        && cp "$CODEX_SKILL_SRC" "$CODEX_SKILLS_DIR/close-session/SKILL.md"; then
      echo "✓ Codex close-session skill → $CODEX_SKILLS_DIR/close-session/"
    else
      DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
    fi
  fi
  # ③ AGENTS.md 규약 snippet — 마커 사이 idempotent 교체 (사용자 내용 보존)
  CODEX_AGENTS_MD="${MV3_CODEX_AGENTS_MD:-$CODEX_HOME/AGENTS.md}"
  if [ -f "$REPO_DIR/codex/AGENTS-mindvault-snippet.md" ]; then
    if python3 - "$REPO_DIR/codex/AGENTS-mindvault-snippet.md" "$CODEX_AGENTS_MD" <<'PY'
import sys
from pathlib import Path

snippet_path, agents_path = Path(sys.argv[1]), Path(sys.argv[2])
START = "<!-- MINDVAULT_MEMORY_START -->"
END = "<!-- MINDVAULT_MEMORY_END -->"
snippet = snippet_path.read_text(encoding="utf-8").strip() + "\n"
if not (snippet.startswith(START) and snippet.rstrip().endswith(END)):
    print("snippet 마커 불일치 — skip", file=sys.stderr)
    sys.exit(1)
text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
if START in text and END in text:
    pre, rest = text.split(START, 1)
    _, post = rest.split(END, 1)
    new = pre + snippet.rstrip("\n") + post
else:
    sep = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    new = text + sep + snippet
if new != text:
    tmp = agents_path.with_suffix(agents_path.suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(agents_path)
PY
    then
      echo "✓ Codex AGENTS.md MindVault 규약 반영 ($CODEX_AGENTS_MD)"
    else
      echo "⚠️  Codex AGENTS.md snippet 반영 실패"
      DEPLOY_FAILURES=$((DEPLOY_FAILURES+1))
    fi
  fi
else
  echo "↷ Codex 미감지 ($CODEX_HOME 없음) — Codex 통합 skip"
fi

echo ""
# v3.2.7: manifest atomic commit — 여기까지 도달했으면 모든 deploy 단계가
# set -e 를 통과. tmp → final 로 atomic mv (os.rename 동치).
mv "$INSTALL_MANIFEST_TMP" "$INSTALL_MANIFEST"

# v3.2.3 (#16): deploy 실패 누적 시 명시 경고 — 옛 `|| true` 가 silent swallow 했던
# 결함 fix. "Installation complete" 메시지를 사용자가 보고도 실제로는 skill 누락된
# 상태였던 시나리오 차단.
if [ "$DEPLOY_FAILURES" -gt 0 ]; then
  echo "⚠️  Installation finished with $DEPLOY_FAILURES deploy failure(s)."
  echo "   manifest: $INSTALL_MANIFEST"
  echo "   원인 진단 후 ./install.sh 재실행 권장."
else
  echo "Installation complete. Start a new Claude Code session to verify."
fi
echo "Try: /recall <검색어>"
echo "Uninstall: ./uninstall.sh"
