"""Policy: propensities, KL, gradient updates, JSON checkpoint round-trip."""
from __future__ import annotations

import numpy as np
import pytest

from mmbot.learning.features import FEATURE_NAMES
from mmbot.learning.policy import ACTIONS, ACTION_INDEX, SoftmaxLinearPolicy


def make_policy() -> SoftmaxLinearPolicy:
    p = SoftmaxLinearPolicy(n_features=len(FEATURE_NAMES))
    a, f = len(ACTIONS), len(FEATURE_NAMES)
    p.weights = (np.arange(a * f, dtype=float).reshape(a, f) - 5.0) / 50.0
    p.bias = np.linspace(-0.1, 0.1, a)
    return p


def test_probs_sum_to_one_and_propensity_matches():
    p = make_policy()
    x = [0.1 * (i + 1) for i in range(len(FEATURE_NAMES))]
    probs = p.probs(x)[0]
    assert probs.sum() == pytest.approx(1.0)
    for action in ACTIONS:
        assert p.log_prob(x, action) == pytest.approx(
            float(np.log(probs[ACTION_INDEX[action]])))
    rng = np.random.default_rng(0)
    action, prop = p.sample(x, rng)
    assert action in ACTIONS
    assert prop == pytest.approx(float(probs[ACTION_INDEX[action]]))


def test_checkpoint_round_trip(tmp_path):
    p = make_policy()
    p.trained_steps = 123
    path = tmp_path / "policy.json"
    p.save(path)
    q = SoftmaxLinearPolicy.load(path)
    assert q.actions == p.actions
    assert q.feature_names == p.feature_names
    assert np.allclose(q.weights, p.weights)
    assert np.allclose(q.bias, p.bias)
    assert q.trained_steps == 123
    # The checkpoint is plain JSON (diffable, no pickle).
    assert '"schema"' in path.read_text()


def test_vector_scales_and_clips_raw_features():
    from mmbot.learning.features import vector

    x = vector({"spread_bps": 445.0, "obi_nw": 2.0, "mid_ret_1s": -100.0})
    assert x[FEATURE_NAMES.index("spread_bps")] == pytest.approx(445.0 / 50.0)
    assert x[FEATURE_NAMES.index("obi_nw")] == pytest.approx(1.0)
    assert x[FEATURE_NAMES.index("mid_ret_1s")] == pytest.approx(-5.0)
    assert vector({}) == [0.0] * len(FEATURE_NAMES)


def test_kl_is_zero_for_self_and_positive_after_update():
    p = make_policy()
    pad = [0.0] * (len(FEATURE_NAMES) - 2)
    xs = np.asarray([[0.1, 0.2] + pad, [0.3, -0.1] + pad])
    assert p.kl_to(p.clone(), xs) == pytest.approx(0.0, abs=1e-12)
    q = p.clone()
    q.bias[0] += 1.0
    assert p.kl_to(q, xs) > 0.0


def test_gradient_ascent_increases_chosen_action_probability():
    p = make_policy()
    x = [0.2] * len(FEATURE_NAMES)
    before = p.probs(x)[0][ACTION_INDEX["quote_both"]]
    gw, gb = p.grad_log_prob(x, "quote_both")
    p.apply_gradient(-gw, -gb, 0.1)          # ascend log pi(a|x)
    after = p.probs(x)[0][ACTION_INDEX["quote_both"]]
    assert after > before
