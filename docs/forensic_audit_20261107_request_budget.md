# Forensic Audit — HYPE live session that exhausted the request budget

**Session audited:** `2026-11-07 08:55:32.860` → `11:06:37.491 UTC`
**Duration:** `7,865.14 s` = `131.086 min` = `2.185 h`
**Result:** exchange cumulative request cap exhausted, 8 consecutive rejects, kill switch.
**Status:** live trial remains **stopped/flat** (`mmbot-live-trial.service` disabled and inactive).

---

## 0. Executive summary

| Metric | Value |
|---|---|
| Metered exchange actions sent (exact, from logs) | **4,621** |
| — `batchModify` calls | **4,561** |
| — order legs modified | **6,161** |
| — `bulk_place` calls / legs | **14 / 15** |
| — `cancel_oids` calls (44 orphan + 1 shutdown) | **45** |
| — `flatten` calls | **1** |
| Read-only (`/info`) requests (estimated) | **≈ 3,620** |
| Total API requests (metered + reads) | **≈ 8,240** |
| Traded notional in session | **$117.5711** |
| Writes per $1 traded | **39.30** |
| Volume earned per write | **$0.02544** |
| Average requote rate | **34.79 batch/min · 0.580 batch/s** |
| Average leg-modify rate | **47.0 legs/min · 0.783 legs/s** |
| Average price shift per modify | **7.94 bps buy · 8.39 bps sell** |
| Exchange cap at halt | `10,421` for lifetime volume `$422.03` |
| Exchange used at halt | `10,422` → `10,426` after shutdown writes |

**Root cause:** the strategy's configured floor of **1 requote/second** (plus a toxicity fast path down to 0.1 s) produced ~2,115 exchange actions/hour. The account only earns **1 request per $1 of traded volume**, and the session traded about **$54/hour**. The session therefore needed roughly **39× more requests than its own volume could pay for**, and consumed the entire remaining lifetime headroom.

This is a **budget/design failure, not a network, exchange, or daemon failure**.

---

## 1. Method and data sources

There is **no database**. The audit is reconstructed from:

| Source | Used for |
|---|---|
| `logs/live_trial_20261107_hype.console.log` | exact per-call `modify_quotes`, `bulk_place`, `cancel_oids`, `cancel_all`, `flatten`, `book_snapshot`, orphan and reject lines, millisecond timestamps |
| `logs/live_trial_20261107_hype.jsonl` | per-leg `modify` (price/side/ts), `fill`, `equity`, `toxicity`, `markout` events |
| `logs/live_trial_20261107.console.log`, `logs/live_trial_20261107_run1_aborted.console.log` | earlier same-day sessions (budget accounting) |
| `src/mmbot/main.py`, `src/mmbot/exchange/client.py`, `src/mmbot/execution/order_manager.py` | request-generating code paths |
| Live exchange `userRateLimit` query | exact current lifetime counter: `nRequestsUsed=10426`, `nRequestsCap=10422`, `cumVlm=422.03` |

**Metering verified empirically:** two consecutive `/info` reads did **not** change `nRequestsUsed`. The cumulative counter meters **exchange/order actions only**. Reads still consume the separate per-IP info rate limit, but they did **not** cause this rejection.

### Accuracy statement

* Write counts are **exact** — every outbound exchange call is logged with a millisecond timestamp.
* `open_orders` reconcile count is **estimated** (~525 periodic + ~11 forced + shutdown/startup). It is not emitted as a log line, so it cannot be counted exactly from historical logs.
* The exchange's `10,422` is the **account lifetime** counter, not this session's count. Session writes are `4,621`; the session started with ~`5,805` lifetime requests already used, leaving ~`4,617` headroom — almost exactly what the session then consumed.

---

## 2. Total request count and breakdown

### 2.1 Metered exchange actions (exact)

| Request type | Calls | Log evidence |
|---|---:|---|
| `batchModify` (`modify_quotes`) | **4,561** | 4,561 `modify_quotes raw` lines |
| New orders (`bulk_place`) | **14** | 14 `bulk_place raw` lines (15 legs) |
| Cancels (`cancel_oids`) | **45** | 44 orphan cancels + 1 shutdown `cancel_all` |
| Market flatten | **1** | `flatten -> null` at `11:06:37` |
| **Total metered** | **4,621** | |

Batch composition (statuses returned per call):

| Legs per `batchModify` | Calls |
|---|---:|
| 2 legs | 1,597 |
| 1 leg | 2,958 |
| error responses (budget cap; 9 leg-level rejects) | 6 |
| **Total** | **4,561** |

### 2.2 Read-only `/info` requests (estimated)

| Source | Calculation | Requests |
|---|---|---:|
| Equity/account polls | 1,537 polls × 2 (`user_state` + `spot_user_state`) | **3,074** |
| Startup account read | 1 × 2 | **2** |
| `open_orders` reconcile (15 s cadence) | 7,865 s / 15 s + 1 | **≈ 525** |
| `open_orders` forced reconciles (19 modify rejects) | ~11 actual | **≈ 11** |
| Startup `cancel_all` → `open_orders` | 1 | **1** |
| Shutdown `cancel_all` → `open_orders` | 1 | **1** |
| Shutdown `cancel_sweep` → `open_orders` | 1 | **1** |
| Shutdown flat verification (`account_state`) | 1 × 2 | **2** |
| `book_snapshot` (book-silence confirm) | 1 | **1** |
| **Total reads** | | **≈ 3,620** |

The equity poll count is exact (`1,537` `equity` JSONL events); the reconcile count is estimated from the fixed 15 s cadence. WebSocket pushes (`orderUpdates`, `userFills`, `l2Book`) and heartbeat traffic are **not** API requests and are excluded.

### 2.3 Grand total

```
metered exchange actions          4,621   (exact)
read-only /info requests        ~3,620   (estimated)
--------------------------------------------------
total API requests               ~8,240
```

The rejection itself was triggered purely by the **4,621 metered actions** pushing the lifetime counter from `10,421` to `10,422`.

---

## 3. Order relocation and modification metrics

### 3.1 Frequency

| Metric | Value |
|---|---|
| `batchModify` calls | 4,561 |
| Leg modifies | 6,161 |
| Session duration | 131.086 min |
| **Batch rate** | **34.79 / min = 0.580 / s** |
| **Leg rate** | **47.00 / min = 0.783 / s** |
| Mean inter-batch time | 1.723 s |
| Median inter-batch time | 1.123 s |
| p90 inter-batch time | 3.106 s |
| Min / max inter-batch | 0.363 s / 12.807 s |

Inter-batch interval distribution:

| Interval | Count | Share |
|---|---:|---:|
| 0.25–0.50 s | 65 | 1.4% |
| 0.50–0.99 s | 636 | 13.9% |
| 0.99–1.50 s | 2,045 | 44.9% |
| 1.50–2.50 s | 1,018 | 22.3% |
| 2.50–5.00 s | 698 | 15.3% |
| 5.00–10.0 s | 95 | 2.1% |
| > 10 s | 3 | 0.1% |

The mode sits exactly at the configured **1.0 s minimum requote interval**. `802` intervals (17.6%) were below 1.0 s — these are the **toxicity fast path**.

### 3.2 Price shift per relocation

| Side | Leg modifies | Mean shift | Median | p90 | p99 | Mean time between legs |
|---|---:|---:|---:|---:|---:|---:|
| Buy | 3,018 | **7.941 bps** | 9.078 | 11.227 | 15.076 | 2.604 s |
| Sell | 3,143 | **8.385 bps** | 9.306 | 11.183 | 14.033 | 2.501 s |

* Zero-price-shift modifications: **0**.
* Every relocation was a genuine price change.
* The configured requote threshold was **2.0 bps**. Realized shifts averaged **~8 bps**, i.e. **4× the threshold**, so hysteresis almost never suppressed a cycle.

### 3.3 Trigger conditions

A relocation is produced by `OrderManager._needs_requote()` when either:

1. `|current_price − target_price| / target_price × 10⁴ ≥ requote_threshold_bps` (**2.0 bps**), or
2. remaining size `< (1 − size_tolerance) × target size`.

Given the ~8 bps average book movement per interval, condition (1) was true on nearly every cycle. The dominant trigger was therefore **ordinary book movement relative to a 2 bps threshold**, not a rare event:

| Trigger | Count/effect |
|---|---|
| Price drift ≥ 2 bps | effectively every cycle |
| Toxicity transition fast path | `4,172` toxicity transitions; `802` sub-1 s cycles |
| Orphan reconciliation cancels | 44 events |
| Book-silence REST snapshot | 1 |
| ALo crossings / cross rejects | 0 (`cross_rejects=0`) |
| Feed restarts | 0 |

The `max_actions_per_min=120` guard was never the binding constraint: observed rate peaked at ~46 batches/min (~92 leg actions/min) and averaged ~35/min.

---

## 4. Volume vs request ratio

| Metric | Value |
|---|---:|
| Fills | 10 |
| Traded notional | **$117.5711** |
| Fees | $0.017631 |
| Average fill price | $78.3807 |
| Metered actions | 4,621 |
| **Traded volume per action** | **$0.025443** |
| **Actions per $1 traded** | **39.30** |
| Session volume rate | **$53.81 / hour** |
| Session action rate | **2,115 / hour** |
| Request credits earned by session volume | ~118 |
| Net request deficit | **~4,503** |

Hyperliquid's budget is:

```
cap = 10,000 + cumulative USDC taker/maker volume
```

so each `$1` traded buys **one** request. This session issued **39.3 requests per $1** of its own volume — a structural deficit of roughly **39×**.

Lifetime view (live query at audit time):

```
cumVlm        = 422.03
nRequestsUsed = 10,426
nRequestsCap  = 10,422
nRequestsSurplus = 0
```

Lifetime requests per dollar of volume = `10,426 / 422.03` = **24.7**, against a replenishment rate of **1.0**. The account was already ~56% consumed when this HYPE session started; the session then spent essentially all remaining headroom.

> Sustaining this quote cadence would require roughly **$2,115 of taker volume per hour**; actual volume was **$54/hour**.

---

## 5. Burst analysis — what caused the highest request rates

### 5.1 Top request minutes

| Minute | `batchModify` | Toxicity transitions | Fast-path (<1 s) cycles |
|---|---:|---:|---:|
| 09:17 | 46 | 43 | 8 |
| 09:00 | 45 | 46 | 10 |
| 09:34 | 44 | 45 | 9 |
| 11:03 | 44 | 45 | 11 |
| 09:40 | 43 | 38 | 8 |
| 10:33 | 43 | 40 | 6 |
| 09:36 | 41 | 40 | 9 |
| 10:07 | 41 | 42 | 9 |
| 10:45 | 41 | 44 | 12 |
| 10:34 | 40 | 32 | 14 (max) |

Peak minute = **46 batches** = `0.77 batch/s` — just under the 1.0 s cadence ceiling of 60/min. There was **no runaway loop**; the ceiling is simply high.

### 5.2 Top 10-second windows

| Window (UTC) | Calls | Fast-path | Toxicity transitions |
|---|---:|---:|---:|
| 10:54:50 | 10 | 1 | 7 |
| 08:59:50 | 9 | 2 | 6 |
| 09:00:10 | 9 | 3 | 9 |
| 09:00:30 | 9 | 1 | 10 |
| 09:03:20 | 9 | 4 | 11 |

The single worst window (`10:54:50`, 10 calls) is **not** a fast-path burst: 9 of the 10 calls came at the plain 1.0 s cadence (`1.13, 1.03, 2.02, 0.49, 1.20, 1.03, 1.01, 1.02, 0.95 …` s). The 10th was a single toxicity fast-path cycle.

### 5.3 Cadence vs toxicity

* Correlation between per-minute batch count and toxicity transitions: **r = 0.595**.
* Fast-path cycles: **802 of 4,560** intervals = **17.6%**.
* Remaining **82.4%** were driven by the fixed 1.0 s cadence.

**Conclusion:** the **1.0 s requote cadence is the primary driver**; the toxicity fast path is a secondary amplifier (~18% of cycles). Neither is a bug — both are configured behaviour. The budget was never part of the control loop.

---

## 6. Exact code paths responsible for the request rate

| Path | File / line | Role |
|---|---|---|
| `cycle_loop()` | `src/mmbot/main.py:194-203` | wakes every 0.1 s and calls `maybe_cycle()` |
| `maybe_cycle()` | `src/mmbot/main.py:205-216` | gate: `gap < min_requote_interval_s` returns; toxicity fast path allows `gap >= 0.1` once per 3 s |
| `cycle()` | `src/mmbot/main.py:218-284` | kill check → stale check → `compute_quotes()` → execution |
| `_do_modifies()` | `src/mmbot/main.py:285-343` | one `gw.modify_quotes()` per cycle; `record_actions(1)` per leg |
| `compute_quotes()` | `src/mmbot/strategy/quoter.py:92` | micro-weighted fair value; moves target every book update |
| `toxic_sides()` | `src/mmbot/strategy/quoter.py:44` | sets `_micro_urgent` on toxicity transitions |
| `OrderManager._needs_requote()` | `src/mmbot/execution/order_manager.py:66-71` | 2.0 bps / size hysteresis |
| `gateway.modify_quotes()` | `src/mmbot/exchange/client.py:584` | **the `batchModify` exchange action** |
| `gateway.bulk_place()` | `src/mmbot/exchange/client.py:557` | new-order exchange action |
| `gateway.cancel_oids()` | `src/mmbot/exchange/client.py:639` | cancel exchange action |
| `housekeeping()` equity poll | `src/mmbot/main.py:537-599` | every 5 s → 2 `/info` reads |
| `housekeeping()` reconcile | `src/mmbot/main.py:605-628` | every 15 s → 1 `open_orders` read |
| `gateway.account_state()` | `src/mmbot/exchange/client.py:697-704` | `user_state` + `spot_user_state` |
| `gateway.open_orders()` | `src/mmbot/exchange/client.py:706-707` | `open_orders` |
| `cancel_all()` | `src/mmbot/exchange/client.py:682-688` | `open_orders` read + `cancel_oids` write |
| Config floor | `scripts/live_trial_20261107.py:38-39` | `MM_REQUOTE_THRESHOLD_BPS=2.0`, `MM_MIN_REQUOTE_INTERVAL_S=1.0` |
| Design constraint (known, not enforced) | `BUILD_SPEC.md:35` | "10,000 request buffer + 1 per $1 traded; batches count n against it" |

**No cumulative-budget accounting exists in the code.** `RiskGuard.record_actions()` tracks a rolling per-minute count (`max_actions_per_min=120`) but never tracks the exchange's lifetime `nRequestsUsed` / volume-earned cap. That is the missing control.

---

## 7. Log evidence for the halt

```
11:06:31.287  modify_quotes raw: {"status":"err","response":"Too many cumulative
              requests sent (10422 > 10421) for cumulative volume traded $422.03.
              Place taker orders to free up 1 request per USDC traded."}
11:06:31.288  modify rejected (sell @ 78.363000): Too many cumulative requests ...
...
11:06:36     8 consecutive rejects -> KILL SWITCH: consecutive_errors
11:06:37.349  cancel_all(2) -> bulk_cancel
11:06:37.452  flatten -> null
11:06:37.484  verified flat
11:06:37.491  stopped
```

* 19 total `modify rejected` events in the session.
* 10 were benign `Cannot modify canceled or filled order` (state races) spread across the session.
* 9 were budget rejections in the final 6 seconds.
* `max_consecutive_errors=8` fired exactly as designed.

---

## 8. Findings and required fixes before restart

### Findings

1. **Budget was structurally unreachable.** 1 s cadence ≈ 3,600 batch requests/hour minimum; volume earned ≈ 54 requests/hour. The strategy cannot fund its own request rate at micro-notional size.
2. **The account was already 56% consumed at session start.** Lifetime usage was ~5,805/10,422 before this session.
3. **Read requests did not cause the halt.** Only exchange actions count against `nRequestsUsed`; reads are ~3,620 of the ~8,240 total but 0 of the metered cap.
4. **No burst/loop anomaly.** Peak 10 s window was 10 calls; peak minute 46 calls. The 1.0 s floor, not a bug, set the rate.
5. **Toxicity fast path amplified rate by ~18%**, not the primary cause.
6. **No pre-trade budget check.** The bot cannot know it is at the cap until the exchange rejects.

### Required before any restart (one of)

1. **Fresh account / API wallet** with a new 10,000-request base — cleanest for micro-notional.
2. **Fund the budget with taker volume** — `$5,000` volume buys 5,000 requests, but pays fees/slippage.
3. **Near-static quoting** — raise `min_requote_interval_s` to tens of seconds / minutes and disable the toxicity fast path, so the action rate is below the volume-earned rate. This still slowly drains a fixed buffer without fresh volume.

### Recommended code changes (not yet applied)

* Query `userRateLimit` periodically and **hard-stop / degrade before** the cap (e.g. at 90% of `nRequestsCap`).
* Make `max_actions_per_min` budget-aware (derive from remaining cap and expected volume).
* Add a persistent request counter / alert, and log every `open_orders` reconcile so future audits are exact.
* Consider `batchModify` gating on a minimum absolute book move larger than 2 bps to cut cycles at micro size.

---

## 9. Current safe state

| Item | State |
|---|---|
| Position | `0.000000` HYPE (flat, verified) |
| Open orders | `0` |
| Equity | `$24.14989` |
| Hard stop | `$22.17712` |
| `mmbot-live-trial.service` | installed, **disabled, inactive** |
| `mmbot-ai-sentinel.service` | installed, **disabled, inactive** |
| `mmbot-tg-monitor.service` | active (read-only dashboard) |
| `mmbot-microstructure.service` | active, feeds healthy |
| Exchange request surplus | **`0`** (no headroom) |

The live trial must stay halted until one of the funding/strategy options above is chosen and a budget guard is in place.
