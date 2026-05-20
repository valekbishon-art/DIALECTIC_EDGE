"""I/O для narrative drift tracker.

Отделено от чистой математики (`narratives.py`):
  * Embedding clients (Gemini text-embedding-004, Mistral mistral-embed) с
    fallback chain и кэшированием. DI-based, чтобы тесты не лезли в сеть.
  * Document ingestion pipeline: dedupe → embed batch → online cluster
    → persist → drift detection.
  * Env-flags + конфиг.

Все внешние deps (HTTP, БД) через DI. Без новых пакетов в requirements.txt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Sequence

from market_indicators.narratives import (
    DEFAULT_CLUSTER_JOIN_THRESHOLD,
    DEFAULT_DRIFT_ANCHOR_HOURS,
    DEFAULT_DRIFT_DISTANCE_THRESHOLD,
    DEFAULT_MIN_CLUSTER_DOCS,
    DEFAULT_VELOCITY_WINDOW_HOURS,
    ClusterAssignment,
    DriftSignal,
    NarrativeCluster,
    NarrativeDocument,
    apply_assignment_inplace,
    assign_document,
    cosine_distance,
    detect_drift,
)

logger = logging.getLogger(__name__)


# ─── Типы ────────────────────────────────────────────────────────────────────

#: Callable, который батчем embed'ит тексты. Async, потому что HTTP.
EmbeddingClient = Callable[[Sequence[str]], Awaitable[list[list[float]]]]

#: Callable, который возвращает свежие документы для обработки.
#: (DI-инъектируется из scheduler — может быть Tavily / GDELT / Reddit.)
DocumentProvider = Callable[[], Awaitable[Sequence[NarrativeDocument]]]


# ─── Embedding providers ─────────────────────────────────────────────────────


def _truncate_for_embedding(text: str, max_chars: int = 8000) -> str:
    """Embedding API имеют лимиты по токенам. 8000 chars ≈ 2000 токенов — safe
    для Gemini/Mistral. Текст обрезается, не падаем при слишком длинных доках.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _normalize_text_for_embed(title: str, content: str) -> str:
    """title + content → одна строка. Title повторяем 2x для веса (типичный
    приём в IR-системах: heading-weighting).
    """
    t = (title or "").strip()
    c = (content or "").strip()
    if t and c:
        merged = f"{t}\n{t}\n{c}"
    else:
        merged = t or c
    return _truncate_for_embedding(merged)


async def _post_json(
    *,
    http_client: Any,
    url: str,
    headers: dict,
    json_body: dict,
    timeout_sec: float = 15.0,
) -> dict:
    """Generic POST wrapper. http_client должен иметь .post → ctx manager."""
    import aiohttp  # noqa: PLC0415

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with http_client.post(url, headers=headers, json=json_body, timeout=timeout) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
        return await resp.json()


async def gemini_embed_batch(
    texts: Sequence[str],
    *,
    api_key: str,
    model: str = "text-embedding-004",
    http_session: Any | None = None,
    timeout_sec: float = 15.0,
) -> list[list[float]]:
    """Gemini text-embedding-004 (768-d, бесплатно до 1500 req/day).

    Endpoint: POST :batchEmbedContents.
    https://ai.google.dev/api/embeddings#method:-models.batchembedcontents
    """
    if not texts:
        return []
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY required for gemini_embed_batch")

    import aiohttp  # noqa: PLC0415

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:batchEmbedContents?key={api_key}"
    )
    requests = [
        {
            "model": f"models/{model}",
            "content": {"parts": [{"text": _truncate_for_embedding(t)}]},
        }
        for t in texts
    ]
    body = {"requests": requests}

    close_session = http_session is None
    session = http_session or aiohttp.ClientSession()
    try:
        data = await _post_json(
            http_client=session, url=url, headers={"Content-Type": "application/json"},
            json_body=body, timeout_sec=timeout_sec,
        )
    finally:
        if close_session:
            await session.close()

    out: list[list[float]] = []
    for item in data.get("embeddings", []):
        values = item.get("values") or item.get("embedding", {}).get("values") or []
        out.append([float(x) for x in values])
    if len(out) != len(texts):
        raise RuntimeError(
            f"Gemini вернул {len(out)} embeddings, ожидали {len(texts)}"
        )
    return out


async def mistral_embed_batch(
    texts: Sequence[str],
    *,
    api_key: str,
    model: str = "mistral-embed",
    http_session: Any | None = None,
    timeout_sec: float = 20.0,
) -> list[list[float]]:
    """Mistral mistral-embed (1024-d).

    Endpoint: POST /v1/embeddings.
    https://docs.mistral.ai/api/#tag/embeddings
    """
    if not texts:
        return []
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY required for mistral_embed_batch")

    import aiohttp  # noqa: PLC0415

    url = "https://api.mistral.ai/v1/embeddings"
    body = {"model": model, "input": [_truncate_for_embedding(t) for t in texts]}

    close_session = http_session is None
    session = http_session or aiohttp.ClientSession()
    try:
        data = await _post_json(
            http_client=session, url=url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json_body=body, timeout_sec=timeout_sec,
        )
    finally:
        if close_session:
            await session.close()

    out: list[list[float]] = []
    for item in data.get("data", []):
        emb = item.get("embedding") or []
        out.append([float(x) for x in emb])
    if len(out) != len(texts):
        raise RuntimeError(
            f"Mistral вернул {len(out)} embeddings, ожидали {len(texts)}"
        )
    return out


# ─── Embedding client factory + fallback chain ──────────────────────────────


def get_active_provider() -> str:
    """Из env NARRATIVE_EMBEDDING_PROVIDER. Default 'gemini'."""
    return (os.getenv("NARRATIVE_EMBEDDING_PROVIDER", "gemini") or "gemini").lower().strip()


def make_embedding_client(
    *,
    provider: str | None = None,
    http_session: Any | None = None,
) -> EmbeddingClient:
    """Создать EmbeddingClient с fallback'ом.

    Стратегия:
      1. Пытаемся `provider` (default Gemini).
      2. При ошибке → fallback на Mistral.
      3. Если ни Gemini, ни Mistral ключей нет → RuntimeError.

    Все ошибки логируются, но не блокируют loop — caller обрабатывает.
    """
    active = (provider or get_active_provider()).lower()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    mistral_key = os.getenv("MISTRAL_API_KEY", "").strip()

    if not gemini_key and not mistral_key:
        raise RuntimeError(
            "NARRATIVE_DRIFT requires GEMINI_API_KEY or MISTRAL_API_KEY"
        )

    async def _embed(texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        primary, secondary = (
            ("gemini", "mistral") if active == "gemini" else ("mistral", "gemini")
        )

        async def _try(name: str) -> list[list[float]] | None:
            try:
                if name == "gemini" and gemini_key:
                    return await gemini_embed_batch(
                        texts, api_key=gemini_key, http_session=http_session
                    )
                if name == "mistral" and mistral_key:
                    return await mistral_embed_batch(
                        texts, api_key=mistral_key, http_session=http_session
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("narrative embed %s упал: %s", name, e)
            return None

        for name in (primary, secondary):
            result = await _try(name)
            if result is not None:
                return result
        raise RuntimeError("Все embedding-провайдеры упали (Gemini + Mistral)")

    return _embed


# ─── Document hashing for deduplication ─────────────────────────────────────


def make_doc_id(*, source: str, url: str, title: str) -> str:
    """Стабильный hash для дедупликации. Если source отдаёт собственный
    идентификатор (tweet_id), сначала используем его; иначе hash от url+title.
    """
    base = (url or "").strip().lower() or (title or "").strip().lower()
    if not base:
        base = f"{source}:empty"
    h = hashlib.sha1(f"{source}::{base}".encode("utf-8"), usedforsecurity=False)
    return h.hexdigest()[:24]


# ─── Document classification heuristics (asset_hint) ────────────────────────


_ASSET_PATTERNS = {
    "BTC": re.compile(r"\b(bitcoin|btc|сатош|сатоши|сатош)\b", re.IGNORECASE),
    "ETH": re.compile(r"\b(ethereum|eth|вита́лик|vitalik)\b", re.IGNORECASE),
    "SOL": re.compile(r"\b(solana|sol)\b", re.IGNORECASE),
}


def classify_asset_hint(title: str, content: str) -> str | None:
    """Дешёвая эвристика по ключевым словам. None если нет hits.

    Намеренно простая — для предотвращения слияния BTC- и ETH-нарративов на
    близких embeddings (например, оба пишут про «ETF inflows»).
    """
    text = f"{title or ''} {content or ''}"
    if not text.strip():
        return None
    hits = []
    for asset, pat in _ASSET_PATTERNS.items():
        if pat.search(text):
            hits.append(asset)
    if len(hits) == 1:
        return hits[0]
    return None  # 0 или 2+ → general


# ─── Env-flags ───────────────────────────────────────────────────────────────


def feature_enabled() -> bool:
    """FEATURE_NARRATIVE_DRIFT=1 включает. Default OFF."""
    return os.getenv("FEATURE_NARRATIVE_DRIFT", "0").strip() in {"1", "true", "True", "yes"}


def get_cluster_threshold() -> float:
    try:
        v = float(os.getenv("NARRATIVE_CLUSTER_THRESHOLD", str(DEFAULT_CLUSTER_JOIN_THRESHOLD)))
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return DEFAULT_CLUSTER_JOIN_THRESHOLD


def get_drift_threshold() -> float:
    try:
        v = float(os.getenv("NARRATIVE_DRIFT_THRESHOLD", str(DEFAULT_DRIFT_DISTANCE_THRESHOLD)))
        return max(0.0, min(2.0, v))
    except (TypeError, ValueError):
        return DEFAULT_DRIFT_DISTANCE_THRESHOLD


def get_velocity_window_hours() -> float:
    try:
        return max(1.0, float(os.getenv("NARRATIVE_VELOCITY_WINDOW_H",
                                        str(DEFAULT_VELOCITY_WINDOW_HOURS))))
    except (TypeError, ValueError):
        return float(DEFAULT_VELOCITY_WINDOW_HOURS)


def get_drift_anchor_hours() -> float:
    try:
        return max(1.0, float(os.getenv("NARRATIVE_DRIFT_ANCHOR_H",
                                        str(DEFAULT_DRIFT_ANCHOR_HOURS))))
    except (TypeError, ValueError):
        return float(DEFAULT_DRIFT_ANCHOR_HOURS)


def get_interval_seconds() -> int:
    try:
        return max(300, int(os.getenv("NARRATIVE_INTERVAL_SEC", "3600")))
    except (TypeError, ValueError):
        return 3600


def get_retention_days() -> int:
    try:
        return max(7, int(os.getenv("NARRATIVE_RETENTION_DAYS", "180")))
    except (TypeError, ValueError):
        return 180


def get_max_docs_per_batch() -> int:
    try:
        return max(1, min(100, int(os.getenv("NARRATIVE_MAX_DOCS_PER_BATCH", "50"))))
    except (TypeError, ValueError):
        return 50


def get_min_cluster_docs() -> int:
    try:
        return max(1, int(os.getenv("NARRATIVE_MIN_CLUSTER_DOCS", str(DEFAULT_MIN_CLUSTER_DOCS))))
    except (TypeError, ValueError):
        return DEFAULT_MIN_CLUSTER_DOCS


# ─── Pipeline: ingest + cluster + persist ───────────────────────────────────


@dataclass
class IngestResult:
    """Итог одной итерации pipeline'а."""

    docs_processed: int
    docs_skipped_dup: int
    new_clusters: int
    joined_existing: int
    drift_events: list[DriftSignal]


async def ingest_documents(
    *,
    docs: Sequence[NarrativeDocument],
    embedding_client: EmbeddingClient,
    db_adapter: "NarrativeDBAdapter",
    join_threshold: float | None = None,
    drift_threshold: float | None = None,
    drift_anchor_hours: float | None = None,
    min_cluster_docs: int | None = None,
    now: datetime | None = None,
) -> IngestResult:
    """End-to-end pipeline: dedupe → embed → cluster → persist → detect drift.

    `db_adapter` — DI-обёртка над database.py (см. NarrativeDBAdapter).
    Это позволяет тестам подменять реальный SQLite на in-memory dict.

    Returns:
        IngestResult со статистикой + список drift-событий (если есть).
    """
    if not docs:
        return IngestResult(0, 0, 0, 0, [])

    threshold = join_threshold if join_threshold is not None else get_cluster_threshold()
    drift_th = drift_threshold if drift_threshold is not None else get_drift_threshold()
    anchor_hours = (
        drift_anchor_hours if drift_anchor_hours is not None else get_drift_anchor_hours()
    )
    min_docs = min_cluster_docs if min_cluster_docs is not None else get_min_cluster_docs()
    moment = now or datetime.utcnow()

    # 1. Dedupe по doc_id (in-batch и в БД).
    seen_in_batch: set[str] = set()
    fresh: list[NarrativeDocument] = []
    for d in docs:
        if not d.doc_id or d.doc_id in seen_in_batch:
            continue
        if await db_adapter.document_exists(d.doc_id):
            continue
        seen_in_batch.add(d.doc_id)
        fresh.append(d)

    docs_skipped_dup = len(docs) - len(fresh)
    if not fresh:
        return IngestResult(0, docs_skipped_dup, 0, 0, [])

    # 2. Batch embed.
    texts = [_normalize_text_for_embed(d.title, d.content) for d in fresh]
    try:
        embeddings = await embedding_client(texts)
    except Exception as e:  # noqa: BLE001
        logger.error("narrative embed batch упал: %s", e)
        return IngestResult(0, docs_skipped_dup, 0, 0, [])

    if len(embeddings) != len(fresh):
        logger.error(
            "narrative: embedding count mismatch (got %d, expected %d)",
            len(embeddings), len(fresh),
        )
        return IngestResult(0, docs_skipped_dup, 0, 0, [])

    # 3. Online clustering. Грузим текущие кластеры из БД (только centroids).
    clusters = await db_adapter.load_clusters()
    cluster_by_id: dict[int, NarrativeCluster] = {c.cluster_id: c for c in clusters}
    next_id = (max((c.cluster_id for c in clusters), default=0) + 1) if clusters else 1

    new_clusters = 0
    joined_existing = 0

    for doc, embedding in zip(fresh, embeddings):
        if not embedding:
            logger.warning("narrative: пустой embedding для doc_id=%s, skip", doc.doc_id)
            continue

        assignment: ClusterAssignment = assign_document(
            embedding,
            list(cluster_by_id.values()),
            join_threshold=threshold,
            next_cluster_id=next_id,
            asset_hint=doc.asset_hint,
        )

        if assignment.created_new:
            cluster = NarrativeCluster(
                cluster_id=assignment.cluster_id,
                centroid=[float(x) for x in embedding],
                n_docs=1,
                sources={doc.source},
                created_at=moment,
                last_seen_at=moment,
            )
            cluster_by_id[cluster.cluster_id] = cluster
            next_id = cluster.cluster_id + 1
            new_clusters += 1
        else:
            cluster = cluster_by_id[assignment.cluster_id]
            apply_assignment_inplace(
                cluster, embedding=embedding, source=doc.source, seen_at=moment,
            )
            joined_existing += 1

        await db_adapter.save_document(
            doc=doc, embedding=embedding, cluster_id=cluster.cluster_id,
        )

    # 4. Persist обновлённые кластеры.
    for cluster in cluster_by_id.values():
        await db_adapter.save_cluster(cluster=cluster)

    # 5. Drift detection: для каждого touched кластера сравниваем centroid
    # с anchor-snapshot.
    drift_events: list[DriftSignal] = []
    touched_ids = {c.cluster_id for c in cluster_by_id.values()
                   if c.last_seen_at == moment}
    for cid in touched_ids:
        cluster = cluster_by_id[cid]
        anchor = await db_adapter.load_anchor_centroid(
            cluster_id=cid, hours_ago=anchor_hours,
        )
        if anchor:
            ds = detect_drift(
                current_centroid=cluster.centroid,
                anchor_centroid=anchor,
                n_docs=cluster.n_docs,
                threshold=drift_th,
                min_docs=min_docs,
            )
            if ds is not None and ds.is_drift:
                drift_events.append(
                    DriftSignal(
                        cluster_id=cid,
                        distance=ds.distance,
                        is_drift=True,
                        n_docs=ds.n_docs,
                    )
                )

    return IngestResult(
        docs_processed=len(fresh),
        docs_skipped_dup=docs_skipped_dup,
        new_clusters=new_clusters,
        joined_existing=joined_existing,
        drift_events=drift_events,
    )


# ─── DB Adapter ─────────────────────────────────────────────────────────────
#
# Изолирует SQLite от core pipeline для тестируемости. Реальная реализация
# (`SqliteNarrativeDBAdapter`) живёт ниже и тонко wraps database.py.


class NarrativeDBAdapter:
    """Interface: всё что нужно ingest_documents() от БД.

    Тесты подменяют реальной in-memory реализацией.
    """

    async def document_exists(self, doc_id: str) -> bool:
        raise NotImplementedError

    async def save_document(
        self, *, doc: NarrativeDocument,
        embedding: Sequence[float], cluster_id: int,
    ) -> None:
        raise NotImplementedError

    async def load_clusters(self) -> list[NarrativeCluster]:
        raise NotImplementedError

    async def save_cluster(self, *, cluster: NarrativeCluster) -> None:
        raise NotImplementedError

    async def load_anchor_centroid(
        self, *, cluster_id: int, hours_ago: float,
    ) -> list[float] | None:
        raise NotImplementedError


class SqliteNarrativeDBAdapter(NarrativeDBAdapter):
    """Реальная реализация, обращается к database.py."""

    async def document_exists(self, doc_id: str) -> bool:
        from database import narrative_document_exists  # noqa: PLC0415
        return await narrative_document_exists(doc_id=doc_id)

    async def save_document(
        self, *, doc: NarrativeDocument,
        embedding: Sequence[float], cluster_id: int,
    ) -> None:
        from database import save_narrative_document  # noqa: PLC0415
        await save_narrative_document(
            doc_id=doc.doc_id, source=doc.source, title=doc.title,
            content=doc.content, asset_hint=doc.asset_hint,
            published_at=doc.published_at.isoformat() if doc.published_at else None,
            embedding_json=json.dumps([float(x) for x in embedding]),
            cluster_id=int(cluster_id),
        )

    async def load_clusters(self) -> list[NarrativeCluster]:
        from database import load_narrative_clusters  # noqa: PLC0415
        rows = await load_narrative_clusters()
        result: list[NarrativeCluster] = []
        for row in rows:
            try:
                centroid_vec = json.loads(row["centroid_json"]) if row.get("centroid_json") else []
            except (json.JSONDecodeError, TypeError):
                centroid_vec = []
            try:
                sources_list = json.loads(row["sources_json"]) if row.get("sources_json") else []
            except (json.JSONDecodeError, TypeError):
                sources_list = []
            try:
                anchor_vec = (
                    json.loads(row["anchor_centroid_json"])
                    if row.get("anchor_centroid_json") else None
                )
            except (json.JSONDecodeError, TypeError):
                anchor_vec = None

            def _parse_dt(s: Any) -> datetime | None:
                if not s:
                    return None
                try:
                    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    return None

            result.append(NarrativeCluster(
                cluster_id=int(row["cluster_id"]),
                centroid=[float(x) for x in centroid_vec],
                n_docs=int(row.get("n_docs") or 0),
                sources=set(str(s) for s in sources_list),
                created_at=_parse_dt(row.get("created_at")),
                last_seen_at=_parse_dt(row.get("last_seen_at")),
                anchor_centroid=anchor_vec,
                anchor_at=_parse_dt(row.get("anchor_at")),
                label=row.get("label"),
            ))
        return result

    async def save_cluster(self, *, cluster: NarrativeCluster) -> None:
        from database import upsert_narrative_cluster  # noqa: PLC0415
        await upsert_narrative_cluster(
            cluster_id=cluster.cluster_id,
            centroid_json=json.dumps([float(x) for x in cluster.centroid]),
            n_docs=cluster.n_docs,
            sources_json=json.dumps(sorted(cluster.sources)),
            created_at=cluster.created_at.isoformat() if cluster.created_at else None,
            last_seen_at=cluster.last_seen_at.isoformat() if cluster.last_seen_at else None,
            label=cluster.label,
        )

    async def load_anchor_centroid(
        self, *, cluster_id: int, hours_ago: float,
    ) -> list[float] | None:
        from database import load_narrative_anchor_centroid  # noqa: PLC0415
        return await load_narrative_anchor_centroid(
            cluster_id=cluster_id, hours_ago=hours_ago,
        )


# ─── Helpers для логирования drift ──────────────────────────────────────────


def format_drift_summary(drift: DriftSignal, *, cluster_label: str | None = None) -> str:
    """Удобная строка для логов/Telegram."""
    label = cluster_label or f"cluster #{drift.cluster_id}"
    return (
        f"🌐 narrative DRIFT {label}: distance={drift.distance:.3f} "
        f"(n_docs={drift.n_docs})"
    )


def compute_distance(
    a: Sequence[float] | None, b: Sequence[float] | None
) -> float | None:
    """Удобная обёртка для caller'ов которые не хотят импортить math-модуль."""
    if not a or not b or len(a) != len(b):
        return None
    return cosine_distance(a, b)
