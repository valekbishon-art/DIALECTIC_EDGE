"""Unit-tests для market_indicators/regime_io.py.

HTTP-клиент полностью замокирован через DI. Без сетевых вызовов.
Покрывают:
  * `_binance_klines_args` — корректные query params.
  * `_parse_binance_closes` — happy-path, малформ, частичные ряды.
  * `fetch_btc_hourly_closes` — успех, ошибка HTTP (isolated).
  * `fetch_regime_signals` — end-to-end с моком, fallback на UNKNOWN при
    пустом ответе.
  * `regime_score_contribution` — все 5 label'ов.
  * `format_regime_for_agents` — текст содержит ожидаемые маркеры.
  * `feature_enabled`, `get_*` env-парсеры — fallback'и, bounds.
"""

from __future__ import annotations

import asyncio
import os
import random
import unittest
from unittest import mock

from market_indicators.regime import (
    LABEL_CRISIS,
    LABEL_RANGING,
    LABEL_TRENDING,
    LABEL_UNKNOWN,
    LABEL_VOLATILE,
    RegimeClassification,
)
from market_indicators.regime_io import (
    BINANCE_SPOT_KLINES_URL,
    DEFAULT_KLINES_LIMIT,
    RegimeSignals,
    _binance_klines_args,
    _parse_binance_closes,
    feature_enabled,
    fetch_btc_hourly_closes,
    fetch_regime_signals,
    format_regime_for_agents,
    get_hazard_rate,
    get_klines_limit,
    get_label_window,
    get_vol_high_annualized,
    regime_score_contribution,
)


def _run(coro):
    return asyncio.run(coro)


def _build_klines_row(close: float) -> list:
    """Минимальная klines-строка Binance: [open_time, o, h, l, c, ...]."""
    return [0, 0.0, 0.0, 0.0, float(close), 0.0]


def _build_uptrend_payload(n: int = 60, start: float = 100.0, step: float = 0.2) -> list:
    """Синтетический uptrend payload."""
    return [_build_klines_row(start + i * step) for i in range(n)]


def _build_oscillating_payload(n: int = 60, mid: float = 100.0, amp: float = 0.5) -> list:
    """Чередующиеся +/- amplitude (mean-reverting)."""
    return [_build_klines_row(mid + (amp if i % 2 == 0 else -amp)) for i in range(n)]


# ─── Endpoint arg builder ────────────────────────────────────────────────────


class BinanceArgsTestCase(unittest.TestCase):
    def test_klines_args(self) -> None:
        args = _binance_klines_args(symbol="BTCUSDT", interval="1h", limit=300)
        self.assertEqual(args["method"], "GET")
        self.assertEqual(args["url"], BINANCE_SPOT_KLINES_URL)
        self.assertEqual(args["params"]["symbol"], "BTCUSDT")
        self.assertEqual(args["params"]["interval"], "1h")
        self.assertEqual(args["params"]["limit"], 300)


# ─── Parse closes ────────────────────────────────────────────────────────────


class ParseBinanceClosesTestCase(unittest.TestCase):
    def test_typical(self) -> None:
        payload = _build_uptrend_payload(n=5, start=50_000.0, step=10.0)
        closes = _parse_binance_closes(payload)
        self.assertEqual(closes, [50000.0, 50010.0, 50020.0, 50030.0, 50040.0])

    def test_skips_short_rows(self) -> None:
        payload = [[0, 0.0, 0.0]]  # длина <5
        self.assertEqual(_parse_binance_closes(payload), [])

    def test_skips_non_numeric(self) -> None:
        payload = [
            _build_klines_row(100.0),
            [0, 0.0, 0.0, 0.0, "junk", 0.0],
            _build_klines_row(101.0),
        ]
        self.assertEqual(_parse_binance_closes(payload), [100.0, 101.0])

    def test_skips_non_positive(self) -> None:
        payload = [
            _build_klines_row(100.0),
            _build_klines_row(0.0),
            _build_klines_row(-1.0),
            _build_klines_row(101.0),
        ]
        self.assertEqual(_parse_binance_closes(payload), [100.0, 101.0])

    def test_empty(self) -> None:
        self.assertEqual(_parse_binance_closes([]), [])

    def test_non_list_input(self) -> None:
        self.assertEqual(_parse_binance_closes(None), [])
        self.assertEqual(_parse_binance_closes({"foo": "bar"}), [])


# ─── fetch_btc_hourly_closes ─────────────────────────────────────────────────


class FetchBtcHourlyClosesTestCase(unittest.TestCase):
    def test_success(self) -> None:
        payload = _build_uptrend_payload(n=10)

        async def http_mock(**kwargs):
            return payload

        closes = _run(fetch_btc_hourly_closes(http_client=http_mock, limit=10))
        self.assertEqual(len(closes), 10)

    def test_http_error_returns_empty(self) -> None:
        async def http_mock(**kwargs):
            raise RuntimeError("HTTP 500")

        closes = _run(fetch_btc_hourly_closes(http_client=http_mock, limit=10))
        self.assertEqual(closes, [])

    def test_timeout_returns_empty(self) -> None:
        async def http_mock(**kwargs):
            raise asyncio.TimeoutError

        closes = _run(fetch_btc_hourly_closes(http_client=http_mock, limit=10))
        self.assertEqual(closes, [])

    def test_malformed_payload_returns_empty(self) -> None:
        async def http_mock(**kwargs):
            return {"unexpected": "shape"}

        closes = _run(fetch_btc_hourly_closes(http_client=http_mock, limit=10))
        self.assertEqual(closes, [])


# ─── fetch_regime_signals (end-to-end) ───────────────────────────────────────


class FetchRegimeSignalsTestCase(unittest.TestCase):
    def test_returns_regime_signals_with_label(self) -> None:
        # Стабильный uptrend с tiny noise → result должен иметь label
        # отличный от UNKNOWN (число observations > 12).
        rng = random.Random(0)
        prices = [100.0]
        for _ in range(80):
            prices.append(prices[-1] * (1.0 + 0.002 + rng.gauss(0.0, 0.0003)))
        payload = [_build_klines_row(p) for p in prices]

        async def http_mock(**kwargs):
            return payload

        signals = _run(fetch_regime_signals(http_client=http_mock))
        self.assertIsInstance(signals, RegimeSignals)
        self.assertNotEqual(signals.btc.label, LABEL_UNKNOWN)
        self.assertEqual(signals.btc.n_observations, 80)
        # Drift положительный
        self.assertGreater(signals.btc.recent_drift_annualized, 0)

    def test_empty_payload_returns_unknown(self) -> None:
        async def http_mock(**kwargs):
            return []

        signals = _run(fetch_regime_signals(http_client=http_mock))
        self.assertEqual(signals.btc.label, LABEL_UNKNOWN)
        self.assertEqual(signals.btc.n_observations, 0)

    def test_http_failure_returns_unknown(self) -> None:
        async def http_mock(**kwargs):
            raise OSError("network down")

        signals = _run(fetch_regime_signals(http_client=http_mock))
        self.assertEqual(signals.btc.label, LABEL_UNKNOWN)

    def test_timestamp_set(self) -> None:
        async def http_mock(**kwargs):
            return []

        signals = _run(fetch_regime_signals(http_client=http_mock))
        self.assertIsNotNone(signals.timestamp_ms)
        self.assertGreater(signals.timestamp_ms, 0)


# ─── regime_score_contribution ───────────────────────────────────────────────


class RegimeScoreContributionTestCase(unittest.TestCase):
    def _signals(self, label: str, *, dir_bias: int = 0) -> RegimeSignals:
        s = RegimeSignals()
        s.btc = RegimeClassification(
            label=label,
            p_changepoint=0.45,
            expected_run_length=10.0,
            recent_volatility_annualized=1.5,
            recent_drift_annualized=0.4 if dir_bias > 0 else (-0.4 if dir_bias < 0 else 0.0),
            recent_autocorr_lag1=0.1,
            drift_to_vol_ratio=0.5 * dir_bias,
            n_observations=50,
            direction_bias=dir_bias,
        )
        return s

    def test_crisis(self) -> None:
        score, bull, bear = regime_score_contribution(self._signals(LABEL_CRISIS, dir_bias=-1))
        self.assertEqual(score, -1)
        self.assertEqual(bull, [])
        self.assertTrue(any("CRISIS" in r for r in bear))

    def test_trending_up(self) -> None:
        score, bull, bear = regime_score_contribution(self._signals(LABEL_TRENDING, dir_bias=+1))
        self.assertEqual(score, +1)
        self.assertTrue(any("TRENDING up" in r for r in bull))
        self.assertEqual(bear, [])

    def test_trending_down(self) -> None:
        score, bull, bear = regime_score_contribution(self._signals(LABEL_TRENDING, dir_bias=-1))
        self.assertEqual(score, -1)
        self.assertEqual(bull, [])
        self.assertTrue(any("TRENDING down" in r for r in bear))

    def test_volatile_no_score_but_warning(self) -> None:
        score, bull, bear = regime_score_contribution(self._signals(LABEL_VOLATILE))
        self.assertEqual(score, 0)
        # Volatile добавляет bearish reason (warning), но не двигает score.
        self.assertTrue(any("VOLATILE" in r for r in bear))

    def test_ranging_neutral(self) -> None:
        score, bull, bear = regime_score_contribution(self._signals(LABEL_RANGING))
        self.assertEqual(score, 0)
        self.assertEqual(bull, [])
        self.assertEqual(bear, [])

    def test_unknown_neutral(self) -> None:
        score, bull, bear = regime_score_contribution(self._signals(LABEL_UNKNOWN))
        self.assertEqual(score, 0)
        self.assertEqual(bull, [])
        self.assertEqual(bear, [])


# ─── format_regime_for_agents ────────────────────────────────────────────────


class FormatRegimeForAgentsTestCase(unittest.TestCase):
    def test_known_label(self) -> None:
        s = RegimeSignals()
        s.btc = RegimeClassification(
            label=LABEL_TRENDING,
            p_changepoint=0.10,
            expected_run_length=80.0,
            recent_volatility_annualized=0.6,
            recent_drift_annualized=0.3,
            recent_autocorr_lag1=0.1,
            drift_to_vol_ratio=0.5,
            n_observations=80,
            direction_bias=1,
        )
        text = format_regime_for_agents(s)
        self.assertIn("РЕЖИМ РЫНКА", text)
        self.assertIn("TRENDING", text)
        self.assertIn("Drift", text)

    def test_unknown_label_short(self) -> None:
        s = RegimeSignals()
        # n_observations < 12 → выдаёт «недостаточно данных».
        s.btc = RegimeClassification(label=LABEL_UNKNOWN, n_observations=5)
        text = format_regime_for_agents(s)
        self.assertIn("Недостаточно данных", text)


# ─── env parsers ─────────────────────────────────────────────────────────────


class FeatureEnabledTestCase(unittest.TestCase):
    def test_default_off(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FEATURE_REGIME_CLASSIFIER", None)
            self.assertFalse(feature_enabled())

    def test_truthy_values(self) -> None:
        for v in ("1", "true", "True", "yes"):
            with mock.patch.dict(os.environ, {"FEATURE_REGIME_CLASSIFIER": v}):
                self.assertTrue(feature_enabled(), f"expected truthy for {v!r}")

    def test_falsy_values(self) -> None:
        for v in ("0", "false", "no", "", "garbage"):
            with mock.patch.dict(os.environ, {"FEATURE_REGIME_CLASSIFIER": v}):
                self.assertFalse(feature_enabled(), f"expected falsy for {v!r}")


class EnvParserTestCase(unittest.TestCase):
    def test_label_window_bounds(self) -> None:
        for raw, expected in [("5", 12), ("12", 12), ("48", 48), ("200", 168), ("garbage", 24)]:
            with mock.patch.dict(os.environ, {"REGIME_LABEL_WINDOW": raw}):
                self.assertEqual(get_label_window(), expected)

    def test_hazard_rate_bounds(self) -> None:
        # Невалидные значения должны fallback'нуться к дефолту.
        for raw in ["0.0", "1.0", "-0.5", "garbage"]:
            with mock.patch.dict(os.environ, {"REGIME_HAZARD_RATE": raw}):
                self.assertAlmostEqual(get_hazard_rate(), 1.0 / 200.0)
        with mock.patch.dict(os.environ, {"REGIME_HAZARD_RATE": "0.01"}):
            self.assertAlmostEqual(get_hazard_rate(), 0.01)

    def test_vol_high_bounds(self) -> None:
        with mock.patch.dict(os.environ, {"REGIME_VOL_HIGH_ANNUALIZED": "0"}):
            self.assertGreater(get_vol_high_annualized(), 0)
        with mock.patch.dict(os.environ, {"REGIME_VOL_HIGH_ANNUALIZED": "2.5"}):
            self.assertAlmostEqual(get_vol_high_annualized(), 2.5)
        with mock.patch.dict(os.environ, {"REGIME_VOL_HIGH_ANNUALIZED": "garbage"}):
            self.assertGreater(get_vol_high_annualized(), 0)

    def test_klines_limit_bounds(self) -> None:
        for raw, expected in [("10", 50), ("50", 50), ("500", 500), ("5000", 1000), ("garbage", DEFAULT_KLINES_LIMIT)]:
            with mock.patch.dict(os.environ, {"REGIME_KLINES_LIMIT": raw}):
                self.assertEqual(get_klines_limit(), expected)


if __name__ == "__main__":
    unittest.main()
