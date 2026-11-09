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
