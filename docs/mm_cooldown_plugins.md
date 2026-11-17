# MM cooldown / circuit-breaker plugins

A modular safety layer for the Hyperliquid market maker.  It pauses **new order
placement** (and withdraws resting quotes) when cumulative session loss reaches
**$0.25**, then stays paused for a configurable window.  The core engine
(`src/mmbot/`) and the main runner (`src/mmbot/main.py`) are **not modified**.

## Layout

```
strategies/market_maker/plugins/
├── __init__.py                 exports load_plugin / install_from_env
├── base.py                     BaseCooldownPlugin + CooldownDecision
├── state.py                    persisted state (atomic JSON writes)
├── cooldown_6h.py              plugin 1: 6-hour pause
├── cooldown_12h.py             plugin 2: 12-hour pause
├── cooldown_24h_4blocks.py     plugin 3: 24h = 4 x 6h check blocks
├── loader.py                   dynamic import + env configuration
├── integration.py              runtime hooks into RiskGuard / OrderManager
└── bootstrap.py                install() + --status / --reset CLI
```

## Trigger and loss accounting

* Trigger threshold defaults to **$0.25** (`>=`; a loss of exactly $0.25 arms it).
* The session baseline is the first positive account equity the plugin observes
  (usually the equity passed to `RiskGuard` at startup).
* `cumulative_loss_usd = max(realized-PnL loss, session_start_equity - last_equity)`.
  Engines that know realized PnL can call `note_realized_pnl(delta)`; the MM
  adapter feeds equity polls, so the equity term is what normally applies.
* Non-positive equity is ignored: the core guard already handles unreadable equity.
* One loss episode arms the breaker once.  If the window expires with the loss
  unchanged, entries resume.  A recovery below the threshold, followed by a new
  loss, arms it again.  A *deeper* loss during an active window extends/re-arms
  with a fresh full window.
* Arming uses **wall-clock epoch seconds**, so a restart cannot shorten the pause.

## Plugins

| `MM_COOLDOWN_PLUGIN` | Window | Structure |
|---|---:|---|
| `cooldown_6h` | 6 h | one block |
| `cooldown_12h` | 12 h | one block |
| `cooldown_24h_4blocks` | 24 h | 4 sequential 6-hour checks |

`cooldown_24h_4blocks` remains blocked for the full 24 hours.  Every 6 hours it
records a completed check (`block_index` 0→1→2→3→4), logs progress, and persists
it, so progress is observable and restart-safe.

## Enabling it (no core edits)

Use the provided launcher, which installs the plugin before constructing `Bot`:

```bash
cd /root/hl-mm-bot
MM_COOLDOWN_PLUGIN=cooldown_6h venv/bin/python scripts/mm_run.py
```

`scripts/mm_run.py` is a drop-in replacement for `python -m mmbot.main`; with
`MM_COOLDOWN_PLUGIN` unset it behaves identically to the original runner.
For a systemd unit, change only `ExecStart` to point at `scripts/mm_run.py` and
add the environment variable (or set it in `EnvironmentFile=.env`).

Inspect / reset state without starting the bot:

```bash
MM_COOLDOWN_PLUGIN=cooldown_24h_4blocks \
    venv/bin/python -m strategies.market_maker.plugins.bootstrap --status

MM_COOLDOWN_PLUGIN=cooldown_24h_4blocks \
    venv/bin/python -m strategies.market_maker.plugins.bootstrap --reset
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MM_COOLDOWN_PLUGIN` | *(unset = disabled)* | built-in name, module path, or `module:Class` |
| `MM_COOLDOWN_LOSS_USD` | `0.25` | loss threshold |
| `MM_COOLDOWN_HOURS` | plugin default | override window length |
| `MM_COOLDOWN_STATE_DIR` | `data/cooldowns` | state directory |

Custom plugins only need to subclass `BaseCooldownPlugin` and set
`plugin_name`, `cooldown_hours`, and optionally `blocks`; the loader discovers
the single plugin class in the module.

## How the non-invasive hook works

`integration.install_plugin()` patches class methods at runtime (the source files
are untouched):

* `RiskGuard.__init__` seeds the plugin with starting equity.
* `RiskGuard.update_equity` feeds every equity poll into loss accounting.
* `RiskGuard.pre_trade` returns `Verdict(False, ...)` while blocked (defence in depth).
* `OrderManager.plan` replaces the target quotes with an empty set while blocked,
  so the normal plan path **cancels resting quotes and places nothing new**.

Only entries are paused.  The MM engine has no separate exit path in this loop;
any fill that races a cancel is still processed normally.  The plugin does not
flatten positions and does not replace the existing kill switch.

If the plugin raises during evaluation, the adapter **fails closed** (new orders
are blocked) and logs the error, rather than silently disabling the safety layer.

## Persistence

State lives at `data/cooldowns/mm_<plugin>.json` (override with
`MM_COOLDOWN_STATE_DIR`).  Writes use a same-directory temp file plus
`os.replace`, followed by `fsync`, so a crash cannot leave partial JSON.

Example:

```json
{
  "version": 1,
  "plugin": "cooldown_24h_4blocks",
  "session_start_equity": 30.0,
  "last_equity": 29.7,
  "session_realized_pnl": 0.0,
  "cooldown_started_at": 1793900000.0,
  "cooldown_until": 1793986400.0,
  "trigger_loss_usd": 0.3,
  "trigger_reason": "session_loss",
  "loss_at_last_trigger": 0.3,
  "block_index": 1,
  "armed_count": 1,
  "updated_at": 1793907000.0
}
```

On startup the plugin reloads this file and immediately blocks if `now <
cooldown_until`.  A corrupt or plugin-mismatched file is ignored with a warning
and a fresh state is started.

## Tests

```bash
venv/bin/python -m unittest discover -s tests -v
```

Coverage includes: exact-threshold arming, 6h/12h expiry, 24h four-block
progression, restart persistence (including mid-cooldown restart), loss-episode
re-arming, corrupt-state recovery, env-based loader resolution and overrides, and
`RiskGuard` / `OrderManager` hook behaviour (veto + quote withdrawal + resume).

## Operational note

The current MM account reads **$0.00 equity**, so `Bot.init()` refuses to start
regardless of the cooldown plugin.  The plugin can be exercised with
`--status` and the unit tests, but live MM trading requires a funded account.
