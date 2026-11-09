"""Order lifecycle + diffing. Pure state machine: no I/O, no clock (callers pass `now`).

Rules that keep exposure bounded:
  * At most ONE active order per side. If a side has a pending (unacked) order or cancel,
    we do nothing for that side until it resolves (no stacking, no double exposure).
  * Cancel-then-place: a requote cancels first; the replacement is placed on a LATER cycle
    after the cancel is acknowledged.
  * Hysteresis: only requote when the target moved >= requote_threshold_bps (saves the
    address-based action budget: 10k initial + 1 per $1 traded).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional

from ..exchange.types import OrderUpdateEvent, PlaceKind, PlaceResult, RemoteOrder
from ..models import Order, OrderStatus, Quote, Side, StrategyParams
from .batch_planner import LiveQuote

log = logging.getLogger("mmbot.orders")
EPS = 1e-12


@dataclass
class Plan:
    cancels: List[Order] = field(default_factory=list)
    modifies: List["Modify"] = field(default_factory=list)
    places: List[Quote] = field(default_factory=list)


@dataclass
class Modify:
    """An in-place quote update: the live order plus the new target."""
    order: Order
    quote: Quote


@dataclass
class ReconcileReport:
    orphans: List[RemoteOrder] = field(default_factory=list)   # on exchange, unknown locally -> cancel
    closed_locally: List[Order] = field(default_factory=list)  # we thought active, exchange says gone


class OrderManager:
    def __init__(self, params: StrategyParams, stale_after_s: float = 5.0, keep_terminal_s: float = 60.0):
        self.p = params
        self.stale_after_s = stale_after_s
        self.keep_terminal_s = keep_terminal_s
        self.orders: Dict[str, Order] = {}
        self._by_oid: Dict[int, Order] = {}

    # ---- queries -----------------------------------------------------------
    def active(self, side: Optional[Side] = None) -> List[Order]:
        return [o for o in self.orders.values() if o.is_active and (side is None or o.side is side)]

    def exposure_notional(self, side: Side) -> float:
        """Notional that could still fill on this side (includes unacked + pending-cancel)."""
        return sum(o.notional_remaining for o in self.active(side))

    @staticmethod
    def new_cloid() -> str:
        return "0x" + uuid.uuid4().hex   # 128-bit hex, the format the API expects

    # ---- planning ------------------------------------------------------------
    def _needs_requote(self, o: Order, t: Quote) -> bool:
        bps = abs(o.price - t.price) / t.price * 1e4
        if bps >= self.p.requote_threshold_bps:
            return True
        return o.remaining < (1.0 - self.p.size_tolerance) * t.size

    def plan(self, targets: Iterable[Quote], now: float, blocked: FrozenSet[Side] = frozenset()) -> Plan:
        tmap = {q.side: q for q in targets}
        plan = Plan()
        for side in (Side.BUY, Side.SELL):
            act = self.active(side)
            if any(o.status in (OrderStatus.PENDING_NEW, OrderStatus.PENDING_MODIFY,
                                OrderStatus.PENDING_CANCEL) for o in act):
                continue                                    # wait for acks
            t = tmap.get(side)
            if t is None:
                plan.cancels.extend(act)
                continue
            if act:
                keep = min(act, key=lambda o: abs(o.price - t.price))
                plan.cancels.extend(o for o in act if o is not keep)   # defensive: duplicates
                if self._needs_requote(keep, t):
                    plan.modifies.append(Modify(keep, t))              # in-place, one L1 action
                continue
            if side not in blocked:
                plan.places.append(t)
        return plan

    # ---- budget-aware batch planning -----------------------------------------
    def active_order(self, side: Side) -> Optional[Order]:
        """The single live order on a side (the oldest, to keep oid identity)."""
        act = self.active(side)
        return min(act, key=lambda o: o.created_ts) if act else None

    def in_flight_sides(self) -> FrozenSet[Side]:
        """Sides with an unacked place/modify/cancel: the planner must skip them."""
        return frozenset(
            side for side in (Side.BUY, Side.SELL)
            if any(o.status in (OrderStatus.PENDING_NEW, OrderStatus.PENDING_MODIFY,
                                OrderStatus.PENDING_CANCEL)
                   for o in self.active(side)))

    def order_for_side(self, side) -> Optional[Order]:
        """Resolve a planner leg's side back to the tracked resting order."""
        try:
            return self.active_order(Side(str(getattr(side, "value", side)).lower()))
        except (ValueError, KeyError):
            return None

    def live_quotes(self, now: float) -> Dict[str, Optional[LiveQuote]]:
        """``{side: LiveQuote | None}`` for the planner; pending sides are None."""
        out: Dict[str, Optional[LiveQuote]] = {}
        pending = self.in_flight_sides()
        for side in (Side.BUY, Side.SELL):
            o = None if side in pending else self.active_order(side)
            if o is None or o.status is not OrderStatus.RESTING or o.oid is None:
                out[side.value] = None
            else:
                out[side.value] = LiveQuote(oid=o.oid, px=o.price, sz=o.remaining,
                                            age_s=max(0.0, now - o.updated_ts))
        return out

    def plan_batched(self, batch_planner, ref: float, tgt_sz: Dict[str, float], now: float,
                     urgent_risk: bool = False, skip: Optional[FrozenSet[Side]] = None):
        """Delegate to ``BatchPlanner`` with in-flight sides passed as ``skip``.

        ``tgt_sz`` is ``{"buy": size, "sell": size}``; a 0/missing side is
        disabled (e.g. inventory cap) and the planner emits a protective pull.
        """
        skip_set = self.in_flight_sides() if skip is None else frozenset(skip)
        live = self.live_quotes(now)
        for side in skip_set:
            live[side.value] = None
        return batch_planner.plan(live, ref, tgt_sz, urgent_risk=urgent_risk,
                                  skip=frozenset(s.value for s in skip_set))

    # ---- state transitions ----------------------------------------------------
    def register_pending(self, q: Quote, now: float) -> Order:
        o = Order(cloid=self.new_cloid(), side=q.side, price=q.price, size=q.size,
                  created_ts=now, updated_ts=now, tif=q.tif)
        self.orders[o.cloid] = o
        return o

    def mark_cancel_sent(self, o: Order, now: float) -> None:
        if o.status in (OrderStatus.RESTING, OrderStatus.PENDING_NEW, OrderStatus.PENDING_MODIFY):
            o.status = OrderStatus.PENDING_CANCEL
            o.cancel_sent_ts = now
            o.updated_ts = now

    def mark_modify_sent(self, o: Order, now: float) -> None:
        if o.status is OrderStatus.RESTING:
            o.status = OrderStatus.PENDING_MODIFY
            o.updated_ts = now

    def on_place_result(self, o: Order, res: PlaceResult, now: float) -> None:
        o.updated_ts = now
        if res.kind is PlaceKind.RESTING:
            if o.status is OrderStatus.PENDING_NEW:
                o.status = OrderStatus.RESTING
            o.oid = res.oid
            if res.oid is not None:
                self._by_oid[res.oid] = o
        elif res.kind is PlaceKind.FILLED:
            o.status = OrderStatus.FILLED
            o.oid = res.oid
            o.filled = o.size
        elif res.kind in (PlaceKind.REJECT_CROSS, PlaceKind.REJECT_OTHER):
            o.status = OrderStatus.REJECTED
        # PlaceKind.ERROR: leave PENDING_NEW; staleness pruning + reconcile decide its fate

    def on_modify_result(self, o: Order, q: Quote, res: PlaceResult, now: float) -> None:
        """Apply a ``batchModify`` ack.

        Hyperliquid's ``batchModify`` CANCELS the old order and returns a NEW oid;
        the old oid is immediately invalid and the cloid is not carried over.
        """
        o.updated_ts = now
        if res.kind is PlaceKind.RESTING:
            old = o.oid
            if old is not None:
                self._by_oid.pop(old, None)
            o.oid = res.oid
            if res.oid is not None:
                self._by_oid[res.oid] = o
            o.price = q.price
            o.size = q.size
            o.status = OrderStatus.RESTING
        elif res.kind is PlaceKind.FILLED:
            old = o.oid
            if old is not None:
                self._by_oid.pop(old, None)
            o.status = OrderStatus.FILLED
            o.oid = res.oid
            if res.oid is not None:
                self._by_oid[res.oid] = o
            o.price = q.price
            o.size = q.size
            o.filled = o.size
        elif res.kind in (PlaceKind.REJECT_CROSS, PlaceKind.REJECT_OTHER):
            # A rejected modify normally leaves the old order resting. Keep it
            # active and let the next cycle/reconcile decide; do not stack.
            o.status = OrderStatus.RESTING
            o.updated_ts = now
        # PlaceKind.ERROR: leave PENDING_MODIFY; reconcile decides its fate.

    def on_cancel_result(self, o: Order, ok: bool, already_gone: bool, now: float) -> None:
        o.updated_ts = now
        if ok or already_gone:
            if o.status is OrderStatus.PENDING_CANCEL:
                o.status = OrderStatus.CANCELED
            # Retire the oid mapping on a confirmed cancel so a later exchange
            # update for that oid cannot resurrect a dead order.
            if o.oid is not None:
                self._by_oid.pop(o.oid, None)
        else:
            if o.status is OrderStatus.PENDING_CANCEL:
                o.status = OrderStatus.RESTING     # cancel failed; it may still be live

    def _find(self, cloid: Optional[str], oid: Optional[int]) -> Optional[Order]:
        # oid is authoritative when present.  batchModify preserves the cloid
        # but rotates the oid and emits a "canceled" update for the OLD oid, so
        # matching by cloid first would resurrect that stale cancel against the
        # live order.  Only fall back to the cloid for an order that has not yet
        # received any oid.
        if oid is not None:
            o = self._by_oid.get(oid)
            if o is not None:
                return o
            if cloid and cloid in self.orders and self.orders[cloid].oid is None:
                return self.orders[cloid]
            return None
        if cloid and cloid in self.orders:
            return self.orders[cloid]
        return None

    def _adopt_oid(self, o: Order, oid: int) -> None:
        """Bind a modified order to its new oid, retiring the old mapping."""
        if o.oid is not None:
            self._by_oid.pop(o.oid, None)
        o.oid = oid
        self._by_oid[oid] = o

    def on_order_update(self, ev: OrderUpdateEvent, now: float) -> Optional[Order]:
        o = self._by_oid.get(ev.oid) if ev.oid is not None else None
        if o is None and ev.cloid:
            cand = self.orders.get(ev.cloid)
            if cand is not None:
                if cand.oid is None:
                    o = cand
                elif ev.oid is not None and ev.oid == cand.oid:
                    o = cand
                elif ev.status == "filled" and ev.oid is not None:
                    # A fill on the new oid raced ahead of the modify ack.
                    self._adopt_oid(cand, ev.oid)
                    o = cand
                # else: retired old-oid event from batchModify -> ignore
        if o is None:
            return None
        if ev.oid is not None and o.oid is None:
            o.oid = ev.oid
            self._by_oid[ev.oid] = o
        st = ev.status
        o.updated_ts = now
        if st == "open":
            if o.status is OrderStatus.PENDING_NEW:
                o.status = OrderStatus.RESTING
        elif st == "filled":
            o.status = OrderStatus.FILLED
            o.filled = o.size
        elif st == "canceled" or st.endswith("Canceled"):
            o.status = OrderStatus.CANCELED
        elif st == "rejected" or st.endswith("Rejected"):
            o.status = OrderStatus.REJECTED
        else:
            log.warning("unhandled order status %r for %s", st, o.cloid)
        return o

    def on_fill(self, cloid: Optional[str], oid: Optional[int], sz: float, now: float) -> Optional[Order]:
        o = self._by_oid.get(oid) if oid is not None else None
        if o is None and cloid:
            cand = self.orders.get(cloid)
            if cand is not None:
                if cand.oid is None or cand.oid == oid:
                    o = cand
                elif oid is not None:
                    # Fill on the new post-modify oid before the modify ack.
                    self._adopt_oid(cand, oid)
                    o = cand
        if o is None:
            return None
        o.filled += sz
        o.updated_ts = now
        if o.filled >= o.size - EPS and o.status is not OrderStatus.CANCELED:
            o.status = OrderStatus.FILLED
        return o

    # ---- hygiene ----------------------------------------------------------------
    def stale(self, now: float) -> List[Order]:
        return [o for o in self.active()
                if o.status in (OrderStatus.PENDING_NEW, OrderStatus.PENDING_MODIFY,
                                OrderStatus.PENDING_CANCEL)
                and now - o.updated_ts > self.stale_after_s]

    def reconcile(self, remote: List[RemoteOrder], now: float) -> ReconcileReport:
        rep = ReconcileReport()
        r_cloids = {r.cloid for r in remote if r.cloid}
        r_oids = {r.oid for r in remote}
        for o in self.active():
            alive = o.cloid in r_cloids or (o.oid is not None and o.oid in r_oids)
            if alive:
                if o.status in (OrderStatus.PENDING_NEW, OrderStatus.PENDING_MODIFY):
                    o.status = OrderStatus.RESTING
                continue
            # not on exchange. Give very fresh pending orders a grace window (ack in flight).
            if o.status is OrderStatus.PENDING_NEW and now - o.updated_ts <= self.stale_after_s:
                continue
            o.status = OrderStatus.REJECTED if o.status is OrderStatus.PENDING_NEW else OrderStatus.CANCELED
            o.updated_ts = now
            rep.closed_locally.append(o)
        local_cloids = set(self.orders)
        local_oids = set(self._by_oid)
        for r in remote:
            if (r.cloid not in local_cloids) and (r.oid not in local_oids):
                rep.orphans.append(r)
        return rep

    def prune_terminal(self, now: float) -> None:
        dead = [c for c, o in self.orders.items()
                if not o.is_active and now - o.updated_ts > self.keep_terminal_s]
        for c in dead:
            o = self.orders.pop(c)
            if o.oid is not None:
                self._by_oid.pop(o.oid, None)
