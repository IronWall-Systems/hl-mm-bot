"""Plugin 1 - a 6-hour pause after a $0.25 cumulative session loss."""
from __future__ import annotations

from .base import BaseCooldownPlugin


class Cooldown6h(BaseCooldownPlugin):
    plugin_name = "cooldown_6h"
    cooldown_hours = 6.0
    blocks = 1
