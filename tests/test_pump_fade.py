"""Тесты структурного pump-FADE эджа (core/pump_fade.py).

Памп → не «покупай», а структурный шорт отката с малым размером, широкой
инвалидацией и честными бэктест-статами. Сеть не нужна — чистая логика.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta

import core.pump_fade as pf
from core.edge_ledger import resolve_against_candles


@dataclass
class C:
    timestamp: datetime
    high: float
    low: float
    close: float


class BuildFadePlayTest(unittest.TestCase):
    def test_none_below_min_runup(self):
        self.assertIsNone(pf.build_fade_play("X", 1.0, 10.0))  # 10% < 15% порог

    def test_none_for_dust_or_missing_price(self):
        self.assertIsNone(pf.build_fade_play("X", None, 50.0))
        self.assertIsNone(pf.build_fade_play("X", 0.0, 50.0))
        self.assertIsNone(pf.build_fade_play("X", 0.0005, 50.0))  # ниже price_floor

    def test_builds_short_with_correct_levels(self):
        p = pf.build_fade_play("AAA", 2.0, 30.0)
        self.assertIsNotNone(p)
        self.assertEqual(p.direction, "SHORT")
        self.assertLess(p.target, p.entry)      # цель ниже (откат)
        self.assertGreater(p.stop, p.entry)     # инвалидация выше (новый лег)
        self.assertAlmostEqual(p.target_pct, 0.06, places=3)
        self.assertAlmostEqual(p.invalidation_pct, 0.30, places=3)
        # RR намеренно < 1 (эдж в вин-рейте/частоте)
        self.assertLess(p.rr_ratio, 1.0)
        self.assertAlmostEqual(p.rr_ratio, 0.06 / 0.30, places=2)

    def test_volume_confirmation_in_certificate(self):
        p_ok = pf.build_fade_play("AAA", 2.0, 30.0, vol_ratio=5.0)
        p_weak = pf.build_fade_play("AAA", 2.0, 30.0, vol_ratio=1.0)
        self.assertTrue(p_ok.certificate["volume_confirmed"])
        self.assertFalse(p_weak.certificate["volume_confirmed"])
        self.assertTrue(any("объём не подтверждён" in r for r in p_weak.reasons))

    def test_ledger_compatible_fields(self):
        p = pf.build_fade_play("AAA", 2.0, 30.0)
        for attr in ("asset", "direction", "entry", "target", "stop",
                     "score", "rr_ratio", "certificate", "reasons"):
            self.assertTrue(hasattr(p, attr), attr)


class FormatTest(unittest.TestCase):
    def test_format_is_honest_fade_not_buy(self):
        p = pf.build_fade_play("AAA", 2.0, 40.0, vol_ratio=5.0)
        msg = pf.format_fade_play(p, capital=1000)
        self.assertIn("FADE", msg)
        self.assertIn("ШОРТ", msg)
        self.assertNotIn("покупай", msg.lower())
        self.assertIn("60%", msg)                 # win-rate
        self.assertIn("Хвост", msg)               # предупреждение о хвосте
        self.assertIn("$20", msg)                 # 2% от $1000 = малый размер


class ResolverIntegrationTest(unittest.TestCase):
    """Fade-план корректно резолвится edge-леджером как SHORT."""
    def _candles(self, lows, highs):
        t0 = datetime(2025, 1, 1)
        return [C(t0 + timedelta(hours=i + 1), h, lo, (h + lo) / 2)
                for i, (lo, h) in enumerate(zip(lows, highs))]

    def test_short_take_profit_on_pullback(self):
        p = pf.build_fade_play("AAA", 100.0, 30.0)  # entry 100, target 94, stop 130
        emitted = datetime(2025, 1, 1)
        candles = self._candles(lows=[93.0], highs=[101.0])  # low<=target → TP
        status, exit_px, pnl, _ = resolve_against_candles(
            "SHORT", p.entry, p.target, p.stop, candles, emitted, p.horizon_hours)
        self.assertEqual(status, "tp")
        self.assertGreater(pnl, 0)

    def test_short_stop_on_new_leg_up(self):
        p = pf.build_fade_play("AAA", 100.0, 30.0)  # stop 130
        emitted = datetime(2025, 1, 1)
        candles = self._candles(lows=[100.0], highs=[131.0])  # high>=stop → SL
        status, exit_px, pnl, _ = resolve_against_candles(
            "SHORT", p.entry, p.target, p.stop, candles, emitted, p.horizon_hours)
        self.assertEqual(status, "sl")
        self.assertLess(pnl, 0)


class ScannerWiringTest(unittest.TestCase):
    def test_attach_and_format(self):
        import pump_scanner as ps
        # большой памп → fade навешивается
        sig = ps.PumpSignal(asset="AAA", pump_pct=25.0, vol_ratio=5.0, prior_pct=3.0,
                            price_from=80.0, price_to=100.0, window_min=30,
                            venues=["binance"], mcap=50_000_000.0)
        ps.attach_fade_to_signal(sig)
        self.assertIsNotNone(sig.fade)
        msg = ps.format_pump_alert(sig)
        self.assertIn("FADE", msg)
        self.assertIn("не сигнал на покупку", msg)  # старое предупреждение осталось

    def test_small_pump_no_fade(self):
        import pump_scanner as ps
        sig = ps.PumpSignal(asset="BBB", pump_pct=6.0, vol_ratio=4.0, prior_pct=2.0,
                            price_from=98.0, price_to=100.0, window_min=30,
                            venues=["binance"])
        ps.attach_fade_to_signal(sig)
        self.assertIsNone(sig.fade)              # 6% < 15% → нет fade
        msg = ps.format_pump_alert(sig)
        self.assertNotIn("FADE", msg)


if __name__ == "__main__":
    unittest.main()
