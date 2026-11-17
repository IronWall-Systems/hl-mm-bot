# MM Parameter Grid Search — rolling_3h reduce_only safety belt

**Window:** 20261020..20261105 · **tick days:** 17 · **records:** 16,693,596

## Protocol

- Coin **HYPE**, **66 ms** Tokyo latency, maker **1.5 bps**, taker **4.5 bps**
- Starting equity **$30.00**, quote **$15**, min order **$10**, cancel_rate **0.5**
- Continuous execution across all days (no daily resets), hourly funding from collector records
- Cooldown: **rolling_3h**, window **3600s**, trigger **$0.25**, action **reduce_only**
- MTM metrics sampled every **60s**; cooldown trigger sees every decision
- Sharpe annualised from calendar-day returns ×√365; Calmar = Net PnL / Max MTM DD

Grid: 6 spreads × 3 skews × 2 caps = **36 configurations** plus 1 no-cooldown baseline.

## Top 12 Configurations (ranked by Calmar, tie-break Net PnL)

| # | Spread | Skew | Max Pos | Trades | Fills | Fills/day | Gross PnL | Fees | Funding | Net PnL | Net % | Max MTM DD | DD % | Sharpe | Calmar | Oct 29 | Nov 4–5 | Triggers |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 20 | 6 | $30 | 338 | 441 | 25.9 | +2.8121 | 0.9144 | -0.0156 | **+1.8822** | +6.27% | 2.6224 | 8.22% | 2.485 | 0.718 | -2.1178 | +0.7021 | 26 |
| 2 | 20 | 6 | $60 | 226 | 307 | 18.1 | +2.2601 | 0.6361 | -0.0298 | **+1.5942** | +5.31% | 2.6804 | 7.91% | 1.914 | 0.595 | -1.9094 | -0.3212 | 43 |
| 3 | 24 | 14 | $60 | 163 | 216 | 12.7 | +0.9227 | 0.4531 | -0.0283 | **+0.4414** | +1.47% | 2.5018 | 7.67% | 0.899 | 0.176 | -1.2263 | -0.3810 | 29 |
| 4 | 20 | 10 | $60 | 219 | 299 | 17.6 | +0.9725 | 0.6258 | -0.0245 | **+0.3221** | +1.07% | 2.4429 | 7.62% | 0.379 | 0.132 | -1.8581 | -0.0985 | 31 |
| 5 | 24 | 14 | $30 | 238 | 303 | 17.8 | +0.8770 | 0.6301 | -0.0016 | **+0.2452** | +0.82% | 2.0730 | 6.57% | 0.627 | 0.118 | -1.3357 | +0.4250 | 16 |
| 6 | 24 | 6 | $30 | 172 | 227 | 13.4 | +0.6864 | 0.4739 | -0.0149 | **+0.1976** | +0.66% | 2.8370 | 8.67% | 0.455 | 0.070 | -1.7958 | -0.7064 | 35 |
| 7 | 28 | 14 | $30 | 101 | 142 | 8.4 | -0.2414 | 0.2952 | -0.0128 | **-0.5493** | -1.83% | 3.3737 | 10.49% | -1.248 | -0.163 | -1.0073 | -0.6433 | 18 |
| 8 | 20 | 14 | $60 | 306 | 411 | 24.2 | +0.0812 | 0.8515 | -0.0140 | **-0.7844** | -2.61% | 2.9710 | 9.46% | -1.683 | -0.264 | -1.9715 | +0.0265 | 28 |
| 9 | 24 | 10 | $30 | 149 | 196 | 11.5 | -0.2876 | 0.4093 | -0.0185 | **-0.7155** | -2.39% | 2.6723 | 8.45% | -1.232 | -0.268 | -1.5416 | -0.2636 | 25 |
| 10 | 16 | 6 | $60 | 446 | 615 | 36.2 | -0.0785 | 1.3036 | -0.0380 | **-1.4201** | -4.73% | 4.5990 | 14.19% | -1.075 | -0.309 | -3.4602 | -0.5191 | 44 *(old 16/6/$60)* |
| 11 | 24 | 10 | $60 | 152 | 209 | 12.3 | -0.6864 | 0.4365 | -0.0443 | **-1.1672** | -3.89% | 3.3480 | 10.41% | -1.562 | -0.349 | -2.1765 | -0.6931 | 37 |
| 12 | 16 | 10 | $60 | 609 | 810 | 47.6 | +0.1566 | 1.6750 | -0.0182 | **-1.5366** | -5.12% | 4.0083 | 12.57% | -2.137 | -0.383 | -2.0855 | +0.0814 | 35 |
| 13 | 16 | 6 | $60 | 755 | 1316 | 77.4 | +2.0180 | 2.7544 | -0.0370 | **-0.7734** | -2.58% | 7.2385 | 20.14% | -0.305 | -0.107 | -6.2923 | -1.2272 | 0 *(baseline)* |

## Key Results

- **Best Calmar:** `20/6/$30` — Net **+1.8822** (+6.27%), Max DD **2.6224** (8.22%), Calmar **0.718**, Sharpe **2.485**, triggers **26**.
- **Best Net PnL:** `20/6/$30` — Net **+1.8822** (+6.27%), Max DD **2.6224**, Calmar **0.718**, triggers **26**.
- **Old 16/6/$60 + 3h reduce-only:** Net **-1.4201** (-4.73%), Max DD **4.5990**, Calmar **-0.309**, Sharpe **-1.075**, Oct 29 **-3.4602**, triggers **44**.
- **No-cooldown baseline 16/6/$60:** Net **-0.7734** (-2.58%), Max DD **7.2385**, Sharpe **-0.305**.

## Marginal Effects (mean Net PnL and Calmar across other axes)

**Half-spread (bps)**

| Value | Mean Net PnL | Mean Calmar | Mean Max DD | Best Net PnL |
|---:|---:|---:|---:|---:|
| 10 | -14.4518 | -0.976 | 14.7473 | -10.1234 |
| 12 | -8.2405 | -0.910 | 8.9206 | -5.3028 |
| 16 | -2.3350 | -0.478 | 4.6741 | -1.4201 |
| 20 | -0.3766 | -0.024 | 3.1233 | +1.8822 |
| 24 | -0.6480 | -0.131 | 3.1444 | +0.4414 |
| 28 | -2.3035 | -0.513 | 4.3588 | -0.5493 |

**Skew (bps)**

| Value | Mean Net PnL | Mean Calmar | Mean Max DD | Best Net PnL |
|---:|---:|---:|---:|---:|
| 6 | -3.6701 | -0.387 | 5.9515 | +1.8822 |
| 10 | -4.8339 | -0.581 | 6.3473 | +0.3221 |
| 14 | -5.6737 | -0.548 | 7.1855 | +0.4414 |

**Max position ($)**

| Value | Mean Net PnL | Mean Calmar | Mean Max DD | Best Net PnL |
|---:|---:|---:|---:|---:|
| 30 | -5.3895 | -0.541 | 6.9179 | +1.8822 |
| 60 | -4.0623 | -0.470 | 6.0716 | +1.5942 |

## Notes & Caveats

- Continuous execution carries inventory/cash/funding across midnight; missing hours are skipped but state is preserved.
- Sources are streamed strictly chronologically (per day, per hour), merging tar-backed Oct 20–27 with local Oct 28–Nov 5. The earlier continuous harness iterated all local files before tar members, which replayed Oct 20–27 out of order; this run corrects that, so its reference numbers can differ.
- 2026-11-04 (9 hours) and 2026-11-05 (18 hours) are partial collector days; 2026-11-03 is now complete.
- The cooldown plugin triggers on the rolling 60-minute MTM equity HWM and uses reduce-only quoting until flat.
- Max DD and Sharpe use a sampled equity grid; intra-sample excursions can be slightly larger.
- One dominant tail day remains (Oct 29). Treat the ranking as a screen, not statistical validation; forward paper testing is still required.
- `src/mmbot/` is untouched; all work is in `scripts/` and `strategies/market_maker/plugins/`.

## Reproduction

```bash
cd /root/hl-mm-bot
venv/bin/python scripts/mm_grid_search.py \
    --start 20261020 --end 20261105 \
    --csv logs/mm_grid_search_20261105.csv \
    --md docs/mm_grid_search_20261105.md
```
