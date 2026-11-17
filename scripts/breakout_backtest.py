#!/usr/bin/env python3
"""scripts/breakout_backtest.py — 1H Asymmetric Volatility Breakout Backtest on HYPE.

Execution-latency model (2026-11-05)
------------------------------------
The original bar-close model filled at the 1H close with NO latency term. A 1H bar
cannot resolve 66 ms vs 500 ms, so the latency adjustment here is measured from the
recorded top-of-book ticks: for every decision/stop trigger at time ``t`` the fill
price is shifted by ``mid(t + latency) - mid(t)``. With ``latency = 0`` this reduces
exactly to the original bar-close model. Tick shards are read from
``data/YYYYMMDD/HH_HYPE.jsonl.gz`` or ``data/archive_tars/data_YYYYMMDD.tar``.

Strategy parameters are unchanged: 1.5 * ATR_14 initial stop, 3.0 * ATR_14 Chandelier
trailing stop, 20-bar breakout, VolRatio >= 1.20, maker entry fee / taker exit fee.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import math
import statistics
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
OUT_CSV = DATA_DIR / "breakout_backtest_trades.csv"
ARCHIVE_DIR = DATA_DIR / "archive_tars"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

MAKER_FEE_BPS = 1.5
TAKER_FEE_BPS = 4.5

# Measured Tokyo VPS TTFB (old Turkey VPS was ~500 ms).
TOKYO_LATENCY_MS = 66.0
TURKEY_LATENCY_MS = 500.0
EXECUTION_LATENCY_MS = TOKYO_LATENCY_MS

# 1.5 * ATR initial stop / 3.0 * ATR Chandelier trailing stop (active variant protocol).
INITIAL_SL_ATR = 1.5
CHANDELIER_ATR = 3.0

# 24-hour circuit breaker: a trade/day loss worse than this pauses new entries.
LOSS_COOLDOWN_USD = 0.25
COOLDOWN_HOURS = 24.0


def fetch_1h_candles(coin: str = "HYPE", lookback_days: int = 30) -> list[dict]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - lookback_days * 86400 * 1000
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": now_ms},
    }
    resp = requests.post(HL_INFO_URL, json=payload, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    candles = []
    for c in raw:
        candles.append({
            "ts_ms": int(c["t"]),
            "open": float(c["o"]),
            "high": float(c["h"]),
            "low": float(c["l"]),
            "close": float(c["c"]),
            "vol": float(c["v"]),
        })
    candles.sort(key=lambda x: x["ts_ms"])
    # Drop the currently forming (unclosed) hour candle
    current_hour_ms = (now_ms // 3600000) * 3600000
    candles = [c for c in candles if c["ts_ms"] < current_hour_ms]
    return candles


def compute_indicators(candles: list[dict], idx: int) -> dict | None:
    """Compute HH_20, LL_20, ATR_14, and VolRatio at closed candle index `idx` (strictly no look-ahead)."""
    if idx < 30:
        return None

    # Preceding 20 candles BEFORE candle `idx`
    prev_20 = candles[idx - 20 : idx]
    hh_20 = max(c["high"] for c in prev_20)
    ll_20 = min(c["low"] for c in prev_20)

    # Wilder ATR_14 up to `idx`
    trs = []
    for i in range(1, idx + 1):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_14 = trs[0]
    for tr in trs[1:]:
        atr_14 = (atr_14 * 13.0 + tr) / 14.0

    # VolRatio: 6h realized log-return stdev vs trailing 24h median of 6h stdev
    def rv6_at(k: int) -> float:
        rets = [
            math.log(candles[j]["close"] / candles[j - 1]["close"])
            for j in range(k - 5, k + 1)
            if candles[j - 1]["close"] > 0
        ]
        return statistics.pstdev(rets) if len(rets) >= 2 else 0.0

    rv_6h = rv6_at(idx)
    rv_hist = [rv6_at(k) for k in range(idx - 23, idx + 1)]
    rv_ref = statistics.median(rv_hist) if rv_hist else 0.0
    vol_ratio = rv_6h / max(rv_ref, 1e-8)

    return {"hh_20": hh_20, "ll_20": ll_20, "atr_14": atr_14, "vol_ratio": vol_ratio}


# --------------------------------------------------------------------------------------
# Tick shard access (HYPE only; used solely to measure the mid move over the latency)
# --------------------------------------------------------------------------------------
_SHARD_CACHE: dict[tuple[str, int], tuple[list, list, list, list] | None] = {}
_MISSING_SHARDS: set[tuple[str, int]] = set()


def _read_shard(day: str, hour: int) -> tuple[list, list, list, list] | None:
    key = (day, hour)
    if key in _SHARD_CACHE:
        return _SHARD_CACHE[key]
    lines: list[str] | None = None
    local = DATA_DIR / day / f"{hour:02d}_HYPE.jsonl.gz"
    if local.exists():
        with gzip.open(local, "rt", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        tar_path = ARCHIVE_DIR / f"data_{day}.tar"
        if tar_path.exists():
            with tarfile.open(tar_path) as tf:
                try:
                    member = tf.extractfile(f"{day}/{hour:02d}_HYPE.jsonl.gz")
                except KeyError:
                    member = None
                if member is not None:
                    lines = gzip.decompress(member.read()).decode("utf-8").splitlines()
    if lines is None:
        _MISSING_SHARDS.add(key)
        _SHARD_CACHE[key] = None
        return None

    bbo_t: list[int] = []
    bbo_mid: list[float] = []
    tr_t: list[int] = []
    tr_px: list[float] = []
    for line in lines:
        try:
            rec = json.loads(line)
            ev = rec.get("e")
            if ev == "bbo":
                bbo_t.append(int(rec["t"]))
                bbo_mid.append((float(rec["b"][0]) + float(rec["a"][0])) / 2.0)
            elif ev == "tr":
                tr_t.append(int(rec["t"]))
                tr_px.append(float(rec["px"]))
        except (ValueError, KeyError, TypeError):
            continue
    out = (bbo_t, bbo_mid, tr_t, tr_px)
    _SHARD_CACHE[key] = out
    return out


def _shard_for(ts_ms: int):
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y%m%d"), dt.hour


def mid_at(ts_ms: int) -> float | None:
    """Last top-of-book mid at or before ts_ms (fallback to first tick of the hour)."""
    day, hour = _shard_for(ts_ms)
    shard = _read_shard(day, hour)
    if shard is None:
        return None
    bbo_t, bbo_mid, _, _ = shard
    if not bbo_t:
        return None
    i = bisect.bisect_right(bbo_t, ts_ms) - 1
    if i >= 0:
        return bbo_mid[i]
    # ts is before the first recorded tick of this hour: use previous hour's last mid.
    prev = ts_ms - 3600000
    pday, phour = _shard_for(prev)
    pshard = _read_shard(pday, phour)
    if pshard and pshard[0]:
        return pshard[1][-1]
    return bbo_mid[0]


def latency_drift(ts_ms: int, latency_ms: float) -> float | None:
    """Mid-price shift over the execution latency, in USD.

    Returns 0.0 when the quote simply did not change inside the window, and None only
    when no tick data exists for the timestamp.
    """
    if latency_ms <= 0:
        return 0.0
    m0 = mid_at(ts_ms)
    m1 = mid_at(ts_ms + int(round(latency_ms)))
    if m0 is None or m1 is None:
        return None
    return m1 - m0


def first_cross_time(bar_ts_ms: int, sl_px: float, is_long: bool) -> int | None:
    """First recorded trade inside the bar that crosses the stop level."""
    day, hour = _shard_for(bar_ts_ms)
    shard = _read_shard(day, hour)
    if shard is None:
        return None
    tr_t, tr_px = shard[2], shard[3]
    end = bar_ts_ms + 3600000
    i = bisect.bisect_left(tr_t, bar_ts_ms)
    while i < len(tr_t) and tr_t[i] < end:
        px = tr_px[i]
        if (is_long and px <= sl_px) or ((not is_long) and px >= sl_px):
            return tr_t[i]
        i += 1
    return None


def tick_coverage() -> tuple[int, int] | None:
    """Full-day tick coverage from local shard dirs + tar archives."""
    days: set[str] = set()
    if ARCHIVE_DIR.exists():
        for tar_path in ARCHIVE_DIR.glob("data_*.tar"):
            day = tar_path.stem.replace("data_", "")
            try:
                datetime.strptime(day, "%Y%m%d")
            except ValueError:
                continue
            days.add(day)
    local_root = DATA_DIR
    for d in local_root.iterdir():
        if d.is_dir() and len(d.name) == 8 and (d / f"00_HYPE.jsonl.gz").exists():
            days.add(d.name)
    if not days:
        return None
    ordered = sorted(days)
    start = datetime.strptime(ordered[0], "%Y%m%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(ordered[-1], "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# --------------------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------------------
def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()


def simulate(candles: list[dict], latency_ms: float = 0.0,
             start_ms: int | None = None, end_ms: int | None = None,
             start_equity: float = 30.0, flatten_at_end: bool = False,
             cooldown_usd: float = LOSS_COOLDOWN_USD,
             cooldown_hours: float = COOLDOWN_HOURS,
             disable_cooldown: bool = False) -> dict:
    equity = start_equity
    peak_equity = start_equity
    max_dd_usd = 0.0
    active_trade = None
    trades: list[dict] = []
    daily_stopouts: dict[str, int] = {}
    daily_pnl: dict[str, float] = {}
    missing_drift = 0
    equity_curve: list[tuple[int, float]] = []
    last_processed = None
    cooldown_until_ms = 0
    cooldown_triggers = 0
    entries_blocked = 0
    cooldown_events: list[dict] = []
    daily_realized: dict[str, float] = {}

    for idx in range(30, len(candles)):
        bar = candles[idx]
        last_processed = bar
        bar_open_ts = bar["ts_ms"]
        bar_close_ts = bar_open_ts + 3600000
        day_str = datetime.fromtimestamp(bar_open_ts / 1000.0, tz=timezone.utc).strftime("%Y%m%d")
        ind = compute_indicators(candles, idx)
        if not ind:
            continue

        # 1. Check stop-loss trigger inside current bar if in position
        if active_trade is not None:
            side = active_trade["side"]
            sl_px = active_trade["sl_px"]
            stopped = False
            base_exit = sl_px
            trigger_ts = bar_open_ts

            if side == "LONG" and bar["low"] <= sl_px:
                stopped = True
                base_exit = min(sl_px, bar["open"])
                if bar["open"] <= sl_px:
                    trigger_ts = bar_open_ts
                else:
                    trigger_ts = first_cross_time(bar_open_ts, sl_px, True) or bar_close_ts
            elif side == "SHORT" and bar["high"] >= sl_px:
                stopped = True
                base_exit = max(sl_px, bar["open"])
                if bar["open"] >= sl_px:
                    trigger_ts = bar_open_ts
                else:
                    trigger_ts = first_cross_time(bar_open_ts, sl_px, False) or bar_close_ts

            if stopped:
                drift = latency_drift(trigger_ts, latency_ms)
                if drift is None:
                    missing_drift += 1
                    drift = 0.0
                exit_px = base_exit + drift
                sz = active_trade["sz"]
                gross = (exit_px - active_trade["entry_px"]) * sz if side == "LONG" else (active_trade["entry_px"] - exit_px) * sz
                fee_exit = (sz * exit_px) * (TAKER_FEE_BPS / 10000.0)
                net_pnl = gross - active_trade["fee_entry"] - fee_exit
                equity += net_pnl
                peak_equity = max(peak_equity, equity)
                max_dd_usd = max(max_dd_usd, peak_equity - equity)
                daily_pnl[day_str] = daily_pnl.get(day_str, 0.0) + net_pnl
                if net_pnl < 0:
                    daily_stopouts[day_str] = daily_stopouts.get(day_str, 0) + 1

                exit_day = datetime.fromtimestamp(trigger_ts / 1000.0, tz=timezone.utc).strftime("%Y%m%d")
                daily_realized[exit_day] = daily_realized.get(exit_day, 0.0) + net_pnl
                if not disable_cooldown:
                    trade_breach = net_pnl < -cooldown_usd
                    day_breach = daily_realized[exit_day] < -cooldown_usd
                    if trade_breach or day_breach:
                        until = trigger_ts + int(cooldown_hours * 3600 * 1000)
                        if until > cooldown_until_ms:
                            cooldown_until_ms = until
                        cooldown_triggers += 1
                        cooldown_events.append({
                            "time": _iso(trigger_ts),
                            "reason": "trade_loss" if trade_breach else "daily_loss",
                            "net_pnl": round(net_pnl, 6),
                            "daily_realized": round(daily_realized[exit_day], 6),
                            "cooldown_until": _iso(until),
                        })

                trades.append({
                    "entry_time": active_trade["entry_time"],
                    "exit_time": _iso(bar_close_ts),
                    "side": side,
                    "entry_px": round(active_trade["entry_px"], 6),
                    "exit_px": round(exit_px, 6),
                    "sz": round(sz, 2),
                    "net_pnl": round(net_pnl, 6),
                    "equity_after": round(equity, 6),
                    "entry_slip_bps": round(active_trade["entry_slip_bps"], 4),
                    "exit_slip_bps": round((drift / base_exit * 1e4) if base_exit else 0.0, 4),
                    "entry_ts_ms": active_trade["entry_ts_ms"],
                    "exit_ts_ms": trigger_ts,
                })
                active_trade = None
            else:
                # Update extreme and Chandelier 3.0 * ATR_14 trailing stop at candle close
                if side == "LONG":
                    active_trade["extreme_px"] = max(active_trade["extreme_px"], bar["high"])
                    cand_sl = active_trade["extreme_px"] - CHANDELIER_ATR * ind["atr_14"]
                    if cand_sl > active_trade["sl_px"] and cand_sl < bar["close"]:
                        active_trade["sl_px"] = cand_sl
                else:
                    active_trade["extreme_px"] = min(active_trade["extreme_px"], bar["low"])
                    cand_sl = active_trade["extreme_px"] + CHANDELIER_ATR * ind["atr_14"]
                    if cand_sl < active_trade["sl_px"] and cand_sl > bar["close"]:
                        active_trade["sl_px"] = cand_sl

        # 2. If FLAT at candle close, evaluate entry
        if active_trade is None:
            if start_ms is not None and bar_close_ts < start_ms:
                continue
            if end_ms is not None and bar_close_ts >= end_ms:
                continue
            if equity < 15.0:
                continue
            if daily_stopouts.get(day_str, 0) >= 2 or daily_pnl.get(day_str, 0.0) <= -1.20:
                continue
            if ind["vol_ratio"] < 1.20:
                continue

            signal = None
            if bar["close"] > ind["hh_20"]:
                signal = "LONG"
            elif bar["close"] < ind["ll_20"]:
                signal = "SHORT"

            if signal and not disable_cooldown and bar_close_ts < cooldown_until_ms:
                entries_blocked += 1
                signal = None

            if signal:
                base_entry = bar["close"]
                drift = latency_drift(bar_close_ts, latency_ms)
                if drift is None:
                    missing_drift += 1
                    drift = 0.0
                entry_px = base_entry + drift
                notional = max(12.0, min(round(0.50 * equity, 2), equity * 0.90))
                sz = round(notional / entry_px, 2)
                if sz * entry_px < 10.5:
                    sz = round(11.0 / entry_px, 2)
                fee_entry = (sz * entry_px) * (MAKER_FEE_BPS / 10000.0)
                sl_px = entry_px - INITIAL_SL_ATR * ind["atr_14"] if signal == "LONG" else entry_px + INITIAL_SL_ATR * ind["atr_14"]

                active_trade = {
                    "side": signal,
                    "entry_px": entry_px,
                    "sz": sz,
                    "sl_px": sl_px,
                    "extreme_px": bar["high"] if signal == "LONG" else bar["low"],
                    "fee_entry": fee_entry,
                    "entry_time": _iso(bar_close_ts),
                    "entry_ts_ms": bar_close_ts,
                    "entry_slip_bps": (drift / base_entry * 1e4) if base_entry else 0.0,
                }

        # Hourly mark-to-market equity snapshot (includes open-position PnL).
        if active_trade is not None:
            sz = active_trade["sz"]
            unreal = ((bar["close"] - active_trade["entry_px"]) * sz
                      if active_trade["side"] == "LONG"
                      else (active_trade["entry_px"] - bar["close"]) * sz)
            fee_exit_est = sz * bar["close"] * (TAKER_FEE_BPS / 10000.0)
            eq_mtm = equity + unreal - active_trade["fee_entry"] - fee_exit_est
        else:
            eq_mtm = equity
        equity_curve.append((bar_close_ts, eq_mtm))

        if flatten_at_end and end_ms is not None and bar_close_ts >= end_ms:
            break

    # Force-close any trade still open at the end of the requested window.
    if active_trade is not None and candles:
        last = last_processed if last_processed is not None else candles[-1]
        side = active_trade["side"]
        exit_px = last["close"]
        sz = active_trade["sz"]
        gross = (exit_px - active_trade["entry_px"]) * sz if side == "LONG" else (active_trade["entry_px"] - exit_px) * sz
        fee_exit = (sz * exit_px) * (TAKER_FEE_BPS / 10000.0)
        net_pnl = gross - active_trade["fee_entry"] - fee_exit
        equity += net_pnl
        peak_equity = max(peak_equity, equity)
        max_dd_usd = max(max_dd_usd, peak_equity - equity)
        trades.append({
            "entry_time": active_trade["entry_time"],
            "exit_time": _iso(last["ts_ms"] + 3600000),
            "side": side,
            "entry_px": round(active_trade["entry_px"], 6),
            "exit_px": round(exit_px, 6),
            "sz": round(sz, 2),
            "net_pnl": round(net_pnl, 6),
            "equity_after": round(equity, 6),
            "entry_slip_bps": round(active_trade["entry_slip_bps"], 4),
            "exit_slip_bps": 0.0,
            "entry_ts_ms": active_trade["entry_ts_ms"],
            "exit_ts_ms": last["ts_ms"] + 3600000,
        })
        equity_curve.append((last["ts_ms"] + 3600000, equity))

    window_curve = [
        (ts, eq) for ts, eq in equity_curve
        if (start_ms is None or ts >= start_ms) and (end_ms is None or ts <= end_ms)
    ]
    mtm_peak = -math.inf
    max_dd_mtm = 0.0
    for _ts, eq in window_curve:
        mtm_peak = max(mtm_peak, eq)
        max_dd_mtm = max(max_dd_mtm, mtm_peak - eq)

    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    slips = [abs(t["entry_slip_bps"]) for t in trades]
    return {
        "latency_ms": latency_ms,
        "trades": trades,
        "n_trades": len(trades),
        "wins": len(wins),
        "win_rate": win_rate,
        "profit_factor": pf,
        "net_pnl": equity - start_equity,
        "end_equity": equity,
        "max_dd_usd": max_dd_usd,
        "max_dd_mtm_usd": max_dd_mtm,
        "avg_abs_entry_slip_bps": (sum(slips) / len(slips)) if slips else 0.0,
        "missing_drift": missing_drift,
        "equity_curve": equity_curve,
        "cooldown_triggers": cooldown_triggers,
        "entries_blocked": entries_blocked,
        "cooldown_until_ms": cooldown_until_ms,
        "cooldown_events": cooldown_events,
        "disable_cooldown": disable_cooldown,
    }


def _save_trades(trades: list[dict], path: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fields = ["entry_time", "exit_time", "side", "entry_px", "exit_px", "sz",
              "net_pnl", "equity_after", "entry_slip_bps", "exit_slip_bps"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades)


def _print_result(res: dict) -> None:
    pf = res["profit_factor"]
    pf_s = "inf" if math.isinf(pf) else f"{pf:.2f}"
    print(f"[{res['latency_ms']:.0f} ms] Trades: {res['n_trades']} | Wins: {res['wins']} "
          f"| WinRate: {res['win_rate']:.1f}% | PF: {pf_s} "
          f"| Net: ${res['net_pnl']:+.2f} | End: ${res['end_equity']:.2f} "
          f"| MaxDD(realized): ${res['max_dd_usd']:.2f} | MaxDD(MTM): ${res['max_dd_mtm_usd']:.2f}"
          f" | avg|slip|: {res['avg_abs_entry_slip_bps']:.2f} bps"
          + (f" | missing-drift: {res['missing_drift']}" if res["missing_drift"] else "")
          + (f" | cooldown: {res['cooldown_triggers']} triggers, {res['entries_blocked']} entries blocked"
             if not res.get("disable_cooldown", False) else " | cooldown: OFF"))


def run_backtest(latency_ms: float = TOKYO_LATENCY_MS,
                 start_ms: int | None = None, end_ms: int | None = None,
                 save_path: Path | None = None, lookback_days: int = 365,
                 cooldown_usd: float = LOSS_COOLDOWN_USD,
                 cooldown_hours: float = COOLDOWN_HOURS,
                 disable_cooldown: bool = False) -> dict:
    candles = fetch_1h_candles("HYPE", lookback_days=lookback_days)
    print(f"[*] Loaded {len(candles)} closed 1H candles for HYPE.")
    res = simulate(candles, latency_ms=latency_ms, start_ms=start_ms, end_ms=end_ms,
                   cooldown_usd=cooldown_usd, cooldown_hours=cooldown_hours,
                   disable_cooldown=disable_cooldown)
    if save_path is not None:
        _save_trades(res["trades"], save_path)
    _print_result(res)
    return res


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latency-ms", type=float, default=EXECUTION_LATENCY_MS,
                        help=f"execution latency in ms (default Tokyo {TOKYO_LATENCY_MS:.0f})")
    parser.add_argument("--compare", default=None,
                        help="comma-separated latencies to compare, e.g. 0,66,500")
    parser.add_argument("--start", default=None, help="start date YYYY-MM-DD (UTC, inclusive)")
    parser.add_argument("--end", default=None, help="end date YYYY-MM-DD (UTC, exclusive)")
    parser.add_argument("--lookback-days", type=int, default=365,
                        help="candle history to request (API caps at ~5000 hourly candles)")
    parser.add_argument("--cooldown-usd", type=float, default=LOSS_COOLDOWN_USD,
                        help="loss threshold that arms the 24h circuit breaker")
    parser.add_argument("--cooldown-hours", type=float, default=COOLDOWN_HOURS)
    parser.add_argument("--no-cooldown", action="store_true", help="disable the circuit breaker")
    args = parser.parse_args()

    start_ms = end_ms = None
    if args.start:
        start_ms = int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    if args.end:
        end_ms = int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

    latencies = [TOKYO_LATENCY_MS]
    if args.compare:
        latencies = [float(x) for x in args.compare.split(",") if x.strip()]
    elif args.latency_ms is not None:
        latencies = [args.latency_ms]

    if start_ms and end_ms:
        print(f"[*] Window: {_iso(start_ms)} -> {_iso(end_ms)}")
    else:
        print("[*] Window: full available candle history")
    print(f"[*] Strategy: 20-bar breakout, VolRatio>=1.20, {INITIAL_SL_ATR}*ATR initial SL, "
          f"{CHANDELIER_ATR}*ATR chandelier, maker {MAKER_FEE_BPS}bps / taker {TAKER_FEE_BPS}bps")

    candles = fetch_1h_candles("HYPE", lookback_days=args.lookback_days)
    print(f"[*] Loaded {len(candles)} closed 1H candles for HYPE."
          f" ({_iso(candles[0]['ts_ms'])} -> {_iso(candles[-1]['ts_ms'] + 3600000)})")

    results = []
    for lat in latencies:
        res = simulate(candles, latency_ms=lat, start_ms=start_ms, end_ms=end_ms,
                       cooldown_usd=args.cooldown_usd, cooldown_hours=args.cooldown_hours,
                       disable_cooldown=args.no_cooldown)
        results.append(res)
        _print_result(res)
        _save_trades(res["trades"], DATA_DIR / f"breakout_backtest_trades_{lat:.0f}ms.csv")

    if len(results) >= 2:
        base = results[0]
        print("\n=== Latency impact ===")
        print(f"{'lat':>6} {'trades':>6} {'win%':>6} {'PF':>6} {'net$':>8} {'maxDD$':>8} "
              f"{'winΔ':>6} {'PFΔ':>6} {'netΔ$':>8} {'DDΔ$':>8}")
        for r in results:
            pf = r["profit_factor"]
            pf_s = "inf" if math.isinf(pf) else f"{pf:.2f}"
            print(f"{r['latency_ms']:>6.0f} {r['n_trades']:>6} {r['win_rate']:>6.1f} {pf_s:>6} "
                  f"{r['net_pnl']:>+8.2f} {r['max_dd_usd']:>8.2f} "
                  f"{r['win_rate']-base['win_rate']:>+6.1f} "
                  f"{((r['profit_factor']-base['profit_factor']) if not (math.isinf(pf) or math.isinf(base['profit_factor'])) else float('nan')):>+6.2f} "
                  f"{r['net_pnl']-base['net_pnl']:>+8.2f} {r['max_dd_usd']-base['max_dd_usd']:>+8.2f}")


if __name__ == "__main__":
    main()
