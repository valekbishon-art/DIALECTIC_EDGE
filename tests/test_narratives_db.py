"""DB tests для narrative_documents / narrative_clusters / snapshots.

Использует tempdir + monkeypatch DB_PATH для изоляции (тот же паттерн что
у test_microstructure_db.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


class _NarrativeDBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "narrative_test.db"

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


# ─── Schema ─────────────────────────────────────────────────────────────────


class SchemaTestCase(_NarrativeDBTestBase):
    def test_narrative_documents_exists(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='narrative_documents'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_narrative_clusters_exists(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='narrative_clusters'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_narrative_cluster_snapshots_exists(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='narrative_cluster_snapshots'"
            ).fetchone()
            self.assertIsNotNone(row)

    def test_indexes_exist(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            self.assertIn("idx_narr_doc_cluster_created", indexes)
            self.assertIn("idx_narr_doc_source_created", indexes)
            self.assertIn("idx_narr_cluster_lastseen", indexes)
            self.assertIn("idx_narr_snap_cluster_created", indexes)

    def test_doc_id_unique_constraint(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO narrative_documents "
                "(doc_id, source, embedding_json, cluster_id) "
                "VALUES (?, ?, ?, ?)",
                ("dup", "test", "[]", 1),
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO narrative_documents "
                    "(doc_id, source, embedding_json, cluster_id) "
                    "VALUES (?, ?, ?, ?)",
                    ("dup", "test2", "[]", 2),
                )

    def test_doc_id_length_check(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO narrative_documents "
                    "(doc_id, source, embedding_json, cluster_id) "
                    "VALUES (?, ?, ?, ?)",
                    ("", "test", "[]", 1),
                )

    def test_cluster_n_docs_check(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO narrative_clusters "
                    "(cluster_id, centroid_json, n_docs) "
                    "VALUES (?, ?, ?)",
                    (1, "[]", -1),
                )


# ─── CRUD ───────────────────────────────────────────────────────────────────


class CrudTestCase(_NarrativeDBTestBase):
    def test_document_exists_negative(self) -> None:
        self.assertFalse(_run(self.db.narrative_document_exists(doc_id="absent")))

    def test_save_then_exists(self) -> None:
        rowid = _run(self.db.save_narrative_document(
            doc_id="a", source="tavily", title="T", content="C",
            asset_hint="BTC", published_at="2026-01-01T00:00:00",
            embedding_json="[0.1, 0.2]", cluster_id=1,
        ))
        self.assertGreater(rowid, 0)
        self.assertTrue(_run(self.db.narrative_document_exists(doc_id="a")))

    def test_save_idempotent_on_duplicate_doc_id(self) -> None:
        _run(self.db.save_narrative_document(
            doc_id="a", source="tavily", title="T", content="C",
            asset_hint=None, published_at=None,
            embedding_json="[0.1]", cluster_id=1,
        ))
        # Второй save с тем же doc_id не должен падать
        _run(self.db.save_narrative_document(
            doc_id="a", source="tavily", title="T2", content="C2",
            asset_hint=None, published_at=None,
            embedding_json="[0.2]", cluster_id=2,
        ))
        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM narrative_documents WHERE doc_id = ?",
                ("a",),
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_upsert_cluster_insert_then_update(self) -> None:
        _run(self.db.upsert_narrative_cluster(
            cluster_id=10, centroid_json="[1.0]", n_docs=1,
            sources_json='["a"]', created_at=None,
            last_seen_at="2026-01-01T00:00:00", label=None,
        ))
        _run(self.db.upsert_narrative_cluster(
            cluster_id=10, centroid_json="[2.0]", n_docs=5,
            sources_json='["a","b"]', created_at=None,
            last_seen_at="2026-01-02T00:00:00", label="BTC narrative",
        ))
        clusters = _run(self.db.load_narrative_clusters())
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertEqual(c["n_docs"], 5)
        self.assertEqual(c["label"], "BTC narrative")
        self.assertEqual(c["centroid_json"], "[2.0]")

    def test_upsert_cluster_writes_snapshot_each_time(self) -> None:
        for i in range(3):
            _run(self.db.upsert_narrative_cluster(
                cluster_id=20, centroid_json=f"[{i}.0]", n_docs=i + 1,
                sources_json="[]", created_at=None,
                last_seen_at=None, label=None,
            ))
        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM narrative_cluster_snapshots "
                "WHERE cluster_id = ?",
                (20,),
            ).fetchone()[0]
            self.assertEqual(count, 3)

    def test_load_clusters_orders_by_last_seen_desc(self) -> None:
        _run(self.db.upsert_narrative_cluster(
            cluster_id=1, centroid_json="[]", n_docs=1,
            sources_json="[]", created_at=None,
            last_seen_at="2026-01-01T00:00:00", label=None,
        ))
        _run(self.db.upsert_narrative_cluster(
            cluster_id=2, centroid_json="[]", n_docs=1,
            sources_json="[]", created_at=None,
            last_seen_at="2026-01-03T00:00:00", label=None,
        ))
        clusters = _run(self.db.load_narrative_clusters())
        self.assertEqual(clusters[0]["cluster_id"], 2)
        self.assertEqual(clusters[1]["cluster_id"], 1)

    def test_anchor_centroid_picks_old_snapshot(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO narrative_cluster_snapshots "
                "(created_at, cluster_id, n_docs, centroid_json) "
                "VALUES (datetime('now', '-100 hours'), 1, 5, ?)",
                (json.dumps([1.0, 0.0]),),
            )
            conn.execute(
                "INSERT INTO narrative_cluster_snapshots "
                "(created_at, cluster_id, n_docs, centroid_json) "
                "VALUES (datetime('now', '-1 hour'), 1, 6, ?)",
                (json.dumps([0.5, 0.5]),),
            )
            conn.commit()

        anchor = _run(self.db.load_narrative_anchor_centroid(
            cluster_id=1, hours_ago=72.0,
        ))
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor, [1.0, 0.0])

    def test_anchor_returns_none_when_no_old_snapshot(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO narrative_cluster_snapshots "
                "(created_at, cluster_id, n_docs, centroid_json) "
                "VALUES (datetime('now', '-1 hour'), 1, 5, ?)",
                (json.dumps([1.0, 0.0]),),
            )
            conn.commit()

        anchor = _run(self.db.load_narrative_anchor_centroid(
            cluster_id=1, hours_ago=72.0,
        ))
        self.assertIsNone(anchor)

    def test_get_recent_documents_limit(self) -> None:
        for i in range(5):
            _run(self.db.save_narrative_document(
                doc_id=f"d{i}", source="t", title=f"T{i}",
                content="", asset_hint=None, published_at=None,
                embedding_json="[]", cluster_id=100,
            ))
        docs = _run(self.db.get_recent_narrative_documents(
            cluster_id=100, limit=3,
        ))
        self.assertEqual(len(docs), 3)

    def test_get_active_narratives(self) -> None:
        _run(self.db.upsert_narrative_cluster(
            cluster_id=1, centroid_json="[]", n_docs=5,
            sources_json="[]", created_at=None,
            last_seen_at="2026-01-02T00:00:00", label="BTC",
        ))
        _run(self.db.upsert_narrative_cluster(
            cluster_id=2, centroid_json="[]", n_docs=3,
            sources_json="[]", created_at=None,
            last_seen_at="2026-01-01T00:00:00", label="ETH",
        ))
        top = _run(self.db.get_active_narratives(limit=10))
        self.assertEqual(top[0]["cluster_id"], 1)
        self.assertEqual(top[0]["label"], "BTC")

    def test_cleanup_old_data(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO narrative_documents "
                "(created_at, doc_id, source, embedding_json, cluster_id) "
                "VALUES (datetime('now', '-500 days'), 'old', 't', '[]', 1)"
            )
            conn.execute(
                "INSERT INTO narrative_documents "
                "(created_at, doc_id, source, embedding_json, cluster_id) "
                "VALUES (datetime('now', '-1 day'), 'fresh', 't', '[]', 1)"
            )
            conn.commit()

        deleted = _run(self.db.cleanup_old_narrative_data(retention_days=180))
        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(_run(self.db.narrative_document_exists(doc_id="old")))
        self.assertTrue(_run(self.db.narrative_document_exists(doc_id="fresh")))


if __name__ == "__main__":
    unittest.main()
