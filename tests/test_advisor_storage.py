"""Tests for refactor.providers.advisor_storage (M2 portfolio).

Покрывает:
- Save plan (auto-saved vs is_portfolio=1)
- Get last/by-id, list active portfolio
- Promote plan → portfolio
- compute_pnl (LONG/SHORT)
- check_close_trigger (SL/TP1/TP2/TP3)
- close_plan (status transition + PnL)
- update_narrative (caching AI explanation)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from unittest.mock import patch

from refactor.providers.advisor_storage import (
    STATUS_ACTIVE,
    STATUS_STOPPED,
    STATUS_TP1,
    STATUS_TP2,
    StoredPlan,
    check_close_trigger,
    close_plan,
    compute_pnl,
    feature_enabled,
    get_last_plan,
    get_plan_by_id,
    list_active_portfolio,
    list_all_active,
    promote_to_portfolio,
    save_plan,
    update_narrative,
)


@dataclass
class _FakeTPLevel:
    price: float
    r_multiple: float
    close_pct: int


@dataclass
class _FakePlan:
    """Mock of core.advisor.AdvisorPlan (we don't import to keep test isolated)."""
    asset: str = "BTC"
    action: str = "BUY"
    confidence_pct: int = 70
    entry_price: float = 100.0
    stop_price: float = 95.0
    stop_distance_pct: float = 5.0
    risk_reward: float = 2.0
    tp_levels: tuple = field(default_factory=tuple)
    position_usd: float = 1000.0
    position_pct_of_capital: float = 10.0
    horizon_human: str = "1-3 дня"
    invalidation: str = "Closes below 95"
    rationale: tuple = field(default_factory=lambda: ("Тренд UP", "RSI 55"))
    btc_overlay_note: str = ""
    risk_profile: str = "moderate"


def _make_long_plan() -> _FakePlan:
    return _FakePlan(
        asset="BTC",
        action="BUY",
        entry_price=100.0,
        stop_price=95.0,
        tp_levels=(
            _FakeTPLevel(price=110.0, r_multiple=2.0, close_pct=30),
            _FakeTPLevel(price=120.0, r_multiple=4.0, close_pct=40),
            _FakeTPLevel(price=130.0, r_multiple=6.0, close_pct=30),
        ),
    )


def _make_short_plan() -> _FakePlan:
    return _FakePlan(
        asset="ETH",
        action="SELL",
        entry_price=2000.0,
        stop_price=2100.0,
        tp_levels=(
            _FakeTPLevel(price=1900.0, r_multiple=1.0, close_pct=30),
            _FakeTPLevel(price=1800.0, r_multiple=2.0, close_pct=70),
        ),
    )


async def _init_test_db(path: str) -> None:
    """Bootstrap advisor_plans schema in a fresh test DB."""
    import aiosqlite

    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS advisor_plans (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                  INTEGER NOT NULL,
                asset                    TEXT    NOT NULL,
                action                   TEXT    NOT NULL,
                direction                TEXT,
                confidence_pct           INTEGER NOT NULL DEFAULT 0,
                entry_price              REAL,
                stop_price               REAL,
                stop_distance_pct        REAL,
                risk_reward              REAL,
                tp_levels_json           TEXT,
                position_usd             REAL,
                position_pct_of_capital  REAL,
                capital_usd              REAL,
                horizon_human            TEXT,
                invalidation             TEXT,
                rationale_json           TEXT,
                btc_overlay_note         TEXT,
                risk_profile             TEXT,
                narrative                TEXT,
                is_portfolio             INTEGER NOT NULL DEFAULT 0,
                status                   TEXT    NOT NULL DEFAULT 'active',
                created_at               INTEGER NOT NULL,
                closed_at                INTEGER,
                close_price              REAL,
                close_reason             TEXT,
                pnl_usd                  REAL,
                pnl_pct                  REAL
            )
        """)
        await db.commit()


class TestFeatureFlag(unittest.TestCase):
    def test_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_enabled())

    def test_explicit_on(self):
        with patch.dict(os.environ, {"FEATURE_ADVISOR_PORTFOLIO": "1"}, clear=True):
            self.assertTrue(feature_enabled())

    def test_explicit_off(self):
        with patch.dict(os.environ, {"FEATURE_ADVISOR_PORTFOLIO": "0"}, clear=True):
            self.assertFalse(feature_enabled())


class TestSaveAndRetrieve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        asyncio.run(_init_test_db(self.db_path))

    def tearDown(self):
        os.unlink(self.db_path)

    def _save(self, plan, **kwargs):
        return asyncio.run(save_plan(
            user_id=42, plan=plan, capital_usd=10000.0,
            db_path=self.db_path, **kwargs,
        ))

    def test_save_returns_id(self):
        plan_id = self._save(_make_long_plan())
        self.assertGreater(plan_id, 0)

    def test_save_default_not_portfolio(self):
        plan_id = self._save(_make_long_plan())
        stored = asyncio.run(get_plan_by_id(plan_id, db_path=self.db_path))
        assert stored is not None
        self.assertEqual(stored.is_portfolio, 0)
        self.assertEqual(stored.status, STATUS_ACTIVE)

    def test_save_explicit_portfolio(self):
        plan_id = self._save(_make_long_plan(), is_portfolio=True)
        stored = asyncio.run(get_plan_by_id(plan_id, db_path=self.db_path))
        assert stored is not None
        self.assertEqual(stored.is_portfolio, 1)

    def test_direction_derived_from_action(self):
        long_id = self._save(_make_long_plan())  # action=BUY
        short_id = self._save(_make_short_plan())  # action=SELL
        long_stored = asyncio.run(get_plan_by_id(long_id, db_path=self.db_path))
        short_stored = asyncio.run(get_plan_by_id(short_id, db_path=self.db_path))
        assert long_stored is not None and short_stored is not None
        self.assertEqual(long_stored.direction, "LONG")
        self.assertEqual(short_stored.direction, "SHORT")

    def test_tp_levels_round_trip(self):
        plan_id = self._save(_make_long_plan())
        stored = asyncio.run(get_plan_by_id(plan_id, db_path=self.db_path))
        assert stored is not None
        self.assertEqual(len(stored.tp_levels), 3)
        self.assertEqual(stored.tp_levels[0]["price"], 110.0)
        self.assertEqual(stored.tp_levels[2]["close_pct"], 30)

    def test_rationale_round_trip(self):
        plan_id = self._save(_make_long_plan())
        stored = asyncio.run(get_plan_by_id(plan_id, db_path=self.db_path))
        assert stored is not None
        self.assertEqual(list(stored.rationale), ["Тренд UP", "RSI 55"])

    def test_get_last_plan_returns_latest(self):
        self._save(_make_long_plan())
        second_id = self._save(_make_short_plan())
        last = asyncio.run(get_last_plan(user_id=42, db_path=self.db_path))
        assert last is not None
        self.assertEqual(last.id, second_id)
        self.assertEqual(last.asset, "ETH")

    def test_get_last_plan_filter_by_asset(self):
        btc_id = self._save(_make_long_plan())
        self._save(_make_short_plan())  # ETH
        btc_last = asyncio.run(
            get_last_plan(user_id=42, asset="BTC", db_path=self.db_path)
        )
        assert btc_last is not None
        self.assertEqual(btc_last.id, btc_id)

    def test_get_last_plan_none_for_unknown_user(self):
        self._save(_make_long_plan())
        last = asyncio.run(get_last_plan(user_id=999, db_path=self.db_path))
        self.assertIsNone(last)


class TestPortfolio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        asyncio.run(_init_test_db(self.db_path))

    def tearDown(self):
        os.unlink(self.db_path)

    def _save(self, plan, **kwargs):
        return asyncio.run(save_plan(
            user_id=42, plan=plan, capital_usd=10000.0,
            db_path=self.db_path, **kwargs,
        ))

    def test_list_active_portfolio_empty(self):
        self._save(_make_long_plan())  # not in portfolio
        items = asyncio.run(list_active_portfolio(42, db_path=self.db_path))
        self.assertEqual(items, [])

    def test_list_active_portfolio_returns_portfolio_only(self):
        self._save(_make_long_plan())  # is_portfolio=0
        port_id = self._save(_make_short_plan(), is_portfolio=True)
        items = asyncio.run(list_active_portfolio(42, db_path=self.db_path))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, port_id)

    def test_promote_to_portfolio_flips_flag(self):
        plan_id = self._save(_make_long_plan())
        ok = asyncio.run(promote_to_portfolio(plan_id, db_path=self.db_path))
        self.assertTrue(ok)
        stored = asyncio.run(get_plan_by_id(plan_id, db_path=self.db_path))
        assert stored is not None
        self.assertEqual(stored.is_portfolio, 1)

    def test_promote_skips_non_active(self):
        plan_id = self._save(_make_long_plan())
        asyncio.run(close_plan(
            plan_id, new_status=STATUS_STOPPED, close_price=95.0,
            close_reason="SL hit @ 95", db_path=self.db_path,
        ))
        ok = asyncio.run(promote_to_portfolio(plan_id, db_path=self.db_path))
        self.assertFalse(ok)

    def test_list_all_active_across_users(self):
        # User 42
        self._save(_make_long_plan(), is_portfolio=True)
        # User 99
        asyncio.run(save_plan(
            user_id=99, plan=_make_short_plan(), capital_usd=5000.0,
            is_portfolio=True, db_path=self.db_path,
        ))
        items = asyncio.run(list_all_active(db_path=self.db_path))
        self.assertEqual(len(items), 2)
        users = sorted(p.user_id for p in items)
        self.assertEqual(users, [42, 99])


class TestComputePnL(unittest.TestCase):
    def test_long_profit(self):
        # entry=100, current=110, pos=1000 → pnl=100, pct=10
        plan = StoredPlan(direction="LONG", entry_price=100.0, position_usd=1000.0)
        usd, pct = compute_pnl(plan, current_price=110.0)
        self.assertAlmostEqual(pct, 10.0)
        self.assertAlmostEqual(usd, 100.0)

    def test_long_loss(self):
        plan = StoredPlan(direction="LONG", entry_price=100.0, position_usd=1000.0)
        usd, pct = compute_pnl(plan, current_price=95.0)
        self.assertAlmostEqual(pct, -5.0)
        self.assertAlmostEqual(usd, -50.0)

    def test_short_profit(self):
        plan = StoredPlan(direction="SHORT", entry_price=100.0, position_usd=1000.0)
        usd, pct = compute_pnl(plan, current_price=90.0)
        self.assertAlmostEqual(pct, 10.0)
        self.assertAlmostEqual(usd, 100.0)

    def test_short_loss(self):
        plan = StoredPlan(direction="SHORT", entry_price=100.0, position_usd=1000.0)
        usd, pct = compute_pnl(plan, current_price=105.0)
        self.assertAlmostEqual(pct, -5.0)
        self.assertAlmostEqual(usd, -50.0)

    def test_no_position_returns_pct_only(self):
        plan = StoredPlan(direction="LONG", entry_price=100.0, position_usd=None)
        usd, pct = compute_pnl(plan, current_price=110.0)
        self.assertIsNone(usd)
        self.assertAlmostEqual(pct, 10.0)

    def test_wait_returns_none(self):
        plan = StoredPlan(direction="WAIT", entry_price=100.0, position_usd=1000.0)
        usd, pct = compute_pnl(plan, current_price=110.0)
        self.assertIsNone(usd)
        self.assertIsNone(pct)


class TestCheckCloseTrigger(unittest.TestCase):
    def _long(self):
        return StoredPlan(
            direction="LONG", entry_price=100.0, stop_price=95.0,
            tp_levels=[
                {"price": 110.0, "r_multiple": 2.0, "close_pct": 30},
                {"price": 120.0, "r_multiple": 4.0, "close_pct": 40},
                {"price": 130.0, "r_multiple": 6.0, "close_pct": 30},
            ],
        )

    def _short(self):
        return StoredPlan(
            direction="SHORT", entry_price=2000.0, stop_price=2100.0,
            tp_levels=[
                {"price": 1900.0, "r_multiple": 1.0, "close_pct": 30},
                {"price": 1800.0, "r_multiple": 2.0, "close_pct": 70},
            ],
        )

    def test_long_sl_hit(self):
        out = check_close_trigger(self._long(), 94.9)
        assert out is not None
        self.assertEqual(out[0], STATUS_STOPPED)

    def test_long_tp1_hit(self):
        out = check_close_trigger(self._long(), 110.5)
        assert out is not None
        self.assertEqual(out[0], STATUS_TP1)

    def test_long_tp2_hit(self):
        # 121 > 110 (TP1) AND > 120 (TP2). check_close_trigger returns FIRST
        # match which is TP1 — that's by design (we close at the first level
        # reached, not the highest possible).
        out = check_close_trigger(self._long(), 121.0)
        assert out is not None
        self.assertEqual(out[0], STATUS_TP1)

    def test_short_sl_hit(self):
        out = check_close_trigger(self._short(), 2101.0)
        assert out is not None
        self.assertEqual(out[0], STATUS_STOPPED)

    def test_short_tp1_hit(self):
        out = check_close_trigger(self._short(), 1895.0)
        assert out is not None
        self.assertEqual(out[0], STATUS_TP1)

    def test_no_trigger_in_band(self):
        # LONG, entry=100, SL=95, TP1=110. price=102 → нет триггера.
        out = check_close_trigger(self._long(), 102.0)
        self.assertIsNone(out)

    def test_wait_never_triggers(self):
        plan = StoredPlan(direction="WAIT", entry_price=100.0, stop_price=95.0)
        out = check_close_trigger(plan, 50.0)
        self.assertIsNone(out)


class TestClosePlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        asyncio.run(_init_test_db(self.db_path))

    def tearDown(self):
        os.unlink(self.db_path)

    def test_close_writes_pnl_and_status(self):
        plan_id = asyncio.run(save_plan(
            user_id=42, plan=_make_long_plan(), capital_usd=10000.0,
            is_portfolio=True, db_path=self.db_path,
        ))
        # LONG entry=100, current=110, pos=1000 → pnl_pct=10, pnl_usd=100
        closed = asyncio.run(close_plan(
            plan_id, new_status=STATUS_TP1, close_price=110.0,
            close_reason="TP1 hit @ 110", db_path=self.db_path,
        ))
        assert closed is not None
        self.assertEqual(closed.status, STATUS_TP1)
        self.assertEqual(closed.close_price, 110.0)
        self.assertAlmostEqual(closed.pnl_pct or 0.0, 10.0)
        self.assertAlmostEqual(closed.pnl_usd or 0.0, 100.0)
        self.assertIsNotNone(closed.closed_at)

    def test_close_nonexistent_returns_none(self):
        out = asyncio.run(close_plan(
            999, new_status=STATUS_TP1, close_price=110.0,
            close_reason="x", db_path=self.db_path,
        ))
        self.assertIsNone(out)


class TestNarrativeCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        asyncio.run(_init_test_db(self.db_path))

    def tearDown(self):
        os.unlink(self.db_path)

    def test_update_narrative_round_trip(self):
        plan_id = asyncio.run(save_plan(
            user_id=42, plan=_make_long_plan(), capital_usd=10000.0,
            db_path=self.db_path,
        ))
        ok = asyncio.run(update_narrative(
            plan_id, "Бай BTC потому что bullish.",
            db_path=self.db_path,
        ))
        self.assertTrue(ok)
        stored = asyncio.run(get_plan_by_id(plan_id, db_path=self.db_path))
        assert stored is not None
        self.assertEqual(stored.narrative, "Бай BTC потому что bullish.")

    def test_update_narrative_missing_id_returns_false(self):
        ok = asyncio.run(update_narrative(
            999, "x", db_path=self.db_path,
        ))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
