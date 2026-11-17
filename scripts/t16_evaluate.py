#!/usr/bin/env python3
"""scripts/t16_evaluate.py — T16 offline PREREG evaluator (resumable, low-memory).

Implements `.agent/PREREG.md` §4 exactly:

  for each candidate (coin, hs), cell (latency, cancel_rate), and full UTC day:
      fresh ReplayEngine -> feed that day's v2 shards -> per-day net PnL
      (after maker fees AND funding), plus four contiguous 6-hour block PnLs.

Results are appended to CSV so a run can be interrupted (OOM, reboot, timeout) and
resumed without recomputing finished (candidate, cell, day) rows.

Memory: one engine at a time.  `ReplayEngine.feed_ages_ms` grows once per record and
is used only for the p50/p90 *diagnostic*; it is cleared periodically here.  Net PnL,
funding, fees and fills never read it, so this cannot change the PREREG numbers.

Usage (staged tars must be local — the rclone mount is not used):
    HL_MM_ARCHIVE_DIR=data/archive_tars \
        nice -n 19 python scripts/t16_evaluate.py --candidate C3
"""

from __future__ import annotations

import argparse
import csv
import gc
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from replay_backtest import (  # noqa: E402
    ReplayEngine,
    gather_sources,
    hour_range,
    iter_records,
    load_specs,
    parse_hour_key,
)

CANDIDATES = [
    ("C1", "XMR", 16),
    ("C2", "HYPE", 12),
    ("C3", "HYPE", 16),
    ("C4", "kBONK", 8),
    ("C5", "kBONK", 12),
]
CELLS = [(500.0, 0.0), (500.0, 0.5), (1000.0, 0.0), (1000.0, 0.5)]
PRIMARY = (500.0, 0.5)
BLOCK_HOURS = 6
BLOCKS_PER_DAY = 24 // BLOCK_HOURS
TRIM_EVERY = 50_000
UNIVERSE = PROJECT_ROOT / "data" / "universe.json"
DATA_DIR = PROJECT_ROOT / "data"
EVAL_CSV = DATA_DIR / "t16_eval.csv"
BLOCKS_CSV = DATA_DIR / "t16_blocks.csv"

EVAL_COLUMNS = [
    "candidate", "coin", "hs", "latency_ms", "cancel_rate", "day",
    "net_pnl_usd", "funding_pnl", "maker_fees_usd", "total_fees_usd",
    "fills", "round_trip_count", "traded_volume_usd",
    "ending_inventory_usd", "max_abs_inventory_usd",
    "records", "elapsed_s",
]
BLOCK_COLUMNS = [
    "candidate", "coin", "hs", "latency_ms", "cancel_rate", "day", "block",
    "net_pnl_usd",
]


def day_start_ts(day: str) -> float:
    y, m, d = int(day[:4]), int(day[4:6]), int(day[6:])
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


def cell_key(latency: float, cancel_rate: float) -> str:
    return f"{latency:.1f}|{cancel_rate:.1f}"


def row_key(row: dict) -> tuple[str, str, str]:
    return (row["candidate"], cell_key(float(row["latency_ms"]), float(row["cancel_rate"])),
            row["day"])


def load_done(path: Path) -> set[tuple[str, str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open(newline="") as fh:
        return {row_key(r) for r in csv.DictReader(fh)}


def full_days(
    coin: str,
    start_day: str,
    end_day: str,
    archive_dir: Path | None,
    data_dir: Path = DATA_DIR,
) -> list[str]:
    """UTC days in [start_day, end_day] with all 24 hours present for `coin`."""
    start = parse_hour_key(start_day)
    end = parse_hour_key(end_day) + 23
    paths, tars = gather_sources(data_dir, coin, start, end, archive_dir=archive_dir)
    covered: set[tuple[str, str]] = set()
    for p in paths:
        covered.add((p.parent.name, p.name[:2]))
    for _tar, member in tars:
        day, name = member.split("/", 1)
        covered.add((day, name[:2]))
    days = []
    for day in sorted({d for d, _ in hour_range(start, end)}):
        if all((day, f"{h:02d}") in covered for h in range(24)):
            days.append(day)
    return days


class BlockAccumulator:
    """Split a day's running net into contiguous `BLOCK_HOURS` block deltas."""

    def __init__(self, day0: float, block_hours: int = BLOCK_HOURS,
                 count: int = BLOCKS_PER_DAY) -> None:
        self.edges = [day0 + k * block_hours * 3600 for k in range(1, count + 1)]
        self.values: list[float] = []
        self.prev = 0.0
        self._edge_i = 0

    def mark(self, t: float, net: float) -> None:
        """Snapshot all block boundaries crossed by a record timestamped `t`."""
        while self._edge_i < len(self.edges) and t >= self.edges[self._edge_i]:
            self.values.append(net - self.prev)
            self.prev = net
            self._edge_i += 1

    def finish(self, net: float) -> list[float]:
        while self._edge_i < len(self.edges):
            self.values.append(net - self.prev)
            self.prev = net
            self._edge_i += 1
        return self.values


def running_net(eng: ReplayEngine) -> float:
    """Net PnL so far, identical to compute_metrics()['net_pnl_usd'] before markout settle."""
    realized = 0.0
    fees = 0.0
    for f in eng.fills:
        realized += f.px * f.sz if f.side == "sell" else -f.px * f.sz
        fees += f.px * f.sz * eng.maker_fee_bps / 1e4
    return realized + eng.pos * eng.last_mid + eng.funding_pnl - fees


def run_day(
    coin: str,
    spec,
    hs: int,
    day: str,
    latency: float,
    cancel_rate: float,
    archive_dir: Path | None,
    sources: dict | None = None,
) -> tuple[dict, list[float]]:
    key = (coin, day)
    if sources is not None and key in sources:
        paths, tars = sources[key]
    else:
        start = parse_hour_key(day)
        paths, tars = gather_sources(
            DATA_DIR, coin, start, start + 23, archive_dir=archive_dir
        )
        if sources is not None:
            sources[key] = (paths, tars)

    eng = ReplayEngine(
        spec,
        half_spread_bps=float(hs),
        latency_ms=latency,
        cancel_rate=cancel_rate,
    )
    acc = BlockAccumulator(day_start_ts(day))
    n = 0
    t0 = time.time()
    for rec in iter_records(list(paths), list(tars), coin):
        t_ms = rec.get("t")
        if isinstance(t_ms, (int, float)):
            acc.mark(t_ms / 1000.0, running_net(eng))
        eng.on_record(rec)
        n += 1
        if n % TRIM_EVERY == 0:
            eng.feed_ages_ms.clear()
    blocks = acc.finish(running_net(eng))
    eng.feed_ages_ms.clear()
    m = eng.compute_metrics()
    if abs(sum(blocks) - m["net_pnl_usd"]) > 1e-6:
        print(f"WARN {coin}:{hs} {day} block sum {sum(blocks):.9f} != "
              f"net {m['net_pnl_usd']:.9f}", file=sys.stderr)

    row = {
        "candidate": "",
        "coin": coin,
        "hs": hs,
        "latency_ms": latency,
        "cancel_rate": cancel_rate,
        "day": day,
        "net_pnl_usd": m["net_pnl_usd"],
        "funding_pnl": m["funding_pnl"],
        "maker_fees_usd": m["maker_fees_usd"],
        "total_fees_usd": m["total_fees_usd"],
        "fills": m["fills"],
        "round_trip_count": m["round_trip_count"],
        "traded_volume_usd": m["traded_volume_usd"],
        "ending_inventory_usd": m["ending_inventory_usd"],
        "max_abs_inventory_usd": m["max_abs_inventory_usd"],
        "records": n,
        "elapsed_s": round(time.time() - t0, 3),
    }
    # The four block deltas must sum to the day net (invariant checked by tests).
    return row, blocks


def append_row(path: Path, columns: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        if new:
            w.writeheader()
        w.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())


def select_candidates(arg: str | None) -> list[tuple[str, str, int]]:
    if not arg:
        return CANDIDATES
    wanted = {a.strip().upper() for a in arg.split(",") if a.strip()}
    out = []
    for cid, coin, hs in CANDIDATES:
        if cid.upper() in wanted or f"{coin.upper()}:{hs}" in wanted:
            out.append((cid, coin, hs))
    if not out:
        raise SystemExit(f"no candidate matched {arg!r}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidate", default=None,
                   help="C1..C5 or COIN:hs, comma-separated (default: all)")
    p.add_argument("--latency", type=float, action="append", default=None)
    p.add_argument("--cancel-rate", type=float, action="append", default=None)
    p.add_argument("--start-day", default="20261020", help="YYYYMMDD (first full candidate day)")
    p.add_argument("--end-day", default=None,
                   help="YYYYMMDD inclusive (default: yesterday UTC)")
    p.add_argument("--archive-dir", default=os.environ.get(
        "HL_MM_ARCHIVE_DIR", str(DATA_DIR / "archive_tars")))
    p.add_argument("--eval-csv", default=str(EVAL_CSV))
    p.add_argument("--blocks-csv", default=str(BLOCKS_CSV))
    p.add_argument("--require-days", type=int, default=14,
                   help="refuse to write if fewer full days exist (default 14)")
    p.add_argument("--allow-provisional", action="store_true",
                   help="write even if < require-days full days (results NOT PREREG-valid)")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    if args.end_day is not None:
        end_day = args.end_day
    else:
        # A UTC day is complete only once its next 00:00 boundary has passed.
        end_day = (now.date() - timedelta(days=1)).strftime("%Y%m%d")

    archive_dir = Path(args.archive_dir) if args.archive_dir else None
    specs = load_specs(UNIVERSE)
    cells = CELLS
    if args.latency:
        cells = [(l, c) for l, c in cells if l in args.latency]
    if args.cancel_rate:
        cells = [(l, c) for l, c in cells if c in args.cancel_rate]
    if not cells:
        raise SystemExit("no cells selected")

    eval_csv = Path(args.eval_csv)
    blocks_csv = Path(args.blocks_csv)
    done = load_done(eval_csv)
    candidates = select_candidates(args.candidate)

    # Day coverage is per coin; compute once per coin.
    days_by_coin: dict[str, list[str]] = {}
    for _cid, coin, _hs in candidates:
        if coin not in days_by_coin:
            days_by_coin[coin] = full_days(coin, args.start_day, end_day, archive_dir)
            print(f"[days] {coin}: {len(days_by_coin[coin])} full days "
                  f"{days_by_coin[coin][0] if days_by_coin[coin] else '-'}.."
                  f"{days_by_coin[coin][-1] if days_by_coin[coin] else '-'}", flush=True)

    if not args.allow_provisional:
        short = {c: d for c, d in days_by_coin.items() if len(d) < args.require_days}
        if short:
            for coin, d in short.items():
                print(f"ERROR: {coin} has only {len(d)} full UTC days; PREREG requires "
                      f">= {args.require_days}. Not writing results. "
                      f"(use --allow-provisional to override, NOT a valid PASS/FAIL)",
                      file=sys.stderr)
            return 2

    total = 0
    for cid, coin, hs in candidates:
        if coin not in specs:
            print(f"skip {coin}: not in universe", file=sys.stderr)
            continue
        spec = specs[coin]
        days = days_by_coin[coin]
        sources: dict = {}
        for latency, cancel_rate in cells:
            for day in days:
                probe = {"candidate": cid, "latency_ms": latency, "cancel_rate": cancel_rate,
                         "day": day}
                if row_key(probe) in done:
                    continue
                row, blocks = run_day(coin, spec, hs, day, latency, cancel_rate,
                                      archive_dir, sources)
                row["candidate"] = cid
                for i, b in enumerate(blocks, start=1):
                    append_row(blocks_csv, BLOCK_COLUMNS, {
                        "candidate": cid, "coin": coin, "hs": hs,
                        "latency_ms": latency, "cancel_rate": cancel_rate,
                        "day": day, "block": i, "net_pnl_usd": b,
                    })
                append_row(eval_csv, EVAL_COLUMNS, row)
                done.add(row_key(row))
                total += 1
                print(f"[{cid} {coin}:{hs} {latency:.0f}/{cancel_rate}] {day} "
                      f"net={row['net_pnl_usd']:+.4f} fund={row['funding_pnl']:+.4f} "
                      f"fills={row['fills']} recs={row['records']} "
                      f"{row['elapsed_s']:.1f}s", flush=True)
                del row
                gc.collect()
    print(f"done: wrote {total} new rows -> {eval_csv}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
