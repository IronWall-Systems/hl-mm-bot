#!/usr/bin/env bash
#
# scripts/vps_cleanup.sh — report / dry-run VPS cleanup for the hl-mm-bot VPS.
#
# DRY-RUN BY DEFAULT. Destructive categories only execute after `--apply` and the typed word
# CLEAN. Every command is printed before it runs, and each category prints a size estimate.
#
# SAFETY (REQ-0034 STAGE 1 / T9b + amendment B, 2026-10-06):
#   * this script never runs `sudo`; journal/apt actions need root — run them yourself if you
#     are not root (rule 7: the agent never runs sudo / never edits system files);
#   * docker is REPORT-ONLY in the report section and `image prune -a` is forbidden (all images
#     are in use); `--docker` prunes containers (already exited), DANGLING images and builder
#     cache only — never volumes, never `-a`, never a running container;
#   * `--vscode-old` prints the full /root/.vscode-server/bin list first, keeps the NEWEST
#     bin/<commit> directory, and REFUSES outright while any vscode-server process is running;
#   * `--caches` runs `npm cache clean --force` + pip cache purge ONLY; /root/.cache
#     subdirectories are always just LISTED for manual review and are never deleted;
#   * `--project-logs` gzips (never deletes) and never touches `.agent/`, `bot.log`,
#     `bot.log.*`, or the LIVE soak files (`soak_bot.log`, `soak_monitor.log/.out`,
#     `soak_pids.txt`); archived soak copies (`soak_bot_t8c.log` …) may be gzipped;
#   * never reads `.env`, `~/.config/rclone/`, any `*.json` directly in `/root`,
#     `/root/.composio`, `/root/.opencode`, or `/root/cleanup.sh` (not ours) — RULES 12;
#   * the agent never runs this script with `--apply` (deletion commands such as
#     `rm -rf -- <old vscode dir>` only ever execute when the human runs them).
#
# NEEDS HUMAN items are printed at the end of every run and are NEVER executed.
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
LOG_DIR="${PROJECT_ROOT}/logs"
DATA_DIR="${PROJECT_ROOT}/data"
VENV_PY="${PROJECT_ROOT}/.venv/bin/python"
JOURNAL_VACUUM="200M"
KEEP_AGE_DAYS=2
CLEAN_WORD="CLEAN"
VSCODE_BIN="/root/.vscode-server/bin"
CACHE_DIR="/root/.cache"
NPM_DIR="/root/.npm"

MODE="plan" # plan (print only) | apply (execute)
DO_REPORT=0
DO_DOCKER=0
DO_JOURNAL=0
DO_APT=0
DO_PIP=0
DO_PLOGS=0
DO_VSCODE_OLD=0
DO_CACHES=0

usage() {
  cat <<'EOF'
usage: scripts/vps_cleanup.sh [--report] [--docker] [--journal] [--apt] [--pip]
                              [--project-logs] [--vscode-old] [--caches]
                              [--apply] [-h|--help]

  --report        read-only report: df -h /, du -xh --max-depth=1 / (tail -15), du of
                  /root/.cache /root/.vscode-server /root/.npm /root/.local, docker system df,
                  container sizes, top 10 /var/lib/docker/containers/*/*-json.log,
                  journalctl --disk-usage, /var/cache/apt, pip cache dir, du /var/log,
                  the 10 largest files in /var/log, /root/.vscode-server/bin commit dirs
                  (newest marked), /root/.cache subdirectories (listing for manual review),
                  du logs/, du data/
                  (this is the DEFAULT when no category flag is given)
  --docker        docker container prune -f | docker image prune -f | docker builder prune -f
                  (dangling only: no volumes, no -a, never a running container;
                  `image prune -a` is forbidden — all images are in use)
  --journal       journalctl --vacuum-size=200M
  --apt           apt-get clean
  --pip           pip cache purge (project venv only)
  --project-logs  gzip files in logs/ older than 2 days (gzip only — never deletes;
                  never .agent/, bot.log, bot.log.*, live soak_* files)
  --vscode-old    delete every /root/.vscode-server/bin/<commit> directory EXCEPT the newest
                  (prints the whole list first; REFUSES if a vscode-server process is running)
  --caches        npm cache clean --force + pip cache purge ONLY; always also LISTs the
                  /root/.cache subdirectories for manual review (never deletes them)
  --apply         actually run the selected categories; asks for the typed word CLEAN
  -h, --help      this text

Without --apply every command is printed and NOT executed (dry-run).
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- pure helpers (unit-tested in tests/test_vps_cleanup_helpers.py) --------------------------
human_size() { # human_size <bytes> -> "12.3 MiB"
  awk -v b="${1:-0}" 'BEGIN {
    split("B KiB MiB GiB TiB PiB", u, " ")
    i = 1; x = b + 0
    while (x >= 1024 && i < 6) { x /= 1024; i++ }
    if (i == 1) printf "%d %s", x, u[i]; else printf "%.1f %s", x, u[i]
  }'
}

is_protected_log() { # never eligible for --project-logs (single source of truth for plogs_list)
  local base="${1##*/}"
  case "$base" in
    bot.log | bot.log.*) return 0 ;; # current run + its rotating backups
    soak_*.log | soak_*.out | soak_*.txt)
      # archived soak copies carry a run suffix (soak_bot_t8c.log) and MAY be gzipped;
      # the live ones (soak_bot.log, soak_monitor.log/.out, soak_pids.txt) never are
      if [[ "$base" == *_t[0-9]* ]]; then return 1; fi
      return 0
      ;;
    *) return 1 ;;
  esac
}

is_clean_word() { # --apply gate
  [[ "${1-}" == "$CLEAN_WORD" ]]
}

which_process_matches() { # which_process_matches <pattern> -> 0 if a live process matches
  # read-only; drops this script's own command line and grep itself so a pattern can never
  # be matched by the shell that is testing it
  ps -eo args= 2>/dev/null |
    grep -v -e 'vps_cleanup.sh' -e 'ps -eo args' -e 'grep' |
    grep -q -- "${1-}"
}

vscode_server_running() { # 0 = a vscode-server process is alive -> --vscode-old must refuse
  which_process_matches 'vscode-server'
}

vscode_bin_dirs() { # vscode_bin_dirs [bin_dir] -> "epoch<TAB>bytes<TAB>path", oldest first
  local bin="${1:-$VSCODE_BIN}" d
  [[ -d "$bin" ]] || return 0
  for d in "$bin"/*/; do
    [[ -d "$d" ]] || continue
    d="${d%/}"
    printf '%s\t%s\t%s\n' \
      "$(stat -c %Y -- "$d" 2>/dev/null || echo 0)" \
      "$(du -sb -- "$d" 2>/dev/null | cut -f1 || echo 0)" \
      "$d"
  done | sort -n
}

vscode_old_dirs() { # every bin/<commit> dir except the newest (empty if there is 0 or 1)
  local bin="${1:-$VSCODE_BIN}" listing n
  listing=$(vscode_bin_dirs "$bin")
  [[ -n "$listing" ]] || return 0
  n=$(printf '%s\n' "$listing" | wc -l)
  ((n > 1)) || return 0
  printf '%s\n' "$listing" | head -n -1 | cut -f3-
}

list_cache_dirs() { # READ-ONLY listing of a cache dir's subdirectories (never deleted here)
  local dir="${1:-$CACHE_DIR}"
  [[ -d "$dir" ]] || return 0
  du -xh --max-depth=1 -- "$dir" 2>/dev/null | sort -h
}
# ---------------------------------------------------------------------------------------------

report_cmd() { # read-only: print the command, then run it, tolerating failure
  printf '  $ %s\n' "$1"
  if ! bash -c "$1"; then printf '    (command failed — continuing)\n'; fi
}

emit() { # print the command; run it ONLY in apply mode
  printf '  $ %s\n' "$1"
  if [[ "$MODE" == "apply" ]]; then
    local rc=0
    bash -c "$1" || rc=$?
    if ((rc != 0)); then printf '    (command failed: exit %d — continuing)\n' "$rc" >&2; fi
  else
    printf '      (dry-run: not executed)\n'
  fi
}

plogs_list() { # eligible files for --project-logs; filtering goes through is_protected_log
  [[ -d "$LOG_DIR" ]] || return 0
  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if is_protected_log "$f"; then continue; fi
    printf '%s\n' "$f"
  done < <(find "$LOG_DIR" -maxdepth 1 -type f -mtime +"${KEEP_AGE_DAYS}" ! -name '*.gz' -print)
}

# --- size estimates (one per category) -------------------------------------------------------
est_docker() {
  printf 'size estimate --docker: '
  if ! command -v docker >/dev/null 2>&1; then
    printf 'docker not installed\n'; return 0
  fi
  local total
  total=$(find /var/lib/docker/containers -name '*-json.log' -printf '%s\n' 2>/dev/null |
    awk '{s += $1} END {print s + 0}')
  printf 'container json logs %s + reclaimable image/builder space below\n' "$(human_size "$total")"
}

est_journal() {
  printf 'size estimate --journal: '
  if ! command -v journalctl >/dev/null 2>&1; then printf 'journalctl not installed\n'; return 0; fi
  journalctl --disk-usage 2>/dev/null || printf '(journalctl --disk-usage failed)\n'
}

est_apt() {
  printf 'size estimate --apt: '
  if [[ ! -d /var/cache/apt ]]; then printf '/var/cache/apt missing\n'; return 0; fi
  du -sh /var/cache/apt 2>/dev/null | awk '{print $1, "in /var/cache/apt (apt-get clean frees this)"}'
}

est_pip() {
  printf 'size estimate --pip: '
  if [[ ! -x "$VENV_PY" ]]; then printf 'project venv python missing (%s)\n' "$VENV_PY"; return 0; fi
  local dir
  dir=$("$VENV_PY" -m pip cache dir 2>/dev/null | tail -1) || dir=""
  if [[ -z "$dir" || ! -d "$dir" ]]; then printf 'pip cache dir unavailable\n'; return 0; fi
  du -sh "$dir" 2>/dev/null | awk -v d="$dir" '{print $1, "in " d}'
}

est_project_logs() {
  printf 'size estimate --project-logs: '
  local n=0 bytes=0 f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    n=$((n + 1))
    bytes=$((bytes + $(stat -c %s -- "$f" 2>/dev/null || echo 0)))
  done < <(plogs_list)
  printf '%d file(s), %s, older than %d day(s) in logs/ (gzip only — .agent/, bot.log/bot.log.* and live soak_* files excluded)\n' \
    "$n" "$(human_size "$bytes")" "$KEEP_AGE_DAYS"
}

# --- command plans (one function per category) -----------------------------------------------
plan_docker() {
  printf 'category --docker (dangling only; never volumes, never -a, never running containers; `image prune -a` is FORBIDDEN — all images are in use):\n'
  est_docker
  emit "docker container prune -f"
  emit "docker image prune -f"
  emit "docker builder prune -f"
}

plan_journal() {
  printf 'category --journal:\n'
  est_journal
  ((EUID == 0)) || printf '  NOTE: needs root — this script never runs sudo, run it yourself if needed\n'
  emit "journalctl --vacuum-size=${JOURNAL_VACUUM}"
}

plan_apt() {
  printf 'category --apt:\n'
  est_apt
  ((EUID == 0)) || printf '  NOTE: needs root — this script never runs sudo, run it yourself if needed\n'
  emit "apt-get clean"
}

plan_pip() {
  printf 'category --pip (project venv only):\n'
  est_pip
  if [[ -x "$VENV_PY" ]]; then
    emit "'${VENV_PY}' -m pip cache purge"
  else
    printf '  (skipped: %s not found)\n' "$VENV_PY"
  fi
}

plan_project_logs() {
  printf 'category --project-logs (gzip only — never deletes, never .agent/, bot.log*, live soak_*):\n'
  est_project_logs
  local f size
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    size=$(stat -c %s -- "$f" 2>/dev/null || echo 0)
    printf '    candidate: %s (%s)\n' "$f" "$(human_size "$size")"
    emit "gzip -f '$f'"
  done < <(plogs_list)
}

est_vscode_old() {
  printf 'size estimate --vscode-old: '
  local old bytes=0 f
  old=$(vscode_old_dirs "$VSCODE_BIN")
  if [[ -z "$old" ]]; then
    printf 'nothing to do (0 or 1 bin/<commit> directory under %s)\n' "$VSCODE_BIN"
    return 0
  fi
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    bytes=$((bytes + $(du -sb -- "$f" 2>/dev/null | cut -f1 || echo 0)))
  done <<< "$old"
  printf '%s across %d old bin/<commit> directory(ies) under %s\n' \
    "$(human_size "$bytes")" "$(printf '%s\n' "$old" | wc -l)" "$VSCODE_BIN"
}

est_caches() {
  printf 'size estimate --caches: '
  local npm_size="npm: (no ${NPM_DIR})" pip_size="pip: (unavailable)"
  if [[ -d "$NPM_DIR" ]]; then
    npm_size="npm $(du -sh "$NPM_DIR" 2>/dev/null | cut -f1)"
  fi
  if [[ -x "$VENV_PY" ]]; then
    local pdir
    pdir=$("$VENV_PY" -m pip cache dir 2>/dev/null | tail -1) || pdir=""
    if [[ -n "$pdir" && -d "$pdir" ]]; then
      pip_size="pip $(du -sh "$pdir" 2>/dev/null | cut -f1)"
    fi
  fi
  printf '%s + %s — both are caches this category may clean; %s/* subdirectories are NEVER deleted by this script\n' \
    "$npm_size" "$pip_size" "$CACHE_DIR"
}

plan_vscode_old() {
  printf 'category --vscode-old (delete every bin/<commit> dir except the newest; list first):\n'
  local listing newest line mtime size path
  listing=$(vscode_bin_dirs "$VSCODE_BIN")
  if [[ -z "$listing" ]]; then
    printf '  (no bin/<commit> directories under %s — nothing to do)\n' "$VSCODE_BIN"
    return 0
  fi
  newest=$(printf '%s\n' "$listing" | tail -1 | cut -f3-)
  printf '  full list (oldest first, * = newest / KEPT):\n'
  while IFS=$'\t' read -r mtime size path; do
    [[ -n "$path" ]] || continue
    if [[ "$path" == "$newest" ]]; then
      printf '    * KEEP  %10s  %s  %s\n' "$(human_size "$size")" \
        "$(date -u -d "@$mtime" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')" "$path"
    else
      printf '    - old   %10s  %s  %s\n' "$(human_size "$size")" \
        "$(date -u -d "@$mtime" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')" "$path"
    fi
  done <<< "$listing"
  if vscode_server_running; then
    printf '  REFUSED: a vscode-server process is running — nothing will be deleted. Stop it and rerun.\n'
    return 0
  fi
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    case "$path" in
      "$VSCODE_BIN"/*) ;; # only ever directly under the bin dir (guard)
      *) printf '    (skipped: outside %s: %s)\n' "$VSCODE_BIN" "$path"; continue ;;
    esac
    emit "rm -rf -- '$path'"
  done <<< "$(vscode_old_dirs "$VSCODE_BIN")"
}

plan_caches() {
  printf 'category --caches (npm cache clean + pip cache purge ONLY; %s/* subdirs are LISTED for manual review, never deleted):\n' "$CACHE_DIR"
  est_caches
  printf '  listing for your manual review (read-only):\n'
  list_cache_dirs | sed 's/^/    /'
  if command -v npm >/dev/null 2>&1; then
    emit "npm cache clean --force"
  else
    printf '  (npm not installed — skipped)\n'
  fi
  if [[ -x "$VENV_PY" ]]; then
    emit "'${VENV_PY}' -m pip cache purge"
  elif command -v pip >/dev/null 2>&1; then
    emit "pip cache purge"
  else
    printf '  (pip not found — skipped)\n'
  fi
}

# --- report ----------------------------------------------------------------------------------
run_report() {
  printf '=== VPS CLEANUP — REPORT (read-only) %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf -- '--- disk ---\n'
  report_cmd "df -h /"
  printf '  $ du -xh --max-depth=1 / | sort -h | tail -15\n'
  du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -15 || printf '    (command failed — continuing)\n'

  printf -- '--- cache / tool dirs (read-only) ---\n'
  printf '  $ du -xh --max-depth=1 /root/.cache /root/.vscode-server /root/.npm /root/.local | sort -h\n'
  du -xh --max-depth=1 /root/.cache /root/.vscode-server /root/.npm /root/.local 2>/dev/null | sort -h ||
    printf '    (command failed — continuing)\n'
  printf '  $ /root/.cache subdirectory listing (for your manual review — never deleted here)\n'
  list_cache_dirs | sed 's/^/    /' || printf '    (listing failed — continuing)\n'

  printf -- '--- vscode-server ---\n'
  if [[ -d "$VSCODE_BIN" ]]; then
    local vlist newest v_mtime v_size v_path v_mark
    vlist=$(vscode_bin_dirs "$VSCODE_BIN")
    if [[ -n "$vlist" ]]; then
      newest=$(printf '%s\n' "$vlist" | tail -1 | cut -f3-)
      printf '  $ ls -1 %s (commit dirs, oldest first; * = newest / would be kept by --vscode-old)\n' "$VSCODE_BIN"
      while IFS=$'\t' read -r v_mtime v_size v_path; do
        [[ -n "$v_path" ]] || continue
        v_mark="  "
        [[ "$v_path" == "$newest" ]] && v_mark="* "
        printf '    %s%10s  %s  %s\n' "$v_mark" "$(human_size "$v_size")" \
          "$(date -u -d "@$v_mtime" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')" "$v_path"
      done <<< "$vlist"
      if vscode_server_running; then
        printf '    NOTE: a vscode-server process is running -> --vscode-old would REFUSE right now.\n'
      else
        printf '    NOTE: no vscode-server process -> --vscode-old would offer the old dirs above.\n'
      fi
    else
      printf '  (no bin/<commit> directories under %s)\n' "$VSCODE_BIN"
    fi
  else
    printf '  %s does not exist — skipped\n' "$VSCODE_BIN"
  fi

  printf -- '--- docker (report-only; `image prune -a` is forbidden — all images are in use) ---\n'
  if command -v docker >/dev/null 2>&1; then
    report_cmd "docker system df"
    report_cmd "docker ps -a --size --format 'table {{.Names}}\t{{.Image}}\t{{.Size}}\t{{.Status}}'"
    printf '  $ top 10 /var/lib/docker/containers/*/*-json.log\n'
    local top
    top=$(find /var/lib/docker/containers -name '*-json.log' -printf '%s\t%p\n' 2>/dev/null |
      sort -rn | head -10 || true)
    if [[ -n "$top" ]]; then
      printf '%s\n' "$top" | awk -F'\t' '{printf "      %10.1f MB  %s\n", $1 / 1048576, $2}'
    else
      printf '      (none readable)\n'
    fi
    printf '      total: '
    find /var/lib/docker/containers -name '*-json.log' -printf '%s\n' 2>/dev/null |
      awk '{s += $1} END {printf "%.1f MB in container json logs\n", s / 1048576}' || true
  else
    printf '  docker not installed — skipped\n'
  fi

  printf -- '--- journal ---\n'
  if command -v journalctl >/dev/null 2>&1; then
    report_cmd "journalctl --disk-usage"
  else
    printf '  journalctl not installed — skipped\n'
  fi

  printf -- '--- apt ---\n'
  report_cmd "du -sh /var/cache/apt"

  printf -- '--- pip (project venv only) ---\n'
  if [[ -x "$VENV_PY" ]]; then
    report_cmd "'${VENV_PY}' -m pip cache dir"
    local pdir
    pdir=$("$VENV_PY" -m pip cache dir 2>/dev/null | tail -1) || pdir=""
    if [[ -n "$pdir" && -d "$pdir" ]]; then
      printf '  $ du -sh %s\n' "$pdir"
      du -sh "$pdir" 2>/dev/null || true
    fi
  else
    printf '  project venv missing — skipped\n'
  fi

  printf -- '--- /var/log ---\n'
  report_cmd "du -sh /var/log"
  printf '  $ 10 largest files in /var/log\n'
  local vlog
  vlog=$(find /var/log -xdev -type f -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -10 || true)
  if [[ -n "$vlog" ]]; then
    printf '%s\n' "$vlog" | awk -F'\t' '{printf "      %10.1f MB  %s\n", $1 / 1048576, $2}'
  else
    printf '      (none readable)\n'
  fi

  printf -- '--- project ---\n'
  report_cmd "du -sh '${LOG_DIR}'"
  if [[ -d "$DATA_DIR" ]]; then
    report_cmd "du -sh '${DATA_DIR}'"
  else
    printf '  $ du -sh %s\n' "$DATA_DIR"
    printf '      (data/ does not exist yet)\n'
  fi

  printf -- '--- size estimate per category (what --apply would reclaim/process) ---\n'
  est_docker
  est_journal
  est_apt
  est_pip
  est_project_logs
  est_vscode_old
  est_caches
}

print_needs_human() {
  printf '=== NEEDS HUMAN — printed only, NEVER run by this script ===\n'
  printf -- '  1) Docker log rotation — edit /etc/docker/daemon.json yourself (system file, rule 7):\n'
  cat <<'EOF'
       { "log-driver": "json-file",
         "log-opts": { "max-size": "50m", "max-file": "3" } }
     NOTE: a `docker restart` (or a daemon restart) restarts the containers — pick your own
     moment; the change alone does not truncate logs that already exist.
EOF
  printf -- '  2) Dangling volumes — for your manual review. Command for you to keep using:\n'
  printf -- '       docker volume ls -f dangling=true\n'
  printf -- '     Current read-only listing (information only — this script never removes a volume):\n'
  if command -v docker >/dev/null 2>&1; then
    local vols=""
    vols=$(docker volume ls -f dangling=true --format '{{.Name}}' 2>/dev/null) || vols=""
    if [[ -n "$vols" ]]; then
      printf '%s\n' "$vols" | sed 's/^/       /'
    else
      printf '       (none)\n'
    fi
  else
    printf '       (docker not installed)\n'
  fi
}

main() {
  local want_apply=0
  while (($#)); do
    case "$1" in
      --report) DO_REPORT=1 ;;
      --docker) DO_DOCKER=1 ;;
      --journal) DO_JOURNAL=1 ;;
      --apt) DO_APT=1 ;;
      --pip) DO_PIP=1 ;;
      --project-logs) DO_PLOGS=1 ;;
      --vscode-old) DO_VSCODE_OLD=1 ;;
      --caches) DO_CACHES=1 ;;
      --apply) want_apply=1 ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
  done

  local any_cat=0
  if ((DO_DOCKER || DO_JOURNAL || DO_APT || DO_PIP || DO_PLOGS || DO_VSCODE_OLD || DO_CACHES)); then
    any_cat=1
  fi

  if ((DO_REPORT)) || ((any_cat == 0)); then run_report; fi

  if ((any_cat)); then
    printf '\n=== CATEGORY PLANS (size estimate + every command) ===\n'
    if ((DO_DOCKER)); then plan_docker; fi
    if ((DO_JOURNAL)); then plan_journal; fi
    if ((DO_APT)); then plan_apt; fi
    if ((DO_PIP)); then plan_pip; fi
    if ((DO_PLOGS)); then plan_project_logs; fi
    if ((DO_VSCODE_OLD)); then plan_vscode_old; fi
    if ((DO_CACHES)); then plan_caches; fi
  fi

  if ((want_apply)); then
    ((any_cat)) || die "--apply needs at least one category flag (--docker/--journal/--apt/--pip/--project-logs/--vscode-old/--caches)"
    printf '\n=== APPLY — type %s to run the commands above (anything else aborts) ===\n' "$CLEAN_WORD"
    printf '> '
    local answer=""
    IFS= read -r answer || die "no input on stdin — aborting"
    is_clean_word "$answer" || die "aborted: expected the word $CLEAN_WORD"
    MODE="apply"
    printf 'confirmed — executing.\n'
    if ((DO_DOCKER)); then plan_docker; fi
    if ((DO_JOURNAL)); then plan_journal; fi
    if ((DO_APT)); then plan_apt; fi
    if ((DO_PIP)); then plan_pip; fi
    if ((DO_PLOGS)); then plan_project_logs; fi
    if ((DO_VSCODE_OLD)); then plan_vscode_old; fi
    if ((DO_CACHES)); then plan_caches; fi
    printf 'done. Re-run with --report to see the new numbers.\n'
  else
    printf '\n(dry-run: nothing was executed. Add --apply and type %s to execute.)\n' "$CLEAN_WORD"
  fi

  print_needs_human
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
