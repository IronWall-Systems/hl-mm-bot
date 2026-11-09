#!/usr/bin/env python3
"""Live verification of the execution-latency optimisations (mainnet, real orders).

Per cycle:
  1. ONE ``bulk_orders`` action places a 2-leg deep-OTM ``Alo`` pair (~$11.5 each);
  2. ONE ``batchModify`` action reprices both legs (oid rotation is asserted);
  3. ONE ``bulk_cancel`` action removes both legs.

All prices are far from the book (BUY at 50% of mid, SELL at 150%) and post-only,
so they cannot cross or fill.  A final sweep asserts ``open_orders == []``.

Usage
-----
    PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/verify_execution_optimizations.py
    ... --cycles 5 --coin BTC --min-modify-p50-ms 0

The script loads ``/root/hl-mm-bot/.env`` (or ``./.env``) and refuses to run unless
``HL_ENV=mainnet`` and the mainnet confirmation phrase are present.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / ".env", Path("/root/hl-mm-bot/.env")):
    if candidate.exists():
        load_dotenv(candidate, override=False)

from mmbot.config import load_settings                     # noqa: E402
from mmbot.exchange.client import HyperliquidGateway       # noqa: E402
from mmbot.exchange.types import PlaceKind                 # noqa: E402
from mmbot.models import Side                              # noqa: E402

TARGET_NOTIONAL = 11.5
MIN_NOTIONAL = 10.0


def percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] * (hi - k) + s[hi] * (k - lo)


def round_price(px: float, sz_decimals: int, rounding) -> float:
    max_dec = max(0, 6 - int(sz_decimals))
    d = Decimal(str(px))
    sig_dec = 4 - d.adjusted()
    dp = min(max_dec, max(0, sig_dec))
    return float(d.quantize(Decimal(1).scaleb(-dp), rounding=rounding))


def round_size(sz: float, sz_decimals: int, px: float) -> float:
    step = Decimal(1).scaleb(-int(sz_decimals))
    s = float(Decimal(str(sz)).quantize(step, rounding=ROUND_UP))
    for _ in range(20):
        if s * px >= MIN_NOTIONAL:
            return s
        s = float(Decimal(str(s)) + step)
    raise RuntimeError("could not size order above the exchange minimum")


def new_cloid() -> str:
    return "0x" + uuid.uuid4().hex


async def open_oids(gw: HyperliquidGateway, coin: str) -> List[int]:
    return [r.oid for r in await gw.open_orders() if r.coin == coin]


def dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, separators=(",", ":"))
    except Exception:                                   # noqa: BLE001
        return repr(obj)


async def run(cycles: int, coin: str, min_modify_p50_ms: float) -> int:
    settings = load_settings()
    if settings.env != "mainnet":
        print(f"REFUSING: HL_ENV must be mainnet for this run (got {settings.env!r})", flush=True)
        return 2
    if not settings.account_address or not settings.api_secret:
        print("REFUSING: missing HL_ACCOUNT_ADDRESS / HL_API_SECRET", flush=True)
        return 2

    gw = HyperliquidGateway(settings.base_url, settings.account_address, settings.api_secret,
                            coin, dry_run=False, timeout=10.0)
    await gw.start()
    rows: List[Dict[str, Any]] = []
    failures: List[str] = []
    try:
        if gw.spec is None:
            raise RuntimeError("asset spec not loaded")
        sz_dec = gw.spec.sz_decimals
        print(f"coin={coin} sz_decimals={sz_dec} notional=${TARGET_NOTIONAL:.2f} "
              f"cycles={cycles}", flush=True)
        bank_before = int(len(await asyncio.to_thread(gw.info.user_fills, settings.account_address)))
        leftovers = await open_oids(gw, coin)
        if leftovers:
            print(f"pre-existing open orders on {coin}: {leftovers}; cancelling first", flush=True)
            await gw.cancel_oids(leftovers)

        for i in range(cycles):
            snap = await gw.book_snapshot()
            if snap is None:
                failures.append(f"cycle {i}: no book snapshot")
                break
            mid = 0.5 * (snap.bid + snap.ask)
            buy_px = round_price(mid * 0.50, sz_dec, ROUND_DOWN)
            sell_px = round_price(mid * 1.50, sz_dec, ROUND_UP)
            buy_sz = round_size(TARGET_NOTIONAL / buy_px, sz_dec, buy_px)
            sell_sz = round_size(TARGET_NOTIONAL / sell_px, sz_dec, sell_px)
            rec: Dict[str, Any] = {"cycle": i, "mid": mid,
                                   "buy_px": buy_px, "sell_px": sell_px,
                                   "buy_sz": buy_sz, "sell_sz": sell_sz}
            cloids = [new_cloid(), new_cloid()]
            legs = [
                {"side": Side.BUY, "price": buy_px, "size": buy_sz, "tif": "Alo", "cloid": cloids[0]},
                {"side": Side.SELL, "price": sell_px, "size": sell_sz, "tif": "Alo", "cloid": cloids[1]},
            ]

            t0 = time.monotonic()
            placed = await gw.bulk_place(legs)
            rec["place_ms"] = (time.monotonic() - t0) * 1e3
            rec["place"] = [{"kind": r.kind.value, "oid": r.oid, "error": r.error} for r in placed]
            if not all(r.kind is PlaceKind.RESTING and r.oid for r in placed):
                failures.append(f"cycle {i}: bulk place failed: {dump(rec['place'])}")
                break
            old_oids = [r.oid for r in placed]

            # Reprice both legs (small move; still deep OTM).  batchModify cancels
            # the old oid and returns a new one.
            move = 1.0 + 0.001
            new_buy_px = round_price(buy_px * move, sz_dec, ROUND_DOWN)
            new_sell_px = round_price(sell_px / move, sz_dec, ROUND_UP)
            mods = [
                {"oid": old_oids[0], "side": Side.BUY, "price": new_buy_px,
                 "size": buy_sz, "tif": "Alo", "cloid": cloids[0]},
                {"oid": old_oids[1], "side": Side.SELL, "price": new_sell_px,
                 "size": sell_sz, "tif": "Alo", "cloid": cloids[1]},
            ]
            t0 = time.monotonic()
            modified = await gw.modify_quotes(mods)
            rec["modify_ms"] = (time.monotonic() - t0) * 1e3
            rec["modify"] = [{"kind": r.kind.value, "oid": r.oid, "error": r.error}
                             for r in modified]
            if not all(r.kind is PlaceKind.RESTING and r.oid for r in modified):
                failures.append(f"cycle {i}: batchModify failed: {dump(rec['modify'])}")
                # Best effort cleanup of whatever still rests.
                await gw.cancel_oids(await open_oids(gw, coin))
                break
            new_oids = [r.oid for r in modified]
            rec["old_oids"], rec["new_oids"] = old_oids, new_oids
            if set(old_oids) & set(new_oids) or old_oids == new_oids:
                failures.append(f"cycle {i}: oids did not rotate: {old_oids} -> {new_oids}")

            t0 = time.monotonic()
            cancelled = await gw.cancel_oids(new_oids)
            rec["cancel_ms"] = (time.monotonic() - t0) * 1e3
            rec["cancel"] = [{"ok": r.ok, "already_gone": r.already_gone, "error": r.error}
                             for r in cancelled]
            if not all(r.ok for r in cancelled):
                failures.append(f"cycle {i}: bulk cancel failed: {dump(rec['cancel'])}")

            remaining = await open_oids(gw, coin)
            if remaining:
                failures.append(f"cycle {i}: orders still resting after cancel: {remaining}")
                await gw.cancel_oids(remaining)
            rec["remaining_after"] = remaining
            rows.append(rec)
            print(f"cycle {i}: place {rec['place_ms']:.1f}ms  modify {rec['modify_ms']:.1f}ms  "
                  f"cancel {rec['cancel_ms']:.1f}ms  oids {old_oids}->{new_oids}", flush=True)
            await asyncio.sleep(0.25)

        bank_after = int(len(await asyncio.to_thread(gw.info.user_fills, settings.account_address)))
        if bank_after != bank_before:
            failures.append(f"fills appeared during the run: {bank_before} -> {bank_after}")
        final_open = await open_oids(gw, coin)
        if final_open:
            failures.append(f"final open_orders not empty: {final_open}")
            await gw.cancel_oids(final_open)

        place_ms = [r["place_ms"] for r in rows]
        modify_ms = [r["modify_ms"] for r in rows]
        cancel_ms = [r["cancel_ms"] for r in rows]
        summary = {
            "utc": datetime.now(timezone.utc).isoformat(),
            "coin": coin, "cycles": len(rows),
            "place": {"n": len(place_ms), "p50": percentile(place_ms, 50),
                      "p95": percentile(place_ms, 95)},
            "modify": {"n": len(modify_ms), "p50": percentile(modify_ms, 50),
                       "p95": percentile(modify_ms, 95)},
            "cancel": {"n": len(cancel_ms), "p50": percentile(cancel_ms, 50),
                       "p95": percentile(cancel_ms, 95)},
            "fill_count_before": bank_before, "fill_count_after": bank_after,
            "final_open_orders": final_open,
            "failures": failures, "rows": rows,
        }
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        out = logs / f"execution_optimizations_live_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps(summary, indent=2, default=str))
        print("\n=== summary ===")
        for name in ("place", "modify", "cancel"):
            s = summary[name]
            print(f"{name:7s}: n={s['n']} p50={s['p50'] or float('nan'):.1f}ms "
                  f"p95={s['p95'] or float('nan'):.1f}ms")
        print(f"fills: {bank_before} -> {bank_after}  final_open_orders={final_open}")
        print(f"json: {out}")
        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        if min_modify_p50_ms and summary["modify"]["p50"] is not None \
                and summary["modify"]["p50"] < min_modify_p50_ms:
            print(f"\nFAIL: modify p50 {summary['modify']['p50']:.1f}ms "
                  f"< required {min_modify_p50_ms:.1f}ms")
            return 1
        print("\nPASS: 2-leg bulk place -> batchModify -> bulk cancel, oids rotated, book clean.")
        return 0
    finally:
        try:
            leftover = await open_oids(gw, coin)
            if leftover:
                print(f"cleanup: cancelling {leftover}", flush=True)
                await gw.cancel_oids(leftover)
        except Exception as exc:                        # noqa: BLE001
            print(f"cleanup failed: {exc!r}", flush=True)
        try:
            await gw.stop()
        except Exception:                               # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--coin", default=os.environ.get("HL_COIN", "BTC"))
    ap.add_argument("--min-modify-p50-ms", type=float, default=0.0,
                    help="optional floor for the modify p50 (0 disables)")
    args = ap.parse_args()
    return asyncio.run(run(args.cycles, args.coin, args.min_modify_p50_ms))


if __name__ == "__main__":
    sys.exit(main())
