"""Тесты сигналов ЗАКРЫВАЙ для carry и кросс-арба (чистая логика, без сети)."""
from __future__ import annotations

import unittest

from core.carry_briefing import close_alerts, arb_close_alerts


class CloseAlertsTest(unittest.TestCase):
    def test_no_alert_when_still_open(self):
        # Актив всё ещё в carry — закрывать нечего.
        self.assertEqual(close_alerts({"BTC": 25.0}, {"BTC": 22.0}), [])

    def test_alert_when_dropped(self):
        # Актив был, исчез из cur → сигнал ЗАКРЫВАЙ.
        msgs = close_alerts({"BTC": 25.0}, {})
        self.assertEqual(len(msgs), 1)
        self.assertIn("ЗАКРЫВАЙ BTC", msgs[0])

    def test_only_dropped_assets_alert(self):
        # ETH остался, SOL ушёл — алерт только по SOL.
        msgs = close_alerts({"ETH": 30.0, "SOL": 18.0}, {"ETH": 28.0})
        self.assertEqual(len(msgs), 1)
        self.assertIn("SOL", msgs[0])


class ArbCloseAlertsTest(unittest.TestCase):
    def test_no_alert_when_spread_holds(self):
        # Спред выше порога — держим позицию.
        self.assertEqual(arb_close_alerts({"BTC": 40.0}, {"BTC": 35.0}), [])

    def test_alert_when_spread_vanished(self):
        msgs = arb_close_alerts({"BTC": 40.0}, {})
        self.assertEqual(len(msgs), 1)
        self.assertIn("ЗАКРЫВАЙ АРБ BTC", msgs[0])
        self.assertIn("исчез", msgs[0])

    def test_alert_when_spread_below_min_keep(self):
        # Спред упал ниже min_keep (12%) → закрываем.
        msgs = arb_close_alerts({"SOL": 30.0}, {"SOL": 8.0})
        self.assertEqual(len(msgs), 1)
        self.assertIn("упал до 8%", msgs[0])

    def test_custom_min_keep(self):
        # С повышенным порогом 20% спред 15% уже сигнал на выход.
        msgs = arb_close_alerts({"SOL": 30.0}, {"SOL": 15.0}, min_keep=20.0)
        self.assertEqual(len(msgs), 1)
        self.assertIn("ЗАКРЫВАЙ АРБ SOL", msgs[0])


if __name__ == "__main__":
    unittest.main()
