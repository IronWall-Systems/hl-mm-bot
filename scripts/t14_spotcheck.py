#!/usr/bin/env python3
"""T14 item 1 — traced replay of HYPE/XMR hs16 lat500 cr0 over all 9 days.

Records placements (px, size, queue_ahead), trade hits against the resting order,
and fills; dumps JSON for the round-trip spot check. Resumable per coin.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from replay_backtest import ReplayEngine, iter_records, load_specs, resolve_range, shard_paths  # noqa: E402


class TracedEngine(ReplayEngine):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.places: list[dict] = []
        self.hits: list[dict] = []
        self.traced_fills: list[dict] = []
        self._wid = 0
        self._cur: dict | None = None

    def _apply_place(self, side, px, size):
        wid = self._wid
        super()._apply_place(side, px, size)
        w = self.working[side]
        if w is not None:
            self._wid += 1
            self._cur = {"wid": wid, "side": side, "px": px, "size": size,
                         "queue_ahead": w.queue_ahead, "t": self.exch_ts}
            self.places.append(self._cur)

    def _on_trade(self, t, rec):
        w = self.working.get("buy") if rec.get("side") == "A" else self.working.get("sell")
        if w is not None:
            try:
                px, sz = float(rec["px"]), float(rec["sz"])
            except (KeyError, TypeError, ValueError):
                px = sz = None
            if px is not None:
                hit = px <= w.px if w.side == "buy" else px >= w.px
                if hit:
                    self.hits.append({
                        "wid": self._cur["wid"] if self._cur and self._cur["px"] == w.px
                        and self._cur["side"] == w.side else None,
                        "t": t, "px": px, "sz": sz, "trade_side": rec.get("side"),
                        "queue_before": w.queue_ahead, "w_px": w.px,
                    })
        super()._on_trade(t, rec)

    def _fill(self, side, px, sz):
        w = self.working[side]
        q_before = w.queue_ahead
        wid = self._cur["wid"] if self._cur and self._cur["px"] == w.px and self._cur["side"] == side else None
        super()._fill(side, px, sz)
        self.traced_fills.append({
            "wid": wid, "t": self.exch_ts, "side": side, "px": px, "sz": sz,
            "mid": self.last_mid, "queue_before_fill": q_before,
        })


def main() -> int:
    coin = sys.argv[1]
    out = ROOT / f"data/t14_trace_{coin}.json"
    if out.exists():
        print(f"{coin} trace exists, skip")
        return 0
    specs = load_specs(ROOT / "data/universe.json")
    start, end = resolve_range("20261006", "20261014")
    paths = shard_paths(ROOT / "data", coin, start, end)
    eng = TracedEngine(specs[coin], half_spread_bps=16.0, latency_ms=500.0,
                       feed_age_ms=400.0, cancel_rate=0.0)
    t0 = time.time()
    n = 0
    for rec in iter_records(paths, [], coin):
        eng.on_record(rec)
        n += 1
    m = eng.compute_metrics()
    out.write_text(json.dumps({
        "coin": coin, "recs": n, "elapsed_s": round(time.time() - t0, 1),
        "places": eng.places, "hits": eng.hits, "fills": eng.traced_fills,
        "metrics": {k: m[k] for k in ("fills", "round_trip_spread_pnl", "inventory_mtm_pnl",
                                      "net_pnl_usd", "round_trip_count", "total_fees_usd")},
    }))
    print(f"{coin} traced recs={n:,} fills={len(eng.traced_fills)} places={len(eng.places)} "
          f"{time.time()-t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
