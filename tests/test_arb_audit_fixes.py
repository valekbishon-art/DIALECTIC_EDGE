"""Тесты аудит-фиксов кросс-биржевого funding-арба (PR #73).

Покрываем три косяка в шумоподавлении Hyperliquid + честный нетто-спред:
  1) sane-cap в fetch_all больше не роняет ЖИВУЮ ногу HL из-за 1ч-спайка
     (HL пропускается сырым → сглаживается позже);
  2) refine_hl_average при отказе истории (avg_full=None) или невменяемом
     сглаженном среднем УБИРАЕТ ногу HL, а не оставляет сырой снимок;
  3) net_spread() вычитает round-trip косты 2 бирж (аннуализированные на срок).
Сеть замокана — оффлайн, детерминированно.
"""
from __future__ import annotations

import unittest

import core.cross_exchange as ce


class FetchAllHlCapExemptionTest(unittest.TestCase):
    def setUp(self):
        self._orig = ce.VENUES
        # BTC: HL даёт спайк 300% (> SANE_ABS_CAP), Gate — вменяемо.
        # XRP: не-HL биржа даёт 300% → должна быть отсечена.
        ce.VENUES = {
            "Gate": lambda: {"BTC": 9.0, "XRP": 5.0},
            "Hyperliquid": lambda: {"BTC": 300.0},   # 1ч-спайк, > cap
            "Bybit": lambda: {"XRP": 300.0},          # выброс на не-HL ноге
        }

    def tearDown(self):
        ce.VENUES = self._orig

    def test_hl_leg_kept_despite_cap(self):
        by = ce.fetch_all()
        # HL-нога BTC сохранена сырой (будет сглажена в refine), не отсечена cap.
        self.assertEqual(by["BTC"]["Hyperliquid"], 300.0)
        self.assertEqual(by["BTC"]["Gate"], 9.0)

    def test_non_hl_outlier_still_dropped(self):
        by = ce.fetch_all()
        # Выброс 300% на Bybit (не-HL) по-прежнему отсекается sane-cap.
        self.assertNotIn("Bybit", by.get("XRP", {}))


class RefineDropsBadHlTest(unittest.TestCase):
    def tearDown(self):
        # восстановим, если тест подменял
        pass

    def test_drop_hl_when_history_missing(self):
        orig = ce.hl_funding_windows
        ce.hl_funding_windows = lambda coin, full_hours=24, recent_hours=4: (None, None)
        try:
            by = {"DOT": {"Gate": 11.0, "Hyperliquid": -43.0}}
            opps = ce.find_spreads(by)            # фантом-финалист из 1ч-снимка
            by2 = ce.refine_hl_average(by, opps)
            # история не пришла → ногу HL убрали (не оставили сырой -43%)
            self.assertNotIn("Hyperliquid", by2["DOT"])
            self.assertEqual(ce.find_spreads(by2), [])
        finally:
            ce.hl_funding_windows = orig

    def test_drop_hl_when_smoothed_insane(self):
        orig = ce.hl_funding_windows
        # сглаженное среднее всё ещё за пределами cap (стейл-мем) → дропаем
        ce.hl_funding_windows = lambda coin, full_hours=24, recent_hours=4: (-450.0, -450.0)
        try:
            by = {"DOT": {"Gate": 11.0, "Hyperliquid": -43.0}}
            opps = ce.find_spreads(by)
            by2 = ce.refine_hl_average(by, opps)
            self.assertNotIn("Hyperliquid", by2["DOT"])
        finally:
            ce.hl_funding_windows = orig

    def test_sane_smoothed_hl_still_replaced(self):
        orig = ce.hl_funding_windows
        ce.hl_funding_windows = lambda coin, full_hours=24, recent_hours=4: (-40.0, -40.0)
        try:
            by = {"ATOM": {"Gate": 4.0, "Hyperliquid": -46.0}}
            opps = ce.find_spreads(by)
            by2 = ce.refine_hl_average(by, opps)
            self.assertEqual(by2["ATOM"]["Hyperliquid"], -40.0)  # заменено на среднее
        finally:
            ce.hl_funding_windows = orig


class NetSpreadTest(unittest.TestCase):
    def test_net_spread_subtracts_amortized_cost(self):
        o = ce.ArbOpportunity(asset="BTC", long_venue="Gate", short_venue="Bybit",
                              long_ann=-10.0, short_ann=30.0)
        self.assertAlmostEqual(o.spread, 40.0)
        # cost 0.5% за круг, удержание 10 дн → 0.5*365/10 = 18.25 нетто-вычет
        self.assertAlmostEqual(o.net_spread(cost_pct=0.5, hold_days=10.0),
                               40.0 - 18.25, places=4)

    def test_net_below_gross(self):
        o = ce.ArbOpportunity(asset="ETH", long_venue="Gate", short_venue="Bybit",
                              long_ann=0.0, short_ann=12.0)
        self.assertLess(o.net_spread(), o.spread)

    def test_format_shows_net(self):
        o = ce.ArbOpportunity(asset="SOL", long_venue="Gate", short_venue="Bybit",
                              long_ann=-5.0, short_ann=20.0)
        msg = ce.format_arb_md([o])
        self.assertIn("нетто", msg)


if __name__ == "__main__":
    unittest.main()
