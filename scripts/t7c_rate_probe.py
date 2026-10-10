"""T7c part 2 (controlled): which request classes increment userRateLimit.nRequestsUsed?
Read-only classes only (NO order actions - rule: no live orders outside sanctioned runs).
    set -a; . ./.env; set +a; .venv/bin/python scripts/t7c_rate_probe.py
"""
import json
import os
import time

from hyperliquid.info import Info

TESTNET = "https://api.hyperliquid-testnet.xyz"
ADDR = None
N = 20


def redact(a: str) -> str:
    return a[:6] + "..." + a[-4:]


def used(info) -> int:
    r = info.post("/info", {"type": "userRateLimit", "user": ADDR})
    return int(r["nRequestsUsed"]), r


def main() -> None:
    global ADDR
    ADDR = os.environ["HL_ACCOUNT_ADDRESS"]
    info = Info(TESTNET, skip_ws=True)
    c0, r0 = used(info)
    print(f"c0 used={c0} raw={json.dumps(r0, default=str).replace(ADDR, redact(ADDR))}", flush=True)

    t = time.monotonic()
    for _ in range(N):
        info.post("/info", {"type": "l2Book", "coin": "BTC"})
    c1, _ = used(info)
    print(f"after {N} public l2Book /info: used={c1} delta={c1 - c0} "
          f"({(time.monotonic() - t):.1f}s)", flush=True)

    t = time.monotonic()
    for _ in range(N):
        info.user_state(ADDR)
    c2, _ = used(info)
    print(f"after {N} address user_state: used={c2} delta={c2 - c1} "
          f"({(time.monotonic() - t):.1f}s)", flush=True)

    t = time.monotonic()
    for _ in range(N):
        info.open_orders(ADDR)
    c3, _ = used(info)
    print(f"after {N} address open_orders: used={c3} delta={c3 - c2} "
          f"({(time.monotonic() - t):.1f}s)", flush=True)

    t = time.monotonic()
    for _ in range(N):
        info.spot_user_state(ADDR)
    c4, r4 = used(info)
    print(f"after {N} spot_user_state +1 probe: used={c4} delta={c4 - c3} "
          f"({(time.monotonic() - t):.1f}s) raw={json.dumps(r4, default=str).replace(ADDR, redact(ADDR))}",
          flush=True)


if __name__ == "__main__":
    main()
