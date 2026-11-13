"""Trading-process recorder: writes decision steps and settles delayed rewards.

The recorder is observational.  It logs the action the production quoter
actually took (or ``risk_hold`` when LossGuard is active) together with the
policy propensity; it never changes an order.  Exploration happens only in the
separate shadow-paper loop.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from .buffer import DecisionStep, RollingExperienceBuffer
from .features import FEATURE_NAMES, FeatureExtractor, vector
from .policy import (ACTION_INDEX, RISK_HOLD_ACTION,
                     SoftmaxLinearPolicy)
from .rewards import RewardTracker

ROOT = Path(__file__).resolve().parents[3]
TRUTHY = ("1", "true", "yes", "on")


def _env_bool(env, key: str, default: bool = False) -> bool:
    return str(env.get(key, "1" if default else "0")).lower() in TRUTHY


class LearningRecorder:
    def __init__(self, buffer: RollingExperienceBuffer,
                 policy: SoftmaxLinearPolicy,
                 *, coin: str = "HYPE", max_notional: float = 11.0,
                 extractor: Optional[FeatureExtractor] = None,
                 tracker: Optional[RewardTracker] = None,
                 guard_state_path: Optional[str | os.PathLike] = None,
                 policy_path: Optional[str | os.PathLike] = None,
                 protection_cap_usd: float = 0.10,
                 seed: int = 7, now_fn=time.time):
        self.buffer = buffer
        self.policy = policy
        self.coin = coin
        self.max_notional = float(max_notional)
        self.extractor = extractor or FeatureExtractor()
        self.tracker = tracker or RewardTracker(
            buffer, protection_cap_usd=protection_cap_usd, now_fn=now_fn)
        self.guard_state_path = Path(guard_state_path) if guard_state_path else None
        self.policy_path = Path(policy_path) if policy_path else None
        self.rng = np.random.default_rng(seed)
        self.now = now_fn
        self._last_trim = -1e9
        self._last_settle = -1e9
        self.steps = 0
        self.fills = 0

    # ---- construction ----------------------------------------------------
    @classmethod
    def from_env(cls, *, coin: str, max_notional: float,
                 environ=None, now_fn=time.time) -> Optional["LearningRecorder"]:
        env = os.environ if environ is None else environ
        if not _env_bool(env, "MM_LEARNING", False):
            return None
        buf_path = Path(env.get("MM_ROLLING_BUFFER", str(ROOT / "data" / "rolling_buffer.jsonl")))
        policy_path = Path(env.get("MM_POLICY_PATH", str(ROOT / "models" / "policy_latest.json")))
        guard_path = Path(env.get("MM_GUARD_STATE", str(ROOT / "data" / "learning_guard_state.json")))
        buffer = RollingExperienceBuffer.from_env(buf_path, env)
        policy = SoftmaxLinearPolicy.load_or_new(policy_path)
        if len(policy.feature_names) != len(FEATURE_NAMES):
            policy = SoftmaxLinearPolicy(n_features=len(FEATURE_NAMES))
        return cls(buffer, policy, coin=coin, max_notional=max_notional,
                   guard_state_path=guard_path, policy_path=policy_path,
                   protection_cap_usd=float(env.get("MM_REWARD_CAP_USD", "0.10")),
                   seed=int(float(env.get("MM_TRAIN_SEED", "7"))),
                   now_fn=now_fn)

    # ---- observation -----------------------------------------------------
    def observe(self, now: float, mid: float, *, micro=None,
                position: float = 0.0, equity: float = 0.0,
                guard_status: str = "ok",
                action_hint: Optional[str] = None,
                spread_bps: Optional[float] = None) -> DecisionStep:
        features = self.extractor.update(
            now, mid, micro=micro, position=position,
            max_notional=self.max_notional, spread_bps=spread_bps)
        gs = (guard_status or "ok").lower()
        x = vector(features)
        if gs == "hard":
            # LossGuard supremacy: no exploration, no counterfactual sampling.
            action, propensity, explored = RISK_HOLD_ACTION, 1.0, False
        elif gs in ("soft", "pull"):
            # Risk actions are deterministic: record what was actually done,
            # but never explore while the guard is engaged.
            default = "reduce_only" if gs == "soft" else "pull"
            action = action_hint if action_hint in ACTION_INDEX else default
            propensity, explored = 1.0, False
        else:
            probs = self.policy.probs(x)[0]
            if action_hint in ACTION_INDEX:
                action = action_hint
                propensity = float(probs[ACTION_INDEX[action]])
                explored = False
            else:
                action, propensity = self.policy.sample(x, self.rng)
                explored = True
        step = DecisionStep(
            step_id=0, ts=float(now), mono=float(now), coin=self.coin,
            features=features, action=action, propensity=float(propensity),
            guard_status=gs, mid=float(mid), position=float(position),
            equity=float(equity), meta={"explored": bool(explored)})
        self.buffer.append(step)
        self.tracker.on_decision(step.step_id, step.ts, step.mid,
                                 step.position, step.action)
        self.steps += 1
        self._write_guard_state(now, gs, equity, position)
        return step

    def on_mark(self, now: float, mid: float) -> None:
        self.extractor.on_mid(now, mid)
        self.tracker.on_mark(now, mid)

    def on_fill(self, now: float, side: str, px: float, sz: float, fee: float) -> None:
        self.tracker.on_fill(now, side, px, sz, fee)
        self.fills += 1

    # ---- maintenance -----------------------------------------------------
    def settle(self, now: float) -> int:
        out = self.tracker.settle(now)
        self._last_settle = now
        return len(out)

    def maybe_trim(self, now: float, every_s: float = 60.0) -> int:
        if now - self._last_trim < every_s:
            return 0
        self._last_trim = now
        return self.buffer.trim(now)

    def maybe_checkpoint(self) -> Optional[str]:
        if self.policy_path is None:
            return None
        try:
            return str(self.policy.save(self.policy_path))
        except OSError:
            return None

    def status(self) -> Dict[str, Any]:
        return {"enabled": True, "steps": self.steps, "fills": self.fills,
                "pending_rewards": self.tracker.pending,
                "policy_version": self.policy.version,
                "buffer": self.buffer.stats()}

    # ---- internals -------------------------------------------------------
    def _write_guard_state(self, now: float, guard: str,
                           equity: float, position: float) -> None:
        if self.guard_state_path is None:
            return
        try:
            self.guard_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.guard_state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "ts": round(now, 3), "guard": guard,
                "equity": round(float(equity), 6),
                "position": round(float(position), 8),
            }), encoding="utf-8")
            os.replace(tmp, self.guard_state_path)
        except OSError:
            pass
