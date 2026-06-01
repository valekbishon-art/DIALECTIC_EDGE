"""Тесты calendar basis carry (чистая математика+логика, без сети)."""
from __future__ import annotations

import unittest
from datetime import date

from core.basis_carry import (
    BasisOpportunity, find_basis, format_basis_md, _parse_expiry, COST_ROUNDTRIP,
)

TODAY = date(2026, 6, 1)
# Числа из живого зонда Binance (2026-06-01).
SPOT = {"BTC": 71135.25}
QUARTERLY = [
    ("BTC", "BTCUSDT_260626", 71286.4, date(2026, 6, 26)),   # 25 дн → нетто <0
    ("BTC", "BTCUSDT_260925", 71777.4, date(2026, 9, 25)),   # 116 дн → нетто >0
]


class BasisMathTest(unittest.TestCase):
    def test_annual_pct_clean(self):
        o = BasisOpportunity("BTC", "X", spot=100.0, future=110.0,
                             days_to_exp=365, expiry="2027-06-01")
        self.assertAlmostEqual(o.annual_pct, 10.0, places=6)

    def test_net_subtracts_annualized_cost(self):
        o = BasisOpportunity("BTC", "X", spot=100.0, future=110.0,
                             days_to_exp=365, expiry="2027-06-01")
        # 1 год → косты round-trip вычитаются как есть (0.4%).
        self.assertAlmostEqual(o.net_annual_pct, 10.0 - COST_ROUNDTRIP * 100.0, places=6)

    def test_short_hold_cost_drag_larger(self):
        # Одинаковый GROSS годовой (10%), но разный срок: на коротком сроке
        # фикс-косты аннуализируются крупнее → нетто ниже. F подобраны так, что
        # (F/S-1)*365/days == 10% в обоих случаях.
        long_o = BasisOpportunity("BTC", "X", 100.0, 110.0, 365, "")        # gross 10%
        short_o = BasisOpportunity("BTC", "X", 100.0, 100.82192, 30, "")    # gross ~10%
        self.assertAlmostEqual(long_o.annual_pct, short_o.annual_pct, places=1)
        self.assertLess(short_o.net_annual_pct, long_o.net_annual_pct)

    def test_zero_guard(self):
        self.assertEqual(BasisOpportunity("BTC", "X", 0.0, 10.0, 30, "").annual_pct, 0.0)


class ParseExpiryTest(unittest.TestCase):
    def test_quarterly(self):
        self.assertEqual(_parse_expiry("BTCUSDT_260626"), date(2026, 6, 26))

    def test_perp_is_none(self):
        self.assertIsNone(_parse_expiry("BTCUSD_PERP"))

    def test_no_underscore_none(self):
        self.assertIsNone(_parse_expiry("BTCUSDT"))

    def test_garbage_none(self):
        self.assertIsNone(_parse_expiry("BTCUSDT_999999"))


class FindBasisTest(unittest.TestCase):
    def test_picks_best_net_not_front(self):
        # Фронт (июнь, 25дн) нетто отрицателен; сентябрь (116дн) нетто +.
        # Должен выбрать СЕНТЯБРЬ, не ближайший.
        opps = find_basis(SPOT, QUARTERLY, min_net=1.0, today=TODAY)
        self.assertEqual(len(opps), 1)
        self.assertTrue(opps[0].contract.endswith("260925"))
        self.assertGreater(opps[0].net_annual_pct, 1.0)
        self.assertLess(opps[0].net_annual_pct, 2.0)  # ~1.58%

    def test_threshold_filters(self):
        # Порог 2.0% — сентябрь (~1.58%) не проходит → пусто.
        self.assertEqual(find_basis(SPOT, QUARTERLY, min_net=2.0, today=TODAY), [])

    def test_window_excludes_near_expiry(self):
        q = [("BTC", "BTCUSDT_260605", 80000.0, date(2026, 6, 5))]  # 4 дня < MIN_DAYS
        self.assertEqual(find_basis(SPOT, q, min_net=0.0, today=TODAY), [])

    def test_window_excludes_far(self):
        q = [("BTC", "BTCUSDT_261226", 90000.0, date(2026, 12, 26))]  # 208 дн > MAX
        self.assertEqual(find_basis(SPOT, q, min_net=0.0, today=TODAY), [])

    def test_no_spot_skipped(self):
        self.assertEqual(find_basis({}, QUARTERLY, min_net=0.0, today=TODAY), [])


class FormatTest(unittest.TestCase):
    def test_empty_message(self):
        msg = format_basis_md([])
        self.assertIn("BASIS CARRY", msg)
        self.assertIn("тонкий", msg)

    def test_nonempty_has_steps(self):
        opps = find_basis(SPOT, QUARTERLY, min_net=1.0, today=TODAY)
        msg = format_basis_md(opps, capital=1000.0)
        self.assertIn("ЛОНГ спот", msg)
        self.assertIn("ШОРТ квартальный фьюч", msg)
        self.assertIn("260925", msg)
        self.assertIn("$500", msg)  # нога = capital/2


if __name__ == "__main__":
    unittest.main()
