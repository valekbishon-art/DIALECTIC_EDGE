from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.advisor import (
    ACTION_BUY,
    ACTION_SELL,
    ACTION_WAIT,
    BTC_VETO_CONFIDENCE_MIN,
    HORIZON_MEDIUM,
    HORIZON_SHORT,
    RISK_AGGRESSIVE,
    RISK_CONSERVATIVE,
    RISK_MODERATE,
    AdvisorInputs,
    feature_enabled,
    format_advisor_markdown,
    recommend,
)


class TestAdvisorFeatureFlag(unittest.TestCase):
    def test_default_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(feature_enabled())

    def test_can_be_disabled(self):
        with patch.dict(os.environ, {"FEATURE_ADVISOR": "0"}, clear=True):
            self.assertFalse(feature_enabled())


class TestAdvisorWaitCases(unittest.TestCase):
    def test_empty_inputs_returns_wait(self):
        plan = recommend(AdvisorInputs())
        self.assertEqual(plan.action, ACTION_WAIT)
        self.assertEqual(plan.confidence_pct, 0)

    def test_no_quant_no_trend_returns_wait(self):
        plan = recommend(
            AdvisorInputs(asset="ETH", entry_price=3500.0, atr_14d_usd=120.0)
        )
        self.assertEqual(plan.action, ACTION_WAIT)

    def test_neutral_quant_returns_wait(self):
        plan = recommend(
            AdvisorInputs(
                asset="ETH",
                entry_price=3500.0,
                atr_14d_usd=120.0,
                quant_verdict="NEUTRAL",
                quant_confidence=0.5,
                trend="SIDEWAYS",
            )
        )
        self.assertEqual(plan.action, ACTION_WAIT)


class TestAdvisorBuyPath(unittest.TestCase):
    def _bullish_inputs(self, **over):
        defaults = dict(
            asset="BTC",
            entry_price=68000.0,
            atr_14d_usd=1500.0,
            atr_14d_pct=2.2,
            rsi_14d=58.0,
            trend="UPTREND",
            quant_verdict="LONG",
            quant_confidence=0.7,
            btc_lean="BULL",
            btc_confidence_pct=75,
            risk_profile=RISK_MODERATE,
            time_horizon=HORIZON_MEDIUM,
            capital_usd=10000.0,
        )
        defaults.update(over)
        return AdvisorInputs(**defaults)

    def test_full_bull_setup_yields_buy(self):
        plan = recommend(self._bullish_inputs())
        self.assertEqual(plan.action, ACTION_BUY)
        self.assertGreater(plan.confidence_pct, 50)

    def test_buy_stop_below_entry(self):
        plan = recommend(self._bullish_inputs())
        self.assertIsNotNone(plan.stop_price)
        self.assertIsNotNone(plan.entry_price)
        self.assertLess(plan.stop_price, plan.entry_price)

    def test_buy_tps_above_entry_ordered(self):
        plan = recommend(self._bullish_inputs())
        tps = plan.tp_levels
        self.assertEqual(len(tps), 3)
        self.assertLess(plan.entry_price, tps[0].price)
        self.assertLess(tps[0].price, tps[1].price)
        self.assertLess(tps[1].price, tps[2].price)
        # Close-pct should sum to 100.
        self.assertEqual(sum(tp.close_pct for tp in tps), 100)

    def test_buy_position_size_respects_capital(self):
        plan = recommend(self._bullish_inputs(capital_usd=10000.0))
        self.assertIsNotNone(plan.position_usd)
        # Cap at 25% of capital.
        self.assertLessEqual(plan.position_usd, 10000.0 * 0.25 + 0.01)
        self.assertIsNotNone(plan.position_pct_of_capital)
        self.assertGreater(plan.position_pct_of_capital, 0)

    def test_no_capital_no_position_size(self):
        plan = recommend(self._bullish_inputs(capital_usd=None))
        self.assertIsNone(plan.position_usd)


class TestAdvisorSellPath(unittest.TestCase):
    def test_full_bear_setup_yields_sell(self):
        plan = recommend(
            AdvisorInputs(
                asset="BTC",
                entry_price=68000.0,
                atr_14d_usd=1500.0,
                trend="DOWNTREND",
                quant_verdict="SHORT",
                quant_confidence=0.8,
                btc_lean="BEAR",
                btc_confidence_pct=78,
                risk_profile=RISK_MODERATE,
                capital_usd=5000.0,
            )
        )
        self.assertEqual(plan.action, ACTION_SELL)
        self.assertIsNotNone(plan.stop_price)
        # SELL stop is ABOVE entry.
        self.assertGreater(plan.stop_price, plan.entry_price)
        # TPs below entry.
        for tp in plan.tp_levels:
            self.assertLess(tp.price, plan.entry_price)


class TestAdvisorBTCVeto(unittest.TestCase):
    """BTC outlook acts as veto for alt trades when strong & contradictory."""

    def test_alt_long_with_strong_btc_bear_gets_vetoed(self):
        plan = recommend(
            AdvisorInputs(
                asset="ETH",
                entry_price=3500.0,
                atr_14d_usd=120.0,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.8,
                btc_lean="BEAR",
                btc_confidence_pct=80,
                risk_profile=RISK_MODERATE,
                capital_usd=5000.0,
            )
        )
        self.assertEqual(plan.action, ACTION_WAIT)
        self.assertIn("BTC", plan.btc_overlay_note)
        self.assertIn("вето", plan.btc_overlay_note.lower())

    def test_alt_long_with_weak_btc_bear_not_vetoed(self):
        # BTC bear but below veto threshold → no veto, just dampens confidence.
        plan = recommend(
            AdvisorInputs(
                asset="ETH",
                entry_price=3500.0,
                atr_14d_usd=120.0,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.8,
                btc_lean="BEAR",
                btc_confidence_pct=BTC_VETO_CONFIDENCE_MIN - 5,
                risk_profile=RISK_MODERATE,
                capital_usd=5000.0,
            )
        )
        # Action stays BUY but with reduced confidence.
        self.assertEqual(plan.action, ACTION_BUY)

    def test_btc_itself_not_subject_to_veto(self):
        # If asset IS BTC, no BTC overlay applied.
        plan = recommend(
            AdvisorInputs(
                asset="BTC",
                entry_price=68000.0,
                atr_14d_usd=1500.0,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.8,
                btc_lean="BEAR",  # contradictory but we don't double-count
                btc_confidence_pct=90,
                capital_usd=5000.0,
            )
        )
        self.assertEqual(plan.action, ACTION_BUY)
        # BTC overlay note should NOT be set when asset is BTC.
        self.assertEqual(plan.btc_overlay_note, "")

    def test_alt_long_with_btc_bull_gets_boost_note(self):
        plan = recommend(
            AdvisorInputs(
                asset="SOL",
                entry_price=180.0,
                atr_14d_usd=8.0,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.7,
                btc_lean="BULL",
                btc_confidence_pct=75,
                capital_usd=2000.0,
            )
        )
        self.assertEqual(plan.action, ACTION_BUY)
        self.assertIn("совпадает", plan.btc_overlay_note)


class TestAdvisorProfileEffect(unittest.TestCase):
    """Conservative/aggressive profiles should produce different sizing/stops."""

    def _inputs(self, profile: str):
        return AdvisorInputs(
            asset="BTC",
            entry_price=68000.0,
            atr_14d_usd=1500.0,
            trend="UPTREND",
            quant_verdict="LONG",
            quant_confidence=0.7,
            btc_lean="BULL",
            btc_confidence_pct=75,
            risk_profile=profile,
            capital_usd=10000.0,
        )

    def test_conservative_has_wider_stop_than_aggressive(self):
        cons = recommend(self._inputs(RISK_CONSERVATIVE))
        agg = recommend(self._inputs(RISK_AGGRESSIVE))
        self.assertGreater(cons.stop_distance_pct, agg.stop_distance_pct)

    def test_aggressive_position_size_higher_than_conservative(self):
        cons = recommend(self._inputs(RISK_CONSERVATIVE))
        agg = recommend(self._inputs(RISK_AGGRESSIVE))
        # Aggressive uses 2% risk vs conservative 0.5% — bigger position.
        self.assertGreater(agg.position_usd, cons.position_usd)


class TestAdvisorMissingData(unittest.TestCase):
    def test_no_entry_returns_wait(self):
        plan = recommend(
            AdvisorInputs(
                asset="BTC",
                entry_price=None,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.8,
            )
        )
        self.assertEqual(plan.action, ACTION_WAIT)
        self.assertIn("цены", plan.invalidation.lower())

    def test_no_atr_falls_back_to_2pct_stop(self):
        plan = recommend(
            AdvisorInputs(
                asset="BTC",
                entry_price=68000.0,
                atr_14d_usd=None,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.7,
                btc_lean="BULL",
                btc_confidence_pct=70,
                capital_usd=5000.0,
            )
        )
        self.assertEqual(plan.action, ACTION_BUY)
        # Fallback: 2% stop distance.
        self.assertAlmostEqual(plan.stop_distance_pct, 2.0, places=1)


class TestAdvisorFormat(unittest.TestCase):
    def test_format_buy_plan_contains_key_fields(self):
        plan = recommend(
            AdvisorInputs(
                asset="BTC",
                entry_price=68000.0,
                atr_14d_usd=1500.0,
                atr_14d_pct=2.2,
                rsi_14d=58.0,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.7,
                btc_lean="BULL",
                btc_confidence_pct=75,
                risk_profile=RISK_MODERATE,
                time_horizon=HORIZON_MEDIUM,
                capital_usd=10000.0,
            )
        )
        md = format_advisor_markdown(plan)
        self.assertIn("BTC", md)
        self.assertIn(ACTION_BUY, md)
        self.assertIn("Вход", md)
        self.assertIn("Стоп", md)
        self.assertIn("Тейк", md)
        self.assertIn("Инвалидация", md)
        self.assertIn("Почему", md)

    def test_format_wait_plan_no_levels(self):
        plan = recommend(AdvisorInputs(asset="ETH"))
        md = format_advisor_markdown(plan)
        self.assertIn("WAIT", md)
        # No entry/stop/tp lines.
        self.assertNotIn("Вход:", md)

    def test_format_short_horizon_human_text(self):
        plan = recommend(
            AdvisorInputs(
                asset="BTC",
                entry_price=68000.0,
                atr_14d_usd=1500.0,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=0.7,
                time_horizon=HORIZON_SHORT,
                capital_usd=5000.0,
            )
        )
        md = format_advisor_markdown(plan)
        self.assertIn("1-3 дня", md)


class TestAdvisorConfidenceClamp(unittest.TestCase):
    def test_perfect_alignment_clamped_to_100(self):
        plan = recommend(
            AdvisorInputs(
                asset="BTC",
                entry_price=68000.0,
                atr_14d_usd=1500.0,
                trend="UPTREND",
                quant_verdict="LONG",
                quant_confidence=1.0,
                btc_lean="BULL",
                btc_confidence_pct=95,
                capital_usd=10000.0,
            )
        )
        self.assertLessEqual(plan.confidence_pct, 100)
        self.assertGreaterEqual(plan.confidence_pct, 70)

    def test_confidence_never_negative(self):
        plan = recommend(
            AdvisorInputs(
                asset="ETH",
                entry_price=3500.0,
                atr_14d_usd=120.0,
                trend="DOWNTREND",
                quant_verdict="LONG",  # against trend
                quant_confidence=0.3,
                btc_lean="BEAR",
                btc_confidence_pct=50,  # below veto threshold
                capital_usd=5000.0,
            )
        )
        self.assertGreaterEqual(plan.confidence_pct, 0)


if __name__ == "__main__":
    unittest.main()
