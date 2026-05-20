"""Unit-tests для market_indicators.options_skew_io.

HTTP-клиент полностью замокирован через DI. Без сетевых вызовов.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime
from unittest import mock

from market_indicators.options_skew_io import (
    _deribit_book_summary_args,
    _deribit_index_args,
    _parse_deribit_index,
    _parse_deribit_options,
    feature_enabled,
    fetch_options_skew,
    get_currencies,
    get_interval_seconds,
)


def _run(coro):
    return asyncio.run(coro)


def _book_payload(currency: str, *, rows: list[dict] | None = None) -> dict:
    """Pre-baked Deribit get_book_summary_by_currency payload."""
    rows = rows or []
    return {"result": rows}


def _make_book_row(
    *, instrument_name: str, mark_iv_pct: float, underlying_price: float = 100_000.0,
) -> dict:
    return {
        "instrument_name": instrument_name,
        "mark_iv": mark_iv_pct,
        "underlying_price": underlying_price,
    }


# ─── Endpoint arg builders ──────────────────────────────────────────────────


class DeribitArgsTestCase(unittest.TestCase):
    def test_book_summary_args(self):
        args = _deribit_book_summary_args("btc")
        self.assertEqual(args["method"], "GET")
        self.assertIn("get_book_summary_by_currency", args["url"])
        self.assertEqual(args["params"]["currency"], "BTC")
        self.assertEqual(args["params"]["kind"], "option")

    def test_index_args(self):
        args = _deribit_index_args("BTC")
        self.assertIn("get_index_price", args["url"])
        self.assertEqual(args["params"]["index_name"], "btc_usd")


# ─── Index parser ───────────────────────────────────────────────────────────


class ParseDeribitIndexTestCase(unittest.TestCase):
    def test_typical(self):
        self.assertEqual(_parse_deribit_index({"result": {"index_price": 100_000.5}}), 100_000.5)

    def test_missing_index(self):
        self.assertIsNone(_parse_deribit_index({"result": {}}))

    def test_zero_returns_none(self):
        self.assertIsNone(_parse_deribit_index({"result": {"index_price": 0}}))

    def test_negative_returns_none(self):
        self.assertIsNone(_parse_deribit_index({"result": {"index_price": -1.0}}))

    def test_malformed(self):
        self.assertIsNone(_parse_deribit_index(None))
        self.assertIsNone(_parse_deribit_index({}))


# ─── Options parser ─────────────────────────────────────────────────────────


class ParseDeribitOptionsTestCase(unittest.TestCase):
    def test_extracts_quotes(self):
        payload = _book_payload("BTC", rows=[
            _make_book_row(instrument_name="BTC-26DEC25-100000-C", mark_iv_pct=65.0),
            _make_book_row(instrument_name="BTC-26DEC25-100000-P", mark_iv_pct=66.0),
            _make_book_row(instrument_name="BTC-26DEC25-120000-C", mark_iv_pct=75.0),
        ])
        quotes, spot = _parse_deribit_options(payload, currency="BTC")
        self.assertEqual(len(quotes), 3)
        self.assertEqual(spot, 100_000.0)
        # Mark IV конвертится из % в долю
        for q in quotes:
            self.assertLessEqual(q.mark_iv, 1.0)
            self.assertGreaterEqual(q.mark_iv, 0.5)
        self.assertEqual(quotes[0].currency, "BTC")
        self.assertEqual(quotes[0].kind, "C")
        self.assertEqual(quotes[1].kind, "P")
        self.assertEqual(quotes[0].strike, 100_000.0)

    def test_filters_by_currency(self):
        payload = _book_payload("BTC", rows=[
            _make_book_row(instrument_name="BTC-26DEC25-100000-C", mark_iv_pct=65.0),
            _make_book_row(instrument_name="ETH-26DEC25-3500-C", mark_iv_pct=70.0),
        ])
        quotes, _ = _parse_deribit_options(payload, currency="BTC")
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].currency, "BTC")

    def test_skips_invalid_iv(self):
        payload = _book_payload("BTC", rows=[
            _make_book_row(instrument_name="BTC-26DEC25-100000-C", mark_iv_pct=0.0),
            _make_book_row(instrument_name="BTC-26DEC25-100000-P", mark_iv_pct=65.0),
        ])
        quotes, _ = _parse_deribit_options(payload, currency="BTC")
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].kind, "P")

    def test_skips_invalid_names(self):
        payload = _book_payload("BTC", rows=[
            {"instrument_name": "BTC-PERPETUAL", "mark_iv": 50, "underlying_price": 100_000.0},
            _make_book_row(instrument_name="BTC-26DEC25-100000-C", mark_iv_pct=65.0),
        ])
        quotes, _ = _parse_deribit_options(payload, currency="BTC")
        self.assertEqual(len(quotes), 1)

    def test_empty_payload(self):
        quotes, spot = _parse_deribit_options({"result": []}, currency="BTC")
        self.assertEqual(quotes, [])
        self.assertIsNone(spot)

    def test_handles_malformed(self):
        quotes, spot = _parse_deribit_options(None, currency="BTC")
        self.assertEqual(quotes, [])
        self.assertIsNone(spot)

    def test_spot_is_median(self):
        # Underlying prices: 100k, 100k, 200k → median = 100k
        payload = _book_payload("BTC", rows=[
            _make_book_row(instrument_name="BTC-26DEC25-100000-C", mark_iv_pct=65.0, underlying_price=100_000.0),
            _make_book_row(instrument_name="BTC-26DEC25-110000-C", mark_iv_pct=66.0, underlying_price=100_000.0),
            _make_book_row(instrument_name="BTC-26DEC25-120000-C", mark_iv_pct=70.0, underlying_price=200_000.0),
        ])
        _, spot = _parse_deribit_options(payload, currency="BTC")
        self.assertEqual(spot, 100_000.0)

    def test_accepts_bare_list_payload(self):
        # На случай если HTTP-клиент уже отдаёт `result` напрямую.
        rows = [
            _make_book_row(instrument_name="BTC-26DEC25-100000-C", mark_iv_pct=65.0),
        ]
        quotes, _ = _parse_deribit_options(rows, currency="BTC")
        self.assertEqual(len(quotes), 1)


# ─── End-to-end fetch ───────────────────────────────────────────────────────


class FetchOptionsSkewTestCase(unittest.TestCase):
    def _make_http(self, responses: dict[str, object]):
        """Возвращает async http(url=..., method=..., ...) который смотрит
        по подстроке url и возвращает заранее заданный ответ.
        """
        async def _http(*, method, url, params=None, json=None, timeout=8.0):
            for key, val in responses.items():
                if key in url:
                    if callable(val):
                        return val()
                    return val
            raise RuntimeError(f"no mock response for {url}")

        return _http

    def _book_rows_near_and_far(
        self,
        *,
        currency: str = "BTC",
        near_dt: datetime,
        far_dt: datetime,
    ) -> list[dict]:
        def _name(strike: int, kind: str, dt: datetime) -> str:
            day = f"{dt.day}{dt.strftime('%b').upper()}{dt.strftime('%y')}"
            return f"{currency}-{day}-{strike}-{kind}"

        return [
            # near 7d — put_skew
            {"instrument_name": _name(80000, "P", near_dt), "mark_iv": 95.0, "underlying_price": 100_000.0},
            {"instrument_name": _name(100000, "C", near_dt), "mark_iv": 65.0, "underlying_price": 100_000.0},
            {"instrument_name": _name(100000, "P", near_dt), "mark_iv": 66.0, "underlying_price": 100_000.0},
            {"instrument_name": _name(120000, "C", near_dt), "mark_iv": 72.0, "underlying_price": 100_000.0},
            # far 30d — мягче, но всё ещё put skew
            {"instrument_name": _name(80000, "P", far_dt), "mark_iv": 85.0, "underlying_price": 100_000.0},
            {"instrument_name": _name(100000, "C", far_dt), "mark_iv": 60.0, "underlying_price": 100_000.0},
            {"instrument_name": _name(100000, "P", far_dt), "mark_iv": 61.0, "underlying_price": 100_000.0},
            {"instrument_name": _name(120000, "C", far_dt), "mark_iv": 67.0, "underlying_price": 100_000.0},
        ]

    def test_full_path_both_buckets(self):
        now = datetime(2026, 5, 19, 8, 0, 0)
        near_dt = datetime(2026, 5, 26, 8, 0, 0)   # 7d
        far_dt = datetime(2026, 6, 19, 8, 0, 0)    # 31d
        rows = self._book_rows_near_and_far(near_dt=near_dt, far_dt=far_dt)
        responses = {
            "get_book_summary_by_currency": {"result": rows},
            "get_index_price": {"result": {"index_price": 100_000.0}},
        }
        sig = _run(fetch_options_skew(
            currency="BTC",
            http_client=self._make_http(responses),
            now=now,
        ))
        self.assertEqual(sig.currency, "BTC")
        self.assertEqual(sig.underlying_price, 100_000.0)
        self.assertEqual(sig.near_expiry_days, 7)
        self.assertEqual(sig.far_expiry_days, 31)
        self.assertIsNotNone(sig.near_rr_25d)
        self.assertIsNotNone(sig.far_rr_25d)
        # put_skew → RR отрицательный
        self.assertLess(sig.far_rr_25d, 0.0)
        self.assertIn("deribit", sig.venues_used)

    def test_index_failure_falls_back_to_median_underlying(self):
        now = datetime(2026, 5, 19, 8, 0, 0)
        near_dt = datetime(2026, 5, 26, 8, 0, 0)
        far_dt = datetime(2026, 6, 19, 8, 0, 0)
        rows = self._book_rows_near_and_far(near_dt=near_dt, far_dt=far_dt)

        def raise_err():
            raise RuntimeError("simulated index down")

        responses = {
            "get_book_summary_by_currency": {"result": rows},
            "get_index_price": raise_err,
        }
        sig = _run(fetch_options_skew(
            currency="BTC",
            http_client=self._make_http(responses),
            now=now,
        ))
        # Fallback на медиану underlying_price из quotes.
        self.assertEqual(sig.underlying_price, 100_000.0)
        self.assertIsNotNone(sig.near_rr_25d)

    def test_empty_book_returns_unknown(self):
        now = datetime(2026, 5, 19, 8, 0, 0)
        responses = {
            "get_book_summary_by_currency": {"result": []},
            "get_index_price": {"result": {"index_price": 100_000.0}},
        }
        sig = _run(fetch_options_skew(
            currency="BTC",
            http_client=self._make_http(responses),
            now=now,
        ))
        self.assertEqual(sig.skew_class, "unknown")
        self.assertIsNone(sig.near_atm_iv)
        self.assertIsNone(sig.far_atm_iv)

    def test_book_failure_returns_unknown(self):
        now = datetime(2026, 5, 19, 8, 0, 0)

        def raise_err():
            raise RuntimeError("deribit down")

        responses = {
            "get_book_summary_by_currency": raise_err,
            "get_index_price": {"result": {"index_price": 100_000.0}},
        }
        sig = _run(fetch_options_skew(
            currency="BTC",
            http_client=self._make_http(responses),
            now=now,
        ))
        self.assertEqual(sig.skew_class, "unknown")


# ─── Env flags ──────────────────────────────────────────────────────────────


class FeatureEnabledTestCase(unittest.TestCase):
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_enabled())

    def test_on_with_1(self):
        with mock.patch.dict(os.environ, {"FEATURE_OPTIONS_SKEW": "1"}):
            self.assertTrue(feature_enabled())

    def test_off_with_garbage(self):
        with mock.patch.dict(os.environ, {"FEATURE_OPTIONS_SKEW": "lol"}):
            self.assertFalse(feature_enabled())


class GetCurrenciesTestCase(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_currencies(), ("BTC", "ETH"))

    def test_custom(self):
        with mock.patch.dict(os.environ, {"OPTIONS_SKEW_SYMBOLS": "SOL,BTC"}):
            self.assertEqual(get_currencies(), ("SOL", "BTC"))

    def test_empty_returns_default(self):
        with mock.patch.dict(os.environ, {"OPTIONS_SKEW_SYMBOLS": "  "}):
            self.assertEqual(get_currencies(), ("BTC", "ETH"))

    def test_dedup(self):
        with mock.patch.dict(os.environ, {"OPTIONS_SKEW_SYMBOLS": "BTC,BTC,ETH"}):
            self.assertEqual(get_currencies(), ("BTC", "ETH"))


class GetIntervalSecondsTestCase(unittest.TestCase):
    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_interval_seconds(), 1800)

    def test_custom(self):
        with mock.patch.dict(os.environ, {"OPTIONS_SKEW_INTERVAL_SEC": "600"}):
            self.assertEqual(get_interval_seconds(), 600)

    def test_minimum_300(self):
        with mock.patch.dict(os.environ, {"OPTIONS_SKEW_INTERVAL_SEC": "1"}):
            self.assertEqual(get_interval_seconds(), 300)

    def test_invalid_returns_default(self):
        with mock.patch.dict(os.environ, {"OPTIONS_SKEW_INTERVAL_SEC": "bad"}):
            self.assertEqual(get_interval_seconds(), 1800)


if __name__ == "__main__":
    unittest.main()
