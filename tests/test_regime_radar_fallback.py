"""Тесты фоллбэк-цепочки regime_radar: Gate/Hyperliquid когда Binance геоблок.

Сеть замокана через подмену core.regime_radar._try.
"""
from __future__ import annotations

import unittest

import core.regime_radar as rr


def _gate_rows(n: int):
    # Gate v4 spot candlesticks: [ts, quote_vol, close, high, low, open, ...]
    return [[str(1700000000 + i * 86400), "1.0", str(100.0 + i), "0", "0", "0"]
            for i in range(n)]


def _hl_rows(n: int):
    return [{"t": 1700000000000 + i * 86400000, "c": str(200.0 + i)} for i in range(n)]


class RegimeFallbackTest(unittest.TestCase):
    def setUp(self):
        self._orig = rr._try

    def tearDown(self):
        rr._try = self._orig

    def test_falls_through_to_gate(self):
        # Binance/Bybit падают (геоблок), Gate отдаёт данные → берём Gate.
        def fake(url, parse, timeout=15, post=None):
            if "gateio" in url:
                return parse(_gate_rows(70))
            raise OSError("451 geoblock")
        rr._try = fake
        closes = rr._daily_closes("BTCUSDT")
        self.assertEqual(len(closes), 70)
        self.assertEqual(closes[0], 100.0)       # close = индекс 2
        self.assertEqual(closes[-1], 169.0)      # порядок по возрастанию (свежие в конце)

    def test_falls_through_to_hyperliquid(self):
        # Всё GET-источники падают, остаётся POST Hyperliquid.
        def fake(url, parse, timeout=15, post=None):
            if "hyperliquid" in url:
                self.assertIsNotNone(post)       # HL — это POST
                return parse(_hl_rows(80))
            raise OSError("451 geoblock")
        rr._try = fake
        closes = rr._daily_closes("BTCUSDT")
        self.assertEqual(len(closes), 80)
        self.assertEqual(closes[0], 200.0)

    def test_all_fail_returns_empty(self):
        rr._try = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        self.assertEqual(rr._daily_closes("BTCUSDT"), [])

    def test_too_few_closes_skipped(self):
        # <60 свечей от Gate → не принимаем, идём дальше (тут дальше пусто).
        def fake(url, parse, timeout=15, post=None):
            if "gateio" in url:
                return parse(_gate_rows(40))     # мало
            raise OSError("451")
        rr._try = fake
        self.assertEqual(rr._daily_closes("BTCUSDT"), [])

    def test_base_derivation_for_gate_pair(self):
        # Символ BTCUSDT → пара Gate BTC_USDT (проверяем что url содержит верную пару).
        seen = {}

        def fake(url, parse, timeout=15, post=None):
            if "gateio" in url:
                seen["url"] = url
                return parse(_gate_rows(70))
            raise OSError("451")
        rr._try = fake
        rr._daily_closes("BTCUSDT")
        self.assertIn("currency_pair=BTC_USDT", seen["url"])


if __name__ == "__main__":
    unittest.main()
