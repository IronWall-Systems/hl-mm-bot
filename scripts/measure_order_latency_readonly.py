#!/usr/bin/env python3
"""Read-only latency measurement for the PREREG2 v2 amendment.

Authenticated place/cancel ACK latency requires private keys and live order
actions, which are prohibited for this research run.  This script therefore
measures the closest read-only proxies from the same Tokyo VPS:

* mainnet ``/info`` REST round-trip (``Info.meta()``), n samples;
* testnet ``/info`` REST round-trip;
* testnet WS connect -> first l2Book message.

It also records ``chronyc tracking`` so clock-discipline status is auditable.
Output: ``logs/latency_tokyo_ack_proxy_20261106.json`` and ``.log``.

NO keys, NO orders, NO account state changes.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TESTNET = "https://api.hyperliquid-testnet.xyz"
MAINNET = "https://api.hyperliquid.xyz"


def pct(xs, p):
    s = sorted(xs)
    if not s:
        return float("nan")
    return s[max(0, math.ceil(p * len(s)) - 1)]


def summary(xs, fails):
    return {
        "n": len(xs),
        "fails": fails,
        "min": min(xs) if xs else None,
        "p50": pct(xs, 0.50) if xs else None,
        "p95": pct(xs, 0.95) if xs else None,
        "p99": pct(xs, 0.99) if xs else None,
        "max": max(xs) if xs else None,
        "mean": statistics.fmean(xs) if xs else None,
    }


def probe_rest(base_url, n, timeout):
    from hyperliquid.info import Info
    info = Info(base_url, skip_ws=True, timeout=timeout)
    info.meta()
    xs, fails = [], 0
    for _ in range(n):
        t0 = time.monotonic()
        try:
            info.meta()
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  rest failure: {e!r}")
            continue
        xs.append((time.monotonic() - t0) * 1e3)
        time.sleep(0.1)
    return xs, fails


def probe_ws(base_url, n, timeout):
    import websocket
    ws_url = "ws" + base_url[len("http"):] + "/ws"
    xs, fails = [], 0
    for _ in range(n):
        t0 = time.monotonic()
        ws = None
        got = False
        try:
            ws = websocket.create_connection(ws_url, timeout=timeout)
            ws.send(json.dumps({"method": "subscribe",
                                "subscription": {"type": "l2Book", "coin": "BTC"}}))
            deadline = time.monotonic() + timeout
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
                    xs.append((time.monotonic() - t0) * 1e3)
                    got = True
                    break
            if not got:
                fails += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ws failure: {e!r}")
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(0.2)
    return xs, fails


def chrony():
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True,
                             text=True, timeout=10)
        return {"ok": out.returncode == 0, "text": out.stdout.strip(),
                "stderr": out.stderr.strip()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "stderr": repr(e)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--out", default=str(PROJECT_ROOT / "logs" /
                                         "latency_tokyo_ack_proxy_20261106.json"))
    args = ap.parse_args(argv)

    print(f"read-only latency probe {datetime.now(timezone.utc).isoformat()} "
          f"n={args.n}")
    main_xs, main_f = probe_rest(MAINNET, args.n, args.timeout)
    test_xs, test_f = probe_rest(TESTNET, args.n, args.timeout)
    ws_xs, ws_f = probe_ws(TESTNET, args.n, args.timeout)
    ch = chrony()

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": ("Read-only proxies only. Authenticated place/cancel ACK latency "
                 "was NOT measured because private keys and live orders are "
                 "prohibited for this run."),
        "mainnet_rest_meta": summary(main_xs, main_f),
        "testnet_rest_meta": summary(test_xs, test_f),
        "testnet_ws_first_book": summary(ws_xs, ws_f),
        "chronyc_tracking": ch,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
