# Incident 2026-11-07 — HYPE trial halted by exchange request budget

## Summary

At `11:06:36 UTC` the HYPE `live_trial` hit Hyperliquid's cumulative request
ceiling and the risk guard performed its designed kill switch. The bot
cancelled both quotes, verified flat, and stopped. At `11:07:08 UTC` the tmux
server then segfaulted, which also terminated `ai_sentinel` and `tg_monitor`
(the microstructure daemon runs under systemd and was unaffected).

The account is flat with `0` open orders. Equity was `$24.14989` at the halt.

## Root cause

Hyperliquid rejects order actions once an account exceeds:

```text
10,000 + cumulative USDC volume traded
```

Observed rejection:

```text
Too many cumulative requests sent (10422 > 10421)
for cumulative volume traded $422.03
```

The account had sent `10,422` requests against a budget of `10,421`. Each
rejected `batchModify` counted as a consecutive error; after 8 errors the risk
guard raised `KILL SWITCH: consecutive_errors` and shut down safely.

This is **not** a network outage, exchange outage, or daemon defect. The
micro-notional trial's requote cadence is structurally incompatible with this
account's earned request budget: 10 fills ($118 notional) earn only about 118
requests, while the strategy consumed thousands during the session.

## Timeline

| UTC | Event |
|---|---|
| 10:59–11:06 | Normal quoting, equity ~`$24.15`, flat, `0` errors |
| 11:06:31 | First `Too many cumulative requests sent` rejection |
| 11:06:36 | 8 consecutive rejects → risk kill switch |
| 11:06:37 | Quotes cancelled, flat verified, process stopped |
| 11:07:08 | `tmux` server segfault (kernel log), all tmux children lost |
| 11:41 | Health check detected the halt; no restart performed |

## Current safe state

* `live_trial`: stopped, flat, `0` open orders, no supervisor running.
* `mmbot-live-trial.service`: installed, **disabled** on purpose.
* `mmbot-ai-sentinel.service`: installed, **disabled** on purpose (it would only
  review stale logs while trading is halted).
* `mmbot-tg-monitor.service`: installed and running (read-only dashboard).
* `mmbot-microstructure.service`: running, feeds healthy, state fresh.

## What must happen before restart

The account has no request headroom. Restarting now would immediately fail the
first `batchModify` and kill again. One of:

1. **Rotate to a fresh account/API wallet with a new request budget** (cleanest
   for a micro-notional trial).
2. **Fund the budget with volume** — every `$1` traded earns one request. A
   meaningful buffer (e.g. `$5,000` volume) costs fees/slippage but buys `5,000`
   requests.
3. **Reduce action rate to near the volume-earned rate** — recent fills earned
   well under `1` request/minute, which is too low for active repricing. A
   near-static or much wider/longer-interval quote schedule is required.

## tmux removal

The tmux server segfaulted twice (`08:23` and `11:07`), each time killing every
session. All long-running processes are now represented by systemd units:

| Unit | Enabled | Running |
|---|---|---|
| `mmbot-microstructure.service` | yes | yes |
| `mmbot-tg-monitor.service` | yes | yes |
| `mmbot-live-trial.service` | no | no |
| `mmbot-ai-sentinel.service` | no | no |

The AI sentinel now stops the bot through
`systemctl stop mmbot-live-trial.service`, with the legacy tmux path only as a
fallback.
