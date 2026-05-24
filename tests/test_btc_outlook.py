from __future__ import annotations

import unittest

from core.btc_outlook import (
    LEAN_BEAR,
    LEAN_BULL,
    LEAN_NEUTRAL,
    BTCOutlookInputs,
    compute_btc_outlook,
    format_btc_outlook_markdown,
)


class TestBTCOutlookEmpty(unittest.TestCase):
    def test_empty_inputs_neutral_zero_confidence(self):
        v = compute_btc_outlook(BTCOutlookInputs())
        self.assertEqual(v.lean, LEAN_NEUTRAL)
        self.assertEqual(v.confidence_pct, 0)
        self.assertEqual(v.inputs_seen, 0)
        self.assertTrue(v.fallback_used)

    def test_format_empty_says_no_signals(self):
        v = compute_btc_outlook(BTCOutlookInputs())
        text = format_btc_outlook_markdown(v, BTCOutlookInputs())
        self.assertIn("Нет сигналов", text)


class TestBTCOutlookBullSetup(unittest.TestCase):
    def test_strong_bullish_inputs_yield_bull_lean(self):
        inputs = BTCOutlookInputs(
            btc_price_usd=68000.0,
            price_change_24h_pct=3.5,
            funding_rate_8h_pct=0.025,
            oi_change_24h_pct=4.0,
            top_trader_ls_ratio=1.4,
            btc_dominance_pct=58.0,
            dominance_change_7d_pct=1.2,
            etf_basket_change_5d_avg_pct=0.8,
            stablecoin_supply_delta_24h_pct=0.5,
            options_skew_25d=-3.0,
            fear_greed_index=22,
            quant_verdict_direction="LONG",
            quant_verdict_strength=0.7,
            regime="BULL",
        )
        v = compute_btc_outlook(inputs)
        self.assertEqual(v.lean, LEAN_BULL)
        self.assertGreaterEqual(v.confidence_pct, 50)
        self.assertGreater(v.bull_score, v.bear_score)


class TestBTCOutlookBearSetup(unittest.TestCase):
    def test_strong_bearish_inputs_yield_bear_lean(self):
        inputs = BTCOutlookInputs(
            btc_price_usd=58000.0,
            price_change_24h_pct=-4.2,
            funding_rate_8h_pct=-0.03,
            oi_change_24h_pct=5.0,
            top_trader_ls_ratio=2.8,
            btc_dominance_pct=54.0,
            dominance_change_7d_pct=-1.5,
            etf_outflow_signal="CRIT",
            stablecoin_supply_delta_24h_pct=-0.7,
            options_skew_25d=6.0,
            fear_greed_index=82,
            quant_verdict_direction="SHORT",
            quant_verdict_strength=0.6,
            regime="BEAR",
        )
        v = compute_btc_outlook(inputs)
        self.assertEqual(v.lean, LEAN_BEAR)
        self.assertGreaterEqual(v.confidence_pct, 50)
        self.assertGreater(v.bear_score, v.bull_score)


class TestBTCOutlookConflict(unittest.TestCase):
    def test_mixed_signals_yield_neutral_or_low_confidence(self):
        inputs = BTCOutlookInputs(
            price_change_24h_pct=1.0,
            funding_rate_8h_pct=-0.02,
            oi_change_24h_pct=-2.0,
            top_trader_ls_ratio=1.0,
            btc_dominance_pct=55.0,
            dominance_change_7d_pct=0.0,
            etf_basket_change_5d_avg_pct=0.0,
            stablecoin_supply_delta_24h_pct=0.0,
            fear_greed_index=50,
            regime="RANGE",
        )
        v = compute_btc_outlook(inputs)
        if v.lean != LEAN_NEUTRAL:
            self.assertLess(v.confidence_pct, 50)


class TestOIPriceCombo(unittest.TestCase):
    def test_oi_up_price_up_strong_bull(self):
        v = compute_btc_outlook(
            BTCOutlookInputs(price_change_24h_pct=2.0, oi_change_24h_pct=3.0)
        )
        oi_contrib = next(c for c in v.contributions if c.name == "oi_price")
        self.assertEqual(oi_contrib.direction, 1)
        self.assertGreaterEqual(oi_contrib.weight, 0.7)

    def test_oi_up_price_down_strong_bear(self):
        v = compute_btc_outlook(
            BTCOutlookInputs(price_change_24h_pct=-2.0, oi_change_24h_pct=3.0)
        )
        oi_contrib = next(c for c in v.contributions if c.name == "oi_price")
        self.assertEqual(oi_contrib.direction, -1)
        self.assertGreaterEqual(oi_contrib.weight, 0.6)

    def test_oi_down_price_up_short_squeeze_weak_bull(self):
        v = compute_btc_outlook(
            BTCOutlookInputs(price_change_24h_pct=2.0, oi_change_24h_pct=-3.0)
        )
        oi_contrib = next(c for c in v.contributions if c.name == "oi_price")
        self.assertEqual(oi_contrib.direction, 1)
        self.assertLess(oi_contrib.weight, 0.6)

    def test_oi_down_price_down_capitulation(self):
        v = compute_btc_outlook(
            BTCOutlookInputs(price_change_24h_pct=-2.0, oi_change_24h_pct=-3.0)
        )
        oi_contrib = next(c for c in v.contributions if c.name == "oi_price")
        self.assertEqual(oi_contrib.direction, -1)


class TestFundingContrarian(unittest.TestCase):
    def test_extreme_positive_funding_is_bearish(self):
        v = compute_btc_outlook(BTCOutlookInputs(funding_rate_8h_pct=0.08))
        c = next(c for c in v.contributions if c.name == "funding")
        self.assertEqual(c.direction, -1)

    def test_extreme_negative_funding_is_bullish_squeeze(self):
        v = compute_btc_outlook(BTCOutlookInputs(funding_rate_8h_pct=-0.08))
        c = next(c for c in v.contributions if c.name == "funding")
        self.assertEqual(c.direction, 1)

    def test_moderate_positive_funding_is_bullish(self):
        v = compute_btc_outlook(BTCOutlookInputs(funding_rate_8h_pct=0.03))
        c = next(c for c in v.contributions if c.name == "funding")
        self.assertEqual(c.direction, 1)


class TestETFSignals(unittest.TestCase):
    def test_crit_outflow_strong_bear(self):
        v = compute_btc_outlook(BTCOutlookInputs(etf_outflow_signal="CRIT"))
        c = next(c for c in v.contributions if c.name == "etf")
        self.assertEqual(c.direction, -1)
        self.assertGreaterEqual(c.weight, 0.8)

    def test_positive_basket_bull(self):
        v = compute_btc_outlook(BTCOutlookInputs(etf_basket_change_5d_avg_pct=1.5))
        c = next(c for c in v.contributions if c.name == "etf")
        self.assertEqual(c.direction, 1)


class TestFearGreedContrarian(unittest.TestCase):
    def test_extreme_fear_is_contra_bull(self):
        v = compute_btc_outlook(BTCOutlookInputs(fear_greed_index=10))
        c = next(c for c in v.contributions if c.name == "fear_greed")
        self.assertEqual(c.direction, 1)

    def test_extreme_greed_is_contra_bear(self):
        v = compute_btc_outlook(BTCOutlookInputs(fear_greed_index=90))
        c = next(c for c in v.contributions if c.name == "fear_greed")
        self.assertEqual(c.direction, -1)


class TestFormatting(unittest.TestCase):
    def test_format_includes_lean_and_confidence(self):
        inputs = BTCOutlookInputs(
            btc_price_usd=70000.0,
            price_change_24h_pct=2.5,
            funding_rate_8h_pct=0.02,
            etf_basket_change_5d_avg_pct=1.0,
        )
        v = compute_btc_outlook(inputs)
        text = format_btc_outlook_markdown(v, inputs)
        self.assertIn("BTC outlook", text)
        self.assertIn(v.lean, text)
        self.assertIn(f"{v.confidence_pct}%", text)
        self.assertIn("$70,000", text)

    def test_format_includes_ai_narrative_when_provided(self):
        inputs = BTCOutlookInputs(price_change_24h_pct=1.0)
        v = compute_btc_outlook(inputs)
        text = format_btc_outlook_markdown(v, inputs, ai_narrative="Тест нарратив.")
        self.assertIn("AI synth", text)
        self.assertIn("Тест нарратив", text)


class TestNonFinite(unittest.TestCase):
    def test_nan_inputs_are_ignored(self):
        v = compute_btc_outlook(
            BTCOutlookInputs(
                price_change_24h_pct=float("nan"),
                funding_rate_8h_pct=float("inf"),
                oi_change_24h_pct=float("-inf"),
            )
        )
        self.assertEqual(v.inputs_seen, 0)
        self.assertEqual(v.lean, LEAN_NEUTRAL)


if __name__ == "__main__":
    unittest.main()
