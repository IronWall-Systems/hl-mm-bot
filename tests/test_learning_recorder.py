"""Recorder: propensity logging, guard supremacy, reward settlement, trim."""
from __future__ import annotations

import json

import pytest

from mmbot.learning.buffer import RollingExperienceBuffer
from mmbot.learning.features import FEATURE_NAMES
from mmbot.learning.policy import ACTIONS, SoftmaxLinearPolicy
from mmbot.learning.recorder import LearningRecorder

NOW = 1000.0


def make(tmp_path, *, cap: float = 10.0, retention: float = 10 ** 9):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=retention,
                                  now_fn=lambda: NOW)
    policy = SoftmaxLinearPolicy(n_features=len(FEATURE_NAMES))
    rec = LearningRecorder(
        buf, policy, coin="HYPE", max_notional=11.0,
        guard_state_path=tmp_path / "guard.json",
        policy_path=tmp_path / "policy.json",
        protection_cap_usd=cap, now_fn=lambda: NOW)
    return rec, buf, policy


def test_observe_logs_action_propensity_and_guard_state(tmp_path):
    rec, buf, _ = make(tmp_path)
    step = rec.observe(NOW, 100.0, position=0.0, equity=100.0,
                       guard_status="ok", action_hint="quote_both",
                       spread_bps=5.0)
    assert step.action == "quote_both"
    assert 0.0 < step.propensity <= 1.0
    assert step.guard_status == "ok"
    assert step.features["spread_bps"] == pytest.approx(5.0)
    assert len(buf) == 1
    state = json.loads((tmp_path / "guard.json").read_text())
    assert state["guard"] == "ok"


def test_soft_guard_records_reduce_only_without_exploration(tmp_path):
    rec, _, _ = make(tmp_path)
    step = rec.observe(NOW, 100.0, position=0.2, equity=99.0,
                       guard_status="soft", action_hint="reduce_only")
    assert step.action == "reduce_only"
    assert step.propensity == 1.0
    assert step.meta["explored"] is False


def test_hard_guard_records_risk_hold(tmp_path):
    rec, _, _ = make(tmp_path)
    step = rec.observe(NOW, 100.0, position=0.2, equity=98.0,
                       guard_status="hard")
    assert step.action == "risk_hold"
    assert step.propensity == 1.0
    assert step.meta["explored"] is False


def test_unknown_hint_samples_and_marks_exploration(tmp_path):
    rec, _, _ = make(tmp_path)
    step = rec.observe(NOW, 100.0, guard_status="ok", action_hint="not-an-action")
    assert step.action in ACTIONS
    assert step.meta["explored"] is True


def test_reward_settlement_uses_markout_and_cap(tmp_path):
    rec, buf, _ = make(tmp_path, cap=10.0)
    step = rec.observe(NOW, 100.0, position=0.1, equity=100.0,
                       action_hint="quote_both")
    rec.on_mark(NOW + 30.0, 101.0)
    settled = rec.settle(NOW + 30.0)
    assert settled == 1
    assert step.reward_30s == pytest.approx(0.1)
    assert buf.all()[0].reward_30s == pytest.approx(0.1)


def test_trim_and_checkpoint(tmp_path):
    rec, buf, policy = make(tmp_path, retention=60.0)
    rec.observe(NOW, 100.0)
    removed = rec.maybe_trim(NOW + 200.0, every_s=0.0)
    assert removed == 1
    assert len(buf) == 0
    path = rec.maybe_checkpoint()
    assert path is not None
    assert (tmp_path / "policy.json").exists()


def test_from_env_is_opt_in(tmp_path):
    assert LearningRecorder.from_env(coin="HYPE", max_notional=11.0,
                                     environ={}) is None
    rec = LearningRecorder.from_env(
        coin="HYPE", max_notional=11.0,
        environ={"MM_LEARNING": "1",
                 "MM_ROLLING_BUFFER": str(tmp_path / "b.jsonl"),
                 "MM_POLICY_PATH": str(tmp_path / "p.json"),
                 "MM_GUARD_STATE": str(tmp_path / "g.json")})
    assert rec is not None
    assert rec.policy_path == tmp_path / "p.json"
