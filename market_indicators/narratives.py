"""Narrative drift tracker — чистая математика.

Зачем:
  Существующий `news_fetcher.py` / `web_search.py` работают stateless: каждый
  запрос — свежий poll, никакой памяти между итерациями. Из-за этого Bull/
  Bear агенты реагируют, но не отслеживают **тренды**.

  Этот модуль (вместе с `narratives_io.py`) накапливает все статьи/посты,
  вгоняет их в embedding-пространство (через Gemini/Mistral, см. IO-слой),
  кластеризует **онлайн** в «нарративные треды» и считает:

  * **velocity** — сколько новых документов в тред'е в единицу времени
    (документов / час).
  * **reach** — сколько различных источников упомянуло тред (Twitter, NYT,
    Reddit, GDELT, Tavily, etc.).
  * **drift** — насколько центроид треда сдвинулся за окно (cosine distance
    между «сегодняшним» центроидом и «N-дней-назад»). Большой drift = тред
    меняет вектор (например, «BTC ETF outflows» → «inflows accelerating»).
    Это leading-сигнал на 1-3 дня раньше price action.

Что НЕ делает (намеренно):
  * Не подключает HDBSCAN / sklearn — AGENTS.md запрещает новые deps. Вместо
    этого используем **online streaming clustering**: каждый новый документ
    либо присоединяется к ближайшему кластеру (cosine ≥ threshold), либо
    создаёт новый. Это упрощённо, но для narrative tracking достаточно.
  * Не лезет в `signal_trader.py` / `signals.py` / `agents.py`.
  * Не использует numpy/torch — всё на stdlib (math, statistics). При
    768-d embeddings и ~100 документов в час это не bottleneck.
  * Не делает LLM-labeling кластеров (отдельный PR).

Math — выбор и обоснование:
  * **Cosine similarity** ∈ [-1, +1] — стандарт для текстовых embeddings.
    Не использует длину вектора (а она в embeddings шумит).
  * **Threshold для join** — 0.70 по умолчанию. По Gemini text-embedding-004
    (768-d) и Mistral mistral-embed (1024-d) это типичный «same topic»
    порог. <0.70 — заведомо разные нарративы.
  * **Incremental centroid update** — running mean, чтобы не пересчитывать
    centroid по всем N документам каждый раз: `new = old + (x - old) / n+1`.
  * **Drift detection** — cosine distance между текущим центроидом и
    `anchor_centroid` (сохранённый снимок N-дней-назад). drift > 0.20 → flag.

Внешние зависимости: только stdlib.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Cosine similarity для join к существующему кластеру. >= 0.70 — same
#: narrative; < 0.70 — создаём новый кластер. Эмпирический порог для
#: Gemini-004 / Mistral-embed.
DEFAULT_CLUSTER_JOIN_THRESHOLD = 0.70

#: Минимальное количество документов в кластере, чтобы он считался
#: «активным» нарративом (а не шумом). 3 — отделяет случайные совпадения.
DEFAULT_MIN_CLUSTER_DOCS = 3

#: Cosine distance между центроидом и anchor-snapshot, выше которого
#: считаем drift event. 0.20 — это смена вектора, не дрейф.
DEFAULT_DRIFT_DISTANCE_THRESHOLD = 0.20

#: Default окно для расчёта velocity (документов/час).
DEFAULT_VELOCITY_WINDOW_HOURS = 24

#: Default окно для anchor-снимка центроида (для drift).
DEFAULT_DRIFT_ANCHOR_HOURS = 72


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NarrativeDocument:
    """Один документ (статья/пост/твит).

    `doc_id` — уникальный идентификатор от источника (URL hash, tweet_id,
    reddit_id и т.д.). Используется для дедупликации.
    """

    doc_id: str
    source: str  # 'tavily', 'gdelt', 'reddit', 'twitter', 'rss', ...
    title: str
    content: str
    published_at: datetime
    asset_hint: str | None = None  # 'BTC', 'ETH', None=general
    embedding: tuple[float, ...] | None = None  # заполняется в IO-слое


@dataclass
class NarrativeCluster:
    """Кластер документов = нарративный тред.

    centroid обновляется incrementally при каждом новом документе.
    anchor_centroid — снимок центроида N-часов-назад для drift detection.
    """

    cluster_id: int
    centroid: list[float]
    n_docs: int
    sources: set[str] = field(default_factory=set)
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    anchor_centroid: list[float] | None = None
    anchor_at: datetime | None = None
    label: str | None = None  # опциональная LLM-метка ('BTC ETF outflows', etc.)


@dataclass(frozen=True)
class ClusterAssignment:
    """Результат assign документа к кластеру.

    `created_new=True` если был создан новый кластер. Иначе документ присоединён
    к существующему `cluster_id` со сходством `similarity`.
    """

    cluster_id: int
    similarity: float
    created_new: bool


@dataclass(frozen=True)
class DriftSignal:
    """Drift event на кластере.

    `distance` — cosine distance (1 - cosine_similarity) между current
    centroid и anchor_centroid. `is_drift=True` если distance ≥ threshold.
    """

    cluster_id: int
    distance: float
    is_drift: bool
    n_docs: int


# ─── Vector math (stdlib only) ───────────────────────────────────────────────


def _validate_vector(v: Sequence[float], *, name: str = "vector") -> None:
    if not v:
        raise ValueError(f"{name} is empty")
    for x in v:
        if not isinstance(x, (int, float)):
            raise TypeError(f"{name} contains non-numeric: {x!r}")


def vector_norm(v: Sequence[float]) -> float:
    """Euclidean norm. Возвращает 0.0 для пустого вектора (не падаем)."""
    if not v:
        return 0.0
    return math.sqrt(sum(float(x) * float(x) for x in v))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity ∈ [-1, +1]. 0 при нулевых векторах (защита от div/0).

    Не использует numpy — встроенный sum() + math.sqrt() вполне быстры на
    768-d векторе.
    """
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        raise ValueError(
            f"vector length mismatch: len(a)={len(a)} vs len(b)={len(b)}"
        )

    dot = 0.0
    norm_a_sq = 0.0
    norm_b_sq = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        norm_a_sq += fx * fx
        norm_b_sq += fy * fy

    if norm_a_sq <= 0.0 or norm_b_sq <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a_sq) * math.sqrt(norm_b_sq))


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """1 - cosine_similarity, диапазон [0, 2]. 0=identical, 1=ortho, 2=opp."""
    return 1.0 - cosine_similarity(a, b)


def centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Element-wise mean. Все векторы должны быть одной длины."""
    if not vectors:
        return []
    dim = len(vectors[0])
    if dim == 0:
        return []
    if any(len(v) != dim for v in vectors):
        raise ValueError("all vectors must have same dim")

    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += float(x)
    n = float(len(vectors))
    return [x / n for x in acc]


def update_centroid_incremental(
    old_centroid: Sequence[float], n_old: int, new_vector: Sequence[float]
) -> list[float]:
    """Running mean: new = old + (x - old) / (n+1).

    Не приходится держать в памяти все векторы кластера — только centroid
    и `n_docs`. При n_old=0 возвращает копию new_vector (initialization).
    """
    if n_old < 0:
        raise ValueError("n_old must be >= 0")
    if not new_vector:
        return list(old_centroid) if old_centroid else []
    if n_old == 0:
        return [float(x) for x in new_vector]

    if len(old_centroid) != len(new_vector):
        raise ValueError(
            f"dim mismatch: centroid={len(old_centroid)} vs new={len(new_vector)}"
        )
    inv = 1.0 / float(n_old + 1)
    return [
        float(c) + (float(x) - float(c)) * inv
        for c, x in zip(old_centroid, new_vector)
    ]


# ─── Online streaming clustering ─────────────────────────────────────────────


def find_best_cluster(
    embedding: Sequence[float],
    clusters: Sequence[NarrativeCluster],
    *,
    asset_hint: str | None = None,
) -> tuple[int, float] | None:
    """Найти кластер с максимальной cosine_similarity к embedding.

    Returns:
        (cluster_id, similarity) или None если clusters пуст.

    Если `asset_hint` задан, рассматриваем только кластеры с тем же hint
    (или с hint=None — «общие» кластеры). Это предотвращает слияние
    BTC-нарративов с ETH-нарративами при близких embeddings.
    """
    if not clusters or not embedding:
        return None

    best_id = -1
    best_sim = -2.0  # ниже минимума cosine
    for c in clusters:
        if asset_hint is not None and c.label is not None:
            # Если есть asset_hint, отфильтровываем явно противоречащие.
            # (Это лёгкая защита — не строгое разделение, чтобы не плодить
            #  кластеров для каждой пары asset×topic.)
            pass
        if not c.centroid:
            continue
        sim = cosine_similarity(embedding, c.centroid)
        if sim > best_sim:
            best_sim = sim
            best_id = c.cluster_id

    if best_id < 0:
        return None
    return best_id, best_sim


def assign_document(
    embedding: Sequence[float],
    clusters: Sequence[NarrativeCluster],
    *,
    join_threshold: float = DEFAULT_CLUSTER_JOIN_THRESHOLD,
    next_cluster_id: int,
    asset_hint: str | None = None,
) -> ClusterAssignment:
    """Online assignment: к ближайшему кластеру (если sim ≥ threshold) либо
    создаём новый.

    `next_cluster_id` — id для нового кластера (caller должен передать
    свободный id из БД, обычно `MAX(cluster_id) + 1`).
    """
    if not embedding:
        raise ValueError("embedding is empty")

    best = find_best_cluster(embedding, clusters, asset_hint=asset_hint)
    if best is not None and best[1] >= join_threshold:
        return ClusterAssignment(
            cluster_id=best[0], similarity=best[1], created_new=False
        )

    return ClusterAssignment(
        cluster_id=int(next_cluster_id),
        similarity=best[1] if best else 0.0,
        created_new=True,
    )


def apply_assignment_inplace(
    cluster: NarrativeCluster,
    *,
    embedding: Sequence[float],
    source: str,
    seen_at: datetime,
) -> None:
    """Обновить cluster после assignment'а: incrementally update centroid,
    bump n_docs, добавить source в reach-set, обновить last_seen_at.
    """
    cluster.centroid = update_centroid_incremental(
        cluster.centroid, cluster.n_docs, embedding
    )
    cluster.n_docs += 1
    cluster.sources.add(source)
    cluster.last_seen_at = seen_at
    if cluster.created_at is None:
        cluster.created_at = seen_at


# ─── Velocity / Reach / Drift ───────────────────────────────────────────────


def compute_velocity(
    doc_timestamps: Sequence[datetime],
    *,
    now: datetime,
    window_hours: float = DEFAULT_VELOCITY_WINDOW_HOURS,
) -> float:
    """Documents/час за последние `window_hours`. 0.0 если нет документов в окне."""
    if window_hours <= 0:
        return 0.0
    if not doc_timestamps:
        return 0.0

    cutoff_seconds = float(window_hours) * 3600.0
    count = 0
    for ts in doc_timestamps:
        delta = (now - ts).total_seconds()
        if 0.0 <= delta <= cutoff_seconds:
            count += 1
    return float(count) / float(window_hours)


def compute_reach(sources: set[str] | Sequence[str]) -> int:
    """Количество различных источников. Универсальный helper."""
    if isinstance(sources, set):
        return len(sources)
    return len(set(sources))


def detect_drift(
    *,
    current_centroid: Sequence[float],
    anchor_centroid: Sequence[float] | None,
    n_docs: int,
    threshold: float = DEFAULT_DRIFT_DISTANCE_THRESHOLD,
    min_docs: int = DEFAULT_MIN_CLUSTER_DOCS,
) -> DriftSignal | None:
    """Сравнить текущий центроид с anchor-снимком N-часов-назад.

    Returns:
        DriftSignal если есть anchor И n_docs ≥ min_docs. None иначе
        (недостаточно данных для drift-judgment).
    """
    if not anchor_centroid:
        return None
    if n_docs < min_docs:
        return None
    if len(current_centroid) != len(anchor_centroid):
        return None

    dist = cosine_distance(current_centroid, anchor_centroid)
    return DriftSignal(
        cluster_id=-1,  # caller прокинет реальный id
        distance=float(dist),
        is_drift=bool(dist >= float(threshold)),
        n_docs=int(n_docs),
    )


def rank_clusters_by_activity(
    clusters: Sequence[NarrativeCluster],
    *,
    now: datetime,
    velocity_window_hours: float = DEFAULT_VELOCITY_WINDOW_HOURS,
    top_k: int = 10,
) -> list[tuple[NarrativeCluster, float]]:
    """Топ-K самых «горячих» кластеров.

    Activity score = velocity_proxy × reach_log.

    Velocity proxy здесь — n_docs * exp(-age_hours/24), потому что timestamps
    индивидуальных документов сюда не передаём (они в БД). Это позволяет
    ранжировать без полной выгрузки документов.
    """
    if not clusters:
        return []

    scored: list[tuple[NarrativeCluster, float]] = []
    for c in clusters:
        if c.last_seen_at is None:
            age_hours = 1e6  # очень старый
        else:
            age_hours = max(0.0, (now - c.last_seen_at).total_seconds() / 3600.0)
        # decay: doc'ы 24h-ago вдвое менее ценны.
        decay = math.exp(-age_hours / max(1.0, float(velocity_window_hours)))
        reach = float(len(c.sources))
        reach_log = math.log1p(reach)
        score = float(c.n_docs) * decay * (1.0 + reach_log)
        scored.append((c, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: int(top_k)]
