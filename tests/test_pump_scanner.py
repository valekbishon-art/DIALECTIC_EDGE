"""Юнит-тесты ядра памп-сканера (сеть НЕ нужна)."""

import unittest

from pump_scanner import (
    PumpConfig,
    PumpSignal,
    evaluate_pump,
    format_pump_alert,
    max_rise_pct,
    merge_universes,
    passes_static_filters,
    pct_change,
    trade_url,
    volume_ratio,
    window_anchor_price,
    window_pump_pct,
)
from pump_scanner import _Ticker  # noqa: E402  (внутренний для теста merge)


class TestMath(unittest.TestCase):
    def test_pct_change(self):
        self.assertAlmostEqual(pct_change(0.2268, 0.2441), 7.6279, places=3)
        self.assertEqual(pct_change(0, 1), 0.0)
        self.assertEqual(pct_change(None, 1), 0.0)
        self.assertEqual(pct_change(1, None), 0.0)

    def test_window_pump_pct(self):
        self.assertAlmostEqual(window_pump_pct([1.0, 1.02, 1.05, 1.10], 3), 10.0, places=4)
        self.assertAlmostEqual(window_pump_pct([2.0, 2.2], 30), 10.0, places=4)
        self.assertEqual(window_pump_pct([], 30), 0.0)
        self.assertEqual(window_pump_pct([1.0], 30), 0.0)

    def test_window_anchor_price(self):
        self.assertEqual(window_anchor_price([1.0, 1.05, 1.10], 2), 1.0)
        self.assertEqual(window_anchor_price([5.0], 30), 5.0)
        self.assertIsNone(window_anchor_price([], 30))

    def test_volume_ratio(self):
        self.assertAlmostEqual(volume_ratio(900, 14400, 30), 3.0, places=4)
        self.assertEqual(volume_ratio(900, 0, 30), 0.0)
        self.assertEqual(volume_ratio(900, 14400, 0), 0.0)

    def test_max_rise_pct(self):
        self.assertAlmostEqual(max_rise_pct([1.0, 0.95, 1.0, 1.08]), 13.6842, places=3)
        self.assertEqual(max_rise_pct([2.0, 1.5, 1.0]), 0.0)
        self.assertEqual(max_rise_pct([]), 0.0)


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.cfg = PumpConfig()

    def test_price_floor(self):
        self.assertFalse(passes_static_filters(0.0005, None, cfg=self.cfg))
        self.assertTrue(passes_static_filters(0.5, None, cfg=self.cfg))

    def test_mcap_range(self):
        self.assertTrue(passes_static_filters(1.0, 100_000_000, cfg=self.cfg))
        self.assertFalse(passes_static_filters(1.0, 5_000_000, cfg=self.cfg))
        self.assertFalse(passes_static_filters(1.0, 900_000_000, cfg=self.cfg))

    def test_unknown_mcap_allowed(self):
        self.assertTrue(passes_static_filters(1.0, None, cfg=self.cfg))


def _series(start, pct, n=31):
    end = start * (1 + pct / 100.0)
    return [start + (end - start) * i / (n - 1) for i in range(n)]


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.cfg = PumpConfig()

    def test_all_conditions_pass(self):
        closes = _series(1.0, 7.0)
        vols = [10.0] * 31
        is_pump, m, fails = evaluate_pump(
            closes, vols, vol_24h_total=4000.0,
            daily_closes=[1.0, 1.02, 1.0],
            price=closes[-1], mcap=100_000_000, cfg=self.cfg)
        self.assertTrue(is_pump, fails)
        self.assertEqual(fails, [])
        self.assertGreaterEqual(m.pump_pct, 5.0)
        self.assertGreaterEqual(m.vol_ratio, 3.0)

    def test_fail_low_pump(self):
        closes = _series(1.0, 2.0)
        is_pump, m, fails = evaluate_pump(
            closes, [10.0] * 31, 4000.0, [1.0, 1.0, 1.0],
            price=closes[-1], mcap=100_000_000, cfg=self.cfg)
        self.assertFalse(is_pump)
        self.assertIn("pump_pct", fails)

    def test_fail_low_volume(self):
        closes = _series(1.0, 7.0)
        is_pump, m, fails = evaluate_pump(
            closes, [10.0] * 31, vol_24h_total=1_000_000.0,
            daily_closes=[1.0, 1.0, 1.0],
            price=closes[-1], mcap=100_000_000, cfg=self.cfg)
        self.assertFalse(is_pump)
        self.assertIn("vol_ratio", fails)

    def test_fail_already_heated(self):
        closes = _series(1.0, 7.0)
        is_pump, m, fails = evaluate_pump(
            closes, [10.0] * 31, 4000.0,
            daily_closes=[1.0, 1.30, 1.25],
            price=closes[-1], mcap=100_000_000, cfg=self.cfg)
        self.assertFalse(is_pump)
        self.assertIn("already_heated", fails)

    def test_fail_static_filter_price(self):
        closes = _series(0.0001, 7.0)
        is_pump, m, fails = evaluate_pump(
            closes, [10.0] * 31, 4000.0, [1.0, 1.0, 1.0],
            price=closes[-1], mcap=100_000_000, cfg=self.cfg)
        self.assertFalse(is_pump)
        self.assertIn("static_filters", fails)


class TestSignalAndUrls(unittest.TestCase):
    def test_trade_url(self):
        self.assertEqual(trade_url("Bybit", "0PN"),
                         "https://www.bybit.com/en/trade/spot/0PN/USDT")
        self.assertEqual(trade_url("MEXC", "0PN"),
                         "https://www.mexc.com/exchange/0PN_USDT")
        self.assertIsNone(trade_url("UnknownExch", "0PN"))

    def test_venue_buttons(self):
        sig = PumpSignal(asset="0PN", pump_pct=7.63, vol_ratio=3.2,
                         prior_pct=1.0, price_from=0.2268, price_to=0.2441,
                         window_min=30, venues=["Bybit", "MEXC"])
        btns = sig.venue_buttons()
        labels = [b[0] for b in btns]
        self.assertIn("Биржа BYBIT", labels)
        self.assertIn("Биржа MEXC", labels)
        for _, url in btns:
            self.assertTrue(url.startswith("https://"))

    def test_format_alert(self):
        sig = PumpSignal(asset="0PN", pump_pct=7.63, vol_ratio=3.2,
                         prior_pct=1.0, price_from=0.2268, price_to=0.2441,
                         window_min=30, venues=["Bybit", "MEXC"],
                         mcap=120_000_000)
        txt = format_pump_alert(sig)
        self.assertIn("0PN", txt)
        self.assertIn("7.63%", txt)
        self.assertIn("30", txt)
        self.assertIn("x3.2", txt)


class TestMergeUniverses(unittest.TestCase):
    def test_merge(self):
        a = {"AAA": _Ticker("AAA", 1.0, 100.0, {"Binance"})}
        b = {"AAA": _Ticker("AAA", 1.01, 500.0, {"Bybit"}),
             "BBB": _Ticker("BBB", 2.0, 50.0, {"MEXC"})}
        merged = merge_universes(a, b)
        self.assertEqual(merged["AAA"].venues, {"Binance", "Bybit"})
        self.assertAlmostEqual(merged["AAA"].quote_vol_24h, 500.0)
        self.assertAlmostEqual(merged["AAA"].price, 1.01)
        self.assertEqual(merged["BBB"].venues, {"MEXC"})


if __name__ == "__main__":
    unittest.main()
