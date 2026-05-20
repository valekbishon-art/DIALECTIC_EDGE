"""Per-agent calibration — отвечает на вопрос «кто из Bull/Bear/Verifier/Synth
реально прав, а кто эффектно говорит».

Зачем:
  В `agents.py` дебаты выдают качественные аргументы, но **каждый агент НЕ
  оценивается отдельно**. Существующий `core/calibration.py` калибрует *решение
  на уровне сигнала* (per-signal), а не *каждого участника дебата* — мы не
  знаем, кто из агентов over-confident, кто under-confident, кто чаще прав на
  каком регуляторе рынка.

  Этот модуль добавляет **per-agent layer**:
    1. После каждого дебата каждый агент выдаёт **probabilistic forecast**
       (P(asset up >= threshold за horizon)).
    2. Через `horizon` часов прогноз scoring'уется через Brier score
       (reused из `core/recalibration.brier_score` — DRY).
    3. Накопив N прогнозов per-agent, считаем **calibration weight** через
       Bayesian shrinkage к prior=0.5 (равный вес). При N=20+ shrinkage
       минимален, при N<5 вес агента возвращается почти к 0.5.

Что НЕ делает (намеренно):
  * Не подменяет `core/calibration.py` (тот считает per-signal hit-rate).
  * Не интегрируется в torговую логику `signal_trader.py` (per AGENTS.md).
  * Не использует numpy/scipy/sklearn (только stdlib + опционально math).
  * Не делает isotonic recalibration агентов (это потенциальный PR-N+1).

Математика (что и почему):
  * **Brier score** (Brier 1950): mean squared error для probabilistic
    предсказаний. Brier = E[(p - y)^2], где p ∈ [0,1] — предсказанная
    вероятность, y ∈ {0, 1} — реализация. Brier=0 perfect, Brier=0.25 coin
    flip, Brier=1 worst-case. Properscoring rule — нельзя сжульничать
    систематическим bias'ом.
  * **Bayesian shrinkage**: при малом N raw Brier шумит. Сжимаем оценку
    к prior=0.25 (Brier coin-flip baseline) через
        Brier_post = (n / (n + k)) * Brier_obs + (k / (n + k)) * 0.25
    где k — strength prior (по умолчанию 5).
    При n=0 → Brier_post = 0.25 (агент считается «случайным»).
    При n>>k → Brier_post → Brier_obs (доверяем эмпирике).
  * **Weight aggregation**: вес = (0.25 - Brier_post) / 0.25, clip [0, 1].
    Это даёт w=1 если Brier=0 (perfect), w=0 если Brier ≥ 0.25 (хуже coin
    flip). Synth aggregation использует softmax по этим весам.

Внешние зависимости: только stdlib + `core.recalibration.brier_score`
(который сам stdlib-only).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Sequence

from core.recalibration import brier_score

logger = logging.getLogger(__name__)


# ─── Константы математики ────────────────────────────────────────────────────

#: Brier score случайного прогнозирования (всегда p=0.5). 0.5^2 = 0.25.
COIN_FLIP_BRIER = 0.25

#: Strength of the prior для Bayesian shrinkage. k=5 означает «pseudo-counts»
#: 5 наблюдений с Brier=COIN_FLIP_BRIER. При реальном n<5 веса агентов сильно
#: shrink'нуты к нейтральному. При n>20 — почти не влияет.
DEFAULT_SHRINKAGE_PRIOR_STRENGTH = 5

#: Минимальное количество resolved прогнозов агента, ниже которого вес
#: принудительно = 1/N_agents (равномерный). Защищает от cold-start bias.
DEFAULT_MIN_RESOLVED = 3


# ─── Базовые math primitives ─────────────────────────────────────────────────


def clip_probability(p: float, eps: float = 1e-6) -> float:
    """Зажимаем p в [eps, 1-eps] — Brier стабилен и без этого, но при log-loss
    extension'ах нужно избегать log(0). Сохраняем здесь для единообразия."""
    return max(eps, min(1.0 - eps, float(p)))


def shrink_brier(
    observed_brier: float,
    n: int,
    prior_strength: int = DEFAULT_SHRINKAGE_PRIOR_STRENGTH,
    prior_brier: float = COIN_FLIP_BRIER,
) -> float:
    """Bayesian shrinkage Brier-оценки к prior'у.

    Формула (conjugate-style для squared loss):
        Brier_post = (n / (n + k)) * Brier_obs + (k / (n + k)) * Brier_prior

    При n=0 возвращает prior (фактически COIN_FLIP_BRIER). При больших n →
    observed_brier. Защищает от «случайно угадал 3 из 3 → Brier=0 → вес 1.0».

    Args:
        observed_brier: empirical mean Brier по resolved прогнозам.
        n: количество resolved прогнозов.
        prior_strength: «вес» prior'а в pseudo-counts. Чем выше — тем дольше
            сжимаемся к prior'у.
        prior_brier: Brier prior'а (по умолчанию coin-flip = 0.25).

    Returns:
        Shrunk Brier score в [0, 1].
    """
    if n <= 0:
        return prior_brier
    if prior_strength < 0:
        raise ValueError(f"prior_strength must be >= 0, got {prior_strength}")
    weight_obs = n / (n + prior_strength)
    weight_prior = prior_strength / (n + prior_strength)
    return weight_obs * observed_brier + weight_prior * prior_brier


def brier_to_weight(brier: float) -> float:
    """Конвертация Brier score → unnormalized weight.

    w = (COIN_FLIP_BRIER - brier) / COIN_FLIP_BRIER
    Clipped в [0, 1]. Brier=0 → w=1 (perfect), Brier=0.25 → w=0 (random),
    Brier>0.25 → w=0 (хуже монетки, бесполезен).
    """
    raw = (COIN_FLIP_BRIER - brier) / COIN_FLIP_BRIER
    return max(0.0, min(1.0, raw))


def softmax_weights(
    raw_weights: Sequence[float], temperature: float = 1.0
) -> list[float]:
    """Softmax-normalization для агрегирующего среднего.

    Temperature → ∞ возвращает равномерное распределение (1/N), → 0 — argmax
    (1.0 у максимума, 0 у остальных). Default=1.0 — стандартный softmax.

    Если все веса равны 0 (например, все агенты хуже coin-flip) — fallback
    к равномерному (1/N), чтобы не делить на 0.
    """
    if not raw_weights:
        return []
    if temperature <= 0:
        raise ValueError(f"temperature must be > 0, got {temperature}")
    arr = [w / temperature for w in raw_weights]
    # subtract max for numerical stability
    m = max(arr)
    exps = [math.exp(x - m) for x in arr]
    total = sum(exps)
    if total <= 0:
        # all zeros — равномерное
        return [1.0 / len(raw_weights)] * len(raw_weights)
    return [e / total for e in exps]


# ─── Per-agent calibration ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentCalibrationStats:
    """Статистика калибровки агента по N resolved прогнозам."""

    agent_role: str  #: 'bull' / 'bear' / 'verifier' / 'synth'
    n_resolved: int  #: сколько прогнозов уже резолвнуто
    raw_brier: float  #: empirical mean Brier (без shrinkage)
    shrunk_brier: float  #: Brier после Bayesian shrinkage
    weight: float  #: unnormalized weight в [0, 1] из brier_to_weight
    mean_predicted_p: float  #: средняя предсказанная P(up) — для bias-детекта
    mean_realized_y: float  #: средняя реализация — для baseline calibration


def compute_agent_stats(
    agent_role: str,
    predicted_probabilities: Sequence[float],
    realized_outcomes: Sequence[bool],
    prior_strength: int = DEFAULT_SHRINKAGE_PRIOR_STRENGTH,
) -> AgentCalibrationStats:
    """Считает калибровку агента по resolved прогнозам.

    Args:
        agent_role: имя агента ('bull' / 'bear' / 'verifier' / 'synth').
        predicted_probabilities: список p ∈ [0, 1] — предсказания агента.
        realized_outcomes: список y ∈ {True, False} — что реально произошло.
        prior_strength: сила Bayesian prior'а для shrinkage.

    Returns:
        AgentCalibrationStats. При n=0 возвращает «нейтральные» стат'ы
        с weight=brier_to_weight(COIN_FLIP_BRIER)=0.0.
    """
    if len(predicted_probabilities) != len(realized_outcomes):
        raise ValueError(
            f"Length mismatch: {len(predicted_probabilities)} predictions "
            f"vs {len(realized_outcomes)} outcomes"
        )
    n = len(predicted_probabilities)
    if n == 0:
        return AgentCalibrationStats(
            agent_role=agent_role,
            n_resolved=0,
            raw_brier=COIN_FLIP_BRIER,
            shrunk_brier=COIN_FLIP_BRIER,
            weight=0.0,
            mean_predicted_p=0.5,
            mean_realized_y=0.5,
        )
    raw = brier_score(predicted_probabilities, realized_outcomes)
    shrunk = shrink_brier(raw, n, prior_strength=prior_strength)
    return AgentCalibrationStats(
        agent_role=agent_role,
        n_resolved=n,
        raw_brier=raw,
        shrunk_brier=shrunk,
        weight=brier_to_weight(shrunk),
        mean_predicted_p=sum(predicted_probabilities) / n,
        mean_realized_y=sum(1.0 if y else 0.0 for y in realized_outcomes) / n,
    )


# ─── Aggregation для Synth-prompt'а ──────────────────────────────────────────


def aggregate_agent_probabilities(
    agent_predictions: dict[str, float],
    agent_stats: dict[str, AgentCalibrationStats],
    min_resolved: int = DEFAULT_MIN_RESOLVED,
    softmax_temperature: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """Агрегирует prob'ы агентов в одно calibration-weighted значение.

    Args:
        agent_predictions: {role: P(up)} — текущие predictions агентов.
        agent_stats: {role: AgentCalibrationStats} — history-based калибровки.
        min_resolved: при n<min_resolved у всех агентов — fallback на равные
            веса (нет данных, чтобы решить кому доверять).
        softmax_temperature: температура softmax. 1.0 = стандарт.

    Returns:
        (aggregated_p, role→normalized_weight). aggregated_p — weighted mean
        predictions. weights суммируются в 1.0.
    """
    if not agent_predictions:
        return 0.5, {}
    roles = list(agent_predictions.keys())
    # raw weights (unnormalized) per role
    raw_weights: list[float] = []
    for role in roles:
        stats = agent_stats.get(role)
        if stats is None or stats.n_resolved < min_resolved:
            # cold start — равный вес
            raw_weights.append(0.5)  # «нейтрально», softmax выровняет
        else:
            raw_weights.append(stats.weight)

    # Если все calibration-weights = 0 (все агенты хуже coin-flip) — все
    # равные, либо если N agents <= 1.
    if all(w <= 0 for w in raw_weights):
        norm = [1.0 / len(roles)] * len(roles)
    else:
        norm = softmax_weights(raw_weights, temperature=softmax_temperature)

    aggregated = sum(agent_predictions[r] * w for r, w in zip(roles, norm))
    return clip_probability(aggregated), dict(zip(roles, norm))


# ─── Public API ──────────────────────────────────────────────────────────────


__all__ = [
    "COIN_FLIP_BRIER",
    "DEFAULT_MIN_RESOLVED",
    "DEFAULT_SHRINKAGE_PRIOR_STRENGTH",
    "AgentCalibrationStats",
    "aggregate_agent_probabilities",
    "brier_to_weight",
    "clip_probability",
    "compute_agent_stats",
    "shrink_brier",
    "softmax_weights",
]
