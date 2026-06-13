"""Гарды целостности данных пампового сканера (тотальный фикс фантомов/коллизий).

Покрывает три источника «алертов на плохих данных», вскрытых на реальных
делистнутых парах (CLV/BAKE/FIRO/LTO/LIT, 2026-06):
  1) свежесть свечи  → kline_is_fresh (делистнутые отдают свечи многомесячной
     давности как «текущие»);
  2) коллизия тикеров → merge_universes отбрасывает venue другого токена с тем
     же тикером (LIT: Binance Litentry ~0.74 vs MEXC LIT ~1.60);
  3) согласованность цены → price_from/price_to/pump_pct из ОДНОГО ряда свечей
     (раньше price_to склеивался с ценой другого venue → 0.677→1.6042).
"""
import unittest

from pump_scanner import (
    PumpConfig,
    _Ticker,
    evaluate_pump,
    kline_is_fresh,
    merge_universes,
)

MIN = 60_000  # мс в минуте


class TestKlineFreshness(unittest.TestCase):
    def test_fresh_recent(self):
        now = 1_000_000_000_000
        self.assertTrue(kline_is_fresh(now - 1 * MIN, now, max_age_min=30.0))

    def test_stale_delisted(self):
        now = 1_000_000_000_000
        # свеча 120 дней назад (типичный делистнутый фантом) → отвергнуть
        self.assertFalse(
            kline_is_fresh(now - 120 * 24 * 60 * MIN, now, max_age_min=30.0))

    def test_just_over_threshold(self):
        now = 1_000_000_000_000
        self.assertTrue(kline_is_fresh(now - 30 * MIN, now, max_age_min=30.0))
        self.assertFalse(kline_is_fresh(now - 31 * MIN, now, max_age_min=30.0))

    def test_none_and_zero(self):
        now = 1_000_000_000_000
        self.assertFalse(kline_is_fresh(None, now, max_age_min=30.0))
        self.assertFalse(kline_is_fresh(0, now, max_age_min=30.0))

    def test_minor_clock_skew_ok(self):
        # последняя свеча «в будущем» на минуту из-за рассинхрона часов — ок
        now = 1_000_000_000_000
        self.assertTrue(kline_is_fresh(now + 1 * MIN, now, max_age_min=30.0))


class TestMergeCollision(unittest.TestCase):
    def test_divergent_token_dropped(self):
        # LIT: Binance Litentry (делист, стейл) 0.743 малый объём
        #      vs MEXC LIT (другой токен) 1.60 большой объём.
        binance = {"LIT": _Ticker("LIT", 0.743, 100.0, {"Binance"}, "Binance")}
        mexc = {"LIT": _Ticker("LIT", 1.6042, 5000.0, {"MEXC"}, "MEXC")}
        merged = merge_universes(binance, mexc, price_tol=0.15)["LIT"]
        # канон = самый ликвидный (MEXC); расходящийся Binance-leg отброшен
        self.assertEqual(merged.price, 1.6042)
        self.assertEqual(merged.venues, {"MEXC"})
        self.assertEqual(merged.primary_venue, "MEXC")

    def test_coherent_token_unioned(self):
        # один и тот же токен, цены близки → объединяем venues
        a = {"AAA": _Ticker("AAA", 1.00, 1000.0, {"Binance"}, "Binance")}
        b = {"AAA": _Ticker("AAA", 1.005, 5000.0, {"MEXC"}, "MEXC")}
        merged = merge_universes(a, b, price_tol=0.15)["AAA"]
        self.assertEqual(merged.venues, {"Binance", "MEXC"})
        self.assertEqual(merged.primary_venue, "MEXC")  # max-объём
        self.assertEqual(merged.price, 1.005)

    def test_three_venues_one_imposter(self):
        # 2 согласованных + 1 чужой токен с тем же тикером
        a = {"X": _Ticker("X", 2.00, 800.0, {"Binance"}, "Binance")}
        b = {"X": _Ticker("X", 2.02, 9000.0, {"Bybit"}, "Bybit")}
        c = {"X": _Ticker("X", 5.00, 300.0, {"MEXC"}, "MEXC")}  # другой токен
        merged = merge_universes(a, b, c, price_tol=0.15)["X"]
        self.assertEqual(merged.venues, {"Binance", "Bybit"})
        self.assertEqual(merged.primary_venue, "Bybit")
        self.assertNotIn("MEXC", merged.venues)


class TestPriceConsistency(unittest.TestCase):
    def test_price_to_from_same_series(self):
        # без аргумента price: price_to == последний close тех же свечей,
        # price_from == якорь начала окна → согласованный «памп».
        cfg = PumpConfig(window_min=3, min_pct=5.0, vol_mult=1.0,
                         max_prior_pct=1e9, price_floor=0.0,
                         mcap_min=0.0, mcap_max=1e15)
        closes = [1.00, 1.02, 1.05, 1.10]   # +10% по окну
        vols = [10.0, 10.0, 10.0, 10.0]
        _, m, _ = evaluate_pump(closes, vols, 1000.0, [1.0, 1.0], cfg=cfg)
        self.assertEqual(m.price_to, closes[-1])
        self.assertEqual(m.price_from, closes[-1 - cfg.window_candles])
        # pump_pct согласован с price_from→price_to
        expect = (m.price_to / m.price_from - 1.0) * 100.0
        self.assertAlmostEqual(m.pump_pct, expect, places=6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
