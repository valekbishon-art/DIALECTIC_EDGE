"""Rigorous tests for the unified paywall access rule (``payments.db.has_access``)
and the trial-aware ``@require_vip`` decorator.

Background — the bug this guards against:
    The bot had *two* paywalls that disagreed. The global
    ``SubscriptionMiddleware`` (via ``ensure_access``) honours the free trial,
    but the per-handler ``@require_vip`` decorator called ``check_vip`` which is
    trial-blind. Net effect: trial users passed the global gate yet were blocked
    on every ``@require_vip`` handler (``/markets``, ``/screener`` …) while
    ungated handlers (``/carry``, ``/arb``) worked. ``has_access`` is now the
    single source of truth (admin OR active VIP OR active trial) and
    ``require_vip`` delegates to it.

No real DB, network or Telegram. The DB session is mocked; ``has_access`` row
tests require SQLAlchemy (skipped on the unit-fast CI subset).
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import sqlalchemy  # noqa: F401
    HAS_SQLALCHEMY = True
except ImportError:  # unit-fast CI installs minimal deps without sqlalchemy
    HAS_SQLALCHEMY = False

try:
    import aiogram  # noqa: F401
    HAS_AIOGRAM = True
except ImportError:
    HAS_AIOGRAM = False

NOW = datetime.now(timezone.utc)
FUTURE = NOW + timedelta(days=2)
PAST = NOW - timedelta(days=2)


class _FakeSession:
    """Async-context-manager stand-in for an SQLAlchemy session.

    ``execute().one_or_none()`` returns the row we were primed with.
    """

    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        res = MagicMock()
        res.one_or_none.return_value = self._row
        return res


def _patch_session(row):
    """Patch ``payments.db._get_session`` so ``await _get_session()`` yields a
    fake session returning ``row`` (a tuple ``(is_vip, sub_end, trial_end)`` or
    ``None``)."""
    return patch("payments.db._get_session",
                 new=AsyncMock(return_value=_FakeSession(row)))


class TestHasAccessNoDb(unittest.IsolatedAsyncioTestCase):
    """Admin bypass and graceful degradation need no SQLAlchemy."""

    async def test_admin_always_has_access(self):
        import payments.db as db
        orig_admin = os.environ.get("ADMIN_IDS", "")
        orig_url = db.DATABASE_URL
        try:
            os.environ["ADMIN_IDS"] = "777"
            db.DATABASE_URL = "postgresql://x/y"  # paywall on, but admin wins
            self.assertTrue(await db.has_access(777))
        finally:
            if orig_admin:
                os.environ["ADMIN_IDS"] = orig_admin
            else:
                os.environ.pop("ADMIN_IDS", None)
            db.DATABASE_URL = orig_url

    async def test_no_postgres_open(self):
        import payments.db as db
        orig = db.DATABASE_URL
        db.DATABASE_URL = ""  # paywall disabled → everyone passes
        try:
            self.assertTrue(await db.has_access(12345))
        finally:
            db.DATABASE_URL = orig


@unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy not installed (unit-fast subset)")
class TestHasAccessRows(unittest.IsolatedAsyncioTestCase):
    """The core access matrix, exercised against mocked DB rows."""

    def setUp(self):
        import payments.db as db
        self.db = db
        self._orig_url = db.DATABASE_URL
        self._orig_admin = os.environ.get("ADMIN_IDS", "")
        db.DATABASE_URL = "postgresql://x/y"  # paywall enabled
        os.environ.pop("ADMIN_IDS", None)     # user under test is NOT admin

    def tearDown(self):
        self.db.DATABASE_URL = self._orig_url
        if self._orig_admin:
            os.environ["ADMIN_IDS"] = self._orig_admin
        else:
            os.environ.pop("ADMIN_IDS", None)

    async def _access(self, row):
        with _patch_session(row):
            return await self.db.has_access(42)

    async def test_vip_active(self):
        # is_vip=True, subscription not expired
        self.assertTrue(await self._access((True, FUTURE, None)))

    async def test_vip_no_expiry(self):
        # is_vip=True, sub_end NULL (lifetime/admin-granted)
        self.assertTrue(await self._access((True, None, None)))

    async def test_vip_expired(self):
        self.assertFalse(await self._access((True, PAST, None)))

    async def test_trial_active_is_the_bug_fix(self):
        # is_vip=False but trial still running → MUST have access.
        self.assertTrue(await self._access((False, None, FUTURE)))

    async def test_trial_expired(self):
        self.assertFalse(await self._access((False, None, PAST)))

    async def test_vip_false_but_trial_active(self):
        # Even with is_vip flag False, an active trial grants access.
        self.assertTrue(await self._access((False, PAST, FUTURE)))

    async def test_expired_vip_but_trial_active(self):
        # Paid sub lapsed, trial still valid → access (belt & suspenders).
        self.assertTrue(await self._access((True, PAST, FUTURE)))

    async def test_unknown_user_no_row(self):
        self.assertFalse(await self._access(None))

    async def test_no_vip_no_trial(self):
        self.assertFalse(await self._access((False, None, None)))

    async def test_blocked_overrides_active_vip(self):
        # blocked=True must kill access even for a paying VIP.
        self.assertFalse(await self._access((True, FUTURE, None, True)))

    async def test_blocked_overrides_active_trial(self):
        # blocked=True must kill access even within the free trial.
        self.assertFalse(await self._access((False, None, FUTURE, True)))

    async def test_not_blocked_4tuple_still_grants(self):
        # Explicit blocked=False 4-tuple behaves like the legacy 3-tuple.
        self.assertTrue(await self._access((True, FUTURE, None, False)))

    async def test_trial_disabled_kills_active_trial(self):
        # trial_end in the future but trial_disabled=True → no access.
        # Row layout: (is_vip, sub_end, trial_end, blocked, trial_disabled)
        self.assertFalse(await self._access((False, None, FUTURE, False, True)))

    async def test_trial_disabled_does_not_touch_vip(self):
        # trial_disabled only kills the trial; an active VIP still has access.
        self.assertTrue(await self._access((True, FUTURE, None, False, True)))

    async def test_trial_enabled_5tuple_still_grants(self):
        # trial_disabled=False 5-tuple keeps the trial working.
        self.assertTrue(await self._access((False, None, FUTURE, False, False)))

    async def test_db_error_fails_open(self):
        # A transient DB outage must never lock paying users out.
        with patch("payments.db._get_session",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            self.assertTrue(await self.db.has_access(42))


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class TestRequireVipUsesHasAccess(unittest.IsolatedAsyncioTestCase):
    """The decorator must gate on has_access (trial-aware), not check_vip."""

    def _msg(self, uid: int = 5):
        m = MagicMock()
        m.from_user = MagicMock(id=uid)
        m.answer = AsyncMock()
        return m

    async def test_trial_user_passes(self):
        from refactor.handlers.subscription_handler import require_vip
        called = {"n": 0}

        @require_vip
        async def handler(message):
            called["n"] += 1
            return "ok"

        msg = self._msg()
        with patch("payments.db.has_access", new=AsyncMock(return_value=True)):
            result = await handler(msg)
        self.assertEqual(result, "ok")
        self.assertEqual(called["n"], 1)
        msg.answer.assert_not_called()

    async def test_no_access_user_blocked_with_paywall(self):
        from refactor.handlers.subscription_handler import require_vip
        called = {"n": 0}

        @require_vip
        async def handler(message):
            called["n"] += 1

        msg = self._msg()
        with patch("payments.db.has_access", new=AsyncMock(return_value=False)):
            await handler(msg)
        self.assertEqual(called["n"], 0)        # handler NOT run
        msg.answer.assert_called_once()         # paywall message shown

    async def test_decorator_delegates_to_has_access_not_check_vip(self):
        """Regression guard: require_vip must call has_access, never the
        trial-blind check_vip."""
        from refactor.handlers.subscription_handler import require_vip

        @require_vip
        async def handler(message):
            return "ok"

        msg = self._msg()
        with patch("payments.db.has_access", new=AsyncMock(return_value=True)) as ha, \
                patch("payments.db.check_vip", new=AsyncMock(return_value=False)) as cv:
            await handler(msg)
        ha.assert_awaited_once()
        cv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
