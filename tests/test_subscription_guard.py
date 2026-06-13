"""Tests for the bot-wide subscription paywall middleware + trial logic.

No real Telegram, DB, or network. aiogram objects are built with
``model_construct`` to bypass validation; DB access is mocked.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Message, User

from refactor.middleware.subscription_guard import (
    SubscriptionMiddleware,
    _bare_command,
    _is_truthy,
)


def _msg(text: str | None, uid: int = 1, username: str = "u") -> Message:
    m = Message.model_construct(text=text, caption=None,
                                from_user=User.model_construct(id=uid, username=username))
    return m


def _cb(data: str, uid: int = 1) -> CallbackQuery:
    return CallbackQuery.model_construct(
        data=data, from_user=User.model_construct(id=uid, username="u"), message=None)


class TestHelpers(unittest.TestCase):
    def test_bare_command(self):
        self.assertEqual(_bare_command("/premium"), "premium")
        self.assertEqual(_bare_command("/premium@MyBot arg"), "premium")
        self.assertEqual(_bare_command("/Start"), "start")
        self.assertIsNone(_bare_command("hello"))
        self.assertIsNone(_bare_command(None))

    def test_is_truthy(self):
        for v in ("1", "true", "YES", "on"):
            self.assertTrue(_is_truthy(v))
        for v in ("0", "false", "", None):
            self.assertFalse(_is_truthy(v))


class TestWhitelist(unittest.TestCase):
    def setUp(self):
        self.mw = SubscriptionMiddleware(enabled=True)

    def test_free_commands(self):
        for c in ("/start", "/help", "/premium", "/id"):
            self.assertTrue(self.mw._is_whitelisted(_msg(c)))

    def test_paid_commands(self):
        for c in ("/daily", "/signal", "/pump", "/debate"):
            self.assertFalse(self.mw._is_whitelisted(_msg(c)))

    def test_sub_callbacks_free(self):
        self.assertTrue(self.mw._is_whitelisted(_cb("sub:pay")))
        self.assertTrue(self.mw._is_whitelisted(_cb("sub:check")))
        self.assertFalse(self.mw._is_whitelisted(_cb("pump:more")))


class TestGating(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mw = SubscriptionMiddleware(enabled=True)
        # Don't actually send Telegram messages.
        self.mw._send_paywall = AsyncMock()
        self.mw._send_trial_welcome = AsyncMock()
        self.handler = AsyncMock(return_value="HANDLED")

    async def _run(self, event, access_info):
        with patch("payments.db.ensure_access", AsyncMock(return_value=access_info)):
            return await self.mw(self.handler, event, {})

    async def test_disabled_passes_through(self):
        mw = SubscriptionMiddleware(enabled=False)
        res = await mw(self.handler, _msg("/daily"), {})
        self.assertEqual(res, "HANDLED")
        self.handler.assert_awaited_once()

    async def test_vip_access_allows_paid_command(self):
        info = {"access": True, "reason": "vip", "trial_started": False}
        res = await self._run(_msg("/daily"), info)
        self.assertEqual(res, "HANDLED")
        self.handler.assert_awaited_once()
        self.mw._send_paywall.assert_not_awaited()

    async def test_trial_access_allows_and_no_welcome_when_not_new(self):
        info = {"access": True, "reason": "trial", "trial_started": False}
        res = await self._run(_msg("/pump"), info)
        self.assertEqual(res, "HANDLED")
        self.mw._send_trial_welcome.assert_not_awaited()

    async def test_new_user_gets_welcome(self):
        info = {"access": True, "reason": "trial", "trial_started": True,
                "trial_end": None}
        await self._run(_msg("/start"), info)
        self.mw._send_trial_welcome.assert_awaited_once()
        self.handler.assert_awaited_once()

    async def test_expired_blocks_paid_command(self):
        info = {"access": False, "reason": "expired", "trial_started": False}
        res = await self._run(_msg("/daily"), info)
        self.assertIsNone(res)
        self.handler.assert_not_awaited()
        self.mw._send_paywall.assert_awaited_once()

    async def test_expired_can_still_reach_premium(self):
        info = {"access": False, "reason": "expired", "trial_started": False}
        res = await self._run(_msg("/premium"), info)
        self.assertEqual(res, "HANDLED")
        self.handler.assert_awaited_once()
        self.mw._send_paywall.assert_not_awaited()

    async def test_expired_blocks_callback(self):
        info = {"access": False, "reason": "expired", "trial_started": False}
        res = await self._run(_cb("pump:more"), info)
        self.assertIsNone(res)
        self.mw._send_paywall.assert_awaited_once()


class TestEnsureAccessLogic(unittest.IsolatedAsyncioTestCase):
    async def test_admin_always_access(self):
        import payments.db as db
        with patch.object(db, "_is_admin", return_value=True):
            info = await db.ensure_access(999)
        self.assertTrue(info["access"])
        self.assertEqual(info["reason"], "admin")

    async def test_no_db_fail_open(self):
        import payments.db as db
        with patch.object(db, "_is_admin", return_value=False), \
             patch.object(db, "_is_enabled", return_value=False):
            info = await db.ensure_access(1)
        self.assertTrue(info["access"])
        self.assertEqual(info["reason"], "no_db")

    async def test_trial_days_env(self):
        import payments.db as db
        with patch.dict("os.environ", {"TRIAL_DAYS": "7"}):
            self.assertEqual(db._trial_days(), 7)
        with patch.dict("os.environ", {"TRIAL_DAYS": "bad"}):
            self.assertEqual(db._trial_days(), 3)


if __name__ == "__main__":
    unittest.main()
