"""Reward alignment: 30 s / 300 s markouts, fees, protection cap."""
from __future__ import annotations

import pytest

from mmbot.learning.buffer import DecisionStep, RollingExperienceBuffer
from mmbot.learning.rewards import RewardTracker, compute_step_reward


def mkstep(ts: float = 0.0) -> DecisionStep:
    return DecisionStep(step_id=0, ts=ts, mono=ts, coin="HYPE",
                        features={}, action="quote_both", propensity=0.5,
                        mid=100.0, position=0.0, equity=100.0)


def test_markout_reward_and_protection_cap():
    assert compute_step_reward(mid_t=100, mid_h=101, position=0.1,
                               realized=0.0, fees=0.0,
                               protection_cap_usd=10.0) == pytest.approx(0.1)
    # Adverse markout is clipped by the protection cap.
    assert compute_step_reward(mid_t=100, mid_h=90, position=0.1,
                               realized=0.0, fees=0.0,
                               protection_cap_usd=0.05) == pytest.approx(-0.05)
    # Fees always subtract, even when the markout is clipped.
    assert compute_step_reward(mid_t=100, mid_h=99, position=0.1,
                               realized=0.0, fees=0.001,
                               protection_cap_usd=10.0) == pytest.approx(-0.101)


def test_tracker_settles_realised_pnl_after_fees(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10_000,
                                  now_fn=lambda: 1000.0)
    step = buf.append(mkstep(0.0))
    tr = RewardTracker(buf, horizons=(30, 300), protection_cap_usd=10.0,
                       now_fn=lambda: 1000.0)
    tr.on_decision(step.step_id, 0.0, 100.0, 0.0, "quote_both")
    tr.on_mark(0.0, 100.0)
    tr.on_fill(10.0, "buy", 100.0, 0.1, 0.001)
    tr.on_fill(20.0, "sell", 101.0, 0.1, 0.001)
    tr.on_mark(30.0, 101.0)
    tr.on_mark(300.0, 102.0)

    out = tr.settle(30.0)
    assert [(sid, h) for sid, h, _ in out] == [(step.step_id, 30)]
    assert step.reward_30s == pytest.approx(0.1 - 0.002)
    assert step.reward_300s is None
    # A single settled horizon is already usable (fresh data trains sooner);
    # the combined reward is refreshed when the longer horizon lands.
    assert step.reward == pytest.approx(0.1 - 0.002)

    out = tr.settle(300.0)
    assert [(h) for _, h, _ in out] == [300]
    assert step.reward_300s == pytest.approx(0.1 - 0.002)
    assert step.reward == pytest.approx(0.098)
    assert tr.pending == 0


def test_tracker_markout_uses_position_at_decision(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10_000,
                                  now_fn=lambda: 1000.0)
    step = buf.append(mkstep(0.0))
    tr = RewardTracker(buf, horizons=(30,), protection_cap_usd=10.0,
                       now_fn=lambda: 1000.0)
    tr.on_decision(step.step_id, 0.0, 100.0, 0.1, "quote_both")
    tr.on_mark(0.0, 100.0)
    tr.on_mark(30.0, 101.0)
    tr.settle(30.0)
    assert step.reward_30s == pytest.approx(0.1)


def test_tracker_waits_for_a_mark_at_the_horizon(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10_000,
                                  now_fn=lambda: 1000.0)
    step = buf.append(mkstep(0.0))
    tr = RewardTracker(buf, horizons=(30,), protection_cap_usd=10.0,
                       now_fn=lambda: 1000.0)
    tr.on_decision(step.step_id, 0.0, 100.0, 0.0, "quote_both")
    tr.on_mark(0.0, 100.0)
    assert tr.settle(30.0) == []
    assert step.reward is None
    tr.on_mark(31.0, 100.5)
    tr.settle(31.0)
    assert step.reward_30s is not None
