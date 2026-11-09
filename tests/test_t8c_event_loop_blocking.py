"""T8c (REQ-0028.2): fills must not wait behind the quoting cycle's REST calls.

RED test for the PROPOSED change (diff shown to the human before applying).
Defect (T8b evidence): event_loop() ends every batch with `await self.maybe_cycle()`, and
cycle() holds self._lock across place/cancel REST calls -> a fill that arrives while a
place/cancel REST is in flight is applied only after that REST returns.
T8b timestamps: WS fill receipt 18:32:12,381 -> applied late; T8a: fill WS receipt
12:43:41.393 -> FILL line 12:43:43.477 (+2.084 s, inside cancel RESTs at 42.507/43.476).

Expected: FAILED with the current event_loop (fill blocked), PASSED once quoting runs in its
own task (see the diff shown for REQ-0028.2).
"""
import asyncio

from mmbot.exchange.fake import FakeGateway
from mmbot.exchange.types import FillEvent
from mmbot.main import Bot
from mmbot.models import RiskConfig, Side, StrategyParams
from mmbot.strategy.quoter import compute_quotes

PARAMS = StrategyParams(quote_notional=20, max_notional=60, min_requote_interval_s=0.0)
RISK = RiskConfig(max_order_notional=25, max_position_notional=80)


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


class SlowGateway(FakeGateway):
    """place_alo() blocks on a gate the test controls -> deterministic 'REST in flight' window."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.placing = False

    async def place_alo(self, side, price, size, cloid):
        self.placing = True
        try:
            await self.gate.wait()
        finally:
            self.placing = False
        return await super().place_alo(side, price, size, cloid)


async def make():
    gw = SlowGateway()
    clk = Clock()
    bot = Bot(gw, PARAMS, RISK, now_fn=clk)
    await bot.init()
    bot._last_cycle = -1e9                          # quoting may run
    gw.set_book(70000, 70001)                       # book event queued for the event loop
    return bot, gw, clk


def run(coro):
    return asyncio.run(coro)


async def wait_until(cond, timeout=2.0):
    """Real-time wait (asyncio.sleep), used only for cross-task choreography."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def test_fill_is_applied_while_cycle_awaits_a_rest_call():
    async def go():
        bot, gw, clk = await make()
        task = asyncio.create_task(bot.event_loop())
        # T8c hunk B/C: quoting lives in its own task once the four hunks are applied
        cycle_task = asyncio.create_task(bot.cycle_loop()) if hasattr(bot, "cycle_loop") else None
        try:
            assert await wait_until(lambda: gw.placing), "cycle never reached a place REST"
            gw.events.put_nowait(FillEvent("BTC", Side.BUY, 70000.5, 0.00017, 1, 990001, 0.0, 0.0, False))
            applied = await wait_until(lambda: abs(bot.pos.size - 0.00017) < 1e-12, timeout=0.4)
            assert applied, ("fill waited behind the quoting cycle's REST call: local pos still "
                             f"{bot.pos.size} after the REST was in flight")
        finally:
            bot.stop_event.set()                     # let event_loop() drain and exit on its own
            gw.gate.set()                            # release the cycle
            cycle_task.cancel()
            await asyncio.gather(task, cycle_task, return_exceptions=True)
            await asyncio.sleep(0)
    run(go())


class SlowModifyGateway(FakeGateway):
    """modify_quotes() blocks on a gate -> a deterministic window between compute_quotes() and
    the re-read of inventory."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.gate.set()                              # open: cycle 1 places normally
        self.modifying = False

    async def modify_quotes(self, modifies):
        self.modifying = True
        try:
            await self.gate.wait()
        finally:
            self.modifying = False
        return await super().modify_quotes(modifies)


async def pump(bot, gw):
    while not gw.events.empty():
        await bot.handle_event(gw.events.get_nowait())


def test_quote_sizing_is_re_read_right_before_placing():
    """REQ-0029: a fill landing during the modify RESTs must be reflected in the resting quotes."""
    async def go():
        gw = SlowModifyGateway()
        clk = Clock()
        bot = Bot(gw, PARAMS, RISK, now_fn=clk)
        await bot.init()
        bot._last_cycle = -1e9
        gw.set_book(69999, 70001)
        await pump(bot, gw)
        await bot.cycle()                            # cycle 1: two quotes resting at inventory 0
        await pump(bot, gw)
        assert len(gw.orders) == 2
        gw.gate.clear()                              # the repricing modify will block
        gw.set_book(70014, 70016)                    # ~2.1 bps: reprice both sides, no order fills
        await pump(bot, gw)
        task = asyncio.create_task(bot.cycle())
        try:
            assert await wait_until(lambda: gw.modifying), "cycle never reached a modify REST"
            await bot.handle_event(FillEvent("BTC", Side.BUY, 70006.0, 0.0005, 1, 990002, 0.0, 0.0, False))
            assert abs(bot.pos.size - 0.0005) < 1e-12
            gw.gate.set()
            await task
        finally:
            gw.gate.set()
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        fresh = compute_quotes(bot.market, bot.pos.size, PARAMS, gw.spec)
        stale = compute_quotes(bot.market, 0.0, PARAMS, gw.spec)
        assert fresh != stale, "sanity: inventory skew must move the targets"
        assert len(gw.orders) == 2
        for q in fresh:
            hit = any(o["side"] is q.side and abs(o["px"] - q.price) < 1e-9
                      and abs(o["sz"] - q.size) < 1e-12 for o in gw.orders.values())
            assert hit, (f"resting {q.side.value} quote does not use the post-fill inventory: "
                         f"resting={[(o['side'], o['px'], o['sz']) for o in gw.orders.values()]} "
                         f"fresh={[(x.side, x.price, x.size) for x in fresh]}")
    run(go())
