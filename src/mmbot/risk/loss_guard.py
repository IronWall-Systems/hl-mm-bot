"""Hard loss limit for a small live test.

Stdlib only, clock injectable, no exchange SDK import.  This module is *logic*
only: it does not place, cancel, or flatten anything by itself.

Layers (all independent; none is a guarantee):
  1. On-chain reduce-only stop per position -> survives a dead VPS / lost link
     (``sl_trigger_px`` helper).
  2. ``LossGuard`` soft stop -> no new risk, exits only, short time limit.
  3. ``LossGuard`` hard stop -> cancel everything, taker-flatten ladder, halt
     marker, exit code 3 (4 if still not flat).
  4. Staleness / unreadable-equity handling -> pull quotes first, flatten later
     (never trade blind).

Why the HARD threshold sits below the loss you can afford: equity is polled and
a taker flatten slips.  The guard therefore *estimates* equity on every book
tick as ``last_polled_equity + pos * (mid - mid_at_poll)``.  Even so, a gap or
an exchange stall can lose more than the threshold: keep a buffer (hard = 80%
of the limit).

Not wired into ``mmbot.main`` by default.  The caller owns the actions:
  * OK    -> normal quoting
  * SOFT  -> quote the reducing side only
  * PULL  -> cancel resting quotes
  * HARD  -> run ``hard_stop_sequence`` and exit with its code (pass
             ``guard.halt_path`` so the marker matches the systemd check)
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional, Protocol


class GuardAction(str, Enum):
    OK = "ok"
    SOFT = "soft"              # stop adding risk: reduce-only quoting, time-limited
    PULL = "pull"              # equity unreadable / stale: cancel quotes, do not trade blind
    HARD = "hard"              # cancel all + flatten + halt


@dataclass(frozen=True)
class LossGuardConfig:
    soft_loss_usd: float = 0.50
    hard_loss_usd: float = 0.80          # buffer below the $1.00 limit
    poll_stale_s: float = 20.0           # no equity poll for this long -> PULL
    unreadable_hard_s: float = 90.0      # still no equity -> HARD
    soft_flat_deadline_s: float = 1200.0  # in SOFT, not flat after this -> HARD
    halt_file: str = "HALT"


class LossGuard:
    def __init__(self, start_equity: float,
                 cfg: LossGuardConfig = LossGuardConfig(),
                 now: Callable[[], float] = time.monotonic,
                 halt_path: Optional[str] = None):
        if start_equity <= 0:
            raise ValueError("start_equity must be > 0 (equity unreadable?)")
        self.e0, self.cfg, self.now = start_equity, cfg, now
        # Caller passes this straight to hard_stop_sequence(); configurable so
        # the marker sits next to the service's ExecStartPre check.
        self.halt_path = cfg.halt_file if halt_path is None else halt_path
        self.last_equity: Optional[float] = start_equity
        self.last_pos = 0.0
        self.last_mid: Optional[float] = None
        self.last_poll = now()
        self._soft_since: Optional[float] = None
        self.sticky: GuardAction = GuardAction.OK

    # ---- inputs -------------------------------------------------------------
    def on_poll(self, equity: Optional[float], pos_size: float,
                mid: float) -> None:
        """Call every ~5 s with the account value and signed position."""
        if equity is None or equity <= 0:
            return                       # unreadable: do NOT refresh last_poll
        self.last_equity = equity
        self.last_pos = pos_size
        self.last_mid = mid
        self.last_poll = self.now()

    def estimated_equity(self, mid: float) -> Optional[float]:
        if self.last_equity is None:
            return None
        if self.last_mid is None:
            return self.last_equity
        return self.last_equity + self.last_pos * (mid - self.last_mid)

    # ---- decision -----------------------------------------------------------
    def evaluate(self, mid: Optional[float],
                 pos_size: Optional[float] = None) -> GuardAction:
        """Decide from the latest mid.

        ``mid=None`` means no live book has been seen yet; the equity estimate
        is skipped and only the staleness / soft-deadline rules apply.
        """
        t, c = self.now(), self.cfg
        if self.sticky is GuardAction.HARD:
            return GuardAction.HARD
        age = t - self.last_poll
        if age > c.unreadable_hard_s:
            self.sticky = GuardAction.HARD
            return GuardAction.HARD
        est = self.estimated_equity(mid) if mid is not None else None
        loss = self.e0 - est if est is not None else 0.0
        if loss >= c.hard_loss_usd:
            self.sticky = GuardAction.HARD
            return GuardAction.HARD
        flat = abs(pos_size if pos_size is not None else self.last_pos) < 1e-9
        if (self._soft_since is not None and not flat
                and t - self._soft_since > c.soft_flat_deadline_s):
            self.sticky = GuardAction.HARD
            return GuardAction.HARD
        if age > c.poll_stale_s:
            return GuardAction.PULL
        if loss >= c.soft_loss_usd or self._soft_since is not None:
            if self._soft_since is None:
                self._soft_since = t
            return GuardAction.SOFT      # latched until the process restarts
        return GuardAction.OK


# ---- on-chain stop helper ---------------------------------------------------
def sl_trigger_px(side: str, entry_px: float, adverse_pct: float,
                  round_fn: Callable[[float, str], float]) -> float:
    """Trigger price for a reduce-only stop on a position opened at ``entry_px``.

    ``side`` is the POSITION side: ``"long"`` stops below entry, ``"short"``
    stops above.  ``round_fn(px, "floor"|"ceil")`` must apply the exchange tick
    rules.  Typical ``adverse_pct`` for a ~$11 position: ``0.04`` (~-$0.45).
    """
    if side == "long":
        return round_fn(entry_px * (1.0 - adverse_pct), "floor")
    return round_fn(entry_px * (1.0 + adverse_pct), "ceil")


# ---- hard stop sequence -----------------------------------------------------
class Gateway(Protocol):
    async def cancel_all(self) -> int: ...
    async def flatten(self, slippage: float = 0.05) -> None: ...   # taker IOC close
    async def position(self) -> float: ...


@dataclass
class HardStopResult:
    flat: bool
    attempts: int
    exit_code: int


async def hard_stop_sequence(
        gw: Gateway, halt_path: Optional[str], log: Callable[[str], None],
        slippages=(0.01, 0.03, 0.05), wait_s: float = 1.5,
        max_total_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic) -> HardStopResult:
    """1) cancel everything (retry), 2) taker-flatten ladder, 3) verify flat,
    4) cancel again (catch orders that landed late), 5) write HALT marker.

    Never gives up silently: if still not flat after ``max_total_s`` it logs
    CRITICAL and returns exit code 4.  Exit code 3 means flat.
    """
    t0 = now()
    deadline = t0 + max_total_s
    for i in range(3):
        try:
            n = await gw.cancel_all()
            log(f"cancel_all ok ({n} orders)")
            break
        except Exception as e:            # keep going: the position matters more
            log(f"cancel_all failed ({e!r}), retry {i + 1}")
            await sleep(0.5)

    async def _ladder() -> tuple[bool, int]:
        """Taker-flatten ladder until flat or the overall deadline expires."""
        tries, done = 0, False
        while now() < deadline and not done:
            for slip in slippages:
                tries += 1
                try:
                    if abs(await gw.position()) < 1e-9:
                        done = True
                        break
                    await gw.flatten(slip)
                    await sleep(wait_s)
                    done = abs(await gw.position()) < 1e-9
                except Exception as e:
                    log(f"flatten attempt {tries} at slippage {slip} "
                        f"failed ({e!r})")
                    await sleep(wait_s)
                if done:
                    break
            if not done:
                await sleep(5.0)
        return done, tries

    attempts, flat = 0, False
    flat, n_tries = await _ladder()
    attempts += n_tries

    # An in-flight place can land AFTER the initial cancel_all's open-orders
    # query and survive the flatten (same race as mmbot.safety.cancel_sweep).
    # Cancel again, then re-verify: if a late order filled, flatten it too.
    sweeps = 0
    while flat and sweeps < 2 and now() < deadline:
        sweeps += 1
        try:
            n = await gw.cancel_all()
            log(f"post-flatten cancel sweep {sweeps}: cancel_all ok ({n} orders)")
        except Exception as e:
            log(f"post-flatten cancel sweep {sweeps} failed ({e!r})")
        await sleep(0.5)
        try:
            flat = abs(await gw.position()) < 1e-9
        except Exception as e:
            log(f"post-flatten verify failed ({e!r})")
            flat = False
        if not flat:
            log("position reappeared after flatten (late fill); "
                "resuming flatten ladder")
            flat, n_tries = await _ladder()
            attempts += n_tries

    if halt_path:
        try:
            open(halt_path, "w").write("hard stop\n")
        except OSError as e:
            log(f"could not write halt marker: {e!r}")
    if not flat:
        log("CRITICAL: NOT FLAT after hard stop sequence - manual intervention required")
    return HardStopResult(flat, attempts, 3 if flat else 4)


# ---------------------------------------------------------------- self-test
def _selftest() -> None:
    import tempfile
    t = [0.0]
    clk = lambda: t[0]

    # OK until the loss estimate crosses soft; the estimate reacts to mid
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.16, 74.0)
    assert g.evaluate(74.0) is GuardAction.OK
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.16, 74.0)
    assert g.evaluate(74.0 - 0.49 / 0.16) is GuardAction.OK
    assert g.evaluate(74.0 - 0.51 / 0.16) is GuardAction.SOFT
    assert g.evaluate(74.0) is GuardAction.SOFT            # SOFT is latched
    assert g.evaluate(74.0 - 0.81 / 0.16) is GuardAction.HARD
    assert g.evaluate(80.0) is GuardAction.HARD            # HARD is sticky

    # short position loses when price rises
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, -0.16, 74.0)
    assert g.evaluate(74.0 + 0.81 / 0.16) is GuardAction.HARD

    # staleness: PULL first, HARD later; None equity does not refresh the clock
    t[0] = 100.0
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.0, 74.0)
    t[0] += 10
    g.on_poll(None, 0.0, 74.0)
    assert g.evaluate(74.0) is GuardAction.OK
    t[0] += 15
    assert g.evaluate(74.0) is GuardAction.PULL
    t[0] += 70
    assert g.evaluate(74.0) is GuardAction.HARD

    # soft stop that fails to get flat within the deadline escalates to hard
    t[0] = 0.0
    g = LossGuard(21.15, now=clk)
    g.on_poll(21.15, 0.16, 74.0)
    assert g.evaluate(74.0 - 0.51 / 0.16, pos_size=0.16) is GuardAction.SOFT
    for _ in range(100):
        t[0] += 15
        g.on_poll(20.60, 0.16, 70.8)
        g.evaluate(70.8, pos_size=0.16)
        if g.sticky is GuardAction.HARD:
            break
    assert g.sticky is GuardAction.HARD

    # on-chain stop trigger prices
    assert sl_trigger_px("long", 75.0, 0.04, lambda p, m: round(p, 2)) == 72.0
    assert sl_trigger_px("short", 75.0, 0.04, lambda p, m: round(p, 2)) == 78.0

    # hard stop sequence with a fake gateway: flat after the second ladder step
    class FakeGW:
        def __init__(self):
            self.pos, self.calls, self.cancels = 0.16, [], 0

        async def cancel_all(self):
            self.cancels += 1
            return 2

        async def flatten(self, slippage):
            self.calls.append(slippage)
            if slippage >= 0.03:
                self.pos = 0.0

        async def position(self):
            return self.pos

    async def fake_sleep(_):
        return None

    async def run():
        d = tempfile.mkdtemp()
        halt = os.path.join(d, "HALT")
        msgs = []
        gw = FakeGW()
        r = await hard_stop_sequence(gw, halt, msgs.append, sleep=fake_sleep)
        assert r.flat and r.exit_code == 3 and gw.calls == [0.01, 0.03]
        assert os.path.exists(halt)

        class StuckGW(FakeGW):
            async def flatten(self, slippage):
                self.calls.append(slippage)       # never fills

        tt = [0.0]

        def tick():
            tt[0] += 7.0
            return tt[0]

        gw2, msgs2 = StuckGW(), []
        r2 = await hard_stop_sequence(gw2, None, msgs2.append,
                                      sleep=fake_sleep, now=tick,
                                      max_total_s=30.0)
        assert not r2.flat and r2.exit_code == 4
        assert any("CRITICAL" in m for m in msgs2)

        class FailCancel(FakeGW):
            async def cancel_all(self):
                raise RuntimeError("boom")

        gw3, msgs3 = FailCancel(), []
        r3 = await hard_stop_sequence(gw3, None, msgs3.append,
                                      sleep=fake_sleep)
        assert r3.flat
        assert sum("cancel_all failed" in m for m in msgs3) == 3

    asyncio.run(run())
    print("loss_guard selftest OK")


if __name__ == "__main__":
    _selftest()
