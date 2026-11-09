"""Contract tests for the budget-aware quote bands and batch planner."""
from __future__ import annotations

import asyncio

from mmbot.exchange.types import BookEvent
from mmbot.execution.batch_planner import (
    BatchPlanner, BatchParams, Leg, LiveQuote, Payload)
from mmbot.execution.quote_bands import (
    Act, BandParams, EWMARef, QuotePlanner, RequestBudget, calibrate_costs,
    dist_bps, effective_half_spread, target_px)
from mmbot.main import Bot
from mmbot.models import MarketState, MicroState, RiskConfig, StrategyParams


def test_package_imports_reconstruct_direct_execution():
    # batch_planner supports both package and direct execution import styles.
    assert BatchPlanner is not None and Payload is not None
    assert LiveQuote is not None and Leg is not None


def test_band_geometry_defaults():
    b = BandParams()
    assert b.near_edge_bps == 13.0
    assert b.far_edge_bps == 40.0
    assert b.pull_edge_bps == 9.0


def test_ewma_respects_wall_clock_not_tick_count():
    e = EWMARef(tau_s=3.0)
    e.update(100.0, now=0.0)
    v = e.update(110.0, now=3.0)
    assert abs(v - (100.0 + (1 - 2.718281828459045 ** -1) * 10.0)) < 1e-6
    # 1 ms later must barely move
    assert abs(e.update(110.0, now=3.001) - v) < 0.02


def test_budget_defaults_to_96h_horizon():
    b = RequestBudget()
    assert b.horizon_h == 96.0
    assert b.TOKEN_START == 15.0 and b.TOKEN_CAP == 25.0


def test_budget_reserve_allows_only_cleanup():
    b = RequestBudget(reserve=500, horizon_h=24.0, now=lambda: 0.0)
    b.update_from_api({"nRequestsUsed": 19600, "nRequestsCap": 20000, "cumVlm": 1.0})
    assert b.mode == "DEGRADED"          # headroom 400 <= reserve 500
    assert not b.may_send("quote")
    assert b.may_send("cancel")


def test_budget_standdown_below_cancel_floor():
    b = RequestBudget(reserve=500, horizon_h=24.0, now=lambda: 0.0)
    b.update_from_api({"nRequestsUsed": 19950, "nRequestsCap": 20000, "cumVlm": 1.0})
    assert b.mode == "STANDDOWN"         # headroom 50 <= CANCEL_FLOOR 100
    assert not b.may_send("quote")
    assert not b.may_send("cancel")


def test_token_bucket_blocks_quotes_but_not_cancels():
    clock = [0.0]
    b = RequestBudget(reserve=500, horizon_h=96.0, now=lambda: clock[0])
    b.update_from_api({"nRequestsUsed": 0, "nRequestsCap": 10000, "cumVlm": 0.0})
    assert b.may_send("quote", 1)
    for _ in range(15):
        b.record(1)
    # bucket drained at a frozen clock
    assert not b.may_send("quote", 1)
    assert b.may_send("cancel", 1)          # protective pulls still allowed
    # one hour of continuous refill restores a full burst
    clock[0] += 3600.0
    assert b.may_send("quote", 1)
    assert b._tokens > 15.0


def test_budget_record_is_monotonic_against_lagging_api():
    clock = [0.0]
    b = RequestBudget(now=lambda: clock[0])
    b.update_from_api({"nRequestsUsed": 10, "nRequestsCap": 10000, "cumVlm": 0.0})
    b.record(3)
    assert b.nRequestsUsed == 13
    # a stale poll must not roll the optimistic counter backwards
    b.update_from_api({"nRequestsUsed": 11, "nRequestsCap": 10000, "cumVlm": 0.0})
    assert b.nRequestsUsed == 13


def test_degraded_planner_turns_near_quote_into_cancel():
    b = RequestBudget(reserve=500, horizon_h=24.0, now=lambda: 0.0)
    # below reserve -> quotes refused, cancels allowed
    b.update_from_api({"nRequestsUsed": 19600, "nRequestsCap": 20000, "cumVlm": 1.0})
    bp = BatchPlanner(b, BandParams(), charge_mode="call", now=lambda: 0.0)
    ref = 100.0
    live = {"buy": LiveQuote(7, target_px("buy", ref, 8.0), 0.15, 0.0), "sell": None}
    payloads = bp.plan(live, ref, {"buy": 0.15, "sell": 0.15})
    assert [p.kind for p in payloads] == ["cancel"]
    assert all(l.why == "pull" for l in payloads[0].legs)


def test_near_and_far_snap():
    b = RequestBudget(reserve=500, horizon_h=24.0, now=lambda: 0.0)
    b.update_from_api({"nRequestsUsed": 0, "nRequestsCap": 20000, "cumVlm": 1.0})
    qp = QuotePlanner(b, BandParams(), now=lambda: 0.0)
    ref = 100.0
    act, px = qp.decide("buy", target_px("buy", ref, 14.0), ref)
    assert act is Act.MOVE_NEAR and abs(dist_bps("buy", px, ref) - 13.0) < 1e-6
    act, px = qp.decide("buy", target_px("buy", ref, 36.0), ref)
    assert act is Act.MOVE_FAR and abs(dist_bps("buy", px, ref) - 40.0) < 1e-6
    act, _ = qp.decide("buy", target_px("buy", ref, 5.0), ref)
    assert act is Act.PULL


def test_calibration_verdicts():
    assert calibrate_costs(100, 101, 2) == "call"
    assert calibrate_costs(100, 102, 2) == "leg"
    assert calibrate_costs(100, 100, 2) == "unknown"
    assert calibrate_costs(100, 105, 2) == "mixed"


def test_effective_half_spread_widens_smoothly():
    assert effective_half_spread(20.0, 10.0, 100.0) == 20.0
    heavy = effective_half_spread(20.0, 200.0, 100.0)
    assert 20.0 < heavy <= 40.0


def _fresh_budget(clock):
    b = RequestBudget(reserve=500, horizon_h=96.0, now=lambda: clock[0])
    b.update_from_api({"nRequestsUsed": 0, "nRequestsCap": 10000, "cumVlm": 0.0})
    return b


def test_planner_places_both_sides_when_empty():
    clock = [0.0]
    bp = BatchPlanner(_fresh_budget(clock), BandParams(), charge_mode="call",
                      now=lambda: clock[0])
    payloads = bp.plan({}, 100.0, {"buy": 0.15, "sell": 0.15})
    assert len(payloads) == 1
    assert payloads[0].kind == "place"
    assert {l.side for l in payloads[0].legs} == {"buy", "sell"}


def test_planner_inventory_cap_emits_protective_cancel():
    clock = [0.0]
    bp = BatchPlanner(_fresh_budget(clock), BandParams(), charge_mode="call",
                      now=lambda: clock[0])
    ref = 100.0
    live = {"buy": LiveQuote(7, target_px("buy", ref, 20.0), 0.15, 0.0), "sell": None}
    # buy size 0 => side disabled at the one-sided inventory cap
    payloads = bp.plan(live, ref, {"buy": 0.0, "sell": 0.15})
    kinds = [p.kind for p in payloads]
    assert "cancel" in kinds
    cancel = next(p for p in payloads if p.kind == "cancel")
    assert any(l.side == "buy" and l.why == "pull" for l in cancel.legs)


def test_planner_skips_in_flight_sides():
    clock = [0.0]
    bp = BatchPlanner(_fresh_budget(clock), BandParams(), charge_mode="call",
                      now=lambda: clock[0])
    ref = 100.0
    live = {"buy": LiveQuote(7, target_px("buy", ref, 8.0), 0.15, 0.0), "sell": None}
    payloads = bp.plan(live, ref, {"buy": 0.15, "sell": 0.15},
                       skip=frozenset({"buy"}))
    assert not any(any(l.side == "buy" for l in p.legs) for p in payloads)


def test_planner_near_leg_is_necessary_modify():
    clock = [0.0]
    bp = BatchPlanner(_fresh_budget(clock), BandParams(), charge_mode="call",
                      now=lambda: clock[0])
    ref = 100.0
    live = {"buy": LiveQuote(7, target_px("buy", ref, 10.0), 0.15, 0.0), "sell": None}
    payloads = bp.plan(live, ref, {"buy": 0.15, "sell": 0.15})
    modify = next(p for p in payloads if p.kind == "modify")
    assert any(l.why == "near" and l.necessary for l in modify.legs)


def test_rate_per_h_uses_600s_span_floor():
    clock = [0.0]
    b = _fresh_budget(clock)
    bp = BatchPlanner(b, BandParams(), charge_mode="call", now=lambda: clock[0])
    bp.on_sent(Payload("place", [Leg("buy", "place", 99.0, 0.1, True, "place")]))
    clock[0] = 1.0
    bp.on_sent(Payload("place", [Leg("sell", "place", 101.0, 0.1, True, "place")]))
    # 2 calls over the 600 s floor -> 12/h, not 3600/h
    assert abs(bp.rate_per_h() - 12.0) < 1e-6


def test_budget_degraded_action_backs_off_before_replacing():
    clock = [0.0]
    b = _fresh_budget(clock)
    for _ in range(15):                       # drain the burst bucket
        b.record(1)
    bp = BatchPlanner(b, BandParams(), charge_mode="call", now=lambda: clock[0])
    ref = 100.0
    live = {"buy": LiveQuote(7, target_px("buy", ref, 10.0), 0.15, 0.0), "sell": None}
    p1 = bp.plan(live, ref, {"buy": 0.15, "sell": 0.15})
    assert any(p.kind == "cancel" for p in p1)          # degraded to protective pull
    for p in p1:
        bp.on_sent(p)
    clock[0] = 5.0
    p2 = bp.plan({"buy": None, "sell": None}, ref, {"buy": 0.15, "sell": 0.15})
    assert not any(any(l.side == "buy" for l in p.legs) for p in p2)
    clock[0] = 61.0                            # backoff elapsed + bucket refilled
    p3 = bp.plan({"buy": None, "sell": None}, ref, {"buy": 0.15, "sell": 0.15})
    assert any(any(l.side == "buy" and l.kind == "place" for l in p.legs) for p in p3)


class _NullGw:
    pass


def test_toxicity_debounce_and_hysteresis():
    bot = Bot(_NullGw(), StrategyParams(coin="HYPE"), RiskConfig())

    def book(obi: float) -> BookEvent:
        return BookEvent(MarketState("HYPE", 100.0, 100.1, 1.0, 1.0, 0.0),
                         MicroState(obi_nw=obi))

    # two toxic snapshots are not enough (confirm_n = 3)
    for _ in range(2):
        asyncio.run(bot.handle_event(book(-0.9)))
    assert bot._micro_urgent is False
    asyncio.run(bot.handle_event(book(-0.9)))
    assert bot._micro_urgent is True
    assert bot.metrics["tox_events"] == 1

    bot._micro_urgent = False                  # cycle consumes the trigger
    # chatter must not re-trigger while unarmed
    for obi in (-0.9, 0.0, -0.9, 0.0):
        asyncio.run(bot.handle_event(book(obi)))
    assert bot._micro_urgent is False

    # clear_n = 10 clean snapshots re-arm, then 3 sustained toxic snapshots fire
    for _ in range(10):
        asyncio.run(bot.handle_event(book(0.0)))
    for _ in range(3):
        asyncio.run(bot.handle_event(book(-0.9)))
    assert bot._micro_urgent is True
    assert bot.metrics["tox_events"] == 2
