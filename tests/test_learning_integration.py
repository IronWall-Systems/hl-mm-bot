"""Bot integration: opt-in recorder, guard-tagged steps, no behaviour change."""
from __future__ import annotations

import asyncio
import time

from mmbot.exchange.fake import FakeGateway
from mmbot.learning.policy import ACTIONS
from mmbot.main import Bot
from mmbot.models import MarketState, RiskConfig, StrategyParams


def _bot(monkeypatch, tmp_path, *, guard: bool = False):
    monkeypatch.setenv("MM_LEARNING", "1")
    monkeypatch.setenv("MM_ROLLING_BUFFER", str(tmp_path / "buf.jsonl"))
    monkeypatch.setenv("MM_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.setenv("MM_GUARD_STATE", str(tmp_path / "guard.json"))
    if guard:
        monkeypatch.setenv("RISK_LOSS_GUARD", "1")
        monkeypatch.setenv("RISK_SOFT_LOSS_USD", "0.50")
        monkeypatch.setenv("RISK_MAX_LOSS_USD", "0.80")
        monkeypatch.setenv("RISK_HALT_FILE", str(tmp_path / "HALT"))
    gw = FakeGateway(coin="HYPE", sz_decimals=2, bid=74.0, ask=74.02,
                     equity=21.0)
    bot = Bot(gw, StrategyParams(coin="HYPE", quote_notional=11.0,
                                 max_notional=11.0, min_requote_interval_s=0.0),
              RiskConfig())
    return bot, gw


def test_cycle_records_a_decision_step(monkeypatch, tmp_path):
    async def go():
        bot, gw = _bot(monkeypatch, tmp_path)
        await bot.init()
        assert bot.learning is not None
        gw.set_book(74.0, 74.02)
        await bot.handle_event(gw.events.get_nowait())
        await bot.cycle()
        assert len(bot.learning.buffer) >= 1
        step = bot.learning.buffer.all()[-1]
        assert step.action in ACTIONS
        assert 0.0 < step.propensity <= 1.0
        assert step.coin == "HYPE"
        assert step.guard_status == "ok"

    asyncio.run(go())


def test_soft_guard_tags_step_reduce_only(monkeypatch, tmp_path):
    async def go():
        bot, gw = _bot(monkeypatch, tmp_path, guard=True)
        await bot.init()
        bot.market = MarketState("HYPE", 74.0, 74.02, 5.0, 5.0, time.monotonic())
        bot.pos.size = 0.14
        bot.loss_guard.on_poll(20.40, 0.14, bot.market.mid)   # -$0.60 -> SOFT
        await bot.cycle()
        step = bot.learning.buffer.all()[-1]
        assert step.guard_status == "soft"
        assert step.action == "reduce_only"
        assert step.propensity == 1.0

    asyncio.run(go())


def test_learning_is_off_without_env(monkeypatch):
    monkeypatch.delenv("MM_LEARNING", raising=False)

    async def go():
        gw = FakeGateway(coin="HYPE", sz_decimals=2, bid=74.0, ask=74.02,
                         equity=21.0)
        bot = Bot(gw, StrategyParams(coin="HYPE", quote_notional=11.0,
                                     max_notional=11.0), RiskConfig())
        await bot.init()
        assert bot.learning is None

    asyncio.run(go())
