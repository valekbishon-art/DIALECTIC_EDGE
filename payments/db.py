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
    trial_started_at    TIMESTAMPTZ,
    trial_end           TIMESTAMPTZ,
    blocked             BOOLEAN DEFAULT FALSE,
    trial_disabled      BOOLEAN DEFAULT FALSE,
    vip_notified        BOOLEAN DEFAULT FALSE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
"""

# Backward-compat: add columns to a pre-existing vip_users table.
#
# Admin-facing boolean toggles (flip them right in the Neon table editor):
#   blocked         — hard kill-switch; overrides BOTH paid VIP and the trial.
#                     The answer to "I set is_vip=false by hand but the bot
#                     still let the user in" (the trial was still ticking).
#   trial_disabled  — kill THIS user's free trial without touching the
#                     trial_end timestamp. VIP can still be granted on top.
#   is_vip          — flip to TRUE to grant lifetime VIP by hand (leave
#                     subscription_end NULL = never expires). The bot then DMs
#                     the user "you're VIP now" (see run_vip_notifier in main).
#
# Internal (don't touch by hand):
#   vip_notified    — set once we've DM'd the user about their VIP grant, so the
#                     notifier doesn't spam them every poll. Reset to FALSE
#                     automatically when is_vip goes back to FALSE.
_MIGRATE_TRIAL_COLUMNS = (
    "ALTER TABLE vip_users ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ;",
    "ALTER TABLE vip_users ADD COLUMN IF NOT EXISTS trial_end TIMESTAMPTZ;",
    "ALTER TABLE vip_users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE vip_users ADD COLUMN IF NOT EXISTS trial_disabled BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE vip_users ADD COLUMN IF NOT EXISTS vip_notified BOOLEAN DEFAULT FALSE;",
)


def _trial_days() -> int:
    """Free-trial length for new users (env ``TRIAL_DAYS``, default 3, 0 disables)."""
    raw = os.getenv("TRIAL_DAYS", "3")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3

_CREATE_DAILY_DIGESTS = """
CREATE TABLE IF NOT EXISTS daily_digests (
    id              SERIAL PRIMARY KEY,
    digest_date     DATE UNIQUE NOT NULL,
    digest_text     TEXT NOT NULL,
    short_report    TEXT DEFAULT '',
    market_regime   TEXT DEFAULT '',
    broadcast_done  BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

# Idempotent migrations for daily_digests (column added after the table shipped).
#   broadcast_done — set TRUE once the 09:00 MSK digest broadcast went out for
#                    that date. Lets the GitHub-Actions fallback skip sending
#                    when the live bot scheduler already delivered the digest.
_MIGRATE_DIGEST_COLUMNS = [
    "ALTER TABLE daily_digests ADD COLUMN IF NOT EXISTS broadcast_done BOOLEAN DEFAULT FALSE;",
]


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
            for stmt in _MIGRATE_TRIAL_COLUMNS:
                await conn.execute(text(stmt))
            await conn.execute(text(_CREATE_DAILY_DIGESTS))
            for stmt in _MIGRATE_DIGEST_COLUMNS:
                await conn.execute(text(stmt))
        logger.info("PostgreSQL tables ready (vip_users + trial cols, daily_digests)")
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
                    INSERT INTO vip_users (user_id, is_vip, subscription_end, vip_notified)
                    VALUES (:uid, TRUE, :end, TRUE)
                    ON CONFLICT (user_id) DO UPDATE
                        SET is_vip = TRUE, subscription_end = :end, vip_notified = TRUE
                """),
                {"uid": user_id, "end": new_end},
            )
            await session.commit()
        logger.info("VIP granted: user=%s until %s", user_id, new_end)
        return new_end
    except Exception as e:
        logger.error("grant_vip(%s) failed: %s", user_id, e)
        return None


async def revoke_vip(user_id: int) -> bool:
    """Soft-revoke: strip paid VIP *and* the free trial in one shot.

    Sets ``is_vip = FALSE`` and nulls both ``subscription_end`` and
    ``trial_end`` so the user loses access immediately. This is the fix for the
    "I set is_vip=false by hand but the bot still let them in" bug — editing
    ``is_vip`` alone left ``trial_end`` in the future, and ``has_access`` is
    ``vip OR trial``, so the trial kept the door open.

    Does NOT set ``blocked`` — the user can start a fresh trial / pay again
    (use :func:`block_user` for a permanent ban). Returns True if a row was
    updated.
    """
    if not _is_enabled():
        return False
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            res = await session.execute(
                text("""
                    UPDATE vip_users
                    SET is_vip = FALSE, subscription_end = NULL, trial_end = NULL
                    WHERE user_id = :uid
                """),
                {"uid": user_id},
            )
            await session.commit()
        updated = (res.rowcount or 0) > 0
        logger.info("VIP revoked: user=%s (row_found=%s)", user_id, updated)
        return updated
    except Exception as e:
        logger.error("revoke_vip(%s) failed: %s", user_id, e)
        return False


async def block_user(user_id: int, username: str = "") -> bool:
    """Hard-ban: set ``blocked = TRUE`` (overrides VIP *and* trial everywhere).

    Upserts the row so you can pre-ban a user_id that has never messaged the
    bot. ``has_access`` / ``ensure_access`` / ``check_vip`` all short-circuit on
    ``blocked``, so this is the single, reliable kill-switch — no more guessing
    which column to edit in SQL. Returns True on success.
    """
    if not _is_enabled():
        return False
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO vip_users (user_id, username, blocked)
                    VALUES (:uid, :uname, TRUE)
                    ON CONFLICT (user_id) DO UPDATE SET blocked = TRUE
                """),
                {"uid": user_id, "uname": username or ""},
            )
            await session.commit()
        logger.info("User blocked: user=%s", user_id)
        return True
    except Exception as e:
        logger.error("block_user(%s) failed: %s", user_id, e)
        return False


async def unblock_user(user_id: int) -> bool:
    """Lift a hard ban (``blocked = FALSE``). Does NOT restore VIP/trial —
    the user comes back as a plain non-VIP. Returns True if a row was updated.
    """
    if not _is_enabled():
        return False
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            res = await session.execute(
                text("UPDATE vip_users SET blocked = FALSE WHERE user_id = :uid"),
                {"uid": user_id},
            )
            await session.commit()
        updated = (res.rowcount or 0) > 0
        logger.info("User unblocked: user=%s (row_found=%s)", user_id, updated)
        return updated
    except Exception as e:
        logger.error("unblock_user(%s) failed: %s", user_id, e)
        return False


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
                    SELECT is_vip, subscription_end, blocked FROM vip_users
                    WHERE user_id = :uid
                """),
                {"uid": user_id},
            )
            result = row.one_or_none()
            if not result:
                return False
            is_vip, sub_end = result[0], result[1]
            blocked = bool(result[2]) if len(result) > 2 else False
            if blocked:
                return False  # hard kill-switch overrides paid VIP
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


async def has_access(user_id: int) -> bool:
    """Return ``True`` if the user may use a gated feature right now.

    Unlike :func:`check_vip` (paid subscription only), this is the *single
    source of truth* for access and honours the free trial as well:

        admin  →  active paid VIP  →  active free trial  →  access granted.

    This mirrors the access rule enforced by :class:`SubscriptionMiddleware`
    (via :func:`ensure_access`) so the two layers can never disagree. The bug
    this closes: the per-handler ``@require_vip`` decorator used to call
    :func:`check_vip`, which is trial-blind, so trial users were let through
    the global middleware yet blocked on every ``@require_vip`` handler
    (``/markets``, ``/screener`` …) while ungated handlers (``/carry``,
    ``/arb``) worked — an inconsistent paywall.

    Read-only: it never creates a trial (the middleware's
    :func:`ensure_access` already did that on first contact). Fails open on
    DB errors so a transient outage never locks paying users out.
    """
    if _is_admin(user_id):
        return True
    if not _is_enabled():
        return True  # No paywall when Postgres is off
    try:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        async with await _get_session() as session:
            row = await session.execute(
                text("""
                    SELECT is_vip, subscription_end, trial_end, blocked, trial_disabled
                    FROM vip_users WHERE user_id = :uid
                """),
                {"uid": user_id},
            )
            result = row.one_or_none()
        if not result:
            return False
        is_vip_flag, sub_end, trial_end = result[0], result[1], result[2]
        # Newer columns may be absent in legacy rows / mocked tests → default False.
        blocked = bool(result[3]) if len(result) > 3 else False
        trial_disabled = bool(result[4]) if len(result) > 4 else False
        if blocked:
            return False  # hard kill-switch: overrides VIP *and* trial
        vip_active = bool(is_vip_flag) and (sub_end is None or sub_end > now)
        trial_active = (not trial_disabled) and trial_end is not None and trial_end > now
        return vip_active or trial_active
    except Exception as e:
        logger.warning("has_access(%s) failed: %s — fail-open", user_id, e)
        return True  # Fail-open: don't block users on DB errors


async def ensure_access(user_id: int, username: str = "") -> dict:
    """Register the user, start a free trial on FIRST contact, and report access.

    This is the single entry point used by the subscription middleware. It is
    idempotent: the free trial is created exactly once (on the user's very first
    message). Returns a dict::

        {
          "access": bool,          # may use the bot right now?
          "reason": str,           # admin | vip | trial | expired | no_db | error
          "is_vip": bool,          # paid subscription active
          "trial_active": bool,    # within free-trial window
          "subscription_end": datetime | None,
          "trial_end": datetime | None,
          "trial_started": bool,   # True ONLY on the call that created the trial
          "pg_enabled": bool,
        }
    """
    if _is_admin(user_id):
        return {"access": True, "reason": "admin", "is_vip": True,
                "trial_active": False, "subscription_end": None,
                "trial_end": None, "trial_started": False, "pg_enabled": _is_enabled()}
    if not _is_enabled():
        # No paywall when Postgres is off (graceful degradation).
        return {"access": True, "reason": "no_db", "is_vip": True,
                "trial_active": False, "subscription_end": None,
                "trial_end": None, "trial_started": False, "pg_enabled": False}
    try:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        days = _trial_days()
        trial_end = now + timedelta(days=days) if days > 0 else None
        async with await _get_session() as session:
            created_row = await session.execute(
                text("""
                    INSERT INTO vip_users
                        (user_id, username, trial_started_at, trial_end)
                    VALUES (:uid, :uname, :ts, :tend)
                    ON CONFLICT (user_id) DO NOTHING
                    RETURNING user_id
                """),
                {"uid": user_id, "uname": username or "",
                 "ts": now if trial_end else None, "tend": trial_end},
            )
            trial_started = created_row.scalar_one_or_none() is not None

            row = await session.execute(
                text("""
                    SELECT is_vip, subscription_end, trial_end, blocked, trial_disabled
                    FROM vip_users WHERE user_id = :uid
                """),
                {"uid": user_id},
            )
            result = row.one_or_none()
            await session.commit()

        if not result:
            return {"access": False, "reason": "expired", "is_vip": False,
                    "trial_active": False, "subscription_end": None,
                    "trial_end": None, "trial_started": False, "pg_enabled": True}

        is_vip_flag, sub_end, t_end = result[0], result[1], result[2]
        blocked = bool(result[3]) if len(result) > 3 else False
        trial_disabled = bool(result[4]) if len(result) > 4 else False
        if blocked:
            # Hard ban — overrides VIP and trial. No access, distinct reason so
            # the paywall/log can tell a ban apart from a lapsed subscription.
            return {"access": False, "reason": "blocked", "is_vip": False,
                    "trial_active": False, "subscription_end": sub_end,
                    "trial_end": t_end, "trial_started": False, "pg_enabled": True}
        vip_active = bool(is_vip_flag) and (sub_end is None or sub_end > now)
        trial_active = (not trial_disabled) and t_end is not None and t_end > now
        access = vip_active or trial_active
        reason = "vip" if vip_active else "trial" if trial_active else "expired"
        return {"access": access, "reason": reason, "is_vip": vip_active,
                "trial_active": trial_active, "subscription_end": sub_end,
                "trial_end": t_end, "trial_started": trial_started, "pg_enabled": True}
    except Exception as e:
        logger.warning("ensure_access(%s) failed: %s — fail-open", user_id, e)
        return {"access": True, "reason": "error", "is_vip": False,
                "trial_active": False, "subscription_end": None,
                "trial_end": None, "trial_started": False, "pg_enabled": True}


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
                text("SELECT is_vip, subscription_end, trial_end, blocked, trial_disabled "
                     "FROM vip_users WHERE user_id = :uid"),
                {"uid": user_id},
            )
            result = row.one_or_none()
            if not result:
                return {"is_vip": False, "subscription_end": None,
                        "trial_active": False, "trial_end": None,
                        "blocked": False, "trial_disabled": False, "pg_enabled": True}
            now = datetime.now(timezone.utc)
            t_end = result[2]
            blocked = bool(result[3]) if len(result) > 3 else False
            trial_disabled = bool(result[4]) if len(result) > 4 else False
            return {
                "is_vip": result[0] and (result[1] is None or result[1] > now),
                "subscription_end": result[1],
                "trial_active": (not trial_disabled) and t_end is not None and t_end > now,
                "trial_end": t_end,
                "blocked": blocked,
                "trial_disabled": trial_disabled,
                "pg_enabled": True,
            }
    except Exception as e:
        logger.warning("get_vip_info(%s) failed: %s", user_id, e)
        return {"is_vip": True, "subscription_end": None, "pg_enabled": True}


# ─── Manual-VIP notifier support ─────────────────────────────────────────────
#
# Lets the bot react to VIP grants made by hand in the Neon table editor. The
# bot never watches the DB live, so a background loop (run_vip_notifier in
# main.py) polls these helpers every few seconds:
#   1. reset_stale_vip_notifications() — clear the flag for anyone who lost VIP,
#      so a future re-grant notifies again.
#   2. pending_vip_notifications()     — users who are VIP now but haven't been
#      told yet (is_vip flipped on directly in Neon).
#   3. mark_vip_notified(uid)          — after the DM is delivered.
# grant_vip() (the in-bot payment path) pre-sets vip_notified=TRUE, so only
# *manual* edits ever trigger a DM here — no double messages.

async def pending_vip_notifications(limit: int = 50) -> list[dict]:
    """Return users with active VIP that haven't been notified yet.

    Each item: ``{"user_id": int, "subscription_end": datetime | None}``.
    Empty list when Postgres is off or on any error (never raises).
    """
    if not _is_enabled():
        return []
    try:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        async with await _get_session() as session:
            rows = await session.execute(
                text("""
                    SELECT user_id, subscription_end FROM vip_users
                    WHERE is_vip = TRUE
                      AND COALESCE(blocked, FALSE) = FALSE
                      AND COALESCE(vip_notified, FALSE) = FALSE
                      AND (subscription_end IS NULL OR subscription_end > :now)
                    ORDER BY user_id
                    LIMIT :lim
                """),
                {"now": now, "lim": limit},
            )
            return [{"user_id": r[0], "subscription_end": r[1]} for r in rows.all()]
    except Exception as e:
        logger.warning("pending_vip_notifications failed: %s", e)
        return []


async def mark_vip_notified(user_id: int) -> None:
    """Flag a user as already told about their VIP grant (idempotent)."""
    if not _is_enabled():
        return
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            await session.execute(
                text("UPDATE vip_users SET vip_notified = TRUE WHERE user_id = :uid"),
                {"uid": user_id},
            )
            await session.commit()
    except Exception as e:
        logger.warning("mark_vip_notified(%s) failed: %s", user_id, e)


async def reset_stale_vip_notifications() -> int:
    """Clear vip_notified for users who are no longer active VIP.

    Ensures that if VIP is turned off and later on again (by hand or by a new
    payment), the user gets a fresh "you're VIP" DM. Returns rows affected.
    """
    if not _is_enabled():
        return 0
    try:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        async with await _get_session() as session:
            res = await session.execute(
                text("""
                    UPDATE vip_users SET vip_notified = FALSE
                    WHERE COALESCE(vip_notified, FALSE) = TRUE
                      AND (
                            COALESCE(is_vip, FALSE) = FALSE
                            OR (subscription_end IS NOT NULL AND subscription_end <= :now)
                            OR COALESCE(blocked, FALSE) = TRUE
                          )
                """),
                {"now": now},
            )
            await session.commit()
            return res.rowcount or 0
    except Exception as e:
        logger.warning("reset_stale_vip_notifications failed: %s", e)
        return 0


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


async def get_recent_digests(days: int = 14) -> list[dict]:
    """Return the last ``days`` cached digests (newest first) for «База Дайджестов».

    Each item: {digest_date, short_report, market_regime, created_at}.
    The heavy ``digest_text`` is intentionally omitted — the list view only needs
    date + regime; the full text is fetched on demand via :func:`get_digest_by_date`.
    """
    if not _is_enabled():
        return []
    try:
        from sqlalchemy import text

        cutoff = date.today() - timedelta(days=days)
        async with await _get_session() as session:
            rows = await session.execute(
                text("""
                    SELECT digest_date, short_report, market_regime, created_at
                    FROM daily_digests
                    WHERE digest_date >= :cutoff
                    ORDER BY digest_date DESC
                """),
                {"cutoff": cutoff},
            )
            out: list[dict] = []
            for r in rows.fetchall():
                out.append({
                    "digest_date": r[0],
                    "short_report": r[1] or "",
                    "market_regime": r[2] or "",
                    "created_at": r[3],
                })
            return out
    except Exception as e:
        logger.warning("get_recent_digests failed: %s", e)
        return []


async def get_digest_by_date(digest_date: date) -> Optional[dict]:
    """Fetch one cached digest by its date. Returns None if not found."""
    if not _is_enabled():
        return None
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            row = await session.execute(
                text("""
                    SELECT digest_text, short_report, market_regime, created_at
                    FROM daily_digests WHERE digest_date = :d
                """),
                {"d": digest_date},
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
        logger.warning("get_digest_by_date(%s) failed: %s", digest_date, e)
        return None


async def is_digest_broadcast(digest_date: date) -> bool:
    """True if the 09:00 broadcast for ``digest_date`` already went out.

    Used by the GitHub-Actions fallback to avoid double-sending when the live
    bot scheduler already delivered the digest.
    """
    if not _is_enabled():
        return False
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            row = await session.execute(
                text("SELECT broadcast_done FROM daily_digests WHERE digest_date = :d"),
                {"d": digest_date},
            )
            result = row.one_or_none()
            return bool(result and result[0])
    except Exception as e:
        logger.warning("is_digest_broadcast(%s) failed: %s", digest_date, e)
        return False


async def mark_digest_broadcast(digest_date: date) -> bool:
    """Mark the digest for ``digest_date`` as already broadcast (idempotent)."""
    if not _is_enabled():
        return False
    try:
        from sqlalchemy import text

        async with await _get_session() as session:
            await session.execute(
                text("UPDATE daily_digests SET broadcast_done = TRUE WHERE digest_date = :d"),
                {"d": digest_date},
            )
            await session.commit()
        return True
    except Exception as e:
        logger.warning("mark_digest_broadcast(%s) failed: %s", digest_date, e)
        return False


async def list_access_user_ids() -> list[int]:
    """Return user_ids that currently have access: active paid VIP OR active free
    trial. Blocked users and trial_disabled users are excluded.

    This is the recipient set for the 09:00 MSK digest broadcast — i.e. exactly
    the users honoured by :func:`has_access` (premium *or* trial), so users with
    neither receive nothing.
    """
    if not _is_enabled():
        return []
    try:
        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        async with await _get_session() as session:
            rows = await session.execute(
                text("""
                    SELECT user_id FROM vip_users
                    WHERE COALESCE(blocked, FALSE) = FALSE
                      AND (
                            (is_vip = TRUE AND (subscription_end IS NULL OR subscription_end > :now))
                         OR (COALESCE(trial_disabled, FALSE) = FALSE
                             AND trial_end IS NOT NULL AND trial_end > :now)
                      )
                """),
                {"now": now},
            )
            return [int(r[0]) for r in rows.fetchall()]
    except Exception as e:
        logger.warning("list_access_user_ids failed: %s", e)
        return []


async def close_postgres() -> None:
    """Dispose engine on shutdown."""
    global _engine, _async_session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None
