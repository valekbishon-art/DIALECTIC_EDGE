"""Unit-tests для market_indicators.funding_term_io.

HTTP-клиент полностью замокирован через DI. Без сетевых вызовов.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime
from unittest import mock

from market_indicators.funding_term_io import (
    _parse_binance_deliverable,
    _parse_binance_funding,
    _parse_binance_spot_price,
    _parse_bybit_deliverable,
    _parse_bybit_funding,
    feature_enabled,
    fetch_term_structure,
    format_term_summary,
    get_interval_seconds,
    get_symbols,
)


def _run(coro):
    return asyncio.run(coro)


# ─── Bybit parsers ──────────────────────────────────────────────────────────


class BybitFundingParseTestCase(unittest.TestCase):
    def test_typical(self):
        payload = {"result": {"list": [{
            "symbol": "BTCUSDT", "fundingRate": "0.0001",
            "nextFundingTime": "1737806400000",
        }]}}
        snap = _parse_bybit_funding(payload, asset="BTC")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.venue, "bybit")
        self.assertEqual(snap.asset, "BTC")
        self.assertAlmostEqual(snap.rate, 0.0001)
        self.assertEqual(snap.period_hours, 8.0)
        self.assertEqual(snap.next_funding_time_ms, 1737806400000)

    def test_empty_list_returns_none(self):
        self.assertIsNone(_parse_bybit_funding(
            {"result": {"list": []}}, asset="BTC",
        ))

    def test_missing_result_returns_none(self):
        self.assertIsNone(_parse_bybit_funding({}, asset="BTC"))

    def test_malformed_returns_none(self):
        self.assertIsNone(_parse_bybit_funding(None, asset="BTC"))


class BybitDeliverableParseTestCase(unittest.TestCase):
    def test_extracts_delivery_symbol(self):
        # BTC-26DEC25 — годен (вместо 2025-12-26 — в будущем при тесте)
        # подменим now чтобы 26DEC25 был в будущем
        payload = {"result": {"list": [
            {"symbol": "BTC-26DEC25", "lastPrice": "102000.0"},
            {"symbol": "BTCUSDT", "lastPrice": "100000.0"},  # perp, не годится
            {"symbol": "ETH-26DEC25", "lastPrice": "4000.0"},  # другой asset
        ]}}
        now = datetime(2025, 10, 1)
        out = _parse_bybit_deliverable(
            payload, asset="BTC", spot_price=100000.0, now=now,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].symbol, "BTC-26DEC25")
        self.assertEqual(out[0].asset, "BTC")
        self.assertAlmostEqual(out[0].futures_price, 102000.0)
        self.assertAlmostEqual(out[0].spot_price, 100000.0)

    def test_zero_spot_returns_empty(self):
        payload = {"result": {"list": [{"symbol": "BTC-26DEC25", "lastPrice": "102000"}]}}
        self.assertEqual(_parse_bybit_deliverable(
            payload, asset="BTC", spot_price=0.0,
        ), [])

    def test_past_expiry_skipped(self):
        payload = {"result": {"list": [{"symbol": "BTC-01JAN20", "lastPrice": "100"}]}}
        out = _parse_bybit_deliverable(
            payload, asset="BTC", spot_price=100.0, now=datetime(2026, 1, 1),
        )
        self.assertEqual(out, [])

    def test_unparseable_symbol_skipped(self):
        payload = {"result": {"list": [{"symbol": "BTCUSDT-PERP", "lastPrice": "100"}]}}
        out = _parse_bybit_deliverable(
            payload, asset="BTC", spot_price=100.0, now=datetime(2025, 10, 1),
        )
        self.assertEqual(out, [])

    def test_garbage_payload_returns_empty(self):
        self.assertEqual(_parse_bybit_deliverable(
            None, asset="BTC", spot_price=100.0,
        ), [])


# ─── Binance parsers ─────────────────────────────────────────────────────────


class BinanceFundingParseTestCase(unittest.TestCase):
    def test_typical(self):
        payload = {
            "symbol": "BTCUSDT", "lastFundingRate": "0.00012",
            "nextFundingTime": "1737806400000",
        }
        snap = _parse_binance_funding(payload, asset="BTC")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.venue, "binance")
        self.assertAlmostEqual(snap.rate, 0.00012)

    def test_empty_rate_zero(self):
        snap = _parse_binance_funding({"symbol": "BTCUSDT"}, asset="BTC")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.rate, 0.0)

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_binance_funding(None, asset="BTC"))


class BinanceSpotPriceParseTestCase(unittest.TestCase):
    def test_typical(self):
        self.assertAlmostEqual(_parse_binance_spot_price(
            {"price": "100000.0"}
        ), 100000.0)

    def test_empty(self):
        self.assertEqual(_parse_binance_spot_price({}), 0.0)

    def test_garbage(self):
        self.assertEqual(_parse_binance_spot_price(None), 0.0)


class BinanceDeliverableParseTestCase(unittest.TestCase):
    def test_extracts_btcusd_quarterly(self):
        payload = [
            {"symbol": "BTCUSD_251226", "markPrice": "102000.0"},
            {"symbol": "BTCUSD_PERP", "markPrice": "100000.0"},  # perp, не подходит
            {"symbol": "ETHUSD_251226", "markPrice": "4000.0"},
        ]
        out = _parse_binance_deliverable(
            payload, asset="BTC", spot_price=100000.0,
            now=datetime(2025, 10, 1),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].symbol, "BTCUSD_251226")

    def test_invalid_date_skipped(self):
        payload = [{"symbol": "BTCUSD_259999", "markPrice": "100"}]
        out = _parse_binance_deliverable(
            payload, asset="BTC", spot_price=100.0,
            now=datetime(2025, 1, 1),
        )
        self.assertEqual(out, [])

    def test_garbage(self):
        self.assertEqual(_parse_binance_deliverable(
            None, asset="BTC", spot_price=100.0,
        ), [])


# ─── Fetcher с моком HTTP ──────────────────────────────────────────────────


class FetchTermStructureTestCase(unittest.TestCase):
    def _make_http(self, responses):
        """responses: dict {url_substring: payload}. Returns async callable."""
        async def _call(*, method, url, params=None, json=None, timeout=8.0):
            for key, payload in responses.items():
                if key in url:
                    if callable(payload):
                        return payload()
                    return payload
            return None
        return _call

    def test_happy_path(self):
        responses = {
            "api/v3/ticker/price": {"price": "100000.0"},
            "v5/market/tickers": {"result": {"list": [
                {"symbol": "BTCUSDT", "fundingRate": "0.0001",
                 "nextFundingTime": "1737806400000"},
                {"symbol": "BTC-26DEC25", "lastPrice": "102000.0"},
            ]}},
            "fapi/v1/premiumIndex": {
                "symbol": "BTCUSDT", "lastFundingRate": "0.0002",
                "nextFundingTime": "1737806400000",
            },
            "dapi/v1/premiumIndex": [
                {"symbol": "BTCUSD_251226", "markPrice": "101500.0"},
            ],
        }
        sig = _run(fetch_term_structure(
            asset="BTC",
            http_client=self._make_http(responses),
            now=datetime(2025, 10, 1),
        ))
        self.assertEqual(sig.asset, "BTC")
        self.assertIsNotNone(sig.spot_funding_annual)
        self.assertIn("bybit", sig.venues_used)
        self.assertIn("binance", sig.venues_used)
        self.assertIsNotNone(sig.quarterly_basis_annual)

    def test_one_venue_failure_doesnt_kill_others(self):
        def raise_err():
            raise RuntimeError("simulated bybit down")
        responses = {
            "api/v3/ticker/price": {"price": "100000.0"},
            "v5/market/tickers": raise_err,
            "fapi/v1/premiumIndex": {
                "symbol": "BTCUSDT", "lastFundingRate": "0.0002",
            },
            "dapi/v1/premiumIndex": [],
        }
        sig = _run(fetch_term_structure(
            asset="BTC",
            http_client=self._make_http(responses),
            now=datetime(2025, 10, 1),
        ))
        # Binance funding всё ещё прошёл
        self.assertIsNotNone(sig.spot_funding_annual)
        self.assertIn("binance", sig.venues_used)
        self.assertNotIn("bybit", sig.venues_used)

    def test_no_spot_price_no_basis(self):
        responses = {
            "api/v3/ticker/price": {"price": "0"},  # spot нет
            "v5/market/tickers": {"result": {"list": [
                {"symbol": "BTCUSDT", "fundingRate": "0.0001"},
            ]}},
            "fapi/v1/premiumIndex": {"symbol": "BTCUSDT", "lastFundingRate": "0.0002"},
            "dapi/v1/premiumIndex": [],
        }
        sig = _run(fetch_term_structure(
            asset="BTC",
            http_client=self._make_http(responses),
            now=datetime(2025, 10, 1),
        ))
        self.assertIsNotNone(sig.spot_funding_annual)
        self.assertIsNone(sig.quarterly_basis_annual)
        self.assertIsNone(sig.monthly_basis_annual)


# ─── Env flags ──────────────────────────────────────────────────────────────


class FeatureEnabledTestCase(unittest.TestCase):
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_enabled())

    def test_on_with_1(self):
        with mock.patch.dict(os.environ, {"FEATURE_FUNDING_TERM": "1"}):
            self.assertTrue(feature_enabled())

    def test_off_with_garbage(self):
        with mock.patch.dict(os.environ, {"FEATURE_FUNDING_TERM": "lol"}):
            self.assertFalse(feature_enabled())


class GetSymbolsTestCase(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_symbols(), ("BTC", "ETH"))

    def test_custom(self):
        with mock.patch.dict(os.environ, {"FUNDING_TERM_SYMBOLS": "SOL,AVAX"}):
            self.assertEqual(get_symbols(), ("SOL", "AVAX"))

    def test_empty_returns_default(self):
        with mock.patch.dict(os.environ, {"FUNDING_TERM_SYMBOLS": "  "}):
            self.assertEqual(get_symbols(), ("BTC", "ETH"))

    def test_dedup(self):
        with mock.patch.dict(os.environ, {"FUNDING_TERM_SYMBOLS": "BTC,BTC,ETH"}):
            self.assertEqual(get_symbols(), ("BTC", "ETH"))


class GetIntervalSecondsTestCase(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_interval_seconds(), 1800)

    def test_custom(self):
        with mock.patch.dict(os.environ, {"FUNDING_TERM_INTERVAL_SEC": "600"}):
            self.assertEqual(get_interval_seconds(), 600)

    def test_minimum_300(self):
        with mock.patch.dict(os.environ, {"FUNDING_TERM_INTERVAL_SEC": "1"}):
            self.assertEqual(get_interval_seconds(), 300)

    def test_invalid_returns_default(self):
        with mock.patch.dict(os.environ, {"FUNDING_TERM_INTERVAL_SEC": "bad"}):
            self.assertEqual(get_interval_seconds(), 1800)


# ─── Format helper ──────────────────────────────────────────────────────────


class FormatTermSummaryTestCase(unittest.TestCase):
    def test_with_event(self):
        from market_indicators.funding_term_structure import TermStructureSignal
        sig = TermStructureSignal(
            asset="BTC", timestamp_ms=0,
            spot_funding_annual=0.10, monthly_basis_annual=0.08,
            quarterly_basis_annual=0.05,
            slope_annual=-0.05, is_inverted=True,
            venues_used=("bybit", "binance"),
        )
        out = format_term_summary(sig, event="inversion_onset")
        self.assertIn("BTC", out)
        self.assertIn("inversion_onset", out)
        self.assertIn("inv=Y", out)
        self.assertIn("bybit", out)

    def test_none_values_render_as_na(self):
        from market_indicators.funding_term_structure import TermStructureSignal
        sig = TermStructureSignal(
            asset="ETH", timestamp_ms=0,
            spot_funding_annual=None, monthly_basis_annual=None,
            quarterly_basis_annual=None,
            slope_annual=None, is_inverted=False,
            venues_used=(),
        )
        out = format_term_summary(sig)
        self.assertIn("n/a", out)
        self.assertIn("venues=none", out)


if __name__ == "__main__":
    unittest.main()
