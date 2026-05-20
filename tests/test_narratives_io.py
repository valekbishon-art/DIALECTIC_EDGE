"""Unit-tests для market_indicators.narratives_io.

Все embedding-клиенты и DB заменены DI-моками. Без сетевых вызовов.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime
from typing import Sequence
from unittest import mock

from market_indicators.narratives import NarrativeCluster, NarrativeDocument
from market_indicators.narratives_io import (
    IngestResult,
    NarrativeDBAdapter,
    _normalize_text_for_embed,
    _truncate_for_embedding,
    classify_asset_hint,
    compute_distance,
    feature_enabled,
    format_drift_summary,
    get_active_provider,
    get_cluster_threshold,
    get_drift_anchor_hours,
    get_drift_threshold,
    get_interval_seconds,
    get_max_docs_per_batch,
    get_min_cluster_docs,
    get_retention_days,
    get_velocity_window_hours,
    ingest_documents,
    make_doc_id,
    make_embedding_client,
)


class _MemoryDBAdapter(NarrativeDBAdapter):
    """In-memory реализация для тестов."""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.clusters: dict[int, NarrativeCluster] = {}
        self.anchors: dict[int, list[float]] = {}

    async def document_exists(self, doc_id: str) -> bool:
        return doc_id in self.documents

    async def save_document(
        self, *, doc: NarrativeDocument,
        embedding: Sequence[float], cluster_id: int,
    ) -> None:
        self.documents[doc.doc_id] = {
            "source": doc.source,
            "title": doc.title,
            "embedding": list(embedding),
            "cluster_id": cluster_id,
        }

    async def load_clusters(self) -> list[NarrativeCluster]:
        return list(self.clusters.values())

    async def save_cluster(self, *, cluster: NarrativeCluster) -> None:
        self.clusters[cluster.cluster_id] = cluster

    async def load_anchor_centroid(
        self, *, cluster_id: int, hours_ago: float,
    ) -> list[float] | None:
        return self.anchors.get(cluster_id)


def _doc(doc_id: str, *, source: str = "test", title: str = "", content: str = "",
         asset_hint: str | None = None) -> NarrativeDocument:
    return NarrativeDocument(
        doc_id=doc_id,
        source=source,
        title=title,
        content=content,
        published_at=datetime(2026, 1, 1, 12, 0),
        asset_hint=asset_hint,
    )


# ─── Text helpers ───────────────────────────────────────────────────────────


class TruncateTestCase(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(_truncate_for_embedding("hello"), "hello")

    def test_long_text_truncated(self):
        long_text = "a" * 10_000
        result = _truncate_for_embedding(long_text, max_chars=8000)
        self.assertEqual(len(result), 8000)

    def test_empty(self):
        self.assertEqual(_truncate_for_embedding(""), "")


class NormalizeForEmbedTestCase(unittest.TestCase):
    def test_title_duplicated(self):
        result = _normalize_text_for_embed("BTC ETF", "details")
        self.assertEqual(result.count("BTC ETF"), 2)

    def test_only_title(self):
        self.assertEqual(_normalize_text_for_embed("Headline", ""), "Headline")

    def test_only_content(self):
        self.assertEqual(_normalize_text_for_embed("", "Body"), "Body")


# ─── Doc-id hashing ──────────────────────────────────────────────────────────


class MakeDocIdTestCase(unittest.TestCase):
    def test_stable_for_same_input(self):
        a = make_doc_id(source="tavily", url="https://example.com/x", title="T")
        b = make_doc_id(source="tavily", url="https://example.com/x", title="T")
        self.assertEqual(a, b)

    def test_different_for_different_sources(self):
        a = make_doc_id(source="tavily", url="https://example.com/x", title="T")
        b = make_doc_id(source="gdelt", url="https://example.com/x", title="T")
        self.assertNotEqual(a, b)

    def test_url_case_insensitive(self):
        a = make_doc_id(source="t", url="https://EXAMPLE.com/x", title="T")
        b = make_doc_id(source="t", url="https://example.com/x", title="T")
        self.assertEqual(a, b)

    def test_empty_url_uses_title(self):
        result = make_doc_id(source="t", url="", title="some title")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)


# ─── classify_asset_hint ─────────────────────────────────────────────────────


class ClassifyAssetHintTestCase(unittest.TestCase):
    def test_btc_in_title(self):
        self.assertEqual(classify_asset_hint("BTC price drops", ""), "BTC")

    def test_bitcoin_word(self):
        self.assertEqual(classify_asset_hint("Bitcoin ETF news", ""), "BTC")

    def test_eth_in_content(self):
        self.assertEqual(classify_asset_hint("", "Ethereum upgrades"), "ETH")

    def test_solana(self):
        self.assertEqual(classify_asset_hint("Solana ecosystem", ""), "SOL")

    def test_no_hits(self):
        self.assertIsNone(classify_asset_hint("Generic crypto market", ""))

    def test_multiple_hits_returns_none(self):
        self.assertIsNone(classify_asset_hint("BTC vs ETH analysis", ""))

    def test_empty_input(self):
        self.assertIsNone(classify_asset_hint("", ""))


# ─── Env flag parsing ───────────────────────────────────────────────────────


class FeatureFlagTestCase(unittest.TestCase):
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_enabled())

    def test_on_with_1(self):
        with mock.patch.dict(os.environ, {"FEATURE_NARRATIVE_DRIFT": "1"}):
            self.assertTrue(feature_enabled())

    def test_on_with_true(self):
        with mock.patch.dict(os.environ, {"FEATURE_NARRATIVE_DRIFT": "true"}):
            self.assertTrue(feature_enabled())

    def test_off_with_garbage(self):
        with mock.patch.dict(os.environ, {"FEATURE_NARRATIVE_DRIFT": "lol"}):
            self.assertFalse(feature_enabled())


class GetActiveProviderTestCase(unittest.TestCase):
    def test_default_gemini(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_active_provider(), "gemini")

    def test_mistral(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_EMBEDDING_PROVIDER": "Mistral"}):
            self.assertEqual(get_active_provider(), "mistral")


class GetParamsTestCase(unittest.TestCase):
    def test_cluster_threshold_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertAlmostEqual(get_cluster_threshold(), 0.70, places=2)

    def test_cluster_threshold_clamped(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_CLUSTER_THRESHOLD": "5"}):
            self.assertEqual(get_cluster_threshold(), 1.0)
        with mock.patch.dict(os.environ, {"NARRATIVE_CLUSTER_THRESHOLD": "-2"}):
            self.assertEqual(get_cluster_threshold(), 0.0)
        with mock.patch.dict(os.environ, {"NARRATIVE_CLUSTER_THRESHOLD": "garbage"}):
            self.assertAlmostEqual(get_cluster_threshold(), 0.70, places=2)

    def test_drift_threshold(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_DRIFT_THRESHOLD": "0.3"}):
            self.assertAlmostEqual(get_drift_threshold(), 0.3)

    def test_velocity_window_minimum_1(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_VELOCITY_WINDOW_H": "0"}):
            self.assertGreaterEqual(get_velocity_window_hours(), 1.0)

    def test_anchor_hours(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_DRIFT_ANCHOR_H": "48"}):
            self.assertEqual(get_drift_anchor_hours(), 48.0)

    def test_interval_minimum_300(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_INTERVAL_SEC": "1"}):
            self.assertGreaterEqual(get_interval_seconds(), 300)

    def test_retention_minimum_7(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_RETENTION_DAYS": "1"}):
            self.assertGreaterEqual(get_retention_days(), 7)

    def test_max_docs_per_batch_clamped(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_MAX_DOCS_PER_BATCH": "9999"}):
            self.assertLessEqual(get_max_docs_per_batch(), 100)
        with mock.patch.dict(os.environ, {"NARRATIVE_MAX_DOCS_PER_BATCH": "0"}):
            self.assertGreaterEqual(get_max_docs_per_batch(), 1)

    def test_min_cluster_docs(self):
        with mock.patch.dict(os.environ, {"NARRATIVE_MIN_CLUSTER_DOCS": "5"}):
            self.assertEqual(get_min_cluster_docs(), 5)


# ─── make_embedding_client (с моком HTTP) ───────────────────────────────────


class MakeEmbeddingClientTestCase(unittest.TestCase):
    def test_raises_without_any_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                make_embedding_client()

    def test_returns_callable_with_gemini_key(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}):
            client = make_embedding_client()
            self.assertTrue(callable(client))

    def test_fallback_to_mistral_when_gemini_fails(self):
        async def _run():
            with mock.patch.dict(os.environ, {
                "GEMINI_API_KEY": "fake_g", "MISTRAL_API_KEY": "fake_m",
                "NARRATIVE_EMBEDDING_PROVIDER": "gemini",
            }):
                async def fake_gemini(texts, **kw):  # noqa: ARG001
                    raise RuntimeError("simulated gemini failure")

                async def fake_mistral(texts, **kw):  # noqa: ARG001
                    return [[1.0, 2.0] for _ in texts]

                with mock.patch(
                    "market_indicators.narratives_io.gemini_embed_batch",
                    new=fake_gemini,
                ), mock.patch(
                    "market_indicators.narratives_io.mistral_embed_batch",
                    new=fake_mistral,
                ):
                    client = make_embedding_client()
                    result = await client(["a", "b"])
                    self.assertEqual(len(result), 2)
                    self.assertEqual(result[0], [1.0, 2.0])

        asyncio.run(_run())

    def test_raises_when_all_providers_fail(self):
        async def _run():
            with mock.patch.dict(os.environ, {
                "GEMINI_API_KEY": "fake_g", "MISTRAL_API_KEY": "fake_m",
            }):
                async def boom(texts, **kw):  # noqa: ARG001
                    raise RuntimeError("simulated")

                with mock.patch(
                    "market_indicators.narratives_io.gemini_embed_batch",
                    new=boom,
                ), mock.patch(
                    "market_indicators.narratives_io.mistral_embed_batch",
                    new=boom,
                ):
                    client = make_embedding_client()
                    with self.assertRaises(RuntimeError):
                        await client(["a"])

        asyncio.run(_run())

    def test_empty_input_returns_empty(self):
        async def _run():
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "fake"}):
                client = make_embedding_client()
                self.assertEqual(await client([]), [])

        asyncio.run(_run())


# ─── ingest_documents pipeline ──────────────────────────────────────────────


class IngestDocumentsTestCase(unittest.TestCase):
    def _make_embedding_client(self, embeddings):
        async def _client(texts):
            return embeddings[: len(texts)]
        return _client

    def test_empty_docs(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client([])
            result = await ingest_documents(
                docs=[], embedding_client=client, db_adapter=db,
            )
            self.assertEqual(result.docs_processed, 0)
            self.assertEqual(len(db.clusters), 0)

        asyncio.run(_run())

    def test_creates_cluster_for_first_doc(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client([[1.0, 0.0]])
            result = await ingest_documents(
                docs=[_doc("a", title="BTC")],
                embedding_client=client, db_adapter=db,
            )
            self.assertEqual(result.docs_processed, 1)
            self.assertEqual(result.new_clusters, 1)
            self.assertEqual(result.joined_existing, 0)
            self.assertEqual(len(db.clusters), 1)

        asyncio.run(_run())

    def test_joins_existing_cluster_when_similar(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client([[1.0, 0.0], [0.95, 0.05]])
            await ingest_documents(
                docs=[_doc("a", title="A")],
                embedding_client=client, db_adapter=db,
            )
            client2 = self._make_embedding_client([[0.95, 0.05]])
            result = await ingest_documents(
                docs=[_doc("b", title="B")],
                embedding_client=client2, db_adapter=db,
                join_threshold=0.7,
            )
            self.assertEqual(result.joined_existing, 1)
            self.assertEqual(result.new_clusters, 0)

        asyncio.run(_run())

    def test_creates_new_cluster_when_dissimilar(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client([[1.0, 0.0]])
            await ingest_documents(
                docs=[_doc("a", title="A")],
                embedding_client=client, db_adapter=db,
            )
            client2 = self._make_embedding_client([[0.0, 1.0]])
            result = await ingest_documents(
                docs=[_doc("b", title="B")],
                embedding_client=client2, db_adapter=db,
                join_threshold=0.7,
            )
            self.assertEqual(result.new_clusters, 1)

        asyncio.run(_run())

    def test_dedup_skips_existing(self):
        async def _run():
            db = _MemoryDBAdapter()
            db.documents["a"] = {"foo": "bar"}
            client = self._make_embedding_client([[1.0, 0.0]])
            result = await ingest_documents(
                docs=[_doc("a", title="A")],
                embedding_client=client, db_adapter=db,
            )
            self.assertEqual(result.docs_processed, 0)
            self.assertEqual(result.docs_skipped_dup, 1)

        asyncio.run(_run())

    def test_in_batch_dedup(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client([[1.0, 0.0]])
            result = await ingest_documents(
                docs=[_doc("dup", title="A"), _doc("dup", title="B")],
                embedding_client=client, db_adapter=db,
            )
            self.assertEqual(result.docs_processed, 1)
            self.assertEqual(result.docs_skipped_dup, 1)

        asyncio.run(_run())

    def test_embedding_failure_returns_empty(self):
        async def _run():
            db = _MemoryDBAdapter()

            async def broken_client(texts):
                raise RuntimeError("simulated")

            result = await ingest_documents(
                docs=[_doc("a", title="A")],
                embedding_client=broken_client, db_adapter=db,
            )
            self.assertEqual(result.docs_processed, 0)
            self.assertEqual(len(db.clusters), 0)

        asyncio.run(_run())

    def test_embedding_count_mismatch_returns_empty(self):
        async def _run():
            db = _MemoryDBAdapter()

            async def short_client(texts):
                return []  # вернёт меньше чем запросили

            result = await ingest_documents(
                docs=[_doc("a", title="A")],
                embedding_client=short_client, db_adapter=db,
            )
            self.assertEqual(result.docs_processed, 0)

        asyncio.run(_run())

    def test_drift_detection_fires_when_anchor_far(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client(
                [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
            )
            await ingest_documents(
                docs=[_doc(f"d{i}", title=f"T{i}") for i in range(4)],
                embedding_client=client, db_adapter=db,
            )
            db.anchors[1] = [-1.0, 0.0]
            client2 = self._make_embedding_client([[1.0, 0.0]])
            result = await ingest_documents(
                docs=[_doc("d_new", title="new")],
                embedding_client=client2, db_adapter=db,
                drift_threshold=0.5, min_cluster_docs=3,
            )
            self.assertGreaterEqual(len(result.drift_events), 1)
            self.assertTrue(result.drift_events[0].is_drift)

        asyncio.run(_run())

    def test_no_drift_when_no_anchor(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client([[1.0, 0.0]])
            result = await ingest_documents(
                docs=[_doc("a", title="A")],
                embedding_client=client, db_adapter=db,
            )
            self.assertEqual(len(result.drift_events), 0)

        asyncio.run(_run())

    def test_empty_embedding_skipped(self):
        async def _run():
            db = _MemoryDBAdapter()
            client = self._make_embedding_client([[], [1.0, 0.0]])
            result = await ingest_documents(
                docs=[_doc("a", title="A"), _doc("b", title="B")],
                embedding_client=client, db_adapter=db,
            )
            self.assertEqual(result.new_clusters, 1)

        asyncio.run(_run())


# ─── Misc helpers ───────────────────────────────────────────────────────────


class FormatDriftSummaryTestCase(unittest.TestCase):
    def test_with_label(self):
        from market_indicators.narratives import DriftSignal
        ds = DriftSignal(cluster_id=42, distance=0.55, is_drift=True, n_docs=10)
        out = format_drift_summary(ds, cluster_label="BTC ETF outflows")
        self.assertIn("BTC ETF outflows", out)
        self.assertIn("0.55", out)
        self.assertIn("10", out)

    def test_without_label(self):
        from market_indicators.narratives import DriftSignal
        ds = DriftSignal(cluster_id=42, distance=0.55, is_drift=True, n_docs=10)
        out = format_drift_summary(ds)
        self.assertIn("42", out)


class ComputeDistanceTestCase(unittest.TestCase):
    def test_normal(self):
        d = compute_distance([1.0, 0.0], [0.0, 1.0])
        self.assertAlmostEqual(d, 1.0, places=4)

    def test_returns_none_for_none(self):
        self.assertIsNone(compute_distance(None, [1.0]))
        self.assertIsNone(compute_distance([1.0], None))

    def test_returns_none_for_dim_mismatch(self):
        self.assertIsNone(compute_distance([1.0], [1.0, 2.0]))


if __name__ == "__main__":
    unittest.main()
