"""DB tests для options_skew_snapshots."""

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


class OptionsSkewDBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "oskew_test.db"

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


class SchemaTestCase(OptionsSkewDBTestBase):
    def test_table_exists(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='options_skew_snapshots'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_indexes_exist(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            self.assertIn("idx_oskew_currency_ts", names)
            self.assertIn("idx_oskew_class", names)


class CrudTestCase(OptionsSkewDBTestBase):
    def _save(self, **overrides):
        defaults = dict(
            currency="BTC",
            timestamp_ms=1700000000000,
            underlying_price=100_000.0,
            near_expiry_days=7,
            near_atm_iv=0.65,
            near_rr_25d=-0.03,
            far_expiry_days=30,
            far_atm_iv=0.60,
            far_rr_25d=-0.05,
            atm_iv_term_slope=-0.05,
            skew_class="extreme_put_skew",
            venues_csv="deribit",
        )
        defaults.update(overrides)
        return _run(self.db.save_options_skew_snapshot(**defaults))

    def test_save_returns_rowid(self) -> None:
        rowid = self._save()
        self.assertGreater(rowid, 0)

    def test_nan_inf_become_null(self) -> None:
        self._save(
            near_atm_iv=float("nan"),
            near_rr_25d=float("inf"),
            far_atm_iv=float("-inf"),
            far_rr_25d=None,
            atm_iv_term_slope=None,
        )
        rows = _run(self.db.get_recent_options_skew_snapshots(currency="BTC", limit=1))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIsNone(r["near_atm_iv"])
        self.assertIsNone(r["near_rr_25d"])
        self.assertIsNone(r["far_atm_iv"])
        self.assertIsNone(r["far_rr_25d"])
        self.assertIsNone(r["atm_iv_term_slope"])

    def test_optional_int_fields_null(self) -> None:
        self._save(near_expiry_days=None, far_expiry_days=None)
        rows = _run(self.db.get_recent_options_skew_snapshots(currency="BTC", limit=1))
        self.assertIsNone(rows[0]["near_expiry_days"])
        self.assertIsNone(rows[0]["far_expiry_days"])

    def test_get_recent_desc_order(self) -> None:
        self._save(timestamp_ms=1000, far_rr_25d=-0.01)
        self._save(timestamp_ms=2000, far_rr_25d=-0.05)
        rows = _run(self.db.get_recent_options_skew_snapshots(currency="BTC", limit=10))
        self.assertEqual(rows[0]["timestamp_ms"], 2000)
        self.assertEqual(rows[1]["timestamp_ms"], 1000)

    def test_get_recent_filters_by_currency(self) -> None:
        self._save(currency="BTC", timestamp_ms=1000)
        self._save(currency="ETH", timestamp_ms=1000)
        btc_rows = _run(self.db.get_recent_options_skew_snapshots(currency="BTC", limit=10))
        eth_rows = _run(self.db.get_recent_options_skew_snapshots(currency="ETH", limit=10))
        self.assertEqual(len(btc_rows), 1)
        self.assertEqual(len(eth_rows), 1)
        self.assertEqual(btc_rows[0]["currency"], "BTC")
        self.assertEqual(eth_rows[0]["currency"], "ETH")

    def test_count_options_skew_class(self) -> None:
        self._save(skew_class="put_skew")
        self._save(skew_class="put_skew")
        self._save(skew_class="neutral")
        cnt_put = _run(self.db.count_options_skew_class(
            currency="BTC", skew_class="put_skew", lookback_hours=24,
        ))
        cnt_neutral = _run(self.db.count_options_skew_class(
            currency="BTC", skew_class="neutral", lookback_hours=24,
        ))
        cnt_other = _run(self.db.count_options_skew_class(
            currency="BTC", skew_class="call_skew", lookback_hours=24,
        ))
        self.assertEqual(cnt_put, 2)
        self.assertEqual(cnt_neutral, 1)
        self.assertEqual(cnt_other, 0)

    def test_currency_normalized_uppercase(self) -> None:
        self._save(currency="btc")
        rows = _run(self.db.get_recent_options_skew_snapshots(currency="BTC", limit=10))
        self.assertEqual(rows[0]["currency"], "BTC")


if __name__ == "__main__":
    unittest.main()
