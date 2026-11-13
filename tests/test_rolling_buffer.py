"""Rolling buffer: retention window, compaction, delayed reward persistence."""
from __future__ import annotations

import json

import pytest

from mmbot.learning.buffer import (MAX_RETENTION_S, DecisionStep,
                                   RollingExperienceBuffer)


def mkstep(ts: float, sid: int = 0, action: str = "quote_both") -> DecisionStep:
    return DecisionStep(step_id=sid, ts=ts, mono=ts, coin="HYPE",
                        features={"obi_nw": 0.1, "tfi2": -0.2},
                        action=action, propensity=0.5, guard_status="ok",
                        mid=100.0, position=0.0, equity=100.0)


def test_retention_window_trims_and_compacts(tmp_path):
    path = tmp_path / "buf.jsonl"
    buf = RollingExperienceBuffer(path, retention_s=150, now_fn=lambda: 1000.0)
    for t in (700.0, 800.0, 900.0, 1000.0):
        buf.append(mkstep(t))
    assert len(buf) == 4
    removed = buf.trim(now=1000.0)
    assert removed == 2
    assert [s.ts for s in buf.all()] == [900.0, 1000.0]
    # Reload must see the compacted file, not the pre-trim history.
    again = RollingExperienceBuffer(path, retention_s=150, now_fn=lambda: 1000.0)
    assert [s.ts for s in again.all()] == [900.0, 1000.0]
    assert json.loads(path.read_text().splitlines()[0])["type"] == "step"


def test_reward_updates_persist_and_recombine(tmp_path):
    path = tmp_path / "buf.jsonl"
    buf = RollingExperienceBuffer(path, retention_s=10_000, now_fn=lambda: 1000.0)
    step = buf.append(mkstep(1000.0))
    buf.set_reward(step.step_id, 30, 0.02)
    buf.set_reward(step.step_id, 300, 0.04)
    assert step.reward_30s == pytest.approx(0.02)
    assert step.reward_300s == pytest.approx(0.04)
    assert step.reward == pytest.approx(0.03)

    again = RollingExperienceBuffer(path, retention_s=10_000, now_fn=lambda: 1000.0)
    loaded = again.all()[0]
    assert loaded.reward_30s == pytest.approx(0.02)
    assert loaded.reward_300s == pytest.approx(0.04)
    assert loaded.reward == pytest.approx(0.03)


def test_recent_window(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10_000,
                                  now_fn=lambda: 300.0)
    for t in (100.0, 200.0, 300.0):
        buf.append(mkstep(t))
    assert [s.ts for s in buf.recent(150.0, now=300.0)] == [200.0, 300.0]
    assert len(buf.recent(None)) == 3


def test_max_steps_cap_keeps_newest(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10_000,
                                  max_steps=3, now_fn=lambda: 1000.0)
    for t in range(5):
        buf.append(mkstep(float(t)))
    removed = buf.trim(now=1000.0)
    assert removed == 2
    assert [s.ts for s in buf.all()] == [2.0, 3.0, 4.0]


def test_retention_is_clamped_to_seven_days(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10 ** 9,
                                  now_fn=lambda: 0.0)
    assert buf.retention_s == pytest.approx(MAX_RETENTION_S)
    assert buf.set_retention(60 * 24 * 3600) == pytest.approx(MAX_RETENTION_S)


def test_guard_status_and_stats(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10_000,
                                  now_fn=lambda: 1000.0)
    s = mkstep(1000.0)
    s.guard_status = "soft"
    buf.append(s)
    assert buf.latest_guard_status(now=1000.0) == "soft"
    st = buf.stats()
    assert st["steps"] == 1 and st["by_guard"] == {"soft": 1}


def test_compaction_is_batched_but_force_compacts(tmp_path):
    path = tmp_path / "b.jsonl"
    buf = RollingExperienceBuffer(path, retention_s=10, compact_min_steps=100,
                                  compact_every_s=1000.0,
                                  now_fn=lambda: 2000.0)
    buf.append(mkstep(950.0))
    buf._last_compact = 2000.0          # a compaction just happened
    removed = buf.trim(now=2000.0)      # cutoff 1940: drop the 950 step
    assert removed == 1
    # The in-memory view is trimmed; the file rewrite is deferred.
    assert len(buf) == 0
    assert len(path.read_text().splitlines()) == 1
    buf.trim(now=2000.0, force=True)
    assert path.read_text() == ""


def test_load_prunes_expired_history(tmp_path):
    path = tmp_path / "b.jsonl"
    buf = RollingExperienceBuffer(path, retention_s=100, now_fn=lambda: 1000.0)
    buf.append(mkstep(500.0))
    buf.append(mkstep(950.0))
    again = RollingExperienceBuffer(path, retention_s=100, now_fn=lambda: 1000.0)
    assert [s.ts for s in again.all()] == [950.0]


def test_parquet_export_is_optional(tmp_path):
    buf = RollingExperienceBuffer(tmp_path / "b.jsonl", retention_s=10_000,
                                  now_fn=lambda: 1000.0)
    buf.append(mkstep(1000.0))
    result = buf.export_parquet(tmp_path / "b.parquet")
    assert isinstance(result, bool)
