#!/usr/bin/env python3
"""Read-only HYPE deep-book wall scanner + whale/vault tracker.

Never sends orders and never imports the trading loop.  Uses only public
Hyperliquid endpoints plus the public leaderboard/stats endpoint.

Usage:
    PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/whale_and_book_scanner.py
    ... --leadlag-seconds 45 --top-wallets 12
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from hyperliquid.info import Info

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "whale_book_diagnostic_20261107.md"
JSON_OUT = ROOT / "logs" / "whale_book_diagnostic_20261107.json"

COIN = "HYPE"
HLP = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
BINANCE_FAPI = "https://fapi.binance.com"
TICK = 0.001  # HYPE pxDecimals = 3

INFO = Info("https://api.hyperliquid.xyz", skip_ws=True, timeout=12)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _pct(v: Optional[float], n: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{n}f}"


def _bbo(book: Dict[str, Any]) -> Tuple[float, float, float]:
    bids, asks = book["levels"][0], book["levels"][1]
    bb, ba = _f(bids[0]["px"]), _f(asks[0]["px"])
    return bb, ba, (bb + ba) / 2.0


def fetch_book(sig: Optional[int]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"type": "l2Book", "coin": COIN}
    if sig is not None:
        payload["nSigFigs"] = sig
    return INFO.post("/info", payload)


def analyse_book(sig: Optional[int]) -> Dict[str, Any]:
    book = fetch_book(sig)
    bids = book["levels"][0]
    asks = book["levels"][1]
    bb, ba, mid = _bbo(book)
    sizes = [_f(l["sz"]) for l in bids] + [_f(l["sz"]) for l in asks]
    median_sz = sorted(sizes)[len(sizes) // 2] if sizes else 0.0

    def walls(levels: List[Dict[str, Any]], side: str) -> List[Dict[str, Any]]:
        out = []
        for l in levels:
            px, sz, n = _f(l["px"]), _f(l["sz"]), int(l.get("n") or 0)
            if n >= 5 and sz >= 3.0 * median_sz and sz > 0:
                out.append({
                    "side": side, "px": px, "sz": sz, "n": n,
                    "wall_score": sz * math.log1p(n),
                    "dist_bps": abs(mid - px) / mid * 1e4,
                })
        return out

    bw = walls(bids, "bid")
    aw = walls(asks, "ask")
    closest_bid = min(bw, key=lambda w: w["dist_bps"]) if bw else None
    closest_ask = min(aw, key=lambda w: w["dist_bps"]) if aw else None
    top_score = max(bw + aw, key=lambda w: w["wall_score"]) if (bw or aw) else None

    def front_run(w: Optional[Dict[str, Any]]) -> Optional[float]:
        if not w:
            return None
        return w["px"] + TICK if w["side"] == "bid" else w["px"] - TICK

    top5_b = sum(_f(l["sz"]) for l in bids[:5])
    top5_a = sum(_f(l["sz"]) for l in asks[:5])
    obi = (top5_b - top5_a) / (top5_b + top5_a) if (top5_b + top5_a) else None

    return {
        "sig": "default/5" if sig is None else str(sig),
        "time": book.get("time"),
        "bbo": [bb, ba],
        "mid": mid,
        "spread_bps": (ba - bb) / mid * 1e4,
        "n_levels": [len(bids), len(asks)],
        "bid_span_bps": (mid - _f(bids[-1]["px"])) / mid * 1e4,
        "ask_span_bps": (_f(asks[-1]["px"]) - mid) / mid * 1e4,
        "median_level_sz": median_sz,
        "obi_top5_sz": obi,
        "walls": {"bid": bw, "ask": aw},
        "closest_bid_wall": closest_bid,
        "closest_ask_wall": closest_ask,
        "top_score_wall": top_score,
        "front_run_bid": front_run(closest_bid),
        "front_run_ask": front_run(closest_ask),
    }


def discover_wallets(top_n: int) -> List[Dict[str, Any]]:
    tracked: Dict[str, Dict[str, Any]] = {
        HLP: {"address": HLP, "label": "HLP vault", "source": "known vault"},
    }
    try:
        rows = requests.get(LEADERBOARD, timeout=25).json().get("leaderboardRows", [])
        rows.sort(key=lambda r: _f(r.get("accountValue")), reverse=True)
        for r in rows[:top_n]:
            a = str(r.get("ethAddress", "")).lower()
            if not a:
                continue
            tracked.setdefault(a, {
                "address": a, "label": r.get("displayName") or "leaderboard",
                "source": "leaderboard", "account_value": _f(r.get("accountValue")),
            })
    except Exception as exc:
        print(f"[warn] leaderboard: {type(exc).__name__}: {exc}")

    try:
        trades = INFO.post("/info", {"type": "recentTrades", "coin": COIN}) or []
        vol: Dict[str, float] = {}
        for t in trades:
            notional = _f(t.get("px")) * _f(t.get("sz"))
            for u in (t.get("users") or []):
                a = str(u).lower()
                vol[a] = vol.get(a, 0.0) + notional
        for a, v in sorted(vol.items(), key=lambda kv: kv[1], reverse=True)[:10]:
            tracked.setdefault(a, {
                "address": a, "label": "recent large trader",
                "source": "recentTrades", "recent_notional": v,
            })
    except Exception as exc:
        print(f"[warn] recentTrades: {type(exc).__name__}: {exc}")
    return list(tracked.values())


def label_vault(addr: str) -> Optional[str]:
    try:
        d = INFO.post("/info", {"type": "vaultDetails", "vaultAddress": addr})
        return d.get("name") if isinstance(d, dict) else None
    except Exception:
        return None


def scan_wallets(top_n: int) -> Dict[str, Any]:
    wallets = discover_wallets(top_n)
    mid = _bbo(fetch_book(None))[2]
    long_n = short_n = 0.0
    rows: List[Dict[str, Any]] = []
    resting: List[Dict[str, Any]] = []
    for w in wallets:
        a = w["address"]
        if w.get("source") == "leaderboard":
            name = label_vault(a)
            if name:
                w["label"] = f"vault: {name}"
        pos_n = 0.0
        szi = 0.0
        upnl = 0.0
        try:
            us = INFO.user_state(a)
            for p in us.get("assetPositions", []):
                pos = p.get("position", {})
                if pos.get("coin") != COIN:
                    continue
                szi = _f(pos.get("szi"))
                pos_n = szi * mid
                upnl = _f(pos.get("unrealizedPnl"))
        except Exception:
            pass
        if pos_n > 0:
            long_n += pos_n
        elif pos_n < 0:
            short_n += -pos_n
        orders = []
        try:
            for o in INFO.frontend_open_orders(a):
                if o.get("coin") != COIN:
                    continue
                item = {
                    "wallet": w.get("label") or a,
                    "address": a,
                    "side": "bid" if o.get("side") == "B" else "ask",
                    "px": _f(o.get("limitPx")),
                    "sz": _f(o.get("sz")),
                    "oid": o.get("oid"),
                    "reduce_only": bool(o.get("reduceOnly")),
                }
                orders.append(item)
                resting.append(item)
        except Exception:
            pass
        rows.append({
            **w, "hype_szi": szi, "hype_notional": pos_n,
            "hype_upnl": upnl, "resting_orders": orders,
            "tracked": bool(pos_n or orders),
        })
    rows.sort(key=lambda r: abs(r["hype_notional"]), reverse=True)
    resting.sort(key=lambda o: o["sz"] * o["px"], reverse=True)
    return {
        "mid": mid,
        "wallets": rows,
        "resting_orders": resting,
        "long_notional": long_n,
        "short_notional": short_n,
        "net_bias_notional": long_n - short_n,
        "bias_ratio": (long_n - short_n) / (long_n + short_n) if (long_n + short_n) else None,
    }


def asset_ctx() -> Dict[str, Any]:
    meta, ctxs = INFO.meta_and_asset_ctxs()
    names = [u.get("name") for u in meta.get("universe", [])]
    idx = names.index(COIN)
    c = ctxs[idx]
    out = {
        "oraclePx": _f(c.get("oraclePx")),
        "markPx": _f(c.get("markPx")),
        "midPx": _f(c.get("midPx")),
        "funding": _f(c.get("funding")),
        "openInterest": _f(c.get("openInterest")),
        "dayNtlVlm": _f(c.get("dayNtlVlm")),
        "premium": _f(c.get("premium")),
    }
    if out["oraclePx"]:
        out["oracle_mark_divergence_bps"] = (out["markPx"] - out["oraclePx"]) / out["oraclePx"] * 1e4
        out["mark_mid_divergence_bps"] = (out["markPx"] - out["midPx"]) / out["midPx"] * 1e4 if out["midPx"] else None
    else:
        out["oracle_mark_divergence_bps"] = None
        out["mark_mid_divergence_bps"] = None
    return out


def _corr(a: List[float], b: List[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 5:
        return None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def lead_lag(seconds: int) -> Dict[str, Any]:
    symbol = None
    try:
        ex = requests.get(f"{BINANCE_FAPI}/fapi/v1/exchangeInfo", timeout=20).json()
        syms = {s.get("symbol") for s in ex.get("symbols", [])}
        symbol = "HYPEUSDT" if "HYPEUSDT" in syms else ("BTCUSDT" if "BTCUSDT" in syms else None)
    except Exception as exc:
        return {"error": f"binance exchangeInfo: {type(exc).__name__}"}
    if not symbol:
        return {"error": "no binance futures symbol"}
    rows = []
    for _ in range(max(10, int(seconds))):
        try:
            br = requests.get(f"{BINANCE_FAPI}/fapi/v1/ticker/price",
                              params={"symbol": symbol}, timeout=8).json()
            bp = _f(br.get("price"))
            hl = INFO.post("/info", {"type": "allMids"}) or {}
            hp = _f(hl.get(COIN))
            if bp and hp:
                rows.append((time.time(), bp, hp))
        except Exception:
            pass
        time.sleep(1.0)
    if len(rows) < 8:
        return {"symbol": symbol, "error": "insufficient samples", "samples": len(rows)}
    rb = [math.log(rows[i][1] / rows[i - 1][1]) for i in range(1, len(rows))]
    rh = [math.log(rows[i][2] / rows[i - 1][2]) for i in range(1, len(rows))]
    lags = {}
    for lag in range(0, 5):
        lags[str(lag)] = _corr(rb[:len(rb) - lag] if lag else rb,
                               rh[lag:] if lag else rh)
    # overlapping 5-second returns
    r5b = [math.log(rows[i][1] / rows[i - 5][1]) for i in range(5, len(rows))]
    r5h = [math.log(rows[i][2] / rows[i - 5][2]) for i in range(5, len(rows))]
    lags5 = {}
    for lag in range(0, 4):
        lags5[str(lag)] = _corr(r5b[:len(r5b) - lag] if lag else r5b,
                                r5h[lag:] if lag else r5h)
    best1 = max(((k, v) for k, v in lags.items() if v is not None),
                key=lambda kv: abs(kv[1]), default=(None, None))
    best5 = max(((k, v) for k, v in lags5.items() if v is not None),
                key=lambda kv: abs(kv[1]), default=(None, None))
    return {
        "symbol": symbol, "samples": len(rows),
        "corr_1s_by_lag": lags, "corr_5s_by_lag": lags5,
        "best_1s": {"lag_s": best1[0], "corr": best1[1]},
        "best_5s": {"lag_s": best5[0], "corr": best5[1]},
    }


def _fmt_wall(w: Optional[Dict[str, Any]], tick: float) -> str:
    if not w:
        return "none"
    fr = w["px"] + tick if w["side"] == "bid" else w["px"] - tick
    return (f"{w['side']} {w['px']:.3f} × {w['sz']:.2f} "
            f"(n={w['n']}, score {w['wall_score']:.0f}, {w['dist_bps']:.2f} bps, "
            f"front-run {fr:.3f})")


def build_report(books: Dict[str, Any], whales: Dict[str, Any],
                 ctx: Dict[str, Any], ll: Dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# HYPE Deep-Book & Whale Diagnostic — 2026-11-07",
        "",
        f"**Generated:** {now} · read-only mainnet · coin `{COIN}`",
        "",
        "## 1. Multi-resolution L2 books",
        "",
        "| Resolution | BBO | Spread | Levels | Bid span | Ask span | Median level sz | OBI top5 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for sig in ("default/5", "4", "3"):
        b = books[sig]
        lines.append(
            f"| `nSigFigs={sig}` | {b['bbo'][0]:.3f} / {b['bbo'][1]:.3f} | "
            f"{b['spread_bps']:.3f} bps | {b['n_levels'][0]}/{b['n_levels'][1]} | "
            f"{b['bid_span_bps']:.2f} bps | {b['ask_span_bps']:.2f} bps | "
            f"{b['median_level_sz']:.2f} | {_pct(b['obi_top5_sz']*100 if b['obi_top5_sz'] is not None else None)}% |")
    lines += ["", "### Verified anti-spoofing walls (`n >= 5`, `sz >= 3x median`)", ""]
    for sig in ("4", "3"):
        b = books[sig]
        lines += [
            f"**nSigFigs={sig}**",
            f"* Closest bid wall: {_fmt_wall(b['closest_bid_wall'], TICK)}",
            f"* Closest ask wall: {_fmt_wall(b['closest_ask_wall'], TICK)}",
            f"* Largest wall score: {_fmt_wall(b['top_score_wall'], TICK)}",
            f"* Total verified bid/ask walls: {len(b['walls']['bid'])}/{len(b['walls']['ask'])}",
            "",
        ]
    lines += [
        "## 2. Whale / vault wallets",
        "",
        f"Tracked wallets: **{len(whales['wallets'])}** · HYPE mid `{whales['mid']:.4f}`",
        "",
        f"* Long HYPE notional: **${whales['long_notional']:.2f}**",
        f"* Short HYPE notional: **${whales['short_notional']:.2f}**",
        f"* Net bias: **${whales['net_bias_notional']:.2f}** "
        f"({_pct(whales['bias_ratio']*100 if whales['bias_ratio'] is not None else None)}% of gross)",
    ]
    shorts = sorted([w for w in whales["wallets"] if w["hype_notional"] < 0],
                    key=lambda w: w["hype_notional"])[:2]
    if shorts:
        tot_short = sum(-w["hype_notional"] for w in shorts)
        lines += ["",
                  "**Concentration note:** the two largest short wallets account for "
                  f"`${tot_short:,.0f}` of the gross short notional "
                  f"({tot_short / max(whales['short_notional'], 1e-9) * 100:.1f}%). "
                  "Treat the net bias as whale-flow evidence, not a clean directional signal."]
    hlp = next((w for w in whales["wallets"] if w["address"].lower() == HLP), None)
    if hlp is not None:
        lines += ["",
                  f"**HLP vault** (`{HLP}`): HYPE perp size `{hlp['hype_szi']:+.3f}`, "
                  f"notional `${hlp['hype_notional']:+.2f}`, "
                  f"resting HYPE orders `{len(hlp['resting_orders'])}`. "
                  "HLP is a multi-strategy market-making/liquidation vault; it may hold "
                  "spot/other perp exposure outside HYPE."]
    lines += [
        "",
        "| Wallet | Source | HYPE size | Notional | uPnL | Resting HYPE orders |",
        "|---|---|---|---|---|---|",
    ]
    for w in whales["wallets"][:20]:
        oo = ", ".join(f"{o['side']} {o['px']:.3f}x{o['sz']:.2f}"
                       for o in w["resting_orders"][:4]) or "—"
        lines.append(
            f"| `{w['address'][:10]}…` {w.get('label') or ''} | {w.get('source')} | "
            f"{w['hype_szi']:+.3f} | ${w['hype_notional']:+.2f} | "
            f"${w['hype_upnl']:+.2f} | {oo} |")
    lines += ["", "### All tracked resting HYPE limit orders", ""]
    if whales["resting_orders"]:
        for o in whales["resting_orders"][:40]:
            lines.append(f"* {o['wallet']} · **{o['side']} {o['px']:.3f} × {o['sz']:.2f}** "
                         f"(notional ${o['px']*o['sz']:.2f}, reduceOnly={o['reduce_only']})")
    else:
        lines.append("* None found.")
    lines += [
        "",
        "## 3. Oracle / mark / funding",
        "",
        f"* oraclePx: `{ctx['oraclePx']:.5f}`",
        f"* markPx: `{ctx['markPx']:.5f}`",
        f"* midPx: `{ctx['midPx']:.5f}`",
        f"* **oracle–mark divergence: `{_pct(ctx['oracle_mark_divergence_bps'])} bps`**",
        f"* mark–mid divergence: `{_pct(ctx['mark_mid_divergence_bps'])} bps`",
        f"* funding: `{ctx['funding']:.8f}`",
        f"* open interest: `{ctx['openInterest']:.2f}` HYPE",
        f"* 24h notional volume: `${ctx['dayNtlVlm']:.0f}`",
        "",
        "## 4. Binance futures lead-lag",
        "",
    ]
    if ll.get("error"):
        lines.append(f"* {ll['error']}")
    else:
        lines += [
            f"* Symbol: **{ll['symbol']}** · live samples: {ll['samples']} × 1s",
            f"* 1s returns, corr by Binance lead (s): `{ll['corr_1s_by_lag']}`",
            f"* 5s returns, corr by Binance lead (s): `{ll['corr_5s_by_lag']}`",
            f"* Best 1s: lag **{ll['best_1s']['lag_s']}s**, corr `{_pct(ll['best_1s']['corr'], 3)}`",
            f"* Best 5s: lag **{ll['best_5s']['lag_s']}s**, corr `{_pct(ll['best_5s']['corr'], 3)}`",
            "",
            "Live sampling is indicative only (short window); use for directional sanity, not statistical proof.",
        ]
    lines += [
        "",
        "## 5. Method & limitations",
        "",
        "* `nSigFigs=4` aggregates HYPE into $0.01 buckets; `nSigFigs=3` into $0.10 buckets.",
        "* Wall score = `sz * log1p(n)`; a level must have `n >= 5` independent orders and",
        "  `sz >= 3x` the median level size across both sides to qualify, filtering single-order spoofs.",
        "* Whale sample = HLP + top public leaderboard accounts + largest recent HYPE trade counterparties.",
        "* `frontendOpenOrders` / `clearinghouseState` are public read-only endpoints.",
        "* Lead-lag uses live 1s sampling and is exploratory; no execution is performed.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-wallets", type=int, default=12)
    ap.add_argument("--leadlag-seconds", type=int, default=40)
    args = ap.parse_args()

    print("[1/4] multi-resolution books ...", flush=True)
    books = {sig: analyse_book(None if sig == "default/5" else int(sig))
             for sig in ("default/5", "4", "3")}
    print("[2/4] whale wallets ...", flush=True)
    whales = scan_wallets(args.top_wallets)
    print("[3/4] oracle / funding ...", flush=True)
    ctx = asset_ctx()
    print("[4/4] binance lead-lag ...", flush=True)
    ll = lead_lag(args.leadlag_seconds)

    report = build_report(books, whales, ctx, ll)
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(report)
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps({
        "generated": time.time(), "books": books, "whales": whales,
        "asset_ctx": ctx, "lead_lag": ll,
    }, indent=2, default=str))

    print("\n=== HYPE DEEP BOOK / WHALES ===")
    for sig in ("default/5", "4", "3"):
        b = books[sig]
        print(f"sig={sig:<9} BBO {b['bbo'][0]:.3f}/{b['bbo'][1]:.3f} "
              f"span {b['bid_span_bps']:.2f}/{b['ask_span_bps']:.2f} bps "
              f"levels {b['n_levels']}")
    for sig in ("4", "3"):
        b = books[sig]
        print(f"  sig={sig} closest bid wall:", _fmt_wall(b["closest_bid_wall"], TICK))
        print(f"  sig={sig} closest ask wall:", _fmt_wall(b["closest_ask_wall"], TICK))
    print(f"whales: long ${whales['long_notional']:.2f} short ${whales['short_notional']:.2f} "
          f"net ${whales['net_bias_notional']:.2f} bias {_pct(whales['bias_ratio']*100 if whales['bias_ratio'] is not None else None)}%")
    print("resting whale HYPE orders:")
    for o in whales["resting_orders"][:12]:
        print(f"  {o['wallet']} {o['side']} {o['px']:.3f} x {o['sz']:.2f}")
    print(f"oracle-mark divergence {_pct(ctx['oracle_mark_divergence_bps'])} bps | "
          f"funding {ctx['funding']:.8f} | OI {ctx['openInterest']:.0f}")
    print(f"lead-lag {ll.get('symbol')}: best1s {ll.get('best_1s')} best5s {ll.get('best_5s')}")
    print(f"\nwrote {DOC}")


if __name__ == "__main__":
    main()
