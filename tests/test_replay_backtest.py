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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mmbot.models import Side  # noqa: E402
from mmbot.precision import AssetSpec, round_px, tick_at  # noqa: E402
from mmbot.strategy.quoter import compute_quotes  # noqa: E402
from mmbot.models import MarketState, StrategyParams  # noqa: E402

from replay_backtest import (  # noqa: E402
    ReplayEngine,
    discover_tars,
    gather_sources,
    hour_range,
    iter_records,
    parse_hour_key,
    resolve_range,
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
    eng._open_fills.append(eng.fills[-1])
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


def test_gather_sources_merges_local_and_archive_tar_transparently(tmp_path: Path):
    data = tmp_path / "data"
    archive = tmp_path / "archive"
    archive.mkdir()
    day = data / "20260101"
    day.mkdir(parents=True)
    local_shard = day / "00_TST.jsonl.gz"
    with gzip.open(local_shard, "wt") as fh:
        fh.write(json.dumps(l2(0.0, 100.00, 1.0, 100.10, 1.0)) + "\n")

    h00 = tmp_path / "00_TST.jsonl.gz"
    h01 = tmp_path / "01_TST.jsonl.gz"
    with gzip.open(h00, "wt") as fh:
        fh.write(json.dumps(l2(0.5, 100.00, 1.0, 100.10, 1.0)) + "\n")
    with gzip.open(h01, "wt") as fh:
        fh.write(json.dumps(tr(1.0, "A", 99.99, 1.0)) + "\n")
    tar_path = archive / "data_20260101.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(h00, arcname="20260101/00_TST.jsonl.gz")  # duplicate of local
        tar.add(h01, arcname="20260101/01_TST.jsonl.gz")  # only in the archive

    start, end = resolve_range("20260101", "20260101")
    paths, tars = gather_sources(data, "TST", start, end, archive_dir=archive)
    assert paths == [local_shard]
    assert [m for _, m in tars] == ["20260101/01_TST.jsonl.gz"], \
        "local hour wins; the archive only fills the missing hour"
    assert [r["e"] for r in iter_records(paths, tars, "TST")] == ["l2", "tr"]

    explicit = tar_members(tar_path, "TST", start, end)
    _, tars2 = gather_sources(data, "TST", start, end, archive_dir=None, explicit_tars=explicit)
    assert [m for _, m in tars2] == ["20260101/01_TST.jsonl.gz"], \
        "explicit --tar members obey the same no-double-count rule"


def test_discover_tars_missing_dir_and_no_duplicates(tmp_path: Path):
    start, end = resolve_range("20260101", "20260101")
    assert discover_tars(tmp_path / "nope", "TST", start, end) == []
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "data_20260102.tar").write_bytes(b"not a tar for this day")
    assert discover_tars(archive, "TST", start, end) == [], "only the requested day is opened"


def test_feed_age_measured_when_recv_stamp_present():
    eng = engine()
    rec = l2(0.0, 100.00, 1.0, 100.10, 1.0)
    rec["r"] = rec["t"] + 350          # "r" is local receive epoch *ms*
    eng.on_record(rec)
    assert eng.feed_ages_ms and abs(eng.feed_ages_ms[0] - 350.0) < 1e-6
    m = eng.compute_metrics()
    assert m["feed_age_median_ms"] is not None


def test_recv_stamp_overrides_fixed_feed_age_for_decisions():
    """When "r" is present the engine must use the TRUE feed age (r - t), not the
    fixed --feed-age-ms fallback."""
    eng = engine(feed_age_ms=5000.0)
    rec = l2(0.0, 99.90, 0.5, 100.10, 10.0)
    rec["r"] = rec["t"] + 50           # 50 ms real feed age
    eng.on_record(rec)
    assert len(eng.pending) == 2, "both sides scheduled"
    assert all(p[0] == 0.05 for p in eng.pending), "effective time = t + real(r-t) + latency"


def test_header_and_gap_records_are_ignored():
    eng = engine()
    eng.on_record({"e": "header", "c": "TST", "schema": "hlmm-shard/2"})
    eng.on_record({"t": 0, "e": "gap", "c": "TST", "reason": "x", "reconnect": 1})
    assert eng.stats.book_updates == 0 and eng.stats.trades_seen == 0
    assert eng.stats.decisions == 0


def test_funding_pnl_accrues_hourly_and_enters_net():
    eng = engine()
    eng.on_record(l2(0.0, 100.0, 1.0, 100.10, 1.0))
    eng.pos = 10.0
    eng.last_mid = 100.0
    eng.on_record({"t": 0, "c": "TST", "e": "funding", "funding": 0.0001, "mark_px": 100.0})
    assert eng.funding_pnl == 0.0, "first funding sample only anchors the hour"
    eng.on_record({"t": 3_600_000, "c": "TST", "e": "funding", "funding": 0.0001, "mark_px": 100.0})
    assert abs(eng.funding_pnl - (-10.0 * 100.0 * 0.0001)) < 1e-12
    m = eng.compute_metrics()
    assert abs(m["funding_pnl"] - (-0.1)) < 1e-12
    expect = (m["matched_spread_usd"] + m["inventory_mtm_pnl"] + m["funding_pnl"]
              - m["total_fees_usd"])
    assert abs(m["net_pnl_usd"] - expect) < 1e-9
    assert m["funding_samples"] == 2


def test_incremental_markout_settle_and_grid_prune():
    eng = engine()
    eng.on_record(l2(0.0, 100.00, 10.0, 100.10, 1.0))
    eng.on_record(l2(0.4, 100.00, 10.0, 100.10, 1.0))
    eng.on_record(tr(0.5, "A", 100.00, 20.0))
    assert len(eng.fills) == 1 and eng.fills[0].markouts is None
    for i in range(1, 63):
        eng.on_record(l2(0.5 + i * 0.5, 100.00, 10.0, 100.10, 1.0))
    f = eng.fills[0]
    assert f.markouts is not None and set(f.markouts) == {1.0, 5.0, 30.0}
    assert eng._open_fills == []
    assert len(eng.mid_grid) < 900, f"grid must stay a rolling window, got {len(eng.mid_grid)}"
    m = eng.compute_metrics()
    assert m["fills"] == 1 and m["markout_30s_bps"] != 0.0


def test_resolve_range_expands_bare_days():
    assert resolve_range("20261013", "20261014") == (2026101300, 2026101423)
    assert resolve_range("2026101305", "20261014") == (2026101305, 2026101423)
    assert resolve_range("2026101305", "2026101307") == (2026101305, 2026101307)
    assert resolve_range("20261014", "20261014") == (2026101400, 2026101423)
    with pytest.raises(ValueError):
        resolve_range("20261014", "20261013")


def test_hour_range_crosses_midnight_without_skipping():
    hours = [f"{d}{h}" for d, h in hour_range(2026101322, 2026101402)]
    assert hours == ["2026101322", "2026101323", "2026101400", "2026101401", "2026101402"]
    two_days = list(hour_range(2026101300, 2026101423))
    assert len(two_days) == 48 and two_days[-1] == ("20261014", "23")


# --------------------------------------------------------------------------
# T13 — strict PnL decomposition (round-trip spread vs inventory MTM vs fees)
# --------------------------------------------------------------------------


def _assert_identities(m):
    """The exact accounting identities the T13 audit relies on."""
    assert abs(m["total_fees_usd"] - m["maker_fees_usd"]) < 1e-12
    assert abs(m["total_fees_usd"] - (m["fees_matched_usd"] + m["fees_unmatched_usd"])) < 1e-9
    # net = matched_gross + inventory_mtm + funding - total fees
    expect1 = (m["matched_spread_usd"] + m["inventory_mtm_pnl"] + m["funding_pnl"]
               - m["total_fees_usd"])
    assert abs(m["net_pnl_usd"] - expect1) < 1e-9, (m["net_pnl_usd"], expect1)
    # net = round_trip(after matched fees) + inventory_mtm + funding - unmatched-leg fees
    expect2 = (m["round_trip_spread_pnl"] + m["inventory_mtm_pnl"] + m["funding_pnl"]
               - m["fees_unmatched_usd"])
    assert abs(m["net_pnl_usd"] - expect2) < 1e-9, (m["net_pnl_usd"], expect2)
    # round_trip definition
    assert abs(m["round_trip_spread_pnl"] - (m["matched_spread_usd"] - m["fees_matched_usd"])) < 1e-12


def test_pnl_decomposition_hand_computed_long_residual():
    eng = engine()
    eng.last_mid = 101.0
    eng._fill("buy", 100.0, 2.0)          # long 2 @100
    eng.last_mid = 102.0
    eng._fill("sell", 102.0, 1.0)         # round-trip 1 @100 -> @102
    eng.last_mid = 105.0                  # final mid: residual long marked here
    m = eng.compute_metrics()

    volume = 100.0 * 2.0 + 102.0 * 1.0                       # 302
    assert abs(m["traded_volume_usd"] - volume) < 1e-12
    assert abs(m["gross_spread_usd"] - ((101.0 - 100.0) * 2.0)) < 1e-12  # edge vs fill mids
    assert abs(m["total_fees_usd"] - volume * 1.5 / 1e4) < 1e-12         # 0.0453
    assert m["round_trip_count"] == 1
    assert abs(m["matched_spread_usd"] - 2.0) < 1e-12
    assert abs(m["fees_matched_usd"] - (100.0 + 102.0) * 1.0 * 1.5 / 1e4) < 1e-12  # 0.0303
    assert abs(m["round_trip_spread_pnl"] - (2.0 - 202.0 * 1.5 / 1e4)) < 1e-12
    assert abs(m["ending_inventory_usd"] - 105.0) < 1e-12
    assert abs(m["inventory_mtm_pnl"] - (105.0 - 100.0)) < 1e-12         # +5 drift
    assert abs(m["max_abs_inventory_usd"] - 2 * 101.0) < 1e-12           # peak |pos*mid|
    # net from cash accounting: -200 + 102 + 1*105 - fees
    assert abs(m["net_pnl_usd"] - (-200.0 + 102.0 + 105.0 - m["total_fees_usd"])) < 1e-9
    _assert_identities(m)


def test_pnl_decomposition_fifo_lots_and_counts():
    eng = engine()
    eng.last_mid = 100.0
    eng._fill("buy", 100.0, 1.0)          # lot A
    eng._fill("buy", 101.0, 1.0)          # lot B
    eng._fill("sell", 103.0, 1.5)         # eats lot A fully + 0.5 of lot B
    eng.last_mid = 100.0
    m = eng.compute_metrics()

    assert m["round_trip_count"] == 2                      # one (fill,lot) match event each
    assert abs(m["matched_spread_usd"] - ((103.0 - 100.0) * 1.0 + (103.0 - 101.0) * 0.5)) < 1e-9
    assert abs(m["fees_matched_usd"] - ((100.0 + 103.0) * 1.0 + (101.0 + 103.0) * 0.5) * 1.5 / 1e4) < 1e-9
    assert abs(m["final_position"] - 0.5) < 1e-12          # 0.5 of lot B still open
    assert abs(m["ending_inventory_usd"] - 0.5 * 100.0) < 1e-12
    assert abs(m["inventory_mtm_pnl"] - (0.5 * 100.0 - 101.0 * 0.5)) < 1e-12  # -0.5
    cash = -100.0 - 101.0 + 103.0 * 1.5
    assert abs(m["net_pnl_usd"] - (cash + 0.5 * 100.0 - m["total_fees_usd"])) < 1e-9
    _assert_identities(m)


def test_pnl_decomposition_short_residual_inventory():
    eng = engine()
    eng.last_mid = 100.0
    eng._fill("sell", 100.0, 1.0)         # short 1 @100
    eng.last_mid = 98.0
    eng._fill("buy", 98.0, 0.5)           # cover half: round-trip (100-98)*0.5
    eng.last_mid = 96.0                   # price keeps falling: short residual gains
    m = eng.compute_metrics()

    assert m["round_trip_count"] == 1
    assert abs(m["matched_spread_usd"] - (100.0 - 98.0) * 0.5) < 1e-12
    assert abs(m["final_position"] - (-0.5)) < 1e-12
    assert abs(m["ending_inventory_usd"] - (-0.5 * 96.0)) < 1e-12
    # residual basis for a short = -(proceeds of open lot) => mtm = -48 - (-50) = +2
    assert abs(m["inventory_mtm_pnl"] - ((-0.5 * 96.0) - (-100.0 * 0.5))) < 1e-12
    cash = 100.0 - 98.0 * 0.5
    assert abs(m["net_pnl_usd"] - (cash + (-0.5 * 96.0) - m["total_fees_usd"])) < 1e-9
    _assert_identities(m)


def test_pnl_decomposition_negative_gross_positive_net_explained():
    """The T13 red flag: stale buy fills ABOVE mid (gross negative) yet net is
    positive only because inventory drifts up — decomposition must attribute it
    to inventory_mtm_pnl, not round-trip spread."""
    eng = engine()
    eng.last_mid = 100.0                  # mid at fill: below our stale bid px
    eng._fill("buy", 101.0, 1.0)
    eng.last_mid = 105.0                  # price recovered -> lucky long drift
    m = eng.compute_metrics()

    assert m["gross_spread_usd"] < 0.0                       # -1.0: edge vs fill mid
    assert m["round_trip_spread_pnl"] == 0.0                 # no matched pair at all
    assert abs(m["inventory_mtm_pnl"] - (105.0 - 101.0)) < 1e-12   # +4 drift, the only driver
    assert m["net_pnl_usd"] > 0.0                            # net = 0 + 4 - fees
    assert abs(m["net_pnl_usd"] - (4.0 - 101.0 * 1.5 / 1e4)) < 1e-9
    _assert_identities(m)


def test_iter_records_tolerates_truncated_gzip(tmp_path: Path):
    day = tmp_path / "20261009"
    day.mkdir()
    lines = [
        l2(0.0, 100.00, 1.0, 100.10, 1.0),
        tr(0.5, "A", 99.99, 1.0),
        tr(0.6, "A", 99.98, 1.0),
        tr(0.7, "A", 99.97, 1.0),
    ]
    blob = "".join(json.dumps(r) + "\n" for r in lines)
    shard = day / "15_TST.jsonl.gz"
    with gzip.open(shard, "wt") as fh:
        fh.write(blob)
    raw = shard.read_bytes()
    shard.write_bytes(raw[: len(raw) - 25])  # simulate the Oct-9 outage cut

    recs = list(iter_records([shard], [], "TST"))
    assert recs and recs[0]["e"] == "l2", "no crash; prefix before the cut survives"


def test_write_csv_emits_decomposition_columns(tmp_path: Path):
    import argparse

    from replay_backtest import CSV_KEYS, write_csv

    eng = engine()
    eng.last_mid = 100.0
    eng._fill("buy", 100.0, 1.0)
    m = eng.compute_metrics()
    args = argparse.Namespace(
        latency_ms=500.0, half_spread_bps=8.0, skew_bps=6.0, cancel_rate=0.5,
        feed_age_ms=400.0, start="20261013",
    )
    out = tmp_path / "out.csv"
    write_csv(out, [m], args)
    import csv as _csv

    with out.open() as fh:
        rows = list(_csv.DictReader(fh))
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "20261013" and row["cancel_rate"] == "0.5"
    for key in ("round_trip_spread_pnl", "inventory_mtm_pnl", "funding_pnl", "total_fees_usd",
                "fees_matched_usd", "fees_unmatched_usd", "max_abs_inventory_usd",
                "ending_inventory_usd", "round_trip_count"):
        assert key in CSV_KEYS and key in row
