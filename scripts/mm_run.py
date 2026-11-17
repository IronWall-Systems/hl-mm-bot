#!/usr/bin/env python3
"""Drop-in MM launcher that installs cooldown plugins before the engine starts.

This exists so the plugin system can be enabled without editing ``src/mmbot/main.py``
or any core engine file::

    MM_COOLDOWN_PLUGIN=cooldown_6h venv/bin/python scripts/mm_run.py
    MM_COOLDOWN_PLUGIN=cooldown_12h venv/bin/python scripts/mm_run.py
    MM_COOLDOWN_PLUGIN=cooldown_24h_4blocks venv/bin/python scripts/mm_run.py

With ``MM_COOLDOWN_PLUGIN`` unset it behaves exactly like ``python -m mmbot.main``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Both the repo root (for ``strategies``) and src (for ``mmbot``) must be importable.
for path in (ROOT / "src", ROOT):
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)

from strategies.market_maker.plugins.bootstrap import install  # noqa: E402

install()

from mmbot.main import main  # noqa: E402

if __name__ == "__main__":
    main()
