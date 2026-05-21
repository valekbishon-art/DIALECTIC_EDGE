"""Per-user, per-command rate limiter for aiogram v3.

Problem: heavy commands (``/daily``, ``/markets``, ``/analyze`` …) burn the
free-tier quota of every AI provider in seconds if a user spams them. The bot
shares a single set of LLM keys, so even one spam loop can wedge it for hours.

Solution: lightweight aiogram middleware that enforces a sliding window per
``(user_id, command)`` pair. Non-heavy messages are passed through untouched.

Design notes:

* **Single-tenant**: this bot is owner-only, so an in-memory dict is fine.
  No need for Redis. Process restart resets the limiter (acceptable).
* **Soft block**: when a user hits the limit, we send a polite "wait Ns"
  message and silently drop the update — we do NOT call the next handler.
* **Configurable** via env (no code change to tune):
    - ``FEATURE_RATE_LIMITER``    — ``0`` disables completely (default ``1``)
    - ``RATE_LIMITER_WINDOW_SEC`` — window in seconds (default ``30``)
    - ``RATE_LIMITER_COMMANDS``   — csv list of bare command names
      (default: ``daily,markets,analyze,research,audit,why,starttrade,stop``)

The middleware never touches torgovaya / scheduler / signal_trader logic. It
only short-circuits a Telegram update before the handler runs.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

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
        self._clock = clock
        # {(user_id, command): last_trigger_monotonic_ts}
        self._last: dict[tuple[int, str], float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def window_sec(self) -> int:
        return self._window

    @property
    def heavy_commands(self) -> tuple[str, ...]:
        return self._heavy

    def _check(self, user_id: int, command: str) -> Optional[int]:
        """If allowed: update timestamp, return ``None``.
        If blocked: return remaining seconds (int, always >= 1).
        """
        now = self._clock()
        key = (user_id, command)
        last = self._last.get(key)
        if last is not None:
            elapsed = now - last
            if elapsed < self._window:
                remaining = self._window - elapsed
                return max(1, int(remaining) + (1 if remaining - int(remaining) > 0 else 0))
        self._last[key] = now
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._enabled:
            return await handler(event, data)

        # Only intercept Message events. Callbacks / inline / etc. pass through.
        if not isinstance(event, Message):
            return await handler(event, data)

        command = _extract_bare_command(event.text or event.caption)
        if command is None or command not in self._heavy:
            return await handler(event, data)

        user = event.from_user
        if user is None:
            return await handler(event, data)

        remaining = self._check(user.id, command)
        if remaining is None:
            return await handler(event, data)

        # Blocked — send polite message and DROP the update.
        logger.info(
            "rate_limiter: user_id=%s command=%s blocked (%ds left)",
            user.id, command, remaining,
        )
        try:
            await event.reply(
                f"⏱ Подожди {remaining} сек — команда /{command} только что выполнялась."
            )
        except Exception as exc:  # noqa: BLE001 — reply is best-effort
            logger.warning("rate_limiter: failed to send wait notice: %s", exc)
        return None
