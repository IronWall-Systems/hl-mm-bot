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

from dotenv import dotenv_values, load_dotenv

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


def _env_value(key: str, default: str = "") -> str:
    """Read a key from the on-disk .env first, then the process environment.

    The monitor is long-lived, so a value cached at import time goes stale as
    soon as an operator edits .env (e.g. rotating HL_ACCOUNT_ADDRESS).  Reading
    the file per call keeps the dashboard pointed at the active wallet without
    requiring a restart.
    """
    for path in (ROOT / ".env", Path("/root/hl-mm-bot/.env")):
        try:
            vals = dotenv_values(path)
        except Exception:
            continue
        value = vals.get(key)
        if value:
            return str(value).strip()
    return os.environ.get(key, default)


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


def exchange_state(coin: Optional[str] = None) -> Dict[str, Any]:
    """Read-only account/order snapshot. Returns an ``error`` key on failure.

    The wallet is re-read from .env on every call (see ``_env_value``) so a
    rotated HL_ACCOUNT_ADDRESS cannot leave the dashboard pointed at an empty
    legacy account.  An empty or malformed response is reported as an error
    instead of being silently rendered as ``$0.00``.
    """
    coin = (coin or _env_value("HL_COIN", "HYPE")).upper()
    result: Dict[str, Any] = {"ok": False, "equity": None, "unrealized_pnl": None,
                              "positions": [], "orders": [], "orders_known": False,
                              "address": None, "error": None}
    try:
        from hyperliquid.info import Info
        addr = _env_value("HL_ACCOUNT_ADDRESS")
        if not addr:
            raise RuntimeError("HL_ACCOUNT_ADDRESS missing")
        if not (addr.startswith("0x") and len(addr) == 42):
            raise RuntimeError(f"HL_ACCOUNT_ADDRESS malformed: {addr!r}")
        result["address"] = addr
        info = Info(_env_value("HL_BASE_URL", "https://api.hyperliquid.xyz"),
                    skip_ws=True, timeout=8)
        us = info.user_state(addr)
        if not isinstance(us, dict) or not us:
            raise RuntimeError("empty user_state response")
        ms = us.get("marginSummary") or {}
        if "accountValue" not in ms:
            raise RuntimeError("user_state missing marginSummary.accountValue")
        equity = float(ms.get("accountValue"))
        if not math.isfinite(equity):
            raise RuntimeError(f"non-finite accountValue={equity!r}")
        result["equity"] = equity
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
        # Orders are a separate call: a transient failure there must not throw
        # away an otherwise valid equity/position snapshot.
        try:
            for o in info.open_orders(addr):
                if o.get("coin") != coin:
                    continue
                result["orders"].append({
                    "oid": o.get("oid"), "side": o.get("side"),
                    "price": float(o.get("limitPx") or 0.0),
                    "size": float(o.get("sz") or 0.0),
                })
            result["orders_known"] = True
        except Exception as exc:
            result["error"] = f"open_orders: {type(exc).__name__}: {exc}"
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

    # The bot writes an `equity` event every few seconds from its own live
    # exchange snapshot.  Use it as an independent source of truth so a bad
    # monitor-side query (stale wallet, empty response) cannot render $0.00.
    eq_events = [e for e in session if e.get("event") == "equity"]
    last_eq = eq_events[-1] if eq_events else None
    eq_ts = float(last_eq["ts"]) if last_eq and last_eq.get("ts") else None
    telemetry_fresh = (now - eq_ts) <= 120.0 if eq_ts else False
    telemetry_equity = (float(last_eq["equity"])
                        if last_eq and last_eq.get("equity") is not None else None)
    telemetry_pos = (float(last_eq["position"])
                     if last_eq and last_eq.get("position") is not None else None)
    telemetry_upnl = (float(last_eq["unrealized_pnl"])
                      if last_eq and last_eq.get("unrealized_pnl") is not None else None)

    ex_ok = bool(ex.get("ok"))
    ex_equity = ex.get("equity") if ex_ok else None
    # A non-positive account snapshot while the bot's own fresh telemetry says
    # the account is funded is a stale/failed read, not a real wipeout.
    stale_snapshot = (ex_equity is not None and ex_equity <= 0
                      and telemetry_fresh and (telemetry_equity or 0.0) > 0)
    exchange_error = ex.get("error")
    if stale_snapshot and not exchange_error:
        exchange_error = (f"exchange returned non-positive equity ({ex_equity}); "
                          f"using bot telemetry")

    equity = None if stale_snapshot else ex_equity
    equity_source = "exchange" if equity is not None else None
    if equity is None and telemetry_fresh:
        equity = telemetry_equity
        equity_source = "telemetry" if equity is not None else None

    if stale_snapshot:
        position = telemetry_pos
        unrealized_pnl = telemetry_upnl
        orders_known = False
    else:
        position = (sum(p["size"] for p in ex.get("positions", []))
                    if ex_ok else (telemetry_pos if telemetry_fresh else None))
        unrealized_pnl = (ex.get("unrealized_pnl") if ex_ok
                          else (telemetry_upnl if telemetry_fresh else None))
        orders_known = bool(ex_ok and ex.get("orders_known"))

    pnl = (equity - start_equity) if equity is not None else None
    pnl_bps = (pnl / start_equity * 1e4) if pnl is not None and start_equity else None

    return {
        "now": now,
        "start_equity": start_equity,
        "started_at": started_at,
        "uptime_s": (now - started_at) if started_at else None,
        "heartbeat_age_s": (now - last_ts) if last_ts else None,
        "equity": equity,
        "equity_source": equity_source,
        "pnl": pnl,
        "pnl_bps": pnl_bps,
        "unrealized_pnl": unrealized_pnl,
        "hard_stop_equity": start_equity - HARD_STOP_USD,
        "stop_distance": (equity - (start_equity - HARD_STOP_USD)) if equity is not None else None,
        "position": position,
        "orders": ex.get("orders", []),
        "orders_known": orders_known,
        "exchange_address": ex.get("address"),
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
        "exchange_error": exchange_error,
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
    elif m["orders_known"]:
        lines.append("No resting orders")
    else:
        lines.append("Resting orders: n/a (exchange read unavailable)")
    if m["equity_source"] != "exchange" or not m["orders_known"]:
        reason = m["exchange_error"] or "incomplete exchange snapshot"
        lines.append(f"⚠️ exchange read: {html.escape(str(reason))}")

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
