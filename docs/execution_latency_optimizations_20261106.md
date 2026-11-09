# Execution-latency optimisations — implementation, live verification, replay

Date: 2026-11-06
Branch: `live-safety-trial` (worktree `/root/hl-mm-bot-live-safety-trial`)
Base commit: `0578aeb`

`/.agent/PREREG2.md` was **not** modified (mode 444, clean in git).

## 1. What changed

The live path used to do one L1 action per leg and cancel-then-place for every
requote.  Measured mainnet latencies: `Alo` place ~365–392 ms, `batchModify`
~388–410 ms, cancel ~668–735 ms, `Gtc` place ~678–753 ms.  A two-leg
`batchModify` has p50 409.9 ms versus ~2.1 s for cancel+place.

Nine changes:

1. `OrderStatus.PENDING_MODIFY` added and made an active status.
2. `Plan` gained `modifies: List[Modify]`; a requote now emits a modify, not a cancel.
3. `OrderManager.on_modify_result()` rotates the oid (old oid removed from
   `_by_oid`, new oid mapped to the same local order).
4. Gateway `bulk_place()` — one `bulk_orders` action for all legs.
5. Gateway `modify_quotes()` — one `batchModify` action for all legs.
6. Gateway `cancel_oids()` / `cancel_cloids()` — one `bulk_cancel` /
   `bulk_cancel_by_cloid` action per id kind.
7. Orchestrator `_apply_plan()`: modifies → cancels → places, each bucket at
   most one L1 action; `_do_cancels()` splits oid-bearing and cloid-only orders.
8. Orphan sweep uses one bulk oid cancel.
9. `install_monotonic_nonce()` patches the SDK's `get_timestamp_ms` so
   back-to-back actions never reuse a nonce; `Info`/`Exchange` get a 10 s
   timeout and share cached `meta`/`spot_meta`.

## 2. Tests

New `tests/test_execution_optimizations.py` (9 tests):

* bulk place / modify / cancel response parsers, including a short response
  (never misaligns legs);
* oid rotation and retirement of the old oid mapping, including a late
  `order_update` on the old oid being ignored;
* one L1 action per bucket (`bulk_place_calls`, `modify_quotes_calls`,
  `bulk_cancel_calls`);
* oid cancels on `cancel_oids`, cloid-only cancels on `cancel_cloids`;
* strictly increasing, idempotently installed monotonic nonce.

Updated existing requote/slow-gate tests to the modify semantics.

Full suite: **225 passed, 0 failed** (`PYTHONPATH=src venv/bin/python -m pytest -q tests/`).
`test_collector.py` needed the untracked deployment artifact `data/coins.json`
(Phase A selection); it was created locally and is not a tracked change.

## 3. Live mainnet verification

Script: `scripts/verify_execution_optimizations.py` (production
`HyperliquidGateway`, not the fake).  Five cycles, each:

1. one `bulk_orders` places a 2-leg deep-OTM `Alo` pair (~$11.50/leg;
   BUY at 50% of mid, SELL at 150% of mid);
2. one `batchModify` reprices both legs;
3. one `bulk_cancel` removes both new oids.

| bucket | n | p50 (ms) | p95 (ms) |
|---|---:|---:|---:|
| bulk place | 5 | 436.5 | 465.8 |
| batchModify | 5 | **409.8** | 533.8 |
| bulk cancel | 5 | 685.8 | 773.4 |

* oids rotated on every cycle, e.g. `596921662037/…038 -> 596921665551/…552`;
* fill count unchanged (4 → 4), zero fills/fees from these orders;
* final `open_orders = []` and no leftovers after the run.

JSON: `logs/execution_optimizations_live_20261106_100814.json`.

## 4. Strict-engine replay at real modify latency

`scripts/run_strict_engine.py`, window 2026-10-20..2026-11-05, 16,693,596 records.
`--p95-ms` was set equal to `--p50-ms` so `full_p50` and `full_p95` coincide.

| run | variant | Net PnL | Max DD | Fills | Trades | Hard-stops | Taker flattens |
|---|---|---:|---:|---:|---:|---:|---:|
| 410 ms | full | −0.6678 | 2.1217 | 321 | 220 | 3 | 3 |
| 410 ms | soft-only | −0.4162 | 2.2749 | 412 | 312 | 0 | 0 |
| 520 ms | full | −0.2119 | 1.9855 | 293 | 205 | 3 | 3 |
| 520 ms | soft-only | −0.5651 | 2.4877 | 405 | 300 | 0 | 0 |

Hard inventory cap triggers were 0 in all runs.  Net PnL is **not monotonic**
in latency: 520 ms is better than 410 ms for the full rule set, driven by a
different fill/cooldown path (fills 293 vs 321, soft vetoes 399k vs 532k).
Both remain negative in this window.

JSON: `logs/mm_strict_latency_410_20261106.json`,
`logs/mm_strict_latency_520_20261106.json` (in the main worktree `logs/`).

## 5. Latency-model limitation (PREREG2 amendment input)

The replay models a **single** order-path delay, `latency_ms`
(`ReplayEngine.__init__` → `self.latency_s`).  Every cancel and every place is
scheduled at `eff = seen_t + latency_s` in `_maybe_decide`; the same value is
also passed as `feed_age_ms`.  The engine has no `modify` action at all —
requotes are always cancel+place.  `p95_latency_s` is used only for the
delayed taker flatten, not for normal quotes.

So the current script **cannot** model separate place/modify vs cancel
latencies (410 ms vs ~690 ms).  The 410/520 runs above therefore apply the
modify latency to cancels too, which is optimistic for cancel-heavy paths.
A PREREG2 amendment would need distinct `place_latency_s`,
`modify_latency_s`, `cancel_latency_s` (and a modify action) before the
replay can price the new execution path faithfully.  No amendment was made.
