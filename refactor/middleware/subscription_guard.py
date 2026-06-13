"""Bot-wide subscription paywall for aiogram v3.

Why a middleware (not per-handler ``@require_vip``)
---------------------------------------------------
``@require_vip`` only guarded 8 handlers in ``main.py``; every newer handler
(btc, debate, funding, pump, market, p2p, advisor …) was wide open. Decorating
each one is fragile — easy to forget on the next feature. This middleware closes
**everything** centrally: one place, no leaks.

Behaviour
---------
* Runs on every ``Message`` and ``CallbackQuery``.
* A small **whitelist** of commands/callbacks stays free so users can always
  navigate and pay: ``/start``, ``/help``, ``/premium``, ``/id`` and the
  ``sub:*`` payment callbacks.
* **Free trial**: a brand-new user is auto-granted a ``TRIAL_DAYS`` (default 3)
  trial on first contact and gets a one-time welcome notice.
* Active subscription **or** active trial → request passes through.
* Otherwise → a paywall message is sent and the update is dropped.
* Admins (``ADMIN_IDS``) always pass. If Postgres is off, the paywall is
  disabled (fail-open) — same graceful-degradation contract as ``check_vip``.

Config (env, no code change):
    FEATURE_PAYWALL   — ``0`` disables the paywall entirely (default ``1``)
    TRIAL_DAYS        — free-trial length in days (default ``3``, ``0`` = off)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

logger = logging.getLogger(__name__)

# Commands that must stay reachable WITHOUT a subscription.
FREE_COMMANDS = {"start", "help", "premium", "id"}
# Callback prefixes that must stay free (payment flow).
FREE_CALLBACK_PREFIXES = ("sub:",)


def _is_truthy(val: Optional[str]) -> bool:
    return bool(val) and val.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _bare_command(text: Optional[str]) -> Optional[str]:
    """'/premium@Bot arg' -> 'premium'; non-command -> None."""
    if not text:
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    head = text[1:].split(maxsplit=1)[0].split("@", 1)[0]
    return head.lower() or None


def _pay_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💎 Купить доступ", callback_data="sub:pay"),
    ]])


class SubscriptionMiddleware(BaseMiddleware):
    """Bot-wide paywall + free-trial enforcement."""

    def __init__(self, *, enabled: Optional[bool] = None) -> None:
        self._enabled = (
            enabled if enabled is not None
            else _is_truthy(os.getenv("FEATURE_PAYWALL", "1"))
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _is_whitelisted(self, event: TelegramObject) -> bool:
        if isinstance(event, Message):
            cmd = _bare_command(event.text or event.caption)
            return cmd in FREE_COMMANDS
        if isinstance(event, CallbackQuery):
            data = event.data or ""
            return any(data.startswith(p) for p in FREE_CALLBACK_PREFIXES)
        return False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._enabled:
            return await handler(event, data)

        # Only guard Message / CallbackQuery; everything else passes.
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)

        user = event.from_user
        if user is None:
            return await handler(event, data)

        from payments.db import ensure_access

        info = await ensure_access(user.id, user.username or "")

        # One-time welcome when the free trial is first created.
        if info.get("trial_started"):
            await self._send_trial_welcome(event, info)

        if info.get("access"):
            return await handler(event, data)

        # Whitelisted commands stay reachable even for expired users so they can
        # read /premium and pay.
        if self._is_whitelisted(event):
            return await handler(event, data)

        # Blocked — show paywall and DROP the update.
        logger.info("paywall: user_id=%s blocked (reason=%s)", user.id, info.get("reason"))
        await self._send_paywall(event)
        return None

    async def _send_trial_welcome(self, event: TelegramObject, info: dict) -> None:
        end = info.get("trial_end")
        end_str = end.strftime("%d.%m.%Y") if end else ""
        text = (
            "👋 *Добро пожаловать в Dialectic Edge!*\n\n"
            f"🎁 Тебе активирован *бесплатный пробный период* до {end_str}.\n"
            "Доступны все функции бота. После окончания — оформи подписку командой /premium."
        )
        try:
            target = event.message if isinstance(event, CallbackQuery) else event
            if target is not None:
                await target.answer(text, parse_mode="Markdown")
        except Exception as exc:  # noqa: BLE001
            logger.warning("paywall: trial welcome failed: %s", exc)

    async def _send_paywall(self, event: TelegramObject) -> None:
        try:
            from payments.crypto_pay import SUB_PRICE_AMOUNT, SUB_PRICE_ASSET, SUB_DAYS
            amount, asset, days = SUB_PRICE_AMOUNT, SUB_PRICE_ASSET, SUB_DAYS
        except Exception:  # noqa: BLE001
            amount, asset, days = "30", "USDT", 30
        text = (
            "🔒 *Доступ к боту платный*\n\n"
            f"Стоимость: *{amount} {asset}* / {days} дней\n"
            "Пробный период закончился.\n\n"
            "Оформи подписку через CryptoBot — /premium"
        )
        try:
            if isinstance(event, CallbackQuery):
                await event.answer("🔒 Нужна подписка — /premium", show_alert=True)
                if event.message is not None:
                    await event.message.answer(text, parse_mode="Markdown",
                                               reply_markup=_pay_keyboard())
            else:
                await event.answer(text, parse_mode="Markdown",
                                   reply_markup=_pay_keyboard())
        except Exception as exc:  # noqa: BLE001
            logger.warning("paywall: failed to send notice: %s", exc)
