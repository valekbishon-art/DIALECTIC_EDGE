"""Unit-tests для market_indicators.narratives (pure math)."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from market_indicators.narratives import (
    DEFAULT_CLUSTER_JOIN_THRESHOLD,
    DEFAULT_DRIFT_DISTANCE_THRESHOLD,
    DEFAULT_MIN_CLUSTER_DOCS,
    NarrativeCluster,
    apply_assignment_inplace,
    assign_document,
    centroid,
    compute_reach,
    compute_velocity,
    cosine_distance,
    cosine_similarity,
    detect_drift,
    find_best_cluster,
    rank_clusters_by_activity,
    update_centroid_incremental,
    vector_norm,
)


class CosineSimilarityTestCase(unittest.TestCase):
    def test_identical_vectors_returns_1(self):
        v = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(cosine_similarity(v, v), 1.0, places=6)

    def test_orthogonal_returns_0(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        self.assertAlmostEqual(cosine_similarity(a, b), 0.0, places=6)

    def test_opposite_returns_minus_1(self):
        a = [1.0, 2.0, 3.0]
        b = [-1.0, -2.0, -3.0]
        self.assertAlmostEqual(cosine_similarity(a, b), -1.0, places=6)

    def test_scale_invariance(self):
        a = [1.0, 2.0, 3.0]
        b = [10.0, 20.0, 30.0]
        self.assertAlmostEqual(cosine_similarity(a, b), 1.0, places=6)

    def test_empty_vectors(self):
        self.assertEqual(cosine_similarity([], []), 0.0)
        self.assertEqual(cosine_similarity([1.0], []), 0.0)

    def test_zero_vector(self):
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_dim_mismatch_raises(self):
        with self.assertRaises(ValueError):
            cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])

    def test_cosine_distance_complement(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        self.assertAlmostEqual(cosine_distance(a, b), 1.0, places=6)


class VectorNormTestCase(unittest.TestCase):
    def test_3_4_5_triangle(self):
        self.assertAlmostEqual(vector_norm([3.0, 4.0]), 5.0)

    def test_zero(self):
        self.assertEqual(vector_norm([0.0, 0.0]), 0.0)

    def test_empty(self):
        self.assertEqual(vector_norm([]), 0.0)


class CentroidTestCase(unittest.TestCase):
    def test_single_vector(self):
        self.assertEqual(centroid([[1.0, 2.0, 3.0]]), [1.0, 2.0, 3.0])

    def test_average_two(self):
        result = centroid([[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(result, [2.0, 3.0])

    def test_empty_list(self):
        self.assertEqual(centroid([]), [])

    def test_dim_mismatch_raises(self):
        with self.assertRaises(ValueError):
            centroid([[1.0, 2.0], [3.0]])


class UpdateCentroidIncrementalTestCase(unittest.TestCase):
    def test_first_doc_initialization(self):
        self.assertEqual(update_centroid_incremental([], 0, [1.0, 2.0]), [1.0, 2.0])

    def test_matches_batch_centroid_for_2(self):
        vecs = [[1.0, 0.0], [0.0, 1.0]]
        full = centroid(vecs)
        running = update_centroid_incremental([], 0, vecs[0])
        running = update_centroid_incremental(running, 1, vecs[1])
        for a, b in zip(full, running):
            self.assertAlmostEqual(a, b, places=6)

    def test_matches_batch_centroid_for_5(self):
        vecs = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [-1.0, 0.0], [10.0, 10.0]]
        full = centroid(vecs)
        running = []
        n = 0
        for v in vecs:
            running = update_centroid_incremental(running, n, v)
            n += 1
        for a, b in zip(full, running):
            self.assertAlmostEqual(a, b, places=6)

    def test_negative_n_raises(self):
        with self.assertRaises(ValueError):
            update_centroid_incremental([1.0], -1, [1.0])

    def test_dim_mismatch_raises(self):
        with self.assertRaises(ValueError):
            update_centroid_incremental([1.0, 2.0], 1, [1.0])


class FindBestClusterTestCase(unittest.TestCase):
    def test_returns_closest(self):
        c1 = NarrativeCluster(1, [1.0, 0.0], 1)
        c2 = NarrativeCluster(2, [0.0, 1.0], 1)
        result = find_best_cluster([0.9, 0.1], [c1, c2])
        assert result is not None
        cluster_id, sim = result
        self.assertEqual(cluster_id, 1)
        self.assertGreater(sim, 0.9)

    def test_returns_none_for_empty(self):
        self.assertIsNone(find_best_cluster([1.0, 2.0], []))

    def test_skips_clusters_with_empty_centroid(self):
        c1 = NarrativeCluster(1, [], 0)
        c2 = NarrativeCluster(2, [1.0, 0.0], 1)
        result = find_best_cluster([1.0, 0.0], [c1, c2])
        assert result is not None
        self.assertEqual(result[0], 2)


class AssignDocumentTestCase(unittest.TestCase):
    def test_creates_new_when_no_clusters(self):
        assignment = assign_document(
            [1.0, 0.0], [], join_threshold=0.7, next_cluster_id=1,
        )
        self.assertTrue(assignment.created_new)
        self.assertEqual(assignment.cluster_id, 1)

    def test_joins_existing_when_sim_above_threshold(self):
        c1 = NarrativeCluster(5, [1.0, 0.0], 3)
        assignment = assign_document(
            [0.95, 0.05], [c1], join_threshold=0.7, next_cluster_id=99,
        )
        self.assertFalse(assignment.created_new)
        self.assertEqual(assignment.cluster_id, 5)

    def test_creates_new_when_sim_below_threshold(self):
        c1 = NarrativeCluster(5, [1.0, 0.0], 3)
        assignment = assign_document(
            [0.0, 1.0], [c1], join_threshold=0.7, next_cluster_id=99,
        )
        self.assertTrue(assignment.created_new)
        self.assertEqual(assignment.cluster_id, 99)

    def test_empty_embedding_raises(self):
        with self.assertRaises(ValueError):
            assign_document([], [], join_threshold=0.7, next_cluster_id=1)

    def test_threshold_exactly_at_boundary_joins(self):
        c1 = NarrativeCluster(7, [1.0, 0.0], 3)
        assignment = assign_document(
            [1.0, 0.0], [c1], join_threshold=1.0, next_cluster_id=99,
        )
        self.assertFalse(assignment.created_new)
        self.assertEqual(assignment.cluster_id, 7)


class ApplyAssignmentInPlaceTestCase(unittest.TestCase):
    def test_first_join_updates_state(self):
        c = NarrativeCluster(1, [1.0, 0.0], 1, {"tavily"})
        moment = datetime(2026, 1, 1, 12, 0)
        apply_assignment_inplace(
            c, embedding=[0.5, 0.5], source="gdelt", seen_at=moment,
        )
        self.assertEqual(c.n_docs, 2)
        self.assertIn("gdelt", c.sources)
        self.assertEqual(c.last_seen_at, moment)
        self.assertAlmostEqual(c.centroid[0], 0.75, places=6)
        self.assertAlmostEqual(c.centroid[1], 0.25, places=6)

    def test_first_seen_sets_created_at(self):
        c = NarrativeCluster(1, [1.0, 0.0], 1)
        self.assertIsNone(c.created_at)
        moment = datetime(2026, 1, 1)
        apply_assignment_inplace(
            c, embedding=[1.0, 0.0], source="x", seen_at=moment,
        )
        self.assertEqual(c.created_at, moment)


class ComputeVelocityTestCase(unittest.TestCase):
    def test_zero_for_empty(self):
        self.assertEqual(
            compute_velocity([], now=datetime(2026, 1, 1), window_hours=24), 0.0
        )

    def test_zero_for_negative_window(self):
        now = datetime(2026, 1, 1)
        self.assertEqual(
            compute_velocity([now], now=now, window_hours=-1), 0.0
        )

    def test_typical_rate(self):
        now = datetime(2026, 1, 1, 12, 0)
        timestamps = [now - timedelta(hours=h) for h in range(0, 12)]
        rate = compute_velocity(timestamps, now=now, window_hours=24)
        self.assertAlmostEqual(rate, 12.0 / 24.0)

    def test_excludes_older_than_window(self):
        now = datetime(2026, 1, 1, 12, 0)
        # 5 в окне, 3 вне
        in_window = [now - timedelta(hours=h) for h in range(5)]
        out_window = [now - timedelta(hours=h) for h in (30, 50, 100)]
        rate = compute_velocity(in_window + out_window, now=now, window_hours=24)
        self.assertAlmostEqual(rate, 5.0 / 24.0)

    def test_excludes_future_timestamps(self):
        now = datetime(2026, 1, 1, 12, 0)
        timestamps = [now + timedelta(hours=1)]
        self.assertEqual(compute_velocity(timestamps, now=now, window_hours=24), 0.0)


class ComputeReachTestCase(unittest.TestCase):
    def test_set_input(self):
        self.assertEqual(compute_reach({"a", "b", "c"}), 3)

    def test_list_input_deduped(self):
        self.assertEqual(compute_reach(["a", "a", "b"]), 2)

    def test_empty(self):
        self.assertEqual(compute_reach(set()), 0)
        self.assertEqual(compute_reach([]), 0)


class DetectDriftTestCase(unittest.TestCase):
    def test_returns_none_without_anchor(self):
        self.assertIsNone(detect_drift(
            current_centroid=[1.0, 0.0], anchor_centroid=None, n_docs=10,
        ))

    def test_returns_none_when_n_docs_below_min(self):
        self.assertIsNone(detect_drift(
            current_centroid=[1.0, 0.0], anchor_centroid=[0.0, 1.0],
            n_docs=1, min_docs=3,
        ))

    def test_returns_none_for_dim_mismatch(self):
        self.assertIsNone(detect_drift(
            current_centroid=[1.0], anchor_centroid=[1.0, 0.0],
            n_docs=10, min_docs=1,
        ))

    def test_flags_drift_when_distance_above_threshold(self):
        signal = detect_drift(
            current_centroid=[1.0, 0.0],
            anchor_centroid=[0.0, 1.0],
            n_docs=10, threshold=0.5,
        )
        assert signal is not None
        self.assertTrue(signal.is_drift)
        self.assertAlmostEqual(signal.distance, 1.0, places=6)

    def test_does_not_flag_when_below_threshold(self):
        signal = detect_drift(
            current_centroid=[1.0, 0.1],
            anchor_centroid=[1.0, 0.0],
            n_docs=10, threshold=0.5,
        )
        assert signal is not None
        self.assertFalse(signal.is_drift)
        self.assertLess(signal.distance, 0.5)

    def test_default_min_docs_filter(self):
        # n_docs ниже DEFAULT_MIN_CLUSTER_DOCS=3 → None
        self.assertIsNone(detect_drift(
            current_centroid=[1.0, 0.0], anchor_centroid=[0.0, 1.0], n_docs=2,
        ))


class RankClustersTestCase(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(rank_clusters_by_activity([], now=datetime(2026, 1, 1)), [])

    def test_more_docs_ranks_higher(self):
        now = datetime(2026, 1, 1, 12, 0)
        c_small = NarrativeCluster(
            1, [1.0, 0.0], n_docs=2, sources={"a"}, last_seen_at=now,
        )
        c_large = NarrativeCluster(
            2, [0.0, 1.0], n_docs=20, sources={"a"}, last_seen_at=now,
        )
        ranked = rank_clusters_by_activity([c_small, c_large], now=now)
        self.assertEqual(ranked[0][0].cluster_id, 2)

    def test_older_cluster_ranks_lower(self):
        now = datetime(2026, 1, 1, 12, 0)
        c_fresh = NarrativeCluster(
            1, [1.0, 0.0], n_docs=5, sources={"a"}, last_seen_at=now,
        )
        c_old = NarrativeCluster(
            2, [0.0, 1.0], n_docs=5, sources={"a"},
            last_seen_at=now - timedelta(hours=48),
        )
        ranked = rank_clusters_by_activity([c_old, c_fresh], now=now)
        self.assertEqual(ranked[0][0].cluster_id, 1)

    def test_more_sources_ranks_higher(self):
        now = datetime(2026, 1, 1, 12, 0)
        c_narrow = NarrativeCluster(
            1, [1.0, 0.0], n_docs=5, sources={"a"}, last_seen_at=now,
        )
        c_wide = NarrativeCluster(
            2, [0.0, 1.0], n_docs=5, sources={"a", "b", "c"}, last_seen_at=now,
        )
        ranked = rank_clusters_by_activity([c_narrow, c_wide], now=now)
        self.assertEqual(ranked[0][0].cluster_id, 2)

    def test_top_k_truncation(self):
        now = datetime(2026, 1, 1, 12, 0)
        clusters = [
            NarrativeCluster(
                i, [float(i), 0.0], n_docs=i, sources={"a"}, last_seen_at=now,
            )
            for i in range(1, 11)
        ]
        ranked = rank_clusters_by_activity(clusters, now=now, top_k=3)
        self.assertEqual(len(ranked), 3)


class ConstantsTestCase(unittest.TestCase):
    """Smoke-проверка что константы в разумных диапазонах."""

    def test_threshold_in_unit_interval(self):
        self.assertGreater(DEFAULT_CLUSTER_JOIN_THRESHOLD, 0.5)
        self.assertLessEqual(DEFAULT_CLUSTER_JOIN_THRESHOLD, 1.0)

    def test_drift_threshold_positive(self):
        self.assertGreater(DEFAULT_DRIFT_DISTANCE_THRESHOLD, 0.0)
        self.assertLess(DEFAULT_DRIFT_DISTANCE_THRESHOLD, 2.0)

    def test_min_cluster_docs(self):
        self.assertGreaterEqual(DEFAULT_MIN_CLUSTER_DOCS, 2)


if __name__ == "__main__":
    unittest.main()
