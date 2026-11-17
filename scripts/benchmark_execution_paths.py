#!/usr/bin/env python3
"""Benchmark Hyperliquid execution paths from this host (Tokyo VPS).

Compares, for real signed actions:

  * persistent HTTP REST (``requests.Session``, keep-alive, pooled)
  * persistent WebSocket ``post`` action (``wss://api.hyperliquid.xyz/ws``)
  * in-place ``batchModify`` vs sequential cancel + place
  * cancel by ``oid`` vs cancel by ``cloid``
  * place -> wait -> cancel (delay sweep)
  * ``Alo`` (post-only) vs ``Gtc`` order type

Every live order is a HYPE perp BUY placed ~50% below mid with ``Alo`` so it
cannot cross or fill; the only exception is the explicit ``Gtc`` comparison,
which is also placed ~50% below mid and therefore cannot fill.  Every order is
cancelled at the end of its cycle and a final ``open_orders`` sweep asserts
that nothing is left behind.

For each action we separate:

  ``local_sign_ms``  EIP-712 construction + signing, no network
  ``*_ack_ms``       wire-to-wire: just before send -> response received

Usage:
    venv/bin/python scripts/benchmark_execution_paths.py
    venv/bin/python scripts/benchmark_execution_paths.py --cycles 20 --sleep 0.25
    venv/bin/python scripts/benchmark_execution_paths.py --groups rest,ws
    venv/bin/python scripts/benchmark_execution_paths.py --dry-run   # no orders
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import eth_account
import requests
import websocket
from dotenv import load_dotenv
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import (
    order_request_to_order_wire,
    order_wires_to_order_action,
)
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid
from hyperliquid.utils.signing import (
    get_timestamp_ms,
    sign_l1_action,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_LINES: List[str] = []
WS_URL = "wss://api.hyperliquid.xyz/ws"


# ---------------------------------------------------------------------------
# logging / stats
# ---------------------------------------------------------------------------
def log(msg: str = "") -> None:
    line = str(msg)
    LOG_LINES.append(line)
    print(line, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def summarize(xs: List[float]) -> Dict[str, Optional[float]]:
    if not xs:
        return {"n": 0, "min": None, "p50": None, "p95": None, "p99": None,
                "max": None, "mean": None}
    return {
        "n": len(xs),
        "min": min(xs),
        "p50": percentile(xs, 50),
        "p95": percentile(xs, 95),
        "p99": percentile(xs, 99),
        "max": max(xs),
        "mean": statistics.fmean(xs),
    }


def dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return repr(obj)


# ---------------------------------------------------------------------------
# price / size formatting (same rules as scripts/measure_latency.py)
# ---------------------------------------------------------------------------
def round_price(px: float, sz_decimals: int, rounding=ROUND_DOWN) -> float:
    if px <= 0:
        raise ValueError(f"non-positive price: {px}")
    max_dec = max(0, 6 - int(sz_decimals))
    d = Decimal(str(px))
    sig_dec = 4 - d.adjusted()
    dp = min(max_dec, max(0, sig_dec))
    quantum = Decimal(1).scaleb(-dp)
    return float(d.quantize(quantum, rounding=rounding))


def round_size(sz: float, sz_decimals: int, px: float,
               min_notional: float) -> float:
    step = Decimal(1).scaleb(-int(sz_decimals))
    d = Decimal(str(sz)).quantize(step, rounding=ROUND_UP)
    s = float(d)
    for _ in range(10):
        if s * px >= min_notional:
            return s
        s = float(Decimal(str(s)) + step)
    return s


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------
class Signer:
    def __init__(self, account: LocalAccount, asset: int, coin: str,
                 is_mainnet: bool = True):
        self.account = account
        self.asset = asset
        self.coin = coin
        self.is_mainnet = is_mainnet
        self._last_nonce = 0

    def _sign(self, action: dict) -> Tuple[dict, float]:
        t0 = time.perf_counter()
        nonce = max(get_timestamp_ms(), self._last_nonce + 1)
        self._last_nonce = nonce
        sig = sign_l1_action(self.account, action, None, nonce, None,
                             self.is_mainnet)
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": sig,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        return payload, (time.perf_counter() - t0) * 1e3

    def order_wire(self, px: float, sz: float, is_buy: bool = True,
                   tif: str = "Alo", cloid: Optional[str] = None) -> dict:
        req = {
            "coin": self.coin,
            "is_buy": is_buy,
            "sz": sz,
            "limit_px": px,
            "order_type": {"limit": {"tif": tif}},
            "reduce_only": False,
        }
        if cloid is not None:
            req["cloid"] = Cloid.from_str(cloid)
        return order_request_to_order_wire(req, self.asset)

    def place(self, px: float, sz: float, tif: str = "Alo",
              cloid: Optional[str] = None, is_buy: bool = True):
        wire = self.order_wire(px, sz, is_buy=is_buy, tif=tif, cloid=cloid)
        action = order_wires_to_order_action([wire], grouping="na")
        return self._sign(action)

    def cancel_oid(self, oid: int):
        action = {"type": "cancel",
                  "cancels": [{"a": self.asset, "o": int(oid)}]}
        return self._sign(action)

    def cancel_cloid(self, cloid: str):
        action = {"type": "cancelByCloid",
                  "cancels": [{"asset": self.asset, "cloid": cloid}]}
        return self._sign(action)

    def batch_modify(self, oid: int, px: float, sz: float, tif: str = "Alo"):
        action = {
            "type": "batchModify",
            "modifies": [{"oid": int(oid),
                          "order": self.order_wire(px, sz, tif=tif)}],
        }
        return self._sign(action)


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------
class RestTransport:
    name = "REST"

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Content-Type": "application/json"})

    def post(self, payload: dict) -> Tuple[dict, float]:
        t0 = time.perf_counter()
        r = self.session.post(f"{self.base_url}/exchange", json=payload,
                              timeout=self.timeout)
        dt = (time.perf_counter() - t0) * 1e3
        r.raise_for_status()
        return r.json(), dt

    def close(self) -> None:
        self.session.close()


class WsTransport:
    name = "WS"

    def __init__(self, url: str, timeout: float, ping_idle_s: float = 20.0):
        self.url = url
        self.timeout = timeout
        self.ping_idle_s = ping_idle_s
        self._id = 0
        self._last_activity = time.monotonic()
        self.ws = None
        self._connect()

    # -- connection management -------------------------------------------
    def _connect(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.ws = websocket.create_connection(self.url, timeout=self.timeout)
        self.ws.settimeout(self.timeout)
        self._last_activity = time.monotonic()

    def _maybe_ping(self) -> None:
        """Hyperliquid closes idle WebSockets; keep the link warm."""
        if time.monotonic() - self._last_activity < self.ping_idle_s:
            return
        try:
            self.ws.send(json.dumps({"method": "ping"}))
            # pong arrives as {"channel":"pong"} and is skipped by recv loops
            self._last_activity = time.monotonic()
        except Exception:  # noqa: BLE001
            self._connect()

    def send_action(self, payload: dict) -> int:
        self._maybe_ping()
        self._id += 1
        req_id = self._id
        req = {"method": "post", "id": req_id,
               "request": {"type": "action", "payload": payload}}
        self.ws.send(json.dumps(req))
        self._last_activity = time.monotonic()
        return req_id

    def _recv_until(self, req_ids, t0: float):
        pending = set(req_ids)
        out = []
        while pending:
            raw = self.ws.recv()
            self._last_activity = time.monotonic()
            if raw in ("", None):
                raise websocket.WebSocketConnectionClosedException(
                    "socket closed by peer")
            msg = json.loads(raw)
            ch = msg.get("channel")
            if ch in ("pong", "subscriptionResponse", "error"):
                if ch == "error":
                    # surface protocol errors instead of hanging
                    raise RuntimeError(f"ws error frame: {dump(msg)}")
                continue
            if ch != "post":
                continue
            data = msg.get("data", {})
            rid = data.get("id")
            if rid in pending:
                dt = (time.perf_counter() - t0) * 1e3
                resp = data.get("response", {})
                out.append((rid, resp.get("payload", resp), dt))
                pending.remove(rid)
        return out

    def post(self, payload: dict) -> Tuple[dict, float]:
        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                t0 = time.perf_counter()
                req_id = self.send_action(payload)
                out = self._recv_until([req_id], t0)
                return out[0][1], (time.perf_counter() - t0) * 1e3
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._connect()
                time.sleep(0.1)
        raise last_err  # type: ignore[misc]

    def post_batch(self, payloads: List[dict]) -> List[Tuple[int, dict, float]]:
        """Send several actions back-to-back, then collect all ACKs.

        Returns [(req_id, payload, ack_ms_since_first_send), ...].
        """
        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                t0 = time.perf_counter()
                ids = [self.send_action(p) for p in payloads]
                out = self._recv_until(ids, t0)
                return out
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._connect()
                time.sleep(0.1)
        raise last_err  # type: ignore[misc]

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# response parsing
# ---------------------------------------------------------------------------
def action_status(payload: Any) -> Tuple[Optional[int], Optional[str], Any]:
    """Return (oid, error, raw_status) for a place/modify payload."""
    if not isinstance(payload, dict):
        return None, repr(payload), payload
    if payload.get("status") != "ok":
        return None, dump(payload), payload
    try:
        st = payload["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return None, f"unexpected payload: {dump(payload)[:300]}", payload
    if not isinstance(st, dict):
        return None, f"unexpected status: {dump(st)[:200]}", st
    if st.get("error"):
        return None, str(st["error"]), st
    resting = st.get("resting")
    if resting and resting.get("oid") is not None:
        return int(resting["oid"]), None, st
    # modify may return {"status":"ok"} without resting
    if st.get("status") == "ok":
        return None, None, st
    return None, f"no resting oid: {dump(st)[:200]}", st


def cancel_error(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return repr(payload)
    if payload.get("status") != "ok":
        return dump(payload)
    resp = payload.get("response", {})
    if resp.get("type") == "cancel":
        data = resp.get("data", {})
        statuses = data.get("statuses")
        if statuses:
            bad = [s for s in statuses if isinstance(s, dict) and s.get("error")]
            if bad:
                return dump(bad)
        return None
    # generic error encoded under response
    if isinstance(resp, dict) and resp.get("type") == "error":
        return dump(resp)
    return None


# ---------------------------------------------------------------------------
# measurement groups
# ---------------------------------------------------------------------------
class Bench:
    def __init__(self, signer: Signer, info: Info, account_address: str,
                 args: argparse.Namespace):
        self.signer = signer
        self.info = info
        self.account_address = account_address
        self.args = args
        self.records: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, Any]] = []
        self.open_oids: List[int] = []

    # -- book -------------------------------------------------------------
    def book(self) -> Tuple[float, float, float]:
        l2 = self.info.l2_snapshot(self.args.coin)
        bid = float(l2["levels"][0][0]["px"])
        ask = float(l2["levels"][1][0]["px"])
        return bid, ask, (bid + ask) / 2.0

    def order_params(self, mid: float, tif: str = "Alo") -> Tuple[float, float, float]:
        px = round_price(mid * (1.0 - self.args.offset_pct),
                         self.args.sz_decimals)
        sz = round_size(self.args.notional / px, self.args.sz_decimals, px,
                        self.args.min_notional)
        return px, sz, sz * px

    def new_cloid(self) -> str:
        return "0x" + uuid.uuid4().hex

    # -- one cycle --------------------------------------------------------
    def cycle(self, transport, action: str, tif: str = "Alo",
              cancel_by: str = "oid", delay_s: float = 0.0,
              mid: Optional[float] = None) -> Optional[Dict[str, Any]]:
        bid, ask, mid = (self.book() if mid is None else (None, None, mid))
        px, sz, notional = self.order_params(mid, tif=tif)
        if bid is not None and px >= bid:
            raise RuntimeError(f"BUY {px} would cross best bid {bid}")
        cloid = self.new_cloid()

        rec: Dict[str, Any] = {
            "transport": transport.name, "action": action, "tif": tif,
            "cancel_by": cancel_by, "delay_s": delay_s,
            "mid": mid, "px": px, "sz": sz, "notional": notional,
            "cloid": cloid,
        }

        # ---- place ----
        payload, sign_ms = self.signer.place(px, sz, tif=tif, cloid=cloid)
        rec["place_sign_ms"] = sign_ms
        try:
            resp, ack_ms = transport.post(payload)
        except Exception as e:  # noqa: BLE001
            rec["place_error"] = repr(e)
            self.errors.append({"action": action, "stage": "place",
                                "error": repr(e)})
            self.records.append(rec)
            return rec
        rec["place_ack_ms"] = ack_ms
        rec["place_response"] = resp
        oid, err, _ = action_status(resp)
        if err:
            rec["place_error"] = err
            self.errors.append({"action": action, "stage": "place",
                                "error": err, "response": resp})
            self.records.append(rec)
            return rec
        rec["oid"] = oid
        if oid is not None:
            self.open_oids.append(oid)

        # ---- optional modify ----
        if action in ("rest_modify", "ws_modify"):
            new_px = round_price(mid * (1.0 - self.args.offset_pct) * 0.999,
                                 self.args.sz_decimals)
            new_sz = sz
            mp, msign = self.signer.batch_modify(oid, new_px, new_sz, tif=tif)
            rec["modify_sign_ms"] = msign
            rec["modify_px"] = new_px
            try:
                mresp, mack = transport.post(mp)
                rec["modify_ack_ms"] = mack
                rec["modify_response"] = mresp
                new_oid, merr, _ = action_status(mresp)
                if merr:
                    rec["modify_error"] = merr
                    self.errors.append({"action": action, "stage": "modify",
                                        "error": merr, "response": mresp})
                elif new_oid is not None:
                    # batchModify returns a NEW oid; the old one is gone.
                    rec["new_oid"] = new_oid
                    if oid in self.open_oids:
                        self.open_oids.remove(oid)
                    self.open_oids.append(new_oid)
                    oid = new_oid
            except Exception as e:  # noqa: BLE001
                rec["modify_error"] = repr(e)
                self.errors.append({"action": action, "stage": "modify",
                                    "error": repr(e)})

        # ---- optional delay (place -> wait -> cancel) ----
        if delay_s > 0:
            time.sleep(delay_s)

        # ---- cancel ----
        if cancel_by == "cloid":
            cp, csign = self.signer.cancel_cloid(cloid)
        else:
            cp, csign = self.signer.cancel_oid(oid)
        rec["cancel_sign_ms"] = csign
        try:
            cresp, cack = transport.post(cp)
            rec["cancel_ack_ms"] = cack
            rec["cancel_response"] = cresp
            cerr = cancel_error(cresp)
            if cerr:
                rec["cancel_error"] = cerr
                self.errors.append({"action": action, "stage": "cancel",
                                    "error": cerr, "response": cresp})
            else:
                if oid in self.open_oids:
                    self.open_oids.remove(oid)
        except Exception as e:  # noqa: BLE001
            rec["cancel_error"] = repr(e)
            self.errors.append({"action": action, "stage": "cancel",
                                "error": repr(e)})

        # ---- combined metric ----
        if "place_ack_ms" in rec and "cancel_ack_ms" in rec:
            rec["place_cancel_ms"] = rec["place_ack_ms"] + rec["cancel_ack_ms"]

        self.records.append(rec)
        log(f"  [{transport.name}/{action}/{tif}/{cancel_by}] "
            f"sign={rec.get('place_sign_ms', float('nan')):.1f} "
            f"place={rec.get('place_ack_ms', float('nan')):.1f} "
            f"modify={rec.get('modify_ack_ms', float('nan')):.1f} "
            f"cancel={rec.get('cancel_ack_ms', float('nan')):.1f} ms"
            + (f"  ERR={rec.get('place_error') or rec.get('modify_error') or rec.get('cancel_error')}"
               if any(k in rec for k in ("place_error", "modify_error", "cancel_error"))
               else ""))
        return rec

    # -- pipelined cancel+place (do not wait for the cancel ACK) ----------
    def pipeline_cycle(self, ws: "WsTransport", mid: float) -> Optional[Dict[str, Any]]:
        px, sz, notional = self.order_params(mid)
        cloid = self.new_cloid()
        payload, sign_ms = self.signer.place(px, sz, cloid=cloid)
        rec: Dict[str, Any] = {
            "transport": "WS", "action": "ws_pipeline", "tif": "Alo",
            "cancel_by": "oid", "mid": mid, "px": px, "sz": sz,
            "notional": notional, "cloid": cloid, "place_sign_ms": sign_ms,
        }
        try:
            resp, ack = ws.post(payload)
        except Exception as e:  # noqa: BLE001
            rec["place_error"] = repr(e)
            self.errors.append({"action": "ws_pipeline", "stage": "place",
                                "error": repr(e)})
            self.records.append(rec)
            return rec
        rec["place_ack_ms"] = ack
        oid, err, _ = action_status(resp)
        if err or oid is None:
            rec["place_error"] = err or "no oid"
            self.records.append(rec)
            return rec
        rec["oid"] = oid
        self.open_oids.append(oid)

        new_px = round_price(mid * (1.0 - self.args.offset_pct) * 0.999,
                             self.args.sz_decimals)
        new_cloid = self.new_cloid()
        p2, s2 = self.signer.place(new_px, sz, cloid=new_cloid)
        cp, cs = self.signer.cancel_oid(oid)
        rec["pipeline_place_sign_ms"] = s2
        rec["pipeline_cancel_sign_ms"] = cs
        rec["new_px"] = new_px
        rec["new_cloid"] = new_cloid
        try:
            results = ws.post_batch([cp, p2])
        except Exception as e:  # noqa: BLE001
            rec["pipeline_error"] = repr(e)
            self.errors.append({"action": "ws_pipeline", "stage": "batch",
                                "error": repr(e)})
            self.records.append(rec)
            return rec
        # results are (req_id, payload, ack_ms); order is cancel then place
        results.sort(key=lambda x: x[0])
        rec["pipeline_cancel_ack_ms"] = results[0][2]
        rec["pipeline_place_ack_ms"] = results[1][2]
        rec["pipeline_total_ms"] = max(results[0][2], results[1][2])
        rec["pipeline_cancel_response"] = results[0][1]
        rec["pipeline_place_response"] = results[1][1]
        new_oid, perr, _ = action_status(results[1][1])
        if new_oid is not None:
            if oid in self.open_oids:
                self.open_oids.remove(oid)
            self.open_oids.append(new_oid)
            rec["new_oid"] = new_oid
        if perr:
            rec["pipeline_place_error"] = perr
            self.errors.append({"action": "ws_pipeline", "stage": "place2",
                                "error": perr, "response": results[1][1]})
        # clean up the replacement order
        try:
            cpay, _ = self.signer.cancel_oid(new_oid)
            cresp, cack = ws.post(cpay)
            rec["cleanup_cancel_ack_ms"] = cack
            if new_oid in self.open_oids:
                self.open_oids.remove(new_oid)
        except Exception as e:  # noqa: BLE001
            self.errors.append({"action": "ws_pipeline", "stage": "cleanup",
                                "error": repr(e)})
        self.records.append(rec)
        log(f"  [WS/ws_pipeline/Alo/oid] sign={sign_ms:.1f} "
            f"place={ack:.1f} cancel+place(send together)="
            f"{rec['pipeline_total_ms']:.1f} ms "
            f"(cancel ack {rec['pipeline_cancel_ack_ms']:.1f}, "
            f"place ack {rec['pipeline_place_ack_ms']:.1f})")
        return rec

    # -- cleanup ----------------------------------------------------------
    def cleanup(self) -> None:
        try:
            open_now = self.info.open_orders(self.account_address)
        except Exception as e:  # noqa: BLE001
            log(f"[cleanup] open_orders failed: {e!r}")
            return
        if not open_now:
            log("[cleanup] open_orders=[]")
            return
        log(f"[cleanup] cancelling {len(open_now)} leftover order(s): "
            f"{dump(open_now)[:400]}")
        rest = RestTransport(self.args.base_url, self.args.timeout) \
            if hasattr(self.args, "base_url") else \
            RestTransport(constants.MAINNET_API_URL, self.args.timeout)
        try:
            for o in open_now:
                try:
                    payload, _ = self.signer.cancel_oid(int(o["oid"]))
                    resp, _ = rest.post(payload)
                    log(f"[cleanup] oid={o['oid']} -> {dump(resp)[:200]}")
                except Exception as e:  # noqa: BLE001
                    log(f"[cleanup] oid={o['oid']} exception: {e!r}")
        finally:
            rest.close()
        try:
            after = self.info.open_orders(self.account_address)
            log(f"[cleanup] open_orders after sweep={dump(after)[:300]}")
        except Exception as e:  # noqa: BLE001
            log(f"[cleanup] post-check failed: {e!r}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coin", default="HYPE")
    p.add_argument("--cycles", type=int, default=20)
    p.add_argument("--sleep", type=float, default=0.25)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--notional", type=float, default=11.5)
    p.add_argument("--offset-pct", type=float, default=0.50)
    p.add_argument("--min-notional", type=float, default=10.0)
    p.add_argument("--groups", default="all",
                   help="comma list: rest,ws,modify,cloid,delay,tif (default all)")
    p.add_argument("--delay-cycles", type=int, default=8,
                   help="cycles per delay value in the delay sweep")
    p.add_argument("--dry-run", action="store_true",
                   help="preflight only, place nothing")
    p.add_argument("--out", default=str(PROJECT_ROOT / "logs" /
                                        "execution_paths_benchmark.json"))
    return p


def preflight(info: Info, account_address: str, signer: str,
              min_notional: float) -> Dict[str, Any]:
    pf: Dict[str, Any] = {}
    us = info.user_state(account_address)
    pf["perp_account_value_usd"] = float(
        us.get("marginSummary", {}).get("accountValue", 0.0))
    pf["withdrawable_usd"] = float(us.get("withdrawable", 0.0))
    log(f"[preflight] perp accountValue=${pf['perp_account_value_usd']:.4f} "
        f"withdrawable=${pf['withdrawable_usd']:.4f}")
    pf["account_role"] = info.user_role(account_address)
    log(f"[preflight] account_role={dump(pf['account_role'])}")
    try:
        agents = info.extra_agents(account_address)
    except Exception as e:  # noqa: BLE001
        agents = []
        pf["agents_error"] = repr(e)
    pf["approved_agents"] = agents
    approved = [a for a in agents
                if str(a.get("address", "")).lower() == signer.lower()]
    pf["signer_address"] = signer
    pf["signer_is_approved_agent"] = bool(approved)
    log(f"[preflight] signer={signer} approved_agent={bool(approved)}")
    pf["open_orders"] = info.open_orders(account_address)
    log(f"[preflight] open_orders={dump(pf['open_orders'])[:300]}")
    if pf["perp_account_value_usd"] < min_notional:
        log("[preflight] WARNING: perp equity below minimum notional")
    if not approved:
        log("[preflight] WARNING: signer is not an approved agent")
    if pf["open_orders"]:
        log("[preflight] WARNING: existing open orders will not be touched")
    return pf


def group_metrics(records: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    sel = [r for r in records if r.get("label") == label]
    return {
        "n": len(sel),
        "place_sign_ms": summarize([r["place_sign_ms"] for r in sel
                                    if "place_sign_ms" in r]),
        "place_ack_ms": summarize([r["place_ack_ms"] for r in sel
                                   if "place_ack_ms" in r and "place_error" not in r]),
        "modify_sign_ms": summarize([r["modify_sign_ms"] for r in sel
                                     if "modify_sign_ms" in r]),
        "modify_ack_ms": summarize([r["modify_ack_ms"] for r in sel
                                    if "modify_ack_ms" in r and "modify_error" not in r]),
        "cancel_sign_ms": summarize([r["cancel_sign_ms"] for r in sel
                                     if "cancel_sign_ms" in r]),
        "cancel_ack_ms": summarize([r["cancel_ack_ms"] for r in sel
                                    if "cancel_ack_ms" in r and "cancel_error" not in r]),
        "place_cancel_ms": summarize([r["place_cancel_ms"] for r in sel
                                      if "place_cancel_ms" in r and "cancel_error" not in r]),
        "pipeline_total_ms": summarize([r["pipeline_total_ms"] for r in sel
                                        if "pipeline_total_ms" in r]),
        "errors": sum(1 for r in sel if any(k in r for k in
                                            ("place_error", "modify_error",
                                             "cancel_error", "pipeline_error",
                                             "pipeline_place_error"))),
    }


def print_table(metrics: Dict[str, Dict[str, Any]]) -> None:
    log("")
    log("=" * 110)
    log(f"EXECUTION PATH COMPARISON  {utc_now()}")
    log("=" * 110)
    hdr = (f"{'path':<30}{'n':>4}{'sign':>8}{'place':>9}"
           f"{'modify':>9}{'cancel':>10}{'p+c':>10}{'pipe':>9}"
           f"{'p95p':>9}{'p95c':>9}{'err':>5}")
    log(hdr)
    log("-" * 118)
    for label, m in metrics.items():
        def f(key: str) -> str:
            s = m.get(key) or {}
            v = s.get("p50")
            return "—" if v is None else f"{v:.1f}"
        def p95(key: str) -> str:
            s = m.get(key) or {}
            v = s.get("p95")
            return "—" if v is None else f"{v:.1f}"
        log(f"{label:<30}{m['n']:>4}{f('place_sign_ms'):>8}"
            f"{f('place_ack_ms'):>9}{f('modify_ack_ms'):>9}"
            f"{f('cancel_ack_ms'):>10}{f('place_cancel_ms'):>10}"
            f"{f('pipeline_total_ms'):>9}"
            f"{p95('place_ack_ms'):>9}{p95('cancel_ack_ms'):>9}"
            f"{m['errors']:>5}")
    log("=" * 118)
    log("values are p50 ms unless marked p95; sign = local EIP-712 only")
    log("p+c = sequential place ACK + cancel ACK; pipe = cancel+place sent "
        "back-to-back, time to last ACK")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    log("=" * 72)
    log(f"Hyperliquid execution path benchmark  started {utc_now()}")
    log(f"coin={args.coin} cycles={args.cycles} sleep={args.sleep:.2f}s "
        f"timeout={args.timeout:.1f}s groups={args.groups}")
    log("=" * 72)

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
        log(f"[env] loaded {env_path}")
    secret = os.environ.get("HL_API_SECRET")
    account_address = os.environ.get("HL_ACCOUNT_ADDRESS")
    if not secret or not account_address:
        log("[env] ERROR missing HL_API_SECRET / HL_ACCOUNT_ADDRESS")
        return 2
    account: LocalAccount = eth_account.Account.from_key(secret)
    log(f"[env] account_address={account_address} signer={account.address}")

    base_url = constants.MAINNET_API_URL
    args.base_url = base_url
    info = Info(base_url, skip_ws=True, timeout=args.timeout)
    meta = info.meta()
    coin_info = next(c for c in meta["universe"] if c["name"] == args.coin)
    sz_decimals = int(coin_info["szDecimals"])
    asset = info.name_to_asset(args.coin)
    args.sz_decimals = sz_decimals
    log(f"[meta] {args.coin}: asset={asset} szDecimals={sz_decimals}")

    pf = preflight(info, account_address, account.address, args.min_notional)
    if not pf.get("signer_is_approved_agent"):
        log("[fatal] signer is not an approved agent; aborting")
        return 2
    if pf["perp_account_value_usd"] < args.min_notional:
        log("[fatal] perp equity below minimum notional; aborting")
        return 2

    if args.dry_run:
        log("[dry-run] metadata + preflight OK; no orders placed")
        return 0

    signer = Signer(account, asset, args.coin)
    bench = Bench(signer, info, account_address, args)
    groups = ({"rest", "ws", "modify", "cloid", "delay", "tif", "pipeline"}
              if args.groups == "all" else
              {g.strip() for g in args.groups.split(",") if g.strip()})
    log(f"[groups] {sorted(groups)}")

    rest = RestTransport(base_url, args.timeout)
    ws = WsTransport(WS_URL, args.timeout)
    try:
        # fixed mid for a whole group; deep OTM so movement is irrelevant
        bid, ask, mid = bench.book()
        log(f"[book] bid={bid} ask={ask} mid={mid:.6f}")

        def run(label: str, transport, action: str, **kw) -> None:
            log("")
            log(f"--- {label}: {args.cycles} cycles ---")
            for i in range(args.cycles):
                bench.cycle(transport, action, mid=mid, **kw)
                if i + 1 < args.cycles:
                    time.sleep(max(0.0, args.sleep))

        def tag(label: str) -> None:
            # stamp the records produced by the previous run()
            for r in bench.records:
                if "label" not in r:
                    r["label"] = label

        if "rest" in groups:
            run("rest_place", rest, "rest_place")
            tag("rest_place")
        if "ws" in groups:
            run("ws_place", ws, "ws_place")
            tag("ws_place")
        if "modify" in groups:
            run("rest_modify", rest, "rest_modify")
            tag("rest_modify")
            run("ws_modify", ws, "ws_modify")
            tag("ws_modify")
        if "cloid" in groups:
            run("ws_cancel_cloid", ws, "ws_cancel_cloid", cancel_by="cloid")
            tag("ws_cancel_cloid")
            run("rest_cancel_cloid", rest, "rest_cancel_cloid",
                cancel_by="cloid")
            tag("rest_cancel_cloid")
        if "delay" in groups:
            for d in (0.0, 0.05, 0.10, 0.25, 0.50):
                label = f"ws_cancel_delay_{int(d * 1000)}ms"
                log("")
                log(f"--- {label}: {args.delay_cycles} cycles ---")
                for i in range(args.delay_cycles):
                    bench.cycle(ws, "ws_delay", delay_s=d, mid=mid)
                    if i + 1 < args.delay_cycles:
                        time.sleep(max(0.0, args.sleep))
                tag(label)
        if "tif" in groups:
            run("rest_place_gtc", rest, "rest_place_gtc", tif="Gtc")
            tag("rest_place_gtc")
            run("ws_place_gtc", ws, "ws_place_gtc", tif="Gtc")
            tag("ws_place_gtc")
        if "pipeline" in groups:
            log("")
            log(f"--- ws_pipeline: {args.cycles} cycles ---")
            for i in range(args.cycles):
                bench.pipeline_cycle(ws, mid)
                if i + 1 < args.cycles:
                    time.sleep(max(0.0, args.sleep))
            tag("ws_pipeline")

    finally:
        bench.cleanup()
        rest.close()
        ws.close()

    # ---- local signing microbenchmark -----------------------------------
    log("")
    log("--- local signing microbenchmark (200 iterations) ---")
    bid, ask, mid = bench.book()
    px, sz, _ = bench.order_params(mid)
    sign_times: List[float] = []
    for _ in range(200):
        _, s = signer.place(px, sz, tif="Alo", cloid=bench.new_cloid())
        sign_times.append(s)
    sign_sum = summarize(sign_times)
    log(f"[sign] place local_sign_ms p50={sign_sum['p50']:.3f} "
        f"p95={sign_sum['p95']:.3f} mean={sign_sum['mean']:.3f}")

    # ---- metrics ---------------------------------------------------------
    labels: List[str] = []
    for r in bench.records:
        lab = r.get("label")
        if lab and lab not in labels:
            labels.append(lab)
    metrics = {lab: group_metrics(bench.records, lab) for lab in labels}
    print_table(metrics)

    final_orders = info.open_orders(account_address)
    log(f"[final] open_orders={dump(final_orders)[:500]}")
    us = info.user_state(account_address)
    log(f"[final] perp accountValue="
        f"{us.get('marginSummary', {}).get('accountValue')}")

    out = {
        "started": utc_now(),
        "args": vars(args),
        "preflight": pf,
        "sign_microbenchmark_ms": sign_sum,
        "metrics": metrics,
        "records": bench.records,
        "errors": bench.errors,
        "final_open_orders": final_orders,
        "final_perp_account_value": us.get("marginSummary", {}).get("accountValue"),
        "transcript": LOG_LINES,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    log(f"[out] wrote {args.out}  records={len(bench.records)} "
        f"errors={len(bench.errors)}")
    return 0 if not final_orders else 1


if __name__ == "__main__":
    raise SystemExit(main())
