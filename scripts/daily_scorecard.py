#!/usr/bin/env python3
"""scripts/daily_scorecard.py — read-only daily net-PnL scorecard + PREREG statistics.

Two data sources, in order of preference:

1. `data/t16_eval.csv` + `data/t16_blocks.csv` produced by `scripts/t16_evaluate.py`
   (the PREREG offline replay).  This yields per-day and per-6h-block net PnL after
   maker fees AND funding, and therefore the full `.agent/PREREG.md` verdict.
2. `data/paper_live_ledger.csv` (legacy live shadow).  Rows are *cumulative* snapshots,
   so day PnL is derived as the day-over-day delta.  Monitoring only, NOT the PREREG test.

Nothing here trades, fetches, or mutates state.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
EVAL_CSV = DATA_DIR / "t16_eval.csv"
BLOCKS_CSV = DATA_DIR / "t16_blocks.csv"
LEDGER_CSV = DATA_DIR / "paper_live_ledger.csv"

PRIMARY_LATENCY = 500.0
PRIMARY_CANCEL_RATE = 0.5
CELLS = [(500.0, 0.0), (500.0, 0.5), (1000.0, 0.0), (1000.0, 0.5)]
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20261019
ALPHA = 0.10  # 90% CI
MIN_DAYS = 14
MIN_GREEN_DAYS = 9
TAIL_MULT = 3.0


# --------------------------------------------------------------------------- io
def load_rows(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _float(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return default


def _same_cell(row: dict, latency: float, cancel_rate: float) -> bool:
    return (abs(_float(row, "latency_ms", -1) - latency) < 1e-9
            and abs(_float(row, "cancel_rate", -1) - cancel_rate) < 1e-9)


def candidate_rows(rows: list[dict], coin: str, hs: int) -> list[dict]:
    return [r for r in rows if r.get("coin") == coin and _float(r, "hs", -1) == float(hs)]


def cell_rows(rows: list[dict], latency: float, cancel_rate: float) -> list[dict]:
    return [r for r in rows if _same_cell(r, latency, cancel_rate)]


def daily_net(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        d = r.get("day", "")
        if d:
            out[d] = out.get(d, 0.0) + _float(r, "net_pnl_usd")
    return dict(sorted(out.items()))


# --------------------------------------------------------------------- statistics
def summarize(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "t": 0.0}
    mean = sum(values) / n
    if n > 1:
        sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1))
        if sd > 0:
            t = mean / (sd / math.sqrt(n))
        else:
            t = math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
    else:
        sd = 0.0
        t = 0.0
    return {"n": n, "mean": mean, "sd": sd, "t": t}


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))
    return sorted_vals[i]


def bootstrap_mean_ci(
    values: list[float],
    n_boot: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = ALPHA,
) -> tuple[float, float]:
    """90% CI for the MEAN daily net PnL (resample days with replacement)."""
    n = len(values)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return (_pct(means, alpha / 2), _pct(means, 1 - alpha / 2))


def block_bootstrap_ci(
    blocks: list[float],
    n_boot: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = ALPHA,
) -> tuple[float, float]:
    """90% CI for the TOTAL net PnL (resample contiguous 6h blocks with replacement)."""
    n = len(blocks)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    totals = []
    for _ in range(n_boot):
        totals.append(sum(blocks[rng.randrange(n)] for _ in range(n)))
    totals.sort()
    return (_pct(totals, alpha / 2), _pct(totals, 1 - alpha / 2))


def top2_stats(values: list[float]) -> dict:
    n = len(values)
    total = sum(values)
    ordered = sorted(values, reverse=True)
    top2 = sum(ordered[:2]) if n else 0.0
    share = (top2 / total) if total != 0 else float("nan")
    mean_excl = ((total - top2) / (n - 2)) if n > 2 else float("nan")
    return {"total": total, "top2": top2, "share": share, "mean_excl": mean_excl}


# ----------------------------------------------------------------------- reporting
def _fmt_money(x: float) -> str:
    return f"${x:+.4f}"


def report_eval(rows: list[dict], blocks: list[dict], coin: str, hs: int,
                require_days: int) -> int:
    cand = candidate_rows(rows, coin, hs)
    tag = f"{coin}:{hs}"
    print(f"=== PREREG SCORECARD — {tag} ===")
    if not cand:
        print(f"no rows for {tag} in the eval CSV; run scripts/t16_evaluate.py first")
        return 1

    days_all = sorted({r["day"] for r in cand})
    primary_rows = cell_rows(cand, PRIMARY_LATENCY, PRIMARY_CANCEL_RATE)
    daily = daily_net(primary_rows)
    days = list(daily)
    vals = [daily[d] for d in days]

    print(f"days present: {len(days_all)}  ({days_all[0]}..{days_all[-1]})"
          if days_all else "days present: 0")

    print("\n-- all four cells (14-day totals, net after fees + funding) --")
    cell_ok = True
    totals = {}
    for latency, cr in CELLS:
        rs = cell_rows(cand, latency, cr)
        net = sum(_float(r, "net_pnl_usd") for r in rs)
        fund = sum(_float(r, "funding_pnl") for r in rs)
        fees = sum(_float(r, "total_fees_usd") for r in rs)
        fills = int(sum(_float(r, "fills") for r in rs))
        totals[(latency, cr)] = net
        flag = "OK" if net > 0 else "FAIL"
        cell_ok = cell_ok and net > 0
        print(f"  {latency:4.0f} ms / cr {cr:.1f}: net {_fmt_money(net):>12} | "
              f"funding {fund:+.4f} | fees {fees:.4f} | fills {fills:5d}  [{flag}]")

    print(f"\n-- daily net PnL at {PRIMARY_LATENCY:.0f} ms / cr {PRIMARY_CANCEL_RATE}"
          f" ({len(days)} days) --")
    for d in days:
        v = daily[d]
        m = "GREEN" if v > 0 else ("RED  " if v < 0 else "FLAT ")
        print(f"  {d}: {m} {_fmt_money(v):>12}")

    st = summarize(vals)
    print("\n-- daily statistics --")
    print(f"  n={st['n']}  mean={_fmt_money(st['mean'])}  sd={st['sd']:.4f}  "
          f"t-stat={st['t']:+.3f}")
    lo, hi = bootstrap_mean_ci(vals)
    print(f"  90% day-bootstrap CI of mean ({BOOTSTRAP_N} draws, seed {BOOTSTRAP_SEED}): "
          f"[{lo:+.4f}, {hi:+.4f}]")

    t2 = top2_stats(vals)
    share = "n/a" if math.isnan(t2["share"]) else f"{t2['share'] * 100:.1f}%"
    excl = "n/a" if math.isnan(t2["mean_excl"]) else f"{t2['mean_excl']:+.4f}"
    print(f"  top-2 days profit={t2['top2']:+.4f}  share of total={share}  "
          f"mean excl. top-2={excl}")

    primary_blocks = []
    seen_blocks: set[tuple[str, int]] = set()
    for b in blocks:
        if (b.get("coin") != coin or _float(b, "hs", -1) != float(hs)
                or not _same_cell(b, PRIMARY_LATENCY, PRIMARY_CANCEL_RATE)):
            continue
        key = (b.get("day", ""), int(_float(b, "block")))
        if key in seen_blocks:  # a resumed run may append a block twice
            continue
        seen_blocks.add(key)
        primary_blocks.append(_float(b, "net_pnl_usd"))
    blo, bhi = block_bootstrap_ci(primary_blocks)
    print(f"\n-- 6h-block bootstrap (binding CI) --")
    print(f"  blocks={len(primary_blocks)}  90% CI of total: [{blo:+.4f}, {bhi:+.4f}]  "
          f"lower>0: {'YES' if blo > 0 else 'NO'}")

    # ------------------------------------------------------------------ verdict
    green = sum(1 for v in vals if v > 0)
    wins = [v for v in vals if v > 0]
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    worst = min(vals) if vals else 0.0
    tail_ok = worst >= -TAIL_MULT * avg_win if avg_win > 0 else False
    enough = len(days) >= require_days
    c1 = cell_ok
    c2 = blo > 0
    c3 = green >= MIN_GREEN_DAYS
    c4 = tail_ok

    print(f"\n-- PREREG verdict (requires {require_days} full days) --")
    print(f"  [{'OK' if enough else 'FAIL'}] window: {len(days)}/{require_days} full UTC days")
    print(f"  [{'OK' if c1 else 'FAIL'}] rule 1 all 4 cells > 0")
    print(f"  [{'OK' if c2 else 'FAIL'}] rule 2 6h-block 90% CI lower > 0 "
          f"(lower={blo:+.4f})")
    print(f"  [{'OK' if c3 else 'FAIL'}] rule 3 positive days {green}/{len(days)} "
          f">= {MIN_GREEN_DAYS}")
    print(f"  [{'OK' if c4 else 'FAIL'}] rule 4 worst day {worst:+.4f} >= "
          f"-{TAIL_MULT:g}x avg win {avg_win:+.4f} ({-TAIL_MULT * avg_win:+.4f})")
    passed = enough and c1 and c2 and c3 and c4
    if not enough:
        print(f"\nRESULT: NOT VALID YET — only {len(days)} full days (need {require_days}). "
              f"Provisional value: {'PASS' if (c1 and c2 and c3 and c4) else 'FAIL'}.")
    else:
        print(f"\nRESULT: {'PASS' if passed else 'FAIL'}")
    return 0


SEGMENT_GAP_S = 6 * 3600
COMPLETE_DAY_ROWS = 20
LEDGER_CANDIDATES = ["HYPE:16", "HYPE:12", "XMR:16", "kBONK:12"]


def _parse_ts(s: str) -> float:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def split_segments(rows: list[dict], gap_s: float = SEGMENT_GAP_S) -> list[list[dict]]:
    """Split cumulative ledger rows at engine resets.

    A reset is a time gap longer than `gap_s` (the tracker persists hourly, so a
    multi-hour gap means it was stopped/restarted) or a decrease in cumulative
    fills (impossible inside one continuous engine run).
    """
    segs: list[list[dict]] = []
    cur: list[dict] = []
    prev_ts: float | None = None
    prev_fills: float | None = None
    for r in sorted(rows, key=lambda x: _parse_ts(x.get("ts_utc", ""))):
        ts = _parse_ts(r.get("ts_utc", ""))
        fills = _float(r, "fills")
        reset = bool(cur) and (
            (prev_ts is not None and ts - prev_ts > gap_s)
            or (prev_fills is not None and fills < prev_fills))
        if reset:
            segs.append(cur)
            cur = []
        cur.append(r)
        prev_ts = ts
        prev_fills = fills
    if cur:
        segs.append(cur)
    return segs


def segment_days(seg: list[dict]) -> list[tuple[str, float, int, bool]]:
    """(day, net delta, fills delta, partial) for one segment.

    Deltas are taken only inside the segment.  The first day of a segment is
    always marked partial, and a day with fewer than COMPLETE_DAY_ROWS rows is
    partial, so neither pollutes the daily statistics.
    """
    if not seg:
        return []
    order: list[str] = []
    info: dict[str, dict] = {}
    for r in seg:
        d = r.get("day") or r.get("ts_utc", "")[:10].replace("-", "")
        if d not in info:
            info[d] = {"last": r, "n": 0}
            order.append(d)
        info[d]["last"] = r
        info[d]["n"] += 1
    out: list[tuple[str, float, int, bool]] = []
    prev_last = seg[0]
    for i, d in enumerate(order):
        last = info[d]["last"]
        base = prev_last if i > 0 else seg[0]
        net = _float(last, "net_pnl_usd") - _float(base, "net_pnl_usd")
        fills = int(_float(last, "fills")) - int(_float(base, "fills"))
        partial = (i == 0) or (info[d]["n"] < COMPLETE_DAY_ROWS)
        out.append((d, net, fills, partial))
        prev_last = last
    return out


def _load_catchup(path: Path) -> dict:
    sp = path.parent / "paper_live_state.json"
    if not sp.exists():
        return {}
    try:
        raw = json.loads(sp.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for key, st in (raw.get("candidates") or {}).items():
        if isinstance(st, dict) and st.get("catchup_daily"):
            out[key] = st["catchup_daily"]
    return out


def report_ledger(path: Path) -> int:
    rows = load_rows(path)
    if not rows:
        print(f"no ledger rows at {path}")
        return 1
    by_cand: dict[str, list[dict]] = {}
    for r in rows:
        key = r.get("coin", "") + ":" + r.get("hs", "")
        by_cand.setdefault(key, []).append(r)
    catchup = _load_catchup(path)

    print("=== LIVE SHADOW LEDGER — reset-aware (monitoring only, NOT the PREREG test) ===")
    print(f"Reset rule: split when rows are >{SEGMENT_GAP_S // 3600}h apart or cumulative")
    print("fills decrease. No PnL delta is computed across a reset boundary.\n")

    for c in LEDGER_CANDIDATES:
        rs = by_cand.get(c, [])
        if not rs:
            continue
        segs = split_segments(rs)
        print(f"[{c}]  rows={len(rs)}  segments={len(segs)}")
        complete: list[float] = []
        for si, seg in enumerate(segs, 1):
            t0 = seg[0].get("ts_utc", "")
            t1 = seg[-1].get("ts_utc", "")
            tag = "  [RESET]" if si > 1 else ""
            print(f"  segment {si}: {t0}..{t1}  rows={len(seg)}{tag}")
            if si > 1:
                print(f"    baseline (first row): net={_float(seg[0], 'net_pnl_usd'):+.4f} "
                      f"fills={int(_float(seg[0], 'fills'))}")
            for d, net, fills, partial in segment_days(seg):
                if len(seg) == 1:
                    continue
                m = "GREEN" if net > 0 else ("RED  " if net < 0 else "FLAT ")
                mark = " (partial)" if partial else ""
                print(f"    {d}: {m} net {net:+.4f}  fills {fills:+d}{mark}")
                if not partial:
                    complete.append(net)
            print(f"    end cumulative: net={_float(seg[-1], 'net_pnl_usd'):+.4f} "
                  f"fills={int(_float(seg[-1], 'fills'))}")

        latest = segs[-1][-1]
        print(f"  latest cumulative: net={_float(latest, 'net_pnl_usd'):+.4f} "
              f"fills={int(_float(latest, 'fills'))}")
        if complete:
            st = summarize(complete)
            lo, hi = bootstrap_mean_ci(complete)
            t2 = top2_stats(complete)
            share = "n/a" if math.isnan(t2["share"]) else f"{t2['share'] * 100:.1f}%"
            print(f"  stats over {len(complete)} complete days inside single segments:")
            print(f"    mean {st['mean']:+.4f} sd {st['sd']:.4f} t {st['t']:+.2f} | "
                  f"90% CI mean [{lo:+.4f}, {hi:+.4f}] | top-2 share {share}")
        else:
            print("  stats: no complete day contained in one segment (not computable)")
        cd = catchup.get(c) or {}
        if cd:
            print("  current segment (state.catchup_daily, monitoring):")
            for d in sorted(cd):
                print(f"    {d}: net {_float(cd[d], 'net_pnl_usd'):+.4f} "
                      f"fills {int(_float(cd[d], 'fills'))}")
        print()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coin", default="HYPE")
    p.add_argument("--hs", type=int, default=16)
    p.add_argument("--eval", default=str(EVAL_CSV))
    p.add_argument("--blocks", default=str(BLOCKS_CSV))
    p.add_argument("--ledger", default=str(LEDGER_CSV))
    p.add_argument("--require-days", type=int, default=MIN_DAYS)
    p.add_argument("--ledger-only", action="store_true")
    args = p.parse_args()

    if args.ledger_only:
        return report_ledger(Path(args.ledger))
    rows = load_rows(Path(args.eval))
    if not rows:
        print(f"(no eval rows at {args.eval}; falling back to live ledger)\n")
        return report_ledger(Path(args.ledger))
    return report_eval(rows, load_rows(Path(args.blocks)), args.coin, args.hs,
                       args.require_days)


if __name__ == "__main__":
    raise SystemExit(main())
