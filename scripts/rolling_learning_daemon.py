#!/usr/bin/env python3
"""Rolling learning daemon (Testnet + Shadow Paper, no credentials).

This process is the *out-of-band* half of the online learning pipeline:

    public l2 feed  ->  causal features  ->  policy sample (propensity)
        -> paper broker fills  ->  rolling JSONL buffer
        -> delayed 30 s / 300 s rewards  ->  SFT/DPO/RLOO/GRPO/PPO
        -> models/policy_latest.json

It never authenticates, never places an order and never reads a private key.
The only exchange access is the public ``l2Book`` info endpoint (or the
deterministic synthetic feed when the network is unavailable).

Safety precedence is the production one: a HALT marker or a shadow
``LossGuard`` action (SOFT/PULL/HARD) is written into every decision step,
suppresses exploration (propensity 1.0) and blocks trainer updates.

Run::

    PYTHONPATH=src /root/hl-mm-bot/venv/bin/python scripts/rolling_learning_daemon.py \
        --env testnet --source l2 --coin HYPE --poll-s 2 --retention-h 24 \
        --train-every-s 14400 --min-steps 200
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mmbot.learning.buffer import RollingExperienceBuffer  # noqa: E402
from mmbot.learning.features import FEATURE_NAMES  # noqa: E402
from mmbot.learning.loop import RollingLearningLoop  # noqa: E402
from mmbot.learning.policy import SoftmaxLinearPolicy  # noqa: E402
from mmbot.learning.recorder import LearningRecorder  # noqa: E402
from mmbot.learning.trainer import RollingTrainer, TrainerConfig  # noqa: E402
from mmbot.risk.loss_guard import GuardAction, LossGuard, LossGuardConfig  # noqa: E402

URLS = {
    "testnet": "https://api.hyperliquid-testnet.xyz",
    "mainnet": "https://api.hyperliquid.xyz",
}


def log(msg: str) -> None:
    print(f"[rolling-learning] {msg}", flush=True)


# ---------------------------------------------------------------------------
# market data
# ---------------------------------------------------------------------------
@dataclass
class Snapshot:
    ts: float
    coin: str
    bid: float
    ask: float
    bid_sz: float = 1.0
    ask_sz: float = 1.0
    btc_lead_bps: float = 0.0

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return ((self.ask - self.bid) / mid * 1e4) if mid > 0 else 0.0

    @property
    def obi(self) -> float:
        total = self.bid_sz + self.ask_sz
        return ((self.bid_sz - self.ask_sz) / total) if total > 0 else 0.0


@dataclass
class SimpleMicro:
    obi_nw: float = 0.0
    tfi2: float = 0.0
    lead_btc_1s: float = 0.0
    wall_bid_bps: float = 0.0
    wall_ask_bps: float = 0.0


class PublicL2Feed:
    """Public ``l2Book`` polling with a BTC 1 s lead-lag series (no auth)."""

    def __init__(self, env: str, coin: str, timeout: float = 10.0,
                 btc_every_s: float = 1.0):
        self.base_url = URLS[env]
        self.coin = coin
        self.timeout = float(timeout)
        self.btc_every_s = float(btc_every_s)
        self.info = None
        self._btc: Deque[Tuple[float, float]] = deque(maxlen=600)
        self._last_btc_fetch = -1e9

    def _ensure(self):
        if self.info is None:
            from hyperliquid.info import Info
            self.info = Info(self.base_url, skip_ws=True, timeout=self.timeout)

    def _levels(self, raw) -> Tuple[float, float, float, float]:
        bids, asks = raw["levels"]
        return (float(bids[0]["px"]), float(asks[0]["px"]),
                float(bids[0].get("sz", 0) or 0), float(asks[0].get("sz", 0) or 0))

    def _btc_lead(self, now: float) -> float:
        if now - self._last_btc_fetch >= self.btc_every_s:
            self._last_btc_fetch = now
            try:
                b = self._levels(self.info.l2_snapshot("BTC"))
                self._btc.append((now, 0.5 * (b[0] + b[1])))
            except Exception:
                pass
        if len(self._btc) < 2:
            return 0.0
        ref = None
        for ts, mid in reversed(self._btc):
            if ts <= now - 1.0:
                ref = mid
                break
        if ref is None or ref <= 0:
            return 0.0
        return (self._btc[-1][1] - ref) / ref * 1e4

    def snapshot(self, now: float) -> Snapshot:
        self._ensure()
        bid, ask, bsz, asz = self._levels(self.info.l2_snapshot(self.coin))
        return Snapshot(now, self.coin, bid, ask, bsz, asz,
                        self._btc_lead(now))


class SyntheticFeed:
    """Deterministic random-walk fallback (offline / network outage)."""

    def __init__(self, coin: str, start_mid: float = 100.0, seed: int = 7,
                 vol_bps: float = 6.0):
        self.coin = coin
        self.mid = float(start_mid)
        self.btc = 60_000.0
        self.vol_bps = float(vol_bps)
        self.rng = np.random.default_rng(seed)
        self._btc_hist: Deque[Tuple[float, float]] = deque(maxlen=600)

    def snapshot(self, now: float) -> Snapshot:
        self.mid *= math.exp(float(self.rng.normal(0, self.vol_bps / 1e4)))
        self.btc *= math.exp(float(self.rng.normal(0, self.vol_bps * 0.7 / 1e4)))
        half = max(1e-4, self.mid * 2.0 / 1e4)
        bsz = float(max(0.01, abs(self.rng.normal(2.0, 0.5))))
        asz = float(max(0.01, abs(self.rng.normal(2.0, 0.5))))
        self._btc_hist.append((now, self.btc))
        ref = None
        for ts, mid in reversed(self._btc_hist):
            if ts <= now - 1.0:
                ref = mid
                break
        lead = (self.btc - ref) / ref * 1e4 if ref and ref > 0 else 0.0
        return Snapshot(now, self.coin, self.mid - half, self.mid + half,
                        bsz, asz, lead)


class ResilientFeed:
    """Try the public feed; fall back to synthetic after repeated failures."""

    def __init__(self, primary, fallback, max_failures: int = 5):
        self.primary = primary
        self.fallback = fallback
        self.max_failures = int(max_failures)
        self.failures = 0
        self.switched = False
        self.last: Optional[Snapshot] = None

    def snapshot(self, now: float) -> Snapshot:
        if not self.switched:
            try:
                snap = self.primary.snapshot(now)
                self.failures = 0
                self.last = snap
                return snap
            except Exception as exc:
                self.failures += 1
                if self.failures == 1:
                    log(f"public feed error ({type(exc).__name__}: {exc}); retrying")
                if self.failures >= self.max_failures:
                    self.switched = True
                    log("public feed unavailable: switching to synthetic feed")
        snap = self.fallback.snapshot(now)
        self.last = snap
        return snap


# ---------------------------------------------------------------------------
# paper execution
# ---------------------------------------------------------------------------
@dataclass
class PaperOrder:
    side: str                 # "buy" | "sell"
    px: float
    sz: float


class PaperBroker:
    """Minimal inventory-aware paper book; fills when the mid crosses a quote.

    A quote placed at/inside the best bid/ask is also exposed to a small
    per-poll probability of being hit by incoming flow.  Without that, a dead
    testnet book (unchanged mid for minutes) would never produce an episode.
    """

    def __init__(self, start_equity: float, max_notional: float,
                 fee_bps: float = 1.5, edge_bps: float = 1.0,
                 fill_prob: float = 0.02, seed: int = 7):
        self.cash = float(start_equity)
        self.start_equity = float(start_equity)
        self.position = 0.0
        self.avg_px = 0.0
        self.fees = 0.0
        self.fills = 0
        self.max_notional = float(max_notional)
        self.fee_bps = float(fee_bps)
        self.edge_bps = float(edge_bps)
        self.fill_prob = max(0.0, min(1.0, float(fill_prob)))
        self.rng = np.random.default_rng(seed)
        self.orders: List[PaperOrder] = []

    # ---- marks -----------------------------------------------------------
    def equity(self, mid: float) -> float:
        return self.cash + self.position * float(mid)

    # ---- quoting ---------------------------------------------------------
    def cancel_all(self) -> None:
        self.orders.clear()

    def set_quotes(self, action: str, snap: Snapshot) -> None:
        self.cancel_all()
        mid = snap.mid
        if mid <= 0 or action in ("pull", "risk_hold"):
            return
        size = max(0.0, self.max_notional / mid)
        if size <= 0:
            return
        buy_px = mid * (1.0 - self.edge_bps / 1e4)
        sell_px = mid * (1.0 + self.edge_bps / 1e4)
        want_buy = action in ("quote_both", "quote_bid")
        want_sell = action in ("quote_both", "quote_ask")
        if action == "reduce_only":
            want_buy = self.position < -1e-12
            want_sell = self.position > 1e-12
        if want_buy and self.position * mid + self.max_notional > self.max_notional:
            want_buy = False
        if want_sell and self.position * mid - self.max_notional < -self.max_notional:
            want_sell = False
        if want_buy:
            self.orders.append(PaperOrder("buy", buy_px, size))
        if want_sell:
            self.orders.append(PaperOrder("sell", sell_px, size))

    # ---- fills -----------------------------------------------------------
    def apply_fills(self, snap: Snapshot, now: float, recorder) -> int:
        n = 0
        for order in list(self.orders):
            crossed = (order.side == "buy" and snap.mid <= order.px) or \
                      (order.side == "sell" and snap.mid >= order.px)
            inside = (order.side == "buy" and order.px >= snap.bid) or \
                     (order.side == "sell" and order.px <= snap.ask)
            flow = inside and float(self.rng.random()) < self.fill_prob
            if not (crossed or flow):
                continue
            self.orders.remove(order)
            notional = order.px * order.sz
            fee = notional * self.fee_bps / 1e4
            if order.side == "buy":
                self.cash -= notional + fee
                self.position += order.sz
            else:
                self.cash += notional - fee
                self.position -= order.sz
            self.fees += fee
            self.fills += 1
            if abs(self.position) < 1e-12:
                self.position = 0.0
            try:
                recorder.on_fill(now, order.side, order.px, order.sz, fee)
            except Exception as exc:
                log(f"paper fill recording failed: {exc!r}")
            n += 1
        return n


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env", choices=sorted(URLS), default="testnet")
    p.add_argument("--source", choices=("l2", "synthetic"), default="l2")
    p.add_argument("--coin", default="HYPE")
    p.add_argument("--poll-s", type=float, default=2.0)
    p.add_argument("--retention-h", type=float, default=24.0)
    p.add_argument("--max-retention-h", type=float, default=168.0)
    p.add_argument("--train-every-s", type=float, default=4 * 3600.0)
    p.add_argument("--min-steps", type=int, default=200)
    p.add_argument("--min-new-steps", type=int, default=100)
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run forever")
    p.add_argument("--start-equity", type=float, default=21.0)
    p.add_argument("--max-notional", type=float, default=11.0)
    p.add_argument("--fee-bps", type=float, default=1.5)
    p.add_argument("--edge-bps", type=float, default=1.0)
    p.add_argument("--fill-prob", type=float, default=0.02,
                   help="per-poll fill probability for a quote inside the book")
    p.add_argument("--soft-loss-usd", type=float, default=2.0)
    p.add_argument("--hard-loss-usd", type=float, default=5.0)
    p.add_argument("--kl-max", type=float, default=0.15)
    p.add_argument("--lr", type=float, default=0.01)
    p.add_argument("--value-lr", type=float, default=0.01)
    p.add_argument("--drift-z", type=float, default=3.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--log-every-s", type=float, default=60.0)
    p.add_argument("--buffer", default=str(ROOT / "data" / "rolling_buffer.jsonl"))
    p.add_argument("--policy", default=str(ROOT / "models" / "policy_latest.json"))
    p.add_argument("--ref-policy", default=str(ROOT / "models" / "policy_ref.json"))
    p.add_argument("--train-log", default=str(ROOT / "logs" / "learning_train.jsonl"))
    p.add_argument("--guard-state", default=str(ROOT / "data" / "learning_guard_state.json"))
    p.add_argument("--heartbeat", default=str(ROOT / "data" / "rolling_loop_state.json"))
    p.add_argument("--halt-file", default=str(ROOT / "logs" / "HALT"))
    return p


def build(args):
    buffer = RollingExperienceBuffer(
        args.buffer,
        retention_s=args.retention_h * 3600.0,
        max_retention_s=min(args.max_retention_h, 168.0) * 3600.0,
        now_fn=time.time)
    policy_path = Path(args.policy)
    policy = SoftmaxLinearPolicy.load_or_new(policy_path)
    if len(policy.feature_names) != len(FEATURE_NAMES):
        log("policy feature schema changed: starting a fresh policy")
        policy = SoftmaxLinearPolicy(n_features=len(FEATURE_NAMES))
    if not policy_path.exists():
        policy.save(policy_path)
        log(f"initial checkpoint written: {policy_path}")

    recorder = LearningRecorder(
        buffer, policy, coin=args.coin, max_notional=args.max_notional,
        guard_state_path=args.guard_state, policy_path=policy_path,
        protection_cap_usd=float(os.environ.get("MM_REWARD_CAP_USD", "0.10")),
        seed=args.seed, now_fn=time.time)

    cfg = TrainerConfig(
        window_s=args.retention_h * 3600.0,
        trigger_every_s=args.train_every_s,
        min_steps=args.min_steps,
        min_new_steps=args.min_new_steps,
        kl_max=args.kl_max,
        drift_z_max=args.drift_z,
        lr=args.lr,
        value_lr=args.value_lr,
        checkpoint_path=str(policy_path),
        ref_path=args.ref_policy,
        log_path=args.train_log,
        halt_path=args.halt_file,
        seed=args.seed)
    trainer = RollingTrainer(buffer, policy, cfg, now_fn=time.time)

    guard = LossGuard(
        args.start_equity,
        LossGuardConfig(soft_loss_usd=args.soft_loss_usd,
                        hard_loss_usd=args.hard_loss_usd,
                        halt_file=args.halt_file),
        now=time.time, halt_path=args.halt_file)
    guard_state = {"value": "ok"}

    loop = RollingLearningLoop(
        buffer, trainer, recorder=recorder, interval_s=args.poll_s,
        now_fn=time.time,
        guard_fn=lambda: guard_state["value"],
        guard_state_path=args.guard_state,
        heartbeat_path=args.heartbeat,
        log_fn=log)
    return buffer, policy, recorder, trainer, guard, guard_state, loop


async def run(args) -> int:
    buffer, policy, recorder, trainer, guard, guard_state, loop = build(args)
    if args.source == "synthetic":
        feed = SyntheticFeed(args.coin, start_mid=100.0, seed=args.seed)
    else:
        feed = ResilientFeed(
            PublicL2Feed(args.env, args.coin),
            SyntheticFeed(args.coin, start_mid=100.0, seed=args.seed))
    broker = PaperBroker(args.start_equity, args.max_notional,
                         fee_bps=args.fee_bps, edge_bps=args.edge_bps,
                         fill_prob=args.fill_prob, seed=args.seed)
    stop = asyncio.Event()

    def _stop(_signum=None, _frame=None):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    log(f"starting env={args.env} source={args.source} coin={args.coin} "
        f"poll={args.poll_s}s retention={args.retention_h}h "
        f"train_every={args.train_every_s}s min_steps={args.min_steps} "
        f"buffer={args.buffer} policy={args.policy}")

    started = time.time()
    last_log = -1e9
    last_snap: Optional[Snapshot] = None
    exit_code = 0
    while not stop.is_set():
        now = time.time()
        snap = feed.snapshot(now)
        last_snap = snap
        # 1) mark to market (settles 30 s / 300 s rewards on the next loop pass)
        recorder.on_mark(now, snap.mid)
        # 2) paper fills against the new book
        broker.apply_fills(snap, now, recorder)
        # 3) LossGuard precedence: real logic, shadow equity
        equity = broker.equity(snap.mid)
        guard.on_poll(equity, broker.position, snap.mid)
        action = guard.evaluate(snap.mid, broker.position)
        guard_status = action.value
        guard_state["value"] = guard_status
        if action is GuardAction.HARD:
            broker.cancel_all()
            log(f"HARD shadow guard: equity={equity:.4f} start={args.start_equity:.4f} "
                "-> stop quoting, block training, exit")
            loop.run_once(now, force_heartbeat=True)
            exit_code = 3
            break
        # 4) one decision step (policy sample + propensity, no exploration in guard)
        micro = SimpleMicro(obi_nw=snap.obi, lead_btc_1s=snap.btc_lead_bps)
        step = recorder.observe(
            now, snap.mid, micro=micro, position=broker.position,
            equity=equity, guard_status=guard_status,
            spread_bps=snap.spread_bps)
        # 5) replace paper quotes with the chosen action
        broker.set_quotes(step.action, snap)
        # 6) settle / trim / maybe train / heartbeat
        loop.run_once(now)

        if now - last_log >= args.log_every_s:
            last_log = now
            res = loop.last_result
            log(f"steps={len(buffer)} ready={len(buffer.ready())} "
                f"pos={broker.position:+.4f} equity={equity:.4f} "
                f"fills={broker.fills} guard={guard_status} "
                f"policy_v={policy.version} "
                f"train={'-' if res is None else (res.reason or 'ok')} "
                f"uptime={now - started:.0f}s")

        if args.duration_s and now - started >= args.duration_s:
            log(f"duration reached ({args.duration_s:.0f}s): stopping")
            break
        try:
            await asyncio.sleep(max(0.05, args.poll_s))
        except asyncio.CancelledError:
            break

    # final settle/train attempt + heartbeat
    try:
        loop.run_once(time.time(), force_heartbeat=True)
    except Exception:
        pass
    log(f"stopped after {time.time() - started:.0f}s: steps={len(buffer)} "
        f"ready={len(buffer.ready())} fills={broker.fills} "
        f"equity={broker.equity(last_snap.mid if last_snap else 0.0):.4f} "
        f"policy_v={policy.version} exit={exit_code}")
    return exit_code


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.retention_h <= 0:
        log("retention must be positive")
        return 2
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
