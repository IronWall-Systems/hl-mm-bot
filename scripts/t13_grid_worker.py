#!/usr/bin/env python3
"""T13 STEP 2 grid driver — per-day fresh-engine runs, resumable.

Usage: t13_grid_worker.py TAG COIN [COIN...]
Rows append to data/t13_daily_{TAG}.csv; progress to logs/t13_{TAG}.log.
Re-running skips keys already present in the CSV (crash/session-death safe).
"""
from __future__ import annotations

import csv
import sys
import time
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from replay_backtest import (  # noqa: E402
    ReplayEngine,
    iter_records,
    load_specs,
    resolve_range,
    shard_paths,
    write_csv,
)

DAYS = ["20261006", "20261007", "20261008", "20261009", "20261010",
        "20261011", "20261012", "20261013", "20261014"]
LATS = [500.0, 1000.0]
HS = [8.0, 12.0, 16.0]
CRS = [0.0, 0.5]


def log(fh, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
    print(line, flush=True)
    print(line, file=fh, flush=True)


def done_keys(csv_path: Path) -> set[tuple]:
    if not (csv_path.exists() and csv_path.stat().st_size > 0):
        return set()
    with csv_path.open() as fh:
        return {
            (r["coin"], float(r["latency_ms"]), float(r["half_spread_bps"]),
             float(r["cancel_rate"]), r["day"])
            for r in csv.DictReader(fh)
        }


def main() -> int:
    tag, coins = sys.argv[1], sys.argv[2:]
    csv_path = ROOT / f"data/t13_daily_{tag}.csv"
    log_path = ROOT / f"logs/t13_{tag}.log"
    specs = load_specs(ROOT / "data/universe.json")
    done = done_keys(csv_path)
    total = len(coins) * len(LATS) * len(HS) * len(CRS) * len(DAYS)
    done_count = len(done)
    log_fh = log_path.open("a")
    log(log_fh, f"WORKER {tag} START coins={coins} done={done_count}/{total}")
    t_start = time.time()
    for coin in coins:
        spec = specs[coin]
        for lat in LATS:
            for hs in HS:
                for cr in CRS:
                    for day in DAYS:
                        key = (coin, lat, hs, cr, day)
                        if key in done:
                            continue
                        start, end = resolve_range(day, day)
                        paths = shard_paths(ROOT / "data", coin, start, end)
                        if not paths:
                            log(log_fh, f"SKIP {coin} lat={lat:g} hs={hs:g} cr={cr:g} "
                                        f"{day} no shards")
                            continue
                        t0 = time.time()
                        try:
                            eng = ReplayEngine(
                                spec,
                                half_spread_bps=hs,
                                latency_ms=lat,
                                feed_age_ms=400.0,
                                cancel_rate=cr,
                            )
                            n = 0
                            for rec in iter_records(paths, [], coin):
                                eng.on_record(rec)
                                n += 1
                            m = eng.compute_metrics()
                        except Exception as exc:  # noqa: BLE001 — keep the grid alive
                            log(log_fh, f"ERROR {coin} lat={lat:g} hs={hs:g} cr={cr:g} "
                                        f"{day} {type(exc).__name__}: {exc}")
                            continue
                        write_csv(
                            csv_path,
                            [m],
                            Namespace(latency_ms=lat, half_spread_bps=hs, skew_bps=6.0,
                                      cancel_rate=cr, feed_age_ms=400.0, start=day),
                        )
                        done_count += 1
                        dt = time.time() - t0
                        log(log_fh,
                            f"DONE {coin} lat={lat:g} hs={hs:g} cr={cr:g} {day} "
                            f"recs={n:,} {dt:.1f}s fills={m['fills']} "
                            f"rt={m['round_trip_spread_pnl']:+.4f} "
                            f"mtm={m['inventory_mtm_pnl']:+.4f} "
                            f"net={m['net_pnl_usd']:+.4f} "
                            f"[{done_count}/{total} {time.time()-t_start:.0f}s]")
    log(log_fh, f"WORKER {tag} COMPLETE rows={done_count}/{total} "
                f"elapsed={time.time()-t_start:.0f}s")
    log_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
