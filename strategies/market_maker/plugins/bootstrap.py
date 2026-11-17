"""Opt-in bootstrap for the cooldown plugins.

Usage options
-------------
1. Launch through the provided wrapper (no engine edits)::

       MM_COOLDOWN_PLUGIN=cooldown_6h venv/bin/python scripts/mm_run.py

2. Import and install from your own launcher before constructing ``Bot``::

       from strategies.market_maker.plugins.bootstrap import install
       install()

3. Inspect the configured plugin without starting the bot::

       MM_COOLDOWN_PLUGIN=cooldown_24h_4blocks \\
           venv/bin/python -m strategies.market_maker.plugins.bootstrap --status
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from .base import BaseCooldownPlugin
from .integration import install_from_env, status

__all__ = ["install", "status"]


def install() -> Optional[BaseCooldownPlugin]:
    """Install the plugin named by ``MM_COOLDOWN_PLUGIN`` (no-op when unset)."""
    return install_from_env()


def _main(argv: list[str]) -> int:
    if "--status" in argv:
        install()
        print(json.dumps(status(), indent=2))
        return 0
    if "--reset" in argv:
        from .loader import load_plugin

        plugin = load_plugin()
        if plugin is None:
            print("MM_COOLDOWN_PLUGIN is not set", file=sys.stderr)
            return 1
        plugin.reset()
        print(json.dumps(plugin.status(), indent=2))
        return 0
    print(
        "Usage: MM_COOLDOWN_PLUGIN=<name> python -m "
        "strategies.market_maker.plugins.bootstrap [--status]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
