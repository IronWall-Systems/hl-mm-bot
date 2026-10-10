"""T8d: reconcile must not judge local orders against an open_orders() snapshot it raced.

T8c soak evidence (logs/bot.log, 2026-10-05, 12 prunes + 11 orphans):
  22:12:46,xxx order 0x72b8225d no longer on exchange: marked canceled   (order placed 22:12:46)
  22:14:04,xxx orphan order on exchange (oid=61937051755): cancelling    (lived 78.3 s)
housekeeping() awaits gw.open_orders() BEFORE taking the lock (main.py), so a place/cancel can
complete while the snapshot is in flight and reconcile() (order_manager.py:173-184) then judges
the new state against the old snapshot:
  * a RESTING order acked during the snapshot is not in it, and the 5 s grace applies only to
    PENDING_NEW -> marked CANCELED (stale_pruned), although it rests on the exchange;
  * the false CANCELED record is evicted after keep_terminal_s=60 s -> the still-live order
    becomes an orphan and is cancelled (age at orphan in the log: 61.7-78.3 s = 60 s + <=15 s).
All 12 prunes in the log happened 0.0-0.7 s after their own place (12/12) - that is the race.

Required behaviour (REQ-0030, Option A chosen by the human):
  * _order_seq bumps when a place completes AND when a cancel completes (order set changed);
  * a reconcile round whose snapshot overlapped such a change is deferred (never applied).
"""
import asyncio
import logging

from mmbot.exchange.fake import FakeGateway
from mmbot.main import Bot
from mmbot.models import OrderStatus, RiskConfig, StrategyParams

PARAMS = StrategyParams(quote_notional=20, max_notional=60, min_requote_interval_s=0.0)
RISK = RiskConfig(max_order_notional=25, max_position_notional=80)


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


class RacingGateway(FakeGateway):
    """open_orders() returns the rows seen BEFORE `race` runs, so the race lands mid-snapshot."""

    def __init__(self, race):
        super().__init__()
        self._race = race
        self.bot = None
        self.raced = False

    async def open_orders(self):
        rows = await super().open_orders()          # snapshot taken NOW
        if not self.raced:
            self.raced = True
            await self._race(self)                  # order set changes while the snapshot is in flight
        return rows                                 # ... and the stale view is what reconcile gets


async def make(race=None, gw=None):
    gw = gw or (RacingGateway(race) if race else FakeGateway())
    clk = Clock()
    bot = Bot(gw, PARAMS, RISK, now_fn=clk)
    await bot.init()
    gw.bot = bot
    return bot, gw, clk


async def pump(bot, gw):
    while not gw.events.empty():
        await bot.handle_event(gw.events.get_nowait())


def run(coro):
    return asyncio.run(coro)


async def place_during_snapshot(gw):
    """A normal quoting cycle (register_pending -> place_alo -> on_place_result) inside the snapshot."""
    await gw.bot.cycle()


async def cancel_during_snapshot(gw):
    """A cancel completes inside the snapshot; nothing is placed afterwards."""
    bot = gw.bot
    await bot._do_cancels(list(bot.om.active()), bot.now())


def test_place_during_the_open_orders_snapshot_is_never_pruned(caplog):
    async def go():
        bot, gw, clk = await make(place_during_snapshot)
        gw.set_book(69999, 70001)
        await pump(bot, gw)                                  # book known, no orders yet
        with caplog.at_level(logging.DEBUG, logger="mmbot"):
            await bot.housekeeping()                         # reconcile fires (first round)
        assert gw.raced and len(gw.orders) == 2               # the race really placed 2 orders
        assert bot.metrics["stale_pruned"] == 0, \
            "order acked during the snapshot was judged against the stale snapshot"
        assert len(bot.om.active()) == 2
        assert all(o.status is OrderStatus.RESTING for o in bot.om.active())
        assert bot.metrics["orphans_cancelled"] == 0
        assert "no longer on exchange" not in caplog.text
        assert "reconcile deferred" in caplog.text
    run(go())


def test_cancel_during_the_open_orders_snapshot_defers_the_round(caplog):
    async def go():
        bot, gw, clk = await make(cancel_during_snapshot)
        gw.set_book(69999, 70001)
        await pump(bot, gw)
        await bot.cycle()                                    # 2 resting orders
        assert len(gw.orders) == 2
        with caplog.at_level(logging.DEBUG, logger="mmbot"):
            await bot.housekeeping()                         # snapshot has them, cancel lands inside it
        assert gw.raced and not gw.orders                    # exchange side is now empty
        assert all(not o.is_active for o in bot.om.orders.values())   # cancelled locally
        assert bot.metrics["stale_pruned"] == 0
        assert bot.metrics["orphans_cancelled"] == 0
        # the round overlapped a change of the order set -> it must not have been applied
        assert "reconcile deferred" in caplog.text
    run(go())


def test_reconcile_still_runs_when_nothing_changed_during_the_snapshot(caplog):
    """Control: without a race the round is applied (orphan detection still works)."""
    class QuietGateway(FakeGateway):
        async def open_orders(self):
            return []                                       # bot has orders, exchange says otherwise

    async def go():
        bot, gw, clk = await make(gw=QuietGateway())
        gw.set_book(69999, 70001)
        await pump(bot, gw)
        await bot.cycle()
        assert len(gw.orders) == 2                           # on the exchange, invisible to open_orders()
        with caplog.at_level(logging.DEBUG, logger="mmbot"):
            await bot.housekeeping()
        assert bot.metrics["stale_pruned"] == 2             # genuinely gone -> still pruned
        assert bot.om.active() == []
        assert "reconcile deferred" not in caplog.text
    run(go())
