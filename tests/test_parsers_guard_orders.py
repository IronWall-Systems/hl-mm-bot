from mmbot.exchange.client import (parse_cancel_response, parse_fills, parse_l2, parse_open_orders,
                                   parse_order_updates, parse_place_response, parse_user_state)
from mmbot.exchange.types import OrderUpdateEvent, PlaceKind, PlaceResult, RemoteOrder
from mmbot.execution.order_manager import OrderManager
from mmbot.models import OrderStatus, PositionState, Quote, RiskConfig, Side, StrategyParams
from mmbot.risk.guard import KillReason, RiskGuard


# ---- parsers (documented response shapes; live shapes must be confirmed on testnet) ----
def test_place_parsing():
    ok = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 77}}]}}}
    r = parse_place_response(ok)
    assert r.kind is PlaceKind.RESTING and r.oid == 77
    cross = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"error": "Post only order would have immediately matched, bbo was 70000@70001"}]}}}
    assert parse_place_response(cross).kind is PlaceKind.REJECT_CROSS
    other = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Insufficient margin"}]}}}
    assert parse_place_response(other).kind is PlaceKind.REJECT_OTHER
    assert parse_place_response({"status": "err", "response": "nope"}).kind is PlaceKind.REJECT_OTHER
    assert parse_place_response(None).kind is PlaceKind.ERROR


def test_cancel_parsing():
    assert parse_cancel_response({"status": "ok", "response": {"data": {"statuses": ["success"]}}}).ok
    r = parse_cancel_response({"status": "ok", "response": {"data": {"statuses": [
        {"error": "Order was never placed, already canceled, or filled."}]}}})
    assert not r.ok and r.already_gone


def test_ws_and_rest_parsers():
    l2 = {"channel": "l2Book", "data": {"coin": "BTC", "time": 1700000000000, "levels": [
        [{"px": "70000.0", "sz": "1.5", "n": 3}], [{"px": "70001.0", "sz": "2.0", "n": 2}]]}}
    m = parse_l2(l2, 1.0).market
    assert (m.bid, m.ask, m.bid_sz) == (70000.0, 70001.0, 1.5)
    assert parse_l2({"data": {"coin": "BTC", "levels": [[], []]}}, 1.0) is None
    ou = parse_order_updates({"data": [{"order": {"coin": "BTC", "side": "B", "limitPx": "70000.0", "sz": "0.0002",
                                                   "oid": 5, "cloid": "0xab"}, "status": "open"}]})
    assert ou[0].oid == 5 and ou[0].side is Side.BUY and ou[0].status == "open"
    fl = parse_fills({"data": {"isSnapshot": True, "user": "0x", "fills": [
        {"coin": "BTC", "px": "70000", "sz": "0.0002", "side": "A", "time": 1700000000000, "oid": 5, "tid": 9, "fee": "0.01"}]}})
    assert fl[0].is_snapshot and fl[0].side is Side.SELL and fl[0].tid == 9
    us = {"marginSummary": {"accountValue": "1000.5", "totalMarginUsed": "12"},
          "assetPositions": [{"position": {"coin": "BTC", "szi": "-0.001", "entryPx": "70000", "unrealizedPnl": "1.5"}}]}
    ps = parse_user_state(us, "BTC")
    assert ps.size == -0.001 and ps.account_value == 1000.5
    oo = parse_open_orders([{"coin": "BTC", "side": "A", "limitPx": "70010", "sz": "0.0002", "oid": 8}, {"coin": "ETH", "side": "B", "limitPx": "1", "sz": "1", "oid": 9}], "BTC")
    assert len(oo) == 1 and oo[0].side is Side.SELL


def test_equity_perps_only():
    us = {"marginSummary": {"accountValue": "250.25", "totalMarginUsed": "12"},
          "assetPositions": [{"position": {"coin": "BTC", "szi": "-0.001", "entryPx": "70000",
                                           "unrealizedPnl": "1.5"}}]}
    assert parse_user_state(us, "BTC").account_value == 250.25
    assert parse_user_state(us, "BTC", None).account_value == 250.25
    assert parse_user_state(us, "BTC", {"balances": []}).account_value == 250.25
    assert parse_user_state(us, "BTC", {"balances": [{"coin": "ETH", "total": "2.5"}]}).account_value == 250.25


def test_equity_spot_only():
    us0 = {"marginSummary": {"accountValue": "0.0", "totalMarginUsed": "0.0"}, "assetPositions": []}
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "999.0", "hold": "0.0", "entryNtl": "0.0"},
                         {"coin": "ETH", "token": 1, "total": "2.5", "hold": "0.0", "entryNtl": "0.0"}]}
    assert parse_user_state(us0, "BTC", spot).account_value == 999.0
    assert parse_user_state(us0, "BTC").account_value == 0.0
    assert parse_user_state(us0, "BTC", {"balances": []}).account_value == 0.0


def test_equity_perps_and_spot():
    us = {"marginSummary": {"accountValue": "1000.5", "totalMarginUsed": "12"}, "assetPositions": []}
    spot = {"balances": [{"coin": "USDC", "token": 0, "total": "999.0", "hold": "0.0", "entryNtl": "0.0"}]}
    assert parse_user_state(us, "BTC", spot).account_value == 1999.5
    assert parse_user_state(us, "BTC").account_value == 1000.5


# ---- risk guard -----------------------------------------------------------------------------
def guard(clock=lambda: 0.0, **kw):
    return RiskGuard(RiskConfig(**kw), 1000.0, clock)


def test_pre_trade_caps():
    g = guard(max_order_notional=25, max_position_notional=80)
    pos = PositionState(size=0.0, account_value=1000)
    assert g.pre_trade(Quote(Side.BUY, 70000, 0.0003), pos, 70000, 0, 0.1).ok          # $21
    assert not g.pre_trade(Quote(Side.BUY, 70000, 0.0005), pos, 70000, 0, 0.1).ok      # $35 > order cap
    near = PositionState(size=60 / 70000, account_value=1000)
    assert not g.pre_trade(Quote(Side.BUY, 70000, 0.0003), near, 70000, 0, 0.1).ok     # 60+21 > 80
    assert g.pre_trade(Quote(Side.SELL, 70000, 0.0003), near, 70000, 0, 0.1).ok        # reduces risk
    assert not g.pre_trade(Quote(Side.BUY, 70000, 0.0003), pos, 70000, 70.0, 0.1).ok   # resting counts
    assert not g.pre_trade(Quote(Side.BUY, 70000, 0.0003), pos, 70000, 0, 9.0).ok      # stale feed


def test_kill_switches():
    pos = PositionState(size=0.0, account_value=1000)
    g = guard(max_drawdown_pct=0.05, max_loss_pct=0.2)
    g.update_equity(1100)
    g.update_equity(1040)                      # 5.45% below peak
    assert g.check_kill(pos, 70000) is KillReason.DRAWDOWN
    g2 = guard(latency_breach_count=3, max_latency_ms=100)
    for ms in (500, 500):
        g2.record_latency(ms)
    assert g2.check_kill(pos, 70000) is None
    g2.record_latency(50)                      # a good sample resets the streak
    for ms in (500, 500, 500):
        g2.record_latency(ms)
    assert g2.check_kill(pos, 70000) is KillReason.LATENCY
    g3 = guard(max_position_notional=80, hard_position_mult=1.5)
    assert g3.check_kill(PositionState(size=130 / 70000, account_value=1000), 70000) is KillReason.POSITION
    g4 = guard(max_consecutive_errors=3)
    for _ in range(3):
        g4.record_error()
    assert g4.check_kill(pos, 70000) is KillReason.ERRORS


def test_action_pacing():
    t = [0.0]
    g = guard(clock=lambda: t[0], max_actions_per_min=3)
    pos = PositionState(account_value=1000)
    g.record_actions(3)
    assert not g.pre_trade(Quote(Side.BUY, 70000, 0.0002), pos, 70000, 0, 0).ok
    t[0] = 61.0
    assert g.pre_trade(Quote(Side.BUY, 70000, 0.0002), pos, 70000, 0, 0).ok


# ---- order manager ----------------------------------------------------------------------------
P = StrategyParams(requote_threshold_bps=1.5)


def rest(om, side, px, sz=0.0002, now=0.0, oid=1):
    o = om.register_pending(Quote(side, px, sz), now)
    om.on_place_result(o, PlaceResult(PlaceKind.RESTING, oid=oid), now)
    return o


def test_plan_hysteresis_and_modify_then_place():
    om = OrderManager(P)
    o = rest(om, Side.BUY, 70000)
    assert om.plan([Quote(Side.BUY, 70000, 0.0002)], 1.0).cancels == []             # unchanged
    assert om.plan([Quote(Side.BUY, 70005, 0.0002)], 1.0).cancels == []             # 0.7 bps < 1.5
    pl = om.plan([Quote(Side.BUY, 70020, 0.0002)], 1.0)                             # 2.9 bps
    assert pl.cancels == [] and pl.places == [] and [m.order for m in pl.modifies] == [o]
    om.mark_modify_sent(o, 1.0)
    assert om.plan([Quote(Side.BUY, 70020, 0.0002)], 1.1).modifies == []            # waits for ack
    om.on_modify_result(o, Quote(Side.BUY, 70020, 0.0002),
                        PlaceResult(PlaceKind.RESTING, oid=2), 1.2)
    assert o.oid == 2 and o.price == 70020
    assert om.plan([Quote(Side.BUY, 70020, 0.0002)], 1.3).modifies == []            # now unchanged
    # A missing target still pulls the resting order.
    assert [x.cloid for x in om.plan([], 1.4).cancels] == [o.cloid]


def test_batch_modify_old_oid_cancel_does_not_kill_new_oid():
    """Live regression: batchModify preserves cloid but rotates the oid.

    The exchange emits "open(new_oid)" and "canceled(old_oid)" with the same
    cloid.  The stale old-oid cancel must not mark the live order CANCELED and
    make the next plan place a duplicate pair.
    """
    om = OrderManager(P)
    o = rest(om, Side.BUY, 70000, sz=0.0002, oid=1)
    om.mark_modify_sent(o, 1.0)
    om.on_modify_result(o, Quote(Side.BUY, 70020, 0.0002),
                        PlaceResult(PlaceKind.RESTING, oid=2), 1.2)
    assert o.oid == 2 and o.status is OrderStatus.RESTING
    # Same cloid on both updates, old oid canceled and new oid open.
    om.on_order_update(OrderUpdateEvent(1, o.cloid, "canceled"), 1.3)
    assert o.status is OrderStatus.RESTING and o.oid == 2
    om.on_order_update(OrderUpdateEvent(2, o.cloid, "open"), 1.3)
    assert o.status is OrderStatus.RESTING and o.oid == 2
    # No duplicate: the unchanged target is kept, not re-placed.
    pl = om.plan([Quote(Side.BUY, 70020, 0.0002)], 1.4)
    assert pl.places == [] and pl.modifies == [] and pl.cancels == []


def test_plan_blocked_side_and_missing_target():
    om = OrderManager(P)
    pl = om.plan([Quote(Side.BUY, 1, 1), Quote(Side.SELL, 2, 1)], 0.0, blocked=frozenset({Side.SELL}))
    assert [q.side for q in pl.places] == [Side.BUY]
    o = rest(om, Side.SELL, 70010)
    assert om.plan([], 1.0).cancels == [o]                                           # no target -> pull


def test_updates_fills_and_exposure():
    om = OrderManager(P)
    o = rest(om, Side.BUY, 70000, sz=0.0002, oid=7)
    assert om.exposure_notional(Side.BUY) == 0.0002 * 70000
    om.on_fill(None, 7, 0.0001, 1.0)
    assert o.status is OrderStatus.RESTING and abs(o.remaining - 0.0001) < 1e-12
    om.on_fill(None, 7, 0.0001, 1.1)
    assert o.status is OrderStatus.FILLED and om.exposure_notional(Side.BUY) == 0
    o2 = rest(om, Side.SELL, 70010, oid=8)
    om.on_order_update(OrderUpdateEvent(8, o2.cloid, "marginCanceled"), 2.0)
    assert o2.status is OrderStatus.CANCELED


def test_stale_and_reconcile():
    om = OrderManager(P, stale_after_s=5.0)
    lost = om.register_pending(Quote(Side.BUY, 70000, 0.0002), 0.0)                  # place result never came back
    assert om.stale(3.0) == [] and om.stale(6.0) == [lost]
    live = rest(om, Side.SELL, 70010, oid=9, now=6.0)
    remote = [RemoteOrder("BTC", 9, Side.SELL, 70010, 0.0002, live.cloid),
              RemoteOrder("BTC", 99, Side.BUY, 69000, 0.0002, None)]                 # orphan
    rep = om.reconcile(remote, 6.0)
    assert [r.oid for r in rep.orphans] == [99]
    assert rep.closed_locally == [lost] and lost.status is OrderStatus.REJECTED
    assert live.status is OrderStatus.RESTING
