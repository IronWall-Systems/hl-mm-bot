#!/usr/bin/env python3
"""24/7 token-efficient AI supervisor for the live HYPE trial.

Two tiers:
  1. Deterministic key-point extractor (every 300 s, 0 tokens) -> compact JSON digest.
  2. Stateless single-turn AI evaluation, invoked only on anomaly or every 30 min.
     Every invocation is a fresh `opencode run` process with only the digest; no
     conversation history is carried.

Read-only with respect to the exchange.  It may only take whitelisted defensive
actions and can never change size, leverage, or spreads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / ".env", Path("/root/hl-mm-bot/.env")):
    if candidate.exists():
        load_dotenv(candidate, override=False)

sys.path.insert(0, str(ROOT / "scripts"))
import status_live_trial as status  # noqa: E402

CHECK_INTERVAL_S = 300
SCHEDULED_REVIEW_S = 1800
MODEL = os.environ.get("AI_SENTINEL_MODEL", "opencode/deepseek-v4.1-flash-free")
LOG_PATH = ROOT / "logs" / "ai_sentinel.log"
STATE_PATH = ROOT / "logs" / "ai_sentinel_state.json"
HARD_STOP_USD = 2.00
DRAWDOWN_TRIGGER_USD = 0.50
TOXIC_MARKOUT_BPS = -5.0
STUCK_INVENTORY_S = 900
LAT_P95_TRIGGER_MS = 650
LAT_P95_DEGRADED_MS = 700
LAT_P50_DEGRADED_MS = 550
PARKS_TRIGGER_5M = 2
CRITICAL_LOSS_USD = 1.20
DEAD_FEED_S = 10.0
REJECT_CRITICAL_5M = 10

# Every ID allowed to operate the bot; also receives sentinel alerts.
AUTH_USER_IDS = {6029973681, 7752127491}
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _parse_ids(raw: str) -> list:
    """Parse a comma/semicolon separated id list, preserving order."""
    ids = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value not in ids:
            ids.append(value)
    return ids


# Alerts fan out to all operators.  TELEGRAM_CHAT_IDS overrides
# TELEGRAM_CHAT_ID; authorized operators are always included.
TG_CHATS = (_parse_ids(os.environ.get("TELEGRAM_CHAT_IDS", ""))
            or _parse_ids(os.environ.get("TELEGRAM_CHAT_ID", ""))
            or sorted(AUTH_USER_IDS))
for _uid in sorted(AUTH_USER_IDS):
    if _uid not in TG_CHATS:
        TG_CHATS.append(_uid)
TG_CHAT = TG_CHATS[0]

SYSTEM_PROMPT = """You are a senior quantitative trader supervising a live HYPE perpetual market-making bot.
You receive ONLY a compact deterministic digest. You have no other context and no tools.
Return ONLY one compact JSON object, <= 120 words total:
{"action":"HOLD_NORMAL|WARN_TELEGRAM|DEFENSIVE_PAUSE|EMERGENCY_FLATTEN","confidence":0.0-1.0,"reason":"<=35 words","telegram":"<=60 words"}
Rules:
- HOLD_NORMAL when nothing material.
- WARN_TELEGRAM for a material but non-critical anomaly; explain fills/markout/inventory/latency.
- DEFENSIVE_PAUSE or EMERGENCY_FLATTEN ONLY if digest.critical is true and digest.stop_candidate is true.
- Never propose changing order size, leverage, or spreads. Never loosen risk limits.
- Be specific, quantitative, and brief."""


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    # When stdout is a terminal (interactive --once) echo there; when tmux
    # already redirects stdout into LOG_PATH, only write once via the file.
    if sys.stdout.isatty():
        print(line, flush=True)
    try:
        with LOG_PATH.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---- deterministic extractor -------------------------------------------------
def _events_window(events: List[Dict[str, Any]], seconds: float) -> List[Dict[str, Any]]:
    cutoff = time.time() - seconds
    return [e for e in events if float(e.get("ts", 0.0)) >= cutoff]


def _lat(events: List[Dict[str, Any]], kind: str) -> List[float]:
    return [float(e["latency_ms"]) for e in events
            if e.get("event") == kind and e.get("latency_ms") is not None]


def _pct(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] if lo == hi else s[lo] * (hi - k) + s[hi] * (k - lo)


def _round(v: Optional[float], n: int = 2) -> Optional[float]:
    return None if v is None else round(v, n)


def _console_counts(window_s: float) -> Dict[str, int]:
    """Count errors/reject lines in the console log within the window."""
    counts = {"errors": 0, "rejects": 0}
    if not status.CONSOLE.exists():
        return counts
    cutoff = time.time() - window_s
    try:
        with status.CONSOLE.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 300_000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return counts
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    for line in text.splitlines():
        m = pat.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if "ERROR" in line or "CRITICAL" in line or "KILL" in line:
            counts["errors"] += 1
        if "rejected" in line.lower() or "REJECT" in line:
            counts["rejects"] += 1
    return counts


def _position_hold_s(events: List[Dict[str, Any]], current: float) -> float:
    if abs(current) < 1e-12:
        return 0.0
    start: Optional[float] = None
    for e in events:
        if e.get("event") != "equity" or e.get("position") is None:
            continue
        pos = float(e["position"])
        if abs(pos) < 1e-12:
            start = None
        elif start is None:
            start = float(e.get("ts", 0.0))
    return (time.time() - start) if start else 0.0


def _maker_taker(coin: str, window_s: float) -> Dict[str, int]:
    out = {"maker": 0, "taker": 0}
    try:
        from hyperliquid.info import Info
        addr = os.environ.get("HL_ACCOUNT_ADDRESS")
        info = Info("https://api.hyperliquid.xyz", skip_ws=True, timeout=8)
        fills = info.user_fills(addr)
        cutoff_ms = (time.time() - window_s) * 1000
        for f in fills:
            if f.get("coin") != coin or float(f.get("time", 0)) < cutoff_ms:
                continue
            if f.get("crossed"):
                out["taker"] += 1
            else:
                out["maker"] += 1
    except Exception:
        pass
    return out


def build_digest() -> Dict[str, Any]:
    events = status.read_events()
    # Scope the session to the latest launch so pre-restart events do not leak in.
    startup_idx = max((i for i, e in enumerate(events) if e.get("event") == "startup"),
                      default=-1)
    if startup_idx >= 0:
        events = events[startup_idx:]
    coin = os.environ.get("HL_COIN", "HYPE").upper()
    ex = status.exchange_state(coin)
    t = status.tmux_status()
    ev5, ev30 = _events_window(events, 300), _events_window(events, 1800)

    startup = next((e for e in reversed(events) if e.get("event") == "startup"), None)
    start_eq = float(startup["equity"]) if startup and startup.get("equity") else status.DEFAULT_START_EQUITY
    equity = ex.get("equity")
    if equity is None:
        eq = [e for e in events if e.get("event") == "equity"]
        equity = float(eq[-1]["equity"]) if eq else start_eq

    peak = max([start_eq] + [float(e["equity"]) for e in events
                             if e.get("event") == "equity" and e.get("equity") is not None])
    pnl = equity - start_eq
    drawdown = peak - equity

    # True 5-minute PnL delta: last equity mark at/before the 5m boundary.
    cutoff = time.time() - 300
    eq_5m_ago = start_eq
    for e in events:
        if e.get("event") != "equity" or e.get("equity") is None:
            continue
        if float(e.get("ts", 0.0)) <= cutoff:
            eq_5m_ago = float(e["equity"])
    pnl_5m = equity - eq_5m_ago

    pos = sum(p["size"] for p in ex.get("positions", [])) if ex.get("ok") else 0.0
    held_s = _position_hold_s(events, pos)
    upnl = (ex.get("unrealized_pnl") or 0.0) if ex.get("ok") else 0.0

    fills5 = [e for e in ev5 if e.get("event") == "fill"]
    fills30 = [e for e in ev30 if e.get("event") == "fill"]
    mt = _maker_taker(coin, 300)
    if not mt["maker"] and not mt["taker"]:
        mt = {"maker": len(fills5), "taker": 0}

    markouts: Dict[str, Optional[float]] = {}
    for h in (1, 5, 30):
        vals = [float(e["markout_bps"]) for e in ev5
                if e.get("event") == "markout" and int(e.get("horizon_s", 0)) == h]
        if not vals:
            vals = [float(e["markout_bps"]) for e in ev30
                    if e.get("event") == "markout" and int(e.get("horizon_s", 0)) == h]
        markouts[f"mo_{h}s"] = _round(_pct(vals, .5), 2) if vals else None

    mod5, mod30 = _lat(ev5, "modify"), _lat(ev30, "modify")
    p50_5, p95_5 = _pct(mod5, .5), _pct(mod5, .95)
    p50_30, p95_30 = _pct(mod30, .5), _pct(mod30, .95)
    parks5 = sum(1 for e in ev5 if e.get("event") == "park")

    last_ts = max((float(e.get("ts", 0.0)) for e in events), default=None)
    hb = (time.time() - last_ts) if last_ts else None
    cc = _console_counts(300)

    flags: List[str] = []
    if fills5:
        flags.append("fill")
    if (markouts["mo_5s"] is not None and markouts["mo_5s"] < TOXIC_MARKOUT_BPS) or \
       (markouts["mo_30s"] is not None and markouts["mo_30s"] < TOXIC_MARKOUT_BPS):
        flags.append("toxic_markout")
    if abs(pos) > 0 and held_s > STUCK_INVENTORY_S:
        flags.append("stuck_inventory")
    if (p95_5 is not None and p95_5 > LAT_P95_TRIGGER_MS) or parks5 >= PARKS_TRIGGER_5M:
        flags.append("lat_or_park")
    if drawdown > DRAWDOWN_TRIGGER_USD:
        flags.append("drawdown")
    if p50_5 is not None and p50_5 > LAT_P50_DEGRADED_MS:
        flags.append("p50_degraded")
    if p95_5 is not None and p95_5 > LAT_P95_DEGRADED_MS:
        flags.append("p95_degraded")
    if not t["running"] or not t["pids"]:
        flags.append("process_down")
    if hb is not None and hb > DEAD_FEED_S:
        flags.append("feed_stale")

    critical = bool(
        (not t["running"] and abs(pos) > 0)
        or pnl <= -CRITICAL_LOSS_USD
        or (hb is not None and hb > DEAD_FEED_S and abs(pos) > 0)
        or cc["rejects"] >= REJECT_CRITICAL_5M
    )
    stop_candidate = bool(
        (not t["running"] and abs(pos) > 0)
        or pnl <= -CRITICAL_LOSS_USD
        or (hb is not None and hb > DEAD_FEED_S and abs(pos) > 0)
    )

    return {
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%SZ"),
        "coin": coin,
        "proc": {"alive": bool(t["running"] and t["pids"]), "hb_s": _round(hb, 1),
                 "err_5m": cc["errors"], "rej_5m": cc["rejects"]},
        "pnl": {"equity": _round(equity, 5), "net": _round(pnl, 4),
                "pnl_5m": _round(pnl_5m, 4), "dd": _round(drawdown, 4),
                "stop_dist": _round(equity - (start_eq - HARD_STOP_USD), 4)},
        "pos": {"size": _round(pos, 4), "held_s": int(held_s), "upnl": _round(upnl, 4)},
        "exec": {"fills_5m": len(fills5), "fills_30m": len(fills30),
                 "maker": mt["maker"], "taker": mt["taker"],
                 "parks_5m": parks5, **markouts},
        "lat": {"p50_5m": _round(p50_5, 1), "p95_5m": _round(p95_5, 1),
                "p50_30m": _round(p50_30, 1), "p95_30m": _round(p95_30, 1)},
        "flags": flags,
        "critical": critical,
        "stop_candidate": stop_candidate,
    }


# ---- state + tier-2 invocation ----------------------------------------------
def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def anomaly_reasons(d: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    reasons = list(d["flags"])
    return [r for r in reasons if r not in ("p50_degraded", "p95_degraded") or
            (r == "p50_degraded" and d["flags"].count("lat_or_park") == 0)]


def invoke_ai(digest: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps(digest, separators=(",", ":"))
    prompt = f"{SYSTEM_PROMPT}\n\nDIGEST: {payload}"
    t0 = time.time()
    try:
        proc = subprocess.run(
            [os.environ.get("OPENCODE_BIN", "opencode"), "run", "-m", MODEL,
             "--format", "default", prompt],
            cwd=str(ROOT), capture_output=True, text=True, timeout=150,
        )
        raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as exc:
        log(f"AI invoke failed: {type(exc).__name__}: {exc}")
        return {"action": "HOLD_NORMAL", "confidence": 0.0,
                "reason": "AI CLI unavailable; digest logged deterministically",
                "telegram": "", "raw": "", "source": "deterministic",
                "elapsed_s": round(time.time() - t0, 2)}
    text = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    parsed: Optional[Dict[str, Any]] = None
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                parsed, _ = json.JSONDecoder().raw_decode(text[i:])
                if isinstance(parsed, dict):
                    break
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        parsed = {"action": "HOLD_NORMAL", "confidence": 0.0,
                  "reason": "unparseable AI response; deterministic digest logged",
                  "telegram": "", "source": "deterministic"}
    else:
        parsed["source"] = "ai"
    parsed["raw"] = text[-1200:]
    parsed["elapsed_s"] = round(time.time() - t0, 2)
    return parsed


def send_telegram(text: str) -> None:
    if not TG_TOKEN:
        log("telegram skipped: no token")
        return
    for chat_id in TG_CHATS:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json={"chat_id": chat_id, "text": text,
                                    "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=15)
            data = r.json()
            if data.get("ok"):
                log(f"telegram sent chat={chat_id} "
                    f"message_id={data['result']['message_id']}")
            else:
                log(f"telegram send failed chat={chat_id}: {data.get('description')}")
        except Exception as exc:
            log(f"telegram send failed chat={chat_id}: "
                f"{type(exc).__name__}: {exc}")


def emergency_stop(reason: str) -> None:
    """Whitelisted graceful stop: SIGINT the live bot (cancels all + flattens)."""
    log(f"DEFENSIVE ACTION: graceful stop requested ({reason})")
    # Prefer the systemd unit; fall back to the legacy tmux session.
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "mmbot-live-trial.service"],
            timeout=5,
        ).returncode == 0
        if active:
            subprocess.run(["systemctl", "stop", "mmbot-live-trial.service"], timeout=20)
            log("systemd stop sent to mmbot-live-trial.service")
            return
    except Exception as exc:
        log(f"systemd stop failed: {type(exc).__name__}: {exc}")
    try:
        subprocess.run(["tmux", "send-keys", "-t", "live_trial", "C-c"], timeout=5)
    except Exception as exc:
        log(f"tmux stop failed: {type(exc).__name__}: {exc}")


def execute_action(action: str, result: Dict[str, Any], digest: Dict[str, Any]) -> str:
    action = str(result.get("action", "HOLD_NORMAL")).upper()
    if action not in ("HOLD_NORMAL", "WARN_TELEGRAM", "DEFENSIVE_PAUSE", "EMERGENCY_FLATTEN"):
        action = "HOLD_NORMAL"

    # Hard safety gate: the AI can never trigger a stop without deterministic
    # critical confirmation, and can never change size/leverage/spreads.
    if action in ("DEFENSIVE_PAUSE", "EMERGENCY_FLATTEN"):
        if not (digest.get("critical") and digest.get("stop_candidate")):
            log(f"AI requested {action} but deterministic gate refused; downgrading to WARN_TELEGRAM")
            action = "WARN_TELEGRAM"

    reason = str(result.get("reason", ""))[:400]
    tg = str(result.get("telegram", ""))[:900]
    header = f"🧠 AI Quant Review — {digest['coin']}"
    body = (f"<b>{header}</b>\nAction: <b>{action}</b> "
            f"(conf {float(result.get('confidence', 0)):.2f})\n{reason}")
    # Routine HOLD_NORMAL never pushes; it is shown inline in the dashboard
    # panel via logs/ai_sentinel_state.json.  Only material/critical actions buzz.
    if action == "HOLD_NORMAL":
        log(f"AI HOLD_NORMAL: {reason}")
    elif action == "WARN_TELEGRAM":
        send_telegram(body + ("\n\n" + tg if tg else ""))
        log(f"AI WARN_TELEGRAM: {reason}")
    else:
        send_telegram(body + "\n\n⚠️ Executing graceful defensive stop.")
        log(f"AI {action}: {reason}")
        emergency_stop(action)
    return action


# ---- loop --------------------------------------------------------------------
def run_once(force: bool = False) -> Dict[str, Any]:
    digest = build_digest()
    payload = json.dumps(digest, separators=(",", ":"))
    approx_tokens = max(1, len(payload) // 4)
    state = load_state()
    now = time.time()
    last_review = float(state.get("last_review_ts", 0.0) or 0.0)
    scheduled_due = (now - last_review) >= SCHEDULED_REVIEW_S
    reasons = anomaly_reasons(digest, state)
    triggered = bool(force or reasons or scheduled_due)

    log(f"digest tokens~{approx_tokens} bytes={len(payload)} flags={digest['flags']} "
        f"critical={digest['critical']} triggered={triggered} "
        f"(force={force} anomaly={bool(reasons)} scheduled={scheduled_due})")
    if not triggered:
        state["last_digest_ts"] = now
        save_state(state)
        return {"digest": digest, "invoked": False, "action": None}

    result = invoke_ai(digest)
    action = execute_action(str(result.get("action", "")), result, digest)
    state.update({"last_review_ts": now, "last_digest_ts": now,
                  "last_action": action,
                  "last_review_source": str(result.get("source", "ai")),
                  "last_confidence": float(result.get("confidence", 0.0) or 0.0),
                  "last_reason": str(result.get("reason", ""))[:300],
                  "last_telegram": str(result.get("telegram", ""))[:600],
                  "invoke_count": int(state.get("invoke_count", 0)) + 1,
                  "last_elapsed_s": result.get("elapsed_s")})
    save_state(state)
    log(f"AI action={action} elapsed={result.get('elapsed_s')}s reason={result.get('reason')}")
    return {"digest": digest, "invoked": True, "result": result, "action": action}


def main() -> None:
    ap = argparse.ArgumentParser(description="token-efficient AI sentinel")
    ap.add_argument("--once", action="store_true", help="run one check and exit")
    ap.add_argument("--review-now", action="store_true",
                    help="force an on-demand stateless review and notify Telegram")
    ap.add_argument("--digest-only", action="store_true",
                    help="build/log the deterministic digest and refresh its "
                         "timestamp without invoking the AI or taking action")
    args = ap.parse_args()

    if args.digest_only:
        digest = build_digest()
        payload = json.dumps(digest, separators=(",", ":"))
        now = time.time()
        state = load_state()
        state["last_digest_ts"] = now
        state["last_digest_flags"] = digest.get("flags", [])
        state["last_digest_critical"] = bool(digest.get("critical"))
        save_state(state)
        log(f"digest-only tokens~{max(1, len(payload) // 4)} "
            f"bytes={len(payload)} flags={digest['flags']} "
            f"critical={digest['critical']}")
        print(json.dumps(digest, indent=2, default=str))
        return
    log(f"ai_sentinel start model={MODEL} interval={CHECK_INTERVAL_S}s "
        f"review={SCHEDULED_REVIEW_S}s token_cap=250 stateless=true")
    if args.once or args.review_now:
        out = run_once(force=args.review_now)
        print(json.dumps(out, indent=2, default=str))
        return
    while True:
        try:
            run_once()
        except Exception as exc:
            log(f"sentinel cycle error: {type(exc).__name__}: {exc}")
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
