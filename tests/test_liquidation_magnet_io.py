"""I/O tests для liquidation_magnet_io (mocked HTTP)."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from market_indicators.liquidation_magnet import (
    LABEL_DOWN_MAGNET,
    LABEL_NEUTRAL,
    LABEL_UNKNOWN,
    LABEL_UP_MAGNET,
)
from market_indicators.liquidation_magnet_io import (
    _binance_oi_hist_args,
    _binance_top_ls_ratio_args,
    _bybit_interval_for,
    _bybit_oi_hist_args,
    _compute_oi_limit,
    _parse_binance_oi_hist,
    _parse_binance_top_ls_ratio,
    _parse_bybit_oi_hist,
    feature_enabled,
    fetch_liquidation_magnet_signal,
    format_liquidation_magnet_for_agents,
    get_lookback_hours,
    get_ls_long_heavy,
    get_oi_buildup_pct,
    get_period,
    get_symbol,
    liquidation_magnet_score_contribution,
)
from market_indicators.liquidation_magnet import LiquidationMagnetSignal


def _async_run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ─── Args builders ──────────────────────────────────────────────────────────


class TestArgsBuilders(unittest.TestCase):
    def test_binance_oi_hist_args(self):
        args = _binance_oi_hist_args(symbol="btcusdt", period="1h", limit=24)
        self.assertEqual(args["method"], "GET")
        self.assertEqual(args["url"], "https://fapi.binance.com/futures/data/openInterestHist")
        self.assertEqual(args["params"]["symbol"], "BTCUSDT")
        self.assertEqual(args["params"]["period"], "1h")
        self.assertEqual(args["params"]["limit"], 24)

    def test_binance_top_ls_ratio_args(self):
        args = _binance_top_ls_ratio_args(symbol="BTCUSDT", period="1h", limit=1)
        self.assertEqual(args["params"]["symbol"], "BTCUSDT")
        self.assertEqual(args["url"], "https://fapi.binance.com/futures/data/topLongShortPositionRatio")

    def test_bybit_oi_hist_args(self):
        args = _bybit_oi_hist_args(symbol="btcusdt", interval="1h", limit=25)
        self.assertEqual(args["params"]["category"], "linear")
        self.assertEqual(args["params"]["symbol"], "BTCUSDT")
        self.assertEqual(args["params"]["intervalTime"], "1h")
        self.assertEqual(args["params"]["limit"], 25)

    def test_bybit_interval_mapping(self):
        self.assertEqual(_bybit_interval_for("1h"), "1h")
        self.assertEqual(_bybit_interval_for("4h"), "4h")
        self.assertEqual(_bybit_interval_for("12h"), "4h")  # fallback
        self.assertEqual(_bybit_interval_for("1d"), "1d")
        self.assertEqual(_bybit_interval_for("unknown"), "1h")

    def test_compute_oi_limit(self):
        # 24h with 1h period → 24 + 2 = 26
        self.assertEqual(_compute_oi_limit("1h", 24), 26)
        # 24h with 4h period → 6 + 2 = 8
        self.assertEqual(_compute_oi_limit("4h", 24), 8)
        # min=2
        self.assertEqual(_compute_oi_limit("1d", 1), 2)
        # max clamp at 500
        self.assertEqual(_compute_oi_limit("5m", 168), 500)


# ─── Parsers ────────────────────────────────────────────────────────────────


class TestBinanceOIParser(unittest.TestCase):
    def test_normal_payload(self):
        payload = [
            {"symbol": "BTCUSDT", "sumOpenInterest": "100000.5",
             "sumOpenInterestValue": "5000000000.0", "timestamp": 1700000000000},
            {"symbol": "BTCUSDT", "sumOpenInterest": "110000.0",
             "sumOpenInterestValue": "5500000000.0", "timestamp": 1700003600000},
        ]
        points = _parse_binance_oi_hist(payload)
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].timestamp_ms, 1700000000000)
        self.assertAlmostEqual(points[0].oi_contracts, 100000.5, places=1)
        self.assertAlmostEqual(points[0].oi_usd, 5000000000.0, places=1)

    def test_error_payload(self):
        # Binance returns dict with code on error.
        payload = {"code": -1000, "msg": "Service unavailable"}
        points = _parse_binance_oi_hist(payload)
        self.assertEqual(points, [])

    def test_malformed_rows_skipped(self):
        payload = [
            {"symbol": "BTC", "sumOpenInterest": "not_a_number", "timestamp": 1700000000000},
            {"sumOpenInterest": "1.0", "timestamp": 1700000000000},  # valid
            "not_a_dict",
            {"sumOpenInterest": "-1.0", "timestamp": 1700000000000},  # negative OI → skip
        ]
        points = _parse_binance_oi_hist(payload)
        self.assertEqual(len(points), 1)

    def test_non_list_payload(self):
        self.assertEqual(_parse_binance_oi_hist({}), [])
        self.assertEqual(_parse_binance_oi_hist(None), [])


class TestBinanceLSRatioParser(unittest.TestCase):
    def test_normal_payload(self):
        payload = [
            {"symbol": "BTCUSDT", "longShortRatio": "2.5",
             "longAccount": "0.7", "shortAccount": "0.3", "timestamp": 1700000000000},
        ]
        ratio = _parse_binance_top_ls_ratio(payload)
        self.assertIsNotNone(ratio)
        self.assertEqual(ratio.long_short_ratio, 2.5)
        self.assertAlmostEqual(ratio.long_account_pct, 0.7, places=2)
        self.assertAlmostEqual(ratio.short_account_pct, 0.3, places=2)

    def test_takes_last_when_multiple(self):
        payload = [
            {"longShortRatio": "1.0", "longAccount": "0.5",
             "shortAccount": "0.5", "timestamp": 1700000000000},
            {"longShortRatio": "3.0", "longAccount": "0.75",
             "shortAccount": "0.25", "timestamp": 1700003600000},
        ]
        ratio = _parse_binance_top_ls_ratio(payload)
        self.assertIsNotNone(ratio)
        self.assertEqual(ratio.long_short_ratio, 3.0)

    def test_empty_list(self):
        self.assertIsNone(_parse_binance_top_ls_ratio([]))

    def test_error_payload(self):
        self.assertIsNone(_parse_binance_top_ls_ratio({"code": -1, "msg": "err"}))

    def test_zero_ratio_returns_none(self):
        # Invalid: ratio shouldn't be 0.
        payload = [{"longShortRatio": "0", "longAccount": "0", "shortAccount": "0",
                    "timestamp": 1700000000000}]
        self.assertIsNone(_parse_binance_top_ls_ratio(payload))


class TestBybitOIParser(unittest.TestCase):
    def test_normal_payload(self):
        payload = {
            "result": {
                "list": [
                    {"timestamp": "1700000000000", "openInterest": "100000.5"},
                    {"timestamp": "1700003600000", "openInterest": "110000.0"},
                ]
            }
        }
        points = _parse_bybit_oi_hist(payload)
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0].timestamp_ms, 1700000000000)
        self.assertAlmostEqual(points[0].oi_contracts, 100000.5, places=1)

    def test_missing_result(self):
        self.assertEqual(_parse_bybit_oi_hist({"foo": "bar"}), [])

    def test_non_dict(self):
        self.assertEqual(_parse_bybit_oi_hist("garbage"), [])
        self.assertEqual(_parse_bybit_oi_hist(None), [])

    def test_malformed_items_skipped(self):
        payload = {
            "result": {
                "list": [
                    {"timestamp": "abc", "openInterest": "100"},  # invalid ts
                    {"timestamp": "1700000000000", "openInterest": "100"},  # valid
                    "not_a_dict",
                ]
            }
        }
        points = _parse_bybit_oi_hist(payload)
        self.assertEqual(len(points), 1)


# ─── End-to-end fetcher (mocked HTTP) ───────────────────────────────────────


class TestFetchLiquidationMagnetSignal(unittest.TestCase):
    def _build_binance_oi_payload(self):
        """24h of 1h OI snapshots, growing from 1000 to 1240 (24% buildup)."""
        hour_ms = 3600 * 1000
        return [
            {"symbol": "BTCUSDT",
             "sumOpenInterest": str(1000.0 + i * 10.0),
             "sumOpenInterestValue": str((1000.0 + i * 10.0) * 50000),
             "timestamp": i * hour_ms}
            for i in range(25)
        ]

    def _build_ls_payload(self, ratio: float):
        return [{
            "symbol": "BTCUSDT",
            "longShortRatio": str(ratio),
            "longAccount": "0.7", "shortAccount": "0.3",
            "timestamp": 24 * 3600 * 1000,
        }]

    def _mock_client(self, *, binance_oi=None, binance_ls=None, bybit_oi=None,
                     binance_oi_fail=False, binance_ls_fail=False, bybit_oi_fail=False):
        async def client(*, method, url, params=None, **_):
            if "openInterestHist" in url:
                if binance_oi_fail:
                    raise RuntimeError("network down")
                return binance_oi if binance_oi is not None else []
            if "topLongShortPositionRatio" in url:
                if binance_ls_fail:
                    raise RuntimeError("network down")
                return binance_ls if binance_ls is not None else []
            if "open-interest" in url and "bybit" in url:
                if bybit_oi_fail:
                    raise RuntimeError("network down")
                return bybit_oi if bybit_oi is not None else {"result": {"list": []}}
            raise RuntimeError(f"unexpected url: {url}")
        return client

    def test_full_binance_signal_down_magnet(self):
        client = self._mock_client(
            binance_oi=self._build_binance_oi_payload(),
            binance_ls=self._build_ls_payload(2.6),  # heavy long, > 2.5 extreme
            bybit_oi={"result": {"list": []}},
        )
        signal = _async_run(fetch_liquidation_magnet_signal(client))
        self.assertEqual(signal.venue, "binance")
        # 24% growth не дотягивает до strong=25%, label=DOWN_MAGNET weak
        self.assertEqual(signal.label, LABEL_DOWN_MAGNET)
        self.assertEqual(signal.top_long_short_ratio, 2.6)

    def test_binance_fail_bybit_fallback_unknown(self):
        """Binance OI fail, Bybit OI OK, но без L/S → UNKNOWN."""
        client = self._mock_client(
            binance_oi_fail=True,
            binance_ls_fail=True,
            bybit_oi={
                "result": {
                    "list": [
                        {"timestamp": "0", "openInterest": "1000"},
                        {"timestamp": str(24*3600*1000), "openInterest": "1300"},
                    ]
                }
            },
        )
        signal = _async_run(fetch_liquidation_magnet_signal(client))
        self.assertEqual(signal.venue, "bybit")
        self.assertEqual(signal.label, LABEL_UNKNOWN)
        self.assertIsNone(signal.top_long_short_ratio)

    def test_both_fail_returns_unknown(self):
        client = self._mock_client(
            binance_oi_fail=True, binance_ls_fail=True, bybit_oi_fail=True,
        )
        signal = _async_run(fetch_liquidation_magnet_signal(client))
        self.assertEqual(signal.label, LABEL_UNKNOWN)

    def test_balanced_ls_returns_neutral(self):
        client = self._mock_client(
            binance_oi=self._build_binance_oi_payload(),
            binance_ls=self._build_ls_payload(1.0),  # balanced
            bybit_oi={"result": {"list": []}},
        )
        signal = _async_run(fetch_liquidation_magnet_signal(client))
        self.assertEqual(signal.label, LABEL_NEUTRAL)

    def test_extreme_short_up_magnet_strong(self):
        # OI grows 30% (above STRONG=25), L/S=0.35 (below EXTREME=0.4)
        hour_ms = 3600 * 1000
        oi_payload = [
            {"symbol": "BTCUSDT", "sumOpenInterest": "1000",
             "sumOpenInterestValue": "5e10", "timestamp": 0},
            {"symbol": "BTCUSDT", "sumOpenInterest": "1300",
             "sumOpenInterestValue": "6.5e10", "timestamp": 24 * hour_ms},
        ]
        client = self._mock_client(
            binance_oi=oi_payload,
            binance_ls=self._build_ls_payload(0.35),
            bybit_oi={"result": {"list": []}},
        )
        signal = _async_run(fetch_liquidation_magnet_signal(client))
        self.assertEqual(signal.label, LABEL_UP_MAGNET)
        self.assertTrue(signal.is_strong_signal)


# ─── Score contribution ─────────────────────────────────────────────────────


class TestScoreContribution(unittest.TestCase):
    def test_unknown_returns_zero(self):
        sig = LiquidationMagnetSignal(label=LABEL_UNKNOWN)
        delta, bull, bear = liquidation_magnet_score_contribution(sig)
        self.assertEqual(delta, 0)
        self.assertEqual(bull, [])
        self.assertEqual(bear, [])

    def test_neutral_returns_zero(self):
        sig = LiquidationMagnetSignal(label=LABEL_NEUTRAL)
        delta, _, _ = liquidation_magnet_score_contribution(sig)
        self.assertEqual(delta, 0)

    def test_up_magnet_weak(self):
        sig = LiquidationMagnetSignal(
            label=LABEL_UP_MAGNET, is_strong_signal=False,
            top_long_short_ratio=0.55, oi_change_pct=15.0,
        )
        delta, bull, bear = liquidation_magnet_score_contribution(sig)
        self.assertEqual(delta, 1)
        self.assertEqual(len(bull), 1)
        self.assertEqual(bear, [])

    def test_up_magnet_strong(self):
        sig = LiquidationMagnetSignal(
            label=LABEL_UP_MAGNET, is_strong_signal=True,
            top_long_short_ratio=0.35, oi_change_pct=30.0,
        )
        delta, bull, _ = liquidation_magnet_score_contribution(sig)
        self.assertEqual(delta, 2)
        self.assertEqual(len(bull), 1)

    def test_down_magnet_weak(self):
        sig = LiquidationMagnetSignal(
            label=LABEL_DOWN_MAGNET, is_strong_signal=False,
            top_long_short_ratio=1.8, oi_change_pct=15.0,
        )
        delta, bull, bear = liquidation_magnet_score_contribution(sig)
        self.assertEqual(delta, -1)
        self.assertEqual(bull, [])
        self.assertEqual(len(bear), 1)

    def test_down_magnet_strong(self):
        sig = LiquidationMagnetSignal(
            label=LABEL_DOWN_MAGNET, is_strong_signal=True,
            top_long_short_ratio=2.8, oi_change_pct=30.0,
        )
        delta, _, bear = liquidation_magnet_score_contribution(sig)
        self.assertEqual(delta, -2)
        self.assertEqual(len(bear), 1)

    def test_none_signal_returns_zero(self):
        delta, _, _ = liquidation_magnet_score_contribution(None)
        self.assertEqual(delta, 0)


# ─── Agent formatting ───────────────────────────────────────────────────────


class TestAgentFormatting(unittest.TestCase):
    def test_unknown_format(self):
        sig = LiquidationMagnetSignal(label=LABEL_UNKNOWN)
        out = format_liquidation_magnet_for_agents(sig)
        self.assertIn("нет данных", out)

    def test_up_magnet_format(self):
        sig = LiquidationMagnetSignal(
            label=LABEL_UP_MAGNET, is_strong_signal=True,
            top_long_short_ratio=0.35, oi_change_pct=30.0,
            oi_now_contracts=1300.0, oi_baseline_contracts=1000.0,
            top_long_account_pct=0.3, top_short_account_pct=0.7,
            venue="binance", symbol="BTCUSDT", oi_lookback_hours=24,
        )
        out = format_liquidation_magnet_for_agents(sig)
        self.assertIn("UP MAGNET", out)
        self.assertIn("strong", out)
        self.assertIn("BTCUSDT", out)
        self.assertIn("0.35", out)
        self.assertIn("30.0", out)

    def test_no_ls_ratio_in_format(self):
        sig = LiquidationMagnetSignal(
            label=LABEL_NEUTRAL, top_long_short_ratio=None,
            venue="bybit", symbol="BTCUSDT",
        )
        out = format_liquidation_magnet_for_agents(sig)
        self.assertIn("n/a", out)


# ─── Env parsers ────────────────────────────────────────────────────────────


class TestEnvParsers(unittest.TestCase):
    def _clear_env(self):
        for k in list(os.environ):
            if k.startswith("LIQUIDATION_MAGNET") or k == "FEATURE_LIQUIDATION_MAGNET":
                del os.environ[k]

    def setUp(self):
        self._clear_env()

    def tearDown(self):
        self._clear_env()

    def test_feature_disabled_by_default(self):
        self.assertFalse(feature_enabled())

    def test_feature_enabled(self):
        with patch.dict(os.environ, {"FEATURE_LIQUIDATION_MAGNET": "1"}):
            self.assertTrue(feature_enabled())

    def test_get_symbol_default(self):
        self.assertEqual(get_symbol(), "BTCUSDT")

    def test_get_symbol_override(self):
        with patch.dict(os.environ, {"LIQUIDATION_MAGNET_SYMBOL": "ethusdt"}):
            self.assertEqual(get_symbol(), "ETHUSDT")

    def test_get_period_default(self):
        self.assertEqual(get_period(), "1h")

    def test_get_period_invalid_falls_back(self):
        with patch.dict(os.environ, {"LIQUIDATION_MAGNET_PERIOD": "nonsense"}):
            self.assertEqual(get_period(), "1h")

    def test_get_lookback_hours_default(self):
        self.assertEqual(get_lookback_hours(), 24)

    def test_get_lookback_hours_out_of_range(self):
        with patch.dict(os.environ, {"LIQUIDATION_MAGNET_LOOKBACK_HOURS": "9999"}):
            self.assertEqual(get_lookback_hours(), 24)

    def test_get_oi_buildup_pct_override(self):
        with patch.dict(os.environ, {"LIQUIDATION_MAGNET_OI_BUILDUP_PCT": "15.5"}):
            self.assertAlmostEqual(get_oi_buildup_pct(), 15.5, places=2)

    def test_get_ls_long_heavy_invalid_falls_back(self):
        with patch.dict(os.environ, {"LIQUIDATION_MAGNET_LS_LONG_HEAVY": "not_a_float"}):
            self.assertAlmostEqual(get_ls_long_heavy(), 1.7, places=2)


if __name__ == "__main__":
    unittest.main()
