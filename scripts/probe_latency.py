"""T2 read-only latency probe. NO keys, NO orders, NO state changes on the exchange.

Measures, N samples each (default 30):
  (a) testnet  Info.meta() REST round trip
  (b) mainnet  Info.meta() REST round trip      <- read-only mainnet, allowed for T2 ONLY
  (c) testnet  WS connect + subscribe -> first l2Book message (BTC)

REST uses the SDK's `Info(base_url, skip_ws=True)` (explicitly sanctioned for this script).
WS uses `websocket.create_connection` (websocket-client, the SDK's own dependency) so the
measurement is exactly: TCP+TLS connect -> subscribe sent -> first book received.

Prints min / median / p95 (plus mean / max / n_ok / n_fail) in milliseconds.
Usage:  .venv/bin/python scripts/probe_latency.py [--n 30] [--timeout 10]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from typing import List, Tuple

TESTNET = "https://api.hyperliquid-testnet.xyz"
MAINNET = "https://api.hyperliquid.xyz"


def _pct(xs: List[float], p: float) -> float:
    """Nearest-rank percentile (no interpolation; n is small)."""
    s = sorted(xs)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def _report(name: str, xs: List[float], fails: int) -> None:
    if not xs:
        print(f"{name:<34} n=0  (ALL FAILED, {fails} failures)")
        return
    print(f"{name:<34} n={len(xs):<3} min={min(xs):8.1f}  median={statistics.median(xs):8.1f}  "
          f"p95={_pct(xs, 0.95):8.1f}  mean={statistics.fmean(xs):8.1f}  max={max(xs):8.1f} ms"
          + (f"  ({fails} failed)" if fails else ""))


def probe_rest_meta(base_url: str, n: int, timeout: float) -> Tuple[List[float], int]:
    from hyperliquid.info import Info                     # SDK, skip_ws=True: no websocket thread
    info = Info(base_url, skip_ws=True, timeout=timeout)  # construction is NOT timed
    info.meta()                                            # warm-up: first call pays connect/TLS
    samples: List[float] = []
    fails = 0
    for _ in range(n):
        t0 = time.monotonic()
        try:
            info.meta()
        except Exception as e:                             # noqa: BLE001 - probe must not crash
            fails += 1
            print(f"    rest failure: {e!r}")
            continue
        samples.append((time.monotonic() - t0) * 1e3)
        time.sleep(0.1)
    return samples, fails


def probe_ws_first_book(base_url: str, n: int, timeout: float) -> Tuple[List[float], int]:
    import websocket                                       # websocket-client 1.9.2 (SDK dependency)

    ws_url = "ws" + base_url[len("http"):] + "/ws"
    samples: List[float] = []
    fails = 0
    for i in range(n):
        t0 = time.monotonic()
        ws = None
        try:
            ws = websocket.create_connection(ws_url, timeout=timeout)
            ws.send(json.dumps({"method": "subscribe",
                                "subscription": {"type": "l2Book", "coin": "BTC"}}))
            deadline = time.monotonic() + timeout
            got = False
            while time.monotonic() < deadline:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    break
                if not raw or raw == "Websocket connection established.":
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("channel") == "l2Book":
                    samples.append((time.monotonic() - t0) * 1e3)
                    got = True
                    break
            if not got:
                fails += 1
                print("    ws failure: no l2Book within timeout")
        except Exception as e:                             # noqa: BLE001 - probe must not crash
            fails += 1
            print(f"    ws failure: {e!r}")
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:                          # noqa: BLE001
                    pass
        time.sleep(0.2)
    return samples, fails


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Hyperliquid latency probe (no keys, no orders)")
    ap.add_argument("--n", type=int, default=30, help="samples per probe (default 30)")
    ap.add_argument("--timeout", type=float, default=10.0, help="per-request timeout seconds")
    args = ap.parse_args()

    print(f"probe start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  n={args.n}  "
          f"timeout={args.timeout}s  (read-only: /info + public ws only)")
    total_fails = 0

    print("\n(a) testnet Info.meta() REST RTT")
    a, fa = probe_rest_meta(TESTNET, args.n, args.timeout)
    _report("testnet meta()", a, fa)
    total_fails += fa

    print("\n(b) mainnet Info.meta() REST RTT  [READ-ONLY, T2 only]")
    b, fb = probe_rest_meta(MAINNET, args.n, args.timeout)
    _report("mainnet meta()", b, fb)
    total_fails += fb

    print("\n(c) testnet WS connect -> first l2Book (BTC)")
    c, fc = probe_ws_first_book(TESTNET, args.n, args.timeout)
    _report("testnet ws->l2Book", c, fc)
    total_fails += fc

    ok = len(a) == len(b) == len(c) == args.n
    print(f"\nprobe end {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
          f"complete={ok}  total_failures={total_fails}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
