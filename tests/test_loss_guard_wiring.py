"""Wiring tests: Bot -> LossGuard action mapping (SOFT / PULL / HARD).

Offline only.  The guard is disabled unless ``RISK_LOSS_GUARD`` or a threshold
env var is set, so these tests opt in explicitly.
"""
from __future__ import annotations

import asyncio

import pytest

from mmbot.exchange.fake import FakeGateway
from mmbot.main import Bot, GuardAction, loss_guard_config_from_env
from mmbot.models import MarketState, OrderStatus, RiskConfig, Side, StrategyParams


class Clock:
    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _enable(monkeypatch, tmp_path, soft: str = "0.50", hard: str = "0.80") -> None:
    monkeypatch.setenv("RISK_LOSS_GUARD", "1")
    monkeypatch.setenv("RISK_SOFT_LOSS_USD", soft)
    monkeypatch.setenv("RISK_MAX_LOSS_USD", hard)
    monkeypatch.setenv("RISK_HALT_FILE", str(tmp_path / "HALT"))


def _bot(gw: FakeGateway, clk: Clock) -> Bot:
    return Bot(gw, StrategyParams(coin="HYPE", quote_notional=11.0,
                                  max_notional=11.0, min_requote_interval_s=0.0),
               RiskConfig(), now_fn=clk)


# ---- config gating ---------------------------------------------------------
def test_guard_disabled_without_env():
    assert loss_guard_config_from_env({}) is None
    assert loss_guard_config_from_env(
        {"RISK_LOSS_GUARD": "0", "RISK_MAX_LOSS_USD": "0.80"}) is None


def test_guard_enabled_by_thresholds_and_defaults():
    cfg = loss_guard_config_from_env(
        {"RISK_MAX_LOSS_USD": "0.80", "RISK_SOFT_LOSS_USD": "0.50"})
    assert cfg is not None
    assert cfg.soft_loss_usd == pytest.approx(0.50)
    assert cfg.hard_loss_usd == pytest.approx(0.80)
    assert cfg.poll_stale_s == pytest.approx(20.0)
    assert cfg.unreadable_hard_s == pytest.approx(90.0)
    assert cfg.soft_flat_deadline_s == pytest.approx(1200.0)


def test_guard_rejects_soft_at_or_above_hard():
    with pytest.raises(SystemExit):
        loss_guard_config_from_env(
            {"RISK_LOSS_GUARD": "1", "RISK_SOFT_LOSS_USD": "0.90",
             "RISK_MAX_LOSS_USD": "0.80"})


# ---- SOFT: reducing side only ---------------------------------------------
def test_soft_quotes_reducing_side_only(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)

    async def go():
        gw = FakeGateway(coin="HYPE", sz_decimals=2, bid=74.0, ask=74.02,
                         equity=21.0)
        clk = Clock()
        bot = _bot(gw, clk)
        await bot.init()
        assert bot.loss_guard is not None

        gw.set_book(74.0, 74.02)
        await bot.handle_event(gw.events.get_nowait())
        bot.pos.size = 0.14
        bot.loss_guard.on_poll(20.40, 0.14, bot.market.mid)   # -$0.60
        assert bot.loss_guard.evaluate(bot.market.mid, 0.14) is GuardAction.SOFT

        await bot.cycle()
        assert bot._soft_only is True
        # Batch path: adding side is zeroed, reducing side still quotes.
        sizes = bot._tgt_sizes(bot.market.mid)
        assert sizes["buy"] == 0.0 and sizes["sell"] > 0.0
        # Legacy path: every quote sent must reduce the long.
        assert gw.place_calls, "expected a reducing quote"
        assert all(call[0] is Side.SELL for call in gw.place_calls)

    asyncio.run(go())


# ---- PULL: cancel resting quotes ------------------------------------------
def test_pull_cancels_resting_quotes(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)

    async def go():
        gw = FakeGateway(coin="HYPE", sz_decimals=2, bid=74.0, ask=74.02,
                         equity=21.0)
        clk = Clock()
        bot = _bot(gw, clk)
        await bot.init()
        gw.set_book(74.0, 74.02)
        await bot.handle_event(gw.events.get_nowait())
        await bot.cycle()                                  # place normal quotes
        resting = [o for o in bot.om.active()
                   if o.status is OrderStatus.RESTING]
        assert resting, "setup expected resting quotes"

        clk.t += 25.0                                      # poll is now stale
        assert bot.loss_guard.evaluate(bot.market.mid, bot.pos.size) is GuardAction.PULL
        await bot.cycle()

        assert bot._pulled is True
        assert bot.metrics["guard_pulls"] == 1
        assert not [o for o in bot.om.active()
                    if o.status is OrderStatus.RESTING]
        assert gw.cancel_calls or gw.bulk_cancel_calls

    asyncio.run(go())


# ---- HARD: flatten, HALT, exit code ---------------------------------------
def test_hard_stop_flattens_writes_halt_and_sets_exit_code(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)

    async def go():
        gw = FakeGateway(coin="HYPE", sz_decimals=2, bid=74.0, ask=74.02,
                         equity=21.0)
        gw.position = 0.14
        clk = Clock()
        bot = _bot(gw, clk)
        await bot.init()
        assert bot.loss_guard is not None

        bot.market = MarketState("HYPE", 67.99, 68.01, 5.0, 5.0, clk())
        bot.pos.size = 0.14
        bot.loss_guard.on_poll(21.0, 0.14, 74.0)            # baseline mid 74
        assert bot.loss_guard.evaluate(68.0, 0.14) is GuardAction.HARD

        await bot.cycle()

        assert bot.halted is True
        assert bot._hard_exit_code == 3
        assert (tmp_path / "HALT").exists()
        assert gw.position == 0.0
        assert gw.flatten_calls >= 1
        assert bot.stop_event.is_set()

        # run() must propagate the guard's exit code, not the generic halt 2.
        rc = await bot.run()
        assert rc == 3

    asyncio.run(go())


def test_guard_absent_leaves_action_ok(monkeypatch):
    monkeypatch.delenv("RISK_LOSS_GUARD", raising=False)
    monkeypatch.delenv("RISK_MAX_LOSS_USD", raising=False)
    monkeypatch.delenv("RISK_SOFT_LOSS_USD", raising=False)

    async def go():
        gw = FakeGateway(coin="HYPE", sz_decimals=2, bid=74.0, ask=74.02,
                         equity=21.0)
        clk = Clock()
        bot = _bot(gw, clk)
        await bot.init()
        assert bot.loss_guard is None
        gw.set_book(74.0, 74.02)
        await bot.handle_event(gw.events.get_nowait())
        assert bot._guard_action is GuardAction.OK

    asyncio.run(go())
