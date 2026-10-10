import asyncio

import pytest
from mmbot.exchange.client import HyperliquidGateway, _redact
from mmbot.main import Bot
from mmbot.models import RiskConfig, StrategyParams


def test_redact_addresses():
    addr = "0x92Ee65d65F80f7B243EDAa122BE941BbB4CfF3Ba"
    assert _redact(f"user {addr} ok") == "user 0x92Ee...F3Ba ok"
    assert _redact(f"{addr},{addr}") == "0x92Ee...F3Ba,0x92Ee...F3Ba"
    assert _redact("no address here") == "no address here"
    assert _redact("0xshort") == "0xshort"


def gw():
    return HyperliquidGateway("http://localhost", "0x0", "0x0", "BTC")


def test_emit_ignores_unset_loop():
    g = gw()
    assert g._loop is None
    g._emit("ev")


def test_emit_ignores_closed_loop():
    g = gw()
    loop = asyncio.new_event_loop()
    g._loop = loop
    loop.close()
    g._emit("ev")


def test_emit_delivers_on_open_loop():
    g = gw()
    loop = asyncio.new_event_loop()
    g._loop = loop

    async def body():
        g._emit("ev1")
        return await asyncio.wait_for(g.events.get(), 1.0)

    assert loop.run_until_complete(body()) == "ev1"
    loop.close()


class FakeGW:
    def __init__(self, fail_at="account_state"):
        self.fail_at = fail_at
        self.stopped = False

    async def start(self):
        if self.fail_at == "start":
            raise RuntimeError("start failed")

    async def account_state(self):
        if self.fail_at == "account_state":
            raise RuntimeError("account value reads as 0")
        raise AssertionError("unreachable")

    async def stop(self):
        self.stopped = True


@pytest.mark.parametrize("fail_at", ["start", "account_state"])
def test_run_stops_gateway_when_init_fails(fail_at):
    bot = Bot(FakeGW(fail_at), StrategyParams(), RiskConfig())
    with pytest.raises(RuntimeError):
        asyncio.run(bot.run())
    assert bot.gw.stopped
