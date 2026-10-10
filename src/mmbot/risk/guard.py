"""Pre-trade checks + kill-switch decisions. Pure logic (clock injected)."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Optional

from ..models import PositionState, Quote, RiskConfig, Side


class KillReason(str, Enum):
    DRAWDOWN = "drawdown_from_peak"
    MAX_LOSS = "loss_from_start"
    LATENCY = "latency_spike"
    POSITION = "position_beyond_hard_cap"
    MARGIN = "margin_utilization"
    ERRORS = "consecutive_errors"
    EQUITY_UNREADABLE = "equity_unreadable"
    REJECT_WINDOW = "reject_window"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


class RiskGuard:
    def __init__(self, cfg: RiskConfig, start_equity: float, now_fn: Callable[[], float] = time.monotonic):
        if start_equity <= 0:
            raise ValueError("start_equity must be > 0 (equity unreadable?)")
        self.cfg = cfg
        self.now = now_fn
        self.start_equity = start_equity
        self.peak_equity = start_equity
        self.equity = start_equity
        self.lat_ema_ms = 0.0
        self.lat_breaches = 0
        self.consecutive_errors = 0
        self._actions: Deque[float] = deque()
        self._side_rejects = {Side.BUY: Deque[float](), Side.SELL: Deque[float]()}
        self._rejects_5m: Deque[float] = deque()   # T7b window: never cleared by successes

    # ---- inputs ----------------------------------------------------------------
    def update_equity(self, eq: float) -> None:
        self.equity = eq
        self.peak_equity = max(self.peak_equity, eq)

    def record_latency(self, ms: float) -> None:
        self.lat_ema_ms = ms if self.lat_ema_ms == 0 else 0.8 * self.lat_ema_ms + 0.2 * ms
        self.lat_breaches = self.lat_breaches + 1 if ms > self.cfg.max_latency_ms else 0

    def record_error(self) -> None:
        self.consecutive_errors += 1

    def record_success(self) -> None:
        self.consecutive_errors = 0

    @staticmethod
    def _prune(q: Deque[float], t: float, window: float) -> None:
        while q and t - q[0] > window:
            q.popleft()

    def record_reject(self, side: Side) -> None:
        """T7b: windowed place-reject counter. Successes never clear these; they age out."""
        t = self.now()
        self._prune(self._side_rejects[side], t, 60.0)
        self._side_rejects[side].append(t)
        self._prune(self._rejects_5m, t, 300.0)
        self._rejects_5m.append(t)

    def side_rejects_60s(self, side: Side) -> int:
        t = self.now()
        self._prune(self._side_rejects[side], t, 60.0)
        return len(self._side_rejects[side])

    def rejects_5m_count(self) -> int:
        t = self.now()
        self._prune(self._rejects_5m, t, 300.0)
        return len(self._rejects_5m)

    def record_actions(self, n: int = 1) -> None:
        t = self.now()
        for _ in range(n):
            self._actions.append(t)

    def actions_last_min(self) -> int:
        t = self.now()
        while self._actions and t - self._actions[0] > 60.0:
            self._actions.popleft()
        return len(self._actions)

    # ---- decisions -------------------------------------------------------------
    def check_kill(self, pos: PositionState, mid: float) -> Optional[KillReason]:
        c = self.cfg
        if self.equity <= 0:
            return KillReason.EQUITY_UNREADABLE
        if (self.peak_equity - self.equity) / self.peak_equity >= c.max_drawdown_pct:
            return KillReason.DRAWDOWN
        if (self.start_equity - self.equity) / self.start_equity >= c.max_loss_pct:
            return KillReason.MAX_LOSS
        if self.lat_breaches >= c.latency_breach_count:
            return KillReason.LATENCY
        if abs(pos.size * mid) > c.hard_position_mult * c.max_position_notional:
            return KillReason.POSITION
        if pos.margin_utilization >= c.kill_margin_utilization:
            return KillReason.MARGIN
        if self.consecutive_errors >= c.max_consecutive_errors:
            return KillReason.ERRORS
        if self.rejects_5m_count() >= c.kill_rejects_5m:
            return KillReason.REJECT_WINDOW
        return None

    def pre_trade(self, q: Quote, pos: PositionState, mid: float, resting_same_side_notional: float,
                  feed_age_s: float) -> Verdict:
        c = self.cfg
        order_n = q.price * q.size
        if order_n > c.max_order_notional:
            return Verdict(False, f"order notional {order_n:.2f} > max_order_notional")
        pos_n = pos.size * mid
        directional = pos_n if q.side is Side.BUY else -pos_n
        worst = directional + resting_same_side_notional + order_n
        if worst > c.max_position_notional:
            return Verdict(False, f"worst-case exposure {worst:.2f} > max_position_notional")
        if feed_age_s > c.stale_book_s:
            return Verdict(False, f"book stale {feed_age_s:.1f}s")
        if self.lat_ema_ms > c.max_latency_ms:
            return Verdict(False, f"latency ema {self.lat_ema_ms:.0f}ms too high")
        if pos.margin_utilization > c.max_margin_utilization:
            return Verdict(False, f"margin utilization {pos.margin_utilization:.2f}")
        if self.actions_last_min() + 1 > c.max_actions_per_min:
            return Verdict(False, "local action pacing limit")
        return Verdict(True)
