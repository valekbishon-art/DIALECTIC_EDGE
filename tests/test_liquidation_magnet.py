"""Pure-math tests for liquidation_magnet."""

from __future__ import annotations

import unittest

from market_indicators.liquidation_magnet import (
    DEFAULT_LS_LONG_EXTREME,
    DEFAULT_LS_LONG_HEAVY,
    DEFAULT_LS_SHORT_EXTREME,
    DEFAULT_LS_SHORT_HEAVY,
    DEFAULT_OI_BUILDUP_PCT,
    DEFAULT_OI_BUILDUP_STRONG_PCT,
    LABEL_DOWN_MAGNET,
    LABEL_NEUTRAL,
    LABEL_UNKNOWN,
    LABEL_UP_MAGNET,
    OIHistoryPoint,
    TopTraderRatio,
    build_liquidation_magnet_signal,
    classify_liquidation_magnet,
    compute_oi_change_pct,
)


def _oi_history(values: list[tuple[int, float]]) -> list[OIHistoryPoint]:
    return [OIHistoryPoint(timestamp_ms=ts, oi_contracts=oi) for ts, oi in values]


class TestComputeOIChangePct(unittest.TestCase):
    def test_empty_returns_zeros(self):
        oi_now, oi_base, change = compute_oi_change_pct([])
        self.assertEqual(oi_now, 0.0)
        self.assertEqual(oi_base, 0.0)
        self.assertEqual(change, 0.0)

    def test_single_point_returns_zeros(self):
        oi_now, oi_base, change = compute_oi_change_pct(
            _oi_history([(1000, 100.0)])
        )
        self.assertEqual(change, 0.0)

    def test_basic_increase(self):
        # 24 hours of hourly data, OI goes 100 → 120.
        hour_ms = 3600 * 1000
        history = _oi_history([
            (i * hour_ms, 100.0 + i * (20.0 / 23.0)) for i in range(24)
        ])
        oi_now, oi_base, change = compute_oi_change_pct(history, lookback_hours=24)
        self.assertAlmostEqual(oi_now, 120.0, places=2)
        self.assertAlmostEqual(oi_base, 100.0, places=2)
        self.assertAlmostEqual(change, 20.0, places=2)

    def test_negative_change_deleveraging(self):
        hour_ms = 3600 * 1000
        history = _oi_history([
            (0 * hour_ms, 100.0),
            (1 * hour_ms, 90.0),
            (2 * hour_ms, 80.0),
        ])
        _, _, change = compute_oi_change_pct(history, lookback_hours=2)
        self.assertAlmostEqual(change, -20.0, places=2)

    def test_zero_baseline_returns_zero_change(self):
        hour_ms = 3600 * 1000
        history = _oi_history([
            (0, 0.0),
            (hour_ms, 50.0),
        ])
        oi_now, oi_base, change = compute_oi_change_pct(history, lookback_hours=1)
        self.assertEqual(oi_base, 0.0)
        self.assertEqual(change, 0.0)

    def test_unordered_history_sorted_correctly(self):
        hour_ms = 3600 * 1000
        history = _oi_history([
            (3 * hour_ms, 130.0),
            (1 * hour_ms, 110.0),
            (2 * hour_ms, 120.0),
            (0 * hour_ms, 100.0),
        ])
        oi_now, oi_base, change = compute_oi_change_pct(history, lookback_hours=3)
        self.assertEqual(oi_now, 130.0)
        self.assertEqual(oi_base, 100.0)
        self.assertAlmostEqual(change, 30.0, places=2)

    def test_lookback_window_picks_oldest_in_window(self):
        """Окно lookback=12ч из 24ч данных — baseline должен быть точка
        ~12ч назад, не самая ранняя."""
        hour_ms = 3600 * 1000
        history = _oi_history([
            (i * hour_ms, 100.0 + i * 5.0) for i in range(24)
        ])
        # Сейчас точка 23 (i=23, OI=215). 12 часов назад — точка 11 (OI=155).
        oi_now, oi_base, change = compute_oi_change_pct(history, lookback_hours=12)
        self.assertEqual(oi_now, 215.0)
        self.assertEqual(oi_base, 155.0)
        # change = (215-155)/155 = 0.387 → 38.7%
        self.assertAlmostEqual(change, 38.71, places=1)


class TestClassifyLiquidationMagnet(unittest.TestCase):
    def test_none_ratio_returns_unknown(self):
        label, strong = classify_liquidation_magnet(
            oi_change_pct=20.0, top_long_short_ratio=None,
        )
        self.assertEqual(label, LABEL_UNKNOWN)
        self.assertFalse(strong)

    def test_low_oi_buildup_returns_neutral(self):
        # L/S highly skewed, but OI growth ниже порога → no magnet.
        label, strong = classify_liquidation_magnet(
            oi_change_pct=5.0, top_long_short_ratio=3.0,
        )
        self.assertEqual(label, LABEL_NEUTRAL)
        self.assertFalse(strong)

    def test_negative_oi_change_returns_neutral(self):
        label, _ = classify_liquidation_magnet(
            oi_change_pct=-10.0, top_long_short_ratio=2.5,
        )
        self.assertEqual(label, LABEL_NEUTRAL)

    def test_heavily_long_returns_down_magnet(self):
        label, strong = classify_liquidation_magnet(
            oi_change_pct=15.0, top_long_short_ratio=DEFAULT_LS_LONG_HEAVY + 0.1,
        )
        self.assertEqual(label, LABEL_DOWN_MAGNET)
        self.assertFalse(strong)

    def test_extreme_long_strong_signal(self):
        label, strong = classify_liquidation_magnet(
            oi_change_pct=DEFAULT_OI_BUILDUP_STRONG_PCT + 5,
            top_long_short_ratio=DEFAULT_LS_LONG_EXTREME + 0.1,
        )
        self.assertEqual(label, LABEL_DOWN_MAGNET)
        self.assertTrue(strong)

    def test_extreme_long_weak_signal_when_oi_below_strong(self):
        """L/S extreme но OI buildup ниже strong-порога → weak."""
        label, strong = classify_liquidation_magnet(
            oi_change_pct=DEFAULT_OI_BUILDUP_PCT + 2,
            top_long_short_ratio=DEFAULT_LS_LONG_EXTREME + 0.5,
        )
        self.assertEqual(label, LABEL_DOWN_MAGNET)
        self.assertFalse(strong)

    def test_heavily_short_returns_up_magnet(self):
        label, strong = classify_liquidation_magnet(
            oi_change_pct=15.0, top_long_short_ratio=DEFAULT_LS_SHORT_HEAVY - 0.1,
        )
        self.assertEqual(label, LABEL_UP_MAGNET)
        self.assertFalse(strong)

    def test_extreme_short_strong_signal(self):
        label, strong = classify_liquidation_magnet(
            oi_change_pct=DEFAULT_OI_BUILDUP_STRONG_PCT + 5,
            top_long_short_ratio=DEFAULT_LS_SHORT_EXTREME - 0.1,
        )
        self.assertEqual(label, LABEL_UP_MAGNET)
        self.assertTrue(strong)

    def test_balanced_ls_returns_neutral(self):
        label, _ = classify_liquidation_magnet(
            oi_change_pct=20.0, top_long_short_ratio=1.0,
        )
        self.assertEqual(label, LABEL_NEUTRAL)

    def test_custom_thresholds_override(self):
        """Если pass custom thresholds, default не применяются."""
        label, _ = classify_liquidation_magnet(
            oi_change_pct=20.0,
            top_long_short_ratio=1.4,
            ls_long_heavy=1.3,  # custom: 1.4 > 1.3 → heavy
        )
        self.assertEqual(label, LABEL_DOWN_MAGNET)


class TestBuildLiquidationMagnetSignal(unittest.TestCase):
    def _basic_history(self) -> list[OIHistoryPoint]:
        hour_ms = 3600 * 1000
        return _oi_history([
            (i * hour_ms, 1000.0 + i * 10.0) for i in range(25)
        ])

    def test_full_signal_with_ls_ratio_down_magnet(self):
        ratio = TopTraderRatio(
            timestamp_ms=24 * 3600 * 1000,
            long_account_pct=0.7, short_account_pct=0.3,
            long_short_ratio=2.5,
        )
        signal = build_liquidation_magnet_signal(
            oi_history=self._basic_history(),
            top_trader_ratio=ratio,
            venue="binance", symbol="BTCUSDT",
            lookback_hours=24,
            timestamp_ms=24 * 3600 * 1000,
        )
        self.assertEqual(signal.symbol, "BTCUSDT")
        self.assertEqual(signal.venue, "binance")
        self.assertEqual(signal.oi_now_contracts, 1240.0)
        self.assertEqual(signal.oi_baseline_contracts, 1000.0)
        self.assertAlmostEqual(signal.oi_change_pct, 24.0, places=1)
        self.assertEqual(signal.top_long_short_ratio, 2.5)
        # 24% OI growth (выше DEFAULT_OI_BUILDUP_PCT=10, ниже STRONG=25),
        # L/S=2.5 == DEFAULT_LS_LONG_EXTREME → strong требует ОБА условия,
        # OI strong=25 > 24 → weak.
        self.assertEqual(signal.label, LABEL_DOWN_MAGNET)
        self.assertFalse(signal.is_strong_signal)

    def test_unknown_when_no_ls_ratio(self):
        signal = build_liquidation_magnet_signal(
            oi_history=self._basic_history(),
            top_trader_ratio=None,
            venue="bybit", symbol="BTCUSDT",
            lookback_hours=24,
        )
        self.assertEqual(signal.label, LABEL_UNKNOWN)
        self.assertIsNone(signal.top_long_short_ratio)
        self.assertGreater(signal.oi_now_contracts, 0)

    def test_empty_history_returns_unknown(self):
        signal = build_liquidation_magnet_signal(
            oi_history=[],
            top_trader_ratio=TopTraderRatio(
                timestamp_ms=0, long_account_pct=0.7,
                short_account_pct=0.3, long_short_ratio=2.5,
            ),
            venue="bybit", symbol="BTCUSDT",
        )
        # OI change=0, ниже DEFAULT_OI_BUILDUP_PCT → NEUTRAL.
        self.assertEqual(signal.label, LABEL_NEUTRAL)
        self.assertEqual(signal.oi_change_pct, 0.0)

    def test_up_magnet_strong(self):
        hour_ms = 3600 * 1000
        # 30% buildup за 24ч → выше STRONG=25
        history = _oi_history([
            (0, 1000.0),
            (24 * hour_ms, 1300.0),
        ])
        ratio = TopTraderRatio(
            timestamp_ms=24 * hour_ms,
            long_account_pct=0.3, short_account_pct=0.7,
            long_short_ratio=0.35,  # ниже LS_SHORT_EXTREME=0.4
        )
        signal = build_liquidation_magnet_signal(
            oi_history=history, top_trader_ratio=ratio,
            venue="binance", symbol="BTCUSDT",
            lookback_hours=24,
        )
        self.assertEqual(signal.label, LABEL_UP_MAGNET)
        self.assertTrue(signal.is_strong_signal)
        self.assertAlmostEqual(signal.oi_change_pct, 30.0, places=1)


if __name__ == "__main__":
    unittest.main()
