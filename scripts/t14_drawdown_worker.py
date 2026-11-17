#!/usr/bin/env python3
"""T14 item 5b — hourly cumulative-PnL path for drawdown analysis.

HYPE & XMR, hs=16, lat=500, cr=0.0, all 9 days: at every hour boundary snapshot
cash/fees/pos/mid and net = cash + pos*mid - fees. Output data/t14_dd_path.csv.
Resumable per (coin, day).
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from replay_backtest import ReplayEngine, iter_records, load_specs, resolve_range, shard_paths  # noqa: E402

DAYS = ["20261006", "20261007", "20261008", "20261009", "20261010",
        "20261011", "20261012", "20261013", "20261014"]
COINS = ["HYPE", "XMR"]
FIELDS = ["coin", "day", "hour", "t", "cash", "fees", "pos", "mid", "net_cum", "fills"]


def done_keys(path: Path) -> set:
    if not (path.exists() and path.stat().st_size > 0):
        return set()
    with path.open() as fh:
        return {(r["coin"], r["day"]) for r in csv.DictReader(fh)}


def main() -> int:
    csv_path = ROOT / "data/t14_dd_path.csv"
    log_fh = (ROOT / "logs/t14_dd.log").open("a")
    def log(msg):
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
        print(line, flush=True); print(line, file=log_fh, flush=True)

    specs = load_specs(ROOT / "data/universe.json")
    done = done_keys(csv_path)
    total = len(COINS) * len(DAYS)
    log(f"DD START already={len(done)}/{total}")
    t0 = time.time()
    for coin in COINS:
        spec = specs[coin]
        for day in DAYS:
            if (coin, day) in done:
                continue
            start, end = resolve_range(day, day)
            paths = shard_paths(ROOT / "data", coin, start, end)
            if not paths:
                continue
            tr0 = time.time()
            eng = ReplayEngine(spec, half_spread_bps=16.0, latency_ms=500.0,
                               feed_age_ms=400.0, cancel_rate=0.0)
            cash = 0.0
            fees = 0.0
            rows = []
            hour_ms = 3_600_000
            day_int = int(day)
            next_h = day_int * 100  # YYYYMMDDHH
            last_h = -1
            nr = 0
            for rec in iter_records(paths, [], coin):
                t_ms = rec.get("t")
                if isinstance(t_ms, (int, float)):
                    hh = int(t_ms // hour_ms)  # not calendar; use engine exch_ts hour label
                n_before = len(eng.fills)
                eng.on_record(rec)
                nr += 1
                for f in eng.fills[n_before:]:
                    notional = f.px * f.sz
                    cash += -notional if f.side == "buy" else notional
                    fees += notional * 1.5 / 1e4
                # hour boundary by exchange-time floor
                if isinstance(t_ms, (int, float)):
                    t_h = int(t_ms // hour_ms)
                    if last_h == -1:
                        last_h = t_h
                    elif t_h != last_h:
                        mid = eng.last_mid
                        rows.append({
                            "coin": coin, "day": day, "hour": last_h,
                            "t": last_h * hour_ms, "cash": cash, "fees": fees,
                            "pos": eng.pos, "mid": mid,
                            "net_cum": cash + eng.pos * mid - fees,
                            "fills": len(eng.fills),
                        })
                        last_h = t_h
            mid = eng.last_mid
            rows.append({
                "coin": coin, "day": day, "hour": last_h, "t": last_h * hour_ms,
                "cash": cash, "fees": fees, "pos": eng.pos, "mid": mid,
                "net_cum": cash + eng.pos * mid - fees, "fills": len(eng.fills),
            })
            exists = csv_path.exists() and csv_path.stat().st_size > 0
            with csv_path.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                if not exists:
                    w.writeheader()
                w.writerows(rows)
            log(f"DONE {coin} {day} recs={nr:,} snapshots={len(rows)} "
                f"end_net={rows[-1]['net_cum']:+.4f} {time.time()-tr0:.0f}s")
    log(f"DD COMPLETE elapsed={time.time()-t0:.0f}s")
    log_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
