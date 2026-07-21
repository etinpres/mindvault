#!/usr/bin/env bash
# MindVault v3 uninstaller — removes hooks, scripts, launchd services, and skill registrations.
# v3.2.3: personal SKILL manifest restore (#15), MV3_SCRIPTS_DIR 존중 (#22),
#         settings.json atomic write (#17), LAUNCHD_LABELS 정리 (#3).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
INSTALL_MANIFEST="$HOME/.claude/mindvault-v3/.install-manifest"
# v3.2.7: install 중단 시 남는 .tmp manifest 정리 대상.
INSTALL_MANIFEST_TMP="$HOME/.claude/mindvault-v3/.install-manifest.tmp"

# v3.2.0 — Gemma plist + cache 정리.
# launchd 에서 com.mindvault.gemma-mlx 만 정리 (다른 사용자 이름의 gemma-mlx
# 서비스는 보존 — 그건 사용자 자체 관리).
GEMMA_LAUNCH_AGENTS="${MV3_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
GEMMA_PLIST="$GEMMA_LAUNCH_AGENTS/com.mindvault.gemma-mlx.plist"
GEMMA_CACHE="${MV3_GEMMA_CACHE:-$HOME/.cache/mv3-gemma}"

remove_gemma_assets() {
  if [ -f "$GEMMA_PLIST" ]; then
    if [ "${MV3_UNINSTALL_DRY_LAUNCHCTL:-0}" != "1" ]; then
      launchctl unload "$GEMMA_PLIST" 2>/dev/null || true
    fi
    rm -f "$GEMMA_PLIST"
    echo "✓ removed Gemma plist ($GEMMA_PLIST)"
  fi
  if [ -d "$GEMMA_CACHE" ]; then
    rm -rf "$GEMMA_CACHE"
    echo "✓ removed Gemma cache ($GEMMA_CACHE)"
  fi
}

# test 격리: MV3_UNINSTALL_GEMMA_ONLY=1 → Gemma 부분만 처리하고 exit.
if [ "${MV3_UNINSTALL_GEMMA_ONLY:-0}" = "1" ]; then
  remove_gemma_assets
  exit 0
fi

# Optional Codex integration must be unwired before the shared Claude recall
# hook is removed, otherwise ~/.codex/hooks.json retains a broken command.
CODEX_HOOK_MANAGER="$REPO_DIR/scripts/manage_codex_recall.py"
# v4 X1-R1: install 과 동일하게 MV3_CODEX_HOME override 를 --config 로 전달
# (미전달 시 기본 ~/.codex/hooks.json 을 건드리는 비대칭 — Codex 검수 Medium).
CODEX_HOME="${MV3_CODEX_HOME:-$HOME/.codex}"
if [ -f "$CODEX_HOOK_MANAGER" ] && command -v python3 >/dev/null 2>&1; then
  if ! python3 "$CODEX_HOOK_MANAGER" uninstall --config "$CODEX_HOME/hooks.json" >/dev/null; then
    echo "⚠ failed to remove optional Codex recall hook; run: python3 $CODEX_HOOK_MANAGER uninstall" >&2
  fi
fi

# v4 P4: Codex close-session 스킬 + AGENTS.md snippet 제거 (install.sh 대칭).
CODEX_SKILLS_DIR="${MV3_CODEX_SKILLS_DIR:-$HOME/.agents/skills}"
rm -f "$CODEX_SKILLS_DIR/close-session/SKILL.md" 2>/dev/null || true
rmdir "$CODEX_SKILLS_DIR/close-session" 2>/dev/null || true
CODEX_AGENTS_MD="${MV3_CODEX_AGENTS_MD:-$CODEX_HOME/AGENTS.md}"
if [ -f "$CODEX_AGENTS_MD" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$CODEX_AGENTS_MD" <<'PY' || true
import sys
from pathlib import Path

p = Path(sys.argv[1])
START = "<!-- MINDVAULT_MEMORY_START -->"
END = "<!-- MINDVAULT_MEMORY_END -->"
t = p.read_text(encoding="utf-8")
if START in t and END in t:
    pre, rest = t.split(START, 1)
    _, post = rest.split(END, 1)
    new = pre.rstrip("\n") + "\n" + post.lstrip("\n")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(new.lstrip("\n"), encoding="utf-8")
    tmp.replace(p)
PY
fi

# v3.2.3 (#22) — MV3_SCRIPTS_DIR override 존중. install 과 동일 변수 사용.
# (HOOK_TARGETS 보다 먼저 정의 — bug-audit 2026-06-02 #7 의 drift hook 경로 매칭용.)
SCRIPTS_DIR="${MV3_SCRIPTS_DIR:-$HOME/.claude/scripts/mindvault}"

# Hook scripts targeted for file removal (rm). MUST be absolute paths only —
# 이 배열은 settings.json 매칭과 파일 rm 양쪽에 쓰인다.
HOOK_TARGETS=(
  "$HOOKS_DIR/session-memory.py"
  "$HOOKS_DIR/session-memory-end.py"
  "$HOOKS_DIR/session-memory-end-async.sh"
  "$HOOKS_DIR/session-memory-precompute.sh"
  "$HOOKS_DIR/memory-recall.py"
)
# bug-audit 2026-06-02 (R3): settings.json 매칭 *전용* substring (rm 루프엔 절대
# 넣지 않는다). #7 에서 drift 훅 제거를 위해 bare "deploy_drift_check.py" 를
# HOOK_TARGETS 에 넣었더니 rm 루프가 cwd 의 동명 파일(예: repo scripts/ 에서
# uninstall 실행 시 소스)을 삭제하는 footgun 이 됐다. 실제 배포 파일은 $SCRIPTS_DIR
# rm -rf 가 지우므로, 이 토큰은 settings 매칭에만 쓴다. install 은 default
# SCRIPTS_DIR 경로로 등록하지만 uninstall 은 MV3_SCRIPTS_DIR override 를 존중하므로
# 경로 불일치에도 확실히 제거되도록 파일명으로 매칭.
SETTINGS_MATCH_EXTRA=(
  "deploy_drift_check.py"
)

# v3.2.3 (#3) — Launchd labels MindVault v3 가 install 단계에서 deploy 하는 것만.
# gemma-mlx (com.mindvault.gemma-mlx) 는 remove_gemma_assets() 에서 별도 처리.
# 옛 com.yonghaekim.* legacy 도 동시 cleanup — install.sh 의 LEGACY_LAUNCHD_LABELS
# 와 동일 list 유지.
LAUNCHD_LABELS=(
  "com.mindvault.arctic-ko-mlx"
)
LEGACY_LAUNCHD_LABELS=(
  "com.yonghaekim.arctic-ko-mlx"
  "com.yonghaekim.bge-m3-mlx"
  "com.yonghaekim.mv3-env"
  "com.yonghaekim.mv3-gemma-intent"
  "com.yonghaekim.mv3-stats-daily"
)

# --- 1. settings.json hook entries ----------------------------------------
# v3.2.3 (#17): atomic write — tmp + JSON round-trip 검증 + os.replace.
# JSON parse 실패 시 .bak 에서 자동 복원.
if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak"
  # settings 매칭엔 HOOK_TARGETS(절대경로) + SETTINGS_MATCH_EXTRA(substring) 모두 전달.
  python3 - "$SETTINGS" "${HOOK_TARGETS[@]}" "${SETTINGS_MATCH_EXTRA[@]}" <<'PY'
import json, os, sys
from pathlib import Path

path = Path(sys.argv[1])
targets = sys.argv[2:]

try:
    data = json.loads(path.read_text()) if path.stat().st_size else {}
except (json.JSONDecodeError, OSError) as e:
    bak = path.with_suffix(path.suffix + ".bak")
    print(f"⚠️  {path} invalid JSON ({e}). Attempting restore from {bak}.", file=sys.stderr)
    if bak.exists():
        data = json.loads(bak.read_text())
        path.write_text(bak.read_text())
    else:
        sys.exit(1)

hooks = data.get("hooks", {})


def matches(cmd: str) -> bool:
    return any(t in (cmd or "") for t in targets)


removed = 0
for event_name in ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop"):
    events = hooks.get(event_name, [])
    new_events = []
    for entry in events:
        kept = [h for h in entry.get("hooks", []) if not matches(h.get("command", ""))]
        removed += len(entry.get("hooks", [])) - len(kept)
        if kept:
            entry["hooks"] = kept
            new_events.append(entry)
    if new_events:
        hooks[event_name] = new_events
    elif event_name in hooks:
        del hooks[event_name]

serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
json.loads(serialized)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(serialized)
os.replace(tmp, path)
print(f"✓ removed {removed} hook entries from settings.json")
PY
fi

# --- 2. Hook scripts ------------------------------------------------------
for target in "${HOOK_TARGETS[@]}"; do
  if [ -f "$target" ]; then
    rm -f "$target"
    echo "✓ removed $target"
  fi
done

# --- 3. Slash command skills ----------------------------------------------
MEMORY_REVIEW_SKILL="$HOME/.claude/commands/memory_review.md"
if [ -f "$MEMORY_REVIEW_SKILL" ]; then
  rm -f "$MEMORY_REVIEW_SKILL"
  echo "✓ removed $MEMORY_REVIEW_SKILL"
fi

RECALL_SKILL="$HOME/.claude/commands/recall.md"
if [ -f "$RECALL_SKILL" ]; then
  rm -f "$RECALL_SKILL"
  echo "✓ removed $RECALL_SKILL"
fi

# /close-session + /cs alias 정리.
# audit-2026-05-25 N8 + round-3 D: 사용자 커스터마이즈 보존 위해 unique marker `[mv3-skill]` 매칭.
# (두 skill frontmatter description 첫머리에 박힌 고유 토큰. "MindVault v3" 같은 흔한 문구로는
# 사용자 personal 본문도 false-positive delete 가능했음.)
for skill in close-session cs; do
  target="$HOME/.claude/commands/${skill}.md"
  if [ -f "$target" ]; then
    if grep -qF '[mv3-skill]' "$target" 2>/dev/null; then
      rm -f "$target"
      echo "✓ removed $target"
    else
      echo "↷ skipped $target ([mv3-skill] marker 없음 — 사용자 변형으로 보임)"
    fi
  fi
done

# v3.2.3 (#15) — personal SKILL manifest restore.
# install 시 ~/.claude/skills/{close-session,cs} 가 v3 변형이 아닌 사용자 personal
# 본이었으면 ~/.claude/skills.attic/mv3-skill-conflict-*/ 로 displace 됐다. 이를
# manifest 의 personal_skill_displaced 항목 보고 자동 복원.
if [ -f "$INSTALL_MANIFEST" ]; then
  restored=0
  while IFS= read -r line; do
    case "$line" in
      personal_skill_displaced=*)
        mapping="${line#personal_skill_displaced=}"
        # original_path=>backup_path 형식. malformed 라인 (=> 없음) 은 skip.
        case "$mapping" in
          *=\>*) ;;
          *)
            echo "↷ skip malformed manifest entry: $line" >&2
            continue
            ;;
        esac
        original="${mapping%%=>*}"
        backup="${mapping#*=>}"
        if [ -d "$backup" ] && [ ! -e "$original" ]; then
          parent_dir="$(dirname "$original")"
          mkdir -p "$parent_dir"
          mv "$backup" "$original"
          restored=$((restored+1))
          echo "✓ restored personal SKILL: $original (from $backup)"
        elif [ -e "$original" ]; then
          echo "↷ skip restore $original — 이미 존재 ($backup 는 수동 정리 필요)"
        fi
        ;;
    esac
  done < "$INSTALL_MANIFEST"
  if [ "$restored" -gt 0 ]; then
    echo "✓ restored $restored personal SKILL(s) from displace backup"
  fi
fi

# --- 4. Deployed scripts directory ----------------------------------------
# SCRIPTS_DIR 는 파일 상단(HOOK_TARGETS 앞)에서 이미 정의됨 (MV3_SCRIPTS_DIR 존중).
if [ -d "$SCRIPTS_DIR" ]; then
  rm -rf "$SCRIPTS_DIR"
  echo "✓ removed $SCRIPTS_DIR"
fi

# --- 4b. git hook unwire + .repo-path marker (bug-audit 2026-06-02 #2) ------
# install.sh 가 git checkout 에서 실행되면 core.hooksPath=.githooks 를 설정하고
# .repo-path 를 기록한다. 이를 해제하지 않으면 다음 커밋의 post-commit 훅이
# `MV3_SYNC_ONLY=1 install.sh` 를 다시 돌려 방금 제거한 시스템을 통째로 부활시킨다
# (uninstall 이 안 stick — 특히 contributor 의 git checkout 머신).
REPO_PATH_MARKER="$HOME/.claude/mindvault-v3/.repo-path"
PRIOR_HP_MARKER="$HOME/.claude/mindvault-v3/.prior-hookspath"
if [ -f "$REPO_PATH_MARKER" ]; then
  REPO_DIR_RECORDED="$(cat "$REPO_PATH_MARKER" 2>/dev/null || true)"
  if [ -n "$REPO_DIR_RECORDED" ] && [ -d "$REPO_DIR_RECORDED/.git" ]; then
    # 사용자가 직접 다른 값으로 바꾼 경우 보존 — install 이 설정한 .githooks 일 때만 처리.
    current_hp="$(git -C "$REPO_DIR_RECORDED" config --get core.hooksPath 2>/dev/null || true)"
    if [ "$current_hp" = ".githooks" ]; then
      # bug-audit 2026-06-02 (R3): install 이전 사용자 custom hooksPath 가 기록돼 있으면
      # unset 대신 *복원* (install 의 clobber 를 되돌림). 없으면 unset.
      if [ -f "$PRIOR_HP_MARKER" ]; then
        PRIOR_HP="$(cat "$PRIOR_HP_MARKER" 2>/dev/null || true)"
        if [ -n "$PRIOR_HP" ]; then
          git -C "$REPO_DIR_RECORDED" config core.hooksPath "$PRIOR_HP" 2>/dev/null || true
          echo "✓ restored git core.hooksPath='$PRIOR_HP' in $REPO_DIR_RECORDED (install 이전 값 복원)"
        else
          git -C "$REPO_DIR_RECORDED" config --unset core.hooksPath 2>/dev/null || true
          echo "✓ unset git core.hooksPath in $REPO_DIR_RECORDED (post-commit 자동 재배포 차단)"
        fi
      else
        git -C "$REPO_DIR_RECORDED" config --unset core.hooksPath 2>/dev/null || true
        echo "✓ unset git core.hooksPath in $REPO_DIR_RECORDED (post-commit 자동 재배포 차단)"
      fi
    fi
  fi
  rm -f "$REPO_PATH_MARKER" "$PRIOR_HP_MARKER"
  echo "✓ removed .repo-path marker"
fi

# --- 5. Launchd services --------------------------------------------------
LAUNCH_AGENTS="${MV3_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
for label in "${LAUNCHD_LABELS[@]}" "${LEGACY_LAUNCHD_LABELS[@]}"; do
  PLIST="$LAUNCH_AGENTS/${label}.plist"
  if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "✓ removed launchd $label"
  fi
done

# --- 5b. v3.2.0 Gemma plist + cache ---------------------------------------
# v3.2.0 가 새로 도입한 com.mindvault.gemma-mlx 만 정리.
# 다른 사용자 이름의 gemma-mlx (예: com.<user>.gemma-mlx) 는 보존.
remove_gemma_assets

# --- 6. Manifest cleanup --------------------------------------------------
# v3.2.3: 모든 deploy 자산 cleanup 후 manifest 자체 제거 — uninstall 흔적 0.
if [ -f "$INSTALL_MANIFEST" ]; then
  rm -f "$INSTALL_MANIFEST"
  echo "✓ removed $INSTALL_MANIFEST"
fi
# v3.2.7: 중단된 install 이 남긴 .tmp manifest 도 정리.
if [ -f "$INSTALL_MANIFEST_TMP" ]; then
  rm -f "$INSTALL_MANIFEST_TMP"
fi

# --- 7. Optional: drop memories_* tables (--purge-vec) --------------------
if [ "${1:-}" = "--purge-vec" ]; then
  if [ -f "$HOME/.claude/mindvault-v3/index.db" ]; then
    sqlite3 "$HOME/.claude/mindvault-v3/index.db" \
      "DROP TABLE IF EXISTS memories_vec; DROP TABLE IF EXISTS memories_fts; DROP TABLE IF EXISTS memories;" 2>/dev/null \
      && echo "✓ dropped memories_* tables (--purge-vec)"
  fi
fi

echo ""
echo "Uninstall complete."
echo "Cache + index preserved at $HOME/.claude/mindvault-v3/. Delete manually if desired."
echo "Arctic-ko model preserved at $HOME/.cache/mlx-arctic-ko/ (~322MB). Delete manually if desired."
echo "Other gemma-mlx launchd services (com.<user>.gemma-mlx 등) preserved — 본 uninstaller 는 com.mindvault.gemma-mlx 만 정리."
