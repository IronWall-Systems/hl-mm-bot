#!/usr/bin/env bash
# scripts/supervise_paper_shadow.sh — restart scripts/paper_shadow.py with backoff.
#
# Single supervisor (flock guarded). paper_shadow.py itself also holds a single-instance
# flock, so even a stray extra launch can never produce a duplicate tracker. Backoff is
# 15 s after a long run (>60 s) and doubles up to 300 s on repeated fast failures.
set -u
cd /root/hl-mm-bot || exit 1

exec 9>data/paper_shadow_supervisor.lock
flock -n 9 || { echo "supervise_paper_shadow: supervisor already running"; exit 0; }

backoff=15
log=logs/paper_shadow.out
nk(){ date -u +%FT%TZ; }

while true; do
    start=$(date +%s)
    venv/bin/python scripts/paper_shadow.py --interval 300 >>"$log" 2>&1
    rc=$?
    ran=$(( $(date +%s) - start ))
    echo "[$(nk)] supervisor: paper_shadow exited rc=$rc after ${ran}s" >>"$log"
    if [ "$rc" -eq 0 ] && [ "$ran" -ge 60 ]; then
        backoff=15
    elif [ "$ran" -ge 60 ]; then
        backoff=15
    else
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt 300 ] && backoff=300
    fi
    echo "[$(nk)] supervisor: restart in ${backoff}s" >>"$log"
    sleep "$backoff"
done
