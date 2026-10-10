"""Emergency tool: cancel everything and flatten. Run it by hand, or from systemd ExecStopPost.
    python -m mmbot.panic
"""
from __future__ import annotations

import asyncio
import logging

from .config import load_settings
from .exchange.client import HyperliquidGateway
from .safety import cancel_sweep


async def _run() -> None:
    s = load_settings()
    gw = HyperliquidGateway(s.base_url, s.account_address, s.api_secret, s.coin,
                            dry_run=False, fast_book=s.risk.fast_book)
    await gw.start()
    n = await gw.cancel_all()
    leftover = await cancel_sweep(gw)              # T7b: re-query + cancel rounds
    if leftover:
        logging.critical("orders remained after panic cancel rounds: %s", [r.oid for r in leftover])
    else:
        logging.info("panic re-check clean: 0 open orders")
    st = await gw.account_state()
    logging.info("cancelled %d orders; position=%.6f", n, st.size)
    if abs(st.size) > 0:
        await gw.flatten()
        st = await gw.account_state()
    logging.info("final position=%.6f", st.size)
    await gw.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())
