"""Cooldown / circuit-breaker plugins for the Hyperliquid market maker.

Public API::

    from strategies.market_maker.plugins import load_plugin, install_from_env

    plugin = load_plugin("cooldown_6h")
    install_from_env()      # reads MM_COOLDOWN_PLUGIN, hooks the engine
"""
from .base import BaseCooldownPlugin, CooldownDecision, TriggerAction
from .loader import BUILTIN_PLUGINS, PluginLoadError, available_plugins, load_plugin

__all__ = [
    "BUILTIN_PLUGINS",
    "BaseCooldownPlugin",
    "CooldownDecision",
    "PluginLoadError",
    "TriggerAction",
    "available_plugins",
    "load_plugin",
]
