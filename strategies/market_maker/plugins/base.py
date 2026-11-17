"""Abstract cooldown / circuit-breaker contract for the MM strategy.

A plugin owns three responsibilities:

1. **Evaluate loss** - turn account-equity (or explicit realized-PnL) observations
   into a *cumulative session loss* figure.
2. **Check timestamps** - decide whether ``now`` falls inside a persisted cooldown
   window, so a restart cannot reset the timer.
3. **Block/allow execution** - return a :class:`CooldownDecision` that the engine
   adapter (``integration.py``) converts into an order veto.

The base class implements the shared state machine; concrete plugins only declare
``plugin_name``, ``cooldown_hours`` and (optionally) ``blocks``.
"""
from __future__ import annotations

import logging
import time
from abc import ABC
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, ClassVar, Deque, Optional

from .state import CooldownState, CooldownStateStore, default_state_path

log = logging.getLogger("mmbot.cooldown")

#: Default trigger: a net loss of at least $0.25.
DEFAULT_LOSS_THRESHOLD_USD = 0.25
_EPS = 1e-9


class TriggerAction(str, Enum):
    """What the engine should do when a cooldown arms."""

    CANCEL_ALL = "cancel_all"      # legacy: withdraw both quote sides
    FLATTEN = "flatten"            # market-close inventory now, then pause
    REDUCE_ONLY = "reduce_only"    # keep only the risk-reducing quote side


@dataclass(frozen=True)
class CooldownDecision:
    """Result of evaluating a plugin at one instant."""

    allowed: bool
    reason: str = ""
    cooldown_until: float = 0.0
    remaining_s: float = 0.0
    block_index: int = 0
    blocks_total: int = 1
    cumulative_loss_usd: float = 0.0
    action: str = TriggerAction.CANCEL_ALL.value
    loss_window_s: float = 0.0


class BaseCooldownPlugin(ABC):
    """Shared implementation; subclasses set the three class attributes below.

    ``loss_window_s = 0`` keeps the original cumulative-session-loss behaviour.
    A positive value switches the trigger to a rolling high-water-mark loss over
    that many seconds (the continuous MM model uses 3600 s).
    """

    plugin_name: ClassVar[str] = "base"
    cooldown_hours: ClassVar[float] = 0.0
    blocks: ClassVar[int] = 1
    loss_window_s: ClassVar[float] = 0.0
    action: ClassVar[str] = TriggerAction.CANCEL_ALL.value

    def __init__(
        self,
        *,
        state_path: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
        loss_threshold_usd: float = DEFAULT_LOSS_THRESHOLD_USD,
        cooldown_hours: Optional[float] = None,
        loss_window_s: Optional[float] = None,
        action: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        persist: bool = True,
    ) -> None:
        if self.blocks < 1:
            raise ValueError("blocks must be >= 1")
        self.clock = clock
        self.loss_threshold_usd = float(loss_threshold_usd)
        self.cooldown_hours = float(
            self.cooldown_hours if cooldown_hours is None else cooldown_hours
        )
        if self.cooldown_hours <= 0:
            raise ValueError(f"{self.plugin_name}: cooldown_hours must be > 0")
        self.loss_window_s = float(
            self.loss_window_s if loss_window_s is None else loss_window_s
        )
        if self.loss_window_s < 0:
            raise ValueError("loss_window_s must be >= 0")
        self.action = TriggerAction(action or self.action).value
        self.block_hours = self.cooldown_hours / self.blocks
        self.log = logger or log
        self.store = CooldownStateStore(
            Path(state_path) if state_path is not None else default_state_path(self.plugin_name),
            self.plugin_name,
            persist=persist,
        )
        self.state: CooldownState = self.store.load()
        self.state.plugin = self.plugin_name
        # Rolling 60-minute MTM high-water-mark window (in-memory; rebuilt on restart).
        self._eq_window: Deque[tuple[float, float]] = deque()
        self._max_window: Deque[tuple[float, float]] = deque()
        # If a persisted window has already expired, clear it on startup.
        if self.state.cooldown_until and self.clock() >= self.state.cooldown_until:
            self._clear_expired(self.clock(), save=True)

    # ---- inputs ---------------------------------------------------------------
    def observe_equity(self, equity: float) -> None:
        """Feed the latest account equity (USD).

        Non-positive equity is ignored: the core ``RiskGuard`` already kills on
        unreadable equity, and treating a failed read as a total loss would arm
        the breaker spuriously.
        """
        equity = float(equity)
        if equity <= 0:
            return
        changed = False
        if self.state.session_start_equity is None:
            self.state.session_start_equity = equity
            changed = True
        self.state.last_equity = equity
        if self.loss_window_s > 0:
            self._record_rolling_equity(self.clock(), equity)
        if changed:
            self._save()
        # Arm immediately on breach instead of waiting for the next quote attempt.
        self.evaluate()

    def note_realized_pnl(self, delta_usd: float) -> None:
        """Optional hook for engines that can report realized PnL directly."""
        self.state.session_realized_pnl += float(delta_usd)
        self.evaluate()

    # ---- loss accounting ------------------------------------------------------
    def _record_rolling_equity(self, now: float, equity: float) -> None:
        """Maintain trailing-window equity and its monotonic maximum."""
        self._eq_window.append((now, equity))
        while self._max_window and self._max_window[-1][1] <= equity:
            self._max_window.pop()
        self._max_window.append((now, equity))
        self._prune_rolling(now)

    def _prune_rolling(self, now: float) -> None:
        cutoff = now - self.loss_window_s
        while self._eq_window and self._eq_window[0][0] < cutoff:
            self._eq_window.popleft()
        while self._max_window and self._max_window[0][0] < cutoff:
            self._max_window.popleft()

    def rolling_loss_usd(self) -> float:
        """Equity drop from the trailing-window high-water mark."""
        if self.loss_window_s <= 0:
            return 0.0
        self._prune_rolling(self.clock())
        if not self._max_window or self.state.last_equity is None:
            return 0.0
        return max(0.0, self._max_window[0][1] - self.state.last_equity)

    def current_loss_usd(self) -> float:
        """Trigger metric: rolling MTM loss when configured, else session loss."""
        return self.rolling_loss_usd() if self.loss_window_s > 0 else self.cumulative_loss_usd()

    def cumulative_loss_usd(self) -> float:
        """Cumulative session loss, never negative.

        Uses the larger of (a) realized-PnL loss and (b) equity loss vs the session
        baseline.  ``max`` avoids double-counting when both sources are fed.
        """
        realized_loss = max(0.0, -self.state.session_realized_pnl)
        equity_loss = 0.0
        if (
            self.state.session_start_equity is not None
            and self.state.last_equity is not None
            and self.state.session_start_equity > 0
        ):
            equity_loss = max(0.0, self.state.session_start_equity - self.state.last_equity)
        return max(realized_loss, equity_loss)

    # ---- decisions ------------------------------------------------------------
    def evaluate(self) -> CooldownDecision:
        """Advance the state machine, arming/expiring as needed, and return the verdict."""
        now = self.clock()
        loss = self.current_loss_usd()

        if self.state.cooldown_until and now >= self.state.cooldown_until:
            self._clear_expired(now, save=True)

        # A genuine recovery below the threshold starts a new loss episode.  This
        # also runs while blocked so a later, independent breach can re-arm.
        if loss < self.loss_threshold_usd and self.state.loss_at_last_trigger > 0.0:
            self.state.loss_at_last_trigger = 0.0
            self._save()

        if self.state.cooldown_until > now:
            self._advance_blocks(now)
            remaining = self.state.cooldown_until - now
            block = min(self.state.block_index, self.blocks)
            return CooldownDecision(
                allowed=False,
                reason=(
                    f"cooldown:{self.plugin_name} active "
                    f"({remaining / 3600.0:.2f}h remaining, "
                    f"block {block}/{self.blocks}, action={self.action})"
                ),
                cooldown_until=self.state.cooldown_until,
                remaining_s=remaining,
                block_index=block,
                blocks_total=self.blocks,
                cumulative_loss_usd=loss,
                action=self.action,
                loss_window_s=self.loss_window_s,
            )

        # Arm once per loss episode: a flat loss above the watermark (already armed)
        # must not immediately re-arm after the window expires.
        if loss >= self.loss_threshold_usd and loss > self.state.loss_at_last_trigger + _EPS:
            self.arm(loss, reason="rolling_mtm_loss" if self.loss_window_s > 0 else "session_loss")
            return self.evaluate()

        return CooldownDecision(
            allowed=True,
            block_index=self.state.block_index,
            blocks_total=self.blocks,
            cumulative_loss_usd=loss,
            action=self.action,
            loss_window_s=self.loss_window_s,
        )

    def is_blocked(self) -> bool:
        return not self.evaluate().allowed

    def arm(self, loss_usd: Optional[float] = None, *, reason: str = "session_loss") -> CooldownDecision:
        """Start (or extend) a cooldown window from ``now``."""
        now = self.clock()
        loss = self.current_loss_usd() if loss_usd is None else float(loss_usd)
        until = now + self.cooldown_hours * 3600.0
        extended = until > self.state.cooldown_until
        if extended:
            self.state.cooldown_started_at = now
            self.state.cooldown_until = until
            self.state.block_index = 0
        self.state.trigger_loss_usd = loss
        self.state.trigger_reason = reason
        self.state.loss_at_last_trigger = max(self.state.loss_at_last_trigger, loss)
        self.state.armed_count += 1
        self.state.updated_at = now
        self._save()
        self.log.warning(
            "[COOLDOWN] %s armed (%s, %s, window=%.0fs): loss $%.4f >= $%.2f; "
            "blocked for %.2fh until %s (block 0/%d)",
            self.plugin_name,
            reason,
            self.action,
            self.loss_window_s,
            loss,
            self.loss_threshold_usd,
            self.cooldown_hours,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.state.cooldown_until)),
            self.blocks,
        )
        return self.evaluate()

    def reset(self) -> None:
        """Clear session accounting and any active cooldown (operator action)."""
        self.state = CooldownState(plugin=self.plugin_name)
        self._eq_window.clear()
        self._max_window.clear()
        self._save()
        self.log.warning("[COOLDOWN] %s state reset by operator", self.plugin_name)


    # ---- internals ------------------------------------------------------------
    def _clear_expired(self, now: float, *, save: bool) -> None:
        if not self.state.cooldown_until:
            return
        expired_at = self.state.cooldown_until
        self.state.cooldown_until = 0.0
        self.state.cooldown_started_at = 0.0
        self.state.block_index = self.blocks
        self.state.updated_at = now
        if save:
            self._save()
        self.log.info(
            "[COOLDOWN] %s expired at %s; entries allowed again",
            self.plugin_name,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expired_at)),
        )

    def _advance_blocks(self, now: float) -> None:
        """Record completed 6-hour checks while a multi-block cooldown is active."""
        if self.blocks <= 1 or not self.state.cooldown_started_at:
            return
        block_seconds = self.block_hours * 3600.0
        elapsed = max(0.0, now - self.state.cooldown_started_at)
        completed = min(self.blocks - 1, int(elapsed // block_seconds))
        if completed > self.state.block_index:
            for index in range(self.state.block_index + 1, completed + 1):
                self.log.warning(
                    "[COOLDOWN] %s block %d/%d check complete (%.0fh elapsed); %d block(s) left",
                    self.plugin_name,
                    index,
                    self.blocks,
                    index * self.block_hours,
                    self.blocks - index,
                )
            self.state.block_index = completed
            self.state.updated_at = now
            self._save()

    def _save(self) -> None:
        try:
            self.store.save(self.state)
        except Exception as exc:  # persistence failure must not stop the risk veto
            self.log.error("failed to persist cooldown state %s: %r", self.store.path, exc)

    # ---- introspection --------------------------------------------------------
    def status(self) -> dict:
        decision = self.evaluate()
        return {
            "plugin": self.plugin_name,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "cumulative_loss_usd": round(decision.cumulative_loss_usd, 6),
            "loss_threshold_usd": self.loss_threshold_usd,
            "cooldown_hours": self.cooldown_hours,
            "blocks": self.blocks,
            "block_index": decision.block_index,
            "cooldown_until": self.state.cooldown_until,
            "armed_count": self.state.armed_count,
            "state_path": str(self.store.path),
        }
