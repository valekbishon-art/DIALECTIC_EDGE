"""Tests for refactor/services/alert_engine.py."""

import asyncio
import os
import tempfile
import time
import unittest

from refactor.services import (
    AlertCard,
    AlertEngine,
    CallableRule,
    JsonAlertStore,
    alert_engine_chat_ids,
    alert_engine_enabled,
    alert_engine_interval_sec,
    format_alert_card,
)


def _run(coro):
    return asyncio.run(coro)


class AlertCardTests(unittest.TestCase):
    def test_valid_severity_accepted(self):
        for sev in ("INFO", "WARN", "CRIT"):
            card = AlertCard(
                rule_id="r",
                severity=sev,
                title="t",
                body="b",
                dedup_key="k",
            )
            self.assertEqual(card.severity, sev)

    def test_invalid_severity_rejected(self):
        with self.assertRaises(ValueError):
            AlertCard(
                rule_id="r",
                severity="warn",
                title="t",
                body="b",
                dedup_key="k",
            )

    def test_emoji_mapping(self):
        info = AlertCard(rule_id="r", severity="INFO", title="", body="", dedup_key="")
        warn = AlertCard(rule_id="r", severity="WARN", title="", body="", dedup_key="")
        crit = AlertCard(rule_id="r", severity="CRIT", title="", body="", dedup_key="")
        self.assertEqual(info.emoji, "🟢")
        self.assertEqual(warn.emoji, "🟡")
        self.assertEqual(crit.emoji, "🔴")


class FormatAlertCardTests(unittest.TestCase):
    def test_format_contains_severity_title_body(self):
        card = AlertCard(
            rule_id="x",
            severity="WARN",
            title="ETF outflow",
            body="3 days streak",
            dedup_key="streak:3",
            fetched_at=time.time() - 65,
        )
        out = format_alert_card(card)
        self.assertIn("WARN", out)
        self.assertIn("ETF outflow", out)
        self.assertIn("3 days streak", out)
        self.assertIn("x", out)
        # 65s → "1m"
        self.assertIn("1m", out)

    def test_format_uses_seconds_for_young_card(self):
        card = AlertCard(
            rule_id="x",
            severity="INFO",
            title="t",
            body="b",
            dedup_key="d",
            fetched_at=time.time() - 5,
        )
        self.assertIn("5s", format_alert_card(card))


class AlertEngineCooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".json", mode="w"
        )
        self.tmp.write("{}")
        self.tmp.close()
        self.store = JsonAlertStore(path=self.tmp.name)
        self.calls = 0

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _make_rule(self, *cards, rule_id="r", cooldown=600):
        async def fn():
            self.calls += 1
            return cards

        return CallableRule(rule_id=rule_id, cooldown_sec=cooldown, fn=fn)

    def test_first_evaluation_returns_all_cards(self):
        c1 = AlertCard(rule_id="r", severity="WARN", title="t1", body="b", dedup_key="a")
        c2 = AlertCard(rule_id="r", severity="WARN", title="t2", body="b", dedup_key="b")
        engine = AlertEngine(rules=[self._make_rule(c1, c2)], store=self.store)
        out = _run(engine.evaluate_all())
        self.assertEqual([c.dedup_key for c in out], ["a", "b"])

    def test_same_dedup_key_suppressed_by_cooldown(self):
        c = AlertCard(rule_id="r", severity="WARN", title="t", body="b", dedup_key="x")
        engine = AlertEngine(rules=[self._make_rule(c)], store=self.store)
        first = _run(engine.evaluate_all())
        second = _run(engine.evaluate_all())
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_different_dedup_keys_independent(self):
        c1 = AlertCard(rule_id="r", severity="WARN", title="t", body="b", dedup_key="x")
        c2 = AlertCard(rule_id="r", severity="WARN", title="t", body="b", dedup_key="y")
        engine = AlertEngine(rules=[self._make_rule(c1)], store=self.store)
        _run(engine.evaluate_all())
        engine2 = AlertEngine(rules=[self._make_rule(c2)], store=self.store)
        out = _run(engine2.evaluate_all())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].dedup_key, "y")

    def test_rule_exception_does_not_crash_engine(self):
        async def bad():
            raise RuntimeError("rule blew up")

        good_card = AlertCard(
            rule_id="ok", severity="INFO", title="t", body="b", dedup_key="z"
        )

        async def good():
            return (good_card,)

        bad_rule = CallableRule(rule_id="bad", cooldown_sec=60, fn=bad)
        good_rule = CallableRule(rule_id="ok", cooldown_sec=60, fn=good)
        engine = AlertEngine(rules=[bad_rule, good_rule], store=self.store)
        out = _run(engine.evaluate_all())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].rule_id, "ok")

    def test_rule_timeout_does_not_crash_engine(self):
        async def slow():
            await asyncio.sleep(0.5)
            return ()

        async def fast():
            return (
                AlertCard(rule_id="f", severity="INFO", title="t", body="b", dedup_key="k"),
            )

        slow_rule = CallableRule(rule_id="slow", cooldown_sec=60, fn=slow)
        fast_rule = CallableRule(rule_id="f", cooldown_sec=60, fn=fast)
        engine = AlertEngine(rules=[slow_rule, fast_rule], store=self.store)
        out = _run(engine.evaluate_all(rule_timeout_sec=0.05))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].rule_id, "f")

    def test_empty_rules_returns_empty(self):
        engine = AlertEngine(rules=[], store=self.store)
        self.assertEqual(_run(engine.evaluate_all()), [])


class EnvHelperTests(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for k in (
            "FEATURE_ALERT_ENGINE",
            "ALERT_ENGINE_INTERVAL_SEC",
            "ALERT_ENGINE_CHAT_IDS",
        ):
            self._saved[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_feature_flag_default_off(self):
        self.assertFalse(alert_engine_enabled())

    def test_feature_flag_truthy_values(self):
        for v in ("1", "true", "yes", "on", "TRUE"):
            os.environ["FEATURE_ALERT_ENGINE"] = v
            self.assertTrue(alert_engine_enabled())

    def test_interval_clamped(self):
        os.environ["ALERT_ENGINE_INTERVAL_SEC"] = "5"
        self.assertEqual(alert_engine_interval_sec(), 30)
        os.environ["ALERT_ENGINE_INTERVAL_SEC"] = "999999"
        self.assertEqual(alert_engine_interval_sec(), 3600)

    def test_chat_ids_fallback_to_admin(self):
        self.assertEqual(alert_engine_chat_ids([1, 2, 3]), (1, 2, 3))

    def test_chat_ids_explicit(self):
        os.environ["ALERT_ENGINE_CHAT_IDS"] = "100, 200,bad,300"
        self.assertEqual(alert_engine_chat_ids([1]), (100, 200, 300))


if __name__ == "__main__":
    unittest.main()
