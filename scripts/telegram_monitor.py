#!/usr/bin/env python3
"""Standalone read-only Telegram monitor for the live micro-notional trial.

Runs beside the trading process; it only reads logs and queries the exchange
read-only.  It never imports the trading loop and never sends orders.

Features:
  * inline glass buttons: refresh status / latency & fills detail
  * automatic 30-minute heartbeat status
  * instant alerts for fills, emergency OTM parks, and safety-belt hits
  * /start and /status commands

Run:
    PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/telegram_monitor.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / ".env", Path("/root/hl-mm-bot/.env")):
    if candidate.exists():
        load_dotenv(candidate, override=False)

sys.path.insert(0, str(ROOT / "scripts"))
import status_live_trial as status  # noqa: E402

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


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


# Every ID allowed to read the dashboard and send commands.
AUTH_USER_IDS = {6029973681, 7752127491}

# Dashboard + alerts fan out to all operators.  TELEGRAM_CHAT_IDS overrides
# TELEGRAM_CHAT_ID; authorized operators are always included so a newly
# authorized ID also receives panels and alerts.
CHAT_IDS = (_parse_ids(os.environ.get("TELEGRAM_CHAT_IDS", ""))
            or _parse_ids(os.environ.get("TELEGRAM_CHAT_ID", "6029973681")))
for _uid in sorted(AUTH_USER_IDS):
    if _uid not in CHAT_IDS:
        CHAT_IDS.append(_uid)

HEARTBEAT_S = 1800
INIT_RETRY_MIN_S = 15.0
INIT_RETRY_MAX_S = 900.0
API = f"https://api.telegram.org/bot{TOKEN}"
JSONL = status.JSONL
CONSOLE = status.CONSOLE

KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🔄 Refresh Status Now", "callback_data": "refresh_status"}],
        [{"text": "📊 Latency & Fills Detail", "callback_data": "detail_metrics"}],
        [{"text": "🧠 AI Quant Review", "callback_data": "ai_review"}],
    ]
}

# One dashboard message per chat, edited in place on every refresh/review.
_ENV_PANEL = os.environ.get("TG_PANEL_MESSAGE_ID", "").strip()
PANEL_MESSAGE_IDS: Dict[int, Optional[int]] = {}
if _ENV_PANEL.isdigit() and CHAT_IDS:
    PANEL_MESSAGE_IDS[CHAT_IDS[0]] = int(_ENV_PANEL)
LAST_PANEL_REVIEW_TS = 0.0


def _api(method: str, payload: Optional[Dict[str, Any]] = None,
         timeout: float = 35.0) -> Optional[Dict[str, Any]]:
    try:
        r = requests.post(f"{API}/{method}", json=payload or {}, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            print(f"[tg] {method} failed: {data.get('description')}", file=sys.stderr, flush=True)
        return data
    except Exception as exc:
        print(f"[tg] {method} error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return None


def send_status(chat_id: int, detail: bool = False,
                edit_message_id: Optional[int] = None) -> Optional[int]:
    text = status.render_status(detail=detail)
    if edit_message_id is not None:
        data = _api("editMessageText", {
            "chat_id": chat_id, "message_id": edit_message_id,
            "text": text, "parse_mode": "HTML", "reply_markup": KEYBOARD,
            "disable_web_page_preview": True,
        })
        if data and data.get("ok"):
            PANEL_MESSAGE_IDS[chat_id] = edit_message_id
            return edit_message_id
        if data and "message is not modified" in str(data.get("description", "")):
            # Nothing changed: keep the same panel, do not send a new one.
            PANEL_MESSAGE_IDS[chat_id] = edit_message_id
            return edit_message_id
    data = _api("sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "reply_markup": KEYBOARD, "disable_web_page_preview": True,
    })
    if data and data.get("ok"):
        mid = int(data["result"]["message_id"])
        PANEL_MESSAGE_IDS[chat_id] = mid
        return mid
    return None


def send_status_all(detail: bool = False) -> Dict[int, Optional[int]]:
    """Refresh the dashboard in every operator chat, editing in place."""
    return {chat_id: send_status(chat_id, detail=detail,
                                 edit_message_id=PANEL_MESSAGE_IDS.get(chat_id))
            for chat_id in CHAT_IDS}


def _run_ai_review(chat_id: int, panel_message_id: Optional[int]) -> None:
    """Run the stateless review, then fold the result into the dashboard panel."""
    global LAST_PANEL_REVIEW_TS
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ai_sentinel.py"), "--review-now"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=200,
        )
    except Exception as exc:
        print(f"[tg] ai_review failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    ai = status.read_ai_review()
    if ai:
        LAST_PANEL_REVIEW_TS = ai["ts"]
    target = (panel_message_id if panel_message_id is not None
              else PANEL_MESSAGE_IDS.get(chat_id))
    if target is not None:
        send_status(chat_id, edit_message_id=target)


def send_alert(title: str, body: str) -> None:
    text = f"🚨 <b>{title}</b>\n{body}"
    for chat_id in CHAT_IDS:
        _api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                             "reply_markup": KEYBOARD,
                             "disable_web_page_preview": True})


def _tail_new(path: Path, offset: int) -> tuple[int, list[str]]:
    if not path.exists():
        return offset, []
    try:
        size = path.stat().st_size
        if size < offset:      # rotated/truncated
            offset = 0
        if size == offset:
            return offset, []
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        return size, chunk.decode("utf-8", "replace").splitlines()
    except OSError:
        return offset, []


class Tailer(threading.Thread):
    """Watch the JSONL + console and push instant alerts (1 s cadence)."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="tg-tailer")
        self.jsonl_off = JSONL.stat().st_size if JSONL.exists() else 0
        self.console_off = CONSOLE.stat().st_size if CONSOLE.exists() else 0
        self.last_belt_alert = 0.0
        self.last_health_alert = 0.0
        self.seen_console: set = set()

    def _start_equity(self) -> float:
        ev = status.read_events()
        startup = next((e for e in ev if e.get("event") == "startup"), None)
        return float(startup["equity"]) if startup and startup.get("equity") else status.DEFAULT_START_EQUITY

    def _alert_belt(self, equity: float) -> None:
        now = time.time()
        if now - self.last_belt_alert < 300:
            return
        start = self._start_equity()
        stop = start - status.HARD_STOP_USD
        self.last_belt_alert = now
        send_alert("SAFETY BELT / HARD STOP",
                   f"Equity ${equity:.5f} is at/below the ${stop:.5f} hard stop "
                   f"(start ${start:.5f}, loss ${start - equity:.5f}).")

    def run(self) -> None:
        while True:
            try:
                self.jsonl_off, lines = _tail_new(JSONL, self.jsonl_off)
                for line in lines:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = e.get("event")
                    if kind == "fill":
                        send_alert("FILL", f"{e.get('side')} {e.get('size')} @ {e.get('px')} "
                                           f"· fee ${float(e.get('fee') or 0):.5f} · pos {e.get('pos')}")
                    elif kind == "park":
                        send_alert("EMERGENCY OTM PARK",
                                   f"{e.get('side')} parked to {e.get('price')} × {e.get('size')} "
                                   f"· {e.get('latency_ms')} ms · new oid {e.get('new_oid')}")
                    elif kind == "equity" and e.get("equity") is not None:
                        if float(e["equity"]) <= self._start_equity() - status.HARD_STOP_USD:
                            self._alert_belt(float(e["equity"]))

                self.console_off, clines = _tail_new(CONSOLE, self.console_off)
                for line in clines:
                    if "KILL SWITCH" in line or "CRITICAL" in line:
                        key = line[:160]
                        if key in self.seen_console:
                            continue
                        self.seen_console.add(key)
                        if len(self.seen_console) > 500:
                            self.seen_console.clear()
                        send_alert("SAFETY BELT / KILL SWITCH", line.strip()[:900])
            except Exception:
                traceback.print_exc()
            time.sleep(1.0)


def _authorized(user_id: Any) -> bool:
    try:
        return int(user_id) in AUTH_USER_IDS
    except (TypeError, ValueError):
        return False


def handle_update(upd: Dict[str, Any]) -> bool:
    msg = upd.get("message")
    if msg:
        if not _authorized((msg.get("from") or {}).get("id")):
            return False                 # silently ignore unauthorized users
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        if not chat_id:
            return False
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/start") or text.startswith("/status"):
            return send_status(chat_id,
                               edit_message_id=PANEL_MESSAGE_IDS.get(chat_id)) is not None
        return False

    cb = upd.get("callback_query")
    if cb:
        if not _authorized((cb.get("from") or {}).get("id")):
            _api("answerCallbackQuery", {"callback_query_id": cb.get("id"),
                                         "text": "Unauthorized"})
            return False
        chat_id = int(((cb.get("message") or {}).get("chat") or {}).get("id") or 0)
        data = cb.get("data")
        mid = (cb.get("message") or {}).get("message_id")
        if data == "ai_review":
            _api("answerCallbackQuery", {
                "callback_query_id": cb.get("id"),
                "text": "Running AI Quant Review (~4s)...",
            })
            # Run out-of-band, then edit the existing panel in place.
            threading.Thread(target=_run_ai_review, args=(chat_id, mid),
                             daemon=True).start()
            return False
        _api("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        if data == "refresh_status":
            return send_status(chat_id, edit_message_id=mid) is not None
        if data == "detail_metrics":
            return send_status(chat_id, detail=True, edit_message_id=mid) is not None
    return False


def main() -> None:
    global LAST_PANEL_REVIEW_TS
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")
    me = _api("getMe", timeout=10)
    if not me or not me.get("ok"):
        raise SystemExit("Telegram getMe failed; check TELEGRAM_BOT_TOKEN")
    print(f"[tg] connected as @{me['result'].get('username')}", flush=True)

    Tailer().start()
    sent = send_status_all()
    print(f"[tg] initial status sent to "
          f"{ {cid: mid for cid, mid in sent.items() if mid is not None} }", flush=True)
    ai0 = status.read_ai_review()
    if ai0:
        LAST_PANEL_REVIEW_TS = ai0["ts"]
    last_heartbeat = time.time()
    # Per-chat retry schedule for operators who have not pressed Start yet.
    init_retry_at = {cid: time.time() + INIT_RETRY_MIN_S
                     for cid in CHAT_IDS
                     if PANEL_MESSAGE_IDS.get(cid) is None}
    init_retry_delay = {cid: INIT_RETRY_MIN_S for cid in init_retry_at}

    offset = 0
    while True:
        try:
            data = _api("getUpdates", {"offset": offset, "timeout": 30,
                                       "allowed_updates": ["message", "callback_query"]},
                        timeout=40)
            for upd in (data or {}).get("result", []):
                offset = max(offset, int(upd["update_id"]) + 1)
                handle_update(upd)
            # Pick up scheduled/anomaly reviews written by the sentinel daemon
            # and fold them into every existing panel (never a new chat message).
            ai = status.read_ai_review()
            if ai and ai["ts"] > LAST_PANEL_REVIEW_TS:
                LAST_PANEL_REVIEW_TS = ai["ts"]
                for chat_id, mid in list(PANEL_MESSAGE_IDS.items()):
                    if mid is not None:
                        send_status(chat_id, edit_message_id=mid)
            # Telegram forbids bot-initiated chats: retry the first status for
            # any operator who has not pressed Start / sent /start yet, with
            # exponential backoff so an unreachable chat cannot spam the log.
            now = time.time()
            for chat_id in CHAT_IDS:
                if PANEL_MESSAGE_IDS.get(chat_id) is not None:
                    continue
                if now < init_retry_at.get(chat_id, now):
                    continue
                mid = send_status(chat_id)
                if mid is not None:
                    last_heartbeat = now
                    init_retry_at.pop(chat_id, None)
                    init_retry_delay.pop(chat_id, None)
                    print(f"[tg] deferred initial status delivered "
                          f"chat={chat_id} message_id={mid}", flush=True)
                else:
                    delay = min(init_retry_delay.get(chat_id, INIT_RETRY_MIN_S) * 2,
                                INIT_RETRY_MAX_S)
                    init_retry_delay[chat_id] = delay
                    init_retry_at[chat_id] = now + delay
            if time.time() - last_heartbeat >= HEARTBEAT_S:
                send_status_all()
                last_heartbeat = time.time()
        except KeyboardInterrupt:
            break
        except Exception:
            traceback.print_exc()
            time.sleep(3.0)


if __name__ == "__main__":
    main()
