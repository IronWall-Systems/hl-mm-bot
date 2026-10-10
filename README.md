# HL-MM Bot

**Hyperliquid market-making engine with a live multi-coin websocket collector.**

An automated market-making and trade-execution system for the Hyperliquid DEX, paired with a high-throughput market-data collector that streams trades, order-book snapshots and candles for 10 coins over a single multiplexed websocket.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Engine (src/mmbot/)                       │
│                                                                  │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐ │
│  │ strategy │──▶│  engine  │──▶│   exec   │──▶│  Hyperliquid  │ │
│  │ (spread/ │   │ (quote   │   │ (order   │   │  SDK         │ │
│  │  skew)   │   │  loop)   │   │  place)  │   │              │ │
│  └──────────┘   └────┬─────┘   └──────────┘   └──────────────┘ │
│                      │                                          │
│               ┌──────┴──────┐                                   │
│               │    risk     │                                   │
│               │ (limits /   │                                   │
│               │  kill sw.)  │                                   │
│               └──────┬──────┘                                   │
│                      │                                          │
│               ┌──────┴──────┐                                   │
│               │   state     │                                   │
│               │  (SQLite)   │                                   │
│               └─────────────┘                                   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    Collector (scripts/collector.py)              │
│                                                                  │
│   Hyperliquid WS ──▶ 30 subscriptions ──▶ gzip JSONL shards     │
│   (10 coins × 3 feeds: trades · book · candle)                  │
│                                                                  │
│   data/                                                          │
│   ├── trades/COIN/YYYY-MM-DD.jsonl.gz                          │
│   ├── book/COIN/YYYY-MM-DD.jsonl.gz                            │
│   └── candle/COIN/YYYY-MM-DD.jsonl.gz                          │
└─────────────────────────────────────────────────────────────────┘
```

### Two independent halves

| Component | Role | Deployment |
|---|---|---|
| **Engine** (`src/mmbot/`) | Market-making: quote both sides, manage inventory skew, hedge, persist state | Interactive / supervised |
| **Collector** (`scripts/collector.py`) | Live market data: 30 subscriptions over one websocket, gzip-sharded JSONL | systemd (`hlmm-collector.service`) |

The collector does NOT import the engine — they share only the `data/` directory and the venv.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Exchange | hyperliquid-python-sdk 0.24.0 |
| Websocket | websocket-client 1.9.2 |
| Signing | eth-account |
| Persistence | SQLite (engine state) · gzip JSONL (collector shards) |
| Config | python-dotenv + `MM_*` / `RISK_*` env overrides |
| Deployment | systemd (`Restart=always`, `Nice=10`) |

---

## Key Design Decisions

**One websocket, 30 subscriptions.** A single connection multiplexes trades, order-book and candle feeds for all 10 coins — lower overhead, single reconnect path, one backoff counter.

**Gzip-compressed daily shards.** Each (feed, coin, date) gets its own `.jsonl.gz` file. Append-only, cheap to rotate, readable with `zcat`.

**Reconnect-with-backoff.** On websocket drop the client reconnects with exponential backoff, re-issues all 30 subscriptions, and increments a visible reconnect counter in the STAT line.

**Risk gates before every order.** `risk.py` enforces position limits, drawdown guards and a kill switch. The engine refuses to quote outside configured bounds.

**SQLite state.** Fills, positions and PnL are persisted to a local SQLite database. The engine survives a restart with full position awareness.

---

## Project Structure

```
hl-mm-bot/
├── src/mmbot/
│   ├── engine.py        # core MM loop
│   ├── strategy.py      # spread / skew calculation
│   ├── feeds.py         # Hyperliquid websocket feeds
│   ├── exec.py          # order placement / cancel
│   ├── risk.py          # position limits, kill switch
│   ├── state.py         # SQLite persistence
│   └── config.py        # env-driven configuration
├── scripts/
│   ├── collector.py     # live 30-subscription collector
│   └── archive_data.py  # roll aged shards to archive
├── tests/               # offline unit tests
├── data/                # runtime shards (gitignored)
├── logs/                # runtime logs (gitignored)
├── deploy/
│   └── mmbot.service    # systemd unit template
├── docs/                # architecture, blueprint, security
├── requirements.txt
└── pytest.ini
```

---

## Quick Start

```bash
# 1. Clone and install
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — set HL_ENV, HL_ACCOUNT_ADDRESS, HL_API_SECRET, HL_COIN
# For mainnet: HL_CONFIRM_MAINNET=YES_I_UNDERSTAND
# Dry-run by default: HL_DRY_RUN=1

# 3. Run the engine
python -m src.mmbot.engine

# 4. Run the collector (separate process)
python scripts/collector.py
```

### Systemd (collector)

```bash
sudo cp deploy/mmbot.service /etc/systemd/system/hlmm-collector.service
# Edit ExecStart to point at your venv + collector.py
sudo systemctl daemon-reload
sudo systemctl enable --now hlmm-collector
```

---

## Testing

```bash
pytest tests/ -v
```

Offline unit tests with fixtures only — no live exchange connection required.

---

## Security

- `.env` is never committed (strict `.gitignore`).
- `HL_API_SECRET` is a trade-only wallet key — no withdrawal permissions.
- `Settings.api_secret` is `repr=False` — excluded from logs and tracebacks.
- Log lines never include the secret or the raw `.env`.
- Mainnet requires an explicit `HL_CONFIRM_MAINNET` confirmation.
- Dry-run mode (`HL_DRY_RUN=1`) is the default — no orders are placed without opt-in.

---

## License

Private. All rights reserved.
