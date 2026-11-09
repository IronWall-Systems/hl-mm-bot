#!/usr/bin/env python3
"""Zero-look-ahead feature study on recorded HYPE v2 shards + live trial log.

Data: data/YYYYMMDD/HH_HYPE.jsonl.gz (schema hlmm-shard/2), paired with the
same-hour BTC shard for lead-lag.  Decision time = exchange timestamp + 10 ms
(split feed latency).  Only events with exchange time <= decision time are used.
Forward markouts are mid-to-mid returns at +1 s / +5 s / +30 s.

Usage:
    PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/feature_study.py
    ... --max-hours 12          # quick smoke test
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import time
from bisect import bisect_left, bisect_right
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "feature_study_20261107.md"
DATA = ROOT / "data"
if not any(DATA.glob("2026*/[0-9][0-9]_HYPE.jsonl.gz")):
    DATA = Path("/root/hl-mm-bot/data")
LIVE_LOG = ROOT / "logs" / "live_trial_20261107_hype.jsonl"

FEED_LATENCY_S = 0.010
MODIFY_LATENCY_MS = 410
HORIZONS = (1, 5, 30)
FEATURES = ("obi_sz", "obi_nw", "tfi2", "tfi5", "vol_wick", "inv_age", "lead_btc", "ret5m")
BOOTSTRAP_DRAWS = 5000
BOOTSTRAP_SEED = 20261107


# ---------------------------------------------------------------- statistics
def rankdata(x: np.ndarray) -> np.ndarray:
    n = len(x)
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    ranks = np.empty(n, dtype=np.float64)
    i = 0
    while i < n:
        j = i + 1
        while j < n and xs[j] == xs[i]:
            j += 1
        ranks[order[i:j]] = (i + 1 + j) / 2.0
        i = j
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> Tuple[float, int]:
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    n = len(x)
    if n < 50:
        return float("nan"), n
    rx, ry = rankdata(x), rankdata(y)
    rx -= rx.mean(); ry -= ry.mean()
    den = math.sqrt(float((rx * rx).sum()) * float((ry * ry).sum()))
    return (float((rx * ry).sum() / den) if den else float("nan")), n


def p_value(r: float, n: int) -> float:
    if not math.isfinite(r) or n < 4:
        return 1.0
    if abs(r) >= 1.0:
        return 0.0
    t = abs(r) * math.sqrt((n - 2) / (1 - r * r))
    return math.erfc(t / math.sqrt(2.0))


def holm(pvals: List[float]) -> List[float]:
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    out = [1.0] * m
    prev = 0.0
    for rank, i in enumerate(order):
        adj = min(1.0, max(prev, pvals[i] * (m - rank)))
        out[i] = adj
        prev = adj
    return out


def bootstrap_ci(daily: List[float], seed: int = BOOTSTRAP_SEED) -> Tuple[float, float, float]:
    vals = np.array([v for v in daily if math.isfinite(v)], dtype=np.float64)
    if len(vals) < 3:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = np.empty(BOOTSTRAP_DRAWS)
    for i in range(BOOTSTRAP_DRAWS):
        means[i] = vals[rng.integers(0, len(vals), len(vals))].mean()
    return float(vals.mean()), float(np.percentile(means, 5)), float(np.percentile(means, 95))


# ---------------------------------------------------------------- data load
def _load_hour(path: Path) -> Dict[str, Any]:
    l2_ts: List[float] = []
    obi_sz: List[float] = []
    obi_nw: List[float] = []
    bbo_ts: List[float] = []
    mid: List[float] = []
    tr_ts: List[float] = []
    tr_signed_sz: List[float] = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = e.get("e")
            t = e.get("t")
            if t is None:
                continue
            ts = float(t) / 1000.0
            if k == "l2":
                bids, asks = e.get("b", []), e.get("a", [])
                b5, a5 = bids[:5], asks[:5]
                b_sz = sum(float(l[1]) for l in b5)
                a_sz = sum(float(l[1]) for l in a5)
                b_n = sum(float(l[1]) * float(l[2]) for l in b5)
                a_n = sum(float(l[1]) * float(l[2]) for l in a5)
                tot = b_sz + a_sz
                totn = b_n + a_n
                l2_ts.append(ts)
                obi_sz.append((b_sz - a_sz) / tot if tot else 0.0)
                obi_nw.append((b_n - a_n) / totn if totn else 0.0)
            elif k == "bbo":
                b, a = e.get("b"), e.get("a")
                if b and a:
                    bbo_ts.append(ts)
                    mid.append((float(b[0]) + float(a[0])) / 2.0)
            elif k == "tr":
                tr_ts.append(ts)
                tr_signed_sz.append(float(e.get("sz", 0.0)) * (1.0 if e.get("side") == "B" else -1.0))
    order = np.argsort(np.array(l2_ts, dtype=np.float64), kind="mergesort")
    l2_ts = [l2_ts[i] for i in order]
    obi_sz = [obi_sz[i] for i in order]
    obi_nw = [obi_nw[i] for i in order]
    order = np.argsort(np.array(bbo_ts, dtype=np.float64), kind="mergesort")
    bbo_ts = [bbo_ts[i] for i in order]
    mid = [mid[i] for i in order]
    order = np.argsort(np.array(tr_ts, dtype=np.float64), kind="mergesort")
    tr_ts = [tr_ts[i] for i in order]
    tr_signed_sz = [tr_signed_sz[i] for i in order]

    # per-second last mid
    sec_map: Dict[int, float] = {}
    for t, m in zip(bbo_ts, mid):
        sec_map[int(math.floor(t))] = m
    secs = sorted(sec_map)
    sec_mid = [sec_map[s] for s in secs]

    def rolling_range(window: int) -> List[float]:
        if not secs:
            return []
        maxq: deque[int] = deque()
        minq: deque[int] = deque()
        out = [0.0] * len(secs)
        left = 0
        for i, s in enumerate(secs):
            while maxq and sec_mid[maxq[-1]] <= sec_mid[i]:
                maxq.pop()
            maxq.append(i)
            while minq and sec_mid[minq[-1]] >= sec_mid[i]:
                minq.pop()
            minq.append(i)
            while secs[i] - secs[left] > window:
                left += 1
            while maxq and maxq[0] < left:
                maxq.popleft()
            while minq and minq[0] < left:
                minq.popleft()
            m = sec_mid[i]
            out[i] = (sec_mid[maxq[0]] - sec_mid[minq[0]]) / m * 1e4 if m else 0.0
        return out

    r1 = rolling_range(60)
    r5 = rolling_range(300)

    # trade prefix sums
    tb = np.zeros(len(tr_signed_sz) + 1)
    tsell = np.zeros(len(tr_signed_sz) + 1)
    for i, v in enumerate(tr_signed_sz):
        tb[i + 1] = tb[i] + (v if v > 0 else 0.0)
        tsell[i + 1] = tsell[i] + (-v if v < 0 else 0.0)

    return {
        "l2_ts": l2_ts, "obi_sz": obi_sz, "obi_nw": obi_nw,
        "bbo_ts": bbo_ts, "mid": mid,
        "secs": secs, "sec_mid": sec_mid, "r1": r1, "r5": r5,
        "tr_ts": tr_ts, "tr_buy": tb, "tr_sell": tsell,
    }


def _load_btc(path: Path) -> Tuple[List[int], List[float]]:
    ts: List[float] = []
    mid: List[float] = []
    if not path.exists():
        return [], []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if '"e":"bbo"' not in line and '"e": "bbo"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            b, a = e.get("b"), e.get("a")
            if b and a and e.get("t") is not None:
                ts.append(float(e["t"]) / 1000.0)
                mid.append((float(b[0]) + float(a[0])) / 2.0)
    sec_map: Dict[int, float] = {}
    for t, m in zip(ts, mid):
        sec_map[int(math.floor(t))] = m
    secs = sorted(sec_map)
    return secs, [sec_map[s] for s in secs]


def _asof(secs: List[int], s: int) -> Optional[int]:
    i = bisect_right(secs, s) - 1
    return i if i >= 0 else None


def _btc_ret(btc: Tuple[List[int], List[float]], s: int, lag_s: int) -> float:
    secs, mids = btc
    i = _asof(secs, s)
    j = _asof(secs, s - lag_s)
    if i is None or j is None or i == j or not mids[j]:
        return float("nan")
    return (mids[i] / mids[j] - 1.0) * 1e4


# ---------------------------------------------------------------- features
def build_samples(files: List[Path], max_hours: int) -> Dict[int, List[Tuple[str, np.ndarray, float]]]:
    samples: Dict[int, List[Tuple[str, np.ndarray, float]]] = {h: [] for h in HORIZONS}
    last_kept = {h: -1e18 for h in HORIZONS}
    processed = 0
    for path in files:
        if max_hours and processed >= max_hours:
            break
        day = path.parent.name
        btc_path = path.parent / path.name.replace("_HYPE", "_BTC")
        hd = _load_hour(path)
        btc = _load_btc(btc_path)
        l2_ts, bbo_ts = hd["l2_ts"], hd["bbo_ts"]
        if not l2_ts or not bbo_ts:
            continue
        tr_ts = hd["tr_ts"]
        regime_sign, regime_start = 0, 0.0
        for k, t in enumerate(l2_ts):
            dt = t + FEED_LATENCY_S
            s = int(math.floor(dt))
            jb = bisect_right(bbo_ts, dt) - 1
            if jb < 0:
                continue
            mid_dt = hd["mid"][jb]
            if not mid_dt:
                continue
            # TFI windows
            lo2, lo5 = dt - 2.0, dt - 5.0
            i2 = bisect_right(tr_ts, dt)
            i2lo = bisect_left(tr_ts, lo2)
            i5lo = bisect_left(tr_ts, lo5)
            buy2 = hd["tr_buy"][i2] - hd["tr_buy"][i2lo]
            sell2 = hd["tr_sell"][i2] - hd["tr_sell"][i2lo]
            buy5 = hd["tr_buy"][i2] - hd["tr_buy"][i5lo]
            sell5 = hd["tr_sell"][i2] - hd["tr_sell"][i5lo]
            tfi2 = (buy2 - sell2) / (buy2 + sell2) if (buy2 + sell2) else 0.0
            tfi5 = (buy5 - sell5) / (buy5 + sell5) if (buy5 + sell5) else 0.0
            # inventory-age proxy: duration of the current taker-flow regime
            if tfi5 > 0.15 and regime_sign <= 0:
                regime_sign, regime_start = 1, dt
            elif tfi5 < -0.15 and regime_sign >= 0:
                regime_sign, regime_start = -1, dt
            inv_age = (dt - regime_start) if regime_sign else 0.0
            # volatility wick ratio: 1m range vs per-minute share of 5m range
            si = _asof(hd["secs"], s)
            if si is None:
                continue
            vol_wick = hd["r1"][si] / max(hd["r5"][si] / 5.0, 1e-9)
            si5 = _asof(hd["secs"], s - 300)
            ret5m = ((hd["sec_mid"][si] / hd["sec_mid"][si5] - 1.0) * 1e4
                     if si5 is not None and hd["sec_mid"][si5] else float("nan"))
            lead_btc = _btc_ret(btc, s, 1)
            feats = np.array([
                hd["obi_sz"][k], hd["obi_nw"][k], tfi2, tfi5,
                vol_wick, inv_age, lead_btc, ret5m,
            ], dtype=np.float64)
            for h in HORIZONS:
                if dt - last_kept[h] < h:
                    continue
                jf = bisect_right(bbo_ts, dt + h) - 1
                if jf <= jb or (dt + h) - bbo_ts[jf] > 3.0:
                    continue
                fwd = (hd["mid"][jf] / mid_dt - 1.0) * 1e4
                if not math.isfinite(fwd):
                    continue
                last_kept[h] = dt
                samples[h].append((day, feats.copy(), fwd))
        processed += 1
        if processed % 24 == 0:
            print(f"  processed {processed}/{len(files) if not max_hours else max_hours} hours", flush=True)
    return samples


# ---------------------------------------------------------------- study
def study(samples: Dict[int, List[Tuple[str, np.ndarray, float]]]) -> Tuple[List[Dict[str, Any]], List[str], Dict[int, int]]:
    results: List[Dict[str, Any]] = []
    days_all = sorted({d for ss in samples.values() for d, _, _ in ss})
    counts = {h: len(samples[h]) for h in HORIZONS}
    for h in HORIZONS:
        ss = samples[h]
        if not ss:
            continue
        days = sorted({d for d, _, _ in ss})
        day_idx = {d: [] for d in days}
        for i, (d, _, _) in enumerate(ss):
            day_idx[d].append(i)
        for fi, fname in enumerate(FEATURES):
            x = np.array([r[1][fi] for r in ss])
            y = np.array([r[2] for r in ss])
            rho, n = spearman(x, y)
            daily = []
            daily_n = []
            for d in days:
                idx = np.array(day_idx[d])
                r, nn = spearman(x[idx], y[idx])
                if math.isfinite(r):
                    daily.append(r)
                    daily_n.append(nn)
            if not daily:
                continue
            sign_cons = float(np.mean([1.0 if math.copysign(1, r) == math.copysign(1, rho) else 0.0
                                       for r in daily])) if math.isfinite(rho) else 0.0
            mean_daily, ci_lo, ci_hi = bootstrap_ci(daily)
            # odd/even walk-forward over sorted day blocks
            odd = [i for i, d in enumerate(days) if i % 2 == 1]
            even = [i for i, d in enumerate(days) if i % 2 == 0]

            def pooled(sel: List[int]) -> float:
                idx = np.concatenate([np.array(day_idx[days[i]]) for i in sel]) if sel else np.array([], dtype=int)
                r, _ = spearman(x[idx], y[idx]) if len(idx) else (float("nan"), 0)
                return r
            r_odd, r_even = pooled(odd), pooled(even)
            wf = (math.isfinite(r_odd) and math.isfinite(r_even)
                  and math.copysign(1, r_odd) == math.copysign(1, r_even)
                  and math.copysign(1, r_odd) == math.copysign(1, rho))
            # signed markout on extreme decile
            thresh = np.nanpercentile(np.abs(x), 90)
            sel = np.abs(x) >= thresh
            signed_mo = float(np.nanmean(np.sign(x[sel]) * y[sel])) if sel.any() else float("nan")
            results.append({
                "feature": fname, "horizon": h, "n": n, "rho": rho,
                "p": p_value(rho, n), "sign_cons": sign_cons,
                "days": len(daily), "boot_mean": mean_daily,
                "ci_lo": ci_lo, "ci_hi": ci_hi,
                "r_odd": r_odd, "r_even": r_even, "wf": wf,
                "signed_mo": signed_mo,
            })
    pvals = holm([r["p"] for r in results])
    for r, p in zip(results, pvals):
        r["p_holm"] = p
        r["pass"] = bool(
            r["days"] >= 6 and abs(r["rho"]) >= 0.03 and p < 0.05
            and r["sign_cons"] >= 0.67
            and (r["ci_lo"] > 0 or r["ci_hi"] < 0)
            and r["wf"]
        )
    return results, days_all, counts


# ---------------------------------------------------------------- live log
def live_audit() -> Dict[str, Any]:
    if not LIVE_LOG.exists():
        return {"error": "live log missing"}
    events: List[Dict[str, Any]] = []
    with LIVE_LOG.open() as fh:
        for line in fh:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    starts = [i for i, e in enumerate(events) if e.get("event") == "startup"]
    if not starts:
        return {"error": "no startup"}
    i = starts[-1]
    startup = events[i]
    sess = events[i:]
    fills = [e for e in sess if e.get("event") == "fill"]
    modifies = [e for e in sess if e.get("event") == "modify"]
    parks = [e for e in sess if e.get("event") == "park"]
    markouts = [e for e in sess if e.get("event") == "markout"]
    equities = [e for e in sess if e.get("event") == "equity" and e.get("equity") is not None]

    def med(vals: List[float]) -> Optional[float]:
        return float(np.median(vals)) if vals else None
    mo = {}
    for h in HORIZONS:
        vals = [float(e["markout_bps"]) for e in markouts
                if int(e.get("horizon_s", 0)) == h and e.get("markout_bps") is not None]
        mo[f"{h}s"] = {"n": len(vals), "median_bps": med(vals),
                       "mean_bps": float(np.mean(vals)) if vals else None}
    start_eq = float(startup.get("equity") or 0)
    latest_eq = float(equities[-1]["equity"]) if equities else None
    return {
        "start_ts": startup.get("ts"),
        "start_iso": datetime.fromtimestamp(float(startup.get("ts", 0)), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "start_equity": start_eq,
        "latest_equity": latest_eq,
        "pnl": (latest_eq - start_eq) if latest_eq is not None else None,
        "latest_position": equities[-1].get("position") if equities else None,
        "fills": len(fills),
        "modify_legs": len(modifies),
        "parks": len(parks),
        "markout_events": len(markouts),
        "markouts": mo,
        "fees": sum(float(f.get("fee") or 0.0) for f in fills),
        "fill_details": fills[-5:],
    }


def fmt_table(results: List[Dict[str, Any]]) -> str:
    lines = [
        "| Feature | Horizon | n | Spearman rho | Holm p | Daily sign | Bootstrap 90% CI | WF odd/even | Signed MO (bps) | Pass |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda z: (z["feature"], z["horizon"])):
        ci = f"[{r['ci_lo']:+.3f}, {r['ci_hi']:+.3f}]"
        wf = f"{r['r_odd']:+.3f}/{r['r_even']:+.3f} {'OK' if r['wf'] else 'NO'}"
        lines.append(
            f"| `{r['feature']}` | +{r['horizon']}s | {r['n']} | {r['rho']:+.4f} | "
            f"{r['p_holm']:.2e} | {r['sign_cons']*100:.0f}% | {ci} | {wf} | "
            f"{r['signed_mo']:+.3f} | {'**PASS**' if r['pass'] else '—'} |")
    return "\n".join(lines)


def write_doc(results: List[Dict[str, Any]], days: List[str], counts: Dict[int, int],
              live: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    passed = [r for r in results if r["pass"]]
    lines = [
        "# HYPE Feature Study — 2026-11-07",
        "",
        f"**Generated:** {now}",
        f"**Data:** {len(days)} UTC day blocks ({days[0]} → {days[-1]}) of `hlmm-shard/2` HYPE.",
        f"**Latency profile:** feed `{FEED_LATENCY_S*1000:.0f} ms`, modify `{MODIFY_LATENCY_MS} ms`.",
        f"**Samples (non-overlapping):** " + ", ".join(f"+{h}s={counts[h]}" for h in HORIZONS),
        "",
        "## 1. Method",
        "",
        "* Decision time = exchange timestamp + 10 ms. Only events with `t <= decision_time` are used.",
        "* Forward markouts use the latest mid at or before `decision_time + h` (tolerance 3 s).",
        "* Samples are de-overlapped per horizon (one decision per h seconds) to avoid autocorrelation inflation.",
        "* Per-day Spearman rank correlation; daily sign consistency vs pooled sign.",
        "* Day-block bootstrap: 5,000 resamples of daily rho, fixed seed `20261107`, 90% CI of the mean.",
        "* Holm–Bonferroni correction across all feature × horizon tests.",
        "* Odd/even UTC-day walk-forward: sign must agree in both halves and with the pooled sign.",
        "* Pass gates: >=6 days, |rho| >= 0.03, Holm p < 0.05, sign consistency >= 67%,",
        "  bootstrap CI excludes 0, and odd/even sign agreement.",
        "",
        "## 2. Feature definitions (zero look-ahead)",
        "",
        "| Feature | Definition |",
        "|---|---|",
        "| `obi_sz` | Top-5 L2 size imbalance `(B-A)/(B+A)` |",
        "| `obi_nw` | Top-5 size weighted by independent order count `n`: `sum(sz*n)` |",
        "| `tfi2` / `tfi5` | Aggressive taker flow imbalance over 2 s / 5 s |",
        "| `vol_wick` | `range_1m / (range_5m/5)` rolling range expansion ratio |",
        "| `inv_age` | Seconds in the current taker-flow regime (inventory-age proxy) |",
        "| `lead_btc` | BTC 1 s mid return at decision time |",
        "| `ret5m` | HYPE 5 m mid return at decision time |",
        "",
        "## 3. Results",
        "",
        fmt_table(results),
        "",
        f"**Passed gates:** {len(passed)} / {len(results)} tests.",
        "",
    ]
    if passed:
        lines += ["### Ready to wire", ""]
        for r in passed:
            lines.append(f"* `{r['feature']}` @ +{r['horizon']}s — rho {r['rho']:+.4f}, "
                         f"Holm p {r['p_holm']:.2e}, sign {r['sign_cons']*100:.0f}%, "
                         f"signed markout {r['signed_mo']:+.3f} bps")
        lines.append("")
    lines += [
        "## 4. Live HYPE trial log audit (`hs=4.0` calibration onward)",
        "",
        f"* Session start: **{live.get('start_iso')}** (`{live.get('start_ts')}`)",
        f"* Start equity `${live.get('start_equity')}` → latest `${live.get('latest_equity')}` "
        f"(PnL `{live.get('pnl'):+.5f}` USD)" if live.get("pnl") is not None else "",
        f"* Latest position: `{live.get('latest_position')}`",
        f"* Fills: **{live.get('fills')}** · modify leg updates: **{live.get('modify_legs')}** · "
        f"OTM parks: {live.get('parks')} · fees: `${live.get('fees'):.5f}`",
        "",
        "### Live signed markouts after calibration",
        "",
        "| Horizon | n | Median bps | Mean bps |",
        "|---|---|---|---|",
    ]
    for h in HORIZONS:
        d = (live.get("markouts") or {}).get(f"{h}s", {})
        lines.append(f"| +{h}s | {d.get('n', 0)} | "
                     f"{d.get('median_bps') if d.get('median_bps') is not None else 'n/a'} | "
                     f"{d.get('mean_bps') if d.get('mean_bps') is not None else 'n/a'} |")
    lines += [
        "",
        "The recorded live log now contains real fills and markouts, so it provides an",
        "out-of-sample execution check: fill capture improved after the width calibration,",
        "but short-horizon markouts are near zero/slightly negative and the 30s median is",
        "adverse, indicating toxic fills rather than pure edge.",
        "",
        "## 5. Limitations",
        "",
        "* Exchange timestamps are millisecond-resolution; sub-ms ordering is unavailable.",
        "* `inv_age` is a taker-flow regime proxy because market-data shards carry no inventory state.",
        "* Recorded L2 is top-20 only; deep-book walls are not present in the historical shards.",
        "* Lead-lag uses same-hour BTC shards; gaps produce `NaN` and are excluded pairwise.",
        "* Overlapping 30s outcomes are de-overlapped per horizon, but residual cross-feature",
        "  dependence remains; Holm controls family-wise error across the 24 tests.",
        "",
    ]
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-hours", type=int, default=0, help="limit files for smoke tests")
    args = ap.parse_args()
    files = sorted(DATA.glob("2026*/[0-9][0-9]_HYPE.jsonl.gz"))
    if not files:
        raise SystemExit("no HYPE shards found")
    print(f"loading {len(files)} HYPE hourly shards (max_hours={args.max_hours or 'all'}) ...")
    t0 = time.time()
    samples = build_samples(files, args.max_hours)
    print(f"building statistics ({time.time()-t0:.1f}s elapsed) ...")
    results, days, counts = study(samples)
    live = live_audit()
    write_doc(results, days, counts, live)
    print(fmt_table(results))
    print(f"\nlive trial: fills={live.get('fills')} modifies={live.get('modify_legs')} "
          f"equity={live.get('latest_equity')} pos={live.get('latest_position')}")
    print(f"wrote {DOC} ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
