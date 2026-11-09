"""T7e covering tests: fast l2Book subscription (env-tunable) + cadence-aware staleness.
Written first: RED against pre-T7e code (no fast_book param / book_cadence_s /
stale_k / restart_n / feed_age metric; REST confirm path still restarts feeds).
test_parse_l2_handles_fast_five_level_payload is the step-2 regression guard (already
passing: parse_l2 reads top-of-book only)."""
import asyncio
from unittest.mock import patch

from mmbot.config import _apply
from mmbot.exchange.client import HyperliquidGateway, parse_l2
from mmbot.exchange.fake import FakeGateway
from mmbot.main import Bot
from mmbot.models import RiskConfig, StrategyParams

PARAMS = StrategyParams(quote_notional=20, max_notional=60, min_requote_interval_s=0.0)

# Real testnet fast payload captured by T7e gap capture (logs/t7e_gapcap_testnet.json, connB)
FAST_MSG = {"channel": "l2Book", "data": {"coin": "BTC", "time": 1791197127027, "levels": [
    [{"px": "86222.0", "sz": "0.3", "n": 1}, {"px": "86218.0", "sz": "0.00024", "n": 1},
     {"px": "86201.0", "sz": "0.00015", "n": 1}, {"px": "86182.0", "sz": "0.00046", "n": 1},
     {"px": "86176.0", "sz": "0.00006", "n": 1}],
    [{"px": "86232.0", "sz": "0.3", "n": 1}, {"px": "86234.0", "sz": "0.00685", "n": 1},
     {"px": "86237.0", "sz": "0.01682", "n": 2}, {"px": "86252.0", "sz": "0.00046", "n": 1},
     {"px": "86297.0", "sz": "0.00015", "n": 1}]], "fast": True}}


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


async def make(gw=None, rcfg=None):
    gw = gw or FakeGateway()
    clk = Clock()
    bot = Bot(gw, PARAMS, rcfg or RiskConfig(), now_fn=clk)
    await bot.init()
    return bot, gw, clk


async def pump(bot, gw):
    while not gw.events.empty():
        await bot.handle_event(gw.events.get_nowait())


def run(coro):
    return asyncio.run(coro)


# ---- step 2: fast subscription + 5-level parse -------------------------------------

def test_parse_l2_handles_fast_five_level_payload():
    data = FAST_MSG["data"]
    assert len(data["levels"][0]) == 5 and len(data["levels"][1]) == 5   # fixture sanity
    ev = parse_l2(FAST_MSG, 7.5)
    assert ev is not None
    m = ev.market
    assert m.bid == 86222.0 and m.ask == 86232.0            # top of book only -> depth irrelevant
    assert m.bid_sz == 0.3 and m.ask_sz == 0.3
    assert m.exch_ts == data["time"] / 1000.0 and m.recv_ts == 7.5


class _Recorder:
    def __init__(self): self.subs = []
    def subscribe(self, sub, cb): self.subs.append(sub)


def test_gateway_subscribes_fast_l2book_by_default():
    g = HyperliquidGateway("http://localhost", "0x0", "0x0", "BTC")
    g.info = _Recorder()
    g._subscribe()
    assert g.info.subs[0] == {"type": "l2Book", "coin": "BTC", "fast": True}
    core = [s["type"] for s in g.info.subs[:3]]
    assert core == ["l2Book", "orderUpdates", "userFills"]
    # Optional microstructure feeds follow the core subscriptions.
    assert "trades" in [s["type"] for s in g.info.subs]


def test_gateway_falls_back_to_default_l2book_when_fast_disabled():
    g = HyperliquidGateway("http://localhost", "0x0", "0x0", "BTC", fast_book=False)
    g.info = _Recorder()
    g._subscribe()
    assert g.info.subs[0] == {"type": "l2Book", "coin": "BTC"}           # no fast key -> default feed


def test_risk_config_env_tunes_k_n_and_fast_flag():
    r = _apply(RiskConfig(), "RISK_",
               {"RISK_STALE_K": "4", "RISK_RESTART_N": "5", "RISK_FAST_BOOK": "0"})
    assert r.stale_k == 4.0 and r.restart_n == 5.0 and r.fast_book is False
    d = RiskConfig()
    assert d.stale_k == 2.0 and d.restart_n == 3.0 and d.fast_book is True   # T7e step-1 proposal


# ---- step 3: cadence + thresholds ---------------------------------------------------

def test_book_cadence_rolling_median_and_warmup():
    g = HyperliquidGateway("http://localhost", "0x0", "0x0", "BTC")
    msg = {"data": {"coin": "BTC", "time": 0,
                    "levels": [[{"px": "1", "sz": "1"}], [{"px": "2", "sz": "1"}]]}}
    assert g.book_cadence_s() is None                        # no samples yet -> legacy fallback
    with patch("mmbot.exchange.client.time") as t:           # gaps: .5 .5 .5 .7 .5
        t.monotonic.side_effect = [0.0, 0.5, 1.0, 1.5, 2.2, 2.7]
        for _ in range(6):
            g._on_l2(msg)
    assert len(g._book_gaps) == 5
    cad = g.book_cadence_s()
    assert cad is not None and abs(cad - 0.5) < 1e-9         # median of the rolling window


def test_stale_confirm_fires_at_k_times_cadence_only():
    async def go():
        gw = FakeGateway(); gw.cadence = 0.5                 # K=2 -> confirm threshold 1.0 s
        bot, gw, clk = await make(gw, RiskConfig(stale_k=2.0))
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        gw.feed_age = 0.9; await bot.cycle()
        assert gw.book_snapshot_calls == 0                   # below K x cadence: no REST check
        gw.feed_age = 1.2; await bot.cycle()
        assert gw.book_snapshot_calls == 1                   # above threshold: quiet confirm
        assert bot.metrics["stale_pulls"] == 0 and not gw.cancel_calls
    run(go())


def test_stale_fallback_uses_stale_book_s_when_cadence_unknown():
    async def go():
        gw = FakeGateway()                                   # cadence None (default)
        bot, gw, clk = await make(gw, RiskConfig(stale_book_s=5.0, stale_k=2.0))
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        gw.feed_age = 4.0; await bot.cycle()
        assert gw.book_snapshot_calls == 0                   # legacy threshold = stale_book_s 5.0
        gw.feed_age = 5.5; await bot.cycle()
        assert gw.book_snapshot_calls == 1
    run(go())


def test_rest_differ_pulls_but_never_restarts():
    async def go():
        gw = FakeGateway(); gw.cadence = 0.5; gw.snapshot_book = (70100.0, 70102.0)
        bot, gw, clk = await make(gw, RiskConfig(stale_k=2.0, restart_n=3.0))
        gw.set_book(69999, 70001); await pump(bot, gw); await bot.cycle(); await pump(bot, gw)
        assert len(gw.orders) == 2
        gw.feed_age = 1.2                                    # > K x cadence (1.0), < N x cadence (1.5)
        await bot.cycle(); await pump(bot, gw)
        assert gw.book_snapshot_calls == 1 and bot.metrics["stale_pulls"] == 1
        assert not gw.orders and gw.cancel_calls             # pulled
        assert gw.restart_calls == 0                         # T7e: REST confirm NEVER restarts
        clk.t += 1; await bot.housekeeping()                 # 1.2 < 1.5 = N x cadence -> no watchdog
        assert gw.restart_calls == 0
        gw.feed_age = 1.6                                    # sustained silence >= N x cadence
        clk.t += 1; await bot.housekeeping()
        assert gw.restart_calls == 1                         # only the watchdog restarts
    run(go())


def test_watchdog_restarts_only_past_n_cadence():
    async def go():
        gw = FakeGateway(); gw.cadence = 0.5
        bot, gw, clk = await make(gw, RiskConfig(stale_k=2.0, restart_n=3.0))
        gw.feed_age = 1.4; await bot.housekeeping()          # < 1.5 = N x cadence
        assert gw.restart_calls == 0
        gw.feed_age = 1.6; await bot.housekeeping()
        assert gw.restart_calls == 1
    run(go())


def test_watchdog_fallback_without_cadence_keeps_legacy_2x_stale():
    async def go():
        gw = FakeGateway()                                   # cadence None
        bot, gw, clk = await make(gw)                        # stale_book_s 3.0 -> fallback 6.0
        gw.feed_age = 5.0; await bot.housekeeping()
        assert gw.restart_calls == 0
        gw.feed_age = 7.0; await bot.housekeeping()
        assert gw.restart_calls == 1
    run(go())


def test_metrics_line_reports_feed_age():
    async def go():
        bot, gw, clk = await make()
        gw.feed_age = 1.25
        assert "feed_age=1.25" in bot.metrics_line()
    run(go())
