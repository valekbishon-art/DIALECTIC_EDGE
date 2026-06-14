"""Task 5 (Andrey): digest is generated 1×/day and cached; `force` (fresh
re-generation) is ADMIN-ONLY. Ordinary users always get the daily cache —
this protects against abuse and token spend on expensive LLM runs.

aiogram-gated; ``import main`` is safe (config.BOT_TOKEN has a placeholder).
"""
from __future__ import annotations

import unittest

try:
    import aiogram  # noqa: F401
    HAS_AIOGRAM = True
except ImportError:
    HAS_AIOGRAM = False


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class TestDigestForceGate(unittest.TestCase):
    def setUp(self):
        import main
        from refactor.handlers.admin_handler import ADMIN_IDS, setup_admins
        self.main = main
        self._saved = set(ADMIN_IDS)
        setup_admins([4242])  # extends the admin set
        self.admin_id = 4242
        self.user_id = 777

    def tearDown(self):
        from refactor.handlers import admin_handler
        admin_handler.ADMIN_IDS.clear()
        admin_handler.ADMIN_IDS.update(self._saved)

    def test_admin_can_force(self):
        self.assertTrue(self.main._resolve_force_fresh(self.admin_id, True))

    def test_admin_without_request_no_force(self):
        # Даже админ форсит только когда явно попросил.
        self.assertFalse(self.main._resolve_force_fresh(self.admin_id, False))

    def test_nonadmin_cannot_force(self):
        # Главное требование: обычный юзер НЕ может форснуть свежую генерацию.
        self.assertFalse(self.main._resolve_force_fresh(self.user_id, True))

    def test_nonadmin_without_request_no_force(self):
        self.assertFalse(self.main._resolve_force_fresh(self.user_id, False))


if __name__ == "__main__":
    unittest.main()
