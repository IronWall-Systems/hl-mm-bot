"""Orchestrator.  WS event -> state update -> risk check -> quotes -> diff vs live orders -> cancel/place.

Design rules:
  * One lock serialises everything that mutates order/position state (cycle, housekeeping, kill).
  * Book events are coalesced (only the newest matters); requotes are rate-limited.
  * Network calls never raise out of the loop; failures feed the RiskGuard error counter.
  * Kill path: cancel all -> market-flatten -> verify flat -> stop. Never skipped on error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import resource
import signal
import sys
import time
from collections import Counter, deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Deque, Optional, Set

from .config import load_settings
from .exchange.client import Gateway, HyperliquidGateway
from .exchange.types import BookEvent, FeedDownEvent, FillEvent, OrderUpdateEvent, PlaceKind
from .execution.batch_planner import BatchPlanner, Leg, LiveQuote, Payload
from .execution.order_manager import OrderManager
from .execution.quote_bands import (BandParams, EWMARef, RequestBudget,
                                    calibrate_costs)
from .models import (MarketState, MicroState, OrderStatus, PositionState, Quote, RiskConfig, Side,
                     StrategyParams)
from .precision import round_px, round_sz, tick_at
from .risk.guard import KillReason, RiskGuard
from .risk.loss_guard import (GuardAction, LossGuard, LossGuardConfig,
                              hard_stop_sequence)
from .safety import cancel_sweep
from .strategy.quoter import compute_quotes, toxic_sides

log = logging.getLogger("mmbot")

# Durable stop marker.  Lives under logs/ because that is the only path the
# systemd unit grants write access to (ProtectSystem/ProtectHome are strict).
HALT_DEFAULT = Path(__file__).resolve().parents[2] / "logs" / "HALT"


def loss_guard_config_from_env(environ=None) -> Optional[LossGuardConfig]:
    """Build the hard-loss guard config, or ``None`` to leave the guard off.

    Enabled explicitly with ``RISK_LOSS_GUARD=1`` or implicitly when either
    threshold is set (the live trial script sets both).  Tests and dry runs
    therefore keep the old behaviour unless they opt in.
    """
    env = os.environ if environ is None else environ
    flag = env.get("RISK_LOSS_GUARD")
    if flag is not None:
        enabled = flag.strip().lower() not in ("0", "false", "no", "off", "")
    else:
        enabled = "RISK_MAX_LOSS_USD" in env or "RISK_SOFT_LOSS_USD" in env
    if not enabled:
        return None
    soft = float(env.get("RISK_SOFT_LOSS_USD", "0.50"))
    hard = float(env.get("RISK_MAX_LOSS_USD", "0.80"))
    if not (0.0 < soft < hard):
        raise SystemExit(
            f"invalid loss guard thresholds: RISK_SOFT_LOSS_USD={soft} "
            f"RISK_MAX_LOSS_USD={hard} (need 0 < soft < hard)")
    return LossGuardConfig(
        soft_loss_usd=soft,
        hard_loss_usd=hard,
        poll_stale_s=float(env.get("RISK_GUARD_POLL_STALE_S", "20")),
        unreadable_hard_s=float(env.get("RISK_GUARD_UNREADABLE_HARD_S", "90")),
        soft_flat_deadline_s=float(env.get("RISK_GUARD_SOFT_DEADLINE_S", "1200")),
        halt_file=env.get("RISK_HALT_FILE", str(HALT_DEFAULT)))


class _LossGuardGateway:
    """Adapt the exchange Gateway to the guard's minimal async protocol."""

    def __init__(self, gw: Gateway):
        self.gw = gw

    async def cancel_all(self) -> int:
        return await self.gw.cancel_all()

    async def flatten(self, slippage: float = 0.05) -> None:
        await self.gw.flatten(slippage)

    async def position(self) -> float:
        st = await self.gw.account_state()
        return float(st.size)


class ToxicityDebouncer:
    """Hysteresis around the per-tick toxicity signal.

    The raw ``toxic_sides`` set flips on individual book events (575 transitions
    in 15 min on 2026-11-09), which parked the book continuously.  Require the
    same toxic side-set on ``confirm_n`` consecutive book events before firing,
    and re-arm only after ``clear_n`` consecutive clean events.
    """

    def __init__(self, confirm_n: int = 3, clear_n: int = 10):
        self.confirm_n = max(1, int(confirm_n))
        self.clear_n = max(1, int(clear_n))
        self._prev: frozenset = frozenset()
        self.streak = 0
        self._clear = 0
        self.armed = True

    def update(self, micro: MicroState, p: StrategyParams) -> frozenset:
        """Return the toxic set when it should trigger, else an empty set."""
        tox = toxic_sides(micro, p)
        if tox and tox == self._prev:
            self.streak += 1
        elif tox:
            self.streak = 1
        else:
            self.streak = 0
        if tox:
            self._clear = 0
        else:
            self._clear += 1
            if self._clear >= self.clear_n:
                self.armed = True
        fired = tox if (tox and self.armed and self.streak >= self.confirm_n) else frozenset()
        if fired:
            self.armed = False
        self._prev = tox
        return fired

    def current(self) -> frozenset:
        """Confirmed, *directional* toxic side-set for defensive planning.

        Unlike :meth:`update` (which fires once per armed episode for the fast
        path), this stays latched for as long as the same side-set keeps
        appearing on consecutive book events.  It never returns both sides
        because of a single adverse tick.
        """
        return self._prev if self.streak >= self.confirm_n else frozenset()


class Bot:
    def __init__(self, gw: Gateway, params: StrategyParams, rcfg: RiskConfig,
                 now_fn: Callable[[], float] = time.monotonic, flatten_on_exit: bool = True):
        self.gw, self.p, self.rcfg, self.now = gw, params, rcfg, now_fn
        self.flatten_on_exit = flatten_on_exit
        self.om = OrderManager(params)
        self.market: Optional[MarketState] = None
        self.pos = PositionState()
        self.guard: Optional[RiskGuard] = None
        # Independent hard-loss guard (see risk/loss_guard.py).  Off unless
        # RISK_LOSS_GUARD=1 or a threshold env var is present.
        self.loss_cfg = loss_guard_config_from_env()
        self.loss_guard: Optional[LossGuard] = None
        self._guard_action: GuardAction = GuardAction.OK
        self._soft_only = False          # SOFT: quote only the reducing side
        self._pulled = False             # PULL: resting quotes already cancelled
        self._hard_exit_code: Optional[int] = None
        # Rolling online learning (opt-in, observational).  Training runs in a
        # separate process so a checkpoint write never blocks quoting.
        self._learning_enabled = os.environ.get("MM_LEARNING", "0").lower() in (
            "1", "true", "yes", "on")
        self.learning = None
        self.halted = False
        self.kill_reason: Optional[KillReason] = None
        self.stop_event = asyncio.Event()
        self.metrics: Counter = Counter()
        self._lock = asyncio.Lock()
        self._last_cycle = -1e9
        self._last_account = -1e9
        self._last_reconcile = -1e9
        self._last_restart = -1e9
        self._force_reconcile = False
        self._blocked_until = {Side.BUY: 0.0, Side.SELL: 0.0}
        self._cross_streak = {Side.BUY: 0, Side.SELL: 0}
        self._stale_state: Optional[str] = None      # T7b: None=unconfirmed, "quiet", "stale"
        self._last_book_t: float = -1e9              # T7c observability: last WS book event
        self._ws_msgs_at_restart: int = 0            # T7c observability: WS msg watermark
        self._tids: Deque[int] = deque(maxlen=5000)
        self._tid_set: Set[int] = set()
        self._fill_seq: int = 0                    # T8c: fills applied since boot (poll race guard)
        self._poll_issue_t: float = 0.0            # T8c: account-poll issue timestamp (observability)
        self._poll_rtt_ms: float = 0.0             # T8c: last account-poll round trip
        self._poll_lock_wait_ms: float = 0.0       # T8c: last account-poll wait for self._lock
        self._pending_poll_size: Optional[float] = None   # T8c: 1st differing poll awaiting a 2nd
        self._order_seq: int = 0                 # T8d: order set changed while open_orders() was in flight
        self.metrics_interval_s = 60.0
        self._jlog_path = os.environ.get("HL_JSONL_LOG")
        # Forward markout tracker: (monotonic fill ts, side, fill px, mid at fill, remaining horizons)
        self._markouts: list = []
        # Lean 3-signal microstructure state + toxicity fast-path trigger.
        self.micro: Optional[MicroState] = None
        self._micro_urgent = False
        self._last_fast_cycle = -1e9
        # Toxicity hysteresis: confirm a sustained toxic set before acting and
        # re-arm only after a clean spell (see ToxicityDebouncer).
        self._tox = ToxicityDebouncer(
            int(os.environ.get("MM_TOX_CONFIRM_N", "3")),
            int(os.environ.get("MM_TOX_CLEAR_N", "10")))
        # ---- budget-aware batch quoting (MM_BATCH_PLANNER=1) --------------------
        self.use_batch_planner = os.environ.get("MM_BATCH_PLANNER", "0").lower() not in (
            "0", "false", "no", "off", "")
        self.ewma_ref = EWMARef(tau_s=float(os.environ.get("MM_EWMA_TAU_S", "3.0")))
        band_h = float(os.environ.get("MM_BAND_HALF_SPREAD_BPS", str(params.half_spread_bps)))
        self.bands = BandParams(half_spread_bps=band_h,
                                near_frac=float(os.environ.get("MM_BAND_NEAR_FRAC", "0.35")),
                                near_max_bps=float(os.environ.get("MM_BAND_NEAR_MAX_BPS", "7.0")),
                                far_frac=float(os.environ.get("MM_BAND_FAR_FRAC", "1.0")),
                                far_min_bps=float(os.environ.get("MM_BAND_FAR_MIN_BPS", "20.0")),
                                pull_frac=float(os.environ.get("MM_BAND_PULL_FRAC", "0.45")),
                                pull_resume_s=float(os.environ.get("MM_BAND_PULL_RESUME_S", "20.0")))
        self.budget = RequestBudget(
            reserve=int(os.environ.get("MM_BUDGET_RESERVE", "500")),
            horizon_h=float(os.environ.get("MM_BUDGET_HORIZON_H",
                                           str(RequestBudget.DEFAULT_HORIZON_H))),
            now=self.now)
        self.batch_planner = BatchPlanner(
            self.budget, self.bands,
            charge_mode=os.environ.get("MM_BATCH_COST_MODE", "call"),
            now=self.now)
        self._rate_poll_interval_s = float(os.environ.get("MM_RATE_POLL_S", "30"))
        self._last_rate_poll = -1e9
        self._calibration_done = False
        self._calibration_inflight = False
        self._actions_log_hour = -1

    # ---- structured JSONL audit log ------------------------------------------
    def _jlog(self, event: str, **fields) -> None:
        """Append one structured JSON line. Never raises into the trading loop."""
        if not self._jlog_path:
            return
        rec = {"ts": round(time.time(), 3), "mono": round(self.now(), 3), "event": event}
        rec.update(fields)
        try:
            with open(self._jlog_path, "a", buffering=1) as fh:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        except Exception:
            log.exception("jsonl log write failed")

    def _track_markout(self, side: Side, px: float, sz: float, pos: float) -> None:
        if self.market is None:
            return
        self._markouts.append({"t": self.now(), "side": side, "px": px, "sz": sz,
                               "pos": pos, "mid0": self.market.mid,
                               "horizons": [1, 5, 30]})

    def _flush_markouts(self) -> None:
        if not self._markouts or self.market is None:
            return
        now = self.now()
        mid = self.market.mid
        keep = []
        for m in self._markouts:
            age = now - m["t"]
            while m["horizons"] and age >= m["horizons"][0]:
                h = m["horizons"].pop(0)
                sign = 1.0 if m["side"] is Side.BUY else -1.0
                bps = sign * (mid - m["px"]) / m["px"] * 1e4
                self._jlog("markout", horizon_s=h, side=m["side"].value, fill_px=m["px"],
                           size=m["sz"], mid=mid, markout_bps=round(bps, 4))
            if m["horizons"]:
                keep.append(m)
        self._markouts = keep

    # ---- rolling learning (observational) ---------------------------------
    def _learning_action_hint(self) -> str:
        if self._guard_action is GuardAction.SOFT:
            return "reduce_only"
        if self._guard_action is GuardAction.PULL or self._stale_state == "stale":
            return "pull"
        return "quote_both"

    @staticmethod
    def _action_from_sides(buy: bool, sell: bool, soft_only: bool = False) -> str:
        if soft_only:
            return "reduce_only"
        if buy and sell:
            return "quote_both"
        if buy:
            return "quote_bid"
        if sell:
            return "quote_ask"
        return "pull"

    def _learning_observe(self, now: float, action_hint: Optional[str] = None) -> None:
        """Record one decision step.  Never raises into the trading loop.

        The learning layer uses wall-clock timestamps (the buffer's retention
        window is wall-clock); ``now`` here is the bot's monotonic clock and is
        intentionally not reused.
        """
        if self.learning is None or self.market is None:
            return
        try:
            mid = self.market.mid
            if mid <= 0:
                return
            self.learning.observe(
                time.time(), mid, micro=self.micro,
                position=self.pos.size, equity=self.pos.account_value,
                guard_status=self._guard_action.value,
                action_hint=action_hint or self._learning_action_hint(),
                spread_bps=self.market.spread_bps)
        except Exception:
            log.exception("learning observe failed (trading continues)")

    # ---- startup ---------------------------------------------------------------
    async def init(self) -> None:
        await self.gw.start()
        self.pos = await self.gw.account_state()
        if self.pos.account_value <= 0:
            raise RuntimeError("account value reads as 0: cannot enforce drawdown stop "
                               "(wrong address? unified-account mode? unfunded?). Refusing to start.")
        self.guard = RiskGuard(self.rcfg, self.pos.account_value, self.now)
        if self.loss_cfg is not None:
            self.loss_guard = LossGuard(self.pos.account_value, self.loss_cfg,
                                        now=self.now,
                                        halt_path=self.loss_cfg.halt_file)
            log.warning("LOSS GUARD ENABLED: soft=-$%.2f hard=-$%.2f stale_pull=%.0fs "
                        "unreadable_hard=%.0fs soft_deadline=%.0fs halt=%s "
                        "start_equity=%.5f",
                        self.loss_cfg.soft_loss_usd, self.loss_cfg.hard_loss_usd,
                        self.loss_cfg.poll_stale_s, self.loss_cfg.unreadable_hard_s,
                        self.loss_cfg.soft_flat_deadline_s, self.loss_guard.halt_path,
                        self.pos.account_value)
        else:
            log.info("loss guard disabled (set RISK_LOSS_GUARD=1 or RISK_MAX_LOSS_USD to enable)")
        if self._learning_enabled:
            try:
                from .learning.recorder import LearningRecorder
                self.learning = LearningRecorder.from_env(
                    coin=self.p.coin, max_notional=self.p.max_notional)
                if self.learning is not None:
                    log.warning("LEARNING RECORDER ENABLED: buffer=%s steps=%d policy=%s",
                                self.learning.buffer.path, len(self.learning.buffer),
                                self.learning.policy_path)
            except Exception:
                log.exception("learning recorder init failed (trading continues)")
                self.learning = None
        n = await self.gw.cancel_all()          # start-up hygiene: clear leftovers from earlier runs
        log.info("startup: equity=%.2f position=%.6f, cleared %d stale orders",
                 self.pos.account_value, self.pos.size, n)
        self._jlog("startup", equity=self.pos.account_value, position=self.pos.size,
                   cleared_orders=n, env=os.environ.get("HL_ENV"), coin=self.p.coin,
                   loss_guard=self.loss_guard is not None)
        if self.loss_guard is not None:
            self._jlog("loss_guard", enabled=True,
                       start_equity=self.pos.account_value,
                       soft_loss_usd=self.loss_cfg.soft_loss_usd,
                       hard_loss_usd=self.loss_cfg.hard_loss_usd,
                       halt_path=self.loss_guard.halt_path)

    # ---- events ------------------------------------------------------------------
    async def handle_event(self, ev) -> None:
        if isinstance(ev, BookEvent):
            self.market = ev.market
            # Budget-aware reference: time-decayed EWMA is immune to irregular
            # book tick spacing and damps single-tick microstructure flicker.
            try:
                self.ewma_ref.update(ev.market.mid, self.now())
            except ValueError:
                pass
            if ev.micro is not None:
                self.micro = ev.micro
                fired = self._tox.update(self.micro, self.p)
                if fired:
                    self._micro_urgent = True          # bypass cadence: widen the exposed leg
                    self.metrics["tox_events"] += 1
                    self._jlog("toxicity", sides=sorted(s.value for s in fired),
                               streak=self._tox.streak,
                               obi_nw=round(self.micro.obi_nw, 4),
                               tfi2=round(self.micro.tfi2, 4),
                               lead_btc_1s=round(self.micro.lead_btc_1s, 4),
                               wall_bid=self.micro.wall_bid_px,
                               wall_ask=self.micro.wall_ask_px)
            self._stale_state = None                 # T7b: new book event ends the silence episode
            self._last_book_t = self.now()           # T7c observability
            self.metrics["book"] += 1
            self._flush_markouts()
            # Hard-loss guard reacts on every book tick, so the stop does not
            # wait for the next 5 s account poll.
            if self.loss_guard is not None:
                self._guard_action = self.loss_guard.evaluate(
                    ev.market.mid, self.pos.size)
            if self.learning is not None:
                try:
                    self.learning.on_mark(time.time(), ev.market.mid)
                except Exception:
                    log.exception("learning mark failed (trading continues)")
        elif isinstance(ev, FillEvent):
            if ev.is_snapshot:
                return
            if ev.tid is not None:
                if ev.tid in self._tid_set:
                    return
                if len(self._tids) == self._tids.maxlen:
                    self._tid_set.discard(self._tids[0])
                self._tids.append(ev.tid)
                self._tid_set.add(ev.tid)
            self.pos.size += ev.side.sign * ev.sz
            self._fill_seq += 1                    # T8c: bumps only for a genuinely applied fill
            self.om.on_fill(ev.cloid, ev.oid, ev.sz, self.now())
            self.metrics["fills"] += 1
            log.info("FILL %s %.6f @ %.6f pos=%.6f", ev.side.value, ev.sz, ev.px, self.pos.size)
            self._jlog("fill", side=ev.side.value, px=ev.px, size=ev.sz, fee=ev.fee,
                       oid=ev.oid, cloid=ev.cloid, pos=self.pos.size)
            self._track_markout(ev.side, ev.px, ev.sz, self.pos.size)
            if self.learning is not None:
                try:
                    self.learning.on_fill(time.time(), ev.side.value,
                                          ev.px, ev.sz, ev.fee)
                except Exception:
                    log.exception("learning fill failed (trading continues)")
        elif isinstance(ev, OrderUpdateEvent):
            self.om.on_order_update(ev, self.now())
        elif isinstance(ev, FeedDownEvent):
            await self._restart_feeds(ev.reason)

    async def event_loop(self) -> None:
        q = self.gw.events
        while not self.stop_event.is_set():
            try:
                batch = [await asyncio.wait_for(q.get(), timeout=0.25)]
            except asyncio.TimeoutError:
                batch = []
            while not q.empty():
                batch.append(q.get_nowait())
            last_book = None
            for ev in batch:                       # coalesce: only the newest book matters
                if isinstance(ev, BookEvent):
                    last_book = ev
                else:
                    await self.handle_event(ev)
            if last_book:
                await self.handle_event(last_book)
            # T8c hunk A (REQ-0028.2): no maybe_cycle() here - quoting runs in cycle_loop() so
            # fills/order updates are never queued behind cycle()'s REST calls.

    async def cycle_loop(self) -> None:
        """T8c hunk B: quoting in its own task (gated by min_requote_interval_s), so event_loop
        stays responsive while a place/cancel REST is in flight."""
        while not self.stop_event.is_set():
            try:
                await self.maybe_cycle()
            except Exception:
                log.exception("cycle crashed (continuing)")
            await asyncio.sleep(0.1)

    # ---- quoting cycle -------------------------------------------------------------
    async def maybe_cycle(self) -> None:
        if self.halted or self.market is None:
            return
        if self._guard_action in (GuardAction.HARD, GuardAction.PULL):
            await self.cycle()               # risk action must not wait for the requote interval
            return
        gap = self.now() - self._last_cycle
        if gap < self.p.min_requote_interval_s:
            # Toxicity transition gets the fast path, but never more often than
            # once every 3 s so the address action budget is not exhausted.
            fast_ok = (self._micro_urgent and gap >= 0.1
                       and self.now() - self._last_fast_cycle >= 3.0)
            if not fast_ok:
                return
        await self.cycle()

    async def cycle(self) -> None:
        async with self._lock:
            if self.halted or self.market is None:
                return
            now = self.now()
            self._last_cycle = now
            reason = self.guard.check_kill(self.pos, self.market.mid)
            if reason:
                await self._kill_locked(reason)
                return
            # Independent hard-loss guard: HARD -> cancel + taker flatten + HALT,
            # PULL -> cancel resting quotes, SOFT -> reducing side only.
            if self.loss_guard is not None:
                self._guard_action = self.loss_guard.evaluate(
                    self.market.mid, self.pos.size)
            if self._guard_action is GuardAction.HARD:
                self._learning_observe(now, "risk_hold")
                await self._hard_stop_locked()
                return
            if self._guard_action is GuardAction.PULL:
                self._learning_observe(now, "pull")
                await self._pull_quotes_locked(now)
                return
            self._soft_only = self._guard_action is GuardAction.SOFT
            if not self._soft_only:
                self._pulled = False
            age = self.gw.feed_age_s()
            thr = self._stale_threshold_s()          # T7e: K x rolling-median cadence (legacy fallback)
            if age > thr:                            # T7b: confirm via ONE REST l2 snapshot per episode
                if self._stale_state is None:
                    snap = await self.gw.book_snapshot()
                    if (snap is not None and self.market is not None
                            and (snap.bid, snap.ask) == (self.market.bid, self.market.ask)):
                        self._stale_state = "quiet"
                        log.info("book silent %.1fs (thr %.2fs) but REST l2 matches last book: keeping quotes",
                                 age, thr)
                        age = 0.0                    # verified fresh -> quote as usual
                    else:
                        self._stale_state = "stale"
                        d_bid = d_ask = float("nan")
                        if snap is not None and self.market is not None:
                            tick = tick_at(snap.bid, self.gw.spec.sz_decimals) if self.gw.spec else 1.0
                            d_bid = (snap.bid - self.market.bid) / tick
                            d_ask = (snap.ask - self.market.ask) / tick
                        log.warning("stale book confirmed by REST l2 (rest=%s last=%s) "
                                    "silence=%.1fs thr=%.2fs d_bid=%+.1f d_ask=%+.1f ticks: pulling quotes",
                                    snap, (self.market.bid, self.market.ask) if self.market else None,
                                    age, thr, d_bid, d_ask)
                        self.metrics["stale_pulls"] += 1
                        await self._do_cancels([o for o in self.om.active()
                                                if o.status is OrderStatus.RESTING], now)
                        self._learning_observe(now, "pull")
                        return                       # T7e: REST confirm NEVER restarts feeds
                elif self._stale_state == "quiet":
                    age = 0.0                        # already confirmed this episode
                else:
                    return                           # confirmed stale: already pulled + restart sent
            inv0 = self.pos.size                    # T8c REQ-0029: inventory at plan time
            if self._micro_urgent:
                self._last_fast_cycle = now          # rate-limit the toxicity fast path
            self._micro_urgent = False               # one forced cycle per toxicity transition
            if self.use_batch_planner:
                await self._cycle_batched(now, age, inv0)
                return
            targets = compute_quotes(self.market, self.pos.size, self.p, self.gw.spec, self.micro)
            if self._soft_only:
                targets = self._reduce_only(targets)
            sides = {q.side for q in targets}
            self._learning_observe(now, self._action_from_sides(
                Side.BUY in sides, Side.SELL in sides, self._soft_only))
            blocked = frozenset(s for s, t in self._blocked_until.items() if t > now)
            plan = self.om.plan(targets, now, blocked)
            await self._apply_plan(plan, now, age)
            if abs(self.pos.size - inv0) > 1e-12:    # a fill landed during the RESTs:
                targets = compute_quotes(self.market, self.pos.size, self.p, self.gw.spec, self.micro)
                if self._soft_only:
                    targets = self._reduce_only(targets)
                plan = self.om.plan(targets, now, blocked)          # re-read sizing/inventory
                await self._apply_plan(plan, now, age)

    async def _apply_plan(self, plan, now: float, feed_age: float) -> None:
        """Apply a diff in three bulk L1 actions at most.

        modify (one batchModify) -> cancel (one bulk_cancel per id type) -> place
        (one bulk_orders).  Modifies first keep the live book populated while the
        slower cancel/place paths run.
        """
        if plan.modifies:
            await self._do_modifies(plan.modifies, now, feed_age)
        if plan.cancels:
            await self._do_cancels(plan.cancels, now)
        if plan.places:
            await self._do_places(plan.places, now, feed_age)

    # ---- budget-aware batch quoting (MM_BATCH_PLANNER=1) ---------------------------
    def _tgt_sizes(self, ref: float) -> dict:
        """Per-side quote size; a side at the one-sided inventory cap is disabled.

        A zero/missing size makes ``BatchPlanner.plan`` emit a protective cancel
        for any resting quote on that side, so inventory never keeps growing.
        """
        szd = self.gw.spec.sz_decimals
        base = round_sz(self.p.quote_notional / ref, szd)
        max_sz = self.p.max_notional / ref
        # Disable a side once one more base-size fill would breach the cap.
        # Using ``pos < max_sz`` alone lets a 3rd fill through when max_sz is
        # just above 2 x base (e.g. 0.32 vs 0.3217): the cap must bind at 2
        # fills, not 3.
        tol = 1.0001
        sizes = {"buy": base if (self.pos.size + base) <= max_sz * tol else 0.0,
                 "sell": base if (self.pos.size - base) >= -max_sz * tol else 0.0}
        if self._soft_only:
            # SOFT stop: never add risk, only quote the side that reduces.
            if self.pos.size >= 0:
                sizes["buy"] = 0.0
            if self.pos.size <= 0:
                sizes["sell"] = 0.0
        return sizes

    def _reduce_only(self, quotes: list) -> list:
        """Keep only quotes that reduce current inventory (SOFT stop, legacy path)."""
        if self.pos.size > 0:
            return [q for q in quotes if q.side is Side.SELL]
        if self.pos.size < 0:
            return [q for q in quotes if q.side is Side.BUY]
        return []

    def _quote_ref(self, ref: float) -> float:
        """Reservation price with inventory skew applied (batch path).

        Long inventory shifts the reference *down*, which tightens the ask
        (fast profitable exit) and widens the bid (no careless doubling down);
        short inventory mirrors it.  Skew is the same ``skew_bps`` used by the
        legacy quoter, so the batch planner prices identically.
        """
        if ref is None or ref <= 0 or self.p.skew_bps == 0 or self.p.max_notional <= 0:
            return ref
        pos_n = self.pos.size * ref
        clip = max(-1.0, min(1.0, pos_n / self.p.max_notional))
        return ref * (1.0 - self.p.skew_bps * clip / 1e4)

    def _skip_sides(self, now: float) -> frozenset:
        """In-flight acks plus local cross-reject backoff."""
        return self.om.in_flight_sides() | frozenset(
            s for s, t in self._blocked_until.items() if t > now)

    async def _cycle_batched(self, now: float, feed_age: float, inv0: float) -> None:
        """Plan with EWMARef + BatchPlanner and execute the payload groups.

        Called with ``self._lock`` already held by :meth:`cycle`.
        """
        if self.market is None or not self.market.is_valid or self.gw.spec is None:
            return
        ref = self.ewma_ref.value if self.ewma_ref.ready else self.market.mid
        if ref is None or ref <= 0:
            return
        # Use the *debounced, directional* toxic set (not the raw per-tick
        # signal, which fired bilateral pull-park 1 s after placement).
        tox = self._tox.current()
        tox_sides = frozenset(s.value for s in tox)
        sizes = self._tgt_sizes(ref)
        self._learning_observe(now, self._action_from_sides(
            sizes.get("buy", 0.0) > 0 and "buy" not in tox_sides,
            sizes.get("sell", 0.0) > 0 and "sell" not in tox_sides,
            self._soft_only))
        payloads = self.om.plan_batched(self.batch_planner, self._quote_ref(ref),
                                        sizes, now,
                                        skip=self._skip_sides(now), toxic=tox_sides)
        await self._execute_batch_payloads(payloads, now, feed_age)
        if abs(self.pos.size - inv0) > 1e-12:      # a fill landed during the RESTs
            ref = self.ewma_ref.value if self.ewma_ref.ready else self.market.mid
            payloads = self.om.plan_batched(self.batch_planner, self._quote_ref(ref),
                                            self._tgt_sizes(ref), now,
                                            skip=self._skip_sides(now), toxic=tox_sides)
            await self._execute_batch_payloads(payloads, now, feed_age)

    async def _execute_batch_payloads(self, payloads, now: float, feed_age: float) -> None:
        if not payloads:
            return
        sent_any = False
        for p in payloads:
            if p.kind == "modify":
                sent = await self._do_batch_modifies(p, now, feed_age)
            elif p.kind == "place":
                sent = await self._do_batch_places(p, now, feed_age)
            elif p.kind == "cancel":
                sent = await self._do_batch_cancels(p, now)
            else:
                sent = False
            if sent:
                sent_any = True
                self.batch_planner.on_sent(p)
        if sent_any:
            self.metrics["batch_cycles"] += 1
            self.metrics["batch_payloads"] += len(payloads)

    async def _do_batch_modifies(self, payload: Payload, now: float, feed_age: float) -> bool:
        mid = self.market.mid
        szd = self.gw.spec.sz_decimals
        allowed = []
        for leg in payload.legs:
            o = self.om.order_for_side(leg.side)
            if o is None or o.oid is None:
                continue
            side = o.side
            px = round_px(float(leg.px), szd, "floor" if side is Side.BUY else "ceil")
            sz = round_sz(float(leg.sz if leg.sz is not None else o.remaining), szd)
            if px <= 0 or sz <= 0:
                continue
            q = Quote(side, px, sz, tif=o.tif or "Alo")
            if leg.why != "pull-park":
                exposure = max(0.0, self.om.exposure_notional(side) - o.notional_remaining)
                v = self.guard.pre_trade(q, self.pos, mid, exposure, feed_age)
                if not v.ok:
                    self.metrics["risk_rejects"] += 1
                    log.info("risk veto batch modify %s %.2f: %s", side.value, q.price, v.reason)
                    await self._purge_side_exposure(side, now)
                    continue
            allowed.append((leg, o, q))
        if not allowed:
            return False
        for _leg, o, _q in allowed:
            self.om.mark_modify_sent(o, now)
        reqs = [{"oid": o.oid, "side": q.side, "price": q.price, "size": q.size,
                 "tif": q.tif, "cloid": o.cloid} for _leg, o, q in allowed]
        before_api = None
        if not self._calibration_done and not self._calibration_inflight:
            # One free /info read isolates the very first live batchModify.
            try:
                r = await self.gw.user_rate_limit()
                before_api = int(float(r.get("nRequestsUsed", 0)))
            except Exception:
                before_api = None
        results = await self.gw.modify_quotes(reqs)
        for (leg, o, q), res in zip(allowed, results):
            old_oid = o.oid
            self.om.on_modify_result(o, q, res, self.now())
            self._order_seq += 1
            self.guard.record_actions(1)
            self.metrics["modifies"] += 1
            if res.latency_ms:
                self.guard.record_latency(res.latency_ms)
            t = self.now()
            self._jlog("modify", side=q.side.value, price=q.price, size=q.size,
                       old_oid=old_oid, new_oid=res.oid, kind=res.kind.value,
                       latency_ms=round(res.latency_ms, 3), error=res.error,
                       pull_park=(leg.why == "pull-park"), reason=leg.why)
            if res.kind is PlaceKind.RESTING:
                self.guard.record_success()
                self._cross_streak[q.side] = 0
            else:
                if res.kind is PlaceKind.REJECT_CROSS:
                    self.metrics["cross_rejects"] += 1
                    self._cross_streak[q.side] += 1
                    self._blocked_until[q.side] = t + min(0.25 * 2 ** self._cross_streak[q.side], 5.0)
                    log.info("batch modify crossed (%s @ %.6f): %s", q.side.value, q.price, res.error)
                elif res.kind is PlaceKind.ERROR:
                    self.metrics["modify_errors"] += 1
                    self._force_reconcile = True
                    log.error("batch modify failed (state unknown): %s", res.error)
                else:
                    self.metrics["other_rejects"] += 1
                    self._force_reconcile = True
                    log.warning("batch modify rejected (%s @ %.6f): %s", q.side.value, q.price, res.error)
                self.guard.record_error()
                self.guard.record_reject(q.side)
                if leg.why == "pull-park" and old_oid is not None:
                    # Required fallback: a rejected pull-park becomes a raw cancel.
                    try:
                        self.om.mark_cancel_sent(o, self.now())
                        cres = await self.gw.cancel_oids([old_oid])
                        if cres:
                            r0 = cres[0]
                            self.om.on_cancel_result(o, r0.ok, r0.already_gone, self.now())
                            self.guard.record_actions(1)
                            self.batch_planner.on_sent(Payload(
                                "cancel", [Leg(o.side.value, "cancel", None, None, True, "pull")]))
                            self._jlog("cancel", oid=old_oid, cloid=o.cloid, ok=r0.ok,
                                       already_gone=r0.already_gone, reason="pull-park-fallback")
                            log.warning("pull-park rejected -> cancel_oids fallback oid=%s: %s",
                                        old_oid, r0.error)
                    except Exception:
                        log.exception("pull-park cancel fallback failed")
        if before_api is not None:
            self._calibration_inflight = True
            await self._finish_calibration(before_api, len(reqs))
        return True

    async def _finish_calibration(self, before_used: int, n_legs: int) -> None:
        try:
            r = await self.gw.user_rate_limit()
            after = int(float(r.get("nRequestsUsed", before_used)))
            verdict = calibrate_costs(before_used, after, n_legs)
            # Only a multi-leg sample can distinguish call from leg; keep the
            # configured mode and retry on the next multi-leg batch otherwise.
            if verdict in ("call", "leg") and n_legs > 1:
                self.batch_planner.mode = verdict
                self._calibration_done = True
            self.budget.update_from_api(r)
            self.budget.note_verdict(verdict)
            log.warning(
                "REQUEST-BUDGET CALIBRATION: batchModify legs=%d used %d -> %d "
                "=> verdict=%s mode=%s", n_legs, before_used, after, verdict,
                self.batch_planner.mode)
            self._jlog("cost_calibration", legs=n_legs, before=before_used, after=after,
                       verdict=verdict, mode=self.batch_planner.mode)
        except Exception:
            log.exception("budget calibration failed")
        finally:
            self._calibration_inflight = False

    async def _do_batch_places(self, payload: Payload, now: float, feed_age: float) -> bool:
        mid = self.market.mid
        szd = self.gw.spec.sz_decimals
        allowed = []
        for leg in payload.legs:
            side = Side(leg.side)
            px = round_px(float(leg.px), szd, "floor" if side is Side.BUY else "ceil")
            sz = round_sz(float(leg.sz if leg.sz is not None else self.p.quote_notional / px), szd)
            if px <= 0 or sz <= 0:
                continue
            q = Quote(side, px, sz, tif="Alo")
            v = self.guard.pre_trade(q, self.pos, mid, self.om.exposure_notional(side), feed_age)
            if not v.ok:
                self.metrics["risk_rejects"] += 1
                log.info("risk veto batch place %s %.2f: %s", side.value, q.price, v.reason)
                await self._purge_side_exposure(side, now)
                continue
            o = self.om.register_pending(q, now)
            allowed.append((leg, o, q))
        if not allowed:
            return False
        reqs = [{"side": q.side, "price": q.price, "size": q.size,
                 "tif": q.tif, "cloid": o.cloid} for _leg, o, q in allowed]
        results = await self.gw.bulk_place(reqs)
        for (_leg, o, q), res in zip(allowed, results):
            self.om.on_place_result(o, res, self.now())
            self._order_seq += 1
            self.guard.record_actions(1)
            self.metrics["places"] += 1
            if res.latency_ms:
                self.guard.record_latency(res.latency_ms)
            t = self.now()
            self._jlog("place", side=q.side.value, price=q.price, size=q.size,
                       oid=res.oid, cloid=o.cloid, kind=res.kind.value,
                       latency_ms=round(res.latency_ms, 3), error=res.error)
            if res.kind is PlaceKind.RESTING:
                self.guard.record_success()
                self._cross_streak[q.side] = 0
            elif res.kind is PlaceKind.REJECT_CROSS:
                self.metrics["cross_rejects"] += 1
                self._cross_streak[q.side] += 1
                self._blocked_until[q.side] = t + min(0.25 * 2 ** self._cross_streak[q.side], 5.0)
                log.info("ALO crossed (%s @ %.6f), backing off: %s", q.side.value, q.price, res.error)
            else:
                self.metrics["other_rejects"] += 1
                self.guard.record_error()
                self.guard.record_reject(q.side)
                self._blocked_until[q.side] = t + 5.0
                self._force_reconcile = True
                log.warning("batch place rejected (%s @ %.6f): %s", q.side.value, q.price, res.error)
        return True

    async def _do_batch_cancels(self, payload: Payload, now: float) -> bool:
        orders = []
        for leg in payload.legs:
            o = self.om.order_for_side(leg.side)
            if o is not None and o not in orders and o.status in (
                    OrderStatus.RESTING, OrderStatus.PENDING_NEW, OrderStatus.PENDING_MODIFY):
                orders.append(o)
        if not orders:
            return False
        for o in orders:
            self.om.mark_cancel_sent(o, now)
        oid_orders = [o for o in orders if o.oid is not None]
        cloid_orders = [o for o in orders if o.oid is None]
        batches = []
        if oid_orders:
            batches.append((oid_orders, await self.gw.cancel_oids([o.oid for o in oid_orders])))
        if cloid_orders:
            batches.append((cloid_orders, await self.gw.cancel_cloids([o.cloid for o in cloid_orders])))
        for batch, results in batches:
            for o, r in zip(batch, results):
                canceled_oid = o.oid
                self.om.on_cancel_result(o, r.ok, r.already_gone, self.now())
                self._order_seq += 1
                self.guard.record_actions(1)
                self.metrics["cancels"] += 1
                if r.latency_ms:
                    self.guard.record_latency(r.latency_ms)
                self._jlog("cancel", oid=canceled_oid, cloid=o.cloid, ok=r.ok,
                           already_gone=r.already_gone,
                           latency_ms=round(r.latency_ms, 3), error=r.error)
                if r.ok or r.already_gone:
                    self.guard.record_success()
                else:
                    self.guard.record_error()
                    self._force_reconcile = True
                    log.warning("batch cancel failed %s: %s", o.cloid, r.error)
        return True

    async def _do_modifies(self, modifies, now: float, feed_age: float) -> None:
        mid = self.market.mid
        allowed = []
        for m in modifies:
            o, q = m.order, m.quote
            # pre_trade adds the target order's notional itself; the order already
            # rests, so subtract its current notional to avoid double counting.
            exposure = max(0.0, self.om.exposure_notional(q.side) - o.notional_remaining)
            v = self.guard.pre_trade(q, self.pos, mid, exposure, feed_age)
            if not v.ok:
                self.metrics["risk_rejects"] += 1
                log.info("risk veto modify %s %.2f: %s", q.side.value, q.price, v.reason)
                await self._purge_side_exposure(q.side, now)
                continue
            allowed.append(m)
        if not allowed:
            return
        for m in allowed:
            self.om.mark_modify_sent(m.order, now)
        reqs = [{"oid": m.order.oid, "side": m.quote.side, "price": m.quote.price,
                 "size": m.quote.size, "tif": m.quote.tif, "cloid": m.order.cloid}
                for m in allowed]
        results = await self.gw.modify_quotes(reqs)
        for m, res in zip(allowed, results):
            o, q = m.order, m.quote
            old_oid = o.oid
            self.om.on_modify_result(o, q, res, self.now())
            self._order_seq += 1
            self.guard.record_actions(1)
            self.metrics["modifies"] += 1
            if res.latency_ms:
                self.guard.record_latency(res.latency_ms)
            t = self.now()
            self._jlog("modify", side=q.side.value, price=q.price, size=q.size,
                       old_oid=old_oid, new_oid=res.oid, kind=res.kind.value,
                       latency_ms=round(res.latency_ms, 3), error=res.error)
            if res.kind is PlaceKind.RESTING:
                self.guard.record_success()
                self._cross_streak[q.side] = 0
            elif res.kind is PlaceKind.REJECT_CROSS:
                self.metrics["cross_rejects"] += 1
                self.guard.record_reject(q.side)
                self._cross_streak[q.side] += 1
                self._blocked_until[q.side] = t + min(0.25 * 2 ** self._cross_streak[q.side], 5.0)
                log.info("modify crossed (%s @ %.6f): %s", q.side.value, q.price, res.error)
            elif res.kind is PlaceKind.ERROR:
                self.metrics["modify_errors"] += 1
                self.guard.record_error()
                self.guard.record_reject(q.side)
                self._blocked_until[q.side] = t + 1.0
                self._force_reconcile = True
                log.error("modify failed (state unknown): %s", res.error)
            else:
                self.metrics["other_rejects"] += 1
                self.guard.record_error()
                self.guard.record_reject(q.side)
                self._blocked_until[q.side] = t + 5.0
                self._force_reconcile = True
                log.warning("modify rejected (%s @ %.6f): %s", q.side.value, q.price, res.error)

    async def _park_orders(self, orders, now: float):
        """Emergency OTM Park: pull RESTING quotes out of harm's way on the fast path.

        One ``batchModify`` moves every parkable leg far from the book (BUY at
        50% of mid, SELL at 150%, size capped by ``max_order_notional``).  The
        caller then issues a cleanup ``bulk_cancel`` on the rotated oids.  If the
        modify is rejected, the caller falls back to a raw cancel.

        A leg whose far-OTM price would push its notional below the exchange
        minimum ($10) is not modified at all; the caller's bulk_cancel removes
        it instead.  This avoids a guaranteed minimum-value rejection on a
        low-priced asset.
        """
        if not orders or self.market is None:
            return [], list(orders)
        mid = self.market.mid
        if mid <= 0 or self.gw.spec is None:
            return [], list(orders)
        szd = self.gw.spec.sz_decimals
        buy_px = round_px(mid * 0.50, szd, "floor")
        sell_px = round_px(mid * 1.50, szd, "ceil")
        reqs, targets = [], []
        for o in orders:
            px = buy_px if o.side is Side.BUY else sell_px
            if px <= 0:
                continue
            max_sz = self.rcfg.max_order_notional / px
            sz = round_sz(min(o.remaining, max_sz), szd)
            if sz <= 0 or px * sz < self.p.min_order_notional:
                # Cannot park this leg without breaking the exchange minimum
                # order value: leave it untouched for the caller's raw cancel.
                continue
            self.om.mark_modify_sent(o, now)
            reqs.append({"oid": o.oid, "side": o.side, "price": px, "size": sz,
                         "tif": o.tif, "cloid": o.cloid})
            targets.append((o, Quote(o.side, px, sz)))
        if not reqs:
            return [], list(orders)
        results = await self.gw.modify_quotes(reqs)
        parked, failed = [], []
        for (o, q), res in zip(targets, results):
            old_oid = o.oid
            self.om.on_modify_result(o, q, res, self.now())
            self._order_seq += 1
            self.guard.record_actions(1)
            if res.latency_ms:
                self.guard.record_latency(res.latency_ms)
            self._jlog("park", side=q.side.value, price=q.price, size=q.size,
                       old_oid=old_oid, new_oid=res.oid, kind=res.kind.value,
                       latency_ms=round(res.latency_ms, 3), error=res.error)
            if res.kind is PlaceKind.RESTING:
                self.metrics["parks"] += 1
                self.guard.record_success()
                parked.append(o)
            else:
                self.metrics["park_fallbacks"] += 1
                self.guard.record_error()
                self._force_reconcile = True
                failed.append(o)
                log.warning("OTM park rejected (%s oid=%s): %s", q.side.value, o.oid, res.error)
        return parked, failed

    async def _do_cancels(self, orders, now: float) -> None:
        orders = [o for o in orders
                  if o.status in (OrderStatus.RESTING, OrderStatus.PENDING_NEW,
                                  OrderStatus.PENDING_MODIFY)]
        if not orders:
            return
        # Risk-off / toxicity pulls use the fast OTM Park path first: a single
        # batchModify neutralises exposure in ~410 ms, then one bulk_cancel
        # cleans up.  PENDING_NEW (no confirmed oid) is cancelled directly.
        parkable = [o for o in orders
                    if o.status is OrderStatus.RESTING and o.oid is not None]
        if parkable:
            await self._park_orders(parkable, now)
        for o in orders:
            self.om.mark_cancel_sent(o, now)
        # One bulk action per id kind.  RESTING/modified orders always have an oid;
        # only a PENDING_NEW order can lack one and needs the cloid path.
        batches = []
        oid_orders = [o for o in orders if o.oid is not None]
        cloid_orders = [o for o in orders if o.oid is None]
        if oid_orders:
            batches.append((oid_orders, await self.gw.cancel_oids([o.oid for o in oid_orders])))
        if cloid_orders:
            batches.append((cloid_orders, await self.gw.cancel_cloids([o.cloid for o in cloid_orders])))
        for batch, results in batches:
            for o, r in zip(batch, results):
                canceled_oid = o.oid
                self.om.on_cancel_result(o, r.ok, r.already_gone, self.now())
                self._order_seq += 1                # T8d: cancel completed - any snapshot taken before it is stale
                self.guard.record_actions(1)
                self.metrics["cancels"] += 1
                if r.latency_ms:
                    self.guard.record_latency(r.latency_ms)
                self._jlog("cancel", oid=canceled_oid, cloid=o.cloid, ok=r.ok,
                           already_gone=r.already_gone,
                           latency_ms=round(r.latency_ms, 3), error=r.error)
                if r.ok or r.already_gone:
                    self.guard.record_success()
                else:
                    self.guard.record_error()
                    self._force_reconcile = True
                    log.warning("cancel failed %s: %s", o.cloid, r.error)

    async def _purge_side_exposure(self, side: Side, now: float) -> None:
        """Cancel every active order on a side that just failed the risk veto.

        A PENDING_NEW order can still land after the veto, and a RESTING order can
        still fill; either would recreate the exposure the veto rejected. Cancel
        both and count the purge for observability.
        """
        orders = self.om.active(side)
        if not orders:
            return
        self.metrics["veto_purges"] += 1
        log.warning("risk veto purge: cancelling %d %s order(s) after exposure veto",
                    len(orders), side.value)
        await self._do_cancels(orders, now)

    async def _do_places(self, quotes, now: float, feed_age: float) -> None:
        mid = self.market.mid
        allowed = []
        for q in quotes:
            v = self.guard.pre_trade(q, self.pos, mid, self.om.exposure_notional(q.side), feed_age)
            if not v.ok:
                self.metrics["risk_rejects"] += 1
                log.info("risk veto %s %.2f: %s", q.side.value, q.price, v.reason)
                # Veto-path purge: the risk check said this side may not increase
                # exposure. Do not leave a resting/pending order on that side
                # behind (a fill there would recreate the exposure the veto just
                # rejected). This is the live analogue of purging a queued place.
                await self._purge_side_exposure(q.side, now)
                continue
            o = self.om.register_pending(q, now)
            allowed.append((o, q))
        if not allowed:
            return
        reqs = [{"side": q.side, "price": q.price, "size": q.size,
                 "tif": q.tif, "cloid": o.cloid} for o, q in allowed]
        results = await self.gw.bulk_place(reqs)
        for (o, q), res in zip(allowed, results):
            self.om.on_place_result(o, res, self.now())
            self._order_seq += 1                # T8d: an order may now exist on the exchange
            self.guard.record_actions(1)
            self.metrics["places"] += 1
            if res.latency_ms:
                self.guard.record_latency(res.latency_ms)
            t = self.now()
            self._jlog("place", side=q.side.value, price=q.price, size=q.size,
                       oid=res.oid, cloid=o.cloid, kind=res.kind.value,
                       latency_ms=round(res.latency_ms, 3), error=res.error)
            if res.kind is PlaceKind.RESTING:
                self.guard.record_success()
                self._cross_streak[q.side] = 0
            elif res.kind is PlaceKind.REJECT_CROSS:       # expected under fast markets: not an error
                self.metrics["cross_rejects"] += 1
                self._cross_streak[q.side] += 1
                self._blocked_until[q.side] = t + min(0.25 * 2 ** self._cross_streak[q.side], 5.0)
                log.info("ALO crossed (%s @ %.6f), backing off: %s", q.side.value, q.price, res.error)
            elif res.kind is PlaceKind.REJECT_OTHER:
                self.metrics["other_rejects"] += 1
                self.guard.record_error()
                self.guard.record_reject(q.side)       # T7b window: immune to account-poll successes
                n = self.guard.side_rejects_60s(q.side)
                if n >= self.rcfg.side_pause_rejects_60s:
                    self._blocked_until[q.side] = t + self.rcfg.side_pause_s
                    log.warning("pausing %s side %.0fs: %d rejects in 60s",
                                q.side.value, self.rcfg.side_pause_s, n)
                else:
                    self._blocked_until[q.side] = t + 5.0
                log.warning("order rejected (%s @ %.6f): %s", q.side.value, q.price, res.error)
            elif res.kind is PlaceKind.ERROR:               # state unknown -> reconcile soon
                self.metrics["place_errors"] += 1
                self.guard.record_error()
                self.guard.record_reject(q.side)       # T7b: order-action failure counts in the window
                n = self.guard.side_rejects_60s(q.side)
                if n >= self.rcfg.side_pause_rejects_60s:
                    self._blocked_until[q.side] = t + self.rcfg.side_pause_s
                    log.warning("pausing %s side %.0fs: %d rejects in 60s",
                                q.side.value, self.rcfg.side_pause_s, n)
                else:
                    self._blocked_until[q.side] = t + 1.0
                self._force_reconcile = True
                log.error("place failed (state unknown): %s", res.error)
            else:
                log.warning("unexpected immediate fill on Alo: %s", res.raw)

    # ---- housekeeping ----------------------------------------------------------------
    def _stale_threshold_s(self) -> float:
        """T7e: confirm/pull threshold = K x rolling-median cadence; legacy stale_book_s when cold."""
        c = self.gw.book_cadence_s()
        return self.rcfg.stale_k * c if c else self.rcfg.stale_book_s

    def _restart_threshold_s(self) -> float:
        """T7e: feed restart only after N x cadence of silence; legacy 2 x stale_book_s when cold."""
        c = self.gw.book_cadence_s()
        return self.rcfg.restart_n * c if c else self.rcfg.stale_book_s * 2

    async def housekeeping(self) -> None:
        now = self.now()
        if self.halted:
            return
        if self.gw.feed_age_s() > self._restart_threshold_s() and now - self._last_restart > 10.0:
            await self._restart_feeds("book feed stale")   # T7e: the ONLY silence-driven restart path
        if now - self._last_account >= 5.0:
            self._last_account = now
            try:
                self._poll_issue_t = time.monotonic()        # T8c: issue time, recorded BEFORE the await
                seq_at_issue = self._fill_seq                # T8c: fills applied while the poll is in flight
                t0 = time.monotonic()
                st = await self.gw.account_state()
                rtt_ms = (time.monotonic() - t0) * 1e3
                self._poll_rtt_ms = rtt_ms
                self.guard.record_latency(rtt_ms)            # REST probe: lets latency recover/trip even when quoting is vetoed
                t_lock = time.monotonic()
                async with self._lock:
                    lock_wait_ms = (time.monotonic() - t_lock) * 1e3
                    self._poll_lock_wait_ms = lock_wait_ms
                    size_local = self.pos.size
                    if abs(st.size - size_local) > 1e-9:
                        self.metrics["pos_drift"] += 1       # difference OBSERVED (adopted or not)
                        inflight = self._fill_seq - seq_at_issue
                        adopt = False
                        if inflight:
                            outcome = "deferred (%d fill(s) applied while poll was in flight)" % inflight
                        elif (self._pending_poll_size is not None
                              and abs(st.size - self._pending_poll_size) <= 1e-9):
                            outcome = "adopting exchange (2 agreeing polls)"
                            adopt = True
                        else:
                            self._pending_poll_size = st.size
                            outcome = "awaiting confirm"
                        log.warning("position drift local=%.6f exchange=%.6f -> %s "
                                    "(rtt=%.0fms lock_wait=%.0fms)",
                                    size_local, st.size, outcome, rtt_ms, lock_wait_ms)
                        if adopt:
                            self.pos = st
                            self._pending_poll_size = None
                        else:                                # keep the fresher local size, take the rest
                            self.pos.entry_px = st.entry_px
                            self.pos.unrealized_pnl = st.unrealized_pnl
                            self.pos.margin_used = st.margin_used
                            self.pos.account_value = st.account_value
                    else:
                        self._pending_poll_size = None       # agrees with local: streak reset
                        self.pos = st
                    self.guard.update_equity(st.account_value)
                    if self.loss_guard is not None:
                        # Same equity source as RiskGuard, so the two stops agree.
                        mid = self.market.mid if self.market is not None else None
                        if mid is not None:
                            self.loss_guard.on_poll(st.account_value, self.pos.size, mid)
                        self._guard_action = self.loss_guard.evaluate(
                            mid, self.pos.size)
                    self._jlog("equity", equity=st.account_value, position=st.size,
                               entry_px=st.entry_px, unrealized_pnl=st.unrealized_pnl,
                               margin_used=st.margin_used, rtt_ms=round(rtt_ms, 3),
                               peak_equity=self.guard.peak_equity,
                               start_equity=self.guard.start_equity)
                    # T7b: no record_success() here - a good poll must not reset order-action errors
                log.debug("account poll rtt=%.0fms lock_wait=%.0fms local=%.6f exchange=%.6f seq=%d",
                          rtt_ms, lock_wait_ms, self.pos.size, st.size, seq_at_issue)
            except Exception:
                log.exception("account poll failed")
                self.guard.record_error()
        # Hard-loss guard: act here as well as in cycle(), so a dead book feed
        # cannot starve the stop (staleness escalates without any book events).
        if self.loss_guard is not None and not self.halted:
            if self._guard_action is GuardAction.HARD:
                async with self._lock:
                    await self._hard_stop_locked()
                return
            if self._guard_action is GuardAction.PULL:
                async with self._lock:
                    if not self.halted:
                        await self._pull_quotes_locked(self.now())
        if self.learning is not None:
            try:
                wall = time.time()
                self.learning.settle(wall)
                self.learning.maybe_trim(wall)
            except Exception:
                log.exception("learning settle/trim failed (trading continues)")
        # Dead-feed-safe kill check: enforce the account-loss stop even when no
        # fresh book event is arriving (cycle() may be starved by a dead feed).
        if not self.halted:
            ref_mid = self.market.mid if self.market is not None else self.pos.entry_px
            reason = self.guard.check_kill(self.pos, ref_mid)
            if reason is not None:
                await self._kill_locked(reason)
                return
        if now - self._last_reconcile >= 15.0 or self._force_reconcile or self.om.stale(now):
            self._last_reconcile = now
            self._force_reconcile = False
            try:
                seq_at_issue = self._order_seq          # T8d: snapshot must not race a place/cancel
                remote = await self.gw.open_orders()
                async with self._lock:
                    raced = self._order_seq != seq_at_issue
                    rep = None if raced else self.om.reconcile(remote, self.now())
                if raced:
                    log.debug("reconcile deferred: %d order change(s) during open_orders()",
                              self._order_seq - seq_at_issue)
                else:
                    if rep.orphans:
                        log.warning("orphan orders on exchange (oids=%s): cancelling",
                                    [r.oid for r in rep.orphans])
                        await self.gw.cancel_oids([r.oid for r in rep.orphans])
                        self.metrics["orphans_cancelled"] += len(rep.orphans)
                    for o in rep.closed_locally:
                        log.warning("order %s no longer on exchange: marked %s", o.cloid, o.status.value)
                        self.metrics["stale_pruned"] += 1
            except Exception:
                log.exception("reconcile failed")
                self.guard.record_error()
        self.om.prune_terminal(now)
        # T8c hunk D (REQ-0028.2): no maybe_cycle() here - housekeeping (feed watchdog, account
        # poll, reconcile) must never queue behind cycle()'s REST calls; cycle_loop drives quoting.

    async def housekeeping_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.housekeeping()
            except Exception:
                log.exception("housekeeping crashed (continuing)")
            await asyncio.sleep(0.5)

    # ---- metrics -----------------------------------------------------------------------
    def metrics_line(self) -> str:
        """One greppable line: counters + guard state + position/equity + peak RSS (budget 300 MB)."""
        m, g = self.metrics, self.guard
        actions = g.actions_last_min() if g else 0
        lat_ema = g.lat_ema_ms if g else 0.0
        equity = g.equity if g else self.pos.account_value
        rss_peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # Linux: KiB -> MiB
        lg = self.loss_guard
        lg_mid = self.market.mid if self.market is not None else None
        lg_est = (lg.estimated_equity(lg_mid)
                  if (lg is not None and lg_mid is not None) else None)
        lg_loss = (lg.e0 - lg_est) if (lg is not None and lg_est is not None) else 0.0
        lg_action = self._guard_action.value if lg is not None else "off"
        return ("metrics book=%d fills=%d places=%d modifies=%d cancels=%d "
                "cross_rejects=%d risk_rejects=%d other_rejects=%d place_errors=%d "
                "modify_errors=%d parks=%d park_fallbacks=%d stale_pulls=%d feed_restarts=%d pos_drift=%d "
                "orphans_cancelled=%d stale_pruned=%d veto_purges=%d actions_min=%d lat_ema_ms=%.0f "
                "pos=%.6f equity=%.2f rss_peak_mb=%.0f feed_age=%.2f "
                "guard=%s guard_loss=%.3f hard_stops=%d guard_pulls=%d") % (
            m["book"], m["fills"], m["places"], m["modifies"], m["cancels"],
            m["cross_rejects"], m["risk_rejects"], m["other_rejects"], m["place_errors"],
            m["modify_errors"], m["parks"], m["park_fallbacks"], m["stale_pulls"], m["feed_restarts"],
            m["pos_drift"], m["orphans_cancelled"], m["stale_pruned"], m["veto_purges"], actions, lat_ema,
            self.pos.size, equity, rss_peak_mb, self.gw.feed_age_s(),
            lg_action, lg_loss, m["hard_stops"], m["guard_pulls"])

    def emit_metrics(self) -> None:
        log.info(self.metrics_line())

    async def metrics_loop(self) -> None:
        self.emit_metrics()                      # first line immediately, then every interval
        while True:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.metrics_interval_s)
            except asyncio.TimeoutError:
                self.emit_metrics()
                continue
            return                               # stop_event set (shutdown or kill)

    # ---- request-budget polling ------------------------------------------------------
    async def _poll_rate_limit(self) -> None:
        """Refresh the authoritative address request budget (free /info read)."""
        getter = getattr(self.gw, "user_rate_limit", None)
        if getter is None:
            return
        try:
            resp = await getter()
        except Exception:
            log.exception("userRateLimit poll failed")
            return
        if not resp:
            return
        self.budget.update_from_api(resp)
        self._last_rate_poll = self.now()
        self.metrics["rate_polls"] += 1
        allowed = self.budget.allowed_per_h()
        burn = self.budget.burn_per_h()
        self._jlog("rate_limit", used=self.budget.nRequestsUsed, cap=self.budget.nRequestsCap,
                   headroom=self.budget.headroom, mode=self.budget.mode,
                   allowed_per_h=round(allowed, 2), burn_per_h=round(burn, 2),
                   earned_per_h=round(self.budget.earned_per_h(), 2),
                   cost_mode=self.batch_planner.mode, cum_vlm=self.budget.cumVlm,
                   verdict=self.budget.last_verdict)
        log.info("request budget: used=%d cap=%d headroom=%d mode=%s "
                 "allowed/h=%.1f burn/h=%.1f cumVlm=%.2f cost_mode=%s",
                 self.budget.nRequestsUsed, self.budget.nRequestsCap,
                 self.budget.headroom, self.budget.mode, allowed, burn,
                 self.budget.cumVlm, self.batch_planner.mode)

    async def rate_limit_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self._poll_rate_limit()
            except Exception:
                log.exception("rate-limit loop error (continuing)")
            try:
                await asyncio.wait_for(self.stop_event.wait(),
                                       timeout=max(1.0, self._rate_poll_interval_s))
                return
            except asyncio.TimeoutError:
                continue

    async def _restart_feeds(self, reason: str) -> None:
        self._last_restart = self.now()
        self.metrics["feed_restarts"] += 1
        ws = getattr(self.gw, "ws_msgs", 0)
        silence = self.now() - self._last_book_t if self._last_book_t > -1e8 else float("inf")
        log.warning("feed restart: %s | book_silence=%.1fs ws_msgs_since_prev_restart=%d",
                    reason, silence, ws - self._ws_msgs_at_restart)   # T7c observability
        self._ws_msgs_at_restart = ws
        try:
            await self.gw.restart_feeds()
        except Exception:
            log.exception("feed restart failed")
            self.guard.record_error()

    # ---- shutdown / kill ----------------------------------------------------------------
    async def _pull_quotes_locked(self, now: float) -> None:
        """PULL: equity is stale/unreadable, so stop quoting blind.

        Cancels once per episode; ``_pulled`` is cleared when the guard returns
        OK/SOFT again.
        """
        if self._pulled:
            return
        self._pulled = True
        resting = [o for o in self.om.active() if o.status is OrderStatus.RESTING]
        self.metrics["guard_pulls"] += 1
        log.warning("LOSS GUARD PULL: equity estimate stale/unreadable; "
                    "cancelling %d resting quote(s) pos=%.6f",
                    len(resting), self.pos.size)
        self._jlog("loss_guard_pull", orders=len(resting), pos=self.pos.size)
        await self._do_cancels(resting, now)

    async def _hard_stop_locked(self) -> None:
        """HARD: cancel all, taker-flatten ladder, verify, write HALT, exit 3/4.

        Caller must hold ``self._lock`` (or be the only writer).  Idempotent.
        """
        if self.halted:
            return
        self.halted = True
        self.kill_reason = KillReason.MAX_ACCOUNT_LOSS
        self.metrics["hard_stops"] += 1
        g = self.loss_guard
        mid = self.market.mid if self.market is not None else None
        est = g.estimated_equity(mid) if (g is not None and mid is not None) else None
        log.critical("LOSS GUARD HARD STOP: start=%.5f est=%s pos=%.6f mid=%s halt=%s",
                     g.e0 if g else 0.0,
                     f"{est:.5f}" if est is not None else "n/a",
                     self.pos.size, mid, g.halt_path if g else None)
        self._jlog("hard_stop", start_equity=(g.e0 if g else None),
                   est_equity=est, position=self.pos.size, mid=mid,
                   halt_path=(g.halt_path if g else None))

        def _glog(msg: str) -> None:
            (log.critical if "CRITICAL" in msg else log.warning)("hard_stop: %s", msg)

        try:
            res = await hard_stop_sequence(_LossGuardGateway(self.gw),
                                           g.halt_path if g else None, _glog)
            self._hard_exit_code = res.exit_code
            flat, attempts = res.flat, res.attempts
        except Exception:
            # Fail closed: never leave the process running after a HARD decision.
            log.exception("hard_stop_sequence crashed")
            self._hard_exit_code = 4
            flat, attempts = False, 0
            try:
                if g is not None and g.halt_path:
                    with open(g.halt_path, "w") as fh:
                        fh.write("hard stop\n")
            except OSError as e:
                log.critical("could not write HALT marker: %r", e)
        finally:
            log.critical("LOSS GUARD HARD STOP complete: flat=%s attempts=%d exit=%d",
                         flat, attempts, self._hard_exit_code)
            self._jlog("hard_stop_done", flat=flat, attempts=attempts,
                       exit_code=self._hard_exit_code)
            self.stop_event.set()

    async def _shutdown_actions(self, flatten: bool) -> None:
        for _ in range(3):
            try:
                await self.gw.cancel_all()
                break
            except Exception:
                log.exception("cancel_all failed, retrying")
                await asyncio.sleep(0.5)
        leftover = await cancel_sweep(self.gw)             # T7b: catch in-flight orders that landed late
        if leftover:
            log.critical("orders remained after shutdown cancel rounds: %s", [r.oid for r in leftover])
        for o in self.om.active():
            o.status = OrderStatus.CANCELED
        if not flatten:
            return
        for _ in range(3):
            try:
                await self.gw.flatten()
                break
            except Exception:
                log.exception("flatten failed, retrying")
                await asyncio.sleep(0.5)
        for _ in range(6):
            try:
                st = await self.gw.account_state()
                if abs(st.size) < 1e-9:
                    log.info("verified flat")
                    return
            except Exception:
                log.exception("verify failed")
            await asyncio.sleep(0.5)
        log.critical("NOT VERIFIED FLAT - MANUAL INTERVENTION REQUIRED")

    async def _kill_locked(self, reason: KillReason) -> None:
        if self.halted:
            return
        self.halted, self.kill_reason = True, reason
        log.critical("KILL SWITCH: %s | equity=%.2f peak=%.2f lat_ema=%.0fms pos=%.6f",
                     reason.value, self.guard.equity, self.guard.peak_equity, self.guard.lat_ema_ms, self.pos.size)
        await self._shutdown_actions(flatten=True)
        self.stop_event.set()

    # ---- run ------------------------------------------------------------------------------
    async def run(self) -> int:
        try:
            await self.init()
        except BaseException:
            await self.gw.stop()
            raise
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:   # Windows
                pass
        tasks = [asyncio.create_task(self.event_loop()), asyncio.create_task(self.housekeeping_loop()),
                 asyncio.create_task(self.metrics_loop()), asyncio.create_task(self.cycle_loop())]  # T8c hunk C
        if self.use_batch_planner:
            log.info("budget-aware batch planner ENABLED: bands=%s cost_mode=%s reserve=%d horizon=%.0fh",
                     self.bands, self.batch_planner.mode, self.budget.reserve, self.budget.horizon_h)
            tasks.append(asyncio.create_task(self.rate_limit_loop()))
        await self.stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not self.halted:
            async with self._lock:
                await self._shutdown_actions(flatten=self.flatten_on_exit)
        await self.gw.stop()
        log.info("stopped. metrics=%s", dict(self.metrics))
        if self._hard_exit_code is not None:
            return self._hard_exit_code
        return 2 if self.halted else 0


def setup_file_logging(log_dir: str = "logs") -> None:
    """Rotating file log: 5 MB x 5 at <log_dir>/bot.log. Secrets never reach it:
    Settings.api_secret is repr=False and no log statement formats the key (see tests)."""
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, "bot.log"),
                                  maxBytes=5 * 1024 * 1024, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


def _bands_from_env(default_half_spread: float) -> BandParams:
    return BandParams(
        half_spread_bps=float(os.environ.get("MM_BAND_HALF_SPREAD_BPS", str(default_half_spread))),
        near_frac=float(os.environ.get("MM_BAND_NEAR_FRAC", "0.35")),
        near_max_bps=float(os.environ.get("MM_BAND_NEAR_MAX_BPS", "7.0")),
        far_frac=float(os.environ.get("MM_BAND_FAR_FRAC", "1.0")),
        far_min_bps=float(os.environ.get("MM_BAND_FAR_MIN_BPS", "20.0")),
        pull_frac=float(os.environ.get("MM_BAND_PULL_FRAC", "0.45")),
        pull_resume_s=float(os.environ.get("MM_BAND_PULL_RESUME_S", "20.0")))


async def _budget_dry_run(s, minutes: float, cost_mode: str) -> int:
    """Live market feed, full EWMARef + BatchPlanner loop, zero order dispatch.

    This is the required 15-30 minute pre-flight: it measures the projected
    writes/hour against a fresh-wallet request budget without touching the
    exchange order book.
    """
    log.info("DRY-RUN BUDGET: connecting to %s %s feed (no orders will be sent)",
             s.env, s.coin)
    gw = HyperliquidGateway(s.base_url, s.account_address, s.api_secret, s.coin,
                            dry_run=True, fast_book=s.risk.fast_book)
    await gw.start()
    budget = RequestBudget(now=time.monotonic)
    resp = await gw.user_rate_limit()
    if resp:
        budget.update_from_api(resp)
        log.info("DRY-RUN BUDGET: start used=%d cap=%d headroom=%d cumVlm=%.2f",
                 budget.nRequestsUsed, budget.nRequestsCap, budget.headroom, budget.cumVlm)
    bands = _bands_from_env(s.strategy.half_spread_bps)
    ewma = EWMARef(tau_s=float(os.environ.get("MM_EWMA_TAU_S", "3.0")))
    # A spent account (headroom <= reserve) is STANDDOWN, so the planner emits
    # nothing and the run cannot measure consumption.  For a *projection* we
    # mirror a fresh wallet's budget (it earns 10,000 + $1 traded).  The real
    # budget still gates and is reported alongside the projection.
    proj_budget = RequestBudget(now=time.monotonic)
    proj_cap = int(os.environ.get("MM_DRY_RUN_PROJ_CAP", "10000"))
    proj_budget.update_from_api({"nRequestsUsed": 0, "nRequestsCap": proj_cap,
                                 "cumVlm": budget.cumVlm})
    planner = BatchPlanner(proj_budget, bands, charge_mode=cost_mode, now=time.monotonic)
    tox_deb = ToxicityDebouncer(int(os.environ.get("MM_TOX_CONFIRM_N", "3")),
                                int(os.environ.get("MM_TOX_CLEAR_N", "10")))
    urgent_pending = False
    tox_fires = 0
    log.info("DRY-RUN BUDGET: projection uses a fresh-wallet budget used=0 cap=%d "
             "(real used=%d cap=%d headroom=%d mode=%s)",
             proj_cap, budget.nRequestsUsed, budget.nRequestsCap,
             budget.headroom, budget.mode)

    live: dict = {"buy": None, "sell": None}
    quote_t0: dict = {"buy": 0.0, "sell": 0.0}
    oid = [1000]
    actions: Counter = Counter()
    calls = 0
    minute_calls: dict = Counter()
    minute_costs: dict = Counter()
    h_eff_samples = []
    start = time.monotonic()
    last_plan = 0.0
    last_report = start
    last_poll = start
    active_s = 0.0
    last_sample = start
    min_interval = max(0.1, float(s.strategy.min_requote_interval_s))

    def _aged(now: float) -> dict:
        out = {}
        for side in ("buy", "sell"):
            lq = live.get(side)
            if lq is None:
                out[side] = None
            else:
                out[side] = LiveQuote(lq.oid, lq.px, lq.sz, max(0.0, now - quote_t0[side]))
        return out

    def _apply(payloads, now: float) -> int:
        sent = 0
        for p in payloads:
            cost = planner._cost(len(p.legs))
            actions[p.kind] += len(p.legs)
            for leg in p.legs:
                side = leg.side
                if p.kind in ("modify", "place") and leg.px:
                    if p.kind == "place" or live.get(side) is None:
                        oid[0] += 1
                        qid = oid[0]
                    else:
                        qid = live[side].oid
                    live[side] = LiveQuote(qid, float(leg.px),
                                           float(leg.sz) if leg.sz else 0.0, 0.0)
                    quote_t0[side] = now
                elif p.kind == "cancel":
                    live[side] = None
                    quote_t0[side] = 0.0
            planner.on_sent(p)
            sent += cost
        return sent

    try:
        while time.monotonic() - start < minutes * 60.0:
            try:
                ev = await asyncio.wait_for(gw.events.get(), timeout=0.5)
            except asyncio.TimeoutError:
                ev = None
            now = time.monotonic()
            # active-quoting uptime sampling (wall-clock weighted)
            if any(live.get(s) is not None for s in ("buy", "sell")):
                active_s += max(0.0, now - last_sample)
            last_sample = now

            if isinstance(ev, BookEvent) and ev.market.is_valid:
                ref = ewma.update(ev.market.mid, now)
                if ev.micro is not None and tox_deb.update(ev.micro, s.strategy):
                    urgent_pending = True
                    tox_fires += 1
                if now - last_plan >= min_interval:
                    last_plan = now
                    sz = round_sz(s.strategy.quote_notional / ref, gw.spec.sz_decimals)
                    payloads = planner.plan(_aged(now), ref, {"buy": sz, "sell": sz},
                                            urgent_risk=urgent_pending, skip=frozenset())
                    urgent_pending = False
                    added = _apply(payloads, now)
                    if added:
                        calls += added
                        bucket = int((now - start) // 60)
                        minute_calls[bucket] += added
                        minute_costs[bucket] += added
            if now - last_report >= 60.0:
                h_eff_samples.append(planner.h_eff())
                elapsed_h = max((now - start) / 3600.0, 1e-9)
                log.info("DRY-RUN BUDGET: %.1f min calls=%d writes/h=%.1f h_eff=%.2fbps "
                         "tokens=%.1f proj_headroom=%d proj_allowed/h=%.1f "
                         "real_mode=%s real_headroom=%d actions=%s",
                         (now - start) / 60.0, calls, calls / elapsed_h,
                         planner.h_eff(), proj_budget._tokens, proj_budget.headroom,
                         proj_budget.allowed_per_h(), budget.mode,
                         budget.headroom, dict(actions))
                last_report = now
            if now - last_poll >= 30.0:
                r = await gw.user_rate_limit()
                if r:
                    budget.update_from_api(r)
                last_poll = now
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await gw.stop()

    elapsed_s = max(time.monotonic() - start, 1e-9)
    elapsed_h = elapsed_s / 3600.0
    h_eff_samples.append(planner.h_eff())
    h_eff_avg = sum(h_eff_samples) / len(h_eff_samples)
    uptime = 100.0 * active_s / elapsed_s
    dist = " ".join(f"m{m + 1}={minute_calls.get(m, 0)}" for m in range(int(minutes) + 1))
    summary = (
        f"DRY-RUN BUDGET COMPLETE: {calls} calls in {elapsed_s / 60.0:.1f} min -> "
        f"{calls / elapsed_h:.1f} projected writes/hour on a fresh cap={proj_cap} wallet "
        f"(real used={budget.nRequestsUsed} cap={budget.nRequestsCap} "
        f"headroom={budget.headroom} mode={budget.mode}); "
        f"uptime={uptime:.1f}% h_eff(avg)={h_eff_avg:.2f}bps h_eff(final)={planner.h_eff():.2f}bps "
        f"tox_fires={tox_fires} minutes[{dist}] actions={dict(actions)}")
    log.info(summary)
    print(summary)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="hl-mm-bot")
    ap.add_argument("--dry-run-budget", action="store_true",
                    help="run the full budget planner on live HYPE data without sending orders")
    ap.add_argument("--minutes", type=float, default=float(os.environ.get("MM_DRY_RUN_MINUTES", "30")),
                    help="dry-run duration in minutes (default 30)")
    ap.add_argument("--cost-mode", choices=("call", "leg"),
                    default=os.environ.get("MM_BATCH_COST_MODE", "call"),
                    help="request cost model for a multi-leg bulk call")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    setup_file_logging()
    s = load_settings()
    log.info("env=%s coin=%s dry_run=%s strategy=%s risk=%s", s.env, s.coin, s.dry_run, s.strategy, s.risk)
    if args.dry_run_budget:
        sys.exit(asyncio.run(_budget_dry_run(s, args.minutes, args.cost_mode)))
    gw = HyperliquidGateway(s.base_url, s.account_address, s.api_secret, s.coin,
                            dry_run=s.dry_run, fast_book=s.risk.fast_book)
    bot = Bot(gw, s.strategy, s.risk, flatten_on_exit=s.flatten_on_exit)
    sys.exit(asyncio.run(bot.run()))


if __name__ == "__main__":
    main()
