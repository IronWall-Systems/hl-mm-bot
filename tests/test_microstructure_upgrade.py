"""Lean 3-signal microstructure pricing + toxicity gate (PREREG2 upgrade)."""
from __future__ import annotations

from mmbot.models import MarketState, MicroState, Side, StrategyParams
from mmbot.precision import AssetSpec
from mmbot.strategy.quoter import compute_quotes, signal_shift_bps, toxic_sides

SPEC = AssetSpec("HYPE", 2)
P = StrategyParams(coin="HYPE", half_spread_bps=5.5, skew_bps=3.0,
                   quote_notional=12, max_notional=24, min_order_notional=10,
                   micro_enabled=True)


def mkt(bid=79.000, ask=79.002):
    return MarketState("HYPE", bid, ask, 100.0, 100.0, 0.0)


def prices(micro, p=P):
    return {q.side: q.price for q in compute_quotes(mkt(), 0.0, p, SPEC, micro)}


def test_signal_shift_uses_only_passing_features():
    m = MicroState(obi_nw=0.5, tfi2=0.2, lead_btc_1s=1.0, btc_ready=True)
    want = 1.5 * 0.5 + 1.0 * 0.2 + 0.7 * 1.0
    assert abs(signal_shift_bps(m, P) - want) < 1e-12


def test_positive_flow_shifts_both_quotes_up():
    clean = prices(MicroState())
    bull = prices(MicroState(obi_nw=0.8, tfi2=0.5, lead_btc_1s=1.0, btc_ready=True))
    assert bull[Side.BUY] > clean[Side.BUY]
    assert bull[Side.SELL] > clean[Side.SELL]


def test_btc_lead_ignored_until_ready():
    m = MicroState(lead_btc_1s=50.0, btc_ready=False)
    assert signal_shift_bps(m, P) == 0.0


def test_toxicity_only_widens_vulnerable_leg():
    clean = prices(MicroState())
    toxic_bid = prices(MicroState(obi_nw=-0.9, tfi2=-0.1))
    assert toxic_bid[Side.BUY] < clean[Side.BUY]
    # The ask is not the vulnerable leg: widening the bid must not move it
    # (the reservation skew is identical when compared against the same signal).
    no_gate = P.__class__(**{**P.__dict__, "tox_obi": 2.0, "tox_tfi": 2.0})
    same_signal = prices(MicroState(obi_nw=-0.9, tfi2=-0.1), no_gate)
    assert toxic_bid[Side.SELL] == same_signal[Side.SELL]


def test_toxicity_sides_direction():
    assert toxic_sides(MicroState(obi_nw=-0.5), P) == frozenset({Side.BUY})
    assert toxic_sides(MicroState(tfi2=0.7), P) == frozenset({Side.SELL})
    assert toxic_sides(MicroState(lead_btc_1s=-4.0, btc_ready=True), P) == frozenset({Side.BUY})
    assert toxic_sides(MicroState(), P) == frozenset()


def test_verified_wall_snaps_one_tick_inside():
    m = MicroState(tfi2=-0.9, wall_bid_px=78.920, wall_bid_bps=15.0,
                   wall_ask_px=None, wall_ask_bps=None)
    q = prices(m)
    assert q[Side.BUY] == 78.921


def test_wall_outside_band_is_ignored():
    inside = prices(MicroState(tfi2=-0.9, wall_bid_px=78.920, wall_bid_bps=15.0))
    outside = prices(MicroState(tfi2=-0.9, wall_bid_px=78.700, wall_bid_bps=38.0))
    assert inside[Side.BUY] != outside[Side.BUY]


def test_micro_disabled_matches_legacy_pricing():
    p = P.__class__(**{**P.__dict__, "micro_enabled": False})
    q = prices(MicroState(obi_nw=-0.9, tfi2=-0.9, btc_ready=True, lead_btc_1s=-9.0), p)
    legacy = {x.side: x.price for x in compute_quotes(mkt(), 0.0, p, SPEC)}
    assert q == legacy
