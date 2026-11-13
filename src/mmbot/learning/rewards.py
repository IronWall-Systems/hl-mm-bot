"""Delayed reward alignment (30 s / 300 s) with markout protection.

Reward for a decision step = realised PnL from fills in the horizon
                              + mark-to-market of the inventory at the horizon
                              - fees
The mark-to-market term is clipped at ``protection_cap_usd`` so one adverse
gap cannot dominate a minibatch (the "markout protection" term).  Fills are
attributed to every decision whose horizon contains them; this is intentional
(overlapping decisions are separate counterfactual samples), not a ledger.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

DEFAULT_HORIZONS: Tuple[int, int] = (30, 300)


def compute_step_reward(*, mid_t: float, mid_h: float, position: float,
                        realized: float, fees: float,
                        protection_cap_usd: float) -> float:
    """Pure reward function used by the tracker and the unit tests."""
    markout = position * (mid_h - mid_t)
    cap = abs(float(protection_cap_usd))
    if cap > 0:
        markout = max(-cap, min(cap, markout))
    return float(realized + markout - fees)


@dataclass
class _Pending:
    step_id: int
    ts: float
    mid: float
    position: float
    action: str
    settled: set = field(default_factory=set)


class RewardTracker:
    """Accumulates marks/fills and settles pending decisions when horizons elapse."""

    def __init__(self, buffer, horizons: Tuple[int, ...] = DEFAULT_HORIZONS,
                 protection_cap_usd: float = 0.10,
                 now_fn=None, max_marks: int = 200_000):
        self.buffer = buffer
        self.horizons = tuple(int(h) for h in horizons)
        self.protection_cap_usd = float(protection_cap_usd)
        self.now = now_fn
        self.max_marks = int(max_marks)
        # Parallel timestamp/price lists keep settlement O(log n): the old
        # deque rebuilt a full timestamp list for every pending step, which is
        # O(pending * marks) per poll on a 24 h buffer.
        self._mid_ts: List[float] = []
        self._mid_px: List[float] = []
        self._fill_ts: List[float] = []
        self._fill_realized: List[float] = []
        self._fill_fee: List[float] = []
        self._pending: Dict[int, _Pending] = {}
        self._pos = 0.0
        self._avg = 0.0

    # ---- inputs ----------------------------------------------------------
    def on_mark(self, ts: float, mid: float) -> None:
        if mid <= 0:
            return
        if self._mid_ts and ts < self._mid_ts[-1]:
            return
        self._mid_ts.append(float(ts))
        self._mid_px.append(float(mid))
        if len(self._mid_ts) > self.max_marks:
            drop = len(self._mid_ts) - self.max_marks
            del self._mid_ts[:drop]
            del self._mid_px[:drop]

    def on_fill(self, ts: float, side: str, px: float, sz: float, fee: float) -> float:
        """Update inventory at average cost and record the realised delta."""
        qty = float(sz) if str(side).lower() == "buy" else -float(sz)
        realized = 0.0
        if qty == 0:
            return 0.0
        if self._pos == 0.0 or (self._pos > 0) == (qty > 0):
            self._avg = ((self._avg * abs(self._pos) + float(px) * abs(qty))
                         / (abs(self._pos) + abs(qty)))
        else:
            closed = min(abs(qty), abs(self._pos))
            realized = (float(px) - self._avg) * closed * (1 if self._pos > 0 else -1)
            if abs(qty) > abs(self._pos):
                self._avg = float(px)                  # flipped: new basis
        self._pos += qty
        if abs(self._pos) < 1e-12:
            self._pos, self._avg = 0.0, 0.0
        self._fill_ts.append(float(ts))
        self._fill_realized.append(float(realized))
        self._fill_fee.append(float(fee))
        return float(realized)

    def on_decision(self, step_id: int, ts: float, mid: float,
                    position: float, action: str) -> None:
        self._pending[int(step_id)] = _Pending(int(step_id), float(ts), float(mid),
                                               float(position), str(action))

    # ---- settlement ------------------------------------------------------
    def _mid_at(self, target: float) -> Optional[float]:
        if not self._mid_ts:
            return None
        i = bisect.bisect_left(self._mid_ts, target)
        if i >= len(self._mid_ts):
            return None
        return self._mid_px[i]

    def _fill_sum(self, lo: float, hi: float) -> Tuple[float, float]:
        a = bisect.bisect_right(self._fill_ts, lo)
        b = bisect.bisect_right(self._fill_ts, hi)
        return (sum(self._fill_realized[a:b]), sum(self._fill_fee[a:b]))

    def settle(self, now: Optional[float] = None) -> List[Tuple[int, int, float]]:
        now = (self.now() if self.now is not None else None) if now is None else now
        if now is None:
            return []
        out: List[Tuple[int, int, float]] = []
        done: List[int] = []
        for step_id, p in list(self._pending.items()):
            for h in self.horizons:
                if h in p.settled or now < p.ts + h:
                    continue
                target = p.ts + h
                mid_h = self._mid_at(target)
                if mid_h is None:
                    continue                            # wait for the next mark
                realized, fees = self._fill_sum(p.ts, target)
                reward = compute_step_reward(
                    mid_t=p.mid, mid_h=mid_h, position=p.position,
                    realized=realized, fees=fees,
                    protection_cap_usd=self.protection_cap_usd)
                self.buffer.set_reward(step_id, h, reward)
                p.settled.add(h)
                out.append((step_id, h, reward))
            if all(h in p.settled for h in self.horizons):
                done.append(step_id)
        for step_id in done:
            self._pending.pop(step_id, None)
        return out

    @property
    def pending(self) -> int:
        return len(self._pending)

    @property
    def inventory(self) -> float:
        return self._pos
