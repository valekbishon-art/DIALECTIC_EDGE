"""Тесты бэктеста памп-детектора (сеть НЕ нужна)."""

import unittest

from pump_scanner import PumpConfig
from pump_backtest import (
    DEFAULT_HORIZONS_MIN,
    aggregate,
    backtest_series,
    demo,
    excursions,
    forward_return,
)


def _series(start, pct, n):
    end = start * (1 + pct / 100.0)
    return [start + (end - start) * i / (n - 1) for i in range(n)]


class TestForwardMath(unittest.TestCase):
    def test_forward_return(self):
        closes = [1.0, 1.1, 1.2, 1.3]
        self.assertAlmostEqual(forward_return(closes, 0, 2), 20.0, places=4)
        self.assertIsNone(forward_return(closes, 0, 100))
        self.assertIsNone(forward_return(closes, 3, 1))

    def test_excursions(self):
        closes = [1.0, 1.2, 0.9, 1.1]
        mfe, mae = excursions(closes, 0, 3)
        self.assertAlmostEqual(mfe, 20.0, places=4)
        self.assertAlmostEqual(mae, -10.0, places=4)

    def test_excursions_no_future(self):
        mfe, mae = excursions([1.0], 0, 5)
        self.assertIsNone(mfe)
        self.assertIsNone(mae)


class TestBacktestSeries(unittest.TestCase):
    def test_no_signal_on_flat(self):
        n = 1440 * 2
        closes = [1.0 + 0.0 for _ in range(n)]
        vols = [10.0] * n
        trades = backtest_series("FLAT", closes, vols, cfg=PumpConfig())
        self.assertEqual(len(trades), 0)

    def test_detects_injected_pump(self):
        # плоско сутки, потом +9% за 30 мин с резким объёмом
        n = 1440 + 200
        closes = [1.0] * n
        vols = [10.0] * n
        p0 = 1440
        for k in range(p0, p0 + 30):
            frac = (k - p0 + 1) / 30.0
            closes[k] = 1.0 * (1.0 + 0.09 * frac)
            vols[k] = 200.0
        # после пампа держим цену выше
        for k in range(p0 + 30, n):
            closes[k] = closes[p0 + 29]
        trades = backtest_series("PUMP", closes, vols, cfg=PumpConfig())
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0].tier, "pump")
        self.assertGreaterEqual(trades[0].pump_pct, 5.0)

    def test_no_lookahead(self):
        # сигнал не должен использовать будущие свечи: индекс входа < len-1
        n = 1440 + 200
        closes = [1.0] * n
        vols = [10.0] * n
        p0 = 1440
        for k in range(p0, p0 + 30):
            frac = (k - p0 + 1) / 30.0
            closes[k] = 1.0 * (1.0 + 0.09 * frac)
            vols[k] = 200.0
        for k in range(p0 + 30, n):
            closes[k] = closes[p0 + 29]
        trades = backtest_series("PUMP", closes, vols, cfg=PumpConfig())
        for t in trades:
            self.assertLess(t.index, n)


class TestAggregateAndDemo(unittest.TestCase):
    def test_aggregate_keys(self):
        n = 1440 + 400
        closes = [1.0] * n
        vols = [10.0] * n
        p0 = 1440
        for k in range(p0, p0 + 30):
            frac = (k - p0 + 1) / 30.0
            closes[k] = 1.0 * (1.0 + 0.09 * frac)
            vols[k] = 200.0
        for k in range(p0 + 30, n):
            closes[k] = closes[p0 + 29]
        trades = backtest_series("PUMP", closes, vols, cfg=PumpConfig())
        stats = aggregate(trades)
        self.assertIn("signals", stats)
        self.assertIn("horizons", stats)
        self.assertIn(60, stats["horizons"])

    def test_demo_runs(self):
        res = demo()
        self.assertIn("stats", res)
        self.assertGreaterEqual(res["stats"]["signals"], 1)


if __name__ == "__main__":
    unittest.main()
