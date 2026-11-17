"""Plugin 3 - a 24-hour pause structured as 4 sequential 6-hour check blocks.

The breaker stays armed for the full 24 hours.  Every 6 hours the plugin records
a completed "check" (logged and persisted as ``block_index``), which makes the
progress observable and restart-safe without shortening the window.
"""
from __future__ import annotations

from .base import BaseCooldownPlugin


class Cooldown24h4Blocks(BaseCooldownPlugin):
    plugin_name = "cooldown_24h_4blocks"
    cooldown_hours = 24.0
    blocks = 4
    # BaseCooldownPlugin derives block_hours = cooldown_hours / blocks = 6.0.
