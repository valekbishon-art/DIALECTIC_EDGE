"""Tests for the risk-aware carry allocation optimizer (#4)."""
from __future__ import annotations

import unittest

from core.carry_signal import (
    CarryOpportunity,
    _annualized_cost,
    optimize_carry_allocation,
)


def _opp(asset: str, annual: float) -> CarryOpportunity:
    return CarryOpportunity(
        symbol=f"{asset}USDT", asset=asset, rate=0.0001,
        interval_h=8.0, annual_pct=annual, positive=annual > 0,
    )


class TestCost(unittest.TestCase):
    def test_annualized_cost(self):
        # 0.30% за 30 дней ≈ 3.65%/год
        self.assertAlmostEqual(_annualized_cost(0.30, 30.0), 3.65, places=2)
        self.assertEqual(_annualized_cost(0.30, 0.0), 0.0)


class TestOptimizer(unittest.TestCase):
    def test_respects_max_weight_cap(self):
        opps = [_opp("BTC", 100), _opp("ETH", 90), _opp("SOL", 80),
                _opp("BNB", 70), _opp("XRP", 60)]
        plan = optimize_carry_allocation(opps, 1000.0, max_weight=0.25,
                                         min_net_annual=0.0)
        for a in plan["allocations"]:
            self.assertLessEqual(a.capital_usd, 250.0 + 1e-6)

    def test_budget_not_exceeded(self):
        opps = [_opp("BTC", 100), _opp("ETH", 90), _opp("SOL", 80),
                _opp("BNB", 70), _opp("XRP", 60), _opp("ADA", 55)]
        plan = optimize_carry_allocation(opps, 1000.0, max_weight=0.25,
                                         min_net_annual=0.0)
        self.assertLessEqual(plan["total_allocated"], 1000.0 + 1e-6)

    def test_prefers_higher_net_yield(self):
        opps = [_opp("BTC", 100), _opp("ETH", 30), _opp("SOL", 25)]
        plan = optimize_carry_allocation(opps, 1000.0, max_weight=0.5,
                                         min_net_annual=0.0)
        # Highest-yield BTC should be funded to its cap first.
        first = plan["allocations"][0]
        self.assertEqual(first.opp.asset, "BTC")
        self.assertAlmostEqual(first.capital_usd, 500.0)

    def test_cost_hurdle_drops_thin(self):
        opps = [_opp("BTC", 100), _opp("ETH", 5)]  # ETH net≈1.35% < THIN(8)
        plan = optimize_carry_allocation(opps, 1000.0)
        assets = [a.opp.asset for a in plan["allocations"]]
        self.assertIn("BTC", assets)
        self.assertNotIn("ETH", assets)

    def test_beats_equal_weight_on_dispersion(self):
        # Loose cap (>1/n) + dispersed yields → yield-weighting beats equal weight.
        opps = [_opp("BTC", 120), _opp("ETH", 100), _opp("SOL", 90),
                _opp("BNB", 30), _opp("XRP", 25)]
        plan = optimize_carry_allocation(opps, 1000.0, max_weight=0.4,
                                         min_net_annual=0.0)
        self.assertGreater(plan["port_net_year_usd"], plan["baseline_net_year_usd"])
        self.assertGreater(plan["uplift_pct"], 0.0)

    def test_uplift_zero_when_caps_all_bind(self):
        # cap = 1/n_legs → optimizer == equal weight → no uplift, no false claim.
        opps = [_opp("BTC", 120), _opp("ETH", 80), _opp("SOL", 60), _opp("BNB", 40)]
        plan = optimize_carry_allocation(opps, 1000.0, max_weight=0.25,
                                         min_net_annual=0.0)
        self.assertEqual(plan["n_legs"], 4)
        self.assertAlmostEqual(plan["uplift_pct"], 0.0, places=1)

    def test_negative_funding_uses_abs(self):
        # Inverse carry (negative funding) still earns |annual| (short spot+long perp).
        opps = [_opp("BTC", -90)]
        plan = optimize_carry_allocation(opps, 1000.0, min_net_annual=0.0)
        self.assertEqual(len(plan["allocations"]), 1)
        self.assertAlmostEqual(plan["allocations"][0].gross_annual_pct, 90.0)

    def test_empty(self):
        plan = optimize_carry_allocation([], 1000.0)
        self.assertEqual(plan["allocations"], [])
        self.assertEqual(plan["n_legs"], 0)
        self.assertEqual(plan["uplift_pct"], 0.0)

    def test_risk_caps_override(self):
        opps = [_opp("BTC", 100), _opp("ETH", 90)]
        plan = optimize_carry_allocation(
            opps, 1000.0, max_weight=0.5, min_net_annual=0.0,
            risk_caps={"BTC": 0.1})  # tighter cap on BTC
        btc = next(a for a in plan["allocations"] if a.opp.asset == "BTC")
        self.assertAlmostEqual(btc.capital_usd, 100.0)  # 0.1 * 1000


if __name__ == "__main__":
    unittest.main()
