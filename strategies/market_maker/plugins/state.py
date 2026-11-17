"""Persistent cooldown state.

The state is written as JSON with an atomic replace so a crash mid-write can never
leave a truncated file.  Cooldown timestamps are wall-clock epoch seconds (not the
engine's monotonic clock) because they must survive a process restart.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("mmbot.cooldown")

STATE_VERSION = 1


def project_root() -> Path:
    """Repository root (``.../hl-mm-bot``), derived from this file's location."""
    return Path(__file__).resolve().parents[3]


def default_state_dir() -> Path:
    override = os.environ.get("MM_COOLDOWN_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return project_root() / "data" / "cooldowns"


def default_state_path(plugin_name: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in plugin_name)
    return default_state_dir() / f"mm_{safe}.json"


@dataclass
class CooldownState:
    """Everything a cooldown plugin needs to survive a restart."""

    version: int = STATE_VERSION
    plugin: str = ""
    # Session accounting (baseline captured the first time positive equity is seen).
    session_start_equity: Optional[float] = None
    last_equity: Optional[float] = None
    session_realized_pnl: float = 0.0
    # Active cooldown window (wall-clock epoch seconds).
    cooldown_started_at: float = 0.0
    cooldown_until: float = 0.0
    trigger_loss_usd: float = 0.0
    trigger_reason: str = ""
    # Watermark so one loss episode arms the breaker exactly once.
    loss_at_last_trigger: float = 0.0
    # Block bookkeeping (used by multi-block plugins).
    block_index: int = 0
    armed_count: int = 0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CooldownState":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known}
        state = cls(**clean)
        state.version = STATE_VERSION
        return state

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CooldownStateStore:
    """Load/save a single plugin's state file."""

    def __init__(self, path: Path, plugin_name: str, persist: bool = True):
        self.path = Path(path)
        self.plugin_name = plugin_name
        self.persist = persist

    def load(self) -> CooldownState:
        if not self.persist or not self.path.exists():
            return CooldownState(plugin=self.plugin_name)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = CooldownState.from_dict(raw)
        except Exception as exc:  # corrupt / hand-edited file: start clean, never crash the bot
            log.warning("cooldown state %s unreadable (%r); starting fresh", self.path, exc)
            return CooldownState(plugin=self.plugin_name)
        if state.plugin and state.plugin != self.plugin_name:
            log.warning(
                "cooldown state %s belongs to %r, expected %r; starting fresh",
                self.path, state.plugin, self.plugin_name,
            )
            return CooldownState(plugin=self.plugin_name)
        state.plugin = self.plugin_name
        return state

    def save(self, state: CooldownState) -> None:
        if not self.persist:
            return
        state.plugin = self.plugin_name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
        # Same-directory temp file + os.replace => atomic on POSIX and Windows.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.stem}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
