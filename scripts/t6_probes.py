"""T6 segment 2: deliberate TESTNET reject probes -> raw payload capture.

Captures the two cases segment 1 never observed organically (BUILD_SPEC §3):
  MIN-NOTIONAL: ~$5 post-only BTC order (A-02: bot constant says $10 min)
  MARGIN:       ~$1300 notional post-only order vs ~$999 equity

Safety: testnet-only assert; both orders are Alo (post-only) placed 5% away from
mid so they cannot match; anything resting is cancelled by cloid seconds later.
Prints raw exchange responses (defensively address-redacted); never prints key.

Needs env sourced (`set -a; . ./.env; set +a`) + PYTHONPATH=src.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid

from mmbot.config import load_settings

COIN = "BTC"
_OFF_BPS = 500  # 5% away from mid: post-only orders rest, never match
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def redact(text: str) -> str:
    return _ADDR_RE.sub(lambda m: m.group(0)[:6] + "..." + m.group(0)[-4:], text)


def raw(tag: str, resp) -> None:
    print(f"PROBE {tag} raw: {redact(json.dumps(resp))}", flush=True)


def new_cloid() -> str:
    return "0x" + uuid.uuid4().hex


def main() -> int:
    s = load_settings(os.environ)
    if s.env != "testnet":
        print("ABORT: probes are testnet-only", flush=True)
        return 2

    wallet = Account.from_key(s.api_secret)
    acct = None if s.account_address.lower() == wallet.address.lower() else s.account_address
    info = Info(s.base_url, skip_ws=True)  # no WS thread -> clean exit
    ex = Exchange(wallet, s.base_url, account_address=acct)

    snap = info.l2_snapshot(COIN)
    bids, asks = snap["levels"][0], snap["levels"][1]
    mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2.0
    px = float(int(mid * (1 - _OFF_BPS / 10_000)))  # BTC tick = 1.0 (whole dollars)
    print(f"PROBE mid={mid:.1f} bid={bids[0]['px']} ask={asks[0]['px']} px={px}", flush=True)

    meta = info.meta()
    szd = next(int(a["szDecimals"]) for a in meta["universe"] if a["name"] == COIN)
    floor = 10 ** -szd
    print(f"PROBE szDecimals={szd}", flush=True)

    results: dict[str, str] = {}

    # --- P1: min-notional (~$5) -------------------------------------------
    c1 = new_cloid()
    sz1 = max(round(5.0 / px, szd), floor)
    if sz1 * px >= 10.0:
        results["min_notional"] = f"IMPOSSIBLE szd={szd} min_sz={floor} -> min_notional={floor * px:.2f} USD"
        print(f"PROBE min_notional {results['min_notional']}", flush=True)
    else:
        print(f"PROBE min_notional place px={px} sz={sz1} notional={sz1 * px:.2f}", flush=True)
        try:
            resp = ex.order(COIN, True, sz1, px, {"limit": {"tif": "Alo"}},
                            reduce_only=False, cloid=Cloid.from_str(c1))
            raw("min_notional", resp)
            results["min_notional"] = json.dumps(resp)
        except Exception as e:
            print(f"PROBE min_notional exception: {e!r}", flush=True)
            results["min_notional"] = f"EXCEPTION {e!r}"

    # --- P2: margin (big notional vs ~$999 equity) ------------------------
    # $41k ≈ 10x equity; at ~40x max leverage margin needed ≈ $1026 > 999,
    # so a placement-time margin reject should fire if the exchange enforces it.
    c2 = new_cloid()
    sz2 = max(round(41000.0 / px, szd), floor)
    print(f"PROBE margin place px={px} sz={sz2} notional={sz2 * px:.2f}", flush=True)
    try:
        resp = ex.order(COIN, True, sz2, px, {"limit": {"tif": "Alo"}},
                        reduce_only=False, cloid=Cloid.from_str(c2))
        raw("margin", resp)
        results["margin"] = json.dumps(resp)
    except Exception as e:
        print(f"PROBE margin exception: {e!r}", flush=True)
        results["margin"] = f"EXCEPTION {e!r}"

    # --- cancel whatever rested (also captures cancel raws) ---------------
    time.sleep(1)
    for attempt in range(3):
        left = 0
        for tag, cl in (("min_notional", c1), ("margin", c2)):
            try:
                raw(f"cancel_{tag}", ex.cancel_by_cloid(COIN, Cloid.from_str(cl)))
            except Exception as e:
                print(f"PROBE cancel_{tag} exception: {e!r}", flush=True)
        rows = info.open_orders(s.account_address)
        mine = [r for r in rows if str(r.get("cloid", "")) in (c1, c2)]
        left = len(mine)
        if left == 0:
            break
        print(f"PROBE leftover={left}, retrying cancel", flush=True)
        time.sleep(1)

    rows = info.open_orders(s.account_address)
    print(f"PROBE open_orders={len(rows)}", flush=True)
    ok = len(rows) == 0
    print(f"PROBE result={'CLEAN' if ok else 'LEFTOVER'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
