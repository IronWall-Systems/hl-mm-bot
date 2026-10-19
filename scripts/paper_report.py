#!/usr/bin/env python3
"""scripts/paper_report.py — T15 live shadow paper-trading report (offline, read-only).

Reads data/paper_live_state.json (written by scripts/paper_shadow.py) and prints, per
pre-registered candidate: live cumulative and today's Fills, Round-Trips,
round_trip_spread_pnl, inventory_mtm_pnl, funding_pnl, fees_usd, net_pnl_usd, current open
inventory and worst drawdown. Derived from the same metrics as scripts/replay_backtest.py.

`--line` emits ONE summary line (used by scripts/collector_report.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = PROJECT_ROOT / "data" / "paper_live_state.json"


def load_state(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _fmt(v) -> str:
    try:
        return f"{float(v):+.4f}"
    except (TypeError, ValueError):
        return "n/a"


def summary_line(state: dict | None) -> str:
    if not state:
        return "paper    : no shadow tracker state yet (data/paper_live_state.json)"
    cands = state.get("candidates", {})
    parts = []
    for key in sorted(cands):
        c = cands[key]
        cum = c.get("cumulative", {})
        parts.append(f"{key} net={_fmt(cum.get('net_pnl_usd'))} "
                     f"fills={int(cum.get('fills', 0))} rt={int(cum.get('round_trip_count', 0))}")
    return "paper    : " + " | ".join(parts)


def render(state: dict | None) -> list[str]:
    if state is None:
        return ["=== paper shadow report ===",
                "no state file (has scripts/paper_shadow.py run yet?)"]
    out = [
        f"=== paper shadow report {state.get('updated_utc', '?')} ===",
        f"grid     : latency={state.get('latency_ms')} ms  cancel_rate={state.get('cancel_rate')}"
        f"  today={state.get('today')}",
    ]
    for key in sorted(state.get("candidates", {})):
        c = state["candidates"][key]
        cum = c.get("cumulative", {})
        tod = c.get("today", {})
        out.append(f"[{key}] coin={c.get('coin')} hs={c.get('hs')} "
                   f"hours={c.get('processed_hours')}")
        out.append(
            f"  cumulative : fills={int(cum.get('fills', 0))} "
            f"round_trips={int(cum.get('round_trip_count', 0))} "
            f"rt_spread={_fmt(cum.get('round_trip_spread_pnl'))} "
            f"inv_mtm={_fmt(cum.get('inventory_mtm_pnl'))} "
            f"funding={_fmt(cum.get('funding_pnl'))} "
            f"fees={_fmt(cum.get('total_fees_usd'))} "
            f"net={_fmt(cum.get('net_pnl_usd'))} "
            f"open_inv={_fmt(c.get('open_inventory_usd'))} "
            f"max_dd={_fmt(c.get('worst_drawdown_usd'))}"
        )
        out.append(
            f"  today      : fills={int(tod.get('fills', 0))} "
            f"round_trips={int(tod.get('round_trip_count', 0))} "
            f"rt_spread={_fmt(tod.get('round_trip_spread_pnl'))} "
            f"inv_mtm={_fmt(tod.get('inventory_mtm_pnl'))} "
            f"funding={_fmt(tod.get('funding_pnl'))} "
            f"fees={_fmt(tod.get('total_fees_usd'))} "
            f"net={_fmt(tod.get('net_pnl_usd'))}"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T15 shadow paper-trading report")
    ap.add_argument("--state-file", type=Path, default=STATE_FILE)
    ap.add_argument("--line", action="store_true", help="print one summary line only")
    args = ap.parse_args(argv)
    state = load_state(args.state_file)
    if args.line:
        print(summary_line(state))
    else:
        print("\n".join(render(state)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
