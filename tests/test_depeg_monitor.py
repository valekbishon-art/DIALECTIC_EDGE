"""Тесты depeg_monitor — детект депега, тексты, авто-алерт без сети/БД.

Написаны как unittest.TestCase, чтобы их реально запускал CI
(`python -m unittest discover`). depeg_monitor не требует aiogram на импорте
(aiogram подтягивается лениво в _alert_kb), поэтому проходит и в unit-fast.
"""
import asyncio
import unittest

import depeg_monitor as dm


class TestDetect(unittest.TestCase):
    def test_no_opps_when_at_peg(self):
        prices = {"USDC": 1.0001, "TUSD": 0.9995, "USDP": 1.0, "FDUSD": 0.9991}
        self.assertEqual(dm.detect_opportunities(prices, entry=0.99, floor=0.90), [])

    def test_detects_and_sorts_by_depth(self):
        prices = {"USDC": 0.97, "TUSD": 0.985, "USDP": 1.0}
        opps = dm.detect_opportunities(prices, entry=0.99, floor=0.90)
        syms = [o["symbol"] for o in opps]
        self.assertEqual(syms, ["USDC", "TUSD"])  # глубже первым, USDP отфильтрован
        self.assertAlmostEqual(opps[0]["discount_pct"], 3.0, places=4)

    def test_severity_levels(self):
        prices = {"A": 0.988, "B": 0.94, "C": 0.85}
        opps = {o["symbol"]: o for o in
                dm.detect_opportunities(prices, entry=0.99, floor=0.90)}
        self.assertEqual(opps["A"]["severity"], "mild")
        self.assertEqual(opps["B"]["severity"], "deep")
        self.assertEqual(opps["C"]["severity"], "danger")  # ниже пола


class TestTexts(unittest.TestCase):
    def test_status_empty(self):
        txt = dm.format_status({}, [])
        self.assertIn("Не смог получить", txt)

    def test_status_shows_prices_and_no_opp(self):
        prices = {"USDC": 1.0, "TUSD": 0.9995}
        txt = dm.format_status(prices, [])
        self.assertIn("USDC", txt)
        self.assertIn("Все у пега", txt)

    def test_alert_text_mentions_symbol_and_risk(self):
        opps = dm.detect_opportunities({"USDC": 0.96}, entry=0.99, floor=0.90)
        txt = dm.format_opportunity_alert(opps)
        self.assertIn("USDC", txt)
        self.assertIn("хвостовым риском", txt)
        self.assertEqual(dm.format_opportunity_alert([]), "")

    def test_what_to_do_guide_is_honest(self):
        g = dm.WHAT_TO_DO_MD
        for must in ["UST", "не инвест", "винрейт", "SVB", "Маленький размер"]:
            self.assertIn(must, g)

    def test_config_defaults(self):
        self.assertGreater(dm.get_entry_threshold(), 0.9)
        self.assertLess(dm.get_entry_threshold(), 1.0)
        self.assertGreaterEqual(dm.get_interval_seconds(), 60)
        self.assertFalse(dm.feature_enabled())  # по умолчанию выкл


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))


class TestAlertSystem(unittest.TestCase):
    def setUp(self):
        self.bot = _FakeBot()
        self.sys = dm.DepegAlertSystem(self.bot)
        # in-memory состояние вместо БД
        self._state = {}

        async def _load():
            return dict(self._state)

        async def _save(f):
            self._state = dict(f)

        self.sys._load_flagged = _load   # type: ignore
        self.sys._save_flagged = _save   # type: ignore

    def _run(self, coro):
        return asyncio.run(coro)

    def test_alerts_on_new_depeg_then_no_respam(self):
        async def fake_fetch(*a, **k):
            return {"USDC": 0.96, "TUSD": 1.0}
        dm.fetch_prices = fake_fetch  # monkeypatch модульной функции

        subs = [{"user_id": 111}, {"user_id": 222}]
        sent1 = self._run(self.sys.check_and_alert(subs))
        self.assertEqual(sent1, 2)               # оба подписчика
        self.assertIn("USDC", self.bot.sent[0][1])

        # повторный прогон с тем же депегом — НЕ спамим
        sent2 = self._run(self.sys.check_and_alert(subs))
        self.assertEqual(sent2, 0)

    def test_resets_after_recovery_and_realerts(self):
        seq = [{"USDC": 0.96}, {"USDC": 0.999}, {"USDC": 0.95}]
        idx = {"i": 0}

        async def fake_fetch(*a, **k):
            v = seq[min(idx["i"], len(seq) - 1)]
            idx["i"] += 1
            return v
        dm.fetch_prices = fake_fetch

        subs = [{"user_id": 1}]
        self.assertEqual(self._run(self.sys.check_and_alert(subs)), 1)  # депег
        self.assertEqual(self._run(self.sys.check_and_alert(subs)), 0)  # восстановился
        self.assertEqual(self._run(self.sys.check_and_alert(subs)), 1)  # снова депег

    def test_no_send_on_empty_prices(self):
        async def fake_fetch(*a, **k):
            return {}
        dm.fetch_prices = fake_fetch
        self.assertEqual(self._run(self.sys.check_and_alert([{"user_id": 1}])), 0)


class TestExplainer(unittest.TestCase):
    def test_depeg_explainer_registered(self):
        import explainers
        self.assertIn("depeg", explainers.EXPLAINERS)
        txt = explainers.get("depeg")
        self.assertIn("Депег", txt)
        self.assertIn("UST", txt)


if __name__ == "__main__":
    unittest.main()
