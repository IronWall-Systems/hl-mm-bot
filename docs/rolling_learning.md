# Rolling-Buffer Online Learning (Shadow/Testnet)

Continuous post-training for `mmbot` without static backtests. The trading
process only **records** decision steps; a separate daemon settles delayed
rewards, trims the rolling window, trains periodically, and checkpoints the
policy. A checkpoint fsync can therefore never block quoting.

## Architecture

```
public l2Book (testnet, no auth)          trading process (opt-in)
        │                                        │
        ▼                                        ▼
  PaperBroker fills                     LearningRecorder.observe()
        │                                        │
        └──────────────► data/rolling_buffer.jsonl ◄──────────┘
                                   │
                                   ▼
              RollingLearningLoop (scripts/rolling_learning_daemon.py)
                 ├─ RewardTracker: 30 s / 300 s delayed rewards
                 ├─ buffer.trim(): sliding retention window (4 h … 7 d)
                 ├─ RollingTrainer: SFT → DPO → RLOO → GRPO → PPO
                 ├─ KL(pi_theta || pi_ref) + covariate drift guard
                 └─ models/policy_latest.json
```

### Modules

| File | Responsibility |
| --- | --- |
| `src/mmbot/learning/buffer.py` | `DecisionStep`, append-only JSONL, retention trim + atomic compaction, optional parquet export |
| `src/mmbot/learning/features.py` | causal feature vector (`FEATURE_NAMES`), OBI/trade-flow/vol-ratio/BTC lead-lag, drift z-score |
| `src/mmbot/learning/rewards.py` | 30 s / 300 s markout rewards, fees, `protection_cap_usd`, bisect-based settlement |
| `src/mmbot/learning/policy.py` | softmax-linear policy, propensities, KL, JSON checkpoints, in-place rollback |
| `src/mmbot/learning/trainer.py` | SFT/DPO/RLOO/GRPO/PPO stages, KL rollback, covariate drift guard |
| `src/mmbot/learning/recorder.py` | trading-process recorder, guard tagging, reward settlement |
| `src/mmbot/learning/loop.py` | out-of-band settle/trim/train/heartbeat loop |
| `src/mmbot/learning/keys.py` | in-memory-only dev key vault + parser/redaction |
| `scripts/rolling_learning_daemon.py` | Testnet + Shadow Paper runner (public feed, paper fills) |

## Rolling buffer

* One JSON object per line, two record types: `step` and `reward`. Delayed
  rewards are appended separately so the hot path never rewrites the file.
* `MM_ROLLING_RETENTION_H` (default `24`), `MM_ROLLING_MAX_RETENTION_H`
  (default `168`, hard-capped at 7 days), `MM_ROLLING_MAX_STEPS` (default
  `250000`).
* `trim()` drops expired steps and atomically rewrites the file (`os.replace`),
  so a concurrent reader never sees a half-written buffer.
* Every step stores: wall-clock timestamp, causal features, chosen action,
  propensity `p`, guard status, mid, position, equity, 30 s/300 s reward, and
  `meta.explored`.

## Reward alignment

`reward = realized_pnl + position * (mid_horizon - mid_decision) - fees`, with
the markout term clipped at `MM_REWARD_CAP_USD` (default `$0.10`). The 30 s
horizon settles as soon as a mark at/after +30 s exists; the 300 s horizon
refreshes the combined reward when it lands. A single settled horizon is
already trainable, so a feed gap cannot leave the whole buffer unready.

## Training pipeline

Triggered every `MM_TRAIN_EVERY_H` (default `4 h`) **and** after
`MM_TRAIN_MIN_NEW_STEPS` (default `100`) new steps, provided at least
`MM_TRAIN_MIN_STEPS` (default `200`) ready steps are in the window.

1. **SFT** — advantage-weighted imitation of the top-30 % positive rewards.
2. **DPO** — pairwise preference between top-30 % winners and bottom-30 %
   losers, with a frozen-reference log-ratio margin.
3. **RLOO** — leave-one-out group advantages, self-normalised importance
   weights from the logged propensities.
4. **GRPO** — group-normalised advantages.
5. **PPO** — GAE advantages plus a linear critic; ratios use the logged
   propensity as the old policy.

Each stage is snapshotted; if `KL(pi_theta || pi_ref) > MM_TRAIN_KL_MAX`
(default `0.15`) the stage is restored **in place** (callers keep a valid
policy reference). The covariate guard compares the training window's feature
means with the frozen reference baseline; features whose baseline is
near-constant are ignored, and sustained drift (3 consecutive skips) refreshes
the baseline instead of blocking forever.

Checkpoints are plain JSON (`models/policy_latest.json`,
`models/policy_ref.json`) and the training log is append-only JSONL
(`logs/learning_train.jsonl`).

## LossGuard precedence

`LossGuard` is authoritative in both processes:

* `hard` → recorder writes `risk_hold`, propensity `1.0`, `explored=False`;
  the daemon cancels all paper quotes, blocks training and exits `3`.
* `soft` → `reduce_only`, no exploration; trainer refuses to update.
* `pull` → `pull`, no exploration; trainer refuses to update.
* A `logs/HALT` marker also blocks training (`halt_file` reason).

Training steps are filtered to actions in the policy action space, so
guard-only `risk_hold` samples can never corrupt a gradient update.

## Dev-mode Telegram key ingestion

Disabled by default. Enable with `TG_ALLOW_KEY_INGEST=1` (and
`TG_KEY_TTL_S`, default `3600`). An authorised operator sends:

```
/key openai sk-...
/key sk-...
sk-...
```

The monitor reads the message, immediately calls `deleteMessage` for both
sides of the chat, stores the key only in `InMemoryKeyVault` (process RAM,
TTL-bounded), and replies with a confirmation that never echoes the key. Raw
keys are never written to disk or logs. Do not enable this on a production
account; prefer the OS keyring or a secrets manager.

## Running

Trading process (opt-in, observational):

```bash
MM_LEARNING=1 PYTHONPATH=src python -m mmbot.main
```

Shadow daemon (no credentials):

```bash
PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/rolling_learning_daemon.py \
  --env testnet --source l2 --coin HYPE --poll-s 2 \
  --retention-h 24 --train-every-s 14400 --min-steps 200
```

The systemd unit is `deploy/mmbot-rolling-learning.service`
(`mmbot-rolling-learning.service`). It writes:

| Path | Content |
| --- | --- |
| `data/rolling_buffer.jsonl` | rolling experience buffer |
| `data/rolling_loop_state.json` | heartbeat / last train result |
| `data/learning_guard_state.json` | latest guard status for the trainer |
| `models/policy_latest.json` | live policy checkpoint |
| `models/policy_ref.json` | frozen KL/reference baseline |
| `logs/learning_train.jsonl` | one record per training attempt |
| `logs/rolling_learning.console.log` | daemon stdout |

### Inspecting

```bash
systemctl status mmbot-rolling-learning.service
cat data/rolling_loop_state.json | python -m json.tool
tail -f logs/rolling_learning.console.log
tail -n 5 logs/learning_train.jsonl | python -m json.tool --json-lines
```

## Tests

`pytest -q` covers retention/compaction, reward alignment and protection cap,
propensity logging, checkpoint round-trips, KL rollback, covariate drift,
guard blocking, periodic scheduling, recorder guard tags, Telegram deletion
ordering/memory-only storage, and Bot integration.
