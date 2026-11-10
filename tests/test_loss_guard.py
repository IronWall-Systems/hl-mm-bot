"""Tests for the standalone hard-loss guard (stdlib only, clock injected)."""
from __future__ import annotations

import asyncio
import os

import pytest

from mmbot.risk.loss_guard import (
    GuardAction, LossGuard, LossGuardConfig, hard_stop_sequence, sl_trigger_px)


class _Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_start_equity_must_be_positive():
    with pytest.raises(ValueError):
        LossGuard(0.0)


def test_halt_path_defaults_to_config_and_can_be_overridden():
    assert LossGuard(21.15).halt_path == "HALT"
    assert LossGuard(21.15, LossGuardConfig(halt_file="/run/mmbot/HALT")).halt_path \
        == "/run/mmbot/HALT"
    assert LossGuard(21.15, halt_path="/tmp/x/HALT").halt_path == "/tmp/x/HALT"


def test_long_soft_then_hard_is_sticky():
    clk = _Clock()
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.16, 74.0)
    assert g.evaluate(74.0) is GuardAction.OK
    assert g.evaluate(74.0 - 0.49 / 0.16) is GuardAction.OK
    assert g.evaluate(74.0 - 0.51 / 0.16) is GuardAction.SOFT
    assert g.evaluate(74.0) is GuardAction.SOFT        # latched
    assert g.evaluate(74.0 - 0.81 / 0.16) is GuardAction.HARD
    assert g.evaluate(80.0) is GuardAction.HARD        # sticky even on recovery


def test_short_loses_when_price_rises():
    clk = _Clock()
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, -0.16, 74.0)
    assert g.evaluate(74.0 + 0.81 / 0.16) is GuardAction.HARD


def test_estimated_equity_tracks_mid_between_polls():
    clk = _Clock()
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.16, 74.0)
    assert g.estimated_equity(74.0) == pytest.approx(21.15)
    assert g.estimated_equity(73.0) == pytest.approx(21.15 - 0.16)


def test_stale_equity_pulls_then_hards_and_none_does_not_refresh():
    clk = _Clock(100.0)
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.0, 74.0)
    clk.t += 10
    g.on_poll(None, 0.0, 74.0)              # unreadable: clock must not refresh
    assert g.evaluate(74.0) is GuardAction.OK
    clk.t += 15                              # 25 s since the last good poll
    assert g.evaluate(74.0) is GuardAction.PULL
    clk.t += 70                              # 95 s
    assert g.evaluate(74.0) is GuardAction.HARD


def test_soft_without_flat_escalates_to_hard_after_deadline():
    clk = _Clock()
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.16, 74.0)
    assert g.evaluate(74.0 - 0.51 / 0.16, pos_size=0.16) is GuardAction.SOFT
    for _ in range(100):
        clk.t += 15
        g.on_poll(20.60, 0.16, 70.8)
        g.evaluate(70.8, pos_size=0.16)
        if g.sticky is GuardAction.HARD:
            break
    assert g.sticky is GuardAction.HARD


def test_soft_does_not_escalate_when_flat():
    clk = _Clock()
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.16, 74.0)
    assert g.evaluate(74.0 - 0.51 / 0.16, pos_size=0.16) is GuardAction.SOFT
    for _ in range(100):
        clk.t += 15
        g.on_poll(20.60, 0.0, 70.8)
        assert g.evaluate(70.8, pos_size=0.0) is GuardAction.SOFT
    assert g.sticky is GuardAction.OK


def test_on_chain_trigger_prices():
    floor = lambda p, mode: float(int(p * 100)) / 100.0
    ceil = lambda p, mode: -float(int(-p * 100)) / 100.0
    assert sl_trigger_px("long", 75.0, 0.04, floor) == 72.0
    assert sl_trigger_px("short", 75.0, 0.04, ceil) == 78.0


class _FakeGateway:
    def __init__(self, close_at: float = 0.03, cancel_fails: int = 0):
        self.pos = 0.16
        self.calls: list[float] = []
        self.cancels = 0
        self.close_at = close_at
        self.cancel_fails = cancel_fails

    async def cancel_all(self) -> int:
        self.cancels += 1
        if self.cancel_fails:
            self.cancel_fails -= 1
            raise RuntimeError("boom")
        return 2

    async def flatten(self, slippage: float) -> None:
        self.calls.append(slippage)
        if slippage >= self.close_at:
            self.pos = 0.0

    async def position(self) -> float:
        return self.pos


async def _no_sleep(_: float) -> None:
    return None


def test_hard_stop_flat_writes_halt_and_exits_3(tmp_path):
    gw = _FakeGateway(close_at=0.03)
    msgs: list[str] = []
    halt = tmp_path / "HALT"
    r = asyncio.run(hard_stop_sequence(gw, str(halt), msgs.append,
                                      sleep=_no_sleep))
    assert r.flat and r.exit_code == 3
    assert gw.calls == [0.01, 0.03]
    assert halt.read_text() == "hard stop\n"
    assert any("cancel_all ok" in m for m in msgs)


def test_hard_stop_stuck_logs_critical_and_exits_4():
    gw = _FakeGateway(close_at=99.0)       # never fills
    msgs: list[str] = []
    ticks = [0.0]

    def now() -> float:
        ticks[0] += 7.0
        return ticks[0]

    r = asyncio.run(hard_stop_sequence(gw, None, msgs.append, sleep=_no_sleep,
                                      now=now, max_total_s=30.0))
    assert not r.flat and r.exit_code == 4
    assert any("CRITICAL" in m for m in msgs)


def test_hard_stop_keeps_flattening_when_cancel_fails():
    gw = _FakeGateway(close_at=0.03, cancel_fails=3)
    msgs: list[str] = []
    r = asyncio.run(hard_stop_sequence(gw, None, msgs.append, sleep=_no_sleep))
    assert r.flat and r.exit_code == 3
    assert sum("cancel_all failed" in m for m in msgs) == 3


class _LateFillGateway(_FakeGateway):
    """Simulates an in-flight place that lands after the initial cancel."""

    def __init__(self) -> None:
        super().__init__(close_at=0.01)
        self.revived = False

    async def cancel_all(self) -> int:
        self.cancels += 1
        if self.cancels == 2 and not self.revived:   # first post-flatten sweep
            self.revived = True
            self.pos = 0.16                          # the late order filled
        return 1


def test_hard_stop_sweeps_after_flatten_and_catches_late_fill():
    gw = _LateFillGateway()
    msgs: list[str] = []
    r = asyncio.run(hard_stop_sequence(gw, None, msgs.append, sleep=_no_sleep))
    assert r.flat and r.exit_code == 3
    assert any("post-flatten cancel sweep 1" in m for m in msgs)
    assert any("late fill" in m for m in msgs)
    assert gw.calls == [0.01, 0.01]        # re-flattened the revived position
    assert gw.cancels == 3                 # initial cancel + two sweeps
