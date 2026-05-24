"""I/O layer for P2P self-audit (SQLite persistence + backcheck loop).

Designed to be imported lazily by the scheduler / handler. All functions are
asyncio coroutines; the only synchronous helper is the row decoder.

See :mod:`p2p_audit` for pure-math and dataclass definitions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Sequence

import aiosqlite

from config import DB_PATH

from p2p_arbitrage import P2PAdvert, P2POpportunity, opportunity_key
from p2p_audit import (
    STATUS_EXPIRED,
    STATUS_PENDING,
    OpportunityAuditRecord,
    ThresholdAdjustmentRecommendation,
    compute_realised_spread,
    format_audit_summary,
    get_backcheck_delay_min,
    get_backcheck_interval_min,
    get_retention_days,
    recommend_threshold_adjustment,
)

logger = logging.getLogger(__name__)

# ─── Table init (idempotent) ─────────────────────────────────────────────────


async def ensure_audit_table_exists(db_path: str | None = None) -> None:
    """Idempotent CREATE TABLE — safe to call from init_db() or lazily."""
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS p2p_audit_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_key   TEXT    NOT NULL,
                asset             TEXT    NOT NULL,
                fiat              TEXT    NOT NULL,
                venue_buy         TEXT    NOT NULL,
                venue_sell        TEXT    NOT NULL,
                buy_price         REAL    NOT NULL,
                sell_price        REAL    NOT NULL,
                gross_spread_pct  REAL    NOT NULL,
                net_spread_pct    REAL    NOT NULL,
                risk_level        TEXT    NOT NULL,
                shown_at_ms       INTEGER NOT NULL,
                realised_at_ms    INTEGER,
                realised_spread_pct REAL,
                status            TEXT    NOT NULL DEFAULT 'pending'
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_p2p_audit_shown_at "
            "ON p2p_audit_log (shown_at_ms DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_p2p_audit_status "
            "ON p2p_audit_log (status, shown_at_ms DESC)"
        )
        await db.commit()


# ─── Persist ─────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


async def persist_opportunity_for_audit(
    opportunity: P2POpportunity,
    *,
    shown_at_ms: int | None = None,
    db_path: str | None = None,
) -> int:
    """Insert a new audit log row for an opportunity that was just surfaced."""
    path = db_path or DB_PATH
    when = shown_at_ms or _now_ms()
    key = opportunity_key(opportunity)
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            """
            INSERT INTO p2p_audit_log (
                opportunity_key, asset, fiat, venue_buy, venue_sell,
                buy_price, sell_price, gross_spread_pct, net_spread_pct,
                risk_level, shown_at_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                opportunity.asset,
                opportunity.fiat,
                opportunity.buy_ad.venue,
                opportunity.sell_ad.venue,
                opportunity.buy_ad.price,
                opportunity.sell_ad.price,
                opportunity.gross_spread_pct,
                opportunity.net_spread_pct,
                opportunity.risk_level,
                when,
                STATUS_PENDING,
            ),
        )
        await db.commit()
        return int(cur.lastrowid or 0)


async def persist_opportunities_for_audit(
    opportunities: Sequence[P2POpportunity],
    *,
    shown_at_ms: int | None = None,
    db_path: str | None = None,
) -> int:
    """Bulk insert with one commit."""
    if not opportunities:
        return 0
    path = db_path or DB_PATH
    when = shown_at_ms or _now_ms()
    rows = [
        (
            opportunity_key(opp),
            opp.asset,
            opp.fiat,
            opp.buy_ad.venue,
            opp.sell_ad.venue,
            opp.buy_ad.price,
            opp.sell_ad.price,
            opp.gross_spread_pct,
            opp.net_spread_pct,
            opp.risk_level,
            when,
            STATUS_PENDING,
        )
        for opp in opportunities
    ]
    async with aiosqlite.connect(path) as db:
        await db.executemany(
            """
            INSERT INTO p2p_audit_log (
                opportunity_key, asset, fiat, venue_buy, venue_sell,
                buy_price, sell_price, gross_spread_pct, net_spread_pct,
                risk_level, shown_at_ms, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.commit()
    return len(rows)


# ─── Load ────────────────────────────────────────────────────────────────────


def _row_to_record(row: aiosqlite.Row) -> OpportunityAuditRecord:
    return OpportunityAuditRecord(
        opportunity_key=row["opportunity_key"],
        asset=row["asset"],
        fiat=row["fiat"],
        venue_buy=row["venue_buy"],
        venue_sell=row["venue_sell"],
        buy_price=float(row["buy_price"]),
        sell_price=float(row["sell_price"]),
        gross_spread_pct=float(row["gross_spread_pct"]),
        net_spread_pct=float(row["net_spread_pct"]),
        risk_level=row["risk_level"],
        shown_at_ms=int(row["shown_at_ms"]),
        realised_at_ms=int(row["realised_at_ms"]) if row["realised_at_ms"] is not None else None,
        realised_spread_pct=(
            float(row["realised_spread_pct"]) if row["realised_spread_pct"] is not None else None
        ),
        status=row["status"],
    )


async def load_pending_audit_records(
    *,
    now_ms: int | None = None,
    backcheck_delay_min: int | None = None,
    limit: int = 100,
    db_path: str | None = None,
) -> list[OpportunityAuditRecord]:
    """Pending records whose ``shown_at_ms`` is older than the backcheck delay."""
    path = db_path or DB_PATH
    when_now = now_ms or _now_ms()
    delay_min = backcheck_delay_min if backcheck_delay_min is not None else get_backcheck_delay_min()
    cutoff_ms = when_now - delay_min * 60 * 1000

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, opportunity_key, asset, fiat, venue_buy, venue_sell,
                   buy_price, sell_price, gross_spread_pct, net_spread_pct,
                   risk_level, shown_at_ms, realised_at_ms,
                   realised_spread_pct, status
            FROM p2p_audit_log
            WHERE status = ? AND shown_at_ms <= ?
            ORDER BY shown_at_ms ASC
            LIMIT ?
            """,
            (STATUS_PENDING, cutoff_ms, limit),
        ) as cur:
            rows = await cur.fetchall()
    out: list[OpportunityAuditRecord] = []
    for row in rows:
        rec = _row_to_record(row)
        # We carry the rowid through a separate channel via ``opportunity_key``
        # but updates need primary id; we re-query inside mark functions to avoid
        # tracking it here.
        out.append(rec)
    return out


async def load_recent_audit_records(
    *,
    limit: int = 50,
    db_path: str | None = None,
) -> list[OpportunityAuditRecord]:
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, opportunity_key, asset, fiat, venue_buy, venue_sell,
                   buy_price, sell_price, gross_spread_pct, net_spread_pct,
                   risk_level, shown_at_ms, realised_at_ms,
                   realised_spread_pct, status
            FROM p2p_audit_log
            ORDER BY shown_at_ms DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_record(row) for row in rows]


# ─── Update ──────────────────────────────────────────────────────────────────


async def mark_audit_record_resolved(
    *,
    opportunity_key: str,
    shown_at_ms: int,
    status: str,
    realised_spread_pct: float | None,
    realised_at_ms: int | None = None,
    db_path: str | None = None,
) -> bool:
    """Update the most recent matching pending record."""
    path = db_path or DB_PATH
    when = realised_at_ms or _now_ms()
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            """
            UPDATE p2p_audit_log
            SET status = ?, realised_spread_pct = ?, realised_at_ms = ?
            WHERE opportunity_key = ? AND shown_at_ms = ? AND status = ?
            """,
            (status, realised_spread_pct, when, opportunity_key, shown_at_ms, STATUS_PENDING),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def cleanup_old_audit_records(
    *,
    retention_days: int | None = None,
    db_path: str | None = None,
) -> int:
    """Delete records older than retention. Returns rows deleted."""
    path = db_path or DB_PATH
    days = retention_days if retention_days is not None else get_retention_days()
    cutoff_ms = _now_ms() - days * 24 * 60 * 60 * 1000
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "DELETE FROM p2p_audit_log WHERE shown_at_ms < ?",
            (cutoff_ms,),
        )
        await db.commit()
        return int(cur.rowcount or 0)


# ─── Backcheck pass ──────────────────────────────────────────────────────────


FetchAdsFn = Callable[..., Awaitable[tuple[list[P2PAdvert], list[P2PAdvert], tuple[str, ...], str]]]
"""Signature: ``async def fetch_p2p_ads(*, asset, fiat, pay_types)``."""


async def run_audit_backcheck_pass(
    *,
    fetch_p2p_ads: FetchAdsFn,
    now_ms: int | None = None,
    db_path: str | None = None,
    price_tolerance_pct: float | None = None,
    decay_threshold_pct: float | None = None,
    expire_after_min: int | None = None,
    backcheck_delay_min: int | None = None,
) -> dict[str, int]:
    """Pull all pending records past the delay window, fetch current orderbook
    once per (asset, fiat) combo, classify each record.

    Returns counters: ``{"checked": N, "confirmed": ..., "decayed": ..., ...}``.
    """
    when_now = now_ms or _now_ms()
    expire_min = expire_after_min if expire_after_min is not None else max(
        24 * 60, get_backcheck_delay_min() * 4
    )

    pending = await load_pending_audit_records(
        now_ms=when_now,
        backcheck_delay_min=backcheck_delay_min,
        db_path=db_path,
    )
    if not pending:
        return {"checked": 0}

    # Group by (asset, fiat) — one orderbook fetch per group.
    grouped: dict[tuple[str, str], list[OpportunityAuditRecord]] = {}
    for rec in pending:
        grouped.setdefault((rec.asset, rec.fiat), []).append(rec)

    counters: dict[str, int] = {"checked": 0}

    for (asset, fiat), records in grouped.items():
        try:
            buy_ads, sell_ads, _errors, _source = await fetch_p2p_ads(
                asset=asset, fiat=fiat, pay_types=()
            )
        except Exception as exc:
            logger.warning("p2p audit: fetch failed for %s/%s: %s", asset, fiat, exc)
            buy_ads, sell_ads = [], []

        for rec in records:
            # If the original opportunity is *very* stale, mark expired rather
            # than backcheck — pricing has long since drifted.
            age_min = (when_now - rec.shown_at_ms) / 60_000
            if age_min > expire_min:
                ok = await mark_audit_record_resolved(
                    opportunity_key=rec.opportunity_key,
                    shown_at_ms=rec.shown_at_ms,
                    status=STATUS_EXPIRED,
                    realised_spread_pct=None,
                    realised_at_ms=when_now,
                    db_path=db_path,
                )
                if ok:
                    counters["checked"] = counters.get("checked", 0) + 1
                    counters[STATUS_EXPIRED] = counters.get(STATUS_EXPIRED, 0) + 1
                continue

            result = compute_realised_spread(
                rec,
                current_buy_ads=buy_ads,
                current_sell_ads=sell_ads,
                price_tolerance_pct=price_tolerance_pct,
                decay_threshold_pct=decay_threshold_pct,
            )
            ok = await mark_audit_record_resolved(
                opportunity_key=rec.opportunity_key,
                shown_at_ms=rec.shown_at_ms,
                status=result.status,
                realised_spread_pct=result.realised_spread_pct,
                realised_at_ms=when_now,
                db_path=db_path,
            )
            if ok:
                counters["checked"] = counters.get("checked", 0) + 1
                counters[result.status] = counters.get(result.status, 0) + 1

    return counters


# ─── Scheduler loop ──────────────────────────────────────────────────────────


async def p2p_audit_loop(
    *,
    fetch_p2p_ads: FetchAdsFn,
    stop_event: asyncio.Event,
    interval_seconds: int | None = None,
) -> None:
    """Forever-running coroutine. Fires backcheck pass on a fixed cadence."""
    interval = interval_seconds if interval_seconds is not None else get_backcheck_interval_min() * 60
    logger.info("📊 P2P self-audit loop started (interval=%ds)", interval)

    while not stop_event.is_set():
        try:
            counters = await run_audit_backcheck_pass(fetch_p2p_ads=fetch_p2p_ads)
            if counters.get("checked", 0) > 0:
                logger.info("p2p audit pass: %s", counters)
        except Exception:
            logger.exception("p2p audit pass crashed")

        # Daily cleanup
        try:
            now_utc = datetime.now(timezone.utc)
            # Once per day at the first tick after 03:00 UTC
            if now_utc.hour == 3 and now_utc.minute < 5:
                deleted = await cleanup_old_audit_records()
                if deleted:
                    logger.info("p2p audit retention cleanup: deleted %d rows", deleted)
        except Exception:
            logger.exception("p2p audit retention cleanup crashed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("📊 P2P self-audit loop stopped")


# ─── Stats / formatting ──────────────────────────────────────────────────────


async def get_audit_stats(
    *,
    limit: int = 100,
    db_path: str | None = None,
) -> tuple[list[OpportunityAuditRecord], ThresholdAdjustmentRecommendation]:
    records = await load_recent_audit_records(limit=limit, db_path=db_path)
    recommendation = recommend_threshold_adjustment(records)
    return records, recommendation


async def format_audit_report(*, limit: int = 100, db_path: str | None = None) -> str:
    records, recommendation = await get_audit_stats(limit=limit, db_path=db_path)
    return format_audit_summary(records, recommendation=recommendation)


__all__ = [
    "cleanup_old_audit_records",
    "ensure_audit_table_exists",
    "format_audit_report",
    "get_audit_stats",
    "load_pending_audit_records",
    "load_recent_audit_records",
    "mark_audit_record_resolved",
    "p2p_audit_loop",
    "persist_opportunities_for_audit",
    "persist_opportunity_for_audit",
    "run_audit_backcheck_pass",
]
