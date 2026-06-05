"""
pump_alert.py — push-рассылка памп-алертов (фича ПАМП).

Раз в PUMP_SCAN_INTERVAL_SEC гоним core.pump_scanner.scan_pumps() по всему спот-
рынку (Bybit + MEXC + Binance) и рассылаем подписчикам найденные пампы
с графиком и кнопками на биржи («Биржа MEXC» / «Биржа BYBIT» — как на скрине).

Анти-спам: одну монету не чаще раза в PUMP_COOLDOWN_MIN минут.
Никогда не падает целиком — каждая ошибка логируется и итерация пропускается.
Это информация, НЕ торговый сигнал. Никакой авто-торговли.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 180
DEFAULT_COOLDOWN_MIN = 60


def _env_int(name: str, default: int, *, min_val: int = 0,
             max_val: int = 10 ** 9) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(min_val, min(max_val, v))


def feature_enabled() -> bool:
    raw = os.getenv("FEATURE_PUMP_SCANNER", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_interval_sec() -> int:
    return _env_int("PUMP_SCAN_INTERVAL_SEC", DEFAULT_INTERVAL_SEC,
                    min_val=30, max_val=3600)


def get_cooldown_min() -> int:
    return _env_int("PUMP_COOLDOWN_MIN", DEFAULT_COOLDOWN_MIN,
                    min_val=0, max_val=24 * 60)


@dataclass
class _LastPump:
    asset: str
    pump_pct: float
    fired_at: datetime


def _build_keyboard(sig):
    """InlineKeyboardMarkup с кнопками на биржи. None если aiogram недоступен."""
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:  # pragma: no cover
        return None
    rows = []
    for label, url in sig.venue_buttons():
        rows.append([InlineKeyboardButton(text=label, url=url)])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_chart(sig) -> Optional[bytes]:
    """PNG-байты графика пампа (или None). Non-fatal."""
    try:
        from chart_generator import generate_pump_chart
        buf = generate_pump_chart(
            sig.asset, sig.closes,
            price_from=sig.price_from, price_to=sig.price_to,
            pump_pct=sig.pump_pct, window_min=sig.window_min)
        if buf is None:
            return None
        return buf.getvalue()
    except Exception:
        logger.debug("pump: chart render skipped", exc_info=True)
        return None


class PumpAlertSystem:
    """Фоновая рассылка памп-алертов подписчикам сигналов."""

    def __init__(self, bot):
        self.bot = bot
        self._last: dict[str, _LastPump] = {}

    def _is_fresh(self, asset: str, pump_pct: float, now: datetime,
                  cooldown_min: int) -> bool:
        prev = self._last.get(asset)
        if prev is None:
            return True
        mins = (now - prev.fired_at).total_seconds() / 60.0
        return mins >= cooldown_min

    async def _send_one(self, chat_id: int, sig) -> bool:
        """Отправляет один алерт (фото+капшн либо текст). True при успехе."""
        from core.pump_scanner import format_pump_alert
        text = format_pump_alert(sig)
        kb = _build_keyboard(sig)
        png = _render_chart(sig)
        try:
            if png is not None:
                from aiogram.types import BufferedInputFile
                await self.bot.send_photo(
                    chat_id,
                    photo=BufferedInputFile(png, filename=f"pump_{sig.asset}.png"),
                    caption=text, parse_mode="Markdown", reply_markup=kb)
            else:
                await self.bot.send_message(
                    chat_id, text, parse_mode="Markdown", reply_markup=kb)
            return True
        except Exception as e:
            logger.warning("pump: send error chat %s: %s", chat_id, e)
            return False

    async def check_and_alert(self, subscribers: list) -> int:
        """Одна итерация: скан -> рассылка. Возвращает число отправленных сообщений."""
        if not subscribers or not feature_enabled():
            return 0
        try:
            from core.pump_scanner import PumpConfig, scan_pumps
        except Exception as e:  # pragma: no cover
            logger.warning("pump: import scan failed: %s", e)
            return 0
        try:
            signals = await scan_pumps(cfg=PumpConfig.from_env(), max_symbols=0)
        except Exception as e:
            logger.warning("pump: scan_pumps failed: %s", e)
            return 0
        if not signals:
            return 0

        now = datetime.now()
        cooldown = get_cooldown_min()
        # фильтруем по cooldown и маркируем
        fresh = [s for s in signals if self._is_fresh(s.asset, s.pump_pct, now, cooldown)]
        if not fresh:
            return 0
        for s in fresh:
            self._last[s.asset] = _LastPump(asset=s.asset, pump_pct=s.pump_pct,
                                            fired_at=now)

        sent = 0
        for user in subscribers:
            chat_id = user.get("user_id") if isinstance(user, dict) else user
            if chat_id is None:
                continue
            for s in fresh:
                if await self._send_one(chat_id, s):
                    sent += 1
        if sent:
            logger.info("pump: sent %s alerts (%s coins, %s subs)",
                        sent, len(fresh), len(subscribers))
        return sent


__all__ = [
    "PumpAlertSystem",
    "DEFAULT_COOLDOWN_MIN",
    "DEFAULT_INTERVAL_SEC",
    "feature_enabled",
    "get_cooldown_min",
    "get_interval_sec",
]
