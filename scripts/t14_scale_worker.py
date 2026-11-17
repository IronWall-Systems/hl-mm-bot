#!/usr/bin/env python3
"""T14 item 5c — quote-size scaling: HYPE hs16 & XMR hs16, lat 500, cr {0, 0.5},
quote_notional {15,30,60,150} (max_notional = 4x, min_order_notional = 10 fixed).
Daily runs, resumable via data/t14_scale.csv (extra quote_notional column).
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from replay_backtest import (  # noqa: E402
    CSV_KEYS, ReplayEngine, iter_records, load_specs, resolve_range, shard_paths,
)

DAYS = ["20261006", "20261007", "20261008", "20261009", "20261010",
        "20261011", "20261012", "20261013", "20261014"]
COINS = ["HYPE", "XMR"]
QS = [15.0, 30.0, 60.0, 150.0]
CRS = [0.0, 0.5]
FIELDS = ["quote_notional", "latency_ms", "half_spread_bps", "skew_bps", "cancel_rate",
          "day", "feed_age_ms"] + CSV_KEYS


def done_keys(path: Path) -> set:
    if not (path.exists() and path.stat().st_size > 0):
        return set()
    with path.open() as fh:
        return {(r["coin"], float(r["quote_notional"]), float(r["cancel_rate"]), r["day"])
                for r in csv.DictReader(fh)}


def main() -> int:
    csv_path = ROOT / "data/t14_scale.csv"
    log_fh = (ROOT / "logs/t14_scale.log").open("a")
    def log(msg):
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
        print(line, flush=True); print(line, file=log_fh, flush=True)

    specs = load_specs(ROOT / "data/universe.json")
    done = done_keys(csv_path)
    configs = [(c, q, cr) for c in COINS for q in QS for cr in CRS]
    total = len(configs) * len(DAYS)
    log(f"SCALE START configs={len(configs)} total_runs={total} already={len(done)}")
    n_done = len(done)
    t0 = time.time()
    for coin, q, cr in configs:
        spec = specs[coin]
        for day in DAYS:
            key = (coin, q, cr, day)
            if key in done:
                continue
            start, end = resolve_range(day, day)
            paths = shard_paths(ROOT / "data", coin, start, end)
            if not paths:
                continue
            tr0 = time.time()
            try:
                eng = ReplayEngine(spec, half_spread_bps=16.0, latency_ms=500.0,
                                   feed_age_ms=400.0, cancel_rate=cr,
                                   quote_notional=q, max_notional=4.0 * q,
                                   min_order_notional=10.0)
                nr = 0
                for rec in iter_records(paths, [], coin):
                    eng.on_record(rec); nr += 1
                m = eng.compute_metrics()
            except Exception as exc:  # noqa: BLE001
                log(f"ERROR {coin} q={q:g} cr={cr:g} {day} {type(exc).__name__}: {exc}")
                continue
            exists = csv_path.exists() and csv_path.stat().st_size > 0
            with csv_path.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                if not exists:
                    w.writeheader()
                row = {"quote_notional": q, "latency_ms": 500.0, "half_spread_bps": 16.0,
                       "skew_bps": 6.0, "cancel_rate": cr, "day": day, "feed_age_ms": 400.0}
                row.update({k: m.get(k) for k in CSV_KEYS})
                w.writerow(row)
            n_done += 1
            log(f"DONE {coin} q={q:g} cr={cr:g} {day} {time.time()-tr0:.1f}s "
                f"fills={m['fills']} rt={m['round_trip_spread_pnl']:+.4f} "
                f"net={m['net_pnl_usd']:+.4f} maxinv={m['max_abs_inventory_usd']:.1f} "
                f"[{n_done}/{total}]")
    log(f"SCALE COMPLETE {n_done}/{total} elapsed={time.time()-t0:.0f}s")
    log_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
