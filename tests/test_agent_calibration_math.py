"""Unit-тесты для core/agent_calibration.py (математические primitives).

Покрывают:
  * `clip_probability` — clip в [eps, 1-eps].
  * `shrink_brier` — Bayesian shrinkage к prior'у COIN_FLIP_BRIER=0.25:
      - n=0 возвращает prior;
      - n→∞ возвращает observed;
      - корректные веса при разных prior_strength.
  * `brier_to_weight` — convert Brier → weight ∈ [0, 1]:
      - Brier=0 → w=1, Brier=0.25 → w=0, Brier>0.25 → w=0 (clipped).
  * `softmax_weights` — нормировка через softmax с разной температурой,
    fallback на равномерное при нулевых весах.
  * `compute_agent_stats` — конструкция AgentCalibrationStats:
      - empty inputs → нейтральные стат'ы;
      - length mismatch → ValueError;
      - корректный raw/shrunk Brier;
      - mean_predicted_p / mean_realized_y корректны.
  * `aggregate_agent_probabilities` — итоговая P(up) через softmax(weight):
      - cold-start (n<min_resolved у всех) → равные веса;
      - все weight=0 → fallback на равномерное;
      - calibrated weights → агенты с лучшей калибровкой получают больший вес.

Stdlib-only — гоняются и в unit-fast, и в unit-full.
"""

from __future__ import annotations

import math
import unittest

from core.agent_calibration import (
    COIN_FLIP_BRIER,
    DEFAULT_SHRINKAGE_PRIOR_STRENGTH,
    AgentCalibrationStats,
    aggregate_agent_probabilities,
    brier_to_weight,
    clip_probability,
    compute_agent_stats,
    shrink_brier,
    softmax_weights,
)


class ClipProbabilityTestCase(unittest.TestCase):
    def test_inside_range_unchanged(self) -> None:
        self.assertAlmostEqual(clip_probability(0.5), 0.5)
        self.assertAlmostEqual(clip_probability(0.001), 0.001, places=4)

    def test_zero_clipped_to_eps(self) -> None:
        # Не равно нулю, но очень близко.
        self.assertGreater(clip_probability(0.0), 0.0)
        self.assertLess(clip_probability(0.0), 1e-3)

    def test_one_clipped_to_one_minus_eps(self) -> None:
        self.assertLess(clip_probability(1.0), 1.0)
        self.assertGreater(clip_probability(1.0), 0.999)

    def test_out_of_range_clipped(self) -> None:
        # На всякий случай: даже p > 1.0 clip'ится.
        self.assertLess(clip_probability(1.5), 1.0)
        self.assertGreater(clip_probability(-0.5), 0.0)


class ShrinkBrierTestCase(unittest.TestCase):
    def test_zero_n_returns_prior(self) -> None:
        self.assertEqual(shrink_brier(0.0, n=0), COIN_FLIP_BRIER)
        self.assertEqual(shrink_brier(0.5, n=0), COIN_FLIP_BRIER)

    def test_large_n_returns_observed(self) -> None:
        # При n=10000 и k=5 — shrinkage пренебрежимый.
        observed = 0.05
        result = shrink_brier(observed, n=10_000)
        self.assertAlmostEqual(result, observed, places=3)

    def test_n_equals_prior_strength_gives_average(self) -> None:
        # При n=k=5 веса по 0.5/0.5.
        observed = 0.10
        prior_k = DEFAULT_SHRINKAGE_PRIOR_STRENGTH  # 5
        expected = 0.5 * observed + 0.5 * COIN_FLIP_BRIER  # 0.5*0.10 + 0.5*0.25 = 0.175
        result = shrink_brier(observed, n=prior_k)
        self.assertAlmostEqual(result, expected, places=6)

    def test_negative_prior_strength_raises(self) -> None:
        with self.assertRaises(ValueError):
            shrink_brier(0.1, n=5, prior_strength=-1)

    def test_monotonic_in_n(self) -> None:
        # При фиксированном observed < prior, чем больше n — тем меньше shrunk
        # (стремится к observed).
        observed = 0.05
        a = shrink_brier(observed, n=1)
        b = shrink_brier(observed, n=10)
        c = shrink_brier(observed, n=100)
        self.assertGreater(a, b)
        self.assertGreater(b, c)
        self.assertGreater(c, observed)


class BrierToWeightTestCase(unittest.TestCase):
    def test_brier_zero_gives_weight_one(self) -> None:
        self.assertAlmostEqual(brier_to_weight(0.0), 1.0)

    def test_brier_coin_flip_gives_weight_zero(self) -> None:
        self.assertAlmostEqual(brier_to_weight(COIN_FLIP_BRIER), 0.0)

    def test_brier_above_coin_flip_clipped_to_zero(self) -> None:
        # Хуже монетки → бесполезный агент.
        self.assertAlmostEqual(brier_to_weight(0.5), 0.0)
        self.assertAlmostEqual(brier_to_weight(1.0), 0.0)

    def test_intermediate(self) -> None:
        # Brier=0.10 → (0.25 - 0.10) / 0.25 = 0.6.
        self.assertAlmostEqual(brier_to_weight(0.10), 0.6, places=6)


class SoftmaxWeightsTestCase(unittest.TestCase):
    def test_uniform_input_returns_uniform_output(self) -> None:
        result = softmax_weights([0.3, 0.3, 0.3])
        self.assertEqual(len(result), 3)
        for w in result:
            self.assertAlmostEqual(w, 1.0 / 3, places=6)

    def test_sums_to_one(self) -> None:
        result = softmax_weights([0.1, 0.7, 0.4, 0.5])
        self.assertAlmostEqual(sum(result), 1.0, places=6)

    def test_all_zeros_falls_back_to_uniform(self) -> None:
        result = softmax_weights([0.0, 0.0, 0.0, 0.0])
        for w in result:
            self.assertAlmostEqual(w, 0.25, places=6)

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(softmax_weights([]), [])

    def test_zero_temperature_raises(self) -> None:
        with self.assertRaises(ValueError):
            softmax_weights([0.5, 0.5], temperature=0.0)

    def test_argmax_winner_dominates_low_temperature(self) -> None:
        # При очень малой температуре argmax почти 1.0.
        result = softmax_weights([0.0, 1.0, 0.0], temperature=0.01)
        self.assertGreater(result[1], 0.99)
        # Остальные близки к 0.
        self.assertLess(result[0], 0.01)
        self.assertLess(result[2], 0.01)


class ComputeAgentStatsTestCase(unittest.TestCase):
    def test_empty_inputs_neutral_stats(self) -> None:
        stats = compute_agent_stats("bull", [], [])
        self.assertEqual(stats.n_resolved, 0)
        self.assertEqual(stats.raw_brier, COIN_FLIP_BRIER)
        self.assertEqual(stats.shrunk_brier, COIN_FLIP_BRIER)
        self.assertEqual(stats.weight, 0.0)
        self.assertEqual(stats.mean_predicted_p, 0.5)
        self.assertEqual(stats.mean_realized_y, 0.5)

    def test_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_agent_stats("bull", [0.7, 0.8], [True])

    def test_perfect_predictor(self) -> None:
        # Brier=0 → weight=1 при больших N.
        ps = [1.0] * 100 + [0.0] * 100
        ys = [True] * 100 + [False] * 100
        stats = compute_agent_stats("bull", ps, ys)
        self.assertEqual(stats.n_resolved, 200)
        self.assertAlmostEqual(stats.raw_brier, 0.0, places=6)
        # Shrinkage с prior_strength=5 на n=200 даёт пренебрежимый эффект.
        self.assertLess(stats.shrunk_brier, 0.01)
        self.assertGreater(stats.weight, 0.95)

    def test_coin_flip_predictor(self) -> None:
        # Всегда p=0.5 на random outcomes → Brier=0.25 → weight=0.
        ps = [0.5] * 100
        ys = [True if i % 2 == 0 else False for i in range(100)]
        stats = compute_agent_stats("bear", ps, ys)
        self.assertAlmostEqual(stats.raw_brier, 0.25, places=6)
        self.assertAlmostEqual(stats.weight, 0.0, places=4)

    def test_means_correct(self) -> None:
        stats = compute_agent_stats(
            "synth",
            [0.7, 0.3, 0.5],
            [True, False, True],
        )
        self.assertAlmostEqual(stats.mean_predicted_p, 0.5, places=6)
        self.assertAlmostEqual(stats.mean_realized_y, 2.0 / 3, places=6)


class AggregateAgentProbabilitiesTestCase(unittest.TestCase):
    def test_empty_predictions_returns_half(self) -> None:
        p, weights = aggregate_agent_probabilities({}, {})
        self.assertAlmostEqual(p, 0.5)
        self.assertEqual(weights, {})

    def test_cold_start_uniform_weights(self) -> None:
        # Все агенты имеют <min_resolved предсказаний → равные веса.
        empty_stats = {
            "bull": AgentCalibrationStats(
                "bull", n_resolved=0, raw_brier=0.25,
                shrunk_brier=0.25, weight=0.0,
                mean_predicted_p=0.5, mean_realized_y=0.5,
            ),
            "bear": AgentCalibrationStats(
                "bear", n_resolved=0, raw_brier=0.25,
                shrunk_brier=0.25, weight=0.0,
                mean_predicted_p=0.5, mean_realized_y=0.5,
            ),
        }
        p, weights = aggregate_agent_probabilities(
            {"bull": 0.8, "bear": 0.2},
            empty_stats,
        )
        # Равные веса → среднее = 0.5
        self.assertAlmostEqual(p, 0.5, places=6)
        self.assertAlmostEqual(weights["bull"], 0.5, places=6)
        self.assertAlmostEqual(weights["bear"], 0.5, places=6)

    def test_calibrated_agent_dominates(self) -> None:
        # Bull откалиброван (weight=0.9), Bear — нет (weight=0.0).
        stats = {
            "bull": AgentCalibrationStats(
                "bull", n_resolved=50, raw_brier=0.025,
                shrunk_brier=0.025, weight=0.9,
                mean_predicted_p=0.6, mean_realized_y=0.6,
            ),
            "bear": AgentCalibrationStats(
                "bear", n_resolved=50, raw_brier=0.25,
                shrunk_brier=0.25, weight=0.0,
                mean_predicted_p=0.5, mean_realized_y=0.5,
            ),
        }
        p, weights = aggregate_agent_probabilities(
            {"bull": 0.8, "bear": 0.2},
            stats,
            softmax_temperature=0.3,  # больше «острая» аггрегация
        )
        # Bull должен иметь больший вес, p → ближе к 0.8.
        self.assertGreater(weights["bull"], weights["bear"])
        self.assertGreater(p, 0.5)

    def test_all_zero_weights_uniform_fallback(self) -> None:
        # Все агенты хуже coin-flip → все веса 0 → fallback на равные.
        zero_stats = {
            r: AgentCalibrationStats(
                r, n_resolved=50, raw_brier=0.30,
                shrunk_brier=0.30, weight=0.0,
                mean_predicted_p=0.5, mean_realized_y=0.5,
            ) for r in ("bull", "bear", "verifier")
        }
        p, weights = aggregate_agent_probabilities(
            {"bull": 0.7, "bear": 0.3, "verifier": 0.5},
            zero_stats,
        )
        # Равные веса → average = (0.7 + 0.3 + 0.5) / 3 = 0.5
        self.assertAlmostEqual(p, 0.5, places=6)
        for w in weights.values():
            self.assertAlmostEqual(w, 1.0 / 3, places=6)

    def test_clipped_probability(self) -> None:
        # Даже если weighted_p теоретически = 1.0, должно быть clipped.
        stats = {
            "bull": AgentCalibrationStats(
                "bull", n_resolved=100, raw_brier=0.001,
                shrunk_brier=0.001, weight=0.99,
                mean_predicted_p=0.5, mean_realized_y=0.5,
            ),
        }
        p, _ = aggregate_agent_probabilities({"bull": 1.0}, stats)
        self.assertLess(p, 1.0)
        self.assertGreater(p, 0.999)


if __name__ == "__main__":
    unittest.main()
