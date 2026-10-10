"""T7c part 3: read-only l2Book WS observer -> silence (interarrival) statistics.
No keys, no orders. Prints a JSON stats block at the end.
    .venv/bin/python scripts/t7c_feed_observer.py --network testnet|mainnet [--duration 600] [--coin BTC]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

from hyperliquid.info import Info

URLS = {"testnet": "https://api.hyperliquid-testnet.xyz", "mainnet": "https://api.hyperliquid.xyz"}


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", choices=sorted(URLS), required=True)
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--coin", default="BTC")
    args = ap.parse_args()

    info = Info(URLS[args.network], skip_ws=False)
    arrivals: list[float] = []
    first_at = [None]
    t_start = time.monotonic()

    def on_l2(msg) -> None:
        now = time.monotonic()
        arrivals.append(now)
        if first_at[0] is None:
            first_at[0] = now
            print(f"first l2Book after {now - t_start:.2f}s", flush=True)

    info.subscribe({"type": "l2Book", "coin": args.coin}, on_l2)
    end = t_start + args.duration
    while time.monotonic() < end:
        time.sleep(0.5)
    info.disconnect_websocket()

    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    g_sorted = sorted(gaps)
    buckets = {"<1s": 0, "1-3s": 0, "3-5s": 0, "5-10s": 0, "10-30s": 0, ">=30s": 0}
    for g in gaps:
        if g < 1:
            buckets["<1s"] += 1
        elif g < 3:
            buckets["1-3s"] += 1
        elif g < 5:
            buckets["3-5s"] += 1
        elif g < 10:
            buckets["5-10s"] += 1
        elif g < 30:
            buckets["10-30s"] += 1
        else:
            buckets[">=30s"] += 1
    span = (arrivals[-1] - arrivals[0]) if len(arrivals) > 1 else 0.0
    out = {
        "network": args.network, "coin": args.coin, "duration_s": args.duration,
        "connected_first_msg_s": (first_at[0] - t_start) if first_at[0] else None,
        "messages": len(arrivals), "rate_per_min": (len(arrivals) - 1) / (span / 60.0) if span else None,
        "gaps": len(gaps),
        "gap_mean_s": statistics.fmean(gaps) if gaps else None,
        "gap_median_s": statistics.median(gaps) if gaps else None,
        "gap_p90_s": pct(g_sorted, 0.90), "gap_p99_s": pct(g_sorted, 0.99),
        "gap_max_s": max(gaps) if gaps else None,
        "gaps_gt_3s": sum(1 for g in gaps if g > 3.0),
        "gaps_gt_6s": sum(1 for g in gaps if g > 6.0),
        "buckets": buckets,
    }
    print("FEED_OBS " + json.dumps(out, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
