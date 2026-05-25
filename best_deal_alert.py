"""
best_deal_alert.py — push-алерт лучшего setup'а из `core/signal_scorer.rank_signals`.

Идея: команда `/signal` уже даёт top-1 setup по score 0-100, но пользователь должен
сам её нажать. Юзер: «если лучшая сделка набирает свои 60 из 100 очков чтобы она
приходила пользователю сама а не по вызову кнопки».

Этот модуль:
  - Раз в `INTERVAL_SEC` (default 1h) тянет live-prices и пускает `rank_signals`.
  - Если `top` есть (score ≥ DEFAULT_MIN_SCORE=60) — шлёт алерт подписчикам.
  - Анти-спам: пара (asset, direction) не отправляется чаще раз в `COOLDOWN_HOURS`,
    кроме случая когда score прыгнул на ≥SCORE_BUMP пунктов (significant strengthening).
  - Никакой автоматической торговли. Только информация + дисклеймер.

Никогда не падает целиком — каждая ошибка логируется и итерация пропускается.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# Cooldown по паре (asset, direction). 4h — у юзера должно быть время отреагировать
# но не получать сообщение каждый цикл.
DEFAULT_COOLDOWN_HOURS = 4

# Если score вырос на >=N пунктов vs предыдущий алерт по той же паре — игнорим cooldown.
DEFAULT_SCORE_BUMP = 15

# Интервал между проверками. Час — компромисс между «свежо» и «не зашумлять».
DEFAULT_INTERVAL_SEC = 3600


def _env_int(name: str, default: int, *, min_val: int = 0, max_val: int = 10**9) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(min_val, min(max_val, v))


def feature_enabled() -> bool:
    raw = os.getenv("FEATURE_BEST_DEAL_AUTO_PUSH", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_check_interval_sec() -> int:
    return _env_int(
        "BEST_DEAL_ALERT_INTERVAL_SEC",
        DEFAULT_INTERVAL_SEC,
        min_val=300,
        max_val=24 * 3600,
    )


def get_cooldown_hours() -> int:
    return _env_int(
        "BEST_DEAL_ALERT_COOLDOWN_HOURS",
        DEFAULT_COOLDOWN_HOURS,
        min_val=0,
        max_val=72,
    )


def get_score_bump() -> int:
    return _env_int(
        "BEST_DEAL_ALERT_SCORE_BUMP",
        DEFAULT_SCORE_BUMP,
        min_val=0,
        max_val=100,
    )


@dataclass(frozen=True)
class _LastAlert:
    asset: str
    direction: str
    score: int
    fired_at: datetime


def _format_alert(setup) -> str:
    """Рендерит SignalSetup в Telegram-сообщение (Markdown).

    Поля setup: asset, direction (LONG/SHORT), score, entry, stop, target, sigma_1d_pct,
                rr (risk/reward), reason (короткое объяснение).
    """
    direction_emoji = "🟢" if setup.direction == "LONG" else "🔴"
    arrow = "↑" if setup.direction == "LONG" else "↓"
    sigma_str = (
        f" · σ̂₁d={setup.sigma_1d_pct:.2f}%"
        if getattr(setup, "sigma_1d_pct", None) is not None
        else ""
    )
    rr_str = (
        f" · R/R={setup.rr:.2f}"
        if getattr(setup, "rr", None) is not None
        else ""
    )
    reason = getattr(setup, "reason", "") or ""

    lines = [
        f"{direction_emoji} *AUTO-PUSH: лучший setup score ≥ 60*",
        f"_{datetime.now().strftime('%d.%m.%Y %H:%M UTC')}_",
        "",
        f"*{setup.asset}* {arrow} *{setup.direction}* · score *{setup.score}/100*{sigma_str}{rr_str}",
        f"Entry: `${setup.entry:,.4f}`  ·  Stop: `${setup.stop:,.4f}`  ·  Target: `${setup.target:,.4f}`",
    ]
    if reason:
        lines.append("")
        lines.append(f"_{reason}_")

    lines.extend(
        [
            "",
            "⏳ *Горизонт:* 1-3 дня (vol-target sizing, не позиция «купи и держи»).",
            "",
            "💡 _Это не приказ войти, а информация о лучшем актуальном setup'е по нашей "
            "скоринговой модели (0-100, ≥60 = проверенный сильный сигнал). Решение — за тобой. "
            "Если /daily вердикт NEUTRAL — взвесь дважды: эти два сигнала могут противоречить._",
            "",
            "⚠️ _Я бы вошёл при подтверждении свечой за уровнем и стопе на месте. "
            "Это не финансовый совет, своими деньгами рискуешь ты. DYOR._",
        ]
    )
    return "\n".join(lines)


class BestDealAlertSystem:
    """Отслеживает результат rank_signals и пушит лучший setup подписчикам."""

    def __init__(self, bot):
        self.bot = bot
        # asset+direction → последний отправленный алерт. Per-pair cooldown.
        self._last: dict[str, _LastAlert] = {}

    def _should_send(self, asset: str, direction: str, score: int) -> bool:
        key = f"{asset}:{direction}"
        prev = self._last.get(key)
        if prev is None:
            return True
        now = datetime.now()
        hours_passed = (now - prev.fired_at).total_seconds() / 3600
        cooldown_h = get_cooldown_hours()
        if hours_passed >= cooldown_h:
            return True
        bump = get_score_bump()
        if score >= prev.score + bump:
            return True
        return False

    def _user_allowed_assets(self, user: dict) -> Optional[set[str]]:
        """Возвращает множество активов, на которые подписан юзер. None = все.

        Источник — `users.signals_assets` (CSV) или, для back-compat, поле
        `signals_assets` в dict.
        """
        raw = user.get("signals_assets")
        if raw is None or raw == "":
            return None  # None = все активы
        if isinstance(raw, str):
            parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
        elif isinstance(raw, (list, tuple, set)):
            parts = [str(p).strip().upper() for p in raw if str(p).strip()]
        else:
            return None
        return set(parts) if parts else set()

    async def check_and_alert(self, subscribers: list[dict]) -> int:
        """Один цикл: live-prices → rank_signals → per-user send.

        Каждому подписчику отправляем его *персональный* лучший setup среди
        активов, на которые он подписан. Если у пользователя пустой список —
        пропускаем (он явно снял всё).
        """
        if not subscribers:
            return 0
        if not feature_enabled():
            return 0

        try:
            from core.signal_scorer import SignalSetup, rank_signals
            from web_search import fetch_realtime_prices
        except Exception as e:  # pragma: no cover - import-time safety
            logger.warning(f"best-deal alert: import failed: {e}")
            return 0

        try:
            prices = await fetch_realtime_prices()
        except Exception as e:
            logger.warning(f"best-deal alert: fetch_realtime_prices failed: {e}")
            return 0

        if not prices:
            return 0

        try:
            result = rank_signals(prices, capital=123.0)
        except Exception as e:
            logger.warning(f"best-deal alert: rank_signals failed: {e}")
            return 0

        tradable: list = (
            result.get("tradable_setups", []) if isinstance(result, dict) else []
        )
        if not tradable:
            return 0

        sent = 0
        for user in subscribers:
            user_id = user.get("user_id")
            if user_id is None:
                continue
            allowed = self._user_allowed_assets(user)
            if allowed is not None and not allowed:
                # Юзер явно «Снять все» — ничего не шлём.
                continue
            user_top = None
            for s in tradable:
                if not isinstance(s, SignalSetup):
                    continue
                if allowed is not None and s.asset.upper() not in allowed:
                    continue
                user_top = s
                break
            if user_top is None:
                continue
            # Per-user cooldown ключ: (user_id, asset, direction).
            cooldown_key = f"{user_id}:{user_top.asset}:{user_top.direction}"
            prev = self._last.get(cooldown_key)
            if prev is not None:
                hours_passed = (datetime.now() - prev.fired_at).total_seconds() / 3600
                if hours_passed < get_cooldown_hours() and user_top.score < prev.score + get_score_bump():
                    continue

            message = _format_alert(user_top)
            try:
                await self.bot.send_message(user_id, message, parse_mode="Markdown")
                sent += 1
                self._last[cooldown_key] = _LastAlert(
                    asset=user_top.asset,
                    direction=user_top.direction,
                    score=int(user_top.score),
                    fired_at=datetime.now(),
                )
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(
                    "best-deal alert send error user %s: %s", user_id, e
                )

        if sent:
            logger.info("best-deal alert: sent to %s users", sent)
        return sent


__all__ = [
    "BestDealAlertSystem",
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_INTERVAL_SEC",
    "DEFAULT_SCORE_BUMP",
    "feature_enabled",
    "get_check_interval_sec",
    "get_cooldown_hours",
    "get_score_bump",
]
