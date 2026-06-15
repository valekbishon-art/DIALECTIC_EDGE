"""Тесты сигналов ЗАКРЫВАЙ для carry и кросс-арба (чистая логика, без сети)."""
from __future__ import annotations

import os
import tempfile
import unittest

from core.carry_briefing import (arb_close_alerts, close_alerts,
                                  load_monitor_state, save_monitor_state,
                                  scan_carry_open)


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

    def test_unhealthy_suppresses_mass_exit(self):
        # БАГ-ФИКС: пустой/сбойный фетч (cur_healthy=False) НЕ должен закрывать
        # все позиции разом, даже если cur_open пуст.
        self.assertEqual(
            close_alerts({"BTC": 25.0, "ETH": 30.0}, {}, cur_healthy=False), [])

    def test_healthy_default_still_alerts(self):
        # По умолчанию cur_healthy=True — поведение прежнее.
        self.assertEqual(len(close_alerts({"BTC": 25.0}, {})), 1)


class ScanCarryOpenTest(unittest.TestCase):
    def test_empty_funding_is_unhealthy(self):
        # fetch_funding вернул {} → healthy=False, open пуст (НЕ значит «carry исчез»).
        open_state, pos, healthy = scan_carry_open(data={})
        self.assertFalse(healthy)
        self.assertEqual(open_state, {})
        self.assertEqual(pos, [])

    def test_positive_funding_opens_state(self):
        # rate=0.0002 при 8ч-интервале ≈ +21.9% годовых ≥ THIN(8%) → попадает в open.
        data = {"BTCUSDT": {"rate": 0.0002, "interval_h": 8.0, "next": None}}
        open_state, pos, healthy = scan_carry_open(data=data)
        self.assertTrue(healthy)
        self.assertIn("BTC", open_state)
        self.assertTrue(open_state["BTC"] > 8.0)


class MonitorStatePersistenceTest(unittest.TestCase):
    def test_roundtrip(self):
        carry = {"BTC": 21.9, "ETH": 15.0}
        # arb-значение как кортеж; после JSON станет списком — это ок (_arb_unpack понимает).
        arb = {"SOL": (30.0, "Binance", "Gate", 30.0, 0.0)}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "carry_monitor_state.json")
            save_monitor_state(path, carry, arb)
            lc, la = load_monitor_state(path)
        self.assertEqual(lc, carry)
        self.assertEqual(la["SOL"], [30.0, "Binance", "Gate", 30.0, 0.0])

    def test_missing_file_returns_empty(self):
        lc, la = load_monitor_state(os.path.join(tempfile.gettempdir(), "no_such_carry_state_xyz.json"))
        self.assertEqual((lc, la), ({}, {}))


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
