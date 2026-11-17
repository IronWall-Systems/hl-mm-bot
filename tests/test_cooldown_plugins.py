"""Tests for the MM cooldown plugin system.

Runs with the standard library::

    venv/bin/python -m unittest discover -s tests -v

(or under pytest, since the cases are plain ``unittest.TestCase`` classes).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from strategies.market_maker.plugins.base import BaseCooldownPlugin, TriggerAction
from strategies.market_maker.plugins.cooldown_12h import Cooldown12h
from strategies.market_maker.plugins.cooldown_24h_4blocks import Cooldown24h4Blocks
from strategies.market_maker.plugins.cooldown_6h import Cooldown6h
from strategies.market_maker.plugins.loader import (
    PluginLoadError,
    load_plugin,
)
from strategies.market_maker.plugins.rolling import (
    Rolling1h,
    Rolling3h,
    Rolling6h,
    Rolling12h,
    ROLLING_WINDOW_S,
)

HOUR = 3600.0


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class PluginTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock()
        self.env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.env_backup)
        self._tmp.cleanup()

    def make(self, cls=Cooldown6h, name="test_plugin", **kwargs):
        return cls(
            state_path=self.tmp / f"{name}.json",
            clock=self.clock,
            **kwargs,
        )


class ThresholdTests(PluginTestBase):
    def test_exactly_threshold_arms(self):
        p = self.make()
        p.observe_equity(30.0)
        p.observe_equity(29.75)  # loss == $0.25
        d = p.evaluate()
        self.assertFalse(d.allowed)
        self.assertIn("cooldown", d.reason)
        self.assertAlmostEqual(d.cumulative_loss_usd, 0.25, places=9)

    def test_below_threshold_allows(self):
        p = self.make()
        p.observe_equity(30.0)
        p.observe_equity(29.76)  # loss == $0.24
        self.assertTrue(p.evaluate().allowed)

    def test_non_positive_equity_is_ignored(self):
        p = self.make()
        p.observe_equity(30.0)
        p.observe_equity(0.0)
        self.assertTrue(p.evaluate().allowed)

    def test_realized_pnl_input(self):
        p = self.make()
        p.note_realized_pnl(-0.20)
        self.assertTrue(p.evaluate().allowed)
        p.note_realized_pnl(-0.06)
        self.assertFalse(p.evaluate().allowed)


class ExpiryTests(PluginTestBase):
    def test_6h_expiry(self):
        p = self.make(Cooldown6h)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        self.assertFalse(p.evaluate().allowed)
        self.clock.advance(6 * HOUR - 1)
        self.assertFalse(p.evaluate().allowed)
        self.clock.advance(1)
        self.assertTrue(p.evaluate().allowed)

    def test_12h_expiry(self):
        p = self.make(Cooldown12h)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        self.assertFalse(p.evaluate().allowed)
        self.clock.advance(12 * HOUR - 1)
        self.assertFalse(p.evaluate().allowed)
        self.clock.advance(1)
        self.assertTrue(p.evaluate().allowed)

    def test_flat_loss_does_not_rearm_after_expiry(self):
        p = self.make(Cooldown6h)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        p.evaluate()
        self.clock.advance(6 * HOUR)
        self.assertTrue(p.evaluate().allowed)
        self.clock.advance(HOUR)
        self.assertTrue(p.evaluate().allowed)
        self.assertEqual(p.state.armed_count, 1)

    def test_recovery_then_new_loss_rearms(self):
        p = self.make(Cooldown6h)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        p.evaluate()
        self.clock.advance(6 * HOUR)
        self.assertTrue(p.evaluate().allowed)
        p.observe_equity(30.0)  # recovered
        p.observe_equity(29.70)  # new episode
        self.assertFalse(p.evaluate().allowed)
        self.assertEqual(p.state.armed_count, 2)


class RollingWindowTests(PluginTestBase):
    def test_rolling_60m_hwm_tracks_equity(self):
        """Rolling 60m window tracks HWM correctly."""
        p = self.make(Rolling6h, loss_threshold_usd=0.25)
        # Start at 30.0
        p.observe_equity(30.0)
        self.assertEqual(p.rolling_loss_usd(), 0.0)
        # Equity rises to 31.0 - new HWM
        p.observe_equity(31.0)
        self.assertEqual(p.rolling_loss_usd(), 0.0)
        # Equity drops to 30.8 - loss from HWM is 0.2
        p.observe_equity(30.8)
        self.assertAlmostEqual(p.rolling_loss_usd(), 0.2, places=9)
        # Equity drops to 30.5 - loss from HWM is 0.5 > threshold
        p.observe_equity(30.5)
        self.assertAlmostEqual(p.rolling_loss_usd(), 0.5, places=9)
        d = p.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.action, TriggerAction.FLATTEN.value)
        self.assertEqual(d.loss_window_s, ROLLING_WINDOW_S)

    def test_rolling_window_expires_old_hwm(self):
        """Old equity points outside 60m window should be pruned."""
        p = self.make(Rolling6h, loss_threshold_usd=0.25)
        p.observe_equity(30.0)
        p.observe_equity(31.0)  # HWM = 31.0
        # Advance 61 minutes - the 31.0 HWM should fall out of window
        self.clock.advance(61 * 60)
        p.observe_equity(30.0)
        # Now the window only has 30.0, so loss should be 0
        self.assertEqual(p.rolling_loss_usd(), 0.0)

    def test_rolling_recovery_resets_loss_at_last_trigger(self):
        """Loss dropping below threshold should reset loss_at_last_trigger (but cooldown stays active until timer expires)."""
        p = self.make(Rolling6h, loss_threshold_usd=0.25)
        p.observe_equity(30.0)
        p.observe_equity(29.5)  # loss = 0.5 >= 0.25 -> arms
        p.evaluate()
        self.assertTrue(p.state.loss_at_last_trigger > 0)
        # Now equity recovers to 30.2
        p.observe_equity(30.2)
        # Loss from HWM (now 30.2) is now 0.0 (below threshold)
        self.assertEqual(p.rolling_loss_usd(), 0.0)
        # Cooldown still active - should return blocked
        d = p.evaluate()
        self.assertFalse(d.allowed)
        # But loss_at_last_trigger should be reset to 0 for next episode
        self.assertEqual(p.state.loss_at_last_trigger, 0.0)
        # After cooldown expires, should allow
        self.clock.advance(6 * 3600 + 1)
        self.assertTrue(p.evaluate().allowed)

    def test_rolling_trigger_once_per_episode(self):
        """Should not re-arm immediately after window expires if loss persists."""
        p = self.make(Rolling1h, loss_threshold_usd=0.25)
        p.observe_equity(30.0)
        p.observe_equity(29.5)  # loss = 0.5
        d = p.evaluate()
        self.assertFalse(d.allowed)
        # Advance just past the 1h cooldown
        self.clock.advance(1 * 3600 + 1)
        # Equity still at 29.5 - loss from HWM (30.0) is still 0.5
        p.observe_equity(29.5)
        # Should NOT re-arm because loss_at_last_trigger is still 0.5
        d = p.evaluate()
        self.assertTrue(d.allowed)


class RollingActionTests(PluginTestBase):
    def test_flatten_action_default(self):
        p = self.make(Rolling6h)
        p.observe_equity(30.0)
        p.observe_equity(29.5)
        d = p.evaluate()
        self.assertEqual(d.action, TriggerAction.FLATTEN.value)

    def test_reduce_only_action_via_constructor(self):
        p = self.make(Rolling6h, action="reduce_only")
        p.observe_equity(30.0)
        p.observe_equity(29.5)
        d = p.evaluate()
        self.assertEqual(d.action, TriggerAction.REDUCE_ONLY.value)

    def test_cancel_all_action_legacy(self):
        p = self.make(Cooldown6h, action="cancel_all")
        p.observe_equity(30.0)
        p.observe_equity(29.5)
        d = p.evaluate()
        self.assertEqual(d.action, TriggerAction.CANCEL_ALL.value)


class MultiBlockTests(PluginTestBase):
    def test_24h_is_four_six_hour_blocks(self):
        p = self.make(Cooldown24h4Blocks)
        self.assertEqual(p.blocks, 4)
        self.assertEqual(p.block_hours, 6.0)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        d = p.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.block_index, 0)
        self.assertEqual(d.blocks_total, 4)

        for completed in (1, 2, 3):
            self.clock.advance(6 * HOUR)
            d = p.evaluate()
            self.assertFalse(d.allowed)
            self.assertEqual(d.block_index, completed, f"after {completed} blocks")
            self.assertEqual(d.blocks_total, 4)

        self.clock.advance(6 * HOUR)  # 24h total
        d = p.evaluate()
        self.assertTrue(d.allowed)
        self.assertEqual(d.block_index, 4)

    def test_block_progress_survives_restart(self):
        p = self.make(Cooldown24h4Blocks)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        p.evaluate()
        self.clock.advance(13 * HOUR)

        restarted = self.make(Cooldown24h4Blocks)
        d = restarted.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.block_index, 2)  # 12h -> 2 completed, in the 3rd block
        self.assertGreater(d.remaining_s, 10 * HOUR)
        self.assertLess(d.remaining_s, 12 * HOUR)


class PersistenceTests(PluginTestBase):
    def test_cooldown_and_baseline_survive_restart(self):
        p = self.make(Cooldown6h)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        p.evaluate()
        path = p.store.path
        self.assertTrue(path.exists())
        raw = json.loads(path.read_text())
        self.assertEqual(raw["plugin"], "cooldown_6h")
        self.assertGreater(raw["cooldown_until"], 0)
        self.assertAlmostEqual(raw["session_start_equity"], 30.0)

        self.clock.advance(3 * HOUR)
        restarted = self.make(Cooldown6h)
        d = restarted.evaluate()
        self.assertFalse(d.allowed, "restart must not bypass the timer")
        self.assertAlmostEqual(d.remaining_s, 3 * HOUR, places=3)

        self.clock.advance(3 * HOUR)
        self.assertTrue(restarted.evaluate().allowed)

    def test_state_is_valid_json_and_atomic_temp_removed(self):
        p = self.make(Cooldown6h)
        p.observe_equity(30.0)
        p.evaluate()
        leftovers = list(p.store.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertIn("cooldown_until", json.loads(p.store.path.read_text()))

    def test_corrupt_state_starts_fresh(self):
        path = self.tmp / "test_plugin.json"
        path.write_text("{not valid json")
        p = Cooldown6h(state_path=path, clock=self.clock)
        self.assertTrue(p.evaluate().allowed)

    def test_reset_clears_window(self):
        p = self.make(Cooldown6h)
        p.observe_equity(30.0)
        p.observe_equity(29.70)
        self.assertFalse(p.evaluate().allowed)
        p.reset()
        self.assertTrue(p.evaluate().allowed)


class LoaderTests(PluginTestBase):
    def setUp(self):
        super().setUp()
        os.environ["MM_COOLDOWN_STATE_DIR"] = str(self.tmp / "state")

    def test_builtins_load_by_name(self):
        for name, cls, hours in (
            ("cooldown_6h", Cooldown6h, 6.0),
            ("cooldown_12h", Cooldown12h, 12.0),
            ("cooldown_24h_4blocks", Cooldown24h4Blocks, 24.0),
        ):
            os.environ["MM_COOLDOWN_PLUGIN"] = name
            plugin = load_plugin()
            self.assertIsInstance(plugin, cls)
            self.assertEqual(plugin.cooldown_hours, hours)
            self.assertEqual(plugin.loss_threshold_usd, 0.25)

    def test_env_overrides(self):
        os.environ["MM_COOLDOWN_PLUGIN"] = "cooldown_12h"
        os.environ["MM_COOLDOWN_LOSS_USD"] = "1.50"
        os.environ["MM_COOLDOWN_HOURS"] = "3"
        plugin = load_plugin()
        self.assertEqual(plugin.loss_threshold_usd, 1.5)
        self.assertEqual(plugin.cooldown_hours, 3.0)

    def test_unset_env_disables(self):
        os.environ.pop("MM_COOLDOWN_PLUGIN", None)
        self.assertIsNone(load_plugin())
        for value in ("none", "off", "disabled", "0"):
            self.assertIsNone(load_plugin(value))

    def test_unknown_plugin_raises_clear_error(self):
        with self.assertRaises(PluginLoadError) as ctx:
            load_plugin("does_not_exist")
        self.assertIn("cannot load cooldown plugin", str(ctx.exception))
        self.assertIn("cooldown_6h", str(ctx.exception))


class EngineIntegrationTests(PluginTestBase):
    def setUp(self):
        super().setUp()
        from strategies.market_maker.plugins.integration import uninstall

        uninstall()
        self.plugin = self.make(Cooldown6h)
        from strategies.market_maker.plugins.integration import install_plugin

        install_plugin(self.plugin)

    def tearDown(self):
        from strategies.market_maker.plugins.integration import uninstall

        uninstall()
        super().tearDown()

    def _guard(self):
        from mmbot.models import RiskConfig
        from mmbot.risk.guard import RiskGuard

        return RiskGuard(RiskConfig(), 30.0, lambda: 0.0)

    def _quote(self):
        from mmbot.models import Quote, Side

        return Quote(Side.BUY, 100.0, 0.01)

    def _pretrade(self, guard):
        from mmbot.models import PositionState

        return guard.pre_trade(self._quote(), PositionState(account_value=30.0), 100.0, 0.0, 0.0)

    def test_pretrade_allows_then_blocks_after_loss(self):
        guard = self._guard()
        self.assertTrue(self._pretrade(guard).ok)
        # Equity poll feeds the plugin through the patched RiskGuard.update_equity.
        guard.update_equity(29.70)
        verdict = self._pretrade(guard)
        self.assertFalse(verdict.ok)
        self.assertIn("cooldown", verdict.reason)

    def test_pretrade_unblocks_after_window(self):
        guard = self._guard()
        guard.update_equity(29.70)
        self.assertFalse(self._pretrade(guard).ok)
        self.clock.advance(6 * HOUR)
        self.assertTrue(self._pretrade(guard).ok)

    def test_plan_withdraws_resting_quotes_while_blocked(self):
        from mmbot.execution.order_manager import OrderManager
        from mmbot.models import OrderStatus, StrategyParams

        om = OrderManager(StrategyParams(coin="HYPE"))
        order = om.register_pending(self._quote(), 0.0)
        order.status = OrderStatus.RESTING
        self.plugin.observe_equity(30.0)
        self.plugin.observe_equity(29.70)

        plan = om.plan([self._quote()], 1.0)
        self.assertEqual(plan.places, [])
        self.assertEqual([o.cloid for o in plan.cancels], [order.cloid])

    def test_plan_resumes_after_window(self):
        from mmbot.execution.order_manager import OrderManager
        from mmbot.models import StrategyParams

        self.plugin.observe_equity(30.0)
        self.plugin.observe_equity(29.70)
        self.clock.advance(6 * HOUR)
        om = OrderManager(StrategyParams(coin="HYPE"))
        plan = om.plan([self._quote()], 1.0)
        self.assertEqual(len(plan.places), 1)

    def test_install_is_idempotent(self):
        from strategies.market_maker.plugins.integration import install_plugin

        self.assertIs(install_plugin(self.plugin), self.plugin)


if __name__ == "__main__":
    unittest.main(verbosity=2)
