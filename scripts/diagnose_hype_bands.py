#!/usr/bin/env python3
"""Empirical HYPE BBO diagnostic for BandParams calibration.

The live-trial JSONL (logs/live_trial_20261107_hype.jsonl) does NOT carry a
1-second BBO series: it has only quote `modify` prices, 90 sparse `markout`
mids, and equity/toxicity events.  The project's collector shards
(data/<YYYYMMDD>/HH_HYPE.jsonl.gz) DO carry every `bbo` message, so this
script uses those (2026-11-04 and 2026-11-05 are the newest HYPE days on disk)
and reproduces the exact same-clock window as the failed 2026-11-07 session.

Usage:
    python3 scripts/diagnose_hype_bands.py [DATA_ROOT]
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import sys
from pathlib import Path

import numpy as np


def _mid_series(day: Path, hours=range(24), t0: int | None = None, t1: int | None = None):
    """Last BBO mid per wall-clock second, forward-filled over gaps <= 5 s."""
    buckets: dict[int, float] = {}
    for h in hours:
        p = day / f"{h:02d}_HYPE.jsonl.gz"
        if not p.exists():
            continue
        with gzip.open(p, "rt") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("e") != "bbo":
                    continue
                t = e.get("t")
                b, a = e.get("b"), e.get("a")
                if t is None or not b or not a:
                    continue
                if t0 is not None and not (t0 <= t <= t1):
                    continue
                buckets[int(t) // 1000] = 0.5 * (float(b[0]) + float(a[0]))
    if not buckets:
        return np.array([])
    start, end = min(buckets), max(buckets)
    out, last, last_s = [], None, None
    for s in range(start, end + 1):
        if s in buckets:
            last, last_s = buckets[s], s
            out.append(last)
        elif last is not None and s - last_s <= 5:
            out.append(last)
        else:
            out.append(np.nan)
    return np.asarray(out, dtype=float)


def _metrics(mid: np.ndarray, label: str) -> dict:
    mid = mid[~np.isnan(mid)]
    if mid.size < 400:
        print(f"== {label} ==  (too short: {mid.size} samples)")
        return {}
    r1 = np.diff(np.log(mid))
    sigma = (np.log(mid[60:]) - np.log(mid[:-60])).std() * 1e4
    out = {"n": int(mid.size), "r1_std_bps": float(r1.std() * 1e4),
           "sigma_bps_sqrt_min": float(sigma)}
    print(f"== {label} ==")
    print(f"  1s samples          : {out['n']}")
    print(f"  r1 std              : {out['r1_std_bps']:.3f} bps")
    print(f"  sigma_bps_sqrt_min  : {out['sigma_bps_sqrt_min']:.3f}")
    for lag in (5, 30, 60, 300):
        rl = np.log(mid[lag:]) - np.log(mid[:-lag])
        vr = float(rl.std() / (r1.std() * math.sqrt(lag))) if r1.std() > 0 else float("nan")
        out[f"vr{lag}"] = vr
        print(f"  VR({lag:>3})             : {vr:.4f}")
    print()
    return out


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
    print(f"data root: {root.resolve()}\n")
    results = {}
    for day_name in ("20261105", "20261104"):
        day = root / day_name
        if not day.exists():
            continue
        results[day_name] = _metrics(_mid_series(day), f"{day_name} HYPE full available day")
    # Same 08:55:32-11:06:38 window as the failed live session, on Nov 5.
    day = root / "20261105"
    if day.exists():
        t0 = int(dt.datetime(2026, 11, 5, 8, 55, 32, tzinfo=dt.timezone.utc).timestamp() * 1000)
        t1 = int(dt.datetime(2026, 11, 5, 11, 6, 38, tzinfo=dt.timezone.utc).timestamp() * 1000)
        results["window"] = _metrics(_mid_series(day, t0=t0, t1=t1),
                                     "2026-11-05 HYPE 08:55:32-11:06:38 (same clock as 11-07)")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
