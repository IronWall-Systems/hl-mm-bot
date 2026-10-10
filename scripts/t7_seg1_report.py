"""T7 part 1: segment-1 analysis — equity start/end, fees, 1s/5s/30s markouts per fill.

Reads logs/t6_seg1.log (orderUpdates/userFills raw payloads, already address-redacted at
capture) + one read-only equity fetch (no key use, address never printed).
Markout mid = quote-implied mid (our resting bid+ask)/2 (accuracy ~ requote threshold).
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict

LOG = "logs/t6_seg1.log"
OFFS = (1.0, 5.0, 30.0)


def parse(log_lines):
    orders = {}          # oid -> {side, px, open_ts, close_ts}
    fills = {}           # tid -> fill dict
    for line in log_lines:
        if "orderUpdates raw: " in line:
            ts_ms = None
            m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})", line)
            payload = json.loads(line.split("orderUpdates raw: ", 1)[1])
            for u in payload.get("data", []):
                o = u["order"]
                oid = int(o["oid"])
                rec = orders.setdefault(oid, {"side": o["side"], "px": float(o["limitPx"]),
                                              "open_ts": int(o["timestamp"]), "close_ts": None})
                if u["status"] in ("canceled", "filled", "rejected"):
                    st = int(u["statusTimestamp"])
                    rec["close_ts"] = max(rec["close_ts"] or 0, st)
        elif "userFills raw: " in line:
            payload = json.loads(line.split("userFills raw: ", 1)[1])
            for f in payload["data"].get("fills", []):
                fills[int(f["tid"])] = dict(f)
    return orders, fills


def side_px(orders, side, t_ms):
    cands = [o for o in orders.values() if o["side"] == side and o["open_ts"] <= t_ms]
    if not cands:
        return None
    open_now = [o for o in cands if o["close_ts"] is None or o["close_ts"] > t_ms]
    pick = max(open_now or cands, key=lambda o: o["open_ts"])
    return pick["px"]


def mid_at(orders, t_ms):
    b, a = side_px(orders, "B", t_ms), side_px(orders, "A", t_ms)
    return None if b is None or a is None else (b + a) / 2.0


def main():
    lines = open(LOG, errors="replace").read().splitlines()
    orders, fills = parse(lines)
    equity_series = [l for l in lines if "INFO mmbot: metrics" in l]
    startup = next(l for l in lines if "startup: equity" in l)
    print("STARTUP:", startup.split("INFO mmbot: ")[1])
    print("\nEQUITY SERIES (metrics lines):")
    for l in equity_series:
        m = re.search(r"(\d{2}:\d{2}:\d{2}).*fills=(\d+).*equity=([\d.]+)", l)
        print(f"  {m.group(1)} fills={m.group(2)} equity={m.group(3)}")

    ordered = sorted(fills.values(), key=lambda f: f["time"])
    print(f"\nFILLS: {len(ordered)} unique (deduped by tid)")
    tot_fee = tot_pnl = 0.0
    print(f"{'#':>2} {'time':<9} {'side':<4} {'dir':<13} {'px':>10} {'sz':>9} "
          f"{'fee':>9} {'clPnl':>9} {'mo1s_bps':>9} {'mo5s_bps':>9} {'mo30s_bps':>9} {'mo5s_$':>8}")
    for i, f in enumerate(ordered, 1):
        px, sz = float(f["px"]), float(f["sz"])
        fee, cp = float(f["fee"]), float(f.get("closedPnl") or 0.0)
        tot_fee += fee
        tot_pnl += cp
        row = []
        for off in OFFS:
            m = mid_at(orders, int(f["time"]) + int(off * 1000))
            if m is None:
                row.append(float("nan"))
                continue
            raw = (px - m) if f["side"] == "A" else (m - px)
            row.append(raw / px * 1e4)
        m5 = mid_at(orders, int(f["time"]) + 5000)
        usd5 = ((px - m5) if f["side"] == "A" else (m5 - px)) * sz if m5 else float("nan")
        hhmmss = f"{(f['time'] // 1000) % 86400 // 3600:02d}:{(f['time'] // 1000) % 3600 // 60:02d}:{(f['time'] // 1000) % 60:02d}"
        print(f"{i:>2} {hhmmss:<9} {'sell' if f['side']=='A' else 'buy':<4} {f['dir']:<13} "
              f"{px:>10.1f} {sz:>9.5f} {fee:>9.6f} {cp:>9.6f} "
              f"{row[0]:>9.3f} {row[1]:>9.3f} {row[2]:>9.3f} {usd5:>8.4f}")
    print(f"\nTOTALS: fees={tot_fee:.6f} USDC, closedPnl={tot_pnl:.6f} USDC, "
          f"net={tot_pnl - tot_fee:.6f} USDC")

    # live read-only equity components (address never printed)
    from hyperliquid.info import Info
    addr = os.environ["HL_ACCOUNT_ADDRESS"]
    info = Info("https://api.hyperliquid-testnet.xyz", skip_ws=True)
    us = info.user_state(addr)
    spot = info.spot_user_state(addr)
    av = float(us["marginSummary"]["accountValue"])
    sp = sum(float(b.get("total", 0) or 0) for b in spot.get("balances", []) if b.get("coin") == "USDC")
    hold = sum(float(b.get("hold", 0) or 0) for b in spot.get("balances", []) if b.get("coin") == "USDC")
    pos = [float(ap["position"]["szi"]) for ap in us.get("assetPositions", [])
           if ap["position"]["coin"] == "BTC"]
    print(f"\nLIVE (flat) COMPONENTS: margin accountValue={av:.6f} spotUSDC total={sp:.6f} "
          f"(hold={hold:.6f}) combined={av + sp:.6f} pos={pos[0] if pos else 0.0}")


if __name__ == "__main__":
    main()
