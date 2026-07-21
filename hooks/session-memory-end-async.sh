#!/bin/bash
# MindVault v3 — SessionEnd 비동기 래퍼.
# 원본 session-memory-end.py가 Gemma를 동기 호출하여 exit 30s+ 블로킹 → detach로 해결.
# 2026-05-22: 무한 재귀 방지 guard 추가 (sub-session에서 즉시 exit).
# 2026-05-24 (NEXT-19): Claude Code 가 hook subprocess spawn 시 본체 env 만 inherit —
# shell init 안 거침. plist + .zshenv 로 셸·login 보장해도 hook 본체 env 가 비어있으면
# 의미 없음. wrapper 에서 명시 export 로 hook 까지 inherit 강제. 다른 fallback 다 fail 한
# 최후 수단.

set -u

# sub-session의 SessionEnd hook 즉시 skip
if [ "${MV3_HOOK_RECURSION_GUARD:-}" = "1" ]; then
  exit 0
fi

# NEXT-19 hook subprocess env 강제 (위 주석 참조)
# [2026-07-21] MV3_GEMMA_INTENT export 제거 — session-end 경로는 이 env 를 읽지
# 않고(query_intent 는 recall hot path 전용), 전역 off 결정과 표기 일관 유지.
export MV3_AUTO_COMPILE=1
export MV3_EXTRACTOR_ALWAYS_FIRE=1

TMP_DIR="${TMPDIR:-/tmp}"
TMP_STDIN=$(mktemp "${TMP_DIR}/mindvault-end-stdin.XXXXXX") || exit 0

find "${TMP_DIR}" -maxdepth 1 -name 'mindvault-end-stdin.*' -type f -mmin +60 -delete 2>/dev/null || true

cat > "$TMP_STDIN" 2>/dev/null || true

# NEXT-23 (2026-05-24): macOS 에 setsid 없음 (`setsid: command not found`) →
# 옛 wrapper 가 첫 줄에서 fail → async detach 못함 → settings.json 두 번째 hook
# (session-memory-end.py 직접) 만 fire → Claude Code 종료 시 subprocess SIGTERM →
# Gemma 호출 도중 죽음. subshell + nohup + disown 으로 macOS 호환 detach.
(
  trap 'rm -f "$TMP_STDIN"' EXIT
  nohup /usr/bin/env python3 "$HOME/.claude/hooks/session-memory-end.py" < "$TMP_STDIN" >/dev/null 2>&1
) </dev/null >/dev/null 2>&1 &
disown
exit 0
