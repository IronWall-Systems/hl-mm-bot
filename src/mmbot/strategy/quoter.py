"""Pure quoting engine. No I/O, no clocks, no globals: (market, inventory, params) -> targets.

Lean 3-signal reservation price (PREREG2_MICROSTRUCTURE_UPGRADE_20261107):

    r = mid * (1 + (s_inv + w_obi*obi_nw + w_tfi*tfi2 + w_btc*lead_btc_1s) / 1e4)
    bid = floor_tick(r * (1 - h))     ask = ceil_tick(r * (1 + h))

``s_inv`` is the existing inventory skew in bps.  Only the statistically
passing signals from ``docs/feature_study_20261107.md`` are admitted:
``obi_nw``, ``tfi2`` and ``lead_btc_1s``.  ``ret5m``, ``vol_wick`` and the
6-TF indicators remain excluded.

Toxicity gate: when opposing micro-flow is toxic for a resting side
(``|obi_nw|`` / ``|tfi2|`` beyond the configured thresholds, or an adverse
BTC 1 s move), that leg is widened by ``tox_widen_mult``.  If a verified
``nSigFigs=4`` wall sits 12-22 bps away, the leg snaps one tick inside it
instead.  The other leg is untouched.

Then the Alo clamp keeps bid <= best_ask - tick and ask >= best_bid + tick so a
post-only order is never submitted on the wrong side of the book we last saw.
"""
from __future__ import annotations

from typing import List, Optional

from ..models import MarketState, MicroState, Quote, Side, StrategyParams
from ..precision import AssetSpec, round_px, round_sz, tick_at


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def signal_shift_bps(micro: Optional[MicroState], p: StrategyParams) -> float:
    """Reservation-price shift from the three validated signals, in bps."""
    if micro is None or not p.micro_enabled:
        return 0.0
    btc = micro.lead_btc_1s if micro.btc_ready else 0.0
    return (p.w_obi_bps * micro.obi_nw
            + p.w_tfi_bps * micro.tfi2
            + p.w_btc_bps * btc)


def toxic_sides(micro: Optional[MicroState], p: StrategyParams) -> frozenset:
    """Sides whose resting quotes are exposed to adverse opposing flow."""
    if micro is None or not p.micro_enabled:
        return frozenset()
    out = set()
    btc = micro.lead_btc_1s if micro.btc_ready else 0.0
    # Selling pressure is toxic to our resting bid; buying pressure to our ask.
    if micro.obi_nw <= -p.tox_obi or micro.tfi2 <= -p.tox_tfi or btc <= -p.tox_btc_bps:
        out.add(Side.BUY)
    if micro.obi_nw >= p.tox_obi or micro.tfi2 >= p.tox_tfi or btc >= p.tox_btc_bps:
        out.add(Side.SELL)
    return frozenset(out)


def _wall_price(side: Side, micro: Optional[MicroState], p: StrategyParams,
                spec: AssetSpec) -> Optional[float]:
    """One tick inside a verified nSigFigs=4 wall, only inside the 12-22 bps band."""
    if micro is None:
        return None
    if side is Side.BUY:
        px, dist = micro.wall_bid_px, micro.wall_bid_bps
    else:
        px, dist = micro.wall_ask_px, micro.wall_ask_bps
    if px is None or dist is None or not (p.wall_min_bps <= dist <= p.wall_max_bps):
        return None
    tick = tick_at(px, spec.sz_decimals)
    return px + tick if side is Side.BUY else px - tick


def _leg(m: MarketState, r: float, side: Side, width_bps: float,
         micro: Optional[MicroState], p: StrategyParams, spec: AssetSpec,
         snap_wall: bool = False) -> float:
    width = width_bps / 1e4
    if side is Side.BUY:
        px = round_px(r * (1.0 - width), spec.sz_decimals, "floor")
        wall = _wall_price(side, micro, p, spec) if snap_wall else None
        if wall is not None:
            px = round_px(wall, spec.sz_decimals, "floor")
        ceiling = round_px(m.ask - tick_at(m.ask, spec.sz_decimals), spec.sz_decimals, "floor")
        return min(px, ceiling)
    px = round_px(r * (1.0 + width), spec.sz_decimals, "ceil")
    wall = _wall_price(side, micro, p, spec) if snap_wall else None
    if wall is not None:
        px = round_px(wall, spec.sz_decimals, "ceil")
    floor_ = round_px(m.bid + tick_at(m.bid, spec.sz_decimals), spec.sz_decimals, "ceil")
    return max(px, floor_)


def compute_quotes(m: MarketState, pos_size: float, p: StrategyParams, spec: AssetSpec,
                   micro: Optional[MicroState] = None) -> List[Quote]:
    if not m.is_valid:
        return []
    mid = m.mid
    pos_n = pos_size * mid
    hs = p.half_spread_bps
    inv_skew_bps = -p.skew_bps * _clip(pos_n / p.max_notional, -1.0, 1.0)
    r = mid * (1.0 + (inv_skew_bps + signal_shift_bps(micro, p)) / 1e4)
    toxic = toxic_sides(micro, p)
    out: List[Quote] = []

    if pos_n < p.max_notional:
        buy_toxic = Side.BUY in toxic
        width = hs * (p.tox_widen_mult if buy_toxic else 1.0)
        px = _leg(m, r, Side.BUY, width, micro, p, spec, snap_wall=buy_toxic)
        if px > 0:
            sz = round_sz(p.quote_notional / px, spec.sz_decimals)
            if sz > 0 and sz * px >= p.min_order_notional:
                out.append(Quote(Side.BUY, px, sz))

    if pos_n > -p.max_notional:
        sell_toxic = Side.SELL in toxic
        width = hs * (p.tox_widen_mult if sell_toxic else 1.0)
        px = _leg(m, r, Side.SELL, width, micro, p, spec, snap_wall=sell_toxic)
        if px > 0:
            sz = round_sz(p.quote_notional / px, spec.sz_decimals)
            if sz > 0 and sz * px >= p.min_order_notional:
                out.append(Quote(Side.SELL, px, sz))
    return out
