# Tokyo latency re-run — 1H HYPE volatility breakout (2026-11-05)

## Premise correction

`scripts/breakout_backtest.py` did **not** contain a 500 ms execution-delay parameter. It
filled at the 1H bar close and charged 1.5 bps maker / 4.5 bps taker fees, i.e. it was
latency-insensitive by construction. The 500 ms default lives in
`scripts/replay_backtest.py` (the market-making replay engine), which is a different model.

A latency-aware execution layer was added to `breakout_backtest.py`:

- `TOKYO_LATENCY_MS = 66`, `TURKEY_LATENCY_MS = 500`, `EXECUTION_LATENCY_MS = 66`.
- For a decision/stop trigger at `t`, the fill is shifted by
  `mid(t + latency) - mid(t)`, measured from the recorded HYPE BBO ticks.
- At `latency = 0` this reproduces the original bar-close model exactly.
- Strategy logic unchanged: 20-bar breakout, VolRatio >= 1.20, 1.5 x ATR_14 initial
  stop, 3.0 x ATR_14 Chandelier trailing stop, 2 daily stop-outs / -$1.20 daily loss cap.

CLI: `python scripts/breakout_backtest.py --compare 0,66,500`.

## Data

- 1H candles: Hyperliquid mainnet API, 600 closed candles (~25 days).
- Tick latency model: 14 full days of HYPE BBO+trade shards, 2026-10-20 .. 2026-11-02
  (`data/archive_tars`). BBO median inter-arrival ~99 ms.
- Executable window with ticks: 2026-10-20T00:00Z .. 2026-11-03T00:00Z.

## Latency comparison (tick-covered 14-day window)

| Latency | Trades | Win rate | PF | Net PnL | End equity | Max DD | Avg abs entry slip |
|---|---|---|---|---|---|---|---|
| 0 ms (bar close) | 8 | 37.5% | 1.1399 | +$0.1464 | $30.1464 | $0.8011 | 0.00 bps |
| **66 ms (Tokyo)** | 8 | 37.5% | **1.1415** | **+$0.1479** | **$30.1479** | **$0.8005** | **0.080 bps** |
| 500 ms (Turkey) | 8 | 37.5% | 1.1360 | +$0.1438 | $30.1438 | $0.8157 | 0.335 bps |

**66 ms vs 500 ms:** win rate Δ 0.0 pp, PF Δ +0.0055, net Δ **+$0.0042**, max DD Δ
**−$0.0152**. The entire latency effect is under half a cent on this sample.

Mid-price move at all 336 hourly closes: 66 ms p90 = $0.001 (0.125 bps), max $0.0275;
500 ms p90 = $0.009 (1.1 bps), max $0.0425. Median is $0.00 at both because the BBO
median update interval (~99 ms) is coarser than 66 ms.

## Strategy edge — full 25-day window (0 ms)

| Trades | Win rate | PF | Net PnL | Max DD |
|---|---|---|---|---|
| 15 | 20.0% | 0.41 | **−$1.63** | $2.17 |

Bootstrap (20,000 resamples) 90% CI of net PnL: **(−$3.30, +$0.64)**, P(net ≤ 0) = 0.90.

Parameter robustness grid (initial SL {1.0,1.5,2.0,2.5} x Chandelier {2.0,3.0,4.0,5.0},
25-day window): **0 of 16 cells positive**; active 1.5/3.0 cell is −$1.63 / PF 0.41.
The 14-day +$0.15 result is a 3-win sample with 90% CI (−$1.46, +$2.38).

## Conclusion

1. Latency is **not** a meaningful lever for this 1H strategy. Tokyo's 66 ms is nominally
   better than Turkey's 500 ms by $0.004 / 0.006 PF / $0.015 DD on 8 trades — noise, not edge.
2. The strategy itself shows **no validated edge**: negative on the full 25-day sample,
   0/16 parameter cells positive, and a bootstrap CI that includes zero.
3. **Do not switch the bot from dry-run to live on the basis of this test.**

## Caveats

- Only mid-price drift over latency is modelled; fill probability, queue position, and
  exchange trigger-engine delay are not.
- Tick coverage is 14 days; trades outside that window have no latency adjustment.
- BBO median gap (~99 ms) is close to the 66 ms latency, so sub-100 ms effects are
  under-resolved.
- 8-15 trades is far too few for statistical confidence.
