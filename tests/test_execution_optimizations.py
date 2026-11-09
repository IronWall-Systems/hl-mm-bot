"""Execution-latency optimisations: bulk place/modify/cancel and oid rotation.

Covers:
  * response parsers for ``bulk_orders``, ``batchModify`` and ``bulk_cancel``;
  * ``OrderManager`` oid rotation on a successful modify (old oid dies, new one maps);
  * orchestrator routing: one L1 action per diff bucket (place / modify / cancel),
    with oid-bearing cancels on ``cancel_oids`` and cloid-only cancels on
    ``cancel_cloids``;
  * the monotonic SDK nonce that makes back-to-back actions legal.
"""
import asyncio

from mmbot.exchange.client import (install_monotonic_nonce, parse_bulk_cancel_response,
                                   parse_bulk_place_response, parse_modify_response)
from mmbot.exchange.fake import FakeGateway
from mmbot.exchange.types import PlaceKind, PlaceResult
from mmbot.execution.order_manager import OrderManager
from mmbot.main import Bot
from mmbot.models import MarketState, Quote, RiskConfig, Side, StrategyParams

PARAMS = StrategyParams(quote_notional=20, max_notional=60, min_requote_interval_s=0.0)
RISK = RiskConfig(max_order_notional=25, max_position_notional=80)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


async def make():
    gw = FakeGateway()
    clk = Clock()
    bot = Bot(gw, PARAMS, RISK, now_fn=clk)
    await bot.init()
    bot.market = MarketState("BTC", 70000.0, 70001.0, 5.0, 5.0, clk.t)
    return bot, gw, clk


# ---- response parsers -------------------------------------------------------
def _ok(statuses):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


def test_bulk_place_parser_maps_every_leg():
    resp = _ok([{"resting": {"oid": 11}}, {"resting": {"oid": 12}}])
    out = parse_bulk_place_response(resp, 2)
    assert [r.kind for r in out] == [PlaceKind.RESTING, PlaceKind.RESTING]
    assert [r.oid for r in out] == [11, 12]


def test_modify_parser_returns_new_oids_and_handles_short_response():
    resp = _ok([{"resting": {"oid": 21}}])
    out = parse_modify_response(resp, 2)
    assert out[0].kind is PlaceKind.RESTING and out[0].oid == 21
    assert out[1].kind is PlaceKind.ERROR           # never silently misalign legs


def test_bulk_cancel_parser_flags_success_and_already_gone():
    resp = _ok(["success", {"error": "Order was never placed, already canceled, or filled."}])
    out = parse_bulk_cancel_response(resp, 2)
    assert out[0].ok and not out[1].ok and out[1].already_gone


# ---- order manager: oid rotation ------------------------------------------
def test_modify_rotates_oid_and_retires_the_old_mapping():
    om = OrderManager(PARAMS)
    o = om.register_pending(Quote(Side.BUY, 70000.0, 0.0003), 0.0)
    om.on_place_result(o, PlaceResult(PlaceKind.RESTING, oid=100), 0.0)
    assert om._by_oid[100] is o

    om.mark_modify_sent(o, 1.0)
    assert o.status.value == "pending_modify"
    om.on_modify_result(o, Quote(Side.BUY, 70010.0, 0.0003),
                        PlaceResult(PlaceKind.RESTING, oid=101), 1.1)

    assert o.oid == 101 and o.price == 70010.0
    assert 100 not in om._by_oid and om._by_oid[101] is o
    # A late update on the retired oid must not resurrect or alias the order.
    from mmbot.exchange.types import OrderUpdateEvent
    assert om.on_order_update(OrderUpdateEvent(100, None, "filled", Side.BUY, 70000.0, 0.0003), 1.2) is None
    assert o.status.value == "resting"


# ---- orchestrator routing ---------------------------------------------------
def test_places_use_one_bulk_action():
    async def go():
        bot, gw, _ = await make()
        quotes = [Quote(Side.BUY, 69990.0, 0.00028), Quote(Side.SELL, 70011.0, 0.00028)]
        await bot._do_places(quotes, 0.0, 0.0)
        assert gw.bulk_place_calls == 1 and len(gw.place_calls) == 2
        assert len(gw.orders) == 2
    run(go())


def test_oid_cancels_use_one_bulk_action():
    async def go():
        bot, gw, _ = await make()
        quotes = [Quote(Side.BUY, 69990.0, 0.00028), Quote(Side.SELL, 70011.0, 0.00028)]
        await bot._do_places(quotes, 0.0, 0.0)
        await bot._do_cancels(bot.om.active(), 1.0)
        # Risk-off pulls park via one batchModify first, then clean up with one
        # bulk cancel on the rotated oids.
        assert gw.modify_quotes_calls == 1 and len(gw.modify_calls) == 2
        assert bot.metrics["parks"] == 2 and bot.metrics["park_fallbacks"] == 0
        assert gw.bulk_cancel_calls == 1 and len(gw.cancel_calls) == 2
        assert not bot.om.active() and not gw.orders
    run(go())


def test_cloid_only_cancels_use_the_cloid_bulk_path():
    async def go():
        bot, gw, _ = await make()
        o = bot.om.register_pending(Quote(Side.BUY, 69990.0, 0.00028), 0.0)  # no oid ack yet
        assert o.oid is None
        await bot._do_cancels([o], 1.0)
        assert gw.bulk_cancel_calls == 1 and gw.cancel_calls == [o.cloid]
    run(go())


def test_requote_uses_one_batch_modify_and_rotates_oids():
    async def go():
        bot, gw, _ = await make()
        quotes = [Quote(Side.BUY, 69990.0, 0.00028), Quote(Side.SELL, 70011.0, 0.00028)]
        await bot._do_places(quotes, 0.0, 0.0)
        old = {o.side: o.oid for o in bot.om.active()}
        assert len(old) == 2

        moved = [Quote(Side.BUY, 70020.0, 0.00028), Quote(Side.SELL, 70041.0, 0.00028)]
        plan = bot.om.plan(moved, 1.0)
        assert len(plan.modifies) == 2 and not plan.cancels and not plan.places
        await bot._apply_plan(plan, 1.0, 0.0)

        assert gw.modify_quotes_calls == 1 and len(gw.modify_calls) == 2
        assert bot.metrics["modifies"] == 2
        new = {o.side: o.oid for o in bot.om.active()}
        assert new != old and all(new[s] != old[s] for s in old)
        for oid in old.values():
            assert oid not in bot.om._by_oid
        for oid in new.values():
            assert oid in bot.om._by_oid
        assert {o["oid"] for o in gw.orders.values()} == set(new.values())
    run(go())


def test_risk_off_pull_parks_via_batch_modify_then_cancels():
    async def go():
        bot, gw, _ = await make()
        quotes = [Quote(Side.BUY, 69990.0, 0.00028), Quote(Side.SELL, 70011.0, 0.00028)]
        await bot._do_places(quotes, 0.0, 0.0)
        old = {o.side: o.oid for o in bot.om.active()}

        await bot._do_cancels(bot.om.active(), 1.0)

        assert gw.modify_quotes_calls == 1 and len(gw.modify_calls) == 2
        assert bot.metrics["parks"] == 2 and bot.metrics["park_fallbacks"] == 0
        assert gw.bulk_cancel_calls == 1
        # Parked far from the book, then cleaned up: no orders left.
        assert not gw.orders and not bot.om.active()
        assert all(oid not in bot.om._by_oid for oid in old.values())
        for m in gw.modify_calls:
            assert m["price"] < 69990.0 if m["side"] is Side.BUY else m["price"] > 70011.0
    run(go())


def test_park_rejection_falls_back_to_raw_cancel():
    async def go():
        bot, gw, _ = await make()
        await bot._do_places([Quote(Side.BUY, 69990.0, 0.00028)], 0.0, 0.0)
        gw.forced_modify = [PlaceKind.REJECT_OTHER]
        o = bot.om.active(Side.BUY)[0]
        oid = o.oid

        await bot._do_cancels([o], 1.0)

        assert bot.metrics["parks"] == 0 and bot.metrics["park_fallbacks"] == 1
        # The raw cancel path still removes the original oid.
        assert gw.cancel_calls and oid not in bot.om._by_oid
        assert not gw.orders and not bot.om.active()
    run(go())


# ---- nonce ------------------------------------------------------------------
def test_monotonic_nonce_is_strictly_increasing_and_idempotent():
    import hyperliquid.exchange as hx
    install_monotonic_nonce()
    patched = hx.get_timestamp_ms
    install_monotonic_nonce()
    assert hx.get_timestamp_ms is patched            # no repeated wrapping
    stamps = [hx.get_timestamp_ms() for _ in range(50)]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
