"""Regime classifier — Bayesian Online Changepoint Detection + market label.

Зачем:
  Traders profitable в trending-режиме теряют деньги в range-режиме,
  и наоборот. Большинство retail-ботов не понимают, в каком режиме сейчас
  рынок — они применяют один и тот же signal-stack везде. У нас фикс:
  на каждом /daily пересчитываем регулярный режим (trending / ranging /
  volatile / crisis), и в дебаты прокидываем contextual penalty/bonus
  под текущий режим. Это leading-meta-signal, отдельный от funding/OI/MVRV.

Что считаем (всё на ряду log-returns BTC 1h):
  1. **BOCPD posterior over run-lengths** — Adams & MacKay 2007 online
     algorithm. Observation model — Normal с conjugate Normal-Inverse-Gamma
     prior. Hazard — constant (geometric prior на длину сегмента).
  2. **p_changepoint** — суммарная масса posterior'а в run_length ≤ 2
     (rolled-over недавний changepoint).
  3. **expected_run_length** — posterior mean. Высокий = долгий стабильный
     сегмент.
  4. **Recent volatility** — std последних N точек (для labeling).
  5. **Recent drift** — mean последних N точек (signed).
  6. **Autocorrelation lag-1** — для отличия trending vs ranging
     (high abs autocorr → есть momentum / тренд; ~0 → random walk / range).
  7. **Regime label** — производное от (1)-(6). Финальный enum для дебатов.

Что НЕ делает (намеренно):
  * Не использует numpy / scipy / pandas. Stdlib only (math + statistics)
    — чтобы гонять в unit-fast CI job и не тащить новые deps (см. AGENTS.md
    «Не вводи новые зависимости в requirements.txt без обсуждения»).
  * Не лезет в torgovuyu логику (signal_trader.py / signals.py /
    core/dynamic_risk.py). Только output для дебатов и opcionalniy
    score-contribution.
  * Не подбирает hyperparameters online — hazard / prior strength фиксированные.

Внешние зависимости: только stdlib.

Reference:
  Adams, R. P., & MacKay, D. J. C. (2007). Bayesian Online Changepoint
  Detection. arXiv:0710.3742.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Sequence

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Hazard rate — вероятность changepoint на каждом шаге (constant hazard).
#: 1/200 = ожидаемая длина сегмента 200 шагов (≈ 8 дней для 1h-bars). Это
#: достаточно консервативно: changepoint'ы детектируются только когда
#: данные действительно сильно отклоняются от текущего сегмента.
DEFAULT_HAZARD_RATE = 1.0 / 200.0

#: Длина окна для labeling (последние N точек). 24h — естественный crypto-cycle.
DEFAULT_LABEL_WINDOW = 24

#: Порог volatility (annualized std для 1h returns), выше которого режим
#: считается «volatile». 1h-std × √(24×365) ≈ annualized. 1.5 = 150% annualized.
#: Для BTC исторически 60-90% — это календарь, 120%+ — стрессовый период.
DEFAULT_VOL_HIGH_ANNUALIZED = 1.2

#: Порог |drift| / vol (Sharpe-like). Высокий ratio → значимый тренд.
DEFAULT_DRIFT_RATIO_THRESHOLD = 0.15

#: Порог autocorrelation lag-1. Положительный (>0.05) — momentum; около нуля —
#: random walk; отрицательный (<-0.05) — mean-reversion (range).
DEFAULT_AUTOCORR_TRENDING = 0.05
DEFAULT_AUTOCORR_RANGING = -0.05

#: Порог p_changepoint, при превышении которого помечаем «recent changepoint».
DEFAULT_P_CHANGEPOINT_THRESHOLD = 0.30

#: Prior для Normal-Inverse-Gamma observation model.
#:   μ0 — prior mean (0.0 для log-returns: «по дефолту нет drift'а»);
#:   κ0 — strength of prior on mean (1.0 = «один эквивалентный observation»);
#:   α0 — shape для variance (1.0 = слабый prior);
#:   β0 — scale для variance (0.0001 = ожидаем small returns).
DEFAULT_PRIOR_MU = 0.0
DEFAULT_PRIOR_KAPPA = 1.0
DEFAULT_PRIOR_ALPHA = 1.0
DEFAULT_PRIOR_BETA = 1e-4

#: Минимальное число observations для стабильного labeling. Ниже — режим
#: «unknown», избегаем шумных выводов.
MIN_OBSERVATIONS_FOR_LABEL = 12

#: Максимальная длина posterior'а (run-length truncation). После N шагов
#: маленькие хвосты отсекаются — иначе arrays растут линейно по T.
MAX_RUN_LENGTH = 500

#: Расшифровка label'ов — стабильный enum для consumer'ов.
LABEL_TRENDING = "trending"
LABEL_RANGING = "ranging"
LABEL_VOLATILE = "volatile"
LABEL_CRISIS = "crisis"
LABEL_UNKNOWN = "unknown"

ALL_LABELS = (LABEL_TRENDING, LABEL_RANGING, LABEL_VOLATILE, LABEL_CRISIS, LABEL_UNKNOWN)


# ─── Output dataclass ────────────────────────────────────────────────────────


@dataclass
class RegimeClassification:
    """Финальный результат classifier'а на конкретный момент времени."""

    # Сам label — главный output для дебатов.
    label: str = LABEL_UNKNOWN

    # Сырьё для labeling (даём дебатёрам видеть детали).
    p_changepoint: float = 0.0            # P(recent changepoint), [0, 1]
    expected_run_length: float = 0.0      # posterior mean run-length, в шагах
    recent_volatility_annualized: float = 0.0  # σ × √(24×365), доля
    recent_drift_annualized: float = 0.0       # μ × 24×365, доля (signed)
    recent_autocorr_lag1: float = 0.0     # ∈ [-1, +1]
    drift_to_vol_ratio: float = 0.0       # signed, |·| ≈ Sharpe-like

    # Сколько шагов в input'е использовано (для дебагa и доверия).
    n_observations: int = 0

    # Direction bias из drift'а (+1 / 0 / -1) — удобно для scorer.
    direction_bias: int = 0


@dataclass
class _BocpdState:
    """Внутреннее состояние BOCPD (для unit-тестов и сериализации)."""

    # Posterior log-probs над run-length. Index = run-length, value = log P.
    log_probs: list[float] = field(default_factory=list)
    # Suficient stats per run-length: для Normal-Inverse-Gamma.
    #   mu, kappa, alpha, beta (как у Murphy 2007 «Conjugate Bayesian analysis
    #   of the Gaussian distribution»).
    mu: list[float] = field(default_factory=list)
    kappa: list[float] = field(default_factory=list)
    alpha: list[float] = field(default_factory=list)
    beta: list[float] = field(default_factory=list)


# ─── BOCPD core ──────────────────────────────────────────────────────────────


def _student_t_logpdf(
    x: float, *, mu: float, kappa: float, alpha: float, beta: float,
) -> float:
    """Predictive log-pdf для Normal с conjugate Normal-Inverse-Gamma prior.

    После маргинализации по (μ, σ²) predictive — Student-t с:
        df    = 2α
        loc   = μ
        scale = sqrt(β (κ+1) / (α κ))

    Murphy, K. P. (2007), eq. (110).
    """
    df = 2.0 * alpha
    scale_sq = beta * (kappa + 1.0) / (alpha * kappa)
    if scale_sq <= 0.0 or df <= 0.0:
        # Численная защита — крайне маловероятно при валидном prior'е, но
        # лучше вернуть очень низкий log-prob чем NaN.
        return -1e9
    scale = math.sqrt(scale_sq)
    z = (x - mu) / scale
    # log Student-t pdf:
    #   log Γ((df+1)/2) - log Γ(df/2) - 0.5 log(df π) - log scale
    #     - ((df+1)/2) log(1 + z²/df)
    log_norm = (
        math.lgamma(0.5 * (df + 1.0))
        - math.lgamma(0.5 * df)
        - 0.5 * math.log(df * math.pi)
        - math.log(scale)
    )
    log_kernel = -0.5 * (df + 1.0) * math.log1p(z * z / df)
    return log_norm + log_kernel


def _logsumexp(values: Sequence[float]) -> float:
    """Numerically-stable log(Σ exp(v_i)) без numpy."""
    if not values:
        return -math.inf
    m = max(values)
    if m == -math.inf:
        return -math.inf
    total = 0.0
    for v in values:
        total += math.exp(v - m)
    return m + math.log(total)


def bocpd_run(
    observations: Sequence[float],
    *,
    hazard_rate: float = DEFAULT_HAZARD_RATE,
    prior_mu: float = DEFAULT_PRIOR_MU,
    prior_kappa: float = DEFAULT_PRIOR_KAPPA,
    prior_alpha: float = DEFAULT_PRIOR_ALPHA,
    prior_beta: float = DEFAULT_PRIOR_BETA,
    max_run_length: int = MAX_RUN_LENGTH,
) -> _BocpdState:
    """Пройти BOCPD по всей последовательности и вернуть итоговое state.

    Реализация полностью из stdlib, O(T × R) по времени где R — текущая длина
    posterior'а (truncated на max_run_length).
    """
    if hazard_rate <= 0.0 or hazard_rate >= 1.0:
        raise ValueError(f"hazard_rate must be in (0, 1), got {hazard_rate}")
    if prior_kappa <= 0 or prior_alpha <= 0 or prior_beta <= 0:
        raise ValueError("prior_kappa/alpha/beta must be > 0")

    log_h = math.log(hazard_rate)
    log_1mh = math.log1p(-hazard_rate)

    state = _BocpdState(
        log_probs=[0.0],            # log P(r_0=0) = 0 (масса целиком на reset)
        mu=[prior_mu],
        kappa=[prior_kappa],
        alpha=[prior_alpha],
        beta=[prior_beta],
    )

    for x in observations:
        # Predictive log-prob для каждого текущего run-length r.
        log_pred = [
            _student_t_logpdf(
                x,
                mu=state.mu[r],
                kappa=state.kappa[r],
                alpha=state.alpha[r],
                beta=state.beta[r],
            )
            for r in range(len(state.log_probs))
        ]

        # Growth probabilities: r_t = r_{t-1} + 1.
        growth = [
            state.log_probs[r] + log_pred[r] + log_1mh
            for r in range(len(state.log_probs))
        ]

        # Changepoint mass: r_t = 0.
        cp = _logsumexp([
            state.log_probs[r] + log_pred[r] + log_h
            for r in range(len(state.log_probs))
        ])

        # New posterior: [cp, *growth].
        new_log_probs = [cp] + growth

        # Update sufficient stats (Murphy 2007 eq. (86)-(89)):
        #   posterior после добавления x при run-length r:
        #     μ' = (κ μ + x) / (κ + 1)
        #     κ' = κ + 1
        #     α' = α + 0.5
        #     β' = β + (κ (x - μ)²) / (2 (κ + 1))
        new_mu = [prior_mu]
        new_kappa = [prior_kappa]
        new_alpha = [prior_alpha]
        new_beta = [prior_beta]
        for r in range(len(state.mu)):
            mu_r = state.mu[r]
            kappa_r = state.kappa[r]
            alpha_r = state.alpha[r]
            beta_r = state.beta[r]
            kappa_new = kappa_r + 1.0
            mu_new = (kappa_r * mu_r + x) / kappa_new
            alpha_new = alpha_r + 0.5
            delta = x - mu_r
            beta_new = beta_r + (kappa_r * delta * delta) / (2.0 * kappa_new)
            new_mu.append(mu_new)
            new_kappa.append(kappa_new)
            new_alpha.append(alpha_new)
            new_beta.append(beta_new)

        # Truncate posterior — отбрасываем хвосты с малой массой.
        if len(new_log_probs) > max_run_length:
            new_log_probs = new_log_probs[: max_run_length]
            new_mu = new_mu[: max_run_length]
            new_kappa = new_kappa[: max_run_length]
            new_alpha = new_alpha[: max_run_length]
            new_beta = new_beta[: max_run_length]

        # Renormalize log_probs.
        norm = _logsumexp(new_log_probs)
        if norm == -math.inf:
            # Дегенеративное состояние (численная катастрофа). Reset.
            logger.warning("BOCPD posterior collapsed to -inf, resetting")
            new_log_probs = [0.0]
            new_mu = [prior_mu]
            new_kappa = [prior_kappa]
            new_alpha = [prior_alpha]
            new_beta = [prior_beta]
        else:
            new_log_probs = [lp - norm for lp in new_log_probs]

        state.log_probs = new_log_probs
        state.mu = new_mu
        state.kappa = new_kappa
        state.alpha = new_alpha
        state.beta = new_beta

    return state


def posterior_p_changepoint(state: _BocpdState, *, recent_k: int = 3) -> float:
    """Суммарная вероятность что changepoint был в последние `recent_k` шагов."""
    if not state.log_probs:
        return 0.0
    k = min(recent_k, len(state.log_probs))
    return sum(math.exp(lp) for lp in state.log_probs[:k])


def posterior_expected_run_length(state: _BocpdState) -> float:
    """E[run_length | x_1:t] = Σ r · P(r_t=r)."""
    if not state.log_probs:
        return 0.0
    return sum(r * math.exp(lp) for r, lp in enumerate(state.log_probs))


# ─── Pure-stdlib helpers для labeling ────────────────────────────────────────


def log_returns_from_closes(closes: Sequence[float]) -> list[float]:
    """Log-returns: r_t = log(C_t / C_{t-1}). Невалидные точки (≤0) отброшены."""
    out: list[float] = []
    prev: float | None = None
    for c in closes:
        try:
            v = float(c)
        except (TypeError, ValueError):
            prev = None
            continue
        if v <= 0.0 or not math.isfinite(v):
            prev = None
            continue
        if prev is not None:
            out.append(math.log(v / prev))
        prev = v
    return out


def _autocorrelation_lag1(values: Sequence[float]) -> float:
    """Pearson autocorr на лаге 1. Возвращает 0 если недостаточно точек или σ=0."""
    n = len(values)
    if n < 3:
        return 0.0
    m = mean(values)
    num = 0.0
    den = 0.0
    for i in range(n):
        d = values[i] - m
        den += d * d
        if i + 1 < n:
            num += d * (values[i + 1] - m)
    if den <= 0.0:
        return 0.0
    ac = num / den
    # Защита от выбросов численки.
    if ac > 1.0:
        return 1.0
    if ac < -1.0:
        return -1.0
    return ac


def _annualization_factor_hours() -> float:
    """sqrt(24×365) для перевода 1h-std в annualized std."""
    return math.sqrt(24.0 * 365.0)


def _safe_stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    try:
        return pstdev(values)
    except Exception:  # noqa: BLE001
        return 0.0


def label_regime(
    *,
    p_changepoint: float,
    recent_returns: Sequence[float],
    expected_run_length: float,
    vol_high_annualized: float = DEFAULT_VOL_HIGH_ANNUALIZED,
    drift_ratio_threshold: float = DEFAULT_DRIFT_RATIO_THRESHOLD,
    autocorr_trending: float = DEFAULT_AUTOCORR_TRENDING,
    autocorr_ranging: float = DEFAULT_AUTOCORR_RANGING,
    p_changepoint_threshold: float = DEFAULT_P_CHANGEPOINT_THRESHOLD,
) -> tuple[str, float, float, float, float, int]:
    """Из недавних returns + p_changepoint собирает label + supporting metrics.

    Returns: (label, vol_annualized, drift_annualized, autocorr, drift_to_vol, dir_bias)
    """
    n = len(recent_returns)
    if n < MIN_OBSERVATIONS_FOR_LABEL:
        return (LABEL_UNKNOWN, 0.0, 0.0, 0.0, 0.0, 0)

    sigma_1h = _safe_stdev(recent_returns)
    mu_1h = mean(recent_returns)
    ac1 = _autocorrelation_lag1(recent_returns)

    vol_ann = sigma_1h * _annualization_factor_hours()
    drift_ann = mu_1h * 24.0 * 365.0
    drift_to_vol = (mu_1h / sigma_1h) if sigma_1h > 0 else 0.0
    dir_bias = 1 if mu_1h > 0 else (-1 if mu_1h < 0 else 0)

    # Crisis: недавний changepoint + высокая волатильность одновременно.
    if (
        p_changepoint >= p_changepoint_threshold
        and vol_ann >= vol_high_annualized
    ):
        return (LABEL_CRISIS, vol_ann, drift_ann, ac1, drift_to_vol, dir_bias)

    # Volatile: только высокая волатильность (без явного changepoint).
    if vol_ann >= vol_high_annualized:
        return (LABEL_VOLATILE, vol_ann, drift_ann, ac1, drift_to_vol, dir_bias)

    # Trending: значимый signed drift_to_vol (Sharpe-like порог). Autocorr НЕ
    # требуется — strong drift сам по себе — это trend (даже если рассчитан
    # на серии iid-returns с μ≠0; ac1 у такой серии ~0, но это всё равно тренд).
    is_strong_drift = abs(drift_to_vol) >= drift_ratio_threshold
    if is_strong_drift:
        return (LABEL_TRENDING, vol_ann, drift_ann, ac1, drift_to_vol, dir_bias)

    # Ranging: явный mean-reversion (autocorr отрицательный) ИЛИ слабый drift +
    # стабильный сегмент (длинный expected_run_length).
    if ac1 <= autocorr_ranging or expected_run_length >= DEFAULT_LABEL_WINDOW:
        return (LABEL_RANGING, vol_ann, drift_ann, ac1, drift_to_vol, dir_bias)

    # Fallback: достаточно данных, но ни один порог не выполнен — ranging
    # по дефолту (рынок шатается без явной структуры).
    return (LABEL_RANGING, vol_ann, drift_ann, ac1, drift_to_vol, dir_bias)


def classify_regime(
    closes: Sequence[float],
    *,
    hazard_rate: float = DEFAULT_HAZARD_RATE,
    label_window: int = DEFAULT_LABEL_WINDOW,
    vol_high_annualized: float = DEFAULT_VOL_HIGH_ANNUALIZED,
    drift_ratio_threshold: float = DEFAULT_DRIFT_RATIO_THRESHOLD,
    p_changepoint_threshold: float = DEFAULT_P_CHANGEPOINT_THRESHOLD,
) -> RegimeClassification:
    """End-to-end: closes → log-returns → BOCPD → label.

    Главный публичный entry point. Принимает 1h closes (или любой timestep —
    только vol/drift annualization предполагает hourly bars), отдаёт
    `RegimeClassification`.
    """
    returns = log_returns_from_closes(closes)
    if len(returns) < MIN_OBSERVATIONS_FOR_LABEL:
        return RegimeClassification(
            label=LABEL_UNKNOWN,
            n_observations=len(returns),
        )

    state = bocpd_run(returns, hazard_rate=hazard_rate)
    p_cp = posterior_p_changepoint(state, recent_k=3)
    exp_rl = posterior_expected_run_length(state)

    recent = returns[-label_window:] if len(returns) >= label_window else returns
    label, vol_ann, drift_ann, ac1, dtv, dir_bias = label_regime(
        p_changepoint=p_cp,
        recent_returns=recent,
        expected_run_length=exp_rl,
        vol_high_annualized=vol_high_annualized,
        drift_ratio_threshold=drift_ratio_threshold,
        p_changepoint_threshold=p_changepoint_threshold,
    )

    return RegimeClassification(
        label=label,
        p_changepoint=p_cp,
        expected_run_length=exp_rl,
        recent_volatility_annualized=vol_ann,
        recent_drift_annualized=drift_ann,
        recent_autocorr_lag1=ac1,
        drift_to_vol_ratio=dtv,
        n_observations=len(returns),
        direction_bias=dir_bias,
    )
