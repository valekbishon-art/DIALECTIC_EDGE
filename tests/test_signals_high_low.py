"""Task 6 (Andrey): MARKET SIGNALS Bybit-style — per-coin 24h High/Low + маркер
близости к краю дня (📈 у дневного максимума / 📉 у дневного минимума, ~1% от
края). Funding сохраняется.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from signals import _fmt_price, build_signals_message, high_low_lines


class TestFmtPrice(unittest.TestCase):
    def test_big_price_space_separated(self):
        self.assertEqual(_fmt_price(70950), "70 950")

    def test_four_digit_price_no_decimals(self):
        # >=1000 → целое со space-разделителем (для 24ч-диапазона достаточно).
        self.assertEqual(_fmt_price(3512.4), "3 512")

    def test_sub_thousand_two_decimals(self):
        self.assertEqual(_fmt_price(512.4), "512.40")

    def test_small_alt_price(self):
        self.assertEqual(_fmt_price(0.4218), "0.4218")

    def test_zero_or_none(self):
        self.assertEqual(_fmt_price(0), "—")
        self.assertEqual(_fmt_price(None), "—")


class TestHighLowLines(unittest.TestCase):
    def test_near_high_marker(self):
        with patch.dict(os.environ, {}, clear=True):
            lines = high_low_lines(
                {"high_24h": 71500, "low_24h": 67200, "last_price": 71100}
            )
        self.assertTrue(any("у дневного максимума" in line for line in lines))
        self.assertTrue(any("24ч" in line for line in lines))

    def test_near_low_marker(self):
        with patch.dict(os.environ, {}, clear=True):
            lines = high_low_lines(
                {"high_24h": 71500, "low_24h": 67200, "last_price": 67400}
            )
        self.assertTrue(any("у дневного минимума" in line for line in lines))

    def test_middle_shows_position(self):
        with patch.dict(os.environ, {}, clear=True):
            lines = high_low_lines(
                {"high_24h": 71500, "low_24h": 67200, "last_price": 69000}
            )
        self.assertFalse(any("максимума" in line for line in lines))
        self.assertFalse(any("минимума" in line for line in lines))
        self.assertTrue(any("диапазона дня" in line for line in lines))

    def test_missing_high_low_empty(self):
        self.assertEqual(high_low_lines({"last_price": 100}), [])
        self.assertEqual(high_low_lines({}), [])

    def test_invalid_high_below_low_empty(self):
        self.assertEqual(
            high_low_lines({"high_24h": 100, "low_24h": 200, "last_price": 150}), []
        )

    def test_threshold_env_override_widens_edge(self):
        # last=69000, high=71500 → 3.5% от хая. Дефолт 1% → не у края.
        # При SIGNALS_EDGE_THRESHOLD_PCT=5 → 3.5% ≤ 5% → у максимума.
        data = {"high_24h": 71500, "low_24h": 67200, "last_price": 69000}
        with patch.dict(os.environ, {"SIGNALS_EDGE_THRESHOLD_PCT": "5"}, clear=True):
            lines = high_low_lines(data)
        self.assertTrue(any("у дневного максимума" in line for line in lines))

    def test_explicit_threshold_arg(self):
        data = {"high_24h": 71500, "low_24h": 67200, "last_price": 71100}
        # Очень узкий порог 0.1% → 0.56% от хая НЕ попадает.
        lines = high_low_lines(data, threshold_pct=0.1)
        self.assertFalse(any("максимума" in line for line in lines))


class TestBuildSignalsHighLow(unittest.TestCase):
    def test_message_includes_high_low_and_keeps_funding(self):
        binance_data = {
            "BTCUSDT": {
                "price_change": 2.1, "funding_rate": 0.0002,
                "last_price": 71100, "high_24h": 71500, "low_24h": 67200,
                "long": 58, "short": 42, "has_traders_data": True,
            },
            "ETHUSDT": {
                "price_change": -1.2, "funding_rate": -0.0001,
                "last_price": 3402, "high_24h": 3600, "low_24h": 3390,
            },
        }
        with patch.dict(os.environ, {}, clear=True):
            msg = build_signals_message([], binance_data, {"verdict": "BULLISH"})
        self.assertIn("24ч", msg)
        self.assertIn("у дневного максимума", msg)      # BTC near high
        self.assertIn("у дневного минимума", msg)        # ETH near low
        self.assertIn("Funding", msg)                    # funding kept
        self.assertIn("Лонг", msg)                       # traders branch intact


if __name__ == "__main__":
    unittest.main()
