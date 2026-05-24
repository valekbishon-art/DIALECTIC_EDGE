from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.btc_alerts import (
    BTCAlertSnapshot,
    feature_enabled,
    format_btc_alert_headline,
    get_alert_chat_ids,
    get_alert_confidence_delta,
    get_alert_cooldown_sec,
    get_alert_interval_sec,
    get_alert_min_confidence,
    should_fire_btc_alert,
)
from core.btc_outlook import (
    LEAN_BEAR,
    LEAN_BULL,
    LEAN_NEUTRAL,
    BTCOutlookInputs,
    BTCSignalContribution,
    BTCOutlookVerdict,
    compute_btc_outlook,
)


def _verdict(lean: str, conf: int, has_signals: bool = True) -> BTCOutlookVerdict:
    sigs: tuple[BTCSignalContribution, ...] = ()
    if has_signals:
        sigs = (
            BTCSignalContribution(
                name="price_24h",
                label="Цена 24ч",
                direction=1 if lean == LEAN_BULL else -1 if lean == LEAN_BEAR else 0,
                weight=0.5,
                raw_value="+2.5%",
                explanation="",
            ),
        )
    return BTCOutlookVerdict(
        lean=lean,
        confidence_pct=conf,
        net_score=0.5 if lean == LEAN_BULL else -0.5 if lean == LEAN_BEAR else 0.0,
        bull_score=0.5 if lean == LEAN_BULL else 0.0,
        bear_score=0.5 if lean == LEAN_BEAR else 0.0,
        contributions=sigs,
        summary="",
        fallback_used=False,
        inputs_seen=1 if has_signals else 0,
    )


class TestBTCAlertEnv(unittest.TestCase):
    def test_feature_enabled_default_on(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(feature_enabled())

    def test_feature_disabled_via_zero(self):
        with patch.dict(os.environ, {"FEATURE_BTC_OUTLOOK_ALERTS": "0"}, clear=True):
            self.assertFalse(feature_enabled())

    def test_default_min_confidence_70(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_alert_min_confidence(), 70)

    def test_default_confidence_delta_15(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_alert_confidence_delta(), 15)

    def test_default_interval_1800(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_alert_interval_sec(), 1800)

    def test_default_cooldown_7200(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_alert_cooldown_sec(), 7200)

    def test_chat_ids_empty_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_alert_chat_ids(), ())

    def test_chat_ids_parsed_from_csv(self):
        with patch.dict(
            os.environ, {"BTC_OUTLOOK_ALERT_CHAT_IDS": "111,222, 333 ,abc"}, clear=True
        ):
            self.assertEqual(get_alert_chat_ids(), (111, 222, 333))

    def test_env_overrides_clamped(self):
        with patch.dict(
            os.environ,
            {
                "BTC_OUTLOOK_ALERT_MIN_CONFIDENCE": "150",  # clamped to 100
                "BTC_OUTLOOK_ALERT_CONFIDENCE_DELTA": "-5",  # clamped to 0
            },
            clear=True,
        ):
            self.assertEqual(get_alert_min_confidence(), 100)
            self.assertEqual(get_alert_confidence_delta(), 0)


class TestBTCAlertFirstFire(unittest.TestCase):
    def test_first_run_high_confidence_fires(self):
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BULL, 75),
            previous=None,
            now_ts=1000.0,
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertTrue(d.should_fire)
        self.assertIn("first-fire", d.reason)

    def test_first_run_low_confidence_holds(self):
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BULL, 50),
            previous=None,
            now_ts=1000.0,
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertFalse(d.should_fire)
        self.assertIn("confidence 50 < min 70", d.suppressed_reason)

    def test_first_run_neutral_never_fires(self):
        d = should_fire_btc_alert(
            current=_verdict(LEAN_NEUTRAL, 0),
            previous=None,
            now_ts=1000.0,
        )
        self.assertFalse(d.should_fire)
        self.assertIn("neutral", d.suppressed_reason)


class TestBTCAlertLeanFlip(unittest.TestCase):
    def test_bull_to_bear_fires(self):
        prev = BTCAlertSnapshot(lean=LEAN_BULL, confidence_pct=72, fired_at_ts=0.0)
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BEAR, 80),
            previous=prev,
            now_ts=10000.0,  # > cooldown
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertTrue(d.should_fire)
        self.assertIn("lean flip", d.reason)
        self.assertIn("BULL", d.reason)
        self.assertIn("BEAR", d.reason)

    def test_neutral_to_bear_fires(self):
        prev = BTCAlertSnapshot(lean=LEAN_NEUTRAL, confidence_pct=20, fired_at_ts=0.0)
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BEAR, 75),
            previous=prev,
            now_ts=10000.0,
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertTrue(d.should_fire)
        self.assertIn("lean flip", d.reason)


class TestBTCAlertConfidenceJump(unittest.TestCase):
    def test_same_lean_big_jump_fires(self):
        prev = BTCAlertSnapshot(lean=LEAN_BULL, confidence_pct=70, fired_at_ts=0.0)
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BULL, 90),
            previous=prev,
            now_ts=10000.0,
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertTrue(d.should_fire)
        self.assertIn("confidence jump", d.reason)

    def test_same_lean_small_jump_holds(self):
        prev = BTCAlertSnapshot(lean=LEAN_BULL, confidence_pct=70, fired_at_ts=0.0)
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BULL, 75),  # +5 < 15 delta
            previous=prev,
            now_ts=10000.0,
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertFalse(d.should_fire)
        self.assertIn("Δ=5", d.suppressed_reason)


class TestBTCAlertCooldown(unittest.TestCase):
    def test_within_cooldown_blocks_lean_flip(self):
        prev = BTCAlertSnapshot(lean=LEAN_BULL, confidence_pct=80, fired_at_ts=1000.0)
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BEAR, 90),
            previous=prev,
            now_ts=1500.0,  # 500s < 7200s
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertFalse(d.should_fire)
        self.assertIn("cooldown", d.suppressed_reason)

    def test_after_cooldown_lean_flip_fires(self):
        prev = BTCAlertSnapshot(lean=LEAN_BULL, confidence_pct=80, fired_at_ts=1000.0)
        d = should_fire_btc_alert(
            current=_verdict(LEAN_BEAR, 90),
            previous=prev,
            now_ts=10000.0,
            min_confidence=70,
            confidence_delta=15,
            cooldown_sec=7200,
        )
        self.assertTrue(d.should_fire)


class TestBTCAlertEmptySignals(unittest.TestCase):
    def test_no_signals_never_fires(self):
        # If all sources failed, contributions=() — alert must hold back.
        v = _verdict(LEAN_BULL, 80, has_signals=False)
        d = should_fire_btc_alert(
            current=v,
            previous=None,
            now_ts=1000.0,
        )
        self.assertFalse(d.should_fire)
        self.assertIn("no signals", d.suppressed_reason)


class TestBTCAlertFormatHeadline(unittest.TestCase):
    def test_headline_contains_lean_and_confidence(self):
        v = _verdict(LEAN_BULL, 78)
        from core.btc_alerts import BTCAlertDecision

        d = BTCAlertDecision(True, "lean flip: BEAR→BULL")
        hl = format_btc_alert_headline(d, v)
        self.assertIn("BTC outlook alert", hl)
        self.assertIn("BULL", hl)
        self.assertIn("78%", hl)
        self.assertIn("lean flip", hl)


class TestBTCAlertIntegrationWithCompute(unittest.TestCase):
    """Sanity: compute_btc_outlook output feeds cleanly into should_fire."""

    def test_bullish_inputs_alert_fires_on_first_run(self):
        v = compute_btc_outlook(
            BTCOutlookInputs(
                btc_price_usd=68000.0,
                price_change_24h_pct=3.5,
                oi_change_24h_pct=4.0,
                top_trader_ls_ratio=1.6,
                etf_basket_change_5d_avg_pct=1.4,
                fear_greed_index=55,
                quant_verdict_direction="LONG",
                quant_verdict_strength=0.7,
            )
        )
        # If compute says BULL with confidence ≥ 70, alert fires on first run.
        if v.lean == LEAN_BULL and v.confidence_pct >= 70:
            d = should_fire_btc_alert(
                current=v,
                previous=None,
                now_ts=1000.0,
                min_confidence=70,
                confidence_delta=15,
                cooldown_sec=7200,
            )
            self.assertTrue(d.should_fire)


if __name__ == "__main__":
    unittest.main()
