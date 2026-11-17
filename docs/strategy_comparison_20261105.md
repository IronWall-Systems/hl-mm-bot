# Strategy comparison — HYPE, 20261020..20261104

- Latency: 66 ms (Tokyo); fees: maker 1.5 bps, taker 4.5 bps; starting equity $30
- Strategy A: passive MM, half-spread 16 bps, skew 6 bps, cancel_rate 0.5, fresh engine per UTC day
- Strategy B: 20-bar breakout, VolRatio >= 1.20, 1.5×ATR stop, 3.0×ATR Chandelier, flattened at window end
- Funding accrued hourly from collector records; MM net includes inventory MTM at day boundaries

## Side-by-side

```
Metric                                Strategy A — Passive MM       Strategy B — 1H Breakout
--------------------------------------------------------------------------------------------
Total trades / round trips                                647                              8
Exchange fills (entries+exits)                           1155                             16
Trades per day                                           40.4                            0.5
Fills per day                                            72.2                            1.0
Orders placed per day                                   28794                            0.5
Cooldown triggers (24h)                        n/a (baseline)                              3
Signals blocked by cooldown                    n/a (baseline)                              1
Win rate                                                53.3%                          25.0%
Profit factor                                            0.93                           0.83
Gross PnL                                              $+1.44                         $-0.17
Fees paid                                              $-2.42                         $-0.07
Funding PnL                                            $-0.02                         $+0.00
Net PnL (after fees+funding)                           $-1.00                         $-0.24
Return on $30                                           -3.3%                          -0.8%
Max drawdown                                    $7.98 (22.7%)                   $1.23 (4.0%)
Best day                                               $+2.66                         $+1.06
Worst day                                              $-6.54                         $-0.35
```

## Daily PnL

```
UTC day          MM $   Breakout $     MM cum $     BO cum $
------------------------------------------------------------
20261020        +0.58        -0.10        +0.58        -0.10
20261021        +0.86        +0.00        +1.44        -0.10
20261022        -0.05        +0.00        +1.39        -0.10
20261023        +0.61        -0.25        +2.00        -0.35
20261024        +0.24        +0.00        +2.24        -0.35
20261025        -0.22        +0.00        +2.02        -0.35
20261026        +2.66        -0.27        +4.68        -0.63
20261027        +0.28        +0.00        +4.96        -0.63
20261028        -0.56        -0.21        +4.40        -0.84
20261029        -6.54        +1.06        -2.14        +0.22
20261030        +2.32        +0.00        +0.18        +0.22
20261031        +0.07        +0.00        +0.25        +0.22
20261101        +0.57        +0.00        +0.82        +0.22
20261102        -1.65        -0.21        -0.83        +0.01
20261103        +0.00        +0.10        -0.83        +0.11
20261104        -0.17        -0.35        -1.00        -0.24
```

## Worst days

- **A (MM)**: 20261029 $-6.54, 20261102 $-1.65, 20261028 $-0.56
- **B (Breakout)**: 20261104 $-0.35, 20261026 $-0.27, 20261023 $-0.25

## Conclusion

Strategy B (1H breakout) outperforms Strategy A (passive MM) on this window: net $-0.24 vs $-1.00.
Risk: MM max DD $7.98 (22.7%), breakout max DD $1.23 (4.0%); PF 0.93 vs 0.83.
The MM sample has 647 round trips and a large Oct-29 inventory tail; the breakout has only 8 trades, so neither result is statistically powered. Both models are net negative on this window.

## Caveats

- One coin (HYPE), 14 days; not a statistically powered comparison.
- MM engine is re-instantiated daily (PREREG protocol), so inventory is marked to market at each UTC day boundary rather than carried.
- Breakout takes one position at a time and is flattened at window end.
- Maker fill assumptions: MM uses the replay queue model; breakout entry uses a maker fee but no queue model.
