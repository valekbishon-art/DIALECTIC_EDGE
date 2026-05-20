"""DB tests для stablecoin_supply_snapshots + stablecoin_flow_snapshots."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


class StablecoinDBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "sc_test.db"

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


class SchemaTestCase(StablecoinDBTestBase):
    def test_supply_table_exists(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='stablecoin_supply_snapshots'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_flow_table_exists(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='stablecoin_flow_snapshots'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_indexes_exist(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            self.assertIn("idx_sc_supply_token_chain_ts", names)
            self.assertIn("idx_sc_flow_token_ts", names)
            self.assertIn("idx_sc_flow_class", names)


class SupplyCrudTestCase(StablecoinDBTestBase):
    def test_save_returns_rowid(self) -> None:
        rowid = _run(self.db.save_stablecoin_supply_snapshot(
            token="USDT", chain="ethereum",
            raw_supply_units_str=str(60_000_000_000 * 10**6),
            decimals=6, timestamp_ms=1700000000000,
        ))
        self.assertGreater(rowid, 0)

    def test_large_supply_stored_as_text(self) -> None:
        # Реальные supply'ы USDT > 2^63, в TEXT-колонке должно сохраняться.
        big = 10**30  # 1e30 raw_units
        _run(self.db.save_stablecoin_supply_snapshot(
            token="USDT", chain="ethereum",
            raw_supply_units_str=str(big),
            decimals=6, timestamp_ms=1700000000000,
        ))
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT raw_supply_units_str FROM stablecoin_supply_snapshots LIMIT 1"
            ).fetchone()
            self.assertEqual(int(row[0]), big)

    def test_token_chain_normalized(self) -> None:
        _run(self.db.save_stablecoin_supply_snapshot(
            token="usdt", chain="ETHEREUM",
            raw_supply_units_str="100", decimals=6, timestamp_ms=1000,
        ))
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT token, chain FROM stablecoin_supply_snapshots LIMIT 1"
            ).fetchone()
            self.assertEqual(row[0], "USDT")
            self.assertEqual(row[1], "ethereum")

    def test_get_supply_snapshot_at_or_before_empty(self) -> None:
        out = _run(self.db.get_supply_snapshot_at_or_before(
            token="USDT", chain="ethereum", hours_ago=24.0,
        ))
        self.assertIsNone(out)

    def test_get_supply_snapshot_at_or_before_returns_old(self) -> None:
        # Создаём snapshot прямо сейчас, потом запрашиваем "≤ 24ч назад" —
        # текущий не должен попасть (created_at=now). Используем
        # hours_ago=0 для теста с границей now (вернётся None для строго <)
        # либо нужно вставить created_at вручную.
        _run(self.db.save_stablecoin_supply_snapshot(
            token="USDT", chain="ethereum",
            raw_supply_units_str="100", decimals=6, timestamp_ms=1000,
        ))
        # Сразу подкорректируем created_at в прошлое.
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE stablecoin_supply_snapshots "
                "SET created_at = datetime('now', '-25 hours')"
            )
            conn.commit()
        time.sleep(0.05)
        out = _run(self.db.get_supply_snapshot_at_or_before(
            token="USDT", chain="ethereum", hours_ago=24.0,
        ))
        self.assertIsNotNone(out)
        self.assertEqual(out["token"], "USDT")
        self.assertEqual(out["chain"], "ethereum")
        self.assertEqual(int(out["raw_supply_units_str"]), 100)


class FlowCrudTestCase(StablecoinDBTestBase):
    def _save_flow(self, **overrides):
        defaults = dict(
            token="USDT", timestamp_ms=1000,
            supply_total_usd=140e9, delta_24h_usd=500e6,
            delta_pct_24h=500e6 / 140e9,
            flow_class="mint", chains_csv="ethereum,tron",
        )
        defaults.update(overrides)
        return _run(self.db.save_stablecoin_flow_snapshot(**defaults))

    def test_save_returns_rowid(self) -> None:
        rowid = self._save_flow()
        self.assertGreater(rowid, 0)

    def test_nan_inf_become_null(self) -> None:
        self._save_flow(
            delta_24h_usd=float("nan"),
            delta_pct_24h=float("inf"),
        )
        rows = _run(self.db.get_recent_stablecoin_flow_snapshots(token="USDT", limit=1))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["delta_24h_usd"])
        self.assertIsNone(rows[0]["delta_pct_24h"])

    def test_get_recent_desc_order(self) -> None:
        self._save_flow(timestamp_ms=1000)
        self._save_flow(timestamp_ms=2000)
        rows = _run(self.db.get_recent_stablecoin_flow_snapshots(token="USDT", limit=10))
        self.assertEqual(rows[0]["timestamp_ms"], 2000)
        self.assertEqual(rows[1]["timestamp_ms"], 1000)

    def test_get_recent_filters_by_token(self) -> None:
        self._save_flow(token="USDT")
        self._save_flow(token="USDC")
        usdt_rows = _run(self.db.get_recent_stablecoin_flow_snapshots(token="USDT", limit=10))
        usdc_rows = _run(self.db.get_recent_stablecoin_flow_snapshots(token="USDC", limit=10))
        self.assertEqual(len(usdt_rows), 1)
        self.assertEqual(len(usdc_rows), 1)
        self.assertEqual(usdt_rows[0]["token"], "USDT")
        self.assertEqual(usdc_rows[0]["token"], "USDC")

    def test_count_flow_class(self) -> None:
        self._save_flow(flow_class="mint")
        self._save_flow(flow_class="mint")
        self._save_flow(flow_class="neutral")
        cnt_mint = _run(self.db.count_stablecoin_flow_class(
            token="USDT", flow_class="mint", lookback_hours=24,
        ))
        cnt_neutral = _run(self.db.count_stablecoin_flow_class(
            token="USDT", flow_class="neutral", lookback_hours=24,
        ))
        cnt_redeem = _run(self.db.count_stablecoin_flow_class(
            token="USDT", flow_class="redeem", lookback_hours=24,
        ))
        self.assertEqual(cnt_mint, 2)
        self.assertEqual(cnt_neutral, 1)
        self.assertEqual(cnt_redeem, 0)

    def test_unknown_class_default(self) -> None:
        # Передаём пустой/None flow_class → должен быть 'unknown'.
        _run(self.db.save_stablecoin_flow_snapshot(
            token="USDT", timestamp_ms=1000, supply_total_usd=1e9,
            delta_24h_usd=None, delta_pct_24h=None,
            flow_class="", chains_csv=None,
        ))
        rows = _run(self.db.get_recent_stablecoin_flow_snapshots(token="USDT", limit=1))
        self.assertEqual(rows[0]["flow_class"], "unknown")

    def test_token_normalized_uppercase(self) -> None:
        self._save_flow(token="usdt")
        rows = _run(self.db.get_recent_stablecoin_flow_snapshots(token="USDT", limit=1))
        self.assertEqual(rows[0]["token"], "USDT")


if __name__ == "__main__":
    unittest.main()
