# Feed-age mode by table — clarification (2026-11-06)

## Short answer

**Every MM replay table reported on 2026-11-05 used the empirical `r - t` feed
age, not the configured 66 ms.** The `feed_age_ms: 66` values in the JSON/log
headers are configuration metadata. `scripts/replay_backtest.py` overrides a
fixed feed age whenever a record carries `r` (collector receive time), and every
shard in 2026-10-20..2026-11-05 carries `r`.

## Code path

`scripts/replay_backtest.py`:

```python
# on_record(), ~line 513
t = t_ms / 1000.0
recv = rec.get("r")
if isinstance(recv, (int, float)):
    age_ms = float(recv) - float(t_ms)
    ...
    self.current_feed_age_s = age_ms / 1000.0
else:
    self.current_feed_age_s = self.feed_age_s      # fixed config fallback
```

```python
# _maybe_decide(), ~line 695
seen_t = t + self.current_feed_age_s               # decision time
...
eff = seen_t + self.latency_s                      # order-effective time
```

So `feed_age_ms` is only a fallback for records without `r`. The measured
`r - t` is ~410 ms median and ~627 ms p90 (local samples; archive days
398–438 ms median).

## Table-by-table

| Table / artefact | Label in header | Feed age actually used | Order-path delay varied? |
|---|---|---|---|
| `logs/mm_grid_search_20261105.*` (36 configs) | `feed_age_ms: 66` | **empirical `r - t`** | no (fixed 66 ms) |
| `logs/mm_cooldown_rolling_continuous_20261105.*` (8 rolling variants + baseline) | `feed_age_ms: 66` | **empirical `r - t`** | no (fixed 66 ms) |
| `logs/mm_cooldown_backtest_20261105.*` (3 legacy UTC-day variants) | `feed_age_ms: 66` | **empirical `r - t`** | no (fixed 66 ms) |
| `logs/mm_daily_pnl_matrix_20261105.csv/.json` (K=48) | `latency_ms: 66` | **empirical `r - t`** (imported rows) | no |
| Latency sweep table (`docs/mm_validation_20261105.md` §3) | 66/150/300/500/1000 ms | **empirical `r - t`** | **yes, `latency_ms` only** |
| Strict 1.0x hard-cap table (§4) | cap + 66/300 ms | **empirical `r - t`** | **yes, `latency_ms` only** |
| Oct 29 stress table (§5) | 66/300/500 ms | **empirical `r - t`** | **yes, `latency_ms` only** |
| `logs/mm_strict_v2_baseline_20261106.json` | p50 66 / p95 150 ms | **empirical `r - t`** | **yes, `latency_ms`; taker flatten at `seen_t + 150 ms`** |
| `docs/backtest_latency_tokyo_20261105.md` (breakout) | 0/66/500 ms | bar-close shift using tick `mid(t+latency)` | yes, but not the MM replay |
| `logs/latency_tokyo_ack_proxy_20261106.*` | read-only RTT | n/a (not a replay) | n/a |

## Why the latency sweep is still meaningful

Setting `feed_age_ms=latency` in the sweep does **not** change the effective feed
age, because `r` is present and overrides it. Only `latency_ms` changes. The
sweep therefore isolates the **order-path** component (decision → effective
order), while holding the collector feed age at its measured empirical value.
The 66 ms row reproduces the grid because that is the configured order-path
delay there.

## Consequence

The 66 ms label is an **order-path latency**, not a 66 ms data feed. Any future
report must say “order-path 66 ms with empirical ~410 ms feed age” to avoid the
ambiguity that prompted this audit.
