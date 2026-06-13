"""
refactor/handlers/subscription_handler.py — VIP subscription & payment handler.

Commands:
  /premium — show VIP status + pay button (CryptoBot)
  Callback: sub:pay — create CryptoBot invoice
  Callback: sub:check — check payment status

Note: команда `/subscribe` (legacy) — это выбор времени авто-рассылки
дайджеста, она живёт в main.py и НЕ относится к платной подписке.

Paywall decorator:
  @require_vip — wraps any handler; if user is not VIP, shows pay button instead.
"""

from __future__ import annotations

import functools
import logging
from typing import Callable

from aiogram import Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)

router = Router(name="subscription")


def _pay_keyboard(pay_url: str | None = None) -> InlineKeyboardMarkup:
    """Build subscription keyboard with pay button."""
    buttons = []
    if pay_url:
        buttons.append([InlineKeyboardButton(
            text="💎 Оплатить подписку",
            url=pay_url,
        )])
    buttons.append([InlineKeyboardButton(
        text="🔄 Проверить оплату",
        callback_data="sub:check",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💎 Купить VIP", callback_data="sub:pay"),
    ]])


@router.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    """Show VIP subscription status / payment entry point."""
    from payments.db import get_vip_info

    user_id = message.from_user.id
    info = await get_vip_info(user_id)

    if not info.get("pg_enabled"):
        await message.answer("⚙️ Система подписок в процессе настройки.")
        return

    if info.get("is_vip"):
        end = info.get("subscription_end")
        end_str = end.strftime("%d.%m.%Y") if end else "∞"
        await message.answer(
            f"💎 *VIP активен*\n"
            f"Действует до: {end_str}\n\n"
            f"Спасибо за поддержку!",
            parse_mode="Markdown",
        )
    elif info.get("trial_active"):
        from payments.crypto_pay import SUB_PRICE_AMOUNT, SUB_PRICE_ASSET, SUB_DAYS

        t_end = info.get("trial_end")
        t_str = t_end.strftime("%d.%m.%Y") if t_end else ""
        await message.answer(
            f"🎁 *Бесплатный пробный период активен*\n"
            f"Действует до: {t_str}\n\n"
            f"После окончания — подписка *{SUB_PRICE_AMOUNT} {SUB_PRICE_ASSET}* / {SUB_DAYS} дней.\n"
            f"Можешь оформить заранее кнопкой ниже:",
            parse_mode="Markdown",
            reply_markup=_status_keyboard(),
        )
    else:
        from payments.crypto_pay import SUB_PRICE_AMOUNT, SUB_PRICE_ASSET, SUB_DAYS

        await message.answer(
            f"🔒 *Подписка VIP*\n\n"
            f"Стоимость: *{SUB_PRICE_AMOUNT} {SUB_PRICE_ASSET}* / {SUB_DAYS} дней\n\n"
            f"Что входит:\n"
            f"• 📊 Ежедневный AI-дайджест\n"
            f"• 🎯 Авто-сигналы (/signal)\n"
            f"• 🧠 Дебаты ИИ-агентов\n"
            f"• 📋 Торговый план\n\n"
            f"Нажми кнопку ниже для оплаты через CryptoBot:",
            parse_mode="Markdown",
            reply_markup=_status_keyboard(),
        )


@router.callback_query(lambda c: c.data == "sub:pay")
async def cb_pay(callback: CallbackQuery) -> None:
    """Create invoice and send pay link."""
    from payments.crypto_pay import create_invoice, is_enabled

    if not is_enabled():
        await callback.answer("⚙️ Платёжная система в процессе настройки", show_alert=True)
        return

    await callback.answer("Создаю счёт...")
    user_id = callback.from_user.id
    pay_url = await create_invoice(user_id)

    if not pay_url:
        await callback.message.answer("❌ Не удалось создать счёт. Попробуй позже.")
        return

    from payments.crypto_pay import SUB_PRICE_AMOUNT, SUB_PRICE_ASSET, SUB_DAYS

    await callback.message.answer(
        f"💳 *Счёт создан*\n\n"
        f"Сумма: *{SUB_PRICE_AMOUNT} {SUB_PRICE_ASSET}*\n"
        f"Период: {SUB_DAYS} дней\n\n"
        f"Нажми кнопку ниже — откроется CryptoBot для оплаты.\n"
        f"После оплаты нажми «Проверить оплату».",
        parse_mode="Markdown",
        reply_markup=_pay_keyboard(pay_url),
    )


@router.callback_query(lambda c: c.data == "sub:check")
async def cb_check_payment(callback: CallbackQuery) -> None:
    """Check if user has paid (poll recent invoices)."""
    from payments.crypto_pay import get_paid_invoices, is_enabled, SUB_DAYS
    from payments.db import grant_vip, check_vip

    if not is_enabled():
        await callback.answer("⚙️ Платёжная система не настроена", show_alert=True)
        return

    await callback.answer("Проверяю оплату...")
    user_id = callback.from_user.id

    # Already VIP?
    if await check_vip(user_id):
        await callback.message.answer("💎 У тебя уже активна VIP подписка!")
        return

    # Check recent paid invoices for this user_id in payload.
    paid = await get_paid_invoices(count=50)
    found = False
    for inv in paid:
        payload = str(inv.get("payload") or "")
        if payload == str(user_id) and inv.get("status") == "paid":
            new_end = await grant_vip(user_id, SUB_DAYS)
            if new_end:
                await callback.message.answer(
                    f"✅ *Оплата подтверждена!*\n\n"
                    f"💎 VIP активирован до {new_end.strftime('%d.%m.%Y')}\n"
                    f"Спасибо за поддержку!",
                    parse_mode="Markdown",
                )
                found = True
                break

    if not found:
        await callback.message.answer(
            "⏳ Оплата пока не найдена.\n\n"
            "Если ты уже оплатил — подожди 1-2 минуты и нажми «Проверить» снова.\n"
            "Если нет — нажми «Оплатить подписку».",
            reply_markup=_status_keyboard(),
        )


def require_vip(handler: Callable) -> Callable:
    """Decorator: blocks users without access with a paywall message.

    Access = active paid VIP **or** active free trial **or** admin. We delegate
    to :func:`payments.db.has_access` (the single source of truth shared with
    the global ``SubscriptionMiddleware``) rather than the trial-blind
    ``check_vip`` — otherwise trial users pass the global paywall yet get
    blocked on every ``@require_vip`` handler, which is exactly the
    inconsistency this fixes.

    Usage:
        @require_vip
        async def cmd_daily(message: Message):
            ...
    """
    @functools.wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        from payments.db import has_access

        user_id = message.from_user.id
        if await has_access(user_id):
            return await handler(message, *args, **kwargs)

        from payments.crypto_pay import SUB_PRICE_AMOUNT, SUB_PRICE_ASSET

        await message.answer(
            f"🔒 *Эта функция доступна только VIP подписчикам*\n\n"
            f"Стоимость: *{SUB_PRICE_AMOUNT} {SUB_PRICE_ASSET}* в месяц\n"
            f"Подробнее: /premium",
            parse_mode="Markdown",
            reply_markup=_status_keyboard(),
        )
    return wrapper


def register(dp: Dispatcher) -> None:
    """Register subscription handlers on the dispatcher."""
    dp.include_router(router)
    logger.info("Subscription handler registered")
