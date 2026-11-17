"""Dynamic plugin loader.

The active plugin is selected with ``MM_COOLDOWN_PLUGIN`` (for example
``cooldown_6h``).  No engine or runner file needs to change: the loader resolves
the class at runtime and returns a plugin instance for the integration layer.

Supported values:

* a legacy cumulative-session plugin (``cooldown_6h``, ``cooldown_12h``,
  ``cooldown_24h_4blocks``)
* a rolling 60-minute-HWM plugin (``rolling_1h``, ``rolling_3h``,
  ``rolling_6h``, ``rolling_12h``)
* a module name (``my_plugins.weekend_lock``)
* a fully qualified ``module:Class`` reference

Environment overrides:

* ``MM_COOLDOWN_PLUGIN``      plugin name (empty/unset disables the system)
* ``MM_COOLDOWN_LOSS_USD``    loss threshold, default ``0.25``
* ``MM_COOLDOWN_HOURS``       override the plugin's cooldown length
* ``MM_COOLDOWN_ACTION``      ``flatten`` (default) or ``reduce_only`` for the
                               rolling plugins; legacy plugins ignore it
* ``MM_COOLDOWN_STATE_DIR``   directory for persisted state (default ``data/cooldowns``)
"""
from __future__ import annotations

import importlib
import inspect
import os
from typing import Dict, List, Optional, Type

from .base import DEFAULT_LOSS_THRESHOLD_USD, BaseCooldownPlugin

BUILTIN_PLUGINS: Dict[str, str] = {
    "cooldown_6h": "strategies.market_maker.plugins.cooldown_6h:Cooldown6h",
    "cooldown_12h": "strategies.market_maker.plugins.cooldown_12h:Cooldown12h",
    "cooldown_24h_4blocks": "strategies.market_maker.plugins.cooldown_24h_4blocks:Cooldown24h4Blocks",
    "rolling_1h": "strategies.market_maker.plugins.rolling:Rolling1h",
    "rolling_3h": "strategies.market_maker.plugins.rolling:Rolling3h",
    "rolling_6h": "strategies.market_maker.plugins.rolling:Rolling6h",
    "rolling_12h": "strategies.market_maker.plugins.rolling:Rolling12h",
}

PLUGIN_PACKAGE = "strategies.market_maker.plugins"
_DISABLED_VALUES = {"", "none", "off", "disabled", "0"}


class PluginLoadError(RuntimeError):
    """Raised when ``MM_COOLDOWN_PLUGIN`` names a plugin that cannot be loaded."""


def available_plugins() -> List[str]:
    return sorted(BUILTIN_PLUGINS)


def _resolve_spec(name: str) -> str:
    if name in BUILTIN_PLUGINS:
        return BUILTIN_PLUGINS[name]
    if ":" in name:
        return name
    if "." in name:
        return name
    return f"{PLUGIN_PACKAGE}.{name}"


def _discover_class(module, name: str) -> Type[BaseCooldownPlugin]:
    candidates = [
        obj
        for _attr, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseCooldownPlugin)
        and obj is not BaseCooldownPlugin
        and obj.__module__ == module.__name__
    ]
    if not candidates:
        raise PluginLoadError(
            f"module {module.__name__!r} defines no BaseCooldownPlugin subclass"
        )
    if len(candidates) > 1:
        raise PluginLoadError(
            f"module {module.__name__!r} defines multiple plugins "
            f"({', '.join(c.__name__ for c in candidates)}); use 'module:Class'"
        )
    return candidates[0]


def load_plugin(
    name: Optional[str] = None,
    *,
    loss_threshold_usd: Optional[float] = None,
    cooldown_hours: Optional[float] = None,
    action: Optional[str] = None,
    **kwargs,
) -> Optional[BaseCooldownPlugin]:
    """Load and instantiate a plugin, or return ``None`` when no name is configured."""
    name = (name if name is not None else os.environ.get("MM_COOLDOWN_PLUGIN", "")).strip()
    if name.lower() in _DISABLED_VALUES:
        return None

    spec = _resolve_spec(name)
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, attr) if attr else _discover_class(module, name)
    except PluginLoadError:
        raise
    except Exception as exc:
        raise PluginLoadError(
            f"cannot load cooldown plugin {name!r} ({module_name}): {exc}. "
            f"Built-ins: {', '.join(available_plugins())}"
        ) from exc

    if not (inspect.isclass(cls) and issubclass(cls, BaseCooldownPlugin)):
        raise PluginLoadError(f"{spec!r} is not a BaseCooldownPlugin subclass")

    if loss_threshold_usd is None:
        raw = os.environ.get("MM_COOLDOWN_LOSS_USD")
        loss_threshold_usd = float(raw) if raw else DEFAULT_LOSS_THRESHOLD_USD
    if cooldown_hours is None:
        raw = os.environ.get("MM_COOLDOWN_HOURS")
        cooldown_hours = float(raw) if raw else None
    if action is None:
        action = os.environ.get("MM_COOLDOWN_ACTION") or None
    if loss_threshold_usd <= 0:
        raise PluginLoadError("loss threshold must be > 0")

    return cls(
        loss_threshold_usd=loss_threshold_usd,
        cooldown_hours=cooldown_hours,
        action=action,
        **kwargs,
    )
