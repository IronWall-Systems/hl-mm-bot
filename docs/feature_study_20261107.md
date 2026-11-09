# HYPE Feature Study — 2026-11-07

**Generated:** 2026-11-07 07:47:18 UTC
**Data:** 9 UTC day blocks (20261028 → 20261105) of `hlmm-shard/2` HYPE.
**Latency profile:** feed `10 ms`, modify `410 ms`.
**Samples (non-overlapping):** +1s=615888, +5s=128555, +30s=22758

## 1. Method

* Decision time = exchange timestamp + 10 ms. Only events with `t <= decision_time` are used.
* Forward markouts use the latest mid at or before `decision_time + h` (tolerance 3 s).
* Samples are de-overlapped per horizon (one decision per h seconds) to avoid autocorrelation inflation.
* Per-day Spearman rank correlation; daily sign consistency vs pooled sign.
* Day-block bootstrap: 5,000 resamples of daily rho, fixed seed `20261107`, 90% CI of the mean.
* Holm–Bonferroni correction across all feature × horizon tests.
* Odd/even UTC-day walk-forward: sign must agree in both halves and with the pooled sign.
* Pass gates: >=6 days, |rho| >= 0.03, Holm p < 0.05, sign consistency >= 67%,
  bootstrap CI excludes 0, and odd/even sign agreement.

## 2. Feature definitions (zero look-ahead)

| Feature | Definition |
|---|---|
| `obi_sz` | Top-5 L2 size imbalance `(B-A)/(B+A)` |
| `obi_nw` | Top-5 size weighted by independent order count `n`: `sum(sz*n)` |
| `tfi2` / `tfi5` | Aggressive taker flow imbalance over 2 s / 5 s |
| `vol_wick` | `range_1m / (range_5m/5)` rolling range expansion ratio |
| `inv_age` | Seconds in the current taker-flow regime (inventory-age proxy) |
| `lead_btc` | BTC 1 s mid return at decision time |
| `ret5m` | HYPE 5 m mid return at decision time |

## 3. Results

| Feature | Horizon | n | Spearman rho | Holm p | Daily sign | Bootstrap 90% CI | WF odd/even | Signed MO (bps) | Pass |
|---|---|---|---|---|---|---|---|---|---|
| `inv_age` | +1s | 615888 | +0.0075 | 4.44e-08 | 78% | [+0.004, +0.013] | +0.009/+0.007 OK | +0.027 | — |
| `inv_age` | +5s | 128555 | +0.0103 | 1.42e-03 | 89% | [+0.007, +0.019] | +0.012/+0.009 OK | +0.090 | — |
| `inv_age` | +30s | 22758 | +0.0070 | 8.81e-01 | 67% | [-0.001, +0.019] | +0.011/+0.004 OK | +0.182 | — |
| `lead_btc` | +1s | 588707 | +0.1866 | 0.00e+00 | 100% | [+0.154, +0.209] | +0.172/+0.197 OK | +0.688 | **PASS** |
| `lead_btc` | +5s | 122806 | +0.1225 | 0.00e+00 | 100% | [+0.102, +0.138] | +0.110/+0.131 OK | +1.047 | **PASS** |
| `lead_btc` | +30s | 21657 | +0.0531 | 5.88e-14 | 100% | [+0.038, +0.060] | +0.048/+0.057 OK | +1.282 | **PASS** |
| `obi_nw` | +1s | 615888 | +0.2978 | 0.00e+00 | 100% | [+0.289, +0.321] | +0.300/+0.297 OK | +0.724 | **PASS** |
| `obi_nw` | +5s | 128555 | +0.2213 | 0.00e+00 | 100% | [+0.204, +0.250] | +0.208/+0.231 OK | +1.177 | **PASS** |
| `obi_nw` | +30s | 22758 | +0.0793 | 4.70e-32 | 100% | [+0.069, +0.101] | +0.066/+0.089 OK | +1.452 | **PASS** |
| `obi_sz` | +1s | 615888 | +0.2639 | 0.00e+00 | 100% | [+0.256, +0.289] | +0.264/+0.264 OK | +0.641 | **PASS** |
| `obi_sz` | +5s | 128555 | +0.1990 | 0.00e+00 | 100% | [+0.184, +0.227] | +0.187/+0.208 OK | +1.023 | **PASS** |
| `obi_sz` | +30s | 22758 | +0.0709 | 9.83e-26 | 100% | [+0.061, +0.091] | +0.058/+0.081 OK | +1.311 | **PASS** |
| `ret5m` | +1s | 563541 | +0.0185 | 7.83e-43 | 100% | [+0.015, +0.023] | +0.018/+0.019 OK | +0.114 | — |
| `ret5m` | +5s | 117674 | +0.0182 | 4.64e-09 | 100% | [+0.013, +0.024] | +0.019/+0.018 OK | +0.231 | — |
| `ret5m` | +30s | 20817 | +0.0036 | 1.00e+00 | 56% | [-0.010, +0.011] | -0.003/+0.009 NO | +0.403 | — |
| `tfi2` | +1s | 615888 | +0.0741 | 0.00e+00 | 100% | [+0.073, +0.081] | +0.081/+0.070 OK | +0.059 | **PASS** |
| `tfi2` | +5s | 128555 | +0.0535 | 4.09e-81 | 100% | [+0.050, +0.062] | +0.061/+0.048 OK | +0.116 | **PASS** |
| `tfi2` | +30s | 22758 | +0.0280 | 1.86e-04 | 100% | [+0.020, +0.035] | +0.035/+0.023 OK | +0.180 | — |
| `tfi5` | +1s | 615888 | +0.0581 | 0.00e+00 | 100% | [+0.056, +0.065] | +0.066/+0.053 OK | +0.037 | **PASS** |
| `tfi5` | +5s | 128555 | +0.0480 | 2.80e-65 | 100% | [+0.043, +0.054] | +0.057/+0.042 OK | +0.071 | **PASS** |
| `tfi5` | +30s | 22758 | +0.0275 | 2.39e-04 | 100% | [+0.020, +0.038] | +0.023/+0.031 OK | +0.130 | — |
| `vol_wick` | +1s | 615888 | -0.0017 | 7.00e-01 | 67% | [-0.005, +0.002] | -0.001/-0.002 OK | -0.003 | — |
| `vol_wick` | +5s | 128555 | -0.0068 | 7.05e-02 | 78% | [-0.014, -0.002] | -0.003/-0.010 OK | -0.016 | — |
| `vol_wick` | +30s | 22758 | -0.0039 | 1.00e+00 | 56% | [-0.016, +0.010] | +0.002/-0.009 NO | +0.018 | — |

**Passed gates:** 13 / 24 tests.

### Ready to wire

* `obi_sz` @ +1s — rho +0.2639, Holm p 0.00e+00, sign 100%, signed markout +0.641 bps
* `obi_nw` @ +1s — rho +0.2978, Holm p 0.00e+00, sign 100%, signed markout +0.724 bps
* `tfi2` @ +1s — rho +0.0741, Holm p 0.00e+00, sign 100%, signed markout +0.059 bps
* `tfi5` @ +1s — rho +0.0581, Holm p 0.00e+00, sign 100%, signed markout +0.037 bps
* `lead_btc` @ +1s — rho +0.1866, Holm p 0.00e+00, sign 100%, signed markout +0.688 bps
* `obi_sz` @ +5s — rho +0.1990, Holm p 0.00e+00, sign 100%, signed markout +1.023 bps
* `obi_nw` @ +5s — rho +0.2213, Holm p 0.00e+00, sign 100%, signed markout +1.177 bps
* `tfi2` @ +5s — rho +0.0535, Holm p 4.09e-81, sign 100%, signed markout +0.116 bps
* `tfi5` @ +5s — rho +0.0480, Holm p 2.80e-65, sign 100%, signed markout +0.071 bps
* `lead_btc` @ +5s — rho +0.1225, Holm p 0.00e+00, sign 100%, signed markout +1.047 bps
* `obi_sz` @ +30s — rho +0.0709, Holm p 9.83e-26, sign 100%, signed markout +1.311 bps
* `obi_nw` @ +30s — rho +0.0793, Holm p 4.70e-32, sign 100%, signed markout +1.452 bps
* `lead_btc` @ +30s — rho +0.0531, Holm p 5.88e-14, sign 100%, signed markout +1.282 bps

## 4. Live HYPE trial log audit (`hs=4.0` calibration onward)

* Session start: **2026-11-07 05:47:14 UTC** (`1794030434.879`)
* Start equity `$24.295370899999998` → latest `$24.2034669` (PnL `-0.09190` USD)
* Latest position: `0.0`
* Fills: **19** · modify leg updates: **1161** · OTM parks: 0 · fees: `$0.03206`

### Live signed markouts after calibration

| Horizon | n | Median bps | Mean bps |
|---|---|---|---|
| +1s | 19 | -0.6953 | -1.6609105263157893 |
| +5s | 19 | -0.4416 | -1.962352631578947 |
| +30s | 19 | -4.7393 | -3.3863263157894736 |

The recorded live log now contains real fills and markouts, so it provides an
out-of-sample execution check: fill capture improved after the width calibration,
but short-horizon markouts are near zero/slightly negative and the 30s median is
adverse, indicating toxic fills rather than pure edge.

## 5. Limitations

* Exchange timestamps are millisecond-resolution; sub-ms ordering is unavailable.
* `inv_age` is a taker-flow regime proxy because market-data shards carry no inventory state.
* Recorded L2 is top-20 only; deep-book walls are not present in the historical shards.
* Lead-lag uses same-hour BTC shards; gaps produce `NaN` and are excluded pairwise.
* Overlapping 30s outcomes are de-overlapped per horizon, but residual cross-feature
  dependence remains; Holm controls family-wise error across the 24 tests.
