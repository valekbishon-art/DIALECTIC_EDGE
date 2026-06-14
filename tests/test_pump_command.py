"""Tests for the on-demand /pump command and the Pitch→Pump menu swap.

Andrey's task 3: the persistent menu loses the 💎 Питч button (pitch now lives
only in the /start welcome) and gains a 🚀 Памп button + /pump command that runs
the same pump_scanner as the auto-alerts (with the PR #75 integrity guards).

aiogram-gated (CI unit-fast subset has no aiogram). ``import main`` is safe —
config.BOT_TOKEN falls back to a placeholder, same as test_markets_sections.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import aiogram  # noqa: F401
    HAS_AIOGRAM = True
except ImportError:
    HAS_AIOGRAM = False


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class TestPumpMenuWiring(unittest.TestCase):
    def test_pump_button_replaces_pitch_in_persistent_kb(self):
        import main
        kb_text = str(main.persistent_kb())
        self.assertIn("🚀 Памп", kb_text)
        self.assertNotIn("💎 Питч", kb_text)

    def test_button_constants(self):
        import main
        self.assertEqual(main.PERSISTENT_BTN_PUMP, "🚀 Памп")

    def test_cmd_pump_exists(self):
        import main
        self.assertTrue(callable(main.cmd_pump))

    def test_ondemand_limit_env_parsing(self):
        import main
        with patch.dict("os.environ", {"PUMP_ONDEMAND_LIMIT": "3"}, clear=False):
            self.assertEqual(main._pump_ondemand_limit(), 3)
        with patch.dict("os.environ", {"PUMP_ONDEMAND_LIMIT": "0"}, clear=False):
            self.assertEqual(main._pump_ondemand_limit(), 1)  # floored to >= 1
        with patch.dict("os.environ", {"PUMP_ONDEMAND_LIMIT": "junk"}, clear=False):
            self.assertEqual(main._pump_ondemand_limit(), 5)  # default on bad value


def _fake_message(uid: int = 1):
    """Message stub whose .answer returns a deletable notice."""
    msg = MagicMock()
    msg.from_user = MagicMock(id=uid)
    notice = MagicMock()
    notice.delete = AsyncMock()
    msg.answer = AsyncMock(return_value=notice)
    return msg, notice


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class TestCmdPumpHandler(unittest.IsolatedAsyncioTestCase):
    """The handler delegates to scan_pumps/format_pump_alert (already tested)
    and degrades gracefully. require_vip passes here: no DATABASE_URL → paywall
    fails open.
    """

    async def test_no_pumps_message(self):
        import main
        msg, notice = _fake_message()
        with patch("pump_scanner.scan_pumps", new=AsyncMock(return_value=[])), \
                patch("pump_scanner.PumpConfig") as cfg:
            cfg.from_env.return_value = object()
            await main.cmd_pump(msg)
        notice.delete.assert_awaited_once()
        # A "рынок спокоен" notice is sent (last answer call).
        joined = " ".join(str(c.args[0]) for c in msg.answer.call_args_list)
        self.assertIn("спокоен", joined)

    async def test_renders_top_signals(self):
        import main
        msg, notice = _fake_message()
        sigs = [MagicMock(asset=f"C{i}") for i in range(8)]
        with patch("pump_scanner.scan_pumps", new=AsyncMock(return_value=sigs)), \
                patch("pump_scanner.PumpConfig") as cfg, \
                patch("pump_scanner.format_pump_alert", return_value="ALERT") as fmt:
            cfg.from_env.return_value = object()
            await main.cmd_pump(msg)
        # Default limit 5 → 1 header + 5 alerts = 6 answer calls (+ the notice).
        self.assertEqual(fmt.call_count, 5)

    async def test_scan_error_is_caught(self):
        import main
        msg, _ = _fake_message()
        with patch("pump_scanner.scan_pumps",
                   new=AsyncMock(side_effect=RuntimeError("boom"))), \
                patch("pump_scanner.PumpConfig") as cfg:
            cfg.from_env.return_value = object()
            await main.cmd_pump(msg)  # must not raise
        joined = " ".join(str(c.args[0]) for c in msg.answer.call_args_list)
        self.assertIn("Ошибка", joined)


if __name__ == "__main__":
    unittest.main()
