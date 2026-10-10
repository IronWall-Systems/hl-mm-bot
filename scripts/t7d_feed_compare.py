"""T7d read-only feed comparison (raw websocket, no keys, no orders).

Connection A: l2Book default + bbo + allMids.  Connection B: l2Book {"fast": true}.
Records the EXACT subscription JSON sent, every inbound message classified per
(conn, channel), gap/feed-age/level statistics, and any raw server reply that is not a
data push (rejected/ignored feeds show up there or as missing data).

    .venv/bin/python scripts/t7d_feed_compare.py --network testnet|mainnet --duration 600
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time

import websocket

URLS = {"testnet": "wss://api.hyperliquid-testnet.xyz/ws", "mainnet": "wss://api.hyperliquid.xyz/ws"}
DATA_CHANNELS = {"l2Book", "bbo", "allMids"}

SUBS_A = [{"type": "l2Book", "coin": "BTC"},
          {"type": "bbo", "coin": "BTC"},
          {"type": "allMids"}]
SUBS_B = [{"type": "l2Book", "coin": "BTC", "fast": True}]


class Conn(threading.Thread):
    def __init__(self, name: str, url: str, subs: list):
        super().__init__(daemon=True)
        self.name_ = name
        self.url = url
        self.subs = subs
        self.sent: list[str] = []
        self.inbound: list[tuple[float, str]] = []      # (wall_recv, raw)
        self.connected_at: float | None = None
        self.closed = threading.Event()
        self.ws = None
        self._stop_ping = threading.Event()

    def run(self) -> None:
        self.ws = websocket.WebSocketApp(
            self.url, on_open=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close)
        threading.Thread(target=self._ping_loop, daemon=True).start()
        self.ws.run_forever()

    def _ping_loop(self) -> None:                       # mirror SDK: ping every 50 s
        while not self._stop_ping.wait(50):
            if self.ws is not None and self.ws.sock and self.ws.sock.connected:
                try:
                    self.ws.send(json.dumps({"method": "ping"}))
                except Exception:
                    break

    def _on_open(self, ws) -> None:
        self.connected_at = time.time()
        for sub in self.subs:
            msg = json.dumps({"method": "subscribe", "subscription": sub}, separators=(",", ":"))
            self.sent.append(msg)
            ws.send(msg)
            time.sleep(0.05)

    def _on_message(self, _ws, message) -> None:
        self.inbound.append((time.time(), message))

    def _on_error(self, _ws, error) -> None:
        self.inbound.append((time.time(), f"__ERROR__ {error!r}"))

    def _on_close(self, _ws, status, msg) -> None:
        self.inbound.append((time.time(), f"__CLOSE__ {status} {msg}"))
        self.closed.set()

    def stop(self) -> None:
        self._stop_ping.set()
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass


def stats(vals: list) -> dict | None:
    if not vals:
        return None
    v = sorted(vals)
    def pct(p):
        return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]
    return {"n": len(v), "mean": round(statistics.fmean(v), 3), "median": round(statistics.median(v), 3),
            "p90": round(pct(0.90), 3), "p99": round(pct(0.99), 3), "max": round(v[-1], 3),
            "min": round(v[0], 3)}


def analyse(conn: Conn, duration: float) -> dict:
    per: dict[str, dict] = {}
    replies: list[tuple[float, str]] = []
    n_pong = 0
    for wall, raw in conn.inbound:
        if raw in ("Websocket connection established.",):
            replies.append((wall, raw)); continue
        try:
            obj = json.loads(raw)
        except Exception:
            replies.append((wall, raw[:500])); continue
        ch = obj.get("channel")
        if ch == "pong":
            n_pong += 1; continue
        if ch not in DATA_CHANNELS:
            replies.append((wall, raw[:1500])); continue
        key = ch
        b = per.setdefault(key, {"recv": [], "raw_first": raw[:2000],
                                 "ex_time": [], "levels_b": [], "levels_a": [], "mids": []})
        b["recv"].append(wall)
        d = obj.get("data") or {}
        if isinstance(d, dict) and isinstance(d.get("time"), (int, float)):
            b["ex_time"].append((wall, d["time"] / 1000.0))
        if ch == "l2Book" and isinstance(d.get("levels"), list) and len(d["levels"]) == 2:
            b["levels_b"].append(len(d["levels"][0])); b["levels_a"].append(len(d["levels"][1]))
        if ch == "allMids" and isinstance(d.get("mids"), dict):
            b["mids"].append(len(d["mids"]))

    out_channels = {}
    for ch, b in per.items():
        recv = b["recv"]
        gaps = [y - x for x, y in zip(recv, recv[1:])]
        span = (recv[-1] - recv[0]) if len(recv) > 1 else 0.0
        ages = [rw - et for rw, et in b["ex_time"]] if b["ex_time"] else []
        entry = {
            "messages": len(recv),
            "rate_per_min": round((len(recv) - 1) / (span / 60.0), 3) if span > 0 else None,
            "first_msg_after_connect_s": round(recv[0] - conn.connected_at, 3) if conn.connected_at and recv else None,
            "gap_s": stats(gaps),
            "gaps_s_raw": [round(g, 4) for g in gaps],
            "feed_age_s (recv_wall - exchange time)": stats(ages),
            "has_exchange_time_field": bool(b["ex_time"]),
            "levels_per_message": None,
            "first_raw": b["raw_first"],
        }
        if ch == "l2Book":
            entry["levels_per_message"] = {"bids": stats(b["levels_b"]), "asks": stats(b["levels_a"]),
                                           "total_mean": round(statistics.fmean(
                                               [x + y for x, y in zip(b["levels_b"], b["levels_a"])]), 2)
                                           if b["levels_b"] else None}
        elif ch == "allMids":
            entry["levels_per_message"] = {"mids_count": stats(b["mids"])}
        elif ch == "bbo":
            entry["levels_per_message"] = {"note": "bbo = best bid/offer (see first_raw)"}
        out_channels[ch] = entry

    return {
        "url": conn.url, "connected_at": conn.connected_at,
        "subscription_json_sent": conn.sent,
        "n_pongs": n_pong,
        "server_replies_and_errors": [{"t": w, "raw": r} for w, r in replies],
        "channels": out_channels,
        "n_inbound_total": len(conn.inbound),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", choices=sorted(URLS), required=True)
    ap.add_argument("--duration", type=float, default=600.0)
    args = ap.parse_args()

    conn_a = Conn("A_default+bbo+allMids", URLS[args.network], SUBS_A)
    conn_b = Conn("B_l2book_fast", URLS[args.network], SUBS_B)
    conn_a.start(); conn_b.start()
    time.sleep(args.duration)
    conn_a.stop(); conn_b.stop()
    time.sleep(2.0)
    conn_a.join(timeout=5); conn_b.join(timeout=5)

    report = {"network": args.network, "duration_s": args.duration,
              "local_wall_start": conn_a.connected_at,
              "connA": analyse(conn_a, args.duration),
              "connB": analyse(conn_b, args.duration)}
    print(json.dumps(report, indent=1, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
