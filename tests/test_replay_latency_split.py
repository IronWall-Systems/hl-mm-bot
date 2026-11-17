"""Split-latency replay model: feed transit, fast place/modify, slow cancel.

The old engine applied one ``latency_ms`` to every action *and* reused it as
``feed_age_ms``.  These tests pin the decoupled timeline used for PREREG2 v3
profiles.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from mmbot.precision import AssetSpec  # noqa: E402
import replay_backtest as rbt  # noqa: E402
import run_strict_engine as rse  # noqa: E402


SPEC = AssetSpec("BTC", 5)


def engine(**kw):
    base = dict(latency_ms=410.0, feed_age_ms=10.0,
                place_modify_latency_ms=410.0, cancel_latency_ms=686.0)
    base.update(kw)
    return rbt.ReplayEngine(SPEC, **base)


def bbo(t_ms: int, bid=70000.0, ask=70001.0, **kw):
    rec = {"e": "bbo", "t": t_ms, "b": [bid, 1.0], "a": [ask, 1.0]}
    rec.update(kw)
    return rec


def pending_of(eng, kind):
    return [(eff, p) for eff, _seq, k, p in eng.pending if k == kind]


def test_defaults_remain_backward_compatible():
    eng = rbt.ReplayEngine(SPEC, latency_ms=410.0, feed_age_ms=10.0)
    assert eng.place_modify_latency_s == pytest.approx(0.410)
    assert eng.cancel_latency_s == pytest.approx(0.410)
    assert eng.feed_age_s == pytest.approx(0.010)


def test_ready_times_are_independent():
    eng = engine()
    seen = eng.seen_time(100.0)
    assert seen == pytest.approx(100.010)
    assert eng.place_ready_time(seen) == pytest.approx(100.420)
    assert eng.cancel_ready_time(seen) == pytest.approx(100.696)
    # Risk-off pull: raw cancel by default, fast OTM park when enabled.
    assert eng.park_ready_time(seen) == pytest.approx(100.696)
    fast = engine(park_via_modify=True)
    assert fast.park_ready_time(seen) == pytest.approx(100.420)


def test_feed_transit_is_not_the_action_latency():
    eng = engine(feed_age_ms=10.0, place_modify_latency_ms=410.0, cancel_latency_ms=686.0)
    eng.on_record(bbo(100_000))
    places = pending_of(eng, "place")
    assert len(places) == 2
    # 100.000 s + 10 ms feed + 410 ms place/modify -- not 2 * 410 ms.
    assert all(eff == pytest.approx(100.420) for eff, _ in places)
    assert eng.current_feed_age_s == pytest.approx(0.010)


def test_recorded_receive_timestamp_sets_feed_age():
    eng = engine()
    # collector v2 record: exchange ts 100000 ms, local receive 100007 ms.
    eng.on_record(bbo(100_000, r=100_007))
    assert eng.current_feed_age_s == pytest.approx(0.007)
    places = pending_of(eng, "place")
    assert all(eff == pytest.approx(100.417) for eff, _ in places)


def test_fixed_feed_age_ignores_recorded_receive_timestamp():
    eng = engine(feed_age_from_record=False)
    # Archive r-t is the collector path (~hundreds of ms), not warm Tokyo transit.
    eng.on_record(bbo(100_000, r=100_400))
    assert eng.current_feed_age_s == pytest.approx(0.010)
    places = pending_of(eng, "place")
    assert all(eff == pytest.approx(100.420) for eff, _ in places)


def test_requote_uses_fast_path_and_raw_pull_uses_cancel_latency(monkeypatch):
    eng = engine()
    eng.on_record(bbo(100_000))
    eng._advance_to(101.0)                         # places become working orders
    assert eng.working["buy"] is not None and eng.working["sell"] is not None
    eng.pending = []
    eng.pending_place = {"buy": None, "sell": None}
    eng.pending_cancel = {"buy": False, "sell": False}

    # No targets -> both sides are risk-off pulls: slow cancel path.
    monkeypatch.setattr(rbt, "compute_quotes", lambda *a, **k: [])
    eng._maybe_decide(200.0)
    cancels = pending_of(eng, "cancel")
    assert len(cancels) == 2
    assert all(eff == pytest.approx(200.696) for eff, _ in cancels)

    # With fast OTM park, the same pull is effective after 410 ms.
    eng.pending = []
    eng.pending_cancel = {"buy": False, "sell": False}
    fast = engine(park_via_modify=True)
    fast.working = dict(eng.working)
    monkeypatch.setattr(rbt, "compute_quotes", lambda *a, **k: [])
    fast._maybe_decide(200.0)
    assert all(eff == pytest.approx(200.420) for eff, _ in pending_of(fast, "cancel"))


def test_requote_pair_uses_place_modify_path(monkeypatch):
    from mmbot.models import Quote, Side

    eng = engine()
    eng.on_record(bbo(100_000))
    eng._advance_to(101.0)
    eng.pending = []
    eng.pending_place = {"buy": None, "sell": None}
    eng.pending_cancel = {"buy": False, "sell": False}

    moved = [Quote(Side.BUY, 69900.0, 0.002), Quote(Side.SELL, 70100.0, 0.002)]
    monkeypatch.setattr(rbt, "compute_quotes", lambda *a, **k: moved)
    eng._maybe_decide(200.0)

    # A requote is a batchModify in production: both the pull and the new leg
    # land on the fast place/modify path, not the slow cancel path.
    assert all(eff == pytest.approx(200.420) for eff, _ in pending_of(eng, "cancel"))
    assert all(eff == pytest.approx(200.420) for eff, _ in pending_of(eng, "place"))


def test_strict_builder_wires_split_timings():
    clock = [0.0]
    eng = rse.build_engine(
        SPEC, 410.0, 535.0, True, clock,
        feed_age_ms=10.0, feed_age_from_record=False,
        place_modify_latency_ms=410.0,
        cancel_latency_ms=686.0, park_via_modify=True,
    )
    assert eng.feed_age_s == pytest.approx(0.010)
    assert eng.place_modify_latency_s == pytest.approx(0.410)
    assert eng.cancel_latency_s == pytest.approx(0.686)
    assert eng.park_via_modify is True
    assert eng.p95_latency_s == pytest.approx(0.535)
