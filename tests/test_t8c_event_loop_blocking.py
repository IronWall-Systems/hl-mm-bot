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
            await asyncio.wait_for(self.gate.wait(), timeout=5.0)
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
            gw.gate.set()                            # release the cycle
            for t in (task, cycle_task):
                if t is not None:
                    t.cancel()
            for t in (task, cycle_task):
                if t is not None:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
            await asyncio.sleep(0)
    run(go())


class SlowCancelGateway(FakeGateway):
    """cancel() blocks on a gate -> a deterministic window between compute_quotes() and places."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.gate.set()                              # open: cycle 1 places normally
        self.cancelling = False

    async def cancel(self, cloid):
        self.cancelling = True
        await asyncio.wait_for(self.gate.wait(), timeout=5.0)
        return await super().cancel(cloid)


async def pump(bot, gw):
    while not gw.events.empty():
        await bot.handle_event(gw.events.get_nowait())


def test_quote_sizing_is_re_read_right_before_placing():
    """REQ-0029: a fill landing during the cancel RESTs must be reflected in the quotes placed."""
    async def go():
        gw = SlowCancelGateway()
        clk = Clock()
        bot = Bot(gw, PARAMS, RISK, now_fn=clk)
        await bot.init()
        bot._last_cycle = -1e9
        gw.set_book(69999, 70001)
        await pump(bot, gw)
        await bot.cycle()                            # cycle 1: two quotes resting at inventory 0
        await pump(bot, gw)
        assert len(gw.orders) == 2
        gw.gate.clear()                              # the repricing cancels will block
        gw.set_book(70014, 70016)                    # ~2.1 bps: reprice both sides, no order fills
        await pump(bot, gw)
        task = asyncio.create_task(bot.cycle())
        try:
            assert await wait_until(lambda: gw.cancelling), "cycle never reached a cancel REST"
            await bot.handle_event(FillEvent("BTC", Side.BUY, 70006.0, 0.00017, 1, 990002, 0.0, 0.0, False))
            assert abs(bot.pos.size - 0.00017) < 1e-12
            gw.gate.set()
            await task
        finally:
            gw.gate.set()
        fresh = compute_quotes(bot.market, bot.pos.size, PARAMS, gw.spec)
        stale = compute_quotes(bot.market, 0.0, PARAMS, gw.spec)
        assert fresh != stale, "sanity: inventory skew must move the targets"
        placed = [(side, px, sz) for side, px, sz, _ in gw.place_calls]
        for q in fresh:
            hit = any(side is q.side and abs(px - q.price) < 1e-9 and abs(sz - q.size) < 1e-12
                      for side, px, sz in placed)
            assert hit, (f"placed {q.side.value} quote does not use the post-fill inventory: "
                         f"placed={placed} fresh={[(x.side, x.price, x.size) for x in fresh]}")
    run(go())
