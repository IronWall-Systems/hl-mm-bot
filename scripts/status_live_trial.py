#!/usr/bin/env python3
"""Read-only status engine for the live micro-notional trial.

Parses ``logs/live_trial_20261107.jsonl`` and queries the exchange read-only
(``info.user_state`` / ``info.open_orders``).  This module never sends orders and
is imported by the Telegram monitor; it has zero effect on the trading loop.

CLI:
    PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/status_live_trial.py
    ... --detail
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / ".env", Path("/root/hl-mm-bot/.env")):
    if candidate.exists():
        load_dotenv(candidate, override=False)

JSONL = Path(os.environ.get("HL_LIVE_JSONL",
                            ROOT / "logs" / "live_trial_20261107_hype.jsonl"))
CONSOLE = JSONL.with_suffix("").with_suffix(".console.log")
AI_STATE = ROOT / "logs" / "ai_sentinel_state.json"
TMUX_SESSION = "live_trial"
HARD_STOP_USD = 2.00
DEFAULT_START_EQUITY = 24.29537


# ---- helpers ----------------------------------------------------------------
def _utc(ts: Optional[float]) -> str:
    if not ts:
        return "n/a"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ago(seconds: Optional[float]) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "n/a"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.1f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _pct(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] * (hi - k) + s[hi] * (k - lo)


def _f(value: Optional[float], fmt: str = "{:.2f}", dash: str = "n/a") -> str:
    return dash if value is None else fmt.format(value)


def read_events(path: Path = JSONL) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def read_ai_review() -> Optional[Dict[str, Any]]:
    """Latest cached AI review from the sentinel state file (read-only)."""
    try:
        state = json.loads(AI_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not state.get("last_review_ts") or not state.get("last_action"):
        return None
    return {
        "ts": float(state.get("last_review_ts", 0.0)),
        "action": str(state.get("last_action", "")),
        "confidence": (None if state.get("last_confidence") is None
                       else float(state.get("last_confidence") or 0.0)),
        "reason": str(state.get("last_reason", "")),
        "telegram": str(state.get("last_telegram", "")),
        "elapsed_s": state.get("last_elapsed_s"),
    }


LIVE_SERVICE = "mmbot-live-trial.service"


def service_active(name: str = LIVE_SERVICE) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", name],
                              timeout=3).returncode == 0
    except Exception:
        return False


def tmux_status(session: str = TMUX_SESSION) -> Dict[str, Any]:
    svc = service_active()
    running = False
    created: Optional[float] = None
    try:
        r = subprocess.run(["tmux", "has-session", "-t", session],
                           capture_output=True, text=True, timeout=3)
        running = r.returncode == 0
    except Exception:
        running = False
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", session,
                            "#{session_created}"], capture_output=True, text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip().isdigit():
            created = float(r.stdout.strip())
    except Exception:
        pass
    proc = subprocess.run(["pgrep", "-af", "live_trial_20261107.py"],
                          capture_output=True, text=True)
    pids = []
    for line in proc.stdout.splitlines():
        pid, _, cmd = line.partition(" ")
        if "python" in cmd and "tmux" not in cmd and "bash -c" not in cmd:
            pids.append(pid)
    return {"running": running or svc, "tmux": running, "service": svc,
            "created": created, "pids": pids}


def exchange_state(coin: str = "BTC") -> Dict[str, Any]:
    """Read-only account/order snapshot. Returns an ``error`` key on failure."""
    result: Dict[str, Any] = {"ok": False, "equity": None, "unrealized_pnl": None,
                              "positions": [], "orders": [], "error": None}
    try:
        from hyperliquid.info import Info
        addr = os.environ.get("HL_ACCOUNT_ADDRESS")
        if not addr:
            raise RuntimeError("HL_ACCOUNT_ADDRESS missing")
        info = Info(os.environ.get("HL_BASE_URL", "https://api.hyperliquid.xyz"),
                    skip_ws=True, timeout=8)
        us = info.user_state(addr)
        ms = us.get("marginSummary", {})
        result["equity"] = float(ms.get("accountValue", 0.0))
        result["unrealized_pnl"] = 0.0
        for p in us.get("assetPositions", []):
            pos = p.get("position", {})
            try:
                szi = float(pos.get("szi", 0.0))
            except (TypeError, ValueError):
                szi = 0.0
            if abs(szi) > 0:
                result["positions"].append({
                    "coin": pos.get("coin", coin), "size": szi,
                    "entry_px": float(pos.get("entryPx") or 0.0),
                    "unrealized_pnl": float(pos.get("unrealizedPnl") or 0.0),
                })
                result["unrealized_pnl"] = (result["unrealized_pnl"] or 0.0) + \
                    float(pos.get("unrealizedPnl") or 0.0)
        for o in info.open_orders(addr):
            if o.get("coin") != coin:
                continue
            result["orders"].append({
                "oid": o.get("oid"), "side": o.get("side"),
                "price": float(o.get("limitPx") or 0.0),
                "size": float(o.get("sz") or 0.0),
            })
        result["ok"] = True
    except Exception as exc:  # read-only best effort; never raise into callers
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# ---- aggregates -------------------------------------------------------------
def build_metrics(events: List[Dict[str, Any]], ex: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    startup_idx = max((i for i, e in enumerate(events) if e.get("event") == "startup"),
                      default=-1)
    startup = events[startup_idx] if startup_idx >= 0 else None
    # Scope session stats to the most recent launch so restarts show a clean panel.
    session = events[startup_idx:] if startup_idx >= 0 else events
    start_equity = float(startup["equity"]) if startup and startup.get("equity") else DEFAULT_START_EQUITY
    started_at = float(startup["ts"]) if startup and startup.get("ts") else None

    def lat(kind: str) -> List[float]:
        return [float(e["latency_ms"]) for e in session
                if e.get("event") == kind and e.get("latency_ms") is not None]

    place_lat, modify_lat = lat("place"), lat("modify")
    park_lat, cancel_lat = lat("park"), lat("cancel")
    fills = [e for e in session if e.get("event") == "fill"]
    parks = [e for e in session if e.get("event") == "park"]
    fee = sum(float(e.get("fee") or 0.0) for e in fills)

    mark = {"1s": [], "5s": [], "30s": []}
    for e in session:
        if e.get("event") != "markout":
            continue
        key = f"{int(e.get('horizon_s', 0))}s"
        if key in mark and e.get("markout_bps") is not None:
            mark[key].append(float(e["markout_bps"]))

    last_ts = max((float(e.get("ts", 0.0)) for e in session), default=None)
    equity = ex.get("equity") if ex.get("ok") else None
    if equity is None:
        eq_events = [e for e in session if e.get("event") == "equity"]
        equity = float(eq_events[-1]["equity"]) if eq_events else None

    pnl = (equity - start_equity) if equity is not None else None
    pnl_bps = (pnl / start_equity * 1e4) if pnl is not None and start_equity else None

    return {
        "now": now,
        "start_equity": start_equity,
        "started_at": started_at,
        "uptime_s": (now - started_at) if started_at else None,
        "heartbeat_age_s": (now - last_ts) if last_ts else None,
        "equity": equity,
        "pnl": pnl,
        "pnl_bps": pnl_bps,
        "unrealized_pnl": ex.get("unrealized_pnl") if ex.get("ok") else None,
        "hard_stop_equity": start_equity - HARD_STOP_USD,
        "stop_distance": (equity - (start_equity - HARD_STOP_USD)) if equity is not None else None,
        "position": sum(p["size"] for p in ex.get("positions", [])) if ex.get("ok") else None,
        "orders": ex.get("orders", []),
        "requotes": len(modify_lat),
        "parks": len(parks),
        "fills": len(fills),
        "fees": fee,
        "place_lat": place_lat,
        "modify_lat": modify_lat,
        "park_lat": park_lat,
        "cancel_lat": cancel_lat,
        "markout": mark,
        "recent_fills": fills[-10:],
        "exchange_error": ex.get("error"),
    }


def render_status(detail: bool = False) -> str:
    events = read_events()
    coin = os.environ.get("HL_COIN", "HYPE").upper()
    ex = exchange_state(coin)
    m = build_metrics(events, ex)
    t = tmux_status()

    health = ("🟢 RUNNING" if (t["running"] and t["pids"])
              else ("🟡 STARTING" if t["running"] else "🔴 STOPPED"))
    if t["service"]:
        runner = "systemd"
    elif t["tmux"]:
        runner = f"tmux <code>{TMUX_SESSION}</code>"
    else:
        runner = "no supervisor"
    lines = [
        f"🟢 <b>LIVE TRIAL — {coin}</b>",
        f"🕒 {_utc(m['now'])}",
        f"<b>Health:</b> {health} · {runner}"
        f" · pid {','.join(t['pids']) or 'n/a'}",
        f"<b>Uptime:</b> {_ago(m['uptime_s'])} · <b>Heartbeat:</b> {_ago(m['heartbeat_age_s'])} ago",
        "",
        "<b>EQUITY &amp; SAFETY BELTS</b>",
        f"Perp equity: <b>${_f(m['equity'], '{:.5f}')}</b>",
        f"Net PnL: <b>{_f(m['pnl'], '{:+.5f}')} USD</b> ({_f(m['pnl_bps'], '{:+.2f}')} bps)",
        f"Unrealized PnL: {_f(m['unrealized_pnl'], '{:+.5f}')} USD",
        f"Hard stop: ${m['hard_stop_equity']:.5f} (−$2.00) · "
        f"<b>distance {_f(m['stop_distance'], '${:.5f}')}</b>",
        "",
        "<b>ACTIVE QUOTES &amp; INVENTORY</b>",
        f"Net position: <b>{_f(m['position'], '{:.6f}')}</b> {coin}",
    ]
    if m["orders"]:
        for o in sorted(m["orders"], key=lambda x: x["side"], reverse=True):
            name = "Bid" if o["side"] == "B" else "Ask"
            px = f"{o['price']:.3f}" if o["price"] < 1000 else f"{o['price']:.1f}"
            lines.append(f"{name}: <b>{px}</b> × {o['size']:.5f} "
                         f"(<code>{o['oid']}</code>) · ${o['price'] * o['size']:.2f}")
    else:
        lines.append("No resting orders")
    if not ex.get("ok"):
        lines.append(f"⚠️ exchange read failed: {m['exchange_error']}")

    mod = m["modify_lat"]
    lines += [
        "",
        "<b>EXECUTION &amp; LATENCY</b>",
        f"Requotes (batchModify): <b>{m['requotes']}</b>",
        f"batchModify: p50 {_f(_pct(mod, .5), '{:.1f}')} / p95 {_f(_pct(mod, .95), '{:.1f}')}"
        f" / max {_f(max(mod) if mod else None, '{:.1f}')} ms",
        f"OTM parks: <b>{m['parks']}</b>",
        f"Fills: <b>{m['fills']}</b> · fees/rebates: ${_f(m['fees'], '{:.4f}')}",
        "Markout 1s/5s/30s: " + " / ".join(
            f"{_f(_pct(m['markout'][k], .5), '{:+.2f}')}" for k in ("1s", "5s", "30s")) + " bps",
    ]

    if detail:
        lines += ["", "<b>LATENCY DETAIL (ms)</b>"]
        for label, arr in (("place", m["place_lat"]), ("modify", m["modify_lat"]),
                           ("park", m["park_lat"]), ("cancel", m["cancel_lat"])):
            if arr:
                lines.append(f"{label}: n={len(arr)} p50 {_pct(arr, .5):.1f} · "
                             f"p95 {_pct(arr, .95):.1f} · max {max(arr):.1f}")
            else:
                lines.append(f"{label}: n=0")
        lines += ["", "<b>RECENT FILLS</b>"]
        if m["recent_fills"]:
            for f in m["recent_fills"]:
                lines.append(f"{_utc(f.get('ts'))} · {f.get('side')} {f.get('size')} @ "
                             f"{f.get('px')} · fee ${float(f.get('fee') or 0):.5f}")
        else:
            lines.append("No fills yet")
        lines += ["", f"<b>Markout samples:</b> 1s {len(m['markout']['1s'])} · "
                      f"5s {len(m['markout']['5s'])} · 30s {len(m['markout']['30s'])}"]

    ai = read_ai_review()
    if ai:
        age = max(0.0, time.time() - ai["ts"])
        conf = "n/a" if ai["confidence"] is None else f"{ai['confidence']:.2f}"
        lines += [
            "",
            "🧠 <b>AI QUANT REVIEW</b>",
            f"🕒 {_utc(ai['ts'])} ({_ago(age)} ago) · "
            f"Action: <b>{html.escape(ai['action'])}</b> (conf {conf})",
        ]
        if ai["reason"]:
            lines.append(html.escape(ai["reason"]))
        if ai["telegram"]:
            lines.append(html.escape(ai["telegram"]))
        if ai.get("elapsed_s") is not None:
            lines.append(f"<i>evaluated in {float(ai['elapsed_s']):.2f}s · "
                         f"stateless single-turn</i>")

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="read-only live trial status")
    ap.add_argument("--detail", action="store_true", help="include latency percentiles and fills")
    ap.add_argument("--json", action="store_true", help="print raw JSON metrics")
    args = ap.parse_args()
    if args.json:
        ev = read_events()
        print(json.dumps(build_metrics(ev, exchange_state()), indent=2, default=str))
    else:
        print(render_status(detail=args.detail))


if __name__ == "__main__":
    main()
