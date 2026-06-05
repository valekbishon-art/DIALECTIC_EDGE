"""Команда /pump — on-demand памп-сканер (фича ПАМП).

Пользователь жмёт /pump — бот прямо сейчас сканирует спот-рынок (Bybit+MEXC+
Binance) и присылает топ найденных пампов с графиком и кнопками на биржи.
Фоновая авто-рассылка живёт отдельно в pump_alert.py + scheduler.py.
"""

from __future__ import annotations

import logging
import os

from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)

MAX_REPLY = 8  # сколько пампов максимум прислать за один /pump


def _feature_enabled() -> bool:
    return os.getenv("FEATURE_PUMP_SCANNER", "0").strip().lower() in {
        "1", "true", "yes", "on"}


def _ondemand_max_symbols() -> int:
    raw = os.getenv("PUMP_ONDEMAND_MAX_SYMBOLS", "400").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 400


def _build_keyboard(sig):
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:  # pragma: no cover
        return None
    rows = [[InlineKeyboardButton(text=label, url=url)]
            for label, url in sig.venue_buttons()]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_signal(message: Message, sig) -> None:
    from core.pump_scanner import format_pump_alert
    text = format_pump_alert(sig)
    kb = _build_keyboard(sig)
    png = None
    try:
        from chart_generator import generate_pump_chart
        buf = generate_pump_chart(
            sig.asset, sig.closes, price_from=sig.price_from,
            price_to=sig.price_to, pump_pct=sig.pump_pct,
            window_min=sig.window_min)
        png = buf.getvalue() if buf is not None else None
    except Exception:
        logger.debug("pump: chart skipped", exc_info=True)
    try:
        if png is not None:
            from aiogram.types import BufferedInputFile
            await message.answer_photo(
                BufferedInputFile(png, filename=f"pump_{sig.asset}.png"),
                caption=text, parse_mode="Markdown", reply_markup=kb)
        else:
            await message.answer(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.warning("pump: /pump send error: %s", e)


async def handle_pump_command(message: Message) -> None:
    # Ручная команда /pump работает всегда (явное действие пользователя).
    # Фоновая авто-рассылка остаётся под флагом (см. pump_alert.feature_enabled).
    try:
        from core.pump_scanner import PumpConfig, scan_pumps
    except Exception as e:
        logger.warning("pump: import failed: %s", e)
        await message.answer("⚠️ Памп-сканер недоступен.")
        return

    status = await message.answer("🔍 Сканирую спот-рынок (Bybit + MEXC + Binance)…")
    try:
        signals = await scan_pumps(
            cfg=PumpConfig.from_env(),
            max_symbols=_ondemand_max_symbols())
    except Exception as e:
        logger.warning("pump: scan failed: %s", e)
        try:
            await status.edit_text("⚠️ Не удалось отсканировать рынок, попробуй позже.")
        except Exception:
            pass
        return

    if not signals:
        try:
            await status.edit_text(
                "😴 Сейчас пампов не найдено (>5% за 30мин + x3 объём, без разогретых).")
        except Exception:
            pass
        return

    top = signals[:MAX_REPLY]
    try:
        await status.edit_text(f"🚀 Найдено пампов: {len(signals)}. Топ-{len(top)}:")
    except Exception:
        pass
    for sig in top:
        await _send_signal(message, sig)


def register_pump_handlers(dp) -> None:
    dp.message.register(handle_pump_command, Command("pump"))


__all__ = ["handle_pump_command", "register_pump_handlers", "MAX_REPLY"]
