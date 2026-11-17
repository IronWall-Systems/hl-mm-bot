# MM cooldown-plugin backtest — HYPE, 2026-10-20 → 2026-11-04

Research-only backtest of the modular cooldown plugins in
`strategies/market_maker/plugins/`.  No core engine file was modified.

## Scope and data coverage

- **Coin:** HYPE
- **Engine:** `scripts/replay_backtest.ReplayEngine`, driven by a new harness
  (`scripts/mm_cooldown_backtest.py`) that calls the plugins through their public API.
- **Requested window:** 2026-10-20 → 2026-11-04 (through the latest available tick data).
- **Tick days actually used:** 2026-10-20 … 2026-11-02 (14 full UTC days) plus
  2026-11-04 (partial: hours 03, 04, 09, 11, 12, 16, 18, 21, 22).
- **Missing tick data:** no collector shards exist for **2026-11-03** or **2026-11-05**.
  1H HYPE candles exist through 2026-11-05 14:00 UTC, but the MM queue replay requires
  L2/trade ticks, so those days cannot be simulated without inventing microstructure.
- **Protocol:** 16 bps half-spread, 6 bps skew, $15 quote / $60 max, $10 min,
  cancel_rate 0.5, 66 ms latency, maker fee 1.5 bps, $30 starting equity, fresh engine
  per UTC day, hourly funding from collector records.  This matches the prior
  `compare_strategies.py` Strategy A protocol.

### Validation of the harness

The baseline reproduces the previously published result exactly:

| Metric | This run | Prior reference |
|---|---:|---:|
| Round trips | 647 | 647 |
| Net PnL | **−$1.0022** | −$1.00 |
| Win rate | 53.32% | 53.3% |
| Oct 29 net | **−$6.5424** | −$6.54 |

(The max-DD here, $8.1086, is measured on a 10-second equity grid; the earlier
$7.98 figure used hourly points.)

## Cooldown hook used in the replay

The plugin is consulted on every quoting decision (~0.3 s minimum interval):

1. Account equity is marked to market:
   `start_equity + cash + position × mid + funding − fees`.
2. `plugin.observe_equity(equity)` and `plugin.evaluate()` run on a **simulated clock**
   whose time is the replayed event timestamp.
3. When the plugin reports `allowed=False`, the target quote set is emptied.  The normal
   replay path then **cancels all resting quotes and places nothing new** — the same
   behaviour as the live `OrderManager.plan` hook in
   `strategies/market_maker/plugins/integration.py`.
4. Plugin state is written to a temporary directory, so production cooldown state is
   never touched.  Threshold: **$0.25** (inclusive).

## Primary results

| Variant | Trades (round trips) | Win rate | Profit factor | Net PnL | Max DD | Max DD % | Worst day | Oct 29 net | Oct 29 intraday DD | Triggers | Blocked decisions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Baseline (no cooldown)** | 647 | 53.32% | 0.931 | **−$1.0022** | $8.1086 | 23.04% | −$6.5424 | −$6.5424 | $7.2174 | 0 | 0.00% |
| **Cooldown 6h** | 465 | 54.41% | 1.071 | **+$4.8047** | $7.6103 | 21.33% | −$1.6451 | −$1.4958 | $7.0054 | 2 | 3.74% |
| **Cooldown 12h** | 437 | 54.23% | 1.009 | **+$2.3061** | $6.8735 | 19.57% | −$2.7598 | −$2.7598 | $6.2686 | 2 | 7.53% |
| **Cooldown 24h (4 × 6h)** | 410 | 52.93% | 0.894 | **+$0.5616** | $6.8735 | 19.69% | −$2.7598 | −$2.7598 | $6.2686 | 2 | 14.72% |

### Execution / cost detail

| Variant | Fills | Fees | Funding | Volume (USD) | Net / MaxDD | Positive days |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | 1,155 | $2.4164 | −$0.0236 | 16,109 | −0.124 | 9/15 |
| Cooldown 6h | 825 | $1.7240 | −$0.0165 | 11,494 | **0.631** | 9/15 |
| Cooldown 12h | 770 | $1.6056 | −$0.0142 | 10,704 | 0.336 | 9/15 |
| Cooldown 24h (4 × 6h) | 724 | $1.5043 | −$0.0122 | 10,029 | 0.082 | 9/15 |

## Tail-risk mitigation (vs baseline)

| Variant | Full max DD reduction | Oct 29 net loss reduction | Oct 29 intraday DD reduction | Worst-day loss reduction | Oct 29 + Nov 2 tail net | Net PnL change |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | — | — | — | — | −$8.1875 | — |
| Cooldown 6h | 6.15% | **77.14%** | 2.94% | **74.85%** | **−$3.1410** | +$5.8069 |
| Cooldown 12h | **15.23%** | 57.82% | **13.15%** | 57.82% | −$4.4049 | +$3.3083 |
| Cooldown 24h (4 × 6h) | **15.23%** | 57.82% | **13.15%** | 57.82% | −$4.4049 | +$1.5638 |

Tail improvement vs baseline: 6h **61.6%**, 12h/24h **46.2%**.

Max-drawdown peaks/troughs:

- **Baseline:** $35.2001 on 2026-10-27 17:05 → $27.0915 on 2026-10-30 00:54.
- **6h:** $35.6807 on 2026-10-27 17:05 → $28.0704 on 2026-10-29 21:37.
- **12h:** $35.1174 on 2026-10-27 17:05 → $28.2438 on 2026-10-29 21:37.
- **24h:** $34.9149 on 2026-10-27 17:05 → $28.0414 on 2026-10-29 21:37.

### Cooldown timeline

All three plugins armed twice, at nearly the same points:

| Variant | Arm 1 | Arm 1 loss | Blocked until | Arm 2 | Arm 2 loss | Blocked until |
|---|---|---|---:|---|---:|---|
| 6h | 10-20 08:06 | $0.2542 | 10-20 14:06 | 10-29 20:50 | $0.2900 | 10-30 02:50 |
| 12h | 10-20 08:06 | $0.2542 | 10-20 20:06 | 10-29 20:49 | $0.2715 | 10-30 08:49 |
| 24h (4 × 6h) | 10-20 08:06 | $0.2542 | 10-21 08:06 | 10-29 20:48 | $0.2750 | 10-30 20:48 |

The 24-hour plugin completed all four 6-hour checks in each window (the 4th
check coincides with expiry).

## Interpretation

1. **On this full-period sample, every cooldown variant beat the baseline on both
   net PnL and max drawdown.** The baseline lost $1.00; the variants made
   +$0.56 to +$4.80 while cutting max DD by 6–15%.
2. **`cooldown_6h` has the best net PnL and profit factor** (+$4.8047, PF 1.071),
   but the smallest drawdown reduction (6.15%). It blocks only 3.74% of decisions
   and catches part of the Oct 29 move without missing most of the Oct 30 recovery.
3. **`cooldown_12h` has the best drawdown profile** ($6.8735, 19.57%) and cuts the
   Oct 29 net loss by 58% and Oct 29 intraday DD by 13%. It sacrifices some
   Oct 30 recovery but far less than the 24h variant.
4. **`cooldown_24h_4blocks` gives the same max DD as 12h but lower net PnL
   (+$0.5616).** Its extra block through Oct 30 20:48 removes most of a profitable
   day (+$0.39 vs +$1.93 for 12h and +$2.32 baseline). On this sample the fourth
   block costs more than it protects.
5. The breaker mainly reduces **realized tail loss** (worst day −75% to −58%), not
   the intraday mark-to-market excursion (Oct 29 DD only −3% to −13%).

## Important caveat: path dependence and inventory stranding

The result is not a guarantee of tail protection.  A separate **isolated Oct 29
stress run** (same engine, but starting a fresh $30 session on Oct 29, so the
threshold arms early) gives the opposite risk result:

| Variant | Trades | Net PnL | Max DD | Oct 29 DD | Triggers |
|---|---:|---:|---:|---:|---:|
| Baseline | 368 | −$6.5424 | $7.2174 | $7.2174 | 0 |
| 6h | 22 | −$6.8483 | $9.1008 | $9.1008 | 2 |
| 12h | 2 | −$4.5828 | $9.1008 | $9.1008 | 2 |
| 24h (4 × 6h) | 2 | −$4.5828 | $9.1008 | $9.1008 | 1 |

In that scenario the breaker fires at 10:32 UTC.  Cancelling **both** quote sides
strands one-sided inventory during a trend, so the intraday mark-to-market
drawdown grows from $7.22 to $9.10.  In the full-period run the trigger happened
much later (20:48–20:50), after a different inventory/equity path, and therefore
helped.  A robust live design should keep the risk-reducing side (or flatten on
trigger) rather than cancelling every quote.

## Limitations

- Only 15 tick days and **two trigger events**; one day (Oct 29) dominates the tail
  statistics.  This is a screen, not statistical validation.
- No tick data for Nov 3 or Nov 5, so the window is not literally continuous to
  Nov 5.
- Equity-based loss includes unrealized PnL, so ordinary inventory noise can arm
  the breaker (first arm on Oct 20 was a $0.2542 mark-to-market dip on a day that
  still finished positive).
- Daily engine resets follow the prior PREREG protocol; a continuous-engine run
  would produce a different path.

## Recommendation

- **Best net / opportunity cost:** `cooldown_6h`.
- **Best drawdown reduction:** `cooldown_12h`.
- **Not supported by this sample:** `cooldown_24h_4blocks` (same DD as 12h, lower net).
- Before any live consideration, add inventory-aware trigger behaviour and run a
  forward paper test.  The MM account currently reads $0.00 equity, so live orders
  are impossible regardless of plugin choice.

## Reproduction

```bash
cd /root/hl-mm-bot

# Full comparison (15 available tick days, 4 variants in one pass)
venv/bin/python scripts/mm_cooldown_backtest.py \
    --start 20261020 --end 20261104 \
    --csv logs/mm_cooldown_backtest_20261105.csv \
    --json logs/mm_cooldown_backtest_20261105.json \
    | tee logs/mm_cooldown_backtest_20261105.log

# Isolated Oct 29 stress run (fresh $30 session on Oct 29)
venv/bin/python scripts/mm_cooldown_backtest.py \
    --start 20261029 --end 20261029 \
    --csv logs/mm_cooldown_oct29_stress_20261105.csv \
    --json logs/mm_cooldown_oct29_stress_20261105.json \
    | tee logs/mm_cooldown_oct29_stress_20261105.log
```

Artifacts:

- `scripts/mm_cooldown_backtest.py` — new harness; no core edits.
- `logs/mm_cooldown_backtest_20261105.{log,csv,json}`
- `logs/mm_cooldown_oct29_stress_20261105.{log,csv,json}`
- `docs/mm_cooldown_backtest_20261105.md` (this report)
