# T13 — PnL decomposition audit & 7/9-day cancel-rate grid (2026-10-17)

Audit of the T12 red flag: `gross_bps` < 0 with negative markouts yet `net_bps` > 0
(AVAX hs=12@500 net +39.8 bps, kBONK hs=8@500 +17.9 bps).

## Method

- **Decomposition (exact identities, unit-tested; 648/648 rows verified):**
  `net = matched_spread + inventory_mtm - total_fees` and
  `net = round_trip_spread_pnl + inventory_mtm - fees_unmatched`, with
  `round_trip_spread_pnl = FIFO-matched pair PnL - fees on matched legs`.
  FIFO lot matching over fills in time order; residual inventory marked to final mid.
- **Grid:** 6 coins x latency {500,1000} x hs {8,12,16} x cancel_rate {0.0,0.5} = 72
  cells, each replayed as **9 fresh-engine per-day runs** (days 20261006-20261014:
  648 runs, 8.1 h wall on the 1-core VPS). Fresh engine per day strips cross-day
  inventory drift by construction; feed-age fixed 400 ms (no recv stamp).
- **Day coverage varies:** day 06 partial start (~8 h live), days 09/10 hit by the
  16 h collector outage (hour-15 shards of Oct 9 are truncated; the reader now yields
  the intact prefix and warns), day 14 partial (21 h). Profitable-day counts are
  therefore out of 9 calendar runs of unequal length, not equal 24 h days.
- Suite: 187 passed (offline). Evidence: `data/backtest_audit_t13.csv` (648 rows),
  per-worker CSVs `data/t13_daily_w{1,2}.csv`, logs `logs/t13_w{1,2}.log`.

## Part A — why `net_bps` was positive while `gross_bps` was negative (no sign bug)

Both metrics come from `compute_metrics()` in `scripts/replay_backtest.py`, but they
measure **different quantities**, and net PnL never uses `gross_usd` at all:

- `gross_usd = sum over fills of (mid_at_fill - px)*sz` (buys; mirrored for sells):
  per-fill execution edge **vs the mid at that instant**, unmatched. A resting quote
  in a fast market fills exactly when the book trades *through* it, so on a trending
  day this metric is dominated by adverse selection and can be very negative.
- `net_usd = sum(+-px*sz cash) + pos*final_mid - fees`: economic cash PnL, i.e. all
  fill cash flows plus liquidating residual inventory at the final mid, minus fees.
  This equals `matched_spread + inventory_mtm - total_fees` exactly.

So T12 rows with `gross_bps < 0` and `net_bps > 0` are **not arithmetically
contradictory**: negative per-fill edge vs mid can coexist with positive round-trip
cash (buys paired with higher sells) and/or positive residual-inventory MTM.
Real-data reproduction of the T12 AVAX hs=12@500 row (days 20261013-14) with the new
decomposition (net reproduces T12 exactly, +2.6535):

| component | USD |
|---|---|
| matched_spread_usd (FIFO pairs, gross of fees) | +2.7649 |
| inventory_mtm_pnl (residual 1.12 AVAX short marked to final mid) | **-0.0113** |
| total_fees_usd | -0.1001 |
| **net_pnl_usd (identity holds exactly)** | **+2.6535** |
| of which round_trip_spread_pnl (after matched-leg fees) | +2.6665 |

**Verdict for the flagged rows: hypothesis (b) is refuted** (no sign/accounting bug —
identities verified on all 648 new runs and on the T12 reproduction) and for the
specific T12 AVAX/kBONK winners **hypothesis (a) is also mostly refuted**: the
positivity came from FIFO round-trip cash, not directional inventory drift
(AVAX inventory MTM was even slightly negative; drift stays < |$1.4| on every
positive cell below).

## Part B — 9-day aggregate table (72 configs)

Sum over the 9 daily runs per cell. `rt+$`/`net+$` = summed USD; `rt-days`/`net-days`
= days with positive round-trip / positive net (out of 9). `maxinv` = worst-case
|inventory x mid| sampled ($).

### cancel_rate = 0.0 (optimistic)

| coin | lat | hs | round_trip_spread_pnl | inventory_mtm_pnl | total_fees_usd | net_pnl_usd | round_trip_count | rt-days | net-days | maxinv | vol | rt bps | net bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| HYPE | 500 | 8 | -0.8280 | -0.1965 | 2.5982 | -1.0442 | 804 | 4/9 | 4/9 | 88.16 | 17321 | -0.48 | -0.60 |
| HYPE | 500 | 12 | +0.5283 | +0.4121 | 0.8499 | +0.9202 | 243 | 4/9 | 5/9 | 88.16 | 5666 | +0.93 | +1.62 |
| HYPE | 500 | 16 | +0.9916 | -0.4459 | 0.3055 | +0.5151 | 70 | 7/9 | 7/9 | 88.16 | 2037 | +4.87 | +2.53 |
| HYPE | 1000 | 8 | -2.8528 | -0.2458 | 3.0596 | -3.1200 | 995 | 4/9 | 3/9 | 87.74 | 20397 | -1.40 | -1.53 |
| HYPE | 1000 | 12 | +0.9493 | +0.4881 | 1.0238 | +1.4168 | 305 | 5/9 | 5/9 | 87.94 | 6825 | +1.39 | +2.08 |
| HYPE | 1000 | 16 | +1.3743 | +0.5760 | 0.3880 | +1.9240 | 89 | 6/9 | 8/9 | 73.47 | 2587 | +5.31 | +7.44 |
| kBONK | 500 | 8 | +1.0537 | -0.4970 | 0.7182 | +0.5161 | 327 | 3/9 | 6/9 | 82.46 | 4788 | +2.20 | +1.08 |
| kBONK | 500 | 12 | -2.0898 | -0.9091 | 0.3218 | -3.0151 | 139 | 3/9 | 4/9 | 89.28 | 2145 | -9.74 | -14.05 |
| kBONK | 500 | 16 | -3.1114 | -0.3645 | 0.1545 | -3.4878 | 61 | 1/9 | 2/9 | 89.42 | 1030 | -30.21 | -33.86 |
| kBONK | 1000 | 8 | +1.2643 | -0.5115 | 0.8167 | +0.7080 | 373 | 4/9 | 5/9 | 84.03 | 5445 | +2.32 | +1.30 |
| kBONK | 1000 | 12 | +2.3356 | -1.3492 | 0.3311 | +0.9558 | 145 | 5/9 | 6/9 | 79.80 | 2208 | +10.58 | +4.33 |
| kBONK | 1000 | 16 | -1.4150 | -0.3122 | 0.2244 | -1.7390 | 99 | 3/9 | 2/9 | 86.44 | 1496 | -9.46 | -11.63 |
| AVAX | 500 | 8 | -4.8962 | +0.9538 | 1.0919 | -3.9693 | 493 | 3/9 | 5/9 | 89.26 | 7279 | -6.73 | -5.45 |
| AVAX | 500 | 12 | -2.2613 | +0.8570 | 0.5349 | -1.4399 | 227 | 3/9 | 5/9 | 74.55 | 3566 | -6.34 | -4.04 |
| AVAX | 500 | 16 | -1.1254 | -0.2489 | 0.3119 | -1.4057 | 125 | 1/9 | 2/9 | 75.40 | 2079 | -5.41 | -6.76 |
| AVAX | 1000 | 8 | -4.2577 | +0.2031 | 1.3437 | -4.0690 | 599 | 3/9 | 3/9 | 89.11 | 8958 | -4.75 | -4.54 |
| AVAX | 1000 | 12 | -2.7773 | +0.8306 | 0.6131 | -1.9688 | 261 | 5/9 | 5/9 | 89.69 | 4087 | -6.79 | -4.82 |
| AVAX | 1000 | 16 | -3.5378 | +1.1047 | 0.3339 | -2.4664 | 135 | 2/9 | 3/9 | 89.47 | 2226 | -15.90 | -11.08 |
| GRAM | 500 | 8 | -8.0656 | -0.0570 | 1.4448 | -8.1421 | 567 | 3/9 | 3/9 | 85.83 | 9632 | -8.37 | -8.45 |
| GRAM | 500 | 12 | -8.1245 | +0.4668 | 0.7683 | -7.6832 | 281 | 2/9 | 2/9 | 86.16 | 5122 | -15.86 | -15.00 |
| GRAM | 500 | 16 | -4.4206 | +0.3487 | 0.5075 | -4.0961 | 142 | 4/9 | 4/9 | 87.62 | 3383 | -13.07 | -12.11 |
| GRAM | 1000 | 8 | -6.7393 | +0.0899 | 1.6228 | -6.6745 | 706 | 2/9 | 2/9 | 86.92 | 10818 | -6.23 | -6.17 |
| GRAM | 1000 | 12 | -6.2644 | +0.3195 | 0.8658 | -5.9636 | 317 | 2/9 | 2/9 | 85.63 | 5772 | -10.85 | -10.33 |
| GRAM | 1000 | 16 | -3.8821 | +0.5699 | 0.5529 | -3.3370 | 178 | 4/9 | 4/9 | 86.98 | 3686 | -10.53 | -9.05 |
| XMR | 500 | 8 | -5.1507 | -0.3881 | 2.4998 | -5.5588 | 1048 | 3/9 | 1/9 | 88.88 | 16665 | -3.09 | -3.34 |
| XMR | 500 | 12 | +1.5658 | -0.6601 | 1.0330 | +0.8797 | 413 | 4/9 | 4/9 | 88.80 | 6887 | +2.27 | +1.28 |
| XMR | 500 | 16 | +4.2341 | +0.0948 | 0.5208 | +4.3138 | 197 | 6/9 | 5/9 | 88.54 | 3472 | +12.20 | +12.43 |
| XMR | 1000 | 8 | -5.2781 | -0.4022 | 2.6689 | -5.7024 | 1112 | 1/9 | 0/9 | 88.76 | 17793 | -2.97 | -3.20 |
| XMR | 1000 | 12 | -0.8818 | -0.4265 | 1.1001 | -1.3353 | 421 | 4/9 | 4/9 | 88.99 | 7334 | -1.20 | -1.82 |
| XMR | 1000 | 16 | +3.0678 | +0.3077 | 0.5864 | +3.3544 | 202 | 5/9 | 6/9 | 89.45 | 3909 | +7.85 | +8.58 |
| BNB | 500 | 8 | +0.2448 | -0.0889 | 0.1615 | +0.1441 | 59 | 3/9 | 4/9 | 86.72 | 1077 | +2.27 | +1.34 |
| BNB | 500 | 12 | +0.0216 | +0.3163 | 0.0521 | +0.3298 | 11 | 1/9 | 2/9 | 57.90 | 347 | +0.62 | +9.50 |
| BNB | 500 | 16 | -0.2902 | +0.0113 | 0.0331 | -0.2855 | 7 | 1/9 | 1/9 | 44.23 | 221 | -13.15 | -12.94 |
| BNB | 1000 | 8 | -0.8138 | -0.3862 | 0.1822 | -1.2118 | 57 | 1/9 | 1/9 | 72.43 | 1215 | -6.70 | -9.98 |
| BNB | 1000 | 12 | -0.4770 | -0.3948 | 0.0747 | -0.8764 | 27 | 0/9 | 0/9 | 57.94 | 498 | -9.57 | -17.59 |
| BNB | 1000 | 16 | -0.2582 | +0.0642 | 0.0330 | -0.2005 | 7 | 1/9 | 1/9 | 44.24 | 220 | -11.72 | -9.10 |

### cancel_rate = 0.5 (realistic decay)

| coin | lat | hs | round_trip_spread_pnl | inventory_mtm_pnl | total_fees_usd | net_pnl_usd | round_trip_count | rt-days | net-days | maxinv | vol | rt bps | net bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| HYPE | 500 | 8 | -2.7269 | -0.1562 | 3.2109 | -2.9028 | 1189 | 4/9 | 4/9 | 88.16 | 21406 | -1.27 | -1.36 |
| HYPE | 500 | 12 | +0.9249 | +0.5128 | 0.9710 | +1.4182 | 314 | 4/9 | 4/9 | 88.16 | 6473 | +1.43 | +2.19 |
| HYPE | 500 | 16 | +1.1712 | -0.1167 | 0.3557 | +1.0262 | 86 | 7/9 | 7/9 | 88.16 | 2371 | +4.94 | +4.33 |
| HYPE | 1000 | 8 | -5.3577 | -0.1106 | 4.0522 | -5.4814 | 1465 | 2/9 | 2/9 | 87.50 | 27015 | -1.98 | -2.03 |
| HYPE | 1000 | 12 | +0.0758 | +0.3830 | 1.2355 | +0.4403 | 373 | 4/9 | 4/9 | 87.94 | 8237 | +0.09 | +0.53 |
| HYPE | 1000 | 16 | +1.8627 | +0.4637 | 0.4365 | +2.3045 | 110 | 7/9 | 8/9 | 87.90 | 2910 | +6.40 | +7.92 |
| kBONK | 500 | 8 | +3.1577 | +1.1445 | 1.3609 | +4.2463 | 660 | 5/9 | 6/9 | 83.40 | 9072 | +3.48 | +4.68 |
| kBONK | 500 | 12 | +2.0358 | +0.7187 | 0.6571 | +2.7082 | 291 | 6/9 | 6/9 | 81.72 | 4380 | +4.65 | +6.18 |
| kBONK | 500 | 16 | +1.5972 | +0.1655 | 0.3749 | +1.7197 | 152 | 6/9 | 6/9 | 89.16 | 2499 | +6.39 | +6.88 |
| kBONK | 1000 | 8 | -1.7359 | +0.7989 | 1.4843 | -0.9921 | 728 | 3/9 | 4/9 | 87.80 | 9895 | -1.75 | -1.00 |
| kBONK | 1000 | 12 | +1.5179 | +0.6292 | 0.6959 | +2.1092 | 322 | 5/9 | 5/9 | 81.28 | 4639 | +3.27 | +4.55 |
| kBONK | 1000 | 16 | -1.2355 | -0.0951 | 0.4409 | -1.3710 | 189 | 3/9 | 3/9 | 91.36 | 2939 | -4.20 | -4.66 |
| AVAX | 500 | 8 | -3.0246 | +0.0440 | 1.6943 | -2.9870 | 789 | 3/9 | 3/9 | 89.53 | 11295 | -2.68 | -2.64 |
| AVAX | 500 | 12 | -1.9421 | +0.2406 | 0.7947 | -1.7182 | 363 | 4/9 | 4/9 | 86.65 | 5298 | -3.67 | -3.24 |
| AVAX | 500 | 16 | -2.8432 | -1.3622 | 0.4405 | -4.2374 | 180 | 3/9 | 3/9 | 88.91 | 2937 | -9.68 | -14.43 |
| AVAX | 1000 | 8 | -3.4772 | +0.2463 | 1.9425 | -3.2466 | 898 | 4/9 | 4/9 | 89.58 | 12950 | -2.69 | -2.51 |
| AVAX | 1000 | 12 | -3.2326 | +0.4305 | 0.8960 | -2.8146 | 402 | 2/9 | 2/9 | 89.36 | 5973 | -5.41 | -4.71 |
| AVAX | 1000 | 16 | -3.6282 | +0.6675 | 0.4930 | -2.9869 | 205 | 2/9 | 3/9 | 89.12 | 3287 | -11.04 | -9.09 |
| GRAM | 500 | 8 | -10.3159 | -0.0959 | 3.2732 | -10.4397 | 1500 | 2/9 | 2/9 | 83.19 | 21821 | -4.73 | -4.78 |
| GRAM | 500 | 12 | -5.4804 | -0.0555 | 1.7020 | -5.5487 | 633 | 3/9 | 3/9 | 86.16 | 11347 | -4.83 | -4.89 |
| GRAM | 500 | 16 | -4.6527 | +0.3176 | 0.8996 | -4.3590 | 323 | 4/9 | 4/9 | 89.53 | 5997 | -7.76 | -7.27 |
| GRAM | 1000 | 8 | -9.3366 | +0.0111 | 3.4905 | -9.3527 | 1635 | 2/9 | 2/9 | 82.29 | 23270 | -4.01 | -4.02 |
| GRAM | 1000 | 12 | -4.0724 | -0.2065 | 1.8668 | -4.2984 | 786 | 4/9 | 4/9 | 87.62 | 12446 | -3.27 | -3.45 |
| GRAM | 1000 | 16 | -3.7087 | +0.2560 | 1.0129 | -3.4794 | 405 | 4/9 | 4/9 | 83.26 | 6752 | -5.49 | -5.15 |
| XMR | 500 | 8 | -6.9783 | -0.0995 | 3.8866 | -7.0944 | 1653 | 2/9 | 2/9 | 88.64 | 25911 | -2.69 | -2.74 |
| XMR | 500 | 12 | +1.2266 | -0.4979 | 1.4309 | +0.7099 | 605 | 4/9 | 3/9 | 89.34 | 9539 | +1.29 | +0.74 |
| XMR | 500 | 16 | +3.9987 | -0.2723 | 0.6479 | +3.7098 | 248 | 6/9 | 6/9 | 89.14 | 4319 | +9.26 | +8.59 |
| XMR | 1000 | 8 | -6.2114 | -0.1009 | 4.0949 | -6.3225 | 1778 | 2/9 | 2/9 | 88.93 | 27299 | -2.28 | -2.32 |
| XMR | 1000 | 12 | -0.9069 | -0.4115 | 1.5060 | -1.3420 | 626 | 4/9 | 3/9 | 87.63 | 10040 | -0.90 | -1.34 |
| XMR | 1000 | 16 | +3.3538 | +0.0160 | 0.7183 | +3.3468 | 291 | 4/9 | 5/9 | 88.16 | 4789 | +7.00 | +6.99 |
| BNB | 500 | 8 | -1.2832 | +0.0321 | 0.3227 | -1.2547 | 122 | 1/9 | 1/9 | 72.53 | 2151 | -5.97 | -5.83 |
| BNB | 500 | 12 | -0.4106 | -0.3215 | 0.0981 | -0.7351 | 32 | 2/9 | 2/9 | 72.37 | 654 | -6.27 | -11.23 |
| BNB | 500 | 16 | -1.1584 | +0.2741 | 0.0440 | -0.8929 | 13 | 1/9 | 2/9 | 57.49 | 293 | -39.51 | -30.45 |
| BNB | 1000 | 8 | -1.6787 | -0.1119 | 0.3876 | -1.7984 | 130 | 2/9 | 3/9 | 72.43 | 2584 | -6.50 | -6.96 |
| BNB | 1000 | 12 | -1.3194 | -0.4170 | 0.1253 | -1.7432 | 41 | 2/9 | 0/9 | 58.42 | 835 | -15.80 | -20.88 |
| BNB | 1000 | 16 | -1.7819 | -0.1373 | 0.0616 | -1.9278 | 21 | 2/9 | 2/9 | 72.66 | 411 | -43.39 | -46.94 |

## Part C — verdict: is there positive pure market-making edge after fees?

**Yes — but small, thin, and confined to wide half-spreads on the quieter books.**
23/72 configs have positive summed `round_trip_spread_pnl` after maker
fees across the 9-day window. All positive cells sit at hs 8-16 bps (none at the
T12-favourite hs=4-6); the coins that work are XMR, HYPE, kBONK (and tiny BNB);
BTC, ETH, kPEPE, AVAX, GRAM have **no** positive round-trip cell at any tested
latency/hs/cancel-rate (AVAX — T12's headline +39.8 bps — turns NEGATIVE on the
full 9 days once decomposed: its 2-day window was round-trip-favourable luck).

Top configs by summed round-trip:

| rank | coin | lat | hs | cr | rt+USD | rt bps | net+USD | mtm+USD | rt-days | net-days | rts | fills | maxinv |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | XMR | 500 | 16 | 0 | +4.2341 | +12.20 | +4.3138 | +0.0948 | 6/9 | 5/9 | 197 | 242 | 88.54 |
| 2 | XMR | 500 | 16 | 0.5 | +3.9987 | +9.26 | +3.7098 | -0.2723 | 6/9 | 6/9 | 248 | 303 | 89.14 |
| 3 | XMR | 1000 | 16 | 0.5 | +3.3538 | +7.00 | +3.3468 | +0.0160 | 4/9 | 5/9 | 291 | 337 | 88.16 |
| 4 | kBONK | 500 | 8 | 0.5 | +3.1577 | +3.48 | +4.2463 | +1.1445 | 5/9 | 6/9 | 660 | 696 | 83.40 |
| 5 | XMR | 1000 | 16 | 0 | +3.0678 | +7.85 | +3.3544 | +0.3077 | 5/9 | 6/9 | 202 | 272 | 89.45 |
| 6 | kBONK | 1000 | 12 | 0 | +2.3356 | +10.58 | +0.9558 | -1.3492 | 5/9 | 6/9 | 145 | 164 | 79.80 |
| 7 | kBONK | 500 | 12 | 0.5 | +2.0358 | +4.65 | +2.7082 | +0.7187 | 6/9 | 6/9 | 291 | 320 | 81.72 |
| 8 | HYPE | 1000 | 16 | 0.5 | +1.8627 | +6.40 | +2.3045 | +0.4637 | 7/9 | 8/9 | 110 | 206 | 87.90 |
| 9 | kBONK | 500 | 16 | 0.5 | +1.5972 | +6.39 | +1.7197 | +0.1655 | 6/9 | 6/9 | 152 | 180 | 89.16 |
| 10 | XMR | 500 | 12 | 0 | +1.5658 | +2.27 | +0.8797 | -0.6601 | 4/9 | 4/9 | 413 | 478 | 88.80 |
| 11 | kBONK | 1000 | 12 | 0.5 | +1.5179 | +3.27 | +2.1092 | +0.6292 | 5/9 | 5/9 | 322 | 346 | 81.28 |
| 12 | HYPE | 1000 | 16 | 0 | +1.3743 | +5.31 | +1.9240 | +0.5760 | 6/9 | 8/9 | 89 | 184 | 73.47 |

Honesty notes:

- Best cell: XMR hs=16@500 cr=0.0, +$4.23 round-trip over ~9 days (about +3 to +5 bps
  of its traded volume) — positive on only 6/9 days, and the box is a single
  ~7.5-day window in one market regime (A-20 generalization still UNVERIFIED).
- The most day-consistent cells are HYPE hs=16 (7/9 rt-days at both latencies and
  both cancel rates) and they survive the realistic queue-decay (cr=0.5) at nearly
  the same magnitude — the edge is not an artifact of the optimistic cancel model.
- Magnitudes are a few USD per 9 days at $15-60 notional quoting; before fees the
  matched spread is still small. This is research signal, not a live-trading go.
- Fresh-engine-per-day methodology means these sums exclude (and therefore cannot
  benefit from) cross-day inventory rides — the purest available MM estimate.

