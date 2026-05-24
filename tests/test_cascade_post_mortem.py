"""Pure-math tests for cascade_post_mortem."""

from __future__ import annotations

import unittest

from market_indicators.cascade_post_mortem import (
    DEFAULT_THRESHOLD_24H_USD,
    DEFAULT_THRESHOLD_4H_ACUTE_USD,
    SIDE_LONG,
    SIDE_SHORT,
    WINDOW_TYPE_24H,
    WINDOW_TYPE_4H_ACUTE,
    CascadeSnapshot,
    LiquidationEvent,
    WindowAggregate,
    aggregate_24h,
    aggregate_4h,
    aggregate_window,
    attribute_signals,
    derive_action_items,
    format_post_mortem_markdown,
    should_trigger,
)

# Helpers
MS_PER_HOUR = 3600 * 1000


def _ev(
    *,
    age_h: float,
    now_ms: int,
    side: str = SIDE_LONG,
    value: float = 1_000_000.0,
    venue: str = "binance",
    symbol: str = "BTCUSDT",
) -> LiquidationEvent:
    return LiquidationEvent(
        timestamp_ms=now_ms - int(age_h * MS_PER_HOUR),
        venue=venue,
        symbol=symbol,
        side=side,
        value_usd=value,
    )


class TestAggregateWindow(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_700_000_000_000

    def test_empty_events_yields_zero(self) -> None:
        agg = aggregate_24h([], now_ms=self.now)
        self.assertEqual(agg.total_usd, 0.0)
        self.assertEqual(agg.long_usd, 0.0)
        self.assertEqual(agg.short_usd, 0.0)
        self.assertEqual(agg.event_count, 0)

    def test_includes_events_within_window(self) -> None:
        events = [
            _ev(age_h=0.5, now_ms=self.now, side=SIDE_LONG, value=10_000_000),
            _ev(age_h=12, now_ms=self.now, side=SIDE_SHORT, value=5_000_000),
            _ev(age_h=23, now_ms=self.now, side=SIDE_LONG, value=2_000_000),
        ]
        agg = aggregate_24h(events, now_ms=self.now)
        self.assertEqual(agg.total_usd, 17_000_000)
        self.assertEqual(agg.long_usd, 12_000_000)
        self.assertEqual(agg.short_usd, 5_000_000)
        self.assertEqual(agg.event_count, 3)
        self.assertEqual(agg.window_hours, 24)

    def test_excludes_events_outside_window(self) -> None:
        events = [
            _ev(age_h=25, now_ms=self.now, value=999_000_000),  # past window
            _ev(age_h=0.1, now_ms=self.now, value=1_000_000),
        ]
        agg = aggregate_24h(events, now_ms=self.now)
        self.assertEqual(agg.total_usd, 1_000_000)
        self.assertEqual(agg.event_count, 1)

    def test_excludes_future_events(self) -> None:
        future = LiquidationEvent(
            timestamp_ms=self.now + 60_000,
            venue="binance",
            symbol="BTCUSDT",
            side=SIDE_LONG,
            value_usd=999_000,
        )
        agg = aggregate_24h([future], now_ms=self.now)
        self.assertEqual(agg.total_usd, 0.0)

    def test_4h_window_smaller_than_24h(self) -> None:
        events = [
            _ev(age_h=0.1, now_ms=self.now, value=10_000_000),
            _ev(age_h=3.5, now_ms=self.now, value=10_000_000),
            _ev(age_h=5, now_ms=self.now, value=10_000_000),  # outside 4h
        ]
        agg4 = aggregate_4h(events, now_ms=self.now)
        self.assertEqual(agg4.total_usd, 20_000_000)
        self.assertEqual(agg4.event_count, 2)
        self.assertEqual(agg4.window_type, WINDOW_TYPE_4H_ACUTE)
        self.assertEqual(agg4.window_hours, 4)

    def test_negative_window_returns_empty(self) -> None:
        events = [_ev(age_h=0.1, now_ms=self.now, value=10_000_000)]
        agg = aggregate_window(
            events, now_ms=self.now, window_seconds=0, window_type="empty"
        )
        self.assertEqual(agg.total_usd, 0.0)

    def test_skips_events_with_zero_value(self) -> None:
        events = [
            _ev(age_h=0.1, now_ms=self.now, value=0),
            _ev(age_h=0.2, now_ms=self.now, value=-100),  # invalid (negative)
            _ev(age_h=0.3, now_ms=self.now, value=1_000_000),
        ]
        agg = aggregate_24h(events, now_ms=self.now)
        self.assertEqual(agg.total_usd, 1_000_000)
        self.assertEqual(agg.event_count, 1)


class TestWindowAggregateProps(unittest.TestCase):
    def test_long_share_zero_when_total_zero(self) -> None:
        w = WindowAggregate(
            window_type=WINDOW_TYPE_24H,
            window_hours=24,
            total_usd=0,
            long_usd=0,
            short_usd=0,
            event_count=0,
        )
        self.assertEqual(w.long_share, 0.0)
        self.assertEqual(w.dominant_side, "mixed")

    def test_dominant_long_when_share_above_60pct(self) -> None:
        w = WindowAggregate(
            window_type=WINDOW_TYPE_24H,
            window_hours=24,
            total_usd=100,
            long_usd=70,
            short_usd=30,
            event_count=2,
        )
        self.assertEqual(w.dominant_side, SIDE_LONG)
        self.assertAlmostEqual(w.long_share, 0.70)

    def test_dominant_short_when_long_share_below_40pct(self) -> None:
        w = WindowAggregate(
            window_type=WINDOW_TYPE_24H,
            window_hours=24,
            total_usd=100,
            long_usd=20,
            short_usd=80,
            event_count=2,
        )
        self.assertEqual(w.dominant_side, SIDE_SHORT)

    def test_dominant_mixed_when_in_middle(self) -> None:
        w = WindowAggregate(
            window_type=WINDOW_TYPE_24H,
            window_hours=24,
            total_usd=100,
            long_usd=50,
            short_usd=50,
            event_count=2,
        )
        self.assertEqual(w.dominant_side, "mixed")


class TestShouldTrigger(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_700_000_000_000

    def _agg(self, total: float, side: str = SIDE_LONG, window_type: str = WINDOW_TYPE_24H) -> WindowAggregate:
        return WindowAggregate(
            window_type=window_type,
            window_hours=24 if window_type == WINDOW_TYPE_24H else 4,
            total_usd=total,
            long_usd=total if side == SIDE_LONG else 0,
            short_usd=total if side == SIDE_SHORT else 0,
            event_count=1,
        )

    def test_no_trigger_below_thresholds(self) -> None:
        d = should_trigger(
            agg_24h=self._agg(10_000_000, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(5_000_000, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
        )
        self.assertFalse(d.should_fire)
        self.assertIn("below", d.reason)

    def test_trigger_on_24h_threshold(self) -> None:
        d = should_trigger(
            agg_24h=self._agg(600_000_000, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(100_000_000, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
        )
        self.assertTrue(d.should_fire)
        self.assertIsNotNone(d.window)
        assert d.window is not None
        self.assertEqual(d.window.window_type, WINDOW_TYPE_24H)

    def test_trigger_on_4h_acute_threshold(self) -> None:
        d = should_trigger(
            agg_24h=self._agg(100_000_000, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(300_000_000, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
        )
        self.assertTrue(d.should_fire)
        assert d.window is not None
        self.assertEqual(d.window.window_type, WINDOW_TYPE_4H_ACUTE)

    def test_4h_acute_has_priority_over_24h(self) -> None:
        # обе сработали — 4h приоритет (более острый сигнал)
        d = should_trigger(
            agg_24h=self._agg(800_000_000, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(300_000_000, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
        )
        self.assertTrue(d.should_fire)
        assert d.window is not None
        self.assertEqual(d.window.window_type, WINDOW_TYPE_4H_ACUTE)

    def test_cooldown_blocks_trigger(self) -> None:
        # каскад случился 1 час назад, cooldown 6h — не триггерим
        d = should_trigger(
            agg_24h=self._agg(800_000_000, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(0, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
            last_triggered_ms=self.now - 1 * 3600 * 1000,
            cooldown_hours=6,
        )
        self.assertFalse(d.should_fire)
        self.assertIn("cooldown", d.reason)

    def test_cooldown_expired_allows_trigger(self) -> None:
        d = should_trigger(
            agg_24h=self._agg(800_000_000, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(0, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
            last_triggered_ms=self.now - 7 * 3600 * 1000,  # 7h ago
            cooldown_hours=6,
        )
        self.assertTrue(d.should_fire)

    def test_default_thresholds_match_constants(self) -> None:
        # ровно на пороге — должно сработать
        d = should_trigger(
            agg_24h=self._agg(DEFAULT_THRESHOLD_24H_USD, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(0, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
        )
        self.assertTrue(d.should_fire)

        d2 = should_trigger(
            agg_24h=self._agg(DEFAULT_THRESHOLD_24H_USD - 1, window_type=WINDOW_TYPE_24H),
            agg_4h=self._agg(DEFAULT_THRESHOLD_4H_ACUTE_USD, window_type=WINDOW_TYPE_4H_ACUTE),
            now_ms=self.now,
        )
        self.assertTrue(d2.should_fire)


class TestAttributeSignals(unittest.TestCase):
    def test_down_magnet_with_long_cascade_marks_as_saw(self) -> None:
        indicators = {
            "liquidation_magnet": {
                "label": "down_magnet",
                "is_strong_signal": True,
            }
        }
        saw, missed = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertTrue(any("DOWN" in s for s in saw))
        self.assertTrue(any("strong" in s for s in saw))
        self.assertEqual(missed, [])

    def test_down_magnet_with_short_cascade_marks_as_missed(self) -> None:
        indicators = {
            "liquidation_magnet": {
                "label": "down_magnet",
                "is_strong_signal": False,
            }
        }
        saw, missed = attribute_signals(
            dominant_side=SIDE_SHORT, indicators=indicators
        )
        self.assertEqual(saw, [])
        self.assertTrue(any("DOWN" in m for m in missed))

    def test_up_magnet_with_short_cascade_marks_as_saw(self) -> None:
        indicators = {"liquidation_magnet": {"label": "up_magnet"}}
        saw, missed = attribute_signals(
            dominant_side=SIDE_SHORT, indicators=indicators
        )
        self.assertTrue(any("UP" in s for s in saw))
        self.assertEqual(missed, [])

    def test_neutral_magnet_not_attributed(self) -> None:
        indicators = {"liquidation_magnet": {"label": "neutral"}}
        saw, missed = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertEqual(saw, [])
        self.assertEqual(missed, [])

    def test_smart_money_distributing_long_cascade(self) -> None:
        indicators = {"smart_money_wallets": {"label": "distributing"}}
        saw, missed = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertTrue(any("DISTRIBUTING" in s for s in saw))

    def test_smart_money_accumulating_short_cascade(self) -> None:
        indicators = {"smart_money_wallets": {"label": "accumulating"}}
        saw, missed = attribute_signals(
            dominant_side=SIDE_SHORT, indicators=indicators
        )
        self.assertTrue(any("ACCUMULATING" in s for s in saw))

    def test_etf_outflow_streak_long_cascade(self) -> None:
        indicators = {
            "btc_etf_flow": {"streak_days": 4, "severity": "WARN"}
        }
        saw, missed = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertTrue(any("ETF outflow" in s for s in saw))

    def test_etf_outflow_short_streak_below_threshold_not_attributed(self) -> None:
        indicators = {
            "btc_etf_flow": {"streak_days": 1, "severity": "WARN"}
        }
        saw, missed = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        # 1d streak — below 3d threshold
        self.assertFalse(any("ETF" in s for s in saw))

    def test_funding_inverted_with_long_cascade(self) -> None:
        indicators = {"funding_term": {"is_inverted": True}}
        saw, _ = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertTrue(any("INVERTED" in s for s in saw))

    def test_options_skew_put_premium_long_cascade(self) -> None:
        indicators = {"options_skew": {"skew_class": "put_premium"}}
        saw, _ = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertTrue(any("PUT-premium" in s for s in saw))

    def test_regime_crisis_attributed(self) -> None:
        indicators = {"regime": {"label": "crisis"}}
        saw, _ = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertTrue(any("CRISIS" in s for s in saw))

    def test_unknown_indicators_ignored(self) -> None:
        indicators = {"liquidation_magnet": {"label": "unknown"}}
        saw, missed = attribute_signals(
            dominant_side=SIDE_LONG, indicators=indicators
        )
        self.assertEqual(saw, [])
        self.assertEqual(missed, [])

    def test_empty_indicators_yields_empty(self) -> None:
        saw, missed = attribute_signals(
            dominant_side=SIDE_LONG, indicators={}
        )
        self.assertEqual(saw, [])
        self.assertEqual(missed, [])

    def test_mixed_cascade_skips_unidirectional_attribution(self) -> None:
        indicators = {
            "liquidation_magnet": {"label": "down_magnet"},
            "smart_money_wallets": {"label": "distributing"},
        }
        saw, missed = attribute_signals(
            dominant_side="mixed", indicators=indicators
        )
        # mixed → ни в saw ни в missed для directional сигналов
        self.assertEqual(saw, [])
        self.assertEqual(missed, [])


class TestActionItems(unittest.TestCase):
    def test_missed_more_than_saw_recommends_tuning(self) -> None:
        items = derive_action_items(
            dominant_side=SIDE_LONG,
            window_type=WINDOW_TYPE_24H,
            saw=[],
            missed=["a", "b", "c"],
        )
        self.assertTrue(any("Donастроить" in i for i in items))

    def test_4h_acute_with_signals_adds_confirmation_step(self) -> None:
        items = derive_action_items(
            dominant_side=SIDE_LONG,
            window_type=WINDOW_TYPE_4H_ACUTE,
            saw=["something"],
            missed=[],
        )
        self.assertTrue(any("4h acute" in i for i in items))

    def test_mixed_cascade_recommends_classifier_review(self) -> None:
        items = derive_action_items(
            dominant_side="mixed",
            window_type=WINDOW_TYPE_24H,
            saw=[],
            missed=[],
        )
        self.assertTrue(any("Mixed-side" in i for i in items))

    def test_clean_run_yields_ok_message(self) -> None:
        items = derive_action_items(
            dominant_side=SIDE_LONG,
            window_type=WINDOW_TYPE_24H,
            saw=["a", "b"],
            missed=[],
        )
        # saw>missed, 24h → должен быть ok
        self.assertTrue(any("Система отработала" in i for i in items))


class TestFormatPostMortemMarkdown(unittest.TestCase):
    def _snapshot(self, **kwargs) -> CascadeSnapshot:
        window = WindowAggregate(
            window_type=kwargs.get("window_type", WINDOW_TYPE_24H),
            window_hours=kwargs.get("window_hours", 24),
            total_usd=kwargs.get("total_usd", 600_000_000),
            long_usd=kwargs.get("long_usd", 400_000_000),
            short_usd=kwargs.get("short_usd", 200_000_000),
            event_count=kwargs.get("event_count", 1500),
        )
        return CascadeSnapshot(
            triggered_at_iso="2025-05-21 08:00:00",
            triggered_at_ms=1_700_000_000_000,
            window=window,
            indicators=kwargs.get("indicators", {}),
            debate_excerpt=kwargs.get("debate_excerpt"),
        )

    def test_includes_header_with_total(self) -> None:
        text = format_post_mortem_markdown(self._snapshot())
        self.assertIn("Cascade", text)
        self.assertIn("$600.0M", text)

    def test_includes_window_type_label(self) -> None:
        t24 = format_post_mortem_markdown(self._snapshot())
        self.assertIn("24h rolling", t24)
        t4 = format_post_mortem_markdown(
            self._snapshot(window_type=WINDOW_TYPE_4H_ACUTE, window_hours=4)
        )
        self.assertIn("4h acute", t4)

    def test_includes_long_vs_short_breakdown(self) -> None:
        text = format_post_mortem_markdown(self._snapshot())
        self.assertIn("Long flush", text)
        self.assertIn("Short squeeze", text)
        self.assertIn("$400.0M", text)
        self.assertIn("$200.0M", text)

    def test_includes_dominant_side(self) -> None:
        text = format_post_mortem_markdown(self._snapshot())
        self.assertIn("Dominant: *LONG*", text)

    def test_no_indicators_yields_empty_saw_block(self) -> None:
        text = format_post_mortem_markdown(self._snapshot())
        self.assertIn("Что МЫ видели", text)
        self.assertIn("никаких directional", text)

    def test_indicators_appear_in_saw_block(self) -> None:
        text = format_post_mortem_markdown(
            self._snapshot(
                indicators={
                    "liquidation_magnet": {
                        "label": "down_magnet",
                        "is_strong_signal": True,
                    }
                }
            )
        )
        self.assertIn("DOWN", text)
        self.assertIn("strong", text)

    def test_debate_excerpt_included_and_truncated(self) -> None:
        long_text = "a" * 1000
        text = format_post_mortem_markdown(
            self._snapshot(debate_excerpt=long_text)
        )
        self.assertIn("debate excerpt", text)
        self.assertIn("…", text)
        # Должно быть < 1000 знаков 'a'
        a_count = text.count("a")
        self.assertLess(a_count, 700)

    def test_no_debate_excerpt_omits_block(self) -> None:
        text = format_post_mortem_markdown(self._snapshot(debate_excerpt=None))
        self.assertNotIn("debate excerpt", text)

    def test_action_items_present(self) -> None:
        text = format_post_mortem_markdown(self._snapshot())
        self.assertIn("Action items", text)

    def test_triggered_at_footer(self) -> None:
        text = format_post_mortem_markdown(self._snapshot())
        self.assertIn("Triggered at 2025-05-21", text)


if __name__ == "__main__":
    unittest.main()
