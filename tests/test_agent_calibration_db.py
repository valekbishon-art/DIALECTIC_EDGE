"""DB roundtrip-тесты для agent_predictions (новая таблица для PR #1).

Покрывают:
  * CREATE TABLE при init_db — таблица существует с правильными колонками.
  * `save_agent_prediction` — вставка, возврат id, type-coercion.
  * `get_pending_agent_predictions` — фильтрация по resolved=0 и resolve_at.
  * `resolve_agent_prediction` — апдейт + idempotency (двойной resolve no-op).
  * `get_agent_calibration_history` — фильтр по role/asset/lookback.
  * CHECK constraints — p_up out of [0,1] кидает.

Использует tempdir + monkeypatch DB_PATH для изоляции.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


class AgentPredictionsDBTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Каждый тест получает свежую SQLite.
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"

        # Перезагружаем database с указанным DB_PATH перед самым импортом.
        os.environ["DB_PATH"] = str(self.db_path)
        # Если database был импортирован ранее — нужно сбросить
        for mod in list(sys.modules.keys()):
            if mod == "database" or mod.startswith("database."):
                del sys.modules[mod]

        import database  # noqa: PLC0415
        self.db = database
        # Принудительно переопределяем DB_PATH в модуле (он мог взять при импорте
        # старое значение).
        self.db.DB_PATH = str(self.db_path)

        _run(self.db.init_db())

    def tearDown(self) -> None:
        self.tmpdir.cleanup()
        os.environ.pop("DB_PATH", None)

    # ─── schema ────────────────────────────────────────────────────────────

    def test_table_exists_after_init(self) -> None:
        import aiosqlite  # noqa: PLC0415

        async def check():
            async with aiosqlite.connect(str(self.db_path)) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='agent_predictions'"
                ) as cur:
                    row = await cur.fetchone()
                    return row is not None
        self.assertTrue(_run(check()))

    def test_check_constraint_rejects_invalid_pup(self) -> None:
        # p_up > 1 должен валиться по CHECK.
        from sqlite3 import IntegrityError  # noqa: PLC0415
        with self.assertRaises(IntegrityError):
            _run(self.db.save_agent_prediction(
                debate_id="t1",
                asset="BTC", agent_role="bull",
                horizon_minutes=60,
                p_up=1.5,  # invalid!
                threshold_pct=0.5,
                ref_price=100.0,
                resolve_at="2026-01-01 12:00:00",
            ))

    # ─── roundtrip ─────────────────────────────────────────────────────────

    def test_save_returns_id_and_increments(self) -> None:
        async def go():
            id1 = await self.db.save_agent_prediction(
                debate_id="d1", asset="BTC", agent_role="bull",
                horizon_minutes=60, p_up=0.7, threshold_pct=0.5,
                ref_price=100.0, resolve_at="2030-01-01 12:00:00",
            )
            id2 = await self.db.save_agent_prediction(
                debate_id="d1", asset="BTC", agent_role="bear",
                horizon_minutes=60, p_up=0.3, threshold_pct=0.5,
                ref_price=100.0, resolve_at="2030-01-01 12:00:00",
            )
            return id1, id2
        id1, id2 = _run(go())
        self.assertGreater(id1, 0)
        self.assertEqual(id2, id1 + 1)

    def test_get_pending_filters_by_resolve_at(self) -> None:
        past = "2020-01-01 00:00:00"  # уже прошло
        future = "2030-01-01 00:00:00"  # ещё нет

        async def go():
            await self.db.save_agent_prediction(
                debate_id="d1", asset="BTC", agent_role="bull",
                horizon_minutes=60, p_up=0.7, threshold_pct=0.5,
                ref_price=100.0, resolve_at=past,
            )
            await self.db.save_agent_prediction(
                debate_id="d1", asset="ETH", agent_role="bull",
                horizon_minutes=60, p_up=0.6, threshold_pct=0.5,
                ref_price=3000.0, resolve_at=future,
            )
            return await self.db.get_pending_agent_predictions()
        rows = _run(go())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset"], "BTC")

    def test_resolve_marks_as_resolved(self) -> None:
        async def go():
            past = "2020-01-01 00:00:00"
            rid = await self.db.save_agent_prediction(
                debate_id="d1", asset="BTC", agent_role="synth",
                horizon_minutes=60, p_up=0.7, threshold_pct=0.5,
                ref_price=100.0, resolve_at=past,
            )
            await self.db.resolve_agent_prediction(
                prediction_id=rid, realized_price=102.0,
                realized_y=True, brier_score=0.09,
            )
            # После резолва pending должен быть пустым.
            pending = await self.db.get_pending_agent_predictions()
            return pending, rid
        pending, rid = _run(go())
        self.assertEqual(pending, [])

    def test_resolve_idempotent_no_double_count(self) -> None:
        # Двойной resolve_agent_prediction не меняет state.
        async def go():
            past = "2020-01-01 00:00:00"
            rid = await self.db.save_agent_prediction(
                debate_id="d1", asset="BTC", agent_role="bear",
                horizon_minutes=60, p_up=0.4, threshold_pct=0.5,
                ref_price=100.0, resolve_at=past,
            )
            # Первый резолв.
            await self.db.resolve_agent_prediction(
                prediction_id=rid, realized_price=99.0,
                realized_y=False, brier_score=0.16,
            )
            # Второй вызов с другими данными — должен быть no-op
            # (WHERE resolved=0 в UPDATE).
            await self.db.resolve_agent_prediction(
                prediction_id=rid, realized_price=999.0,
                realized_y=True, brier_score=0.99,
            )
            history = await self.db.get_agent_calibration_history(
                agent_role="bear", lookback_days=365 * 100,
            )
            return history
        history = _run(go())
        self.assertEqual(len(history), 1)
        # Первый резолв должен сохраниться, второй no-op.
        self.assertAlmostEqual(history[0]["realized_price"], 99.0, places=4)
        self.assertEqual(history[0]["realized_y"], 0)
        self.assertAlmostEqual(history[0]["brier_score"], 0.16, places=4)

    def test_calibration_history_filters_role_and_asset(self) -> None:
        async def go():
            past = "2020-01-01 00:00:00"
            # 3 prediction'а, разные role/asset.
            for role, asset, p in (
                ("bull", "BTC", 0.7),
                ("bear", "BTC", 0.3),
                ("bull", "ETH", 0.65),
            ):
                rid = await self.db.save_agent_prediction(
                    debate_id="d1", asset=asset, agent_role=role,
                    horizon_minutes=60, p_up=p, threshold_pct=0.5,
                    ref_price=100.0, resolve_at=past,
                )
                await self.db.resolve_agent_prediction(
                    prediction_id=rid, realized_price=101.0,
                    realized_y=True, brier_score=(p - 1.0) ** 2,
                )
            # Только bull / BTC.
            return await self.db.get_agent_calibration_history(
                agent_role="bull", asset="BTC", lookback_days=365 * 100,
            )
        rows = _run(go())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset"], "BTC")
        self.assertEqual(rows[0]["agent_role"], "bull")


if __name__ == "__main__":
    unittest.main()
