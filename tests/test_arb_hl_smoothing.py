"""Тесты сглаживания Hyperliquid-фандинга в кросс-арбе (сеть замокана).

Проверяем, что фантомный арб из часового спайка HL (DOT 54% из мгновенного
-43%) отсекается, когда HL пересчитан по среднему за сутки (+5.5%).
"""
from __future__ import annotations

import unittest

import core.cross_exchange as ce


class RefineHlAverageTest(unittest.TestCase):
    def setUp(self):
        self._orig_avg = ce.hl_funding_avg
        self._calls = []

        def fake_avg(coin, hours=24):
            self._calls.append(coin)
            return {"DOT": 5.5, "ATOM": -40.0}.get(coin)  # DOT схлопнулся, ATOM держится

        ce.hl_funding_avg = fake_avg

    def tearDown(self):
        ce.hl_funding_avg = self._orig_avg

    def test_patches_only_hl_finalists(self):
        by = {
            "DOT": {"Gate": 11.0, "Hyperliquid": -43.0},
            "ATOM": {"Gate": 4.0, "Hyperliquid": -46.0},
            "SOL": {"Gate": 20.0, "Bybit": 5.0},   # без HL — не трогаем
        }
        opps = ce.find_spreads(by)  # DOT 54, ATOM 50, SOL 15 — все проходят
        ce.refine_hl_average(by, opps)
        # тянули историю только по HL-активам
        self.assertEqual(set(self._calls), {"DOT", "ATOM"})
        # HL-значения заменены на средние
        self.assertEqual(by["DOT"]["Hyperliquid"], 5.5)
        self.assertEqual(by["ATOM"]["Hyperliquid"], -40.0)
        # не-HL актив не тронут
        self.assertEqual(by["SOL"], {"Gate": 20.0, "Bybit": 5.0})

    def test_phantom_arb_dropped_after_smoothing(self):
        by = {"DOT": {"Gate": 11.0, "Hyperliquid": -43.0}}
        opps = ce.find_spreads(by)
        self.assertEqual(len(opps), 1)
        self.assertAlmostEqual(opps[0].spread, 54.0)        # фантом до сглаживания
        by2 = ce.refine_hl_average(by, opps)
        opps2 = ce.find_spreads(by2)
        # DOT: Gate 11 vs HL 5.5 → спред 5.5 < 12 → отсеян
        self.assertEqual(opps2, [])

    def test_real_arb_survives_smoothing(self):
        # ATOM держит -40% за сутки → спред 4-(-40)=44 остаётся выше порога.
        by = {"ATOM": {"Gate": 4.0, "Hyperliquid": -46.0}}
        opps = ce.find_spreads(by)
        by2 = ce.refine_hl_average(by, opps)
        opps2 = ce.find_spreads(by2)
        self.assertEqual(len(opps2), 1)
        self.assertAlmostEqual(opps2[0].spread, 44.0)
        self.assertEqual(opps2[0].long_venue, "Hyperliquid")  # лонг там где низкий


class ScanPipelineTest(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = ce.fetch_all
        self._orig_avg = ce.hl_funding_avg
        ce.fetch_all = lambda: {"DOT": {"Gate": 11.0, "Hyperliquid": -43.0}}
        ce.hl_funding_avg = lambda coin, hours=24: 5.5

    def tearDown(self):
        ce.fetch_all = self._orig_fetch
        ce.hl_funding_avg = self._orig_avg

    def test_scan_refine_drops_phantom(self):
        self.assertEqual(ce.scan(refine=True), [])

    def test_scan_no_refine_keeps_phantom(self):
        opps = ce.scan(refine=False)
        self.assertEqual(len(opps), 1)
        self.assertAlmostEqual(opps[0].spread, 54.0)


if __name__ == "__main__":
    unittest.main()
