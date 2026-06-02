"""Тесты багов из лог-разбора (02.06): масс-выход на сбое данных, переворот
направления арба, листинги акций/pre-IPO, метка окна волатильности."""
from __future__ import annotations

import unittest

from core.carry_briefing import arb_close_alerts, listing_block, _is_equity_perp
from core.regime_radar import format_regime_md


class ArbHealthGuardTest(unittest.TestCase):
    def test_no_mass_close_when_unhealthy(self):
        # Биржи не отдали данных (cur пуст, healthy=False) — НЕ закрываем 10 позиций.
        prev = {a: (30.0, "Gate", "Hyperliquid") for a in
                ("FIL", "NEAR", "XRP", "ATOM", "BCH", "XLM", "APT", "ORDI", "TRX", "INJ")}
        msgs = arb_close_alerts(prev, {}, cur_healthy=False)
        self.assertEqual(msgs, [])

    def test_closes_when_healthy_and_gone(self):
        prev = {"DOT": (30.0, "Gate", "Hyperliquid")}
        msgs = arb_close_alerts(prev, {}, cur_healthy=True)
        self.assertEqual(len(msgs), 1)
        self.assertIn("ЗАКРЫВАЙ АРБ DOT", msgs[0])
        self.assertIn("исчез", msgs[0])


class ArbDirectionFlipTest(unittest.TestCase):
    def test_direction_flip_triggers_reopen(self):
        # Направление перевернулось: было ШОРТ Gate/ЛОНГ HL, стало наоборот.
        prev = {"DOT": (40.0, "Gate", "Hyperliquid")}
        cur = {"DOT": (30.0, "Hyperliquid", "Gate")}
        msgs = arb_close_alerts(prev, cur)
        self.assertEqual(len(msgs), 1)
        self.assertIn("ПЕРЕВОРОТ АРБ DOT", msgs[0])
        self.assertIn("ШОРТ Hyperliquid", msgs[0])

    def test_same_direction_spread_shrinks_no_alert(self):
        # Спред упал 72→36 но НАПРАВЛЕНИЕ то же и >порога → не дёргаем (как BERSERK DOT).
        prev = {"DOT": (72.0, "Hyperliquid", "Gate")}
        cur = {"DOT": (36.0, "Hyperliquid", "Gate")}
        self.assertEqual(arb_close_alerts(prev, cur), [])

    def test_backcompat_float_values(self):
        # Старый формат (голый float) не падает и не выдаёт ложный переворот.
        self.assertEqual(arb_close_alerts({"BTC": 40.0}, {"BTC": 35.0}), [])
        self.assertEqual(len(arb_close_alerts({"BTC": 40.0}, {"BTC": 5.0})), 1)


class EquityListingTest(unittest.TestCase):
    def test_detects_equity_perps(self):
        for s in ("SAMSUNGUSDT", "HYUNDAIUSDT", "SKHYNIXUSDT", "ANTHROPICUSDT", "ASTSUSDT"):
            self.assertTrue(_is_equity_perp(s), s)

    def test_crypto_not_flagged(self):
        for s in ("BTCUSDT", "DOTUSDT", "PEPEUSDT", "SOLUSDT"):
            self.assertFalse(_is_equity_perp(s), s)

    def test_block_warns_on_equity(self):
        listings = [{"symbol": "ANTHROPICUSDT", "age_h": 10},
                    {"symbol": "SAMSUNGUSDT", "age_h": 11}]
        msg = listing_block(listings)
        self.assertIn("акция/pre-IPO", msg)
        self.assertIn("НЕ применима", msg)
        self.assertIn("по крипто-токенам", msg)  # стат явно про крипто

    def test_block_no_equity_note_for_pure_crypto(self):
        msg = listing_block([{"symbol": "FOOUSDT", "age_h": 3}])
        self.assertNotIn("акция/pre-IPO", msg)


class RegimeWindowLabelTest(unittest.TestCase):
    def test_window_labeled(self):
        r = {"label": "СПОКОЙНЫЙ рынок", "emoji": "🟢", "rv30": 29.3, "rv7": 17.0,
             "pct": 0.24, "rising": False, "carry_size": 1.0, "action": "ок"}
        msg = format_regime_md(r)
        self.assertIn("30д: 29.3%", msg)
        self.assertIn("7д: 17.0%", msg)
        self.assertNotIn("29.3% годовых\n", msg)  # старая безоконная формулировка ушла


if __name__ == "__main__":
    unittest.main()
