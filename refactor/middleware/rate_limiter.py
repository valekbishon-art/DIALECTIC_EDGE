"""Per-user, per-command rate limiter for aiogram v3.

Problem: heavy commands (``/daily``, ``/markets``, ``/analyze`` …) burn the
free-tier quota of every AI provider in seconds if a user spams them. The bot
shares a single set of LLM keys, so even one spam loop can wedge it for hours.

Solution: lightweight aiogram middleware with **two** independent guards:

1. **Per-(user, command) cooldown** — a sliding window for heavy commands
   (``/daily``, ``/markets`` …). Stops re-running the same expensive command.
2. **Global per-user flood cap** — at most ``N`` events (messages *and*
   inline callbacks) per rolling minute, regardless of content. This is the
   real anti-spam net: it catches the cases the per-command cooldown misses.

Why the global cap matters — the button-bypass hole
---------------------------------------------------
The persistent reply-keyboard buttons send plain **text** ("📊 Прогноз"), not
``/daily``. The per-command cooldown only sees slash-commands, so a user could
mash the *buttons* and bypass it entirely — burning the shared LLM quota. Two
defences close this:
  * ``BUTTON_COMMANDS`` maps known button labels back to their heavy command so
    the per-command cooldown applies to button taps too.
  * The global flood cap covers *everything* else (unknown buttons, inline
    callbacks, raw text) so nothing can spam the bot uncapped.

Design notes:

* **Single-tenant-ish**: in-memory dicts are fine for this bot's scale. No
  Redis. Process restart resets the limiter (acceptable).
* **Soft block**: on a hit we send a polite "wait Ns" notice and silently drop
  the update — we never call the next handler, never ban.
* **Configurable** via env (no code change to tune):
    - ``FEATURE_RATE_LIMITER``        — ``0`` disables completely (default ``1``)
    - ``RATE_LIMITER_WINDOW_SEC``     — per-command window in s (default ``30``)
    - ``RATE_LIMITER_COMMANDS``       — csv of heavy bare-command names
      (default: ``daily,markets,analyze,research,audit,why,starttrade,stop``)
    - ``RATE_LIMITER_MAX_PER_MIN``    — global cap per user (default ``20``)
    - ``RATE_LIMITER_FLOOD_WINDOW_SEC`` — flood window in s (default ``60``)

The middleware never touches torgovaya / scheduler / signal_trader logic. It
only short-circuits a Telegram update before the handler runs.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)

DEFAULT_HEAVY_COMMANDS = (
    "daily",
    "markets",
    "analyze",
    "research",
    "audit",
    "why",
    "starttrade",
    "stop",
)

# Persistent reply-keyboard labels → the heavy command they trigger, so taps on
# the buttons hit the same per-command cooldown as the slash command. Keep in
# sync with PERSISTENT_BTN_* in main.py (only the heavy ones need mapping).
BUTTON_COMMANDS = {
    "📊 Прогноз": "daily",
    "🏛 Рынки": "markets",
}


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _is_truthy(val: Optional[str]) -> bool:
    if not val:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _extract_bare_command(text: Optional[str]) -> Optional[str]:
    """Return the bare command name (no slash, no @bot, no args) or ``None``.

    Examples:
        '/daily'         → 'daily'
        '/daily BTC'     → 'daily'
        '/daily@MyBot'   → 'daily'
        '/Markets'       → 'markets'
        'plain text'     → None
        ''               → None
        None             → None
    """
    if not text:
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    tail = text[1:]
    parts = tail.split(maxsplit=1)
    if not parts:
        return None
    head = parts[0].split("@", 1)[0]
    return head.lower() or None


class RateLimitMiddleware(BaseMiddleware):
    """Sliding-window limiter keyed by ``(user_id, command)``.

    Stores last-trigger monotonic timestamps in an in-memory dict.
    """

    def __init__(
        self,
        *,
        window_sec: Optional[int] = None,
        heavy_commands: Optional[tuple[str, ...]] = None,
        max_per_window: Optional[int] = None,
        flood_window_sec: Optional[int] = None,
        enabled: Optional[bool] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = (
            enabled if enabled is not None else _is_truthy(os.getenv("FEATURE_RATE_LIMITER", "1"))
        )
        self._window = window_sec if window_sec is not None else _env_int(
            "RATE_LIMITER_WINDOW_SEC", 30
        )
        self._heavy = heavy_commands if heavy_commands is not None else _env_csv(
            "RATE_LIMITER_COMMANDS", DEFAULT_HEAVY_COMMANDS
        )
        self._max_per_window = max_per_window if max_per_window is not None else _env_int(
            "RATE_LIMITER_MAX_PER_MIN", 20
        )
        self._flood_window = flood_window_sec if flood_window_sec is not None else _env_int(
            "RATE_LIMITER_FLOOD_WINDOW_SEC", 60
        )
        self._clock = clock
        # {(user_id, command): last_trigger_monotonic_ts}
        self._last: dict[tuple[int, str], float] = {}
        # {user_id: [event monotonic ts, …]} for the global flood cap.
        self._events: dict[int, list[float]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def window_sec(self) -> int:
        return self._window

    @property
    def heavy_commands(self) -> tuple[str, ...]:
        return self._heavy

    @property
    def max_per_window(self) -> int:
        return self._max_per_window

    @property
    def flood_window_sec(self) -> int:
        return self._flood_window

    def _check(self, user_id: int, command: str) -> Optional[int]:
        """Per-command cooldown. If allowed: update timestamp, return ``None``.
        If blocked: return remaining seconds (int, always >= 1).
        """
        now = self._clock()
        key = (user_id, command)
        last = self._last.get(key)
        if last is not None:
            elapsed = now - last
            if elapsed < self._window:
                remaining = self._window - elapsed
                return _ceil_seconds(remaining)
        self._last[key] = now
        return None

    def _check_flood(self, user_id: int) -> Optional[int]:
        """Global per-user flood cap over a rolling window. If under the cap:
        record the event, return ``None``. If at/over the cap: return the
        seconds until the oldest event ages out (does NOT record, so a blocked
        user isn't pushed further back).
        """
        now = self._clock()
        bucket = self._events.setdefault(user_id, [])
        cutoff = now - self._flood_window
        # Drop events that have aged out of the window.
        while bucket and bucket[0] <= cutoff:
            bucket.pop(0)
        if len(bucket) >= self._max_per_window:
            retry = self._flood_window - (now - bucket[0])
            return _ceil_seconds(retry)
        bucket.append(now)
        return None

    @staticmethod
    def _resolve_command(text: Optional[str]) -> Optional[str]:
        """Heavy command from a slash-command OR a known persistent button."""
        cmd = _extract_bare_command(text)
        if cmd is not None:
            return cmd
        if text is not None:
            return BUTTON_COMMANDS.get(text.strip())
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._enabled:
            return await handler(event, data)

        # Guard messages and inline callbacks; everything else passes through.
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        user = event.from_user
        if user is None:
            return await handler(event, data)

        # ── 1) Per-command cooldown (heavy commands + their buttons) ──────────
        if isinstance(event, Message):
            command = self._resolve_command(event.text or event.caption)
            if command is not None and command in self._heavy:
                remaining = self._check(user.id, command)
                if remaining is not None:
                    logger.info(
                        "rate_limiter: user_id=%s command=%s cooldown (%ds left)",
                        user.id, command, remaining,
                    )
                    await self._notify(
                        event,
                        f"⏱ Подожди {remaining} сек — команда /{command} только что выполнялась.",
                    )
                    return None

        # ── 2) Global flood cap (messages + callbacks, any content) ───────────
        flood = self._check_flood(user.id)
        if flood is not None:
            logger.info(
                "rate_limiter: user_id=%s flood-capped (%ds left)", user.id, flood,
            )
            await self._notify(
                event,
                f"⏱ Слишком много запросов — подожди {flood} сек.",
            )
            return None

        return await handler(event, data)

    async def _notify(self, event: TelegramObject, text: str) -> None:
        """Best-effort soft-block notice for Message or CallbackQuery."""
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=False)
            else:
                await event.reply(text)
        except Exception as exc:  # noqa: BLE001 — notice is best-effort
            logger.warning("rate_limiter: failed to send wait notice: %s", exc)


def _ceil_seconds(remaining: float) -> int:
    """Round a positive seconds-remaining up to an int >= 1."""
    return max(1, int(remaining) + (1 if remaining - int(remaining) > 0 else 0))
