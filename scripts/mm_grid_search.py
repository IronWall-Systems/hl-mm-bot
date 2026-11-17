#!/usr/bin/env python3
"""MM parameter grid search with the rolling 3h reduce-only safety belt.

Research-only: no credentials, no network orders, no core edits.

Grid (36 combinations):
    half_spread_bps   [10, 12, 16, 20, 24, 28]
    skew_bps          [6, 10, 14]
    max_notional_usd  [30, 60]
plus one continuous no-cooldown baseline at [16, 6, 60].

Every variant runs in ONE continuous pass over the full tick dataset
(2026-10-20 .. 2026-11-05): inventory, cash, funding and equity are carried
across midnight and data gaps, and the rolling 60-minute MTM high-water-mark
cooldown is evaluated on every quoting decision.

Cooldown safety belt (locked):
    plugin      rolling_3h
    window      3600 s rolling equity HWM
    trigger     $0.25 (>=)
    action      reduce_only

Fixed execution assumptions:
    coin HYPE, 66 ms Tokyo latency, maker 1.5 bps, taker 4.5 bps,
    hourly funding from collector records, $30 start, $15 quote,
    $10 min order, cancel_rate 0.5, requote threshold 1.5 bps.

The script stores only incremental statistics per engine (running drawdown,
rolling 60 m deque, daily closes) instead of full equity curves, so 37 engines
fit comfortably in 1 GB RAM.  Mark-to-market is sampled every ``--sample-s``
seconds (default 60) for drawdown/Sharpe; the cooldown trigger itself still
sees every quoting decision.

Usage::

    venv/bin/python scripts/mm_grid_search.py \
        --start 20261020 --end 20261105 \
        --csv logs/mm_grid_search_20261105.csv \
        --md  docs/mm_grid_search_20261105.md
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import math
import statistics
import sys
import tarfile
import time
from array import array
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from replay_backtest import (  # noqa: E402
    ReplayEngine,
    gather_sources,
    load_specs,
)
from mm_cooldown_backtest import (  # noqa: E402
    ContinuousCooldownEngine,
    day_keys,
    round_trip_pnls_with_fees,
)
from strategies.market_maker.plugins.rolling import Rolling3h  # noqa: E402

COIN = "HYPE"
START_EQUITY = 30.0
MAKER_FEE_BPS = 1.5
TAKER_FEE_BPS = 4.5
HOUR = 3600.0

FIXED = dict(
    quote_notional=15.0,
    min_order_notional=10.0,
    requote_threshold_bps=1.5,
    size_tolerance=0.5,
    min_requote_interval_s=0.3,
    latency_ms=66.0,
    feed_age_ms=66.0,
    cancel_rate=0.5,
    maker_fee_bps=MAKER_FEE_BPS,
)

SPREADS = [10.0, 12.0, 16.0, 20.0, 24.0, 28.0]
SKEWS = [6.0, 10.0, 14.0]
MAX_POS = [30.0, 60.0]
DEFAULT_SPREAD, DEFAULT_SKEW, DEFAULT_MAX = 16.0, 6.0, 60.0

DAY_SECONDS = 86400


# ---------------------------------------------------------------------------
# source discovery + chronological streaming
# ---------------------------------------------------------------------------
def _day_sources(data_dir: Path, archive_dir: Path | None, coin: str, day: str):
    key = int(day) * 100
    return gather_sources(data_dir, coin, key, key + 23, archive_dir=archive_dir)


def _iter_local(path: Path, coin: str):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("c") == coin:
                yield rec


def _iter_tar_members(tar_path: Path, members: list[str], coin: str):
    """Open one tar archive once and stream the requested members in order."""
    with tarfile.open(tar_path, "r") as tar:
        for member in members:
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                with gzip.GzipFile(fileobj=fh, mode="rb") as gz:
                    for raw in gz:
                        line = raw.decode("utf-8").strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("c") == coin:
                            yield rec
            except (EOFError, gzip.BadGzipFile, OSError) as exc:
                print(f"warning: truncated {tar_path}:{member}: {exc}", file=sys.stderr)


def iter_records_chrono(data_dir: Path, archive_dir: Path | None, coin: str, days: list[str]):
    """Yield records in true chronological order, day by day, hour by hour.

    Local shards win over tar members for the same hour (via gather_sources).
    Missing hours are simply skipped; the engine keeps state across the gap.
    """
    for day in days:
        paths, tars = _day_sources(data_dir, archive_dir, coin, day)
        # hour -> source (gather_sources already de-duplicates)
        local_by_hour = {p.name[:2]: p for p in paths}
        tar_by_hour: dict[str, tuple[Path, str]] = {}
        for tp, member in tars:
            name = member.split("/", 1)[1]
            tar_by_hour[name[:2]] = (tp, member)

        pending_tar: tuple[Path, list[str]] | None = None
        for hh in (f"{h:02d}" for h in range(24)):
            if hh in local_by_hour:
                if pending_tar:
                    yield from _iter_tar_members(pending_tar[0], pending_tar[1], coin)
                    pending_tar = None
                yield from _iter_local(local_by_hour[hh], coin)
            elif hh in tar_by_hour:
                tp, member = tar_by_hour[hh]
                if pending_tar and pending_tar[0] == tp:
                    pending_tar[1].append(member)
                else:
                    if pending_tar:
                        yield from _iter_tar_members(pending_tar[0], pending_tar[1], coin)
                    pending_tar = (tp, [member])
        if pending_tar:
            yield from _iter_tar_members(pending_tar[0], pending_tar[1], coin)


# ---------------------------------------------------------------------------
# fast line counting (startup visibility only)
# ---------------------------------------------------------------------------
def count_records(data_dir: Path, archive_dir: Path | None, coin: str, days: list[str]) -> tuple[int, dict]:
    total = 0
    per_day: dict[str, int] = {}
    for day in days:
        paths, tars = _day_sources(data_dir, archive_dir, coin, day)
        day_total = 0
        for p in paths:
            with gzip.open(p, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    day_total += chunk.count(b"\n")
        grouped: dict[Path, list[str]] = {}
        for tp, member in tars:
            grouped.setdefault(tp, []).append(member)
        for tp, members in grouped.items():
            with tarfile.open(tp, "r") as tar:
                for member in members:
                    fh = tar.extractfile(member)
                    if fh is None:
                        continue
                    with gzip.GzipFile(fileobj=fh, mode="rb") as gz:
                        while True:
                            chunk = gz.read(1 << 20)
                            if not chunk:
                                break
                            day_total += chunk.count(b"\n")
        per_day[day] = day_total
        total += day_total
    return total, per_day


# ---------------------------------------------------------------------------
# lightweight continuous engine (incremental metrics only)
# ---------------------------------------------------------------------------
class GridEngine(ContinuousCooldownEngine):
    def __init__(self, spec, *, plugin=None, sample_s=60.0, action=None,
                 anchor_daily_start=False, **kwargs):
        super().__init__(
            spec, plugin=plugin, base_equity=START_EQUITY,
            curve_resolution_s=sample_s, **kwargs,
        )
        self.sample_s = float(sample_s)
        self.action = action
        self.anchor_daily_start = bool(anchor_daily_start)
        self._last_sample_t = -math.inf
        self._last_eq = None
        self._peak = -math.inf
        self._peak_t = 0.0
        self.max_dd = 0.0
        self.dd_peak = 0.0
        self.dd_peak_t = 0.0
        self.dd_trough = 0.0
        self.dd_trough_t = 0.0
        self._win = deque()
        self._win_max = deque()
        self.worst60 = 0.0
        self.worst60_hwm = 0.0
        self.worst60_hwm_t = 0.0
        self.worst60_trough = 0.0
        self.worst60_trough_t = 0.0
        self._day = None
        self._day_start: dict[int, float] = {}
        self._day_end: dict[int, float] = {}
        self._day_first_t: dict[int, float] = {}
        self._day_last_t: dict[int, float] = {}
        self._inv_sum = 0.0
        self._inv_count = 0
        self._inv = array("f")
        # Drop the parent's curve lists - this engine never fills them.
        self.equity_curve = []
        self.inventory_samples = []

    # -- sample every N seconds ------------------------------------------------
    def _sample(self, t: float) -> None:
        if self.last_mid <= 0:
            return
        if t - self._last_sample_t < self.sample_s:
            return
        prev_sample_t = self._last_sample_t
        self._last_sample_t = t
        eq = self.equity()
        inv = abs(self.pos * self.last_mid)

        if eq > self._peak:
            self._peak = eq
            self._peak_t = t
        dd = self._peak - eq
        if dd > self.max_dd:
            self.max_dd = dd
            self.dd_peak = self._peak
            self.dd_peak_t = self._peak_t
            self.dd_trough = eq
            self.dd_trough_t = t

        # rolling 60 m HWM drop
        self._win.append((t, eq))
        cutoff = t - HOUR
        while self._win and self._win[0][0] < cutoff:
            self._win.popleft()
        while self._win_max and self._win_max[-1][1] <= eq:
            self._win_max.pop()
        self._win_max.append((t, eq))
        while self._win_max and self._win_max[0][0] < cutoff:
            self._win_max.popleft()
        if self._win_max:
            drop = self._win_max[0][1] - eq
            if drop > self.worst60:
                self.worst60 = drop
                self.worst60_hwm = self._win_max[0][1]
                self.worst60_hwm_t = self._win_max[0][0]
                self.worst60_trough = eq
                self.worst60_trough_t = t

        # daily closes (used for Sharpe / recent-day attribution)
        day = int(t // DAY_SECONDS)
        if self._day is None:
            self._day = day
            # Anchor the first UTC day at the starting equity so summed daily
            # PnL equals final equity minus the initial bankroll.
            self._day_start[day] = self.base_equity if self.anchor_daily_start else eq
            self._day_first_t[day] = t
        elif day != self._day:
            if self._last_eq is not None:
                self._day_end[self._day] = self._last_eq
                self._day_last_t[self._day] = prev_sample_t
                self._day_start[day] = self._last_eq
            self._day = day
            self._day_first_t[day] = t
        self._last_eq = eq

        self._inv_sum += inv
        self._inv_count += 1
        self._inv.append(inv)

    def finalize(self, last_t: float) -> dict:
        # Force a final mark even if inside the sampling interval.
        if self.last_mid > 0:
            self._last_sample_t = -math.inf
            self._sample(last_t)
        if self._day is not None and self._last_eq is not None:
            self._day_end[self._day] = self._last_eq
            self._day_last_t[self._day] = last_t
        return {}

    # -- results ---------------------------------------------------------------
    def daily_returns(self) -> list[float]:
        """Close-to-close daily returns, skipping days with <1h of samples.

        A few records just after midnight used to create a near-zero "day" and
        halve the Sharpe ratio.  Filtering short partial days keeps the daily
        sample comparable across variants.
        """
        valid = [
            d for d in sorted(self._day_end)
            if self._day_last_t.get(d, 0.0) - self._day_first_t.get(d, 0.0) >= 3600.0
        ]
        closes = [self._day_end[d] for d in valid]
        out = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                out.append(closes[i] / closes[i - 1] - 1.0)
        return out

    def day_net(self, day_key: str) -> float:
        idx = int(datetime.strptime(day_key, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp() // DAY_SECONDS)
        start = self._day_start.get(idx)
        end = self._day_end.get(idx)
        if start is None or end is None:
            return 0.0
        return end - start

    def daily_pnl(self) -> dict[str, float]:
        """UTC-day close-to-close PnL in USD, keyed YYYYMMDD."""
        out: dict[str, float] = {}
        for idx in sorted(self._day_end):
            start = self._day_start.get(idx)
            if start is None:
                continue
            key = datetime.fromtimestamp(idx * DAY_SECONDS, tz=timezone.utc).strftime("%Y%m%d")
            out[key] = self._day_end[idx] - start
        return out


def _inv_p95(arr: array) -> float:
    if not arr:
        return 0.0
    vals = sorted(arr)
    return vals[min(len(vals) - 1, int(0.95 * len(vals)))]


def sharpe_365(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mu = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd <= 0:
        return 0.0
    return (mu / sd) * math.sqrt(365.0)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def discover_days(start: str, end: str, data_dir: Path, archive_dir: Path | None):
    days, sources, missing = [], {}, []
    for day in day_keys(start, end):
        paths, tars = _day_sources(data_dir, archive_dir, COIN, day)
        if not paths and not tars:
            missing.append(day)
            continue
        days.append(day)
        sources[day] = (len(paths), len(tars))
    return days, sources, missing


def run(args) -> dict:
    specs = load_specs(Path(args.universe))
    spec = specs[COIN]
    data_dir = Path(args.data_dir)
    archive_dir = Path(args.archive_dir) if args.archive_dir else None

    days, sources, missing = discover_days(args.start, args.end, data_dir, archive_dir)
    print("=" * 100)
    print("MM GRID SEARCH — continuous, rolling_3h reduce_only safety belt")
    print("=" * 100)
    print(f"Included tick days ({len(days)}): {', '.join(days)}")
    if missing:
        print(f"Missing tick days: {', '.join(missing)}")
    print("Per-day sources (local shards / tar members):")
    for d in days:
        n_local, n_tar = sources[d]
        print(f"  {d}: {n_local} local + {n_tar} tar")
    t0 = time.time()
    print("Counting tick records (startup pass)...", flush=True)
    total_ticks, per_day = count_records(data_dir, archive_dir, COIN, days)
    print(f"Total tick records: {total_ticks:,} (count pass {time.time() - t0:.1f}s)")
    for d in days:
        print(f"  {d}: {per_day.get(d, 0):,}")
    print(flush=True)

    # Build variants: 36 grid + 1 baseline
    variants = []
    seen = set()
    for hs in SPREADS:
        for sk in SKEWS:
            for mp in MAX_POS:
                key = (hs, sk, mp)
                if key in seen:
                    continue
                seen.add(key)
                variants.append({
                    "key": f"s{int(hs)}_k{int(sk)}_m{int(mp)}",
                    "spread": hs, "skew": sk, "max_pos": mp,
                    "cooldown": True,
                    "label": f"{hs:.0f}/{sk:.0f}/${mp:.0f}",
                })
    baseline_key = ("base", DEFAULT_SPREAD, DEFAULT_SKEW, DEFAULT_MAX)
    variants.append({
        "key": "baseline_16_6_60", "spread": DEFAULT_SPREAD, "skew": DEFAULT_SKEW,
        "max_pos": DEFAULT_MAX, "cooldown": False,
        "label": "Baseline 16/6/$60 (no cooldown)",
    })

    engines = {}
    plugins = {}
    for v in variants:
        plugin = None
        if v["cooldown"]:
            plugin = Rolling3h(
                state_path=Path(args.state_dir) / f"{v['key']}.json" if args.state_dir else None,
                clock=lambda: _CLOCK[0],
                loss_threshold_usd=args.loss_usd,
                action="reduce_only",
                persist=False,
            )
        eng = GridEngine(
            spec,
            plugin=plugin,
            sample_s=args.sample_s,
            action="reduce_only" if v["cooldown"] else None,
            half_spread_bps=v["spread"],
            skew_bps=v["skew"],
            max_notional=v["max_pos"],
            **FIXED,
        )
        v["engine"] = eng
        engines[v["key"]] = eng
    print(f"Engines: {len(engines)} (36 grid + 1 baseline)\n", flush=True)

    # One continuous chronological pass, feeding every engine.
    n = 0
    last_t = 0.0
    t0 = time.time()
    for rec in iter_records_chrono(data_dir, archive_dir, COIN, days):
        t_ms = rec.get("t")
        if isinstance(t_ms, (int, float)):
            last_t = float(t_ms) / 1000.0
            _CLOCK[0] = last_t
        for eng in engines.values():
            eng.on_record(rec)
        n += 1
        if n % 50_000 == 0:
            for eng in engines.values():
                if eng.feed_ages_ms:
                    eng.feed_ages_ms.clear()
        if n % 500_000 == 0:
            elapsed = time.time() - t0
            rate = n / elapsed if elapsed else 0
            print(f"  {n:,} / {total_ticks:,} records  ({rate:,.0f}/s, {elapsed:.0f}s)", flush=True)

    print(f"Processed {n:,} records in {time.time() - t0:.1f}s\n", flush=True)

    # Summarize
    active_days = len(days)
    results = []
    for v in variants:
        eng = v["engine"]
        eng.finalize(last_t)
        fees = eng._fees
        funding = eng.funding_pnl
        net = eng.equity() - START_EQUITY
        gross = net - funding + fees
        rts = round_trip_pnls_with_fees(eng.fills_with_fees, MAKER_FEE_BPS, TAKER_FEE_BPS)
        wins = sum(1 for p in rts if p > 0)
        losses = sum(1 for p in rts if p <= 0)
        dd = eng.max_dd
        trades = len(rts)
        triggers = sum(1 for e in eng.cooldown_events if e["type"] == "arm")
        returns = eng.daily_returns()
        sharpe = sharpe_365(returns)
        results.append({
            "key": v["key"], "label": v["label"],
            "spread": v["spread"], "skew": v["skew"], "max_pos": v["max_pos"],
            "cooldown": v["cooldown"],
            "trades": trades, "fills": len(eng.fills),
            "fills_per_day": len(eng.fills) / active_days if active_days else 0.0,
            "win_rate": (wins / trades * 100.0) if trades else 0.0,
            "gross_pnl": gross, "fees": fees, "funding": funding,
            "net_pnl": net, "net_pct": (net / START_EQUITY * 100.0),
            "max_dd": dd,
            "max_dd_pct": (dd / eng.dd_peak * 100.0) if eng.dd_peak > 0 else 0.0,
            "worst_60m": eng.worst60,
            "sharpe": sharpe,
            "calmar": (net / dd) if dd > 0 else float("inf"),
            "oct29_net": eng.day_net("20261029"),
            "nov45_net": eng.day_net("20261104") + eng.day_net("20261105"),
            "trigger_count": triggers,
            "max_inv": eng.max_abs_inventory_usd,
            "avg_inv": (eng._inv_sum / eng._inv_count) if eng._inv_count else 0.0,
            "p95_inv": _inv_p95(eng._inv),
            "blocked_share": (eng.blocked_decisions / eng.decisions * 100.0) if eng.decisions else 0.0,
            "decisions": eng.decisions,
            "end_inv": eng.pos * (eng.last_mid if eng.last_mid > 0 else 0.0),
        })

    return {
        "window": f"{args.start}..{args.end}",
        "days": days, "missing": missing, "total_ticks": total_ticks,
        "params": dict(FIXED, spreads=SPREADS, skews=SKEWS, max_pos=MAX_POS,
                       loss_usd=args.loss_usd, plugin="rolling_3h", action="reduce_only",
                       window_s=HOUR, sample_s=args.sample_s, start_equity=START_EQUITY),
        "results": results,
    }


_CLOCK = [0.0]


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _fmt(v, digits=4, signed=False):
    if v is None:
        return "n/a"
    if isinstance(v, float) and not math.isfinite(v):
        return "inf"
    return f"{v:+,.{digits}f}" if signed else f"{v:.{digits}f}"


def rank(results: list[dict]) -> list[dict]:
    return sorted(
        results,
        key=lambda r: (-(r["calmar"] if math.isfinite(r["calmar"]) else 1e9), -r["net_pnl"]),
    )


def print_top(payload: dict, top_n: int = 12):
    ranked = rank(payload["results"])
    grid = [r for r in ranked if r["cooldown"]]
    baseline = next(r for r in payload["results"] if not r["cooldown"])
    default = next(r for r in grid
                   if r["spread"] == DEFAULT_SPREAD and r["skew"] == DEFAULT_SKEW
                   and r["max_pos"] == DEFAULT_MAX)
    top = grid[:top_n]
    if default not in top:
        top = top + [default]
    if baseline not in top:
        top = top + [baseline]

    print("TOP CONFIGURATIONS (ranked by Calmar, tie-break Net PnL)")
    header = (
        f"{'Rank':>4} {'Spread':>6} {'Skew':>5} {'MaxPos':>7} {'Trades':>6} {'Fills':>6} "
        f"{'Fills/d':>7} {'Gross':>9} {'Fees':>8} {'Funding':>8} {'NetPnL':>10} {'Net%':>7} "
        f"{'MaxDD':>8} {'DD%':>6} {'Sharpe':>7} {'Calmar':>7} {'Oct29':>9} {'Nov4-5':>9} {'Trig':>5}"
    )
    print(header)
    print("-" * len(header))
    for i, r in enumerate(top, 1):
        tag = ""
        if not r["cooldown"]:
            tag = " BASE"
        elif r is default:
            tag = " OLD"
        print(
            f"{i:>4} {r['spread']:>6.0f} {r['skew']:>5.0f} {r['max_pos']:>7.0f} "
            f"{r['trades']:>6} {r['fills']:>6} {r['fills_per_day']:>7.1f} "
            f"{r['gross_pnl']:>+9.4f} {r['fees']:>8.4f} {r['funding']:>+8.4f} "
            f"{r['net_pnl']:>+10.4f} {r['net_pct']:>+6.2f}% "
            f"{r['max_dd']:>8.4f} {r['max_dd_pct']:>5.2f}% "
            f"{r['sharpe']:>7.3f} {r['calmar']:>7.3f} "
            f"{r['oct29_net']:>+9.4f} {r['nov45_net']:>+9.4f} {r['trigger_count']:>5}{tag}"
        )
    print()

    # Top by net PnL for transparency
    top_net = sorted(grid, key=lambda r: -r["net_pnl"])[:5]
    print("TOP 5 BY NET PnL")
    for r in top_net:
        print(f"  {r['label']:<14} net {r['net_pnl']:+.4f}  dd {r['max_dd']:.4f}  "
              f"calmar {r['calmar']:.3f}  sharpe {r['sharpe']:.3f}  trig {r['trigger_count']}")
    print()

    best = grid[0]
    print("BEST BY CALMAR")
    print(f"  {best['label']}: net {best['net_pnl']:+.4f} ({best['net_pct']:+.2f}%), "
          f"maxDD {best['max_dd']:.4f} ({best['max_dd_pct']:.2f}%), "
          f"Calmar {best['calmar']:.3f}, Sharpe {best['sharpe']:.3f}, "
          f"trades {best['trades']}, triggers {best['trigger_count']}")
    print()


def write_csv(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "key", "spread_bps", "skew_bps", "max_pos_usd", "cooldown",
        "trades", "fills", "fills_per_day", "win_rate_pct",
        "gross_pnl_usd", "fees_usd", "funding_usd", "net_pnl_usd", "net_pnl_pct",
        "max_dd_usd", "max_dd_pct", "worst_60m_drop_usd", "sharpe_365", "calmar",
        "oct29_net_usd", "nov4_5_net_usd", "trigger_count",
        "max_inventory_usd", "avg_inventory_usd", "p95_inventory_usd",
        "blocked_pct", "decisions", "ending_inventory_usd",
    ]
    ranked = rank(payload["results"])
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in ranked:
            w.writerow({
                "key": r["key"],
                "spread_bps": r["spread"], "skew_bps": r["skew"],
                "max_pos_usd": r["max_pos"], "cooldown": r["cooldown"],
                "trades": r["trades"], "fills": r["fills"],
                "fills_per_day": f"{r['fills_per_day']:.2f}",
                "win_rate_pct": f"{r['win_rate']:.2f}",
                "gross_pnl_usd": f"{r['gross_pnl']:.4f}",
                "fees_usd": f"{r['fees']:.4f}", "funding_usd": f"{r['funding']:.4f}",
                "net_pnl_usd": f"{r['net_pnl']:.4f}", "net_pnl_pct": f"{r['net_pct']:.2f}",
                "max_dd_usd": f"{r['max_dd']:.4f}", "max_dd_pct": f"{r['max_dd_pct']:.2f}",
                "worst_60m_drop_usd": f"{r['worst_60m']:.4f}",
                "sharpe_365": f"{r['sharpe']:.4f}", "calmar": f"{r['calmar']:.4f}",
                "oct29_net_usd": f"{r['oct29_net']:.4f}",
                "nov4_5_net_usd": f"{r['nov45_net']:.4f}",
                "trigger_count": r["trigger_count"],
                "max_inventory_usd": f"{r['max_inv']:.4f}",
                "avg_inventory_usd": f"{r['avg_inv']:.4f}",
                "p95_inventory_usd": f"{r['p95_inv']:.4f}",
                "blocked_pct": f"{r['blocked_share']:.2f}",
                "decisions": r["decisions"],
                "ending_inventory_usd": f"{r['end_inv']:.4f}",
            })
    print(f"[saved] {path}")


def write_markdown(path: Path, payload: dict, top_n: int = 12):
    ranked = rank(payload["results"])
    grid = [r for r in ranked if r["cooldown"]]
    baseline = next(r for r in payload["results"] if not r["cooldown"])
    default = next(r for r in grid
                   if r["spread"] == DEFAULT_SPREAD and r["skew"] == DEFAULT_SKEW
                   and r["max_pos"] == DEFAULT_MAX)
    top = grid[:top_n]
    if default not in top:
        top = top + [default]
    if baseline not in top:
        top = top + [baseline]

    best = grid[0]
    best_net = max(grid, key=lambda r: r["net_pnl"])
    p = payload["params"]

    lines = []
    lines.append("# MM Parameter Grid Search — rolling_3h reduce_only safety belt")
    lines.append("")
    lines.append(f"**Window:** {payload['window']} · **tick days:** {len(payload['days'])} · "
                 f"**records:** {payload['total_ticks']:,}")
    if payload["missing"]:
        lines.append(f"**Missing days:** {', '.join(payload['missing'])}")
    lines.append("")
    lines.append("## Protocol")
    lines.append("")
    lines.append(f"- Coin **HYPE**, **{p['latency_ms']:.0f} ms** Tokyo latency, "
                 f"maker **{p['maker_fee_bps']} bps**, taker **{p['taker_fee_bps'] if 'taker_fee_bps' in p else TAKER_FEE_BPS} bps**")
    lines.append(f"- Starting equity **${p['start_equity']:.2f}**, quote **${p['quote_notional']:.0f}**, "
                 f"min order **${p['min_order_notional']:.0f}**, cancel_rate **{p['cancel_rate']}**")
    lines.append(f"- Continuous execution across all days (no daily resets), hourly funding from collector records")
    lines.append(f"- Cooldown: **rolling_3h**, window **{p['window_s']:.0f}s**, trigger **${p['loss_usd']:.2f}**, "
                 f"action **{p['action']}**")
    lines.append(f"- MTM metrics sampled every **{p['sample_s']:.0f}s**; cooldown trigger sees every decision")
    lines.append(f"- Sharpe annualised from calendar-day returns ×√365; "
                 f"Calmar = Net PnL / Max MTM DD")
    lines.append("")
    lines.append(f"Grid: {len(SPREADS)} spreads × {len(SKEWS)} skews × {len(MAX_POS)} caps "
                 f"= **{len(grid)} configurations** plus 1 no-cooldown baseline.")
    lines.append("")

    lines.append("## Top 12 Configurations (ranked by Calmar, tie-break Net PnL)")
    lines.append("")
    lines.append("| # | Spread | Skew | Max Pos | Trades | Fills | Fills/day | Gross PnL | Fees | Funding | Net PnL | Net % | Max MTM DD | DD % | Sharpe | Calmar | Oct 29 | Nov 4–5 | Triggers |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, r in enumerate(top, 1):
        tag = ""
        if not r["cooldown"]:
            tag = " *(baseline)*"
        elif r is default:
            tag = " *(old 16/6/$60)*"
        lines.append(
            f"| {i} | {r['spread']:.0f} | {r['skew']:.0f} | ${r['max_pos']:.0f} | "
            f"{r['trades']} | {r['fills']} | {r['fills_per_day']:.1f} | "
            f"{r['gross_pnl']:+.4f} | {r['fees']:.4f} | {r['funding']:+.4f} | "
            f"**{r['net_pnl']:+.4f}** | {r['net_pct']:+.2f}% | "
            f"{r['max_dd']:.4f} | {r['max_dd_pct']:.2f}% | "
            f"{r['sharpe']:.3f} | {r['calmar']:.3f} | "
            f"{r['oct29_net']:+.4f} | {r['nov45_net']:+.4f} | {r['trigger_count']}{tag} |"
        )
    lines.append("")

    lines.append("## Key Results")
    lines.append("")
    lines.append(f"- **Best Calmar:** `{best['label']}` — Net **{best['net_pnl']:+.4f}** "
                 f"({best['net_pct']:+.2f}%), Max DD **{best['max_dd']:.4f}** "
                 f"({best['max_dd_pct']:.2f}%), Calmar **{best['calmar']:.3f}**, "
                 f"Sharpe **{best['sharpe']:.3f}**, triggers **{best['trigger_count']}**.")
    lines.append(f"- **Best Net PnL:** `{best_net['label']}` — Net **{best_net['net_pnl']:+.4f}** "
                 f"({best_net['net_pct']:+.2f}%), Max DD **{best_net['max_dd']:.4f}**, "
                 f"Calmar **{best_net['calmar']:.3f}**, triggers **{best_net['trigger_count']}**.")
    lines.append(f"- **Old 16/6/$60 + 3h reduce-only:** Net **{default['net_pnl']:+.4f}** "
                 f"({default['net_pct']:+.2f}%), Max DD **{default['max_dd']:.4f}**, "
                 f"Calmar **{default['calmar']:.3f}**, Sharpe **{default['sharpe']:.3f}**, "
                 f"Oct 29 **{default['oct29_net']:+.4f}**, triggers **{default['trigger_count']}**.")
    lines.append(f"- **No-cooldown baseline 16/6/$60:** Net **{baseline['net_pnl']:+.4f}** "
                 f"({baseline['net_pct']:+.2f}%), Max DD **{baseline['max_dd']:.4f}**, "
                 f"Sharpe **{baseline['sharpe']:.3f}**.")
    lines.append("")

    # spread / skew / cap marginal analysis
    lines.append("## Marginal Effects (mean Net PnL and Calmar across other axes)")
    lines.append("")
    for name, key, values in (
        ("Half-spread (bps)", "spread", SPREADS),
        ("Skew (bps)", "skew", SKEWS),
        ("Max position ($)", "max_pos", MAX_POS),
    ):
        lines.append(f"**{name}**")
        lines.append("")
        lines.append("| Value | Mean Net PnL | Mean Calmar | Mean Max DD | Best Net PnL |")
        lines.append("|---:|---:|---:|---:|---:|")
        for val in values:
            subset = [r for r in grid if r[key] == val]
            mean_net = statistics.fmean(r["net_pnl"] for r in subset)
            mean_cal = statistics.fmean(r["calmar"] for r in subset
                                        if math.isfinite(r["calmar"]))
            mean_dd = statistics.fmean(r["max_dd"] for r in subset)
            best_net = max(r["net_pnl"] for r in subset)
            lines.append(f"| {val:.0f} | {mean_net:+.4f} | {mean_cal:.3f} | {mean_dd:.4f} | {best_net:+.4f} |")
        lines.append("")

    lines.append("## Notes & Caveats")
    lines.append("")
    lines.append("- Continuous execution carries inventory/cash/funding across midnight; "
                 "missing hours are skipped but state is preserved.")
    lines.append("- Sources are streamed strictly chronologically (per day, per hour), merging "
                 "tar-backed Oct 20–27 with local Oct 28–Nov 5. The earlier continuous harness "
                 "iterated all local files before tar members, which replayed Oct 20–27 out of "
                 "order; this run corrects that, so its reference numbers can differ.")
    lines.append("- 2026-11-04 (9 hours) and 2026-11-05 (18 hours) are partial collector days; "
                 "2026-11-03 is now complete.")
    lines.append("- The cooldown plugin triggers on the rolling 60-minute MTM equity HWM and "
                 "uses reduce-only quoting until flat.")
    lines.append("- Max DD and Sharpe use a sampled equity grid; intra-sample excursions can be "
                 "slightly larger.")
    lines.append("- One dominant tail day remains (Oct 29). Treat the ranking as a screen, not "
                 "statistical validation; forward paper testing is still required.")
    lines.append("- `src/mmbot/` is untouched; all work is in `scripts/` and "
                 "`strategies/market_maker/plugins/`.")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append("cd /root/hl-mm-bot")
    lines.append("venv/bin/python scripts/mm_grid_search.py \\")
    lines.append(f"    --start {payload['window'].split('..')[0]} --end {payload['window'].split('..')[1]} \\")
    lines.append("    --csv logs/mm_grid_search_20261105.csv \\")
    lines.append("    --md docs/mm_grid_search_20261105.md")
    lines.append("```")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[saved] {path}")


def build_parser():
    p = argparse.ArgumentParser(description="MM parameter grid search with rolling_3h reduce_only")
    p.add_argument("--start", default="20261020")
    p.add_argument("--end", default="20261105")
    p.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    p.add_argument("--archive-dir", default=str(PROJECT_ROOT / "data" / "archive_tars"))
    p.add_argument("--universe", default=str(PROJECT_ROOT / "data" / "universe.json"))
    p.add_argument("--loss-usd", type=float, default=0.25)
    p.add_argument("--sample-s", type=float, default=60.0)
    p.add_argument("--state-dir", default=None)
    p.add_argument("--csv", default=None)
    p.add_argument("--md", default=None)
    p.add_argument("--json", default=None)
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--max-days", type=int, default=None, help="smoke-test only")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    # 37 plugins would otherwise flood the log with arm/expire lines.
    logging.getLogger("mmbot.cooldown").setLevel(logging.ERROR)
    if args.max_days:
        days = day_keys(args.start, args.end)[: args.max_days]
        if days:
            args.start, args.end = days[0], days[-1]
    payload = run(args)
    print_top(payload, args.top)
    if args.csv:
        write_csv(Path(args.csv), payload)
    if args.md:
        write_markdown(Path(args.md), payload, args.top)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        slim = {k: v for k, v in payload.items() if k != "results"}
        slim["results"] = [
            {k: v for k, v in r.items()} for r in payload["results"]
        ]
        Path(args.json).write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
        print(f"[saved] {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
