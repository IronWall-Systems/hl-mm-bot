# Testnet log (fill during testnet)

## T6 — payload capture (2026-10-04, DONE-0019)
Full payload evidence: `docs/observed_payloads.md` §1–§8 (all six BUILD_SPEC §3 cases).
Segments ended `FLAT_CHECK result=FLAT` each time.

## T7 — error-case drills (2026-10-04 23:14–23:50Z, DONE-0020)
Every run: `HL_ENV=testnet`, `HL_DRY_RUN=0`, `MM_REQUOTE_THRESHOLD_BPS=3.0` (env only, no
code/default edits), address redacted (`0x92ee...f3ba`), ended
`FLAT_CHECK open_orders=0 position=0.000000 result=FLAT` (D3 exception below, cleaned).
Analyze first (human request): T6 seg1 equity 999.00 -> 998.972261 flat (exact
reconciliation: -fees 0.019959 -closedPnl 0.007780), 9 fills, markouts in DONE-0020.

| # | Drill (BUILD_SPEC §4) | How provoked | Expected | Observed | Result | Covering test |
|---|---|---|---|---|---|---|
| 1 | Crossed book / Alo reject | `MM_HALF_SPREAD_BPS=0.1` (+requote 3.0), 5 min live bot | cross_rejects at INFO, side backs off, no crash, no error count | cross_rejects=19; 19x INFO `ALO crossed (...), backing off: Post only order would have immediately matched...`; other_rejects=0, place_errors=0, 0 Tracebacks, bot continued (7 fills), FLAT (`logs/t7_d1_crossed.log`) | **PASS** | `test_crossed_book_rejection_does_not_crash_and_backs_off` (test_bot_scenarios) |
| 2 | Reject other | `MM_QUOTE_NOTIONAL=9 MM_MIN_ORDER_NOTIONAL=5` (bot allows, exchange min $10) | WARNING, record_error, side blocked 5 s, bot continues; 8 consecutive -> kill | 74x WARNING `order rejected (...): Order must have minimum value of $10. asset=3`; other_rejects=74, place_errors=0; ~6 s spacing = 5 s block; fills=0; 0 Tracebacks; FLAT. **Kill-at-8 not reached live**: account poll succeeds every 5 s and calls `record_success` (main.py:217) resetting the counter before 8 can accumulate (side block 5 s caps rate at ~2/5 s) — logic covered by tests (`logs/t7_d2_reject_other.log`) | **PASS** (8-consecutive kill clause test-only, see note) | `test_forced_reject_other_and_error_paths` + `test_kill_switches` |
| 3 | Stale feed | default `stale_book_s=3.0` on the quiet testnet book (no firewall) | book age>3 s: quotes pulled; >6 s: restart_feeds; quoting resumes | stale_pulls 0→72 in 4 min (metrics 18/34/53/min); each pull cancels all then re-places on next book (places=89 cancels=88); quoting resumed; 0 Tracebacks; FLAT. `book feed stale` 6 s restart not observed this run (test-covered; natural WS `Expired` restarts seen T5/T6). Network-drop provoke **skipped per human -> NEEDS HUMAN** (firewall change) (`logs/t7_d3_stale.log`) | **PASS** (pull+resume live; 6 s restart test-only) | `test_stale_feed_pulls_quotes_and_restarts_feeds` + `test_stale_and_reconcile` |
| 4 | Kill switch | `RISK_MAX_DRAWDOWN_PCT=0.0001` | cancel all -> flatten -> "verified flat" -> exit 2 | 23:41:39 CRITICAL `KILL SWITCH: drawdown_from_peak \| equity=999.70 peak=1000.44 lat_ema=944ms pos=0.000000` (dd 0.074% ≥ 0.01%) -> `verified flat` 23:41:41 -> **BOT_EXIT=2**; FLAT; 0 Tracebacks (`logs/t7_d4_kill.log`) | **PASS** | `test_drawdown_kill_cancels_flattens_and_stops` + `test_kill_switches` |
| 5 | SIGTERM | `kill -TERM <python pid>` at +120 s; `RISK_MAX_LATENCY_MS=999999` suppressed the latency kill so the TERM path is observable (kills shown in #4 + bonus) | cancel all (+flatten, `HL_FLATTEN_ON_EXIT=1`), exit 0 | SIGTERM 23:50:09 -> `verified flat` 23:50:11 -> stopped -> **BOT_EXIT=0**; FLAT; 0 Tracebacks (`logs/t7_d5b_sigterm.log`) | **PASS** | `test_sigterm_wiring_and_graceful_stop_zero` (**added this task**, TR-0027) |
| — | Network drop | skipped per human: needs firewall change | — | — | **NEEDS HUMAN** | — |

### Bonus observations (T7)
- Extra kill reason live: `KILL SWITCH: latency_spike lat_ema=1968ms` (23:46:04, D5) ->
  verified flat -> exit 2 (`logs/t7_d5_sigterm.log`).
- **Shutdown race (D3, DEFECT-class):** timeout SIGTERM landed mid-`place`; the in-flight
  place landed on exchange (oid 61856281944) with **no `place raw` log** (task cancelled
  after HTTP sent); the single `cancel_all` pass missed it (open_orders REST returned 1 of
  2). External FLAT_CHECK caught `open_orders=1`; manually cancelled -> `success`;
  pos stayed 0. Proposed fix (NOT applied, no code changes this turn): second
  open-orders sweep after flatten + log cancelled-in-flight places.
- `flatten -> null` WARNING when there is no position to close (D3/D5/D5): benign;
  `verified flat` still confirmed.
- **Actions/hour** (places+cancels per hour from `stopped. metrics`):
  seg1 (requote 1.5) 2696/h | D1 2628/h | D3 2672/h | D5 2709/h | D5b 2746/h | D4 2769/h.
  T7 avg (requote 3.0) **~2670/h** — threshold 1.5→3.0 changed nothing: stale-pull churn
  (age>3 s cancel-all + re-place, ~20-25 pulls/min here) dominates. At ~2670/h the 10k
  address buffer lasts **~3.7 h** (implications for T8 soak / A-09).

## T7b — defect fixes + 15 min live segment (2026-10-05 00:25–00:41Z, DONE-0021)
Offline TDD: 13 new tests written first (all RED, TR-0029) -> fixes -> **51 passed**
(TR-0030/0031), compileall 0. Covers: stale-feed REST confirm, windowed reject counters,
shutdown/kill/panic cancel rounds. Live run: `HL_DRY_RUN=0`, `MM_REQUOTE_THRESHOLD_BPS=3.0`
(carried over from T7), `timeout 900`, address redacted, FLAT after.

| Metric | T7 (before) | T7b (after, 15.07 min) |
|---|---|---|
| actions/hour | ~2670 | **670** (87 places + 81 cancels = 168) |
| stale_pulls | ~20–25/min | **39 total (2.6/min)**: 107 quiet confirms kept quotes, 39 differ confirms pulled |
| cross_rejects | 19 (spike drill) / else 0 | **2** |
| REST l2 confirms | 0 | 146 calls (~580/h; budget A-15 UNVERIFIED) |
| other_rejects / place_errors / risk_rejects | 74 in D2 / 0 / 0 | **0 / 0 / 0** |
| feed_restarts | housekeeping-driven | 27 (26 × `stale confirmed by REST l2`, 1 × `book feed stale`) |
| Tracebacks / kills | 0 / 2 (drill) | **0 / 0** |
| fills / pos / result | — | 1 (sell 0.00017 @86474 → exit flatten), **verified flat → BOT_EXIT=124 → FLAT_CHECK FLAT** |

Observations: `book silent Xs but REST l2 matches last book: keeping quotes` (quiet path,
107×); `stale book confirmed by REST l2 (...)`: pull + feed restart (39×); sweep round 1
found 0 open at shutdown (no `orders remained` logs); lat_ema peak 1399 ms (below 1500);
rss_peak 82 MB; `stale_pruned=2` pending-order prunes after restarts, 0 orphans.
N=6/60s side-pause and M=20/5min kill justification + A-15: see DONE-0021 / ASSUMPTIONS.

## T7c — investigation: restarts, userRateLimit, feed cadence (2026-10-05, DONE-0022)
Read-only/instrumentation only (log fields added: `ws_msgs` counter, `book_silence` +
`ws_msgs_since_prev_restart` in `feed restart`, `silence` + `d_bid/d_ask` ticks in stale
confirm). No strategy/guard/threshold change; T8 not started.

### 1) Why 27 feed restarts in 15 min (T7b) — reproduced with full fields (T7c run 09:20–09:35Z)
T7b restarts decompose as **26 × `stale confirmed by REST l2` + 1 × `book feed stale`**.
Each stale-confirmed restart = one book-silence episode whose REST top-of-book **differed**
from the WS book (T7b spec: differ -> pull + restart), rate-capped by the 10 s restart
guard. Root cause = **feed cadence, not a loop**: the l2Book WS pushes books every
**~5.3 s** (part 3 observer), so `stale_book_s=3.0` is exceeded in almost every inter-book
gap (T7c: 160 confirms ≈ 0.9 per book event); only price-different gaps restart
(T7b 39/146 = 27% of confirms differed -> 26 restarts; calmer T7c 11/160 = 7% -> 10 restarts).

T7c per-restart log (all 10, fields as requested):

| # | time (UTC) | reason | book_silence at restart | WS msgs since prev restart |
|---|---|---|---|---|
| 1 | 09:21:30 | stale confirmed by REST l2 | 5.8 s | 20 |
| 2 | 09:21:56 | stale confirmed by REST l2 | 5.6 s | 10 |
| 3 | 09:22:22 | stale confirmed by REST l2 | 5.6 s | 15 |
| 4 | 09:24:24 | stale confirmed by REST l2 | 5.3 s | 29 |
| 5 | 09:30:14 | stale confirmed by REST l2 | 5.9 s | 72 |
| 6 | 09:31:27 | stale confirmed by REST l2 | 5.5 s | 23 |
| 7 | 09:32:52 | stale confirmed by REST l2 | 5.3 s | 21 |
| 8 | 09:33:08 | stale confirmed by REST l2 | 5.8 s | 8 |
| 9 | 09:33:39 | stale confirmed by REST l2 | 5.6 s | 12 |
| 10 | 09:34:32 | stale confirmed by REST l2 | 5.9 s | 16 |

REST-vs-WS top-of-book diffs (BTC tick = 1.0): T7c confirms (n=11) d_bid
[-15,-9,-3,-2,-1,-1,+1,+4,+9,+9,+29], d_ask [-15,-4,-3,-2,-1,0,0,+1,+3,+9,+29] ticks.
T7b confirms (n=39, old log format): d_bid mean 1.67 / median 0 / p90 9 / range -16..+38,
d_ask mean 0.64 / median 0 / p90 12 / range -22..+44 ticks.

Silence-duration distributions (time since last WS book at confirm):

| sample | n | mean | median | p90 | max | histogram |
|---|---|---|---|---|---|---|
| T7c quiet confirms | 149 | 3.16 s | 3.2 s | 3.3 s | 3.6 s | 148 in [3.0,3.5), 1 in [3.5,4) |
| T7c differ confirms | 11 | 3.21 s | 3.3 s | 3.3 s | 3.5 s | 3.0-3.5 |
| T7b quiet confirms | 107 | 3.22 s | 3.3 s | 3.3 s | 7.7 s | all >3; 1 >6 |

(Confirms fire immediately at the 3.0 s threshold; restart-time `book_silence` reads
5.3-5.9 s because the confirm cycle's REST+cancel round trips take ~2 s at ~0.5 s RTT.)

### 2) userRateLimit (testnet, read-only, address redacted)
`{"type":"userRateLimit","user":<addr>}` works:
`before {"cumVlm":"235.23","nRequestsUsed":1606,"nRequestsCap":10235,"nRequestsSurplus":0}`
`after  {"cumVlm":"235.23","nRequestsUsed":1658,"nRequestsCap":10235,"nRequestsSurplus":0}`
- **Cap = 10000 + cumVlm** (10235 = 10000 + 235.23) — supports A-09's "10k + 1/$ traded" shape.
- Delta **52 over 16.27 min ≈ 192/h**; controlled probe (`scripts/t7c_rate_probe.py`):
  20× l2Book /info = **+0**, 20× user_state = **+0**, 20× open_orders = **+0**,
  20× spot_user_state = **+0**, userRateLimit probes = **+0**.
- Run accounting closes **exactly**: 26 places + 24 cancels + 1 bulk_cancel + 1 flatten =
  **52 = counter delta**. => nRequestsUsed counts **signed /exchange requests 1:1**
  (bulk counted as **1**, not n — one live observation against A-09's "batches count n").
- Bot address-budget consumption: **T7c ≈ 192/h** (50 actions/15.07 min ≈ 199/h during run),
  T7b ≈ 677/h, T7 seg1 ≈ 2696/h. At 677/h the 10235 cap lasts ≈15.1 h.
- REST l2Book: 160 calls in T7c (149 quiet + 11 differ) — **0 counted against the address
  budget**; separate per-IP /info quota not observed (unverified). => **A-15 VERIFIED
  (scoped)** — see ASSUMPTIONS row.
- T7c live segment itself: 09:20:11–09:35:00Z, startup 998.94, `verified flat` ->
  BOT_EXIT=124 -> FLAT_CHECK FLAT; book=178 places=26 cancels=24 stale_pulls=11
  feed_restarts=10, 0 rejects/kills/Tracebacks (`logs/t7c_live.log`).

### 3) l2Book WS silence: testnet vs mainnet (10 min each, read-only, no keys)
| | testnet (09:20-09:30Z) | mainnet (09:37-09:47Z) |
|---|---|---|
| messages / rate | 114 / 11.33 per min | 112 / 11.16 per min |
| gap mean / median | 5.29 s / 5.29 s | 5.38 s / 5.40 s |
| gap p90 / p99 / max | 5.47 / 5.63 / 5.78 s | 5.52 / 5.74 / 5.76 s |
| gaps >3 s (stale threshold) | **113 / 113** | **111 / 111** |
| gaps >6 s (2× threshold) | 0 | 0 |
| buckets | 3-5 s:1, 5-10 s:112 | 3-5 s:2, 5-10 s:109 |

Both networks push l2Book on the same **~5.3-5.4 s cadence** (heartbeat-like); mainnet
mean is 0.09 s slower — no meaningful difference. With `stale_book_s=3.0` below the
cadence, REST confirms fire in essentially every inter-book gap on EITHER network.
(`scripts/t7c_feed_observer.py`, `logs/t7c_observer_{testnet,mainnet}.log`.)

## T7d — WS feed comparison (read-only, raw subscriptions, no orders, no code changes)

`scripts/t7d_feed_compare.py`, evidence `logs/t7d_testnet.json` + `logs/t7d_mainnet.json`.
Two raw WS connections per network, 10 min each, BTC: **connA** = default l2Book + bbo +
allMids, **connB** = l2Book `{"fast":true}`. Exact subscription JSON sent (recorded verbatim
by the script):

    connA: {"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC"}}
           {"method":"subscribe","subscription":{"type":"bbo","coin":"BTC"}}
           {"method":"subscribe","subscription":{"type":"allMids"}}
    connB: {"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC","fast":true}}

Raw server replies (no feed rejected; every subscription accepted on both networks):

    {"channel":"subscriptionResponse","data":{"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC","nSigFigs":null,"mantissa":null,"fast":false}}}
    {"channel":"subscriptionResponse","data":{"method":"subscribe","subscription":{"type":"l2Book","coin":"BTC","nSigFigs":null,"mantissa":null,"fast":true}}}
    {"channel":"subscriptionResponse","data":{"method":"subscribe","subscription":{"type":"bbo","coin":"BTC"}}}
    {"channel":"subscriptionResponse","data":{"method":"subscribe","subscription":{"type":"allMids"}}}

### Gap / rate / levels / feed-age (10 min windows, BTC)

| feed | net | msgs | msgs/min | gap mean / med / p90 / p99 / max (s) | levels per msg | age mean / med / p90 / max (s) |
|---|---|---|---|---|---|---|
| l2Book default | testnet | 115 | 11.43 | 5.251 / 5.284 / 5.475 / 5.628 / 5.737 | 20 bids + 20 asks = 40 | 0.503 / 0.490 / 0.557 / 0.908 |
| l2Book default | mainnet | 112 | 11.20 | 5.358 / 5.400 / 5.565 / 5.684 / 5.751 | 20 + 20 = 40 | 0.453 / 0.431 / 0.548 / 0.781 |
| l2Book fast:true | testnet | 1132 | 113.28 | 0.530 / 0.521 / 0.611 / 0.756 / 1.233 | 5 + 5 = 10 | 0.467 / 0.457 / 0.517 / 1.058 |
| l2Book fast:true | mainnet | 1101 | 110.15 | 0.545 / 0.541 / 0.629 / 0.790 / 1.111 | 5 + 5 = 10 | 0.419 / 0.406 / 0.463 / 0.736 |
| bbo | testnet | 126 | 12.83 | 4.676 / 1.248 / 13.983 / 49.214 / 64.390 | bid+ask pair (2 levels) | 0.442 / 0.409 / 0.570 / 0.884 |
| bbo | mainnet | 3697 | 370.41 | 0.162 / 0.116 / 0.358 / 0.819 / 2.047 | bid+ask pair (2 levels) | 0.383 / 0.354 / 0.459 / 0.958 |
| allMids | testnet | 119 | 11.92 | 5.035 / 5.037 / 5.125 / 5.174 / 5.177 | 3280 mids, **no time field** | n/a (no exchange time) |
| allMids | mainnet | 118 | 11.91 | 5.038 / 5.033 / 5.148 / 5.338 / 5.388 | 1237 mids, **no time field** | n/a (no exchange time) |

Feed age = local wall receive time − exchange `time` field (includes NTP skew; same clock
path for all feeds, so relative comparison valid). Payload shapes (raw, first message):
bbo `{"channel":"bbo","data":{"coin":"BTC","time":1791195060859,"bbo":[{"px","sz","n"},{"px","sz","n"}]}}`;
fast l2Book levels array carries `"fast":true`; allMids is `{channel,data:{mids:{...}}}` only.

### Findings

1. **`fast:true` exists on BOTH networks** (A-16 VERIFIED): subscriptionResponse echoes
   `fast:true`, data flows ~0.52-0.54 s cadence (≈10x default), depth 5+5 levels vs 20+20.
2. **Lowest bounded top-of-book age: fast l2Book** — max gap 1.233 s (testnet) / 1.111 s
   (mainnet), so worst-case ToB age ≈ gap+age ≈ 2.3 / 1.9 s; default l2Book ≈ 6.6 / 6.5 s;
   bbo has the lowest median receive-age (0.409 / 0.354 s) but is event-driven — testnet
   silence up to **64.39 s** in the quiet market (A-19 UNVERIFIED) → cannot bound staleness;
   mainnet bbo stays ≤2.047 s in this window only (A-20 UNVERIFIED). allMids has no `time`
   field → not an age signal; regular ~5.03 s heartbeat both networks.
3. Default l2Book cadence confirmed again ~5.3 s both networks (min gaps 0.677 / 2.365 s
   show occasional faster pushes); allMids ~5.03 s; no errors/rejects anywhere.

## T7e — fast l2Book adoption + cadence-aware staleness (live testnet, no strategy math)

Offline TDD: RED 9/11 new tests (TR-0034) → implementation → **62 passed** (TR-0035;
51→62, +11; two old-spec tests updated: REST confirm never restarts). `compileall` 0.
New tunables (existing guard thresholds untouched): `RISK_STALE_K` (default 2.0),
`RISK_RESTART_N` (default 3.0), `RISK_FAST_BOOK` (default 1; 0 = legacy default feed).

### Step 1 — fast-feed gaps vs K x median cadence (raw capture, 10 min each network)

| network | n | median | gaps > 2x (thr) | gaps > 3x (thr) | gaps > 4x (thr) | max gap |
|---|---|---|---|---|---|---|
| testnet | 1125 | 0.5229 s | **8**/10 min (1.046 s) | **0** (1.569) | **0** (2.092) | 1.343 s |
| mainnet | 1103 | 0.5382 s | **2**/10 min (1.076 s) | **0** (1.615) | **0** (2.153) | 1.544 s |

Chosen **K=2** (catches every observed tail silence at ~1.05 s vs legacy 3.0 s; K=3/K=4 fire
zero times in observed data = blind spot) and **N=3** (0 restarts/10 min healthy vs N=2 = 8+2;
N=4 = 0 but 0.5 s slower dead-feed detection). K < N keeps a confirm-before-restart band.
Evidence: `logs/t7e_gapcap_{testnet,mainnet}.json`.

### Steps 2–3 — implementation (offline, minimal)

- Gateway `_subscribe` sends `{"type":"l2Book","coin":"BTC","fast":true}` when
  `fast_book=True` (env `RISK_FAST_BOOK=0` → legacy subscription without the key); logged at
  start-up; `restart_feeds` re-subscribes with the same setting. SDK passes the dict through
  unchanged (proven T7d).
- `parse_l2` regression test on the real 5-level payload: top-of-book only → depth irrelevant
  (already passing; locked by `test_parse_l2_handles_fast_five_level_payload`).
- Rolling-median cadence: gateway keeps the last 30 interarrival gaps (warm after 5),
  `book_cadence_s()`; gaps ≥60 s excluded from the sample window.
- Cycle: confirm/pull when `age > K x cadence` (fallback `stale_book_s` while cadence cold);
  quiet → keep quotes, differ/fail → pull; **REST confirm never restarts** (cycle restart path
  removed). Watchdog: restart only when `age > N x cadence` (fallback `2 x stale_book_s`),
  10 s cooldown kept. `stale_pulls`/restart logs now carry `thr=`; metrics line carries
  `feed_age=`.

### Step 4 — 15 min live testnet segment (11:06:14–11:21:14Z, HL_DRY_RUN=0)

Wrapper env: `RISK_FAST_BOOK=1 RISK_STALE_K=2.0 RISK_RESTART_N=3.0`. Start-up line confirms
`fast_book=True stale_k=2.0 restart_n=3.0` and `l2Book subscription: {..., "fast": True}`.
`BOT_EXIT=124` → `FLAT_CHECK ... result=FLAT` → `FLAT_EXIT=0`.

| metric | value |
|---|---|
| duration / books | 15.05 min / 1530 processed (raw WS ~110/min while connected) |
| **actions/hour** | (18 places + 16 cancels) = 34 → **136/h** |
| fills | 0 |
| cross / risk / other rejects, place_errors | **0 / 0 / 0 / 0** (0 vetoes) |
| **stale_pulls** | **0** (2 REST confirms, both quiet: silence 1.2 s at thr 1.03/1.05 s = K x cadence ≈ 0.52) |
| **feed_restarts** | **1** (was 27 in T7b / 10 in T7c): 11:16:30 SDK session `Expired - goodbye` → quiet confirm kept quotes → silence 2.3 s > N x cadence (1.59 s) → watchdog restart → reconnected +3 s; quotes never pulled |
| pos_drift / orphans / stale_pruned | 0 / 0 / 0 |
| **feed_age** (metrics samples, n=15) | min 0.01 / median 0.20 / p90 0.45 / **max 0.59 s** |
| lat_ema_ms | 714–906 (below 1500 kill threshold) |
| equity | 998.94 → 999.68 display; pos 0 flat |
| userRateLimit | nRequestsUsed 1658 → 1698 (+40; l2Book/reads +0 per A-15; cap 10235 unchanged) |

No Tracebacks, no kills, no rejects of any kind. Evidence: `logs/t7e_live.log`.

### Step 5 — assumption

A-21 added: "feed age 0.4–0.5 s is mostly network distance" = **UNVERIFIED** (consistent with
T2 RTT medians but no one-way decomposition; see ASSUMPTIONS.md).
