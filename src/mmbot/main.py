"""Orchestrator.  WS event -> state update -> risk check -> quotes -> diff vs live orders -> cancel/place.

Design rules:
  * One lock serialises everything that mutates order/position state (cycle, housekeeping, kill).
  * Book events are coalesced (only the newest matters); requotes are rate-limited.
  * Network calls never raise out of the loop; failures feed the RiskGuard error counter.
  * Kill path: cancel all -> market-flatten -> verify flat -> stop. Never skipped on error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import resource
import signal
import sys
import time
from collections import Counter, deque
from logging.handlers import RotatingFileHandler
from typing import Callable, Deque, Optional, Set

from .config import load_settings
from .exchange.client import Gateway, HyperliquidGateway
from .exchange.types import BookEvent, FeedDownEvent, FillEvent, OrderUpdateEvent, PlaceKind
from .execution.order_manager import OrderManager
from .models import MarketState, OrderStatus, PositionState, RiskConfig, Side, StrategyParams
from .precision import tick_at
from .risk.guard import KillReason, RiskGuard
from .safety import cancel_sweep
from .strategy.quoter import compute_quotes

log = logging.getLogger("mmbot")


class Bot:
    def __init__(self, gw: Gateway, params: StrategyParams, rcfg: RiskConfig,
                 now_fn: Callable[[], float] = time.monotonic, flatten_on_exit: bool = True):
        self.gw, self.p, self.rcfg, self.now = gw, params, rcfg, now_fn
        self.flatten_on_exit = flatten_on_exit
        self.om = OrderManager(params)
        self.market: Optional[MarketState] = None
        self.pos = PositionState()
        self.guard: Optional[RiskGuard] = None
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

    # ---- startup ---------------------------------------------------------------
    async def init(self) -> None:
        await self.gw.start()
        self.pos = await self.gw.account_state()
        if self.pos.account_value <= 0:
            raise RuntimeError("account value reads as 0: cannot enforce drawdown stop "
                               "(wrong address? unified-account mode? unfunded?). Refusing to start.")
        self.guard = RiskGuard(self.rcfg, self.pos.account_value, self.now)
        n = await self.gw.cancel_all()          # start-up hygiene: clear leftovers from earlier runs
        log.info("startup: equity=%.2f position=%.6f, cleared %d stale orders",
                 self.pos.account_value, self.pos.size, n)

    # ---- events ------------------------------------------------------------------
    async def handle_event(self, ev) -> None:
        if isinstance(ev, BookEvent):
            self.market = ev.market
            self._stale_state = None                 # T7b: new book event ends the silence episode
            self._last_book_t = self.now()           # T7c observability
            self.metrics["book"] += 1
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
        if self.now() - self._last_cycle < self.p.min_requote_interval_s:
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
                        return                       # T7e: REST confirm NEVER restarts feeds
                elif self._stale_state == "quiet":
                    age = 0.0                        # already confirmed this episode
                else:
                    return                           # confirmed stale: already pulled + restart sent
            inv0 = self.pos.size                    # T8c REQ-0029: inventory at plan time
            targets = compute_quotes(self.market, self.pos.size, self.p, self.gw.spec)
            blocked = frozenset(s for s, t in self._blocked_until.items() if t > now)
            plan = self.om.plan(targets, now, blocked)
            await self._do_cancels(plan.cancels, now)
            if abs(self.pos.size - inv0) > 1e-12:    # a fill landed during the cancel RESTs:
                targets = compute_quotes(self.market, self.pos.size, self.p, self.gw.spec)
                plan = self.om.plan(targets, now, blocked)          # re-read sizing/inventory
                await self._do_cancels(plan.cancels, now)
            await self._do_places(plan.places, now, age)

    async def _do_cancels(self, orders, now: float) -> None:
        orders = [o for o in orders if o.status in (OrderStatus.RESTING, OrderStatus.PENDING_NEW)]
        for o in orders:
            self.om.mark_cancel_sent(o, now)
        results = await asyncio.gather(*(self.gw.cancel(o.cloid) for o in orders))
        for o, r in zip(orders, results):
            self.om.on_cancel_result(o, r.ok, r.already_gone, self.now())
            self._order_seq += 1                # T8d: cancel completed - any snapshot taken before it is stale
            self.guard.record_actions(1)
            self.metrics["cancels"] += 1
            if r.latency_ms:
                self.guard.record_latency(r.latency_ms)
            if r.ok or r.already_gone:
                self.guard.record_success()
            else:
                self.guard.record_error()
                self._force_reconcile = True
                log.warning("cancel failed %s: %s", o.cloid, r.error)

    async def _do_places(self, quotes, now: float, feed_age: float) -> None:
        mid = self.market.mid
        for q in quotes:
            v = self.guard.pre_trade(q, self.pos, mid, self.om.exposure_notional(q.side), feed_age)
            if not v.ok:
                self.metrics["risk_rejects"] += 1
                log.info("risk veto %s %.2f: %s", q.side.value, q.price, v.reason)
                continue
            o = self.om.register_pending(q, now)
            res = await self.gw.place_alo(q.side, q.price, q.size, o.cloid)
            self.om.on_place_result(o, res, self.now())
            self._order_seq += 1                # T8d: an order may now exist on the exchange
            self.guard.record_actions(1)
            self.metrics["places"] += 1
            if res.latency_ms:
                self.guard.record_latency(res.latency_ms)
            t = self.now()
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
                    # T7b: no record_success() here - a good poll must not reset order-action errors
                log.debug("account poll rtt=%.0fms lock_wait=%.0fms local=%.6f exchange=%.6f seq=%d",
                          rtt_ms, lock_wait_ms, self.pos.size, st.size, seq_at_issue)
            except Exception:
                log.exception("account poll failed")
                self.guard.record_error()
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
                    for r in rep.orphans:
                        log.warning("orphan order on exchange (oid=%s): cancelling", r.oid)
                        await self.gw.cancel_oid(r.oid)
                        self.metrics["orphans_cancelled"] += 1
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
        return ("metrics book=%d fills=%d places=%d cancels=%d cross_rejects=%d risk_rejects=%d "
                "other_rejects=%d place_errors=%d stale_pulls=%d feed_restarts=%d pos_drift=%d "
                "orphans_cancelled=%d stale_pruned=%d actions_min=%d lat_ema_ms=%.0f "
                "pos=%.6f equity=%.2f rss_peak_mb=%.0f feed_age=%.2f") % (
            m["book"], m["fills"], m["places"], m["cancels"], m["cross_rejects"], m["risk_rejects"],
            m["other_rejects"], m["place_errors"], m["stale_pulls"], m["feed_restarts"],
            m["pos_drift"], m["orphans_cancelled"], m["stale_pruned"], actions, lat_ema,
            self.pos.size, equity, rss_peak_mb, self.gw.feed_age_s())

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
        await self.stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not self.halted:
            async with self._lock:
                await self._shutdown_actions(flatten=self.flatten_on_exit)
        await self.gw.stop()
        log.info("stopped. metrics=%s", dict(self.metrics))
        return 2 if self.halted else 0


def setup_file_logging(log_dir: str = "logs") -> None:
    """Rotating file log: 5 MB x 5 at <log_dir>/bot.log. Secrets never reach it:
    Settings.api_secret is repr=False and no log statement formats the key (see tests)."""
    os.makedirs(log_dir, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(log_dir, "bot.log"),
                                  maxBytes=5 * 1024 * 1024, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    setup_file_logging()
    s = load_settings()
    log.info("env=%s coin=%s dry_run=%s strategy=%s risk=%s", s.env, s.coin, s.dry_run, s.strategy, s.risk)
    gw = HyperliquidGateway(s.base_url, s.account_address, s.api_secret, s.coin,
                            dry_run=s.dry_run, fast_book=s.risk.fast_book)
    bot = Bot(gw, s.strategy, s.risk, flatten_on_exit=s.flatten_on_exit)
    sys.exit(asyncio.run(bot.run()))


if __name__ == "__main__":
    main()
