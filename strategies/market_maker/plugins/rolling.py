"""Rolling-window cooldown plugins.

The legacy plugins in this package trigger on *cumulative session* loss.  That is
appropriate for a bot that trades one UTC session at a time, but it is a poor
model for a continuously-running market maker: a $0.25 drawdown early in the day
can keep the breaker armed long after the account has recovered, and a loss that
spans midnight is split between two sessions.

These plugins instead measure **loss from the rolling 60-minute mark-to-market
equity high-water mark**::

    loss(t) = max(equity[t-60min .. t]) - equity[t]

where equity is marked to market (cash + position * mid + funding - fees).  A
breach arms the cooldown for ``cooldown_hours``.  The trigger window and the
cooldown duration are independent: the window is always 60 minutes, the pause is
1 h / 3 h / 6 h / 12 h depending on the class.

What the engine should do when the breaker arms is expressed by
:class:`~.base.TriggerAction`:

``flatten``
    Close inventory at the BBO immediately, then stop quoting for N hours.
``reduce_only``
    Keep only the quote side that reduces current inventory; stop quoting
    entirely once flat, and resume normal quoting when the window expires.

The legacy ``cancel_all`` action is still supported for the older plugins.

Plugins are selected through the same loader as the legacy ones::

    MM_COOLDOWN_PLUGIN=rolling_6h
    MM_COOLDOWN_ACTION=reduce_only     # or flatten
    MM_COOLDOWN_LOSS_USD=0.25
"""
from __future__ import annotations

from .base import BaseCooldownPlugin, TriggerAction

#: The MTM loss is always measured over a rolling 60-minute window.
ROLLING_WINDOW_S = 3600.0


class RollingWindowCooldown(BaseCooldownPlugin):
    """Shared implementation for the 60-minute-HWM family."""

    loss_window_s = ROLLING_WINDOW_S
    action = TriggerAction.FLATTEN.value


class Rolling1h(RollingWindowCooldown):
    plugin_name = "rolling_1h"
    cooldown_hours = 1.0


class Rolling3h(RollingWindowCooldown):
    plugin_name = "rolling_3h"
    cooldown_hours = 3.0


class Rolling6h(RollingWindowCooldown):
    plugin_name = "rolling_6h"
    cooldown_hours = 6.0


class Rolling12h(RollingWindowCooldown):
    plugin_name = "rolling_12h"
    cooldown_hours = 12.0


__all__ = [
    "ROLLING_WINDOW_S",
    "RollingWindowCooldown",
    "Rolling1h",
    "Rolling3h",
    "Rolling6h",
    "Rolling12h",
]
