"""Task 7 (Andrey): «Лучшая сделка» = лучший ЖИВОЙ delta-neutral edge
(carry / кросс-арб / базис), а НЕ directional price-bet (тот убыточен по
бэктесту и удалён). Чистый picker + форматтер, без сети.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from core.best_edge import (
    BestEdge,
    format_best_edge,
    pick_best_edge,
)


@dataclass
class FakeCarry:
    asset: str
    symbol: str
    annual_pct: float
    positive: bool

    @property
    def play(self) -> str:
        if self.positive:
            return f"ЛОНГ спот {self.asset} + ШОРТ перп {self.symbol}"
        return f"ШОРТ спот {self.asset} + ЛОНГ перп {self.symbol}"


@dataclass
class FakeArb:
    asset: str
    long_venue: str
    short_venue: str
    long_ann: float
    short_ann: float

    @property
    def spread(self) -> float:
        return self.short_ann - self.long_ann

    def net_spread(self) -> float:
        return self.spread - 4.0  # roundtrip cost


@dataclass
class FakeBasis:
    asset: str
    contract: str
    days_to_exp: int
    _net: float

    @property
    def net_annual_pct(self) -> float:
        return self._net


class TestPickBestEdge(unittest.TestCase):
    def test_picks_highest_net_apr_across_kinds(self):
        carry = [FakeCarry("BTC", "BTCUSDT", 14.0, True)]
        arb = [FakeArb("ETH", "Gate", "HL", 2.0, 30.0)]   # net 28-4 = 24
        basis = [FakeBasis("BTC", "BTCUSDT_260626", 90, 11.0)]
        e = pick_best_edge(carry_opps=carry, arb_opps=arb, basis_opps=basis)
        self.assertIsInstance(e, BestEdge)
        self.assertEqual(e.kind, "arb")
        self.assertEqual(e.asset, "ETH")
        self.assertAlmostEqual(e.net_apr, 24.0, places=1)

    def test_carry_wins_when_highest(self):
        carry = [FakeCarry("SOL", "SOLUSDT", 40.0, True)]
        arb = [FakeArb("ETH", "Gate", "HL", 2.0, 20.0)]   # net 14
        basis = [FakeBasis("BTC", "BTCUSDT_260626", 90, 11.0)]
        e = pick_best_edge(carry_opps=carry, arb_opps=arb, basis_opps=basis)
        self.assertEqual(e.kind, "carry")
        self.assertEqual(e.asset, "SOL")

    def test_negative_carry_uses_abs(self):
        # Обратный carry (funding отрицательный) тоже торгуем — берём |annual|.
        carry = [FakeCarry("XRP", "XRPUSDT", -35.0, False)]
        e = pick_best_edge(carry_opps=carry, arb_opps=[], basis_opps=[])
        self.assertEqual(e.kind, "carry")
        self.assertAlmostEqual(e.net_apr, 35.0, places=1)
        self.assertIn("ШОРТ спот XRP", e.headline)

    def test_all_empty_returns_none(self):
        self.assertIsNone(pick_best_edge(carry_opps=[], arb_opps=[], basis_opps=[]))

    def test_non_positive_edge_skipped(self):
        # Все ниже/равно нулю → нет сделки.
        carry = [FakeCarry("BTC", "BTCUSDT", 0.0, True)]
        arb = [FakeArb("ETH", "Gate", "HL", 30.0, 30.0)]  # net = -4
        self.assertIsNone(pick_best_edge(carry_opps=carry, arb_opps=arb, basis_opps=[]))

    def test_basis_wins_when_highest(self):
        carry = [FakeCarry("BTC", "BTCUSDT", 9.0, True)]
        basis = [FakeBasis("ETH", "ETHUSDT_260626", 90, 18.0)]
        e = pick_best_edge(carry_opps=carry, arb_opps=[], basis_opps=basis)
        self.assertEqual(e.kind, "basis")
        self.assertIn("ETHUSDT_260626", e.headline)


class TestFormatBestEdge(unittest.TestCase):
    def test_none_is_honest_sit_out(self):
        msg = format_best_edge(None, capital=0)
        self.assertIn("Лучшая сделка", msg)
        self.assertIn("нет", msg.lower())
        self.assertIn("/carry", msg)
        # Никаких выдуманных процентов/направлений.
        self.assertNotIn("%", msg)

    def test_edge_message_has_apr_and_command(self):
        edge = BestEdge(
            kind="arb", asset="ETH", net_apr=24.0,
            headline="ЛОНГ перп ETH на Gate + ШОРТ перп на HL",
            detail="кросс-биржевой funding-спред",
            command="/arb",
        )
        msg = format_best_edge(edge, capital=5000)
        self.assertIn("24% годовых", msg)
        self.assertIn("/arb", msg)
        self.assertIn("ETH", msg)
        self.assertIn("год", msg)  # $-оценка показана при capital>0

    def test_no_dollar_estimate_without_capital(self):
        edge = BestEdge(
            kind="carry", asset="BTC", net_apr=14.0,
            headline="ЛОНГ спот BTC + ШОРТ перп BTCUSDT",
            detail="фандинг-carry", command="/carry",
        )
        msg = format_best_edge(edge, capital=0)
        self.assertNotIn("/год", msg)


if __name__ == "__main__":
    unittest.main()
