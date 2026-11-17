from collections import deque
import math
from dataclasses import dataclass

@dataclass
class QuoteDecision:
    bid_spread_bps: float
    ask_spread_bps: float
    reservation_shift_bps: float
    allow_bid: bool
    allow_ask: bool
    regime_mode: str  # "MM_NORMAL", "MM_TOXIC_DEFENSE", "REDUCE_ONLY", "BREAKOUT_LONG"

class AlphaRegimeShieldPlugin:
    """
    Combines HftBacktest OBI/VAMP Micro-Price + Trade Flow Imbalance (TFI)
    + Rolling 1h MTM Cooldown + Bullish Breakout Handover for /root/hl-mm-bot.
    """
    def __init__(
        self,
        base_spread_bps: float = 20.0,
        norm_skew_bps_per_30usd: float = 5.5,  # Safe ridge: 3.0 to 6.0 bps per $30 inv
        max_pos_usd: float = 30.0,
        quote_notional_usd: float = 15.0,
        obi_weight_bps: float = 6.0,           # Max micro-price shift from book imbalance
        tfi_toxic_threshold: float = 0.45,     # 30s aggressive trade imbalance threshold
        toxic_widening_mult: float = 2.2,      # Widens vulnerable side from 20 -> 44 bps
        cooldown_loss_usd: float = 0.35,       # Relaxed from $0.25 to avoid normal chop lockouts
        cooldown_window_s: float = 3600.0
    ):
        self.base_spread_bps = base_spread_bps
        self.norm_skew_bps_per_30usd = norm_skew_bps_per_30usd
        self.max_pos_usd = max_pos_usd
        self.quote_notional_usd = quote_notional_usd
        self.obi_weight_bps = obi_weight_bps
        self.tfi_toxic_threshold = tfi_toxic_threshold
        self.toxic_widening_mult = toxic_widening_mult
        self.cooldown_loss_usd = cooldown_loss_usd
        self.cooldown_window_s = cooldown_window_s

        self.mtm_history = deque()   # (timestamp, equity_usd)
        self.trade_flow = deque()    # (timestamp, signed_notional_usd)
        self.cooldown_until_ts = 0.0

    def record_trade_tick(self, ts: float, side: str, notional_usd: float) -> None:
        signed_val = notional_usd if side.upper() == "BUY" else -notional_usd
        self.trade_flow.append((ts, signed_val))
        cutoff = ts - 30.0
        while self.trade_flow and self.trade_flow[0][0] < cutoff:
            self.trade_flow.popleft()

    def _compute_tfi_30s(self) -> float:
        if not self.trade_flow:
            return 0.0
        buys = sum(v for _, v in self.trade_flow if v > 0)
        sells = sum(-v for _, v in self.trade_flow if v < 0)
        total = buys + sells
        return (buys - sells) / total if total > 1e-6 else 0.0

    def evaluate(
        self,
        ts: float,
        best_bid: float,
        best_ask: float,
        bid_depth_top5_usd: float,
        ask_depth_top5_usd: float,
        inventory_usd: float,
        resting_bid_usd: float,
        resting_ask_usd: float,
        mtm_equity_usd: float,
        is_1h_bull_breakout: bool = False
    ) -> QuoteDecision:
        # 1. Update rolling 60m MTM High-Water Mark
        self.mtm_history.append((ts, mtm_equity_usd))
        cutoff_1h = ts - self.cooldown_window_s
        while self.mtm_history and self.mtm_history[0][0] < cutoff_1h:
            self.mtm_history.popleft()
        rolling_hwm = max(eq for _, eq in self.mtm_history)
        rolling_dd = rolling_hwm - mtm_equity_usd

        if rolling_dd >= self.cooldown_loss_usd and ts > self.cooldown_until_ts:
            # Lock for 60 minutes (instead of 3 hours) or hand over to Breakout
            self.cooldown_until_ts = ts + 3600.0

        # 2. Order Book Imbalance (OBI) + Dollar-Normalized Inventory Skew
        depth_sum = bid_depth_top5_usd + ask_depth_top5_usd
        obi = (bid_depth_top5_usd - ask_depth_top5_usd) / depth_sum if depth_sum > 0 else 0.0
        
        # Positive shift moves both Bid and Ask UP (protects Ask during buy pressure, unloads Short inv)
        inv_skew_bps = -self.norm_skew_bps_per_30usd * (inventory_usd / 30.0)
        obi_shift_bps = self.obi_weight_bps * obi
        reservation_shift_bps = inv_skew_bps + obi_shift_bps

        # 3. Toxic Flow Asymmetric Widening (TFI)
        tfi = self._compute_tfi_30s()
        bid_spread = self.base_spread_bps
        ask_spread = self.base_spread_bps
        mode = "MM_NORMAL"

        if tfi > self.tfi_toxic_threshold:
            # Heavy aggressive buying -> widen Ask to avoid getting run over
            ask_spread *= self.toxic_widening_mult
            mode = "MM_TOXIC_DEFENSE"
        elif tfi < -self.tfi_toxic_threshold:
            # Heavy aggressive selling -> widen Bid to avoid catching a falling knife
            bid_spread *= self.toxic_widening_mult
            mode = "MM_TOXIC_DEFENSE"

        # 4. Hard Pre-Trade Inventory Reservation (Fixes the $56.90 overshoot bug)
        allow_bid = (inventory_usd + resting_bid_usd + self.quote_notional_usd) <= self.max_pos_usd
        allow_ask = ( abs(inventory_usd - resting_ask_usd - self.quote_notional_usd) <= self.max_pos_usd ) or (inventory_usd > 0)

        # 5. Cooldown / Breakout Handover Logic
        if ts < self.cooldown_until_ts or is_1h_bull_breakout:
            if is_1h_bull_breakout:
                # Oct 29 scenario: Hold/build Long up to $30 cap, block Short selling
                mode = "BREAKOUT_LONG"
                allow_bid = inventory_usd < self.max_pos_usd
                allow_ask = False
                bid_spread = max(8.0, self.base_spread_bps * 0.5)  # Aggressive maker entry
            else:
                mode = "REDUCE_ONLY"
                allow_bid = inventory_usd < -1.0   # Only buy back if short
                allow_ask = inventory_usd > 1.0    # Only sell if long

        return QuoteDecision(
            bid_spread_bps=bid_spread,
            ask_spread_bps=ask_spread,
            reservation_shift_bps=reservation_shift_bps,
            allow_bid=allow_bid,
            allow_ask=allow_ask,
            regime_mode=mode
        )