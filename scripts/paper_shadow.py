#!/usr/bin/env python3
"""scripts/paper_shadow.py — T15 live SHADOW paper-trading tracker (read-only, offline).

Tracks the pre-registered T15 candidates (`.agent/PREREG.md`) live while the collector runs:

    XMR hs16 · HYPE hs12 · HYPE hs16 · kBONK hs12
    at --latency-ms 500, --cancel-rate 0.5, using the TRUE recorded feed age ("r - t").

Architecture (pre-registered rule): this process NEVER opens a WebSocket and NEVER touches the
collector's write path. It only READS closed hourly shards `data/YYYYMMDD/HH_<coin>.jsonl.gz`
(much like `replay_backtest.iter_records`), feeds each not-yet-seen closed hour into an
in-memory `ReplayEngine`, and persists:

    data/paper_live_state.json    cumulative + per-day metrics per candidate
    data/paper_live_ledger.csv    one append-only row per candidate per cycle

Only hours strictly BEFORE the current UTC hour are read, so a file the collector is still
appending to is never half-read. A truncated hour (collector crash) is skipped and retried on
the next cycle. On restart the tracker rebuilds from every v2 closed hour it has not seen
(shards must carry the v2 format header, so legacy pre-T15 shards are ignored).

This is monitoring only — the confirmatory test is the full offline PREREG evaluation.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import gzip
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from replay_backtest import ReplayEngine, iter_records, load_specs  # noqa: E402

V2_SCHEMA = "hlmm-shard/2"
DATA_DIR = PROJECT_ROOT / "data"
STATE_FILE = DATA_DIR / "paper_live_state.json"
LEDGER_FILE = DATA_DIR / "paper_live_ledger.csv"
UNIVERSE = DATA_DIR / "universe.json"
LOG_FILE = PROJECT_ROOT / "logs" / "paper_shadow.log"
LOCK_FILE = DATA_DIR / "paper_shadow.lock"

LATENCY_MS = 500.0
CANCEL_RATE = 0.5
CANDIDATES = [("XMR", 16), ("HYPE", 12), ("HYPE", 16), ("kBONK", 12)]

SUMMARY_KEYS = [
    "fills", "round_trip_count", "round_trip_spread_pnl", "inventory_mtm_pnl",
    "funding_pnl", "total_fees_usd", "traded_volume_usd", "net_pnl_usd",
    "ending_inventory_usd", "max_abs_inventory_usd", "funding_samples",
]

LEDGER_COLUMNS = [
    "ts_utc", "coin", "hs", "day", "hour", "fills", "round_trips",
    "round_trip_spread_pnl", "inventory_mtm_pnl", "funding_pnl", "fees_usd",
    "net_pnl_usd", "open_inventory_usd", "worst_drawdown_usd",
]


def now_utc(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_hour(ts: float) -> tuple[str, str]:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y%m%d"), dt.strftime("%H")


def is_v2_shard(path: Path) -> bool:
    """True iff the shard's first line is the collector-v2 format header."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                return rec.get("e") == "header" and rec.get("schema") == V2_SCHEMA
    except (OSError, EOFError, gzip.BadGzipFile, ValueError):
        return False
    return False


def acquire_lock(path: Path):
    """Advisory single-instance lock. Returns the open handle (keep it alive) or None."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.close()
        except (OSError, UnboundLocalError):
            pass
        return None
    return fh


def summary_of(metrics: dict) -> dict:
    return {k: metrics.get(k, 0.0) for k in SUMMARY_KEYS}


class Candidate:
    def __init__(self, coin: str, hs: int, spec):
        self.coin = coin
        self.hs = hs
        self.key = f"{coin}:{hs}"
        self.engine = ReplayEngine(
            spec,
            half_spread_bps=float(hs),
            latency_ms=LATENCY_MS,
            cancel_rate=CANCEL_RATE,
        )
        self.day_cursor: str | None = None
        self.day_base: dict = {k: 0.0 for k in SUMMARY_KEYS}
        self.peak_net = 0.0
        self.worst_dd = 0.0

    def snapshot(self) -> dict:
        return summary_of(self.engine.compute_metrics())

    def today(self) -> dict:
        cur = self.snapshot()
        return {k: cur[k] - self.day_base.get(k, 0.0) for k in SUMMARY_KEYS}


class ShadowTracker:
    def __init__(
        self,
        *,
        data_dir: Path = DATA_DIR,
        specs: dict,
        candidates: list[tuple[str, int]] = CANDIDATES,
        state_path: Path = STATE_FILE,
        ledger_path: Path = LEDGER_FILE,
        log_path: Path = LOG_FILE,
        now=time.time,
    ):
        self.data_dir = Path(data_dir)
        self.state_path = Path(state_path)
        self.ledger_path = Path(ledger_path)
        self.log_path = Path(log_path)
        self.now = now
        self.candidates = [Candidate(c, h, specs[c]) for c, h in candidates if c in specs]
        self.seen: set[tuple[str, str, str]] = set()
        self._v2_cache: dict[str, bool] = {}
        self._log_fh = None

    # ------------------------------------------------------------------ logging
    def _log(self, level: str, msg: str) -> None:
        line = f"[{now_utc(self.now())}] {level:<5} {msg}\n"
        try:
            if self._log_fh is None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_fh = self.log_path.open("a", encoding="utf-8")
            self._log_fh.write(line)
            self._log_fh.flush()
        except OSError:
            pass

    # ------------------------------------------------------------------ discovery
    def closed_hour_shards(self, coin: str, now: float) -> list[tuple[str, str, Path]]:
        today, cur_hh = day_hour(now)
        out: list[tuple[str, str, Path]] = []
        if not self.data_dir.is_dir():
            return out
        for day_dir in sorted(self.data_dir.glob("[0-9]" * 8)):
            if not day_dir.is_dir():
                continue
            day = day_dir.name
            if day > today:
                continue
            for path in sorted(day_dir.glob(f"*_{coin}.jsonl.gz")):
                hh = path.name.split("_", 1)[0]
                if len(hh) != 2 or not hh.isdigit():
                    continue
                if (day, hh) >= (today, cur_hh):
                    continue  # current or future hour: the collector may still be writing it
                key = str(path)
                cached = self._v2_cache.get(key)
                if cached is None:
                    cached = is_v2_shard(path)
                    if cached:
                        self._v2_cache[key] = True
                    # negatives are NOT cached — a half-written crash shard may become
                    # readable once the collector restarts the hour; retry next cycle
                if cached:
                    out.append((day, hh, path))
        return out

    # ------------------------------------------------------------------ cycle
    def cycle(self, now: float | None = None) -> int:
        now = self.now() if now is None else now
        fed = 0
        for cand in self.candidates:
            for day, hh, path in self.closed_hour_shards(cand.coin, now):
                key = (cand.key, day, hh)
                if key in self.seen:
                    continue
                if day != cand.day_cursor:
                    cand.day_base = cand.snapshot()
                    cand.day_cursor = day
                try:
                    for rec in iter_records([path], [], cand.coin):
                        cand.engine.on_record(rec)
                except Exception as exc:  # truncated / unreadable: retry next cycle
                    self._log("WARN", f"feed failed {cand.key} {day}/{hh}: "
                                      f"{type(exc).__name__}: {exc}")
                    continue
                self.seen.add(key)
                fed += 1
                m = cand.snapshot()
                net = m["net_pnl_usd"]
                cand.peak_net = max(cand.peak_net, net)
                cand.worst_dd = max(cand.worst_dd, cand.peak_net - net)
        if fed:
            self.persist(now)
        return fed

    # ------------------------------------------------------------------ persistence
    def _state(self, now: float) -> dict:
        today, _ = day_hour(now)
        out = {
            "updated_utc": now_utc(now),
            "interval_generated_s": None,
            "latency_ms": LATENCY_MS,
            "cancel_rate": CANCEL_RATE,
            "today": today,
            "candidates": {},
        }
        for cand in self.candidates:
            cum = cand.snapshot()
            tod = cand.today() if cand.day_cursor == today else {k: 0.0 for k in SUMMARY_KEYS}
            out["candidates"][cand.key] = {
                "coin": cand.coin,
                "hs": cand.hs,
                "latency_ms": LATENCY_MS,
                "cancel_rate": CANCEL_RATE,
                "cumulative": cum,
                "today": tod,
                "open_inventory_usd": cum["ending_inventory_usd"],
                "worst_drawdown_usd": cand.worst_dd,
                "processed_hours": sum(1 for k in self.seen if k[0] == cand.key),
            }
        return out

    def persist(self, now: float | None = None) -> None:
        now = self.now() if now is None else now
        state = self._state(now)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        tmp.replace(self.state_path)
        exists = self.ledger_path.exists() and self.ledger_path.stat().st_size > 0
        with self.ledger_path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
            if not exists:
                w.writeheader()
            for cand in self.candidates:
                cum = cand.snapshot()
                w.writerow({
                    "ts_utc": now_utc(now),
                    "coin": cand.coin,
                    "hs": cand.hs,
                    "day": cand.day_cursor or "",
                    "hour": "",
                    "fills": cum["fills"],
                    "round_trips": cum["round_trip_count"],
                    "round_trip_spread_pnl": cum["round_trip_spread_pnl"],
                    "inventory_mtm_pnl": cum["inventory_mtm_pnl"],
                    "funding_pnl": cum["funding_pnl"],
                    "fees_usd": cum["total_fees_usd"],
                    "net_pnl_usd": cum["net_pnl_usd"],
                    "open_inventory_usd": cum["ending_inventory_usd"],
                    "worst_drawdown_usd": cand.worst_dd,
                })

    def close(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="T15 live shadow paper-trading tracker (read-only)")
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    p.add_argument("--universe", type=Path, default=UNIVERSE)
    p.add_argument("--state-file", type=Path, default=STATE_FILE)
    p.add_argument("--ledger-file", type=Path, default=LEDGER_FILE)
    p.add_argument("--log-file", type=Path, default=LOG_FILE)
    p.add_argument("--interval", type=float, default=300.0, help="seconds between cycles")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock = acquire_lock(LOCK_FILE)
    if lock is None:
        print(f"paper_shadow: another instance holds {LOCK_FILE}; exiting")
        return 0
    specs = load_specs(args.universe)
    tracker = ShadowTracker(
        data_dir=args.data_dir,
        specs=specs,
        state_path=args.state_file,
        ledger_path=args.ledger_file,
        log_path=args.log_file,
    )
    stop = {"flag": False}

    def _on_signal(signum, _frame):
        stop["flag"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (OSError, ValueError):
            pass

    tracker._log("INFO", f"shadow tracker starting: "
                         f"{[c.key for c in tracker.candidates]} lat={LATENCY_MS} "
                         f"cr={CANCEL_RATE}, interval={args.interval}s")
    try:
        while not stop["flag"]:
            fed = tracker.cycle()
            if fed:
                tracker._log("INFO", f"fed {fed} closed hour-shard(s)")
            if args.once:
                break
            slept = 0.0
            while not stop["flag"] and slept < args.interval:
                time.sleep(min(1.0, args.interval - slept))
                slept += 1.0
    finally:
        try:
            tracker.persist()
        except OSError:
            pass
        tracker._log("INFO", "shadow tracker stopped")
        tracker.close()
        try:
            lock.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
