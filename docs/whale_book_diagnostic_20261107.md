# HYPE Deep-Book & Whale Diagnostic — 2026-11-07

**Generated:** 2026-11-07 07:46:04 UTC · read-only mainnet · coin `HYPE`

## 1. Multi-resolution L2 books

| Resolution | BBO | Spread | Levels | Bid span | Ask span | Median level sz | OBI top5 |
|---|---|---|---|---|---|---|---|
| `nSigFigs=default/5` | 79.057 / 79.058 | 0.126 bps | 20/20 | 4.11 bps | 3.23 bps | 45.59 | -94.51% |
| `nSigFigs=4` | 79.050 / 79.060 | 1.265 bps | 20/20 | 24.67 bps | 24.67 bps | 1516.08 | -55.79% |
| `nSigFigs=3` | 79.000 / 79.100 | 12.650 bps | 20/20 | 246.68 bps | 246.68 bps | 12553.97 | -7.32% |

### Verified anti-spoofing walls (`n >= 5`, `sz >= 3x median`)

**nSigFigs=4**
* Closest bid wall: none
* Closest ask wall: ask 79.130 × 4641.22 (n=22, score 14553, 9.49 bps, front-run 79.129)
* Largest wall score: ask 79.250 × 4628.22 (n=30, score 15893, 24.67 bps, front-run 79.249)
* Total verified bid/ask walls: 0/2

**nSigFigs=3**
* Closest bid wall: none
* Closest ask wall: ask 79.900 × 48211.18 (n=224, score 261117, 107.53 bps, front-run 79.899)
* Largest wall score: ask 79.900 × 48211.18 (n=224, score 261117, 107.53 bps, front-run 79.899)
* Total verified bid/ask walls: 0/5

## 2. Whale / vault wallets

Tracked wallets: **21** · HYPE mid `79.0595`

* Long HYPE notional: **$1018266.60**
* Short HYPE notional: **$134824497.81**
* Net bias: **$-133806231.22** (-98.50% of gross)

**Concentration note:** the two largest short wallets account for `$134,622,426` of the gross short notional (99.9%). Treat the net bias as whale-flow evidence, not a clean directional signal.

**HLP vault** (`0xdfc24b077bc1425ad1dea75bcb6f8158e10df303`): HYPE perp size `+0.000`, notional `$+0.00`, resting HYPE orders `0`. HLP is a multi-strategy market-making/liquidation vault; it may hold spot/other perp exposure outside HYPE.

| Wallet | Source | HYPE size | Notional | uPnL | Resting HYPE orders |
|---|---|---|---|---|---|
| `0xb83de012…` leaderboard | leaderboard | -962615.550 | $-76103904.08 | $-3771301.56 | ask 79.070x12.65, ask 85.811x11.65 |
| `0x5b5d5120…` leaderboard | leaderboard | -740183.300 | $-58518521.61 | $-2668821.29 | ask 79.070x0.30 |
| `0x92cbb22a…` recent large trader | recentTrades | +11377.500 | $+899499.46 | $-29904.87 | — |
| `0xae78adab…` recent large trader | recentTrades | +1485.990 | $+117481.63 | $-101.09 | — |
| `0x7cee351d…` recent large trader | recentTrades | -1404.600 | $-111046.97 | $+4656.18 | — |
| `0x65c252c4…` recent large trader | recentTrades | -634.530 | $-50165.62 | $+68.46 | — |
| `0xf5d81a13…` recent large trader | recentTrades | -516.820 | $-40859.53 | $+30.40 | bid 79.057x115.46, ask 79.090x29.25, bid 79.018x26.70 |
| `0xc029043c…` recent large trader | recentTrades | +9.200 | $+727.35 | $-0.28 | — |
| `0x72795661…` recent large trader | recentTrades | +7.060 | $+558.16 | $-0.12 | bid 79.051x4.75, ask 79.060x4.80, ask 79.086x7.45, bid 79.042x5.59 |
| `0xdfc24b07…` HLP vault | known vault | +0.000 | $+0.00 | $+0.00 | — |
| `0xa822a9ce…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0x1c498a93…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0x24de6b77…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0x393d0b87…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0xaacda0c3…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0xe6111266…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0x8d68efbf…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0x875783b1…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0x488d2a9b…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |
| `0x4eb8d907…` leaderboard | leaderboard | +0.000 | $+0.00 | $+0.00 | — |

### All tracked resting HYPE limit orders

* recent large trader · **bid 79.057 × 115.46** (notional $9127.92, reduceOnly=False)
* recent large trader · **ask 79.090 × 29.25** (notional $2313.38, reduceOnly=False)
* recent large trader · **bid 79.018 × 26.70** (notional $2109.78, reduceOnly=False)
* leaderboard · **ask 79.070 × 12.65** (notional $1000.24, reduceOnly=False)
* leaderboard · **ask 85.811 × 11.65** (notional $999.70, reduceOnly=False)
* recent large trader · **bid 79.026 × 7.80** (notional $616.40, reduceOnly=False)
* recent large trader · **ask 79.086 × 7.45** (notional $589.19, reduceOnly=False)
* recent large trader · **ask 79.068 × 5.89** (notional $465.71, reduceOnly=False)
* recent large trader · **bid 79.042 × 5.59** (notional $441.84, reduceOnly=False)
* recent large trader · **ask 79.060 × 4.80** (notional $379.49, reduceOnly=False)
* recent large trader · **bid 79.051 × 4.75** (notional $375.49, reduceOnly=False)
* leaderboard · **ask 79.070 × 0.30** (notional $23.72, reduceOnly=False)

## 3. Oracle / mark / funding

* oraclePx: `79.04900`
* markPx: `79.04200`
* midPx: `79.05950`
* **oracle–mark divergence: `-0.89 bps`**
* mark–mid divergence: `-2.21 bps`
* funding: `0.00001250`
* open interest: `22347667.80` HYPE
* 24h notional volume: `$267699960`

## 4. Binance futures lead-lag

* Symbol: **HYPEUSDT** · live samples: 20 × 1s
* 1s returns, corr by Binance lead (s): `{'0': 0.07549868632586607, '1': 0.7634085189613842, '2': -0.4596767674800079, '3': -0.08180061483560827, '4': 0.2965819608274321}`
* 5s returns, corr by Binance lead (s): `{'0': 0.6710612909796594, '1': 0.8860635112077087, '2': 0.30950601867362776, '3': 0.28518935117996264}`
* Best 1s: lag **1s**, corr `0.763`
* Best 5s: lag **1s**, corr `0.886`

Live sampling is indicative only (short window); use for directional sanity, not statistical proof.

## 5. Method & limitations

* `nSigFigs=4` aggregates HYPE into $0.01 buckets; `nSigFigs=3` into $0.10 buckets.
* Wall score = `sz * log1p(n)`; a level must have `n >= 5` independent orders and
  `sz >= 3x` the median level size across both sides to qualify, filtering single-order spoofs.
* Whale sample = HLP + top public leaderboard accounts + largest recent HYPE trade counterparties.
* `frontendOpenOrders` / `clearinghouseState` are public read-only endpoints.
* Lead-lag uses live 1s sampling and is exploratory; no execution is performed.
