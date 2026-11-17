#!/usr/bin/env python3
"""MM validation suite: daily PnL matrix, Reality Check / SPA, latency and hard cap.

Research-only. No credentials, no network orders, no edits to ``src/mmbot``.

What this script does
---------------------
A. Full-period pass (2026-10-20 .. 2026-11-05) with every engine in one stream:

   * 36 grid configs (rolling_3h reduce_only)
   * 7 extra rolling variants  (1h/3h/6h/12h x flatten/reduce_only; the
     3h reduce_only trial is the same engine as grid 16/6/$60)
   * 3 legacy UTC-day cooldown variants (6h/12h/24h_4blocks, 16/6/$60)
   * 1 continuous no-cooldown baseline (16/6/$60)
   -> 48 trial rows for the daily PnL matrix (K = 48)

   * 20 extra latency engines: Top 5 configs x {150,300,500,1000} ms
     (the 66 ms rows reuse the grid engines)
   * 12 strict hard-cap engines: {20/6, 24/14} x {$15,$25,$30} x {66,300} ms
     quote_notional=$10, max_notional=hard_cap, and a pre-place check
     abs(pos*mid) + pending_same_side_notional + quote_notional <= hard_cap.

B. Oct 29-only pass:
   * normal 20/6/$30 and strict $15/$25 caps at 66 ms and 300 ms
   * harsh versions at 500 ms, cancel_rate 0.25, taker 6.0 bps

C. Bootstrap: 5,000 stationary day-block draws over the K x D matrix.
   White Reality Check p-value, Hansen SPA consistent p-value, winner daily
   t-statistic, and a 98% percentile CI for mean daily PnL.

Outputs:
    logs/mm_daily_pnl_matrix_20261105.csv
    logs/mm_daily_pnl_matrix_20261105.json
    logs/mm_validation_20261105.log
    docs/mm_validation_20261105.md

Usage::

    venv/bin/python scripts/mm_validation_suite.py
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import mm_grid_search as mgs  # noqa: E402
from mm_cooldown_backtest import (  # noqa: E402
    ContinuousCooldownEngine,
    day_keys,
    round_trip_pnls_with_fees,
)
from replay_backtest import load_specs, gather_sources  # noqa: E402
from strategies.market_maker.plugins.cooldown_6h import Cooldown6h  # noqa: E402
from strategies.market_maker.plugins.cooldown_12h import Cooldown12h  # noqa: E402
from strategies.market_maker.plugins.cooldown_24h_4blocks import Cooldown24h4Blocks  # noqa: E402
from strategies.market_maker.plugins.rolling import (  # noqa: E402
    Rolling1h,
    Rolling3h,
    Rolling6h,
    Rolling12h,
)

COIN = "HYPE"
START = mgs.START_EQUITY
MAKER = mgs.MAKER_FEE_BPS
TAKER = mgs.TAKER_FEE_BPS
SPREADS = mgs.SPREADS
SKEWS = mgs.SKEWS
MAX_POS = mgs.MAX_POS
DEFAULT = (16.0, 6.0, 60.0)
TOP5 = [(20.0, 6.0, 30.0), (20.0, 6.0, 60.0), (24.0, 14.0, 60.0),
        (20.0, 10.0, 60.0), (24.0, 14.0, 30.0)]
LATENCIES = [66.0, 150.0, 300.0, 500.0, 1000.0]
HARD_CAPS = [15.0, 25.0, 30.0]
BOOT_DRAWS = 5000
BOOT_BLOCK_DAYS = 5.0
BOOT_SEED = 20261105


# ---------------------------------------------------------------------------
# strict 1.0x equity hard-cap engine
# ---------------------------------------------------------------------------
class HardCapEngine(mgs.GridEngine):
    """GridEngine with a strict pre-place inventory hard cap.

    A risk-increasing target is suppressed (and any resting/pending order on
    that side is cancelled by the normal target path) unless::

        abs(pos * mid) + pending_same_side_notional + quote_notional <= cap

    ``pending_same_side_notional`` includes both the live working order and an
    in-flight placement on the same side.  Risk-reducing quotes are never vetoed.
    """

    def __init__(self, spec, *, hard_cap_usd, **kwargs):
        super().__init__(spec, **kwargs)
        self.hard_cap_usd = float(hard_cap_usd)
        self.cap_vetoes = 0
        self.cap_veto_notional = 0.0
        self.max_seen_pos_units = 0.0
        self.max_seen_pos_usd = 0.0

    def _purge_pending_place(self, side):
        """Remove a not-yet-effective risk-increasing placement from the heap.

        Returning no target only schedules a cancel; if a cancel is already in
        flight (``pending_cancel`` true) the normal path skips scheduling, and a
        previously queued place could still execute after the cancel.  Purging
        the queued place is the strict pre-place veto.
        """
        self.pending = [
            entry for entry in self.pending
            if not (entry[2] == "place" and entry[3][0] == side)
        ]
        self.pending_place[side] = None

    def _apply_target_constraints(self, targets, m):
        if m.mid <= 0:
            return targets
        pos_n = abs(self.pos * m.mid)
        self.max_seen_pos_units = max(self.max_seen_pos_units, abs(self.pos))
        self.max_seen_pos_usd = max(self.max_seen_pos_usd, pos_n)
        out = {}
        for side, tgt in targets.items():
            risk_increasing = (
                (side == "buy" and self.pos >= -1e-12)
                or (side == "sell" and self.pos <= 1e-12)
            )
            if not risk_increasing:
                out[side] = tgt
                continue
            pending = 0.0
            working = self.working.get(side)
            if working is not None:
                pending += float(working.px) * float(working.size)
            in_flight = self.pending_place.get(side)
            if in_flight is not None:
                pending += float(in_flight[0]) * float(in_flight[1])
            quote_n = float(tgt.price) * float(tgt.size)
            if pos_n + pending + quote_n <= self.hard_cap_usd + 1e-9:
                out[side] = tgt
            else:
                self.cap_vetoes += 1
                self.cap_veto_notional += quote_n
                self._purge_pending_place(side)
        return out


# ---------------------------------------------------------------------------
# engine factory
# ---------------------------------------------------------------------------
def make_engine(spec, *, spread, skew, max_pos, latency=66.0, quote=15.0,
                plugin_cls=None, action=None, hard_cap=None, taker=TAKER,
                cancel_rate=0.5, min_order=10.0):
    plugin = None
    if plugin_cls is not None:
        plugin = plugin_cls(
            state_path=None,
            clock=lambda: mgs._CLOCK[0],
            loss_threshold_usd=0.25,
            action=action,
            persist=False,
        )
    kwargs = dict(mgs.FIXED)
    kwargs.update(
        latency_ms=float(latency),
        feed_age_ms=float(latency),
        quote_notional=float(quote),
        cancel_rate=float(cancel_rate),
        min_order_notional=float(min_order),
        max_notional=float(hard_cap if hard_cap is not None else max_pos),
    )
    if hard_cap is not None:
        return HardCapEngine(
            spec, hard_cap_usd=hard_cap, plugin=plugin, sample_s=60.0,
            action=action, anchor_daily_start=True,
            half_spread_bps=spread, skew_bps=skew, taker_fee_bps=taker, **kwargs,
        )
    return mgs.GridEngine(
        spec, plugin=plugin, sample_s=60.0, action=action,
        anchor_daily_start=True, half_spread_bps=spread, skew_bps=skew,
        taker_fee_bps=taker, **kwargs,
    )


def summarize(eng, last_t):
    eng.finalize(last_t)
    net = eng.equity() - START
    fees = eng._fees
    funding = eng.funding_pnl
    rts = round_trip_pnls_with_fees(eng.fills_with_fees, MAKER, eng.taker_fee_bps)
    return {
        "net_pnl": net,
        "net_pct": net / START * 100.0,
        "fees": fees,
        "funding": funding,
        "gross_pnl": net - funding + fees,
        "trades": len(rts),
        "fills": len(eng.fills),
        "max_dd": eng.max_dd,
        "max_dd_pct": (eng.max_dd / eng.dd_peak * 100.0) if eng.dd_peak > 0 else 0.0,
        "sharpe": mgs.sharpe_365(eng.daily_returns()),
        "calmar": (net / eng.max_dd) if eng.max_dd > 0 else float("inf"),
        "triggers": sum(1 for e in eng.cooldown_events if e["type"] == "arm"),
        "max_inv": eng.max_abs_inventory_usd,
        "daily": eng.daily_pnl(),
        "cap_vetoes": getattr(eng, "cap_vetoes", 0),
        "cap_veto_notional": getattr(eng, "cap_veto_notional", 0.0),
        "max_seen_pos_units": getattr(eng, "max_seen_pos_units", 0.0),
        "max_seen_pos_usd": getattr(eng, "max_seen_pos_usd", 0.0),
    }


# ---------------------------------------------------------------------------
# trial definitions (K = 48)
# ---------------------------------------------------------------------------
def build_trials():
    trials = []

    def add(tid, family, label, spread, skew, max_pos, key, *, quote=15.0,
            latency=66.0, cap=None, cooldown=True, action="reduce_only"):
        trials.append({
            "trial_id": tid, "family": family, "label": label,
            "spread": spread, "skew": skew, "max_pos": max_pos,
            "quote": quote, "latency": latency, "hard_cap": cap,
            "cooldown": cooldown, "action": action if cooldown else "none",
            "engine_key": key,
        })

    for hs in SPREADS:
        for sk in SKEWS:
            for mp in MAX_POS:
                key = f"g_{int(hs)}_{int(sk)}_{int(mp)}"
                add(key, "grid", f"{hs:.0f}/{sk:.0f}/${mp:.0f}", hs, sk, mp, key)

    rolling = [
        (1.0, Rolling1h, "flatten"), (1.0, Rolling1h, "reduce_only"),
        (3.0, Rolling3h, "flatten"), (3.0, Rolling3h, "reduce_only"),
        (6.0, Rolling6h, "flatten"), (6.0, Rolling6h, "reduce_only"),
        (12.0, Rolling12h, "flatten"), (12.0, Rolling12h, "reduce_only"),
    ]
    for hours, cls, action in rolling:
        tid = f"rolling_{int(hours)}h_{action}"
        # 3h reduce_only at 16/6/$60 is exactly the grid engine.
        key = "g_16_6_60" if (hours == 3.0 and action == "reduce_only") else tid
        add(tid, "rolling_cooldown", f"{hours:.0f}h {action}", 16.0, 6.0, 60.0,
            key, action=action)

    for name, cls, label in (
        ("cooldown_6h", Cooldown6h, "Legacy 6h"),
        ("cooldown_12h", Cooldown12h, "Legacy 12h"),
        ("cooldown_24h_4blocks", Cooldown24h4Blocks, "Legacy 24h (4x6h)"),
    ):
        add(name, "legacy_utc", label, 16.0, 6.0, 60.0, name,
            cooldown=True, action="cancel_all")

    add("baseline_16_6_60", "baseline", "Baseline 16/6/$60 (no cooldown)",
        16.0, 6.0, 60.0, "base_16_6_60", cooldown=False, action="none")
    return trials


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
def stationary_indices(n, mean_block, rng):
    """Politis-Romano stationary bootstrap indices (circular)."""
    p = 1.0 / float(mean_block)
    idx = []
    while len(idx) < n:
        if not idx or rng.random() < p:
            idx.append(rng.randrange(n))
        else:
            idx.append((idx[-1] + 1) % n)
    return idx


def bootstrap(matrix, days, draws=BOOT_DRAWS, block=BOOT_BLOCK_DAYS, seed=BOOT_SEED,
              candidate_index=None):
    """Stationary day-block bootstrap over a K x D daily PnL matrix.

    ``matrix[k][d]`` is config k's USD PnL on day d.  All configs share the same
    resampled calendar days so cross-sectional correlation is preserved.

    ``candidate_index`` selects the frozen candidate (default: max-mean trial) for
    the individual t-statistic and 98% CI.  RC/SPA are always max-over-K.
    """
    k = len(matrix)
    d = len(days)
    means = [statistics.fmean(row) for row in matrix]
    winner = max(range(k), key=lambda i: means[i])
    cand = winner if candidate_index is None else candidate_index
    winner_series = matrix[winner]
    cand_series = matrix[cand]
    sd = statistics.stdev(winner_series) if d > 1 else 0.0
    cand_sd = statistics.stdev(cand_series) if d > 1 else 0.0
    t_stat = (means[winner] / (sd / math.sqrt(d))) if sd > 0 else float("inf")
    cand_t = (means[cand] / (cand_sd / math.sqrt(d))) if cand_sd > 0 else float("inf")
    # one-sided normal p for the winner's/candidate's t-stat
    t_p = 0.5 * math.erfc(t_stat / math.sqrt(2.0)) if math.isfinite(t_stat) else 0.0
    cand_t_p = (0.5 * math.erfc(cand_t / math.sqrt(2.0))
                if math.isfinite(cand_t) else 0.0)

    rng = random.Random(seed)
    sqrt_d = math.sqrt(d)
    obs_t = max(0.0, max(sqrt_d * m for m in means))

    rc_extreme = 0
    spa_extreme = 0
    winner_boot_means = []
    cand_boot_means = []
    for _ in range(draws):
        idx = stationary_indices(d, block, rng)
        boot_means = [sum(row[j] for j in idx) / d for row in matrix]
        winner_boot_means.append(boot_means[winner])
        cand_boot_means.append(boot_means[cand])

        # White Reality Check: recenter every model to zero.
        rc_stat = max(sqrt_d * (boot_means[i] - means[i]) for i in range(k))
        if max(0.0, rc_stat) >= obs_t - 1e-15:
            rc_extreme += 1

        # Hansen SPA consistent recentering: poor models stay at their observed
        # (negative) mean, candidates are recentered to zero.
        spa_stat = max(
            sqrt_d * (boot_means[i] - max(0.0, means[i])) for i in range(k)
        )
        if max(0.0, spa_stat) >= obs_t - 1e-15:
            spa_extreme += 1

    winner_boot_means.sort()
    lo = winner_boot_means[int(0.01 * draws)]
    hi = winner_boot_means[min(draws - 1, int(0.99 * draws) - 1)]
    cand_boot_means.sort()
    cand_lo = cand_boot_means[int(0.01 * draws)]
    cand_hi = cand_boot_means[min(draws - 1, int(0.99 * draws) - 1)]
    return {
        "draws": draws,
        "block_mean_days": block,
        "seed": seed,
        "days": d,
        "K": k,
        "winner_index": winner,
        "winner_mean_daily": means[winner],
        "winner_total_pnl": sum(winner_series),
        "winner_daily_t": t_stat,
        "winner_t_one_sided_p": t_p,
        "winner_daily_sd": sd,
        "candidate_index": cand,
        "candidate_mean_daily": means[cand],
        "candidate_total_pnl": sum(cand_series),
        "candidate_daily_t": cand_t,
        "candidate_t_one_sided_p": cand_t_p,
        "candidate_daily_sd": cand_sd,
        "candidate_ci98_low": cand_lo,
        "candidate_ci98_high": cand_hi,
        "candidate_ci98_low_total": cand_lo * d,
        "candidate_ci98_high_total": cand_hi * d,
        "observed_stat": obs_t,
        "rc_p": (1 + rc_extreme) / (draws + 1),
        "spa_p": (1 + spa_extreme) / (draws + 1),
        "ci98_low": lo,
        "ci98_high": hi,
        "ci98_low_total": lo * d,
        "ci98_high_total": hi * d,
        "mean_daily_all": means,
    }


# ---------------------------------------------------------------------------
# full-period pass
# ---------------------------------------------------------------------------
def run_full(args):
    specs = load_specs(Path(args.universe))
    spec = specs[COIN]
    data_dir = Path(args.data_dir)
    archive_dir = Path(args.archive_dir) if args.archive_dir else None
    days, sources, missing = mgs.discover_days(args.start, args.end, data_dir, archive_dir)
    print("=" * 110)
    print("MM VALIDATION SUITE — daily PnL matrix + bootstrap + latency + strict hard cap")
    print("=" * 110)
    print(f"Included days ({len(days)}): {', '.join(days)}")
    if missing:
        print(f"Missing days: {', '.join(missing)}")
    total_ticks, _per_day = mgs.count_records(data_dir, archive_dir, COIN, days)
    print(f"Total tick records: {total_ticks:,}")
    print(flush=True)

    engines = {}
    meta = {}

    def register(key, engine, info):
        engines[key] = engine
        meta[key] = info

    # 36 grid engines
    for hs in SPREADS:
        for sk in SKEWS:
            for mp in MAX_POS:
                key = f"g_{int(hs)}_{int(sk)}_{int(mp)}"
                register(key, make_engine(
                    spec, spread=hs, skew=sk, max_pos=mp,
                    plugin_cls=Rolling3h, action="reduce_only",
                ), {"family": "grid", "label": f"{hs:.0f}/{sk:.0f}/${mp:.0f}",
                    "spread": hs, "skew": sk, "max_pos": mp,
                    "latency": 66.0, "quote": 15.0, "hard_cap": None})

    # extra rolling variants (3h reduce_only reuses grid engine)
    for hours, cls in ((1.0, Rolling1h), (3.0, Rolling3h),
                       (6.0, Rolling6h), (12.0, Rolling12h)):
        for action in ("flatten", "reduce_only"):
            key = f"rolling_{int(hours)}h_{action}"
            if key in engines:
                continue
            register(key, make_engine(
                spec, spread=16.0, skew=6.0, max_pos=60.0,
                plugin_cls=cls, action=action,
            ), {"family": "rolling", "label": f"{hours:.0f}h {action}",
                "spread": 16.0, "skew": 6.0, "max_pos": 60.0,
                "latency": 66.0, "quote": 15.0, "hard_cap": None})

    # 3 legacy UTC-day variants
    for name, cls in (("cooldown_6h", Cooldown6h),
                      ("cooldown_12h", Cooldown12h),
                      ("cooldown_24h_4blocks", Cooldown24h4Blocks)):
        register(name, make_engine(
            spec, spread=16.0, skew=6.0, max_pos=60.0,
            plugin_cls=cls, action=None,
        ), {"family": "legacy", "label": name,
            "spread": 16.0, "skew": 6.0, "max_pos": 60.0,
            "latency": 66.0, "quote": 15.0, "hard_cap": None})

    # baseline
    register("base_16_6_60", make_engine(
        spec, spread=16.0, skew=6.0, max_pos=60.0,
    ), {"family": "baseline", "label": "no cooldown",
        "spread": 16.0, "skew": 6.0, "max_pos": 60.0,
        "latency": 66.0, "quote": 15.0, "hard_cap": None})

    # latency extras (66 ms rows reuse the grid engines)
    for hs, sk, mp in TOP5:
        for lat in LATENCIES[1:]:
            key = f"lat_{int(hs)}_{int(sk)}_{int(mp)}_{int(lat)}"
            register(key, make_engine(
                spec, spread=hs, skew=sk, max_pos=mp, latency=lat,
                plugin_cls=Rolling3h, action="reduce_only",
            ), {"family": "latency", "label": f"{hs:.0f}/{sk:.0f}/${mp:.0f}",
                "spread": hs, "skew": sk, "max_pos": mp,
                "latency": lat, "quote": 15.0, "hard_cap": None})

    # strict hard-cap engines (two configs x three caps x two latencies)
    for hs, sk in ((20.0, 6.0), (24.0, 14.0)):
        for cap in HARD_CAPS:
            for lat in (66.0, 300.0):
                key = f"cap_{int(hs)}_{int(sk)}_{int(cap)}_l{int(lat)}"
                register(key, make_engine(
                    spec, spread=hs, skew=sk, max_pos=cap, latency=lat,
                    quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                    hard_cap=cap, min_order=5.0,
                ), {"family": "hard_cap", "label": f"{hs:.0f}/{sk:.0f}",
                    "spread": hs, "skew": sk, "max_pos": cap,
                    "latency": lat, "quote": 10.0, "hard_cap": cap})

    print(f"Engines in single pass: {len(engines)} "
          f"(47 distinct matrix + 20 latency + 12 hard-cap)", flush=True)

    n = 0
    last_t = 0.0
    t0 = time.time()
    for rec in mgs.iter_records_chrono(data_dir, archive_dir, COIN, days):
        t_ms = rec.get("t")
        if isinstance(t_ms, (int, float)):
            last_t = float(t_ms) / 1000.0
            mgs._CLOCK[0] = last_t
        for eng in engines.values():
            eng.on_record(rec)
        n += 1
        if n % 50_000 == 0:
            for eng in engines.values():
                if eng.feed_ages_ms:
                    eng.feed_ages_ms.clear()
        if n % 500_000 == 0:
            elapsed = time.time() - t0
            print(f"  {n:,} / {total_ticks:,}  ({n / max(elapsed, 1e-9):,.0f}/s, "
                  f"{elapsed:.0f}s)", flush=True)
    print(f"Processed {n:,} records in {time.time() - t0:.1f}s", flush=True)

    summaries = {key: summarize(eng, last_t) for key, eng in engines.items()}
    return {
        "days": days, "missing": missing, "total_ticks": total_ticks,
        "engines": engines, "meta": meta, "summaries": summaries,
        "last_t": last_t,
    }


# ---------------------------------------------------------------------------
# Oct 29 stress
# ---------------------------------------------------------------------------
def run_stress(args):
    specs = load_specs(Path(args.universe))
    spec = specs[COIN]
    data_dir = Path(args.data_dir)
    archive_dir = Path(args.archive_dir) if args.archive_dir else None
    day = "20261029"

    engines = {}
    meta = {}
    variants = [
        ("base_66", dict(spread=20.0, skew=6.0, max_pos=30.0, latency=66.0,
                         plugin_cls=Rolling3h, action="reduce_only")),
        ("base_300", dict(spread=20.0, skew=6.0, max_pos=30.0, latency=300.0,
                          plugin_cls=Rolling3h, action="reduce_only")),
        ("cap15_66", dict(spread=20.0, skew=6.0, max_pos=15.0, latency=66.0,
                          quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                          hard_cap=15.0, min_order=5.0)),
        ("cap15_300", dict(spread=20.0, skew=6.0, max_pos=15.0, latency=300.0,
                           quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                           hard_cap=15.0, min_order=5.0)),
        ("cap25_66", dict(spread=20.0, skew=6.0, max_pos=25.0, latency=66.0,
                          quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                          hard_cap=25.0, min_order=5.0)),
        ("cap25_300", dict(spread=20.0, skew=6.0, max_pos=25.0, latency=300.0,
                           quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                           hard_cap=25.0, min_order=5.0)),
        ("base_harsh", dict(spread=20.0, skew=6.0, max_pos=30.0, latency=500.0,
                            plugin_cls=Rolling3h, action="reduce_only",
                            cancel_rate=0.25, taker=6.0)),
        ("cap15_harsh", dict(spread=20.0, skew=6.0, max_pos=15.0, latency=500.0,
                             quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                             hard_cap=15.0, min_order=5.0,
                             cancel_rate=0.25, taker=6.0)),
        ("cap25_harsh", dict(spread=20.0, skew=6.0, max_pos=25.0, latency=500.0,
                             quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                             hard_cap=25.0, min_order=5.0,
                             cancel_rate=0.25, taker=6.0)),
    ]
    for key, kwargs in variants:
        engines[key] = make_engine(spec, **kwargs)
        meta[key] = {"label": key, **{k: v for k, v in kwargs.items()
                                     if not callable(v)}}

    n = 0
    last_t = 0.0
    t0 = time.time()
    for rec in mgs.iter_records_chrono(data_dir, archive_dir, COIN, [day]):
        t_ms = rec.get("t")
        if isinstance(t_ms, (int, float)):
            last_t = float(t_ms) / 1000.0
            mgs._CLOCK[0] = last_t
        for eng in engines.values():
            eng.on_record(rec)
        n += 1
        if n % 50_000 == 0:
            for eng in engines.values():
                if eng.feed_ages_ms:
                    eng.feed_ages_ms.clear()
    summaries = {key: summarize(eng, last_t) for key, eng in engines.items()}
    print(f"Oct 29 stress: {n:,} records, {len(engines)} engines, "
          f"{time.time() - t0:.1f}s", flush=True)
    return {"day": day, "records": n, "summaries": summaries, "meta": meta}


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------
def write_matrix(path_csv, path_json, trials, summaries, days, bootstrap_res,
                 full):
    path_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "trial_id", "family", "label", "spread_bps", "skew_bps",
        "max_pos_usd", "quote_notional_usd", "latency_ms", "hard_cap_usd",
        "cooldown", "action",
    ] + days
    with path_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for t in trials:
            s = summaries[t["engine_key"]]
            row = {
                "trial_id": t["trial_id"], "family": t["family"],
                "label": t["label"], "spread_bps": t["spread"],
                "skew_bps": t["skew"], "max_pos_usd": t["max_pos"],
                "quote_notional_usd": t["quote"], "latency_ms": t["latency"],
                "hard_cap_usd": "" if t["hard_cap"] is None else t["hard_cap"],
                "cooldown": t["cooldown"], "action": t["action"],
            }
            for day in days:
                row[day] = f"{s['daily'].get(day, 0.0):.6f}"
            w.writerow(row)
    print(f"[saved] {path_csv}")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": f"{days[0]}..{days[-1]}",
        "days": days,
        "K": len(trials),
        "trial_family_counts": {
            fam: sum(1 for t in trials if t["family"] == fam)
            for fam in sorted({t["family"] for t in trials})
        },
        "bootstrap": {k: v for k, v in bootstrap_res.items() if k != "mean_daily_all"},
        "trials": [],
    }
    for i, t in enumerate(trials):
        s = summaries[t["engine_key"]]
        payload["trials"].append({
            **{k: t[k] for k in ("trial_id", "family", "label", "spread", "skew",
                                 "max_pos", "quote", "latency", "hard_cap",
                                 "cooldown", "action", "engine_key")},
            "net_pnl": s["net_pnl"],
            "daily": {day: s["daily"].get(day, 0.0) for day in days},
        })
    path_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[saved] {path_json}")


def write_report(path, args, full, trials, boot_res, stress):
    sums = full["summaries"]
    meta = full["meta"]
    days = full["days"]
    ranked = sorted(
        [t for t in trials if t["family"] == "grid"],
        key=lambda t: -(sums[t["engine_key"]]["net_pnl"]),
    )
    winner_trial = ranked[0]
    winner = sums[winner_trial["engine_key"]]
    old = next(t for t in trials if t["trial_id"] == "g_16_6_60")
    old_s = sums[old["engine_key"]]
    base = next(t for t in trials if t["family"] == "baseline")
    base_s = sums[base["engine_key"]]

    def row(t, s, label=None):
        return (f"| {label or t['label']} | {s['net_pnl']:+.4f} | "
                f"{s['max_dd']:.4f} | "
                f"{s['max_dd_pct']:.2f}% | {s['sharpe']:.3f} | "
                f"{s['calmar']:.3f} | {s['trades']} | {s['triggers']} |")

    lines = []
    lines.append("# MM validation suite — 2026-11-05")
    lines.append("")
    lines.append(f"Window **{days[0]}..{days[-1]}** · **{len(days)}** tick days · "
                 f"**{full['total_ticks']:,}** records · **K = {len(trials)}** trials.")
    lines.append("")
    lines.append("## 1. Timestamp source (`rec[\"t\"]`)")
    lines.append("")
    lines.append("`t` is the **Hyperliquid exchange timestamp** for market-data channels, "
                 "while `r` is the **collector local receive timestamp**:")
    lines.append("")
    lines.append("- `scripts/collector.py:200` `compact_l2`: `\"t\": data.get(\"time\")` (exchange)")
    lines.append("- `scripts/collector.py:219` `compact_bbo`: `\"t\": data.get(\"time\")` (exchange)")
    lines.append("- `scripts/collector.py:231` `compact_trade`: `\"t\": item.get(\"time\")` (exchange)")
    lines.append("- `scripts/collector.py:557` `recv_ms = lambda: int(time.time() * 1000)` (local)")
    lines.append("- `scripts/collector.py:747` `_handle`: `record[\"r\"] = recv` (local receive)")
    lines.append("- `scripts/collector.py:700` funding: `{\"t\": ts, ... \"r\": recv}` where "
                 "`ts = int(now * 1000)` — **local** for funding")
    lines.append("- `scripts/collector.py:758` gap records: `\"t\": int(self.now() * 1000)` — **local**")
    lines.append("- `scripts/replay_backtest.py:518` `t = t_ms / 1000.0`; `:521` "
                 "`age_ms = float(recv) - float(t_ms)`; `:695` `seen_t = t + feed_age`; "
                 "`:704` `eff = seen_t + latency`")
    lines.append("")
    lines.append("So: `l2Book`, `bbo`, and `trades` carry exchange time in `t` and local "
                 "receive time in `r`; funding and gap records carry local time in `t`. "
                 "The replay treats `t` as exchange/event time and uses `r - t` as the "
                 "empirical feed age. The median observed feed age is ~0.41 s (p90 ~0.63 s), "
                 "far above the nominal 66 ms; the latency sweep therefore varies the "
                 "order-path component while leaving the empirical feed age intact.")
    lines.append("")
    lines.append("## 2. Daily PnL matrix and data-snooping tests")
    lines.append("")
    lines.append(f"- Matrix: `logs/mm_daily_pnl_matrix_20261105.csv` / `.json`, "
                 f"**K = {len(trials)}** rows x **D = {len(days)}** days.")
    lines.append(f"- Families: {json.dumps({f: sum(1 for t in trials if t['family']==f) for f in sorted({t['family'] for t in trials})})}")
    lines.append(f"- Winner: **{winner_trial['label']}** — total **{winner['net_pnl']:+.4f}**, "
                 f"mean daily **{boot_res['winner_mean_daily']:+.4f}**, "
                 f"daily t = **{boot_res['winner_daily_t']:.3f}** "
                 f"(one-sided p {boot_res['winner_t_one_sided_p']:.4f}).")
    cand_trial = next(t for t in trials if t["trial_id"] == "g_20_6_30")
    cand_s = sums[cand_trial["engine_key"]]
    lines.append(f"- Frozen candidate **{cand_trial['label']}**: total "
                 f"**{cand_s['net_pnl']:+.4f}**, mean daily "
                 f"**{boot_res['candidate_mean_daily']:+.4f}**, daily t = "
                 f"**{boot_res['candidate_daily_t']:.3f}** "
                 f"(one-sided p {boot_res['candidate_t_one_sided_p']:.4f}).")
    lines.append(f"- Stationary day-block bootstrap, **{BOOT_DRAWS:,}** draws, "
                 f"mean block {BOOT_BLOCK_DAYS:.0f} days, seed {BOOT_SEED}.")
    lines.append(f"- **White Reality Check p = {boot_res['rc_p']:.4f}** "
                 f"(max over all K={len(trials)} trials)")
    lines.append(f"- **Hansen SPA consistent p = {boot_res['spa_p']:.4f}** "
                 f"(max over all K={len(trials)} trials)")
    lines.append(f"- 98% block-bootstrap CI for the frozen candidate's mean daily PnL: "
                 f"**[{boot_res['candidate_ci98_low']:+.4f}, "
                 f"{boot_res['candidate_ci98_high']:+.4f}]** USD/day "
                 f"({boot_res['candidate_ci98_low_total']:+.4f} .. "
                 f"{boot_res['candidate_ci98_high_total']:+.4f} over {len(days)} days).")
    lines.append("")
    lines.append("| Trial | Net PnL | Max DD | DD % | Sharpe | Calmar | Trades | Triggers |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for t in ranked[:12]:
        s = sums[t["engine_key"]]
        lines.append(row(t, s))
    lines.append(row(old, old_s, "old 16/6/$60 (3h RO)"))
    lines.append(row(base, base_s, "baseline 16/6/$60"))
    # The K=48 max-mean trial is a legacy variant; show it explicitly.
    for t in trials:
        if t["family"] == "legacy_utc" and t["trial_id"] == winner_trial["trial_id"]:
            lines.append(row(t, winner, f"K=48 max: {t['label']}"))
            break
    lines.append("")
    lines.append("## 3. Latency sensitivity (order-path latency, empirical feed age)")
    lines.append("")
    lines.append("| Config | 66 ms | 150 ms | 300 ms | 500 ms | 1000 ms |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for hs, sk, mp in TOP5:
        cells = []
        for lat in LATENCIES:
            key = (f"g_{int(hs)}_{int(sk)}_{int(mp)}" if lat == 66.0
                   else f"lat_{int(hs)}_{int(sk)}_{int(mp)}_{int(lat)}")
            cells.append(f"{sums[key]['net_pnl']:+.4f}")
        lines.append(f"| {hs:.0f}/{sk:.0f}/${mp:.0f} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("| Config | Latency | Net PnL | Max DD | DD % | Sharpe | Calmar | Trades | Triggers |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for hs, sk, mp in TOP5:
        for lat in LATENCIES:
            key = (f"g_{int(hs)}_{int(sk)}_{int(mp)}" if lat == 66.0
                   else f"lat_{int(hs)}_{int(sk)}_{int(mp)}_{int(lat)}")
            s = sums[key]
            lines.append(f"| {hs:.0f}/{sk:.0f}/${mp:.0f} | {lat:.0f} | "
                         f"{s['net_pnl']:+.4f} | {s['max_dd']:.4f} | "
                         f"{s['max_dd_pct']:.2f}% | {s['sharpe']:.3f} | "
                         f"{s['calmar']:.3f} | {s['trades']} | {s['triggers']} |")
    lines.append("")
    lines.append("## 4. Strict 1.0x equity hard cap (quote $10)")
    lines.append("")
    lines.append("| Config | Cap | Latency | Net PnL | Max DD | Max |Inv| | Max pos $ | Cap honoured | Trades | Triggers | Vetoes |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|")
    for hs, sk in ((20.0, 6.0), (24.0, 14.0)):
        for cap in HARD_CAPS:
            for lat in (66.0, 300.0):
                key = f"cap_{int(hs)}_{int(sk)}_{int(cap)}_l{int(lat)}"
                s = sums[key]
                honoured = "yes" if s["max_seen_pos_usd"] <= cap + 0.01 else "NO"
                lines.append(f"| {hs:.0f}/{sk:.0f} | ${cap:.0f} | {lat:.0f} | "
                             f"{s['net_pnl']:+.4f} | {s['max_dd']:.4f} | "
                             f"{s['max_inv']:.4f} | {s['max_seen_pos_usd']:.4f} | "
                             f"{honoured} | {s['trades']} | "
                             f"{s['triggers']} | {s['cap_vetoes']} |")
    lines.append("")
    lines.append("## 5. Oct 29 stress")
    lines.append("")
    lines.append("| Variant | Net PnL | Max DD | DD % | Max |Inv| | Trades | Triggers |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for key, s in stress["summaries"].items():
        lines.append(f"| {key} | {s['net_pnl']:+.4f} | {s['max_dd']:.4f} | "
                     f"{s['max_dd_pct']:.2f}% | {s['max_inv']:.4f} | "
                     f"{s['trades']} | {s['triggers']} |")
    lines.append("")
    lines.append("Normal = 66/300 ms, cancel_rate 0.5, maker 1.5 bps; harsh = 500 ms, "
                 "cancel_rate 0.25, taker 6.0 bps.")
    lines.append("")
    lines.append("## 6. Files and caveats")
    lines.append("")
    lines.append("- `logs/mm_daily_pnl_matrix_20261105.csv` / `.json` — full K=48 matrix")
    lines.append("- `logs/mm_validation_20261105.log` — run log")
    lines.append("- `.agent/PREREG2.md` — frozen candidate (hash in `logs/prereg2_sha256.txt`)")
    lines.append("")
    lines.append("- Bootstrap resamples UTC days jointly across configs; it preserves "
                 "cross-sectional correlation but has only 17 day observations.")
    lines.append("- `src/mmbot/` is untouched; this is research-only, with no credentials "
                 "and no orders.")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[saved] {path}")


# ---------------------------------------------------------------------------
# targeted hard-cap recheck (after a veto-path fix)
# ---------------------------------------------------------------------------
def run_caps_only(args):
    specs = load_specs(Path(args.universe))
    spec = specs[COIN]
    data_dir = Path(args.data_dir)
    archive_dir = Path(args.archive_dir) if args.archive_dir else None
    days, _sources, _missing = mgs.discover_days(args.start, args.end, data_dir, archive_dir)
    total_ticks, _ = mgs.count_records(data_dir, archive_dir, COIN, days)
    engines = {}
    meta = {}
    for hs, sk in ((20.0, 6.0), (24.0, 14.0)):
        for cap in HARD_CAPS:
            for lat in (66.0, 300.0):
                key = f"cap_{int(hs)}_{int(sk)}_{int(cap)}_l{int(lat)}"
                engines[key] = make_engine(
                    spec, spread=hs, skew=sk, max_pos=cap, latency=lat,
                    quote=10.0, plugin_cls=Rolling3h, action="reduce_only",
                    hard_cap=cap, min_order=5.0,
                )
                meta[key] = {"spread": hs, "skew": sk, "cap": cap, "latency": lat}
    print(f"Hard-cap recheck: {len(engines)} engines, {total_ticks:,} records", flush=True)
    n = 0
    last_t = 0.0
    t0 = time.time()
    for rec in mgs.iter_records_chrono(data_dir, archive_dir, COIN, days):
        t_ms = rec.get("t")
        if isinstance(t_ms, (int, float)):
            last_t = float(t_ms) / 1000.0
            mgs._CLOCK[0] = last_t
        for eng in engines.values():
            eng.on_record(rec)
        n += 1
        if n % 50_000 == 0:
            for eng in engines.values():
                if eng.feed_ages_ms:
                    eng.feed_ages_ms.clear()
        if n % 1_000_000 == 0:
            print(f"  {n:,} / {total_ticks:,} ({n / (time.time() - t0):,.0f}/s)",
                  flush=True)
    summaries = {key: summarize(eng, last_t) for key, eng in engines.items()}
    print(f"Hard-cap recheck processed {n:,} records in {time.time() - t0:.1f}s",
          flush=True)
    stress = run_stress(args)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": f"{days[0]}..{days[-1]}",
        "days": days,
        "hard_cap": summaries,
        "stress": stress["summaries"],
    }
    out = PROJECT_ROOT / "logs" / "mm_hardcap_recheck_20261105.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"[saved] {out}")

    print("\nSTRICT HARD CAP RECHECK (quote $10)")
    print(f"{'Config':<10} {'Cap':>4} {'Lat':>5} {'Net':>10} {'MaxDD':>8} "
          f"{'MaxInv':>8} {'MaxPos$':>8} {'CapOK':>6} {'Trades':>6} {'Trig':>5}")
    for key, s in summaries.items():
        cap = meta[key]["cap"]
        ok = "yes" if s["max_seen_pos_usd"] <= cap + 0.01 else "NO"
        print(f"{key:<24} {cap:>4.0f} {meta[key]['latency']:>5.0f} "
              f"{s['net_pnl']:>+10.4f} {s['max_dd']:>8.4f} {s['max_inv']:>8.4f} "
              f"{s['max_seen_pos_usd']:>8.4f} {ok:>6} {s['trades']:>6} "
              f"{s['triggers']:>5}")
    print("\nOCT 29 STRESS (recheck)")
    for key, s in stress["summaries"].items():
        print(f"  {key:<14} net {s['net_pnl']:+.4f}  dd {s['max_dd']:.4f}  "
              f"maxInv {s['max_inv']:.4f}  maxPos$ {s['max_seen_pos_usd']:.4f}  "
              f"trades {s['trades']}  trig {s['triggers']}")
    return 0


# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="MM validation suite (research only)")
    p.add_argument("--start", default="20261020")
    p.add_argument("--end", default="20261105")
    p.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    p.add_argument("--archive-dir", default=str(PROJECT_ROOT / "data" / "archive_tars"))
    p.add_argument("--universe", default=str(PROJECT_ROOT / "data" / "universe.json"))
    p.add_argument("--csv", default=str(PROJECT_ROOT / "logs" / "mm_daily_pnl_matrix_20261105.csv"))
    p.add_argument("--json", default=str(PROJECT_ROOT / "logs" / "mm_daily_pnl_matrix_20261105.json"))
    p.add_argument("--md", default=str(PROJECT_ROOT / "docs" / "mm_validation_20261105.md"))
    p.add_argument("--skip-stress", action="store_true")
    p.add_argument("--caps-only", action="store_true",
                   help="rerun only the strict hard-cap engines + Oct 29 stress")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.getLogger("mmbot.cooldown").setLevel(logging.ERROR)

    if args.caps_only:
        return run_caps_only(args)

    full = run_full(args)
    trials = build_trials()
    sums = full["summaries"]
    days = full["days"]

    matrix = []
    for t in trials:
        s = sums[t["engine_key"]]
        matrix.append([s["daily"].get(day, 0.0) for day in days])
    candidate_idx = next(i for i, t in enumerate(trials) if t["trial_id"] == "g_20_6_30")
    boot_res = bootstrap(matrix, days, candidate_index=candidate_idx)
    # map the winner index back to the trial row
    winner_trial = trials[boot_res["winner_index"]]
    candidate_trial = trials[boot_res["candidate_index"]]
    print("\nBootstrap")
    print(f"  max-mean trial: {winner_trial['trial_id']} ({winner_trial['label']})")
    print(f"  frozen candidate: {candidate_trial['trial_id']} "
          f"({candidate_trial['label']})")
    print(f"  candidate mean daily {boot_res['candidate_mean_daily']:+.6f}, "
          f"t = {boot_res['candidate_daily_t']:.3f} "
          f"(p {boot_res['candidate_t_one_sided_p']:.4f})")
    print(f"  RC p = {boot_res['rc_p']:.4f}  SPA p = {boot_res['spa_p']:.4f}")
    print(f"  candidate 98% CI mean daily = [{boot_res['candidate_ci98_low']:+.6f}, "
          f"{boot_res['candidate_ci98_high']:+.6f}]")

    write_matrix(Path(args.csv), Path(args.json), trials, sums, days, boot_res, full)

    stress = None
    if not args.skip_stress:
        stress = run_stress(args)
    else:
        stress = {"summaries": {}, "meta": {}, "day": "20261029", "records": 0}
    write_report(Path(args.md), args, full, trials, boot_res, stress)

    # console tables
    print("\nLATENCY SENSITIVITY (net PnL USD)")
    print(f"{'Config':<14} " + " ".join(f"{l:>9.0f}ms" for l in LATENCIES))
    for hs, sk, mp in TOP5:
        vals = []
        for lat in LATENCIES:
            key = (f"g_{int(hs)}_{int(sk)}_{int(mp)}" if lat == 66.0
                   else f"lat_{int(hs)}_{int(sk)}_{int(mp)}_{int(lat)}")
            vals.append(sums[key]["net_pnl"])
        print(f"{f'{hs:.0f}/{sk:.0f}/${mp:.0f}':<14} " + " ".join(f"{v:>+10.4f}" for v in vals))

    print("\nSTRICT HARD CAP (quote $10)")
    print(f"{'Config':<10} {'Cap':>4} {'Lat':>5} {'Net':>10} {'MaxDD':>8} "
          f"{'MaxInv':>8} {'CapOK':>6} {'Trades':>6} {'Trig':>5} {'Vetoes':>7}")
    for hs, sk in ((20.0, 6.0), (24.0, 14.0)):
        for cap in HARD_CAPS:
            for lat in (66.0, 300.0):
                key = f"cap_{int(hs)}_{int(sk)}_{int(cap)}_l{int(lat)}"
                s = sums[key]
                ok = "yes" if s["max_inv"] <= cap + 0.01 else "NO"
                print(f"{f'{hs:.0f}/{sk:.0f}':<10} {cap:>4.0f} {lat:>5.0f} "
                      f"{s['net_pnl']:>+10.4f} {s['max_dd']:>8.4f} "
                      f"{s['max_inv']:>8.4f} {ok:>6} {s['trades']:>6} "
                      f"{s['triggers']:>5} {s['cap_vetoes']:>7}")

    print("\nOCT 29 STRESS")
    for key, s in stress["summaries"].items():
        print(f"  {key:<14} net {s['net_pnl']:+.4f}  dd {s['max_dd']:.4f}  "
              f"maxInv {s['max_inv']:.4f}  trades {s['trades']}  trig {s['triggers']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
