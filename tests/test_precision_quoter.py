from mmbot.models import MarketState, Side, StrategyParams
from mmbot.precision import AssetSpec, px_decimals, round_px, round_sz, tick_at
from mmbot.strategy.quoter import compute_quotes

BTC = AssetSpec("BTC", 5)
P = StrategyParams(quote_notional=20, max_notional=60, half_spread_bps=4, skew_bps=6)


def mkt(bid, ask):
    return MarketState("BTC", bid, ask, 5, 5, 0.0)


def test_precision_rules():
    assert px_decimals(70000.4, 5) == 0 and tick_at(70000.4, 5) == 1.0   # 5 sig figs -> $1 tick on BTC
    assert px_decimals(1234.5, 4) == 1                                     # docs: 1234.5 ok, 1234.56 not
    assert px_decimals(0.001234, 0) == 6
    assert round_px(70000.9, 5, "floor") == 70000 and round_px(70000.1, 5, "ceil") == 70001
    assert round_sz(0.000285714, 5) == 0.00028


def test_symmetric_when_flat():
    q = {x.side: x for x in compute_quotes(mkt(69999, 70001), 0.0, P, BTC)}
    mid = 70000
    assert q[Side.BUY].price <= mid * (1 - 4e-4) and q[Side.SELL].price >= mid * (1 + 4e-4)
    assert abs((mid - q[Side.BUY].price) - (q[Side.SELL].price - mid)) <= 2   # equal up to tick rounding


def test_long_inventory_shifts_down():
    flat = {x.side: x.price for x in compute_quotes(mkt(69999, 70001), 0.0, P, BTC)}
    long_ = {x.side: x.price for x in compute_quotes(mkt(69999, 70001), 30 / 70000, P, BTC)}
    assert long_[Side.BUY] < flat[Side.BUY] and long_[Side.SELL] < flat[Side.SELL]


def test_inventory_gating():
    at_cap = compute_quotes(mkt(69999, 70001), 60 / 70000, P, BTC)
    assert [x.side for x in at_cap] == [Side.SELL]                          # no more buying
    at_neg = compute_quotes(mkt(69999, 70001), -60 / 70000, P, BTC)
    assert [x.side for x in at_neg] == [Side.BUY]


def test_alo_clamp_never_crosses():
    tight = mkt(70000, 70001)                                               # 1-tick spread
    p = StrategyParams(quote_notional=20, max_notional=60, half_spread_bps=0.1, skew_bps=0)
    q = {x.side: x.price for x in compute_quotes(tight, 0.0, p, BTC)}
    assert q[Side.BUY] <= 70000 and q[Side.SELL] >= 70001                   # inside-or-at touch, never through
    # with heavy skew pushing bid up through the ask, clamp still holds
    p2 = StrategyParams(quote_notional=20, max_notional=60, half_spread_bps=0.1, skew_bps=50)
    q2 = {x.side: x.price for x in compute_quotes(tight, -60 / 70000 * 0.99, p2, BTC)}
    assert q2[Side.BUY] < 70001


def test_min_notional_and_invalid_book():
    p = StrategyParams(quote_notional=5, min_order_notional=10)
    assert compute_quotes(mkt(69999, 70001), 0.0, p, BTC) == []
    assert compute_quotes(mkt(70001, 70000), 0.0, P, BTC) == []
