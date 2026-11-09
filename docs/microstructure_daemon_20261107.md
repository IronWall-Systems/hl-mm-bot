# Microstructure Signal Daemon — Deployment & Operations (2026-11-07)

## What this is

`src/mmbot/microstructure_daemon.py` is a standalone, read-only asyncio daemon
that computes the validated HYPE microstructure signals and publishes them to a
shared JSON state file. It never imports the trading loop, never reads account
credentials, and never sends orders.

| Signal | Definition |
|---|---|
| `obi_nw` | Top-5 L2 imbalance, level-weighted by `sz * log1p(n) * exp(-0.5*i)` |
| `tfi2` | 2 s aggressive taker-flow imbalance `(V_buy - V_sell) / (V_buy + V_sell)` |
| `lead_btc_1s` | Binance BTC `bookTicker` mid return over 1 s, in bps |
| `walls` | Verified `nSigFigs=4` levels with `n >= 5` and `sz >= 3x` median, nearest first |

The daemon publishes every `100 ms` (configurable `50–200 ms`) and is
independent of `live_trial`; it can be restarted or upgraded without touching
open orders or inventory.

## State file

Path: `logs/microstructure_state.json`. Writes are atomic (`tmp` + `os.replace`),
so readers never observe a partial file.

```json
{
  "schema": "mmbot-microstructure/1",
  "seq": 1234,
  "ts": 1794041712.9,
  "iso": "2026-11-07T08:55:12.900000Z",
  "coin": "HYPE",
  "signals": {"obi_nw": 0.12, "tfi2": -0.30, "lead_btc_1s": 0.8, "btc_ready": true},
  "book": {"mid": 78.79, "best_bid": 78.78, "best_ask": 78.79, "spread_bps": 1.2},
  "walls": {
    "sig": 4,
    "bid": [{"px": 78.70, "sz": 4400, "n": 22, "score": 13600, "dist_bps": 11.4}],
    "ask": [],
    "nearest_bid": {"px": 78.70, "dist_bps": 11.4},
    "nearest_ask": null
  },
  "health": {
    "hl_connected": true, "hl_age_s": 0.04,
    "binance_connected": true, "binance_age_s": 0.2,
    "hl_reconnects": 0, "binance_reconnects": 0,
    "engine": "ok", "last_error": null
  }
}
```

The trading loop can consume it without touching the execution path:

```python
from mmbot.microstructure_daemon import SharedMicroStateReader

micro_reader = SharedMicroStateReader()      # logs/microstructure_state.json
micro = micro_reader.read()                  # MicroState | None (stale => None)
quotes = compute_quotes(market, pos_size, params, spec, micro)
```

`SharedMicroStateReader` caches by `(inode, mtime_ns, size)` and re-reads only
when the daemon replaces the file, so the per-cycle cost is one `stat`.

## Service

Installed at `/etc/systemd/system/mmbot-microstructure.service` and mirrored in
`deploy/mmbot-microstructure.service`.

Key settings:

* `Restart=always`, `RestartSec=3`, `StartLimitIntervalSec=0`
* `MemoryMax=192M`, `MemoryHigh=160M`, `CPUQuota=25%`, `TasksMax=64`
* `ProtectSystem=strict`, `ProtectHome=read-only`,
  `ReadWritePaths=/root/hl-mm-bot-live-safety-trial/logs`
* Runs as `root` because the existing `live_trial` process does; the daemon
  itself needs only public market data.
* Logs: `logs/microstructure_daemon.log` (rotating, 5 MiB x 5).

### Install / enable / start

```bash
sudo install -m 0644 deploy/mmbot-microstructure.service \
  /etc/systemd/system/mmbot-microstructure.service
sudo systemctl daemon-reload
sudo systemctl enable --now mmbot-microstructure.service

# verify
systemctl status mmbot-microstructure.service
systemctl is-active mmbot-microstructure.service
tail -f logs/microstructure_daemon.log

# state freshness (age should be < 0.5 s, seq should increase every ~0.1 s)
python - <<'PY'
import json, time
d = json.load(open("logs/microstructure_state.json"))
print(d["seq"], round(time.time() - d["ts"], 3), d["signals"])
PY
```

### Upgrade / restart safely

The daemon is read-only and stateless, so a restart is safe at any time:

```bash
sudo systemctl restart mmbot-microstructure.service
```

Do not restart `live_trial` unless its own configuration changes. This daemon
does not own positions or orders.

### Uninstall

```bash
sudo systemctl disable --now mmbot-microstructure.service
sudo rm /etc/systemd/system/mmbot-microstructure.service
sudo systemctl daemon-reload
```

## Reconnect / failure handling

* Hyperliquid SDK has no auto-reconnect: the feed is rebuilt on stall.
  Rebuilds are rate-limited with `5 s` exponential backoff capped at `60 s`, so
  an exchange outage does not become a reconnect storm.
* Binance `websocket-client` runs in its own daemon thread with exponential
  backoff (`1 s` to `30 s`), and the watchdog restarts the thread if it exits.
* Staleness thresholds: Hyperliquid `12 s`, Binance `15 s`.
* Malformed frames are logged and dropped; one bad message never kills a feed.
* Publication failures are logged and retried on the next 100 ms tick.
* `--self-test` runs an offline deterministic check:

```bash
PYTHONPATH=src python src/mmbot/microstructure_daemon.py --self-test
```

## Validation

```bash
/root/hl-mm-bot/venv/bin/python -m pytest -q
```

Coverage includes OBI weighting/top-5 truncation, 2 s flow window, BTC lead
readiness, wall verification, atomic writes, stale/missing/malformed state,
shared-reader caching, and reconnect backoff.

## Operational note (2026-11-07)

During the Hyperliquid `502` outage at approximately `08:18 UTC`, `live_trial`
halted because it could not verify flat during shutdown. The position was
flattened manually after the exchange recovered, and `live_trial`,
`ai_sentinel`, and `tg_monitor` were relaunched. The microstructure daemon
remained healthy throughout and continued publishing fresh state.
