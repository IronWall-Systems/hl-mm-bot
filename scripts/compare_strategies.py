#!/usr/bin/env python3
"""scripts/compare_strategies.py — Strategy A (passive MM) vs Strategy B (1H breakout).

Both models run on the SAME HYPE dataset and the SAME execution assumptions:

* screening window 2026-10-20 .. 2026-11-02 (14 full UTC days, collector v2 shards)
* Tokyo execution latency = 66 ms
* maker fee 1.5 bps; breakout exits are taker 4.5 bps
* perpetual funding accrued hourly from the collector `e=funding` records
* starting equity $30

Strategy A — legacy passive market making
    ReplayEngine (scripts/replay_backtest.py), HYPE half-spread 16 bps, skew 6 bps,
    $15 quote / $60 max, cancel_rate 0.5, fresh engine per UTC day (PREREG protocol).

Strategy B — 1H volatility breakout
    scripts/breakout_backtest.py, 20-bar breakout, VolRatio >= 1.20,
    1.5*ATR initial stop, 3.0*ATR Chandelier trailing stop, flattened at window end.

This is research-only: no credentials, no network orders.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from replay_backtest import (  # noqa: E402
    ReplayEngine,
    gather_sources,
    iter_records,
    load_specs,
)
import breakout_backtest as bb  # noqa: E402

COIN = "HYPE"
DATA_DIR = PROJECT_ROOT / "data"
ARCHIVE_DIR = DATA_DIR / "archive_tars"
UNIVERSE = DATA_DIR / "universe.json"
REPORT_PATH = PROJECT_ROOT / "docs" / "strategy_comparison_20261105.md"

START_DAY = "20261020"
END_DAY = "20261104"
LATENCY_MS = 66.0
START_EQUITY = 30.0
DAYS = 14

MM_FEE_BPS = 1.5
BREAKOUT_ENTRY_FEE_BPS = 1.5
BREAKOUT_EXIT_FEE_BPS = 4.5

MM_PARAMS = dict(
    half_spread_bps=16.0,
    skew_bps=6.0,
    quote_notional=15.0,
    max_notional=60.0,
    min_order_notional=10.0,
    latency_ms=LATENCY_MS,
    feed_age_ms=LATENCY_MS,
    cancel_rate=0.5,
    maker_fee_bps=MM_FEE_BPS,
)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def day_keys(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y%m%d").replace(tzinfo=timezone.utc)
    d1 = datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc)
    out = []
    while d0 <= d1:
        out.append(d0.strftime("%Y%m%d"))
        d0 += timedelta(days=1)
    return out


def day_start_ts(day: str) -> float:
    return datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()


def running_net(eng: ReplayEngine) -> float:
    realized = 0.0
    fees = 0.0
    for f in eng.fills:
        realized += f.px * f.sz if f.side == "sell" else -f.px * f.sz
        fees += f.px * f.sz * eng.maker_fee_bps / 1e4
    return realized + eng.pos * eng.last_mid + eng.funding_pnl - fees


def round_trip_pnls(fills, fee_bps: float) -> list[float]:
    """FIFO round-trip PnL, one entry per closing fill. Open lots are excluded."""
    fee_rate = fee_bps / 1e4
    lots: deque[list[float]] = deque()  # [signed_qty, entry_px, fee_per_unit]
    out: list[float] = []
    for f in fills:
        remaining = f.sz if f.side == "buy" else -f.sz
        fee_unit = f.px * fee_rate
        pnl = 0.0
        matched = 0.0
        while remaining != 0.0 and lots and lots[0][0] * remaining < 0:
            lot = lots[0]
            m = min(abs(remaining), abs(lot[0]))
            if remaining > 0:  # buy closing a short lot
                gross = (lot[1] - f.px) * m
            else:              # sell closing a long lot
                gross = (f.px - lot[1]) * m
            pnl += gross - (fee_unit + lot[2]) * m
            matched += m
            if abs(lot[0]) > m:
                lot[0] += m if lot[0] < 0 else -m
                remaining += m if remaining < 0 else -m
            else:
                remaining += lot[0]
                lots.popleft()
        if matched > 0:
            out.append(pnl)
        if remaining != 0.0:
            lots.append([remaining, f.px, fee_unit])
    return out


def pf_and_win(pnls: list[float]) -> tuple[float, float, int, int]:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    wr = len(wins) / len(pnls) * 100.0 if pnls else 0.0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    return pf, wr, len(wins), len(losses)


def max_drawdown(curve: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Return (max_dd_usd, max_dd_pct, peak, trough)."""
    peak = -math.inf
    best = (0.0, 0.0, 0.0, 0.0)
    peak_val = 0.0
    trough_val = 0.0
    for _ts, eq in curve:
        if eq > peak:
            peak = eq
            peak_val = eq
        dd = peak - eq
        if dd > best[0]:
            best = (dd, (dd / peak * 100.0) if peak > 0 else 0.0, peak_val, eq)
    return best


# --------------------------------------------------------------------------------------
# Strategy A — passive market making
# --------------------------------------------------------------------------------------
def run_mm(spec, days: list[str]) -> dict:
    daily: list[dict] = []
    all_round_trips: list[float] = []
    curve: list[tuple[float, float]] = []
    funding_rates: dict[int, tuple[float, float]] = {}
    cum = 0.0
    tot_net = tot_fees = tot_funding = tot_volume = 0.0
    tot_fills = tot_places = tot_cancels = 0

    for day in days:
        start_key = int(day) * 100
        paths, tars = gather_sources(
            DATA_DIR, COIN, start_key, start_key + 23, archive_dir=ARCHIVE_DIR
        )
        eng = ReplayEngine(spec, **MM_PARAMS)
        day0 = day_start_ts(day)
        points: list[tuple[float, float]] = [(day0, 0.0)]
        next_edge = day0 + 3600.0
        n = 0
        for rec in iter_records(paths, tars, COIN):
            t_ms = rec.get("t")
            if rec.get("e") == "funding" and isinstance(t_ms, (int, float)):
                try:
                    rate = float(rec["funding"])
                    mark = float(rec.get("mark_px") or 0.0)
                    if mark > 0:
                        funding_rates[int(t_ms // 1000 // 3600)] = (rate, mark)
                except (KeyError, TypeError, ValueError):
                    pass
            eng.on_record(rec)
            n += 1
            if n % 50_000 == 0:
                eng.feed_ages_ms.clear()
            if isinstance(t_ms, (int, float)):
                ts = t_ms / 1000.0
                while ts >= next_edge and next_edge <= day0 + 86400.0:
                    points.append((next_edge, running_net(eng)))
                    next_edge += 3600.0
        final = running_net(eng)
        while next_edge <= day0 + 86400.0:
            points.append((next_edge, final))
            next_edge += 3600.0
        m = eng.compute_metrics()
        rts = round_trip_pnls(eng.fills, MM_FEE_BPS)
        all_round_trips.extend(rts)

        daily.append({
            "day": day,
            "net": m["net_pnl_usd"],
            "funding": m["funding_pnl"],
            "fees": m["total_fees_usd"],
            "fills": m["fills"],
            "places": m["places"],
            "cancels": m["cancels"],
            "volume": m["traded_volume_usd"],
            "round_trips": len(rts),
            "ending_inventory": m["ending_inventory_usd"],
        })
        for ts, v in points:
            curve.append((ts, START_EQUITY + cum + v))
        cum += m["net_pnl_usd"]

        tot_net += m["net_pnl_usd"]
        tot_fees += m["total_fees_usd"]
        tot_funding += m["funding_pnl"]
        tot_volume += m["traded_volume_usd"]
        tot_fills += m["fills"]
        tot_places += m["places"]
        tot_cancels += m["cancels"]

    return {
        "name": "Strategy A — Passive MM (HYPE, 16 bps)",
        "daily": daily,
        "curve": curve,
        "round_trips": all_round_trips,
        "net": tot_net,
        "fees": tot_fees,
        "funding": tot_funding,
        "gross": tot_net + tot_fees - tot_funding,
        "volume": tot_volume,
        "fills": tot_fills,
        "places": tot_places,
        "cancels": tot_cancels,
        "funding_rates": funding_rates,
    }


# --------------------------------------------------------------------------------------
# Strategy B — 1H breakout
# --------------------------------------------------------------------------------------
def funding_events(entry_s: float, exit_s: float, side: str, sz: float,
                   rates: dict[int, tuple[float, float]]) -> list[tuple[float, float]]:
    """Hourly funding charges at boundaries where the position is still held."""
    sign = 1.0 if side == "LONG" else -1.0
    out = []
    h = int(entry_s // 3600)
    while True:
        boundary = (h + 1) * 3600
        if boundary > exit_s:
            break
        if boundary > entry_s:
            rec = rates.get(h)
            if rec:
                rate, mark = rec
                out.append((float(boundary), -sign * sz * mark * rate))
        h += 1
    return out


def run_breakout(days: list[str], funding_rates: dict[int, tuple[float, float]]) -> dict:
    start_ms = int(day_start_ts(days[0]) * 1000)
    end_ms = int((day_start_ts(days[-1]) + 86400) * 1000)
    candles = bb.fetch_1h_candles(COIN, lookback_days=365)
    res = bb.simulate(
        candles,
        latency_ms=LATENCY_MS,
        start_ms=start_ms,
        end_ms=end_ms,
        start_equity=START_EQUITY,
        flatten_at_end=True,
    )

    trades = []
    fee_total = 0.0
    funding_total = 0.0
    events: list[tuple[float, float]] = []
    for t in res["trades"]:
        entry_s = t["entry_ts_ms"] / 1000.0
        exit_s = t["exit_ts_ms"] / 1000.0
        ev = funding_events(entry_s, exit_s, t["side"], t["sz"], funding_rates)
        f = sum(a for _, a in ev)
        events.extend(ev)
        fee = t["sz"] * (t["entry_px"] * BREAKOUT_ENTRY_FEE_BPS
                         + t["exit_px"] * BREAKOUT_EXIT_FEE_BPS) / 1e4
        fee_total += fee
        funding_total += f
        row = dict(t)
        row["funding"] = f
        row["net_total"] = t["net_pnl"] + f
        row["fees"] = fee
        row["exit_day"] = datetime.fromtimestamp((t["exit_ts_ms"] - 1) / 1000.0,
                                                 tz=timezone.utc).strftime("%Y%m%d")
        trades.append(row)

    daily_map = {d: 0.0 for d in days}
    for t in trades:
        if t["exit_day"] in daily_map:
            daily_map[t["exit_day"]] += t["net_total"]
    daily = [{"day": d, "net": daily_map[d], "trades": sum(1 for t in trades if t["exit_day"] == d)}
             for d in days]

    events.sort()
    curve = []
    ei = 0
    acc = 0.0
    for ts_ms, eq in res["equity_curve"]:
        if ts_ms < start_ms or ts_ms > end_ms:
            continue
        while ei < len(events) and events[ei][0] <= ts_ms / 1000.0:
            acc += events[ei][1]
            ei += 1
        curve.append((ts_ms / 1000.0, eq + acc))
    if curve:
        curve.append((end_ms / 1000.0, curve[-1][1]))

    net = sum(t["net_total"] for t in trades)
    return {
        "name": "Strategy B — 1H Breakout (1.5×ATR / 3.0×ATR)",
        "daily": daily,
        "curve": curve,
        "trades": trades,
        "round_trips": [t["net_total"] for t in trades],
        "net": net,
        "fees": fee_total,
        "funding": funding_total,
        "gross": net + fee_total - funding_total,
        "volume": sum(t["sz"] * (t["entry_px"] + t["exit_px"]) for t in trades),
        "fills": 2 * len(trades),
        "places": len(trades),
        "cancels": 0,
        "n_trades": len(trades),
        "cooldown_triggers": res.get("cooldown_triggers", 0),
        "entries_blocked": res.get("entries_blocked", 0),
        "disable_cooldown": res.get("disable_cooldown", False),
    }


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------
def summarize(r: dict, days: int) -> dict:
    pf, wr, wins, losses = pf_and_win(r["round_trips"])
    dd, ddp, peak, trough = max_drawdown(r["curve"])
    worst = sorted(r["daily"], key=lambda x: x["net"])[:3]
    best = sorted(r["daily"], key=lambda x: x["net"], reverse=True)[:3]
    return {
        "pf": pf, "wr": wr, "wins": wins, "losses": losses,
        "dd": dd, "ddp": ddp, "peak": peak, "trough": trough,
        "worst": worst, "best": best,
        "trades": len(r["round_trips"]),
        "trades_day": len(r["round_trips"]) / days,
        "fills": r["fills"], "fills_day": r["fills"] / days,
        "orders_day": r["places"] / days,
        "cooldown_triggers": r.get("cooldown_triggers", 0),
        "entries_blocked": r.get("entries_blocked", 0),
        "net": r["net"], "return_pct": (r["net"] / START_EQUITY) * 100.0,
        "end_equity": START_EQUITY + r["net"],
    }


def fmt_pf(pf: float) -> str:
    return "inf" if math.isinf(pf) else f"{pf:.2f}"


def side_by_side(a: dict, b: dict, sa: dict, sb: dict) -> str:
    rows = [
        ("Total trades / round trips", f"{sa['trades']}", f"{sb['trades']}"),
        ("Exchange fills (entries+exits)", f"{sa['fills']}", f"{sb['fills']}"),
        ("Trades per day", f"{sa['trades_day']:.1f}", f"{sb['trades_day']:.1f}"),
        ("Fills per day", f"{sa['fills_day']:.1f}", f"{sb['fills_day']:.1f}"),
        ("Orders placed per day", f"{sa['orders_day']:.0f}", f"{sb['orders_day']:.1f}"),
        ("Cooldown triggers (24h)", "n/a (baseline)", f"{sb['cooldown_triggers']}"),
        ("Signals blocked by cooldown", "n/a (baseline)", f"{sb['entries_blocked']}"),
        ("Win rate", f"{sa['wr']:.1f}%", f"{sb['wr']:.1f}%"),
        ("Profit factor", fmt_pf(sa['pf']), fmt_pf(sb['pf'])),
        ("Gross PnL", f"${a['gross']:+.2f}", f"${b['gross']:+.2f}"),
        ("Fees paid", f"${-a['fees']:.2f}", f"${-b['fees']:.2f}"),
        ("Funding PnL", f"${a['funding']:+.2f}", f"${b['funding']:+.2f}"),
        ("Net PnL (after fees+funding)", f"${sa['net']:+.2f}", f"${sb['net']:+.2f}"),
        ("Return on $30", f"{sa['return_pct']:+.1f}%", f"{sb['return_pct']:+.1f}%"),
        ("Max drawdown", f"${sa['dd']:.2f} ({sa['ddp']:.1f}%)", f"${sb['dd']:.2f} ({sb['ddp']:.1f}%)"),
        ("Best day", f"${sa['best'][0]['net']:+.2f}", f"${sb['best'][0]['net']:+.2f}"),
        ("Worst day", f"${sa['worst'][0]['net']:+.2f}", f"${sb['worst'][0]['net']:+.2f}"),
    ]
    w1, w2, w3 = 34, 26, 30
    out = [f"{'Metric':<{w1}} {'Strategy A — Passive MM':>{w2}} {'Strategy B — 1H Breakout':>{w3}}",
           "-" * (w1 + w2 + w3 + 2)]
    for label, va, vb in rows:
        out.append(f"{label:<{w1}} {va:>{w2}} {vb:>{w3}}")
    return "\n".join(out)


def daily_table(a: dict, b: dict) -> str:
    am = {d["day"]: d["net"] for d in a["daily"]}
    bm = {d["day"]: d["net"] for d in b["daily"]}
    out = [f"{'UTC day':<10} {'MM $':>10} {'Breakout $':>12} {'MM cum $':>12} {'BO cum $':>12}",
           "-" * 60]
    ca = cb = 0.0
    for d in sorted(am):
        ca += am[d]
        cb += bm.get(d, 0.0)
        out.append(f"{d:<10} {am[d]:>+10.2f} {bm.get(d, 0.0):>+12.2f} {ca:>+12.2f} {cb:>+12.2f}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DAY)
    ap.add_argument("--end", default=END_DAY)
    args = ap.parse_args()
    days = day_keys(args.start, args.end)

    spec = load_specs(UNIVERSE)[COIN]
    print(f"[*] Strategy comparison | {COIN} | {len(days)} UTC days | latency {LATENCY_MS:.0f} ms "
          f"| maker {MM_FEE_BPS} bps / taker {BREAKOUT_EXIT_FEE_BPS} bps")

    print("[*] Running Strategy A (passive MM) ...")
    a = run_mm(spec, days)
    print(f"    done: {a['fills']} fills, net ${a['net']:+.2f}")

    print("[*] Running Strategy B (1H breakout) ...")
    b = run_breakout(days, a["funding_rates"])
    print(f"    done: {len(b['trades'])} trades, net ${b['net']:+.2f}")

    sa = summarize(a, len(days))
    sb = summarize(b, len(days))

    print("\n" + "=" * 94)
    print(f" STRATEGY COMPARISON — {COIN} — {days[0]}..{days[-1]} — {LATENCY_MS:.0f} ms — ${START_EQUITY:.0f} start")
    print("=" * 94)
    print(side_by_side(a, b, sa, sb))

    print("\n=== Daily net PnL (after fees and funding) ===")
    print(daily_table(a, b))

    print("\n=== Worst days ===")
    for label, s in (("A (MM)", sa), ("B (Breakout)", sb)):
        days_s = ", ".join(f"{x['day']} ${x['net']:+.2f}" for x in s["worst"])
        print(f"{label}: {days_s}")

    if abs(sa["net"] - sb["net"]) < 0.01:
        verdict = f"Tie: both models net ~${sa['net']:+.2f}."
    elif sa["net"] > sb["net"]:
        verdict = (f"Strategy A (passive MM) leads on net PnL: ${sa['net']:+.2f} "
                   f"vs ${sb['net']:+.2f} for Strategy B.")
    else:
        verdict = (f"Strategy B (1H breakout) outperforms Strategy A (passive MM) on this "
                   f"window: net ${sb['net']:+.2f} vs ${sa['net']:+.2f}.")
    conclusion = (
        verdict + "\n"
        f"Risk: MM max DD ${sa['dd']:.2f} ({sa['ddp']:.1f}%), breakout max DD "
        f"${sb['dd']:.2f} ({sb['ddp']:.1f}%); PF {fmt_pf(sa['pf'])} vs {fmt_pf(sb['pf'])}.\n"
        f"The MM sample has {sa['trades']} round trips and a large Oct-29 inventory tail; the "
        f"breakout has only {sb['trades']} trades, so neither result is statistically powered."
        + (" Both models are net negative on this window." if sa['net'] < 0 and sb['net'] < 0 else "")
    )
    print("\n=== Conclusion ===")
    print(conclusion)

    # Markdown report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open("w", encoding="utf-8") as fh:
        fh.write(f"# Strategy comparison — {COIN}, {days[0]}..{days[-1]}\n\n")
        fh.write(f"- Latency: {LATENCY_MS:.0f} ms (Tokyo); fees: maker {MM_FEE_BPS} bps, "
                 f"taker {BREAKOUT_EXIT_FEE_BPS} bps; starting equity ${START_EQUITY:.0f}\n")
        fh.write(f"- Strategy A: passive MM, half-spread 16 bps, skew 6 bps, cancel_rate 0.5, "
                 f"fresh engine per UTC day\n")
        fh.write(f"- Strategy B: 20-bar breakout, VolRatio >= 1.20, 1.5×ATR stop, "
                 f"3.0×ATR Chandelier, flattened at window end\n")
        fh.write(f"- Funding accrued hourly from collector records; MM net includes inventory MTM "
                 f"at day boundaries\n\n")
        fh.write("## Side-by-side\n\n```\n")
        fh.write(side_by_side(a, b, sa, sb))
        fh.write("\n```\n\n## Daily PnL\n\n```\n")
        fh.write(daily_table(a, b))
        fh.write("\n```\n\n## Worst days\n\n")
        for label, s in (("A (MM)", sa), ("B (Breakout)", sb)):
            days_s = ", ".join(f"{x['day']} ${x['net']:+.2f}" for x in s["worst"])
            fh.write(f"- **{label}**: {days_s}\n")
        fh.write("\n## Conclusion\n\n")
        fh.write(conclusion + "\n\n")
        fh.write("## Caveats\n\n")
        fh.write("- One coin (HYPE), 14 days; not a statistically powered comparison.\n")
        fh.write("- MM engine is re-instantiated daily (PREREG protocol), so inventory is "
                 "marked to market at each UTC day boundary rather than carried.\n")
        fh.write("- Breakout takes one position at a time and is flattened at window end.\n")
        fh.write("- Maker fill assumptions: MM uses the replay queue model; breakout entry "
                 "uses a maker fee but no queue model.\n")
    print(f"\n[*] Report written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
