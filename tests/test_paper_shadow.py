"""Offline tests for scripts/paper_shadow.py + scripts/paper_report.py (T15).

Everything runs on synthetic v2 shards under tmp_path — no network, no collector, no
real dataset. Covered: v2-header detection, closed-hour selection (never the current
hour), feeding an hour into the in-memory ReplayEngine (a fill appears), state/ledger
persistence, idempotency, and the report renderer.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import paper_report as pr  # noqa: E402
import paper_shadow as ps  # noqa: E402
from mmbot.precision import AssetSpec  # noqa: E402

UTC = dt.timezone.utc
DAY0 = dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
T0 = DAY0.timestamp()
NOW = dt.datetime(2026, 1, 1, 3, 0, tzinfo=UTC).timestamp()
SPECS = {"XMR": AssetSpec("XMR", 3)}


def _l2(t_s: float, bid: float, bsz: float, ask: float, asz: float) -> dict:
    t_ms = int((T0 + t_s) * 1000)
    return {"t": t_ms, "c": "XMR", "e": "l2", "r": t_ms + 50,
            "b": [[str(bid), str(bsz), 1]], "a": [[str(ask), str(asz), 1]]}


def _tr(t_s: float, side: str, px: float, sz: float) -> dict:
    t_ms = int((T0 + t_s) * 1000)
    return {"t": t_ms, "c": "XMR", "e": "tr", "r": t_ms + 50,
            "side": side, "px": str(px), "sz": str(sz)}


def _write_v2_shard(tmp: Path, day: str, hh: str, coin: str, records: list[dict],
                    header: bool = True) -> Path:
    d = tmp / "data" / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{hh}_{coin}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        if header:
            fh.write(json.dumps({"e": "header", "c": coin, "schema": ps.V2_SCHEMA,
                                 "recv_field": "r"}) + "\n")
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _tracker(tmp: Path, now: float = NOW) -> ps.ShadowTracker:
    return ps.ShadowTracker(
        data_dir=tmp / "data",
        specs=SPECS,
        candidates=[("XMR", 0)],  # hs=0 -> quotes at the touch, deterministic fill
        state_path=tmp / "state.json",
        ledger_path=tmp / "ledger.csv",
        log_path=tmp / "shadow.log",
        now=lambda: now,
    )


def test_is_v2_shard_detects_header(tmp_path: Path):
    good = _write_v2_shard(tmp_path, "20260101", "00", "XMR", [])
    bad = _write_v2_shard(tmp_path, "20260101", "01", "XMR", [{"t": 1, "c": "XMR", "e": "l2"}],
                          header=False)
    assert ps.is_v2_shard(good) is True
    assert ps.is_v2_shard(bad) is False
    assert ps.is_v2_shard(tmp_path / "missing.jsonl.gz") is False


def test_tracker_feeds_closed_hour_and_persists_metrics(tmp_path: Path):
    _write_v2_shard(tmp_path, "20260101", "00", "XMR", [
        _l2(0.0, 100.00, 10.0, 100.10, 1.0),
        _l2(0.4, 100.00, 10.0, 100.10, 1.0),
        _tr(1.0, "A", 99.99, 20.0),
    ])
    tr = _tracker(tmp_path)
    assert tr.cycle() == 1

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["today"] == "20260101"
    cand = state["candidates"]["XMR:0"]
    assert cand["processed_hours"] == 1
    assert cand["cumulative"]["fills"] == 1
    assert cand["cumulative"]["net_pnl_usd"] != 0.0
    assert "worst_drawdown_usd" in cand and "open_inventory_usd" in cand

    with (tmp_path / "ledger.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows and rows[0]["coin"] == "XMR" and rows[0]["hs"] == "0"
    assert float(rows[0]["fills"]) == 1.0
    for col in ps.LEDGER_COLUMNS:
        assert col in rows[0]


def test_tracker_is_idempotent_across_cycles(tmp_path: Path):
    _write_v2_shard(tmp_path, "20260101", "00", "XMR", [
        _l2(0.0, 100.00, 10.0, 100.10, 1.0),
        _l2(0.4, 100.00, 10.0, 100.10, 1.0),
        _tr(1.0, "A", 99.99, 20.0),
    ])
    tr = _tracker(tmp_path)
    assert tr.cycle() == 1
    assert tr.cycle() == 0, "a closed hour must be fed exactly once"
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["candidates"]["XMR:0"]["cumulative"]["fills"] == 1


def test_tracker_never_reads_the_current_hour(tmp_path: Path):
    # NOW = 03:00Z -> hour 03 is still being written by the collector, hour 02 is closed.
    _write_v2_shard(tmp_path, "20260101", "02", "XMR", [_l2(0.0, 100.0, 1.0, 100.1, 1.0)])
    _write_v2_shard(tmp_path, "20260101", "03", "XMR", [_l2(0.0, 100.0, 1.0, 100.1, 1.0)])
    tr = _tracker(tmp_path)
    assert tr.cycle() == 1, "only the closed hour (02) is fed"
    assert all(k[2] != "03" for k in tr.seen)


def test_tracker_skips_legacy_shards(tmp_path: Path):
    _write_v2_shard(tmp_path, "20260101", "00", "XMR", [_l2(0.0, 100.0, 1.0, 100.1, 1.0)],
                    header=False)
    tr = _tracker(tmp_path)
    assert tr.cycle() == 0
    assert not (tmp_path / "state.json").exists()


def test_report_renders_state_and_summary_line(tmp_path: Path):
    _write_v2_shard(tmp_path, "20260101", "00", "XMR", [_l2(0.0, 100.0, 1.0, 100.1, 1.0)])
    tr = _tracker(tmp_path)
    tr.cycle()
    state = json.loads((tmp_path / "state.json").read_text())
    text = "\n".join(pr.render(state))
    assert "XMR:0" in text and "cumulative" in text and "today" in text
    line = pr.summary_line(state)
    assert line.startswith("paper    :") and "XMR:0" in line
    assert "no shadow tracker state" in pr.summary_line(None)


def test_acquire_lock_prevents_duplicate_instances(tmp_path: Path):
    lock_path = tmp_path / "paper_shadow.lock"
    first = ps.acquire_lock(lock_path)
    assert first is not None
    assert ps.acquire_lock(lock_path) is None, "a second instance must not get the lock"
    first.close()
    again = ps.acquire_lock(lock_path)
    assert again is not None, "lock is released when the holder closes it"
    again.close()


def test_report_missing_state_is_graceful(tmp_path: Path):
    missing = tmp_path / "nope.json"
    assert pr.load_state(missing) is None
    assert "no state file" in "\n".join(pr.render(None))
