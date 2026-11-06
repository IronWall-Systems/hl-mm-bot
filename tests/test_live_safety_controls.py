"""Live safety-port tests: account-loss kill-switch, $40 emergency flatten,
and the veto-path pending/resting order purge.

These cover the three controls ported from the strict replay onto the
``live-safety-trial`` branch.  They are additive; no pre-registered behaviour
is changed unless the new env vars/fields are set.
"""
import asyncio

from mmbot.config import _apply
from mmbot.exchange.fake import FakeGateway
from mmbot.exchange.types import PlaceKind, PlaceResult
from mmbot.main import Bot
from mmbot.models import MarketState, PositionState, Quote, RiskConfig, Side, StrategyParams
from mmbot.risk.guard import KillReason, RiskGuard


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


def guard(start=1000.0, **kw):
    return RiskGuard(RiskConfig(**kw), start, lambda: 0.0)


# ---- hard account-equity kill-switch ---------------------------------------
def test_max_account_loss_usd_triggers_kill():
    g = guard(start=1000.0, max_account_loss_usd=5.0)
    g.update_equity(996.0)
    pos = PositionState(account_value=996.0)
    assert g.check_kill(pos, 70000) is None          # $4 loss < $5 budget
    g.update_equity(994.99)
    assert g.check_kill(pos, 70000) is KillReason.MAX_ACCOUNT_LOSS


def test_max_account_loss_usd_zero_disables():
    g = guard(start=1000.0, max_account_loss_usd=0.0, max_loss_pct=0.9,
              max_drawdown_pct=0.9, emergency_flatten_notional=0.0)
    g.update_equity(800.0)
    assert g.check_kill(PositionState(account_value=800), 70000) is None


def test_max_account_loss_env_mapping():
    cfg = _apply(RiskConfig(), "RISK_", {
        "RISK_MAX_ACCOUNT_LOSS_USD": "3.5",
        "RISK_EMERGENCY_FLATTEN_NOTIONAL": "40",
    })
    assert cfg.max_account_loss_usd == 3.5
    assert cfg.emergency_flatten_notional == 40.0


# ---- $40 emergency taker flatten -------------------------------------------
def test_emergency_flatten_notional_triggers_kill():
    g = guard(start=1000.0, max_account_loss_usd=0.0,
              emergency_flatten_notional=40.0)
    # 0.0006 BTC * 70,000 = $42 >= $40
    pos = PositionState(size=0.0006, account_value=1000.0)
    assert g.check_kill(pos, 70000) is KillReason.EMERGENCY_FLATTEN
    pos_ok = PositionState(size=0.0005, account_value=1000.0)   # $35
    assert g.check_kill(pos_ok, 70000) is None


def test_emergency_flatten_notional_zero_disables():
    g = guard(start=1000.0, max_account_loss_usd=0.0,
              emergency_flatten_notional=0.0, hard_position_mult=100.0)
    pos = PositionState(size=0.01, account_value=1000.0)        # $700
    assert g.check_kill(pos, 70000) is None


def test_cycle_emergency_flatten_cancels_flattens_and_halts():
    async def go():
        gw = FakeGateway()
        clk = Clock()
        bot = Bot(gw, StrategyParams(quote_notional=20, max_notional=60),
                  RiskConfig(max_order_notional=25, max_position_notional=80,
                             max_account_loss_usd=0.0,
                             emergency_flatten_notional=40.0),
                  now_fn=clk)
        await bot.init()
        bot.market = MarketState("BTC", 69999.0, 70001.0, 5.0, 5.0, clk.t)
        bot.pos = PositionState(size=0.001, account_value=1000.0)   # $70 >> $40
        await bot.cycle()
        assert bot.halted and bot.kill_reason is KillReason.EMERGENCY_FLATTEN
        assert gw.flatten_calls == 1
        assert bot.stop_event.is_set()

    run(go())


# ---- veto-path order purge --------------------------------------------------
def test_veto_purges_resting_same_side_orders():
    async def go():
        gw = FakeGateway()
        clk = Clock()
        # A tight position cap makes the second BUY quote a guaranteed veto.
        bot = Bot(gw, StrategyParams(quote_notional=20, max_notional=60),
                  RiskConfig(max_order_notional=100, max_position_notional=30,
                             max_account_loss_usd=0.0,
                             emergency_flatten_notional=0.0),
                  now_fn=clk)
        await bot.init()
        bot.market = MarketState("BTC", 69999.0, 70001.0, 5.0, 5.0, clk.t)

        # Seed one resting BUY worth ~$28.
        q = Quote(Side.BUY, 69999.0, 0.0004)
        o = bot.om.register_pending(q, clk.t)
        bot.om.on_place_result(o, PlaceResult(PlaceKind.RESTING, oid=123), clk.t)
        assert bot.om.active(Side.BUY)

        # Another BUY would push worst-case exposure 28 + 21 > 30 -> veto.
        await bot._do_places([Quote(Side.BUY, 69999.0, 0.0003)], clk.t, 0.0)

        assert bot.metrics["risk_rejects"] == 1
        assert bot.metrics["veto_purges"] == 1
        assert not bot.om.active(Side.BUY)
        assert gw.cancel_calls, "the resting buy should have been cancelled"

    run(go())
