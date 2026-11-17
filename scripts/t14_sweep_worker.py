#!/usr/bin/env python3
"""T14 item 4 — fine latency/cancel sweep (new configs only; T13 rows are reused).

Cells: (HYPE,12),(HYPE,16),(XMR,16) x lat {250,500,750,1000,1500} x cr {0,0.25,0.5,0.75},
minus the 4 configs already in the T13 grid (lat 500/1000 x cr 0/0.5). Daily fresh-engine
runs; resumable via data/t14_sweep.csv.
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
    ReplayEngine, iter_records, load_specs, resolve_range, shard_paths, write_csv,
)

DAYS = ["20261006", "20261007", "20261008", "20261009", "20261010",
        "20261011", "20261012", "20261013", "20261014"]
CELLS = [("HYPE", 12.0), ("HYPE", 16.0), ("XMR", 16.0)]
LATS = [250.0, 500.0, 750.0, 1000.0, 1500.0]
CRS = [0.0, 0.25, 0.5, 0.75]
HAVE = {(500.0, 0.0), (500.0, 0.5), (1000.0, 0.0), (1000.0, 0.5)}


def done_keys(path: Path) -> set:
    if not (path.exists() and path.stat().st_size > 0):
        return set()
    with path.open() as fh:
        return {(r["coin"], float(r["latency_ms"]), float(r["half_spread_bps"]),
                 float(r["cancel_rate"]), r["day"]) for r in csv.DictReader(fh)}


def main() -> int:
    csv_path = ROOT / "data/t14_sweep.csv"
    log_fh = (ROOT / "logs/t14_sweep.log").open("a")
    def log(msg):
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
        print(line, flush=True); print(line, file=log_fh, flush=True)

    specs = load_specs(ROOT / "data/universe.json")
    done = done_keys(csv_path)
    configs = [(c, hs, lat, cr) for c, hs in CELLS for lat in LATS for cr in CRS
               if (lat, cr) not in HAVE]
    total = len(configs) * len(DAYS)
    log(f"SWEEP START configs={len(configs)} total_runs={total} already={len(done)}")
    n_done = len(done)
    t0 = time.time()
    for coin, hs, lat, cr in configs:
        spec = specs[coin]
        for day in DAYS:
            key = (coin, lat, hs, cr, day)
            if key in done:
                continue
            start, end = resolve_range(day, day)
            paths = shard_paths(ROOT / "data", coin, start, end)
            if not paths:
                continue
            tr0 = time.time()
            try:
                eng = ReplayEngine(spec, half_spread_bps=hs, latency_ms=lat,
                                   feed_age_ms=400.0, cancel_rate=cr)
                nr = 0
                for rec in iter_records(paths, [], coin):
                    eng.on_record(rec); nr += 1
                m = eng.compute_metrics()
            except Exception as exc:  # noqa: BLE001
                log(f"ERROR {coin} hs={hs:g} lat={lat:g} cr={cr:g} {day} {type(exc).__name__}: {exc}")
                continue
            write_csv(csv_path, [m], Namespace(latency_ms=lat, half_spread_bps=hs,
                      skew_bps=6.0, cancel_rate=cr, feed_age_ms=400.0, start=day))
            n_done += 1
            log(f"DONE {coin} hs={hs:g} lat={lat:g} cr={cr:g} {day} recs={nr:,} "
                f"{time.time()-tr0:.1f}s rt={m['round_trip_spread_pnl']:+.4f} "
                f"net={m['net_pnl_usd']:+.4f} fills={m['fills']} [{n_done}/{total}]")
    log(f"SWEEP COMPLETE {n_done}/{total} elapsed={time.time()-t0:.0f}s")
    log_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
