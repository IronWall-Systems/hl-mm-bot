"""Pure quoting engine. No I/O, no clocks, no globals: (market, inventory, params) -> targets.

Same formula as the backtester:
    shift = skew_bps/1e4 * mid * clip(pos_notional / max_notional, -1, 1)
    bid   = floor_to_tick(mid*(1-hs) - shift)     ask = ceil_to_tick(mid*(1+hs) - shift)
Long inventory shifts BOTH quotes down (cheaper bid, easier-to-hit ask), short shifts up.
Then the Alo clamp keeps bid <= best_ask - tick and ask >= best_bid + tick so a post-only
order is never submitted on the wrong side of the book we last saw.
"""
from __future__ import annotations

from typing import List

from ..models import MarketState, Quote, Side, StrategyParams
from ..precision import AssetSpec, round_px, round_sz, tick_at


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_quotes(m: MarketState, pos_size: float, p: StrategyParams, spec: AssetSpec) -> List[Quote]:
    if not m.is_valid:
        return []
    mid = m.mid
    pos_n = pos_size * mid
    hs = p.half_spread_bps / 1e4
    shift = p.skew_bps / 1e4 * mid * _clip(pos_n / p.max_notional, -1.0, 1.0)
    out: List[Quote] = []

    if pos_n < p.max_notional:
        px = round_px(mid * (1 - hs) - shift, spec.sz_decimals, "floor")
        ceiling = round_px(m.ask - tick_at(m.ask, spec.sz_decimals), spec.sz_decimals, "floor")
        px = min(px, ceiling)
        if px > 0:
            sz = round_sz(p.quote_notional / px, spec.sz_decimals)
            if sz > 0 and sz * px >= p.min_order_notional:
                out.append(Quote(Side.BUY, px, sz))

    if pos_n > -p.max_notional:
        px = round_px(mid * (1 + hs) - shift, spec.sz_decimals, "ceil")
        floor_ = round_px(m.bid + tick_at(m.bid, spec.sz_decimals), spec.sz_decimals, "ceil")
        px = max(px, floor_)
        if px > 0:
            sz = round_sz(p.quote_notional / px, spec.sz_decimals)
            if sz > 0 and sz * px >= p.min_order_notional:
                out.append(Quote(Side.SELL, px, sz))
    return out
