from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")

# unit-fast CI job ставит минимальный набор зависимостей без aiogram.
# refactor.handlers.sniping_handler импортит aiogram на верхнем уровне,
# поэтому весь модуль тестов нужно гардить — иначе ImportError при
# `unittest discover` (см. tests/test_signal_explain_button.py паттерн).
try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

if HAS_AIOGRAM:
    from core.support_resistance import Level, SRLevels
    from refactor.handlers.sniping_handler import (
        SniperReport,
        build_sniper_plans_for_asset,
        format_sniper_report,
        parse_sniping_callback_data,
        sniping_callback_data,
    )


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestSnipingCallbackData(unittest.TestCase):
    def test_roundtrip(self):
        data = sniping_callback_data(42, 250)
        self.assertEqual(data, "sniping:42:250.00")
        self.assertEqual(parse_sniping_callback_data(data), (42, 250.0))

    def test_malformed_returns_none(self):
        self.assertIsNone(parse_sniping_callback_data("sniping:bad"))
        self.assertIsNone(parse_sniping_callback_data(""))


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestSniperPlans(unittest.TestCase):
    def test_long_support_plan_uses_dynamic_risk_size(self):
        sr_by_tf = {
            "H1": SRLevels(
                resistances=[Level(price=112.0, touches=2, last_idx=10, kind="HIGH", score=2.5)],
                supports=[
                    Level(price=98.0, touches=3, last_idx=20, kind="LOW", score=3.5),
                    Level(price=94.0, touches=2, last_idx=12, kind="LOW", score=2.2),
                ],
            )
        }
        plans = build_sniper_plans_for_asset(
            asset="BTC",
            direction="LONG",
            current_price=100.0,
            sr_by_tf=sr_by_tf,
            capital=1000.0,
            max_plans=1,
        )
        self.assertEqual(len(plans), 1)
        p = plans[0]
        self.assertEqual(p.direction, "LONG")
        self.assertLess(p.entry, 100.0)
        self.assertLess(p.stop, p.entry)
        self.assertGreater(p.target, p.entry)
        self.assertGreaterEqual(p.rr_ratio, 1.2)
        self.assertGreater(p.position_value, 0)
        self.assertGreater(p.risk_amount, 0)

    def test_short_resistance_plan_uses_dynamic_risk_size(self):
        # Симметричный SHORT-кейс: ждём вынос вверх к сопротивлению, SL
        # выше следующего сопротивления, TP вниз к ближайшей поддержке.
        sr_by_tf = {
            "H1": SRLevels(
                resistances=[
                    Level(price=102.0, touches=3, last_idx=20, kind="HIGH", score=3.5),
                    Level(price=106.0, touches=2, last_idx=12, kind="HIGH", score=2.2),
                ],
                supports=[Level(price=88.0, touches=2, last_idx=10, kind="LOW", score=2.5)],
            )
        }
        plans = build_sniper_plans_for_asset(
            asset="BTC",
            direction="SHORT",
            current_price=100.0,
            sr_by_tf=sr_by_tf,
            capital=1000.0,
            max_plans=1,
        )
        self.assertEqual(len(plans), 1)
        p = plans[0]
        self.assertEqual(p.direction, "SHORT")
        self.assertGreater(p.entry, 100.0)
        self.assertGreater(p.stop, p.entry)
        self.assertLess(p.target, p.entry)
        self.assertGreaterEqual(p.rr_ratio, 1.2)
        self.assertGreater(p.position_value, 0)
        self.assertGreater(p.risk_amount, 0)

    def test_no_levels_returns_empty(self):
        plans = build_sniper_plans_for_asset(
            asset="BTC",
            direction="LONG",
            current_price=100.0,
            sr_by_tf={},
            capital=1000.0,
        )
        self.assertEqual(plans, [])

    def test_empty_report_says_no_sniper_levels(self):
        text = format_sniper_report(SniperReport("BTC", "LONG", 100.0, 55, [], "мало данных"))
        self.assertIn("снайперских лимиток нет", text)
        self.assertIn("мало данных", text)


if __name__ == "__main__":
    unittest.main()
