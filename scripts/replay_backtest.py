#!/usr/bin/env python3
"""T11 — offline replay market-making backtester over the T10 collector's compact shards.

Reads `data/YYYYMMDD/HH_<COIN>.jsonl.gz` shards (or the members of `data_YYYYMMDD.tar`
archives) and replays them through the *real* quoter (`src/mmbot/strategy/quoter.py`
`compute_quotes`) with latency, ALO clamping and a top-of-book queue model.

Time model
----------
* Records carry only the exchange stamp ``t`` (ms). The collector did NOT record a
  receive stamp, so the bot's view time is modelled as ``t_seen = t + feed_age_s``
  (``--feed-age-ms``, default 400 — UNVERIFIED, sized from the T7d fast-book mainnet
  measurements, A-16..A-18). If a record ever carries ``"r"`` (recv unix seconds) the
  real feed age is measured from it and reported instead.
* An order/cancel decided at ``t_seen`` reaches the exchange at
  ``t_eff = t_seen + latency_ms/1000`` and only interacts with trades at or after
  ``t_eff`` — no look-ahead.
* Events are replayed in file (arrival) order, the causal order the collector saw.

Queue model
-----------
A resting order joins with ``queue_ahead`` = recorded size at-or-better than its price
(top-2+ levels; equals the touch size when we are at the touch). Trades at our price
shrink the queue; once the queue is gone the trade size fills us; a trade *through*
our price fills us once the printed volume exceeds the queue. ``--cancel-rate``
(1/s) decays the queue between events (adverse-selection queue cancels).

Metrics
-------
Fills, traded volume (USD), gross spread captured (edge vs mid at fill, USD + bps),
maker fees (1.5 bps/fill default), 1s/5s/30s mid markouts (bps), net PnL (USD + bps),
feed-age median/p90 (ms; "n/a" when the shard carries no recv stamp).

This script is research-only: it never imports credentials, never touches the network
and never places orders.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import json
import statistics
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mmbot.models import MarketState, StrategyParams  # noqa: E402
from mmbot.precision import AssetSpec, round_px, round_sz  # noqa: E402
from mmbot.strategy.quoter import compute_quotes  # noqa: E402

MAKER_FEE_BPS = 1.5
MARKOUT_S = (1.0, 5.0, 30.0)
MID_GRID_S = 0.05
HOUR_MS = 3_600_000


def parse_hour_key(value: str) -> int:
    """'20261014' -> 2026101400, '2026101419' -> 2026101419."""
    v = value.strip()
    if len(v) == 8 and v.isdigit():
        return int(v) * 100
    if len(v) == 10 and v.isdigit():
        return int(v)
    raise ValueError(f"bad date/hour key: {value!r} (want YYYYMMDD or YYYYMMDDHH)")


def hour_range(start: int, end: int) -> Iterator[tuple[str, str]]:
    if end < start:
        raise ValueError("end before start")
    cur = start
    while cur <= end:
        s = f"{cur:010d}"
        yield s[:8], s[8:10]
        day, hh = int(s[:8]), int(s[8:10])
        if hh == 23:
            day, hh = day + 1, 0
            y, m, d = day // 10000, (day // 100) % 100, day % 100
            import datetime

            nxt = datetime.date(y, m, d) + datetime.timedelta(days=1)
            cur = nxt.year * 10000 + nxt.month * 100 + nxt.day
            cur = cur * 100 + hh
        else:
            cur = day * 100 + hh + 1


def load_specs(universe_path: Path) -> dict[str, AssetSpec]:
    doc = json.loads(Path(universe_path).read_text())
    universe = doc[0]["universe"] if isinstance(doc, list) else doc.get("universe", [])
    return {u["name"]: AssetSpec(u["name"], int(u["szDecimals"])) for u in universe}


def shard_paths(data_dir: Path, coin: str, start: int, end: int) -> list[Path]:
    out = []
    for day, hh in hour_range(start, end):
        p = data_dir / day / f"{hh}_{coin}.jsonl.gz"
        if p.exists():
            out.append(p)
    return out


def tar_members(tar_path: Path, coin: str, start: int, end: int) -> list[tuple[Path, str]]:
    wanted = {f"{day}/{hh}_{coin}.jsonl.gz" for day, hh in hour_range(start, end)}
    out = []
    with tarfile.open(tar_path, "r") as tar:
        for m in tar.getmembers():
            if m.name in wanted and m.isfile():
                out.append((tar_path, m.name))
    return out


def _lines_from_gz_stream(fh) -> Iterator[str]:
    for raw in fh:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if line:
            yield line


def iter_records(
    paths: list[Path],
    tars: list[tuple[Path, str]],
    coin: str,
) -> Iterator[dict]:
    """Yield compact records for `coin` in arrival (file) order."""
    for p in paths:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
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
    for tar_path, member in tars:
        with tarfile.open(tar_path, "r") as tar:
            fh = tar.extractfile(member)
            if fh is None:
                continue
            with gzip.GzipFile(fileobj=fh, mode="rb") as gz:
                for line in _lines_from_gz_stream(gz):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("c") == coin:
                        yield rec


@dataclass
class Fill:
    t: float
    side: str
    px: float
    sz: float
    mid: float


@dataclass
class WorkingOrder:
    side: str
    px: float
    size: float
    queue_ahead: float
    t_placed: float


@dataclass
class EngineStats:
    places: int = 0
    cancels: int = 0
    alo_rejects: int = 0
    trades_seen: int = 0
    book_updates: int = 0
    decisions: int = 0


class ReplayEngine:
    def __init__(
        self,
        spec: AssetSpec,
        *,
        half_spread_bps: float = 4.0,
        skew_bps: float = 6.0,
        quote_notional: float = 15.0,
        max_notional: float = 60.0,
        min_order_notional: float = 10.0,
        requote_threshold_bps: float = 1.5,
        size_tolerance: float = 0.5,
        min_requote_interval_s: float = 0.3,
        latency_ms: float = 500.0,
        feed_age_ms: float = 400.0,
        cancel_rate: float = 0.0,
        maker_fee_bps: float = MAKER_FEE_BPS,
    ) -> None:
        self.spec = spec
        self.params = StrategyParams(
            coin=spec.coin,
            half_spread_bps=half_spread_bps,
            skew_bps=skew_bps,
            quote_notional=quote_notional,
            max_notional=max_notional,
            min_order_notional=min_order_notional,
            requote_threshold_bps=requote_threshold_bps,
            size_tolerance=size_tolerance,
            min_requote_interval_s=min_requote_interval_s,
        )
        self.latency_s = latency_ms / 1000.0
        self.feed_age_s = feed_age_ms / 1000.0
        self.cancel_rate = cancel_rate
        self.maker_fee_bps = maker_fee_bps

        self.bid_levels: list[list[float]] = []
        self.ask_levels: list[list[float]] = []
        self.exch_ts = 0.0
        self.last_recv_ts: float | None = None
        self.feed_ages_ms: list[float] = []

        self.pos = 0.0
        self.working: dict[str, WorkingOrder | None] = {"buy": None, "sell": None}
        self.pending: list[tuple[float, int, str, object]] = []
        self.pending_place: dict[str, tuple[float, float] | None] = {"buy": None, "sell": None}
        self.pending_cancel: dict[str, bool] = {"buy": False, "sell": False}
        self._seq = 0

        self.fills: list[Fill] = []
        self.stats = EngineStats()
        self.last_decision_seen = -1e18
        self.last_decay_t = 0.0
        self.mid_grid: dict[int, tuple[float, float]] = {}
        self.last_mid = 0.0

    # ------------------------------------------------------------------ book
    @property
    def bid(self) -> float:
        return self.bid_levels[0][0] if self.bid_levels else 0.0

    @property
    def ask(self) -> float:
        return self.ask_levels[0][0] if self.ask_levels else 0.0

    @property
    def bid_sz(self) -> float:
        return self.bid_levels[0][1] if self.bid_levels else 0.0

    @property
    def ask_sz(self) -> float:
        return self.ask_levels[0][1] if self.ask_levels else 0.0

    @property
    def mid(self) -> float:
        b, a = self.bid, self.ask
        return 0.5 * (b + a) if b > 0 and a > b else self.last_mid

    def market_state(self, recv_ts: float) -> MarketState:
        return MarketState(
            coin=self.spec.coin,
            bid=self.bid,
            ask=self.ask,
            bid_sz=self.bid_sz,
            ask_sz=self.ask_sz,
            recv_ts=recv_ts,
            exch_ts=self.exch_ts,
        )

    def _set_levels(self, side: str, levels: list[list[float]]) -> None:
        cur = self.bid_levels if side == "buy" else self.ask_levels
        cur.clear()
        for px, sz in levels[:5]:
            cur.append([px, sz])

    def _overlay_top(self, side: str, px: float, sz: float) -> None:
        levels = self.bid_levels if side == "buy" else self.ask_levels
        if levels and abs(levels[0][0] - px) < 1e-12:
            levels[0][1] = sz
            return
        levels.insert(0, [px, sz])
        del levels[5:]

    def _record_mid(self) -> None:
        m = self.mid
        if m > 0:
            self.last_mid = m
            self.mid_grid[int(self.exch_ts / MID_GRID_S)] = (self.exch_ts, m)

    def mid_at(self, t: float) -> float:
        key = int(t / MID_GRID_S)
        if not self.mid_grid:
            return self.last_mid
        if key in self.mid_grid:
            return self.mid_grid[key][1]
        keys = [k for k in self.mid_grid if k <= key]
        return self.mid_grid[max(keys)][1] if keys else self.last_mid

    # ------------------------------------------------------------- scheduling
    def _schedule(self, eff: float, kind: str, payload: object) -> None:
        self._seq += 1
        if kind == "place":
            side, px, size = payload  # type: ignore[misc]
            self.pending_place[side] = (px, size)
        else:
            self.pending_cancel[payload] = True  # type: ignore[index]
            self.pending_place[payload] = None  # type: ignore[index]
        heapq.heappush(self.pending, (eff, self._seq, kind, payload))

    def _alo_crosses(self, side: str, px: float) -> bool:
        if side == "buy" and self.ask > 0 and px >= self.ask:
            return True
        if side == "sell" and self.bid > 0 and px <= self.bid:
            return True
        return False

    def _queue_for(self, side: str, px: float) -> float:
        levels = self.bid_levels if side == "buy" else self.ask_levels
        total = 0.0
        for lvl_px, lvl_sz in levels:
            better = lvl_px >= px if side == "buy" else lvl_px <= px
            if better:
                total += lvl_sz
        return total

    def _apply_place(self, side: str, px: float, size: float) -> None:
        self.pending_place[side] = None
        self.stats.places += 1
        if self._alo_crosses(side, px):
            self.stats.alo_rejects += 1
            return
        q = self._queue_for(side, px)
        self.working[side] = WorkingOrder(side, px, size, q, self.exch_ts)

    def _apply_cancel(self, side: str) -> None:
        self.pending_cancel[side] = False
        self.stats.cancels += 1
        self.working[side] = None

    def _advance_to(self, t: float) -> None:
        while self.pending and self.pending[0][0] <= t:
            _, _, kind, payload = heapq.heappop(self.pending)
            if kind == "place":
                side, px, size = payload  # type: ignore[misc]
                self._apply_place(side, px, size)
            else:
                self._apply_cancel(payload)  # type: ignore[arg-type]

    def _decay_queues(self, t: float) -> None:
        if self.cancel_rate <= 0:
            self.last_decay_t = t
            return
        dt = max(0.0, t - self.last_decay_t)
        if dt > 0:
            factor = (1.0 - self.cancel_rate) ** dt
            for w in self.working.values():
                if w is not None:
                    w.queue_ahead *= factor
        self.last_decay_t = t

    # ---------------------------------------------------------------- events
    def on_record(self, rec: dict) -> None:
        t_ms = rec.get("t")
        if not isinstance(t_ms, (int, float)):
            return
        t = t_ms / 1000.0
        self.exch_ts = t
        recv = rec.get("r")
        if isinstance(recv, (int, float)):
            self.last_recv_ts = float(recv)
            self.feed_ages_ms.append((float(recv) - t) * 1000.0)
        self._advance_to(t)
        ev = rec.get("e")
        if ev == "l2":
            self._on_l2(t, rec)
        elif ev == "bbo":
            self._on_bbo(t, rec)
        elif ev == "tr":
            self._on_trade(t, rec)
        else:
            return
        self._decay_queues(t)
        self._maybe_decide(t)

    @staticmethod
    def _levels(raw: list, invert: bool) -> list[list[float]]:
        out = []
        for lvl in raw or []:
            if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
                continue
            try:
                px, sz = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError):
                continue
            if px > 0 and sz >= 0:
                out.append([px, sz])
        if invert:
            out.reverse()
        return out

    def _on_l2(self, t: float, rec: dict) -> None:
        self._set_levels("buy", self._levels(rec.get("b"), invert=True))
        self._set_levels("sell", self._levels(rec.get("a"), invert=False))
        self.stats.book_updates += 1
        self._record_mid()

    def _on_bbo(self, t: float, rec: dict) -> None:
        for side, key in (("buy", "b"), ("sell", "a")):
            lvl = rec.get(key)
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                try:
                    px, sz = float(lvl[0]), float(lvl[1])
                except (TypeError, ValueError):
                    continue
                if px > 0 and sz >= 0:
                    self._overlay_top(side, px, sz)
        self.stats.book_updates += 1
        self._record_mid()

    def _fill(self, side: str, px: float, sz: float) -> None:
        self.fills.append(Fill(self.exch_ts, side, px, sz, self.mid))
        self.pos += sz if side == "buy" else -sz

    def _on_trade(self, t: float, rec: dict) -> None:
        self.stats.trades_seen += 1
        try:
            px, sz = float(rec["px"]), float(rec["sz"])
        except (KeyError, TypeError, ValueError):
            return
        side = rec.get("side")
        w = self.working["buy"] if side == "A" else self.working["sell"] if side == "B" else None
        if w is None or sz <= 0:
            return
        hit = px <= w.px if w.side == "buy" else px >= w.px
        if not hit:
            return
        if sz >= w.queue_ahead:
            fill_sz = min(w.size, sz - w.queue_ahead)
            w.queue_ahead = 0.0
            if fill_sz > 0:
                self._fill(w.side, w.px, fill_sz)
                w.size = round(w.size - fill_sz, 12)
            if w.size <= 0:
                self.working[w.side] = None
        else:
            w.queue_ahead -= sz

    # -------------------------------------------------------------- strategy
    def _maybe_decide(self, t: float) -> None:
        m = self.market_state(t + self.feed_age_s)
        if not m.is_valid:
            return
        seen_t = t + self.feed_age_s
        if seen_t - self.last_decision_seen < self.params.min_requote_interval_s:
            return
        self.last_decision_seen = seen_t
        self.stats.decisions += 1
        targets = {q.side.value: q for q in compute_quotes(m, self.pos, self.params, self.spec)}
        eff = seen_t + self.latency_s
        for side in ("buy", "sell"):
            tgt = targets.get(side)
            w = self.working[side]
            if self.pending_cancel[side] and tgt is None:
                continue
            if tgt is None:
                if w is not None or self.pending_place[side] is not None:
                    self._schedule(eff, "cancel", side)
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
                        self._schedule(eff, "cancel", side)
                        self._schedule(eff, "place", (side, px, sz))
                    continue
                self._schedule(eff, "place", (side, px, sz))
                continue
            if self.pending_cancel[side]:
                continue
            moved_bps = abs(px - w.px) / w.px * 1e4
            undersized = w.size < (1.0 - self.params.size_tolerance) * sz
            if moved_bps >= self.params.requote_threshold_bps or undersized:
                self._schedule(eff, "cancel", side)
                self._schedule(eff, "place", (side, px, sz))

    # --------------------------------------------------------------- results
    def compute_metrics(self, markouts: Iterable[float] = MARKOUT_S) -> dict:
        volume = sum(f.px * f.sz for f in self.fills)
        gross_usd = 0.0
        fees_usd = 0.0
        for f in self.fills:
            notional = f.px * f.sz
            edge = (f.mid - f.px) if f.side == "buy" else (f.px - f.mid)
            gross_usd += edge * f.sz
            fees_usd += notional * self.maker_fee_bps / 1e4
        realized = 0.0
        for f in self.fills:
            realized += f.px * f.sz if f.side == "sell" else -f.px * f.sz
        mid_last = self.last_mid
        net_usd = realized + self.pos * mid_last - fees_usd
        metrics = {
            "coin": self.spec.coin,
            "fills": len(self.fills),
            "traded_volume_usd": volume,
            "gross_spread_usd": gross_usd,
            "gross_spread_bps": (gross_usd / volume * 1e4) if volume > 0 else 0.0,
            "maker_fees_usd": fees_usd,
            "net_pnl_usd": net_usd,
            "net_pnl_bps": (net_usd / volume * 1e4) if volume > 0 else 0.0,
            "final_position": self.pos,
            "places": self.stats.places,
            "cancels": self.stats.cancels,
            "alo_rejects": self.stats.alo_rejects,
            "decisions": self.stats.decisions,
            "trades_seen": self.stats.trades_seen,
            "book_updates": self.stats.book_updates,
        }
        for secs in markouts:
            vals = []
            for f in self.fills:
                m = self.mid_at(f.t + secs)
                if m <= 0:
                    continue
                edge = (m - f.px) if f.side == "buy" else (f.px - m)
                vals.append(edge / f.px * 1e4)
            metrics[f"markout_{int(secs)}s_bps"] = statistics.fmean(vals) if vals else 0.0
        if self.feed_ages_ms:
            srt = sorted(self.feed_ages_ms)
            metrics["feed_age_median_ms"] = statistics.median(srt)
            metrics["feed_age_p90_ms"] = srt[min(len(srt) - 1, int(0.9 * len(srt)))]
        else:
            metrics["feed_age_median_ms"] = None
            metrics["feed_age_p90_ms"] = None
        return metrics


def run_backtest(
    coin: str,
    spec: AssetSpec,
    records: Iterable[dict],
    **engine_kwargs,
) -> dict:
    eng = ReplayEngine(spec, **engine_kwargs)
    for rec in records:
        eng.on_record(rec)
    return eng.compute_metrics()


MARKDOWN_ROWS = [
    ("Total fills", "fills", None),
    ("Traded volume (USD)", "traded_volume_usd", "{:,.2f}"),
    ("Gross spread captured (USD)", "gross_spread_usd", "{:,.4f}"),
    ("Gross spread captured (bps)", "gross_spread_bps", "{:.3f}"),
    ("Maker fees paid (USD)", "maker_fees_usd", "{:,.4f}"),
    ("Markout 1s (bps)", "markout_1s_bps", "{:+.3f}"),
    ("Markout 5s (bps)", "markout_5s_bps", "{:+.3f}"),
    ("Markout 30s (bps)", "markout_30s_bps", "{:+.3f}"),
    ("Net PnL (USD)", "net_pnl_usd", "{:+.4f}"),
    ("Net PnL (bps)", "net_pnl_bps", "{:+.3f}"),
    ("Final position", "final_position", "{:+.6f}"),
    ("Places / cancels / ALO rejects", "place_cancel_alo", None),
    ("Feed age median (ms)", "feed_age_median_ms", "{:.1f}"),
    ("Feed age p90 (ms)", "feed_age_p90_ms", "{:.1f}"),
]


def format_markdown(metrics: dict, args: argparse.Namespace) -> str:
    lines = [
        f"## replay backtest — {metrics['coin']}  "
        f"(latency {args.latency_ms:.0f} ms, hs {args.half_spread_bps} bps, "
        f"skew {args.skew_bps} bps, cancel_rate {args.cancel_rate}, "
        f"feed_age {args.feed_age_ms:.0f} ms fixed)",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    row_map = dict(metrics)
    row_map["place_cancel_alo"] = (
        f"{metrics['places']} / {metrics['cancels']} / {metrics['alo_rejects']}"
    )
    for label, key, fmt in MARKDOWN_ROWS:
        val = row_map.get(key)
        if val is None:
            text = "n/a (no recv stamp in dataset)"
        elif fmt:
            text = fmt.format(val)
        else:
            text = str(val)
        lines.append(f"| {label} | {text} |")
    note = (
        "NOTE: t_seen = exchange t + fixed feed age (dataset has no recv stamp); "
        "orders/cancels effective at t_seen + latency; queue = top-2 book levels "
        "with trade depletion"
    )
    if metrics.get("feed_age_median_ms") is None:
        lines += ["", f"> {note}"]
    return "\n".join(lines)


CSV_KEYS = [
    "coin", "fills", "traded_volume_usd", "gross_spread_usd", "gross_spread_bps",
    "maker_fees_usd", "markout_1s_bps", "markout_5s_bps", "markout_30s_bps",
    "net_pnl_usd", "net_pnl_bps", "final_position", "places", "cancels",
    "alo_rejects", "decisions", "trades_seen", "book_updates",
    "feed_age_median_ms", "feed_age_p90_ms",
]


def write_csv(path: Path, rows: list[dict], args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "latency_ms", "half_spread_bps", "skew_bps", "cancel_rate", "feed_age_ms",
    ] + CSV_KEYS
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        for m in rows:
            row = {
                "latency_ms": args.latency_ms,
                "half_spread_bps": args.half_spread_bps,
                "skew_bps": args.skew_bps,
                "cancel_rate": args.cancel_rate,
                "feed_age_ms": args.feed_age_ms,
            }
            row.update({k: m.get(k) for k in CSV_KEYS})
            w.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Offline replay backtest over T10 collector shards")
    p.add_argument("--coin", required=True)
    p.add_argument("--start", required=True, help="YYYYMMDD or YYYYMMDDHH")
    p.add_argument("--end", required=True, help="YYYYMMDD or YYYYMMDDHH (inclusive)")
    p.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    p.add_argument("--tar", action="append", default=[], help="data_YYYYMMDD.tar (repeatable)")
    p.add_argument("--universe", default=str(PROJECT_ROOT / "data" / "universe.json"))
    p.add_argument("--latency-ms", type=float, default=500.0)
    p.add_argument("--half-spread-bps", type=float, default=4.0)
    p.add_argument("--skew-bps", type=float, default=6.0)
    p.add_argument("--cancel-rate", type=float, default=0.0)
    p.add_argument("--feed-age-ms", type=float, default=400.0)
    p.add_argument("--quote-notional", type=float, default=15.0)
    p.add_argument("--max-notional", type=float, default=60.0)
    p.add_argument("--min-order-notional", type=float, default=10.0)
    p.add_argument("--maker-fee-bps", type=float, default=MAKER_FEE_BPS)
    p.add_argument("--csv", default=None, help="append summary rows to this CSV")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    specs = load_specs(Path(args.universe))
    if args.coin not in specs:
        print(f"coin {args.coin} not in {args.universe}", file=sys.stderr)
        return 2
    start, end = parse_hour_key(args.start), parse_hour_key(args.end)
    paths = shard_paths(Path(args.data_dir), args.coin, start, end)
    tars = []
    for t in args.tar:
        tars.extend(tar_members(Path(t), args.coin, start, end))
    if not paths and not tars:
        print(f"no shards for {args.coin} in {args.start}..{args.end}", file=sys.stderr)
        return 2
    records = iter_records(paths, tars, args.coin)
    metrics = run_backtest(
        args.coin,
        specs[args.coin],
        records,
        half_spread_bps=args.half_spread_bps,
        skew_bps=args.skew_bps,
        quote_notional=args.quote_notional,
        max_notional=args.max_notional,
        min_order_notional=args.min_order_notional,
        latency_ms=args.latency_ms,
        feed_age_ms=args.feed_age_ms,
        cancel_rate=args.cancel_rate,
        maker_fee_bps=args.maker_fee_bps,
    )
    print(format_markdown(metrics, args))
    if args.csv:
        write_csv(Path(args.csv), [metrics], args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
