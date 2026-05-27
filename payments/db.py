"""
payments/db.py — PostgreSQL models + async CRUD for subscriptions & cached digests.

Uses SQLAlchemy 2.0 async API with asyncpg driver.
All operations are no-op when DATABASE_URL is not set (graceful degradation).

Tables:
  - vip_users: Telegram user_id, VIP status, subscription expiry.
  - daily_digests: Pre-generated digest cache (one row per date).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ─── Lazy engine / session (created on first use) ────────────────────────────

_engine = None
_async_session_factory = None


def _is_enabled() -> bool:
    return bool(DATABASE_URL)


def _admin_ids() -> set[int]:
    """Parse `ADMIN_IDS` env (comma-separated). Empty -> empty set."""
    raw = os.getenv("ADMIN_IDS", "")
    if not raw:
        return set()
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def _is_admin(user_id: int) -> bool:
    return user_id in _admin_ids()


def _normalize_url(url: str) -> tuple[str, dict]:
    """Convert a libpq-style PostgreSQL URL to one asyncpg/SQLAlchemy accept.

    - Forces the `postgresql+asyncpg://` scheme.
    - Strips libpq-only query params (`sslmode`, `gssencmode`, `channel_binding`)
      that asyncpg doesn't understand — `asyncpg.connect()` rejects them with
      `unexpected keyword argument 'sslmode'`. Neon-style URLs typically carry
      `?sslmode=require`; we translate that to `connect_args={"ssl": "require"}`.

    Returns the cleaned URL and a `connect_args` dict to pass to
    `create_async_engine(..., connect_args=...)`.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlparse(url)
    connect_args: dict = {}
    kept: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k == "sslmode":
            if v == "disable":
                connect_args["ssl"] = False
            elif v in ("require", "verify-ca", "verify-full"):
                connect_args["ssl"] = v
            else:
                connect_args["ssl"] = True
        elif k in ("gssencmode", "channel_binding"):
            continue
        else:
            kept.append((k, v))

    cleaned = urlunparse(parsed._replace(query=urlencode(kept)))
    return cleaned, connect_args


async def _get_engine():
    global _engine
    if _engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        url, connect_args = _normalize_url(DATABASE_URL)
        _engine = create_async_engine(
            url,
            echo=False,
            pool_size=5,
            max_overflow=2,
            connect_args=connect_args,
        )
    return _engine


async def _get_session():
    global _async_session_factory
    if _async_session_factory is None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = await _get_engine()
        _async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _async_session_factory()


# ─── Table definitions (raw SQL — no ORM mapping overhead) ───────────────────

_CREATE_VIP_USERS = """
CREATE TABLE IF NOT EXISTS vip_users (
    user_id             BIGINT PRIMARY KEY,
    username            TEXT DEFAULT '',
    is_vip              BOOLEAN DEFAULT FALSE,
    subscription_end    TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
"""

_CREATE_DAILY_DIGESTS = """
CREATE TABLE IF NOT EXISTS daily_digests (
    id              SERIAL PRIMARY KEY,
    digest_date     DATE UNIQUE NOT NULL,
    digest_text     TEXT NOT NULL,
    short_report    TEXT DEFAULT '',
    market_regime   TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
"""


async def init_postgres() -> bool:
    """Create tables if they don't exist. Returns True on success."""
    if not _is_enabled():
        logger.info("DATABASE_URL not set — PostgreSQL disabled")
        return False
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(text(_CREATE_VIP_USERS))
            await conn.execute(text(_CREATE_DAILY_DIGESTS))
        logger.info("PostgreSQL tables ready (vip_users, daily_digests)")
        return True
    except Exception as e:
        logger.error("PostgreSQL init failed: %s", e)
        return False


# ─── VIP user CRUD ───────────────────────────────────────────────────────────

async def upsert_vip_user(user_id: int, username: str = "") -> None:
    """Insert or update user record (does NOT grant VIP)."""
    if not _is_enabled():
        return
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO vip_users (user_id, username)
                    VALUES (:uid, :uname)
                    ON CONFLICT (user_id) DO UPDATE SET username = :uname
                """),
                {"uid": user_id, "uname": username},
            )
            await session.commit()
    except Exception as e:
        logger.warning("upsert_vip_user(%s) failed: %s", user_id, e)


async def grant_vip(user_id: int, days: int = 30) -> Optional[datetime]:
    """Grant VIP for N days. Returns new expiry datetime or None on failure."""
    if not _is_enabled():
        return None
    try:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        new_end = now + timedelta(days=days)
        async with await _get_session() as session:
            # If user already has active VIP, extend from current end date.
            row = await session.execute(
                text("SELECT subscription_end FROM vip_users WHERE user_id = :uid"),
                {"uid": user_id},
            )
            existing = row.scalar_one_or_none()
            if existing and existing > now:
                new_end = existing + timedelta(days=days)

            await session.execute(
                text("""
                    INSERT INTO vip_users (user_id, is_vip, subscription_end)
                    VALUES (:uid, TRUE, :end)
                    ON CONFLICT (user_id) DO UPDATE
                        SET is_vip = TRUE, subscription_end = :end
                """),
                {"uid": user_id, "end": new_end},
            )
            await session.commit()
        logger.info("VIP granted: user=%s until %s", user_id, new_end)
        return new_end
    except Exception as e:
        logger.error("grant_vip(%s) failed: %s", user_id, e)
        return None


async def check_vip(user_id: int) -> bool:
    """Check if user has active VIP subscription.

    Admins (`ADMIN_IDS` env) always pass without DB lookup.
    """
    if _is_admin(user_id):
        return True
    if not _is_enabled():
        return True  # No paywall when Postgres is off
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            row = await session.execute(
                text("""
                    SELECT is_vip, subscription_end FROM vip_users
                    WHERE user_id = :uid
                """),
                {"uid": user_id},
            )
            result = row.one_or_none()
            if not result:
                return False
            is_vip, sub_end = result
            if not is_vip:
                return False
            if sub_end and sub_end < datetime.now(timezone.utc):
                # Expired — revoke
                await session.execute(
                    text("UPDATE vip_users SET is_vip = FALSE WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                await session.commit()
                return False
            return True
    except Exception as e:
        logger.warning("check_vip(%s) failed: %s — defaulting to True", user_id, e)
        return True  # Fail-open: don't block users on DB errors


async def get_vip_info(user_id: int) -> dict:
    """Get VIP status details for display.

    Admins (`ADMIN_IDS` env) always shown as VIP with no expiry.
    """
    if _is_admin(user_id):
        return {
            "is_vip": True,
            "subscription_end": None,
            "pg_enabled": _is_enabled(),
            "is_admin": True,
        }
    if not _is_enabled():
        return {"is_vip": True, "subscription_end": None, "pg_enabled": False}
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            row = await session.execute(
                text("SELECT is_vip, subscription_end FROM vip_users WHERE user_id = :uid"),
                {"uid": user_id},
            )
            result = row.one_or_none()
            if not result:
                return {"is_vip": False, "subscription_end": None, "pg_enabled": True}
            return {
                "is_vip": result[0] and (
                    result[1] is None or result[1] > datetime.now(timezone.utc)
                ),
                "subscription_end": result[1],
                "pg_enabled": True,
            }
    except Exception as e:
        logger.warning("get_vip_info(%s) failed: %s", user_id, e)
        return {"is_vip": True, "subscription_end": None, "pg_enabled": True}


# ─── Daily digest cache ─────────────────────────────────────────────────────

async def save_digest(
    digest_date: date,
    digest_text: str,
    short_report: str = "",
    market_regime: str = "",
) -> bool:
    """Cache a pre-generated digest for a given date."""
    if not _is_enabled():
        return False
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO daily_digests (digest_date, digest_text, short_report, market_regime)
                    VALUES (:d, :full, :short, :regime)
                    ON CONFLICT (digest_date) DO UPDATE SET
                        digest_text = :full,
                        short_report = :short,
                        market_regime = :regime,
                        created_at = NOW()
                """),
                {"d": digest_date, "full": digest_text, "short": short_report, "regime": market_regime},
            )
            await session.commit()
        logger.info("Digest cached for %s", digest_date)
        return True
    except Exception as e:
        logger.error("save_digest(%s) failed: %s", digest_date, e)
        return False


async def get_today_digest() -> Optional[dict]:
    """Get cached digest for today. Returns None if not available."""
    if not _is_enabled():
        return None
    try:
        from sqlalchemy import text

        today = date.today()
        async with await _get_session() as session:
            row = await session.execute(
                text("""
                    SELECT digest_text, short_report, market_regime, created_at
                    FROM daily_digests WHERE digest_date = :d
                """),
                {"d": today},
            )
            result = row.one_or_none()
            if not result:
                return None
            return {
                "digest_text": result[0],
                "short_report": result[1],
                "market_regime": result[2],
                "created_at": result[3],
            }
    except Exception as e:
        logger.warning("get_today_digest failed: %s", e)
        return None


async def close_postgres() -> None:
    """Dispose engine on shutdown."""
    global _engine, _async_session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None
