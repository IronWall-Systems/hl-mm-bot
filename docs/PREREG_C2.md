# PREREG_C2 — Cycle 2 Offline Screen (HYPE:16 market making)

Status: DRAFT until the human freezes it (commit hash recorded in section 0). After freezing, nothing in sections 2–5 may change. Every deviation goes into `.agent/C2_DEVIATIONS.md` with a timestamp and reason.

## 0. Purpose, status, rules

- **Purpose:** screen six configurations on the Oct 20–Nov 2, 2026 collector shards to find out whether any avoids the Oct 29 / Nov 2 tail losses while keeping quiet-state edge.
- **Status of the data:** the guards were designed after seeing Oct 29 and Nov 2. A pass on this window is a *screen*, not validation. It earns a forward paper test on Nov 3+ data under a new prereg. It never authorizes live deployment.
- **Rules for the agent:**
  1. No parameter may be changed after seeing results. Values in this document are final. Do not grid-search.
  2. If you find a bug, fix it, log it, and re-run every config affected by that bug. Do not change parameters while doing so.
  3. Report all configs including failures, and state the total number of configs run.
  4. If anything here is ambiguous or contradicts the code, stop and ask the human. Do not choose silently.
  5. All features use trailing data only. Section 6 specifies a causality test that must pass before any run.
- **Frozen at commit:** `<fill in>`  **Frozen by:** `<human>`  **Date:** `<fill in>`

## 1. Setup and open items to resolve before freezing

**Data and engine.** Use `ReplayEngine` from `scripts/replay_backtest.py` and the v2 shards, coin HYPE, days 20261020–20261102. Cells: latency {500, 1000} ms × cancel_rate {0.0, 0.5} (the `cancel_rate` column in `t16_eval.csv`; read the engine and document what it does). Fresh engine per UTC day, as in `t16_evaluate.py`, except in sensitivity S4. Net PnL is after maker fees and funding, with inventory marked to market (as in the T16 evaluator).

**Items the agent must report back on before the run (do not guess):**

1. **Warm-up data.** The 24h baseline for `sigma_ref` needs shards before 20261020 00:00Z. Check whether they exist. If yes, load the previous 24h as warm-up (no trading). If no, stop and report to the human, because the choice (drop Oct 20 for all configs or shorten the baseline window) changes the 14-day rules.
2. **Current fill rule.** Document exactly how the engine decides a fill (touch, trade-through, queue position) and how skew works (formula, coefficient). Baseline skew is used unchanged in every config.
3. **Feed resolution.** Report the median and p99 interval between HYPE book records. If the median exceeds 100 ms, the 100 ms markout horizon is stale-mid and must be labeled so.
4. **Constants to verify against current Hyperliquid docs before freezing:** `TAKER_FEE_BPS` (assumed 4.5, used only in S4), order-action rate limits (S5).
5. **`LLM_MODEL_ID`** (OpenRouter slug for the chosen Nemotron model), and which provider serves it. The human fills this in and verifies the model responds. If it is unavailable later, stop. Do not substitute another model.

## 2. Prerequisite: per-fill markout analysis (`scripts/c2_markout.py`)

**Gate G0:** if this analysis fails the criteria below, the cycle ends. No configs are run.

### 2.1 Inputs
Fill logs from the baseline config in all four cells. Extend the engine (if needed) to log each fill: `ts_ms, side(+1 buy/−1 sell), px, qty, notional, inv_before, inv_after, cell, day`. Forward mids come from the shard feed, not from the simulation.

### 2.2 Markout definitions (bps)
- `mid_f(t)` = last book-record mid at or before `t`. Mid = (best_bid + best_ask)/2.
- `M_mid(h) = side × (mid_f(t+h) − mid_f(t)) / mid_f(t) × 1e4` (adverse move only; negative = toxic).
- `M_fill(h) = side × (mid_f(t+h) − px) / px × 1e4` (includes spread capture).
- Horizons h ∈ {100 ms, 1 s, 5 s}. At 500 ms+ simulated latency the 100 ms horizon is below the latency, so label it descriptive only.
- Dollar markout = `M_fill × notional / 1e4`.

### 2.3 Features (all trailing, evaluated at the fill timestamp using only data strictly before it)
| Feature | Definition |
|---|---|
| **VolRatio** | `sigma_60s / sigma_ref`. `sigma_60s` = sqrt of EWMA (half-life 60 s) of squared log mid returns on 1-second grid. `sigma_ref` = median over the trailing 24h of `sigma_60s` sampled each minute. |
| **S_aligned** | `−side × S`, where `S = (buy_vol − sell_vol)/(buy_vol + sell_vol)` over the trailing 30 s of aggressor-signed trades. Positive = flow running against the position the fill just created. |
| **OBI_aligned** | `−side × OBI`, `OBI = (bid_qty − ask_qty)/(bid_qty + ask_qty)` over the top 5 levels just before the fill. Positive = adverse. |
| **FillRateZ** | `(n60 − mu) / max(sd, 1.0)`. `n60` = own fills in the trailing 60 s. `mu`, `sd` = mean/sd of per-minute own-fill counts over the trailing 24h. |

### 2.4 Fixed buckets (no data-driven cut points)
- VolRatio: [0,1), [1,2), [2,4), [4,∞)
- FillRateZ: (−∞,1), [1,3), [3,∞)
- S_aligned and OBI_aligned: ≤ −0.3, (−0.3, 0.3), ≥ 0.3
- Also: 2×2 of (VolRatio <2 vs ≥2) × (FillRateZ <3 vs ≥3).
- **Inventory role split:** fills that reduce |inventory| vs fills that add to it. This tests the churn hypothesis directly: if reducing fills carry the loss, the skew/exit logic is implicated, not just entry toxicity.

### 2.5 Outputs (CSV + short markdown report)
Per bucket and horizon: fill count, share of fills, mean `M_mid` and `M_fill` (per-fill and notional-weighted), share of total negative dollar markout, and a 90% CI from a 6-hour-block bootstrap (5000 draws, seed 20261103). Report three samples: all 14 days; Oct 29 only; all days excluding Oct 29 and Nov 2. Report all four cells; primary cell is 500 ms / cr 0.5.

### 2.6 Gate G0 (pass if at least one feature satisfies all of a–c)
"Toxic bucket" is fixed in advance: VolRatio ≥ 2; FillRateZ ≥ 3; S_aligned ≥ 0.3; OBI_aligned ≥ 0.3.
- (a) At the 1 s horizon in the primary cell, mean `M_mid` in the toxic bucket is at least 2 bps lower than in the rest, and the block-bootstrap 90% CI of the difference excludes zero.
- (b) Same condition holds in the sample excluding Oct 29 and Nov 2, with at least 100 toxic-bucket fills. If fewer than 100, mark "insufficient", which counts as a fail for (b).
- (c) The toxic bucket contains no more than 25% of fills on the non-tail days.

If G0 fails: write the report, state "cycle ends; no configs run", stop.

## 3. Configuration specifications

All configs share the baseline's quoting code, skew, size, and refresh cadence unless stated. Notation: `inv` = signed inventory in USD notional. **Reduce-only** means quote only the side that reduces |inv|. When |inv| < $1 (dust) reduce-only quotes nothing. The replay never taker-flattens (S4 prices an exit separately).

### Config 0 — BASE
Fixed half-spread 16 bps, existing linear skew, no extra inventory cap. Unchanged T16 candidate C3.

### Configs 1–2 — A1, A2 (vol-scaled spread)
- `half_spread_t = clip(16 × VolRatio_t^β, 12, 48)` bps, with `VolRatio_t` as in 2.3 refreshed every 1 s.
- `size_t = base_size × clip(1/VolRatio_t, 0.25, 1.0)`.
- Reprice only if the target half-spread moves by ≥ 1 bps; count quote updates per day.
- A1: β = 0.5. A2: β = 1.0. No other values.

### Config 3 — B (local state machine plus inventory cap)
Features: VolRatio, FillRateZ (2.3), and `M5` = mean 1 s `M_mid` of own fills over the trailing 5 minutes (defined only with ≥ 5 fills in the window; otherwise undefined and never triggers).

States and transitions:

| From → To | Condition |
|---|---|
| NORMAL → REDUCE_ONLY | `M5 < −8 bps`, or `FillRateZ > 3` |
| REDUCE_ONLY → NORMAL | all of: (`M5 ≥ −4` or undefined), `FillRateZ < 1.5`, `VolRatio < 2`, held continuously for 15 min, and min 10 min dwell in REDUCE_ONLY |
| NORMAL or REDUCE_ONLY → PULL | `VolRatio > 4` for ≥ 10 consecutive seconds, or intraday PnL (realized + MTM since 00:00Z) ≤ −$1.60 |
| PULL → REDUCE_ONLY | after a vol-trigger PULL: `VolRatio < 2` for 30 min and min 30 min dwell. After a loss-trigger PULL: next UTC day only. |

- NORMAL quotes at A-style fixed 16 bps (no vol scaling, so B is not confounded with A).
- PULL cancels all quotes and holds inventory.
- **Inventory cap:** in every state, if |inv| ≥ $15, stop quoting the side that adds inventory.
- Thresholds (8 bps = half the base half-spread, $1.60 = 2× average winning day) are round numbers fixed in advance.
- Log state occupancy (% time per state), transitions per day (flicker), and which trigger fired.

### Configs 4–5 — C-LLM and C-CTL (slow overlay on BASE)
Both run on BASE quoting (not on A) so the overlay is the only change. The overlay outputs `{mult ∈ {1.0, 1.5, 2.0}, reduce_only ∈ {true, false}}`, applied as `half_spread = 16 × mult`. Decisions are made hourly at HH:00Z using only data up to that time and take effect 30 s later (fixed delay modeling the call). Each decision holds until the next.

**C-LLM**
- Model: `LLM_MODEL_ID` via OpenRouter, provider pinned with fallbacks disabled, temperature 0, fixed seed if supported, `max_tokens` 100.
- Prompt: fixed template stored in `configs/c2_llm_prompt.txt` (hashed into this prereg). Inputs are numbers only, no date, no coin name, no price level: trailing-1h and 6h realized vol ratio, trailing-1h signed flow, own fill count and own markout, trailing 1h/6h PnL, current |inv| as % of equity, trade volume vs 24h median. Round all inputs to two significant figures.
- Output must be valid JSON matching the schema. Otherwise retry up to 2 times; the first valid response is cached.
- **Fallback:** no valid response → `{mult: 2.0, reduce_only: true}`, cached with a `fallback` flag.
- **Caching:** key = sha256 of the rendered prompt + model ID. The evaluator reads the cache only and has no network access. A missing key is a hard error. Cache all raw responses with model ID, provider, latency, and timestamp. Generate the cache once (about 336 calls) before any replay.

**C-CTL** (control, rule-based at the same cadence)
- `vol6` = trailing-6h realized vol, compared with its own distribution over all earlier hourly samples (up to 7 days). Needs ≥ 48 samples; otherwise `{1.0, false}`.
- Below the 80th percentile → `{1.0, false}`. 80th–95th → `{2.0, false}`. Above the 95th → `{2.0, true}`.

C-LLM earns attention only if it beats C-CTL (section 4).

### Optional diagnostic D1 (not a screening config)
B's inventory cap alone (BASE + $15 cap, no state machine), to separate cap effect from state-machine effect. If run, it is reported and counted in the tally (7 runs), and it is ineligible for the screening pass.

## 4. Evaluation protocol and screening criteria

**Metrics per config × cell:** 14-day net; daily net; 6-hour block net; fees; funding; fills; worst day; positive days; peak |inv| as a multiple of $30 equity; state occupancy and transitions (B, C); quote updates per day; block-bootstrap 90% CI of total (reported, not required).

**Baseline comparisons are per cell.** Let `T0` = BASE's combined Oct 29 + Nov 2 net in that cell, `Q0` = BASE's net over the other 12 days in that cell.

### Screening pass: all must hold in all four cells (Section 5 sensitivities also apply)
- **P1:** 14-day net > 0.
- **P2:** tail loss reduced by more than 50%: `(Tv − T0) / |T0| > 0.5`, where `Tv` is the variant's Oct 29 + Nov 2 net.
- **P3:** quiet-state retention: variant's 12-day net ≥ 60% of `Q0` when `Q0 > 0`; if `Q0 ≤ 0`, variant's 12-day net ≥ `Q0`.
- **P4:** worst day ≥ −2 × the variant's own average winning day (requires ≥ 3 winning days, else fail).
- **P5 (C-LLM only):** also beats C-CTL on 14-day net in at least 3 of 4 cells. Otherwise the LLM is dropped and C-CTL is judged alone.

The original PREREG rules 2 (block-bootstrap CI lower > 0) and 3 (≥ 9/14 positive days) are not screen requirements, because 14 days cannot support them for a tail-driven strategy. They remain required for the forward test.

## 5. Sensitivities (run for every config that passes P1–P4 in the base run)

- **S1 pessimistic fills:** fill only when a trade prints strictly through the quote price by ≥ 1 tick. If the engine already uses trade-through, instead add a queue assumption: our order is at the back of the displayed size at placement and fills only after that size trades.
- **S2 fee stress:** maker fee ×2.
- **S3 rate limits:** count order actions per day; compare with the documented limit; flag a config **infeasible** if exceeded.
- **S4 carry:** one continuous engine across all 14 days with no midnight reset. Mark final inventory at the exit price including `TAKER_FEE_BPS` (verify) and report it separately.
- **Pass condition for sensitivities:** the config must still satisfy P1 and P2 in at least 3 of 4 cells under S1 and under S2, and S3 must not flag infeasible.

## 6. Implementation requirements

- **Causality test (before any run):** for 20 random timestamps, truncate the shard at `t`, recompute every feature, and assert it matches the full-data value at `t`. Any mismatch is a look-ahead bug.
- Evaluator output extends `t16_eval.csv` columns with `config, state_occupancy_json, quote_updates, trigger_counts_json`. Results append and are resumable as in `t16_evaluate.py`.
- Files: `scripts/c2_markout.py`, `scripts/c2_features.py`, `scripts/c2_evaluate.py`, `configs/c2_*.json`, `configs/c2_llm_prompt.txt`, `data/c2_llm_cache.jsonl`.
- Run order: causality test → G0 → LLM cache generation → configs → sensitivities → report.

## 7. Reporting and stop rules

- One summary table of all configs × cells, plus the pass/fail on P1–P5 and S1–S4, with failures shown.
- State the number of configurations and sensitivities run.
- **No config passes:** the cycle ends and the strategy is closed.
- **A config passes:** it earns a forward paper test from Nov 3 UTC under a new prereg (at least 6 weeks, a tail-day rule, pre-written stop conditions). A pass is not a deployment decision.
