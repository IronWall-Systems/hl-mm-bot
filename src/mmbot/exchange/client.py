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
import math
import re
import time
from collections import deque
from statistics import median
from typing import Any, Deque, List, Optional, Protocol, Tuple

from ..models import MarketState, MicroState, PositionState, Side
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
    async def bulk_place(self, orders: List[dict]) -> List[PlaceResult]: ...
    async def modify_quotes(self, modifies: List[dict]) -> List[PlaceResult]: ...
    async def cancel(self, cloid: str) -> CancelResult: ...
    async def cancel_oid(self, oid: int) -> CancelResult: ...
    async def cancel_oids(self, oids: List[int]) -> List[CancelResult]: ...
    async def cancel_cloids(self, cloids: List[str]) -> List[CancelResult]: ...
    async def cancel_all(self) -> int: ...
    async def flatten(self, slippage: float = 0.05) -> None: ...
    async def account_state(self) -> PositionState: ...
    async def open_orders(self) -> List[RemoteOrder]: ...
    async def user_rate_limit(self) -> dict: ...
    async def restart_feeds(self) -> None: ...
    def feed_age_s(self) -> float: ...
    def book_cadence_s(self) -> Optional[float]: ...
    async def book_snapshot(self) -> Optional[MarketState]: ...


# ---- pure parsers (unit-tested without the SDK) --------------------------------
def _side(code: str) -> Side:
    return Side.BUY if code == "B" else Side.SELL


def _status_to_place_result(st: Any, resp: Any) -> PlaceResult:
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


def parse_place_response(resp: Any) -> PlaceResult:
    try:
        if resp.get("status") != "ok":
            return PlaceResult(PlaceKind.REJECT_OTHER, error=str(resp.get("response", resp)), raw=resp)
        st = resp["response"]["data"]["statuses"][0]
    except (AttributeError, KeyError, IndexError, TypeError):
        return PlaceResult(PlaceKind.ERROR, error=f"unparseable response: {resp!r}", raw=resp)
    return _status_to_place_result(st, resp)


def _parse_status_list(resp: Any, n: int) -> List[PlaceResult]:
    """Parse an order/batchModify response containing one status per requested leg."""
    if not isinstance(resp, dict):
        return [PlaceResult(PlaceKind.ERROR, error=f"unparseable response: {resp!r}", raw=resp)
                for _ in range(n)]
    if resp.get("status") != "ok":
        err = str(resp.get("response", resp))
        return [PlaceResult(PlaceKind.REJECT_OTHER, error=err, raw=resp) for _ in range(n)]
    try:
        sts = resp["response"]["data"]["statuses"]
    except (KeyError, IndexError, TypeError):
        return [PlaceResult(PlaceKind.ERROR, error=f"unparseable response: {resp!r}", raw=resp)
                for _ in range(n)]
    out = [_status_to_place_result(st, resp) for st in sts]
    while len(out) < n:                       # defensive: never misalign callers
        out.append(PlaceResult(PlaceKind.ERROR, error="missing status in response", raw=resp))
    return out[:n]


def parse_bulk_place_response(resp: Any, n: int) -> List[PlaceResult]:
    return _parse_status_list(resp, n)


def parse_modify_response(resp: Any, n: int) -> List[PlaceResult]:
    """``batchModify`` returns the NEW oid for each modified leg."""
    return _parse_status_list(resp, n)


def _cancel_result_from_status(st: Any) -> CancelResult:
    if st == "success":
        return CancelResult(True)
    err = str(st.get("error", st)) if isinstance(st, dict) else str(st)
    gone = any(m in err.lower() for m in GONE_MARKERS)
    return CancelResult(False, already_gone=gone, error=err)


def parse_cancel_response(resp: Any) -> CancelResult:
    try:
        if resp.get("status") != "ok":
            return CancelResult(False, error=str(resp))
        st = resp["response"]["data"]["statuses"][0]
    except (AttributeError, KeyError, IndexError, TypeError):
        return CancelResult(False, error=f"unparseable response: {resp!r}")
    return _cancel_result_from_status(st)


def parse_bulk_cancel_response(resp: Any, n: int) -> List[CancelResult]:
    if not isinstance(resp, dict):
        return [CancelResult(False, error=f"unparseable response: {resp!r}") for _ in range(n)]
    if resp.get("status") != "ok":
        return [CancelResult(False, error=str(resp)) for _ in range(n)]
    try:
        sts = resp["response"]["data"]["statuses"]
    except (KeyError, IndexError, TypeError):
        return [CancelResult(False, error=f"unparseable response: {resp!r}") for _ in range(n)]
    out = [_cancel_result_from_status(st) for st in sts]
    while len(out) < n:
        out.append(CancelResult(False, error="missing status in response"))
    return out[:n]


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
def install_monotonic_nonce() -> None:
    """Make every SDK-signed action use a strictly increasing millisecond nonce.

    ``hyperliquid.exchange`` resolves ``get_timestamp_ms`` from its module globals at
    call time, so replacing it here affects ``order``/``bulk_orders``/
    ``bulk_modify_orders_new``/``bulk_cancel`` without forking the SDK.  Two actions
    signed in the same millisecond would otherwise share a nonce and one would be
    rejected with "Invalid nonce: duplicate nonce".
    """
    import hyperliquid.exchange as hx
    if getattr(hx, "_mmbot_monotonic_nonce", False):
        return
    original = hx.get_timestamp_ms
    state = {"last": 0}

    def _monotonic_ms() -> int:
        n = max(int(original()), state["last"] + 1)
        state["last"] = n
        return n

    hx.get_timestamp_ms = _monotonic_ms
    hx._mmbot_monotonic_nonce = True


class HyperliquidGateway:
    def __init__(self, base_url: str, account_address: str, api_secret: str, coin: str,
                 dry_run: bool = False, fast_book: bool = True, timeout: float = 10.0):
        self.base_url, self.addr, self._secret = base_url, account_address, api_secret
        self.coin, self.dry_run, self.fast_book = coin, dry_run, fast_book
        self.timeout = float(timeout)
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
        # ---- lean 3-signal microstructure state (callbacks run in the SDK thread) ----
        self._trade_flow: Deque[Tuple[float, float]] = deque(maxlen=4000)  # (mono, signed sz)
        self._btc_hist: Deque[Tuple[float, float]] = deque(maxlen=600)     # (mono, mid)
        self._sig4_wall_bid: Optional[Tuple[float, float]] = None           # (px, bps)
        self._sig4_wall_ask: Optional[Tuple[float, float]] = None
        self._obi_nw = 0.0
        self._btc_ready = False

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
        install_monotonic_nonce()
        # Fetch meta once and hand the SAME objects to Info and Exchange.  The SDK's
        # Exchange.__init__ otherwise builds its own Info, which re-fetches spot_meta
        # (a redundant HTTP round trip at startup; not in the hot path, but avoidable).
        bootstrap = Info(self.base_url, skip_ws=True, timeout=self.timeout)
        meta = bootstrap.meta()
        spot_meta = bootstrap.spot_meta()
        self.info = Info(self.base_url, skip_ws=False, meta=meta, spot_meta=spot_meta,
                         timeout=self.timeout)
        acct = None if self.addr.lower() == wallet.address.lower() else self.addr   # API wallet -> master
        self.exchange = Exchange(wallet, self.base_url, meta=meta, spot_meta=spot_meta,
                                 account_address=acct, timeout=self.timeout)
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
        # optional microstructure feeds: never let one unsupported channel break trading
        for sub, cb, label in (
            ({"type": "trades", "coin": self.coin}, self._on_trades, "trades"),
            ({"type": "bbo", "coin": "BTC"}, self._on_btc_bbo, "bbo/BTC"),
            ({"type": "l2Book", "coin": self.coin, "nSigFigs": 4}, self._on_l2_sig4, "l2Book/sig4"),
        ):
            try:
                self.info.subscribe(sub, cb)
                log.info("%s subscription: %s", label, sub)
            except Exception:
                log.exception("optional %s subscription failed (trading continues)", label)

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
            self._update_obi(msg)
            ev = parse_l2(msg, self._last_book_mono)
            if ev:
                self._emit(BookEvent(ev.market, self._micro_snapshot(now)))
        except Exception:
            log.exception("l2 parse failed: %r", msg)

    # ---- microstructure callbacks (SDK thread) --------------------------------
    def _update_obi(self, msg: Any) -> None:
        try:
            bids, asks = msg["data"]["levels"]
            def wsum(levels) -> float:
                return sum(float(l["sz"]) * math.log1p(int(l.get("n") or 1))
                           for l in levels[:5])
            b, a = wsum(bids), wsum(asks)
            self._obi_nw = (b - a) / (b + a) if (b + a) else 0.0
        except Exception:
            pass

    def _on_trades(self, msg) -> None:
        self.ws_msgs += 1
        try:
            now = time.monotonic()
            for t in msg.get("data", []) or []:
                try:
                    sz = float(t.get("sz", 0.0))
                    if sz <= 0:
                        continue
                    sign = 1.0 if t.get("side") == "B" else -1.0
                    self._trade_flow.append((now, sign * sz))
                except (TypeError, ValueError):
                    continue
            while self._trade_flow and now - self._trade_flow[0][0] > 5.0:
                self._trade_flow.popleft()
        except Exception:
            log.exception("trades parse failed: %r", msg)

    def _on_btc_bbo(self, msg) -> None:
        """BTC best bid/offer feed -> 1 s lead return (mid, not last trade)."""
        self.ws_msgs += 1
        try:
            bbo = msg.get("data", {}).get("bbo") or []
            if len(bbo) < 2:
                return
            bid, ask = float(bbo[0]["px"]), float(bbo[1]["px"])
            if bid <= 0 or ask <= 0:
                return
            now = time.monotonic()
            self._btc_hist.append((now, 0.5 * (bid + ask)))
            while self._btc_hist and now - self._btc_hist[0][0] > 3.0:
                self._btc_hist.popleft()
        except Exception:
            log.exception("bbo/BTC parse failed: %r", msg)

    def _on_all_mids(self, msg) -> None:
        self.ws_msgs += 1
        try:
            data = msg.get("data", {})
            mids = data.get("mids", data) if isinstance(data, dict) else {}
            price = mids.get("BTC")
            if price is None:
                return
            now = time.monotonic()
            self._btc_hist.append((now, float(price)))
            while self._btc_hist and now - self._btc_hist[0][0] > 3.0:
                self._btc_hist.popleft()
        except Exception:
            log.exception("allMids parse failed: %r", msg)

    def _on_l2_sig4(self, msg) -> None:
        """Verified walls from the medium (nSigFigs=4) book, filtered to 12-22 bps."""
        self.ws_msgs += 1
        try:
            bids, asks = msg["data"]["levels"]
            if not bids or not asks:
                return
            bb, ba = float(bids[0]["px"]), float(asks[0]["px"])
            mid = 0.5 * (bb + ba)
            sizes = [float(l["sz"]) for l in bids] + [float(l["sz"]) for l in asks]
            med = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
            if mid <= 0 or med <= 0:
                return

            def nearest(levels, side: str):
                best = None
                for l in levels:
                    px, sz, n = float(l["px"]), float(l["sz"]), int(l.get("n") or 0)
                    if n < 5 or sz < 3.0 * med or sz <= 0:
                        continue
                    dist = abs(mid - px) / mid * 1e4
                    if not (12.0 <= dist <= 22.0):
                        continue
                    if best is None or dist < best[1]:
                        best = (px, dist)
                return best
            self._sig4_wall_bid = nearest(bids, "bid")
            self._sig4_wall_ask = nearest(asks, "ask")
        except Exception:
            log.exception("l2Book/sig4 parse failed: %r", msg)

    def _micro_snapshot(self, now: float) -> MicroState:
        cut = now - 2.0
        buy = sell = 0.0
        for ts, v in self._trade_flow:
            if ts < cut:
                continue
            if v > 0:
                buy += v
            else:
                sell += -v
        tfi2 = (buy - sell) / (buy + sell) if (buy + sell) else 0.0

        lead = 0.0
        ready = False
        if self._btc_hist:
            latest = self._btc_hist[-1][1]
            base = None
            for ts, px in self._btc_hist:
                if ts <= now - 1.0:
                    base = px
                else:
                    break
            if base and latest:
                lead = (latest / base - 1.0) * 1e4
                ready = True
        self._btc_ready = ready
        wb = self._sig4_wall_bid
        wa = self._sig4_wall_ask
        return MicroState(
            obi_nw=self._obi_nw, tfi2=tfi2, lead_btc_1s=lead,
            wall_bid_px=wb[0] if wb else None, wall_bid_bps=wb[1] if wb else None,
            wall_ask_px=wa[0] if wa else None, wall_ask_bps=wa[1] if wa else None,
            btc_ready=ready, ts=now,
        )

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

    def _order_request(self, o: dict) -> dict:
        from hyperliquid.utils.types import Cloid
        req = {
            "coin": self.coin,
            "is_buy": o["side"].is_buy,
            "sz": o["size"],
            "limit_px": o["price"],
            "order_type": {"limit": {"tif": o.get("tif", "Alo")}},
            "reduce_only": False,
        }
        cloid = o.get("cloid")
        if cloid:
            req["cloid"] = Cloid.from_str(cloid)
        return req

    async def bulk_place(self, orders: List[dict]) -> List[PlaceResult]:
        """Place all legs in ONE L1 action (``bulk_orders``)."""
        if not orders:
            return []
        if self.dry_run:
            out = []
            for o in orders:
                self._fake_oid += 1
                log.info("[dry-run] bulk place %s %.6f @ %.6f",
                         o["side"].value, o["size"], o["price"])
                out.append(PlaceResult(PlaceKind.RESTING, oid=self._fake_oid))
            return out
        t0 = time.monotonic()
        try:
            reqs = [self._order_request(o) for o in orders]
            async with self._lock:
                resp = await asyncio.to_thread(self.exchange.bulk_orders, reqs)
        except Exception as e:  # network / signing / SDK exceptions: state unknown
            return [PlaceResult(PlaceKind.ERROR, error=repr(e),
                                latency_ms=(time.monotonic() - t0) * 1e3) for _ in orders]
        _raw("bulk_place", resp)
        lat = (time.monotonic() - t0) * 1e3
        out = parse_bulk_place_response(resp, len(orders))
        for r in out:
            r.latency_ms = lat
        return out

    async def modify_quotes(self, modifies: List[dict]) -> List[PlaceResult]:
        """Update all legs in ONE L1 action (``batchModify``). Each result carries the
        NEW oid; the old oid is invalid immediately."""
        if not modifies:
            return []
        if self.dry_run:
            out = []
            for m in modifies:
                self._fake_oid += 1
                log.info("[dry-run] modify oid=%s -> %s @ %.6f",
                         m.get("oid"), m["size"], m["price"])
                out.append(PlaceResult(PlaceKind.RESTING, oid=self._fake_oid))
            return out
        t0 = time.monotonic()
        try:
            reqs = [{"oid": int(m["oid"]), "order": self._order_request(m)}
                    for m in modifies]
            async with self._lock:
                resp = await asyncio.to_thread(self.exchange.bulk_modify_orders_new, reqs)
        except Exception as e:  # state unknown
            return [PlaceResult(PlaceKind.ERROR, error=repr(e),
                                latency_ms=(time.monotonic() - t0) * 1e3) for _ in modifies]
        _raw("modify_quotes", resp)
        lat = (time.monotonic() - t0) * 1e3
        out = parse_modify_response(resp, len(modifies))
        for r in out:
            r.latency_ms = lat
        return out

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

    async def cancel_oids(self, oids: List[int]) -> List[CancelResult]:
        """Cancel all oids in ONE L1 action (``bulk_cancel``)."""
        if not oids:
            return []
        if self.dry_run:
            return [CancelResult(True) for _ in oids]
        t0 = time.monotonic()
        try:
            reqs = [{"coin": self.coin, "oid": int(o)} for o in oids]
            async with self._lock:
                resp = await asyncio.to_thread(self.exchange.bulk_cancel, reqs)
        except Exception as e:
            return [CancelResult(False, error=repr(e),
                                 latency_ms=(time.monotonic() - t0) * 1e3) for _ in oids]
        _raw("cancel_oids", resp)
        lat = (time.monotonic() - t0) * 1e3
        out = parse_bulk_cancel_response(resp, len(oids))
        for r in out:
            r.latency_ms = lat
        return out

    async def cancel_cloids(self, cloids: List[str]) -> List[CancelResult]:
        """Cancel all cloids in ONE L1 action (``bulk_cancel_by_cloid``)."""
        if not cloids:
            return []
        if self.dry_run:
            return [CancelResult(True) for _ in cloids]
        t0 = time.monotonic()
        try:
            from hyperliquid.utils.types import Cloid
            reqs = [{"coin": self.coin, "cloid": Cloid.from_str(c)} for c in cloids]
            async with self._lock:
                resp = await asyncio.to_thread(self.exchange.bulk_cancel_by_cloid, reqs)
        except Exception as e:
            return [CancelResult(False, error=repr(e),
                                 latency_ms=(time.monotonic() - t0) * 1e3) for _ in cloids]
        _raw("cancel_cloids", resp)
        lat = (time.monotonic() - t0) * 1e3
        out = parse_bulk_cancel_response(resp, len(cloids))
        for r in out:
            r.latency_ms = lat
        return out

    async def cancel_all(self) -> int:
        rows = await self.open_orders()
        if rows and not self.dry_run:
            await self.cancel_oids([r.oid for r in rows])
            log.info("cancel_all(%d) -> bulk_cancel", len(rows))
        return len(rows)

    async def flatten(self, slippage: float = 0.05) -> None:
        if self.dry_run:
            log.info("[dry-run] flatten")
            return
        async with self._lock:
            resp = await asyncio.to_thread(self.exchange.market_close, self.coin,
                                           slippage=slippage)
        log.warning("flatten slippage=%.2f -> %s", slippage,
                    _redact(json.dumps(resp, default=str)))

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

    async def user_rate_limit(self) -> dict:
        """Authoritative address request budget.

        ``/info`` reads are free and do NOT increment ``nRequestsUsed``; this is
        the only way to observe the cumulative exchange-action counter.
        """
        if self.info is None:
            return {}
        try:
            return await asyncio.to_thread(self.info.user_rate_limit, self.addr) or {}
        except Exception as e:
            log.warning("userRateLimit failed: %r", e)
            return {}

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
