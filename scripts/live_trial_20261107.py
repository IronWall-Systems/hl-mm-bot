#!/usr/bin/env python3
"""Live micro-notional trial launcher (2026-11-07).

Loads mainnet credentials, applies the frozen execution/sizing belts from
`.agent/PREREG2_EXECUTION_ADDENDUM_20261107.md`, and starts the production bot
with structured JSONL logging to `logs/live_trial_20261107.jsonl`.

Run under tmux:
    cd /root/hl-mm-bot-live-safety-trial
    PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/live_trial_20261107.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT / ".env", Path("/root/hl-mm-bot/.env")):
    if candidate.exists():
        load_dotenv(candidate, override=False)

# ---- mainnet + live ---------------------------------------------------------
os.environ["HL_ENV"] = "mainnet"
os.environ["HL_CONFIRM_MAINNET"] = "YES_ISOLATED_WALLET"
os.environ["HL_DRY_RUN"] = "0"
os.environ["HL_FLATTEN_ON_EXIT"] = "1"
os.environ.setdefault("HL_COIN", "HYPE")
os.environ["HL_COIN"] = "HYPE"
os.environ["HL_JSONL_LOG"] = str(ROOT / "logs" / "live_trial_20261107_hype.jsonl")

# ---- budget-aware batch quoting (validated by the 2026-11-09 dry-run) -------
os.environ.setdefault("MM_BATCH_PLANNER", "1")
os.environ.setdefault("MM_BATCH_COST_MODE", "leg")

# ---- lean 3-signal microstructure upgrade (2026-11-07, see PREREG2_MICROSTRUCTURE_UPGRADE) ----
# Volatility-calibrated width (2026-11-09): HYPE realised sigma is 8.2-12 bps
# per sqrt(min).  Base 15 bps sits outside that noise; near_max caps the tight
# edge at H - 5 bps (10 bps), far_min keeps the deferral edge at 20 bps.
# skew_bps=6.5 tightens the reducing side by ~6.5 bps at full inventory so a
# fill exits at ~+8.5 bps over mid while the adding side widens to ~21.5 bps.
os.environ["MM_HALF_SPREAD_BPS"] = "15.0"
os.environ["MM_BAND_HALF_SPREAD_BPS"] = "15.0"
os.environ["MM_BAND_NEAR_MAX_BPS"] = "5.0"
os.environ["MM_BAND_FAR_MIN_BPS"] = "20.0"
os.environ["MM_BAND_PULL_FRAC"] = "0.45"
os.environ["MM_SKEW_BPS"] = "6.5"
os.environ["MM_REQUOTE_THRESHOLD_BPS"] = "2.0"
os.environ["MM_MIN_REQUOTE_INTERVAL_S"] = "1.0"
os.environ["MM_SIZE_TOLERANCE"] = "0.5"
# Only the three statistically passing signals are enabled.
os.environ["MM_MICRO_ENABLED"] = "1"
os.environ["MM_W_OBI_BPS"] = "1.5"
os.environ["MM_W_TFI_BPS"] = "1.0"
os.environ["MM_W_BTC_BPS"] = "0.7"
os.environ["MM_TOX_OBI"] = "0.45"
os.environ["MM_TOX_TFI"] = "0.65"
os.environ["MM_TOX_BTC_BPS"] = "3.0"
os.environ["MM_TOX_WIDEN_MULT"] = "2.5"
os.environ["MM_WALL_MIN_BPS"] = "12.0"
os.environ["MM_WALL_MAX_BPS"] = "22.0"

# ---- execution sizing + hard risk belts -------------------------------------
os.environ["MM_QUOTE_NOTIONAL"] = "12.0"
os.environ["MM_MIN_ORDER_NOTIONAL"] = "10.0"
os.environ["MM_MAX_NOTIONAL"] = "24.0"
os.environ["RISK_MAX_ORDER_NOTIONAL"] = "15.0"
os.environ["RISK_MAX_POSITION_NOTIONAL"] = "30.0"
os.environ["RISK_EMERGENCY_FLATTEN_NOTIONAL"] = "30.0"
# Hard stop on $21.12 equity: $2.00 absolute loss -> floor $19.12.  The
# percentage rules are looser backstops; the USD rule binds first (overnight).
os.environ["RISK_MAX_ACCOUNT_LOSS_USD"] = "2.00"
os.environ["RISK_MAX_DRAWDOWN_PCT"] = "0.10"
os.environ["RISK_MAX_LOSS_PCT"] = "0.10"
# Stale-feed watchdog / disruption belts.
os.environ["RISK_STALE_BOOK_S"] = "3.0"
os.environ["RISK_MAX_LATENCY_MS"] = "1500"
os.environ["RISK_LATENCY_BREACH_COUNT"] = "5"
os.environ["RISK_MAX_CONSECUTIVE_ERRORS"] = "8"

sys.path.insert(0, str(ROOT / "src"))
from mmbot.main import main  # noqa: E402

if __name__ == "__main__":
    main()
