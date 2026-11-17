#!/usr/bin/env python3
"""MM replay backtest with the modular cooldown plugins.

Research-only: no credentials, no network orders.

The harness deliberately does **not** edit ``scripts/replay_backtest.py`` or
``src/mmbot``.  It subclasses :class:`ReplayEngine` and drives the plugins through
their public API (``observe_equity`` / ``evaluate``), mirroring what
``strategies/market_maker/plugins/integration.py`` does for the live engine:

* when a plugin reports ``allowed=False`` the target quote set is emptied, so the
  normal replay path cancels resting orders and places nothing new;
* equity is marked to market every decision (cash + position x mid + funding - fees);
* each plugin uses a simulated wall clock, so its persisted cooldown timestamps
  advance with the replay data rather than real time.

This version runs **continuously** across the entire tick dataset (no daily UTC
resets), carrying inventory and equity across midnight and data gaps.  It tests
the rolling 60-minute MTM window plugins with two action modes:

* ``flatten``: close inventory at BBO (4.5 bps taker fee), then pause quoting
* ``reduce_only``: cancel risk-increasing side, keep risk-reducing side until flat

Protocol matches ``scripts/compare_strategies.py`` Strategy A:
HYPE, 16 bps half-spread, 6 bps skew, $15 quote / $60 max, cancel_rate 0.5,
66 ms latency, maker fee 1.5 bps, $30 starting equity.

Usage::

    venv/bin/python scripts/mm_cooldown_backtest.py \
        --start 20261020 --end 20261104 \
        --archive-dir data/archive_tars \
        --csv logs/mm_cooldown_backtest_20261105.csv \
        --json logs/mm_cooldown_backtest_20261105.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import tempfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from replay_backtest import (  # noqa: E402
    ReplayEngine,
    compute_quotes,
    gather_sources,
    iter_records,
    load_specs,
)
from strategies.market_maker.plugins.base import CooldownDecision, TriggerAction  # noqa: E402
from strategies.market_maker.plugins.cooldown_6h import Cooldown6h  # noqa: E402
from strategies.market_maker.plugins.cooldown_12h import Cooldown12h  # noqa: E402
from strategies.market_maker.plugins.cooldown_24h_4blocks import Cooldown24h4Blocks  # noqa: E402
from strategies.market_maker.plugins.rolling import (  # noqa: E402
    Rolling1h, Rolling3h, Rolling6h, Rolling12h,
)

COIN = "HYPE"
START_EQUITY = 30.0
MM_FEE_BPS = 1.5
TAKER_FEE_BPS = 4.5  # used for flatten-on-trigger fills

# Same execution/strategy assumptions as compare_strategies.py Strategy A.
MM_PARAMS = dict(
    half_spread_bps=16.0,
    skew_bps=6.0,
    quote_notional=15.0,
    max_notional=60.0,
    min_order_notional=10.0,
    latency_ms=66.0,
    feed_age_ms=66.0,
    cancel_rate=0.5,
    maker_fee_bps=MM_FEE_BPS,
)

# Variants: baseline (no cooldown) + rolling plugins with flatten/reduce_only
ROLLING_VARIANTS = {
    "baseline": {"label": "Baseline (no cooldown)", "cls": None, "action": None},
    "rolling_1h_flatten": {"label": "1h Flatten", "cls": Rolling1h, "action": "flatten"},
    "rolling_1h_reduce_only": {"label": "1h Reduce-Only", "cls": Rolling1h, "action": "reduce_only"},
    "rolling_3h_flatten": {"label": "3h Flatten", "cls": Rolling3h, "action": "flatten"},
    "rolling_3h_reduce_only": {"label": "3h Reduce-Only", "cls": Rolling3h, "action": "reduce_only"},
    "rolling_6h_flatten": {"label": "6h Flatten", "cls": Rolling6h, "action": "flatten"},
    "rolling_6h_reduce_only": {"label": "6h Reduce-Only", "cls": Rolling6h, "action": "reduce_only"},
    "rolling_12h_flatten": {"label": "12h Flatten", "cls": Rolling12h, "action": "flatten"},
    "rolling_12h_reduce_only": {"label": "12h Reduce-Only", "cls": Rolling12h, "action": "reduce_only"},
}

# Keep legacy variants for comparison if needed
LEGACY_VARIANTS = {
    "cooldown_6h": {"label": "Cooldown 6h", "cls": Cooldown6h, "action": "cancel_all"},
    "cooldown_12h": {"label": "Cooldown 12h", "cls": Cooldown12h, "action": "cancel_all"},
    "cooldown_24h_4blocks": {"label": "Cooldown 24h (4x6h)", "cls": Cooldown24h4Blocks, "action": "cancel_all"},
}


class SimClock:
    """Wall clock driven by the replayed event timestamp (epoch seconds)."""
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def day_keys(start_day: str, end_day: str) -> list[str]:
    d0 = datetime.strptime(start_day, "%Y%m%d").replace(tzinfo=timezone.utc)
    d1 = datetime.strptime(end_day, "%Y%m%d").replace(tzinfo=timezone.utc)
    out = []
    while d0 <= d1:
        out.append(d0.strftime("%Y%m%d"))
        d0 += timedelta(days=1)
    return out


def day_start_ts(day: str) -> float:
    return datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()


def round_trip_pnls_with_fees(fills_with_fees, maker_fee_bps: float, taker_fee_bps: float) -> list[float]:
    """FIFO round-trip PnL with per-fill fee rates (maker vs taker)."""
    lots = deque()
    out = []
    for fill, fee_bps in fills_with_fees:
        fee_rate = fee_bps / 1e4
        remaining = fill.sz if fill.side == "buy" else -fill.sz
        fee_unit = fill.px * fee_rate
        pnl = 0.0
        matched = 0.0
        while remaining != 0.0 and lots and lots[0][0] * remaining < 0:
            lot = lots[0]
            m = min(abs(remaining), abs(lot[0]))
            if remaining > 0:
                gross = (lot[1] - fill.px) * m
            else:
                gross = (fill.px - lot[1]) * m
            pnl += gross - (fee_unit + lot[2]) * m
            matched += m
            if abs(lot[0]) > m:
                lot[0] += m if lot[0] < 0 else -m
                remaining += m if remaining < 0 else -m
            else:
                remaining += lot[0]
                lots.popleft()
        if matched > 0:
            out.append(pnl)
        if remaining != 0.0:
            lots.append([remaining, fill.px, fee_unit])
    return out


def pf_and_win(pnls: list[float]) -> tuple[float, float, int, int]:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / len(pnls) * 100.0 if pnls else 0.0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    return pf, wr, len(wins), len(losses)


def max_drawdown(curve: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Return (max_dd_usd, max_dd_pct, peak, trough)."""
    peak = -math.inf
    best = (0.0, 0.0, 0.0, 0.0)
    peak_val = trough_val = 0.0
    for _ts, eq in curve:
        if eq > peak:
            peak = eq
            peak_val = eq
        dd = peak - eq
        if dd > best[0]:
            best = (dd, (dd / peak * 100.0) if peak > 0 else 0.0, peak_val, eq)
            trough_val = eq
    return best[0], best[1], best[2], trough_val


def worst_60m_window(curve: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Find the worst rolling 60-minute equity drop in the curve."""
    if len(curve) < 2:
        return (0.0, 0.0, 0.0, 0.0)
    worst = (0.0, 0.0, 0.0, 0.0)
    hwm = -math.inf
    hwm_ts = 0.0
    hwm_val = 0.0
    j = 0
    for i, (ts, eq) in enumerate(curve):
        while j < len(curve) and curve[j][0] <= ts - 3600:
            if curve[j][1] > hwm:
                hwm = curve[j][1]
                hwm_ts = curve[j][0]
                hwm_val = curve[j][1]
            j += 1
        if eq > hwm:
            hwm = eq
            hwm_ts = ts
            hwm_val = eq
        drop = hwm - eq
        if drop > worst[0]:
            worst = (drop, hwm_ts, ts, eq)
    return worst


class ContinuousCooldownEngine(ReplayEngine):
    """Engine that runs continuously across days with rolling MTM cooldown."""

    def __init__(
        self,
        spec,
        plugin=None,
        base_equity=START_EQUITY,
        curve_resolution_s=10.0,
        taker_fee_bps=TAKER_FEE_BPS,
        **kwargs,
    ):
        super().__init__(spec, **kwargs)
        self.plugin = plugin
        self.base_equity = base_equity
        self.curve_resolution_s = curve_resolution_s
        self.taker_fee_bps = float(taker_fee_bps)
        self._cash = 0.0
        self._fees = 0.0
        self._last_curve_t = -math.inf
        self.equity_curve = []
        self.cooldown_events = []
        self.blocked_decisions = 0
        self.decisions = 0
        self._last_cooldown_until = plugin.state.cooldown_until if plugin else 0.0
        # Track fills with their fee rates for round-trip calculation
        self.fills_with_fees = []  # list of (Fill, fee_bps)
        # Inventory tail risk sampling
        self.inventory_samples = []
        self._last_inventory_t = -math.inf

    def _fill(self, side, px, sz):
        super()._fill(side, px, sz)
        notional = px * sz
        self._cash += -notional if side == "buy" else notional
        fee = notional * self.maker_fee_bps / 1e4
        self._fees += fee
        self.fills_with_fees.append((self.fills[-1], self.maker_fee_bps))

    def _fill_taker(self, side, px, sz):
        """Execute a taker fill (for flatten-on-trigger) at the configured taker fee."""
        super()._fill(side, px, sz)
        notional = px * sz
        self._cash += -notional if side == "buy" else notional
        fee = notional * self.taker_fee_bps / 1e4
        self._fees += fee
        self.fills_with_fees.append((self.fills[-1], self.taker_fee_bps))

    def flatten_inventory(self):
        """Flatten open inventory at current BBO (taker fee)."""
        if abs(self.pos) < 1e-12:
            return False
        if self.pos > 0 and self.bid > 0:
            self._fill_taker("sell", self.bid, abs(self.pos))
            return True
        elif self.pos < 0 and self.ask > 0:
            self._fill_taker("buy", self.ask, abs(self.pos))
            return True
        return False

    def equity(self):
        mid = self.last_mid if self.last_mid > 0 else self.mid
        return self.base_equity + self._cash + self.pos * mid + self.funding_pnl - self._fees

    def _sample(self, t):
        if self.last_mid <= 0:
            return
        if self.equity_curve and t - self._last_curve_t < self.curve_resolution_s:
            return
        eq = self.equity()
        inv_notional = abs(self.pos * self.last_mid)
        self.equity_curve.append((t, eq))
        self.inventory_samples.append((t, inv_notional))
        self._last_curve_t = t
        self._last_inventory_t = t

    def _track_cooldown(self, decision, t):
        until = decision.cooldown_until
        if until == self._last_cooldown_until:
            return
        if until > 0 and until > self._last_cooldown_until:
            self.cooldown_events.append({
                "type": "arm",
                "time": t,
                "until": until,
                "loss_usd": round(decision.cumulative_loss_usd, 6),
                "action": decision.action,
                "block_index": decision.block_index,
                "blocks_total": decision.blocks_total,
            })
        elif until == 0 and self._last_cooldown_until > 0:
            self.cooldown_events.append({
                "type": "expire",
                "time": t,
                "until": 0.0,
                "loss_usd": round(decision.cumulative_loss_usd, 6),
                "action": decision.action,
                "block_index": decision.block_index,
                "blocks_total": decision.blocks_total,
            })
        self._last_cooldown_until = until

    def _apply_target_constraints(self, targets, m):
        """Last-mile target filter; subclasses may veto specific quote sides."""
        return targets

    def _maybe_decide(self, t):
        seen_t = t + self.current_feed_age_s
        if seen_t - self.last_decision_seen < self.params.min_requote_interval_s:
            return
        m = self.market_state(seen_t)
        if not m.is_valid:
            return
        self.last_decision_seen = seen_t
        self.stats.decisions += 1
        self.decisions += 1
        eq = self.equity()
        self._sample(t)

        blocked = False
        action = None
        if self.plugin is not None:
            self.plugin.observe_equity(eq)
            decision = self.plugin.evaluate()
            self._track_cooldown(decision, t)
            blocked = not decision.allowed
            action = decision.action
            if blocked:
                self.blocked_decisions += 1
                # Handle flatten-on-trigger
                if action == TriggerAction.FLATTEN.value and abs(self.pos) > 1e-12:
                    self.flatten_inventory()
                    # Cancel all resting quotes immediately
                    for side in ("buy", "sell"):
                        self.working[side] = None
                        self.pending_place[side] = None
                        self.pending_cancel[side] = False
                        # Also clear pending heap entries for this side
                        self.pending = [(e, s, k, p) for e, s, k, p in self.pending
                                        if not (k == "place" and p[0] == side and not isinstance(p, str))]
                    return

        # Determine targets based on action and blocked state
        targets = {}
        all_targets = {q.side.value: q for q in compute_quotes(m, self.pos, self.params, self.spec)}

        if not blocked:
            # Cooldown inactive: always resume normal two-sided quoting, even if
            # inventory is non-zero.  Reduce-only is a *during-cooldown* policy.
            targets = all_targets
        else:
            # Blocked by cooldown
            if action == TriggerAction.REDUCE_ONLY.value and abs(self.pos) > 1e-12:
                # In reduce_only cooldown but inventory not flat - keep reducing
                if self.pos > 0:
                    targets = {k: v for k, v in all_targets.items() if k == "sell"}
                elif self.pos < 0:
                    targets = {k: v for k, v in all_targets.items() if k == "buy"}
                # If flat (pos == 0), no targets (fully blocked)
            else:
                # flatten or cancel_all or reduce_only when flat - no targets
                targets = {}

        # Strategy-specific last-mile constraint (e.g. strict 1.0x equity cap).
        targets = self._apply_target_constraints(targets, m)

        eff_place = self.place_ready_time(seen_t)
        eff_park = self.park_ready_time(seen_t)
        for side in ("buy", "sell"):
            tgt = targets.get(side)
            w = self.working[side]
            if self.pending_cancel[side] and tgt is None:
                continue
            if tgt is None:
                if w is not None or self.pending_place[side] is not None:
                    self._schedule(eff_park, "cancel", side)
                continue
            px = float(tgt.price)
            sz = float(tgt.size)
            in_flight = self.pending_place[side]
            if w is None:
                if self.pending_cancel[side]:
                    continue
                if in_flight is not None:
                    moved = abs(px - in_flight[0]) / in_flight[0] * 1e4
                    if moved >= self.params.requote_threshold_bps:
                        self._schedule(eff_place, "cancel", side)
                        self._schedule(eff_place, "place", (side, px, sz))
                    continue
                self._schedule(eff_place, "place", (side, px, sz))
                continue
            if self.pending_cancel[side]:
                continue
            moved_bps = abs(px - w.px) / w.px * 1e4
            undersized = w.size < (1.0 - self.params.size_tolerance) * sz
            if moved_bps >= self.params.requote_threshold_bps or undersized:
                self._schedule(eff_place, "cancel", side)
                self._schedule(eff_place, "place", (side, px, sz))

    def finalize(self, last_t):
        if self.last_mid > 0:
            self.equity_curve.append((last_t, self.equity()))
        return self.compute_metrics()


def make_plugins(args, clock, tmp):
    plugins = {"baseline": None}
    for key, cfg in ROLLING_VARIANTS.items():
        if cfg["cls"] is None:
            continue
        plugins[key] = cfg["cls"](
            state_path=tmp / f"{key}.json",
            clock=clock,
            loss_threshold_usd=args.loss_usd,
            action=cfg["action"],
        )
    return plugins


def run(args):
    specs = load_specs(Path(args.universe))
    spec = specs[COIN]
    clock = SimClock()
    days = day_keys(args.start, args.end)

    # Collect all sources across all days for continuous execution
    all_paths = []
    all_tars = []
    available_days = []
    missing_days = []
    for day in days:
        start_key = int(day) * 100
        paths, tars = gather_sources(
            Path(args.data_dir), COIN, start_key, start_key + 23,
            archive_dir=Path(args.archive_dir) if args.archive_dir else None,
        )
        if not paths and not tars:
            missing_days.append(day)
            print(f"[skip] {day}: no shards", file=sys.stderr)
            continue
        available_days.append(day)
        all_paths.extend(paths)
        all_tars.extend(tars)

    if not available_days:
        print("No tick data available", file=sys.stderr)
        return None

    print(f"Loaded {len(all_paths)} local shards + {len(all_tars)} tar shards from {len(available_days)} days", file=sys.stderr)

    # Create one continuous engine per variant
    engines = {}
    with tempfile.TemporaryDirectory(prefix="mm_cooldown_bt_") as td:
        plugins = make_plugins(args, clock, Path(td))
        results = {
            key: {
                "key": key,
                "label": cfg["label"],
                "curve": [],
                "inventory_samples": [],
                "round_trips": [],
                "net": 0.0,
                "fees": 0.0,
                "funding": 0.0,
                "fills": 0,
                "places": 0,
                "cancels": 0,
                "volume": 0.0,
                "cooldown_events": [],
                "blocked_decisions": 0,
                "decisions": 0,
            }
            for key, cfg in ROLLING_VARIANTS.items()
        }

        for key, plugin in plugins.items():
            engines[key] = ContinuousCooldownEngine(
                spec,
                plugin=plugin,
                base_equity=START_EQUITY,
                curve_resolution_s=args.curve_resolution_s,
                **MM_PARAMS,
            )
            # Seed initial equity point
            engines[key].equity_curve.append((day_start_ts(available_days[0]), START_EQUITY))

        # Stream all records through all engines
        n_records = 0
        last_t = 0.0
        for rec in iter_records(all_paths, all_tars, COIN):
            t_ms = rec.get("t")
            if isinstance(t_ms, (int, float)):
                clock.now = float(t_ms) / 1000.0
                last_t = clock.now
            for eng in engines.values():
                eng.on_record(rec)
            n_records += 1
            if n_records % 50_000 == 0:
                for eng in engines.values():
                    eng.feed_ages_ms.clear()
            if n_records % 200_000 == 0:
                print(f"  {n_records:,} records...", file=sys.stderr)

        # Finalize all engines
        for key, eng in engines.items():
            metrics = eng.finalize(last_t)
            rts = round_trip_pnls_with_fees(eng.fills_with_fees, MM_FEE_BPS, TAKER_FEE_BPS)
            r = results[key]
            r["round_trips"] = rts
            r["net"] = eng.equity() - START_EQUITY
            r["fees"] = eng._fees
            r["funding"] = eng.funding_pnl
            r["fills"] = len(eng.fills)
            r["places"] = eng.stats.places
            r["cancels"] = eng.stats.cancels
            r["volume"] = sum(f.px * f.sz for f in eng.fills)
            r["blocked_decisions"] = eng.blocked_decisions
            r["decisions"] = eng.decisions
            r["cooldown_events"] = eng.cooldown_events
            r["curve"] = eng.equity_curve
            r["inventory_samples"] = eng.inventory_samples
            r["max_abs_inventory_usd"] = eng.max_abs_inventory_usd
            r["ending_inventory_usd"] = eng.pos * eng.last_mid

            # Inventory tail metrics
            inv_samples = [v for _, v in r["inventory_samples"]]
            if inv_samples:
                r["avg_inventory_usd"] = sum(inv_samples) / len(inv_samples)
                inv_sorted = sorted(inv_samples)
                r["p95_inventory_usd"] = inv_sorted[int(0.95 * len(inv_sorted))]
            else:
                r["avg_inventory_usd"] = 0.0
                r["p95_inventory_usd"] = 0.0

    # Summarize
    for r in results.values():
        pf, wr, wins, losses = pf_and_win(r["round_trips"])
        dd, ddp, peak, trough = max_drawdown(r["curve"])
        w60 = worst_60m_window(r["curve"])

        # Find worst day (equity drop within one calendar day)
        if r["curve"]:
            daily_equity = {}
            for ts, eq in r["curve"]:
                day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
                if day not in daily_equity or eq < daily_equity[day]:
                    daily_equity[day] = eq
            # Worst day is the day with lowest equity, but we want the biggest drop
            # Approximate: find max peak-to-trough within 24h windows
            worst_24h_dd = 0.0
            worst_24h_day = ""
            for i, (ts1, eq1) in enumerate(r["curve"]):
                for j in range(i + 1, len(r["curve"])):
                    ts2, eq2 = r["curve"][j]
                    if ts2 - ts1 > 86400:
                        break
                    dd_24h = eq1 - eq2
                    if dd_24h > worst_24h_dd:
                        worst_24h_dd = dd_24h
                        worst_24h_day = datetime.fromtimestamp(ts2, tz=timezone.utc).strftime("%Y%m%d")
        else:
            worst_24h_dd = 0.0
            worst_24h_day = ""

        # Oct 29 specific
        oct29_curve = [(ts, eq) for ts, eq in r["curve"]
                       if day_start_ts("20261029") <= ts < day_start_ts("20261030")]
        oct29_dd, oct29_ddp, _, _ = max_drawdown(oct29_curve) if oct29_curve else (0.0, 0.0, 0.0, 0.0)
        # Oct 29 net = equity at end of Oct 29 - equity at start
        if oct29_curve:
            oct29_net = oct29_curve[-1][1] - oct29_curve[0][1]
        else:
            oct29_net = 0.0

        # Oct 25 specific (as mentioned in problem statement)
        oct25_curve = [(ts, eq) for ts, eq in r["curve"]
                       if day_start_ts("20261025") <= ts < day_start_ts("20261026")]
        oct25_dd, oct25_ddp, _, _ = max_drawdown(oct25_curve) if oct25_curve else (0.0, 0.0, 0.0, 0.0)
        if oct25_curve:
            oct25_net = oct25_curve[-1][1] - oct25_curve[0][1]
        else:
            oct25_net = 0.0

        trades = len(r["round_trips"])
        trigger_count = sum(1 for e in r["cooldown_events"] if e["type"] == "arm")

        r.update({
            "pf": pf,
            "win_rate": wr,
            "wins": wins,
            "losses": losses,
            "trades": trades,
            "max_dd_usd": dd,
            "max_dd_pct": ddp,
            "dd_peak": peak,
            "dd_trough": trough,
            "worst_60m_drop": w60[0],
            "worst_60m_hwm_ts": w60[1],
            "worst_60m_trough_ts": w60[2],
            "worst_60m_trough_eq": w60[3],
            "worst_24h_dd": worst_24h_dd,
            "worst_24h_day": worst_24h_day,
            "oct25_net": oct25_net,
            "oct25_dd": oct25_dd,
            "oct29_net": oct29_net,
            "oct29_dd": oct29_dd,
            "trigger_count": trigger_count,
            "blocked_share": (r["blocked_decisions"] / r["decisions"] * 100.0)
            if r["decisions"] else 0.0,
        })
        r["net_to_dd"] = (r["net"] / r["max_dd_usd"]) if r["max_dd_usd"] > 0 else float("inf")

    return {
        "window": f"{args.start}..{args.end}",
        "available_days": available_days,
        "missing_days": missing_days,
        "params": MM_PARAMS,
        "start_equity": START_EQUITY,
        "loss_usd": args.loss_usd,
        "variants": results,
    }


def fmt_pf(v):
    return "inf" if not math.isfinite(v) else f"{v:.3f}"


def print_report(payload):
    print("=" * 130)
    print(" MARKET-MAKER COOLDOWN BACKTEST (CONTINUOUS) — HYPE (16 bps, cancel_rate 0.5, 66 ms)")
    print("=" * 130)
    print(f"Window requested : {payload['window']}")
    print(f"Tick days used   : {len(payload['available_days'])} days ({', '.join(payload['available_days'][:5])}...)")
    if payload["missing_days"]:
        print(f"Missing tick days: {', '.join(payload['missing_days'])}")
    print(f"Start equity     : ${payload['start_equity']:.2f} | loss threshold ${payload['loss_usd']:.2f} (rolling 60m MTM)")
    print(f"Params           : {payload['params']}")
    print()

    # Main comparison table
    header = (
        f"{'Variant':<24} {'Trades':>7} {'Fills':>7} {'Win%':>7} {'PF':>7} {'NetPnL':>10} "
        f"{'MaxDD$':>8} {'MaxDD%':>7} {'Net/DD':>8} {'Worst60m':>9} {'Oct25':>10} {'Oct25DD':>9} "
        f"{'Oct29':>10} {'Oct29DD':>9} {'Triggers':>9}"
    )
    print(header)
    print("-" * len(header))

    order = ["baseline",
             "rolling_1h_flatten", "rolling_1h_reduce_only",
             "rolling_3h_flatten", "rolling_3h_reduce_only",
             "rolling_6h_flatten", "rolling_6h_reduce_only",
             "rolling_12h_flatten", "rolling_12h_reduce_only"]

    for key in order:
        r = payload["variants"][key]
        print(
            f"{r['label']:<24} {r['trades']:>7} {r['fills']:>7} {r['win_rate']:>6.2f}% {fmt_pf(r['pf']):>7} "
            f"{r['net']:>+10.4f} {r['max_dd_usd']:>8.4f} {r['max_dd_pct']:>6.2f}% "
            f"{r['net_to_dd']:>8.3f} {r['worst_60m_drop']:>9.4f} "
            f"{r['oct25_net']:>+10.4f} {r['oct25_dd']:>9.4f} "
            f"{r['oct29_net']:>+10.4f} {r['oct29_dd']:>9.4f} "
            f"{r['trigger_count']:>9}"
        )
    print()

    # Inventory tail risk table
    print("Inventory Tail Risk")
    print("-" * 130)
    inv_header = f"{'Variant':<24} {'Max |Inv|':>12} {'Avg |Inv|':>12} {'P95 |Inv|':>12} {'Ending Inv':>12}"
    print(inv_header)
    print("-" * 130)
    for key in order:
        r = payload["variants"][key]
        print(
            f"{r['label']:<24} {r['max_abs_inventory_usd']:>12.4f} {r['avg_inventory_usd']:>12.4f} "
            f"{r['p95_inventory_usd']:>12.4f} {r['ending_inventory_usd']:>+12.4f}"
        )
    print()

    # Execution details
    print("Execution Details")
    print("-" * 130)
    exec_header = f"{'Variant':<24} {'Places':>8} {'Cancels':>8} {'Volume':>12} {'Fees':>10} {'Funding':>10} {'Blocked%':>10}"
    print(exec_header)
    print("-" * 130)
    for key in order:
        r = payload["variants"][key]
        print(
            f"{r['label']:<24} {r['places']:>8} {r['cancels']:>8} {r['volume']:>12.2f} "
            f"{r['fees']:>10.4f} {r['funding']:>+10.4f} {r['blocked_share']:>9.1f}%"
        )
    print()

    # Cooldown events
    print("Cooldown Events")
    print("-" * 130)
    for key in order:
        r = payload["variants"][key]
        arms = [e for e in r["cooldown_events"] if e["type"] == "arm"]
        if not arms:
            continue
        print(f"\n{r['label']}: {len(arms)} armed windows")
        for e in arms[:6]:
            when = datetime.fromtimestamp(e["time"], tz=timezone.utc).strftime("%m-%d %H:%M")
            until = datetime.fromtimestamp(e["until"], tz=timezone.utc).strftime("%m-%d %H:%M")
            print(f"  {when} UTC loss ${e['loss_usd']:+.4f} action={e['action']} until {until}")
        if len(arms) > 6:
            print(f"  ... {len(arms) - 6} more")


def write_csv(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "variant", "trades", "fills", "win_rate", "pf", "net_pnl", "max_dd_usd", "max_dd_pct",
        "net_to_dd", "worst_60m_drop", "oct25_net", "oct25_dd", "oct29_net", "oct29_dd",
        "max_abs_inventory_usd", "avg_inventory_usd", "p95_inventory_usd", "ending_inventory_usd",
        "places", "cancels", "volume", "fees", "funding", "blocked_share", "trigger_count",
    ]
    order = ["baseline",
             "rolling_1h_flatten", "rolling_1h_reduce_only",
             "rolling_3h_flatten", "rolling_3h_reduce_only",
             "rolling_6h_flatten", "rolling_6h_reduce_only",
             "rolling_12h_flatten", "rolling_12h_reduce_only"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for key in order:
            r = payload["variants"][key]
            w.writerow({
                "variant": key,
                "trades": r["trades"],
                "fills": r["fills"],
                "win_rate": f"{r['win_rate']:.2f}",
                "pf": fmt_pf(r["pf"]),
                "net_pnl": f"{r['net']:.4f}",
                "max_dd_usd": f"{r['max_dd_usd']:.4f}",
                "max_dd_pct": f"{r['max_dd_pct']:.2f}",
                "net_to_dd": f"{r['net_to_dd']:.3f}",
                "worst_60m_drop": f"{r['worst_60m_drop']:.4f}",
                "oct25_net": f"{r['oct25_net']:.4f}",
                "oct25_dd": f"{r['oct25_dd']:.4f}",
                "oct29_net": f"{r['oct29_net']:.4f}",
                "oct29_dd": f"{r['oct29_dd']:.4f}",
                "max_abs_inventory_usd": f"{r['max_abs_inventory_usd']:.4f}",
                "avg_inventory_usd": f"{r['avg_inventory_usd']:.4f}",
                "p95_inventory_usd": f"{r['p95_inventory_usd']:.4f}",
                "ending_inventory_usd": f"{r['ending_inventory_usd']:.4f}",
                "places": r["places"],
                "cancels": r["cancels"],
                "volume": f"{r['volume']:.2f}",
                "fees": f"{r['fees']:.4f}",
                "funding": f"{r['funding']:.4f}",
                "blocked_share": f"{r['blocked_share']:.1f}",
                "trigger_count": r["trigger_count"],
            })
    print(f"[saved] {path}")


def build_parser():
    p = argparse.ArgumentParser(description="MM replay with rolling cooldown plugins (continuous)")
    p.add_argument("--start", default="20261020")
    p.add_argument("--end", default="20261104")
    p.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    p.add_argument("--archive-dir", default=str(PROJECT_ROOT / "data" / "archive_tars"))
    p.add_argument("--universe", default=str(PROJECT_ROOT / "data" / "universe.json"))
    p.add_argument("--loss-usd", type=float, default=0.25)
    p.add_argument("--curve-resolution-s", type=float, default=10.0)
    p.add_argument("--csv", default=None)
    p.add_argument("--json", default=None)
    p.add_argument("--max-days", type=int, default=None, help="smoke-test only")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.max_days:
        days = day_keys(args.start, args.end)[: args.max_days]
        if days:
            args.start, args.end = days[0], days[-1]
    payload = run(args)
    if payload is None:
        return 1
    print_report(payload)
    if args.csv:
        write_csv(Path(args.csv), payload)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(payload, indent=2, allow_nan=True), encoding="utf-8"
        )
        print(f"[saved] {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())