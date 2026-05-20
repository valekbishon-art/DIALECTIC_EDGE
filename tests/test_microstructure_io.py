"""Unit-тесты для market_indicators/microstructure_io.py.

Покрывают:
  * Все 5 venue parser'ов (Binance/Bybit/OKX/Bitget/Hyperliquid) на
    realistic payloads + битых форматах.
  * `fetch_venue_snapshot` с моком http-клиента: ok, timeout, exception,
    empty book, unknown venue.
  * `gather_all_venues` — мульти-venue gather с частичными сбоями.
  * `compute_microstructure_signal` — end-to-end pipeline.
  * Env-flags helpers (`feature_enabled`, `get_enabled_venues`,
    `get_symbols`, `get_band_pct`, `get_vacuum_drop_pct`, `get_interval_seconds`).

Все http-вызовы через DI (HttpClient = callable). Без aiohttp / БД.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from unittest.mock import patch

from market_indicators.microstructure import (
    OrderbookLevel,
    VenueMicrostructure,
)
from market_indicators.microstructure_io import (
    SUPPORTED_VENUES,
    VENUES,
    _binance_args,
    _bitget_args,
    _bybit_args,
    _hyperliquid_args,
    _okx_args,
    _parse_binance,
    _parse_bitget,
    _parse_bybit,
    _parse_hyperliquid,
    _parse_okx,
    compute_microstructure_signal,
    feature_enabled,
    fetch_venue_snapshot,
    gather_all_venues,
    get_band_pct,
    get_enabled_venues,
    get_interval_seconds,
    get_symbols,
    get_vacuum_drop_pct,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _async(coro):
    """Sync wrapper для async тестов."""
    return asyncio.run(coro)


def _make_mock_http(responses: dict[str, Any]):
    """Создаёт http_client который возвращает фейковые JSON по URL substring.

    `responses`: dict из URL-substring → payload (или Exception для raise).
    """

    async def _client(
        *, method: str, url: str, params: dict | None = None,
        json: dict | None = None, timeout_sec: float = 5.0,
    ) -> Any:
        for key, value in responses.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise KeyError(f"No mock for url={url!r}")

    return _client


# ─── Venue parser tests ──────────────────────────────────────────────────────


class ParseBinanceTestCase(unittest.TestCase):
    def test_happy_path(self) -> None:
        payload = {
            "lastUpdateId": 1,
            "bids": [["100.0", "1.5"], ["99.9", "2.0"]],
            "asks": [["100.1", "1.0"], ["100.2", "3.0"]],
        }
        bids, asks = _parse_binance(payload)
        self.assertEqual(len(bids), 2)
        self.assertEqual(len(asks), 2)
        self.assertEqual(bids[0], OrderbookLevel(price=100.0, size=1.5))

    def test_missing_keys(self) -> None:
        bids, asks = _parse_binance({})
        self.assertEqual((bids, asks), ((), ()))

    def test_not_dict(self) -> None:
        bids, asks = _parse_binance("garbage")  # type: ignore[arg-type]
        self.assertEqual((bids, asks), ((), ()))

    def test_args_shape(self) -> None:
        args = _binance_args("BTC", 10)
        self.assertIn("binance.com", args["url"])
        self.assertEqual(args["params"]["symbol"], "BTCUSDT")
        self.assertEqual(args["params"]["limit"], 10)


class ParseBybitTestCase(unittest.TestCase):
    def test_happy_path(self) -> None:
        payload = {
            "retCode": 0,
            "result": {
                "s": "BTCUSDT",
                "b": [["100.0", "1.0"], ["99.5", "2.5"]],
                "a": [["100.5", "0.5"], ["100.8", "1.5"]],
            },
        }
        bids, asks = _parse_bybit(payload)
        self.assertEqual(len(bids), 2)
        self.assertEqual(len(asks), 2)

    def test_missing_result(self) -> None:
        bids, asks = _parse_bybit({"retCode": 0})
        self.assertEqual((bids, asks), ((), ()))

    def test_result_not_dict(self) -> None:
        bids, asks = _parse_bybit({"result": "x"})
        self.assertEqual((bids, asks), ((), ()))

    def test_args_shape(self) -> None:
        args = _bybit_args("ETH", 25)
        self.assertIn("bybit.com", args["url"])
        self.assertEqual(args["params"]["symbol"], "ETHUSDT")
        self.assertEqual(args["params"]["category"], "linear")


class ParseOkxTestCase(unittest.TestCase):
    def test_happy_path(self) -> None:
        payload = {
            "code": "0",
            "data": [
                {
                    "bids": [["100.0", "1.0", "0", "1"], ["99.9", "2.0", "0", "1"]],
                    "asks": [["100.1", "1.5", "0", "1"]],
                }
            ],
        }
        bids, asks = _parse_okx(payload)
        self.assertEqual(len(bids), 2)
        self.assertEqual(len(asks), 1)
        # Игнорим дополнительные элементы (n_orders).
        self.assertEqual(bids[0].size, 1.0)

    def test_empty_data(self) -> None:
        self.assertEqual(_parse_okx({"data": []}), ((), ()))

    def test_no_data(self) -> None:
        self.assertEqual(_parse_okx({}), ((), ()))

    def test_args_shape(self) -> None:
        args = _okx_args("BTC", 20)
        self.assertIn("okx.com", args["url"])
        self.assertEqual(args["params"]["instId"], "BTC-USDT-SWAP")


class ParseBitgetTestCase(unittest.TestCase):
    def test_happy_path(self) -> None:
        payload = {
            "code": "00000",
            "data": {
                "bids": [["100.0", "1.0"]],
                "asks": [["100.1", "2.0"]],
                "ts": "1",
            },
        }
        bids, asks = _parse_bitget(payload)
        self.assertEqual(len(bids), 1)
        self.assertEqual(len(asks), 1)

    def test_no_data(self) -> None:
        self.assertEqual(_parse_bitget({}), ((), ()))

    def test_args_shape(self) -> None:
        args = _bitget_args("BTC", 10)
        self.assertEqual(args["params"]["productType"], "usdt-futures")
        self.assertEqual(args["params"]["symbol"], "BTCUSDT")


class ParseHyperliquidTestCase(unittest.TestCase):
    def test_happy_path(self) -> None:
        payload = {
            "coin": "BTC",
            "time": 1700000000000,
            "levels": [
                [{"px": "100.0", "sz": "1.0", "n": 2}, {"px": "99.9", "sz": "2.5", "n": 1}],
                [{"px": "100.1", "sz": "0.5", "n": 1}, {"px": "100.2", "sz": "3.0", "n": 2}],
            ],
        }
        bids, asks = _parse_hyperliquid(payload)
        self.assertEqual(len(bids), 2)
        self.assertEqual(len(asks), 2)
        self.assertEqual(bids[0].price, 100.0)
        self.assertEqual(asks[1].size, 3.0)

    def test_only_one_side(self) -> None:
        payload = {"levels": [[{"px": "100.0", "sz": "1.0"}]]}
        bids, asks = _parse_hyperliquid(payload)
        self.assertEqual((bids, asks), ((), ()))

    def test_no_levels(self) -> None:
        self.assertEqual(_parse_hyperliquid({"levels": None}), ((), ()))

    def test_args_shape(self) -> None:
        args = _hyperliquid_args("ETH", 20)
        self.assertEqual(args["json"]["type"], "l2Book")
        self.assertEqual(args["json"]["coin"], "ETH")


class VenueRegistryTestCase(unittest.TestCase):
    def test_all_supported_in_registry(self) -> None:
        for v in SUPPORTED_VENUES:
            self.assertIn(v, VENUES)

    def test_format_symbols_distinct(self) -> None:
        # Smoke test: каждый venue форматирует BTC по-своему (или одинаково).
        formats = {v: VENUES[v].format_symbol("BTC") for v in SUPPORTED_VENUES}
        self.assertEqual(formats["binance"], "BTCUSDT")
        self.assertEqual(formats["okx"], "BTC-USDT-SWAP")
        self.assertEqual(formats["hyperliquid"], "BTC")


# ─── fetch_venue_snapshot ────────────────────────────────────────────────────


class FetchVenueSnapshotTestCase(unittest.TestCase):
    def test_happy_path_binance(self) -> None:
        payload = {
            "bids": [["100.0", "1.0"], ["99.9", "2.0"]],
            "asks": [["100.1", "1.5"], ["100.2", "0.5"]],
        }
        http = _make_mock_http({"binance.com": payload})
        snap = _async(
            fetch_venue_snapshot(
                venue_name="binance", asset="BTC", http_client=http,
                band_pct=1.0, timestamp_ms=12345,
            )
        )
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.venue, "binance")
        self.assertEqual(snap.timestamp_ms, 12345)
        self.assertAlmostEqual(snap.mid_price, 100.05)

    def test_timeout_returns_none(self) -> None:
        async def _slow(**kwargs):
            await asyncio.sleep(10.0)
            return {}

        snap = _async(
            fetch_venue_snapshot(
                venue_name="binance", asset="BTC", http_client=_slow,
                timeout_sec=0.05,
            )
        )
        self.assertIsNone(snap)

    def test_exception_returns_none(self) -> None:
        http = _make_mock_http({"binance.com": RuntimeError("boom")})
        snap = _async(
            fetch_venue_snapshot(
                venue_name="binance", asset="BTC", http_client=http,
            )
        )
        self.assertIsNone(snap)

    def test_empty_book_returns_none(self) -> None:
        http = _make_mock_http({"binance.com": {"bids": [], "asks": []}})
        snap = _async(
            fetch_venue_snapshot(
                venue_name="binance", asset="BTC", http_client=http,
            )
        )
        self.assertIsNone(snap)

    def test_unknown_venue_returns_none(self) -> None:
        http = _make_mock_http({})
        snap = _async(
            fetch_venue_snapshot(
                venue_name="kraken_xyz", asset="BTC", http_client=http,
            )
        )
        self.assertIsNone(snap)


# ─── gather_all_venues ───────────────────────────────────────────────────────


class GatherAllVenuesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.binance_payload = {
            "bids": [["100.0", "1.0"]], "asks": [["100.1", "1.0"]],
        }
        self.bybit_payload = {
            "result": {"b": [["100.0", "1.0"]], "a": [["100.1", "1.0"]]}
        }

    def test_collects_all_successful(self) -> None:
        http = _make_mock_http({
            "binance.com": self.binance_payload,
            "bybit.com": self.bybit_payload,
        })
        snaps = _async(
            gather_all_venues(
                asset="BTC", http_client=http,
                venues=("binance", "bybit"),
            )
        )
        self.assertEqual(len(snaps), 2)
        venues = {s.venue for s in snaps}
        self.assertEqual(venues, {"binance", "bybit"})

    def test_partial_failure_filtered(self) -> None:
        http = _make_mock_http({
            "binance.com": self.binance_payload,
            "bybit.com": RuntimeError("bybit down"),
        })
        snaps = _async(
            gather_all_venues(
                asset="BTC", http_client=http,
                venues=("binance", "bybit"),
            )
        )
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].venue, "binance")

    def test_all_failed_returns_empty(self) -> None:
        http = _make_mock_http({
            "binance.com": RuntimeError("x"),
            "bybit.com": RuntimeError("y"),
        })
        snaps = _async(
            gather_all_venues(
                asset="BTC", http_client=http,
                venues=("binance", "bybit"),
            )
        )
        self.assertEqual(snaps, [])


# ─── compute_microstructure_signal ───────────────────────────────────────────


class ComputeMicrostructureSignalTestCase(unittest.TestCase):
    def test_full_pipeline(self) -> None:
        payloads = {
            "binance.com": {
                "bids": [["100.0", "1.0"], ["99.5", "2.0"]],
                "asks": [["100.1", "1.0"], ["100.5", "2.0"]],
            },
            "bybit.com": {
                "result": {
                    "b": [["100.0", "1.0"]],
                    "a": [["100.1", "1.0"]],
                }
            },
        }
        http = _make_mock_http(payloads)

        async def baseline_provider(asset: str) -> float | None:
            return 100.0

        signal = _async(
            compute_microstructure_signal(
                asset="BTC", http_client=http,
                baseline_provider=baseline_provider,
                venues=("binance", "bybit"),
            )
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.aggregate.venue_count, 2)
        self.assertEqual(signal.aggregate.asset, "BTC")

    def test_no_data_returns_none(self) -> None:
        http = _make_mock_http({
            "binance.com": RuntimeError(),
            "bybit.com": RuntimeError(),
        })
        signal = _async(
            compute_microstructure_signal(
                asset="BTC", http_client=http,
                venues=("binance", "bybit"),
            )
        )
        self.assertIsNone(signal)

    def test_baseline_provider_failure_handled(self) -> None:
        payload = {
            "bids": [["100.0", "1.0"]], "asks": [["100.1", "1.0"]],
        }
        http = _make_mock_http({"binance.com": payload})

        async def failing_baseline(asset: str) -> float | None:
            raise RuntimeError("db down")

        signal = _async(
            compute_microstructure_signal(
                asset="BTC", http_client=http,
                baseline_provider=failing_baseline,
                venues=("binance",),
            )
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        # baseline failure → vacuum=False (no baseline to compare).
        self.assertFalse(signal.vacuum)

    def test_vacuum_detected(self) -> None:
        # Тонкий стакан + большой baseline → vacuum.
        thin_payload = {
            "bids": [["100.0", "0.001"]], "asks": [["100.1", "0.001"]],
        }
        http = _make_mock_http({"binance.com": thin_payload})

        async def big_baseline(asset: str) -> float | None:
            return 1_000_000.0  # огромный baseline

        signal = _async(
            compute_microstructure_signal(
                asset="BTC", http_client=http,
                baseline_provider=big_baseline,
                venues=("binance",),
            )
        )
        assert signal is not None
        self.assertTrue(signal.vacuum)


# ─── Env-flag helpers ────────────────────────────────────────────────────────


class FeatureFlagsTestCase(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FEATURE_MICROSTRUCTURE", None)
            self.assertFalse(feature_enabled())

    def test_enabled_when_set(self) -> None:
        with patch.dict(os.environ, {"FEATURE_MICROSTRUCTURE": "1"}):
            self.assertTrue(feature_enabled())

    def test_enabled_when_true(self) -> None:
        with patch.dict(os.environ, {"FEATURE_MICROSTRUCTURE": "true"}):
            self.assertTrue(feature_enabled())

    def test_unsupported_venues_filtered(self) -> None:
        with patch.dict(os.environ, {"MICROSTRUCTURE_VENUES": "binance,foobar,bybit"}):
            self.assertEqual(set(get_enabled_venues()), {"binance", "bybit"})

    def test_default_venues_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MICROSTRUCTURE_VENUES", None)
            self.assertEqual(set(get_enabled_venues()), set(SUPPORTED_VENUES))

    def test_default_symbols(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MICROSTRUCTURE_SYMBOLS", None)
            self.assertEqual(get_symbols(), ("BTC", "ETH"))

    def test_symbols_uppercase(self) -> None:
        with patch.dict(os.environ, {"MICROSTRUCTURE_SYMBOLS": "btc, eth, sol"}):
            self.assertEqual(get_symbols(), ("BTC", "ETH", "SOL"))

    def test_band_pct_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MICROSTRUCTURE_BAND_PCT", None)
            self.assertEqual(get_band_pct(), 0.5)

    def test_band_pct_invalid_returns_default(self) -> None:
        with patch.dict(os.environ, {"MICROSTRUCTURE_BAND_PCT": "xyz"}):
            self.assertEqual(get_band_pct(), 0.5)

    def test_vacuum_drop_pct_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MICROSTRUCTURE_VACUUM_DROP_PCT", None)
            self.assertEqual(get_vacuum_drop_pct(), 40.0)

    def test_interval_clamped_min_60(self) -> None:
        with patch.dict(os.environ, {"MICROSTRUCTURE_INTERVAL_SEC": "10"}):
            self.assertEqual(get_interval_seconds(), 60)

    def test_interval_invalid_returns_default(self) -> None:
        with patch.dict(os.environ, {"MICROSTRUCTURE_INTERVAL_SEC": "abc"}):
            self.assertEqual(get_interval_seconds(), 300)


# ─── Sanity на VenueMicrostructure dataclass ────────────────────────────────


class VenueDataClassTestCase(unittest.TestCase):
    def test_total_depth_sum(self) -> None:
        v = VenueMicrostructure(
            venue="x", mid_price=100.0, best_bid=99.0, best_ask=101.0,
            bid_depth_usd=300.0, ask_depth_usd=400.0, band_pct=0.5,
            quoted_spread_bps=10.0, asymmetry=-0.1, timestamp_ms=1,
        )
        self.assertEqual(v.total_depth_usd(), 700.0)


if __name__ == "__main__":
    unittest.main()
