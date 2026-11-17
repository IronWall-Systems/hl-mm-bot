#!/usr/bin/env bash
# scripts/run_t16_final.sh — strict T16 PREREG evaluation for HYPE:16 (C3).
#
# Intended to run once at/after 2026-11-03T00:00Z, when the 14th full UTC day
# (2026-11-02) has closed.  It is resumable: re-running skips finished rows.
#
#   cd /root/hl-mm-bot && bash scripts/run_t16_final.sh
#
# Uses only local data: local v2 shards (Oct 28..Nov 2) + staged tars
# (Oct 20..27 in data/archive_tars).  The hung rclone mount is never touched.
set -uo pipefail

cd /root/hl-mm-bot
source venv/bin/activate
ulimit -v 600000 || true   # cap our VM so a runaway replay can never OOM the collector
export HL_MM_ARCHIVE_DIR=/root/hl-mm-bot/data/archive_tars
export PYTHONUNBUFFERED=1
LOG=logs/t16_final.log
mkdir -p logs data/archive_tars

exec 9>/tmp/t16_final.lock
if ! flock -n 9; then
  echo "[$(date -u +%FT%TZ)] already running; exit" >> "$LOG"
  exit 0
fi

echo "[$(date -u +%FT%TZ)] T16 final run start" >> "$LOG"

# If launched early, wait for 00:05Z on the day after --end-day.
python3 - <<'PY' >> "$LOG" 2>&1
import datetime as dt, time
target = dt.datetime(2026, 11, 3, 0, 5, tzinfo=dt.timezone.utc)
now = dt.datetime.now(dt.timezone.utc)
wait = (target - now).total_seconds()
if wait > 0:
    print(f"waiting {wait:.0f}s until {target.isoformat()}")
    time.sleep(wait)
PY

END_DAY=${END_DAY:-20261102}
START_DAY=${START_DAY:-20261020}

# Wait (up to 40 min) for the final day's hour-23 HYPE shard to close.
for _ in $(seq 1 40); do
  n=$(find "data/$END_DAY" -maxdepth 1 -name '*_HYPE.jsonl.gz' 2>/dev/null | wc -l)
  [ "$n" -ge 24 ] && break
  echo "[$(date -u +%FT%TZ)] waiting for $END_DAY (HYPE shards=$n/24)" >> "$LOG"
  sleep 60
done
n=$(find "data/$END_DAY" -maxdepth 1 -name '*_HYPE.jsonl.gz' 2>/dev/null | wc -l)
if [ "$n" -lt 24 ]; then
  echo "[$(date -u +%FT%TZ)] ERROR: $END_DAY incomplete ($n/24 HYPE shards); aborting" >> "$LOG"
  exit 3
fi

# Snapshot the last day locally so a 06:00 auto-archive cannot remove it mid-run.
python3 - "$END_DAY" <<'PY' >> "$LOG" 2>&1
import sys, tarfile
from pathlib import Path
day = sys.argv[1]
src = Path("data") / day
dst = Path("data/archive_tars") / f"data_{day}.tar"
if not src.is_dir():
    print(f"WARN: {src} missing; not snapshotting")
    raise SystemExit(0)
hours = sorted({p.name[:2] for p in src.glob("*.jsonl.gz") if p.name[:2].isdigit()})
print(f"snapshot {src} -> {dst}: {len(hours)} hours {hours[0] if hours else '-'}..{hours[-1] if hours else '-'}")
with tarfile.open(dst, "w") as tar:
    for p in sorted(src.glob("*.jsonl.gz")):
        tar.add(p, arcname=f"{day}/{p.name}")
PY

# Strict run: refuses to write unless all 14 full UTC days are present.
nice -n 19 python scripts/t16_evaluate.py \
  --candidate C3 --start-day "$START_DAY" --end-day "$END_DAY" \
  --require-days 14 --archive-dir data/archive_tars >> "$LOG" 2>&1
rc=$?
echo "[$(date -u +%FT%TZ)] evaluator rc=$rc" >> "$LOG"

nice -n 19 python scripts/daily_scorecard.py \
  --coin HYPE --hs 16 --require-days 14 >> "$LOG" 2>&1
echo "[$(date -u +%FT%TZ)] scorecard rc=$?" >> "$LOG"
echo "[$(date -u +%FT%TZ)] T16 final run done" >> "$LOG"
