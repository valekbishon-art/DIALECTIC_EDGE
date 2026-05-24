"""Tests for market_indicators/btc_etf_flows.py."""

import asyncio
import os
import unittest

from market_indicators.btc_etf_flows import (
    EtfDaily,
    _parse_yahoo_chart,
    aggregate_basket_flows,
    detect_outflow_signal,
    feature_enabled,
    fetch_btc_etf_dailies,
    get_outflow_day_pct,
    get_outflow_streak_days,
    get_tickers,
)


def _run(coro):
    return asyncio.run(coro)


def _yahoo_payload(closes, volumes=None):
    if volumes is None:
        volumes = [100_000] * len(closes)
    return {
        "chart": {
            "result": [
                {
                    "indicators": {
                        "quote": [
                            {
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    }
                }
            ]
        }
    }


class YahooParseTests(unittest.TestCase):
    def test_parse_happy(self):
        payload = _yahoo_payload([100.0, 102.0, 101.0])
        rows = _parse_yahoo_chart(payload, "IBIT")
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0].change_pct, 2.0)
        self.assertAlmostEqual(rows[1].change_pct, -100.0 / 102.0, places=4)
        self.assertEqual(rows[0].ticker, "IBIT")

    def test_parse_empty(self):
        self.assertEqual(_parse_yahoo_chart({}, "IBIT"), [])
        self.assertEqual(
            _parse_yahoo_chart({"chart": {"result": []}}, "IBIT"), []
        )

    def test_parse_skips_nones(self):
        payload = _yahoo_payload([100.0, None, 102.0])
        rows = _parse_yahoo_chart(payload, "IBIT")
        # nones removed first → effectively [100, 102]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].change_pct, 2.0)


class AggregateBasketTests(unittest.TestCase):
    def test_aggregate_basket_flows(self):
        rows = [
            EtfDaily("IBIT", 100.0, 99.0, 1000, 1.0),
            EtfDaily("IBIT", 99.0, 100.0, 1000, -1.0),
            EtfDaily("FBTC", 50.0, 49.0, 500, 2.0),
            EtfDaily("FBTC", 48.0, 50.0, 500, -4.0),
        ]
        basket = aggregate_basket_flows(rows)
        self.assertEqual(len(basket), 2)
        self.assertAlmostEqual(basket[0]["avg_change_pct"], 1.5)
        self.assertAlmostEqual(basket[1]["avg_change_pct"], -2.5)
        self.assertEqual(basket[0]["tickers_seen"], ("IBIT", "FBTC"))

    def test_aggregate_basket_uneven_series(self):
        rows = [
            EtfDaily("IBIT", 100.0, 99.0, 1000, 1.0),
            EtfDaily("IBIT", 99.0, 100.0, 1000, -1.0),
            EtfDaily("FBTC", 50.0, 49.0, 500, 2.0),
        ]
        basket = aggregate_basket_flows(rows)
        # min_len = 1 → only first day used
        self.assertEqual(len(basket), 1)


class DetectOutflowSignalTests(unittest.TestCase):
    def test_no_signal_on_quiet_basket(self):
        basket = [
            {"day_idx": 0, "avg_change_pct": 0.2, "tickers_seen": ("IBIT",), "volumes_sum": 0},
            {"day_idx": 1, "avg_change_pct": -0.5, "tickers_seen": ("IBIT",), "volumes_sum": 0},
            {"day_idx": 2, "avg_change_pct": 0.3, "tickers_seen": ("IBIT",), "volumes_sum": 0},
        ]
        self.assertIsNone(detect_outflow_signal(basket))

    def test_streak_triggers_warn(self):
        basket = [
            {"day_idx": i, "avg_change_pct": -2.0, "tickers_seen": ("IBIT",), "volumes_sum": 0}
            for i in range(4)
        ]
        sig = detect_outflow_signal(basket)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.severity, "WARN")
        self.assertGreaterEqual(sig.streak_days, 3)

    def test_big_drop_triggers_crit(self):
        basket = [
            {"day_idx": 0, "avg_change_pct": 0.1, "tickers_seen": ("IBIT",), "volumes_sum": 0},
            {"day_idx": 1, "avg_change_pct": -5.5, "tickers_seen": ("IBIT",), "volumes_sum": 0},
            {"day_idx": 2, "avg_change_pct": 0.2, "tickers_seen": ("IBIT",), "volumes_sum": 0},
        ]
        sig = detect_outflow_signal(basket)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.severity, "CRIT")
        self.assertLessEqual(sig.worst_day_pct, -5.0)

    def test_streak_broken_by_up_day_does_not_count(self):
        basket = [
            {"day_idx": 0, "avg_change_pct": -2.0, "tickers_seen": ("IBIT",), "volumes_sum": 0},
            {"day_idx": 1, "avg_change_pct": -2.0, "tickers_seen": ("IBIT",), "volumes_sum": 0},
            {"day_idx": 2, "avg_change_pct": 0.5, "tickers_seen": ("IBIT",), "volumes_sum": 0},
            {"day_idx": 3, "avg_change_pct": -2.0, "tickers_seen": ("IBIT",), "volumes_sum": 0},
        ]
        # latest streak is 1 → no WARN; no big drop → no CRIT
        self.assertIsNone(detect_outflow_signal(basket))

    def test_explicit_thresholds_override(self):
        basket = [
            {"day_idx": i, "avg_change_pct": -1.0, "tickers_seen": ("IBIT",), "volumes_sum": 0}
            for i in range(5)
        ]
        sig = detect_outflow_signal(
            basket, outflow_day_pct=0.5, streak_days=2, big_day_drop_pct=10.0
        )
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.severity, "WARN")


class FetchDailiesTests(unittest.TestCase):
    def test_fetch_uses_injected_http_get(self):
        captured: list[str] = []

        async def fake_http_get(url, params):
            captured.append(url)
            return _yahoo_payload([100.0, 101.0, 99.0], [1000, 1000, 2000])

        rows = _run(
            fetch_btc_etf_dailies(
                http_get=fake_http_get,
                tickers=("IBIT", "FBTC"),
                lookback_days=3,
            )
        )
        # Two tickers × 2 daily rows each (3 closes -> 2 deltas)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(captured), 2)
        self.assertTrue(any("IBIT" in u for u in captured))
        self.assertTrue(any("FBTC" in u for u in captured))

    def test_fetch_skips_failing_tickers(self):
        async def fake_http_get(url, params):
            if "FBTC" in url:
                return None
            return _yahoo_payload([100.0, 101.0])

        rows = _run(
            fetch_btc_etf_dailies(
                http_get=fake_http_get,
                tickers=("IBIT", "FBTC"),
                lookback_days=3,
            )
        )
        self.assertTrue(all(r.ticker == "IBIT" for r in rows))


class EnvConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for k in (
            "FEATURE_ALERT_BTC_ETF",
            "ALERT_BTC_ETF_TICKERS",
            "ALERT_BTC_ETF_OUTFLOW_DAY_PCT",
            "ALERT_BTC_ETF_STREAK_DAYS",
        ):
            self._saved[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_feature_default_on(self):
        self.assertTrue(feature_enabled())

    def test_tickers_env_override(self):
        os.environ["ALERT_BTC_ETF_TICKERS"] = " ibit , fbtc "
        self.assertEqual(get_tickers(), ("IBIT", "FBTC"))

    def test_streak_clamped(self):
        os.environ["ALERT_BTC_ETF_STREAK_DAYS"] = "0"
        self.assertEqual(get_outflow_streak_days(), 2)
        os.environ["ALERT_BTC_ETF_STREAK_DAYS"] = "999"
        self.assertEqual(get_outflow_streak_days(), 14)

    def test_outflow_day_default(self):
        self.assertGreater(get_outflow_day_pct(), 0)


if __name__ == "__main__":
    unittest.main()
