# Delayed taker-flatten audit (2026-11-06)

## Finding

The previous `scripts/run_strict_engine.py` did **not** execute emergency taker
flattens at all. It scheduled them correctly at
`eff_t = t + current_feed_age_s + latency_s`, but the base replay's
`_advance_to()` only understood `"place"`; every other event (including
`"taker_sell"` / `"taker_buy"`) was routed to `_apply_cancel()`. The subclass's
`_execute()` override was therefore dead code. The counters still incremented,
which made the bug invisible in the printed summary.

Two further problems surfaced during the audit:

1. The engine iterated `iter_records(paths, tars)` (local files before archive
   tars), replaying 2026-10-28..11-05 before 2026-10-20..27. The validation
   suite had already switched to `iter_records_chrono`; the strict engine had
   not.
2. The cooldown plugin clock was `lambda: 0.0`, so the "rolling 3,600 s" HWM was
   actually a session-wide HWM.

## Fix

`scripts/run_strict_engine.py` now:

- uses `mm_grid_search.iter_records_chrono` for strict chronological replay;
- drives the plugin clock from the current record timestamp;
- runs the $30 pre-place soft cap with a veto-path queue purge
  (`_purge_pending_place`);
- schedules a taker flatten at `seen_t + p95_latency` when `abs(pos*mid) >= $40`
  or the rolling loss from the 3,600 s HWM is `>= $0.50`;
- cancels working quotes at the same future stamp, then executes the taker fill
  against the BBO known at that timestamp, charging 4.5 bps;
- has a `full_rules=False` reference mode that reproduces the old soft-cap-only
  cell.

The flatten uses the most recent book at or before `seen_t + p95_latency`
(`_advance_to` fires before the record at that timestamp is applied), so it does
not peek at a book update that had not arrived yet.

## Results (2026-10-20 .. 2026-11-05, 17 days, one pass)

| Variant | Order-path latency | Taker flatten at | Net PnL | Max DD | Max inv | Hard-cap | Hard-stop | Flattens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| soft cap only (reference) | 66 ms | — | **-$0.3226** | 2.0648 | 27.2530 | 0 | 0 | 0 |
| full rule set (p50) | 66 ms | 150 ms | **+$0.4053** | 1.6975 | 30.3329 | 0 | 3 | 3 |
| full rule set (p95) | 150 ms | 150 ms | **+$1.5306** | 1.2764 | 27.2531 | 0 | 2 | 2 |

`hard-cap` never fired because the pre-place $30 soft cap kept position notional
below $40 on this sample; the $0.50 rolling hard stop did fire and produced real
taker fills (volume and fees are in the JSON).

## Immediate vs delayed flatten check

The frozen rule set is **delayed**: the flatten fills at `seen_t + p95_latency`
(150 ms after the decision is seen), against the last book at or before that
stamp. A diagnostic immediate variant fills at the current BBO with no wait. Both
charge 4.5 bps taker and use the same triggers, soft cap and cooldown:

| Flatten timing | Net PnL | Max DD | Flattens |
|---|---:|---:|---:|
| immediate (current BBO, diagnostic only) | +$0.4239 | 1.6788 | 3 |
| `seen_t + 150 ms` (frozen) | +$0.4053 | 1.6975 | 3 |

The delayed rule is slightly worse in-sample, so the frozen choice is the
conservative one. Source: `logs/mm_strict_v2_immediate_diagnostic_20261106.json`.

## What the old `-$0.3226` meant

`-$0.3226` was the `20/6/$30` **pre-place-soft-cap-only** cell at 66 ms in the
strict hard-cap sweep (`logs/mm_hardcap_recheck_20261105.json`,
`cap_20_6_30_l66`). It never had a working taker flatten. After the
chronological-order and plugin-clock bugs were fixed, the corrected
soft-cap-only reference reproduces the old cell **exactly**: net `-$0.3226477`,
fees `$0.4978236`, funding `-$0.0244540`, 263 trades and 15 triggers all match
`cap_20_6_30_l66`. That exact match validates the corrected engine. The full
rule set's true in-sample baselines are `+$0.4053` (p50) and `+$1.5306` (p95).

Machine-readable: `logs/mm_strict_v2_baseline_20261106.json`.
