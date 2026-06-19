"""Honest-expectations block must appear in /plan output and the EDGE explainer.

Guards the framing approved with Andrey: NOT "stable income", drawdowns to ~-50%,
losing months are normal, profit carried by few big winners, backtest is past /
not a guarantee, not investment advice. Markdown asterisks must stay balanced so
Telegram rendering never breaks.

Written as unittest.TestCase so it runs under both pytest and the CI
`python -m unittest discover` invocation.
"""
import unittest

import halal_edge
import explainers


_RISK_ON = {
    "as_of": "2026-06-18", "regime": "risk_on",
    "picks": [{"sym": "SOL", "weight": 0.4, "mom90": 0.55},
              {"sym": "BTC", "weight": 0.3, "mom90": 0.2}],
    "cash": 0.3, "invested": 0.7,
}
_RISK_OFF = {
    "as_of": "2026-06-18", "regime": "risk_off",
    "btc_price": 42000, "btc_sma": 48000,
}


class PlanHonestExpectationsTest(unittest.TestCase):
    def _assert_honest(self, text: str):
        self.assertIn("Чего реально ждать", text)
        # Honest framing: explicitly NOT "stable income".
        self.assertIn("не «стабильный доход»", text)
        # Drawdown reality is disclosed.
        self.assertTrue("−50%" in text or "-50%" in text)
        # Not investment advice / not a guarantee.
        self.assertIn("не инвестсовет", text)
        self.assertIn("не гарантия", text)
        # Telegram Markdown must stay balanced.
        self.assertEqual(text.count("*") % 2, 0, "unbalanced bold asterisks")

    def test_plan_risk_on_has_honest_expectations(self):
        self._assert_honest(halal_edge.render_plan_text(_RISK_ON, 1000.0))

    def test_plan_risk_off_has_honest_expectations(self):
        self._assert_honest(halal_edge.render_plan_text(_RISK_OFF, 1000.0))

    def test_edge_explainer_has_honest_expectations(self):
        e = explainers.get("edge")
        self.assertIn("Чего реально ждать", e)
        self.assertIn("не «стабильный доход»", e)
        self.assertEqual(e.count("*") % 2, 0)

    def test_expectations_constant_is_balanced_and_complete(self):
        block = halal_edge.EXPECTATIONS_MD
        self.assertEqual(block.count("*") % 2, 0)
        for needle in ("не «стабильный доход»", "просадки", "крупные победители",
                       "прошлое", "не инвестсовет"):
            self.assertIn(needle, block)


if __name__ == "__main__":
    unittest.main()
