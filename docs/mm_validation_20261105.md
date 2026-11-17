# MM validation suite — 2026-11-05

Window **20261020..20261105** · **17** tick days · **16,693,596** records · **K = 48** trials.

## 1. Timestamp source (`rec["t"]`)

`t` is the **Hyperliquid exchange timestamp** for market-data channels, while `r` is the **collector local receive timestamp**:

- `scripts/collector.py:200` `compact_l2`: `"t": data.get("time")` (exchange)
- `scripts/collector.py:219` `compact_bbo`: `"t": data.get("time")` (exchange)
- `scripts/collector.py:231` `compact_trade`: `"t": item.get("time")` (exchange)
- `scripts/collector.py:557` `recv_ms = lambda: int(time.time() * 1000)` (local)
- `scripts/collector.py:747` `_handle`: `record["r"] = recv` (local receive)
- `scripts/collector.py:700` funding: `{"t": ts, ... "r": recv}` where `ts = int(now * 1000)` — **local** for funding
- `scripts/collector.py:758` gap records: `"t": int(self.now() * 1000)` — **local**
- `scripts/replay_backtest.py:518` `t = t_ms / 1000.0`; `:521` `age_ms = float(recv) - float(t_ms)`; `:695` `seen_t = t + feed_age`; `:704` `eff = seen_t + latency`

So: `l2Book`, `bbo`, and `trades` carry exchange time in `t` and local receive time in `r`; funding and gap records carry local time in `t`. The replay treats `t` as exchange/event time and uses `r - t` as the empirical feed age. The median observed feed age is ~0.41 s (p90 ~0.63 s), far above the nominal 66 ms; the latency sweep therefore varies the order-path component while leaving the empirical feed age intact.

## 2. Daily PnL matrix and data-snooping tests

- Matrix: `logs/mm_daily_pnl_matrix_20261105.csv` / `.json`, **K = 48** rows x **D = 17** days.
- Families: {"baseline": 1, "grid": 36, "legacy_utc": 3, "rolling_cooldown": 8}
- **K=48 max-mean trial:** **Legacy 6h** — total **+2.7780**, mean daily **+0.1634**, daily t = **0.571** (one-sided p 0.2840).
- **Frozen candidate 20/6/$30:** total **+1.8822**, mean daily **+0.1107**, daily t = **0.554** (one-sided p 0.2896).
- Stationary day-block bootstrap, **5,000** draws, mean block 5 days, seed 20261105.
- **White Reality Check p = 0.5169** (max over all K=48 trials)
- **Hansen SPA consistent p = 0.3489** (max over all K=48 trials)
- 98% block-bootstrap CI for the frozen candidate's mean daily PnL: **[-0.1821, +0.3569]** USD/day (-3.0965 .. +6.0678 over 17 days).

| Trial | Net PnL | Max DD | DD % | Sharpe | Calmar | Trades | Triggers |
|---|---:|---:|---:|---:|---:|---:|---:|
| 20/6/$30 | +1.8822 | 2.6224 | 8.22% | 2.485 | 0.718 | 338 | 26 |
| 20/6/$60 | +1.5942 | 2.6804 | 7.91% | 1.914 | 0.595 | 226 | 43 |
| 24/14/$60 | +0.4414 | 2.5018 | 7.67% | 0.899 | 0.176 | 163 | 29 |
| 20/10/$60 | +0.3221 | 2.4429 | 7.62% | 0.379 | 0.132 | 219 | 31 |
| 24/14/$30 | +0.2452 | 2.0730 | 6.57% | 0.627 | 0.118 | 238 | 16 |
| 24/6/$30 | +0.1976 | 2.8370 | 8.67% | 0.455 | 0.070 | 172 | 35 |
| 28/14/$30 | -0.5493 | 3.3737 | 10.49% | -1.248 | -0.163 | 101 | 18 |
| 24/10/$30 | -0.7155 | 2.6723 | 8.45% | -1.232 | -0.268 | 149 | 25 |
| 20/14/$60 | -0.7844 | 2.9710 | 9.46% | -1.683 | -0.264 | 306 | 28 |
| 24/10/$60 | -1.1672 | 3.3480 | 10.41% | -1.562 | -0.349 | 152 | 37 |
| 16/6/$60 | -1.4201 | 4.5990 | 14.19% | -1.075 | -0.309 | 446 | 44 |
| 16/10/$60 | -1.5366 | 4.0083 | 12.57% | -2.137 | -0.383 | 609 | 35 |
| old 16/6/$60 (3h RO) | -1.4201 | 4.5990 | 14.19% | -1.075 | -0.309 | 446 | 44 |
| baseline 16/6/$60 | -0.7734 | 7.2385 | 20.14% | -0.305 | -0.107 | 755 | 0 |

The **K=48 max-mean trial** is the legacy 6h variant: Net **+2.7780**, mean
daily **+0.1634**, t = **0.571**. It is not the frozen candidate; its full
drawdown/Sharpe metrics are not stored in the matrix JSON, only its daily PnL.

## 3. Latency sensitivity (order-path latency, empirical feed age)

| Config | 66 ms | 150 ms | 300 ms | 500 ms | 1000 ms |
|---|---:|---:|---:|---:|---:|
| 20/6/$30 | +1.8822 | -0.7084 | -0.4858 | -1.5902 | -4.6190 |
| 20/6/$60 | +1.5942 | +1.5131 | +0.6029 | +0.3014 | -1.5599 |
| 24/14/$60 | +0.4414 | +0.9422 | -1.0308 | -0.0479 | +1.2767 |
| 20/10/$60 | +0.3221 | +2.7426 | -1.1978 | +1.1015 | -5.9307 |
| 24/14/$30 | +0.2452 | -1.1622 | -2.2391 | -0.4425 | -3.5425 |

| Config | Latency | Net PnL | Max DD | DD % | Sharpe | Calmar | Trades | Triggers |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 20/6/$30 | 66 | +1.8822 | 2.6224 | 8.22% | 2.485 | 0.718 | 338 | 26 |
| 20/6/$30 | 150 | -0.7084 | 3.2130 | 10.22% | -0.711 | -0.220 | 330 | 28 |
| 20/6/$30 | 300 | -0.4858 | 3.3324 | 10.41% | -0.363 | -0.146 | 374 | 33 |
| 20/6/$30 | 500 | -1.5902 | 4.0481 | 12.51% | -2.246 | -0.393 | 305 | 34 |
| 20/6/$30 | 1000 | -4.6190 | 6.4183 | 20.30% | -4.880 | -0.720 | 477 | 33 |
| 20/6/$60 | 66 | +1.5942 | 2.6804 | 7.91% | 1.914 | 0.595 | 226 | 43 |
| 20/6/$60 | 150 | +1.5131 | 2.5373 | 7.53% | 1.871 | 0.596 | 288 | 45 |
| 20/6/$60 | 300 | +0.6029 | 3.6769 | 11.20% | 0.750 | 0.164 | 210 | 48 |
| 20/6/$60 | 500 | +0.3014 | 3.4868 | 10.46% | 0.431 | 0.086 | 235 | 47 |
| 20/6/$60 | 1000 | -1.5599 | 4.1973 | 12.90% | -1.891 | -0.372 | 262 | 44 |
| 24/14/$60 | 66 | +0.4414 | 2.5018 | 7.67% | 0.899 | 0.176 | 163 | 29 |
| 24/14/$60 | 150 | +0.9422 | 2.3682 | 7.29% | 1.685 | 0.398 | 205 | 26 |
| 24/14/$60 | 300 | -1.0308 | 3.1955 | 10.03% | -1.501 | -0.323 | 211 | 25 |
| 24/14/$60 | 500 | -0.0479 | 3.4760 | 10.49% | 0.436 | -0.014 | 231 | 25 |
| 24/14/$60 | 1000 | +1.2767 | 2.8627 | 8.70% | 1.807 | 0.446 | 198 | 30 |
| 20/10/$60 | 66 | +0.3221 | 2.4429 | 7.62% | 0.379 | 0.132 | 219 | 31 |
| 20/10/$60 | 150 | +2.7426 | 1.8393 | 5.41% | 3.610 | 1.491 | 342 | 35 |
| 20/10/$60 | 300 | -1.1978 | 3.8178 | 11.72% | -1.394 | -0.314 | 233 | 35 |
| 20/10/$60 | 500 | +1.1015 | 3.2249 | 9.74% | 1.452 | 0.342 | 290 | 38 |
| 20/10/$60 | 1000 | -5.9307 | 7.8403 | 24.71% | -8.026 | -0.756 | 400 | 33 |
| 24/14/$30 | 66 | +0.2452 | 2.0730 | 6.57% | 0.627 | 0.118 | 238 | 16 |
| 24/14/$30 | 150 | -1.1622 | 2.9773 | 9.46% | -2.186 | -0.390 | 245 | 16 |
| 24/14/$30 | 300 | -2.2391 | 4.1947 | 13.14% | -4.088 | -0.534 | 223 | 15 |
| 24/14/$30 | 500 | -0.4425 | 3.0079 | 9.46% | -0.600 | -0.147 | 290 | 15 |
| 24/14/$30 | 1000 | -3.5425 | 5.2508 | 16.62% | -6.116 | -0.675 | 305 | 15 |

## 4. Strict 1.0x equity hard cap (quote $10)

The first cap sweep exposed a real veto-path bug: when a cancel was already in
flight, a previously queued risk-increasing **place** could still execute after
the cancel. The fix purges that queued place. The table below is the post-fix
rerun (12 engines = 2 configs x 3 caps x 2 latencies; the task's "6 engines" is
per latency).

| Config | Cap | Latency | Net PnL | Max DD | DD % | Max Inv $ | Max pos $ | Pre-place gate | Trades | Triggers | Vetoes |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| 20/6 | $15 | 66 | -0.0924 | 1.3410 | 4.31% | 17.2161 | 17.2146 | yes (MTM drift over cap) | 208 | 4 | 2,483,090 |
| 20/6 | $15 | 300 | -0.2395 | 0.6918 | 2.30% | 9.9741 | 9.9739 | yes | 13 | 2 | 3,039,263 |
| 20/6 | $25 | 66 | +0.7740 | 1.3904 | 4.34% | 23.3724 | 23.3718 | yes | 300 | 10 | 968,118 |
| 20/6 | $25 | 300 | -1.4513 | 2.2408 | 7.32% | 22.3664 | 22.3632 | yes | 320 | 8 | 1,027,237 |
| 20/6 | $30 | 66 | -0.3226 | 2.0592 | 6.51% | 27.2530 | 27.2493 | yes | 263 | 15 | 648,132 |
| 20/6 | $30 | 300 | -0.0595 | 1.6508 | 5.23% | 28.3752 | 28.3752 | yes | 329 | 17 | 498,895 |
| 24/14 | $15 | 66 | -0.1691 | 0.8343 | 2.74% | 17.9228 | 17.9228 | yes (MTM drift over cap) | 136 | 3 | 2,777,599 |
| 24/14 | $15 | 300 | -0.0823 | 0.4704 | 1.56% | 9.9741 | 9.9739 | yes | 8 | 2 | 3,039,446 |
| 24/14 | $25 | 66 | -0.5133 | 1.5512 | 5.03% | 20.1766 | 20.1764 | yes | 181 | 9 | 947,065 |
| 24/14 | $25 | 300 | -1.6268 | 2.0041 | 6.61% | 21.8203 | 21.8203 | yes | 227 | 8 | 970,130 |
| 24/14 | $30 | 66 | +0.5292 | 1.5863 | 4.97% | 27.7803 | 27.7803 | yes | 152 | 12 | 268,743 |
| 24/14 | $30 | 300 | +0.3328 | 1.5482 | 4.90% | 20.4136 | 20.4080 | yes | 173 | 11 | 202,130 |

The **pre-place gate** held for every placement. `Max Inv` / `Max pos` are
marked to market continuously, so they can exceed the cap when price drifts after
a position is opened (the $15 rows at 66 ms peak near $17.2-$17.9); this is mark
drift, not a placement leak. The best strict-cap result is **20/6, $25, 66 ms:
Net +$0.7740, Max DD $1.3904 (4.34%)**, which still trails the uncapped
20/6/$30 candidate (+$1.8822).

## 5. Oct 29 stress

Post-fix results:

| Variant | Net PnL | Max DD | DD % | Max Inv $ | Trades | Triggers |
|---|---:|---:|---:|---:|---:|---:|
| base_66 | -1.4106 | 1.4106 | 4.70% | 57.8941 | 67 | 4 |
| base_300 | -1.5676 | 1.6407 | 5.46% | 44.2255 | 67 | 4 |
| cap15_66 | -0.0545 | 0.1899 | 0.63% | 15.8190 | 71 | 0 |
| cap15_300 | -0.0139 | 0.0239 | 0.08% | 9.9741 | 3 | 0 |
| cap25_66 | -0.1254 | 0.2664 | 0.89% | 20.8020 | 119 | 3 |
| cap25_300 | -1.3149 | 1.3358 | 4.45% | 17.8459 | 166 | 3 |
| base_harsh | -1.9840 | 2.1364 | 7.09% | 44.2255 | 97 | 4 |
| cap15_harsh | -0.0054 | 0.0201 | 0.07% | 10.0304 | 1 | 0 |
| cap25_harsh | -1.0635 | 1.0635 | 3.55% | 19.7006 | 97 | 3 |

Normal = 66/300 ms, cancel_rate 0.5, maker 1.5 bps; harsh = 500 ms,
cancel_rate 0.25, taker 6.0 bps. The $15 cap nearly stops trading on Oct 29
(3 trades at 300 ms, 1 under harsh), so its tiny loss is mostly inactivity, not
demonstrated edge.

## 6. Files and caveats

- `logs/mm_daily_pnl_matrix_20261105.csv` / `.json` — full K=48 matrix
- `logs/mm_validation_20261105.log` — full-period run log
- `logs/mm_hardcap_recheck_20261105.log` / `.json` — post-fix cap and stress rerun
- `.agent/PREREG2.md` — frozen candidate (mode 444); SHA-256
  `04e23f62509395ff7c04ce25c702d38a2fd958f27c0ba9943eb5bae0cc4d98b3`
  recorded in `logs/prereg2_sha256.txt`
- `logs/prereg2_freeze_timestamp.txt` — freeze/change time (2026-11-06T01:03:07Z)

## 7. Caveats that matter

- **No statistical significance.** With K=48, White RC p=0.5169 and Hansen SPA
  p=0.3489; the candidate's own daily t is 0.554 and its 98% CI for mean daily
  PnL crosses zero. The +$1.88 is a screen, not evidence of edge.
- **The K=48 max-mean trial is not the candidate.** The legacy 6h variant has the
  highest total (+$2.7780). It is included precisely to expose data snooping; we
  did not re-optimize to it.
- **Latency fragility.** 20/6/$30 is positive at 66 ms (+$1.8822) but negative at
  150 ms (-$0.7084), 300 ms (-$0.4858), 500 ms (-$1.5902), and 1,000 ms
  (-$4.6190). The edge is confined to the fastest execution cell.
- **The cap leak is fixed, but mark drift remains.** The pre-place gate always
  held after purging stale queued places; continuous MTM inventory can still
  exceed the USD cap when price moves after entry.
- The $15 strict-cap variants barely trade on Oct 29; their small losses reflect
  inactivity, not resilience.
- **Freeze timing.** The prereg was frozen at 2026-11-06T01:03:07Z, 63 minutes
  after the stated 00:00 window start. No 2026-11-06 market data was inspected
  before freezing; the missed interval must be disclosed in the forward report.
- Bootstrap resamples UTC days jointly across configs; it preserves
  cross-sectional correlation but has only 17 day observations.
- `src/mmbot/` is untouched; this is research-only, with no credentials and no
  orders.

## 8. Post-audit corrections (2026-11-06)

Three findings changed how the tables above must be read; none changes the raw
files, which are retained for audit.

- **Feed age.** Every MM table above used the empirical collector feed age
  (`r - t`, ~410 ms median), not the configured 66 ms. The `feed_age_ms: 66` in
  the JSON/log headers is metadata/fallback only; `r` overrides it. The latency
  sweep varied `latency_ms` (order path) while holding the empirical feed age
  fixed. Table-by-table proof: `docs/mm_feed_age_modes_20261106.md`.
- **Taker flatten.** The old strict engine never executed a scheduled taker
  flatten (the event was routed to `_apply_cancel`), and it also replayed local
  files before archive tars and froze the plugin clock. The corrected engine
  replays chronologically, drives a live plugin clock, purges vetoed queued
  places, and executes the flatten at `seen_t + 150 ms`. The corrected
  soft-cap-only control reproduces `cap_20_6_30_l66` exactly (`-$0.3226477`);
  the full rule set is `+$0.4053` at 66 ms and `+$1.5306` at 150 ms in-sample.
  An immediate-flatten diagnostic is `+$0.4239`, so the frozen delayed rule is
  the conservative choice. Details:
  `docs/mm_strict_flatten_audit_20261106.md`.
- **Latency.** Authenticated place/cancel ACK percentiles require keys and live
  orders, which this research run prohibits. Read-only Tokyo proxies (n=60):
  mainnet REST p50 25.2 / p95 83.4 / p99 264.3 ms, mapped to replay order-path
  cells 66 / 150 / 300 ms. `chronyc` was synchronised (stratum 3, +0.86 ms,
  RMS 0.30 ms, Normal). Source: `logs/latency_tokyo_ack_proxy_20261106.json`.

**Governance update.** PREREG2 v1 (14 days, window started 2026-11-06) is
superseded before any data inspection by a frozen v2 amendment: 28 days,
2026-11-07 00:00 UTC .. 2026-12-04 23:59 UTC, evaluated 2026-12-05 00:15 UTC,
with the full `20/6/$30` + `$40` delayed-flatten + `$0.50` hard-stop +
`rolling_3h reduce_only` rule set and pass criteria requiring positive replay Net
PnL at **both** 66 ms and 150 ms, a 90% block-bootstrap lower bound > $0.00/day,
Max MTM DD <= $2.50, and zero unlogged outages or code edits. v1 text and hash
are preserved; v2 SHA-256 is in `logs/prereg2_v2_sha256.txt`.

**Production-safety port.** The three controls were ported on branch
`live-safety-trial` (commit `0578aeb`, not pushed): veto-path pending-order
purge, `$40` emergency taker flatten, and `RISK_MAX_ACCOUNT_LOSS_USD`.
`main` is untouched and `git diff --stat -- src/mmbot/` remains empty. Tests:
7 new in `tests/test_live_safety_controls.py`, plus the existing guard/order and
bot-scenario suites. Details: `docs/live_safety_trial_20261106.md` on that
branch.
