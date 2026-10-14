"""Offline tests for scripts/replay_backtest.py (T11 step 2).

Everything runs on tiny synthetic records / tmp_path shards — no network, no real
dataset, no mainnet. Covered: latency delay enforcement (no look-ahead), ALO
clamping + crossed-ALO reject, queue depletion on trades (incl. trade-through and
cancel-rate decay), markout math, and the gz/tar shard parsers.
"""

from __future__ import annotations

import gzip
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mmbot.models import Side  # noqa: E402
from mmbot.precision import AssetSpec, round_px, tick_at  # noqa: E402
from mmbot.strategy.quoter import compute_quotes  # noqa: E402
from mmbot.models import MarketState, StrategyParams  # noqa: E402

from replay_backtest import (  # noqa: E402
    ReplayEngine,
    iter_records,
    parse_hour_key,
    run_backtest,
    shard_paths,
    tar_members,
)

SPEC = AssetSpec("TST", 2)


def l2(t, bid_px, bid_sz, ask_px, ask_sz):
    return {
        "t": int(t * 1000),
        "c": "TST",
        "e": "l2",
        "b": [[str(bid_px), str(bid_sz), 1]],
        "a": [[str(ask_px), str(ask_sz), 1]],
    }


def tr(t, side, px, sz):
    return {"t": int(t * 1000), "c": "TST", "e": "tr", "side": side, "px": str(px), "sz": str(sz)}


def engine(**kw):
    base = dict(
        half_spread_bps=4.0,
        skew_bps=6.0,
        quote_notional=10.0,
        max_notional=60.0,
        min_order_notional=1.0,
        requote_threshold_bps=1.5,
        min_requote_interval_s=0.3,
        latency_ms=0.0,
        feed_age_ms=0.0,
        cancel_rate=0.0,
    )
    base.update(kw)
    return ReplayEngine(SPEC, **base)


def test_latency_delay_enforced_no_lookahead():
    eng = engine(latency_ms=1000.0)
    eng.on_record(l2(0.0, 99.90, 0.5, 100.10, 10.0))
    assert eng.stats.decisions == 1 and len(eng.pending) == 2
    assert sorted(p[3][0] for p in eng.pending) == ["buy", "sell"]  # type: ignore[index]
    assert eng.stats.places == 0 and eng.working["buy"] is None, (
        "place must not reach the exchange before t_seen+latency"
    )
    eng.on_record(tr(0.5, "A", 99.88, 1.0))
    assert eng.fills == [], "no fill before the order reaches the exchange"
    assert eng.working["buy"] is None
    eng.on_record(tr(1.5, "A", 99.88, 1.0))
    assert eng.stats.places == 2, "both sides' places become live at t_seen+latency"
    assert len(eng.fills) == 1
    assert eng.fills[0].side == "buy" and eng.fills[0].px == 99.96
    assert eng.pos > 0


def test_no_lookahead_book_moves_before_effect():
    eng = engine(latency_ms=1000.0)
    eng.on_record(l2(0.0, 99.90, 0.5, 100.10, 10.0))
    eng.on_record(l2(0.6, 101.00, 5.0, 101.20, 5.0))
    eng.on_record(tr(0.8, "A", 99.89, 1.0))
    assert eng.fills == []
    assert eng.working["buy"] is None
    assert eng.mid == 0.5 * (101.00 + 101.20)


def test_alo_clamp_keeps_quotes_off_the_touch():
    p = StrategyParams(coin="TST", half_spread_bps=200.0, skew_bps=0.0,
                       quote_notional=10.0, min_order_notional=1.0)
    m = MarketState("TST", 99.90, 99.91, 1.0, 1.0, 0.0)
    tick = tick_at(m.ask, SPEC.sz_decimals)
    quotes = {q.side: q for q in compute_quotes(m, 0.0, p, SPEC)}
    buy = quotes[Side.BUY]
    sell = quotes[Side.SELL]
    assert buy.price <= round_px(m.ask - tick, SPEC.sz_decimals, "floor")
    assert sell.price >= round_px(m.bid + tick, SPEC.sz_decimals, "ceil")
    assert buy.price < sell.price


def test_engine_rejects_crossed_alo_place():
    eng = engine()
    eng.on_record(l2(0.0, 99.90, 1.0, 100.10, 1.0))
    eng._apply_place("buy", 100.10, 0.10)
    assert eng.stats.alo_rejects == 1 and eng.working["buy"] is None
    eng._apply_place("sell", 99.90, 0.10)
    assert eng.stats.alo_rejects == 2 and eng.working["sell"] is None
    eng._apply_place("buy", 99.95, 0.10)
    assert eng.stats.alo_rejects == 2 and eng.working["buy"] is not None


def test_queue_depletion_and_trade_through():
    eng = engine()
    eng.on_record(l2(0.0, 100.00, 10.0, 100.10, 1.0))
    assert eng.working["buy"] is None
    eng.on_record(l2(0.4, 100.00, 10.0, 100.10, 1.0))
    w = eng.working["buy"]
    assert w is not None and w.px == 100.00, "target bid sits at the touch"
    assert abs(w.queue_ahead - 10.0) < 1e-9
    eng.on_record(tr(0.5, "A", 100.00, 4.0))
    assert abs(w.queue_ahead - 6.0) < 1e-9 and eng.fills == []
    eng.on_record(tr(0.6, "A", 100.00, 5.0))
    assert abs(w.queue_ahead - 1.0) < 1e-9 and eng.fills == []
    eng.on_record(tr(0.7, "A", 100.00, 2.0))
    assert len(eng.fills) == 1
    fill = eng.fills[0]
    assert fill.side == "buy" and fill.px == 100.00
    assert abs(fill.sz - 0.10) < 1e-9
    assert eng.working["buy"] is None
    eng.on_record(tr(0.8, "A", 99.99, 5.0))
    assert len(eng.fills) == 1, "trade-through with no resting order fills nothing"
    assert abs(eng.pos - 0.10) < 1e-9


def test_cancel_rate_decays_queue():
    records = [l2(0.0, 100.00, 10.0, 100.10, 1.0)]
    for i in range(1, 21):
        records.append(l2(i * 0.1, 100.00, 10.0, 100.10, 1.0))
    records.append(tr(2.1, "A", 100.00, 6.0))

    decaying = engine(cancel_rate=0.5)
    for r in records:
        decaying.on_record(r)
    assert len(decaying.fills) == 1, "decay shrinks queue_ahead below the printed size"

    sticky = engine(cancel_rate=0.0)
    for r in records:
        sticky.on_record(r)
    assert sticky.fills == [], "without decay the same trade only eats into the queue"


def test_markout_math():
    eng = engine()
    from replay_backtest import Fill

    eng.fills.append(Fill(t=10.0, side="buy", px=100.00, sz=0.15, mid=100.00))
    eng.pos = 0.15
    eng.mid_grid[int(11.0 / 0.05)] = (11.0, 100.02)
    eng.mid_grid[int(15.0 / 0.05)] = (15.0, 99.98)
    eng.mid_grid[int(40.0 / 0.05)] = (40.0, 100.05)
    eng.last_mid = 100.05
    m = eng.compute_metrics()
    assert abs(m["markout_1s_bps"] - 2.0) < 1e-9
    assert abs(m["markout_5s_bps"] + 2.0) < 1e-9
    assert abs(m["markout_30s_bps"] - 5.0) < 1e-9
    notional = 100.00 * 0.15
    assert abs(m["maker_fees_usd"] - notional * 1.5 / 1e4) < 1e-12
    expected_net = -notional + 0.15 * 100.05 - m["maker_fees_usd"]
    assert abs(m["net_pnl_usd"] - expected_net) < 1e-9


def test_gz_and_tar_parsers(tmp_path: Path):
    day = tmp_path / "20260101"
    day.mkdir()
    lines = [
        l2(0.0, 100.00, 1.0, 100.10, 1.0),
        {"t": 1000, "c": "OTHER", "e": "bbo", "b": ["1", "1", 1], "a": ["2", "1", 1]},
        tr(0.5, "A", 99.99, 1.0),
    ]
    blob = "".join(json.dumps(r) + "\n" for r in lines)
    shard = day / "00_TST.jsonl.gz"
    with gzip.open(shard, "wt") as fh:
        fh.write(blob)

    paths = shard_paths(tmp_path, "TST", parse_hour_key("20260101"), parse_hour_key("20260101"))
    assert paths == [shard]
    recs = list(iter_records(paths, [], "TST"))
    assert [r["e"] for r in recs] == ["l2", "tr"]

    tar_path = tmp_path / "data_20260101.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(shard, arcname="20260101/00_TST.jsonl.gz")
    members = tar_members(tar_path, "TST", parse_hour_key("20260101"), parse_hour_key("20260101"))
    assert len(members) == 1
    recs2 = list(iter_records([], members, "TST"))
    assert [r["e"] for r in recs2] == ["l2", "tr"]

    m = run_backtest("TST", SPEC, iter(lines), latency_ms=500.0)
    assert m["coin"] == "TST" and "fills" in m


def test_feed_age_measured_when_recv_stamp_present():
    eng = engine()
    rec = l2(0.0, 100.00, 1.0, 100.10, 1.0)
    rec["r"] = rec["t"] / 1000.0 + 0.35
    eng.on_record(rec)
    assert eng.feed_ages_ms and abs(eng.feed_ages_ms[0] - 350.0) < 1e-6
    m = eng.compute_metrics()
    assert m["feed_age_median_ms"] is not None
