#!/usr/bin/env python3
"""Testnet live-execution launcher (dual-mode 2026-11-16).

Loads testnet credentials, applies the frozen execution/sizing belts from
`.agent/PREREG2_EXECUTION_ADDENDUM_20261107.md`, and starts the production bot (REAL testnet orders on HYPE)
with structured JSONL logging to `logs/live_trial_20261107.jsonl`.

Run under tmux:
    cd /root/hl-mm-bot
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
os.environ["HL_ENV"] = "testnet"   # real TESTNET order execution
os.environ["HL_DRY_RUN"] = "0"
os.environ["HL_FLATTEN_ON_EXIT"] = "1"
os.environ.setdefault("HL_COIN", "BTC")   # testnet: BTC has the deepest two-sided book
os.environ["HL_COIN"] = "BTC"
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
# 10% capital-allocation rule: position/order block <= 10% of live perp equity,
# re-computed at every start so sizing dynamically tracks the account balance.
def _perp_equity(address: str, base_url: str) -> float:
    import json as _json
    import urllib.request as _u
    req = _u.Request(f"{base_url}/info",
                     data=_json.dumps({"type": "clearinghouseState",
                                       "user": address}).encode(),
                     headers={"Content-Type": "application/json"})
    with _u.urlopen(req, timeout=10) as r:
        return float(_json.load(r)["marginSummary"]["accountValue"])


try:
    _eq = _perp_equity(os.environ["HL_ACCOUNT_ADDRESS"],
                       "https://api.hyperliquid-testnet.xyz")
    _cap = round(max(10.0, _eq * 0.10), 2)   # 10% ceiling; floor at min_order_notional
    print(f"[sizing] perp equity={_eq:.2f} -> 10% cap={_cap:.2f} USDC", flush=True)
except Exception as _e:                       # fail safe: fall back to a small fixed cap
    _cap = 11.0
    print(f"[sizing] equity fetch failed ({_e}); using fixed cap=11.0", flush=True)
os.environ["MM_QUOTE_NOTIONAL"] = str(_cap)
os.environ["MM_MIN_ORDER_NOTIONAL"] = "10.0"
os.environ["MM_MAX_NOTIONAL"] = str(_cap)
os.environ["RISK_MAX_ORDER_NOTIONAL"] = str(_cap)
os.environ["RISK_MAX_POSITION_NOTIONAL"] = str(_cap)
os.environ["RISK_EMERGENCY_FLATTEN_NOTIONAL"] = str(_cap)
# Independent hard-loss guard (risk/loss_guard.py): soft -$0.50, hard -$0.80.
# HARD -> cancel all, taker-flatten ladder, write HALT, exit 3 (4 if not flat).
os.environ["RISK_LOSS_GUARD"] = "1"
os.environ["RISK_SOFT_LOSS_USD"] = "0.50"
os.environ["RISK_MAX_LOSS_USD"] = "0.80"
os.environ.setdefault("RISK_HALT_FILE", str(ROOT / "logs" / "HALT"))
# Older account-equity kill switch stays as a looser backstop.
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
