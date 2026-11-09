from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def is_buy(self) -> bool:
        return self is Side.BUY


class OrderStatus(str, Enum):
    PENDING_NEW = "pending_new"         # sent, no ack yet
    RESTING = "resting"
    PENDING_MODIFY = "pending_modify"   # batchModify sent, no ack yet (old order may still rest)
    PENDING_CANCEL = "pending_cancel"   # cancel sent, no ack yet
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


ACTIVE_STATUSES = frozenset({OrderStatus.PENDING_NEW, OrderStatus.RESTING,
                             OrderStatus.PENDING_MODIFY, OrderStatus.PENDING_CANCEL})


@dataclass(frozen=True)
class MarketState:
    coin: str
    bid: float
    ask: float
    bid_sz: float
    ask_sz: float
    recv_ts: float          # local monotonic seconds when received
    exch_ts: float = 0.0    # exchange timestamp, seconds

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4 if self.mid > 0 else float("inf")

    @property
    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > self.bid


@dataclass
class PositionState:
    size: float = 0.0              # signed, base units
    entry_px: float = 0.0
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    account_value: float = 0.0

    @property
    def margin_utilization(self) -> float:
        return self.margin_used / self.account_value if self.account_value > 0 else 1.0

    def notional(self, mid: float) -> float:
        return self.size * mid


@dataclass(frozen=True)
class MicroState:
    """Zero-look-ahead microstructure snapshot used by the lean 3-signal quoter.

    ``obi_nw`` is the top-5 L2 size imbalance weighted by ``sz * log1p(n)``;
    ``tfi2`` is the 2 s aggressive taker-flow imbalance; ``lead_btc_1s`` is the
    BTC 1 s mid return in bps.  ``wall_*`` are verified ``nSigFigs=4`` walls
    (``n >= 5`` and ``sz >= 3x`` median) already filtered to 12-22 bps.
    All values are computed from data received at or before the decision time.
    """
    obi_nw: float = 0.0
    tfi2: float = 0.0
    lead_btc_1s: float = 0.0
    wall_bid_px: Optional[float] = None
    wall_ask_px: Optional[float] = None
    wall_bid_bps: Optional[float] = None
    wall_ask_bps: Optional[float] = None
    btc_ready: bool = False
    ts: float = 0.0


@dataclass(frozen=True)
class Quote:
    """A TARGET produced by the pure quoter. Not yet an order."""
    side: Side
    price: float
    size: float
    tif: str = "Alo"


@dataclass
class Order:
    """A live order we track locally."""
    cloid: str
    side: Side
    price: float
    size: float
    created_ts: float
    status: OrderStatus = OrderStatus.PENDING_NEW
    tif: str = "Alo"
    oid: Optional[int] = None
    filled: float = 0.0
    updated_ts: float = 0.0
    cancel_sent_ts: Optional[float] = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    @property
    def notional_remaining(self) -> float:
        return self.remaining * self.price

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass(frozen=True)
class StrategyParams:
    coin: str = "BTC"
    half_spread_bps: float = 4.0
    skew_bps: float = 6.0                 # max price shift (bps) at full inventory
    quote_notional: float = 15.0          # USD per quote
    max_notional: float = 60.0            # inventory cap used by skew + gating (USD)
    min_order_notional: float = 10.0      # exchange minimum order value (VERIFY on testnet)
    requote_threshold_bps: float = 1.5    # only reprice if target moved at least this much
    size_tolerance: float = 0.5           # requote if remaining < (1-tol) * target size
    min_requote_interval_s: float = 0.3
    # ---- lean 3-signal microstructure pricing (PREREG2_MICROSTRUCTURE_UPGRADE) ----
    micro_enabled: bool = True
    w_obi_bps: float = 1.5                # reservation shift per unit obi_nw
    w_tfi_bps: float = 1.0                # reservation shift per unit tfi2
    w_btc_bps: float = 0.7                # reservation shift per bps of BTC 1s return
    tox_obi: float = 0.45                 # |obi_nw| toxicity threshold
    tox_tfi: float = 0.65                 # |tfi2| toxicity threshold
    tox_btc_bps: float = 3.0              # adverse BTC 1s move threshold
    tox_widen_mult: float = 2.5           # widen vulnerable leg by this multiple
    wall_min_bps: float = 12.0            # only snap to walls in this band
    wall_max_bps: float = 22.0


@dataclass(frozen=True)
class RiskConfig:
    max_order_notional: float = 25.0
    max_position_notional: float = 80.0   # worst case incl. resting orders
    hard_position_mult: float = 1.5       # |pos| beyond mult*max_position -> kill
    # Hard account-equity kill-switch: halt if start_equity - equity >= this.
    # 0 disables the check (backwards compatible); set RISK_MAX_ACCOUNT_LOSS_USD.
    max_account_loss_usd: float = 0.0
    # Emergency position cap: |pos| * mid at or above this triggers the kill path,
    # which cancels all orders, taker-flattens, verifies flat, and halts.
    # 0 disables the check.
    emergency_flatten_notional: float = 40.0
    max_drawdown_pct: float = 0.05        # vs high-water mark
    max_loss_pct: float = 0.10            # vs starting equity
    max_latency_ms: float = 1500.0
    latency_breach_count: int = 5         # consecutive breaches -> kill
    stale_book_s: float = 3.0
    stale_k: float = 2.0                  # T7e: confirm when silence > K x rolling-median cadence
    restart_n: float = 3.0                # T7e: restart feeds only after N x cadence of silence
    fast_book: bool = True                # T7e: subscribe l2Book with fast:true (0 -> default feed)
    max_margin_utilization: float = 0.6
    kill_margin_utilization: float = 0.9
    max_actions_per_min: int = 120        # local pacing for the address-based budget
    max_consecutive_errors: int = 8
    side_pause_rejects_60s: int = 6       # T7b N: per-side place rejects in 60 s -> pause that side
    kill_rejects_5m: int = 20             # T7b M: place rejects (both sides) in 5 min -> kill switch
    side_pause_s: float = 60.0            # T7b pause length when the N window trips
