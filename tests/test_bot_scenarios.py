"""End-to-end orchestrator scenarios against the in-memory exchange (deterministic fake clock)."""
import asyncio
import signal

from mmbot.exchange.fake import FakeGateway
from mmbot.exchange.types import FeedDownEvent, PlaceKind, RemoteOrder
from mmbot.main import Bot
from mmbot.models import OrderStatus, RiskConfig, Side, StrategyParams

PARAMS = StrategyParams(quote_notional=20, max_notional=60, min_requote_interval_s=0.0)
RISK = RiskConfig(max_order_notional=25, max_position_notional=80)


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


async def make(gw=None, rcfg=RISK):
    gw = gw or FakeGateway()
    clk = Clock()
    bot = Bot(gw, PARAMS, rcfg, now_fn=clk)
    await bot.init()
    return bot, gw, clk


async def pump(bot, gw):
    while not gw.events.empty():
        await bot.handle_event(gw.events.get_nowait())


def run(coro):
    return asyncio.run(coro)


def test_quotes_both_sides_then_hysteresis_then_requote():
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001)
        await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        assert len(gw.orders) == 2
        n_places = len(gw.place_calls)
        clk.t += 1; gw.set_book(69999.5, 70001.5); await pump(bot, gw); await bot.cycle()   # tiny move
        assert len(gw.place_calls) == n_places and not gw.cancel_calls
        clk.t += 1; gw.set_book(70019, 70021); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        assert len(gw.cancel_calls) == 2                                                    # cancel first
        clk.t += 1; await bot.cycle(); await pump(bot, gw)                                  # then replace
        px = {o["side"]: o["px"] for o in gw.orders.values()}
        assert len(px) == 2 and px[Side.BUY] > 69985 and px[Side.SELL] > 70040   # both repriced up with the book
    run(go())


def test_crossed_book_rejection_does_not_crash_and_backs_off():
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw)
        gw.set_book(70500, 70502, publish=False)         # exchange moved up; our book view is stale
        await bot.cycle(); await pump(bot, gw)
        # stale sell (~70029) would cross the true bid -> post-only rejects it; the bid still rests
        assert bot.metrics["cross_rejects"] == 1 and not bot.halted
        assert [o["side"] for o in gw.orders.values()] == [Side.BUY]
        calls = len(gw.place_calls)
        clk.t += 0.1; await bot.cycle()                  # sell side is in back-off: no hammering
        assert not any(c[0] is Side.SELL for c in gw.place_calls[calls:])
        gw.set_book(70500, 70502); await pump(bot, gw)   # fresh book arrives
        for _ in range(3):                               # cancel stale bid, then place fresh quotes
            clk.t += 10; await bot.cycle(); await pump(bot, gw)
        assert len(gw.orders) == 2 and all(o["px"] > 70400 for o in gw.orders.values())
    run(go())


def test_forced_reject_other_and_error_paths():
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw)
        gw.forced = [PlaceKind.ERROR, PlaceKind.REJECT_OTHER]
        await bot.cycle()
        assert bot.metrics["place_errors"] == 1 and bot.metrics["other_rejects"] == 1 and not bot.halted
        assert bot._force_reconcile                       # unknown state -> reconcile requested
    run(go())


def test_stale_feed_pulls_quotes_watchdog_restarts_on_silence():
    """T7e: silent feed + REST l2 snapshot DIFFERS from the last book -> pull quotes but do NOT
    restart from the cycle (REST confirm never restarts). The N x cadence housekeeping watchdog
    (legacy 2 x stale_book_s fallback with cadence cold) and FeedDown restart still fire on top.
    (Same-book quiet confirm lives in test_t7b_defect_fixes.)"""
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        assert len(gw.orders) == 2
        gw.feed_age = 7.0
        gw.snapshot_book = (70500.0, 70502.0)              # REST disagrees -> real staleness
        await bot.cycle(); await pump(bot, gw)
        assert not gw.orders                              # pulled while blind
        assert bot.metrics["stale_pulls"] == 1 and gw.restart_calls == 0   # T7e: no cycle restart
        gw.feed_age = 7.0                               # silence persists after the pull
        clk.t += 20; await bot.housekeeping()              # cadence cold -> fallback 2x3.0=6.0 < 7
        assert gw.restart_calls == 1                       # watchdog restart
        await bot.handle_event(FeedDownEvent("test")); assert gw.restart_calls == 2
    run(go())


def test_pending_prune_and_orphan_cleanup():
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw)
        gw.forced = [PlaceKind.ERROR]                     # buy placement result lost
        await bot.cycle(); await pump(bot, gw)
        lost = [o for o in bot.om.orders.values() if o.status is OrderStatus.PENDING_NEW]
        assert len(lost) == 1
        gw.hidden_orders.append(RemoteOrder("BTC", 999, Side.BUY, 60000, 0.0003, None))
        clk.t += 6; await bot.housekeeping()
        assert lost[0].status is OrderStatus.REJECTED     # pruned as stale
        assert not gw.hidden_orders and bot.metrics["orphans_cancelled"] == 1
    run(go())


def test_drawdown_kill_cancels_flattens_and_stops():
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        gw.position = 0.0005
        gw.equity = 900.0                                  # -10%
        clk.t += 6; await bot.housekeeping()               # poll equity (T8c hunk D: no cycle inside)
        await bot.cycle()                                  # T8c: the kill check runs in the quoting cycle
        assert bot.halted and bot.stop_event.is_set()
        assert gw.cancel_all_calls >= 1 and gw.flatten_calls == 1 and gw.position == 0.0 and not gw.orders
    run(go())


def test_latency_spike_kills():
    async def go():
        bot, gw, clk = await make(rcfg=RiskConfig(max_latency_ms=100, latency_breach_count=2,
                                                   max_order_notional=25, max_position_notional=80))
        gw.latency_ms = 900.0
        gw.set_book(69999, 70001); await pump(bot, gw)
        for _ in range(4):
            clk.t += 1; await bot.cycle(); await pump(bot, gw)
            gw.set_book(gw.bid + 100, gw.ask + 100); await pump(bot, gw)
        assert bot.halted and bot.kill_reason.value == "latency_spike"
        assert gw.flatten_calls == 1
    run(go())


def test_fill_updates_position_dedupes_and_skews_quotes():
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        gw.set_book(69900, 69902); await pump(bot, gw)    # price trades through our resting bid
        assert bot.pos.size > 0 and bot.metrics["fills"] == 1
        size = bot.pos.size
        from mmbot.exchange.types import FillEvent
        await bot.handle_event(FillEvent("BTC", Side.BUY, 69972, 0.00028, 1, 1, 0, 0, False))  # same tid replayed
        assert bot.pos.size == size and bot.metrics["fills"] == 1
        await bot.handle_event(FillEvent("BTC", Side.BUY, 1, 9, 1, 2, 0, 0, True))              # snapshot ignored
        assert bot.pos.size == size
    run(go())


def test_sigterm_wiring_and_graceful_stop_zero():
    """Covering test for BUILD_SPEC §4 SIGTERM: run() registers SIGINT/SIGTERM ->
    stop_event, then exits 0 with cancel_all + flatten ("verified flat"), not halted."""
    async def go():
        gw = FakeGateway()
        clk = Clock()
        bot = Bot(gw, PARAMS, RISK, now_fn=clk)
        loop = asyncio.get_running_loop()
        seen: list = []
        orig = loop.add_signal_handler

        def spy(sig, callback, *args):
            seen.append(sig)
            return orig(sig, callback, *args)

        loop.add_signal_handler = spy  # type: ignore[method-assign]

        async def stop_soon():
            await asyncio.sleep(0.1)
            bot.stop_event.set()

        st = asyncio.create_task(stop_soon())
        rc = await bot.run()
        await st
        assert rc == 0, rc
        assert signal.SIGTERM in seen and signal.SIGINT in seen, seen
        assert gw.cancel_all_calls >= 1 and gw.flatten_calls >= 1
        assert not gw.orders and not bot.halted
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(s)
    run(go())
