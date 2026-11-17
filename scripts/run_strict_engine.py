#!/usr/bin/env python3
"""Strict-rule replay for the PREREG2 v2 candidate (research only).

This replaces the previous version, whose taker-flatten path was dead code:
``_schedule`` only understood "place"/"cancel", and ``_advance_to`` never called
``_execute``, so a scheduled "taker_sell"/"taker_buy" was silently converted into
a cancel.  The corrected engine:

* enforces the $30 pre-place soft cap with a **veto-path purge** of queued places;
* schedules an emergency taker flatten at ``seen_t + p95_latency`` when either
  the $40 hard inventory cap or the $0.50 rolling hard stop trips;
* executes that flatten against the BBO **at the future timestamp**, charging
  the taker fee;
* supports a soft-cap-only reference mode so the previously reported
  ``-$0.3226`` can be reconciled.

No credentials, no orders, no edits under ``src/mmbot``.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT, PROJECT_ROOT / "scripts", PROJECT_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from mm_cooldown_backtest import (  # noqa: E402
    COIN,
    START_EQUITY,
    TAKER_FEE_BPS,
    ContinuousCooldownEngine,
    day_keys,
    round_trip_pnls_with_fees,
)
import mm_grid_search as mgs  # noqa: E402
from replay_backtest import load_specs  # noqa: E402
from strategies.market_maker.plugins.rolling import Rolling3h  # noqa: E402


class StrictRuleEngine(ContinuousCooldownEngine):
    """Continuous engine with the frozen v2 rule set.

    ``soft_cap_usd`` is the pre-place exposure ceiling.  ``hard_cap_usd`` and
    ``hard_stop_usd`` trigger a delayed taker flatten; they never veto quotes
    directly.
    """

    def __init__(
        self,
        *args,
        soft_cap_usd: float = 30.0,
        hard_cap_usd: float = 40.0,
        hard_stop_usd: float = 0.50,
        p95_latency_s: float = 0.150,
        full_rules: bool = True,
        immediate_flatten: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.soft_cap_usd = float(soft_cap_usd)
        self.hard_cap_usd = float(hard_cap_usd)
        self.hard_stop_usd = float(hard_stop_usd)
        self.p95_latency_s = float(p95_latency_s)
        self.full_rules = bool(full_rules)
        # Diagnostic only: fill at the current BBO instead of at the future
        # seen_t + p95 stamp.  The frozen rule set always uses False.
        self.immediate_flatten = bool(immediate_flatten)
        self.soft_vetoes = 0
        self.hard_cap_triggers = 0
        self.hard_stop_triggers = 0
        self.taker_flattens = 0
        self.taker_volume_usd = 0.0
        self._taker_pending = False

    # ---- soft cap, with veto-path purge -----------------------------------
    def _purge_pending_place(self, side: str) -> None:
        """Drop a queued risk-increasing place, not just its cancel marker."""
        self.pending = [
            entry for entry in self.pending
            if not (entry[2] == "place" and entry[3][0] == side)
        ]
        self.pending_place[side] = None

    def _apply_target_constraints(self, targets, m):
        if m.mid <= 0:
            return targets
        pos_n = abs(self.pos * m.mid)
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
            if pos_n + pending + quote_n <= self.soft_cap_usd + 1e-9:
                out[side] = tgt
            else:
                self.soft_vetoes += 1
                self._purge_pending_place(side)
        return out

    # ---- delayed taker flatten --------------------------------------------
    def _schedule_taker_flatten(self, seen_t: float) -> bool:
        if abs(self.pos) < 1e-12 or self._taker_pending:
            return False
        park_eff = self.park_ready_time(seen_t)
        # Pull quotes first so we do not keep filling while the flatten is in
        # flight.  A fast-path OTM park (batchModify) overtakes a raw cancel.
        for side in ("buy", "sell"):
            if self.working[side] is not None or self.pending_place[side] is not None:
                self._purge_pending_place(side)
                self._schedule(park_eff, "cancel", side)
        # The taker order cannot land before the pull that clears the book.
        eff = max(seen_t + self.p95_latency_s, park_eff)
        self._seq += 1
        heapq.heappush(self.pending, (eff, self._seq, "taker", abs(self.pos)))
        self._taker_pending = True
        return True

    def _execute_taker(self) -> None:
        self._taker_pending = False
        size = abs(self.pos)
        if size < 1e-12:
            return
        if self.pos > 0 and self.bid > 0:
            self._fill_taker("sell", self.bid, size)
        elif self.pos < 0 and self.ask > 0:
            self._fill_taker("buy", self.ask, size)
        else:
            return
        self.taker_flattens += 1
        self.taker_volume_usd += size * (self.last_mid or 0.0)

    def _advance_to(self, t: float) -> None:
        while self.pending and self.pending[0][0] <= t:
            _, _, kind, payload = heapq.heappop(self.pending)
            if kind == "place":
                side, px, size = payload  # type: ignore[misc]
                self._apply_place(side, px, size)
            elif kind == "taker":
                self._execute_taker()
            else:
                self._apply_cancel(payload)  # type: ignore[arg-type]

    def _flatten_now(self) -> bool:
        """Diagnostic immediate flatten at the current BBO (no latency wait)."""
        size = abs(self.pos)
        if size < 1e-12:
            return False
        for side in ("buy", "sell"):
            self._purge_pending_place(side)
        if self.pos > 0 and self.bid > 0:
            self._fill_taker("sell", self.bid, size)
        elif self.pos < 0 and self.ask > 0:
            self._fill_taker("buy", self.ask, size)
        else:
            return False
        self.taker_flattens += 1
        self.taker_volume_usd += size * (self.last_mid or 0.0)
        return True

    # ---- decisions ---------------------------------------------------------
    def _maybe_decide(self, t: float) -> None:
        seen_t = t + self.current_feed_age_s
        if self.full_rules:
            inv = abs(self.pos * self.last_mid)
            if inv >= self.hard_cap_usd:
                if self.immediate_flatten:
                    if self._flatten_now():
                        self.hard_cap_triggers += 1
                elif self._schedule_taker_flatten(seen_t):
                    self.hard_cap_triggers += 1
                return
            if self.plugin is not None and self.plugin.rolling_loss_usd() >= self.hard_stop_usd:
                if self.immediate_flatten:
                    if self._flatten_now():
                        self.hard_stop_triggers += 1
                elif self._schedule_taker_flatten(seen_t):
                    self.hard_stop_triggers += 1
                return
        super()._maybe_decide(t)

    # ---- reporting ---------------------------------------------------------
    def report(self, last_t: float) -> dict:
        self.finalize(last_t)
        eq = self.equity()
        peak = -float("inf")
        max_dd = 0.0
        for _, e in self.equity_curve:
            peak = max(peak, e)
            max_dd = max(max_dd, peak - e)
        return {
            "net_pnl": eq - START_EQUITY,
            "equity": eq,
            "fees": self._fees,
            "funding": self.funding_pnl,
            "fills": len(self.fills),
            "places": self.stats.places,
            "cancels": self.stats.cancels,
            "trades": len(round_trip_pnls_with_fees(
                self.fills_with_fees, self.maker_fee_bps, self.taker_fee_bps)),
            "max_dd": max_dd,
            "max_inv_usd": self.max_abs_inventory_usd,
            "ending_inv_usd": self.pos * (self.last_mid or 0.0),
            "soft_vetoes": self.soft_vetoes,
            "hard_cap_triggers": self.hard_cap_triggers,
            "hard_stop_triggers": self.hard_stop_triggers,
            "taker_flattens": self.taker_flattens,
            "taker_volume_usd": self.taker_volume_usd,
            "triggers": sum(1 for e in self.cooldown_events if e["type"] == "arm"),
        }


def build_engine(spec, latency_ms: float, p95_ms: float, full_rules: bool,
                 clock_ref, immediate: bool = False, *,
                 feed_age_ms: float | None = None,
                 feed_age_from_record: bool = True,
                 place_modify_latency_ms: float | None = None,
                 cancel_latency_ms: float | None = None,
                 park_via_modify: bool = False,
                 half_spread_bps: float = 20.0,
                 skew_bps: float = 6.0,
                 requote_threshold_bps: float = 1.5,
                 min_requote_interval_s: float = 0.3,
                 quote_notional: float = 10.0,
                 max_notional: float = 30.0,
                 min_order_notional: float = 5.0):
    """Build one strict engine.

    ``latency_ms`` is the backward-compatible fallback used for any of the
    three split timings that is not supplied explicitly.
    """
    place_ms = latency_ms if place_modify_latency_ms is None else place_modify_latency_ms
    cancel_ms = latency_ms if cancel_latency_ms is None else cancel_latency_ms
    feed_ms = latency_ms if feed_age_ms is None else feed_age_ms
    plugin = Rolling3h(
        clock=lambda: clock_ref[0],
        loss_threshold_usd=0.25,
        action="reduce_only",
        persist=False,
    )
    params = dict(
        half_spread_bps=half_spread_bps,
        skew_bps=skew_bps,
        quote_notional=quote_notional,
        max_notional=max_notional,
        min_order_notional=min_order_notional,
        requote_threshold_bps=requote_threshold_bps,
        min_requote_interval_s=min_requote_interval_s,
        latency_ms=place_ms,
        feed_age_ms=feed_ms,
        feed_age_from_record=feed_age_from_record,
        place_modify_latency_ms=place_ms,
        cancel_latency_ms=cancel_ms,
        park_via_modify=park_via_modify,
        cancel_rate=0.5,
    )
    return StrictRuleEngine(
        spec,
        plugin=plugin,
        soft_cap_usd=30.0,
        hard_cap_usd=40.0,
        hard_stop_usd=0.50,
        p95_latency_s=p95_ms / 1000.0,
        full_rules=full_rules,
        immediate_flatten=immediate,
        taker_fee_bps=TAKER_FEE_BPS,
        **params,
    )


def build_parser():
    p = argparse.ArgumentParser(description="strict v2 rule replay (research only)")
    p.add_argument("--start", default="20261020")
    p.add_argument("--end", default="20261105")
    p.add_argument("--p50-ms", type=float, default=66.0,
                   help="backward-compatible fallback for all p50 L1 timings")
    p.add_argument("--p95-ms", type=float, default=150.0,
                   help="fallback for all p95 L1 timings and the taker flatten")
    p.add_argument("--feed-age-ms", type=float, default=None,
                   help="market-data transit latency (p50); default = --p50-ms")
    p.add_argument("--feed-age-p95-ms", type=float, default=None,
                   help="market-data transit latency (p95); default = --feed-age-ms")
    p.add_argument("--feed-age-source", choices=("recorded", "fixed"), default="recorded",
                   help="use each record's r-t receive age, or force --feed-age-ms")
    p.add_argument("--place-modify-latency-ms", type=float, default=None,
                   help="fast-path Alo place / batchModify latency (p50); default = --p50-ms")
    p.add_argument("--place-modify-p95-ms", type=float, default=None,
                   help="fast-path Alo place / batchModify latency (p95); default = p50 value")
    p.add_argument("--cancel-latency-ms", type=float, default=None,
                   help="raw bulk-cancel latency (p50); default = --p50-ms")
    p.add_argument("--cancel-p95-ms", type=float, default=None,
                   help="raw bulk-cancel latency (p95); default = p50 value")
    p.add_argument("--park-via-modify", action="store_true",
                   help="model risk-off pulls as an OTM park via batchModify (fast path)")
    p.add_argument("--half-spread-bps", type=float, default=20.0)
    p.add_argument("--skew-bps", type=float, default=6.0)
    p.add_argument("--requote-threshold-bps", type=float, default=1.5)
    p.add_argument("--min-requote-interval-s", type=float, default=0.3)
    p.add_argument("--quote-notional", type=float, default=10.0)
    p.add_argument("--max-notional", type=float, default=30.0)
    p.add_argument("--min-order-notional", type=float, default=5.0)
    p.add_argument("--out", default=str(PROJECT_ROOT / "logs" / "mm_strict_v2_baseline_20261106.json"))
    p.add_argument("--variants", default="full_p50,full_p95,soft_only_p50",
                   help="comma-separated: full_p50, full_p95, soft_only_p50, "
                        "immediate_p50 (diagnostic)")
    return p


def _resolve_p95(p95_value, p50_value, fallback):
    if p95_value is not None:
        return p95_value
    if p50_value is not None:
        return p50_value
    return fallback


def main(argv=None):
    args = build_parser().parse_args(argv)
    specs = load_specs(Path("data/universe.json"))
    spec = specs[COIN]
    days = day_keys(args.start, args.end)
    data_dir = PROJECT_ROOT / "data"
    archive_dir = data_dir / "archive_tars"

    feed_p50 = args.p50_ms if args.feed_age_ms is None else args.feed_age_ms
    feed_p95 = _resolve_p95(args.feed_age_p95_ms, args.feed_age_ms, args.p95_ms)
    place_p50 = args.p50_ms if args.place_modify_latency_ms is None else args.place_modify_latency_ms
    place_p95 = _resolve_p95(args.place_modify_p95_ms, args.place_modify_latency_ms, args.p95_ms)
    cancel_p50 = args.p50_ms if args.cancel_latency_ms is None else args.cancel_latency_ms
    cancel_p95 = _resolve_p95(args.cancel_p95_ms, args.cancel_latency_ms, args.p95_ms)

    def make(level: str, full_rules: bool, immediate: bool = False):
        if level == "p50":
            base, feed, place, cancel = args.p50_ms, feed_p50, place_p50, cancel_p50
        else:
            base, feed, place, cancel = args.p95_ms, feed_p95, place_p95, cancel_p95
        return build_engine(
            spec, base, args.p95_ms, full_rules, clock_ref, immediate=immediate,
            feed_age_ms=feed, feed_age_from_record=(args.feed_age_source == "recorded"),
            place_modify_latency_ms=place,
            cancel_latency_ms=cancel, park_via_modify=args.park_via_modify,
            half_spread_bps=args.half_spread_bps, skew_bps=args.skew_bps,
            requote_threshold_bps=args.requote_threshold_bps,
            min_requote_interval_s=args.min_requote_interval_s,
            quote_notional=args.quote_notional, max_notional=args.max_notional,
            min_order_notional=args.min_order_notional,
        )

    clock_ref = [0.0]
    builders = {
        # The frozen rule set at the mapped p50 and p95 order-path delays.
        "full_p50": lambda: make("p50", True),
        "full_p95": lambda: make("p95", True),
        # Reference: soft cap only (the old reported -$0.3226 cell).
        "soft_only_p50": lambda: make("p50", False),
        # Diagnostic: same rules, but flatten at the current BBO instead of
        # waiting seen_t + p95.  Never used for the frozen run.
        "immediate_p50": lambda: make("p50", True, immediate=True),
    }
    wanted = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = [v for v in wanted if v not in builders]
    if unknown:
        raise SystemExit(f"unknown variants: {unknown}")
    engines = {v: builders[v]() for v in wanted}

    print(f"days {days[0]}..{days[-1]} ({len(days)})  engines={len(engines)}  "
          f"feed p50/p95={feed_p50:.0f}/{feed_p95:.0f}ms  "
          f"place-modify={place_p50:.0f}/{place_p95:.0f}ms  "
          f"cancel={cancel_p50:.0f}/{cancel_p95:.0f}ms  "
          f"feed_source={args.feed_age_source}  "
          f"park_via_modify={args.park_via_modify}  "
          f"hs={args.half_spread_bps:.1f}bps", flush=True)
    n = 0
    last_t = 0.0
    t0 = time.time()
    for rec in mgs.iter_records_chrono(data_dir, archive_dir, COIN, days):
        t_ms = rec.get("t")
        if isinstance(t_ms, (int, float)):
            last_t = float(t_ms) / 1000.0
            clock_ref[0] = last_t
        for eng in engines.values():
            eng.on_record(rec)
        n += 1
        if n % 50_000 == 0:
            for eng in engines.values():
                if eng.feed_ages_ms:
                    eng.feed_ages_ms.clear()
        if n % 1_000_000 == 0:
            print(f"  {n:,} records ({n / (time.time() - t0):,.0f}/s)", flush=True)

    results = {name: eng.report(last_t) for name, eng in engines.items()}
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": f"{days[0]}..{days[-1]}",
        "days": len(days),
        "records": n,
        "p50_ms": args.p50_ms,
        "p95_ms": args.p95_ms,
        "timings": {
            "feed_age_ms": {"p50": feed_p50, "p95": feed_p95},
            "place_modify_latency_ms": {"p50": place_p50, "p95": place_p95},
            "cancel_latency_ms": {"p50": cancel_p50, "p95": cancel_p95},
            "feed_age_source": args.feed_age_source,
            "park_via_modify": bool(args.park_via_modify),
        },
        "params": {
            "half_spread_bps": args.half_spread_bps, "skew_bps": args.skew_bps,
            "requote_threshold_bps": args.requote_threshold_bps,
            "min_requote_interval_s": args.min_requote_interval_s,
            "quote_notional": args.quote_notional, "max_notional": args.max_notional,
            "min_order_notional": args.min_order_notional,
            "soft_cap_usd": 30.0, "hard_cap_usd": 40.0,
            "hard_stop_usd": 0.50, "taker_fee_bps": TAKER_FEE_BPS,
            "cooldown": "rolling_3h reduce_only, 3600s, $0.25",
        },
        "results": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[saved] {out}")
    for name, r in results.items():
        print(f"{name:<15} net {r['net_pnl']:+.4f}  dd {r['max_dd']:.4f}  "
              f"maxInv {r['max_inv_usd']:.4f}  hardCap {r['hard_cap_triggers']}  "
              f"hardStop {r['hard_stop_triggers']}  flattens {r['taker_flattens']}  "
              f"trades {r['trades']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
