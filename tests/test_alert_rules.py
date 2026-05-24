"""Tests for individual alert rules under refactor/services/alert_rules/."""

import asyncio
import os
import unittest
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from refactor.services.alert_engine import AlertCard
from refactor.services.alert_rules.btc_etf_outflow import BtcEtfOutflowRule
from refactor.services.alert_rules.liquidation_cluster import LiquidationClusterRule
from refactor.services.alert_rules.screener_anomaly import (
    ScreenerAnomalyRule,
    _is_high_conviction,
    _severity_for,
)


def _run(coro):
    return asyncio.run(coro)


# ─── Screener anomaly rule ────────────────────────────────────────────────────


class ScreenerHighConvictionTests(unittest.TestCase):
    def test_lone_signal_not_high_conviction(self):
        item = {"signals": ["🔥 Объем x2.0"], "vol_spike": 2.0}
        self.assertFalse(_is_high_conviction(item))

    def test_two_signals_no_strong_threshold_not_high(self):
        item = {
            "signals": ["📈 RSI", "🔥 Объем x2.0"],
            "rsi": 75.0,
            "vol_spike": 2.0,
            "funding": 0.0005,
        }
        self.assertFalse(_is_high_conviction(item))

    def test_two_signals_with_strong_vol_is_high(self):
        item = {
            "signals": ["📈 RSI", "🔥 Объем"],
            "rsi": 75.0,
            "vol_spike": 3.5,
            "funding": 0.0005,
        }
        self.assertTrue(_is_high_conviction(item))

    def test_two_signals_with_extreme_rsi_is_high(self):
        item = {
            "signals": ["📉 RSI", "🔥 Объем"],
            "rsi": 18.0,
            "vol_spike": 2.0,
        }
        self.assertTrue(_is_high_conviction(item))

    def test_severity_crit_on_two_strong(self):
        item = {
            "signals": ["📈 RSI", "🔥 Объем"],
            "rsi": 85.0,
            "vol_spike": 5.0,
            "funding": 0.001,
        }
        self.assertEqual(_severity_for(item), "CRIT")

    def test_severity_warn_on_single_strong(self):
        item = {
            "signals": ["📈 RSI", "🔥 Объем"],
            "rsi": 85.0,
            "vol_spike": 2.5,
        }
        self.assertEqual(_severity_for(item), "WARN")


class ScreenerRuleCheckTests(unittest.TestCase):
    def setUp(self):
        os.environ["FEATURE_ALERT_SCREENER"] = "1"

    def tearDown(self):
        os.environ.pop("FEATURE_ALERT_SCREENER", None)

    def test_filters_low_conviction(self):
        class FakeScanner:
            def __init__(self, top_n):
                self.top_n = top_n

            async def scan(self):
                return [
                    {
                        "symbol": "BTC",
                        "signals": ["📈 RSI"],
                        "rsi": 71.0,
                    },
                    {
                        "symbol": "ETH",
                        "signals": ["📈 RSI", "🔥 Объем"],
                        "rsi": 82.0,
                        "vol_spike": 4.2,
                    },
                ]

        with patch("core.screener.MarketScreener", FakeScanner):
            rule = ScreenerAnomalyRule.build()
            cards = _run(rule.check())
        self.assertEqual(len(cards), 1)
        self.assertIn("ETH", cards[0].title)

    def test_disabled_returns_empty(self):
        os.environ["FEATURE_ALERT_SCREENER"] = "0"
        rule = ScreenerAnomalyRule.build()
        self.assertEqual(_run(rule.check()), [])


# ─── BTC ETF outflow rule ─────────────────────────────────────────────────────


def _yahoo_payload(closes):
    return {
        "chart": {
            "result": [
                {
                    "indicators": {
                        "quote": [{"close": closes, "volume": [1000] * len(closes)}]
                    }
                }
            ]
        }
    }


class BtcEtfOutflowRuleTests(unittest.TestCase):
    def setUp(self):
        os.environ["FEATURE_ALERT_BTC_ETF"] = "1"

    def tearDown(self):
        os.environ.pop("FEATURE_ALERT_BTC_ETF", None)

    def test_outflow_streak_emits_warn(self):
        async def fake_http_get(url, params):
            # series 100 -> 97 -> 94 -> 91 -> 88 ≈ −3% per day
            return _yahoo_payload([100.0, 97.0, 94.0, 91.0, 88.0])

        rule = BtcEtfOutflowRule(
            cooldown_sec=600,
            _http_get=fake_http_get,
        )
        cards = _run(rule.check())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].severity, "WARN")
        self.assertIn("BTC ETF", cards[0].title)

    def test_big_drop_emits_crit(self):
        async def fake_http_get(url, params):
            return _yahoo_payload([100.0, 100.0, 95.0])

        rule = BtcEtfOutflowRule(cooldown_sec=600, _http_get=fake_http_get)
        cards = _run(rule.check())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].severity, "CRIT")

    def test_quiet_basket_no_alert(self):
        async def fake_http_get(url, params):
            return _yahoo_payload([100.0, 100.1, 99.9, 100.0])

        rule = BtcEtfOutflowRule(cooldown_sec=600, _http_get=fake_http_get)
        self.assertEqual(_run(rule.check()), [])

    def test_disabled_returns_empty(self):
        os.environ["FEATURE_ALERT_BTC_ETF"] = "0"

        async def fake_http_get(url, params):
            return _yahoo_payload([100.0, 90.0])

        rule = BtcEtfOutflowRule(cooldown_sec=600, _http_get=fake_http_get)
        self.assertEqual(_run(rule.check()), [])


# ─── Liquidation cluster rule ─────────────────────────────────────────────────


@dataclass
class _FakeSignal:
    label: str
    is_strong_signal: bool
    top_long_short_ratio: float | None
    oi_change_pct: float
    venue: str = "binance"
    symbol: str = "BTCUSDT"
    oi_lookback_hours: int = 24


class LiquidationClusterRuleTests(unittest.TestCase):
    def setUp(self):
        os.environ["FEATURE_ALERT_LIQUIDATION_CLUSTER"] = "1"

    def tearDown(self):
        os.environ.pop("FEATURE_ALERT_LIQUIDATION_CLUSTER", None)

    def _build_rule(self, signal: _FakeSignal | None):
        async def fake_fetch(*args: Any, **kwargs: Any):
            if signal is None:
                raise RuntimeError("data unavailable")
            return signal

        return LiquidationClusterRule(cooldown_sec=600, _fetch_signal=fake_fetch)

    def test_down_magnet_strong_is_crit(self):
        sig = _FakeSignal(
            label="down_magnet",
            is_strong_signal=True,
            top_long_short_ratio=3.5,
            oi_change_pct=12.0,
        )
        rule = self._build_rule(sig)
        cards = _run(rule.check())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].severity, "CRIT")
        self.assertIn("Liquidation magnet", cards[0].title)
        self.assertIn("BTCUSDT", cards[0].body)

    def test_up_magnet_soft_is_warn(self):
        sig = _FakeSignal(
            label="up_magnet",
            is_strong_signal=False,
            top_long_short_ratio=0.4,
            oi_change_pct=6.0,
        )
        rule = self._build_rule(sig)
        cards = _run(rule.check())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].severity, "WARN")

    def test_neutral_label_no_card(self):
        sig = _FakeSignal(
            label="neutral",
            is_strong_signal=False,
            top_long_short_ratio=1.1,
            oi_change_pct=0.5,
        )
        rule = self._build_rule(sig)
        self.assertEqual(_run(rule.check()), [])

    def test_unknown_label_no_card(self):
        sig = _FakeSignal(
            label="unknown",
            is_strong_signal=False,
            top_long_short_ratio=None,
            oi_change_pct=0.0,
        )
        rule = self._build_rule(sig)
        self.assertEqual(_run(rule.check()), [])

    def test_fetch_exception_no_crash(self):
        rule = self._build_rule(None)
        self.assertEqual(_run(rule.check()), [])

    def test_disabled_returns_empty(self):
        os.environ["FEATURE_ALERT_LIQUIDATION_CLUSTER"] = "0"
        sig = _FakeSignal(
            label="down_magnet",
            is_strong_signal=True,
            top_long_short_ratio=3.5,
            oi_change_pct=12.0,
        )
        rule = self._build_rule(sig)
        self.assertEqual(_run(rule.check()), [])

    def test_card_dedup_key_distinguishes_strength(self):
        sig_strong = _FakeSignal(
            label="down_magnet",
            is_strong_signal=True,
            top_long_short_ratio=3.5,
            oi_change_pct=12.0,
        )
        sig_soft = _FakeSignal(
            label="down_magnet",
            is_strong_signal=False,
            top_long_short_ratio=2.0,
            oi_change_pct=4.0,
        )
        c1 = _run(self._build_rule(sig_strong).check())[0]
        c2 = _run(self._build_rule(sig_soft).check())[0]
        self.assertNotEqual(c1.dedup_key, c2.dedup_key)


if __name__ == "__main__":
    unittest.main()
