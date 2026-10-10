"""Shutdown safety: multi-round cancel sweep used by the shutdown path, the kill path
(which goes through _shutdown_actions) and mmbot.panic.

Why: an in-flight place can land on the exchange AFTER cancel_all's open-orders query
(T7b/D3 shutdown race). The sweep re-queries open orders and cancels again, up to 3
rounds, and reports whatever still remained so the caller can log it loudly.
"""
from __future__ import annotations

import logging
from typing import Any, List

log = logging.getLogger("mmbot")


async def cancel_sweep(gw: Any, rounds: int = 3) -> List[Any]:
    """Re-query open orders and cancel again, up to `rounds` rounds.

    Returns the orders still open after the final re-query ([] = clean). If a re-query
    itself fails, the last successfully-known open list is returned (conservative:
    the caller logs it as a possible remainder).
    """
    leftover: List[Any] = []
    for rnd in range(1, rounds + 1):
        try:
            rows = await gw.open_orders()
        except Exception as e:
            log.warning("cancel_sweep round %d: open_orders failed: %r", rnd, e)
            return leftover
        if not rows:
            return []
        leftover = rows
        log.warning("cancel_sweep round %d: %d order(s) still open: cancelling %s",
                    rnd, len(rows), [r.oid for r in rows])
        for r in rows:
            try:
                res = await gw.cancel_oid(r.oid)
                if not (res.ok or res.already_gone):
                    log.warning("cancel_sweep: oid=%s not cancelled: %s", r.oid, res.error)
            except Exception as e:
                log.warning("cancel_sweep: cancel oid=%s failed: %r", r.oid, e)
    try:
        return await gw.open_orders()
    except Exception:
        return leftover
