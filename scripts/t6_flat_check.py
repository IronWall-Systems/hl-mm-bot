"""T6 post-segment flat check (read-only): open orders + position for the .env account.

Runs AFTER the bot exits, with HL_* already exported by the wrapper. Never prints the
address. Exit 0 = flat AND zero open orders (any coin); exit 1 otherwise.
"""
import os
import sys

from hyperliquid.info import Info

TESTNET = "https://api.hyperliquid-testnet.xyz"


def main() -> int:
    addr = os.environ["HL_ACCOUNT_ADDRESS"]
    coin = os.environ.get("HL_COIN", "BTC")
    info = Info(TESTNET, skip_ws=True)   # skip_ws: Info() would start a WS thread that
                                         # keeps the interpreter alive after printing
    rows = info.open_orders(addr)
    us = info.user_state(addr)
    pos = 0.0
    for ap in us.get("assetPositions", []):
        p = ap["position"]
        if p["coin"] == coin:
            pos = float(p["szi"])
    n = len(rows)
    value = us.get("marginSummary", {}).get("accountValue", "?")
    print(f"FLAT_CHECK open_orders={n} position={pos:.6f} accountValue={value}", flush=True)
    ok = n == 0 and abs(pos) < 1e-9
    print(f"FLAT_CHECK result={'FLAT' if ok else 'NOT_FLAT'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
