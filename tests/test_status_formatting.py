"""Formatting and AI-review timestamp regressions for the status panel."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import status_live_trial as status  # noqa: E402


def _ts(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def test_fmt_usd_puts_sign_before_dollar_and_rounds():
    assert status.fmt_usd(-0.08748, 2, signed=True) == "-$0.09"
    assert status.fmt_usd(-0.06144, 2, signed=True) == "-$0.06"
    assert status.fmt_usd(0.0, 2, signed=True) == "+$0.00"
    assert status.fmt_usd(-0.00004, 2, signed=True) == "+$0.00"
    assert status.fmt_usd(-0.0054, 4, signed=True) == "-$0.0054"
    assert status.fmt_usd(21.154264, 5) == "$21.15426"
    assert status.fmt_usd(None, 2, signed=True) == "n/a"


def test_deterministic_fallback_uses_latest_digest_check(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    log = tmp_path / "sentinel.log"
    state.write_text(json.dumps({
        "last_review_ts": _ts("2026-11-09T20:28:54Z"),
        "last_digest_ts": _ts("2026-11-10T08:50:57Z"),
        "last_action": "HOLD_NORMAL",
        "last_reason": "AI CLI unavailable; digest logged deterministically",
        "last_confidence": 0.0,
    }))
    log.write_text(
        "2026-11-09T20:28:54Z digest tokens~113 bytes=452 flags=['drawdown'] critical=True\n"
        "2026-11-09T20:28:54Z AI invoke failed: FileNotFoundError: 'opencode'\n"
        "2026-11-09T20:28:54Z AI action=HOLD_NORMAL elapsed=0.0s "
        "reason=AI CLI unavailable; digest logged deterministically\n"
        "2026-11-10T08:50:57Z digest-only tokens~116 bytes=464 "
        "flags=['stuck_inventory'] critical=False\n"
    )
    monkeypatch.setattr(status, "AI_STATE", state)
    monkeypatch.setattr(status, "AI_LOG", log)

    ai = status.read_ai_review()
    assert ai is not None
    assert ai["source"] == "deterministic"
    assert ai["ts"] == _ts("2026-11-10T08:50:57Z")
    assert ai["check_ts"] == _ts("2026-11-10T08:50:57Z")
    assert ai["review_ts"] == _ts("2026-11-09T20:28:54Z")


def test_real_review_keeps_review_timestamp(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    log = tmp_path / "sentinel.log"
    state.write_text(json.dumps({
        "last_review_ts": _ts("2026-11-10T07:00:00Z"),
        "last_digest_ts": _ts("2026-11-10T08:50:57Z"),
        "last_action": "WARN_TELEGRAM",
        "last_review_source": "ai",
        "last_reason": "Toxic markout",
        "last_confidence": 0.7,
    }))
    log.write_text("2026-11-10T08:50:57Z digest-only tokens~116 bytes=464 "
                   "flags=[] critical=False\n")
    monkeypatch.setattr(status, "AI_STATE", state)
    monkeypatch.setattr(status, "AI_LOG", log)

    ai = status.read_ai_review()
    assert ai is not None
    assert ai["source"] == "ai"
    assert ai["ts"] == _ts("2026-11-10T07:00:00Z")
    assert ai["check_ts"] == _ts("2026-11-10T08:50:57Z")


def test_log_only_digest_is_shown_as_deterministic(tmp_path, monkeypatch):
    log = tmp_path / "sentinel.log"
    log.write_text("2026-11-10T08:50:57Z digest-only tokens~116 bytes=464 "
                   "flags=[] critical=False\n")
    monkeypatch.setattr(status, "AI_STATE", tmp_path / "missing.json")
    monkeypatch.setattr(status, "AI_LOG", log)

    ai = status.read_ai_review()
    assert ai is not None
    assert ai["source"] == "deterministic"
    assert ai["action"] == "HOLD_NORMAL"
    assert ai["ts"] == _ts("2026-11-10T08:50:57Z")
    assert ai["review_ts"] is None
