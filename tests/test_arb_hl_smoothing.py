"""Тесты сглаживания Hyperliquid-фандинга в кросс-арбе (сеть замокана).

Проверяем, что фантомный арб из часового спайка HL (DOT 54% из мгновенного
-43%) отсекается, когда HL пересчитан по среднему за сутки (+5.5%).
"""
from __future__ import annotations

import unittest

import core.cross_exchange as ce


class RefineHlAverageTest(unittest.TestCase):
    def setUp(self):
        self._orig_windows = ce.hl_funding_windows
        self._calls = []

        def fake_windows(coin, full_hours=24, recent_hours=4):
            self._calls.append(coin)
            val = {"DOT": 5.5, "ATOM": -40.0}.get(coin)  # DOT схлопнулся, ATOM держится
            return val, val  # full == recent → знак не менялся, ногу HL не выкидываем

        ce.hl_funding_windows = fake_windows

    def tearDown(self):
        ce.hl_funding_windows = self._orig_windows

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

    def test_fresh_flip_drops_hl_leg(self):
        # Баг SEI: суточное среднее +5.5%, но последние часы уже -30%
        # (свежий разворот знака). Ногу Hyperliquid выкидываем — направление
        # арба по сглаженному знаку ненадёжно (вход был бы не в ту сторону).
        ce.hl_funding_windows = lambda coin, full_hours=24, recent_hours=4: (5.5, -30.0)
        by = {"DOT": {"Gate": 11.0, "Hyperliquid": -43.0}}
        opps = ce.find_spreads(by)
        by2 = ce.refine_hl_average(by, opps)
        self.assertNotIn("Hyperliquid", by2["DOT"])
        self.assertEqual(ce.find_spreads(by2), [])  # осталась одна нога → арба нет


class ScanPipelineTest(unittest.TestCase):
    def setUp(self):
        self._orig_fetch = ce.fetch_all
        self._orig_windows = ce.hl_funding_windows
        _by = {"DOT": {"Gate": 11.0, "Hyperliquid": -43.0}}
        ce.fetch_all = lambda *, with_health=False: (_by, True) if with_health else _by
        ce.hl_funding_windows = lambda coin, full_hours=24, recent_hours=4: (5.5, 5.5)

    def tearDown(self):
        ce.fetch_all = self._orig_fetch
        ce.hl_funding_windows = self._orig_windows

    def test_scan_refine_drops_phantom(self):
        self.assertEqual(ce.scan(refine=True), [])

    def test_scan_no_refine_keeps_phantom(self):
        opps = ce.scan(refine=False)
        self.assertEqual(len(opps), 1)
        self.assertAlmostEqual(opps[0].spread, 54.0)


if __name__ == "__main__":
    unittest.main()
