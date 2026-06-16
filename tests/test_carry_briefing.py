"""Тесты сигналов ЗАКРЫВАЙ для carry и кросс-арба (чистая логика, без сети)."""
from __future__ import annotations

import os
import tempfile
import unittest

from core.carry_briefing import (arb_close_alerts, cap_state, close_alerts,
                                  load_monitor_state, save_monitor_state,
                                  scan_carry_open, select_tracked_arb,
                                  select_tracked_carry)


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


class TrackingAntiSpamTest(unittest.TestCase):
    """Гистерезис + кап: трекаем только рекомендованное, без пачки ЗАКРЫВАЙ."""

    def test_carry_enters_only_strong(self):
        # STRONG=20, THIN=8. Входим только на ≥20; 15% не входит (не рекомендуем).
        new = select_tracked_carry({}, {"BTC": 25.0, "ETH": 15.0, "SOL": 30.0})
        self.assertEqual(set(new), {"BTC", "SOL"})

    def test_carry_holds_below_strong_while_above_thin(self):
        # Уже в позиции ETH (была STRONG), просела до 15% (≥THIN) → ДЕРЖИМ, не закрываем.
        new = select_tracked_carry({"ETH": 22.0}, {"ETH": 15.0})
        self.assertEqual(new, {"ETH": 15.0})
        self.assertEqual(close_alerts({"ETH": 22.0}, new), [])

    def test_carry_closes_when_below_thin(self):
        # Позиция XRP упала ниже THIN → нет в full_open → закрытие.
        new = select_tracked_carry({"XRP": 25.0}, {})
        self.assertEqual(new, {})
        self.assertEqual(len(close_alerts({"XRP": 25.0}, new)), 1)

    def test_carry_cap_limits_new_entries(self):
        full = {"A": 30.0, "B": 29.0, "C": 28.0, "D": 27.0}
        new = select_tracked_carry({}, full, max_track=3)
        self.assertEqual(len(new), 3)

    def test_arb_caps_to_top_by_spread(self):
        full = {"A": (40.0, "X", "Y"), "B": (30.0, "X", "Y"),
                "C": (20.0, "X", "Y"), "D": (15.0, "X", "Y")}
        new = select_tracked_arb({}, full, max_track=2)
        self.assertEqual(set(new), {"A", "B"})

    def test_arb_keeps_survivor(self):
        # Низкоспредовая, но уже в позиции — держим (выживший добавляется первым).
        full = {"D": (13.0, "X", "Y"), "A": (40.0, "X", "Y")}
        new = select_tracked_arb({"D": (15.0, "X", "Y")}, full, max_track=2)
        self.assertIn("D", new)

    def test_cap_state_trims_to_top(self):
        self.assertEqual(set(cap_state({"A": 30.0, "B": 10.0, "C": 20.0}, 2)), {"A", "C"})

    def test_cap_state_by_spread(self):
        st = {"A": (5.0, "X", "Y"), "B": (40.0, "X", "Y"), "C": (20.0, "X", "Y")}
        self.assertEqual(set(cap_state(st, 2, by_spread=True)), {"B", "C"})

    def test_no_burst_from_stale_state(self):
        # Старое раздутое состояние (20 арбов) капается до 3 при загрузке —
        # моделируем: cap_state -> select -> close_alerts не должен дать 20 закрытий.
        stale = {f"C{i}": (12.0 + i, "X", "Y") for i in range(20)}
        capped = cap_state(stale, 3, by_spread=True)
        self.assertEqual(len(capped), 3)


if __name__ == "__main__":
    unittest.main()
