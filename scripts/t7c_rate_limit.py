"""T7c part 2: read-only userRateLimit probe (testnet). Never prints the full address
(6+4 redaction). Exit 0 on a payload, 2 on error. Run with HL_* already exported:
    .venv/bin/python scripts/t7c_rate_limit.py [label]
"""
import json
import os
import sys
import time

from hyperliquid.info import Info

TESTNET = "https://api.hyperliquid-testnet.xyz"


def redact(a: str) -> str:
    return a[:6] + "..." + a[-4:] if isinstance(a, str) and len(a) > 10 else a


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "reading"
    addr = os.environ["HL_ACCOUNT_ADDRESS"]
    info = Info(TESTNET, skip_ws=True)
    t0 = time.monotonic()
    try:
        resp = info.post("/info", {"type": "userRateLimit", "user": addr})
    except Exception as e:
        print(f"USER_RATE_LIMIT[{label}] ERROR {e!r}", flush=True)
        return 2
    rtt = (time.monotonic() - t0) * 1e3
    text = json.dumps(resp, separators=(",", ":"), default=str).replace(addr, redact(addr))
    print(f"USER_RATE_LIMIT[{label}] RTT_MS={rtt:.0f}", flush=True)
    print(f"USER_RATE_LIMIT[{label}] raw: {text}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
