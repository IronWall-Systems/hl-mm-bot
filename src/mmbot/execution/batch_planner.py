from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

try:
    from .quote_bands import BandParams, dist_bps, effective_half_spread, target_px
except ImportError:
    from quote_bands import BandParams, dist_bps, effective_half_spread, target_px


@dataclass(frozen=True)
class LiveQuote:
    oid: object
    px: float
    sz: float
    age_s: float


@dataclass
class Leg:
    side: str
    kind: str                    # modify | place | cancel
    px: Optional[float]
    sz: Optional[float]
    necessary: bool
    why: str                     # near | pull-park | pull | far | far-very | piggy | place


@dataclass
class Payload:
    kind: str                    # modify | place | cancel
    legs: List[Leg] = field(default_factory=list)


@dataclass(frozen=True)
class BatchParams:
    piggy_near_frac: float = 0.5
    piggy_far_frac: float = 0.3
    piggy_min_bps: float = 2.0
    max_defer_s: float = 300.0
    very_far_mult: float = 1.5
    park_mult: float = 4.0
    pull_mode: str = "park"      # park | cancel
    min_gap_near_s: float = 5.0
    min_gap_far_s: float = 35.0


class BatchPlanner:
    def __init__(self, budget, bands: BandParams = BandParams(), params: BatchParams = BatchParams(),
                 charge_mode: str = "call", now: Callable[[], float] = time.monotonic):
        assert charge_mode in ("call", "leg")
        self.b, self.bands, self.q, self.mode, self.now = budget, bands, params, charge_mode, now
        self._last_sent: Dict[str, float] = {}
        self._blocked_until: Dict[str, float] = {}
        self._acts: Deque[Tuple[float, int]] = deque()

    def _cost(self, n_legs: int) -> int:
        return 1 if self.mode == "call" else n_legs

    def rate_per_h(self, window_s: float = 3600.0) -> float:
        t = self.now()
        while self._acts and t - self._acts[0][0] > window_s:
            self._acts.popleft()
        first = self._acts[0][0] if self._acts else t
        span = min(window_s, max(600.0, t - first))
        return sum(c for _, c in self._acts) / (span / 3600.0)

    def h_eff(self) -> float:
        return effective_half_spread(self.bands.half_spread_bps, self.rate_per_h(), self.b.allowed_per_h())

    def on_sent(self, payload: Payload) -> None:
        """Call after EVERY request actually sent (including rejected ones and degraded cancels)."""
        t = self.now()
        self._last_sent[payload.kind] = t
        cost = self._cost(len(payload.legs))
        self._acts.append((t, cost))
        self.b.record(cost)
        for leg in payload.legs:
            if leg.why.startswith("pull"):
                self._blocked_until[leg.side] = t + self.bands.pull_resume_s

    def plan(self, live: Dict[str, Optional[LiveQuote]], ref: float, tgt_sz: Dict[str, float],
             urgent_risk: bool = False, skip: frozenset = frozenset()) -> List[Payload]:
        """tgt_sz: sizes for the sides that may quote (a side missing/0 here is disabled, e.g. inventory cap).
        skip: sides with a request still in flight (do nothing there until it resolves)."""
        t, p, q = self.now(), self.bands, self.q
        H = self.h_eff()
        nec: Dict[str, Leg] = {}
        opt: Dict[str, Leg] = {}
        piggy: Dict[str, Leg] = {}
        places: List[Leg] = []

        for side in ("buy", "sell"):
            if side in skip or t < self._blocked_until.get(side, 0.0):
                continue
            lq, sz, tgt = live.get(side), tgt_sz.get(side), target_px(side, ref, H)
            if lq is not None and not sz:                  # side disabled (inventory cap): pull it
                nec[side] = Leg(side, "cancel", None, None, True, "pull")
                continue
            if lq is None:
                if sz:
                    places.append(Leg(side, "place", tgt, sz, True, "place"))
                continue
            d = dist_bps(side, lq.px, ref)
            a = min(p.near_frac * H, p.near_max_bps)
            far = max(p.far_frac * H, p.far_min_bps)
            new_sz = sz or lq.sz
            if urgent_risk or d < H * p.pull_frac:
                if q.pull_mode == "park":
                    nec[side] = Leg(side, "modify", target_px(side, ref, H * q.park_mult), lq.sz, True, "pull-park")
                else:
                    nec[side] = Leg(side, "cancel", None, None, True, "pull")
            elif d < H - a:
                nec[side] = Leg(side, "modify", tgt, new_sz, True, "near")
            elif d > H + far:
                very = d > H + q.very_far_mult * far or lq.age_s >= q.max_defer_s
                (nec if very else opt)[side] = Leg(side, "modify", tgt, new_sz, very, "far-very" if very else "far")
            else:
                near_slack = d - (H - a)
                far_slack = (H + far) - d
                about_to_fire = near_slack <= q.piggy_near_frac * a or far_slack <= q.piggy_far_frac * far
                if about_to_fire and abs(tgt - lq.px) / ref * 1e4 >= q.piggy_min_bps:
                    piggy[side] = Leg(side, "modify", tgt, new_sz, False, "piggy")

        mod_legs = [l for l in nec.values() if l.kind == "modify"]
        cancel_legs = [l for l in nec.values() if l.kind == "cancel"]
        if opt and (mod_legs or len(opt) >= 2 or piggy):
            mod_legs += [l for s, l in opt.items() if s not in nec]
        if mod_legs:
            taken = {l.side for l in mod_legs}
            mod_legs += [l for s, l in piggy.items() if s not in taken]

        out: List[Payload] = []
        for kind, legs in (("cancel", cancel_legs), ("modify", mod_legs), ("place", places)):
            if not legs:
                continue
            urgent = any(l.why in ("near", "pull", "pull-park", "place") for l in legs)
            gap = q.min_gap_near_s if urgent else q.min_gap_far_s
            if t - self._last_sent.get(kind, -1e9) < gap:
                continue
            cost = self._cost(len(legs))
            ok = self.b.may_send("cancel", cost) if kind == "cancel" else self.b.may_send("quote", cost)
            if ok:
                out.append(Payload(kind, legs))
            elif kind == "modify":
                danger = [l for l in legs if l.why in ("near", "pull-park")]
                if danger and self.b.may_send("cancel", self._cost(len(danger))):
                    out.append(Payload("cancel", [Leg(l.side, "cancel", None, None, True, "pull") for l in danger]))
        return out


# ---------------------------------------------------------------------------
# Minimal executable contract smoke test (the authoritative class above is
# unmodified; this guard only lets `python3 batch_planner.py` verify it loads
# and produces the expected payload shape).
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    from quote_bands import RequestBudget

    clock = [0.0]
    b = RequestBudget(now=lambda: clock[0])
    b.update_from_api({"nRequestsUsed": 0, "nRequestsCap": 10000, "cumVlm": 0.0})
    bp = BatchPlanner(b, charge_mode="call", now=lambda: clock[0])
    ref = 100.0
    out = bp.plan({}, ref, {"buy": 0.15, "sell": 0.15})
    assert len(out) == 1 and out[0].kind == "place" and len(out[0].legs) == 2, out
    lq = {"buy": LiveQuote(1, target_px("buy", ref, 10.0), 0.15, 0.0), "sell": None}
    out = bp.plan({"buy": lq["buy"], "sell": None}, ref, {"buy": 0.15, "sell": 0.15})
    assert any(p.kind == "modify" and any(l.why == "near" for l in p.legs) for p in out), out
    clock[0] += 10.0
    out = bp.plan({"buy": None, "sell": None}, ref, {"buy": 0.0, "sell": 0.15})
    assert not any(any(l.side == "buy" for l in p.legs) for p in out), out
    print("batch_planner authoritative smoke test: PASS")
