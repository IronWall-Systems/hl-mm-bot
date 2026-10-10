#!/usr/bin/env bash
#
# scripts/push_to_drive.sh — push the local dataset folder to the human's Google Drive remote.
#
#   rclone copy data/ gdrive:hl-datasets/ --min-age 2h --checksum
#
# SAFETY (REQ-0033 / T9b) — this script is written but NOT run by the agent:
#   * DRY-RUN BY DEFAULT: it always shows `rclone ... --dry-run` (what would be copied) first;
#     the real copy happens only after `--apply` and the typed word CLEAN;
#   * refuses to run at all if `rclone` is missing (or if data/ does not exist);
#   * NEVER passes a delete flag: no --delete-after / --delete-before / --delete-during /
#     --max-delete / --rmdirs. `rclone copy` does not delete destination files by itself;
#   * NEVER opens, prints, copies or passes `~/.config/rclone/rclone.conf` (or any --config /
#     RCLONE_CONFIG): only the remote name `gdrive:hl-datasets/` is used, and rclone reads its
#     own config;
#   * never runs sudo, never reads .env.
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
SRC="${PROJECT_ROOT}/data/"
REMOTE="gdrive:hl-datasets/"
CLEAN_WORD="CLEAN"

# The exact, only, copy command. No delete flags — keep it that way.
COPY_ARGS=(copy "$SRC" "$REMOTE" --min-age 2h --checksum)

usage() {
  cat <<'EOF'
usage: scripts/push_to_drive.sh [--apply] [-h|--help]

  (no flag)  dry-run: prints the command and runs `rclone ... --dry-run` so you can see what
             would be copied; copies nothing
  --apply    actually copy (after the typed word CLEAN)
  -h, --help this text
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

main() {
  local want_apply=0
  while (($#)); do
    case "$1" in
      --apply) want_apply=1 ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done

  command -v rclone >/dev/null 2>&1 || die "rclone is not installed — nothing will be pushed"
  [[ -d "$SRC" ]] || die "source folder missing: $SRC (nothing to push)"
  if [[ "${COPY_ARGS[*]}" == *"--delete"* || "${COPY_ARGS[*]}" == *"--rmdirs"* ]]; then
    die "refusing to run: a delete flag is present in COPY_ARGS (invariant violated)"
  fi

  printf '=== push_to_drive — what would be copied (remote: %s) ===\n' "$REMOTE"
  printf '  $ rclone'
  printf ' %q' "${COPY_ARGS[@]}" --dry-run
  printf '\n'
  if ! rclone "${COPY_ARGS[@]}" --dry-run; then
    printf '    (rclone --dry-run failed — aborting, nothing was copied)\n' >&2
    exit 1
  fi

  if ((want_apply)); then
    printf '\n=== APPLY — type %s to run the copy above (anything else aborts) ===\n' "$CLEAN_WORD"
    printf '> '
    local answer=""
    IFS= read -r answer || die "no input on stdin — aborting"
    [[ "$answer" == "$CLEAN_WORD" ]] || die "aborted: expected the word $CLEAN_WORD"
    printf '  $ rclone'
    printf ' %q' "${COPY_ARGS[@]}"
    printf '\n'
    rclone "${COPY_ARGS[@]}"
    printf 'done.\n'
  else
    printf '\n(dry-run: nothing was uploaded. Add --apply and type %s to copy.)\n' "$CLEAN_WORD"
  fi

  printf 'NOTE: rclone reads its own config (~/.config/rclone/rclone.conf is never opened,\n'
  printf '      printed or passed by this script); no delete flag is ever used.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
