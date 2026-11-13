"""Small softmax-linear policy with a linear critic and JSON checkpoints.

Why linear: the shadow loop must be auditable and cheap.  The architecture is
the point — propensity logging, delayed rewards, KL drift guards and periodic
checkpoints all work identically for a larger function approximator, which can
be swapped in behind the same interface.

Checkpoints are plain JSON so they can be diffed and rolled back by hand.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np

from .features import FEATURE_NAMES

ACTIONS: Tuple[str, ...] = (
    "quote_both",      # two-sided passive quotes
    "quote_bid",       # bid only
    "quote_ask",       # ask only
    "pull",            # no resting quotes
    "reduce_only",     # only the inventory-reducing side
)
# Guard-only action: written by the recorder when LossGuard is HARD.  It is
# deliberately *not* in the policy action space, so the policy can never learn
# to "halt" as an alpha strategy and every sampled action is executable.
RISK_HOLD_ACTION = "risk_hold"
ACTION_INDEX = {a: i for i, a in enumerate(ACTIONS)}
SCHEMA = "mmbot-policy/1"


@dataclass
class SoftmaxLinearPolicy:
    n_features: int
    actions: Tuple[str, ...] = ACTIONS
    feature_names: Tuple[str, ...] = FEATURE_NAMES
    weights: np.ndarray = field(default=None)          # (A, F)
    bias: np.ndarray = field(default=None)             # (A,)
    value_weights: np.ndarray = field(default=None)    # (F,)
    value_bias: float = 0.0
    feature_mean: np.ndarray = field(default=None)     # (F,)
    feature_std: np.ndarray = field(default=None)      # (F,)
    version: int = 0
    trained_steps: int = 0
    created_ts: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        a, f = len(self.actions), self.n_features
        if self.weights is None:
            self.weights = np.zeros((a, f), dtype=float)
        else:
            self.weights = np.asarray(self.weights, dtype=float).reshape(a, f)
        if self.bias is None:
            self.bias = np.zeros(a, dtype=float)
        else:
            self.bias = np.asarray(self.bias, dtype=float).reshape(a)
        if self.value_weights is None:
            self.value_weights = np.zeros(f, dtype=float)
        else:
            self.value_weights = np.asarray(self.value_weights, dtype=float).reshape(f)
        if self.feature_mean is None:
            self.feature_mean = np.zeros(f, dtype=float)
        else:
            self.feature_mean = np.asarray(self.feature_mean, dtype=float).reshape(f)
        if self.feature_std is None:
            self.feature_std = np.ones(f, dtype=float)
        else:
            self.feature_std = np.asarray(self.feature_std, dtype=float).reshape(f)

    # ---- inference -------------------------------------------------------
    @staticmethod
    def _as2d(x) -> np.ndarray:
        arr = np.asarray(x, dtype=float)
        return arr.reshape(1, -1) if arr.ndim == 1 else arr

    def logits(self, x) -> np.ndarray:
        return self._as2d(x) @ self.weights.T + self.bias

    def probs(self, x) -> np.ndarray:
        z = self.logits(x)
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / np.maximum(e.sum(axis=1, keepdims=True), 1e-12)

    def log_probs(self, x) -> np.ndarray:
        p = np.clip(self.probs(x), 1e-12, 1.0)
        return np.log(p)

    def log_prob(self, x, action: str) -> float:
        idx = ACTION_INDEX[action]
        return float(self.log_probs(x)[0, idx])

    def sample(self, x, rng: np.random.Generator) -> Tuple[str, float]:
        p = self.probs(x)[0]
        idx = int(rng.choice(len(self.actions), p=p / p.sum()))
        return self.actions[idx], float(p[idx])

    def value(self, x) -> np.ndarray:
        return self._as2d(x) @ self.value_weights + self.value_bias

    # ---- gradients -------------------------------------------------------
    def grad_log_prob(self, x, action: str) -> Tuple[np.ndarray, np.ndarray]:
        """d log pi(a|x) / d(weights, bias)."""
        idx = ACTION_INDEX[action]
        p = self.probs(x)[0]
        onehot = np.zeros_like(p)
        onehot[idx] = 1.0
        g = onehot - p
        xv = self._as2d(x)[0]
        return np.outer(g, xv), g

    def apply_gradient(self, grad_w: np.ndarray, grad_b: np.ndarray,
                       lr: float) -> None:
        """Descend the loss: weights -= lr * dLoss/dweights."""
        self.weights -= float(lr) * np.asarray(grad_w, dtype=float)
        self.bias -= float(lr) * np.asarray(grad_b, dtype=float)
        self.version += 1

    def apply_value_gradient(self, grad_w: np.ndarray, grad_b: float,
                             lr: float) -> None:
        self.value_weights -= float(lr) * np.asarray(grad_w, dtype=float)
        self.value_bias -= float(lr) * float(grad_b)

    # ---- drift / statistics ---------------------------------------------
    def kl_to(self, other: "SoftmaxLinearPolicy", xs) -> float:
        x = self._as2d(xs)
        p = np.clip(self.probs(x), 1e-12, 1.0)
        q = np.clip(other.probs(x), 1e-12, 1.0)
        return float(np.mean(np.sum(p * (np.log(p) - np.log(q)), axis=1)))

    def ensure_feature_stats(self, xs) -> None:
        x = self._as2d(xs)
        if x.size == 0:
            return
        mean = x.mean(axis=0)
        std = np.where(x.std(axis=0) < 1e-3, 1e-3, x.std(axis=0))
        self.feature_mean = mean
        self.feature_std = std

    def blend_feature_stats(self, xs, alpha: float = 0.1) -> None:
        x = self._as2d(xs)
        if x.size == 0:
            return
        mean = x.mean(axis=0)
        std = np.where(x.std(axis=0) < 1e-3, 1e-3, x.std(axis=0))
        self.feature_mean = (1 - alpha) * self.feature_mean + alpha * mean
        self.feature_std = (1 - alpha) * self.feature_std + alpha * std

    # ---- checkpointing ---------------------------------------------------
    def clone(self) -> "SoftmaxLinearPolicy":
        return SoftmaxLinearPolicy(
            n_features=self.n_features, actions=tuple(self.actions),
            feature_names=tuple(self.feature_names),
            weights=self.weights.copy(), bias=self.bias.copy(),
            value_weights=self.value_weights.copy(), value_bias=self.value_bias,
            feature_mean=self.feature_mean.copy(), feature_std=self.feature_std.copy(),
            version=self.version, trained_steps=self.trained_steps,
            created_ts=self.created_ts, meta=dict(self.meta))

    def restore_from(self, other: "SoftmaxLinearPolicy") -> None:
        """Restore this object in place from ``other`` (rollback-safe).

        Identity is preserved on purpose: the recorder, loop and any other
        holder keep a valid reference across a KL rollback.
        """
        self.weights = np.asarray(other.weights, dtype=float).copy()
        self.bias = np.asarray(other.bias, dtype=float).copy()
        self.value_weights = np.asarray(other.value_weights, dtype=float).copy()
        self.value_bias = float(other.value_bias)
        self.feature_mean = np.asarray(other.feature_mean, dtype=float).copy()
        self.feature_std = np.asarray(other.feature_std, dtype=float).copy()
        self.version = int(other.version)
        self.trained_steps = int(other.trained_steps)
        self.meta = dict(other.meta)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "actions": list(self.actions),
            "feature_names": list(self.feature_names),
            "weights": self.weights.tolist(),
            "bias": self.bias.tolist(),
            "value_weights": self.value_weights.tolist(),
            "value_bias": float(self.value_bias),
            "feature_mean": self.feature_mean.tolist(),
            "feature_std": self.feature_std.tolist(),
            "version": int(self.version),
            "trained_steps": int(self.trained_steps),
            "created_ts": float(self.created_ts),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SoftmaxLinearPolicy":
        if d.get("schema") not in (None, SCHEMA):
            raise ValueError(f"unsupported policy schema {d.get('schema')!r}")
        return cls(
            n_features=len(d["feature_names"]),
            actions=tuple(d.get("actions", ACTIONS)),
            feature_names=tuple(d["feature_names"]),
            weights=np.asarray(d["weights"], dtype=float),
            bias=np.asarray(d["bias"], dtype=float),
            value_weights=np.asarray(d.get("value_weights", []), dtype=float)
            if d.get("value_weights") else None,
            value_bias=float(d.get("value_bias", 0.0)),
            feature_mean=np.asarray(d.get("feature_mean", []), dtype=float)
            if d.get("feature_mean") else None,
            feature_std=np.asarray(d.get("feature_std", []), dtype=float)
            if d.get("feature_std") else None,
            version=int(d.get("version", 0)),
            trained_steps=int(d.get("trained_steps", 0)),
            created_ts=float(d.get("created_ts", time.time())),
            meta=dict(d.get("meta", {})),
        )

    def save(self, path: str | os.PathLike) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, p)
        return p

    @classmethod
    def load(cls, path: str | os.PathLike) -> "SoftmaxLinearPolicy":
        with Path(path).open("r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def load_or_new(cls, path: str | os.PathLike,
                    n_features: int = len(FEATURE_NAMES)) -> "SoftmaxLinearPolicy":
        p = Path(path)
        if p.exists():
            try:
                pol = cls.load(p)
            except Exception:
                return cls(n_features=n_features)
            if (tuple(pol.actions) != tuple(ACTIONS)
                    or len(pol.feature_names) != n_features):
                return cls(n_features=n_features)
            return pol
        return cls(n_features=n_features)
