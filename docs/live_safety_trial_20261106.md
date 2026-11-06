# Live safety trial — three controls ported from the strict replay

**Branch:** `live-safety-trial` (based on `main` at `dc0d754`)
**Date:** 2026-11-06
**Scope:** research/engineering only. No live orders, no credentials, no change to
the frozen PREREG2 v1 commit.

This branch ports three controls that the audit found missing or unsafe in
`src/mmbot/`. `main` is untouched; the pre-registered commit still has an empty
`git diff --stat -- src/mmbot/` relative to PREREG2 v1.

## 1. Veto-path pending/resting order purge

**Problem.** The replay exposed a queue race: a risk-increasing order could still
execute after a veto because only a cancel marker was recorded and the queued
place remained. The live analogue is a resting or `PENDING_NEW` order surviving a
`pre_trade` veto on the same side.

**Change.** `Bot._do_places` now calls `_purge_side_exposure(side, now)` whenever
`RiskGuard.pre_trade` rejects a quote. It cancels every active order on that
side (RESTING, PENDING_NEW, PENDING_CANCEL) and increments
`metrics["veto_purges"]`; the metrics line now prints `veto_purges`.

**File:** `src/mmbot/main.py`

## 2. $40 emergency taker flatten

**Problem.** The old kill path only fired at
`hard_position_mult * max_position_notional` (default $120), far above the
$30 strategy cap. A runaway position could sit between $40 and $120 with normal
quoting continuing.

**Change.** `RiskConfig.emergency_flatten_notional` defaults to **$40**.
`RiskGuard.check_kill` returns `KillReason.EMERGENCY_FLATTEN` when
`abs(pos.size * mid) >= emergency_flatten_notional`. That reason routes through
the existing, tested kill path: cancel-all → `gw.flatten()` (market taker) →
verify flat → halt with exit code 2. The absolute `POSITION` kill still wins for
positions above the outer bound.

Env override: `RISK_EMERGENCY_FLATTEN_NOTIONAL=40` (0 disables).

**Files:** `src/mmbot/models.py`, `src/mmbot/risk/guard.py`

## 3. Hard Account Equity Kill-Switch

**Problem.** `RISK_MAX_LOSS_PCT` and `RISK_MAX_DRAWDOWN_PCT` are percentage
budgets. On a small account a percentage is awkward to reason about; there was no
explicit USD loss budget.

**Change.** `RiskConfig.max_account_loss_usd` (**default 0 = disabled**) is
checked first in `check_kill`:

```
start_equity - equity >= max_account_loss_usd  ->  KillReason.MAX_ACCOUNT_LOSS
```

Set it with `RISK_MAX_ACCOUNT_LOSS_USD=<usd>` (e.g. `3.0`). The existing
percentage stops remain active as independent backstops.

**Files:** `src/mmbot/models.py`, `src/mmbot/risk/guard.py`,
`src/mmbot/config.py` (negative values refused at startup).

## Bedrock / default behaviour

| Setting | Default | Effect |
|---|---:|---|
| `RISK_MAX_ACCOUNT_LOSS_USD` | `0.0` | disabled; set deliberately per account |
| `RISK_EMERGENCY_FLATTEN_NOTIONAL` | `40.0` | on; breaks the pre-existing $120 outer kill into an earlier $40 flatten |
| veto purge | always on | no new env var |

`RISK_MAX_ACCOUNT_LOSS_USD=0` keeps the old percentage-only behaviour. The $40
emergency flatten is active by default because it removes an unbounded small-account
gap; set it to `0` only to restore the exact legacy kill thresholds.

## Tests

`tests/test_live_safety_controls.py` adds seven tests:

- USD loss budget triggers `MAX_ACCOUNT_LOSS`, boundary inclusive;
- `0` disables the budget;
- env mapping for both new variables;
- $40 emergency flatten triggers exactly at the boundary and can be disabled;
- a full `cycle()` on a $70 position cancels, taker-flattens, verifies flat, and
  sets `stop_event`;
- a `pre_trade` veto cancels the resting same-side order and records the purge.

Existing deterministic suites (`test_parsers_guard_orders`, `test_bot_scenarios`)
still pass; the outer `POSITION` reason is preserved for positions above the
hard bound.

## Deployment note

The live MM account is unfunded ($0.00), so none of this has been exercised
against mainnet. Enable on testnet first and confirm the `KILL SWITCH:` line,
`veto_purges`, and `verified flat` log before any funded run.
