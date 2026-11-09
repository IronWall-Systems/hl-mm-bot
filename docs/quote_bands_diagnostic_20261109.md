# HYPE band diagnostic + budget-aware quoting architecture (2026-11-09)

Scope: TASK 1 variance-ratio diagnosis, TASK 2 `quote_bands.py` contract,
TASK 3 `call` vs `leg` cost calibration, TASK 4 live integration and
`--dry-run-budget`.

## 1. Data used

`logs/live_trial_20261107_hype.jsonl` contains **no 1-second BBO series**. It
has quote `modify` events (bid/ask *quote* prices, not the market mid), 90
sparse `markout` mids and equity/toxicity events. Using quote prices to
measure market volatility would measure the bot's own repricing, not price
diffusion.

The collector shards do carry every BBO message. The newest HYPE days on disk
are `data/20261105` and `data/20261104`. The diagnostic therefore uses those
and reproduces the failed session's exact clock window (08:55:32-11:06:38) on
2026-11-05.

Reproduce:

```bash
python3 scripts/diagnose_hype_bands.py data
```

Saved output: `logs/hype_band_diagnostic_20261109.log`.

Method: last BBO mid per wall-clock second, forward-filled over gaps <= 5 s,
then

```
sigma_bps_sqrt_min = std(log(mid[60:]) - log(mid[:-60])) * 1e4
VR(lag)            = std(log(mid[lag:]) - log(mid[:-lag])) / (std(r1) * sqrt(lag))
```

## 2. Results

| series | 1s samples | r1 std (bps) | sigma bps/sqrt(min) | VR(5) | VR(30) | VR(60) | VR(300) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-11-05 full available day (00-17 UTC) | 61,696 | 1.247 | **11.969** | 1.1066 | 1.2042 | 1.2396 | 1.2934 |
| 2026-11-05 08:55:32-11:06:38 | 7,866 | 0.814 | **8.185** | 1.1435 | 1.2532 | 1.2980 | 1.2375 |
| 2026-11-04 full available day | 30,869 | 2.780 | **22.821** | 1.0275 | 1.0582 | 1.0597 | 1.0055 |

The same-clock window reproduces the ~8-9 bps/sqrt(min) regime quoted in the
brief (sigma 8.185 bps).

### Flicker vs diffusion

Every variance ratio in every sample is **above 1.0**, not below it:

* range across the three series: VR(5) 1.03-1.14, VR(30) 1.06-1.25,
  VR(60) 1.06-1.30, VR(300) 1.01-1.29;
* VR(lag) increasing with lag means 1-second print noise is *less* than the
  variance implied by longer-horizon drift: returns are persistent/trending,
  not bid-ask bounce.

So the answer to "how much of the 8-9 bps is microstructure flicker" is
**none that a variance ratio can detect**. There is no mean-reverting
microstructure component to discount. A tight band would not be "absorbing
flicker"; it would be fighting a trending price and would be run over.

### Band-calibration implication

A half-spread of `H` bps is held for a 1-minute horizon. The measured
1-minute sigma is 8.2 bps in the session window:

* the contract default `H=20` is ~2.4 sigma;
* the live-trial `MM_HALF_SPREAD_BPS=5.5` is ~0.67 sigma, and the main config's
  `StrategyParams.half_spread_bps=4.0` is ~0.49 sigma.

At `H=4-5.5` the band edges sit inside one sigma of a 1-minute move, so a
quote is re-classified (`MOVE_NEAR`/`MOVE_FAR`/`PULL`) on most ticks. This is
exactly the 2026-11-07 behaviour and is not fixable by tuning the deadband
alone; the request budget cannot fund it. Keep a wider `H` (default 20) until
volume-earned request credits justify tighter bands.

## 3. `quote_bands.py` (TASK 2)

`src/mmbot/execution/quote_bands.py` implements the frozen contract:

* `BandParams` defaults: `half_spread_bps=20`, `near_frac=0.35`,
  `near_max_bps=7`, `far_frac=1`, `far_min_bps=20`, `pull_frac=0.45`,
  `pull_resume_s=20` -> derived near 13, far 40, pull 9 bps.
* `dist_bps`, `target_px`, `effective_half_spread`.
* `EWMARef(tau_s=3)` with `alpha = 1 - exp(-dt/tau_s)`.
* `RequestBudget` with `update_from_api`, `record`, `allowed_per_h`,
  `may_send`, and STANDDOWN/DEGRADED/THROTTLED/NORMAL modes.
* `Act` and `QuotePlanner.decide/on_sent` with a 0.4 bps deadband at H=20.

`src/mmbot/execution/batch_planner.py` was **absent from the repo and git
history** (`find`, `git log`, stash all empty), so it was reconstructed from
the stated contract and marked as such in its docstring. It provides
`Payload`, `BatchPlanner.plan/on_sent/cost/calibrate`, the deterministic
`_selftest()` and `--sim`.

Verification:

```
$ python3 batch_planner.py
quote_bands/batch_planner self-test: PASS (36 checks)

$ python3 batch_planner.py --sim
-- sim: 30.0 min, seed=7, mode=leg --
  quote actions                : {'place': 6, 'modify': 8, 'cancel': 6, 'pull-park': 0}
  exchange calls (cost model)  : 20
  projected calls/hour         : 40.0
  budget mode                  : THROTTLED
  sustainable allowed/hour     : 53.3
  verdict                      : WITHIN budget

$ python3 batch_planner.py --sim --minutes 60 --mode call
  exchange calls (cost model)  : 41
  projected calls/hour         : 41.0
  budget mode                  : THROTTLED
  sustainable allowed/hour     : 53.2
  verdict                      : WITHIN budget
```

`RequestBudget.burn_per_h()` uses a **15-minute minimum window**. A one-minute
burst must not be extrapolated to infinity, and a single batch must not trip
DEGRADED before the first 15 minutes of observation. Sustained 1-second
quoting still trips it quickly.

## 4. Cost calibration (TASK 3)

`calibrate_costs(before_used, after_used, n_legs)` returns `call` when the
`nRequestsUsed` delta is 1, `leg` when it equals the leg count, `unknown` when
the poll has not caught up, and `mixed` otherwise.

`/info` reads (including `userRateLimit`) are free and do **not** increment
the counter; this was verified in the 2026-11-07 audit and is why the
calibration can afford a free read before/after the first live batch.

Wiring: `_do_batch_modifies` takes one free `user_rateLimit` read before the
first live `batchModify`, sends the batch, reads again, logs

```
REQUEST-BUDGET CALIBRATION: batchModify legs=N used A -> B => verdict=... mode=...
```

and sets `BatchPlanner.mode`, so the optimistic `RequestBudget.record()` cost
matches the exchange metering from then on. Historical counter evidence
(4,561 `batchModify` calls vs the observed request delta) points to one
request per bulk call, hence the default `MM_BATCH_COST_MODE=call`.

## 5. Live integration (TASK 4)

* `main.py` constructs `EWMARef`, `RequestBudget`, `BandParams` and
  `BatchPlanner`; every `BookEvent` feeds `EWMARef.update(mid, now)`.
* `_cycle_batched` passes the smoothed ref and target size to
  `OrderManager.plan_batched`, which hides in-flight sides (no stacking) and
  delegates to `BatchPlanner.plan` with `urgent_risk=toxic_sides(...)`.
* `_execute_batch_payloads` maps `modify`/`pull-park` to one `batchModify`
  (`Alo`, tick/lot rounded), `place` to `bulk_place`, `cancel` to
  `cancel_oids`; a rejected `pull-park` immediately falls back to
  `cancel_oids`; `planner.on_sent(payload)` runs after each dispatch.
* `rate_limit_loop` polls `userRateLimit` on startup and every 30 s, feeding
  `RequestBudget.update_from_api`.
* Integration is gated by `MM_BATCH_PLANNER=1` (default `0`) so the existing
  execution path and its 248 tests are untouched until the dry run validates
  consumption.
* `--dry-run-budget [--minutes N] [--cost-mode call|leg]` runs the full loop
  against the live HYPE feed with no order dispatch. Because the current
  account has zero headroom (STANDDOWN), the dry run also prints a
  **fresh-wallet projection** (`used=0`, `cap=10000`) alongside the real
  budget, otherwise the planner would emit nothing and the projection would
  be a meaningless zero.

## 6. Current safety state and the blocking result

* Live trial and AI sentinel services are stopped/disabled; monitor and
  microstructure services remain active.
* Real budget at the time of writing: `nRequestsUsed=10430`, `nRequestsCap=10422`,
  headroom 0, `cumVlm=422.03` -> STANDDOWN. **Live execution must not resume.**
* First dry-run minute on the live feed (H=20) extrapolated to **~419
  writes/hour**, but that is a startup burst (place both sides + initial
  repricing) and the 1-minute extrapolation is not the metric. The full
  15-minute run (`logs/dry_run_budget_20261109_h20.log`) finished at
  **7 calls / 15 min = 28.0 writes/hour against a 28.3/hour allowance**,
  i.e. **within** the fresh-wallet, no-volume floor.

  The important nuance is *how* it stayed within budget: it placed both
  sides, modified 10 times and cancelled 2 in the **first minute**, then the
  budget went THROTTLED (`burn_per_h` over the 15-minute window exceeded
  0.75 x allowance) and the planner refused all new quotes for the remaining
  14 minutes. So the average is compliant, but the bot is idle ~93% of the
  time. That is the correct failure mode for a no-volume wallet: 10,000
  requests buy roughly 350 hours of a 28/hour quote cadence, and every $1
  traded earns one more request, which is what must fund a tighter cadence.

  Do not enable `MM_BATCH_PLANNER=1` live until a dry run on the *actual new
  wallet* (which will have headroom) shows the projected rate at or below its
  allowance and the fill-driven `earned_per_h` is large enough to keep the
  book populated. A 0.3 s requote cadence with no volume cannot be funded at
  any band width.

