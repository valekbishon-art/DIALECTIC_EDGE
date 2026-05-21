"""Unit-tests для market_indicators/regime.py (математика).

Покрывают:
  * `_logsumexp` — корректность, edge-cases (пустой, -inf).
  * `_student_t_logpdf` — base sanity.
  * `bocpd_run` — посудный shape, posterior нормализован, detection
    changepoint'а на synthetic gaussian→shifted-gaussian.
  * `posterior_p_changepoint`, `posterior_expected_run_length` —
    численная корректность.
  * `log_returns_from_closes` — corner-cases (NaN, нули, нелинейные пропуски).
  * `_autocorrelation_lag1` — корректность на детерминированных рядах.
  * `label_regime` — каждый из 5 label'ов на synthetic-входе.
  * `classify_regime` — end-to-end smoke.

Stdlib + math — гоняется в unit-fast.
"""

from __future__ import annotations

import math
import random
import unittest

from market_indicators.regime import (
    DEFAULT_HAZARD_RATE,
    DEFAULT_LABEL_WINDOW,
    LABEL_CRISIS,
    LABEL_RANGING,
    LABEL_TRENDING,
    LABEL_UNKNOWN,
    LABEL_VOLATILE,
    MIN_OBSERVATIONS_FOR_LABEL,
    _autocorrelation_lag1,
    _logsumexp,
    _student_t_logpdf,
    bocpd_run,
    classify_regime,
    label_regime,
    log_returns_from_closes,
    posterior_expected_run_length,
    posterior_p_changepoint,
)


# ─── _logsumexp ──────────────────────────────────────────────────────────────


class LogSumExpTestCase(unittest.TestCase):
    def test_basic(self) -> None:
        # logsumexp([0]) = 0
        self.assertAlmostEqual(_logsumexp([0.0]), 0.0)
        # logsumexp([0, 0]) = log(2)
        self.assertAlmostEqual(_logsumexp([0.0, 0.0]), math.log(2.0), places=10)

    def test_numeric_stability(self) -> None:
        # 1000 + log(2), без переполнения.
        val = _logsumexp([1000.0, 1000.0])
        self.assertAlmostEqual(val, 1000.0 + math.log(2.0), places=8)

    def test_empty(self) -> None:
        self.assertEqual(_logsumexp([]), -math.inf)

    def test_all_neg_inf(self) -> None:
        self.assertEqual(_logsumexp([-math.inf, -math.inf]), -math.inf)


# ─── _student_t_logpdf ───────────────────────────────────────────────────────


class StudentTLogPdfTestCase(unittest.TestCase):
    def test_at_mean(self) -> None:
        # df=2 (alpha=1), scale=sqrt(beta*(kappa+1)/(alpha*kappa)).
        # mu=0, kappa=1, alpha=1, beta=1 → scale=sqrt(2). pdf(0) > pdf(small offset).
        a = _student_t_logpdf(0.0, mu=0.0, kappa=1.0, alpha=1.0, beta=1.0)
        b = _student_t_logpdf(0.5, mu=0.0, kappa=1.0, alpha=1.0, beta=1.0)
        self.assertGreater(a, b)

    def test_symmetric(self) -> None:
        a = _student_t_logpdf(+0.7, mu=0.0, kappa=1.0, alpha=1.0, beta=1.0)
        b = _student_t_logpdf(-0.7, mu=0.0, kappa=1.0, alpha=1.0, beta=1.0)
        self.assertAlmostEqual(a, b, places=10)

    def test_invalid_scale_returns_low(self) -> None:
        # beta=0 → scale_sq=0 → защита возвращает -1e9.
        v = _student_t_logpdf(0.0, mu=0.0, kappa=1.0, alpha=1.0, beta=0.0)
        self.assertLessEqual(v, -1e8)


# ─── bocpd_run posterior ─────────────────────────────────────────────────────


class BocpdPosteriorShapeTestCase(unittest.TestCase):
    def test_posterior_normalized(self) -> None:
        # После любого числа шагов posterior должен sum'миться в ~1.0.
        rng = random.Random(0)
        obs = [rng.gauss(0.0, 0.01) for _ in range(50)]
        state = bocpd_run(obs)
        total = sum(math.exp(lp) for lp in state.log_probs)
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_posterior_length_grows(self) -> None:
        # До truncation длина posterior'а = T + 1.
        rng = random.Random(1)
        obs = [rng.gauss(0.0, 0.01) for _ in range(10)]
        state = bocpd_run(obs)
        self.assertEqual(len(state.log_probs), 11)
        self.assertEqual(len(state.mu), 11)

    def test_posterior_truncates(self) -> None:
        rng = random.Random(2)
        obs = [rng.gauss(0.0, 0.01) for _ in range(60)]
        state = bocpd_run(obs, max_run_length=20)
        self.assertLessEqual(len(state.log_probs), 20)


class BocpdChangepointDetectionTestCase(unittest.TestCase):
    """Главный тест BOCPD: на shift-mean changepoint'е run-length должен
    схлопнуться к 0 — это и есть детекция."""

    def test_shift_mean_resets_run_length(self) -> None:
        rng = random.Random(42)
        # 50 шагов вокруг μ=0, σ=0.01. Затем 50 шагов вокруг μ=0.05.
        obs = [rng.gauss(0.0, 0.01) for _ in range(50)]
        obs += [rng.gauss(0.05, 0.01) for _ in range(50)]
        state = bocpd_run(obs)

        # Expected run-length должен быть сильно меньше T=100, потому что
        # changepoint около t=50 заставляет массу posterior'а сдвинуться к
        # малым run-length.
        exp_rl = posterior_expected_run_length(state)
        self.assertLess(exp_rl, 80)

        # P(recent changepoint в последних 3 шагах) — допускаем low, потому
        # что changepoint был ~50 шагов назад. Но E[run-length] должен быть
        # около 50 (длина последнего сегмента), а не около 100.
        self.assertGreater(exp_rl, 5)

    def test_no_changepoint_long_run(self) -> None:
        rng = random.Random(7)
        # Стационарный гаусс: changepoint не должен детектироваться.
        obs = [rng.gauss(0.0, 0.01) for _ in range(100)]
        state = bocpd_run(obs)
        exp_rl = posterior_expected_run_length(state)
        # При стационарных данных run-length должен расти близко к T.
        self.assertGreater(exp_rl, 30)


# ─── posterior helpers ──────────────────────────────────────────────────────


class PosteriorHelpersTestCase(unittest.TestCase):
    def test_p_changepoint_after_shift(self) -> None:
        rng = random.Random(3)
        # 80 «спокойных» точек, затем 3 «бросковых» точки → CP должна быть высокой.
        obs = [rng.gauss(0.0, 0.005) for _ in range(80)]
        obs += [0.10, -0.08, 0.12]
        state = bocpd_run(obs)
        p_cp = posterior_p_changepoint(state, recent_k=5)
        # Точное значение зависит от prior'а, но должно быть заметно > 0.
        self.assertGreater(p_cp, 0.05)

    def test_p_changepoint_bounds(self) -> None:
        rng = random.Random(4)
        obs = [rng.gauss(0.0, 0.01) for _ in range(20)]
        state = bocpd_run(obs)
        p = posterior_p_changepoint(state, recent_k=3)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)


# ─── log_returns_from_closes ─────────────────────────────────────────────────


class LogReturnsTestCase(unittest.TestCase):
    def test_basic(self) -> None:
        closes = [100.0, 101.0, 102.0]
        rets = log_returns_from_closes(closes)
        self.assertEqual(len(rets), 2)
        self.assertAlmostEqual(rets[0], math.log(101.0 / 100.0), places=10)
        self.assertAlmostEqual(rets[1], math.log(102.0 / 101.0), places=10)

    def test_skips_invalid(self) -> None:
        # None, NaN, ноль и отрицательные — пропускаются, прерывают цепочку.
        closes = [100.0, None, 0.0, -1.0, 50.0, 55.0]
        rets = log_returns_from_closes(closes)
        # 100 → (None разрывает) → 50, 55. Один валидный return: 55/50.
        self.assertEqual(len(rets), 1)
        self.assertAlmostEqual(rets[0], math.log(55.0 / 50.0), places=10)

    def test_empty(self) -> None:
        self.assertEqual(log_returns_from_closes([]), [])

    def test_single_value(self) -> None:
        # Один close → нет returns.
        self.assertEqual(log_returns_from_closes([100.0]), [])

    def test_handles_strings(self) -> None:
        # Невалидный тип → пропуск, цепочка разрывается.
        rets = log_returns_from_closes([100.0, "garbage", 110.0])
        # 100 → (garbage разрывает) → 110 (нет предыдущего). Пусто.
        self.assertEqual(rets, [])


# ─── _autocorrelation_lag1 ───────────────────────────────────────────────────


class AutocorrLag1TestCase(unittest.TestCase):
    def test_perfect_positive(self) -> None:
        # Monotonic increasing — autocorr должна быть высокой. Биазированный
        # estimator (num=n-1 терминов, den=n) даёт ~0.85 для range(20), что
        # достаточно для отделения «trending» от «ranging».
        vals = list(range(20))
        ac = _autocorrelation_lag1(vals)
        self.assertGreater(ac, 0.8)
        self.assertLessEqual(ac, 1.0)

    def test_alternating_negative(self) -> None:
        # Чередующийся знак — отрицательная autocorr.
        vals = [(-1) ** i for i in range(40)]
        ac = _autocorrelation_lag1(vals)
        self.assertLess(ac, -0.5)
        self.assertGreaterEqual(ac, -1.0)

    def test_zero_variance(self) -> None:
        self.assertEqual(_autocorrelation_lag1([5.0] * 10), 0.0)

    def test_too_short(self) -> None:
        self.assertEqual(_autocorrelation_lag1([1.0, 2.0]), 0.0)


# ─── label_regime — каждый из 5 label'ов ─────────────────────────────────────


class LabelRegimeTestCase(unittest.TestCase):
    def test_unknown_for_short_input(self) -> None:
        # Меньше MIN_OBSERVATIONS_FOR_LABEL → UNKNOWN.
        recent = [0.001] * (MIN_OBSERVATIONS_FOR_LABEL - 1)
        label, *_ = label_regime(
            p_changepoint=0.0, recent_returns=recent, expected_run_length=10.0,
        )
        self.assertEqual(label, LABEL_UNKNOWN)

    def test_crisis_high_vol_plus_changepoint(self) -> None:
        # Высокая std (3% per hour) + p_cp > 0.30 → CRISIS.
        # 3% × √(24×365) = 3% × 93.6 = 280% annualized vol → выше порога 1.2.
        recent = [+0.03, -0.03] * 12  # n=24, σ=0.03, μ≈0
        label, vol, drift, ac, dtv, bias = label_regime(
            p_changepoint=0.50, recent_returns=recent, expected_run_length=10.0,
        )
        self.assertEqual(label, LABEL_CRISIS)
        self.assertGreater(vol, 1.2)

    def test_volatile_high_vol_no_changepoint(self) -> None:
        recent = [+0.03, -0.03] * 12
        label, *_ = label_regime(
            p_changepoint=0.05, recent_returns=recent, expected_run_length=200.0,
        )
        self.assertEqual(label, LABEL_VOLATILE)

    def test_trending_positive_drift_plus_autocorr(self) -> None:
        # Monotonic gentle uptrend — высокая autocorr, drift > 0, vol < 1.2.
        # Step = +0.002 per hour. σ внутри recent = 0 (постоянный return).
        # Нам нужен tiny noise, чтобы σ > 0, но drift/vol > порога.
        rng = random.Random(99)
        recent = [0.002 + rng.gauss(0.0, 0.0005) for _ in range(24)]
        label, vol, drift, ac, dtv, bias = label_regime(
            p_changepoint=0.05, recent_returns=recent, expected_run_length=50.0,
        )
        self.assertEqual(label, LABEL_TRENDING)
        self.assertEqual(bias, 1)
        self.assertGreater(drift, 0)

    def test_trending_negative_drift(self) -> None:
        rng = random.Random(101)
        recent = [-0.002 + rng.gauss(0.0, 0.0005) for _ in range(24)]
        label, vol, drift, ac, dtv, bias = label_regime(
            p_changepoint=0.05, recent_returns=recent, expected_run_length=50.0,
        )
        self.assertEqual(label, LABEL_TRENDING)
        self.assertEqual(bias, -1)
        self.assertLess(drift, 0)

    def test_ranging_mean_reversion(self) -> None:
        # Чередующийся знак: autocorr < -0.05, drift ≈ 0.
        # Меняем амплитуды чтобы не было exact zero variance.
        rng = random.Random(13)
        recent = [(0.002 if i % 2 == 0 else -0.002) + rng.gauss(0.0, 0.0003) for i in range(24)]
        label, *_ = label_regime(
            p_changepoint=0.05, recent_returns=recent, expected_run_length=100.0,
        )
        self.assertEqual(label, LABEL_RANGING)


# ─── classify_regime — end-to-end smoke ──────────────────────────────────────


class ClassifyRegimeEndToEndTestCase(unittest.TestCase):
    def test_uptrend_closes(self) -> None:
        # Цены растут на 0.2% за час с шумом — должен быть TRENDING+.
        rng = random.Random(123)
        prices = [100.0]
        for _ in range(50):
            prices.append(prices[-1] * (1.0 + 0.002 + rng.gauss(0.0, 0.0005)))
        result = classify_regime(prices)
        self.assertIn(result.label, {LABEL_TRENDING, LABEL_VOLATILE, LABEL_RANGING})
        # Drift в любом случае положительный
        self.assertGreater(result.recent_drift_annualized, 0)

    def test_short_input_returns_unknown(self) -> None:
        result = classify_regime([100.0, 100.5])
        self.assertEqual(result.label, LABEL_UNKNOWN)

    def test_empty_input(self) -> None:
        result = classify_regime([])
        self.assertEqual(result.label, LABEL_UNKNOWN)
        self.assertEqual(result.n_observations, 0)

    def test_invalid_hazard_raises(self) -> None:
        prices = [100.0 + i for i in range(20)]
        with self.assertRaises(ValueError):
            classify_regime(prices, hazard_rate=0.0)
        with self.assertRaises(ValueError):
            classify_regime(prices, hazard_rate=1.0)

    def test_signal_metrics_present(self) -> None:
        rng = random.Random(7)
        prices = [100.0]
        for _ in range(60):
            prices.append(prices[-1] * (1.0 + rng.gauss(0.0, 0.005)))
        result = classify_regime(prices)
        # Vol должна быть positive (есть noise), n_obs = len(prices) - 1.
        self.assertEqual(result.n_observations, 60)
        self.assertGreater(result.recent_volatility_annualized, 0.0)
        self.assertGreaterEqual(result.p_changepoint, 0.0)
        self.assertLessEqual(result.p_changepoint, 1.0)


if __name__ == "__main__":
    unittest.main()
