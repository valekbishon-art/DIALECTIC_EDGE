"""Tests for payments module (db + crypto_pay) — no real DB or API calls."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch


class TestPaymentsDbHelpers(unittest.TestCase):
    """Unit tests for payments.db helper functions."""

    def test_is_enabled_without_url(self):
        with patch("payments.db.DATABASE_URL", ""):
            from payments.db import _is_enabled
            # Re-evaluate with patched value
            import payments.db as db_mod
            orig = db_mod.DATABASE_URL
            db_mod.DATABASE_URL = ""
            self.assertFalse(db_mod._is_enabled())
            db_mod.DATABASE_URL = orig

    def test_is_enabled_with_url(self):
        import payments.db as db_mod
        orig = db_mod.DATABASE_URL
        db_mod.DATABASE_URL = "postgresql://localhost/test"
        self.assertTrue(db_mod._is_enabled())
        db_mod.DATABASE_URL = orig


class TestNormalizeUrl(unittest.TestCase):
    """_normalize_url translates libpq-style URLs for asyncpg."""

    def test_neon_url(self):
        from payments.db import _normalize_url
        url, args = _normalize_url(
            "postgresql://u:p@host/db?sslmode=require"
        )
        self.assertTrue(url.startswith("postgresql+asyncpg://"))
        self.assertNotIn("sslmode", url)
        self.assertEqual(args, {"ssl": "require"})

    def test_postgres_prefix(self):
        from payments.db import _normalize_url
        url, args = _normalize_url("postgres://u:p@host/db?sslmode=verify-full")
        self.assertTrue(url.startswith("postgresql+asyncpg://"))
        self.assertEqual(args, {"ssl": "verify-full"})

    def test_no_sslmode(self):
        from payments.db import _normalize_url
        url, args = _normalize_url("postgresql://u:p@host/db")
        self.assertTrue(url.startswith("postgresql+asyncpg://"))
        self.assertEqual(args, {})

    def test_sslmode_disable(self):
        from payments.db import _normalize_url
        _, args = _normalize_url("postgresql://u:p@host/db?sslmode=disable")
        self.assertEqual(args, {"ssl": False})

    def test_drops_channel_binding_keeps_others(self):
        from payments.db import _normalize_url
        url, args = _normalize_url(
            "postgresql://u:p@host/db"
            "?sslmode=require&channel_binding=require&application_name=app"
        )
        self.assertEqual(args, {"ssl": "require"})
        self.assertIn("application_name=app", url)
        self.assertNotIn("channel_binding", url)


class TestCryptoPayHelpers(unittest.TestCase):
    """Unit tests for crypto_pay module."""

    def test_is_enabled_without_token(self):
        import payments.crypto_pay as cp
        orig = cp.CRYPTOBOT_API_TOKEN
        cp.CRYPTOBOT_API_TOKEN = ""
        self.assertFalse(cp.is_enabled())
        cp.CRYPTOBOT_API_TOKEN = orig

    def test_is_enabled_with_token(self):
        import payments.crypto_pay as cp
        orig = cp.CRYPTOBOT_API_TOKEN
        cp.CRYPTOBOT_API_TOKEN = "test-token"
        self.assertTrue(cp.is_enabled())
        cp.CRYPTOBOT_API_TOKEN = orig

    def test_defaults(self):
        import payments.crypto_pay as cp
        self.assertEqual(cp.SUB_PRICE_ASSET, "USDT")
        self.assertTrue(int(cp.SUB_PRICE_AMOUNT) > 0)


class TestCheckVipFallback(unittest.IsolatedAsyncioTestCase):
    """check_vip should return True when Postgres is disabled (no paywall)."""

    async def test_check_vip_no_postgres(self):
        import payments.db as db_mod
        orig = db_mod.DATABASE_URL
        db_mod.DATABASE_URL = ""
        result = await db_mod.check_vip(12345)
        self.assertTrue(result)
        db_mod.DATABASE_URL = orig

    async def test_get_vip_info_no_postgres(self):
        import payments.db as db_mod
        orig = db_mod.DATABASE_URL
        db_mod.DATABASE_URL = ""
        info = await db_mod.get_vip_info(12345)
        self.assertTrue(info["is_vip"])
        self.assertFalse(info["pg_enabled"])
        db_mod.DATABASE_URL = orig

    async def test_save_digest_no_postgres(self):
        import payments.db as db_mod
        orig = db_mod.DATABASE_URL
        db_mod.DATABASE_URL = ""
        result = await db_mod.save_digest(date.today(), "test")
        self.assertFalse(result)
        db_mod.DATABASE_URL = orig

    async def test_get_today_digest_no_postgres(self):
        import payments.db as db_mod
        orig = db_mod.DATABASE_URL
        db_mod.DATABASE_URL = ""
        result = await db_mod.get_today_digest()
        self.assertIsNone(result)
        db_mod.DATABASE_URL = orig

    async def test_upsert_vip_user_no_postgres(self):
        import payments.db as db_mod
        orig = db_mod.DATABASE_URL
        db_mod.DATABASE_URL = ""
        # Should not raise
        await db_mod.upsert_vip_user(12345, "test")
        db_mod.DATABASE_URL = orig

    async def test_grant_vip_no_postgres(self):
        import payments.db as db_mod
        orig = db_mod.DATABASE_URL
        db_mod.DATABASE_URL = ""
        result = await db_mod.grant_vip(12345, 30)
        self.assertIsNone(result)
        db_mod.DATABASE_URL = orig


class TestCryptoPayNoToken(unittest.IsolatedAsyncioTestCase):
    """CryptoPay operations should return None when token is not set."""

    async def test_create_invoice_no_token(self):
        import payments.crypto_pay as cp
        orig = cp.CRYPTOBOT_API_TOKEN
        cp.CRYPTOBOT_API_TOKEN = ""
        result = await cp.create_invoice(12345)
        self.assertIsNone(result)
        cp.CRYPTOBOT_API_TOKEN = orig

    async def test_get_me_no_token(self):
        import payments.crypto_pay as cp
        orig = cp.CRYPTOBOT_API_TOKEN
        cp.CRYPTOBOT_API_TOKEN = ""
        result = await cp.get_me()
        self.assertIsNone(result)
        cp.CRYPTOBOT_API_TOKEN = orig

    async def test_get_paid_invoices_no_token(self):
        import payments.crypto_pay as cp
        orig = cp.CRYPTOBOT_API_TOKEN
        cp.CRYPTOBOT_API_TOKEN = ""
        result = await cp.get_paid_invoices()
        self.assertEqual(result, [])
        cp.CRYPTOBOT_API_TOKEN = orig


_HAS_AIOGRAM = True
try:
    import aiogram  # noqa: F401
except ImportError:
    _HAS_AIOGRAM = False


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram not installed (unit-fast)")
class TestSubscriptionHandlerImports(unittest.TestCase):
    """Smoke test: subscription handler imports without side effects."""

    def test_import(self):
        from refactor.handlers.subscription_handler import register
        self.assertTrue(callable(register))

    def test_require_vip_import(self):
        from refactor.handlers.subscription_handler import require_vip
        self.assertTrue(callable(require_vip))


if __name__ == "__main__":
    unittest.main()
