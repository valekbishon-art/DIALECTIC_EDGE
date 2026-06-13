"""Юнит-тесты ядра памп-сканера (сеть НЕ нужна)."""

import unittest

from pump_scanner import (  # noqa: F401
    _pair_links,
    _build_mcap_map,
    PumpConfig,
    PumpSignal,
    classify_signal,
    early_pump_score,
    evaluate_pump,
    format_pump_alert,
    max_rise_pct,
    merge_universes,
    momentum_acceleration,
    passes_static_filters,
    pct_change,
    trade_url,
    volume_ramp,
    volume_ratio,
    window_anchor_price,
    window_pump_pct,
)
from pump_scanner import _Ticker, _slope  # noqa: E402


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
            closes, [10.0] * 31, 4000.0, [0.0001, 0.0001, 0.0001],
            price=closes[-1], mcap=100_000_000, cfg=self.cfg)
        self.assertFalse(is_pump)
        self.assertIn("static_filters", fails)


class TestPredictive(unittest.TestCase):
    def test_slope_sign(self):
        self.assertGreater(_slope([1, 2, 3, 4]), 0)
        self.assertLess(_slope([4, 3, 2, 1]), 0)
        self.assertAlmostEqual(_slope([5, 5, 5]), 0.0)
        self.assertEqual(_slope([1]), 0.0)

    def test_momentum_acceleration_positive_when_accelerating(self):
        # плоско потом резкий рост -> ускорение > 0
        closes = [1.0] * 15 + [1.0, 1.01, 1.03, 1.06, 1.10]
        self.assertGreater(momentum_acceleration(closes), 0)

    def test_momentum_acceleration_short_series(self):
        self.assertEqual(momentum_acceleration([1.0, 1.0]), 0.0)

    def test_volume_ramp(self):
        vols = [10.0] * 30 + [30.0] * 5
        self.assertGreater(volume_ramp(vols), 1.5)
        self.assertEqual(volume_ramp([]), 0.0)

    def test_early_score_range_and_high_when_forming(self):
        # рост ~4% (ниже порога 5%) + растущий объём -> высокий скор
        closes = _series(1.0, 4.0)
        vols = [10.0] * 26 + [40.0] * 5
        score, accel, vramp = early_pump_score(closes, vols, cfg=PumpConfig())
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)
        self.assertGreater(score, 0.5)
        self.assertGreater(vramp, 1.0)

    def test_early_score_low_when_flat(self):
        closes = [1.0] * 31
        vols = [10.0] * 31
        score, _, _ = early_pump_score(closes, vols, cfg=PumpConfig())
        self.assertLess(score, 0.3)

    def test_classify_signal(self):
        self.assertEqual(classify_signal(True, 0.0, []), "pump")
        self.assertEqual(
            classify_signal(False, 0.7, ["pump_pct", "vol_ratio"]), "early")
        # разогретая монета не early даже при высоком скоре
        self.assertEqual(
            classify_signal(False, 0.9, ["already_heated"]), "none")
        self.assertEqual(classify_signal(False, 0.1, ["pump_pct"]), "none")

    def test_evaluate_attaches_predictive(self):
        closes = _series(1.0, 4.0)
        vols = [10.0] * 26 + [40.0] * 5
        is_pump, m, fails = evaluate_pump(
            closes, vols, 4000.0, [1.0, 1.0, 1.0],
            price=closes[-1], mcap=100_000_000, cfg=PumpConfig())
        self.assertFalse(is_pump)  # ниже 5%
        self.assertGreaterEqual(m.predictive_score, 0.0)
        self.assertLessEqual(m.predictive_score, 1.0)


class TestSignalAndUrls(unittest.TestCase):
    def test_trade_url(self):
        self.assertEqual(
            trade_url("Bybit", "0PN"),
            "https://www.bybit.com/en/trade/spot/0PN/USDT")
        self.assertEqual(
            trade_url("MEXC", "0PN"),
            "https://www.mexc.com/exchange/0PN_USDT")
        self.assertEqual(
            trade_url("Binance", "0PN"),
            "https://www.binance.com/en/trade/0PN_USDT")
        self.assertIsNone(trade_url("NoSuchEx", "0PN"))

    def test_format_alert(self):
        sig = PumpSignal(
            asset="0PN", pump_pct=7.63, vol_ratio=3.2, prior_pct=2.0,
            price_from=0.2268, price_to=0.2441, window_min=30,
            venues=["Bybit", "MEXC"], mcap=50_000_000)
        text = format_pump_alert(sig)
        self.assertIn("0PN", text)
        self.assertIn("7.63%", text)
        self.assertIn("30", text)
        self.assertIn("x3.2", text)

    def test_format_alert_early_tier(self):
        sig = PumpSignal(
            asset="ABC", pump_pct=3.5, vol_ratio=1.8, prior_pct=1.0,
            price_from=1.0, price_to=1.035, window_min=30,
            venues=["Binance"], tier="early", predictive_score=0.66,
            vol_ramp=2.4, accel=0.12)
        text = format_pump_alert(sig)
        self.assertIn("ABC", text)
        self.assertIn("разогрев", text)
        self.assertIn("66%", text)

    def test_format_alert_has_exact_pair_link(self):
        # Алерт должен нести ссылку на ТОЧНУЮ пару (анти-омоним тикеров)
        sig = PumpSignal(
            asset="PAI", pump_pct=8.8, vol_ratio=340.0, prior_pct=1.0,
            price_from=0.003365, price_to=0.003653, window_min=30,
            venues=["MEXC"])
        text = format_pump_alert(sig)
        self.assertIn("PAI/USDT", text)
        self.assertIn("mexc.com/exchange/PAI_USDT", text)

    def test_pair_links_mexc_first(self):
        sig = PumpSignal(
            asset="ABC", pump_pct=6.0, vol_ratio=4.0, prior_pct=1.0,
            price_from=1.0, price_to=1.06, window_min=30,
            venues=["Bybit", "MEXC"])
        links = _pair_links(sig)
        self.assertTrue(links.index("MEXC") < links.index("BYBIT"))

    def test_venue_buttons(self):
        sig = PumpSignal(
            asset="0PN", pump_pct=7.6, vol_ratio=3.2, prior_pct=2.0,
            price_from=0.22, price_to=0.24, window_min=30,
            venues=["Bybit", "MEXC"])
        buttons = sig.venue_buttons()
        labels = [b[0] for b in buttons]
        self.assertIn("Биржа BYBIT", labels)
        self.assertIn("Биржа MEXC", labels)


class TestMergeUniverses(unittest.TestCase):
    def test_merge(self):
        a = {"AAA": _Ticker("AAA", 1.0, 1000.0, {"Binance"})}
        b = {"AAA": _Ticker("AAA", 1.1, 5000.0, {"MEXC"}),
             "BBB": _Ticker("BBB", 2.0, 200.0, {"Bybit"})}
        merged = merge_universes(a, b)
        self.assertEqual(merged["AAA"].venues, {"Binance", "MEXC"})
        # берём цену/объём от более ликвидной биржи
        self.assertEqual(merged["AAA"].quote_vol_24h, 5000.0)
        self.assertEqual(merged["AAA"].price, 1.1)
        self.assertIn("BBB", merged)


class TestMcapMap(unittest.TestCase):
    def test_unique_ticker_kept(self):
        rows = [{"id": "bitcoin", "symbol": "btc", "market_cap": 1.2e12}]
        out = _build_mcap_map(rows)
        self.assertEqual(out.get("BTC"), 1.2e12)

    def test_ambiguous_ticker_dropped(self):
        # две разные монеты с тикером PAI -> mcap неизвестен
        rows = [
            {"id": "parallel-ai", "symbol": "pai", "market_cap": 9.0e6},
            {"id": "project-pai", "symbol": "pai", "market_cap": 3.0e6},
        ]
        out = _build_mcap_map(rows)
        self.assertNotIn("PAI", out)

    def test_zero_and_missing_skipped(self):
        rows = [
            {"id": "x", "symbol": "x", "market_cap": 0},
            {"id": "y", "symbol": "y"},
            {"id": "z", "symbol": "z", "market_cap": 5.0e7},
        ]
        out = _build_mcap_map(rows)
        self.assertNotIn("X", out)
        self.assertNotIn("Y", out)
        self.assertEqual(out.get("Z"), 5.0e7)


class TestCoinGeckoLink(unittest.TestCase):
    def test_alert_has_coingecko_search_link(self):
        sig = PumpSignal(
            asset="PAI", pump_pct=8.8, vol_ratio=340.0, prior_pct=0.0,
            price_from=0.003365, price_to=0.003653, window_min=30,
            venues=["MEXC"])
        txt = format_pump_alert(sig)
        self.assertIn(
            "https://www.coingecko.com/en/search?query=PAI", txt)
        # ссылка не должна быть обёрнута в литеральные фигурные скобки
        self.assertNotIn("(" + chr(123) + "https", txt)


class TestLiquidityFloorConfig(unittest.TestCase):
    def test_default_min_quote_vol(self):
        cfg = PumpConfig()
        self.assertEqual(cfg.min_quote_vol_24h, 300_000.0)

    def test_env_override(self):
        import os
        os.environ["PUMP_MIN_QUOTE_VOL_24H"] = "1000000"
        try:
            cfg = PumpConfig.from_env()
            self.assertEqual(cfg.min_quote_vol_24h, 1_000_000.0)
        finally:
            os.environ.pop("PUMP_MIN_QUOTE_VOL_24H", None)


if __name__ == "__main__":
    unittest.main()
