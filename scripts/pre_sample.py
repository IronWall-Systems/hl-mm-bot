#!/usr/bin/env python3
"""T10 Phase A — read-only mainnet sample (no keys, no orders, no signed requests).

Authorised by REQ-0034 STAGE 3 for this task only (recorded in .agent/DECISIONS.md):
  * POST {"type":"metaAndAssetCtxs"} to the mainnet info endpoint  -> data/universe.json (raw)
  * one WebSocket connection, l2Book {"fast": true} for the top 60 coins by dayNtlVlm, 3 min

Writes: data/universe.json, data/pre_sample.md (sorted by spread/tick), data/coins.json
(the 12 selected coins with reasons). Prints the table. Never places, signs or cancels
anything; never reads .env.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mmbot.precision import tick_at  # read-only import; src/mmbot is never modified

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE = Path("/tmp/opencode/pre_sample_books.json")
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_PAYLOAD = b'{"type":"metaAndAssetCtxs"}'

TOP_N = 60
DURATION_S = 180.0
SELECT_N = 12
RANK_LO, RANK_HI = 8, 50
MIN_TOUCH_USD = 2000.0
PUB = "hl-mm-bot-pre-sample/1.0"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def med(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


# --------------------------------------------------------------------------- universe
def fetch_info(timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(
        INFO_URL,
        data=INFO_PAYLOAD,
        headers={"Content-Type": "application/json", "User-Agent": PUB},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def split_meta(raw: bytes) -> tuple[dict, list]:
    obj = json.loads(raw)
    if isinstance(obj, list) and len(obj) == 2 and isinstance(obj[0], dict) and isinstance(obj[1], list):
        return obj[0], obj[1]
    raise SystemExit(f"unexpected metaAndAssetCtxs shape: {type(obj).__name__}")


def build_rows(meta: dict, ctxs: list) -> tuple[list[dict], dict]:
    """NATIVE perps only: no builder 'dex:coin' names, nothing delisted, needs a mid."""
    uni = meta.get("universe", [])
    counts = {
        "listed": len(uni),
        "delisted": 0,
        "builder_colon": 0,
        "native": 0,
        "native_without_mid": 0,
    }
    rows: list[dict] = []
    for i, u in enumerate(uni):
        name = str(u.get("name", ""))
        if u.get("isDelisted"):
            counts["delisted"] += 1
            continue
        if ":" in name:
            counts["builder_colon"] += 1
            continue
        counts["native"] += 1
        ctx = ctxs[i] if i < len(ctxs) else None
        if not isinstance(ctx, dict) or not ctx.get("midPx"):
            counts["native_without_mid"] += 1
            continue
        rows.append(
            {
                "coin": name,
                "szDecimals": int(u.get("szDecimals", 0)),
                "maxLeverage": u.get("maxLeverage"),
                "dayNtlVlm": float(ctx.get("dayNtlVlm") or 0.0),
                "midPx": float(ctx["midPx"]),
                "markPx": float(ctx.get("markPx") or 0.0),
                "prevDayPx": float(ctx.get("prevDayPx") or 0.0),
            }
        )
    rows.sort(key=lambda r: r["dayNtlVlm"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows, counts


# --------------------------------------------------------------------------- websocket sample
class Sample:
    def __init__(self, url: str, coins: list[str]):
        self.url = url
        self.coins = coins
        self.books: dict[str, list[dict]] = defaultdict(list)
        self.msg_count: Counter = Counter()
        self.replies: list[str] = []
        self.subs_sent: list[str] = []
        self.connected_at: float | None = None
        self.errors: list[str] = []
        self._ws = None
        self._stop_ping = threading.Event()

    def _on_open(self, ws) -> None:
        self.connected_at = time.time()
        for coin in self.coins:
            msg = json.dumps(
                {"method": "subscribe",
                 "subscription": {"type": "l2Book", "coin": coin, "fast": True}},
                separators=(",", ":"),
            )
            self.subs_sent.append(msg)
            ws.send(msg)
            time.sleep(0.02)

    def _on_message(self, _ws, message) -> None:
        try:
            obj = json.loads(message)
            ch = obj.get("channel")
            if ch == "l2Book":
                d = obj.get("data") or {}
                coin = d.get("coin")
                levels = d.get("levels")
                if not coin or not isinstance(levels, list) or len(levels) != 2:
                    return
                bids, asks = levels
                if not bids or not asks:
                    return
                b0, a0 = bids[0], asks[0]
                self.books[coin].append(
                    {
                        "bid_px": float(b0["px"]),
                        "bid_sz": float(b0["sz"]),
                        "ask_px": float(a0["px"]),
                        "ask_sz": float(a0["sz"]),
                        "b1_px": float(bids[1]["px"]) if len(bids) > 1 else None,
                        "b1_sz": float(bids[1]["sz"]) if len(bids) > 1 else None,
                        "a1_px": float(asks[1]["px"]) if len(asks) > 1 else None,
                        "a1_sz": float(asks[1]["sz"]) if len(asks) > 1 else None,
                        "exch_time": d.get("time"),
                    }
                )
                self.msg_count[coin] += 1
            elif ch == "pong":
                pass
            else:
                self.replies.append(message[:600])
        except Exception as exc:  # a malformed message must never kill the sample
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def _on_error(self, _ws, error) -> None:
        self.errors.append(f"ws error: {error!r}")

    def _on_close(self, _ws, status, msg) -> None:
        self.replies.append(f"__CLOSE__ status={status} msg={msg!r}")

    def _ping_loop(self, ws) -> None:
        while not self._stop_ping.wait(50):
            try:
                if ws.sock and ws.sock.connected:
                    ws.send(json.dumps({"method": "ping"}))
            except Exception as exc:
                self.errors.append(f"ping failed: {exc}")
                break

    def run(self, duration: float) -> dict:
        ws = websocket.WebSocketApp(
            self.url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        threading.Thread(target=ws.run_forever, daemon=True).start()
        threading.Thread(target=self._ping_loop, args=(ws,), daemon=True).start()
        started = time.time()
        time.sleep(duration)
        self._stop_ping.set()
        try:
            ws.close()
        except Exception:
            pass
        time.sleep(1.0)
        ended = time.time()
        return {
            "connected_at": self.connected_at,
            "window_start": started,
            "window_end": ended,
            "duration_s": round(ended - started, 2),
            "subscriptions_sent": len(self.subs_sent),
            "coins_with_data": len(self.books),
            "messages_total": sum(self.msg_count.values()),
            "server_replies": self.replies[:40],
            "errors": self.errors[:40],
        }


# --------------------------------------------------------------------------- metrics
def coin_metrics(
    books: list[dict], n_msgs: int, duration_s: float, row: dict
) -> dict | None:
    if not books:
        return None
    spreads, mids, ratios, bps = [], [], [], []
    touches_avg, touches_thin, touches_sum, touches_pool = [], [], [], []
    for b in books:
        bid, ask = b["bid_px"], b["ask_px"]
        if ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        spread = ask - bid
        bid_usd, ask_usd = bid * b["bid_sz"], ask * b["ask_sz"]
        spreads.append(spread)
        mids.append(mid)
        bps.append(spread / mid * 1e4)
        touches_avg.append((bid_usd + ask_usd) / 2.0)
        touches_thin.append(min(bid_usd, ask_usd))
        touches_sum.append(bid_usd + ask_usd)
        touches_pool.extend((bid_usd, ask_usd))
    if not spreads:
        return None
    ref_mid = statistics.median(mids)
    tick = tick_at(ref_mid, row["szDecimals"])
    ratios = [s / tick for s in spreads]
    return {
        "coin": row["coin"],
        "rank": row["rank"],
        "dayNtlVlm": row["dayNtlVlm"],
        "szDecimals": row["szDecimals"],
        "midPx": row["midPx"],
        "msgs": n_msgs,
        "msgs_per_min": round(n_msgs / (duration_s / 60.0), 2),
        "median_spread_bps": round(statistics.median(bps), 4),
        "median_spread_px": round(statistics.median(spreads), 8),
        "tick_px": tick,
        "tick_bps": round(tick / ref_mid * 1e4, 4),
        "median_spread_over_tick": round(statistics.median(ratios), 2),
        "median_touch_usd": round(statistics.median(touches_avg), 2),
        "median_touch_thin_usd": round(statistics.median(touches_thin), 2),
        "median_touch_sum_usd": round(statistics.median(touches_sum), 2),
        "median_touch_pooled_usd": round(statistics.median(touches_pool), 2),
        "books": len(books),
    }


def select_12(ranked: list[dict], touch_key: str = "median_touch_usd") -> tuple[list[dict], dict]:
    """top 3 by volume + HYPE (if listed) + largest-spread coins from ranks 8-50,
    each with median touch >= $2,000; filled to exactly SELECT_N."""
    picked: list[dict] = []
    reasons: dict[str, str] = {}

    def add(row: dict, why: str) -> None:
        if row["coin"] in reasons:
            return
        picked.append(row)
        reasons[row["coin"]] = why

    for r in ranked[:3]:
        add(r, f"top 3 by volume: rank {r['rank']}, dayNtlVlm ${r['dayNtlVlm']:,.0f}")

    hype = next((r for r in ranked if r["coin"] == "HYPE"), None)
    if hype is None:
        reasons["__HYPE__"] = "HYPE not present in the measured universe"
    elif hype["coin"] in reasons:
        reasons["HYPE"] = reasons["HYPE"] + " (HYPE is always included)"
    else:
        add(hype, f"HYPE always included if listed: rank {hype['rank']}, "
                  f"dayNtlVlm ${hype['dayNtlVlm']:,.0f}")

    cands = [
        r for r in ranked
        if RANK_LO <= r["rank"] <= RANK_HI
        and r.get(touch_key) is not None
        and r[touch_key] >= MIN_TOUCH_USD
    ]
    cands.sort(key=lambda r: r["median_spread_bps"], reverse=True)
    skipped_low_touch = [r["coin"] for r in ranked
                         if RANK_LO <= r["rank"] <= RANK_HI
                         and (r.get(touch_key) is None or r[touch_key] < MIN_TOUCH_USD)]
    taken = 0
    for c in cands:
        if len(picked) >= SELECT_N:
            break
        if c["coin"] in reasons:
            continue
        taken += 1
        add(c, f"rank {c['rank']} in {RANK_LO}-{RANK_HI} with the largest median spread "
               f"{c['median_spread_bps']} bps and median touch ${c[touch_key]:,.0f} "
               f">= ${MIN_TOUCH_USD:,.0f}")
    missing_in_range = [
        r["coin"] for r in ranked
        if RANK_LO <= r["rank"] <= RANK_HI and r.get("median_touch_usd") is None
    ]
    meta = {
        "touch_key": touch_key,
        "reasons": reasons,
        "from_ranks_8_50": taken,
        "candidates_with_touch_ge_2000": len(cands),
        "candidates": [c["coin"] for c in cands],
        "excluded_low_touch": skipped_low_touch,
        "missing_data_in_range": missing_in_range,
        "shortfall": max(0, SELECT_N - len(picked)),
    }
    return picked, meta


def fmt_vlm(v: float) -> str:
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="T10 Phase A read-only mainnet pre-sample")
    ap.add_argument("--duration", type=float, default=DURATION_S)
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--from-cache", action="store_true",
                    help="re-analyse the cached sample instead of reconnecting")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"pre-sample start {utcnow()} (read-only mainnet, no keys, no orders)", flush=True)

    raw = fetch_info()
    (DATA_DIR / "universe.json").write_bytes(raw)
    meta, ctxs = split_meta(raw)
    rows, counts = build_rows(meta, ctxs)
    print(f"  universe.json written ({len(raw)} bytes): {counts}", flush=True)
    top = rows[: args.top]
    print(f"  top {len(top)} by dayNtlVlm: {', '.join(r['coin'] for r in top)}", flush=True)

    sample = Sample(WS_URL, [r["coin"] for r in top])
    if args.from_cache and CACHE.is_file():
        cached = json.loads(CACHE.read_text())
        for coin, books in cached["books"].items():
            sample.books[coin] = [
                {"bid_px": b[0], "bid_sz": b[1], "ask_px": b[2], "ask_sz": b[3]}
                for b in books
            ]
            sample.msg_count[coin] = len(sample.books[coin])
        info = cached["info"]
        print(f"  reusing cached sample: {CACHE} "
              f"({info['messages_total']} messages, {info['duration_s']}s)", flush=True)
    else:
        print(f"  subscribing l2Book fast:true x{len(top)} on ONE connection "
              f"for {args.duration:.0f}s …", flush=True)
        info = sample.run(args.duration)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({
            "info": info,
            "books": {c: [[b["bid_px"], b["bid_sz"], b["ask_px"], b["ask_sz"]]
                          for b in books] for c, books in sample.books.items()},
        }))
        print(f"  sample cached -> {CACHE}", flush=True)
    print(f"  collected: {info['messages_total']} messages for "
          f"{info['coins_with_data']}/{len(top)} coins in {info['duration_s']}s", flush=True)
    if info["server_replies"]:
        print(f"  server replies: {len(info['server_replies'])} "
              f"(first: {info['server_replies'][0][:200]})", flush=True)
    if info["errors"]:
        print(f"  errors: {len(info['errors'])} (first: {info['errors'][0][:200]})", flush=True)

    ranked_metrics: list[dict] = []
    no_data: list[str] = []
    for r in rows:  # full native ranking, metrics only where we sampled
        if r["coin"] not in sample.books:
            if r["rank"] <= args.top:
                no_data.append(r["coin"])
            continue
        m = coin_metrics(sample.books[r["coin"]], sample.msg_count[r["coin"]],
                         info["duration_s"], r)
        if m:
            ranked_metrics.append(m)
        else:
            no_data.append(r["coin"])

    by_spread = sorted(
        [m for m in ranked_metrics if m["median_spread_over_tick"] is not None],
        key=lambda m: m["median_spread_over_tick"],
        reverse=True,
    )
    picked, sel_meta = select_12(ranked_metrics, "median_touch_usd")
    sensitivity = {}
    for key, label in (
        ("median_touch_thin_usd", "thinner side only (min per message)"),
        ("median_touch_pooled_usd", "all side samples pooled (median)"),
        ("median_touch_usd", "per-message average of both sides (PRIMARY)"),
        ("median_touch_sum_usd", "both sides summed per message"),
    ):
        alt_picked, alt_meta = select_12(ranked_metrics, key)
        sensitivity[label] = {
            "touch_key": key,
            "candidates_in_range": alt_meta["candidates_with_touch_ge_2000"],
            "selected_n": len(alt_picked),
            "selected": [p["coin"] for p in alt_picked],
            "added_over_primary": [c for c in [p["coin"] for p in alt_picked]
                                   if c not in [p["coin"] for p in picked]],
        }

    # ---- data/pre_sample.md (sorted by spread/tick) ----
    lines: list[str] = []
    lines.append("# Pre-sample — T10 Phase A (read-only mainnet)")
    lines.append("")
    lines.append(f"- generated: {utcnow()}")
    lines.append(f"- source: `POST {INFO_URL}` `{{\"type\":\"metaAndAssetCtxs\"}}` "
                 f"-> `data/universe.json` ({len(raw)} bytes, raw)")
    lines.append(f"- feed: `wss://api.hyperliquid.xyz/ws`, `l2Book {{\"fast\": true}}`, "
                 f"one connection, top {len(top)} by dayNtlVlm, {info['duration_s']} s "
                 f"({info['messages_total']} messages, "
                 f"{info['coins_with_data']}/{len(top)} coins answered)")
    lines.append(f"- universe: {counts['listed']} listed - {counts['delisted']} isDelisted - "
                 f"{counts['builder_colon']} builder `dex:coin` names = "
                 f"**{counts['native']} NATIVE perps** "
                 f"({counts['native_without_mid']} without a mid, not measurable)")
    lines.append("")
    lines.append("## Metric definitions")
    lines.append("")
    lines.append("- **spread (bps)**: median over messages of `(ask - bid) / mid * 1e4`, "
                 "`mid = (bid + ask) / 2`")
    lines.append("- **tick (bps)**: `tick_at(median mid, szDecimals)` from "
                 "`src/mmbot/precision.py`, expressed in bps at that same mid")
    lines.append("- **spread/tick**: median over messages of `(ask - bid) / tick` "
                 "(integer ticks of mispricing at the touch)")
    lines.append("- **msgs/min**: l2Book messages for that coin / window minutes")
    lines.append("- **touch USD (selection metric)**: median over messages of "
                 "`(bid_px * bid_sz + ask_px * ask_sz) / 2` — typical USD resting at the "
                 "touch, both sides")
    lines.append("- **touch thin USD**: median of `min(bid_px * bid_sz, ask_px * ask_sz)` "
                 "(thinner side only) — shown for the sensitivity check below")
    lines.append("- **dayNtlVlm**: trailing 24 h notional volume from `assetCtxs`")
    lines.append("")
    lines.append("## All measured coins, sorted by spread/tick (desc)")
    lines.append("")
    lines.append("| # | coin | rank | spread bps | tick bps | spread/tick | msgs/min "
                 "| touch USD | touch thin | dayNtlVlm |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, m in enumerate(by_spread, start=1):
        lines.append(
            f"| {i} | {m['coin']} | {m['rank']} | {m['median_spread_bps']} "
            f"| {m['tick_bps']} | {m['median_spread_over_tick']} | {m['msgs_per_min']} "
            f"| {m['median_touch_usd']:,.0f} | {m['median_touch_thin_usd']:,.0f} "
            f"| {fmt_vlm(m['dayNtlVlm'])} |"
        )
    lines.append("")
    if no_data:
        lines.append(f"Subscribed but no usable book in the window: {', '.join(no_data)}")
        lines.append("")
    lines.append(f"## Selection ({len(picked)} of {SELECT_N} requested)")
    lines.append("")
    lines.append("| coin | rank | reason |")
    lines.append("|---|---:|---|")
    for p in picked:
        lines.append(f"| {p['coin']} | {p['rank']} | {sel_meta["reasons"].get(p["coin"], "")} |")
    lines.append("")
    lines.append(f"- candidates in ranks {RANK_LO}-{RANK_HI} with median touch >= "
                 f"${MIN_TOUCH_USD:,.0f} (primary reading): "
                 f"{sel_meta['candidates_with_touch_ge_2000']}")
    lines.append(f"- excluded for median touch < ${MIN_TOUCH_USD:,.0f}: "
                 f"{', '.join(sel_meta['excluded_low_touch']) or 'none'}")
    if no_data:
        lines.append(f"- no usable data in the window: {', '.join(no_data)}")
    lines.append("")
    lines.append("## Definition sensitivity for the $2,000 touch floor")
    lines.append("")
    lines.append(f"`median touch size` can be read {len(sensitivity)} ways for a two-sided "
                 "book; the selection above uses the per-message average of both sides. "
                 "What each reading yields from ranks 8-50 (after the same spread ranking):")
    lines.append("")
    lines.append("| touch reading | candidates in ranks 8-50 | coins selected | added vs primary |")
    lines.append("|---|---:|---:|---|")
    for label, d in sensitivity.items():
        lines.append(f"| {label} | {d['candidates_in_range']} | {d['selected_n']} "
                     f"| {', '.join(d['added_over_primary']) or '-'} |")
    lines.append("")
    if sel_meta["shortfall"]:
        lines.append(f"**SHORTFALL: {sel_meta['shortfall']} coin(s) short of {SELECT_N} "
                     f"under the primary reading — needs the human's call at approval.**")
    (DATA_DIR / "pre_sample.md").write_text("\n".join(lines) + "\n")

    # ---- data/coins.json ----
    coins_doc = {
        "generated_utc": utcnow(),
        "phase": "T10 Phase A (pre-sample) — awaiting human approval before Phase B",
        "source": {
            "info_url": INFO_URL,
            "info_payload": json.loads(INFO_PAYLOAD.decode()),
            "universe_file": "data/universe.json",
            "ws_url": WS_URL,
            "subscription": {"type": "l2Book", "fast": True},
            "window_s": info["duration_s"],
            "top_subscribed": len(top),
        },
        "universe_counts": counts,
        "selection_rule": (
            f"top 3 by dayNtlVlm + HYPE (always, if listed) + enough largest-median-spread "
            f"coins from ranks {RANK_LO}-{RANK_HI} with median touch "
            f">= ${MIN_TOUCH_USD:,.0f}, deduplicated and filled to {SELECT_N} "
            f"(selection is over the coins actually measured)"
        ),
        "selection_meta": sel_meta,
        "touch_definition_sensitivity": sensitivity,
        "no_data": no_data,
        "server_replies": info["server_replies"],
        "errors": info["errors"],
        "coins": [
            {
                "coin": p["coin"],
                "rank": p["rank"],
                "reason": sel_meta["reasons"].get(p["coin"], ""),
                "metrics": p,
            }
            for p in picked
        ],
    }
    (DATA_DIR / "coins.json").write_text(json.dumps(coins_doc, indent=2) + "\n")

    # ---- table for the human ----
    print()
    print(f"=== measured {len(by_spread)} coins, sorted by spread/tick "
          f"({info['duration_s']}s window) ===")
    print(f"{'coin':<12}{'rank':>5} {'spread_bps':>11} {'tick_bps':>9} "
          f"{'spr/tick':>9} {'msgs/min':>9} {'touchUSD':>11} {'thinUSD':>10} "
          f"{'dayNtlVlm':>10}")
    for m in by_spread:
        print(f"{m['coin']:<12}{m['rank']:>5} {m['median_spread_bps']:>11} "
              f"{m['tick_bps']:>9} {m['median_spread_over_tick']:>9} "
              f"{m['msgs_per_min']:>9} {m['median_touch_usd']:>11,.0f} "
              f"{m['median_touch_thin_usd']:>10,.0f} "
              f"{fmt_vlm(m['dayNtlVlm']):>10}")
    print()
    print("=== touch-definition sensitivity ($2,000 floor, ranks 8-50) ===")
    for label, d in sensitivity.items():
        print(f"  {label:<42} candidates={d['candidates_in_range']:>3} "
              f"selected={d['selected_n']:>3}  {', '.join(d['selected'])}")
    print()
    print(f"=== SELECTED {len(picked)} coins (data/coins.json) ===")
    for p in picked:
        print(f"  {p['coin']:<10} rank {p['rank']:>3}  {sel_meta["reasons"].get(p["coin"], "")}")
    print()
    print("wrote data/universe.json, data/pre_sample.md, data/coins.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
