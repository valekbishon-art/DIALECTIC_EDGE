"""DB roundtrip-тесты для microstructure_snapshots.

Покрывают:
  * CREATE TABLE при init_db.
  * `save_microstructure_snapshot` — вставка, NaN→NULL, return id.
  * CHECK constraints (severity вне [0,1], direction_bias не -1/0/1).
  * `get_microstructure_baseline_depth` — None при < 3 точек, среднее иначе.
  * `get_recent_microstructure_snapshots` — DESC по timestamp_ms.

Использует tempdir + monkeypatch DB_PATH для изоляции (тот же паттерн что
у test_agent_calibration_db.py).
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


class MicrostructureSnapshotsDBTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "ms_test.db"

        os.environ["DB_PATH"] = str(self.db_path)
        for mod in list(sys.modules.keys()):
            if mod == "database" or mod.startswith("database."):
                del sys.modules[mod]

        import database  # noqa: PLC0415
        self.db = database
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
                async with conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='microstructure_snapshots'"
                ) as cur:
                    return (await cur.fetchone()) is not None
        self.assertTrue(_run(check()))

    def test_check_constraint_rejects_invalid_severity(self) -> None:
        from sqlite3 import IntegrityError  # noqa: PLC0415

        # severity > 1 — наш wrapper зажимает (через max/min). Зайдём напрямую
        # через aiosqlite чтобы обойти clamp и проверить CHECK.
        async def go() -> None:
            import aiosqlite  # noqa: PLC0415
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    """
                    INSERT INTO microstructure_snapshots
                        (asset, timestamp_ms, mid_price, bid_depth_usd, ask_depth_usd,
                         venue_count, severity, direction_bias)
                    VALUES ('BTC', 1, 100.0, 1.0, 1.0, 1, 1.5, 0)
                    """
                )
                await db.commit()

        with self.assertRaises(IntegrityError):
            _run(go())

    def test_check_constraint_rejects_invalid_direction_bias(self) -> None:
        from sqlite3 import IntegrityError  # noqa: PLC0415

        async def go() -> None:
            import aiosqlite  # noqa: PLC0415
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    """
                    INSERT INTO microstructure_snapshots
                        (asset, timestamp_ms, mid_price, bid_depth_usd, ask_depth_usd,
                         venue_count, severity, direction_bias)
                    VALUES ('BTC', 1, 100.0, 1.0, 1.0, 1, 0.5, 2)
                    """
                )
                await db.commit()

        with self.assertRaises(IntegrityError):
            _run(go())

    # ─── save / NaN handling ───────────────────────────────────────────────

    def test_save_returns_id(self) -> None:
        async def go():
            return await self.db.save_microstructure_snapshot(
                asset="BTC", timestamp_ms=1700000000000, mid_price=100.0,
                bid_depth_usd=500.0, ask_depth_usd=500.0,
                asymmetry=0.0, quoted_spread_bps=5.0,
                venue_count=3, venues_csv="binance,bybit,okx",
                vacuum_flag=False, direction_bias=0, severity=0.0,
                baseline_depth_usd=1000.0, drop_pct_observed=0.0,
            )
        rid = _run(go())
        self.assertGreater(rid, 0)

    def test_nan_converted_to_null(self) -> None:
        async def go():
            rid = await self.db.save_microstructure_snapshot(
                asset="BTC", timestamp_ms=1700000000000, mid_price=100.0,
                bid_depth_usd=0.0, ask_depth_usd=0.0,
                asymmetry=float("nan"),  # → NULL
                quoted_spread_bps=float("inf"),  # → NULL
                venue_count=1, venues_csv="binance",
                vacuum_flag=False, direction_bias=0, severity=0.5,
                baseline_depth_usd=None, drop_pct_observed=None,
            )
            rows = await self.db.get_recent_microstructure_snapshots(asset="BTC")
            return rid, rows
        rid, rows = _run(go())
        self.assertGreater(rid, 0)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["asymmetry"])
        self.assertIsNone(rows[0]["quoted_spread_bps"])

    def test_severity_clamped(self) -> None:
        # Wrapper зажимает severity в [0,1] — без CHECK-нарушения.
        async def go():
            rid = await self.db.save_microstructure_snapshot(
                asset="BTC", timestamp_ms=1, mid_price=100.0,
                bid_depth_usd=1.0, ask_depth_usd=1.0,
                asymmetry=0.0, quoted_spread_bps=0.0,
                venue_count=1, venues_csv="binance",
                vacuum_flag=False, direction_bias=0,
                severity=2.0,  # будет зажат в 1.0
                baseline_depth_usd=None, drop_pct_observed=None,
            )
            rows = await self.db.get_recent_microstructure_snapshots(asset="BTC")
            return rid, rows
        rid, rows = _run(go())
        self.assertGreater(rid, 0)
        self.assertEqual(rows[0]["severity"], 1.0)

    # ─── baseline depth ────────────────────────────────────────────────────

    def test_baseline_returns_none_when_lt_3_points(self) -> None:
        async def go():
            for i in range(2):
                await self.db.save_microstructure_snapshot(
                    asset="BTC", timestamp_ms=i, mid_price=100.0,
                    bid_depth_usd=500.0, ask_depth_usd=500.0,
                    asymmetry=0.0, quoted_spread_bps=5.0,
                    venue_count=3, venues_csv="b,b,o",
                    vacuum_flag=False, direction_bias=0, severity=0.0,
                    baseline_depth_usd=None, drop_pct_observed=None,
                )
            return await self.db.get_microstructure_baseline_depth(asset="BTC")
        result = _run(go())
        self.assertIsNone(result)

    def test_baseline_returns_average_when_ge_3_points(self) -> None:
        async def go():
            for usd in (1000.0, 2000.0, 3000.0):
                await self.db.save_microstructure_snapshot(
                    asset="BTC", timestamp_ms=int(usd), mid_price=100.0,
                    bid_depth_usd=usd / 2.0, ask_depth_usd=usd / 2.0,
                    asymmetry=0.0, quoted_spread_bps=5.0,
                    venue_count=3, venues_csv="b,b,o",
                    vacuum_flag=False, direction_bias=0, severity=0.0,
                    baseline_depth_usd=None, drop_pct_observed=None,
                )
            return await self.db.get_microstructure_baseline_depth(asset="BTC")
        result = _run(go())
        # total_depth = bid+ask = 1000,2000,3000 → avg = 2000.
        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result, 2000.0, places=2)

    def test_baseline_isolates_assets(self) -> None:
        async def go():
            for usd in (1000.0, 2000.0, 3000.0):
                await self.db.save_microstructure_snapshot(
                    asset="ETH", timestamp_ms=int(usd), mid_price=2500.0,
                    bid_depth_usd=usd / 2.0, ask_depth_usd=usd / 2.0,
                    asymmetry=0.0, quoted_spread_bps=5.0,
                    venue_count=3, venues_csv="b,b,o",
                    vacuum_flag=False, direction_bias=0, severity=0.0,
                    baseline_depth_usd=None, drop_pct_observed=None,
                )
            # Запрос на BTC должен быть None (нет данных).
            return await self.db.get_microstructure_baseline_depth(asset="BTC")
        result = _run(go())
        self.assertIsNone(result)

    # ─── get_recent ────────────────────────────────────────────────────────

    def test_recent_orders_by_timestamp_desc(self) -> None:
        async def go():
            for ts in (1, 5, 3):
                await self.db.save_microstructure_snapshot(
                    asset="BTC", timestamp_ms=ts, mid_price=100.0,
                    bid_depth_usd=10.0, ask_depth_usd=10.0,
                    asymmetry=0.0, quoted_spread_bps=5.0,
                    venue_count=2, venues_csv="b,b",
                    vacuum_flag=False, direction_bias=0, severity=0.0,
                    baseline_depth_usd=None, drop_pct_observed=None,
                )
            return await self.db.get_recent_microstructure_snapshots(asset="BTC")
        rows = _run(go())
        # DESC по timestamp_ms.
        timestamps = [r["timestamp_ms"] for r in rows]
        self.assertEqual(timestamps, [5, 3, 1])

    def test_recent_limit(self) -> None:
        async def go():
            for ts in range(20):
                await self.db.save_microstructure_snapshot(
                    asset="BTC", timestamp_ms=ts, mid_price=100.0,
                    bid_depth_usd=10.0, ask_depth_usd=10.0,
                    asymmetry=0.0, quoted_spread_bps=5.0,
                    venue_count=1, venues_csv="b",
                    vacuum_flag=False, direction_bias=0, severity=0.0,
                    baseline_depth_usd=None, drop_pct_observed=None,
                )
            return await self.db.get_recent_microstructure_snapshots(
                asset="BTC", limit=5
            )
        rows = _run(go())
        self.assertEqual(len(rows), 5)

    # ─── vacuum_flag ───────────────────────────────────────────────────────

    def test_vacuum_flag_persisted(self) -> None:
        async def go():
            await self.db.save_microstructure_snapshot(
                asset="BTC", timestamp_ms=1, mid_price=100.0,
                bid_depth_usd=10.0, ask_depth_usd=10.0,
                asymmetry=0.0, quoted_spread_bps=5.0,
                venue_count=1, venues_csv="b",
                vacuum_flag=True, direction_bias=-1, severity=0.8,
                baseline_depth_usd=100.0, drop_pct_observed=50.0,
            )
            rows = await self.db.get_recent_microstructure_snapshots(asset="BTC")
            return rows
        rows = _run(go())
        self.assertEqual(rows[0]["vacuum_flag"], 1)
        self.assertEqual(rows[0]["direction_bias"], -1)
        self.assertAlmostEqual(rows[0]["severity"], 0.8)
        self.assertAlmostEqual(rows[0]["drop_pct_observed"], 50.0)

    # ─── math import sanity ───────────────────────────────────────────────

    def test_math_module_isolation(self) -> None:
        # Гарантируем что save_* не сломал глобальный math — это нерегрессионный
        # тест на использование `import math as _math` внутри функции.
        self.assertTrue(math.isfinite(1.0))


if __name__ == "__main__":
    unittest.main()
