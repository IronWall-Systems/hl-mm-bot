#!/usr/bin/env python3
"""T11 — offline replay market-making backtester over the T10 collector's compact shards.

Reads `data/YYYYMMDD/HH_<COIN>.jsonl.gz` shards (or the members of `data_YYYYMMDD.tar`
archives) and replays them through the *real* quoter (`src/mmbot/strategy/quoter.py`
`compute_quotes`) with latency, ALO clamping and a top-of-book queue model. Tar archives are
now discovered TRANSPARENTLY: any `data_YYYYMMDD.tar` in `--archive-dir` (default
`/root/gdrive_backup/hl-datasets`, env `HL_MM_ARCHIVE_DIR`) fills hours that are not present
locally, with no double counting (local > explicit `--tar` > auto archive); `--no-auto-tar`
disables it. `gather_sources()` exposes the same behaviour to the T16 evaluator.

Time model
----------
* Records carry the exchange stamp ``t`` (ms). Collector v2 (T15) also writes a local
  receive stamp ``"r"`` (epoch **ms**) on every event line: when present the bot's view
  time is ``t_seen = t + (r - t)/1000`` — the TRUE recorded feed age. For legacy shards
  without ``"r"`` the age falls back to ``--feed-age-ms`` (default 400 — UNVERIFIED,
  sized from the T7d fast-book mainnet measurements, A-16..A-18); the median/p90 of the
  measured ages are still reported.
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

PnL decomposition (T13)
-----------------------
Net PnL is split strictly (FIFO lot matching, exact identities):

* ``round_trip_spread_pnl`` = matched_gross − fees_matched — spread realised on
  buy/sell pairs fully matched FIFO, net of maker fees on both matched legs.
* ``inventory_mtm_pnl``     = pos × final_mid − residual_basis — mark-to-market of
  the unmatched residual inventory (lot basis vs final mid), i.e. the directional
  drift component (lucky/unlucky inventory, NOT spread capture).
* ``total_fees_usd``        = maker fees on every fill (= ``maker_fees_usd``).
* ``funding_pnl``           = accrued perpetual funding, ``-pos × mark × rate`` once per
  UTC hour from collector ``e=funding`` records (0 when none are present).

Identities (verified by unit tests)::

    net_pnl_usd = matched_spread_usd + inventory_mtm_pnl + funding_pnl − total_fees_usd
                = round_trip_spread_pnl + inventory_mtm_pnl + funding_pnl − fees_unmatched_usd
    total_fees_usd = fees_matched_usd + fees_unmatched_usd

``gross_spread_bps`` is an execution-quality metric (edge vs contemporaneous mid at
each fill, unmatched) and is NOT a component of net PnL — it can be negative while
net PnL is positive purely via ``inventory_mtm_pnl`` (the T13 audit question).

This script is research-only: it never imports credentials, never touches the network
and never places orders.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import heapq
import json
import os
import statistics
import sys
import tarfile
from collections import deque
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
DEFAULT_ARCHIVE_DIR = Path(
    os.environ.get("HL_MM_ARCHIVE_DIR", "/root/gdrive_backup/hl-datasets")
)


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
            y, m, d = day // 10000, (day // 100) % 100, day % 100
            import datetime

            nxt = datetime.date(y, m, d) + datetime.timedelta(days=1)
            cur = (nxt.year * 10000 + nxt.month * 100 + nxt.day) * 100
        else:
            cur = day * 100 + hh + 1


def resolve_range(start_s: str, end_s: str) -> tuple[int, int]:
    """Bare YYYYMMDD end keys expand to the last hour of that day (23)."""
    start = parse_hour_key(start_s)
    end = parse_hour_key(end_s)
    if len(end_s.strip()) == 8:
        end += 23
    if end < start:
        raise ValueError("end before start")
    return start, end


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


def discover_tars(
    archive_dir: Path | None,
    coin: str,
    start: int,
    end: int,
) -> list[tuple[Path, str]]:
    """Auto-discover `data_YYYYMMDD.tar` for the days in [start, end].

    This makes offline replay transparent after `archive_data.py` has tarred a day and
    removed its local `data/YYYYMMDD/` directory: pass the archive directory once and the
    tar members are read exactly like local shards. Missing directory / missing tar for a
    day yields nothing (never raises), so a partially archived dataset still works.
    """
    if archive_dir is None:
        return []
    archive_dir = Path(archive_dir)
    if not archive_dir.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    for day in sorted({day for day, _ in hour_range(start, end)}):
        tar_path = archive_dir / f"data_{day}.tar"
        if tar_path.is_file():
            out.extend(tar_members(tar_path, coin, start, end))
    return out


def gather_sources(
    data_dir: Path,
    coin: str,
    start: int,
    end: int,
    *,
    archive_dir: Path | None = None,
    explicit_tars: Iterable[tuple[Path, str]] = (),
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Return (local shard paths, tar members) covering [start, end] with NO double count.

    Precedence per (day, hour): local shard > explicit `--tar` member > auto-discovered
    archive tar member. This lets the T16 evaluator (and `replay_backtest.main`) read a
    dataset that is partly local and partly archived without knowing which is which.
    """
    paths = shard_paths(Path(data_dir), coin, start, end)
    covered = {(p.parent.name, p.name[:2]) for p in paths}
    tars: list[tuple[Path, str]] = []
    candidates = list(explicit_tars) + discover_tars(archive_dir, coin, start, end)
    for tar_path, member in candidates:
        if "/" not in member:
            continue
        day, name = member.split("/", 1)
        if len(name) < 2 or (day, name[:2]) in covered:
            continue
        covered.add((day, name[:2]))
        tars.append((Path(tar_path), member))
    return paths, tars


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
    """Yield compact records for `coin` in arrival (file) order.

    Truncated gzip shards (the Oct-9 16:00Z outage left hour-15 files half
    written) yield every record that decompresses, then a warning — the rest of
    the dataset continues.
    """
    for p in paths:
        try:
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
        except (EOFError, gzip.BadGzipFile, OSError) as exc:
            print(f"warning: truncated shard {p}: {exc}", file=sys.stderr)
    for tar_path, member in tars:
        with tarfile.open(tar_path, "r") as tar:
            fh = tar.extractfile(member)
            if fh is None:
                continue
            try:
                with gzip.GzipFile(fileobj=fh, mode="rb") as gz:
                    for line in _lines_from_gz_stream(gz):
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("c") == coin:
                            yield rec
            except (EOFError, gzip.BadGzipFile, OSError) as exc:
                print(f"warning: truncated tar member {tar_path}:{member}: {exc}",
                      file=sys.stderr)


@dataclass
class Fill:
    t: float
    side: str
    px: float
    sz: float
    mid: float
    markouts: dict[float, float] | None = None


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
        feed_age_from_record: bool = True,
        place_modify_latency_ms: float | None = None,
        cancel_latency_ms: float | None = None,
        park_via_modify: bool = False,
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
        # Collector-v2 records carry their own receive timestamp (r).  Useful for
        # local feeds, but archive r-t includes the collector host path (~400 ms
        # here), so warm-feed profiles must force the configured feed age.
        self.feed_age_from_record = bool(feed_age_from_record)
        # Split L1 timing.  ``latency_ms`` remains the backward-compatible
        # fallback for both paths when the explicit values are not supplied.
        self.place_modify_latency_s = (
            latency_ms if place_modify_latency_ms is None else place_modify_latency_ms
        ) / 1000.0
        self.cancel_latency_s = (
            latency_ms if cancel_latency_ms is None else cancel_latency_ms
        ) / 1000.0
        # When True, a risk-off pull is modelled as an OTM park via batchModify
        # (fast path) instead of a raw bulk cancel (slow path).
        self.park_via_modify = bool(park_via_modify)
        self.cancel_rate = cancel_rate
        self.maker_fee_bps = maker_fee_bps

        self.bid_levels: list[list[float]] = []
        self.ask_levels: list[list[float]] = []
        self.exch_ts = 0.0
        self.last_recv_ts: float | None = None
        self.feed_ages_ms: list[float] = []
        self.current_feed_age_s = self.feed_age_s

        # v2 funding state (accrued once per UTC hour from `e=funding` records)
        self.funding_pnl = 0.0
        self.funding_samples = 0
        self._last_funding_hour: int | None = None
        self._last_funding_rate = 0.0
        self._last_funding_mark = 0.0

        self.pos = 0.0
        self.working: dict[str, WorkingOrder | None] = {"buy": None, "sell": None}
        self.pending: list[tuple[float, int, str, object]] = []
        self.pending_place: dict[str, tuple[float, float] | None] = {"buy": None, "sell": None}
        self.pending_cancel: dict[str, bool] = {"buy": False, "sell": False}
        self._seq = 0
        self.markout_horizon_s = max(MARKOUT_S)
        self._open_fills: list[Fill] = []
        self._since_prune = 0

        self.fills: list[Fill] = []
        self.stats = EngineStats()
        self.last_decision_seen = -1e18
        self.last_decay_t = 0.0
        self.mid_grid: dict[int, tuple[float, float]] = {}
        self.last_mid = 0.0

        # T13 PnL decomposition state
        self._open_lots: deque[list] = deque()  # [side, px, remaining_sz] FIFO
        self.matched_gross_usd = 0.0
        self.matched_buy_notional = 0.0
        self.matched_sell_notional = 0.0
        self.fees_matched_usd = 0.0
        self.round_trip_count = 0
        self.max_abs_inventory_usd = 0.0

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
            if self.pos != 0.0:
                v = abs(self.pos * m)
                if v > self.max_abs_inventory_usd:
                    self.max_abs_inventory_usd = v

    def mid_at(self, t: float) -> float:
        key = int(t / MID_GRID_S)
        if not self.mid_grid:
            return self.last_mid
        if key in self.mid_grid:
            return self.mid_grid[key][1]
        keys = [k for k in self.mid_grid if k <= key]
        return self.mid_grid[max(keys)][1] if keys else self.last_mid

    # ------------------------------------------------------------- scheduling
    def seen_time(self, t: float) -> float:
        """Market timestamp as visible to the bot after feed transit."""
        return t + self.current_feed_age_s

    def place_ready_time(self, seen_t: float) -> float:
        """When a fast-path place / batchModify becomes effective."""
        return seen_t + self.place_modify_latency_s

    def cancel_ready_time(self, seen_t: float) -> float:
        """When a raw bulk cancel becomes effective (slow path)."""
        return seen_t + self.cancel_latency_s

    def park_ready_time(self, seen_t: float) -> float:
        """When a risk-off quote pull becomes effective."""
        delay = self.place_modify_latency_s if self.park_via_modify else self.cancel_latency_s
        return seen_t + delay

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
        ev = rec.get("e")
        if ev in ("header", "gap"):
            return
        t_ms = rec.get("t")
        if not isinstance(t_ms, (int, float)):
            return
        t = t_ms / 1000.0
        self.exch_ts = t
        recv = rec.get("r")
        if self.feed_age_from_record and isinstance(recv, (int, float)):
            age_ms = float(recv) - float(t_ms)  # both epoch ms (collector v2)
            if age_ms < 0:
                age_ms = 0.0
            self.last_recv_ts = float(recv)
            self.feed_ages_ms.append(age_ms)
            self.current_feed_age_s = age_ms / 1000.0
        else:
            self.current_feed_age_s = self.feed_age_s
        self._advance_to(t)
        if ev == "l2":
            self._on_l2(t, rec)
        elif ev == "bbo":
            self._on_bbo(t, rec)
        elif ev == "tr":
            self._on_trade(t, rec)
        elif ev == "funding":
            self._on_funding(t, rec)
            self._decay_queues(t)
            return
        else:
            return
        self._settle_markouts(t)
        self._since_prune += 1
        if self._since_prune >= 2000:
            self._since_prune = 0
            self._prune_grid(t)
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
        f = Fill(self.exch_ts, side, px, sz, self.mid)
        self.fills.append(f)
        self._open_fills.append(f)
        self._match_fifo(side, px, sz)
        self.pos += sz if side == "buy" else -sz
        if self.last_mid > 0:
            v = abs(self.pos * self.last_mid)
            if v > self.max_abs_inventory_usd:
                self.max_abs_inventory_usd = v

    def _match_fifo(self, side: str, px: float, sz: float) -> None:
        """Consume opposite-side open lots FIFO; leftover becomes a new lot.

        Records matched notionals and the maker fees belonging to matched legs
        so the strict PnL identities in `compute_metrics` hold exactly.
        """
        fee_rate = self.maker_fee_bps / 1e4
        remaining = sz
        lots = self._open_lots
        while remaining > 1e-12 and lots and lots[0][0] != side:
            lot = lots[0]
            m = min(remaining, lot[2])
            if lot[0] == "buy":
                self.matched_buy_notional += lot[1] * m
                self.matched_sell_notional += px * m
            else:
                self.matched_buy_notional += px * m
                self.matched_sell_notional += lot[1] * m
            self.matched_gross_usd += (px - lot[1]) * m if lot[0] == "buy" else (lot[1] - px) * m
            self.fees_matched_usd += (lot[1] + px) * m * fee_rate
            self.round_trip_count += 1
            lot[2] -= m
            remaining -= m
            if lot[2] <= 1e-12:
                lots.popleft()
        if remaining > 1e-12:
            lots.append([side, px, remaining])

    def _settle_markouts(self, t: float) -> None:
        while self._open_fills and self._open_fills[0].t + self.markout_horizon_s <= t:
            f = self._open_fills.pop(0)
            f.markouts = {}
            for secs in MARKOUT_S:
                m = self.mid_at(f.t + secs)
                if m > 0:
                    edge = (m - f.px) if f.side == "buy" else (f.px - m)
                    f.markouts[secs] = edge / f.px * 1e4

    def _prune_grid(self, t: float) -> None:
        cutoff = int((t - (self.markout_horizon_s + 2 * MID_GRID_S)) / MID_GRID_S)
        if cutoff <= 0:
            return
        for k in [k for k in self.mid_grid if k < cutoff]:
            del self.mid_grid[k]

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

    def _on_funding(self, t: float, rec: dict) -> None:
        """Accrue perpetual funding once per UTC hour.

        Hyperliquid charges funding hourly. A collector `e=funding` record carries the
        current hourly ``funding`` rate and ``mark_px``. The first sample anchors the hour
        (no accrual); when the hour bucket advances we charge the *previous* hour's last
        observed rate over ``hours`` elapsed at the position held then::

            funding_pnl += -position_sz * mark_px * funding_rate

        (long pays when the rate is positive). Missing ``mark_px`` falls back to the last
        mid. This is an hourly step model — within-hour position changes are not split.
        """
        rate = _as_float(rec.get("funding"))
        mark = _as_float(rec.get("mark_px"))
        if mark is None or mark <= 0:
            mark = self.last_mid
        if rate is None or mark <= 0:
            return
        hour = int(t // 3600)
        if self._last_funding_hour is not None and hour > self._last_funding_hour:
            hours = hour - self._last_funding_hour
            self.funding_pnl += -self.pos * self._last_funding_mark * self._last_funding_rate * hours
        self._last_funding_hour = hour
        self._last_funding_rate = rate
        self._last_funding_mark = mark
        self.funding_samples += 1

    # -------------------------------------------------------------- strategy
    def _maybe_decide(self, t: float) -> None:
        seen_t = t + self.current_feed_age_s
        if seen_t - self.last_decision_seen < self.params.min_requote_interval_s:
            return
        m = self.market_state(seen_t)
        if not m.is_valid:
            return
        self.last_decision_seen = seen_t
        self.stats.decisions += 1
        targets = {q.side.value: q for q in compute_quotes(m, self.pos, self.params, self.spec)}
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
                        # A requote is a batchModify in production: fast path.
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

    # --------------------------------------------------------------- results
    def compute_metrics(self, markouts: Iterable[float] = MARKOUT_S) -> dict:
        self._settle_markouts(float("inf"))
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
        net_usd = realized + self.pos * mid_last + self.funding_pnl - fees_usd

        # --- T13 strict decomposition (identities verified by unit tests) ---
        residual_basis = sum(l[1] * l[2] for l in self._open_lots if l[0] == "buy") - sum(
            l[1] * l[2] for l in self._open_lots if l[0] == "sell"
        )
        ending_inventory_usd = self.pos * mid_last
        inventory_mtm_usd = ending_inventory_usd - residual_basis
        fees_unmatched_usd = fees_usd - self.fees_matched_usd
        round_trip_pnl_usd = self.matched_gross_usd - self.fees_matched_usd
        end_inv_abs = abs(ending_inventory_usd)
        if end_inv_abs > self.max_abs_inventory_usd:
            self.max_abs_inventory_usd = end_inv_abs

        metrics = {
            "coin": self.spec.coin,
            "fills": len(self.fills),
            "traded_volume_usd": volume,
            "gross_spread_usd": gross_usd,
            "gross_spread_bps": (gross_usd / volume * 1e4) if volume > 0 else 0.0,
            "maker_fees_usd": fees_usd,
            "net_pnl_usd": net_usd,
            "net_pnl_bps": (net_usd / volume * 1e4) if volume > 0 else 0.0,
            "round_trip_spread_pnl": round_trip_pnl_usd,
            "inventory_mtm_pnl": inventory_mtm_usd,
            "funding_pnl": self.funding_pnl,
            "funding_samples": self.funding_samples,
            "funding_rate_last": self._last_funding_rate,
            "matched_spread_usd": self.matched_gross_usd,
            "fees_matched_usd": self.fees_matched_usd,
            "fees_unmatched_usd": fees_unmatched_usd,
            "total_fees_usd": fees_usd,
            "round_trip_count": self.round_trip_count,
            "max_abs_inventory_usd": self.max_abs_inventory_usd,
            "ending_inventory_usd": ending_inventory_usd,
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
                if f.markouts is None or secs not in f.markouts:
                    continue
                vals.append(f.markouts[secs])
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
    ("Round-trip spread PnL (USD, after matched fees)", "round_trip_spread_pnl", "{:+.4f}"),
    ("Inventory MTM PnL (USD)", "inventory_mtm_pnl", "{:+.4f}"),
    ("Funding PnL (USD)", "funding_pnl", "{:+.4f}"),
    ("Total fees (USD)", "total_fees_usd", "{:+.4f}"),
    ("Fees on matched legs (USD)", "fees_matched_usd", "{:+.4f}"),
    ("Fees on unmatched legs (USD)", "fees_unmatched_usd", "{:+.4f}"),
    ("Round trips (FIFO matches)", "round_trip_count", "{:d}"),
    ("Max |inventory| (USD)", "max_abs_inventory_usd", "{:,.4f}"),
    ("Ending inventory (USD)", "ending_inventory_usd", "{:+.4f}"),
    ("Final position", "final_position", "{:+.6f}"),
    ("Places / cancels / ALO rejects", "place_cancel_alo", None),
    ("Feed age median (ms)", "feed_age_median_ms", "{:.1f}"),
    ("Feed age p90 (ms)", "feed_age_p90_ms", "{:.1f}"),
]


def format_markdown(metrics: dict, args: argparse.Namespace) -> str:
    _place = getattr(args, "place_modify_latency_ms", None)
    _cancel = getattr(args, "cancel_latency_ms", None)
    _place = getattr(args, "latency_ms", 0.0) if _place is None else _place
    _cancel = _place if _cancel is None else _cancel
    _park = getattr(args, "park_via_modify", False)
    lines = [
        f"## replay backtest — {metrics['coin']}  "
        f"(place/modify {_place:.0f} ms, cancel {_cancel:.0f} ms, "
        f"park_via_modify={_park}, hs {args.half_spread_bps} bps, "
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
        "NOTE: t_seen = exchange t + fixed feed age (dataset has no recv stamp \"r\"); "
        "orders/cancels effective at t_seen + latency; queue = top-2 book levels "
        "with trade depletion"
    )
    if metrics.get("feed_age_median_ms") is None:
        lines += ["", f"> {note}"]
    return "\n".join(lines)


CSV_KEYS = [
    "coin", "fills", "traded_volume_usd", "gross_spread_usd", "gross_spread_bps",
    "maker_fees_usd", "markout_1s_bps", "markout_5s_bps", "markout_30s_bps",
    "net_pnl_usd", "net_pnl_bps", "round_trip_spread_pnl", "inventory_mtm_pnl",
    "funding_pnl", "funding_samples", "funding_rate_last",
    "matched_spread_usd", "fees_matched_usd", "fees_unmatched_usd", "total_fees_usd",
    "round_trip_count", "max_abs_inventory_usd", "ending_inventory_usd",
    "final_position", "places", "cancels",
    "alo_rejects", "decisions", "trades_seen", "book_updates",
    "feed_age_median_ms", "feed_age_p90_ms",
]


def write_csv(path: Path, rows: list[dict], args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "latency_ms", "half_spread_bps", "skew_bps", "cancel_rate", "day", "feed_age_ms",
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
                "day": args.start,
                "feed_age_ms": args.feed_age_ms,
            }
            row.update({k: m.get(k) for k in CSV_KEYS})
            w.writerow(row)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Offline replay backtest over T10 collector shards")
    p.add_argument("--coin", required=True)
    p.add_argument("--start", required=True, help="YYYYMMDD (whole day) or YYYYMMDDHH")
    p.add_argument("--end", required=True, help="YYYYMMDD (whole day, inclusive) or YYYYMMDDHH")
    p.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    p.add_argument("--tar", action="append", default=[], help="data_YYYYMMDD.tar (repeatable)")
    p.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE_DIR),
                   help="directory auto-scanned for data_YYYYMMDD.tar (default %(default)s)")
    p.add_argument("--no-auto-tar", action="store_true",
                   help="do not auto-read data_YYYYMMDD.tar from --archive-dir")
    p.add_argument("--universe", default=str(PROJECT_ROOT / "data" / "universe.json"))
    p.add_argument("--latency-ms", type=float, default=500.0,
                   help="backward-compatible fallback for both L1 paths")
    p.add_argument("--place-modify-latency-ms", type=float, default=None,
                   help="fast-path Alo place / batchModify latency; default = --latency-ms")
    p.add_argument("--cancel-latency-ms", type=float, default=None,
                   help="raw bulk-cancel latency; default = --latency-ms")
    p.add_argument("--park-via-modify", action="store_true",
                   help="model risk-off pulls as an OTM park via batchModify (fast path)")
    p.add_argument("--half-spread-bps", type=float, default=4.0)
    p.add_argument("--skew-bps", type=float, default=6.0)
    p.add_argument("--cancel-rate", type=float, default=0.0)
    p.add_argument("--feed-age-ms", type=float, default=400.0)
    p.add_argument("--feed-age-source", choices=("recorded", "fixed"), default="recorded",
                   help="use the record's r-t receive age, or force --feed-age-ms")
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
    start, end = resolve_range(args.start, args.end)
    explicit = []
    for t in args.tar:
        explicit.extend(tar_members(Path(t), args.coin, start, end))
    paths, tars = gather_sources(
        Path(args.data_dir),
        args.coin,
        start,
        end,
        archive_dir=None if args.no_auto_tar else Path(args.archive_dir),
        explicit_tars=explicit,
    )
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
        feed_age_from_record=(args.feed_age_source == "recorded"),
        place_modify_latency_ms=args.place_modify_latency_ms,
        cancel_latency_ms=args.cancel_latency_ms,
        park_via_modify=args.park_via_modify,
        cancel_rate=args.cancel_rate,
        maker_fee_bps=args.maker_fee_bps,
    )
    print(format_markdown(metrics, args))
    if args.csv:
        write_csv(Path(args.csv), [metrics], args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
