"""Regression tests for the RSI label bug in core/screener.py.

Юзер сообщил: сканер аномалий пишет «📉 RSI Перекуплен (17.0)» при RSI=17,
хотя RSI<30 — это перепроданность (oversold), а не перекупленность. Это
один и тот же текст «Перекуплен» был приклеен и к ветке rsi<30, и к
rsi>70 — copy-paste баг в `core/screener.py:83`.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from core.screener import MarketScreener


class TestScreenerRSILabel(unittest.TestCase):
    """RSI<30 → «Перепродан», RSI>70 → «Перекуплен» — не путать."""

    def _run_check(self, rsi_value: float | None) -> list[str]:
        scanner = MarketScreener(top_n=1)

        async def _stub_rsi(self, symbol, timeframe="4h"):
            return rsi_value

        async def _stub_vol(self, symbol):
            return None

        async def _stub_funding(self, symbol):
            return None

        with patch.object(MarketScreener, "_get_rsi", _stub_rsi), \
             patch.object(MarketScreener, "_check_volume_spike", _stub_vol), \
             patch.object(MarketScreener, "_get_funding_rate", _stub_funding):
            result = asyncio.run(scanner._check_symbol("BNBUSDT"))
        return result.get("signals") or []

    def test_rsi_below_30_is_oversold(self):
        # RSI=17 — классическая перепроданность (как у юзера для BNB/TON).
        signals = self._run_check(17.0)
        self.assertEqual(len(signals), 1)
        self.assertIn("Перепродан", signals[0])
        self.assertNotIn("Перекуплен", signals[0])

    def test_rsi_above_70_is_overbought(self):
        signals = self._run_check(82.0)
        self.assertEqual(len(signals), 1)
        self.assertIn("Перекуплен", signals[0])
        self.assertNotIn("Перепродан", signals[0])

    def test_rsi_in_band_no_signal(self):
        # 30 ≤ RSI ≤ 70 — никакого RSI-сигнала.
        signals = self._run_check(50.0)
        self.assertEqual(signals, [])

    def test_rsi_none_no_signal(self):
        # Если данных нет (None) — не падаем и не выдумываем сигнал.
        signals = self._run_check(None)
        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main()
