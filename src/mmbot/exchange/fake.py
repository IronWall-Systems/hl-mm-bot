"""In-memory exchange used by the offline tests and for local dry runs of the orchestrator."""
from __future__ import annotations

import asyncio
import itertools
import time
from typing import Dict, List, Optional

from ..models import MarketState, PositionState, Side
from ..precision import AssetSpec
from .types import (BookEvent, CancelResult, FillEvent, OrderUpdateEvent, PlaceKind, PlaceResult,
                    RemoteOrder)


class FakeGateway:
    def __init__(self, coin="BTC", sz_decimals=5, bid=70000.0, ask=70001.0, equity=1000.0):
        self.events: asyncio.Queue = asyncio.Queue()
        self.spec = AssetSpec(coin, sz_decimals)
        self.coin = coin
        self.bid, self.ask = bid, ask               # the TRUE exchange book
        self.orders: Dict[str, dict] = {}
        self.position = 0.0
        self.equity = equity
        self.latency_ms = 5.0
        self.feed_age = 0.0
        self.forced: List[PlaceKind] = []            # forced results for upcoming place_alo calls
        self.place_calls: List[tuple] = []
        self.cancel_calls: List[str] = []
        self.cancel_all_calls = 0
        self.flatten_calls = 0
        self.restart_calls = 0
        self.hidden_orders: List[RemoteOrder] = []   # orders that exist remotely but unknown to the bot
        self.late_orders: List[RemoteOrder] = []     # T7b: land after the 1st cancel_all (race sim)
        self.ghost_orders: List[RemoteOrder] = []    # T7b: visible to open_orders, never cancellable
        self.snapshot_book: Optional[tuple] = None   # T7b: REST book override (default: current book)
        self.snapshot_fails = False
        self.book_snapshot_calls = 0
        self.cadence: Optional[float] = None         # T7e: rolling-median book cadence (None -> legacy)
        self._oid = itertools.count(1)
        self._tid = itertools.count(1)

    async def start(self): pass
    async def stop(self): pass
    def feed_age_s(self): return self.feed_age

    def book_cadence_s(self): return self.cadence

    def set_book(self, bid, ask, publish=True, size=5.0):
        self.bid, self.ask = bid, ask
        for cloid, o in list(self.orders.items()):
            if (o["side"] is Side.BUY and o["px"] >= ask) or (o["side"] is Side.SELL and o["px"] <= bid):
                self._fill(cloid)
        if publish:
            self.events.put_nowait(BookEvent(MarketState(self.coin, bid, ask, size, size, time.monotonic())))

    def _fill(self, cloid):
        o = self.orders.pop(cloid)
        self.position += o["side"].sign * o["sz"]
        self.events.put_nowait(FillEvent(self.coin, o["side"], o["px"], o["sz"], o["oid"], next(self._tid), 0.0,
                                         time.time(), False, cloid))
        self.events.put_nowait(OrderUpdateEvent(o["oid"], cloid, "filled", o["side"], o["px"], o["sz"]))

    async def place_alo(self, side, price, size, cloid):
        self.place_calls.append((side, price, size, cloid))
        if self.forced:
            kind = self.forced.pop(0)
            if kind is PlaceKind.ERROR:
                return PlaceResult(kind, error="simulated network error", latency_ms=self.latency_ms)
            return PlaceResult(kind, error="Post only order would have immediately matched" if kind is PlaceKind.REJECT_CROSS else "simulated reject",
                               latency_ms=self.latency_ms)
        if (side is Side.BUY and price >= self.ask) or (side is Side.SELL and price <= self.bid):
            return PlaceResult(PlaceKind.REJECT_CROSS, error="Post only order would have immediately matched",
                               latency_ms=self.latency_ms)
        oid = next(self._oid)
        self.orders[cloid] = {"side": side, "px": price, "sz": size, "oid": oid}
        self.events.put_nowait(OrderUpdateEvent(oid, cloid, "open", side, price, size))
        return PlaceResult(PlaceKind.RESTING, oid=oid, latency_ms=self.latency_ms)

    async def cancel(self, cloid):
        self.cancel_calls.append(cloid)
        o = self.orders.pop(cloid, None)
        if o is None:
            return CancelResult(False, already_gone=True, error="Order was never placed, already canceled, or filled.")
        self.events.put_nowait(OrderUpdateEvent(o["oid"], cloid, "canceled", o["side"], o["px"], o["sz"]))
        return CancelResult(True, latency_ms=self.latency_ms)

    async def cancel_oid(self, oid):
        self.hidden_orders = [h for h in self.hidden_orders if h.oid != oid]
        for c, o in list(self.orders.items()):
            if o["oid"] == oid:
                return await self.cancel(c)
        return CancelResult(True)

    async def cancel_all(self):
        self.cancel_all_calls += 1
        n = len(self.orders) + len(self.hidden_orders)
        self.orders.clear()
        self.hidden_orders.clear()
        if self.late_orders:                     # T7b: race sim - in-flight order lands after this sweep
            self.hidden_orders.extend(self.late_orders)
            self.late_orders = []
        return n

    async def flatten(self):
        self.flatten_calls += 1
        self.position = 0.0

    async def account_state(self):
        mid = 0.5 * (self.bid + self.ask)
        return PositionState(size=self.position, entry_px=mid, unrealized_pnl=0.0,
                             margin_used=abs(self.position) * mid / 10, account_value=self.equity)

    async def open_orders(self):
        rows = [RemoteOrder(self.coin, o["oid"], o["side"], o["px"], o["sz"], c) for c, o in self.orders.items()]
        return rows + list(self.hidden_orders) + list(self.ghost_orders)

    async def book_snapshot(self):
        """T7b: REST l2 confirm - defaults to the current true book (quiet unless overridden)."""
        self.book_snapshot_calls += 1
        if self.snapshot_fails:
            return None
        bid, ask = self.snapshot_book if self.snapshot_book else (self.bid, self.ask)
        return MarketState(self.coin, bid, ask, 5.0, 5.0, time.monotonic())

    async def restart_feeds(self):
        self.restart_calls += 1
        self.feed_age = 0.0
