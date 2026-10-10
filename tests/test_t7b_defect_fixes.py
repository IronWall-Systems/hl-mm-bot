"""T7b covering tests: (1) stale-feed REST confirm, (2) windowed reject counters,
(3) shutdown/kill/panic cancel rounds. Written first: every test is RED against the
pre-fix code (no book_snapshot / safety.cancel_sweep / record_reject / REJECT_WINDOW;
account poll still resets the error counter)."""
import asyncio
import logging
from types import SimpleNamespace

from mmbot.exchange.fake import FakeGateway
from mmbot.exchange.types import PlaceKind, RemoteOrder
from mmbot.main import Bot
from mmbot.models import PositionState, RiskConfig, Side, StrategyParams
from mmbot.risk.guard import KillReason, RiskGuard

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


# ---- defect 1: stale feed -> one REST l2 confirm ------------------------------------

def test_parse_l2_snapshot_rest_payload():
    from mmbot.exchange.client import parse_l2_snapshot        # added by the fix
    raw = {"coin": "BTC", "time": 1712000000123,
           "levels": [[{"px": "70000.0", "sz": "1.5"}, {"px": "69999.0", "sz": "2.0"}],
                      [{"px": "70001.0", "sz": "1.0"}]]}
    ms = parse_l2_snapshot(raw, 1.0)
    assert ms.bid == 70000.0 and ms.ask == 70001.0            # REST payload == WS data body
    ms2 = parse_l2_snapshot({"data": raw}, 1.0)                # tolerate a data-wrapped form
    assert ms2.bid == 70000.0 and ms2.ask == 70001.0
    assert parse_l2_snapshot({"coin": "BTC", "time": 0, "levels": [[], []]}, 1.0) is None


def test_stale_rest_quiet_confirms_and_still_quotes():
    """Silence past stale_book_s + REST snapshot == last book -> quiet: quote anyway."""
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw)
        gw.feed_age = 4.0                                       # > stale_book_s (3.0)
        await bot.cycle(); await pump(bot, gw)
        assert gw.book_snapshot_calls == 1                      # exactly one REST confirm
        assert len(gw.orders) == 2                              # quiet -> keep quoting
        assert bot.metrics["stale_pulls"] == 0 and not gw.cancel_calls
    run(go())


def test_stale_rest_same_book_keeps_existing_quotes_one_confirm_per_episode():
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        assert len(gw.orders) == 2
        gw.feed_age = 4.0
        await bot.cycle(); await pump(bot, gw)
        assert gw.book_snapshot_calls == 1
        assert len(gw.orders) == 2 and not gw.cancel_calls      # quiet: no pull
        assert bot.metrics["stale_pulls"] == 0
        clk.t += 1; await bot.cycle()                           # still silent: no 2nd REST call
        assert gw.book_snapshot_calls == 1 and bot.metrics["stale_pulls"] == 0
        gw.set_book(69999.5, 70001.5); await pump(bot, gw)      # new book event ends the episode
        gw.feed_age = 4.0
        await bot.cycle(); await pump(bot, gw)
        assert gw.book_snapshot_calls == 2                      # next silence confirms again
        assert bot.metrics["stale_pulls"] == 0
    run(go())


def test_stale_rest_diff_pulls_quotes_but_never_restarts():        # T7e: spec change
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        assert len(gw.orders) == 2
        gw.feed_age = 4.0
        gw.snapshot_book = (70100.0, 70102.0)                   # REST says the book moved
        await bot.cycle(); await pump(bot, gw)
        assert gw.book_snapshot_calls == 1
        assert bot.metrics["stale_pulls"] == 1 and not gw.orders  # real staleness: pull
        assert gw.cancel_calls and gw.restart_calls == 0         # T7e: REST confirm NEVER restarts
        gw.feed_age = 4.0                                        # still silent after the pull
        clk.t += 1; await bot.cycle()
        assert gw.book_snapshot_calls == 1 and bot.metrics["stale_pulls"] == 1  # one confirm+pull per episode
        gw.snapshot_book = None                                 # feed recovered; next confirm quiet
        gw.set_book(70100, 70102); await pump(bot, gw)
        await bot.cycle(); await pump(bot, gw)
        assert gw.book_snapshot_calls == 2 and len(gw.orders) == 2
    run(go())


# ---- defect 2: windowed reject counters ----------------------------------------------

def test_proposed_reject_window_defaults():
    cfg = RiskConfig()
    assert cfg.side_pause_rejects_60s == 6                      # N: pause one side
    assert cfg.kill_rejects_5m == 20                            # M: kill switch
    assert cfg.side_pause_s == 60.0


def test_reject_windows_age_out_and_success_does_not_clear():
    clk = Clock()
    g = RiskGuard(RiskConfig(), 1000.0, now_fn=clk)
    for _ in range(6):
        g.record_reject(Side.BUY)
    assert g.side_rejects_60s(Side.BUY) == 6
    assert g.side_rejects_60s(Side.SELL) == 0
    for _ in range(14):                                         # 20 total -> kill
        g.record_reject(Side.SELL)
    pos = PositionState(account_value=1000.0)
    assert g.check_kill(pos, 70000) == KillReason.REJECT_WINDOW
    g.record_success()                                          # order-action success must not clear windows
    assert g.rejects_5m_count() == 20 and g.side_rejects_60s(Side.BUY) == 6
    clk.t += 61                                                 # side window (60 s) ages out
    assert g.side_rejects_60s(Side.BUY) == 0 and g.rejects_5m_count() == 20  # 5 min window keeps them
    clk.t += 240                                                # >5 min since the rejects
    assert g.rejects_5m_count() == 0
    assert g.check_kill(pos, 70000) is None


def test_account_poll_success_does_not_reset_error_counter():
    """DEFECT: a successful account poll must not wipe order-action consecutive errors."""
    async def go():
        bot, gw, clk = await make()
        bot.guard.record_error(); bot.guard.record_error()
        assert bot.guard.consecutive_errors == 2
        clk.t += 6
        bot._last_cycle = clk.t + 100                           # isolate: skip the quoting cycle
        await bot.housekeeping()                                # poll succeeds (Fake) ...
        assert bot.guard.consecutive_errors == 2                # ... must NOT reset the counter
    run(go())


def test_account_poll_success_does_not_reset_reject_windows():
    async def go():
        bot, gw, clk = await make()
        for _ in range(3):
            bot.guard.record_reject(Side.BUY)
        before = bot.guard.side_rejects_60s(Side.BUY)
        clk.t += 6
        bot._last_cycle = clk.t + 100
        await bot.housekeeping()
        assert bot.guard.side_rejects_60s(Side.BUY) == before == 3
    run(go())


def test_bot_pauses_side_after_n_rejects_in_window(caplog):
    async def go():
        cfg = RiskConfig(max_order_notional=25, max_position_notional=80,
                         side_pause_rejects_60s=2, side_pause_s=60.0, kill_rejects_5m=1000)
        bot, gw, clk = await make(rcfg=cfg)
        gw.set_book(69999, 70001); await pump(bot, gw)
        gw.forced = [PlaceKind.REJECT_OTHER] * 4
        with caplog.at_level(logging.WARNING, logger="mmbot"):
            await bot.cycle(); await pump(bot, gw)              # 1st reject per side -> 5 s block
            clk.t += 6                                          # blocks expire
            await bot.cycle(); await pump(bot, gw)              # 2nd reject per side -> 60 s pause
        assert bot.metrics["other_rejects"] == 4 and not bot.halted
        assert bot._blocked_until[Side.BUY] >= clk.t + 60 - 1e-9
        assert bot._blocked_until[Side.SELL] >= clk.t + 60 - 1e-9
        assert any("pausing buy" in r.getMessage() for r in caplog.records)
        assert any("pausing sell" in r.getMessage() for r in caplog.records)
    run(go())


# ---- defect 3: shutdown / kill / panic cancel rounds -----------------------------------

def test_cancel_sweep_rounds_catch_late_order():
    async def go():
        from mmbot.safety import cancel_sweep                    # added by the fix
        gw = FakeGateway()
        gw.late_orders = [RemoteOrder("BTC", 42, Side.BUY, 69000.0, 0.001, None)]
        await gw.cancel_all()                                   # bulk cancel; late order lands after it
        leftover = await cancel_sweep(gw)
        assert leftover == [] and gw.hidden_orders == []
        assert gw.cancel_all_calls == 1                         # sweep re-queries + cancels per oid
    run(go())


def test_shutdown_requery_cancel_rounds_clear_late_landing_order(caplog):
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        gw.late_orders = [RemoteOrder("BTC", 42, Side.BUY, 69000.0, 0.001, None)]
        with caplog.at_level(logging.WARNING, logger="mmbot"):
            await bot._shutdown_actions(flatten=False)
            rows = await gw.open_orders()
            assert rows == []                                       # late-landed order caught by round 2
            assert gw.cancel_all_calls == 2                         # init hygiene + one in _shutdown_actions
            assert not any("remained after" in r.getMessage() for r in caplog.records)
    run(go())


def test_shutdown_logs_when_order_remained_after_rounds(caplog):
    async def go():
        bot, gw, clk = await make()
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        gw.ghost_orders = [RemoteOrder("BTC", 66, Side.SELL, 71000.0, 0.001, None)]  # un-cancellable
        with caplog.at_level(logging.CRITICAL, logger="mmbot"):
            await bot._shutdown_actions(flatten=False)
        assert gw.cancel_all_calls == 2                             # init hygiene + one in _shutdown_actions
        assert any(r.levelno == logging.CRITICAL and "remained after" in r.getMessage()
                   for r in caplog.records)
    run(go())


def test_panic_requery_cancel_rounds_logs_remainder(monkeypatch, caplog):
    import mmbot.panic as panic_mod
    fake = FakeGateway()
    fake.ghost_orders = [RemoteOrder("BTC", 77, Side.BUY, 69000.0, 0.001, None)]
    monkeypatch.setattr(panic_mod, "load_settings", lambda: SimpleNamespace(
        base_url="http://x", account_address="0x0", api_secret="k", coin="BTC",
        risk=SimpleNamespace(fast_book=True)))                  # T7e: panic passes fast_book
    monkeypatch.setattr(panic_mod, "HyperliquidGateway", lambda *a, **k: fake)
    with caplog.at_level(logging.CRITICAL):
        asyncio.run(panic_mod._run())
    assert fake.cancel_all_calls == 1
    assert any("remained after" in r.getMessage() for r in caplog.records)
