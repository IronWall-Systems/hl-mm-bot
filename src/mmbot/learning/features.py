"""Causal feature extraction shared by the recorder and the shadow loop.

Every value here is computed from data received at or before ``now``; there is
no look-ahead.  The vector order is frozen (``FEATURE_NAMES``) because policy
checkpoints store weights positionally and validate the names on load.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

FEATURE_NAMES: Tuple[str, ...] = (
    "obi_nw",           # top-5 size imbalance (microstructure daemon)
    "tfi2",             # 2 s aggressive taker-flow imbalance
    "lead_btc_1s",      # BTC 1 s mid return, bps
    "vol_ratio",        # short/long realised vol (30 s / 300 s)
    "spread_bps",       # quoted spread, bps
    "mid_ret_1s",       # own mid return over 1 s, bps
    "mid_ret_5s",       # own mid return over 5 s, bps
    "inventory_frac",   # signed inventory / max_notional
    "wall_bid_bps",     # verified bid wall distance, bps (0 if none)
    "wall_ask_bps",     # verified ask wall distance, bps (0 if none)
)

# Fixed per-feature scale/clip.  The raw features have wildly different units
# (OBI ~0.1, testnet spread ~445 bps); feeding them straight into a linear
# policy makes one SFT step blow through the KL cap.  ``vector`` returns
# O(1)-ish values so the same learning rate works across regimes.
FEATURE_SCALE: Tuple[float, ...] = (
    1.0, 1.0, 10.0, 1.0, 50.0, 10.0, 10.0, 1.0, 50.0, 50.0,
)
FEATURE_CLIP: Tuple[float, ...] = (
    1.0, 5.0, 5.0, 10.0, 10.0, 5.0, 5.0, 3.0, 10.0, 10.0,
)


def _log_returns(points: Sequence[Tuple[float, float]]) -> np.ndarray:
    if len(points) < 2:
        return np.zeros(0)
    out = []
    for (_, a), (_, b) in zip(points, points[1:]):
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return np.asarray(out, dtype=float)


@dataclass
class FeatureExtractor:
    """Online feature builder with a bounded mid-price history."""

    short_window_s: float = 30.0
    long_window_s: float = 300.0
    max_points: int = 8192
    _mids: Deque[Tuple[float, float]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._mids = deque(self._mids, maxlen=self.max_points)

    # ---- history ---------------------------------------------------------
    def on_mid(self, now: float, mid: float) -> None:
        if mid <= 0:
            return
        if self._mids and now < self._mids[-1][0]:
            return                                  # ignore out-of-order ticks
        if self._mids and now == self._mids[-1][0]:
            self._mids[-1] = (now, mid)
            return
        self._mids.append((now, mid))

    def _at_or_before(self, target: float) -> Optional[float]:
        """Nearest mid at or before ``target`` (causal)."""
        best = None
        for ts, mid in reversed(self._mids):
            if ts <= target:
                best = mid
                break
        return best

    def _since(self, now: float, window: float) -> List[Tuple[float, float]]:
        lo = now - window
        return [(ts, mid) for ts, mid in self._mids if ts >= lo]

    # ---- features --------------------------------------------------------
    def update(self, now: float, mid: float, *, micro=None,
               position: float = 0.0, max_notional: float = 0.0,
               spread_bps: Optional[float] = None,
               mid_1s: Optional[float] = None) -> Dict[str, float]:
        self.on_mid(now, mid)
        short = _log_returns(self._since(now, self.short_window_s))
        long = _log_returns(self._since(now, self.long_window_s))
        short_vol = float(np.std(short)) if short.size else 0.0
        long_vol = float(np.std(long)) if long.size else 0.0
        vol_ratio = short_vol / long_vol if long_vol > 1e-12 else 1.0
        vol_ratio = max(0.0, min(10.0, vol_ratio))

        ref_1s = mid_1s if mid_1s is not None else self._at_or_before(now - 1.0)
        ref_5s = self._at_or_before(now - 5.0)

        def ret(ref: Optional[float]) -> float:
            if ref is None or ref <= 0 or mid <= 0:
                return 0.0
            return (mid - ref) / ref * 1e4

        inv_frac = (position * mid / max_notional) if max_notional > 0 else 0.0
        inv_frac = max(-3.0, min(3.0, inv_frac))

        f = {
            "obi_nw": float(getattr(micro, "obi_nw", 0.0) or 0.0),
            "tfi2": float(getattr(micro, "tfi2", 0.0) or 0.0),
            "lead_btc_1s": float(getattr(micro, "lead_btc_1s", 0.0) or 0.0),
            "vol_ratio": vol_ratio,
            "spread_bps": float(spread_bps) if spread_bps is not None else 0.0,
            "mid_ret_1s": ret(ref_1s),
            "mid_ret_5s": ret(ref_5s),
            "inventory_frac": inv_frac,
            "wall_bid_bps": float(getattr(micro, "wall_bid_bps", 0.0) or 0.0),
            "wall_ask_bps": float(getattr(micro, "wall_ask_bps", 0.0) or 0.0),
        }
        return f


def vector(features: Dict[str, float]) -> List[float]:
    """Features in the frozen order, scaled/clipped and NaN-free."""
    out: List[float] = []
    for name, scale, clip in zip(FEATURE_NAMES, FEATURE_SCALE, FEATURE_CLIP):
        try:
            value = float(features.get(name, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value):
            value = 0.0
        out.append(max(-clip, min(clip, value / scale)))
    return out


def matrix(steps: Iterable) -> np.ndarray:
    rows = [vector(s.features) for s in steps]
    if not rows:
        return np.zeros((0, len(FEATURE_NAMES)), dtype=float)
    return np.asarray(rows, dtype=float)


def feature_stats(xs: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    if xs.size == 0:
        z = np.zeros(len(FEATURE_NAMES), dtype=float)
        return z, z + 1.0
    mean = xs.mean(axis=0)
    std = xs.std(axis=0)
    std = np.where(std < 1e-3, 1e-3, std)
    return mean, std


def covariate_drift_z(recent: np.ndarray, ref_mean: np.ndarray,
                      ref_std: np.ndarray) -> float:
    """Max absolute standardised mean shift across features (0 = identical).

    Features whose baseline is (near-)constant are ignored: dividing a tiny
    mean shift by a ~0 std produces meaningless millions.  Only features with
    a real baseline spread contribute to the guard.
    """
    if recent.size == 0 or ref_mean.size == 0:
        return 0.0
    mean = recent.mean(axis=0)
    std = np.asarray(ref_std, dtype=float)
    mask = std > 1e-3
    if not np.any(mask):
        return 0.0
    z = np.abs(mean - ref_mean) / np.maximum(std, 1e-3)
    return float(np.max(z[mask]))
