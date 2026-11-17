# PREREG2 v3 input — split L1 latency model and Tokyo replay

Date: 2026-11-06
Repo: `/root/hl-mm-bot`
Scope: research replay only; no live trading code changed.
`.agent/PREREG2.md` remains frozen (mode 444, untouched).

## 1. Replay model change

`ReplayEngine` / `ContinuousCooldownEngine` now separate the three timing
components that were previously collapsed into `latency_ms`:

| parameter | meaning |
|---|---|
| `feed_age_ms` | exchange timestamp → bot visibility (market-data transit) |
| `place_modify_latency_ms` | fast L1 path: `Alo` bulk place, two-leg `batchModify`, OTM park |
| `cancel_latency_ms` | slow L1 path: raw `bulk_cancel` |

Implementation points:

* `place_ready_time(seen_t) = seen_t + place_modify_latency_s`;
  `cancel_ready_time(seen_t) = seen_t + cancel_latency_s`.
* Requotes are modelled as a `batchModify` pair and therefore use the fast path
  for both the pull and the replacement leg.
* Cancel-only actions (no target / risk-off pull) use the slow cancel path by
  default, unless `--park-via-modify` is set, which models the emergency OTM
  park via `batchModify`.
* The taker flatten now waits for `max(seen_t + p95_latency, park_ready_time)`
  so the quotes are cleared before the flatten executes.
* Backward compatibility: if the new parameters are omitted, both L1 paths
  fall back to `latency_ms`, and the taker flatten keeps the old timing.

### Feed-age source (critical)

Collector-v2 records carry `r` (local receive time).  In this archive
`r - t` has p50 ≈ **395 ms**, p90 ≈ 575 ms, p95 ≈ 733 ms — it is the collector
host path, not warm Tokyo bot transit.  The old code always used `r - t`, which
silently re-inflated feed age to ~400–700 ms even when a small `feed_age_ms`
was requested.

New `--feed-age-source fixed|recorded`:

* `recorded` (default, backward compatible): use each record's `r - t`;
* `fixed`: force the configured `feed_age_ms` / p95 value.

All Tokyo profiles below use `--feed-age-source fixed`; a profile run without
it is not a 10/25 ms feed model.

CLI additions in both `scripts/run_strict_engine.py` and
`scripts/replay_backtest.py`:

```
--feed-age-ms / --feed-age-p95-ms
--feed-age-source recorded|fixed
--place-modify-latency-ms / --place-modify-p95-ms
--cancel-latency-ms / --cancel-p95-ms
--park-via-modify
```

`run_strict_engine.py` also exposes the strategy knobs needed for the sweep:
`--half-spread-bps`, `--skew-bps`, `--requote-threshold-bps`,
`--min-requote-interval-s`, `--quote-notional`, `--max-notional`,
`--min-order-notional`.

### Tests

`tests/test_replay_latency_split.py` (7 tests):

* backward-compatible defaults;
* feed / place-modify / cancel ready times are independent;
* a record with `r` uses recorded age, while `fixed` ignores `r`;
* requote pair lands on the fast place/modify path;
* a cancel-only pull lands on the slow cancel path, and on the fast path with
  `--park-via-modify`;
* `build_engine()` wires the split timings correctly.

Full repo tests: **38 passed** (`tests/test_replay_latency_split.py` +
`tests/test_cooldown_plugins.py`).

## 2. Profile results (2026-10-20..2026-11-05, 16,693,596 records)

`full_p50` and `full_p95` in the same run; hard inventory cap = 0 everywhere.

| Profile | feed | place/modify | cancel | Net PnL | Max DD | Fills | Trades | Hard-stops | Flattens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A fast (raw cancel = fast) | 10 | 410 | 410 | **+0.0027** | 2.3424 | 206 | 136 | 2 | 2 |
| A raw cancel | 10 | 410 | 686 | −0.2076 | 2.5117 | 212 | 142 | 3 | 3 |
| A OTM park (`--park-via-modify`) | 10 | 410 | 686 | **+0.0027** | 2.3424 | 206 | 136 | 2 | 2 |
| B fast (raw cancel = fast) | 25 | 535 | 535 | **+0.9222** | 1.6178 | 183 | 116 | 2 | 2 |
| B raw cancel | 25 | 535 | 775 | +0.4373 | 1.8220 | 182 | 118 | 2 | 2 |
| B OTM park | 25 | 535 | 775 | **+0.9222** | 1.6178 | 183 | 116 | 2 | 2 |

Interpretation:

* **Decoupling was the whole modelling problem.**  With the old inflated feed
  age the p50 case was −0.6686; forcing a realistic 10 ms feed moves it to
  +0.0027, i.e. essentially breakeven.
* Fast OTM park is identical to a fast cancel in this engine: only risk-off
  pulls use the slow path, so making them fast reproduces the 410 ms result.
* Raw 686 ms cancels cost about 21 bps of PnL at p50 and 48 bps at p95.
* The result is **not monotonic in latency** (p95 is much better than p50).
  This is path dependence from a handful of fills, not evidence of a stable
  edge.
* The p50 “profit” is +$0.0027 over 17 days with 136 trades and a $2.34 max
  drawdown.  It is statistically indistinguishable from zero.

## 3. Parameter sweep (p50 fast profile, fixed feed)

Unavailable in this engine: `min_spread_bps`, `obi_veto_threshold`,
`volatility_widening`.  The sweep therefore used the controls the strict
engine actually has: `half_spread_bps`, `skew_bps`,
`requote_threshold_bps`, `min_requote_interval_s`.

Sub-split 2026-11-03..11-05 (where the p50 base is clearly negative):

| hs (bps) | skew | requote thr | min interval | Net PnL | Max DD | Trades |
|---:|---:|---:|---:|---:|---:|---:|
| 20 (base) | 6 | 1.5 | 0.3 | −2.1263 | 2.3749 | 12 |
| 25 | 6 | 1.5 | 0.3 | −0.7294 | 1.1535 | 8 |
| 30 | 6 | 1.5 | 0.3 | −1.6517 | 1.8057 | 4 |
| 40 | 6 | 1.5 | 0.3 | −1.0258 | 1.1347 | 2 |
| 60 | 6 | 1.5 | 0.3 | −1.6870 | 1.7957 | 1 |
| 30 | 6 | 1.5 | 1.0 | −0.8228 | 1.0612 | 4 |
| 30 | 6 | 3.0 | 0.3 | −1.6543 | 1.8082 | 4 |
| 40 | 6 | 1.5 | 1.0 | −0.8998 | 1.1348 | 3 |

Full-window confirmation of the two least-bad defensive variants:

| hs | min interval | Net PnL | Max DD | Fills | Trades | Hard-stops | Flattens |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25 | 0.3 | −0.8885 | 2.7163 | 65 | 38 | 3 | 3 |
| 30 | 1.0 | −1.1237 | 2.8439 | 57 | 31 | 5 | 5 |

Wider spreads and slower requotes **do not** produce a robust positive result;
they reduce trade count and worsen the full-window outcome.  (An earlier
hs=60/hs=100 run looked positive only because it still used recorded `r-t`
feed ages, i.e. the model flaw being fixed here; those numbers are invalid for
calibration.)

## 4. Recommendation

**Option 2 — proceed to live micro-notional forward data collection under the
strict risk belts ($2 max loss / $25 cap).**

Reasons:

1. The corrected p50 result is exactly breakeven (+$0.0027, DD $2.34) and the
   p95 result is positive but non-monotonic; neither is strong enough to lock
   parameters.
2. The parameter sweep has no robust winner: every defensive variant is
   negative over 2026-11-03..11-05 and the two full-window confirmations are
   also negative.
3. Trade counts after widening collapse to 1–38, so any positive cell would be
   a few-trade artifact, not a calibrated edge.
4. The replay still does not model queue position, real maker fill probability,
   or modify rejection/fill races.  Live micro-notional data is the only way to
   validate those.

A `PREREG2 v3` amendment may still adopt the **model correction** (split
`feed_age_ms` / `place_modify_latency_ms` / `cancel_latency_ms`, fixed feed
source for warm profiles) because it is a correctness fix.  It should **not**
lock new strategy parameters from this sample.

Artifacts (main worktree `logs/`):

* `mm_strict_split_fixed_fast410_20261106.json`
* `mm_strict_split_fixed_raw686_20261106.json`
* `mm_strict_split_fixed_park686_20261106.json`
* `mm_strict_split_fixed_hs25_full_20261106.json`
* `mm_strict_split_fixed_hs30mri1_full_20261106.json`
* `mm_sweep_fixed_*_sub.json`
