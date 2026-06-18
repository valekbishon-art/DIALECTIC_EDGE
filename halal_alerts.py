"""halal_alerts.py — спот-автоалерты по смене режима тренда.

Раз в несколько часов считаем:
  • крипто: какие монеты из CRYPTO_UNIVERSE сейчас выше SMA (аптренд → держать спот);
  • акции: топ по моментуму среди тех, кто в аптренде.
Сравниваем с прошлым состоянием (app_kv). Если состав изменился — шлём подписчикам
короткий алерт с диплинками на биржи/график. Только спот/лонг, без деривативов.

Первый прогон состояние молча сохраняет (без спама). Не инвестиционный совет.
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import halal_signals as hs
import links
from database import kv_get, kv_set

logger = logging.getLogger(__name__)

_KEY_CRYPTO = "halal_alert_crypto_hold"
_KEY_STOCKS = "halal_alert_stock_top"


def _alert_kb() -> InlineKeyboardMarkup:
    """Кнопки под алертом: оставить подписку или отключить автоалерты."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔔 Оставить", callback_data="halert_keep"),
        InlineKeyboardButton(text="🔕 Отключить", callback_data="halert_off"),
    ]])


async def _current_crypto_hold(sma: int = 50) -> set[str]:
    uni = list(hs.CRYPTO_UNIVERSE)
    results = await asyncio.gather(*[hs.fetch_closes(f"{c}-USD", "1y") for c in uni])
    hold = set()
    for coin, closes in zip(uni, results):
        if not closes:
            continue
        up, _ = hs.trend_extension(closes, sma)
        if up:
            hold.add(coin)
    return hold


async def _current_stock_top(sma: int = 50, top: int = 8) -> set[str]:
    try:
        from stock_screener import WATCHLIST
    except Exception:  # noqa: BLE001
        WATCHLIST = {}
    symbols = list(WATCHLIST.keys()) or ["AAPL", "MSFT", "GOOGL", "NVDA", "AMD"]
    results = await asyncio.gather(*[hs.fetch_closes(s, "1y") for s in symbols])
    rows = []
    for sym, closes in zip(symbols, results):
        if not closes:
            continue
        up, _ = hs.trend_extension(closes, sma)
        mom = hs.momentum(closes, 126)
        if up and mom is not None:
            rows.append((sym, mom))
    rows.sort(key=lambda r: r[1], reverse=True)
    return {sym for sym, _ in rows[:top]}


def _load(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except Exception:  # noqa: BLE001
        return set()


def _crypto_block(entered: set[str], exited: set[str]) -> list[str]:
    lines = []
    if entered:
        lines.append("🟢 *В аптренд* (покупать спот равным весом):")
        for c in sorted(entered):
            lines.append(f"   • *{c}*  {links.crypto_line(c, prefix='')}")
    if exited:
        lines.append("🔴 *Вышли из тренда* (в стейбл): " + ", ".join(sorted(exited)))
    return lines


def _stock_block(entered: set[str], exited: set[str]) -> list[str]:
    lines = []
    if entered:
        lines.append("🟢 *Вошли в топ силы:*")
        for s in sorted(entered):
            lines.append(f"   • *{s}*  {links.stock_line(s, prefix='')}")
    if exited:
        lines.append("🔴 *Вышли из топа:* " + ", ".join(sorted(exited)))
    return lines


def build_alert_text(c_new: set[str], c_old: set[str],
                     s_new: set[str], s_old: set[str]) -> str | None:
    """Текст алерта по diff'у. None — если изменений нет."""
    c_in, c_out = c_new - c_old, c_old - c_new
    s_in, s_out = s_new - s_old, s_old - s_new
    if not any([c_in, c_out, s_in, s_out]):
        return None
    parts = ["🔔 *Смена тренда — спот*", ""]
    if c_in or c_out:
        parts.append("*Крипто (SMA50):*")
        parts += _crypto_block(c_in, c_out)
        parts.append("")
    if s_in or s_out:
        parts.append("*Акции (моментум-топ):*")
        parts += _stock_block(s_in, s_out)
        parts.append("")
    parts.append("_Правило: выше SMA → держим спот равным весом; ниже → в стейбл. "
                 "Без плеча. Не инвест-совет. Команды: /trend, /stocks._")
    parts.append("")
    parts.append("🔔 *Нужны такие автоалерты?* Оставь или отключи кнопкой ниже "
                 "(включить обратно: /alerts).")
    return "\n".join(parts)


class HalalAlertSystem:
    """Считает режим тренда и шлёт подписчикам алерт при смене состава."""

    def __init__(self, bot):
        self.bot = bot

    async def check_and_alert(self, subscribers: list[dict]) -> int:
        c_new, s_new = await asyncio.gather(
            _current_crypto_hold(50), _current_stock_top(50, 8)
        )
        # пустой результат = сеть легла; не трогаем состояние, не спамим
        if not c_new and not s_new:
            return 0

        c_old = _load(await kv_get(_KEY_CRYPTO))
        s_old = _load(await kv_get(_KEY_STOCKS))
        first_run = (await kv_get(_KEY_CRYPTO)) is None

        await kv_set(_KEY_CRYPTO, json.dumps(sorted(c_new)))
        await kv_set(_KEY_STOCKS, json.dumps(sorted(s_new)))

        if first_run:
            logger.info("Halal alerts: baseline state saved (no spam on first run)")
            return 0

        text = build_alert_text(c_new, c_old, s_new, s_old)
        if not text or not subscribers:
            return 0

        sent = 0
        for sub in subscribers:
            uid = sub.get("user_id")
            if not uid:
                continue
            try:
                await self.bot.send_message(
                    uid, text, parse_mode="Markdown", disable_web_page_preview=True,
                    reply_markup=_alert_kb(),
                )
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Halal alert send failed for {uid}: {e}")
        return sent
