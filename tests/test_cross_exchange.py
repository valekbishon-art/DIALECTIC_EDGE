"""Тесты кросс-биржевого funding-арба (логика спредов, без сети)."""
from __future__ import annotations

import unittest

from core.cross_exchange import (ArbOpportunity, find_spreads, format_arb_md,
                                 LIQUID_ASSETS, MIN_SPREAD_ANNUAL)


class FindSpreadsTest(unittest.TestCase):
    def test_basic_spread_direction(self):
        # BTC: высокий фандинг на Bybit (+30), низкий на Binance (-5) → спред 35
        by_asset = {"BTC": {"Binance": -5.0, "Bybit": 30.0, "Gate": 10.0}}
        opps = find_spreads(by_asset, min_spread=10.0)
        self.assertEqual(len(opps), 1)
        o = opps[0]
        self.assertEqual(o.asset, "BTC")
        self.assertEqual(o.short_venue, "Bybit")     # шорт где ВЫСОКИЙ
        self.assertEqual(o.long_venue, "Binance")    # лонг где НИЗКИЙ
        self.assertAlmostEqual(o.spread, 35.0)

    def test_below_threshold_filtered(self):
        by_asset = {"ETH": {"Binance": 5.0, "Bybit": 8.0}}  # спред 3 < 12
        self.assertEqual(find_spreads(by_asset), [])

    def test_requires_two_venues(self):
        by_asset = {"SOL": {"Binance": 50.0}}  # одна биржа
        self.assertEqual(find_spreads(by_asset, min_spread=1.0), [])

    def test_illiquid_asset_excluded(self):
        # мусорный тикер не в LIQUID_ASSETS — отсекается даже с жирным спредом
        by_asset = {"SCAMCOIN": {"Gate": 300.0, "Bybit": -50.0}}
        self.assertEqual(find_spreads(by_asset, min_spread=10.0), [])

    def test_custom_universe(self):
        by_asset = {"SCAMCOIN": {"Gate": 300.0, "Bybit": -50.0}}
        opps = find_spreads(by_asset, min_spread=10.0, universe={"SCAMCOIN"})
        self.assertEqual(len(opps), 1)

    def test_sorted_by_spread_desc(self):
        by_asset = {
            "BTC": {"Binance": 0.0, "Bybit": 20.0},   # 20
            "ETH": {"Binance": 0.0, "Bybit": 50.0},   # 50
        }
        opps = find_spreads(by_asset, min_spread=10.0)
        self.assertEqual([o.asset for o in opps], ["ETH", "BTC"])

    def test_negative_funding_collected_correctly(self):
        # обе ноги отрицательные: лонг где сильнее минус (получаешь больше)
        by_asset = {"FIL": {"Bybit": -28.0, "Hyperliquid": 11.0}}
        opps = find_spreads(by_asset, min_spread=10.0)
        self.assertEqual(opps[0].long_venue, "Bybit")       # -28 ниже
        self.assertEqual(opps[0].short_venue, "Hyperliquid")  # +11 выше
        self.assertAlmostEqual(opps[0].spread, 39.0)


class FormatTest(unittest.TestCase):
    def test_empty_message(self):
        msg = format_arb_md([])
        self.assertIn("спредов нет", msg)

    def test_message_has_steps_and_sizing(self):
        opps = [ArbOpportunity("FET", "Binance", "Hyperliquid", -3.0, 52.0)]
        msg = format_arb_md(opps, capital=500)
        self.assertIn("FET", msg)
        self.assertIn("ШОРТ перп FET", msg)
        self.assertIn("ЛОНГ перп FET", msg)
        self.assertIn("$250", msg)          # capital/2 на ногу
        self.assertIn("55% годовых", msg)   # spread = 52-(-3)


if __name__ == "__main__":
    unittest.main()
