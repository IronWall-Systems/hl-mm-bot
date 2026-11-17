"""Plugin 2 - a 12-hour pause after a $0.25 cumulative session loss."""
from __future__ import annotations

from .base import BaseCooldownPlugin


class Cooldown12h(BaseCooldownPlugin):
    plugin_name = "cooldown_12h"
    cooldown_hours = 12.0
    blocks = 1
