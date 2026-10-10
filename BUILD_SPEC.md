# BUILD SPEC / CODER PROMPT: Hyperliquid maker bot (testnet first)

You are taking over a working skeleton (`src/mmbot`, 24 offline tests passing). Your job is to
**validate it against the real Hyperliquid testnet, fix whatever differs from our assumptions, and
harden it**. Do NOT add features, do NOT touch mainnet, and do NOT "improve" the strategy math.

## 0. Ground rules
1. Python >= 3.10. Only `hyperliquid-python-sdk==0.24.0` (signatures were inspected at that version), `eth-account`, `pytest`.
2. Only `exchange/client.py` may import the SDK. `strategy/quoter.py` stays pure (no I/O, no clocks).
3. Never log or print secrets. Never commit `.env`. Use an **API/agent wallet** key, not the master key.
4. Every behaviour you change needs a test. `pytest -q` must stay green offline (no network in tests).
5. Anything below marked **[UNVERIFIED]** is an assumption. Confirm it on testnet, then record the
   observed raw payload in `docs/observed_payloads.md` and fix the parser if it differs.
6. Mainnet is out of scope until the architect signs off on the gate in section 6.

## 1. Repository map (already created)
```
src/mmbot/
  models.py              dataclasses/enums: MarketState, PositionState, Quote, Order, OrderStatus, StrategyParams, RiskConfig
  precision.py           tick/lot rounding (5 sig figs, 6-szDecimals decimals, sizes floored)
  strategy/quoter.py     PURE: (market, inventory, params) -> bid/ask Alo targets with skew + clamp
  exchange/types.py      PlaceResult/CancelResult/events (no SDK)
  exchange/client.py     Gateway protocol, pure parsers, HyperliquidGateway (SDK, threads->asyncio)
  exchange/fake.py       in-memory exchange for tests
  execution/order_manager.py  order state machine, diff/plan, reconcile, stale pruning
  risk/guard.py          pre-trade checks + kill-switch decisions
  config.py              env loading, mainnet confirmation gate
  main.py                orchestrator (Bot): event loop, housekeeping watchdog, kill path
  panic.py               python -m mmbot.panic  -> cancel all + flatten
tests/                   24 offline tests (quoter, parsers, guard, order manager, bot scenarios)
```
Event flow: `WS event -> state update -> kill check -> stale-feed check -> compute_quotes -> OrderManager.plan (diff + hysteresis) -> cancels -> RiskGuard.pre_trade -> place Alo`.

## 2. Hard constraints discovered during design (keep them)
* **Address rate limit:** 10,000 request buffer + 1 per $1 traded; batches count n against it; when exhausted, 1 action / 10 s (cancels get a higher cap). A requote is 2 actions per side. That is why the plan uses hysteresis (`requote_threshold_bps`) and `max_actions_per_min`. Do not remove them. Log `metrics` every 60 s and track actions sent.
* **SDK websocket does not auto-reconnect** and allows one `orderUpdates` subscription per `Info`. Reconnect = new `Info` (see `restart_feeds`). The watchdog in `Bot.housekeeping` triggers it.
* **Alo semantics:** an order that would match immediately is cancelled by the engine. Treat as benign, back off that side (exponential, capped 5 s).
* **BTC tick is $1** at ~$70k (5 significant figures), about 1.4 bps. Sub-2 bps spreads are impossible; keep `half_spread_bps` >= 3.
* SDK calls are blocking: always `asyncio.to_thread` under the gateway lock.

## 3. Phase 1: testnet runbook (do in order, tick each box, paste evidence into `docs/testnet_log.md`)
- [ ] **Env:** `python -m venv .venv && pip install -r requirements.txt && pytest -q`
- [ ] **Wallets:** create a fresh master wallet for testnet, then an *API wallet* (agent) approved for it in the Hyperliquid app's API page. Put master address in `HL_ACCOUNT_ADDRESS`, agent key in `HL_API_SECRET`. `HL_ENV=testnet`.
- [ ] **Funds:** claim testnet USDC from the faucet in the testnet app (check the testnet docs/UI for the current faucet procedure; it may require prior mainnet activity). Confirm balance appears.
- [ ] **Account mode check [UNVERIFIED]:** run `python -c "from mmbot... account_state"` or the bot start-up; `accountValue` must be > 0. If it reads 0 while the UI shows funds (unified/spot-collateral account), extend `parse_user_state` to include the spot USDC balance. The bot refuses to start with equity 0 on purpose.
- [ ] **Dry run (`HL_DRY_RUN=1`):** real WS book, orders only logged. Run 30 min. Confirm: book updates arrive, quote prices obey tick rules, hysteresis keeps action count low, no exceptions.
- [ ] **Live testnet (`HL_DRY_RUN=0`) with tiny size** (defaults: $15 quotes, $60 inventory cap):
  - [ ] Orders appear in the UI with TIF Alo; oid/cloid match logs.
  - [ ] Record raw `orderUpdates`, `userFills`, place/cancel responses for each case (resting, filled, canceled, crossed reject, margin reject) in `docs/observed_payloads.md`. Compare to `parse_*` and fix.
  - [ ] **Confirm the post-only reject text** [UNVERIFIED] and update `CROSS_ERROR_MARKERS`.
  - [ ] **Confirm minimum order value** [UNVERIFIED, assumed $10] by placing a $5 order; adjust `min_order_notional`.
  - [ ] **Confirm cancel_by_cloid works** and what the "already gone" response says [UNVERIFIED]; update `GONE_MARKERS`.
  - [ ] Let it run 24 h. Export metrics: fills, cancels, places, cross_rejects, risk_rejects, pos_drift, feed_restarts. Zero unhandled exceptions.

## 4. Error-case verification (each must be demonstrated on testnet AND covered by a test)
| Case | How to provoke | Expected behaviour |
|---|---|---|
| Crossed book / Alo reject | Temporarily set `MM_HALF_SPREAD_BPS=0.1` on a fast market, or sleep 2 s inside a cycle | `cross_rejects` increments at INFO level, side backs off, no crash, no error count |
| Reject other | Set `MM_QUOTE_NOTIONAL` below exchange min, or drain margin | WARNING log, `record_error`, side blocked 5 s, bot continues; 8 consecutive -> kill |
| Network drop | Disable network 20 s (or block the WS host in firewall) | book age > 3 s: quotes pulled; at 6 s `restart_feeds`; quoting resumes; position adopted from exchange poll |
| WS silent death | Kill the SDK ws thread / close socket | Same as above. Add a test using `FakeGateway.feed_age` |
| Lost place response | Raise inside `_place_blocking` (use a patched SDK) | order stays PENDING_NEW, `_force_reconcile`, pruned or adopted within one housekeeping tick |
| Stale / orphan orders | Place an order from the UI, restart bot | start-up `cancel_all` removes leftovers; mid-run orphans cancelled by reconcile |
| Kill switch | `RISK_MAX_DRAWDOWN_PCT=0.0001` or `RISK_MAX_LATENCY_MS=1` | cancel all -> market flatten -> "verified flat" log -> exit code 2 |
| SIGTERM | `kill -TERM <pid>` | cancel all (+ flatten if `HL_FLATTEN_ON_EXIT=1`), exit 0 |
| Rate limit | Force many requote cycles | Local pacing veto before the exchange throttles; if the exchange throttles anyway, treat as REJECT_OTHER and slow down |

## 5. Deliverables for Phase 1
1. `docs/observed_payloads.md`, `docs/testnet_log.md` with the checklist and evidence.
2. Parser/constant fixes with tests. 3. A 24 h testnet metrics summary. 4. List of every assumption that turned out wrong.

## 6. Mainnet gate (architect decides; the coder does NOT proceed on their own)
All must be true:
- [ ] Real-data backtest (S3 loader) shows average markout **above** the maker fee after funding, with the cancel-rate bracket (0 and estimated). **If not, do not go live; fix the strategy first.** (Testnet fills prove plumbing only; testnet liquidity and flow are not representative of mainnet.)
- [ ] Section 3 and 4 fully ticked, 24 h testnet with no unhandled exceptions, kill switch demonstrated.
- [ ] Verified current fee tier for the wallet on the official fee page.

Migration steps (only after the gate):
1. Generate a **new** master wallet used only for this bot. Never reuse a personal wallet. Create a new API wallet; do not give the agent key withdrawal rights (agent wallets cannot withdraw by design; verify).
2. Fund with a small amount you can afford to lose entirely (start <= $100). Check the exact bridging/deposit procedure in the official docs; do not send funds from an exchange without confirming network and token.
3. `.env`: `HL_ENV=mainnet`, `HL_CONFIRM_MAINNET=YES_ISOLATED_WALLET`, keep `MM_*`/`RISK_*` at the testnet-proven values. `HL_DRY_RUN=1` first for 1 h (mainnet data, no orders).
4. Sanity checks before `HL_DRY_RUN=0`: equity reads correctly; tick/lot sample orders pass; `python -m mmbot.panic` tested on testnet and works; alerts/log shipping in place; machine clock synced (NTP); run under systemd with `Restart=on-failure` and `ExecStopPost=python -m mmbot.panic`.
5. Go live at minimum size for 24 h; review fills, markouts vs backtest, fee tier actually charged, action budget consumed. Only then consider raising caps, one step at a time.
6. Kill criteria stay on. Never disable the guard to "let it run".
