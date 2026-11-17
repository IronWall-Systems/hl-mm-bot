# Full-history breakout backtest + 24h cooldown circuit breaker (2026-11-05)

## What changed

- **Cooldown rule** — a realized trade loss, or cumulative realized UTC-day loss, worse
  than **−$0.25** arms a **24-hour** no-new-entry window. It is persisted across restarts.
- `scripts/breakout_live.py`: new state fields `cooldown_until_ms`, `day_realized_pnl`,
  `last_equity`; entries are skipped while the cooldown is active. Threshold/hours are
  configurable via `HL_LOSS_COOLDOWN_USD` / `HL_COOLDOWN_HOURS` (defaults 0.25 / 24).
- `scripts/breakout_backtest.py`: same rule in `simulate()` (CLI `--cooldown-usd`,
  `--cooldown-hours`, `--no-cooldown`), with `cooldown_triggers`, `entries_blocked` and
  a per-event log.
- `scripts/compare_strategies.py`: default window extended to all collected tick days
  (2026-10-20 .. 2026-11-04) and cooldown columns added.

## Data coverage

- **Candles:** 5,001 closed 1H HYPE candles, **2026-04-11 05:00 → 2026-11-05 14:00 UTC**
  (Hyperliquid API caps `candleSnapshot` at ~5,002 hourly candles, so this is the full
  retrievable history).
- **Ticks:** 2026-10-20 .. 2026-11-02 full days, plus 2026-11-04 hours 03–22 partial
  (9 shards). There are **no tick shards for Nov 3 or Nov 5**, so latency/funding
  adjustments in the standalone full-history run are only applied where ticks exist
  (177 entry/exit samples fell outside tick coverage).

## Full-history result: cooldown ON vs OFF (66 ms, fees included)

| Metric | Cooldown ON | Cooldown OFF | Δ |
|---|---:|---:|---:|
| Trades | 96 | 109 | −13 |
| Win rate | 32.29% | 33.94% | −1.65 pp |
| Profit factor | 1.160 | 1.241 | −0.080 |
| Net PnL | **+$2.91** | **+$5.52** | **−$2.61 (−47.3%)** |
| Max DD (realized) | $3.04 | $4.85 | −$1.81 (−37.3%) |
| Max DD (hourly MTM) | **$4.17** | **$5.34** | **−$1.17 (−21.9%)** |
| Fees | $0.94 | $1.17 | −$0.23 |
| Net / MTM-DD | 0.70 | 1.03 | −0.33 |
| Worst day | −$0.59 | −$1.16 | +$0.57 |
| Cooldown triggers | 36 | 0 | — |
| Signals blocked | 28 | 0 | — |

Cooldown trigger mix: **32 trade-loss** + **4 daily-cumulative-loss**. Validation checked
all entries against every cooldown window: **0 entries occurred while a cooldown was active**.

### Worst days

- **ON:** 20260822 −$0.59, 20260604 −$0.56, 20260617 −$0.54
- **OFF:** 20260821 −$1.16, 20260608 −$1.15, 20260930 −$0.75

The breaker roughly halves the worst day and cuts MTM drawdown by ~22%, but it also
removes ~47% of net profit and lowers the return/drawdown ratio. On this sample it is a
**risk-reduction rule, not a performance improvement**.

## Recent window (2026-10-20 → 2026-11-05, 66 ms)

| Metric | Cooldown ON | Cooldown OFF |
|---|---:|---:|
| Trades | 8 | 9 |
| Win rate | 25.0% | 33.3% |
| Profit factor | 0.83 | 0.85 |
| Net PnL | −$0.24 | −$0.20 |
| Max DD (MTM) | $1.23 | $1.23 |
| Cooldown triggers / blocked | 3 / 1 | — |

The rule has almost no effect on the recent sample; the underlying strategy is still
slightly negative over the last ~16 days.

## Extended comparison vs passive MM (all collected tick days)

| Metric | A — Passive MM | B — 1H Breakout (cooldown) |
|---|---:|---:|
| Trades / round trips | 647 | 8 |
| Fills/day | 72.2 | 1.0 |
| Win rate | 53.3% | 25.0% |
| Profit factor | 0.93 | 0.83 |
| Net PnL | −$1.00 | −$0.24 |
| Max DD | $7.98 (22.7%) | $1.23 (4.0%) |
| Worst day | −$6.54 (Oct 29) | −$0.35 (Nov 4) |

Neither model is profitable over the extended window. The breakout loses less and has
~5.6× smaller drawdown, but the MM has the higher profit factor. The Oct-29 inventory
tail remains the dominant MM risk event.

## Conclusion

- The 24h breaker works as specified and is now persisted in the live bot.
- On full history it **reduces max drawdown by 21.9% and the worst day by ~49%**, at the
  cost of **47.3% of net profit**. It does **not** improve net/DD (0.70 vs 1.03).
- Over the most recent 16 days the strategy remains slightly negative with or without
  the breaker, so this is still **not** a validated live edge.
- `hl-collector.service` was restarted on the new code and is active; account equity is
  $0.00, so no orders can be placed until funded.

## Files / logs

- `scripts/breakout_live.py`, `scripts/breakout_backtest.py`, `scripts/compare_strategies.py`
- `logs/breakout_full_history_20261105.log`
- `logs/breakout_cooldown_validation_20261105.log`
- `logs/breakout_cooldown_events_20261105.csv` (36 events)
- `logs/strategy_comparison_extended_20261105.log`
- `docs/strategy_comparison_20261105.md`
