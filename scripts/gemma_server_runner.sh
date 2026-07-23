#!/usr/bin/env bash
# MindVault v3.2.4 — Gemma MLX 서버 launchd entry.
# plist 가 이 스크립트를 ProgramArguments 로 호출.
# v3.2.4: install.sh 의 python3 와 wrapper python3 mismatch 로 인한 mlx
# ImportError 차단 위해 __INSTALL_PYTHON_BIN__ placeholder 도입.
# 환경변수:
#   MV3_RUNNER_DRY_RUN=1  실제 실행 안 하고 명령만 echo (테스트용)
set -euo pipefail

MODEL="${MV3_GEMMA_MODEL:-mlx-community/gemma-4-e4b-it-4bit}"
HOST="${MV3_GEMMA_HOST:-127.0.0.1}"
PORT="${MV3_GEMMA_PORT:-8080}"

# launchd 환경의 좁은 PATH 보완 — pip --user 설치된 mlx_lm 찾기 위해.
# 맨 앞 __INSTALL_PYTHON_BIN__ 는 install.sh 가 deploy 시 자기 python3 의
# bin dir 로 치환 (v3.2.4 fix). 다른 사용자 환경에서도 install/wrapper 일치.
export PATH="__INSTALL_PYTHON_BIN__:$HOME/.local/bin:$HOME/Library/Python/3.10/bin:$HOME/Library/Python/3.11/bin:$HOME/Library/Python/3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

CMD=(python3 -m mlx_lm.server --model "$MODEL" --host "$HOST" --port "$PORT")

if [ "${MV3_RUNNER_DRY_RUN:-0}" = "1" ]; then
  echo "DRY_RUN: ${CMD[*]}"
  exit 0
fi

exec "${CMD[@]}"
