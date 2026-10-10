"""Hyperliquid perp tick/lot rules (docs: for-developers/api/tick-and-lot-size).

Price: <=5 significant figures AND <= (6 - szDecimals) decimals; integer prices always valid.
Size : rounded to szDecimals (we always round DOWN so we never exceed intended notional).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

PERP_MAX_DECIMALS = 6
SIG_FIGS = 5


@dataclass(frozen=True)
class AssetSpec:
    coin: str
    sz_decimals: int


def px_decimals(price: float, sz_decimals: int) -> int:
    allowed = max(0, PERP_MAX_DECIMALS - sz_decimals)
    if price <= 0:
        return allowed
    sig_dec = SIG_FIGS - 1 - math.floor(math.log10(price))
    return max(0, min(allowed, sig_dec))


def tick_at(price: float, sz_decimals: int) -> float:
    return 10.0 ** (-px_decimals(price, sz_decimals))


def round_px(price: float, sz_decimals: int, mode: str) -> float:
    """mode: 'floor' (bids) or 'ceil' (asks)."""
    d = px_decimals(price, sz_decimals)
    q = Decimal(1).scaleb(-d)
    rounding = ROUND_FLOOR if mode == "floor" else ROUND_CEILING
    return float(Decimal(str(price)).quantize(q, rounding=rounding))


def round_sz(size: float, sz_decimals: int) -> float:
    q = Decimal(1).scaleb(-sz_decimals)
    return float(Decimal(str(size)).quantize(q, rounding=ROUND_FLOOR))
