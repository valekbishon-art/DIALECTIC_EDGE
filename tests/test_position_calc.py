"""Тесты калькулятора позиции и track-record (чистая математика, без сети)."""
from __future__ import annotations

import os
import tempfile
import unittest

from core.position_calc import calc_position, format_calc_md, COST_CARRY
from core.track_record import summarize, format_track_md


class CalcTest(unittest.TestCase):
    def test_legs_equal_half_capital(self):
        p = calc_position(1000, 20, kind="carry")
        self.assertEqual(p.leg_usd, 500)

    def test_gross_and_net(self):
        # 1000 депо, 20% год, carry. Нога 500. Гросс = 500*0.20 = 100/год.
        p = calc_position(1000, 20, kind="carry")
        self.assertAlmostEqual(p.gross_year, 100.0)
        self.assertAlmostEqual(p.cost_usd, 500 * COST_CARRY)   # 500*0.004=2
        self.assertAlmostEqual(p.net_year, 100.0 - 2.0)

    def test_net_annual_pct(self):
        p = calc_position(1000, 20, kind="carry")
        self.assertAlmostEqual(p.net_annual_pct, (98.0 / 1000) * 100)

    def test_negative_rate_uses_abs(self):
        # отрицательный фандинг (обратный carry) — доход по модулю
        p = calc_position(1000, -30, kind="carry")
        self.assertAlmostEqual(p.gross_year, 500 * 0.30)

    def test_arb_higher_cost(self):
        carry = calc_position(1000, 40, kind="carry")
        arb = calc_position(1000, 40, kind="arb")
        self.assertGreater(arb.cost_usd, carry.cost_usd)  # 2 биржи дороже

    def test_breakeven_positive(self):
        p = calc_position(1000, 20, kind="carry")
        self.assertGreater(p.breakeven_days, 0)
        self.assertLess(p.breakeven_days, 365)

    def test_format_has_legs_and_yield(self):
        p = calc_position(5000, 50, kind="arb")
        msg = format_calc_md(p, kind="arb", asset="FET")
        self.assertIn("$2,500", msg)        # нога = 5000/2
        self.assertIn("FET", msg)
        self.assertIn("ШОРТ перп", msg)
        self.assertIn("ЛОНГ перп", msg)


class TrackRecordTest(unittest.TestCase):
    def test_empty_when_no_file(self):
        self.assertEqual(summarize("/nonexistent/xxx.csv"), {})

    def test_summary_from_csv(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8") as f:
            f.write("ts_utc,symbol,annual_pct,rate,interval_h,side\n")
            f.write("2026-06-01 10:00:00,BTCUSDT,25.0,0.0001,8,long_spot_short_perp\n")
            f.write("2026-06-01 16:00:00,ETHUSDT,35.0,0.0002,8,long_spot_short_perp\n")
            f.write("2026-06-02 10:00:00,BTCUSDT,30.0,0.0001,8,long_spot_short_perp\n")
            path = f.name
        try:
            s = summarize(path)
            self.assertEqual(s["total_windows"], 3)
            self.assertEqual(s["days_tracked"], 2)
            self.assertAlmostEqual(s["avg_annual"], 30.0)
            self.assertEqual(s["max_annual"], 35.0)
            self.assertEqual(s["top_assets"][0][0], "BTCUSDT")  # чаще всего
        finally:
            os.unlink(path)

    def test_format_empty(self):
        self.assertIn("пусто", format_track_md({}))

    def test_format_nonempty(self):
        s = summarize.__wrapped__ if hasattr(summarize, "__wrapped__") else None
        msg = format_track_md({
            "total_windows": 10, "days_tracked": 3, "first_day": "2026-06-01",
            "last_day": "2026-06-03", "avg_annual": 28.0, "max_annual": 55.0,
            "windows_per_day": 3.3, "top_assets": [("BTCUSDT", 5), ("ETHUSDT", 3)],
        })
        self.assertIn("28% годовых", msg)
        self.assertIn("BTCUSDT", msg)


if __name__ == "__main__":
    unittest.main()
