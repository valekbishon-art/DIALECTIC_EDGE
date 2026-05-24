from __future__ import annotations

import os
import unittest
from unittest.mock import patch

try:
    import aiogram  # noqa: F401
    _HAS_AIOGRAM = True
except ImportError:
    _HAS_AIOGRAM = False

if _HAS_AIOGRAM:
    from refactor.handlers.advisor_handler import (
        DEFAULT_ASSETS,
        SUPPORTED_ASSETS,
        _build_inputs_from_prices,
        _get_default_capital_usd,
        _map_horizon,
        _map_profile,
        _parse_args,
        register_advisor_handlers,
    )
    from core.advisor import (
        HORIZON_LONG,
        HORIZON_MEDIUM,
        HORIZON_SHORT,
        RISK_AGGRESSIVE,
        RISK_CONSERVATIVE,
        RISK_MODERATE,
    )


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestParseArgs(unittest.TestCase):
    def test_no_args_uses_defaults(self):
        assets, capital = _parse_args(None)
        self.assertEqual(assets, DEFAULT_ASSETS)
        self.assertIsNone(capital)

    def test_command_only_uses_defaults(self):
        assets, capital = _parse_args("/advise")
        self.assertEqual(assets, DEFAULT_ASSETS)
        self.assertIsNone(capital)

    def test_single_asset(self):
        assets, capital = _parse_args("/advise btc")
        self.assertEqual(assets, ("BTC",))
        self.assertIsNone(capital)

    def test_single_asset_with_capital(self):
        assets, capital = _parse_args("/advise eth 5000")
        self.assertEqual(assets, ("ETH",))
        self.assertEqual(capital, 5000.0)

    def test_all_keyword_expands_defaults(self):
        assets, capital = _parse_args("/advise all")
        self.assertEqual(assets, DEFAULT_ASSETS)
        self.assertIsNone(capital)

    def test_unsupported_asset_falls_back_to_defaults(self):
        # User typed garbage — fall back to defaults rather than crash.
        assets, _ = _parse_args("/advise DOGE")
        self.assertEqual(assets, DEFAULT_ASSETS)

    def test_invalid_capital_ignored(self):
        assets, capital = _parse_args("/advise btc not-a-number")
        self.assertEqual(assets, ("BTC",))
        self.assertIsNone(capital)

    def test_capital_clamped_max(self):
        _, capital = _parse_args("/advise btc 99999999999")
        # Max clamp = 10M.
        self.assertEqual(capital, 10_000_000.0)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestEnvDefaults(unittest.TestCase):
    def test_default_capital_env(self):
        with patch.dict(os.environ, {"ADVISOR_DEFAULT_CAPITAL_USD": "2500"}, clear=True):
            self.assertEqual(_get_default_capital_usd(), 2500.0)

    def test_default_capital_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_default_capital_usd(), 1000.0)

    def test_invalid_capital_env_fallback(self):
        with patch.dict(os.environ, {"ADVISOR_DEFAULT_CAPITAL_USD": "abc"}, clear=True):
            self.assertEqual(_get_default_capital_usd(), 1000.0)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestMappers(unittest.TestCase):
    def test_map_profile_known_values(self):
        self.assertEqual(_map_profile("conservative"), RISK_CONSERVATIVE)
        self.assertEqual(_map_profile("MODERATE"), RISK_MODERATE)
        self.assertEqual(_map_profile("aggressive"), RISK_AGGRESSIVE)

    def test_map_profile_unknown_defaults_moderate(self):
        self.assertEqual(_map_profile("xxxxx"), RISK_MODERATE)
        self.assertEqual(_map_profile(""), RISK_MODERATE)

    def test_map_horizon(self):
        self.assertEqual(_map_horizon("short"), HORIZON_SHORT)
        self.assertEqual(_map_horizon("medium_term"), HORIZON_MEDIUM)
        self.assertEqual(_map_horizon("long"), HORIZON_LONG)
        self.assertEqual(_map_horizon("???"), HORIZON_MEDIUM)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestBuildInputs(unittest.TestCase):
    def test_basic_bull_inputs_from_prices(self):
        prices = {
            "BTC": {
                "price": 68000.0,
                "atr_14d": 1500.0,
                "atr_14d_pct": 2.2,
                "rsi_14d": 58.0,
                "trend": "UPTREND",
            },
            "ETH": {
                "price": 3500.0,
                "atr_14d": 120.0,
                "atr_14d_pct": 3.4,
                "rsi_14d": 52.0,
                "trend": "UPTREND",
            },
        }
        inputs = _build_inputs_from_prices(
            asset="BTC",
            prices=prices,
            btc_lean="BULL",
            btc_confidence=75,
            risk_profile=RISK_MODERATE,
            horizon=HORIZON_MEDIUM,
            capital_usd=10000.0,
        )
        self.assertEqual(inputs.asset, "BTC")
        self.assertEqual(inputs.entry_price, 68000.0)
        self.assertEqual(inputs.atr_14d_usd, 1500.0)
        self.assertEqual(inputs.trend, "UPTREND")
        self.assertEqual(inputs.quant_verdict, "LONG")
        self.assertEqual(inputs.btc_lean, "BULL")
        self.assertEqual(inputs.btc_confidence_pct, 75)

    def test_missing_asset_in_prices(self):
        # When fetch_realtime_prices doesn't have our asset (e.g. exchange
        # not trading it), we should still produce a (mostly-empty) inputs.
        inputs = _build_inputs_from_prices(
            asset="SOL",
            prices={"BTC": {"price": 68000.0}},
            btc_lean="BULL",
            btc_confidence=70,
            risk_profile=RISK_MODERATE,
            horizon=HORIZON_MEDIUM,
            capital_usd=1000.0,
        )
        self.assertIsNone(inputs.entry_price)
        self.assertIsNone(inputs.atr_14d_usd)
        self.assertIsNone(inputs.quant_verdict)

    def test_downtrend_rsi_overbought_boosts_short_confidence(self):
        prices = {
            "ETH": {
                "price": 3500.0,
                "atr_14d": 120.0,
                "atr_14d_pct": 3.4,
                "rsi_14d": 75.0,  # overbought
                "trend": "DOWNTREND",
            },
        }
        inputs = _build_inputs_from_prices(
            asset="ETH",
            prices=prices,
            btc_lean="BEAR",
            btc_confidence=70,
            risk_profile=RISK_MODERATE,
            horizon=HORIZON_MEDIUM,
            capital_usd=5000.0,
        )
        self.assertEqual(inputs.quant_verdict, "SHORT")
        # RSI bump applied.
        self.assertGreaterEqual(inputs.quant_confidence, 0.7)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestHandlerRegistration(unittest.TestCase):
    def test_register_advisor_handlers_calls_dispatcher(self):
        # Smoke test: register_advisor_handlers should call dp.message.register
        # at least twice (/advise + /advisor aliases).
        class FakeDispatcher:
            def __init__(self):
                self.message = self
                self.calls: list[tuple] = []

            def register(self, handler, *args, **kwargs):
                self.calls.append((handler, args, kwargs))

        dp = FakeDispatcher()
        register_advisor_handlers(dp)
        self.assertGreaterEqual(len(dp.calls), 2)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestSupportedAssets(unittest.TestCase):
    def test_supported_assets_superset_of_defaults(self):
        self.assertTrue(set(DEFAULT_ASSETS).issubset(set(SUPPORTED_ASSETS)))


if __name__ == "__main__":
    unittest.main()
