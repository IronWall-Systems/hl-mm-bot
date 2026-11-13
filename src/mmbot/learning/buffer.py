"""Rolling experience replay buffer (append-only JSONL + atomic compaction).

The buffer is the contract between the trading process (writer) and the
out-of-band trainer (reader).  It holds a *sliding* window, not a growing
archive: ``trim`` drops anything older than ``retention_s`` and compacts the
file atomically, so the model only ever trains on the current regime.

Record types on disk (one JSON object per line)::

    {"type": "step",   "step": {...}}
    {"type": "reward", "step_id": 12, "horizon": 30, "reward": 0.0012, "ts": ...}

Reward records are separate so the hot path never rewrites a whole file just
to attach a delayed 30 s / 300 s reward.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_RETENTION_S = 24 * 3600.0
MAX_RETENTION_S = 7 * 24 * 3600.0
DEFAULT_MAX_STEPS = 250_000


@dataclass
class DecisionStep:
    """One decision tick: causal features, executed action, propensity, reward."""

    step_id: int
    ts: float
    mono: float
    coin: str
    features: Dict[str, float]
    action: str
    propensity: float
    guard_status: str = "ok"
    mid: float = 0.0
    position: float = 0.0
    equity: float = 0.0
    reward_30s: Optional[float] = None
    reward_300s: Optional[float] = None
    reward: Optional[float] = None
    filled: bool = False
    episode_id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DecisionStep":
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def ready(self) -> bool:
        return self.reward is not None


class RollingExperienceBuffer:
    """Time-series buffer with retention trimming and delayed-reward updates."""

    def __init__(self, path: str | os.PathLike,
                 retention_s: float = DEFAULT_RETENTION_S,
                 max_retention_s: float = MAX_RETENTION_S,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 compact_min_steps: int = 500,
                 compact_every_s: float = 300.0,
                 now_fn=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_retention_s = float(max_retention_s)
        self.retention_s = self._clamp_retention(retention_s)
        self.max_steps = int(max_steps)
        self.compact_min_steps = max(1, int(compact_min_steps))
        self.compact_every_s = max(1.0, float(compact_every_s))
        self.now = now_fn
        self._lock = threading.RLock()
        self._steps: List[DecisionStep] = []
        self._by_id: Dict[int, DecisionStep] = {}
        self._next_id = 1
        self._loaded = False
        self._last_compact = -1e18
        self.load()

    # ---- retention -------------------------------------------------------
    def _clamp_retention(self, seconds: float) -> float:
        return max(60.0, min(float(seconds), self.max_retention_s))

    def set_retention(self, seconds: float) -> float:
        with self._lock:
            self.retention_s = self._clamp_retention(seconds)
            return self.retention_s

    @classmethod
    def from_env(cls, path: str | os.PathLike, environ=None) -> "RollingExperienceBuffer":
        env = os.environ if environ is None else environ
        hours = float(env.get("MM_ROLLING_RETENTION_H", "24"))
        max_hours = float(env.get("MM_ROLLING_MAX_RETENTION_H", "168"))
        max_steps = int(env.get("MM_ROLLING_MAX_STEPS", str(DEFAULT_MAX_STEPS)))
        return cls(path, retention_s=hours * 3600.0,
                   max_retention_s=min(max_hours, 168.0) * 3600.0,
                   max_steps=max_steps)

    # ---- persistence -----------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self._steps.clear()
            self._by_id.clear()
            self._next_id = 1
            if self.path.exists():
                try:
                    with self.path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except ValueError:
                                continue
                            if rec.get("type") == "step":
                                try:
                                    step = DecisionStep.from_dict(rec["step"])
                                except (KeyError, TypeError):
                                    continue
                                self._steps.append(step)
                                self._by_id[step.step_id] = step
                                self._next_id = max(self._next_id, step.step_id + 1)
                            elif rec.get("type") == "reward":
                                step = self._by_id.get(int(rec.get("step_id", -1)))
                                if step is None:
                                    continue
                                horizon = int(rec.get("horizon", 0))
                                value = rec.get("reward")
                                if horizon == 30:
                                    step.reward_30s = value
                                elif horizon == 300:
                                    step.reward_300s = value
                                self._recombine(step)
                except OSError:
                    pass
            self._steps.sort(key=lambda s: s.ts)
            # A restart must not resurrect expired history: prune by the
            # retention window (and the hard step cap) immediately.
            cutoff = self.now() - self.retention_s
            keep = [s for s in self._steps if s.ts >= cutoff]
            if len(keep) > self.max_steps:
                keep = keep[-self.max_steps:]
            if len(keep) != len(self._steps):
                self._steps = keep
                self._by_id = {s.step_id: s for s in keep}
                self._compact()
                self._last_compact = self.now()
            self._loaded = True

    def _recombine(self, step: DecisionStep, w30: float = 0.5,
                   w300: float = 0.5) -> None:
        parts = [(step.reward_30s, w30), (step.reward_300s, w300)]
        parts = [(v, w) for v, w in parts if v is not None]
        if len(parts) == 2:
            step.reward = (parts[0][0] * parts[0][1] + parts[1][0] * parts[1][1]) / (
                parts[0][1] + parts[1][1])
        elif len(parts) == 1:
            step.reward = parts[0][0]

    def _append_line(self, rec: Dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), allow_nan=False) + "\n")

    def append(self, step: DecisionStep) -> DecisionStep:
        with self._lock:
            if step.step_id <= 0:
                step.step_id = self._next_id
            self._next_id = max(self._next_id, step.step_id + 1)
            self._steps.append(step)
            self._by_id[step.step_id] = step
            self._append_line({"type": "step", "step": step.to_dict()})
            return step

    def set_reward(self, step_id: int, horizon: int, reward: float) -> None:
        with self._lock:
            step = self._by_id.get(int(step_id))
            if step is None:
                return
            if horizon == 30:
                step.reward_30s = float(reward)
            elif horizon == 300:
                step.reward_300s = float(reward)
            else:
                return
            self._recombine(step)
            self._append_line({"type": "reward", "step_id": int(step_id),
                               "horizon": int(horizon), "reward": float(reward),
                               "ts": round(self.now(), 3)})

    # ---- retention / compaction -----------------------------------------
    def trim(self, now: Optional[float] = None, force: bool = False) -> int:
        """Drop steps older than the retention window; compact the file.

        Returns the number of steps removed.  The rewrite is atomic so a
        concurrent reader never sees a half-written file.
        """
        now = self.now() if now is None else now
        with self._lock:
            cutoff = now - self.retention_s
            keep = [s for s in self._steps if s.ts >= cutoff]
            if len(keep) > self.max_steps:
                keep = keep[-self.max_steps:]
            removed = len(self._steps) - len(keep)
            if removed <= 0 and not force:
                return 0
            self._steps = keep
            self._by_id = {s.step_id: s for s in keep}
            # Rewriting the whole JSONL on every 2 s tick would dominate I/O
            # once the window is full.  Compact in batches / on a timer; the
            # in-memory view is trimmed every call either way.
            if (force or removed >= self.compact_min_steps
                    or now - self._last_compact >= self.compact_every_s):
                self._compact()
                self._last_compact = now
            return removed

    def _compact(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for step in self._steps:
                fh.write(json.dumps({"type": "step", "step": step.to_dict()},
                                    separators=(",", ":"), allow_nan=False) + "\n")
                if step.reward_30s is not None:
                    fh.write(json.dumps({"type": "reward", "step_id": step.step_id,
                                         "horizon": 30, "reward": step.reward_30s},
                                        separators=(",", ":")) + "\n")
                if step.reward_300s is not None:
                    fh.write(json.dumps({"type": "reward", "step_id": step.step_id,
                                         "horizon": 300, "reward": step.reward_300s},
                                        separators=(",", ":")) + "\n")
        os.replace(tmp, self.path)

    # ---- reads -----------------------------------------------------------
    def __len__(self) -> int:
        with self._lock:
            return len(self._steps)

    def all(self) -> List[DecisionStep]:
        with self._lock:
            return list(self._steps)

    def recent(self, window_s: Optional[float] = None,
               now: Optional[float] = None) -> List[DecisionStep]:
        now = self.now() if now is None else now
        with self._lock:
            if window_s is None:
                return list(self._steps)
            lo = now - float(window_s)
            return [s for s in self._steps if s.ts >= lo]

    def ready(self) -> List[DecisionStep]:
        with self._lock:
            return [s for s in self._steps if s.reward is not None]

    def latest_guard_status(self, max_age_s: float = 120.0,
                            now: Optional[float] = None) -> str:
        now = self.now() if now is None else now
        with self._lock:
            for step in reversed(self._steps):
                if now - step.ts <= max_age_s:
                    return step.guard_status
                break
            return "ok"

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            n = len(self._steps)
            ready = sum(1 for s in self._steps if s.reward is not None)
            oldest = self._steps[0].ts if n else None
            newest = self._steps[-1].ts if n else None
            by_guard: Dict[str, int] = {}
            for s in self._steps:
                by_guard[s.guard_status] = by_guard.get(s.guard_status, 0) + 1
            return {"steps": n, "ready": ready, "oldest_ts": oldest,
                    "newest_ts": newest, "retention_s": self.retention_s,
                    "by_guard": by_guard}

    # ---- optional parquet export ----------------------------------------
    def export_parquet(self, path: str | os.PathLike) -> bool:
        """Best-effort columnar export; returns False when pyarrow is absent."""
        try:
            import pyarrow as pa            # type: ignore
            import pyarrow.parquet as pq    # type: ignore
        except Exception:
            return False
        rows = [s.to_dict() for s in self.all()]
        if not rows:
            return False
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, str(path))
        return True
