#!/usr/bin/env python3
"""scripts/paper_catchup.py — memory-safe incremental catch-up for the paper shadow.

The live tracker (`paper_shadow.py`) keeps its engine only in memory, so after a stop it
cannot continue exactly.  Its last persisted state (2026-10-31T05:31:28Z, 77 hours =
Oct 28 00:00Z..Oct 31 04:00Z local-only replay) is the baseline.

This script processes only the *new* closed local shards (default from 2026-10-31 05:00Z)
one candidate at a time, one shard at a time, trimming `ReplayEngine.feed_ages_ms` after
every shard (that diagnostic list is the only unbounded buffer and it does not feed any
PnL/decision).  New metrics are summed onto the stored cumulative and a ledger row is
appended.  This is a documented approximation of a single continuous engine: inventory is
not carried across the resume boundary.  The shadow is monitoring only (PREREG §5); the
confirmatory result is the offline `t16_evaluate.py` run.

    nice -n 19 python scripts/paper_catchup.py --from 2026103105
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from paper_shadow import (  # noqa: E402
    CANDIDATES,
    DATA_DIR,
    LATENCY_MS,
    CANCEL_RATE,
    LEDGER_COLUMNS,
    LOCK_FILE,
    STATE_FILE,
    SUMMARY_KEYS,
    acquire_lock,
    is_v2_shard,
    now_utc,
)
from replay_backtest import (  # noqa: E402
    ReplayEngine,
    hour_range,
    iter_records,
    load_specs,
    parse_hour_key,
)

UNIVERSE = DATA_DIR / "universe.json"
ADDITIVE = [k for k in SUMMARY_KEYS
            if k not in ("ending_inventory_usd", "max_abs_inventory_usd")]


def latest_closed_hour(now: float | None = None) -> int:
    dt = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    dt = dt.replace(minute=0, second=0, microsecond=0)
    prev = dt.timestamp() - 3600
    d = datetime.fromtimestamp(prev, tz=timezone.utc)
    return int(d.strftime("%Y%m%d%H"))


def merge_cumulative(old: dict, delta: dict) -> dict:
    out = dict(old)
    for k in ADDITIVE:
        out[k] = float(old.get(k, 0.0)) + float(delta.get(k, 0.0))
    out["max_abs_inventory_usd"] = max(float(old.get("max_abs_inventory_usd", 0.0)),
                                       float(delta.get("max_abs_inventory_usd", 0.0)))
    out["ending_inventory_usd"] = float(delta.get("ending_inventory_usd", 0.0))
    return out


def process(coin: str, hs: int, spec, start: int, end: int, data_dir: Path):
    eng = ReplayEngine(spec, half_spread_bps=float(hs), latency_ms=LATENCY_MS,
                       cancel_rate=CANCEL_RATE)
    processed = skipped = 0
    day_deltas: dict[str, dict] = {}
    prev = {k: 0.0 for k in SUMMARY_KEYS}
    cur_day = None
    day_base = {k: 0.0 for k in SUMMARY_KEYS}
    peak = 0.0
    worst_dd = 0.0
    for day, hh in hour_range(start, end):
        if day != cur_day:
            snap = eng.compute_metrics()
            cur_day = day
            day_base = {k: snap.get(k, 0.0) for k in SUMMARY_KEYS}
        path = data_dir / day / f"{hh}_{coin}.jsonl.gz"
        if not path.is_file() or not is_v2_shard(path):
            skipped += 1
            continue
        try:
            for rec in iter_records([path], [], coin):
                eng.on_record(rec)
        except Exception as exc:  # truncated shard: leave it for next cycle
            print(f"  WARN {coin}:{hs} {day}/{hh} {type(exc).__name__}: {exc}", flush=True)
            skipped += 1
            continue
        eng.feed_ages_ms.clear()
        processed += 1
        snap = eng.compute_metrics()
        cur = {k: snap.get(k, 0.0) for k in SUMMARY_KEYS}
        d = {k: cur[k] - day_base.get(k, 0.0) for k in SUMMARY_KEYS}
        day_deltas[day] = d
        net = cur["net_pnl_usd"]
        peak = max(peak, net)
        worst_dd = max(worst_dd, peak - net)
        gc.collect()
    final = eng.compute_metrics()
    return ({k: final.get(k, 0.0) for k in SUMMARY_KEYS}, processed, skipped,
            day_deltas, worst_dd)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="start", default="2026103105")
    p.add_argument("--to", dest="end", default=None,
                   help="YYYYMMDDHH (default: latest closed UTC hour)")
    p.add_argument("--candidate", default=None, help="e.g. HYPE:16 (default: all)")
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    p.add_argument("--state-file", type=Path, default=STATE_FILE)
    p.add_argument("--ledger-file", type=Path, default=DATA_DIR / "paper_live_ledger.csv")
    p.add_argument("--universe", type=Path, default=UNIVERSE)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true", help="accepted for symmetry")
    args = p.parse_args(argv)

    lock = None
    if not args.dry_run:
        lock = acquire_lock(LOCK_FILE)
        if lock is None:
            print(f"another tracker holds {LOCK_FILE}; exiting")
            return 0

    start = parse_hour_key(args.start)
    end = parse_hour_key(args.end) if args.end else latest_closed_hour()
    if end < start:
        print(f"--to {args.end} is not after --from {args.start}")
        return 2
    specs = load_specs(args.universe)
    wanted = None
    if args.candidate:
        wanted = {x.strip().upper() for x in args.candidate.split(",") if x.strip()}

    state = json.loads(args.state_file.read_text()) if args.state_file.exists() else {}
    state.setdefault("candidates", {})
    state["updated_utc"] = now_utc()
    state["latency_ms"] = LATENCY_MS
    state["cancel_rate"] = CANCEL_RATE

    ledger_rows = []
    for coin, hs in CANDIDATES:
        key = f"{coin}:{hs}"
        if wanted and key.upper() not in wanted:
            continue
        if coin not in specs:
            print(f"skip {key}: not in universe")
            continue
        print(f"[{key}] {start}..{end} ...", flush=True)
        t0 = time.time()
        delta, processed, skipped, day_deltas, worst_dd = process(
            coin, hs, specs[coin], start, end, args.data_dir)
        entry = state["candidates"].setdefault(key, {"coin": coin, "hs": hs,
                                                     "cumulative": {k: 0.0 for k in SUMMARY_KEYS}})
        old = entry.get("cumulative", {})
        cum = merge_cumulative(old, delta)
        entry["cumulative"] = cum
        entry["open_inventory_usd"] = cum["ending_inventory_usd"]
        entry["processed_hours"] = int(entry.get("processed_hours", 0)) + processed
        entry["worst_drawdown_usd"] = max(float(entry.get("worst_drawdown_usd", 0.0)),
                                          float(worst_dd))
        entry["latency_ms"] = LATENCY_MS
        entry["cancel_rate"] = CANCEL_RATE
        for day, d in sorted(day_deltas.items()):
            entry.setdefault("catchup_daily", {})[day] = d
        state["today"] = max(day_deltas) if day_deltas else state.get("today")
        print(f"  processed={processed} skipped={skipped} "
              f"delta_net={delta['net_pnl_usd']:+.4f} "
              f"cum_net={cum['net_pnl_usd']:+.4f} {time.time() - t0:.0f}s", flush=True)
        ledger_rows.append({
            "ts_utc": now_utc(), "coin": coin, "hs": hs,
            "day": state.get("today") or "", "hour": "",
            "fills": cum["fills"], "round_trips": cum["round_trip_count"],
            "round_trip_spread_pnl": cum["round_trip_spread_pnl"],
            "inventory_mtm_pnl": cum["inventory_mtm_pnl"],
            "funding_pnl": cum["funding_pnl"],
            "fees_usd": cum["total_fees_usd"],
            "net_pnl_usd": cum["net_pnl_usd"],
            "open_inventory_usd": cum["ending_inventory_usd"],
            "worst_drawdown_usd": entry["worst_drawdown_usd"],
        })

    state["cursor_hour"] = f"{end:010d}" if end is not None else None
    state["updated_utc"] = now_utc()
    if args.dry_run:
        print(json.dumps(state, indent=2, sort_keys=True)[:2000])
        return 0

    tmp = args.state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(args.state_file)
    if ledger_rows:
        exists = args.ledger_file.exists() and args.ledger_file.stat().st_size > 0
        with args.ledger_file.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
            if not exists:
                w.writeheader()
            w.writerows(ledger_rows)
    print(f"persisted state={args.state_file} ledger_rows={len(ledger_rows)}", flush=True)
    if lock is not None:
        try:
            lock.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
