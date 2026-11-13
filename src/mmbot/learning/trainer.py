"""Periodic rolling post-training: SFT -> DPO -> RLOO -> GRPO -> PPO.

All stages read the *rolling* buffer window, so the policy tracks the current
regime instead of a frozen historical dataset.  Two guards run around every
update:

* **KL drift guard** — ``KL(pi_theta || pi_ref)`` against a frozen reference
  checkpoint; a stage that exceeds ``kl_max`` is rolled back.
* **Covariate drift guard** — standardised feature-mean shift vs the reference
  statistics; a shifted window is skipped rather than learned.

LossGuard is authoritative: ``guard_status`` in {soft, pull, hard} (or an
existing HALT marker) blocks training outright.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from .buffer import DecisionStep, RollingExperienceBuffer
from .features import covariate_drift_z, matrix, vector
from .policy import ACTION_INDEX, SoftmaxLinearPolicy

EPS = 1e-9


@dataclass
class TrainerConfig:
    window_s: float = 24 * 3600.0
    trigger_every_s: float = 4 * 3600.0
    min_steps: int = 200
    min_new_steps: int = 100
    group_s: float = 300.0
    kl_max: float = 0.15
    drift_z_max: float = 3.0
    drift_refresh_after: int = 3
    lr: float = 0.01
    value_lr: float = 0.01
    dpo_beta: float = 0.1
    gamma: float = 0.99
    lam: float = 0.95
    clip_eps: float = 0.2
    max_kl_sample: int = 256
    seed: int = 7
    checkpoint_path: str = "models/policy_latest.json"
    ref_path: str = "models/policy_ref.json"
    log_path: str = "logs/learning_train.jsonl"
    halt_path: Optional[str] = None

    @classmethod
    def from_env(cls, environ=None) -> "TrainerConfig":
        env = os.environ if environ is None else environ

        def f(name: str, default: float) -> float:
            try:
                return float(env.get(name, default))
            except (TypeError, ValueError):
                return default

        def i(name: str, default: int) -> int:
            try:
                return int(float(env.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            window_s=f("MM_TRAIN_WINDOW_H", 24.0) * 3600.0,
            trigger_every_s=f("MM_TRAIN_EVERY_H", 4.0) * 3600.0,
            min_steps=i("MM_TRAIN_MIN_STEPS", 200),
            min_new_steps=i("MM_TRAIN_MIN_NEW_STEPS", 100),
            group_s=f("MM_TRAIN_GROUP_S", 300.0),
            kl_max=f("MM_TRAIN_KL_MAX", 0.15),
            drift_z_max=f("MM_TRAIN_DRIFT_Z", 3.0),
            lr=f("MM_TRAIN_LR", 0.01),
            dpo_beta=f("MM_TRAIN_DPO_BETA", 0.1),
            checkpoint_path=env.get("MM_POLICY_PATH", "models/policy_latest.json"),
            ref_path=env.get("MM_POLICY_REF_PATH", "models/policy_ref.json"),
            log_path=env.get("MM_TRAIN_LOG", "logs/learning_train.jsonl"),
            halt_path=env.get("RISK_HALT_FILE"),
        )


@dataclass
class TrainResult:
    trained: bool
    reason: str = ""
    n_steps: int = 0
    stages: Dict[str, float] = field(default_factory=dict)
    kl: float = 0.0
    drift_z: float = 0.0
    rolled_back: bool = False
    checkpoint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        def finite(v: Any) -> Any:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return v
            return f if math.isfinite(f) else None

        return {
            "trained": self.trained, "reason": self.reason,
            "n_steps": self.n_steps,
            "stages": {k: finite(v) for k, v in self.stages.items()},
            "kl": finite(round(self.kl, 6)),
            "drift_z": finite(round(self.drift_z, 6)),
            "rolled_back": self.rolled_back, "checkpoint": self.checkpoint,
        }


def _reward(step: DecisionStep) -> float:
    return float(step.reward if step.reward is not None else 0.0)


def _adv_weights(steps: Sequence[DecisionStep], cap: float = 10.0) -> np.ndarray:
    p = np.asarray([max(EPS, min(1.0, s.propensity)) for s in steps], dtype=float)
    return np.minimum(cap, 1.0 / p)


class RollingTrainer:
    def __init__(self, buffer: RollingExperienceBuffer,
                 policy: SoftmaxLinearPolicy,
                 config: Optional[TrainerConfig] = None,
                 ref_policy: Optional[SoftmaxLinearPolicy] = None,
                 now_fn=time.time,
                 log_fn: Optional[Callable[[str], None]] = None):
        self.buffer = buffer
        self.policy = policy
        self.cfg = config or TrainerConfig()
        self.now = now_fn
        self.rng = np.random.default_rng(self.cfg.seed)
        self.log = log_fn or (lambda _msg: None)
        self.last_train_ts: Optional[float] = None
        self.last_result: Optional[TrainResult] = None
        self._drift_streak = 0
        self.ref_policy = ref_policy or self._load_ref()

    # ---- reference / checkpoints ----------------------------------------
    def _load_ref(self) -> SoftmaxLinearPolicy:
        p = Path(self.cfg.ref_path)
        if p.exists():
            try:
                return SoftmaxLinearPolicy.load(p)
            except Exception:
                pass
        ref = self.policy.clone()
        ref.save(p)
        return ref

    def _checkpoint(self) -> str:
        path = self.policy.save(self.cfg.checkpoint_path)
        return str(path)

    def _log_result(self, result: TrainResult) -> None:
        try:
            p = Path(self.cfg.log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": round(self.now(), 3), "event": "rolling_train",
                   **result.to_dict(), "buffer": self.buffer.stats(),
                   "policy_version": self.policy.version}
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    # ---- scheduling ------------------------------------------------------
    def _new_steps(self, now: float) -> int:
        if self.last_train_ts is None:
            return len(self.buffer)
        return sum(1 for s in self.buffer.all() if s.ts > self.last_train_ts)

    def _halt_exists(self) -> bool:
        return bool(self.cfg.halt_path and Path(self.cfg.halt_path).exists())

    def maybe_train(self, now: Optional[float] = None,
                    guard_status: str = "ok",
                    force: bool = False) -> TrainResult:
        now = self.now() if now is None else now
        if self._halt_exists():
            return self._skip("halt_file", now)
        gs = (guard_status or "ok").lower()
        if gs in ("soft", "pull", "hard"):
            return self._skip(f"guard_{gs}", now)
        if not force:
            if self.last_train_ts is not None and now - self.last_train_ts < self.cfg.trigger_every_s:
                return self._skip("not_due", now)
            if self._new_steps(now) < self.cfg.min_new_steps:
                return self._skip("insufficient_new_steps", now)
        return self.train(now)

    def _skip(self, reason: str, now: float) -> TrainResult:
        res = TrainResult(trained=False, reason=reason)
        self.last_result = res
        return res

    # ---- main pipeline ---------------------------------------------------
    def train(self, now: Optional[float] = None) -> TrainResult:
        now = self.now() if now is None else now
        steps = [s for s in self.buffer.recent(self.cfg.window_s, now)
                 if s.reward is not None and s.action in ACTION_INDEX]
        if len(steps) < self.cfg.min_steps:
            return self._skip(f"need_{self.cfg.min_steps}_steps_got_{len(steps)}", now)
        steps.sort(key=lambda s: s.ts)
        xs = matrix(steps)
        rewards = np.asarray([_reward(s) for s in steps], dtype=float)

        # ---- covariate drift guard --------------------------------------
        # The baseline is the frozen reference statistics, not the policy's
        # own (moving) statistics: drift must be measured against the regime
        # the reference policy was trained on.  Sustained drift refreshes the
        # baseline so a genuine regime change cannot block learning forever.
        drift_z = 0.0
        if not self.ref_policy.meta.get("stats_initialized"):
            self.ref_policy.ensure_feature_stats(xs)
            self.ref_policy.meta["stats_initialized"] = True
            self.ref_policy.save(self.cfg.ref_path)
            self.policy.ensure_feature_stats(xs)
            self.policy.meta["stats_initialized"] = True
            self._drift_streak = 0
        else:
            drift_z = covariate_drift_z(xs, self.ref_policy.feature_mean,
                                        self.ref_policy.feature_std)
            if drift_z > self.cfg.drift_z_max:
                self._drift_streak += 1
                if self._drift_streak >= self.cfg.drift_refresh_after:
                    self.log(f"baseline refreshed after {self._drift_streak} "
                             f"sustained drift skips (z={drift_z:.2f})")
                    self.ref_policy.ensure_feature_stats(xs)
                    self.ref_policy.meta["stats_initialized"] = True
                    self.ref_policy.save(self.cfg.ref_path)
                    self.policy.ensure_feature_stats(xs)
                    self._drift_streak = 0
                    drift_z = 0.0
                else:
                    res = self._skip(f"covariate_drift_z={drift_z:.2f}", now)
                    res.drift_z = drift_z
                    self.last_train_ts = now      # rate-limit retries
                    self._log_result(res)
                    return res
            else:
                self._drift_streak = 0

        ref = self.ref_policy
        stages: Dict[str, float] = {}
        rolled_back = False
        kl = 0.0
        sample_idx = (self.rng.choice(len(steps), size=min(self.cfg.max_kl_sample, len(steps)),
                                      replace=False) if len(steps) > self.cfg.max_kl_sample
                      else np.arange(len(steps)))
        sample_x = xs[sample_idx]

        def stage(name: str, fn) -> bool:
            nonlocal kl, rolled_back
            snap = self.policy.clone()
            scale = 1.0
            for _attempt in range(3):
                try:
                    value = float(fn(scale))
                except Exception as exc:                  # never let one stage kill the job
                    self.log(f"stage {name} failed: {exc!r}")
                    self.policy.restore_from(snap)
                    stages[name] = float("nan")
                    return False
                kl = self.policy.kl_to(ref, sample_x)
                if kl <= self.cfg.kl_max:
                    stages[name] = value
                    return True
                # KL-controlled backtracking: retry the same stage with half
                # the step size before giving up on it.
                self.policy.restore_from(snap)
                scale *= 0.5
            rolled_back = True
            stages[name] = float("nan")
            self.log(f"stage {name} rolled back: KL {kl:.3f} > {self.cfg.kl_max} "
                     "after backtracking")
            return False

        if not stage("sft", lambda s: self._sft(xs, rewards, steps, s)):
            pass
        else:
            if not stage("dpo", lambda s: self._dpo(steps, rewards, s)):
                pass
            else:
                if not stage("rloo", lambda s: self._rloo(steps, rewards, s)):
                    pass
                else:
                    if not stage("grpo", lambda s: self._grpo(steps, rewards, s)):
                        pass
                    else:
                        stage("ppo", lambda s: self._ppo(steps, rewards, s))

        if not rolled_back:
            self.policy.trained_steps += len(steps)
            self.policy.blend_feature_stats(xs, alpha=0.1)
        self.last_train_ts = now
        checkpoint = None
        if not rolled_back:
            checkpoint = self._checkpoint()
        res = TrainResult(trained=not rolled_back, reason="ok" if not rolled_back else "kl_rollback",
                          n_steps=len(steps), stages=stages, kl=kl, drift_z=drift_z,
                          rolled_back=rolled_back, checkpoint=checkpoint)
        self.last_result = res
        self._log_result(res)
        return res

    # ---- stage helpers ---------------------------------------------------
    def _pg_update(self, xs: np.ndarray, actions: Sequence[str],
                   advantages: np.ndarray, lr: float,
                   importance: Optional[np.ndarray] = None) -> float:
        n = len(actions)
        if n == 0:
            return 0.0
        gw = np.zeros_like(self.policy.weights)
        gb = np.zeros_like(self.policy.bias)
        adv = np.clip(np.asarray(advantages, dtype=float), -10.0, 10.0)
        w = np.ones(n) if importance is None else np.asarray(importance, dtype=float)
        # Self-normalised importance sampling: keep the *relative* off-policy
        # correction without inflating the overall step size (raw 1/p with a
        # 5-action policy multiplies every update by ~5 and trips the KL cap).
        if importance is not None and w.size and float(w.mean()) > EPS:
            w = w / float(w.mean())
            w = np.minimum(w, 10.0)
        for i, (x, a) in enumerate(zip(xs, actions)):
            gwi, gbi = self.policy.grad_log_prob(x, a)
            gw -= float(adv[i]) * float(w[i]) * gwi
            gb -= float(adv[i]) * float(w[i]) * gbi
        self.policy.apply_gradient(gw / n, gb / n, lr)
        return float(np.mean(adv))

    def _groups(self, steps: Sequence[DecisionStep]) -> List[List[DecisionStep]]:
        buckets: Dict[Any, List[DecisionStep]] = {}
        for s in steps:
            key = s.episode_id if s.episode_id else int(s.ts // max(1.0, self.cfg.group_s))
            buckets.setdefault(key, []).append(s)
        return [sorted(g, key=lambda z: z.ts) for g in buckets.values() if len(g) >= 2]

    def _sft(self, xs: np.ndarray, rewards: np.ndarray,
             steps: Sequence[DecisionStep], lr_scale: float = 1.0) -> float:
        q70 = float(np.quantile(rewards, 0.70))
        idx = [i for i, r in enumerate(rewards) if r >= q70 and r > 0]
        if not idx:
            return 0.0
        acts = [steps[i].action for i in idx]
        adv = np.asarray([rewards[i] for i in idx], dtype=float)
        scale = max(EPS, float(np.mean(np.abs(adv))))
        return self._pg_update(xs[idx], acts, np.clip(adv / scale, -10.0, 10.0),
                               self.cfg.lr * lr_scale)

    def _dpo(self, steps: Sequence[DecisionStep], rewards: np.ndarray,
             lr_scale: float = 1.0) -> float:
        q30 = float(np.quantile(rewards, 0.30))
        q70 = float(np.quantile(rewards, 0.70))
        winners = [i for i, r in enumerate(rewards) if r >= q70 and r > 0]
        losers = [i for i, r in enumerate(rewards) if r <= q30 and r < 0]
        if not winners or not losers:
            return 0.0
        m = min(len(winners), len(losers), 128)
        wi = self.rng.choice(winners, size=m, replace=False)
        li = self.rng.choice(losers, size=m, replace=False)
        gw = np.zeros_like(self.policy.weights)
        gb = np.zeros_like(self.policy.bias)
        beta = self.cfg.dpo_beta
        losses = []
        for iw, il in zip(wi, li):
            sw, sl = steps[int(iw)], steps[int(il)]
            xw, xl = vector(sw.features), vector(sl.features)
            lp_w = self.policy.log_prob(xw, sw.action)
            lp_l = self.policy.log_prob(xl, sl.action)
            ref_w = self.ref_policy.log_prob(xw, sw.action)
            ref_l = self.ref_policy.log_prob(xl, sl.action)
            margin = beta * ((lp_w - lp_l) - (ref_w - ref_l))
            s = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, margin))))
            coef = (1.0 - s) * beta
            gww, gbw = self.policy.grad_log_prob(xw, sw.action)
            gwl, gbl = self.policy.grad_log_prob(xl, sl.action)
            gw -= coef * (gww - gwl)
            gb -= coef * (gbw - gbl)
            losses.append(-math.log(max(EPS, s)))
        self.policy.apply_gradient(gw / m, gb / m, self.cfg.lr * lr_scale)
        return float(np.mean(losses))

    def _rloo(self, steps: Sequence[DecisionStep], rewards: np.ndarray,
              lr_scale: float = 1.0) -> float:
        idx_by_id = {id(s): i for i, s in enumerate(steps)}
        xs_all, acts, advs, imps = [], [], [], []
        for group in self._groups(steps):
            rs = np.asarray([_reward(s) for s in group], dtype=float)
            if len(rs) < 2 or float(np.std(rs)) < EPS:
                continue
            for j, s in enumerate(group):
                others = np.delete(rs, j)
                adv = rs[j] - float(np.mean(others)) if others.size else rs[j]
                xs_all.append(vector(s.features))
                acts.append(s.action)
                advs.append(adv)
                imps.append(min(10.0, 1.0 / max(EPS, s.propensity)))
        if not xs_all:
            return 0.0
        return self._pg_update(np.asarray(xs_all), acts,
                               np.clip(np.asarray(advs, dtype=float), -10.0, 10.0),
                               self.cfg.lr * lr_scale, np.asarray(imps))

    def _grpo(self, steps: Sequence[DecisionStep], rewards: np.ndarray,
              lr_scale: float = 1.0) -> float:
        xs_all, acts, advs, imps = [], [], [], []
        for group in self._groups(steps):
            rs = np.asarray([_reward(s) for s in group], dtype=float)
            std = float(np.std(rs))
            if len(rs) < 2 or std < EPS:
                continue
            norm = (rs - float(np.mean(rs))) / std
            for j, s in enumerate(group):
                xs_all.append(vector(s.features))
                acts.append(s.action)
                advs.append(float(np.clip(norm[j], -10.0, 10.0)))
                imps.append(min(10.0, 1.0 / max(EPS, s.propensity)))
        if not xs_all:
            return 0.0
        return self._pg_update(np.asarray(xs_all), acts, np.asarray(advs),
                               self.cfg.lr * lr_scale, np.asarray(imps))

    def _ppo(self, steps: Sequence[DecisionStep], rewards: np.ndarray,
             lr_scale: float = 1.0) -> float:
        gw = np.zeros_like(self.policy.weights)
        gb = np.zeros_like(self.policy.bias)
        vgw = np.zeros_like(self.policy.value_weights)
        vgb = 0.0
        total = 0
        for group in self._groups(steps):
            xs = matrix(group)
            rs = np.asarray([_reward(s) for s in group], dtype=float)
            values = self.policy.value(xs).reshape(-1)
            adv = np.zeros_like(rs)
            gae = 0.0
            for t in range(len(group) - 1, -1, -1):
                nxt = values[t + 1] if t + 1 < len(values) else 0.0
                delta = rs[t] + self.cfg.gamma * nxt - values[t]
                gae = delta + self.cfg.gamma * self.cfg.lam * gae
                adv[t] = max(-10.0, min(10.0, gae))
            returns = adv + values
            old_lp = np.log(np.maximum(EPS, np.asarray([s.propensity for s in group])))
            new_lp = np.asarray([self.policy.log_prob(x, s.action)
                                 for x, s in zip(xs, group)])
            ratio = np.exp(np.clip(new_lp - old_lp, -20.0, 20.0))
            for t, s in enumerate(group):
                clipped = min(max(ratio[t], 1 - self.cfg.clip_eps),
                              1 + self.cfg.clip_eps)
                if (adv[t] > 0 and ratio[t] < 1 + self.cfg.clip_eps) or \
                   (adv[t] < 0 and ratio[t] > 1 - self.cfg.clip_eps):
                    coef = -adv[t] * ratio[t]
                    gwi, gbi = self.policy.grad_log_prob(xs[t], s.action)
                    gw += coef * gwi
                    gb += coef * gbi
                vgw += (values[t] - returns[t]) * xs[t]
                vgb += (values[t] - returns[t])
                total += 1
        if total == 0:
            return 0.0
        self.policy.apply_gradient(gw / total, gb / total, self.cfg.lr * lr_scale)
        self.policy.apply_value_gradient(vgw / total, vgb / total,
                                         self.cfg.value_lr * lr_scale)
        return float(np.mean(np.abs(adv)))
