# T14 — Replay Backtest Robustness Audit (9-day, local data only)

Date: 2026-10-18 · REQ-0044 (execution) + REQ-0045 (scope trim: no long workers, single-day
fills only) · Data: `data/20261006`–`20261014` (9 unequal days, 6 coins) · Baseline: T13
72-cell grid. No orders, no keys, no mainnet requests, `src/mmbot/` untouched.

**Artifacts**: `data/t14_trace_HYPE.json` / `data/t14_trace_XMR.json` (fill/hit/placement
traces, hs16 lat500 cr0), `data/t14_sweep.csv` (178 rows: HYPE hs12 full 5×4 matrix +
HYPE hs16 lat250 + slivers), `data/t14_scale.csv` (144 rows = complete $15/30/60/150 ×
HYPE/XMR × cr{0,0.5}), `data/t14_dd_path.csv` (18/18 hourly PnL paths), analysis script
`/tmp/opencode/t14_boot_wf.py` (bootstrap/walk-forward), `scripts/t14_spotcheck.py`,
`scripts/t14_{sweep,scale,drawdown}_worker.py` (VCS-clean, resumable). Fill workers were
stopped before XMR hs16 lat{250,750,1500} day13 rows landed (deliberately sacrificed for
wall-time); gaps noted below. Pytest: 187 passed.

**Formula correction (bookkeeping, verified here)**: the exact identities are
`net = matched_spread + inventory_mtm − total_fees` and
`round_trip_spread_pnl = matched_spread − fees_matched` (i.e.
`net = round_trip_spread_pnl + inventory_mtm − fees_matched_less... − fees_unmatched`).
Check cell HYPE/500/16/cr0: 1.2665 − 0.4459 − 0.3055 = **0.5151 = net** exactly.
(Any earlier paraphrase `net = rt + mtm − total_fees` is off by fees_matched; the T13
audit itself used the correct form, 648/648.)

---

## Item 1 — Spot check: 10 random round trips per coin (hs16, lat500, cr0)

Method: traced replay of the full 9 days recording placements (px, size, queue_ahead at
apply), every trade print that touched the resting order while it worked, and every fill;
FIFO lot matching recomputed from the fills exactly like the engine; fees = qty × px ×
1.5 bps per leg. Seed 7; sample = 10 of {HYPE: 126, XMR: 238} FIFO round trips.
Full verbose traces are in this file (below) and the JSONs; compact summary:

| # | side o→c | qty | px in→out | queue_ahead @ placement | depleting trades | RT pnl net |
|---|---|---|---|---|---|---|
| HYPE-1 | BUY→SELL | 0.17 | 82.934→84.146 | leg2: 235.93 | 2 prints, 235.93+251.08 queued | +0.2018 |
| HYPE-2 | BUY→SELL | 0.16 | 79.325→81.492 | leg2: 167.19 | 1 print 564.55 | +0.3429 |
| HYPE-3 | BUY→SELL | 0.01 | 85.703→85.487 | leg2: 0.92 | 1 print 1.17 | −0.0024 |
| HYPE-4 | SELL→BUY | 0.01 | 85.828→85.144 | leg1: 554.28 | 4 prints (9→1048 sh sweep) | +0.0066 |
| HYPE-5 | SELL→BUY | 0.02 | 82.952→81.514 | leg1: 157.42 | 32 prints, queue 157→0 | +0.0283 |
| HYPE-6 | SELL→BUY | 0.16 | 89.761→90.242 | leg1: 454.80 | 1 print 493.65 | −0.0813 |
| HYPE-7 | SELL→BUY | 0.16 | 89.187→88.582 | leg1: 20.70 | 1 print 140.51 | +0.0925 |
| HYPE-8 | SELL→BUY | 0.02 | 81.080→81.055 | leg1: 74.63 | 13 prints, 74.6→6.9 | +0.000014 |
| HYPE-9 | BUY→SELL | 0.17 | 87.091→82.752 | leg2: 97.17 | 5 prints 13.6→125.5 | −0.7420 |
| HYPE-10 | BUY→SELL | 0.16 | 87.342→88.464 | leg2: 15.36 | 2 prints | +0.1753 |
| XMR-1 | BUY→SELL | 0.011 | 522.18→524.89 | leg2: 6.02 | 2 prints | +0.0281 |
| XMR-2 | BUY→SELL | 0.001 | 552.18→551.38 | leg2: 3.48 | 3 prints | −0.0010 |
| XMR-3 | SELL→BUY | 0.016 | 545.87→543.29 | leg1: 22.50 | 6 prints, 22.5→1.2 | +0.0387 |
| XMR-4 | BUY→SELL | 0.016 | 531.20→536.18 | leg2: 0.0 | 1 print 0.037 | +0.0771 |
| XMR-5 | BUY→SELL | 0.002 | 562.12→559.45 | leg2: 5.56 | 4 prints | −0.0057 |
| XMR-6 | BUY→SELL | 0.002 | 557.93→570.13 | leg2: 0.0 | 1 print 0.587 | +0.0241 |
| XMR-7 | SELL→BUY | 0.003 | 555.52→553.84 | leg1: 4.38 | 4 prints, 4.4→0.03 | +0.0045 |
| XMR-8 | SELL→BUY | 0.016 | 523.41→531.48 | leg1: 13.43 | 2 prints | −0.1317 |
| XMR-9 | BUY→SELL | 0.002 | 563.25→559.63 | leg2: 8.96 | 14 prints grinding 9→0.013 | −0.0076 |
| XMR-10 | BUY→SELL | 0.016 | 539.34→543.65 | leg2: 9.53 | 5 prints | +0.0664 |

Fee example (HYPE-1): leg1 0.17×82.934×1.5e-4 = 0.002115, leg2 0.17×84.146×1.5e-4 =
0.002146; gross (84.146−82.934)×0.17 = +0.2060; net = +0.2060 − 0.004261 = **+0.2018**. ✓
Every sampled leg's fee recomputes exactly as qty×px×1.5 bps.

Mechanics observed (consistent across samples):
- The engine's matched quantity is FIFO-split: e.g. HYPE-3 opens 0.01 of a 0.17 fill; the
  remaining 0.16 matches later RTs. Match/carry is internal bookkeeping only.
- Fills are all trade-through events at or through our price; queue_before on the winning
  print shows the queue the trade had to eat (e.g. HYPE-5: 32 prints grinding 157→0).
- Trace totals (continuous 9-day run) vs T13 (fresh engine per day): HYPE rt +0.9825 vs
  +0.99, fees 0.3054 vs 0.31, mtm +0.14 vs −0.45; XMR rt +5.0753 vs +4.23, net +5.1074 vs
  +4.31. Cross-day FIFO carry shifts small amounts between the rt and mtm buckets when
  lots span midnight — the identity `net = matched + mtm − fees` holds in both framings;
  day-fresh matching is the headline accounting basis (T13).
- Placement queue_ahead ranges 0–554 coins (HYPE) / 0–22 (XMR coin units): A-13/A-19-class
  assumptions (queue position on placement) are load-bearing for fill counts.

## Item 2 — Day-block bootstrap (5,000 draws, 90% CI across the 72 T13 cells)

Resample the 9 daily values with replacement; CI = [p5, p95] of the 9-day sum.

| Metric | Cells with CI strictly > 0 | Expected positive-by-chance (~5%/cell) | Expected any-exclusion (~10%/cell) |
|---|---|---|---|
| `round_trip_spread_pnl` | **0 / 72** | ≈ 3.6 | ≈ 7.2 |
| `net_pnl_usd` | **0 / 72** | ≈ 3.6 | ≈ 7.2 |

Top cells (point estimate vs CI): XMR 500/16/0: +4.23 [−1.37, +9.32]; XMR 500/16/.5: +4.00
[−0.26, +8.28]; XMR 1000/16/.5: +3.35 [−0.64, +7.43]; kBONK 500/8/.5: +3.16 [−2.98,+10.02];
HYPE 1000/16/.5: +1.86 [−1.42,+4.56]; HYPE 500/16/0: +0.99 [−2.67,+4.18].
**No cell excludes 0 — the observed 23/72 positive point estimates are statistically
indistinguishable from chance under day-block resampling; nothing can be "discovered" on
9 days at this granularity.** (Also: 3/72 cells have CI strictly < 0 on rt — GRAM/XMR hs8
group — vs ~3.6 expected, again chance-consistent.)

## Item 3 — Walk-forward (Split1 = Oct 6–10, Split2 = Oct 11–14)

Train S1 → test S2 (best (lat,hs,cr) per coin by train rt):
AVAX 1000/12/0: −1.35 → −1.43 · BNB 500/12/0: +0.13 → −0.11 · GRAM 1000/16/0: −1.08 →
−2.81 · **HYPE 500/16/0: +2.07 → −1.07 (FAILS)** · **XMR 500/16/0: +2.51 → +1.73 (PASSES,
test net +2.01)** · kBONK 500/16/.5: +1.17 → +0.42.

Train S2 → test S1: AVAX 500/16/0: +1.73 → −2.86 · BNB 500/8/0: +0.30 → −0.06 · GRAM
1000/12/.5: +0.32 → −4.39 · HYPE 1000/16/.5: +0.18 → +1.69 · XMR 500/12/.5: +3.05 → −1.82
· kBONK 500/8/.5: +4.47 → −1.31.

hs16 persistence matrix: HYPE 500 (.0/.5): +2.07/+1.89 (5/5 days) → −1.07/−0.72 (2/4) =
**does NOT persist**; HYPE 1000: only cr.5 marginally persists (+1.69 → +0.18).
**XMR hs16 persists in all 4 (lat,cr) variants and both split directions**
(+2.51→+1.73, +1.47→+2.53, +1.69→+1.38, +0.97→+2.39).
**Verdict: the single most robust signal in the library is XMR hs16@500ms — but even it
does not clear the item-2 CI bar (its CI spans [−1.4, +9.3]), so it is "best candidate
found so far", not "confirmed edge". HYPE's Split1 edge was regime-specific and is gone
in Split2.**

## Item 4 — Latency & cancel-rate sweep + the 1000 ms > 500 ms diagnosis

HYPE hs=12 — full latency × cancel-rate matrix, 9-day rt sums (T13 + t14_sweep):

| lat\cr | 0.0 | 0.25 | 0.5 | 0.75 |
|---|---|---|---|---|
| 250 | +0.35 | +0.88 | +1.21 | +0.90 |
| 500 | +0.53 | +1.05 | +0.92 | +0.25 |
| 750 | +0.48 | +1.16 | +0.79 | +0.11 |
| 1000 | +0.95 | +0.86 | +0.08 | +0.81 |
| 1500 | +1.33 | +0.78 | +0.37 | +0.96 |

HYPE hs=16 (T13 500/1000 + new lat250; 750/1500 not run — see header gap note):

| lat\cr | 0.0 | 0.25 | 0.5 | 0.75 |
|---|---|---|---|---|
| 250 | **+2.44** | +2.53 | +2.53 | +0.87 |
| 500 | +0.99 | not run | +1.17 | not run |
| 750 | not run | not run | not run | not run |
| 1000 | +1.37 | not run | +1.86 | not run |
| 1500 | not run | not run | not run | not run |

XMR hs=16 outside T13 (lat 250/750/1500 @ day-20261013 fills): **not completed** — the 3
fill runs were killed before their single CSV append (CPU-budget rule REQ-0045), so only
the T13 500/1000 points exist (+4.23/+3.07 cr0, +4.00/+3.35 cr.5).

**Why was HYPE hs16 better at 1000 ms than 500 ms in T13? Three-part answer:**
1. **Flow, not price of risk:** 1000 ms quotes rest longer, accumulate more queue, and get
   hit more: fills 144→184 (+28%), matched round trips 70→89 (+27%), fees 0.31→0.39. The
   extra ~19 round trips × ~16 bps on ~$15-30 clips ≈ +$0.4 of spread capture.
2. **A luck flip in inventory:** mark-to-mid of open inventory swung from −0.45 (500 ms)
   to +0.58 (1000 ms) — a +$1.03 swing that is roughly twice the entire rt gap (+0.38).
   The rt gap itself is SMALLER than this noise term.
3. **The extended curve says it is not structural:** with lat250 in (hs16 cr0 +2.44;
   hs12 cr0 +0.35 — both BETTER than at 500), the latency→PnL curve is non-monotone in
   both directions, and the best cancel-rate flips with latency (hs12: cr.5 best at 250
   ms, cr.25 at 500-750, cr0 at 1000-1500; cr.5@1000 collapses to +0.08). At 90% CI no
   cell in this whole family separates from zero (item 2). **Conclusion: 1000>500 is not
   a latency edge to exploit — it is resampling noise over a jagged, regime-dependent
   response surface. Treat any (lat,cr) pick made on this window as unfitted noise.**

## Item 5 — Funding, drawdowns, size scaling

### 5a Funding [unchanged from interim]
Not modelled in `replay_backtest.py`; not present anywhere in the dataset (shards carry
only bbo/tr/l2/gap). Calm-bound impact ≈ ±$0.1–1.1 over 9 days at the tested inventory
scale (216 h × |rate| ≤ 0.01%/h × |inv| ≤ ~$50): cannot flip any CI verdict, could flip
marginal single-cell signs (BNB +$0.14). Closing this requires re-enabling the collector
or a funding-candle export — human decision, out of scope.

### 5b Worst day & max intraday peak-to-trough drawdown (hs16, lat500, cr0, hourly path)

| coin | worst single day | net (USD) | max intraday DD (USD) and day | DD range over 9 days |
|---|---|---|---|---|
| HYPE | 20261012 | **−2.46** | **2.51** (20261012) | 0.00–2.51 |
| XMR | 20261011 | **−2.09** | **2.09** (20261011) | 0.03–2.09 |

Detailed HYPE DD by day (10xx): 06:0.00 07:0.56 08:0.10 09:0.00 10:0.49 11:0.30 12:2.51
13:0.23 14:0.57 · XMR: 06:0.03 07:0.47 08:0.30 09:0.83 10:0.28 11:2.09 12:0.10 13:0.43
14:0.25. Worst-day nets cross-check T13 exactly (−2.46 / −2.09). DD is small at $15-30
quotes because FIFO matching churns positions fast — but it scales with quote size (5c).

### 5c Quote-size scaling ($15→$150 quotes, max_notional=4q, fills capped by real trade-through)

9-day sums, hs16 lat500 (complete for both coins):

| q$ | cr | HYPE rt | HYPE net | HYPE fills / RTs | HYPE avg max-inv | XMR rt | XMR net | XMR fills / RTs | XMR avg max-inv |
|---|---|---|---|---|---|---|---|---|---|
| 15 | 0.0 | +0.99 | +0.52 | 144 / 70 | $45 | +4.23 | +4.31 | 242 / 197 | $53 |
| 15 | 0.5 | +1.17 | +1.03 | 170 / 86 | $49 | +4.00 | +3.71 | 303 / 248 | $61 |
| 30 | 0.0 | +3.98 | +2.25 | 148 / 101 | $89 | +9.78 | +9.93 | 245 / 213 | $110 |
| 30 | 0.5 | +3.26 | +2.98 | 172 / 123 | $96 | +6.77 | +6.08 | 322 / 284 | $125 |
| 60 | 0.0 | +5.20 | +2.84 | 152 / 115 | $179 | +17.29 | +17.93 | 249 / 226 | $227 |
| 60 | 0.5 | +4.43 | +3.87 | 178 / 142 | $192 | +17.45 | +17.18 | 328 / 299 | $247 |
| 150 | 0.0 | +20.21 | +11.22 | 169 / 146 | $433 | +49.72 | +51.13 | 270 / 247 | $528 |
| 150 | 0.5 | +13.05 | +11.46 | 207 / 177 | $463 | +37.09 | +34.95 | 374 / 352 | $569 |

Reading:
- **The thin book did NOT cap PnL anywhere below $150 quotes** — rt grows roughly
  linearly (even superlinearly for HYPE, from more RT-churn as max-notional cycles
  faster) through the whole curve. The binding constraint is our own max_notional, not
  book liquidity. Per-fill sizes still fit inside observed trade-throughs, so the
  engine's fill cap never bound.
- Risk scales too: avg max-abs-inventory $45→$433 (HYPE), $53→$569 (XMR); net decays
  relative to rt at larger q for HYPE cr0 (mtm drag), and total exposure at q150
  approaches/crosses the funding-impact-uncertainty bound (5a) by 10×.
- **Big caveat (amplifies all unverified assumptions by 10×):** the $150 rows inherit
  A-13/A-19/A-20 queue-position and feed-age assumptions at a size 10× above anything
  observed live (mainnet N=3). Treat the q>60 columns as a liquidity-elasticity upper
  bound, not a plan. If a future phase scales size, it MUST re-verify via testnet first.

---

## Overall verdict (T14)

1. **Statistics**: 0/72 T13 cells beat zero at 90% day-block CI. The grid's apparent
   winners are lottery tickets consistent with ~3.6 expected chance positives.
2. **Robustness**: only **XMR hs16** (500 ms) persists out-of-sample in both walk-forward
   directions; HYPE hs16 does not. No param family in the fine sweep separates from noise.
3. **The 1000>500 HYPE anomaly explained**: more fills/RTs from longer resting time plus a
   +$1.03 inventory-mark swing > the +0.38 rt gap; the extended curve is non-monotone
   (lat250 is best at both hs) ⇒ noise, not structure.
4. **Funding** unmeasured, bounded ±$0.1–1.1 per 9 days at test scale; even XMR's best
   plausible edge (+$4) is above the calm bound but not above its CI.
5. **Risk at $15-30 quotes is trivial** (worst day −$2.5, max intraday DD ≤ $2.5, ~$2/day
   vs $50 inventory), and **PnL did not saturate through $150 quotes** — but scaling
   multiplies every unverified microstructure assumption and edge is not yet statistically
   real, so size is not a lever to pull yet.
6. **Net conclusion for the program**: the 9-day replay cannot certify any market-making
   configuration as profitable. If the human still wants edge discovery, the honest
   options are (i) re-enable the collector to grow the dataset (and add funding), or
   (ii) testnet shadow-run XMR hs16@500 with $15-30 quotes for statistical power, or
   (iii) stop here and account for opportunity cost. No live orders belong in any branch.

Standing caveats: A-20 (feed age 400 ms, UNVERIFIED) is more load-bearing after T14 than
before: every fill-count, latency, and scaling result depends on it. 9 unequal days, one
regime, one market window. Day 14 partial (h00-20), Oct 9 outage, hour-15 truncated shards
handled with intact prefixes. This is a research audit, not a live-performance claim.

---
## Appendix — verbose spot-check traces (engine output, 10 random RTs per coin)


### SPOT CHECK HYPE (hs=16, lat=500ms, cr=0.0) — 126 total FIFO round trips, showing 10 random

RT#1 HYPE open BUY 0.17 @ 82.934 -> close SELL @ 84.146  t 1791482615.752 -> 1791484820.563
  leg1: FILL BUY 0.18 @ 82.934 mid=82.95750000000001 queue_before=0.0
  leg2: FILL SELL 0.17 @ 84.146 mid=84.0445 queue_before=0.0
      PLACE px=84.146 size=0.17 queue_ahead_at_place=235.92999999999998 t=1791484820.289
      leg2 queue-depleting trades (n=2):
        TR t=1791484820.563 px=84.146 sz=0.66 side=B w_px=84.146 queue_before=235.92999999999998
        TR t=1791484820.563 px=84.147 sz=251.08 side=B w_px=84.146 queue_before=235.26999999999998
  fees: leg1=0.002115 leg2=0.002146 total=0.004261  RT pnl (net) = +0.201779
RT#2 HYPE open BUY 0.15999999999999998 @ 79.325 -> close SELL @ 81.492  t 1792001136.953 -> 1792007893.874
  leg1: FILL BUY 0.18 @ 79.325 mid=79.32249999999999 queue_before=0.0
  leg2: FILL SELL 0.18 @ 81.492 mid=81.4935 queue_before=0.0
      PLACE px=81.492 size=0.18 queue_ahead_at_place=167.19 t=1792007893.742
      leg2 queue-depleting trades (n=1):
        TR t=1792007893.874 px=81.492 sz=564.55 side=B w_px=81.492 queue_before=167.19
  fees: leg1=0.001904 leg2=0.001956 total=0.003860  RT pnl (net) = +0.342860
RT#3 HYPE open BUY 0.010000000000000009 @ 85.703 -> close SELL @ 85.487  t 1791459812.471 -> 1791467988.682
  leg1: FILL BUY 0.17 @ 85.703 mid=85.731 queue_before=0.0
  leg2: FILL SELL 0.17 @ 85.487 mid=85.46549999999999 queue_before=0.0
      PLACE px=85.487 size=0.17 queue_ahead_at_place=0.92 t=1791467984.593
      leg2 queue-depleting trades (n=1):
        TR t=1791467988.682 px=85.487 sz=1.17 side=B w_px=85.487 queue_before=0.92
  fees: leg1=0.000129 leg2=0.000128 total=0.000257  RT pnl (net) = -0.002417
RT#4 HYPE open SELL 0.009999999999999981 @ 85.828 -> close BUY @ 85.144  t 1791555804.35 -> 1791558486.396
  leg1: FILL SELL 0.17 @ 85.828 mid=85.7245 queue_before=0.0
      PLACE px=85.828 size=0.17 queue_ahead_at_place=554.2800000000001 t=1791555803.127
  leg2: FILL BUY 0.17 @ 85.144 mid=85.1925 queue_before=0.0
      leg1 queue-depleting trades (n=4):
        TR t=1791555804.35 px=85.828 sz=9.01 side=B w_px=85.828 queue_before=554.2800000000001
        TR t=1791555804.35 px=85.828 sz=18.29 side=B w_px=85.828 queue_before=545.2700000000001
        TR t=1791555804.35 px=85.829 sz=231.53 side=B w_px=85.828 queue_before=526.9800000000001
        TR t=1791555804.35 px=85.829 sz=1048.59 side=B w_px=85.828 queue_before=295.45000000000016
  fees: leg1=0.000129 leg2=0.000128 total=0.000256  RT pnl (net) = +0.006584
RT#5 HYPE open SELL 0.019999999999999962 @ 82.952 -> close BUY @ 81.514  t 1791882590.793 -> 1791893320.283
  leg1: FILL SELL 0.18 @ 82.952 mid=82.9505 queue_before=0.0
      PLACE px=82.952 size=0.18 queue_ahead_at_place=157.42000000000002 t=1791882585.92
  leg2: FILL BUY 0.18 @ 81.514 mid=81.3995 queue_before=0.0
      leg1 queue-depleting trades (n=32):
        TR t=1791882590.793 px=82.952 sz=1.0 side=B w_px=82.952 queue_before=157.42000000000002
        TR t=1791882590.793 px=82.952 sz=2.0 side=B w_px=82.952 queue_before=156.42000000000002
        TR t=1791882590.793 px=82.952 sz=0.14 side=B w_px=82.952 queue_before=154.42000000000002
        TR t=1791882590.793 px=82.953 sz=0.14 side=B w_px=82.952 queue_before=154.28000000000003
        TR t=1791882590.793 px=82.955 sz=0.14 side=B w_px=82.952 queue_before=154.14000000000004
        TR t=1791882590.793 px=82.957 sz=0.14 side=B w_px=82.952 queue_before=154.00000000000006
        TR t=1791882590.793 px=82.958 sz=1.0 side=B w_px=82.952 queue_before=153.86000000000007
        TR t=1791882590.793 px=82.958 sz=0.26 side=B w_px=82.952 queue_before=152.86000000000007
        TR t=1791882590.793 px=82.958 sz=3.0 side=B w_px=82.952 queue_before=152.60000000000008
        TR t=1791882590.793 px=82.958 sz=4.82 side=B w_px=82.952 queue_before=149.60000000000008
        TR t=1791882590.793 px=82.958 sz=0.14 side=B w_px=82.952 queue_before=144.7800000000001
        TR t=1791882590.793 px=82.959 sz=0.14 side=B w_px=82.952 queue_before=144.6400000000001
        TR t=1791882590.793 px=82.96 sz=0.14 side=B w_px=82.952 queue_before=144.5000000000001
        TR t=1791882590.793 px=82.961 sz=73.15 side=B w_px=82.952 queue_before=144.36000000000013
        TR t=1791882590.793 px=82.961 sz=0.14 side=B w_px=82.952 queue_before=71.21000000000012
        TR t=1791882590.793 px=82.962 sz=0.13 side=B w_px=82.952 queue_before=71.07000000000012
        TR t=1791882590.793 px=82.962 sz=0.14 side=B w_px=82.952 queue_before=70.94000000000013
        TR t=1791882590.793 px=82.963 sz=13.18 side=B w_px=82.952 queue_before=70.80000000000013
        TR t=1791882590.793 px=82.963 sz=0.72 side=B w_px=82.952 queue_before=57.620000000000125
        TR t=1791882590.793 px=82.964 sz=4.82 side=B w_px=82.952 queue_before=56.90000000000013
        TR t=1791882590.793 px=82.964 sz=0.61 side=B w_px=82.952 queue_before=52.080000000000126
        TR t=1791882590.793 px=82.965 sz=0.6 side=B w_px=82.952 queue_before=51.47000000000013
        TR t=1791882590.793 px=82.965 sz=1.43 side=B w_px=82.952 queue_before=50.870000000000125
        TR t=1791882590.793 px=82.965 sz=1.07 side=B w_px=82.952 queue_before=49.440000000000126
        TR t=1791882590.793 px=82.965 sz=0.3 side=B w_px=82.952 queue_before=48.370000000000125
        TR t=1791882590.793 px=82.966 sz=0.34 side=B w_px=82.952 queue_before=48.07000000000013
        TR t=1791882590.793 px=82.967 sz=3.0 side=B w_px=82.952 queue_before=47.730000000000125
        TR t=1791882590.793 px=82.967 sz=0.25 side=B w_px=82.952 queue_before=44.730000000000125
        TR t=1791882590.793 px=82.968 sz=0.14 side=B w_px=82.952 queue_before=44.480000000000125
        TR t=1791882590.793 px=82.969 sz=31.28 side=B w_px=82.952 queue_before=44.340000000000124
        TR t=1791882590.793 px=82.969 sz=12.04 side=B w_px=82.952 queue_before=13.060000000000123
        TR t=1791882590.793 px=82.969 sz=12.04 side=B w_px=82.952 queue_before=1.020000000000124
  fees: leg1=0.000249 leg2=0.000245 total=0.000493  RT pnl (net) = +0.028267
RT#6 HYPE open SELL 0.16 @ 89.761 -> close BUY @ 90.242  t 1791338514.147 -> 1791346699.687
  leg1: FILL SELL 0.16 @ 89.761 mid=89.73949999999999 queue_before=0.0
      PLACE px=89.761 size=0.16 queue_ahead_at_place=454.8 t=1791338513.813
  leg2: FILL BUY 0.16 @ 90.242 mid=90.3005 queue_before=0.0
      leg1 queue-depleting trades (n=1):
        TR t=1791338514.147 px=89.762 sz=493.65 side=B w_px=89.761 queue_before=454.8
  fees: leg1=0.002154 leg2=0.002166 total=0.004320  RT pnl (net) = -0.081280
RT#7 HYPE open SELL 0.16 @ 89.187 -> close BUY @ 88.582  t 1791381250.11 -> 1791389465.446
  leg1: FILL SELL 0.16 @ 89.187 mid=89.1245 queue_before=0.0
      PLACE px=89.187 size=0.16 queue_ahead_at_place=20.699999999999996 t=1791381249.979
  leg2: FILL BUY 0.16 @ 88.582 mid=88.6255 queue_before=0.0
      leg1 queue-depleting trades (n=1):
        TR t=1791381250.11 px=89.187 sz=140.51 side=B w_px=89.187 queue_before=20.699999999999996
  fees: leg1=0.002140 leg2=0.002126 total=0.004266  RT pnl (net) = +0.092534
RT#8 HYPE open SELL 0.019999999999999962 @ 81.08 -> close BUY @ 81.055  t 1791981068.175 -> 1791981072.369
  leg1: FILL SELL 0.18 @ 81.08 mid=81.09 queue_before=0.0
      PLACE px=81.08 size=0.18 queue_ahead_at_place=74.63 t=1791981066.43
  leg2: FILL BUY 0.18 @ 81.055 mid=81.05000000000001 queue_before=0.0
      leg1 queue-depleting trades (n=13):
        TR t=1791981068.099 px=81.081 sz=0.14 side=B w_px=81.08 queue_before=74.63
        TR t=1791981068.099 px=81.084 sz=12.18 side=B w_px=81.08 queue_before=74.49
        TR t=1791981068.099 px=81.084 sz=0.16 side=B w_px=81.08 queue_before=62.309999999999995
        TR t=1791981068.099 px=81.087 sz=0.23 side=B w_px=81.08 queue_before=62.15
        TR t=1791981068.099 px=81.087 sz=0.35 side=B w_px=81.08 queue_before=61.92
        TR t=1791981068.099 px=81.088 sz=0.26 side=B w_px=81.08 queue_before=61.57
        TR t=1791981068.099 px=81.09 sz=4.84 side=B w_px=81.08 queue_before=61.31
        TR t=1791981068.099 px=81.091 sz=0.35 side=B w_px=81.08 queue_before=56.47
        TR t=1791981068.099 px=81.092 sz=6.17 side=B w_px=81.08 queue_before=56.12
        TR t=1791981068.099 px=81.094 sz=12.28 side=B w_px=81.08 queue_before=49.949999999999996
        TR t=1791981068.099 px=81.094 sz=12.32 side=B w_px=81.08 queue_before=37.669999999999995
        TR t=1791981068.175 px=81.094 sz=18.48 side=B w_px=81.08 queue_before=25.349999999999994
        TR t=1791981068.175 px=81.094 sz=12.32 side=B w_px=81.08 queue_before=6.869999999999994
  fees: leg1=0.000243 leg2=0.000243 total=0.000486  RT pnl (net) = +0.000014
RT#9 HYPE open BUY 0.17 @ 87.091 -> close SELL @ 82.752  t 1791795196.131 -> 1791841472.238
  leg1: FILL BUY 0.17 @ 87.091 mid=87.156 queue_before=0.0
  leg2: FILL SELL 0.18 @ 82.752 mid=82.7375 queue_before=0.0
      PLACE px=82.752 size=0.18 queue_ahead_at_place=97.17 t=1791841472.238
      leg2 queue-depleting trades (n=5):
        TR t=1791841472.238 px=82.752 sz=13.63 side=B w_px=82.752 queue_before=97.17
        TR t=1791841472.238 px=82.753 sz=24.2 side=B w_px=82.752 queue_before=83.54
        TR t=1791841472.238 px=82.754 sz=3.43 side=B w_px=82.752 queue_before=59.34
        TR t=1791841472.238 px=82.756 sz=16.06 side=B w_px=82.752 queue_before=55.910000000000004
        TR t=1791841472.238 px=82.758 sz=125.49 side=B w_px=82.752 queue_before=39.85000000000001
  fees: leg1=0.002221 leg2=0.002110 total=0.004331  RT pnl (net) = -0.741961
RT#10 HYPE open BUY 0.16 @ 87.342 -> close SELL @ 88.464  t 1791409034.776 -> 1791416350.552
  leg1: FILL BUY 0.17 @ 87.342 mid=87.41 queue_before=0.0
  leg2: FILL SELL 0.16 @ 88.464 mid=88.4575 queue_before=0.0
      PLACE px=88.464 size=0.16 queue_ahead_at_place=15.360000000000001 t=1791416311.308
      leg2 queue-depleting trades (n=2):
        TR t=1791416350.552 px=88.464 sz=1.21 side=B w_px=88.464 queue_before=15.360000000000001
        TR t=1791416350.552 px=88.464 sz=25.03 side=B w_px=88.464 queue_before=14.150000000000002
  fees: leg1=0.002096 leg2=0.002123 total=0.004219  RT pnl (net) = +0.175301
  [trace totals HYPE: fills=143 rts=126 rt_pnl=+0.9825 inv_mtm=+0.1432 fees=0.3054 net=+1.1211]

### SPOT CHECK XMR (hs=16, lat=500ms, cr=0.0) — 238 total FIFO round trips, showing 10 random

RT#1 XMR open BUY 0.01100000000000001 @ 522.18 -> close SELL @ 524.89  t 1791480134.519 -> 1791484515.075
  leg1: FILL BUY 0.028 @ 522.18 mid=522.005 queue_before=0.0
  leg2: FILL SELL 0.028 @ 524.89 mid=526.0699999999999 queue_before=0.0
      PLACE px=524.89 size=0.028 queue_ahead_at_place=6.021000000000001 t=1791484514.385
      leg2 queue-depleting trades (n=2):
        TR t=1791484515.075 px=524.97 sz=0.469 side=B w_px=524.89 queue_before=6.021000000000001
        TR t=1791484515.075 px=524.99 sz=10.618 side=B w_px=524.89 queue_before=5.5520000000000005
  fees: leg1=0.000862 leg2=0.000866 total=0.001728  RT pnl (net) = +0.028082
RT#2 XMR open BUY 0.0010000000000000009 @ 552.18 -> close SELL @ 551.38  t 1791386020.046 -> 1791412485.768
  leg1: FILL BUY 0.027 @ 552.18 mid=552.485 queue_before=0.0
  leg2: FILL SELL 0.027 @ 551.38 mid=551.225 queue_before=0.0
      PLACE px=551.38 size=0.027 queue_ahead_at_place=3.477 t=1791412173.174
      leg2 queue-depleting trades (n=3):
        TR t=1791412485.768 px=551.41 sz=1.458 side=B w_px=551.38 queue_before=3.477
        TR t=1791412485.768 px=551.41 sz=1.764 side=B w_px=551.38 queue_before=2.019
        TR t=1791412485.768 px=551.42 sz=7.633 side=B w_px=551.38 queue_before=0.2550000000000001
  fees: leg1=0.000083 leg2=0.000083 total=0.000166  RT pnl (net) = -0.000966
RT#3 XMR open SELL 0.01599999999999999 @ 545.87 -> close BUY @ 543.29  t 1791506170.281 -> 1791531202.316
  leg1: FILL SELL 0.027 @ 545.87 mid=545.53 queue_before=0.0
      PLACE px=545.87 size=0.027 queue_ahead_at_place=22.496 t=1791506170.281
  leg2: FILL BUY 0.027 @ 543.29 mid=543.56 queue_before=0.0
      leg1 queue-depleting trades (n=6):
        TR t=1791506170.281 px=545.89 sz=2.747 side=B w_px=545.87 queue_before=22.496
        TR t=1791506170.281 px=545.92 sz=7.232 side=B w_px=545.87 queue_before=19.749
        TR t=1791506170.281 px=545.98 sz=2.13 side=B w_px=545.87 queue_before=12.517
        TR t=1791506170.281 px=545.99 sz=5.612 side=B w_px=545.87 queue_before=10.387
        TR t=1791506170.281 px=545.99 sz=3.616 side=B w_px=545.87 queue_before=4.775
        TR t=1791506170.281 px=546.0 sz=17.548 side=B w_px=545.87 queue_before=1.1590000000000003
  fees: leg1=0.001310 leg2=0.001304 total=0.002614  RT pnl (net) = +0.038666
RT#4 XMR open BUY 0.015999999999998536 @ 531.2 -> close SELL @ 536.18  t 1791831581.308 -> 1791835325.591
  leg1: FILL BUY 0.028 @ 531.2 mid=531.65 queue_before=0.0
  leg2: FILL SELL 0.027 @ 536.18 mid=536.9449999999999 queue_before=0.0
      PLACE px=536.18 size=0.027 queue_ahead_at_place=0.0 t=1791835325.591
      leg2 queue-depleting trades (n=1):
        TR t=1791835325.591 px=536.68 sz=0.037 side=B w_px=536.18 queue_before=0.0
  fees: leg1=0.001275 leg2=0.001287 total=0.002562  RT pnl (net) = +0.077118
RT#5 XMR open BUY 0.0020000000000000018 @ 562.12 -> close SELL @ 559.45  t 1791344010.89 -> 1791347459.607
  leg1: FILL BUY 0.026 @ 562.12 mid=562.145 queue_before=0.0
  leg2: FILL SELL 0.026 @ 559.45 mid=559.24 queue_before=0.0
      PLACE px=559.45 size=0.026 queue_ahead_at_place=5.562 t=1791347424.269
      leg2 queue-depleting trades (n=4):
        TR t=1791347459.607 px=559.57 sz=0.469 side=B w_px=559.45 queue_before=5.562
        TR t=1791347459.607 px=559.58 sz=0.089 side=B w_px=559.45 queue_before=5.093
        TR t=1791347459.607 px=559.6 sz=0.715 side=B w_px=559.45 queue_before=5.004
        TR t=1791347459.607 px=559.61 sz=7.329 side=B w_px=559.45 queue_before=4.289
  fees: leg1=0.000169 leg2=0.000168 total=0.000336  RT pnl (net) = -0.005676
RT#6 XMR open BUY 0.0020000000000000018 @ 557.93 -> close SELL @ 570.13  t 1791349219.553 -> 1791356268.267
  leg1: FILL BUY 0.026 @ 557.93 mid=558.915 queue_before=0.0
  leg2: FILL SELL 0.026 @ 570.13 mid=570.28 queue_before=0.0
      PLACE px=570.13 size=0.026 queue_ahead_at_place=0.0 t=1791356268.267
      leg2 queue-depleting trades (n=1):
        TR t=1791356268.267 px=570.35 sz=0.587 side=B w_px=570.13 queue_before=0.0
  fees: leg1=0.000167 leg2=0.000171 total=0.000338  RT pnl (net) = +0.024062
RT#7 XMR open SELL 0.003000000000001151 @ 555.52 -> close BUY @ 553.84  t 1791954700.865 -> 1791970208.954
  leg1: FILL SELL 0.027 @ 555.52 mid=555.195 queue_before=0.0
      PLACE px=555.52 size=0.027 queue_ahead_at_place=4.381 t=1791954698.499
  leg2: FILL BUY 0.027 @ 553.84 mid=553.51 queue_before=0.0
      leg1 queue-depleting trades (n=4):
        TR t=1791954700.865 px=555.52 sz=3.635 side=B w_px=555.52 queue_before=4.381
        TR t=1791954700.865 px=555.55 sz=0.333 side=B w_px=555.52 queue_before=0.7460000000000004
        TR t=1791954700.865 px=555.55 sz=0.381 side=B w_px=555.52 queue_before=0.4130000000000004
        TR t=1791954700.865 px=555.57 sz=0.486 side=B w_px=555.52 queue_before=0.03200000000000042
  fees: leg1=0.000250 leg2=0.000249 total=0.000499  RT pnl (net) = +0.004541
RT#8 XMR open SELL 0.015999999999998536 @ 523.41 -> close BUY @ 531.48  t 1791682461.258 -> 1791729802.267
  leg1: FILL SELL 0.028 @ 523.41 mid=522.9749999999999 queue_before=0.0
      PLACE px=523.41 size=0.028 queue_ahead_at_place=13.434 t=1791682460.789
  leg2: FILL BUY 0.028 @ 531.48 mid=531.475 queue_before=0.0
      leg1 queue-depleting trades (n=2):
        TR t=1791682461.258 px=523.43 sz=1.093 side=B w_px=523.41 queue_before=13.434
        TR t=1791682461.258 px=523.44 sz=15.32 side=B w_px=523.41 queue_before=12.341
  fees: leg1=0.001256 leg2=0.001276 total=0.002532  RT pnl (net) = -0.131652
RT#9 XMR open BUY 0.0020000000000000018 @ 563.25 -> close SELL @ 559.63  t 1791361740.227 -> 1791366343.297
  leg1: FILL BUY 0.026 @ 563.25 mid=563.1600000000001 queue_before=0.0
  leg2: FILL SELL 0.026 @ 559.63 mid=559.405 queue_before=0.0
      PLACE px=559.63 size=0.026 queue_ahead_at_place=8.955 t=1791366342.088
      leg2 queue-depleting trades (n=14):
        TR t=1791366342.624 px=559.76 sz=0.37 side=B w_px=559.63 queue_before=8.955
        TR t=1791366342.624 px=559.76 sz=0.46 side=B w_px=559.63 queue_before=8.585
        TR t=1791366342.624 px=559.76 sz=0.659 side=B w_px=559.63 queue_before=8.125
        TR t=1791366342.624 px=559.76 sz=0.589 side=B w_px=559.63 queue_before=7.466
        TR t=1791366342.624 px=560.0 sz=0.5 side=B w_px=559.63 queue_before=6.877000000000001
        TR t=1791366342.624 px=560.25 sz=0.317 side=B w_px=559.63 queue_before=6.377000000000001
        TR t=1791366342.689 px=560.31 sz=0.035 side=B w_px=559.63 queue_before=6.0600000000000005
        TR t=1791366343.297 px=559.67 sz=1.002 side=B w_px=559.63 queue_before=6.025
        TR t=1791366343.297 px=559.68 sz=1.002 side=B w_px=559.63 queue_before=5.023000000000001
        TR t=1791366343.297 px=559.68 sz=1.002 side=B w_px=559.63 queue_before=4.021000000000001
        TR t=1791366343.297 px=559.69 sz=1.002 side=B w_px=559.63 queue_before=3.019000000000001
        TR t=1791366343.297 px=559.69 sz=1.002 side=B w_px=559.63 queue_before=2.0170000000000012
        TR t=1791366343.297 px=559.69 sz=1.002 side=B w_px=559.63 queue_before=1.0150000000000012
        TR t=1791366343.297 px=559.7 sz=1.002 side=B w_px=559.63 queue_before=0.013000000000001233
  fees: leg1=0.000169 leg2=0.000168 total=0.000337  RT pnl (net) = -0.007577
RT#10 XMR open BUY 0.01599999999999999 @ 539.34 -> close SELL @ 543.65  t 1791503535.306 -> 1791506082.397
  leg1: FILL BUY 0.027 @ 539.34 mid=539.505 queue_before=0.0
  leg2: FILL SELL 0.027 @ 543.65 mid=543.505 queue_before=0.0
      PLACE px=543.65 size=0.027 queue_ahead_at_place=9.533 t=1791506081.794
      leg2 queue-depleting trades (n=5):
        TR t=1791506082.397 px=543.67 sz=0.898 side=B w_px=543.65 queue_before=9.533
        TR t=1791506082.397 px=543.69 sz=0.2 side=B w_px=543.65 queue_before=8.635
        TR t=1791506082.397 px=543.7 sz=7.239 side=B w_px=543.65 queue_before=8.435
        TR t=1791506082.397 px=543.71 sz=0.091 side=B w_px=543.65 queue_before=1.1960000000000006
        TR t=1791506082.397 px=543.72 sz=2.758 side=B w_px=543.65 queue_before=1.1050000000000006
  fees: leg1=0.001294 leg2=0.001305 total=0.002599  RT pnl (net) = +0.066361
  [trace totals XMR: fills=246 rts=238 rt_pnl=+5.0753 inv_mtm=+0.0342 fees=0.5296 net=+5.1074]
