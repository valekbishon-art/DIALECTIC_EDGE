"""Тест: аннуализация фандинга учитывает реальный интервал (4ч/1ч), не хардкод 8ч.

Сеть замокана через core.cross_exchange._get.
"""
from __future__ import annotations

import unittest

import core.cross_exchange as ce


class BinanceIntervalTest(unittest.TestCase):
    def setUp(self):
        self._orig = ce._get

        def fake(url, timeout=12, post=None):
            if "fundingInfo" in url:
                return [{"symbol": "LPTUSDT", "fundingIntervalHours": 4},
                        {"symbol": "IDUSDT", "fundingIntervalHours": 1}]
            if "premiumIndex" in url:
                return [{"symbol": "LPTUSDT", "lastFundingRate": "0.0005"},   # 4ч
                        {"symbol": "IDUSDT", "lastFundingRate": "0.0005"},    # 1ч
                        {"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}]   # 8ч (деф)
            return []
        ce._get = fake

    def tearDown(self):
        ce._get = self._orig

    def test_4h_annualized_x6_not_x3(self):
        out = ce.funding_binance()
        # 0.0005 * (24/4) * 365 * 100 = 109.5 (а хардкод 8ч дал бы 54.75)
        self.assertAlmostEqual(out["LPT"], 0.0005 * 6 * 365 * 100, places=4)

    def test_1h_annualized_x24(self):
        out = ce.funding_binance()
        self.assertAlmostEqual(out["ID"], 0.0005 * 24 * 365 * 100, places=4)

    def test_8h_default_when_not_listed(self):
        out = ce.funding_binance()
        # BTC нет в fundingInfo → дефолт 8ч → ×3
        self.assertAlmostEqual(out["BTC"], 0.0001 * 3 * 365 * 100, places=4)


class BybitIntervalTest(unittest.TestCase):
    def setUp(self):
        self._orig = ce._get

        def fake(url, timeout=12, post=None):
            if "instruments-info" in url:
                return {"result": {"list": [
                    {"symbol": "LPTUSDT", "fundingInterval": 240},   # 4ч
                    {"symbol": "BTCUSDT", "fundingInterval": 480},   # 8ч
                ]}}
            if "tickers" in url:
                return {"result": {"list": [
                    {"symbol": "LPTUSDT", "fundingRate": "0.0005"},
                    {"symbol": "BTCUSDT", "fundingRate": "0.0001"},
                ]}}
            return {}
        ce._get = fake

    def tearDown(self):
        ce._get = self._orig

    def test_bybit_4h_x6(self):
        out = ce.funding_bybit()
        self.assertAlmostEqual(out["LPT"], 0.0005 * 6 * 365 * 100, places=4)

    def test_bybit_8h_x3(self):
        out = ce.funding_bybit()
        self.assertAlmostEqual(out["BTC"], 0.0001 * 3 * 365 * 100, places=4)


if __name__ == "__main__":
    unittest.main()
