"""DB tests для funding_term_snapshots."""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


class FundingTermDBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "fts_test.db"

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


class SchemaTestCase(FundingTermDBTestBase):
    def test_table_exists(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='funding_term_snapshots'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_indexes_exist(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            self.assertIn("idx_fts_asset_ts", names)
            self.assertIn("idx_fts_inverted", names)

    def test_check_is_inverted(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO funding_term_snapshots "
                    "(asset, timestamp_ms, is_inverted) VALUES (?, ?, ?)",
                    ("BTC", 1700000000000, 2),
                )


class CrudTestCase(FundingTermDBTestBase):
    def test_save_returns_rowid(self) -> None:
        rowid = _run(self.db.save_funding_term_snapshot(
            asset="BTC", timestamp_ms=1700000000000,
            spot_funding_annual=0.10,
            monthly_basis_annual=0.08,
            quarterly_basis_annual=0.05,
            slope_annual=-0.05, is_inverted=1,
            venues_csv="bybit,binance",
        ))
        self.assertGreater(rowid, 0)

    def test_nan_inf_become_null(self) -> None:
        _run(self.db.save_funding_term_snapshot(
            asset="BTC", timestamp_ms=1700000000000,
            spot_funding_annual=float("nan"),
            monthly_basis_annual=float("inf"),
            quarterly_basis_annual=float("-inf"),
            slope_annual=None, is_inverted=0,
            venues_csv=None,
        ))
        rows = _run(self.db.get_recent_funding_term_snapshots(asset="BTC", limit=1))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIsNone(r["spot_funding_annual"])
        self.assertIsNone(r["monthly_basis_annual"])
        self.assertIsNone(r["quarterly_basis_annual"])
        self.assertIsNone(r["slope_annual"])

    def test_get_recent_desc_order(self) -> None:
        _run(self.db.save_funding_term_snapshot(
            asset="BTC", timestamp_ms=1000,
            spot_funding_annual=0.1, monthly_basis_annual=None,
            quarterly_basis_annual=None, slope_annual=None,
            is_inverted=0, venues_csv=None,
        ))
        _run(self.db.save_funding_term_snapshot(
            asset="BTC", timestamp_ms=2000,
            spot_funding_annual=0.2, monthly_basis_annual=None,
            quarterly_basis_annual=None, slope_annual=None,
            is_inverted=0, venues_csv=None,
        ))
        rows = _run(self.db.get_recent_funding_term_snapshots(asset="BTC", limit=10))
        self.assertEqual(rows[0]["timestamp_ms"], 2000)
        self.assertEqual(rows[1]["timestamp_ms"], 1000)

    def test_get_recent_filters_by_asset(self) -> None:
        _run(self.db.save_funding_term_snapshot(
            asset="BTC", timestamp_ms=1000,
            spot_funding_annual=0.1, monthly_basis_annual=None,
            quarterly_basis_annual=None, slope_annual=None,
            is_inverted=0, venues_csv=None,
        ))
        _run(self.db.save_funding_term_snapshot(
            asset="ETH", timestamp_ms=1000,
            spot_funding_annual=0.05, monthly_basis_annual=None,
            quarterly_basis_annual=None, slope_annual=None,
            is_inverted=0, venues_csv=None,
        ))
        btc_rows = _run(self.db.get_recent_funding_term_snapshots(asset="BTC", limit=10))
        self.assertEqual(len(btc_rows), 1)
        self.assertEqual(btc_rows[0]["asset"], "BTC")

    def test_count_inversions(self) -> None:
        _run(self.db.save_funding_term_snapshot(
            asset="BTC", timestamp_ms=1000,
            spot_funding_annual=0.1, monthly_basis_annual=None,
            quarterly_basis_annual=None, slope_annual=-0.05,
            is_inverted=1, venues_csv=None,
        ))
        _run(self.db.save_funding_term_snapshot(
            asset="BTC", timestamp_ms=2000,
            spot_funding_annual=0.1, monthly_basis_annual=None,
            quarterly_basis_annual=None, slope_annual=0.05,
            is_inverted=0, venues_csv=None,
        ))
        count = _run(self.db.count_funding_term_inversions(
            asset="BTC", lookback_hours=24,
        ))
        self.assertEqual(count, 1)

    def test_count_inversions_empty(self) -> None:
        count = _run(self.db.count_funding_term_inversions(
            asset="BTC", lookback_hours=24,
        ))
        self.assertEqual(count, 0)


class HelperTestCase(FundingTermDBTestBase):
    def test_nan_to_none_real(self) -> None:
        self.assertIsNone(self.db._nan_to_none_real(None))
        self.assertIsNone(self.db._nan_to_none_real(float("nan")))
        self.assertIsNone(self.db._nan_to_none_real(float("inf")))
        self.assertIsNone(self.db._nan_to_none_real(float("-inf")))
        self.assertEqual(self.db._nan_to_none_real(0.5), 0.5)


if __name__ == "__main__":
    unittest.main()
