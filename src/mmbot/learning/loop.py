"""Out-of-band rolling loop: settle rewards, trim, train, checkpoint.

Runs in its own process so a training burst or a checkpoint fsync can never
block the quoting loop.  LossGuard state is read from (in priority order) the
HALT marker, an injected guard function, the recorder's state file, or the
newest buffered step.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .buffer import RollingExperienceBuffer
from .recorder import LearningRecorder
from .trainer import RollingTrainer, TrainResult

TRUTHY = ("1", "true", "yes", "on")


class RollingLearningLoop:
    def __init__(self, buffer: RollingExperienceBuffer, trainer: RollingTrainer,
                 *, recorder: Optional[LearningRecorder] = None,
                 interval_s: float = 60.0,
                 stop_event: Optional[asyncio.Event] = None,
                 now_fn=time.time,
                 guard_fn: Optional[Callable[[], str]] = None,
                 guard_state_path: Optional[str | os.PathLike] = None,
                 heartbeat_path: Optional[str | os.PathLike] = None,
                 heartbeat_every_s: float = 5.0,
                 log_fn: Optional[Callable[[str], None]] = None):
        self.buffer = buffer
        self.trainer = trainer
        self.recorder = recorder
        self.interval_s = float(interval_s)
        self.stop_event = stop_event
        self.now = now_fn
        self.guard_fn = guard_fn
        self.guard_state_path = Path(guard_state_path) if guard_state_path else None
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self.log = log_fn or (lambda _m: None)
        self.last_result: Optional[TrainResult] = None
        self.iterations = 0
        self.heartbeat_every_s = float(heartbeat_every_s)
        self._last_heartbeat = -1e9

    # ---- guard precedence ------------------------------------------------
    def guard_status(self, now: Optional[float] = None) -> str:
        now = self.now() if now is None else now
        halt = self.trainer.cfg.halt_path
        if halt and Path(halt).exists():
            return "hard"
        if self.guard_fn is not None:
            try:
                return str(self.guard_fn() or "ok").lower()
            except Exception:
                return "pull"                     # fail safe: pause training
        if self.guard_state_path and self.guard_state_path.exists():
            try:
                rec = json.loads(self.guard_state_path.read_text(encoding="utf-8"))
                if now - float(rec.get("ts", 0.0)) <= 120.0:
                    return str(rec.get("guard", "ok")).lower()
            except (OSError, ValueError):
                pass
        return self.buffer.latest_guard_status(now=now)

    # ---- one iteration ---------------------------------------------------
    def run_once(self, now: Optional[float] = None,
                 force_heartbeat: bool = False) -> TrainResult:
        now = self.now() if now is None else now
        if self.recorder is not None:
            try:
                self.recorder.settle(now)
            except Exception as exc:
                self.log(f"settle failed: {exc!r}")
        removed = self.buffer.trim(now)
        if removed:
            self.log(f"trimmed {removed} expired steps")
        guard = self.guard_status(now)
        result = self.trainer.maybe_train(now, guard_status=guard)
        self.last_result = result
        self.iterations += 1
        self._heartbeat(now, guard, result, force=force_heartbeat)
        return result

    async def run_forever(self) -> None:
        while self.stop_event is None or not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"loop iteration failed: {exc!r}")
            await asyncio.sleep(self.interval_s)

    # ---- heartbeat -------------------------------------------------------
    def _heartbeat(self, now: float, guard: str, result: TrainResult,
                   force: bool = False) -> None:
        if self.heartbeat_path is None:
            return
        if not force and now - self._last_heartbeat < self.heartbeat_every_s:
            return
        self._last_heartbeat = now
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            rec: Dict[str, Any] = {
                "ts": round(now, 3), "guard": guard,
                "iterations": self.iterations,
                "steps": len(self.buffer), "ready": len(self.buffer.ready()),
                "policy_version": self.trainer.policy.version,
                "trained_steps": self.trainer.policy.trained_steps,
                "last_train": self.trainer.last_train_ts,
                "last_result": result.to_dict(),
                "recorder": self.recorder.status() if self.recorder else None,
            }
            tmp = self.heartbeat_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.heartbeat_path)
        except OSError:
            pass
