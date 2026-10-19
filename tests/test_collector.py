"""Offline tests for scripts/collector.py + scripts/collector_report.py (REQ-0034 Phase B).

No network: the websocket is a fake object, time/disk/rss/archive are injected, every file is
written under tmp_path. The real public connection is only opened by the actual launch.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collector as col  # noqa: E402
import collector_report as rep  # noqa: E402

APPROVED = ["BTC", "ETH", "HYPE", "kBONK", "kPEPE", "GRAM", "AVAX", "XMR", "XRP", "BNB"]
SHARD_RE = re.compile(r"^\d{2}_[A-Za-z0-9]+\.jsonl\.gz$")


# --------------------------------------------------------------------------- fakes
class Clock:
    def __init__(self, start: float = 1791307200.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeWS:
    def __init__(self, frames: list | None = None):
        self.frames = list(frames or [])
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self) -> str:
        if not self.frames:
            raise TimeoutError("idle")
        item = self.frames.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(self, rc: int | None = None):
        self.rc = rc
        self.waited = False

    def poll(self):
        return self.rc

    def wait(self, timeout: float | None = None):
        self.waited = True
        if self.rc is None:
            self.rc = 0
        return self.rc


def disk(free_bytes: int):
    def usage(_path) -> SimpleNamespace:
        return SimpleNamespace(total=1 << 40, used=(1 << 40) - free_bytes, free=free_bytes)

    return usage


def make_collector(
    tmp_path: Path,
    coins: list[str] | None = None,
    *,
    frames: list | None = None,
    sockets: list[FakeWS] | None = None,
    free: int = 4 << 30,
    rss: int = 40 * 1024 * 1024,
    clock: Clock | None = None,
    archive_enabled: bool = True,
    recv_ms=None,
    funding_fetch=None,
):
    clock = clock or Clock()
    coins = list(coins or APPROVED)
    recv_ms = recv_ms or (lambda: int(clock.now() * 1000))
    funding_fetch = funding_fetch or (lambda _coins: {})
    log = col.RotatingLog(tmp_path / "collector.log")
    shards = col.HourShards(tmp_path / "data", tmp_path / "data" / "raw_sample", coins, now=clock.now)
    sleeps: list[str] = []
    spawns: list[list[str]] = []
    holder: dict = {}
    queue = list(sockets) if sockets is not None else [FakeWS(frames or [])]

    def connect():
        if queue:
            return queue.pop(0)
        holder["c"].request_stop("test: fakes exhausted")
        raise col.CollectorError("no socket left")

    def spawn(cmd: list[str]):
        spawns.append(list(cmd))
        return FakeProc(rc=0)

    c = col.Collector(
        coins,
        connect=connect,
        shards=shards,
        log=log,
        now=clock.now,
        sleep=clock.sleep,
        disk_usage=disk(free),
        spawn_archive=spawn,
        rss=lambda status_path="/proc/self/status": rss
        if status_path == "/proc/self/status"
        else col.rss_bytes(status_path),
        data_dir=tmp_path / "data",
        archive_enabled=archive_enabled,
        recv_ms=recv_ms,
        funding_fetch=funding_fetch,
    )
    holder["c"] = c
    ctx = SimpleNamespace(
        collector=c, clock=clock, sleeps=sleeps, spawns=spawns, log=log, shards=shards, tmp=tmp_path
    )
    return ctx


def read_shards(ctx) -> list[dict]:
    lines: list[dict] = []
    for path in sorted((ctx.tmp / "data").glob("*/*.jsonl.gz")):
        if path.name.endswith("_gaps.jsonl.gz"):
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            lines.extend(json.loads(line) for line in fh if line.strip())
    return lines


def read_gaps(ctx) -> list[dict]:
    out: list[dict] = []
    for path in sorted((ctx.tmp / "data").glob("*/*_gaps.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            out.extend(json.loads(line) for line in fh if line.strip())
    return out


# --------------------------------------------------------------------------- helpers
def test_load_coins_matches_approved_selection():
    coins = col.load_coins(ROOT / "data" / "coins.json")
    assert coins == APPROVED
    assert len(coins) == 10


def test_load_coins_errors(tmp_path):
    with pytest.raises(col.CollectorError):
        col.load_coins(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(col.CollectorError):
        col.load_coins(bad)
    empty = tmp_path / "empty.json"
    empty.write_text('{"coins": []}')
    with pytest.raises(col.CollectorError):
        col.load_coins(empty)


def test_hour_key_is_utc():
    assert col.hour_key(86399) == ("19700101", "23")
    assert col.hour_key(86400) == ("19700102", "00")
    assert col.hour_key(1791307200) == ("20261006", "17")


def test_rss_bytes_from_status_file(tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tcollector\nVmRSS:\t  123456 kB\n")
    assert col.rss_bytes(str(status)) == 123456 * 1024
    assert col.rss_bytes(str(tmp_path / "nope")) == 0


# --------------------------------------------------------------------------- schemas
def test_compact_l2_schema_and_rejects():
    rec = col.compact_l2(
        {"coin": "BTC", "time": 1791307314558,
         "levels": [[{"px": "100.0", "sz": "1", "n": 2}], [{"px": "101.0", "sz": "2", "n": 3}]]}
    )
    assert rec == {"t": 1791307314558, "c": "BTC", "e": "l2",
                   "b": [["100.0", "1", 2]], "a": [["101.0", "2", 3]]}
    assert col.compact_l2({"levels": [[], []]})["c"] is None
    assert col.compact_l2({"levels": []}) is None
    assert col.compact_l2({"levels": "x"}) is None


def test_compact_bbo_schema_and_rejects():
    rec = col.compact_bbo(
        {"coin": "BTC", "time": 1791307329657,
         "bbo": [{"px": "85480.0", "sz": "42.79702", "n": 104},
                 {"px": "85481.0", "sz": "11.18165", "n": 5}]}
    )
    assert rec["e"] == "bbo"
    assert rec["b"] == ["85480.0", "42.79702", 104]
    assert rec["a"][0] == "85481.0"
    assert col.compact_bbo({"bbo": [{"px": "1"}]}) is None
    assert col.compact_bbo({"bbo": []}) is None


def test_compact_trade_drops_hash_and_users():
    rec = col.compact_trade(
        {"coin": "BTC", "side": "B", "px": "85477.0", "sz": "0.01039",
         "time": 1791307314558, "hash": "0xdeadbeef", "tid": 371443539898464,
         "users": ["0x089e", "0xd5e2"]}
    )
    assert rec == {"t": 1791307314558, "c": "BTC", "e": "tr", "side": "B",
                   "px": "85477.0", "sz": "0.01039", "tid": 371443539898464}
    assert "hash" not in rec and "users" not in rec
    assert col.compact_trade({"side": "B"}) is None
    assert col.compact_trade("nope") is None


def test_dispatch_counts_and_shapes():
    counts: Counter = Counter()
    l2 = json.dumps({"channel": "l2Book", "data": {"coin": "ETH", "time": 5,
                                                   "levels": [[{"px": "1", "sz": "1", "n": 1}],
                                                              [{"px": "2", "sz": "1", "n": 1}]]}})
    out = col.dispatch(l2, counts)
    assert [c for c, _ in out] == ["ETH"]
    assert counts["l2"] == 1

    bbo = json.dumps({"channel": "bbo", "data": {"coin": "HYPE", "time": 6,
                                                "bbo": [{"px": "1", "sz": "1", "n": 1},
                                                        {"px": "2", "sz": "1", "n": 1}]}})
    assert col.dispatch(bbo, counts)[0][1]["e"] == "bbo"
    assert counts["bbo"] == 1

    trades = json.dumps({"channel": "trades", "data": [
        {"coin": "BTC", "side": "A", "px": "1", "sz": "1", "time": 7, "tid": 1},
        {"coin": "BTC", "side": "B", "px": "1", "sz": "2", "time": 7, "tid": 2}]})
    assert len(col.dispatch(trades, counts)) == 2
    assert counts["tr"] == 2

    assert col.dispatch(json.dumps({"channel": "pong"}), counts) == []
    assert col.dispatch(json.dumps({"channel": "subscriptionResponse",
                                    "data": {"method": "subscribe"}}), counts) == []
    assert counts["control"] == 2
    assert col.dispatch("{not json", counts) == []
    assert counts["malformed"] == 1
    assert col.dispatch(json.dumps({"channel": "candle", "data": 1}), counts) == []
    assert counts["other"] == 1


def test_wsconnection_refused_inside_tests():
    with pytest.raises(col.CollectorError):
        col.WsConnection()


# --------------------------------------------------------------------------- log + shards
def test_rotating_log_keeps_configured_files(tmp_path):
    log = col.RotatingLog(tmp_path / "collector.log", max_bytes=400, files=3)
    for i in range(60):
        log.write("INFO", f"line {i:03d} " + "x" * 30)
    log.close()
    files = sorted(p.name for p in tmp_path.glob("collector.log*"))
    assert files == ["collector.log", "collector.log.1", "collector.log.2"]
    assert "line 059" in (tmp_path / "collector.log").read_text()
    assert (tmp_path / "collector.log").stat().st_size <= 400 + 120


def test_hour_shards_gzip_and_raw_first_hour(tmp_path):
    clock = Clock()
    coins = ["BTC"]
    shards = col.HourShards(tmp_path / "data", tmp_path / "data" / "raw_sample", coins, now=clock.now)
    shards.write("BTC", {"t": 1, "c": "BTC", "e": "l2", "b": [], "a": []})
    date, hh = col.hour_key(clock.now())
    gz = tmp_path / "data" / date / f"{hh}_BTC.jsonl.gz"
    raw = tmp_path / "data" / "raw_sample" / date / f"{hh}_BTC.jsonl"
    assert gz.is_file() and raw.is_file()
    shards.close()
    assert SHARD_RE.match(gz.name)
    with gzip.open(gz, "rt") as fh:
        payload = [json.loads(line) for line in fh]
    assert payload[0]["e"] == "header" and payload[0]["c"] == "BTC"
    assert payload[1]["e"] == "l2"
    encoded = [json.dumps(rec, separators=(",", ":")) for rec in payload]
    assert raw.read_text().splitlines() == encoded
    shards.close()


def test_hour_shards_checkpoint_is_readable_without_close(tmp_path):
    clock = Clock()
    shards = col.HourShards(tmp_path / "data", tmp_path / "data" / "raw_sample", ["BTC"],
                            now=clock.now)
    date, hh = col.hour_key(clock.now())
    gz = tmp_path / "data" / date / f"{hh}_BTC.jsonl.gz"
    shards.write("BTC", {"t": 1, "c": "BTC", "e": "l2", "b": [], "a": []})
    assert gz.stat().st_size == 0, "gzip holds everything in userspace until finalized"
    shards.flush()
    assert gz.stat().st_size > 0
    assert subprocess.run(["gzip", "-t", str(gz)]).returncode == 0, "valid gzip mid-hour"
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        recs = [json.loads(x) for x in fh if x.strip()]
    assert [r["e"] for r in recs] == ["header", "l2"]

    shards.write("BTC", {"t": 2, "c": "BTC", "e": "tr", "side": "B", "px": "1", "sz": "1"})
    shards.flush()
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        recs = [json.loads(x) for x in fh if x.strip()]
    assert [r["e"] for r in recs] == ["header", "l2", "tr"], "appended member keeps header once"
    shards.close()


def test_hour_shards_maybe_flush_respects_interval(tmp_path):
    clock = Clock()
    shards = col.HourShards(tmp_path / "data", tmp_path / "data" / "raw_sample", ["BTC"],
                            now=clock.now)
    date, hh = col.hour_key(clock.now())
    gz = tmp_path / "data" / date / f"{hh}_BTC.jsonl.gz"
    shards.write("BTC", {"t": 1, "c": "BTC", "e": "l2", "b": [], "a": []})
    clock.advance(1.0)
    shards.maybe_flush(clock.now())
    assert gz.stat().st_size == 0, "no checkpoint before the interval elapses"
    clock.advance(col.FLUSH_EVERY_S)
    shards.maybe_flush(clock.now())
    assert gz.stat().st_size > 0
    shards.close()


def test_housekeeping_flushes_shards_durably(tmp_path):
    clock = Clock()
    frame = json.dumps({"channel": "l2Book", "data": {"coin": "BTC", "time": 1,
                                                      "levels": [[], []]}})
    ctx = make_collector(tmp_path, coins=["BTC"], frames=[frame], clock=clock)
    ctx.collector._handle(frame)
    date, hh = col.hour_key(clock.now())
    gz = tmp_path / "data" / date / f"{hh}_BTC.jsonl.gz"
    assert gz.stat().st_size == 0
    clock.advance(col.FLUSH_EVERY_S + 1.0)
    ctx.collector.housekeeping(force=True)
    assert gz.stat().st_size > 0, "housekeeping checkpoints shards so a crash loses <= the interval"
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        assert any(json.loads(x).get("e") == "l2" for x in fh if x.strip())


def test_hour_shards_rollover_closes_hour_and_writes_gap(tmp_path):
    clock = Clock()
    shards = col.HourShards(tmp_path / "data", tmp_path / "data" / "raw_sample", ["BTC"], now=clock.now)
    date1, hh1 = col.hour_key(clock.now())
    shards.write("BTC", {"t": 1, "c": "BTC", "e": "bbo", "b": None, "a": None})
    shards.write_gap({"t": 2, "e": "gap", "reason": "test", "reconnect": 1})
    clock.advance(3600)
    shards.write("BTC", {"t": 3, "c": "BTC", "e": "bbo", "b": None, "a": None})
    date2, hh2 = col.hour_key(clock.now())
    shards.close()

    assert (tmp_path / "data" / date1 / f"{hh1}_gaps.jsonl.gz").is_file()
    assert (tmp_path / "data" / date1 / f"{hh1}_BTC.jsonl.gz").is_file()
    assert (tmp_path / "data" / date2 / f"{hh2}_BTC.jsonl.gz").is_file()
    with gzip.open(tmp_path / "data" / date2 / f"{hh2}_BTC.jsonl.gz", "rt") as fh:
        second_hour = [json.loads(line) for line in fh]
    assert second_hour[0]["e"] == "header", "each new shard opens with a format header"
    events = [r for r in second_hour if r.get("e") != "header"]
    assert [r["t"] for r in events] == [3]
    raw2 = tmp_path / "data" / "raw_sample" / date2 / f"{hh2}_BTC.jsonl"
    assert not raw2.exists(), "raw copy only for the first hour"
    with gzip.open(tmp_path / "data" / date1 / f"{hh1}_gaps.jsonl.gz", "rt") as fh:
        gaps = [json.loads(line) for line in fh]
    assert gaps[0]["e"] == "gap" and gaps[0]["reconnect"] == 1


def test_write_pid_file(tmp_path):
    pid_file = tmp_path / "collector_pid.txt"
    col.write_pid_file(pid_file, APPROVED)
    lines = pid_file.read_text().splitlines()
    assert int(lines[0]) == os.getpid()
    assert f"coins={','.join(APPROVED)}" in pid_file.read_text()


# --------------------------------------------------------------------------- collector run
def test_subscribes_all_ten_coins_on_one_connection(tmp_path):
    ctx = make_collector(tmp_path, frames=[json.dumps({"channel": "pong"})])
    assert ctx.collector.run(max_loops=4) == 0
    ws_sent = ctx.collector.counts["subs_sent"]
    assert ws_sent == 10 * 3
    assert ctx.collector.reconnects == 0
    log = (tmp_path / "collector.log").read_text()
    assert "subscribed 30" in log
    assert "l2Book fast + trades + bbo, one connection" in log
    assert "collector stopped" in log


def test_subscriptions_payload_shapes(tmp_path):
    l2_frame = json.dumps({"channel": "l2Book", "data": {
        "coin": "BTC", "time": 1,
        "levels": [[{"px": "1", "sz": "1", "n": 1}], [{"px": "2", "sz": "1", "n": 1}]]}})
    bbo_frame = json.dumps({"channel": "bbo", "data": {
        "coin": "HYPE", "time": 2,
        "bbo": [{"px": "1", "sz": "1", "n": 1}, {"px": "2", "sz": "1", "n": 1}]}})
    tr_frame = json.dumps({"channel": "trades", "data": [
        {"coin": "XMR", "side": "B", "px": "1", "sz": "1", "time": 3, "tid": 9}]})
    ctx = make_collector(tmp_path, frames=[l2_frame, bbo_frame, tr_frame])
    ctx.collector.run(max_loops=6)

    ws = FakeWS()
    ctx.collector._subscribe(ws)
    payloads = [json.loads(p) for p in ws.sent]
    assert len(payloads) == 30
    assert all(p["method"] == "subscribe" for p in payloads)
    types = [p["subscription"]["type"] for p in payloads[:3]]
    assert types == ["l2Book", "trades", "bbo"]
    assert payloads[0]["subscription"]["fast"] is True
    assert "fast" not in payloads[1]["subscription"]
    assert "fast" not in payloads[2]["subscription"]
    assert payloads[0]["subscription"]["coin"] == "BTC"
    assert payloads[-1]["subscription"]["coin"] == "BNB"

    recs = [r for r in read_shards(ctx) if r.get("e") != "header"]
    assert {r["e"] for r in recs} == {"l2", "bbo", "tr"}
    assert next(r for r in recs if r["e"] == "tr")["c"] == "XMR"
    assert ctx.collector.counts["malformed"] == 0
    ctx.collector.shutdown()


# ------------------------------------------------------- v2: recv stamp / header / gap / funding
def test_every_recorded_event_carries_recv_stamp(tmp_path):
    l2_frame = json.dumps({"channel": "l2Book", "data": {
        "coin": "BTC", "time": 1,
        "levels": [[{"px": "1", "sz": "1", "n": 1}], [{"px": "2", "sz": "1", "n": 1}]]}})
    bbo_frame = json.dumps({"channel": "bbo", "data": {
        "coin": "HYPE", "time": 2,
        "bbo": [{"px": "1", "sz": "1", "n": 1}, {"px": "2", "sz": "1", "n": 1}]}})
    tr_frame = json.dumps({"channel": "trades", "data": [
        {"coin": "XMR", "side": "B", "px": "1", "sz": "1", "time": 3, "tid": 9}]})
    ctx = make_collector(tmp_path, frames=[l2_frame, bbo_frame, tr_frame])
    ctx.collector.run(max_loops=6)
    events = [r for r in read_shards(ctx) if r.get("e") in ("l2", "bbo", "tr")]
    assert events, "frames must be recorded"
    expected = int(ctx.clock.now() * 1000)
    assert all(isinstance(r.get("r"), int) for r in events)
    assert all(r["r"] == expected for r in events)
    ctx.collector.shutdown()


def test_new_shard_starts_with_format_header(tmp_path):
    clock = Clock()
    shards = col.HourShards(tmp_path / "data", tmp_path / "data" / "raw_sample", ["BTC"], now=clock.now)
    shards.write("BTC", {"t": 1, "c": "BTC", "e": "l2", "b": [], "a": []})
    date, hh = col.hour_key(clock.now())
    shards.close()
    with gzip.open(tmp_path / "data" / date / f"{hh}_BTC.jsonl.gz", "rt") as fh:
        lines = [json.loads(x) for x in fh if x.strip()]
    assert lines[0]["e"] == "header"
    assert lines[0]["c"] == "BTC"
    assert lines[0]["schema"] == col.SCHEMA_ID
    assert lines[0]["recv_field"] == "r"
    assert lines[1]["e"] == "l2"
    clock.advance(3600)
    shards.write("BTC", {"t": 2, "c": "BTC", "e": "l2", "b": [], "a": []})
    date2, hh2 = col.hour_key(clock.now())
    shards.close()
    with gzip.open(tmp_path / "data" / date2 / f"{hh2}_BTC.jsonl.gz", "rt") as fh:
        lines2 = [json.loads(x) for x in fh if x.strip()]
    assert lines2[0]["e"] == "header" and lines2[0]["c"] == "BTC"
    assert lines2[1]["e"] == "l2"


def test_reconnect_writes_gap_marker_per_coin(tmp_path):
    boom = FakeWS([ConnectionError("server went away")])
    healthy = FakeWS([json.dumps({"channel": "bbo", "data": {
        "coin": "BTC", "time": 9, "bbo": [{"px": "1", "sz": "1", "n": 1},
                                          {"px": "2", "sz": "1", "n": 1}]}})])
    ctx = make_collector(tmp_path, coins=["BTC", "ETH"], sockets=[boom, healthy])
    ctx.collector.run(max_loops=5)
    per_coin: dict[str, list] = {}
    for path in sorted((ctx.tmp / "data").glob("*/*.jsonl.gz")):
        if path.name.endswith("_gaps.jsonl.gz"):
            continue
        with gzip.open(path, "rt") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("e") == "gap":
                    per_coin.setdefault(rec.get("c"), []).append(rec)
    assert set(per_coin) == {"BTC", "ETH"}, "a reconnect must mark EVERY subscribed coin"
    assert all(rows and rows[0]["reconnect"] >= 1 for rows in per_coin.values())
    assert read_gaps(ctx), "the global gap file is still written"
    ctx.collector.shutdown()


def test_funding_poll_writes_funding_records(tmp_path):
    clock = Clock()
    calls: list[list[str]] = []

    def fake_fetch(coins):
        calls.append(list(coins))
        return {"BTC": {"funding": 0.0000125, "mark_px": 86000.0, "open_interest": 123.0}}

    ctx = make_collector(tmp_path, coins=["BTC"], clock=clock, funding_fetch=fake_fetch)
    ctx.collector.housekeeping(force=True)
    assert calls, "funding fetch must run on the first housekeeping tick"
    ctx.shards.close()
    recs = [r for r in read_shards(ctx) if r.get("e") == "funding"]
    assert len(recs) == 1
    assert recs[0]["c"] == "BTC" and recs[0]["funding"] == 0.0000125
    assert recs[0]["mark_px"] == 86000.0 and isinstance(recs[0]["r"], int)
    ctx.collector.housekeeping(force=True)
    assert len(calls) == 1, "must not poll again inside the interval"
    clock.advance(61)
    ctx.collector.housekeeping(force=True)
    assert len(calls) == 2
    assert ctx.collector.funding_samples == 2
    ctx.collector.shutdown()


def test_funding_poll_failure_is_logged_not_fatal(tmp_path):
    def boom(_coins):
        raise RuntimeError("rest down")

    ctx = make_collector(tmp_path, free=4 << 30, funding_fetch=boom)
    ctx.collector.housekeeping(force=True)
    assert ctx.collector.stop_reason is None
    assert "funding poll failed" in (tmp_path / "collector.log").read_text()
    ctx.collector.shutdown()


def test_parse_funding_maps_universe_to_ctx():
    doc = [
        {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "XRP"}]},
        [
            {"funding": "0.0000125", "markPx": "86000.0", "openInterest": "10", "premium": "-0.0001"},
            {"funding": "-0.0002", "markPx": "2700.5", "openInterest": "20", "premium": "0.0003"},
            {"funding": "0.0", "markPx": "1.51", "openInterest": "30", "premium": "0.0"},
        ],
    ]
    out = col.parse_funding(doc, ["ETH", "XRP"])
    assert set(out) == {"ETH", "XRP"}
    assert out["ETH"]["funding"] == -0.0002 and out["ETH"]["mark_px"] == 2700.5
    assert out["XRP"]["open_interest"] == 30.0
    assert col.parse_funding({"bad": 1}, ["BTC"]) == {}


def test_survives_malformed_frames_and_stops_cleanly(tmp_path):
    frames = ["{not json", json.dumps({"channel": "l2Book", "data": "weird"}),
              json.dumps({"channel": "trades", "data": [{"not": "a trade"}]}),
              json.dumps({"channel": "mystery", "data": 1})]
    ctx = make_collector(tmp_path, frames=frames)
    assert ctx.collector.run(max_loops=8) == 0
    assert ctx.collector.counts["malformed"] >= 1
    assert ctx.collector.counts["other"] >= 1
    assert ctx.collector.stop_reason.startswith("max_loops")
    ctx.collector.shutdown()


def test_reconnect_records_gap_and_backs_off(tmp_path):
    boom = FakeWS([ConnectionError("server went away")])
    healthy = FakeWS([json.dumps({"channel": "bbo", "data": {
        "coin": "BTC", "time": 9, "bbo": [{"px": "1", "sz": "1", "n": 1},
                                          {"px": "2", "sz": "1", "n": 1}]}})])
    ctx = make_collector(tmp_path, sockets=[boom, healthy])
    ctx.collector.run(max_loops=5)

    assert ctx.collector.reconnects == 1
    gaps = read_gaps(ctx)
    assert len(gaps) == 1
    assert gaps[0]["e"] == "gap"
    assert "server went away" in gaps[0]["reason"]
    backoffs = [s for s in ctx.clock.sleeps if s != 0.01]
    assert backoffs == [1.0]
    assert healthy.closed is True
    log = (tmp_path / "collector.log").read_text()
    assert "gap #1" in log and "reconnect in 1s" in log
    assert ctx.collector.counts["bbo"] >= 1


def test_backoff_doubles_and_caps_at_60(tmp_path):
    class BoomWS(FakeWS):
        def recv(self):
            raise ConnectionError("always down")

    socks = [BoomWS() for _ in range(8)]
    ctx = make_collector(tmp_path, sockets=socks, free=1 << 30)
    ctx.collector.run(max_loops=2)
    backoffs = [s for s in ctx.clock.sleeps if s != 0.01]
    assert backoffs[:5] == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert backoffs[-1] == 60.0, "backoff must cap at 60 s"
    assert ctx.collector.reconnects >= 5


def test_stops_cleanly_when_free_below_300mb(tmp_path):
    ctx = make_collector(tmp_path, free=100 * 1024 * 1024)
    assert ctx.collector.run(max_loops=3) == 0
    assert "free space" in ctx.collector.stop_reason
    assert ctx.collector.counts["subs_sent"] == 0, "must not subscribe before the disk check"
    day_dirs = [p for p in (tmp_path / "data").iterdir() if p.is_dir()]
    assert day_dirs, "stopping must not delete anything"
    assert (tmp_path / "collector.log").read_text().count("collector stopped") == 1


def test_archive_spawned_only_when_free_below_1gib(tmp_path):
    ctx = make_collector(tmp_path, free=4 << 30, archive_enabled=True)
    ctx.collector.housekeeping(force=True)
    assert ctx.spawns == [], "healthy disk must not trigger an archive run"

    ctx2 = make_collector(tmp_path / "b", free=512 << 20, archive_enabled=True)
    ctx2.collector.housekeeping(force=True)
    assert len(ctx2.spawns) == 1
    assert ctx2.spawns[0][-2:] == ["--auto", "--apply"]
    assert ctx2.spawns[0][0] == sys.executable
    assert ctx2.spawns[0][1].endswith("archive_data.py")


def test_archive_respawned_every_five_minutes(tmp_path):
    clock = Clock()
    ctx = make_collector(tmp_path, free=512 << 20, clock=clock, archive_enabled=True)
    ctx.collector.housekeeping(force=True)
    assert len(ctx.spawns) == 1
    clock.advance(10)
    ctx.collector.housekeeping(force=True)  # finishes the run (FakeProc.rc = 0)
    assert len(ctx.spawns) == 1
    clock.advance(299)
    ctx.collector.housekeeping(force=True)
    assert len(ctx.spawns) == 1, "shorter than 5 min must not respawn"
    clock.advance(1)
    ctx.collector.housekeeping(force=True)
    assert len(ctx.spawns) == 2
    assert ctx.collector.archive_last_rc == 0


def test_archive_disabled_flag(tmp_path):
    ctx = make_collector(tmp_path, free=512 << 20, archive_enabled=False)
    ctx.collector.housekeeping(force=True)
    assert ctx.spawns == []


def test_rss_warning_paths(tmp_path):
    ctx = make_collector(tmp_path, free=4 << 30, rss=95 * 1024 * 1024, clock=Clock())
    ctx.collector.housekeeping(force=True)
    ctx.clock.advance(61)
    ctx.collector.housekeeping(force=True)
    log = (tmp_path / "collector.log").read_text()
    assert "rss 95.0 MB >= 90 MB" in log

    ctx2 = make_collector(tmp_path / "hot", free=4 << 30, rss=110 * 1024 * 1024, clock=Clock())
    ctx2.collector.housekeeping(force=True)
    assert "rss 110.0 MB >= 100 MB limit" in (tmp_path / "hot" / "collector.log").read_text()


def test_stat_line_written(tmp_path):
    ctx = make_collector(tmp_path, free=4 << 30, clock=Clock())
    ctx.collector.housekeeping(force=True)
    ctx.collector._handle(json.dumps({"channel": "l2Book", "data": {
        "coin": "BTC", "time": 1,
        "levels": [[{"px": "1", "sz": "1", "n": 1}], [{"px": "2", "sz": "1", "n": 1}]]}}))
    ctx.collector._handle(json.dumps({"channel": "bbo", "data": {
        "coin": "BTC", "time": 2, "bbo": [{"px": "1", "sz": "1", "n": 1},
                                          {"px": "2", "sz": "1", "n": 1}]}}))
    ctx.clock.advance(61)
    ctx.collector.housekeeping(force=True)
    ctx.collector.shutdown()
    log = (tmp_path / "collector.log").read_text()
    stat_lines = [ln for ln in log.splitlines() if "] STAT " in ln]
    assert stat_lines, "a STAT line must be written every 60 s"
    assert "l2=1" in stat_lines[-1] and "bbo=1" in stat_lines[-1]


# --------------------------------------------------------------------------- report
def test_report_is_at_most_25_lines_and_offline(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "collector_report.py")],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) <= 25, f"report printed {len(lines)} lines"
    assert lines[0].startswith("=== collector report ")
    assert any(ln.startswith("status   :") for ln in lines)
    assert any(ln.startswith("coins    : 10") for ln in lines)
    assert any(ln.startswith("disk     :") for ln in lines)


def test_report_line_budget_helper():
    assert rep.MAX_LINES == 25


def test_report_proc_parsing_is_correct():
    nice = rep.nice_of(os.getpid())
    assert nice.lstrip("-").isdigit()
    uptime = rep.uptime_s(os.getpid())
    assert uptime.endswith("s") and uptime != "?s"
    seconds = int(uptime[:-1])
    assert 0 <= seconds < 86400, "uptime must be the process age, not a clock tick"
