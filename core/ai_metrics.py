"""Persistent metrics for every LLM call routed through ``ai_provider``.

Why: as of M0 we have *zero* visibility into how the multi-agent debate is
spending its free-tier quota. When Mistral falls over at 3am we only see it
in real-time logs — there is no historical view of "which provider failed
how many times last 24h, for which role". Without that data, picking a primary
provider is guesswork.

This module is intentionally tiny and *separate* from ``database.py`` (the
god-file). It owns one table:

    ai_call_metrics(
        id, ts, provider, model, role, latency_ms, ok, error
    )

The recorder is **best-effort**: any DB exception is swallowed (logged at
WARNING), because metrics must never break a live debate.

Hot-path cost: one INSERT per call (well under 5 ms on tmpfs SQLite). The
table is unindexed — we only read it in admin queries.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ai_call_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    provider    TEXT NOT NULL,
    model       TEXT,
    role        TEXT,
    latency_ms  INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT
)
"""


async def init_ai_metrics_db(db_path: Optional[str] = None) -> None:
    """Create the ``ai_call_metrics`` table if it does not exist.

    Safe to call multiple times — uses ``IF NOT EXISTS``.
    """
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute(_CREATE_TABLE_SQL)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — never fatal
        logger.warning("ai_metrics: init_db failed: %s", exc)


async def record_ai_call(
    *,
    provider: str,
    model: Optional[str],
    role: Optional[str],
    latency_ms: int,
    ok: bool,
    error: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Insert one row into ``ai_call_metrics``. Never raises."""
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "INSERT INTO ai_call_metrics(provider, model, role, latency_ms, ok, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(provider)[:64],
                    (str(model)[:128] if model else None),
                    (str(role)[:32] if role else None),
                    int(max(0, latency_ms)),
                    1 if ok else 0,
                    (str(error)[:512] if error else None),
                ),
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "ai_metrics: record failed (provider=%s ok=%s): %s",
            provider, ok, exc,
        )


@asynccontextmanager
async def track_ai_call(
    *,
    provider: str,
    model: Optional[str] = None,
    role: Optional[str] = None,
    db_path: Optional[str] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Context manager: records latency + ok flag automatically.

    Usage::

        async with track_ai_call(provider="cerebras", model=m, role="bull") as ctx:
            ctx["model"] = m  # may be updated mid-call
            result = await _call_cerebras(...)

    On any exception inside the ``async with`` body, the call is logged with
    ``ok=False`` and the exception class name (truncated to 512 chars), then
    re-raised. Context dict accepts ``model`` overrides so caller can record
    the actual model that ended up being used after fallback.
    """
    started = time.monotonic()
    ctx: dict[str, Any] = {"model": model}
    try:
        yield ctx
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await record_ai_call(
            provider=provider,
            model=ctx.get("model") or model,
            role=role,
            latency_ms=elapsed_ms,
            ok=False,
            error=f"{type(exc).__name__}: {exc!s}",
            db_path=db_path,
        )
        raise
    else:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await record_ai_call(
            provider=provider,
            model=ctx.get("model") or model,
            role=role,
            latency_ms=elapsed_ms,
            ok=True,
            db_path=db_path,
        )


async def fetch_recent_metrics(
    *,
    hours: int = 24,
    provider: Optional[str] = None,
    role: Optional[str] = None,
    limit: int = 1000,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return rows from the last ``hours`` (descending by ts).

    Use the named params to filter. ``hours <= 0`` returns everything (capped
    by ``limit``). Returns ``[]`` on any error (logged at WARNING) so admin
    commands stay non-fatal.
    """
    path = db_path or DB_PATH
    where: list[str] = []
    params: list[Any] = []
    if hours and hours > 0:
        where.append("ts >= datetime('now', ?)")
        params.append(f"-{int(hours)} hours")
    if provider:
        where.append("provider = ?")
        params.append(provider)
    if role:
        where.append("role = ?")
        params.append(role)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, ts, provider, model, role, latency_ms, ok, error "
        "FROM ai_call_metrics" + where_sql + " ORDER BY id DESC LIMIT ?"
    )
    params.append(int(max(1, limit)))
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "provider": r["provider"],
                    "model": r["model"],
                    "role": r["role"],
                    "latency_ms": r["latency_ms"],
                    "ok": bool(r["ok"]),
                    "error": r["error"],
                }
                for r in rows
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_metrics: fetch failed: %s", exc)
        return []


async def summarise_recent(
    *, hours: int = 24, db_path: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate by provider: ``{"cerebras": {"calls": N, "ok": M, "p50": ms, "p95": ms}, ...}``.

    Tiny rollup for admin / future /metrics command. Returns ``{}`` on error.
    """
    path = db_path or DB_PATH
    try:
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT provider, latency_ms, ok FROM ai_call_metrics "
                "WHERE ts >= datetime('now', ?) ORDER BY provider, latency_ms",
                (f"-{int(max(1, hours))} hours",),
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_metrics: summarise failed: %s", exc)
        return {}

    grouped: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        grouped.setdefault(r["provider"], []).append((int(r["latency_ms"]), int(r["ok"])))
    result: dict[str, dict[str, Any]] = {}
    for provider, samples in grouped.items():
        latencies = sorted(s[0] for s in samples)
        oks = sum(s[1] for s in samples)
        n = len(samples)
        result[provider] = {
            "calls": n,
            "ok": oks,
            "fail": n - oks,
            "p50": latencies[n // 2] if n else 0,
            "p95": latencies[min(n - 1, int(n * 0.95))] if n else 0,
        }
    return result
