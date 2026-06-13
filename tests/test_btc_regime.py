"""Unit tests for core.btc_regime (pure-logic, no network)."""

import importlib.util
import os
import sys
import unittest

# Import the module directly to avoid heavy core/__init__ side imports.
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "core", "btc_regime.py")
_spec = importlib.util.spec_from_file_location("btc_regime", _PATH)
btc_regime = importlib.util.module_from_spec(_spec)
sys.modules["btc_regime"] = btc_regime
_spec.loader.exec_module(btc_regime)

compute_btc_regime = btc_regime.compute_btc_regime
RISK_ON = btc_regime.REGIME_RISK_ON
RISK_OFF = btc_regime.REGIME_RISK_OFF
NEUTRAL = btc_regime.REGIME_NEUTRAL


def _uptrend(n=400, start=100.0, g=0.004):
    """Steady-growth uptrend (constant slope → momentum z ~0 → NEUTRAL-ish)."""
    return [start * ((1 + g) ** i) for i in range(n)]


def _accel_uptrend(n=420):
    """Uptrend that accelerates near the end → positive trend & momentum z."""
    out = [100.0]
    for i in range(1, n):
        g = 0.0015 if i < n - 120 else 0.006  # steeper final leg
        out.append(out[-1] * (1 + g))
    return out


def _downtrend(n=400, start=100.0, g=0.004):
    return [start * ((1 - g) ** i) for i in range(n)]


class TestBtcRegime(unittest.TestCase):
    def test_too_short_returns_none(self):
        self.assertIsNone(compute_btc_regime([100.0] * 50))

    def test_steady_uptrend_gate_open(self):
        # Constant-slope uptrend: gate open (above SMA200), exposure >= 0.5.
        v = compute_btc_regime(_uptrend())
        self.assertIsNotNone(v)
        self.assertIn(v.regime, (RISK_ON, NEUTRAL))
        self.assertGreaterEqual(v.exposure, 0.5)
        self.assertGreater(v.close, v.sma_slow)
        self.assertGreater(v.sma_fast, v.sma_slow)

    def test_accelerating_uptrend_is_risk_on(self):
        v = compute_btc_regime(_accel_uptrend())
        self.assertIsNotNone(v)
        self.assertEqual(v.regime, RISK_ON)
        self.assertGreater(v.exposure, 0.5)
        self.assertLessEqual(v.exposure, 1.0)
        self.assertGreater(v.score, 0.0)

    def test_downtrend_is_risk_off(self):
        v = compute_btc_regime(_downtrend())
        self.assertIsNotNone(v)
        self.assertEqual(v.regime, RISK_OFF)
        self.assertEqual(v.exposure, 0.0)
        self.assertLess(v.close, v.sma_slow)

    def test_exposure_bounds_and_confidence(self):
        for series in (_uptrend(), _downtrend()):
            v = compute_btc_regime(series)
            self.assertGreaterEqual(v.exposure, 0.0)
            self.assertLessEqual(v.exposure, 1.0)
            self.assertGreaterEqual(v.confidence, 0)
            self.assertLessEqual(v.confidence, 100)

    def test_deterministic(self):
        s = _uptrend()
        self.assertEqual(compute_btc_regime(s), compute_btc_regime(s))

    def test_no_lookahead_uses_only_supplied_closes(self):
        # Truncating the series must not change a verdict computed on the prefix.
        s = _uptrend(420)
        v_prefix = compute_btc_regime(s[:400])
        v_again = compute_btc_regime(s[:400])
        self.assertEqual(v_prefix, v_again)

    def test_filters_nonpositive(self):
        s = _uptrend()
        s_with_bad = [None, 0.0, -5.0] + s
        self.assertEqual(compute_btc_regime(s_with_bad).regime,
                         compute_btc_regime(s).regime)


if __name__ == "__main__":
    unittest.main()
