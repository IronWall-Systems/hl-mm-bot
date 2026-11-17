"""Non-invasive adapter from cooldown plugins to the existing MM engine.

The engine (``src/mmbot``) is **not** modified.  This module installs three small
class-level wrappers at runtime:

* ``RiskGuard.__init__``       - seeds the plugin with the starting equity
* ``RiskGuard.update_equity``  - feeds every equity poll to the loss tracker
* ``RiskGuard.pre_trade``      - vetoes any quote while a cooldown is active
* ``OrderManager.plan``        - withdraws resting quotes instead of re-placing

The ``OrderManager.plan`` hook is what makes the pause real: for the legacy
``cancel_all`` action targets are replaced with an empty set, so the normal
plan/cancel path cancels existing quotes and places nothing new.  For
``reduce_only`` the hook keeps exactly one quote side, chosen from the sign of
the last position seen by ``RiskGuard.pre_trade``; when the position is flat it
withdraws everything.  ``flatten`` needs a market-close call that only the
launcher can make, so the hook cancels quotes and logs the request.

Install it from a launcher, e.g.::

    from strategies.market_maker.plugins.integration import install_from_env
    install_from_env()

or run ``scripts/mm_run.py`` with ``MM_COOLDOWN_PLUGIN=cooldown_6h``.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

from .base import BaseCooldownPlugin, CooldownDecision, TriggerAction
from .loader import PluginLoadError, load_plugin

log = logging.getLogger("mmbot.cooldown")

_HOOK_ATTR = "_mm_cooldown_plugin"
_ORIG_ATTR = "_mm_cooldown_originals"
_ACTIVE: Optional[BaseCooldownPlugin] = None
_LAST_ERROR_LOG = 0.0
#: Last signed position size seen by ``RiskGuard.pre_trade``.  The order-planning
#: hook uses it to decide which quote side is risk-reducing during a cooldown.
_LAST_POSITION_SIZE = 0.0
_LAST_ACTION = ""


def _ensure_core_importable() -> None:
    """Make ``mmbot`` importable when this package lives at the repo root."""
    from .state import project_root

    src = str(project_root() / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _safe_decision(plugin: BaseCooldownPlugin) -> CooldownDecision:
    """Evaluate defensively. A plugin bug fails closed (no new orders)."""
    global _LAST_ERROR_LOG
    try:
        return plugin.evaluate()
    except Exception as exc:  # pragma: no cover - defensive path
        now = time.time()
        if now - _LAST_ERROR_LOG > 60:
            _LAST_ERROR_LOG = now
            log.exception("[COOLDOWN] %s evaluation failed; blocking new orders", plugin.plugin_name)
        return CooldownDecision(False, f"cooldown plugin error: {exc}")


def install_plugin(plugin: BaseCooldownPlugin) -> BaseCooldownPlugin:
    """Attach one plugin instance to the engine classes (idempotent)."""
    global _ACTIVE
    _ensure_core_importable()

    from mmbot.execution.order_manager import OrderManager
    from mmbot.risk.guard import RiskGuard, Verdict

    existing = getattr(RiskGuard, _HOOK_ATTR, None)
    if existing is not None:
        if existing is plugin:
            return plugin
        raise RuntimeError(
            f"a different cooldown plugin is already installed: {existing.plugin_name!r}"
        )

    global _LAST_POSITION_SIZE, _LAST_ACTION
    _LAST_POSITION_SIZE = 0.0
    _LAST_ACTION = ""

    originals = {
        "RiskGuard.__init__": RiskGuard.__init__,
        "RiskGuard.update_equity": RiskGuard.update_equity,
        "RiskGuard.pre_trade": RiskGuard.pre_trade,
        "OrderManager.plan": OrderManager.plan,
    }

    def guard_init(self, cfg, start_equity, now_fn=time.monotonic):
        originals["RiskGuard.__init__"](self, cfg, start_equity, now_fn)
        try:
            plugin.observe_equity(start_equity)
        except Exception:
            log.exception("[COOLDOWN] failed to seed session equity %.4f", start_equity)

    def update_equity(self, eq: float) -> None:
        originals["RiskGuard.update_equity"](self, eq)
        try:
            plugin.observe_equity(eq)
        except Exception:
            log.exception("[COOLDOWN] failed to observe equity %.4f", eq)

    def pre_trade(self, *args, **kwargs):
        global _LAST_POSITION_SIZE
        # ``pre_trade(quote, position_state, mid, ...)``; remember the sign so the
        # plan hook can keep the risk-reducing side during reduce-only mode.
        try:
            if len(args) >= 2 and hasattr(args[1], "size"):
                _LAST_POSITION_SIZE = float(args[1].size)
        except Exception:  # pragma: no cover - defensive
            pass
        decision = _safe_decision(plugin)
        if not decision.allowed:
            return Verdict(False, decision.reason)
        return originals["RiskGuard.pre_trade"](self, *args, **kwargs)

    def plan(self, targets, now, blocked=frozenset()):
        global _LAST_ACTION
        decision = _safe_decision(plugin)
        if not decision.allowed:
            action = decision.action
            if action != _LAST_ACTION:
                _LAST_ACTION = action
                if action == TriggerAction.REDUCE_ONLY.value:
                    log.warning(
                        "[COOLDOWN] %s: reduce-only mode (position=%+.6f); "
                        "keeping only the risk-reducing side",
                        plugin.plugin_name,
                        _LAST_POSITION_SIZE,
                    )
                elif action == TriggerAction.FLATTEN.value:
                    log.warning(
                        "[COOLDOWN] %s: flatten-on-trigger requested; cancelling quotes "
                        "(live market-close must be performed by the launcher)",
                        plugin.plugin_name,
                    )
            if action == TriggerAction.REDUCE_ONLY.value:
                # Keep the side that shrinks the position; pause entirely when flat.
                keep = "sell" if _LAST_POSITION_SIZE > 0 else "buy" if _LAST_POSITION_SIZE < 0 else None
                if keep is None:
                    targets = ()
                else:
                    targets = [q for q in targets if q.side.value == keep]
            else:
                targets = ()  # cancel_all / flatten: withdraw everything
        else:
            _LAST_ACTION = ""
        return originals["OrderManager.plan"](self, targets, now, blocked)

    setattr(RiskGuard, _ORIG_ATTR, originals)
    setattr(RiskGuard, _HOOK_ATTR, plugin)
    RiskGuard.__init__ = guard_init
    RiskGuard.update_equity = update_equity
    RiskGuard.pre_trade = pre_trade
    OrderManager.plan = plan

    _ACTIVE = plugin
    decision = _safe_decision(plugin)
    log.warning(
        "[COOLDOWN] plugin %s installed (threshold=$%.2f, window=%.1fh, blocks=%d); "
        "state=%s; currently %s%s",
        plugin.plugin_name,
        plugin.loss_threshold_usd,
        plugin.cooldown_hours,
        plugin.blocks,
        plugin.store.path,
        "BLOCKED" if not decision.allowed else "armed/idle",
        f" - {decision.reason}" if decision.reason else "",
    )
    return plugin


def install_from_env() -> Optional[BaseCooldownPlugin]:
    """Load ``MM_COOLDOWN_PLUGIN`` (if set) and hook it into the engine."""
    name = os.environ.get("MM_COOLDOWN_PLUGIN", "").strip()
    if name.lower() in {"", "none", "off", "disabled", "0"}:
        log.info("[COOLDOWN] MM_COOLDOWN_PLUGIN not set; cooldown plugins disabled")
        return None
    plugin = load_plugin(name)
    if plugin is None:  # pragma: no cover - name was non-empty
        return None
    return install_plugin(plugin)


def uninstall() -> None:
    """Restore the original engine methods (mainly for tests)."""
    global _ACTIVE
    _ensure_core_importable()
    from mmbot.execution.order_manager import OrderManager
    from mmbot.risk.guard import RiskGuard

    originals = getattr(RiskGuard, _ORIG_ATTR, None)
    if originals is None:
        return
    RiskGuard.__init__ = originals["RiskGuard.__init__"]
    RiskGuard.update_equity = originals["RiskGuard.update_equity"]
    RiskGuard.pre_trade = originals["RiskGuard.pre_trade"]
    OrderManager.plan = originals["OrderManager.plan"]
    delattr(RiskGuard, _ORIG_ATTR)
    delattr(RiskGuard, _HOOK_ATTR)
    _ACTIVE = None
    log.info("[COOLDOWN] plugin hooks removed")


def active_plugin() -> Optional[BaseCooldownPlugin]:
    return _ACTIVE


def status() -> dict:
    if _ACTIVE is None:
        return {"installed": False, "plugin": None}
    return {"installed": True, **_ACTIVE.status()}


__all__ = [
    "PluginLoadError",
    "active_plugin",
    "install_from_env",
    "install_plugin",
    "status",
    "uninstall",
]
