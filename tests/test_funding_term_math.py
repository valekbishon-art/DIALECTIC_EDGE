"""Unit-tests для market_indicators.funding_term_structure (pure math)."""

from __future__ import annotations

import math
import unittest
from datetime import datetime

from market_indicators.funding_term_structure import (
    BasisPoint,
    DEFAULT_SLOPE_NEUTRAL_BPS,
    FundingRateSnapshot,
    PERIODS_PER_YEAR_1H,
    PERIODS_PER_YEAR_8H,
    TermStructureSignal,
    _classify_days_to_expiry,
    annualized_funding_rate,
    basis_carry_annualized,
    build_term_structure,
    classify_stress_level,
    detect_inversion_event,
    estimate_days_to_expiry,
    parse_bybit_quarterly_symbol,
)


class AnnualizedFundingTestCase(unittest.TestCase):
    def test_zero_funding(self):
        self.assertEqual(annualized_funding_rate(0.0, period_hours=8.0), 0.0)

    def test_8h_typical(self):
        # 0.01% per 8h × 1095 ≈ 0.1095 = 10.95% годовых
        result = annualized_funding_rate(0.0001, period_hours=8.0)
        self.assertAlmostEqual(result, 0.10954, places=4)

    def test_1h_typical(self):
        # 0.001% per 1h × 8760 = 0.0876 = 8.76% годовых
        result = annualized_funding_rate(0.00001, period_hours=1.0)
        self.assertAlmostEqual(result, 0.0876, places=4)

    def test_negative_funding(self):
        result = annualized_funding_rate(-0.0001, period_hours=8.0)
        self.assertLess(result, 0)

    def test_nan_returns_zero(self):
        self.assertEqual(
            annualized_funding_rate(float("nan"), period_hours=8.0), 0.0,
        )

    def test_inf_returns_zero(self):
        self.assertEqual(
            annualized_funding_rate(float("inf"), period_hours=8.0), 0.0,
        )

    def test_invalid_period_raises(self):
        with self.assertRaises(ValueError):
            annualized_funding_rate(0.0001, period_hours=0.0)
        with self.assertRaises(ValueError):
            annualized_funding_rate(0.0001, period_hours=-8.0)

    def test_periods_per_year_constants(self):
        self.assertAlmostEqual(PERIODS_PER_YEAR_8H, 1095.0, places=2)
        self.assertEqual(PERIODS_PER_YEAR_1H, 8760.0)


class BasisCarryTestCase(unittest.TestCase):
    def test_contango_carry(self):
        # фьючерс на 2% дороже спота с экспирацией через 90 дней →
        # 0.02 × (365 / 90) ≈ 0.0811 = 8.11% годовых
        result = basis_carry_annualized(
            futures_price=102.0, spot_price=100.0, days_to_expiry=90,
        )
        self.assertAlmostEqual(result, 0.0811, places=3)

    def test_backwardation_carry(self):
        result = basis_carry_annualized(
            futures_price=98.0, spot_price=100.0, days_to_expiry=90,
        )
        self.assertAlmostEqual(result, -0.0811, places=3)

    def test_zero_days(self):
        self.assertEqual(basis_carry_annualized(
            futures_price=100.0, spot_price=100.0, days_to_expiry=0,
        ), 0.0)

    def test_zero_spot(self):
        self.assertEqual(basis_carry_annualized(
            futures_price=100.0, spot_price=0.0, days_to_expiry=90,
        ), 0.0)

    def test_nan_input(self):
        self.assertEqual(basis_carry_annualized(
            futures_price=float("nan"), spot_price=100.0, days_to_expiry=90,
        ), 0.0)


class ClassifyDaysTestCase(unittest.TestCase):
    def test_monthly(self):
        self.assertEqual(_classify_days_to_expiry(30), "monthly")
        self.assertEqual(_classify_days_to_expiry(15), "monthly")
        self.assertEqual(_classify_days_to_expiry(45), "monthly")

    def test_quarterly(self):
        self.assertEqual(_classify_days_to_expiry(90), "quarterly")
        self.assertEqual(_classify_days_to_expiry(60), "quarterly")
        self.assertEqual(_classify_days_to_expiry(120), "quarterly")

    def test_neither(self):
        self.assertIsNone(_classify_days_to_expiry(7))
        self.assertIsNone(_classify_days_to_expiry(180))
        self.assertIsNone(_classify_days_to_expiry(50))  # gap 46-59


class EstimateDaysToExpiryTestCase(unittest.TestCase):
    def test_30_days_ahead(self):
        now = datetime(2026, 1, 1, 12, 0)
        expiry = datetime(2026, 1, 31, 12, 0)
        self.assertEqual(estimate_days_to_expiry(expiry_date=expiry, now=now), 30)

    def test_past_expiry_returns_zero(self):
        now = datetime(2026, 1, 1)
        expiry = datetime(2025, 12, 31)
        self.assertEqual(estimate_days_to_expiry(expiry_date=expiry, now=now), 0)


class ParseBybitQuarterlyTestCase(unittest.TestCase):
    def test_btc_dec25(self):
        result = parse_bybit_quarterly_symbol("BTC-26DEC25")
        self.assertEqual(result, datetime(2025, 12, 26, 8, 0))

    def test_eth_jun26(self):
        result = parse_bybit_quarterly_symbol("ETH-30JUN26")
        self.assertEqual(result, datetime(2026, 6, 30, 8, 0))

    def test_returns_none_on_garbage(self):
        self.assertIsNone(parse_bybit_quarterly_symbol("BTCUSDT"))
        self.assertIsNone(parse_bybit_quarterly_symbol("BTC-XX99"))
        self.assertIsNone(parse_bybit_quarterly_symbol(""))
        self.assertIsNone(parse_bybit_quarterly_symbol("BTC-32FEB26"))


class BuildTermStructureTestCase(unittest.TestCase):
    def test_empty_signals_returns_all_none(self):
        sig = build_term_structure(
            asset="BTC", funding_snapshots=[], basis_points=[],
            timestamp_ms=1700000000000,
        )
        self.assertIsNone(sig.spot_funding_annual)
        self.assertIsNone(sig.monthly_basis_annual)
        self.assertIsNone(sig.quarterly_basis_annual)
        self.assertIsNone(sig.slope_annual)
        self.assertFalse(sig.is_inverted)
        self.assertEqual(sig.venues_used, ())

    def test_funding_averaged_across_venues(self):
        sig = build_term_structure(
            asset="BTC",
            funding_snapshots=[
                FundingRateSnapshot("bybit", "BTCUSDT", "BTC", 0.0001, 8.0),
                FundingRateSnapshot("binance", "BTCUSDT", "BTC", 0.0002, 8.0),
            ],
            basis_points=[],
            timestamp_ms=1700000000000,
        )
        # average of 0.0001 and 0.0002 = 0.00015 × 1095 ≈ 0.1643
        self.assertIsNotNone(sig.spot_funding_annual)
        self.assertAlmostEqual(sig.spot_funding_annual, 0.16425, places=3)
        self.assertIn("bybit", sig.venues_used)
        self.assertIn("binance", sig.venues_used)

    def test_only_relevant_asset_used(self):
        sig = build_term_structure(
            asset="BTC",
            funding_snapshots=[
                FundingRateSnapshot("bybit", "ETHUSDT", "ETH", 0.0005, 8.0),
            ],
            basis_points=[],
            timestamp_ms=1700000000000,
        )
        self.assertIsNone(sig.spot_funding_annual)

    def test_quarterly_basis(self):
        sig = build_term_structure(
            asset="BTC",
            funding_snapshots=[
                FundingRateSnapshot("bybit", "BTCUSDT", "BTC", 0.0001, 8.0),
            ],
            basis_points=[
                BasisPoint("bybit", "BTC-26DEC25", "BTC", 102.0, 100.0, 90),
            ],
            timestamp_ms=1700000000000,
        )
        self.assertIsNotNone(sig.quarterly_basis_annual)
        self.assertAlmostEqual(sig.quarterly_basis_annual, 0.0811, places=3)
        self.assertIsNotNone(sig.slope_annual)
        # slope = quarterly (0.0811) - spot (0.1095) = -0.0284 → inverted
        self.assertLess(sig.slope_annual, 0)
        self.assertTrue(sig.is_inverted)

    def test_monthly_and_quarterly(self):
        sig = build_term_structure(
            asset="BTC",
            funding_snapshots=[
                FundingRateSnapshot("bybit", "BTCUSDT", "BTC", 0.0001, 8.0),
            ],
            basis_points=[
                BasisPoint("bybit", "BTC-30JAN26", "BTC", 100.5, 100.0, 30),
                BasisPoint("bybit", "BTC-26DEC25", "BTC", 103.0, 100.0, 90),
            ],
            timestamp_ms=1700000000000,
        )
        self.assertIsNotNone(sig.monthly_basis_annual)
        self.assertIsNotNone(sig.quarterly_basis_annual)
        # quarterly carry = 0.03 × (365/90) = 0.1217 → > spot 0.1095 → normal contango
        self.assertGreater(sig.slope_annual, 0)
        self.assertFalse(sig.is_inverted)

    def test_skip_irrelevant_expiry(self):
        # 7-day futures — не попадает ни в monthly, ни в quarterly
        sig = build_term_structure(
            asset="BTC",
            funding_snapshots=[],
            basis_points=[
                BasisPoint("bybit", "BTC-08JAN26", "BTC", 100.5, 100.0, 7),
            ],
            timestamp_ms=1700000000000,
        )
        self.assertIsNone(sig.monthly_basis_annual)
        self.assertIsNone(sig.quarterly_basis_annual)


class DetectInversionEventTestCase(unittest.TestCase):
    def _signal(self, slope: float | None) -> TermStructureSignal:
        return TermStructureSignal(
            asset="BTC", timestamp_ms=0,
            spot_funding_annual=0.10,
            monthly_basis_annual=None,
            quarterly_basis_annual=None,
            slope_annual=slope, is_inverted=(slope or 0) < 0,
        )

    def test_returns_none_when_previous_none(self):
        self.assertIsNone(detect_inversion_event(
            current=self._signal(-0.05), previous=None,
        ))

    def test_returns_none_when_slope_none(self):
        self.assertIsNone(detect_inversion_event(
            current=self._signal(None), previous=self._signal(0.05),
        ))

    def test_inversion_onset(self):
        result = detect_inversion_event(
            current=self._signal(-0.05),
            previous=self._signal(0.05),
        )
        self.assertEqual(result, "inversion_onset")

    def test_inversion_recovery(self):
        result = detect_inversion_event(
            current=self._signal(0.05),
            previous=self._signal(-0.05),
        )
        self.assertEqual(result, "inversion_recovery")

    def test_no_change_in_same_regime(self):
        self.assertIsNone(detect_inversion_event(
            current=self._signal(0.05), previous=self._signal(0.08),
        ))

    def test_neutral_zone(self):
        # Прошлое и текущее оба в neutral zone — не event
        self.assertIsNone(detect_inversion_event(
            current=self._signal(0.0001),
            previous=self._signal(-0.0001),
            neutral_bps=5.0,
        ))


class ClassifyStressLevelTestCase(unittest.TestCase):
    def _signal(self, slope: float | None) -> TermStructureSignal:
        return TermStructureSignal(
            asset="BTC", timestamp_ms=0,
            spot_funding_annual=None, monthly_basis_annual=None,
            quarterly_basis_annual=None,
            slope_annual=slope, is_inverted=False,
        )

    def test_unknown_when_no_slope(self):
        self.assertEqual(classify_stress_level(self._signal(None)), "unknown")

    def test_panic(self):
        self.assertEqual(classify_stress_level(self._signal(-0.15)), "panic")

    def test_stress(self):
        self.assertEqual(classify_stress_level(self._signal(-0.05)), "stress")

    def test_neutral(self):
        self.assertEqual(classify_stress_level(self._signal(0.0)), "neutral")
        self.assertEqual(classify_stress_level(self._signal(0.005)), "neutral")

    def test_normal_contango(self):
        self.assertEqual(classify_stress_level(self._signal(0.05)), "normal_contango")


class ConstantsTestCase(unittest.TestCase):
    def test_neutral_bps_positive(self):
        self.assertGreater(DEFAULT_SLOPE_NEUTRAL_BPS, 0)


if __name__ == "__main__":
    unittest.main()
