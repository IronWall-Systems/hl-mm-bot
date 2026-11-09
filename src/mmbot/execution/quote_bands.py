"""Budget-aware quote bands, EWMA reference price, and request-budget guard.

This is the dependency module for :mod:`batch_planner`.  It is deliberately
pure Python (stdlib only) so it can be imported both as
``mmbot.execution.quote_bands`` and as a top-level ``quote_bands`` when
``batch_planner.py`` is executed directly.

Design
------
The quote ladder has three characteristic distances from the reference price
``ref`` (all in basis points):

* ``pull``  = ``H * pull_frac``            -- closer than this is unsafe -> PULL
* ``near``  = ``H - min(near_frac*H, near_max_bps)`` -- tighten-to band
* ``far``   = ``H + max(far_frac*H, far_min_bps)``   -- widen-to band

``H`` is the configured half-spread (``BandParams.half_spread_bps``).  With the
defaults ``H=20``, ``near=13``, ``far=40`` and ``pull=9``.

The planner snaps a live quote to the nearest valid band edge.  When the
request budget is under pressure, ``RequestBudget.may_send("quote")`` returns
``False`` and the planner turns near/unsafe legs into protective cancels, so a
degraded bot stops consuming its address budget rather than repeatedly
modifying quotes.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Deque, Optional, Tuple

__all__ = [
    "BandParams",
    "dist_bps",
    "target_px",
    "effective_half_spread",
    "EWMARef",
    "RequestBudget",
    "Act",
    "QuotePlanner",
]


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BandParams:
    """Static band geometry.  All distances are in basis points."""

    half_spread_bps: float = 20.0
    near_frac: float = 0.35
    near_max_bps: float = 7.0
    far_frac: float = 1.0
    far_min_bps: float = 20.0
    pull_frac: float = 0.45
    pull_resume_s: float = 20.0

    # -- derived geometry ---------------------------------------------------
    @property
    def near_edge_bps(self) -> float:
        return self.half_spread_bps - min(self.near_frac * self.half_spread_bps,
                                          self.near_max_bps)

    @property
    def far_edge_bps(self) -> float:
        return self.half_spread_bps + max(self.far_frac * self.half_spread_bps,
                                          self.far_min_bps)

    @property
    def pull_edge_bps(self) -> float:
        return self.half_spread_bps * self.pull_frac


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _s(side: Any) -> str:
    """Normalise a side to ``"buy"`` / ``"sell"`` (accepts Side enums)."""
    v = getattr(side, "value", side)
    v = str(v).lower()
    if v in ("b", "bid", "buy", "long"):
        return "buy"
    if v in ("a", "ask", "sell", "short"):
        return "sell"
    raise ValueError(f"unknown side: {side!r}")


def dist_bps(side: str, quote_px: float, ref_px: float) -> float:
    """Signed distance of ``quote_px`` from ``ref_px`` in bps.

    Positive means the quote is *away* from the reference on its own side:

    * buy:  ``(ref - quote) / ref * 1e4``  (positive when quote < ref)
    * sell: ``(quote - ref) / ref * 1e4``  (positive when quote > ref)
    """
    if ref_px <= 0:
        raise ValueError("ref_px must be > 0")
    if _s(side) == "buy":
        return (ref_px - quote_px) / ref_px * 1e4
    return (quote_px - ref_px) / ref_px * 1e4


def target_px(side: str, ref_px: float, half_spread_bps: float) -> float:
    """Price at a given half-spread distance from ``ref_px``."""
    if _s(side) == "buy":
        return ref_px * (1.0 - half_spread_bps / 1e4)
    return ref_px * (1.0 + half_spread_bps / 1e4)


def effective_half_spread(base_h_bps: float, recent_rate_per_h: float,
                          allowed_per_h: float, max_mult: float = 2.0) -> float:
    """Widen smoothly from ``base_h_bps`` up to ``base_h_bps * max_mult``.

    No widening while the recent request rate is at or below the sustainable
    allowance.  Above it the multiplier saturates smoothly towards ``max_mult``
    (never a step change), so the live spread walks wider instead of jumping.
    """
    base = max(0.0, float(base_h_bps))
    allowed = float(allowed_per_h)
    if allowed <= 0.0:
        return base * max_mult
    recent = max(0.0, float(recent_rate_per_h))
    if recent <= allowed:
        return base
    excess = recent - allowed
    # 0 at the allowance, ->1 as excess dominates; smooth and monotone.
    frac = excess / (allowed + excess)
    return base * (1.0 + (max_mult - 1.0) * frac)


# ---------------------------------------------------------------------------
# EWMA reference price (irregular tick safe)
# ---------------------------------------------------------------------------
class EWMARef:
    """Time-decayed EWMA of the mid price.

    ``alpha = 1 - exp(-dt / tau_s)`` so the effective memory is a wall-clock
    time constant, not a tick count.  ``update`` returns the new value.
    """

    __slots__ = ("tau_s", "value", "_ts")

    def __init__(self, tau_s: float = 3.0):
        if tau_s <= 0:
            raise ValueError("tau_s must be > 0")
        self.tau_s = float(tau_s)
        self.value: Optional[float] = None
        self._ts: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def reset(self) -> None:
        self.value = None
        self._ts = None

    def update(self, mid_px: float, now: Optional[float] = None) -> float:
        px = float(mid_px)
        if not math.isfinite(px) or px <= 0:
            if self.value is None:
                raise ValueError("first mid_px must be finite and > 0")
            return self.value
        t = time.monotonic() if now is None else float(now)
        if self.value is None or self._ts is None:
            self.value, self._ts = px, t
            return self.value
        dt = max(0.0, t - self._ts)
        alpha = 1.0 - math.exp(-dt / self.tau_s) if dt > 0 else 1.0
        self.value += alpha * (px - self.value)
        self._ts = t
        return self.value


# ---------------------------------------------------------------------------
# Request budget
# ---------------------------------------------------------------------------
class RequestBudget:
    """Tracks Hyperliquid's address-based request budget.

    The exchange counters are authoritative (``userRateLimit`` ->
    ``nRequestsUsed`` / ``nRequestsCap`` / ``cumVlm``).  ``/info`` reads are
    free and do not increment ``nRequestsUsed``; only order actions do.
    ``record`` keeps an optimistic local counter so the bot stops *before* the
    exchange rejects, not after.
    """

    SAFETY_RESERVE = 500          # requests never spent by the quoting loop
    CANCEL_FLOOR = 100            # protective cancels allowed above this headroom
    DEFAULT_HORIZON_H = 96.0      # amortise the remaining surplus over 4 days
    TOKEN_CAP = 25.0              # maximum burst bucket
    TOKEN_START = 15.0            # initial burst allowance

    def __init__(self, reserve: int = SAFETY_RESERVE,
                 horizon_h: float = DEFAULT_HORIZON_H,
                 now: Callable[[], float] = time.monotonic):
        self.reserve = int(reserve)
        self.horizon_h = float(horizon_h)
        self._now = now
        self.nRequestsUsed: int = 0
        self.nRequestsCap: int = 0
        self.cumVlm: float = 0.0
        self._api_synced = False
        self._last_api_used = 0
        self._vol_hist: Deque[Tuple[float, float]] = deque()
        self._send_hist: Deque[Tuple[float, int]] = deque()
        # Token bucket: continuous burst control instead of an extrapolated
        # trailing rate that hard-blocks after the first minute.
        self._tokens: float = float(self.TOKEN_START)
        self._token_ts: Optional[float] = None
        self.last_verdict: Optional[str] = None

    # -- authoritative sync --------------------------------------------------
    def update_from_api(self, user_rate_limit_resp: dict) -> None:
        """Feed a ``/info {"type":"userRateLimit"}`` response."""
        def _num(key: str, default: float = 0.0) -> float:
            try:
                return float(user_rate_limit_resp.get(key, default))
            except (TypeError, ValueError):
                return default

        used = int(_num("nRequestsUsed"))
        cap = int(_num("nRequestsCap"))
        vlm = _num("cumVlm")
        now = self._now()
        # The exchange counter is monotonically increasing.  Keep our optimistic
        # local count if the poll is lagging behind requests we just sent.
        self.nRequestsUsed = max(self.nRequestsUsed, used)
        self._last_api_used = used
        self.nRequestsCap = cap
        self.cumVlm = vlm
        self._api_synced = True
        if self._token_ts is None:
            self._token_ts = now
        self._vol_hist.append((now, vlm))
        while self._vol_hist and now - self._vol_hist[0][0] > 3600.0:
            self._vol_hist.popleft()

    # -- local optimistic accounting ----------------------------------------
    def _refill(self) -> None:
        """Continuously refill the burst bucket at the sustainable rate."""
        now = self._now()
        if self._token_ts is None:
            self._token_ts = now
            return
        dt = max(0.0, now - self._token_ts)
        self._token_ts = now
        if dt <= 0.0:
            return
        self._tokens = min(self.TOKEN_CAP,
                           self._tokens + self.allowed_per_h() / 3600.0 * dt)

    def record(self, cost: int = 1) -> None:
        c = max(0, int(cost))
        if c == 0:
            return
        now = self._now()
        self._refill()
        self._tokens = max(0.0, self._tokens - c)
        self.nRequestsUsed += c
        self._send_hist.append((now, c))
        while self._send_hist and now - self._send_hist[0][0] > 3600.0:
            self._send_hist.popleft()

    @property
    def used(self) -> int:
        return self.nRequestsUsed

    @property
    def headroom(self) -> int:
        if self.nRequestsCap <= 0:
            return 0
        return max(0, self.nRequestsCap - self.used)

    @property
    def surplus(self) -> int:
        return self.headroom

    # -- rates ---------------------------------------------------------------
    def earned_per_h(self) -> float:
        """Recent cumulative-volume growth, converted to request credits/hour."""
        if len(self._vol_hist) < 2:
            return 0.0
        (t0, v0), (t1, v1) = self._vol_hist[0], self._vol_hist[-1]
        dt_h = (t1 - t0) / 3600.0
        if dt_h <= 0:
            return 0.0
        return max(0.0, v1 - v0) / dt_h

    def burn_per_h(self) -> float:
        """Optimistic request burn over the trailing hour (or sample window)."""
        if not self._send_hist:
            return 0.0
        now = self._now()
        while self._send_hist and now - self._send_hist[0][0] > 3600.0:
            self._send_hist.popleft()
        if not self._send_hist:
            return 0.0
        total = sum(c for _, c in self._send_hist)
        # Minimum 10 min window, aligned with BatchPlanner.rate_per_h's span
        # floor, so reporting does not extrapolate a startup burst.
        span_h = max((now - self._send_hist[0][0]) / 3600.0, 600.0 / 3600.0)
        return total / span_h

    def allowed_per_h(self) -> float:
        """Sustainable hourly allowance: surplus amortised + volume earned."""
        if self.nRequestsCap <= 0:
            return 0.0
        amortisable = max(0, self.nRequestsCap - self.used - self.reserve)
        return amortisable / max(1.0, self.horizon_h) + self.earned_per_h()

    # -- policy --------------------------------------------------------------
    @property
    def mode(self) -> str:
        if self.nRequestsCap <= 0:
            return "UNKNOWN"          # pre-first-poll: fail open for startup only
        h = self.headroom
        if h <= self.CANCEL_FLOOR:
            return "STANDDOWN"
        if h <= self.reserve:
            return "DEGRADED"
        self._refill()
        if self._tokens < 1.0:
            return "THROTTLED"
        return "NORMAL"

    def may_send(self, kind: str, cost: int = 1) -> bool:
        """Gate an outbound action with a continuous token bucket.

        * Quotes/places/modifies need headroom above the safety reserve AND
          enough burst tokens; otherwise the planner degrades dangerous legs
          into cancels rather than going silent for 14 minutes.
        * Cancels (including inventory-cap pulls) only need headroom above the
          emergency floor, so protective actions are never starved.
        """
        k = str(kind).lower()
        c = max(1, int(cost))
        if not self._api_synced or self.nRequestsCap <= 0:
            return True
        if k in ("cancel", "cancel_oids", "flatten", "cleanup", "pull"):
            return self.headroom > self.CANCEL_FLOOR
        self._refill()
        return self.headroom > self.reserve and self._tokens >= c

    def note_verdict(self, verdict: str) -> None:
        self.last_verdict = verdict


def calibrate_costs(before_used: int, after_used: int, n_legs: int) -> str:
    """Infer whether Hyperliquid meters a batch as one call or per leg.

    Returns ``"call"``, ``"leg"``, ``"unknown"`` (no delta observed -- the API
    poll may lag), or ``"mixed"`` (delta does not match either model).
    """
    delta = int(after_used) - int(before_used)
    if delta <= 0:
        return "unknown"
    if delta == 1:
        return "call"
    if n_legs > 1 and delta == n_legs:
        return "leg"
    return "mixed"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
class Act(Enum):
    NONE = "none"
    PLACE = "place"
    MOVE_NEAR = "move_near"
    MOVE_FAR = "move_far"
    PULL = "pull"


class QuotePlanner:
    """Per-side band decision engine.

    ``decide`` snaps a live quote to the nearest valid edge, with a small
    deadband so millisecond tick noise does not cause a requote.  A quote
    closer than the pull edge, a crossed quote, or a denied budget yields
    ``PULL``.
    """

    def __init__(self, budget: RequestBudget, bands: BandParams = BandParams(),
                 now: Callable[[], float] = time.monotonic):
        self.budget = budget
        self.bands = bands
        self._now = now
        self.urgent = False
        self._last_sent: dict[str, float] = {}
        self._pulled_at: dict[str, float] = {}
        # 0.4 bps at H=20: far smaller than the selftest's 1 bps / 4 bps gaps.
        self.tolerance_bps = max(0.1, 0.02 * max(1e-9, bands.half_spread_bps))

    # -- introspection -------------------------------------------------------
    @property
    def near_edge_bps(self) -> float:
        return self.bands.near_edge_bps

    @property
    def far_edge_bps(self) -> float:
        return self.bands.far_edge_bps

    @property
    def pull_edge_bps(self) -> float:
        return self.bands.pull_edge_bps

    # -- decision ------------------------------------------------------------
    def decide(self, side: str, live_px: Optional[float],
               ref: float) -> Tuple[Act, Optional[float]]:
        s = _s(side)
        if ref is None or not math.isfinite(float(ref)) or ref <= 0:
            return Act.NONE, None
        ref = float(ref)
        now = self._now()

        if live_px is None:
            # Re-quote cooldown after a protective pull.
            pulled = self._pulled_at.get(s)
            if pulled is not None and now - pulled < self.bands.pull_resume_s:
                return Act.NONE, None
            if not self.budget.may_send("quote"):
                return Act.NONE, None
            edge = self.far_edge_bps if self.urgent else self.bands.half_spread_bps
            return Act.PLACE, target_px(s, ref, edge)

        d = dist_bps(s, float(live_px), ref)
        if not math.isfinite(d):
            return Act.NONE, None

        # Crossed / inside the pull band: neutralise immediately.
        if d < 0 or d < self.pull_edge_bps:
            self._pulled_at[s] = now
            return Act.PULL, None

        # Budget gate: refuse repricing but still allow the pull/cancel.
        if not self.budget.may_send("quote"):
            self._pulled_at[s] = now
            return Act.PULL, None

        near = self.near_edge_bps
        far = self.far_edge_bps
        tol = self.tolerance_bps

        if self.urgent:
            if abs(d - far) <= tol:
                return Act.NONE, None
            return Act.MOVE_FAR, target_px(s, ref, far)

        dn, df = abs(d - near), abs(d - far)
        if dn <= tol or df <= tol:
            return Act.NONE, None
        if dn <= df:
            return Act.MOVE_NEAR, target_px(s, ref, near)
        return Act.MOVE_FAR, target_px(s, ref, far)

    def on_sent(self, side: str) -> None:
        s = _s(side)
        self._last_sent[s] = self._now()
        # A successful (re)quote ends the pull cooldown for that side.
        self._pulled_at.pop(s, None)

    def on_pull(self, side: str) -> None:
        self._pulled_at[_s(side)] = self._now()

    def clear_pull(self, side: str) -> None:
        self._pulled_at.pop(_s(side), None)
