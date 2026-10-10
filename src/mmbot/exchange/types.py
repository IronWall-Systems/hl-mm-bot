"""Gateway-facing data types (kept separate so pure modules never import the SDK)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..models import MarketState, Side


class PlaceKind(str, Enum):
    RESTING = "resting"
    FILLED = "filled"              # an Alo should never fill instantly; handled anyway
    REJECT_CROSS = "reject_cross"  # post-only would have matched -> expected, benign
    REJECT_OTHER = "reject_other"  # margin, tick size, min notional, rate limit, ...
    ERROR = "error"                # transport/exception: order state UNKNOWN


@dataclass
class PlaceResult:
    kind: PlaceKind
    oid: Optional[int] = None
    error: Optional[str] = None
    raw: Any = None
    latency_ms: float = 0.0


@dataclass
class CancelResult:
    ok: bool
    already_gone: bool = False
    error: Optional[str] = None
    latency_ms: float = 0.0


@dataclass
class RemoteOrder:
    coin: str
    oid: int
    side: Side
    price: float
    size: float
    cloid: Optional[str] = None


# ---- events delivered on gateway.events -------------------------------------
@dataclass
class BookEvent:
    market: MarketState


@dataclass
class FillEvent:
    coin: str
    side: Side
    px: float
    sz: float
    oid: Optional[int]
    tid: Optional[int]
    fee: float = 0.0
    time: float = 0.0
    is_snapshot: bool = False
    cloid: Optional[str] = None


@dataclass
class OrderUpdateEvent:
    oid: Optional[int]
    cloid: Optional[str]
    status: str           # exchange status string: open | filled | canceled | rejected | ...Canceled
    side: Optional[Side] = None
    px: float = 0.0
    sz: float = 0.0


@dataclass
class FeedDownEvent:
    reason: str
