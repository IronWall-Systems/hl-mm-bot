"""T8c: position-drift race — failing tests written BEFORE the fix; replays of the T8b log cases.

T8b evidence (logs/bot.log, 2026-10-05):
  18:32:14,995 FILL buy 0.000170 @ 85657.000000 pos=0.000170
  18:32:14,996 WARNING position drift local=0.000170 exchange=0.000000 -> adopting exchange
  18:32:19,646 WARNING position drift local=0.000000 exchange=0.000170 -> adopting exchange
The account poll was issued BEFORE the fill landed and its stale answer was adopted AFTER it,
so for 4.65 s the bot believed it was flat while long. T8a had the mirror case
(12:43:43,477 local=0.000010 exchange=0.000020 -> adopting exchange).

Required behaviour (REQ-0028.1):
  * poll records issue time + a fill-sequence counter;
  * adoption is skipped when any fill was applied while the poll was in flight;
  * a genuine difference is adopted only after 2 agreeing polls;
  * rtt and lock wait are logged.
"""
import asyncio
import logging

from mmbot.exchange.fake import FakeGateway
from mmbot.exchange.types import FillEvent
from mmbot.main import Bot
from mmbot.models import PositionState, RiskConfig, Side, StrategyParams

PARAMS = StrategyParams(quote_notional=20, max_notional=60, min_requote_interval_s=0.0)
RISK = RiskConfig(max_order_notional=25, max_position_notional=80)


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


class ScriptedGateway(FakeGateway):
    """account_state() answers from a script; a step may apply a fill while the poll is in flight."""

    def __init__(self):
        super().__init__()
        self.bot = None
        self.script = []
        self.polls = 0

    async def account_state(self):
        self.polls += 1
        if self.script:
            return await self.script.pop(0)(self)
        return await super().account_state()


def _st(gw, size, equity=None):
    return PositionState(size=size, entry_px=0.0, unrealized_pnl=0.0, margin_used=0.0,
                         account_value=gw.equity if equity is None else equity)


def reply(size, equity=None):
    async def step(gw):
        return _st(gw, size, equity)
    return step


def reply_with_fill_in_flight(size, ev, equity=None):
    """Exchange answers with its PRE-fill view AFTER the fill has been applied locally."""
    async def step(gw):
        await gw.bot.handle_event(ev)
        return _st(gw, size, equity)
    return step


def fill(sz, tid, px=85657.0, side=Side.BUY, snapshot=False):
    return FillEvent("BTC", side, px, sz, 1, tid, 0.0, 0.0, snapshot)


async def make(gw=None):
    gw = gw or ScriptedGateway()
    clk = Clock()
    bot = Bot(gw, PARAMS, RISK, now_fn=clk)
    await bot.init()
    gw.bot = bot
    bot._last_cycle = clk.t + 1000                  # isolate: no quoting cycle in housekeeping()
    return bot, gw, clk


def run(coro):
    return asyncio.run(coro)


def test_poll_issued_before_a_fill_must_not_be_adopted(caplog):
    """T8b case: exchange=0 read issued before the fill, answered after it -> keep the fresh local size."""
    async def go():
        bot, gw, clk = await make()
        bot.pos.size = 0.000000                      # flat when the poll was issued
        ev = fill(0.000170, 777001)
        gw.script = [reply_with_fill_in_flight(0.000000, ev)]   # stale (pre-fill) answer
        with caplog.at_level(logging.WARNING, logger="mmbot"):
            await bot.housekeeping()
        assert bot.pos.size == 0.000170, "stale in-flight read must not overwrite the local position"
        assert bot.metrics["pos_drift"] == 1        # the difference is still OBSERVED and reported
        assert any("deferred" in r.getMessage() for r in caplog.records), "deferral must be logged"
    run(go())


def test_two_agreeing_polls_required_before_adoption(caplog):
    async def go():
        bot, gw, clk = await make()
        bot.pos.size = 0.000010
        gw.script = [reply(0.000020)]
        with caplog.at_level(logging.WARNING, logger="mmbot"):
            await bot.housekeeping()                                    # poll 1
        assert bot.pos.size == 0.000010, "a single differing poll must not be adopted"
        assert any("awaiting confirm" in r.getMessage() for r in caplog.records)
        clk.t += 6
        gw.script = [reply(0.000020)]
        await bot.housekeeping()                                        # poll 2 agrees
        assert bot.pos.size == 0.000020, "two agreeing polls must be adopted"
        assert any("2 agreeing polls" in r.getMessage() for r in caplog.records)
    run(go())


def test_disagreeing_polls_never_adopt():
    async def go():
        bot, gw, clk = await make()
        bot.pos.size = 0.000010
        gw.script = [reply(0.000020)]
        await bot.housekeeping()                                        # X
        clk.t += 6
        gw.script = [reply(0.000030)]
        await bot.housekeeping()                                        # Y != X
        assert bot.pos.size == 0.000010, "only one agreeing predecessor: keep local"
        clk.t += 6
        gw.script = [reply(0.000030)]
        await bot.housekeeping()                                        # Y again
        assert bot.pos.size == 0.000030, "Y, Y -> adopt"
    run(go())


def test_agreeing_poll_with_local_clears_the_streak():
    async def go():
        bot, gw, clk = await make()
        bot.pos.size = 0.000010
        gw.script = [reply(0.000020)]
        await bot.housekeeping()                                        # pending = 0.000020
        clk.t += 6
        gw.script = [reply(0.000010)]
        await bot.housekeeping()                                        # agrees with local -> streak reset
        clk.t += 6
        gw.script = [reply(0.000020)]
        await bot.housekeeping()                                        # first of a NEW streak
        assert bot.pos.size == 0.000010, "streak must restart after an agreeing poll"
        clk.t += 6
        gw.script = [reply(0.000020)]
        await bot.housekeeping()                                        # second of the new streak
        assert bot.pos.size == 0.000020
    run(go())


def test_rtt_and_lock_wait_are_logged(caplog):
    async def go():
        bot, gw, clk = await make()
        bot.pos.size = 0.000010
        gw.script = [reply(0.000020)]
        with caplog.at_level(logging.WARNING, logger="mmbot"):
            await bot.housekeeping()
        msgs = [r.getMessage() for r in caplog.records if "position drift" in r.getMessage()]
        assert msgs, "drift line missing"
        assert "rtt=" in msgs[0] and "lock_wait=" in msgs[0], f"timing missing from {msgs[0]}"
    run(go())


def test_fill_sequence_counts_only_applied_fills():
    async def go():
        bot, gw, clk = await make()
        assert bot._fill_seq == 0
        await bot.handle_event(fill(0.000010, 777002))
        assert bot._fill_seq == 1
        await bot.handle_event(fill(0.000010, 777002))                  # duplicate tid: ignored
        assert bot._fill_seq == 1, "dedupe must not bump the sequence"
        await bot.handle_event(fill(9.0, 777003, side=Side.SELL, snapshot=True))
        assert bot._fill_seq == 1, "reconnect snapshot must not bump the sequence"
    run(go())


def test_t8b_timeline_replay_keeps_local_position(caplog):
    """T8b 18:32:14,996 -> 18:32:19,646, replayed end to end (the correction line disappears)."""
    async def go():
        bot, gw, clk = await make()
        bot.pos.size = 0.000000                      # flat when the poll was issued
        with caplog.at_level(logging.WARNING, logger="mmbot"):
            gw.script = [reply_with_fill_in_flight(0.000000, fill(0.000170, 777004))]
            await bot.housekeeping()                # stale 0.000000 while the buy fill is in flight
            assert bot.pos.size == 0.000170, "line 1 must NOT have adopted exchange=0"
            clk.t += 6
            gw.script = [reply(0.000170)]           # fresh poll agrees with local
            await bot.housekeeping()
        assert bot.pos.size == 0.000170, "final position must be the fresh local one"
        assert bot.pos.account_value == gw.equity, "equity must refresh even while size is deferred"
        assert bot.guard.equity == gw.equity
        assert bot.metrics["pos_drift"] == 1
        adopted = [r.getMessage() for r in caplog.records if "adopting exchange" in r.getMessage()]
        assert not adopted, f"stale read was adopted: {adopted}"
    run(go())


def test_t8b_second_line_now_needs_two_agreeing_polls():
    """T8b line 2 input (local=0 exchange=0.000170): blind adoption is replaced by confirmation."""
    async def go():
        bot, gw, clk = await make()
        bot.pos.size = 0.000000                      # e.g. a WS fill was lost locally
        gw.script = [reply(0.000170)]
        await bot.housekeeping()                    # poll 1: difference seen, NOT adopted
        assert bot.pos.size == 0.000000, "a single differing poll must not be adopted"
        clk.t += 6
        gw.script = [reply(0.000170)]
        await bot.housekeeping()                    # poll 2 agrees with poll 1
        assert bot.pos.size == 0.000170, "two agreeing polls must be adopted"
        assert bot.metrics["pos_drift"] == 2
    run(go())
