"""Unit tests for :mod:`refactor.middleware.rate_limiter`.

Coverage:

* Command extraction handles ``/cmd``, ``/cmd args``, ``/cmd@Bot``, case,
  empty / non-command / ``None`` inputs.
* Heavy command from the same user within the window is blocked.
* Heavy command from the same user after the window has elapsed is allowed.
* Different users have independent budgets.
* Different commands have independent budgets per user.
* Non-heavy commands and plain text always pass through.
* ``FEATURE_RATE_LIMITER=0`` disables the limiter completely.
* Env overrides (``RATE_LIMITER_WINDOW_SEC``, ``RATE_LIMITER_COMMANDS``) are honoured.
* When blocked, ``event.reply`` is called and ``handler`` is NOT called.
* When allowed, ``handler`` is called and ``event.reply`` is NOT touched.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# unit-fast CI job ставит минимальный набор зависимостей без aiogram.
# refactor.middleware.rate_limiter импортит aiogram на верхнем уровне, поэтому
# весь модуль тестов нужно гардить — иначе ImportError при `unittest discover`.
# Паттерн повторяет tests/test_funding_handler.py.
try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

if HAS_AIOGRAM:
    from aiogram.types import CallbackQuery, Message

    from refactor.middleware.rate_limiter import (
        BUTTON_COMMANDS,
        DEFAULT_HEAVY_COMMANDS,
        RateLimitMiddleware,
        _extract_bare_command,
        _is_truthy,
    )


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class ExtractCommandTestCase(unittest.TestCase):
    def test_plain_slash_command(self) -> None:
        self.assertEqual(_extract_bare_command("/daily"), "daily")

    def test_command_with_args(self) -> None:
        self.assertEqual(_extract_bare_command("/daily BTC ETH"), "daily")

    def test_command_with_bot_suffix(self) -> None:
        self.assertEqual(_extract_bare_command("/markets@DialecticBot"), "markets")

    def test_command_case_insensitive(self) -> None:
        self.assertEqual(_extract_bare_command("/Daily"), "daily")
        self.assertEqual(_extract_bare_command("/MARKETS"), "markets")

    def test_plain_text_is_none(self) -> None:
        self.assertIsNone(_extract_bare_command("hello there"))
        self.assertIsNone(_extract_bare_command("daily without slash"))

    def test_empty_and_none(self) -> None:
        self.assertIsNone(_extract_bare_command(""))
        self.assertIsNone(_extract_bare_command(None))
        self.assertIsNone(_extract_bare_command("   "))

    def test_just_slash(self) -> None:
        # "/" alone has no name → None (not empty string).
        self.assertIsNone(_extract_bare_command("/"))


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class IsTruthyTestCase(unittest.TestCase):
    def test_truthy(self) -> None:
        for v in ("1", "true", "TRUE", "Yes", "on", "Y", "t"):
            with self.subTest(v=v):
                self.assertTrue(_is_truthy(v))

    def test_falsy(self) -> None:
        for v in ("0", "false", "no", "off", "", "   ", None, "nonsense"):
            with self.subTest(v=v):
                self.assertFalse(_is_truthy(v))


def _make_message(text: str, user_id: int = 100) -> MagicMock:
    """Build a stub aiogram Message with .text, .caption, .from_user.id, .reply.

    Uses ``spec=Message`` so that ``isinstance(msg, Message)`` is ``True``
    inside the middleware.
    """
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.caption = None
    msg.from_user = MagicMock(spec=["id"])
    msg.from_user.id = user_id
    msg.reply = AsyncMock()
    return msg


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class RateLimitMiddlewareCoreTestCase(unittest.IsolatedAsyncioTestCase):
    """Behavioural tests using a fake monotonic clock for deterministic windows."""

    def setUp(self) -> None:
        self.now = [1000.0]  # mutable container for the fake clock

        def clock() -> float:
            return self.now[0]

        self.mw = RateLimitMiddleware(
            window_sec=30,
            heavy_commands=("daily", "markets"),
            enabled=True,
            clock=clock,
        )
        self.handler = AsyncMock(return_value="handler-return")

    async def test_first_heavy_call_passes(self) -> None:
        msg = _make_message("/daily")
        result = await self.mw(self.handler, msg, {})
        self.handler.assert_awaited_once()
        msg.reply.assert_not_called()
        self.assertEqual(result, "handler-return")

    async def test_second_heavy_call_within_window_blocked(self) -> None:
        msg1 = _make_message("/daily")
        msg2 = _make_message("/daily")
        await self.mw(self.handler, msg1, {})
        self.now[0] += 5  # 5 seconds later, still inside 30s window

        result = await self.mw(self.handler, msg2, {})

        # Handler called only once (first call), reply called on the second.
        self.assertEqual(self.handler.await_count, 1)
        msg2.reply.assert_awaited_once()
        # The reply text should mention seconds remaining.
        sent_text = msg2.reply.call_args.args[0]
        self.assertIn("Подожди", sent_text)
        self.assertIn("daily", sent_text)
        self.assertIsNone(result)

    async def test_heavy_call_after_window_passes(self) -> None:
        msg1 = _make_message("/daily")
        msg2 = _make_message("/daily")
        await self.mw(self.handler, msg1, {})
        self.now[0] += 31  # window expired

        await self.mw(self.handler, msg2, {})

        self.assertEqual(self.handler.await_count, 2)
        msg2.reply.assert_not_called()

    async def test_different_users_independent(self) -> None:
        msg_user_a = _make_message("/daily", user_id=100)
        msg_user_b = _make_message("/daily", user_id=200)
        await self.mw(self.handler, msg_user_a, {})
        await self.mw(self.handler, msg_user_b, {})

        # Both should pass — different users → different keys.
        self.assertEqual(self.handler.await_count, 2)
        msg_user_a.reply.assert_not_called()
        msg_user_b.reply.assert_not_called()

    async def test_different_commands_independent_per_user(self) -> None:
        msg_daily = _make_message("/daily", user_id=100)
        msg_markets = _make_message("/markets", user_id=100)
        await self.mw(self.handler, msg_daily, {})
        await self.mw(self.handler, msg_markets, {})

        self.assertEqual(self.handler.await_count, 2)
        msg_daily.reply.assert_not_called()
        msg_markets.reply.assert_not_called()

    async def test_non_heavy_command_always_passes(self) -> None:
        msg = _make_message("/help")  # not in heavy list
        await self.mw(self.handler, msg, {})
        await self.mw(self.handler, msg, {})
        # Both calls go through, no rate limit applied.
        self.assertEqual(self.handler.await_count, 2)
        msg.reply.assert_not_called()

    async def test_plain_text_passes(self) -> None:
        msg = _make_message("hello bot")
        await self.mw(self.handler, msg, {})
        self.handler.assert_awaited_once()
        msg.reply.assert_not_called()

    async def test_disabled_middleware_passes_everything(self) -> None:
        mw = RateLimitMiddleware(
            window_sec=30,
            heavy_commands=("daily",),
            enabled=False,
            clock=lambda: self.now[0],
        )
        msg1 = _make_message("/daily")
        msg2 = _make_message("/daily")
        await mw(self.handler, msg1, {})
        await mw(self.handler, msg2, {})
        # Both pass — limiter is off.
        self.assertEqual(self.handler.await_count, 2)
        msg2.reply.assert_not_called()

    async def test_non_message_event_passes(self) -> None:
        """Callback / inline / poll updates must not be intercepted."""
        not_a_message = MagicMock()  # no spec=Message
        await self.mw(self.handler, not_a_message, {})
        self.handler.assert_awaited_once_with(not_a_message, {})

    async def test_blocked_handler_not_called(self) -> None:
        msg1 = _make_message("/daily")
        msg2 = _make_message("/daily")
        await self.mw(self.handler, msg1, {})
        self.now[0] += 1  # 1 sec later

        await self.mw(self.handler, msg2, {})

        # Handler must have run only for the first call.
        self.assertEqual(self.handler.await_count, 1)

    async def test_reply_failure_does_not_crash(self) -> None:
        """If sending the wait-notice fails, we still drop the update gracefully."""
        msg1 = _make_message("/daily")
        msg2 = _make_message("/daily")
        msg2.reply = AsyncMock(side_effect=RuntimeError("telegram boom"))
        await self.mw(self.handler, msg1, {})

        # Must not raise.
        result = await self.mw(self.handler, msg2, {})

        self.assertIsNone(result)
        self.assertEqual(self.handler.await_count, 1)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class RateLimitEnvOverridesTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_window_from_env(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMITER_WINDOW_SEC": "60"}, clear=False):
            mw = RateLimitMiddleware(
                heavy_commands=("daily",), enabled=True, clock=lambda: 0.0,
            )
        self.assertEqual(mw.window_sec, 60)

    async def test_commands_from_env(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMITER_COMMANDS": "foo, bar , Baz"}, clear=False):
            mw = RateLimitMiddleware(
                window_sec=30, enabled=True, clock=lambda: 0.0,
            )
        # Lowercased and stripped.
        self.assertEqual(mw.heavy_commands, ("foo", "bar", "baz"))

    async def test_default_window_when_env_invalid(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMITER_WINDOW_SEC": "nonsense"}, clear=False):
            mw = RateLimitMiddleware(heavy_commands=("daily",), enabled=True, clock=lambda: 0.0)
        self.assertEqual(mw.window_sec, 30)

    async def test_disabled_via_env(self) -> None:
        with patch.dict("os.environ", {"FEATURE_RATE_LIMITER": "0"}, clear=False):
            mw = RateLimitMiddleware(window_sec=30, heavy_commands=("daily",), clock=lambda: 0.0)
        self.assertFalse(mw.enabled)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class RateLimitDefaultsTestCase(unittest.TestCase):
    def test_default_heavy_commands_match_documented_set(self) -> None:
        # If this changes, update AUTONOMY_ROADMAP and PR description.
        expected = {"daily", "markets", "analyze", "research", "audit", "why", "starttrade", "stop"}
        self.assertEqual(set(DEFAULT_HEAVY_COMMANDS), expected)


def _make_callback(data: str, user_id: int = 100) -> MagicMock:
    """Stub aiogram CallbackQuery with .data, .from_user.id, .answer."""
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = MagicMock(spec=["id"])
    cb.from_user.id = user_id
    cb.answer = AsyncMock()
    return cb


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class ButtonResolutionTestCase(unittest.IsolatedAsyncioTestCase):
    """Persistent reply-keyboard buttons must hit the same per-command cooldown
    as the equivalent slash command (closes the button-bypass hole)."""

    def setUp(self) -> None:
        self.now = [1000.0]
        self.mw = RateLimitMiddleware(
            window_sec=30,
            heavy_commands=("daily", "markets"),
            max_per_window=999,  # disable flood cap for these focused tests
            enabled=True,
            clock=lambda: self.now[0],
        )
        self.handler = AsyncMock(return_value="ok")

    async def test_button_tap_resolves_to_command(self) -> None:
        # "📊 Прогноз" → daily ; second tap inside the window is blocked.
        btn1 = _make_message("📊 Прогноз")
        btn2 = _make_message("📊 Прогноз")
        await self.mw(self.handler, btn1, {})
        self.now[0] += 5
        result = await self.mw(self.handler, btn2, {})
        self.assertEqual(self.handler.await_count, 1)
        btn2.reply.assert_awaited_once()
        self.assertIsNone(result)

    async def test_button_and_slash_share_cooldown(self) -> None:
        # Tapping the 🏛 Рынки button then sending /markets must collide.
        btn = _make_message("🏛 Рынки")
        slash = _make_message("/markets")
        await self.mw(self.handler, btn, {})
        self.now[0] += 1
        await self.mw(self.handler, slash, {})
        self.assertEqual(self.handler.await_count, 1)
        slash.reply.assert_awaited_once()

    async def test_unmapped_button_not_cooled_down(self) -> None:
        # A non-heavy button label is not a heavy command → passes (flood cap
        # is the net for those).
        btn = _make_message("⚙️ Настройки")
        await self.mw(self.handler, btn, {})
        await self.mw(self.handler, btn, {})
        self.assertEqual(self.handler.await_count, 2)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class GlobalFloodCapTestCase(unittest.IsolatedAsyncioTestCase):
    """Per-user global flood cap over a rolling window — the real anti-spam net."""

    def setUp(self) -> None:
        self.now = [1000.0]
        self.mw = RateLimitMiddleware(
            window_sec=0,            # no per-command cooldown interference
            heavy_commands=(),
            max_per_window=5,
            flood_window_sec=60,
            enabled=True,
            clock=lambda: self.now[0],
        )
        self.handler = AsyncMock(return_value="ok")

    async def test_under_cap_passes(self) -> None:
        for _ in range(5):
            self.now[0] += 1
            await self.mw(self.handler, _make_message("hi"), {})
        self.assertEqual(self.handler.await_count, 5)

    async def test_over_cap_blocked(self) -> None:
        for _ in range(5):
            await self.mw(self.handler, _make_message("hi"), {})
        # 6th event within the window is flood-capped.
        blocked = _make_message("hi")
        result = await self.mw(self.handler, blocked, {})
        self.assertEqual(self.handler.await_count, 5)
        blocked.reply.assert_awaited_once()
        self.assertIn("Слишком много", blocked.reply.call_args.args[0])
        self.assertIsNone(result)

    async def test_buttons_count_toward_flood(self) -> None:
        # Mixing plain text + button taps still trips the cap (button-spam).
        for label in ("hi", "📊 Прогноз", "🏛 Рынки", "x", "y"):
            await self.mw(self.handler, _make_message(label), {})
        blocked = _make_message("z")
        await self.mw(self.handler, blocked, {})
        self.assertEqual(self.handler.await_count, 5)
        blocked.reply.assert_awaited_once()

    async def test_inline_callbacks_count_toward_flood(self) -> None:
        for _ in range(5):
            await self.mw(self.handler, _make_callback("cmd:markets"), {})
        blocked = _make_callback("cmd:markets")
        await self.mw(self.handler, blocked, {})
        self.assertEqual(self.handler.await_count, 5)
        blocked.answer.assert_awaited_once()  # callbacks notified via .answer

    async def test_different_users_have_independent_caps(self) -> None:
        for _ in range(5):
            await self.mw(self.handler, _make_message("hi", user_id=1), {})
        # User 2 is unaffected.
        await self.mw(self.handler, _make_message("hi", user_id=2), {})
        self.assertEqual(self.handler.await_count, 6)

    async def test_cap_recovers_after_window(self) -> None:
        for _ in range(5):
            await self.mw(self.handler, _make_message("hi"), {})
        # Jump past the flood window — the bucket ages out, user can act again.
        self.now[0] += 61
        await self.mw(self.handler, _make_message("hi"), {})
        self.assertEqual(self.handler.await_count, 6)

    async def test_blocked_user_not_pushed_further_back(self) -> None:
        # Five events at t=1000. Cap hit. A blocked attempt at t=1030 must not
        # extend the window: at t=1061 (61s after the originals) it's allowed.
        for _ in range(5):
            await self.mw(self.handler, _make_message("hi"), {})
        self.now[0] += 30
        await self.mw(self.handler, _make_message("hi"), {})   # blocked, not recorded
        self.assertEqual(self.handler.await_count, 5)
        self.now[0] += 31  # now t=1061, originals (t=1000) aged out
        await self.mw(self.handler, _make_message("hi"), {})
        self.assertEqual(self.handler.await_count, 6)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast subset)")
class CeilSecondsTestCase(unittest.TestCase):
    def test_ceil_seconds(self) -> None:
        from refactor.middleware.rate_limiter import _ceil_seconds
        self.assertEqual(_ceil_seconds(0.1), 1)
        self.assertEqual(_ceil_seconds(1.0), 1)
        self.assertEqual(_ceil_seconds(1.2), 2)
        self.assertEqual(_ceil_seconds(29.9), 30)

    def test_button_map_targets_are_heavy_by_default(self) -> None:
        # Every mapped button must point at a documented heavy command,
        # otherwise the mapping silently does nothing.
        for label, cmd in BUTTON_COMMANDS.items():
            self.assertIn(cmd, DEFAULT_HEAVY_COMMANDS, f"{label}->{cmd} not heavy")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
