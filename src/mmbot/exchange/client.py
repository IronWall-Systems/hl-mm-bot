"""Hyperliquid gateway: the ONLY module that imports the SDK.

Threading model: the SDK's websocket runs in its own thread and calls our callbacks there; we
parse, then hand events to the asyncio loop with call_soon_threadsafe. SDK REST calls are
blocking, so they run via asyncio.to_thread and are serialised with a lock (nonce safety).

Facts checked against SDK v0.24.0 source: WebsocketManager does NOT reconnect by itself (its
thread ends if the socket drops) and `orderUpdates`/`userEvents` can be subscribed only once per
Info instance. Hence restart_feeds() builds a fresh Info and the orchestrator runs a watchdog.

NOT verified against a live exchange (verify on testnet, see BUILD_SPEC):
  * the exact post-only-cross error text (CROSS_ERROR_MARKERS)
  * orderUpdates / userFills field names (parsers below follow the documented shapes)
  * whether account_value is non-zero for your account mode (see start-up check in Bot.init)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from statistics import median
from typing import Any, Deque, List, Optional, Protocol

from ..models import MarketState, PositionState, Side
from ..precision import AssetSpec
from .types import (BookEvent, CancelResult, FillEvent, OrderUpdateEvent, PlaceKind,
                    PlaceResult, RemoteOrder)

log = logging.getLogger("mmbot.gateway")

CROSS_ERROR_MARKERS = ("post only", "immediately match", "would have been aggressive")
GONE_MARKERS = ("already canceled", "never placed", "already filled", "filled")

_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def _redact(text: str) -> str:
    return _ADDR_RE.sub(lambda m: m.group(0)[:6] + "..." + m.group(0)[-4:], text)


def _raw(label: str, payload: Any) -> None:
    log.info("%s raw: %s", label, _redact(json.dumps(payload, separators=(",", ":"),
                                                     default=str)))


class Gateway(Protocol):
    events: "asyncio.Queue"
    spec: Optional[AssetSpec]

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def place_alo(self, side: Side, price: float, size: float, cloid: str) -> PlaceResult: ...
    async def cancel(self, cloid: str) -> CancelResult: ...
    async def cancel_oid(self, oid: int) -> CancelResult: ...
    async def cancel_all(self) -> int: ...
    async def flatten(self) -> None: ...
    async def account_state(self) -> PositionState: ...
    async def open_orders(self) -> List[RemoteOrder]: ...
    async def restart_feeds(self) -> None: ...
    def feed_age_s(self) -> float: ...
    def book_cadence_s(self) -> Optional[float]: ...
    async def book_snapshot(self) -> Optional[MarketState]: ...


# ---- pure parsers (unit-tested without the SDK) --------------------------------
def _side(code: str) -> Side:
    return Side.BUY if code == "B" else Side.SELL


def parse_place_response(resp: Any) -> PlaceResult:
    try:
        if resp.get("status") != "ok":
            return PlaceResult(PlaceKind.REJECT_OTHER, error=str(resp.get("response", resp)), raw=resp)
        st = resp["response"]["data"]["statuses"][0]
    except (AttributeError, KeyError, IndexError, TypeError):
        return PlaceResult(PlaceKind.ERROR, error=f"unparseable response: {resp!r}", raw=resp)
    if isinstance(st, dict):
        if "resting" in st:
            return PlaceResult(PlaceKind.RESTING, oid=st["resting"]["oid"], raw=resp)
        if "filled" in st:
            return PlaceResult(PlaceKind.FILLED, oid=st["filled"].get("oid"), raw=resp)
        if "error" in st:
            err = str(st["error"])
            low = err.lower()
            kind = PlaceKind.REJECT_CROSS if any(m in low for m in CROSS_ERROR_MARKERS) else PlaceKind.REJECT_OTHER
            return PlaceResult(kind, error=err, raw=resp)
    return PlaceResult(PlaceKind.ERROR, error=f"unknown status {st!r}", raw=resp)


def parse_cancel_response(resp: Any) -> CancelResult:
    try:
        if resp.get("status") != "ok":
            return CancelResult(False, error=str(resp))
        st = resp["response"]["data"]["statuses"][0]
    except (AttributeError, KeyError, IndexError, TypeError):
        return CancelResult(False, error=f"unparseable response: {resp!r}")
    if st == "success":
        return CancelResult(True)
    err = str(st.get("error", st)) if isinstance(st, dict) else str(st)
    gone = any(m in err.lower() for m in GONE_MARKERS)
    return CancelResult(False, already_gone=gone, error=err)


def parse_l2(msg: Any, recv_ts: float) -> Optional[BookEvent]:
    d = msg["data"]
    bids, asks = d["levels"]
    if not bids or not asks:
        return None
    return BookEvent(MarketState(d["coin"], float(bids[0]["px"]), float(asks[0]["px"]),
                                 float(bids[0]["sz"]), float(asks[0]["sz"]), recv_ts,
                                 float(d.get("time", 0)) / 1000.0))


def parse_l2_snapshot(raw: Any, recv_ts: float) -> Optional[MarketState]:
    """T7b: REST /info l2Book payload -> MarketState (the payload is the WS data body)."""
    if isinstance(raw, dict) and "levels" not in raw and isinstance(raw.get("data"), dict):
        raw = raw["data"]
    ev = parse_l2({"data": raw}, recv_ts)
    return ev.market if ev else None


def parse_order_updates(msg: Any) -> List[OrderUpdateEvent]:
    out = []
    for u in msg["data"]:
        o = u["order"]
        out.append(OrderUpdateEvent(oid=o.get("oid"), cloid=o.get("cloid"), status=str(u["status"]),
                                    side=_side(o["side"]) if "side" in o else None,
                                    px=float(o.get("limitPx", 0) or 0), sz=float(o.get("sz", 0) or 0)))
    return out


def parse_fills(msg: Any) -> List[FillEvent]:
    d = msg["data"]
    snap = bool(d.get("isSnapshot", False))
    return [FillEvent(coin=f["coin"], side=_side(f["side"]), px=float(f["px"]), sz=float(f["sz"]),
                      oid=f.get("oid"), tid=f.get("tid"), fee=float(f.get("fee", 0) or 0),
                      time=float(f.get("time", 0)) / 1000.0, is_snapshot=snap, cloid=f.get("cloid"))
            for f in d.get("fills", [])]


def parse_user_state(us: Any, coin: str, spot_us: Any = None) -> PositionState:
    ms = us["marginSummary"]
    account_value = float(ms["accountValue"])
    if spot_us:
        for b in spot_us.get("balances", []):
            if b.get("coin") == "USDC":
                account_value += float(b.get("total", 0.0) or 0.0)
                break
    st = PositionState(account_value=account_value, margin_used=float(ms["totalMarginUsed"]))
    for ap in us.get("assetPositions", []):
        p = ap["position"]
        if p["coin"] == coin:
            st.size = float(p["szi"])
            st.entry_px = float(p.get("entryPx") or 0)
            st.unrealized_pnl = float(p.get("unrealizedPnl") or 0)
    return st


def parse_open_orders(rows: Any, coin: str) -> List[RemoteOrder]:
    return [RemoteOrder(coin=r["coin"], oid=int(r["oid"]), side=_side(r["side"]),
                        price=float(r["limitPx"]), size=float(r["sz"]), cloid=r.get("cloid"))
            for r in rows if r["coin"] == coin]


# ---- real gateway ---------------------------------------------------------------
class HyperliquidGateway:
    def __init__(self, base_url: str, account_address: str, api_secret: str, coin: str,
                 dry_run: bool = False, fast_book: bool = True):
        self.base_url, self.addr, self._secret = base_url, account_address, api_secret
        self.coin, self.dry_run, self.fast_book = coin, dry_run, fast_book
        self.events: asyncio.Queue = asyncio.Queue()
        self.spec: Optional[AssetSpec] = None
        self.info = None
        self.exchange = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = asyncio.Lock()
        self._last_book_mono = 0.0
        self._fake_oid = 0
        self.ws_msgs = 0                    # T7c observability: raw WS messages received
        self._book_gaps: Deque[float] = deque(maxlen=30)   # T7e: book interarrival gaps
        self._last_arrival: Optional[float] = None        # T7e: monotonic time of previous l2Book

    # lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._init_clients)
        await asyncio.to_thread(self._subscribe)

    def _init_clients(self) -> None:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        wallet = Account.from_key(self._secret)
        self.info = Info(self.base_url, skip_ws=False)
        acct = None if self.addr.lower() == wallet.address.lower() else self.addr   # API wallet -> master
        self.exchange = Exchange(wallet, self.base_url, account_address=acct)
        meta = self.info.meta()
        sz = next(a["szDecimals"] for a in meta["universe"] if a["name"] == self.coin)
        self.spec = AssetSpec(self.coin, int(sz))

    def _subscribe(self) -> None:
        sub: dict = {"type": "l2Book", "coin": self.coin}
        if self.fast_book:                   # T7e: fast feed (RISK_FAST_BOOK=0 -> default feed)
            sub["fast"] = True
        log.info("l2Book subscription: %s", sub)
        self.info.subscribe(sub, self._on_l2)
        self.info.subscribe({"type": "orderUpdates", "user": self.addr}, self._on_orders)
        self.info.subscribe({"type": "userFills", "user": self.addr}, self._on_fills)

    async def stop(self) -> None:
        if self.info is None:
            return
        try:
            await asyncio.to_thread(self.info.disconnect_websocket)
        except Exception:
            log.exception("disconnect failed")

    async def restart_feeds(self) -> None:
        log.warning("restarting websocket feeds")
        try:
            await asyncio.to_thread(self.info.disconnect_websocket)
        except Exception:
            log.exception("disconnect failed (continuing)")

        def _rebuild():
            from hyperliquid.info import Info
            self.info = Info(self.base_url, skip_ws=False)
            self._subscribe()
        await asyncio.to_thread(_rebuild)

    def feed_age_s(self) -> float:
        return float("inf") if self._last_book_mono == 0.0 else time.monotonic() - self._last_book_mono

    def book_cadence_s(self) -> Optional[float]:
        """T7e: rolling-median interarrival of l2Book messages; None until warm (5 samples)."""
        if len(self._book_gaps) < 5:
            return None
        return float(median(self._book_gaps))

    # ws callbacks (SDK thread)
    def _emit(self, ev) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self.events.put_nowait, ev)

    def _on_l2(self, msg) -> None:
        self.ws_msgs += 1                   # T7c observability
        try:
            now = time.monotonic()
            if self._last_arrival is not None:
                gap = now - self._last_arrival
                if 0.0 < gap < 60.0:        # T7e: cadence sample (catastrophic gaps excluded)
                    self._book_gaps.append(gap)
            self._last_arrival = now
            self._last_book_mono = now
            ev = parse_l2(msg, self._last_book_mono)
            if ev:
                self._emit(ev)
        except Exception:
            log.exception("l2 parse failed: %r", msg)

    def _on_orders(self, msg) -> None:
        self.ws_msgs += 1                   # T7c observability
        try:
            _raw("orderUpdates", msg)
            for ev in parse_order_updates(msg):
                self._emit(ev)
        except Exception:
            log.exception("orderUpdates parse failed: %r", msg)

    def _on_fills(self, msg) -> None:
        self.ws_msgs += 1                   # T7c observability
        try:
            _raw("userFills", msg)
            for ev in parse_fills(msg):
                self._emit(ev)
        except Exception:
            log.exception("userFills parse failed: %r", msg)

    # actions
    async def place_alo(self, side: Side, price: float, size: float, cloid: str) -> PlaceResult:
        if self.dry_run:
            self._fake_oid += 1
            log.info("[dry-run] place %s %.6f @ %.6f", side.value, size, price)
            return PlaceResult(PlaceKind.RESTING, oid=self._fake_oid)
        t0 = time.monotonic()
        try:
            async with self._lock:
                resp = await asyncio.to_thread(self._place_blocking, side, price, size, cloid)
        except Exception as e:  # network / signing / SDK exceptions: state unknown
            return PlaceResult(PlaceKind.ERROR, error=repr(e), latency_ms=(time.monotonic() - t0) * 1e3)
        _raw("place", resp)
        res = parse_place_response(resp)
        res.latency_ms = (time.monotonic() - t0) * 1e3
        return res

    def _place_blocking(self, side: Side, price: float, size: float, cloid: str):
        from hyperliquid.utils.types import Cloid
        return self.exchange.order(self.coin, side.is_buy, size, price, {"limit": {"tif": "Alo"}},
                                   reduce_only=False, cloid=Cloid.from_str(cloid))

    async def cancel(self, cloid: str) -> CancelResult:
        if self.dry_run:
            return CancelResult(True)
        t0 = time.monotonic()
        try:
            async with self._lock:
                from hyperliquid.utils.types import Cloid
                resp = await asyncio.to_thread(self.exchange.cancel_by_cloid, self.coin, Cloid.from_str(cloid))
        except Exception as e:
            return CancelResult(False, error=repr(e), latency_ms=(time.monotonic() - t0) * 1e3)
        _raw("cancel", resp)
        res = parse_cancel_response(resp)
        res.latency_ms = (time.monotonic() - t0) * 1e3
        return res

    async def cancel_oid(self, oid: int) -> CancelResult:
        if self.dry_run:
            return CancelResult(True)
        try:
            async with self._lock:
                resp = await asyncio.to_thread(self.exchange.cancel, self.coin, oid)
        except Exception as e:
            return CancelResult(False, error=repr(e))
        _raw("cancel_oid", resp)
        return parse_cancel_response(resp)

    async def cancel_all(self) -> int:
        rows = await self.open_orders()
        if rows and not self.dry_run:
            reqs = [{"coin": self.coin, "oid": r.oid} for r in rows]
            async with self._lock:
                resp = await asyncio.to_thread(self.exchange.bulk_cancel, reqs)
            _raw("bulk_cancel", resp)
            log.info("cancel_all(%d) -> %s", len(reqs), resp)
        return len(rows)

    async def flatten(self) -> None:
        if self.dry_run:
            log.info("[dry-run] flatten")
            return
        async with self._lock:
            resp = await asyncio.to_thread(self.exchange.market_close, self.coin)
        log.warning("flatten -> %s", _redact(json.dumps(resp, default=str)))

    async def account_state(self) -> PositionState:
        us = await asyncio.to_thread(self.info.user_state, self.addr)
        try:
            spot = await asyncio.to_thread(self.info.spot_user_state, self.addr)
        except Exception as e:
            log.warning("spot_user_state failed, equity counts margin only: %r", e)
            spot = None
        return parse_user_state(us, self.coin, spot)

    async def open_orders(self) -> List[RemoteOrder]:
        return parse_open_orders(await asyncio.to_thread(self.info.open_orders, self.addr), self.coin)

    async def book_snapshot(self) -> Optional[MarketState]:
        """T7b: one REST l2 snapshot to confirm a silent feed. Budget membership UNVERIFIED
        (see ASSUMPTIONS A-15); the orchestrator issues at most one call per book-silence."""
        try:
            raw = await asyncio.to_thread(self.info.l2_snapshot, self.coin)
            snap = parse_l2_snapshot(raw, time.monotonic())
            _raw("book_snapshot", raw)
            return snap
        except Exception as e:
            log.warning("l2 REST snapshot failed: %r", e)
            return None
