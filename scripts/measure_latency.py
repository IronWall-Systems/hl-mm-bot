#!/usr/bin/env python3
"""Measure real HYPE place/cancel ACK latency from this host (Tokyo VPS).

This script places REAL post-only (Alo) limit BUY orders ~50% below the current
mid, then immediately cancels each by oid.  It is intended for latency
measurement only; it is not a trading strategy.

Why this rewrite exists
-----------------------
The previous version created ``Info(...)`` and ``Exchange(...)`` without a
timeout.  The underlying SDK uses ``requests`` with ``timeout=None``, so any
stalled connection blocks forever right after the first banner line.  It also
ignored ``HL_ACCOUNT_ADDRESS`` (wrong signer/account for API wallets) and used
a 5% offset for the order price.

Safety properties
-----------------
* ``Alo`` post-only, 50% below mid: cannot cross / cannot fill.
* Sized at ~$11.50 notional, above the exchange's $10 minimum.
* Immediately cancelled by oid; a failed cancel is retried once.
* Strict 10 s timeout on every REST/exchange call (configurable).
* Exact exchange response is printed for every rejection.
* No WebSocket threads: ``Info(..., skip_ws=True)``.

Usage
-----
    venv/bin/python scripts/measure_latency.py
    venv/bin/python scripts/measure_latency.py --cycles 30 --sleep 0.3
    venv/bin/python scripts/measure_latency.py --dry-run   # no orders

Output: ``logs/tokyo_ack_latency.json`` plus a terminal summary table.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Dict, List, Optional

import eth_account
from dotenv import load_dotenv
from eth_account.signers.local import LocalAccount
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_LINES: List[str] = []


def log(msg: str = "") -> None:
    """Print immediately (flush) and keep the line for the JSON transcript."""
    line = str(msg)
    LOG_LINES.append(line)
    print(line, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentile(xs: List[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile (same definition as numpy's default)."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def summarize(xs: List[float]) -> Dict[str, Optional[float]]:
    if not xs:
        return {"n": 0, "min": None, "p50": None, "p95": None, "p99": None,
                "max": None, "mean": None}
    return {
        "n": len(xs),
        "min": min(xs),
        "p50": percentile(xs, 50),
        "p95": percentile(xs, 95),
        "p99": percentile(xs, 99),
        "max": max(xs),
        "mean": sum(xs) / len(xs),
    }


# ---------------------------------------------------------------------------
# Hyperliquid price / size formatting
# ---------------------------------------------------------------------------
def round_price(px: float, sz_decimals: int, rounding=ROUND_DOWN) -> float:
    """Round a perp limit price to Hyperliquid's rules.

    Perp prices allow at most 5 significant figures and at most
    ``6 - szDecimals`` decimal places (integer prices are always allowed).
    A BUY is rounded down so it stays further from the market.
    """
    if px <= 0:
        raise ValueError(f"non-positive price: {px}")
    max_dec = max(0, 6 - int(sz_decimals))
    d = Decimal(str(px))
    # Number of decimal places that keeps 5 significant figures.
    sig_dec = 4 - d.adjusted()
    dp = min(max_dec, max(0, sig_dec))
    quantum = Decimal(1).scaleb(-dp)
    return float(d.quantize(quantum, rounding=rounding))


def round_size(sz: float, sz_decimals: int, px: float,
               min_notional: float) -> float:
    """Round size up to the size tick and ensure notional >= the $10 minimum."""
    step = Decimal(1).scaleb(-int(sz_decimals))
    d = Decimal(str(sz)).quantize(step, rounding=ROUND_UP)
    s = float(d)
    # Belt and braces: one tick is usually enough, but loop in case of float
    # representation issues on very small sz_decimals values.
    for _ in range(10):
        if s * px >= min_notional:
            return s
        s = float(Decimal(str(s)) + step)
    return s


# ---------------------------------------------------------------------------
# Exchange response helpers
# ---------------------------------------------------------------------------
def dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return repr(obj)


def order_status(res: Any) -> Dict[str, Any]:
    """Return the first order status dict, or {} if absent."""
    try:
        return res["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return {}


def extract_oid(res: Any) -> Optional[int]:
    st = order_status(res)
    oid = st.get("resting", {}).get("oid") if isinstance(st, dict) else None
    return int(oid) if oid is not None else None


def exchange_error(res: Any) -> Optional[str]:
    """Best-effort extraction of the exchange's exact error message."""
    if not isinstance(res, dict):
        return repr(res)
    if res.get("status") == "err":
        r = res.get("response", res)
        return r if isinstance(r, str) else dump(r)
    st = order_status(res)
    if isinstance(st, dict) and st.get("error"):
        return str(st["error"])
    if res.get("status") != "ok":
        return dump(res)
    return None


# ---------------------------------------------------------------------------
# Clock discipline
# ---------------------------------------------------------------------------
def clock_status() -> Dict[str, Any]:
    for cmd in (["chronyc", "tracking"], ["timedatectl", "timesync-status"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            return {"command": " ".join(cmd), "ok": False, "error": repr(e)}
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        log(f"[clock] {' '.join(cmd)} (exit {p.returncode})")
        if out:
            for line in out.splitlines():
                log(f"[clock]   {line}")
        if err:
            log(f"[clock]   stderr: {err}")
        return {"command": " ".join(cmd), "ok": p.returncode == 0,
                "exit_code": p.returncode, "stdout": out, "stderr": err}
    return {"command": None, "ok": False, "error": "no chronyc/timedatectl"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coin", default="HYPE")
    p.add_argument("--cycles", type=int, default=30)
    p.add_argument("--sleep", type=float, default=0.3,
                   help="seconds between cycles (default 0.3)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="strict HTTP timeout in seconds (default 10)")
    p.add_argument("--notional", type=float, default=11.5,
                   help="target order notional in USD (default 11.5)")
    p.add_argument("--offset-pct", type=float, default=0.50,
                   help="fraction below mid for the BUY (default 0.50 = 50%%)")
    p.add_argument("--min-notional", type=float, default=10.0,
                   help="exchange minimum order notional (default 10.0)")
    p.add_argument("--base-url", default=constants.MAINNET_API_URL)
    p.add_argument("--dry-run", action="store_true",
                   help="fetch meta/l2 and print the order spec, place nothing")
    p.add_argument("--out", default=str(PROJECT_ROOT / "logs" / "tokyo_ack_latency.json"))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    log("=" * 72)
    log(f"Tokyo HYPE place/cancel ACK latency  started {utc_now()}")
    log(f"base_url={args.base_url}  cycles={args.cycles}  sleep={args.sleep:.2f}s  "
        f"timeout={args.timeout:.1f}s")
    log("=" * 72)

    # ---- credentials ------------------------------------------------------
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
        log(f"[env] loaded {env_path}")
    else:
        log(f"[env] WARNING: {env_path} not found; using process environment")

    secret = os.environ.get("HL_API_SECRET")
    account_address = os.environ.get("HL_ACCOUNT_ADDRESS")
    missing = [k for k, v in (("HL_API_SECRET", secret),
                              ("HL_ACCOUNT_ADDRESS", account_address)) if not v]
    if missing:
        log(f"[env] ERROR missing required variables: {', '.join(missing)}")
        return 2

    try:
        account: LocalAccount = eth_account.Account.from_key(secret)
    except Exception as e:  # noqa: BLE001
        log(f"[env] ERROR invalid HL_API_SECRET: {e!r}")
        return 2
    log(f"[env] account_address={account_address}  signer={account.address}  "
        f"secret_loaded=True (value never printed)")

    # ---- SDK clients: strict timeout, no WebSocket threads ---------------
    try:
        info = Info(args.base_url, skip_ws=True, timeout=args.timeout)
        exchange = Exchange(account, args.base_url,
                            account_address=account_address,
                            timeout=args.timeout)
        log(f"[sdk] Info(skip_ws=True, timeout={args.timeout}) and "
            f"Exchange(timeout={args.timeout}) created")
    except Exception as e:  # noqa: BLE001
        log(f"[sdk] ERROR creating clients: {e!r}")
        return 2

    # ---- metadata ---------------------------------------------------------
    try:
        t0 = time.perf_counter()
        meta = info.meta()
        log(f"[meta] info.meta() ok in {(time.perf_counter() - t0) * 1e3:.1f} ms "
            f"({len(meta.get('universe', []))} coins)")
        coin_info = next(c for c in meta["universe"] if c["name"] == args.coin)
    except StopIteration:
        log(f"[meta] ERROR {args.coin} not found in info.meta() universe")
        return 2
    except Exception as e:  # noqa: BLE001
        log(f"[meta] ERROR info.meta() failed: {e!r}")
        log(traceback.format_exc())
        return 2

    sz_decimals = int(coin_info["szDecimals"])
    max_price_decimals = max(0, 6 - sz_decimals)
    log(f"[meta] {args.coin}: szDecimals={sz_decimals} "
        f"max_price_decimals={max_price_decimals} "
        f"max 5 significant figures")

    # ---- read-only account preflight -------------------------------------
    preflight: Dict[str, Any] = {}
    try:
        us = info.user_state(account_address)
        perp_value = float(us.get("marginSummary", {}).get("accountValue", 0.0))
        preflight["perp_account_value_usd"] = perp_value
        preflight["perp_withdrawable_usd"] = float(us.get("withdrawable", 0.0))
        log(f"[preflight] perp accountValue=${perp_value:.4f} "
            f"withdrawable=${preflight['perp_withdrawable_usd']:.4f}")
        if perp_value < args.min_notional:
            log(f"[preflight] WARNING: perp equity is below the "
                f"${args.min_notional:.2f} minimum order notional; an order will "
                f"likely be rejected for insufficient margin")
    except Exception as e:  # noqa: BLE001
        preflight["perp_error"] = repr(e)
        log(f"[preflight] WARNING: user_state failed: {e!r}")
    try:
        spot = info.spot_user_state(account_address)
        bal = {b["coin"]: float(b["total"]) for b in spot.get("balances", [])}
        preflight["spot_balances"] = bal
        if bal:
            log(f"[preflight] spot balances: {dump(bal)}")
    except Exception as e:  # noqa: BLE001
        preflight["spot_error"] = repr(e)
        log(f"[preflight] WARNING: spot_user_state failed: {e!r}")

    # Authorized-signer / account-role checks (read-only).
    try:
        role = info.user_role(account_address)
        preflight["account_role"] = role
        log(f"[preflight] account_role={dump(role)}")
        if role.get("role") != "user":
            log("[preflight] WARNING: HL_ACCOUNT_ADDRESS is not a master/user "
                "account; state reads should use the master address")
    except Exception as e:  # noqa: BLE001
        preflight["role_error"] = repr(e)
        log(f"[preflight] WARNING: user_role failed: {e!r}")
    try:
        agents = info.extra_agents(account_address)
        preflight["approved_agents"] = agents
        signer = account.address.lower()
        approved = [a for a in agents if str(a.get("address", "")).lower() == signer]
        preflight["signer_address"] = account.address
        preflight["signer_is_approved_agent"] = bool(approved)
        log(f"[preflight] signer={account.address} "
            f"approved_agent={bool(approved)}")
        for a in agents:
            log(f"[preflight]   agent name={a.get('name')} "
                f"address={a.get('address')} validUntil={a.get('validUntil')}")
        if not approved:
            log("[preflight] ERROR: the HL_API_SECRET signer is NOT an approved "
                "agent for HL_ACCOUNT_ADDRESS; the exchange will reject orders")
    except Exception as e:  # noqa: BLE001
        preflight["agents_error"] = repr(e)
        log(f"[preflight] WARNING: extra_agents failed: {e!r}")

    # ---- clock discipline -------------------------------------------------
    clock = clock_status()

    # ---- measurement loop -------------------------------------------------
    samples: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    place_lats: List[float] = []          # successful resting ACKs
    rejected_place_lats: List[float] = []  # exchange answered, but rejected
    cancel_lats: List[float] = []
    combined_lats: List[float] = []

    for i in range(1, args.cycles + 1):
        log("")
        log(f"--- cycle {i}/{args.cycles} ---")
        t_cycle = time.perf_counter()
        oid: Optional[int] = None
        cancelled = False
        sample: Dict[str, Any] = {"cycle": i}

        try:
            # Current book / mid
            t_l2 = time.perf_counter()
            l2 = info.l2_snapshot(args.coin)
            l2_ms = (time.perf_counter() - t_l2) * 1e3
            bid = float(l2["levels"][0][0]["px"])
            ask = float(l2["levels"][1][0]["px"])
            mid = (bid + ask) / 2.0
            if not (bid > 0 and ask > 0 and ask >= bid):
                raise ValueError(f"invalid book bid={bid} ask={ask}")
            log(f"[book] l2_snapshot {l2_ms:.1f} ms  bid={bid} ask={ask} mid={mid:.6f}")

            target_px = round_price(mid * (1.0 - args.offset_pct), sz_decimals)
            tick_dp = min(max_price_decimals,
                          max(0, 4 - int(math.floor(math.log10(mid)))))
            price_tick = 10.0 ** (-tick_dp)
            raw_sz = args.notional / target_px
            sz = round_size(raw_sz, sz_decimals, target_px, args.min_notional)
            notional = sz * target_px
            if target_px >= bid:
                raise ValueError(f"BUY target {target_px} is not below best bid {bid}")
            if notional < args.min_notional:
                raise ValueError(f"notional {notional:.4f} below minimum {args.min_notional}")

            sample.update(mid=mid, target_px=target_px, sz=sz, notional=notional,
                          l2_ms=l2_ms, price_tick_at_mid=price_tick,
                          price_decimals=tick_dp)
            log(f"[order] BUY {args.coin} sz={sz} px={target_px} "
                f"notional=${notional:.4f} tif=Alo (post-only, "
                f"{args.offset_pct * 100:.0f}% below mid); "
                f"rules: <=5 sig figs, <={max_price_decimals} dp, "
                f"tick at mid={price_tick:g}")

            if args.dry_run:
                log("[dry-run] not placing order")
                continue

            # ---- place ----
            t0 = time.perf_counter()
            try:
                res = exchange.order(args.coin, True, sz, target_px,
                                     {"limit": {"tif": "Alo"}})
            except Exception as e:  # noqa: BLE001
                dt = (time.perf_counter() - t0) * 1e3
                log(f"[place] EXCEPTION after {dt:.1f} ms: {e!r}")
                log(traceback.format_exc())
                errors.append({"cycle": i, "stage": "place", "error": repr(e),
                               "elapsed_ms": dt})
                sample.update(place_ack_ms=dt, place_error=repr(e))
                samples.append(sample)
                continue
            t1 = time.perf_counter()
            place_ms = (t1 - t0) * 1e3
            sample["place_ack_ms"] = place_ms
            sample["place_response"] = res
            log(f"[place] ACK {place_ms:.1f} ms  raw={dump(res)}")

            err = exchange_error(res)
            oid = extract_oid(res)
            if err is not None or oid is None:
                rejected_place_lats.append(place_ms)
                log(f"[place] REJECTED/UNEXPECTED: {err or 'no resting oid'}")
                errors.append({"cycle": i, "stage": "place",
                               "error": err or "no resting oid",
                               "response": res})
                samples.append(sample)
                continue

            sample["oid"] = oid
            place_lats.append(place_ms)

            # ---- cancel ----
            t2 = time.perf_counter()
            try:
                c_res = exchange.cancel(args.coin, oid)
            except Exception as e:  # noqa: BLE001
                dt = (time.perf_counter() - t2) * 1e3
                log(f"[cancel] EXCEPTION after {dt:.1f} ms: {e!r}")
                log(traceback.format_exc())
                errors.append({"cycle": i, "stage": "cancel", "oid": oid,
                               "error": repr(e), "elapsed_ms": dt})
                sample.update(cancel_ack_ms=dt, cancel_error=repr(e))
                samples.append(sample)
                continue
            t3 = time.perf_counter()
            cancel_ms = (t3 - t2) * 1e3
            sample["cancel_ack_ms"] = cancel_ms
            sample["cancel_response"] = c_res
            log(f"[cancel] ACK {cancel_ms:.1f} ms  oid={oid}  raw={dump(c_res)}")

            cerr = exchange_error(c_res)
            if cerr is not None:
                log(f"[cancel] REJECTED: {cerr}")
                errors.append({"cycle": i, "stage": "cancel", "oid": oid,
                               "error": cerr, "response": c_res})
                samples.append(sample)
                continue

            cancelled = True
            cancel_lats.append(cancel_ms)
            combined = place_ms + cancel_ms
            combined_lats.append(combined)
            sample["combined_ack_ms"] = combined
            samples.append(sample)
            log(f"[cycle] place={place_ms:.1f} ms  cancel={cancel_ms:.1f} ms  "
                f"combined={combined:.1f} ms")

        except Exception as e:  # noqa: BLE001
            log(f"[cycle {i}] ERROR: {e!r}")
            log(traceback.format_exc())
            errors.append({"cycle": i, "stage": "cycle", "error": repr(e)})
            sample["cycle_error"] = repr(e)
            samples.append(sample)

        finally:
            # Best-effort cleanup: never leave a resting order behind.
            if oid is not None and not cancelled and not args.dry_run:
                log(f"[cleanup] cancelling outstanding oid={oid}")
                for attempt in (1, 2):
                    try:
                        c_res = exchange.cancel(args.coin, oid)
                        log(f"[cleanup] attempt {attempt}: {dump(c_res)}")
                        if exchange_error(c_res) is None:
                            cancelled = True
                            break
                    except Exception as e:  # noqa: BLE001
                        log(f"[cleanup] attempt {attempt} exception: {e!r}")
                    time.sleep(0.2)
            # Space every cycle, including rejected/failed ones.  This is in
            # `finally` because the error branches `continue` past the loop tail.
            if i < args.cycles and not args.dry_run:
                time.sleep(max(0.0, args.sleep))

    # ---- summary ----------------------------------------------------------
    place_sum = summarize(place_lats)
    rejected_place_sum = summarize(rejected_place_lats)
    cancel_sum = summarize(cancel_lats)
    combined_sum = summarize(combined_lats)

    log("")
    log("=" * 72)
    log(f"SUMMARY  {utc_now()}")
    log("=" * 72)
    header = f"{'metric':<23}{'n':>5}{'min':>10}{'p50':>10}{'p95':>10}{'p99':>10}{'max':>10}"
    log(header)
    for name, s in (("place_ack_ms", place_sum),
                    ("place_ack_rejected_ms", rejected_place_sum),
                    ("cancel_ack_ms", cancel_sum),
                    ("combined_ack_ms", combined_sum)):
        def f(key):
            v = s[key]
            return "—" if v is None else f"{v:.2f}"
        log(f"{name:<23}{s['n']:>5}{f('min'):>10}{f('p50'):>10}"
            f"{f('p95'):>10}{f('p99'):>10}{f('max'):>10}")
    log(f"errors: {len(errors)}")
    if errors:
        counts: Dict[str, int] = {}
        for e in errors:
            counts[e.get("error", "?")] = counts.get(e.get("error", "?"), 0) + 1
        for msg, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            log(f"  {n}x {msg}")
    if rejected_place_lats and not place_lats:
        log("No successful resting ACKs: cancel latency could not be measured.")
        log("The exchange answered every place with the rejection above; check "
            "that HL_API_SECRET is an approved API wallet for HL_ACCOUNT_ADDRESS "
            "and that the perp account has margin.")
    if args.dry_run:
        log("dry-run: no orders were placed")

    payload = {
        "generated_at": utc_now(),
        "mode": "dry-run" if args.dry_run else "live-post-only",
        "base_url": args.base_url,
        "coin": args.coin,
        "credentials": {
            "account_address": account_address,
            "signer_address": account.address,
            "secret_loaded": True,
        },
        "config": {
            "cycles_requested": args.cycles,
            "sleep_s": args.sleep,
            "timeout_s": args.timeout,
            "target_notional_usd": args.notional,
            "offset_pct_below_mid": args.offset_pct,
            "min_notional_usd": args.min_notional,
            "order_type": "Alo (post-only) limit BUY",
        },
        "meta": {
            "sz_decimals": sz_decimals,
            "max_price_decimals": max_price_decimals,
            "max_significant_figures": 5,
        },
        "preflight": preflight,
        "clock": clock,
        "summary": {
            "place_ack_ms": place_sum,
            "place_ack_rejected_ms": rejected_place_sum,
            "cancel_ack_ms": cancel_sum,
            "combined_ack_ms": combined_sum,
        },
        "error_counts": {
            msg: sum(1 for e in errors if e.get("error") == msg)
            for msg in sorted({e.get("error", "?") for e in errors})
        },
        "samples": samples,
        "errors": errors,
        "terminal_output": LOG_LINES,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log(f"[saved] {out}")

    if args.dry_run:
        return 0
    return 0 if place_lats and cancel_lats else 1


if __name__ == "__main__":
    sys.exit(main())
