#!/usr/bin/env python3
"""Ultra-stable microstructure signal daemon for the HYPE market-making bot.

This process is deliberately independent of ``live_trial``: it never imports the
trading loop, never reads credentials, and never sends orders.  It subscribes
read-only to public Hyperliquid / Binance market data, computes the three
validated signals plus the ``nSigFigs=4`` wall scan, and atomically publishes
them to a shared JSON state file.

Signals (zero look-ahead, all from data received at or before publish time):
  * ``obi_nw``      top-5 L2 imbalance weighted by ``sz * log1p(n)`` with an
                    exponential level decay ``exp(-0.5*i)``
  * ``tfi2``        2 s aggressive taker-flow imbalance
  * ``lead_btc_1s`` Binance BTC 1 s mid return (bps)
  * ``walls``       verified ``nSigFigs=4`` walls (``n >= 5`` and ``sz >= 3x``
                    median level size), nearest first

State file schema ``mmbot-microstructure/1``:
{
  "schema": "mmbot-microstructure/1", "seq": 42, "ts": 1794..., "iso": "...",
  "coin": "HYPE",
  "signals": {"obi_nw": 0.12, "tfi2": -0.30, "lead_btc_1s": 0.8,
               "btc_ready": true},
  "book": {"mid": 79.0, "best_bid": 78.99, "best_ask": 79.01,
            "spread_bps": 2.5},
  "walls": {"sig": 4,
             "bid": [{"px": 78.9, "sz": 5000, "n": 30, "score": 17000,
                       "dist_bps": 12.6}],
             "ask": [...],
             "nearest_bid": {...}|null, "nearest_ask": {...}|null},
  "health": {"hl_connected": true, "hl_age_s": 0.04,
              "binance_connected": true, "binance_age_s": 0.2,
              "hl_reconnects": 0, "binance_reconnects": 0,
              "engine": "ok", "last_error": null}
}

Usage:
    PYTHONPATH=src python -m mmbot.microstructure_daemon
    PYTHONPATH=src python src/mmbot/microstructure_daemon.py --duration 30
    PYTHONPATH=src python src/mmbot/microstructure_daemon.py --self-test

The trading loop can read the file at any time with ``read_micro_state()`` or
``SharedMicroStateReader`` below.  Reads are lock-free and do not block the
daemon.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from statistics import median
from typing import Any, Deque, Dict, List, Optional, Tuple

import websocket  # websocket-client, already required by hyperliquid-python-sdk

try:  # support both ``python -m mmbot.microstructure_daemon`` and direct script runs
    from .models import MicroState
except ImportError:  # pragma: no cover - executed only when run as a file
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mmbot.models import MicroState

LOG = logging.getLogger("mmbot.microstructure")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_FILE = ROOT / "logs" / "microstructure_state.json"
DEFAULT_LOG_FILE = ROOT / "logs" / "microstructure_daemon.log"
SCHEMA = "mmbot-microstructure/1"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
BINANCE_WS_URL = "wss://fstream.binance.com/ws/btcusdt@bookTicker"

WALL_MIN_ORDERS = 5
WALL_MEDIAN_MULT = 3.0
OBI_LEVELS = 5
OBI_DECAY = 0.5
TFI_WINDOW_S = 2.0
BTC_LOOKBACK_S = 1.0
HL_STALE_S = 12.0
BINANCE_STALE_S = 15.0


def _now() -> float:
    return time.monotonic()


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _round(x: Optional[float], n: int = 6) -> Optional[float]:
    return None if x is None else round(float(x), n)


# --------------------------------------------------------------------------- engine
class MicrostructureEngine:
    """Thread-safe signal accumulator fed by WS callback threads."""

    def __init__(self, coin: str = "HYPE") -> None:
        self.coin = coin
        self._lock = threading.Lock()
        self._trade_flow: Deque[Tuple[float, float]] = deque(maxlen=20000)
        self._btc_hist: Deque[Tuple[float, float]] = deque(maxlen=4000)
        self._obi_nw = 0.0
        self._bid = 0.0
        self._ask = 0.0
        self._mid = 0.0
        self._walls_bid: List[Dict[str, Any]] = []
        self._walls_ask: List[Dict[str, Any]] = []
        self._hl_last = 0.0
        self._hl_l2_last = 0.0
        self._hl_sig4_last = 0.0
        self._hl_trades_last = 0.0
        self._binance_last = 0.0
        self._hl_reconnects = 0
        self._binance_reconnects = 0
        self._last_error: Optional[str] = None
        self._seq = 0
        self._btc_ready = False

    # ---- callbacks ------------------------------------------------------------
    def on_hl_message(self, msg: Any, now: Optional[float] = None) -> None:
        now = _now() if now is None else now
        try:
            channel = msg.get("channel")
            data = msg.get("data", msg)
            if channel == "l2Book" or "levels" in data:
                self._on_l2(data, now)
            elif channel == "trades" or "trades" in msg:
                self._on_trades(data, now)
        except Exception as exc:  # never let a malformed frame kill the feed
            self._last_error = f"hl_parse: {type(exc).__name__}: {exc}"
            LOG.exception("HL message parse failed")

    def _on_l2(self, data: Any, now: float) -> None:
        levels = data.get("levels") if isinstance(data, dict) else None
        if not levels or len(levels) < 2:
            return
        bids, asks = levels[0], levels[1]
        if not bids or not asks:
            return
        self._bid = _f(bids[0].get("px"))
        self._ask = _f(asks[0].get("px"))
        self._mid = 0.5 * (self._bid + self._ask) if self._ask > self._bid > 0 else 0.0
        b = a = 0.0
        for i, lvl in enumerate(bids[:OBI_LEVELS]):
            b += _f(lvl.get("sz")) * math.log1p(max(0, int(lvl.get("n") or 1))) * math.exp(-OBI_DECAY * i)
        for i, lvl in enumerate(asks[:OBI_LEVELS]):
            a += _f(lvl.get("sz")) * math.log1p(max(0, int(lvl.get("n") or 1))) * math.exp(-OBI_DECAY * i)
        with self._lock:
            self._obi_nw = (b - a) / (b + a) if (b + a) else 0.0
            self._hl_last = now
            self._hl_l2_last = now

    def _on_trades(self, data: Any, now: float) -> None:
        rows = data if isinstance(data, list) else data.get("trades", []) if isinstance(data, dict) else []
        with self._lock:
            for t in rows:
                sz = _f(t.get("sz"))
                if sz <= 0:
                    continue
                sign = 1.0 if str(t.get("side", "")).upper() == "B" else -1.0
                self._trade_flow.append((now, sign * sz))
            while self._trade_flow and now - self._trade_flow[0][0] > TFI_WINDOW_S:
                self._trade_flow.popleft()
            self._hl_last = now
            self._hl_trades_last = now

    def on_wall_book(self, msg: Any, now: Optional[float] = None) -> None:
        """``nSigFigs=4`` book -> verified walls, nearest first."""
        now = _now() if now is None else now
        try:
            data = msg.get("data", msg)
            levels = data.get("levels") if isinstance(data, dict) else None
            if not levels or len(levels) < 2:
                return
            bids, asks = levels[0], levels[1]
            if not bids or not asks:
                return
            sizes = [_f(l.get("sz")) for l in bids] + [_f(l.get("sz")) for l in asks]
            med = median(sizes) if sizes else 0.0
            if med <= 0:
                return
            with self._lock:
                ref_mid = self._mid or 0.5 * (_f(bids[0].get("px")) + _f(asks[0].get("px")))
                self._walls_bid = self._scan_side(bids, "bid", ref_mid, med)
                self._walls_ask = self._scan_side(asks, "ask", ref_mid, med)
                self._hl_last = now
                self._hl_sig4_last = now
        except Exception as exc:
            self._last_error = f"wall_parse: {type(exc).__name__}: {exc}"
            LOG.exception("wall book parse failed")

    @staticmethod
    def _scan_side(levels: List[Dict[str, Any]], side: str, mid: float,
                   med: float) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for lvl in levels:
            px, sz, n = _f(lvl.get("px")), _f(lvl.get("sz")), int(lvl.get("n") or 0)
            if px <= 0 or sz <= 0 or n < WALL_MIN_ORDERS or sz < WALL_MEDIAN_MULT * med:
                continue
            dist = abs(mid - px) / mid * 1e4 if mid else 0.0
            out.append({
                "side": side, "px": _round(px, 6), "sz": _round(sz, 4), "n": n,
                "score": _round(sz * math.log1p(n), 2), "dist_bps": _round(dist, 3),
            })
        out.sort(key=lambda w: w["dist_bps"])
        return out[:16]

    def on_binance_mid(self, mid: float, now: Optional[float] = None) -> None:
        now = _now() if now is None else now
        if mid <= 0:
            return
        with self._lock:
            self._btc_hist.append((now, mid))
            while self._btc_hist and now - self._btc_hist[0][0] > 3.0:
                self._btc_hist.popleft()
            self._binance_last = now

    def on_binance_message(self, msg: Any, now: Optional[float] = None) -> None:
        now = _now() if now is None else now
        try:
            bid, ask = _f(msg.get("b")), _f(msg.get("a"))
            if bid > 0 and ask > 0:
                self.on_binance_mid(0.5 * (bid + ask), now)
        except Exception as exc:
            self._last_error = f"binance_parse: {type(exc).__name__}: {exc}"
            LOG.exception("Binance message parse failed")

    def mark_hl_reconnect(self) -> None:
        with self._lock:
            self._hl_reconnects += 1

    def mark_binance_reconnect(self) -> None:
        with self._lock:
            self._binance_reconnects += 1

    def set_error(self, text: str) -> None:
        with self._lock:
            self._last_error = text

    # ---- snapshot -------------------------------------------------------------
    def _tfi2(self, now: float) -> float:
        cut = now - TFI_WINDOW_S
        buy = sell = 0.0
        for ts, v in self._trade_flow:
            if ts < cut:
                continue
            if v > 0:
                buy += v
            else:
                sell += -v
        return (buy - sell) / (buy + sell) if (buy + sell) else 0.0

    def _lead_btc(self, now: float) -> Tuple[float, bool]:
        if not self._btc_hist:
            return 0.0, False
        latest = self._btc_hist[-1][1]
        base = None
        for ts, mid in self._btc_hist:
            if ts <= now - BTC_LOOKBACK_S:
                base = mid
            else:
                break
        if base is None:
            oldest_ts, oldest = self._btc_hist[0]
            if now - oldest_ts >= BTC_LOOKBACK_S * 0.9:
                base = oldest
        if not base or not latest:
            return 0.0, False
        return (latest / base - 1.0) * 1e4, True

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = _now() if now is None else now
        with self._lock:
            tfi2 = self._tfi2(now)
            lead, btc_ready = self._lead_btc(now)
            hl_age = now - self._hl_last if self._hl_last else 999.0
            bn_age = now - self._binance_last if self._binance_last else 999.0
            spread = ((self._ask - self._bid) / self._mid * 1e4
                      if self._mid and self._ask > self._bid > 0 else 0.0)
            self._seq += 1
            return {
                "schema": SCHEMA,
                "seq": self._seq,
                "ts": round(time.time(), 4),
                "iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "coin": self.coin,
                "signals": {
                    "obi_nw": _round(self._obi_nw, 6),
                    "tfi2": _round(tfi2, 6),
                    "lead_btc_1s": _round(lead, 6),
                    "btc_ready": bool(btc_ready),
                },
                "book": {
                    "mid": _round(self._mid, 6),
                    "best_bid": _round(self._bid, 6),
                    "best_ask": _round(self._ask, 6),
                    "spread_bps": _round(spread, 4),
                },
                "walls": {
                    "sig": 4,
                    "bid": list(self._walls_bid),
                    "ask": list(self._walls_ask),
                    "nearest_bid": self._walls_bid[0] if self._walls_bid else None,
                    "nearest_ask": self._walls_ask[0] if self._walls_ask else None,
                },
                "health": {
                    "hl_connected": hl_age < 5.0,
                    "hl_age_s": _round(min(hl_age, 999.0), 3),
                    "binance_connected": bn_age < 5.0,
                    "binance_age_s": _round(min(bn_age, 999.0), 3),
                    "hl_reconnects": self._hl_reconnects,
                    "binance_reconnects": self._binance_reconnects,
                    "engine": "ok" if (self._bid > 0 and self._ask > self._bid) else "warming",
                    "last_error": self._last_error,
                },
            }


# --------------------------------------------------------------------------- state file
def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), default=str)
        fh.write("\n")
        fh.flush()
    os.replace(tmp, path)


def read_state(path: Path = DEFAULT_STATE_FILE) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_micro_state(path: Path = DEFAULT_STATE_FILE,
                     max_age_s: float = 2.0) -> Optional[MicroState]:
    """Map the shared JSON to the trading-loop ``MicroState`` type.

    Returns ``None`` when the file is missing, malformed, or stale.  The reader
    never blocks and never mutates the daemon state.
    """
    data = read_state(path)
    if not data:
        return None
    ts = _f(data.get("ts"))
    if ts <= 0 or time.time() - ts > max_age_s:
        return None
    sig = data.get("signals") or {}
    nearest_bid = (data.get("walls") or {}).get("nearest_bid") or {}
    nearest_ask = (data.get("walls") or {}).get("nearest_ask") or {}
    return MicroState(
        obi_nw=_f(sig.get("obi_nw")),
        tfi2=_f(sig.get("tfi2")),
        lead_btc_1s=_f(sig.get("lead_btc_1s")),
        wall_bid_px=(_f(nearest_bid.get("px")) or None) if nearest_bid else None,
        wall_bid_bps=(_f(nearest_bid.get("dist_bps")) or None) if nearest_bid else None,
        wall_ask_px=(_f(nearest_ask.get("px")) or None) if nearest_ask else None,
        wall_ask_bps=(_f(nearest_ask.get("dist_bps")) or None) if nearest_ask else None,
        btc_ready=bool(sig.get("btc_ready")),
        ts=time.monotonic(),
    )


class SharedMicroStateReader:
    """Caches by file mtime so per-cycle reads cost one ``stat`` when unchanged."""

    def __init__(self, path: Path = DEFAULT_STATE_FILE, max_age_s: float = 2.0) -> None:
        self.path = Path(path)
        self.max_age_s = max_age_s
        self._key = None
        self._cached: Optional[MicroState] = None

    def read(self) -> Optional[MicroState]:
        try:
            st = self.path.stat()
        except OSError:
            self._key, self._cached = None, None
            return None
        # os.replace() swaps in a new inode, so include it alongside mtime/size:
        # coarse filesystems can otherwise reuse the same mtime_ns for two writes.
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if key != self._key:
            self._key = key
            self._cached = read_micro_state(self.path, self.max_age_s)
        elif self._cached is not None:
            # The file has not changed, but it may have aged out of the freshness window.
            data = read_state(self.path)
            ts = _f((data or {}).get("ts"))
            if ts <= 0 or time.time() - ts > self.max_age_s:
                self._cached = None
        return self._cached


# --------------------------------------------------------------------------- feeds
class HyperliquidFeed:
    """SDK WebSocket feed with rebuild-on-stall (SDK has no auto-reconnect)."""

    def __init__(self, engine: MicrostructureEngine, coin: str = "HYPE",
                 base_url: str = "https://api.hyperliquid.xyz") -> None:
        self.engine = engine
        self.coin = coin
        self.base_url = base_url
        self.info = None
        self._lock = threading.Lock()
        self._next_attempt = 0.0
        self._backoff = 5.0

    def start(self) -> None:
        self._build()

    def stop(self) -> None:
        with self._lock:
            info, self.info = self.info, None
        if info is not None:
            try:
                info.disconnect_websocket()
            except Exception:
                LOG.exception("HL disconnect failed")

    def reconnect(self) -> None:
        # The watchdog ticks every 2 s; do not rebuild faster than the backoff
        # window or a sustained exchange outage becomes a reconnect storm.
        now = time.monotonic()
        if now < self._next_attempt:
            return
        LOG.warning("reconnecting Hyperliquid websocket feeds (backoff %.0fs)", self._backoff)
        self.engine.mark_hl_reconnect()
        self.stop()
        self._build()

    def _build(self) -> None:
        from hyperliquid.info import Info
        try:
            info = Info(self.base_url, skip_ws=False, timeout=12)
            subs = [
                ({"type": "l2Book", "coin": self.coin, "fast": True}, self.engine.on_hl_message),
                ({"type": "trades", "coin": self.coin}, self.engine.on_hl_message),
                ({"type": "l2Book", "coin": self.coin, "nSigFigs": 4}, self.engine.on_wall_book),
            ]
            for sub, cb in subs:
                info.subscribe(sub, cb)
                LOG.info("subscribed %s", sub)
            with self._lock:
                self.info = info
            self._backoff = 5.0
            # Give a freshly built feed time to warm up before the next rebuild.
            self._next_attempt = time.monotonic() + 5.0
        except Exception as exc:
            self.engine.set_error(f"hl_connect: {type(exc).__name__}: {exc}")
            LOG.exception("Hyperliquid feed build failed")
            self._next_attempt = time.monotonic() + self._backoff
            self._backoff = min(self._backoff * 2.0, 60.0)


class BinanceFeed:
    """websocket-client BTC bookTicker thread with exponential-backoff reconnect."""

    def __init__(self, engine: MicrostructureEngine, url: str = BINANCE_WS_URL) -> None:
        self.engine = engine
        self.url = url
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._lock = threading.Lock()
        self._backoff = 1.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="binance-bbo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def ensure_alive(self) -> None:
        if not self._thread or not self._thread.is_alive():
            LOG.warning("Binance feed thread dead; restarting")
            self.start()

    def force_reconnect(self) -> None:
        self.engine.mark_binance_reconnect()
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            ws = websocket.WebSocketApp(
                self.url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            with self._lock:
                self._ws = ws
            try:
                ws.run_forever(ping_interval=20, ping_timeout=10, skip_utf8_validation=True)
            except Exception as exc:
                self.engine.set_error(f"binance_ws: {type(exc).__name__}: {exc}")
                LOG.exception("Binance websocket crashed")
            if self._stop.is_set():
                break
            wait = min(self._backoff, 30.0) + random.uniform(0.0, 0.5)
            LOG.warning("Binance feed disconnected; reconnecting in %.1fs", wait)
            time.sleep(wait)
            self._backoff = min(self._backoff * 2.0, 30.0)

    def _on_open(self, _ws) -> None:
        self._backoff = 1.0
        LOG.info("Binance BTC bookTicker connected")

    def _on_message(self, _ws, message: str) -> None:
        try:
            self.engine.on_binance_message(json.loads(message))
        except Exception:
            LOG.exception("Binance frame parse failed")

    def _on_error(self, _ws, error) -> None:
        self.engine.set_error(f"binance_error: {error}")
        LOG.warning("Binance websocket error: %r", error)

    def _on_close(self, _ws, code, reason) -> None:
        LOG.warning("Binance websocket closed code=%s reason=%s", code, reason)


# --------------------------------------------------------------------------- runner
def setup_logging(log_file: Path, verbose: bool = False) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers[:] = [fh, sh]


async def _publish_loop(engine: MicrostructureEngine, state_file: Path,
                        interval_s: float, stop: asyncio.Event) -> None:
    last_log = 0.0
    while not stop.is_set():
        try:
            snap = engine.snapshot()
            atomic_write_json(state_file, snap)
            now = _now()
            if now - last_log >= 30.0:
                last_log = now
                h, s = snap["health"], snap["signals"]
                LOG.info("published seq=%d obi_nw=%+.3f tfi2=%+.3f btc1s=%+.3f "
                         "hl_age=%.2fs bn_age=%.2fs walls=%d/%d",
                         snap["seq"], s["obi_nw"], s["tfi2"], s["lead_btc_1s"],
                         h["hl_age_s"], h["binance_age_s"],
                         len(snap["walls"]["bid"]), len(snap["walls"]["ask"]))
        except Exception:
            LOG.exception("publish failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


async def _watchdog_loop(engine: MicrostructureEngine, hl: HyperliquidFeed,
                         bn: BinanceFeed, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.sleep(2.0)
            snap = engine.snapshot()
            h = snap["health"]
            if h["hl_age_s"] > HL_STALE_S:
                await asyncio.to_thread(hl.reconnect)
            if h["binance_age_s"] > BINANCE_STALE_S:
                bn.force_reconnect()
            bn.ensure_alive()
        except Exception:
            LOG.exception("watchdog tick failed")


async def run(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).expanduser().resolve()
    setup_logging(Path(args.log_file).expanduser().resolve(), args.verbose)
    LOG.info("starting microstructure daemon coin=%s state=%s interval=%.0fms",
             args.coin, state_file, args.publish_ms)
    engine = MicrostructureEngine(args.coin)
    hl = HyperliquidFeed(engine, args.coin, args.hl_rest_url)
    bn = BinanceFeed(engine, args.binance_url)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await asyncio.to_thread(hl.start)
    bn.start()
    tasks = [
        asyncio.create_task(_publish_loop(engine, state_file, args.publish_ms / 1000.0, stop)),
        asyncio.create_task(_watchdog_loop(engine, hl, bn, stop)),
    ]
    try:
        if args.duration > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.duration)
            except asyncio.TimeoutError:
                pass
        else:
            await stop.wait()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        bn.stop()
        await asyncio.to_thread(hl.stop)
        LOG.info("microstructure daemon stopped")
    return 0


# --------------------------------------------------------------------------- self test
def self_test() -> int:
    engine = MicrostructureEngine("HYPE")
    l2 = {"channel": "l2Book", "data": {"coin": "HYPE", "time": 1, "levels": [
        [{"px": "79.00", "sz": "100", "n": 10}, {"px": "78.99", "sz": "10", "n": 2}],
        [{"px": "79.01", "sz": "10", "n": 1}, {"px": "79.02", "sz": "10", "n": 1}],
    ]}}
    engine.on_hl_message(l2, now=100.0)
    engine.on_hl_message({"channel": "trades", "data": [
        {"side": "B", "sz": 3}, {"side": "A", "sz": 1},
    ]}, now=100.5)
    engine.on_binance_mid(76000.0, now=99.5)
    engine.on_binance_mid(76008.0, now=101.0)
    walls = {"data": {"levels": [
        [{"px": "78.90", "sz": "5000", "n": 30}, {"px": "78.89", "sz": "10", "n": 1},
         {"px": "78.88", "sz": "10", "n": 1}, {"px": "78.87", "sz": "10", "n": 1}],
        [{"px": "79.20", "sz": "4000", "n": 20}, {"px": "79.21", "sz": "10", "n": 1},
         {"px": "79.22", "sz": "10", "n": 1}, {"px": "79.23", "sz": "10", "n": 1}],
    ]}}
    engine.on_wall_book(walls, now=101.0)
    snap = engine.snapshot(now=101.0)
    print(json.dumps(snap, indent=2))
    ok = (snap["signals"]["obi_nw"] > 0
          and snap["signals"]["tfi2"] > 0
          and snap["signals"]["lead_btc_1s"] > 0
          and snap["walls"]["nearest_bid"]["px"] == 78.90
          and snap["walls"]["nearest_ask"]["px"] == 79.20)
    print("SELF-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="HYPE microstructure signal daemon")
    ap.add_argument("--coin", default="HYPE")
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--log-file", default=str(DEFAULT_LOG_FILE))
    ap.add_argument("--publish-ms", type=int, default=100,
                    help="state publication cadence (50-200 ms recommended)")
    ap.add_argument("--hl-rest-url", default="https://api.hyperliquid.xyz")
    ap.add_argument("--binance-url", default=BINANCE_WS_URL)
    ap.add_argument("--duration", type=float, default=0.0, help="exit after N seconds (0 = forever)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
