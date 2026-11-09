"""Tests for the standalone microstructure daemon (engine, walls, state I/O)."""
from __future__ import annotations

import json
import math
import time

from mmbot.microstructure_daemon import (
    HyperliquidFeed,
    MicrostructureEngine,
    SharedMicroStateReader,
    atomic_write_json,
    read_micro_state,
)


def _l2(bids, asks):
    return {"channel": "l2Book", "data": {"coin": "HYPE", "time": 1,
                                          "levels": [bids, asks]}}


def test_obi_nw_uses_size_and_order_count_with_level_decay():
    e = MicrostructureEngine("HYPE")
    e.on_hl_message(_l2(
        [{"px": "79.00", "sz": "100", "n": 10}, {"px": "78.99", "sz": "10", "n": 2}],
        [{"px": "79.01", "sz": "10", "n": 1}, {"px": "79.02", "sz": "10", "n": 1}],
    ), now=100.0)

    b = 100 * math.log1p(10) + 10 * math.log1p(2) * math.exp(-0.5)
    a = 10 * math.log1p(1) + 10 * math.log1p(1) * math.exp(-0.5)
    want = (b - a) / (b + a)
    snap = e.snapshot(now=100.0)
    assert abs(snap["signals"]["obi_nw"] - want) < 1e-6
    assert snap["book"]["mid"] == 79.005


def test_obi_nw_only_reads_top_five_levels():
    e = MicrostructureEngine("HYPE")
    bids = [{"px": "79.00", "sz": "1", "n": 1} for _ in range(5)]
    bids.append({"px": "78.00", "sz": "100000", "n": 99})   # must be ignored
    asks = [{"px": "79.01", "sz": "1", "n": 1} for _ in range(5)]
    asks.append({"px": "80.00", "sz": "100000", "n": 99})   # must be ignored
    e.on_hl_message(_l2(bids, asks), now=100.0)
    assert e.snapshot(now=100.0)["signals"]["obi_nw"] == 0.0


def test_tfi2_is_two_second_rolling_imbalance():
    e = MicrostructureEngine("HYPE")
    e.on_hl_message({"channel": "trades", "data": [
        {"side": "B", "sz": "3"}, {"side": "A", "sz": "1"},
    ]}, now=100.0)
    assert abs(e.snapshot(now=101.0)["signals"]["tfi2"] - 0.5) < 1e-9
    # A later snapshot outside the 2 s window drops the old flow.
    assert e.snapshot(now=103.0)["signals"]["tfi2"] == 0.0


def test_lead_btc_1s_uses_mid_one_second_ago():
    e = MicrostructureEngine("HYPE")
    e.on_binance_mid(76000.0, now=99.5)
    e.on_binance_mid(76008.0, now=101.0)
    snap = e.snapshot(now=101.0)
    assert snap["signals"]["btc_ready"] is True
    assert abs(snap["signals"]["lead_btc_1s"] - (76008.0 / 76000.0 - 1) * 1e4) < 1e-6


def test_lead_btc_not_ready_without_one_second_of_history():
    e = MicrostructureEngine("HYPE")
    e.on_binance_mid(76000.0, now=100.9)
    snap = e.snapshot(now=101.0)
    assert snap["signals"]["lead_btc_1s"] == 0.0
    assert snap["signals"]["btc_ready"] is False


def test_wall_scanner_requires_orders_and_size_multiple():
    e = MicrostructureEngine("HYPE")
    e.on_hl_message(_l2(
        [{"px": "79.00", "sz": "100", "n": 10}],
        [{"px": "79.01", "sz": "100", "n": 10}],
    ), now=100.0)
    # median size 10 -> 3x = 30; the 5000 bid qualifies, the 20 ask is too small,
    # and the n=2 large bid is rejected as unverified.
    e.on_wall_book({"data": {"levels": [
        [{"px": "78.90", "sz": "5000", "n": 30},
         {"px": "78.89", "sz": "20", "n": 10},
         {"px": "78.88", "sz": "5000", "n": 2},
         {"px": "78.87", "sz": "10", "n": 1},
         {"px": "78.86", "sz": "10", "n": 1}],
        [{"px": "79.20", "sz": "4000", "n": 20},
         {"px": "79.21", "sz": "20", "n": 10},
         {"px": "79.22", "sz": "10", "n": 1},
         {"px": "79.23", "sz": "10", "n": 1},
         {"px": "79.24", "sz": "10", "n": 1}],
    ]}}, now=101.0)

    snap = e.snapshot(now=101.0)
    assert [w["px"] for w in snap["walls"]["bid"]] == [78.90]
    assert [w["px"] for w in snap["walls"]["ask"]] == [79.20]
    nearest = snap["walls"]["nearest_bid"]
    assert nearest["n"] == 30 and nearest["sz"] == 5000
    assert nearest["dist_bps"] == round(abs(79.005 - 78.90) / 79.005 * 1e4, 3)


def test_atomic_write_json_produces_parseable_file_without_temp_leftovers(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})
    assert json.loads(path.read_text()) == {"a": 1, "b": [1, 2, 3]}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_read_micro_state_maps_signals_and_nearest_walls(tmp_path):
    path = tmp_path / "state.json"
    payload = {
        "schema": "mmbot-microstructure/1", "seq": 7, "ts": time.time(),
        "signals": {"obi_nw": 0.2, "tfi2": -0.3, "lead_btc_1s": 1.4, "btc_ready": True},
        "walls": {
            "nearest_bid": {"px": 78.9, "dist_bps": 14.0},
            "nearest_ask": {"px": 79.2, "dist_bps": 18.0},
        },
    }
    atomic_write_json(path, payload)
    micro = read_micro_state(path)
    assert micro is not None
    assert (micro.obi_nw, micro.tfi2, micro.lead_btc_1s) == (0.2, -0.3, 1.4)
    assert (micro.wall_bid_px, micro.wall_bid_bps) == (78.9, 14.0)
    assert (micro.wall_ask_px, micro.wall_ask_bps) == (79.2, 18.0)
    assert micro.btc_ready is True


def test_read_micro_state_rejects_stale_missing_and_bad_files(tmp_path):
    path = tmp_path / "state.json"
    assert read_micro_state(path) is None

    atomic_write_json(path, {"ts": time.time() - 30, "signals": {}})
    assert read_micro_state(path, max_age_s=2.0) is None

    path.write_text("{not json")
    assert read_micro_state(path) is None


def test_shared_reader_caches_until_mtime_changes(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"ts": time.time(), "signals": {"obi_nw": 0.1}})
    reader = SharedMicroStateReader(path, max_age_s=60.0)
    assert reader.read().obi_nw == 0.1

    # Same mtime -> cached object is reused without re-parsing.
    first = reader.read()
    assert reader.read() is first

    atomic_write_json(path, {"ts": time.time(), "signals": {"obi_nw": -0.4}})
    assert reader.read().obi_nw == -0.4


def test_shared_reader_returns_none_when_cached_state_ages_out(tmp_path):
    path = tmp_path / "state.json"
    atomic_write_json(path, {"ts": time.time(), "signals": {"obi_nw": 0.1}})
    reader = SharedMicroStateReader(path, max_age_s=0.05)
    assert reader.read() is not None
    time.sleep(0.1)
    assert reader.read() is None


def test_hl_reconnect_is_rate_limited(monkeypatch):
    feed = HyperliquidFeed(MicrostructureEngine("HYPE"))
    calls = []
    monkeypatch.setattr(feed, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(feed, "_build", lambda: calls.append("build"))
    feed._next_attempt = time.monotonic() + 30.0
    feed.reconnect()
    assert calls == []

    feed._next_attempt = 0.0
    feed.reconnect()
    assert calls == ["stop", "build"]
