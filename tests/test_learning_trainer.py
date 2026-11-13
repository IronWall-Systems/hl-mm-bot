"""Rolling trainer: all five stages, KL rollback, drift and guard blocking."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mmbot.learning.buffer import DecisionStep, RollingExperienceBuffer
from mmbot.learning.features import FEATURE_NAMES, vector
from mmbot.learning.policy import ACTIONS, ACTION_INDEX, SoftmaxLinearPolicy
from mmbot.learning.trainer import RollingTrainer, TrainerConfig

NOW = 100_000.0


def build(tmp_path, n: int = 300, seed: int = 0):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10 ** 9,
                                  now_fn=lambda: NOW)
    rng = np.random.default_rng(seed)
    policy = SoftmaxLinearPolicy(n_features=len(FEATURE_NAMES))
    for i in range(n):
        feats = {name: float(rng.normal()) for name in FEATURE_NAMES}
        action = ACTIONS[i % len(ACTIONS)]
        reward = float(rng.normal(0, 0.01)) + (0.02 if action == "quote_both" else -0.01)
        x = vector(feats)
        prop = float(policy.probs(x)[0][ACTION_INDEX[action]])
        buf.append(DecisionStep(
            step_id=0, ts=NOW - n + i, mono=NOW - n + i, coin="HYPE",
            features=feats, action=action, propensity=prop, guard_status="ok",
            mid=100.0, position=0.0, equity=100.0,
            reward_30s=reward, reward_300s=reward, reward=reward,
            episode_id=f"g{i // 30}"))
    return buf, policy


def cfg(tmp_path, **kw) -> TrainerConfig:
    base = dict(
        window_s=10 ** 9, trigger_every_s=0.0, min_steps=50, min_new_steps=1,
        group_s=300.0, kl_max=10.0, drift_z_max=100.0, lr=0.05,
        checkpoint_path=str(tmp_path / "policy.json"),
        ref_path=str(tmp_path / "ref.json"),
        log_path=str(tmp_path / "train.jsonl"),
        halt_path=str(tmp_path / "HALT"))
    base.update(kw)
    return TrainerConfig(**base)


def test_full_pipeline_runs_all_stages_and_checkpoints(tmp_path):
    buf, policy = build(tmp_path)
    tr = RollingTrainer(buf, policy, cfg(tmp_path), now_fn=lambda: NOW)
    res = tr.train(NOW)
    assert res.trained and not res.rolled_back
    assert {"sft", "dpo", "rloo", "grpo", "ppo"} <= set(res.stages)
    assert Path(cfg(tmp_path).checkpoint_path).exists()
    loaded = SoftmaxLinearPolicy.load(cfg(tmp_path).checkpoint_path)
    assert loaded.trained_steps == 300
    assert Path(cfg(tmp_path).log_path).exists()
    # Reference checkpoint exists and is frozen.
    assert Path(cfg(tmp_path).ref_path).exists()


def test_kl_guard_rolls_back_a_stage(tmp_path):
    buf, policy = build(tmp_path)
    tr = RollingTrainer(buf, policy, cfg(tmp_path, kl_max=0.0), now_fn=lambda: NOW)
    before = policy.weights.copy()
    res = tr.train(NOW)
    assert res.rolled_back is True
    assert res.trained is False
    assert res.reason == "kl_rollback"
    assert np.allclose(policy.weights, before)
    assert not Path(cfg(tmp_path).checkpoint_path).exists()


@pytest.mark.parametrize("status", ["soft", "pull", "hard"])
def test_loss_guard_blocks_training(tmp_path, status):
    buf, policy = build(tmp_path)
    tr = RollingTrainer(buf, policy, cfg(tmp_path), now_fn=lambda: NOW)
    res = tr.maybe_train(NOW, guard_status=status, force=True)
    assert res.trained is False
    assert res.reason == f"guard_{status}"
    assert not Path(cfg(tmp_path).checkpoint_path).exists()


def test_halt_marker_blocks_training(tmp_path):
    buf, policy = build(tmp_path)
    Path(cfg(tmp_path).halt_path).write_text("halt\n")
    tr = RollingTrainer(buf, policy, cfg(tmp_path), now_fn=lambda: NOW)
    res = tr.maybe_train(NOW, force=True)
    assert res.reason == "halt_file"


def test_covariate_drift_blocks_training(tmp_path):
    buf, policy = build(tmp_path)
    tr = RollingTrainer(buf, policy, cfg(tmp_path, drift_z_max=0.5),
                        now_fn=lambda: NOW)
    # Drift is measured against the frozen reference baseline.
    tr.ref_policy.feature_mean = np.full(len(FEATURE_NAMES), 100.0)
    tr.ref_policy.feature_std = np.ones(len(FEATURE_NAMES))
    tr.ref_policy.meta["stats_initialized"] = True
    res = tr.maybe_train(NOW, force=True)
    assert res.trained is False
    assert res.reason.startswith("covariate_drift")


def test_sustained_drift_refreshes_baseline_instead_of_blocking_forever(tmp_path):
    buf, policy = build(tmp_path)
    tr = RollingTrainer(buf, policy, cfg(tmp_path, drift_z_max=0.5,
                                         drift_refresh_after=2),
                        now_fn=lambda: NOW)
    tr.ref_policy.feature_mean = np.full(len(FEATURE_NAMES), 100.0)
    tr.ref_policy.feature_std = np.ones(len(FEATURE_NAMES))
    tr.ref_policy.meta["stats_initialized"] = True
    r1 = tr.maybe_train(NOW, force=True)
    assert r1.reason.startswith("covariate_drift")
    r2 = tr.maybe_train(NOW + 1.0, force=True)
    assert r2.trained is True
    assert tr._drift_streak == 0


def test_min_steps_gate(tmp_path):
    buf, policy = build(tmp_path)
    tr = RollingTrainer(buf, policy, cfg(tmp_path, min_steps=10_000),
                        now_fn=lambda: NOW)
    res = tr.train(NOW)
    assert res.trained is False
    assert res.reason.startswith("need_10000")


def test_large_raw_spread_stays_within_kl_cap(tmp_path):
    """Regression: testnet spreads (~445 bps) must not blow up a linear step."""
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10 ** 9,
                                  now_fn=lambda: NOW)
    policy = SoftmaxLinearPolicy(n_features=len(FEATURE_NAMES))
    for i in range(250):
        feats = {name: 0.0 for name in FEATURE_NAMES}
        feats["spread_bps"] = 445.0
        feats["obi_nw"] = 0.2 * ((i % 5) - 2)
        action = ACTIONS[i % len(ACTIONS)]
        reward = 0.01 if action == "quote_both" else -0.005
        x = vector(feats)
        prop = float(policy.probs(x)[0][ACTION_INDEX[action]])
        buf.append(DecisionStep(
            step_id=0, ts=NOW - 250 + i, mono=NOW - 250 + i, coin="HYPE",
            features=feats, action=action, propensity=prop, guard_status="ok",
            mid=100.0, position=0.0, equity=100.0,
            reward_30s=reward, reward_300s=reward, reward=reward,
            episode_id=f"g{i // 25}"))
    tr = RollingTrainer(buf, policy, cfg(tmp_path, kl_max=0.15, lr=0.01),
                        now_fn=lambda: NOW)
    res = tr.train(NOW)
    assert res.rolled_back is False
    assert res.kl <= 0.15


def test_periodic_schedule_and_checkpoint_version(tmp_path):
    buf, policy = build(tmp_path)
    tr = RollingTrainer(buf, policy, cfg(tmp_path, trigger_every_s=10.0,
                                         min_new_steps=1), now_fn=lambda: NOW)
    r1 = tr.maybe_train(NOW, force=True)
    assert r1.trained
    v1 = tr.policy.version
    r2 = tr.maybe_train(NOW + 1.0)
    assert r2.reason == "not_due"

    # One genuinely new episode makes the next scheduled run due.
    buf.append(DecisionStep(
        step_id=0, ts=NOW + 5.0, mono=NOW + 5.0, coin="HYPE",
        features={name: 0.1 for name in FEATURE_NAMES}, action="quote_both",
        propensity=0.5, mid=100.0, reward=0.05, reward_30s=0.05,
        reward_300s=0.05, episode_id="new"))
    r3 = tr.maybe_train(NOW + 11.0)
    assert r3.trained
    assert tr.policy.version > v1
