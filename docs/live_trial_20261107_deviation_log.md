# Live trial 2026-11-07 — deviation log

The frozen addendum `.agent/PREREG2_EXECUTION_ADDENDUM_20261107.md` remains
untouched at mode 444. This file logs the code change made during the first
live minutes, as required by its governance section.

## Run 1 — aborted after finding duplicate resting orders

* Session: tmux `live_trial`, started `2026-11-07 04:44:14 UTC`.
* Account preflight and startup clean: equity `$24.29537`, 0 positions, 0 orders.
* First quote pair placed normally (2 legs, `$11.36–$11.40` each).
* On the first `batchModify`, the exchange emitted, for each leg:
  * `open` on the **new** oid, and
  * `canceled` on the **old** oid,
  * both carrying the **same cloid**.
* `OrderManager._find()` matched by cloid first, so the stale old-oid `canceled`
  event marked the live order `CANCELED`. The next cycle then placed a new pair
  while the modified pair was still resting. Within two requotes the exchange
  held **6 resting orders** (3 duplicate pairs).
* No fills occurred; equity stayed `$24.29537`.
* The bot was stopped with SIGINT; shutdown cancelled all 6 orders in one
  `bulk_cancel` and verified flat.

Run 1 logs are archived:

* `logs/live_trial_20261107_run1_aborted.jsonl`
* `logs/live_trial_20261107_run1_aborted.console.log`

## Fix

`src/mmbot/execution/order_manager.py`:

* `_find()` now treats oid as authoritative when present and only falls back to
  cloid for an order that has not yet received an oid.
* `on_order_update()` ignores a retired old-oid event unless it is a fill on the
  new oid.
* `on_fill()` adopts a new post-modify oid if the fill races ahead of the modify
  ack, retiring the old mapping.

Regression test added:
`tests/test_parsers_guard_orders.py::test_batch_modify_old_oid_cancel_does_not_kill_new_oid`.

Full suite after the fix: **228 passed** (the addendum's pre-fix count of 227 is
superseded by this logged change).

## Run 2 — live

* Restarted `2026-11-07 04:48:05 UTC` from a verified-flat account.
* Open orders stayed at exactly **2** (one bid, one ask) across repeated
  `batchModify` cycles.
* No fills, no fees, equity unchanged at `$24.29537` through the first ~5
  minutes.
* Observed `batchModify` p50 `400.1 ms`, p95 `444.6 ms`; `Alo` bulk place
  `426.9 ms`.

## Run 3 — protocol amendment: 30-fill halt rule waived (2026-11-10)

* **Amendment ID:** `AMD-20261110-01`
* **Logged:** `2026-11-10 20:01 UTC` (live session started `2026-11-10 10:04:04 UTC`)
* **Authority:** explicit operator instruction in-session.

### Trigger

The pre-registered rule — *halt after 30 fills if average net PnL per fill is
negative* — was satisfied and not acted on at the time:

| Fill # | Time (UTC) | Cumulative net | Avg net/fill |
|--------|-----------|----------------|--------------|
| 30 | 15:40:52 | **−$0.1063** | **−$0.00354** |
| 40 | 15:56:52 | −$0.0379 | −$0.00095 |
| 50 | 17:38:12 | **+$0.0378** | **+$0.00076** |

The 6-hour window (10:04→16:04 UTC) closed with **40 fills, net −$0.0379**
(gross +$0.0267, fees $0.0646). The manual halt that the rule called for at
~15:40 UTC did not occur. This entry records that fact rather than papering
over it.

### Decision

The operator **waives the manual 30-fill halt rule** for this run. Rationale:
`loss_guard` is active and strictly enforces the **−$0.50 soft / −$0.80 hard**
account-equity floor on every book tick, so the equity stop — not the fill
count — is the binding control. The trial continues under `loss_guard` plus the
**72-hour** time limit.

### Scope — what is *not* waived

* Soft `−$0.50` / hard `−$0.80` thresholds and all `loss_guard` behaviour
  (SOFT reduce-only, PULL cancel, HARD cancel→flatten→HALT→exit 3/4) unchanged.
* No parameter changes; the frozen trial parameters remain in force.
* The 72-hour time limit remains.
* `Restart=no`, `RestartPreventExitStatus=2 3 4`, and the `ExecStartPre` HALT
  check remain.
* Future fills are not exempt from scrutiny: a *new* negative stretch should be
  reviewed on its own merits, not covered by this waiver.

### State at logging

* Service untouched: `mmbot-live-trial.service`, PID `111775`, same process
  since `10:04:04 UTC`, `NRestarts=0`, uptime ~10 h. No restart performed to
  record this amendment.
* Equity `$21.13877`; net vs baseline `$21.018501` = **+$0.1203**.
* Realized (avg-cost, net of fees) **+$0.1181** over **54 fills**
  (avg **+$0.00219**/fill); unrealized **+$0.00225**; position `−0.010000` HYPE.
* `guard=ok`, `guard_loss=−0.119`, `hard_stops=0`, `guard_pulls=0`; no HALT.

### Caveat

`loss_guard` bounds *account equity* at the poll/book cadence; it is not a
guarantee of a $1.00 maximum loss. A gap through the stop can still slip past
the −$0.80 floor. This waiver accepts that residual risk knowingly.
