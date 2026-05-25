"""
smart_money_alert.py — Алерт при конвергенции smart-money сигналов.

Идея: regular MARKET SIGNALS digest приходит раз в 2 часа независимо от того,
есть ли что показать. Этот модуль шлёт ОТДЕЛЬНЫЙ prominent алерт только
когда ≥2 институциональных индикаторов (Top-trader L/S, Coinbase Premium,
CME Basis, Funding dispersion) одновременно показывают одно направление
с заметной силой — то есть «много крупных трейдеров почти в одно время
зашли в одну сторону».

Источник данных — `market_indicators.smart_money.fetch_smart_money_signals`
+ scoring из `smart_money_score_contribution`.

Анти-спам:
- Не шлём одно и то же направление чаще раз в COOLDOWN_HOURS
- Шлём сразу если direction сменился (например, LONG → SHORT)
- Шлём сразу если score стал заметно сильнее (delta ≥ STRENGTH_BUMP)

Алерт — это информация, НЕ торговый сигнал. Решение за пользователем.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from market_indicators.smart_money import (
    SmartMoneySignals,
    fetch_smart_money_signals,
    smart_money_score_contribution,
)

logger = logging.getLogger(__name__)

# Минимум баллов чтобы считать конвергенцию состоявшейся (range примерно ±7).
# Раньше было 3 — оставляло слишком много false positives (юзер стабильно
# ловил конвергенцию в противоположную рынку сторону на 2-3 дневном горизонте). Порог 4
# требует более жёсткого совпадения всех 4 индикаторов (или 3 из 4 с явным перевесом).
MIN_SCORE = 4

# Минимум независимых категорий-сигналов в одну сторону.
# 3 (было 2) — при 2-х совпадениях частичный сигнал легко ловит случайные корреляции
MIN_REASONS = 3

# Через сколько часов разрешаем повторно слать тот же direction
COOLDOWN_HOURS = 6

# Если score прибавил на столько vs предыдущего — игнорим cooldown
STRENGTH_BUMP = 2

# Горизонт работы smart-money convergence сигнала. Это информация о позиционировании
# крупных игроков ПРЯМО СЕЙЧАС, а не прогноз на недели. На 2-3 дневном
# горизонте исторически бывает ложные срабатывания (smart-money разворачивался),
# поэтому в тексте явно указываем горизонт и предупреждаем.
HORIZON_HOURS = 24


def _format_alert(direction: str, score: int, reasons: list[str], signals: SmartMoneySignals) -> str:
    """Текст алерта для Telegram (Markdown)."""
    if direction == "LONG":
        emoji = "🟢"
        title = "топ-трейдеры и институционалы синхронно идут в *LONG*"
    else:
        emoji = "🔴"
        title = "топ-трейдеры и институционалы синхронно идут в *SHORT*"

    lines = [
        f"{emoji} *SMART-MONEY CONVERGENCE*",
        f"_{datetime.now().strftime('%d.%m.%Y %H:%M UTC')}_",
        "",
        f"BTC: {title}",
        "",
        "📊 *Совпавшие индикаторы:*",
    ]
    for r in reasons:
        lines.append(f"• {r}")

    lines.append("")
    lines.append(f"📈 *Score:* {score:+d} (порог: ≥{MIN_SCORE} или ≤−{MIN_SCORE})")

    lines.extend(
        [
            "",
            f"⏳ *Горизонт:* {HORIZON_HOURS}ч (позиционирование крупных игроков СЕЙЧАС, не прогноз на неделю).",
            "",
            "💡 _Это информация о том, как расположились большие игроки, а не готовый торговый сигнал. "
            "На горизонте 2-3 дней smart-money часто разворачивается — слепо следовать не стоит. "
            "/daily — полный план с верификацией через 4 агента. "
            "Работаем только по проверенным сильным сигналам._",
            "",
            "⚠️ _Я бы сделал так, но это не финансовый совет. DYOR и своими деньгами отвечаешь ты._",
        ]
    )
    return "\n".join(lines)


def _evaluate_convergence(
    score: int, bullish: list[str], bearish: list[str]
) -> tuple[Optional[str], list[str]]:
    """Решает, есть ли конвергенция, и в какую сторону.

    Returns: (direction, reasons) или (None, []).
    """
    if score >= MIN_SCORE and len(bullish) >= MIN_REASONS:
        return "LONG", bullish
    if score <= -MIN_SCORE and len(bearish) >= MIN_REASONS:
        return "SHORT", bearish
    return None, []


class SmartMoneyAlertSystem:
    """Отслеживает конвергенцию smart-money сигналов и шлёт алерты подписчикам."""

    def __init__(self, bot):
        self.bot = bot
        self._last_direction: Optional[str] = None
        self._last_score: int = 0
        self._last_time: Optional[datetime] = None

    def _should_send(self, direction: str, score: int) -> bool:
        now = datetime.now()

        # Смена направления — всегда шлём
        if direction != self._last_direction:
            return True

        # Резкое усиление того же направления — шлём
        if abs(score) >= abs(self._last_score) + STRENGTH_BUMP:
            return True

        # Иначе — соблюдаем cooldown
        if self._last_time is None:
            return True
        hours_passed = (now - self._last_time).total_seconds() / 3600
        return hours_passed >= COOLDOWN_HOURS

    async def check_and_alert(self, subscribers: list[dict]) -> int:
        """Один цикл проверки: fetch → scoring → send (если есть конвергенция).

        Возвращает число отправленных сообщений (0 если не сработало).
        """
        if not subscribers:
            return 0

        try:
            signals = await fetch_smart_money_signals()
        except Exception as e:
            logger.warning(f"smart-money fetch error: {e}")
            return 0

        score, bullish, bearish = smart_money_score_contribution(signals)
        direction, reasons = _evaluate_convergence(score, bullish, bearish)

        logger.info(
            "smart-money check: score=%+d bull=%d bear=%d → %s",
            score, len(bullish), len(bearish), direction or "—",
        )

        if direction is None:
            return 0

        if not self._should_send(direction, score):
            logger.info(
                "smart-money convergence %s suppressed (cooldown / no strength bump)",
                direction,
            )
            return 0

        text = _format_alert(direction, score, reasons, signals)
        sent = 0
        for user in subscribers:
            try:
                await self.bot.send_message(
                    user["user_id"],
                    text,
                    parse_mode="Markdown",
                )
                sent += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"smart-money alert send error user {user['user_id']}: {e}")

        self._last_direction = direction
        self._last_score = score
        self._last_time = datetime.now()
        logger.info(f"✅ smart-money convergence alert sent to {sent} subscribers")
        return sent
