"""
best_deal_alert.py — push-алерт лучших setup'ов из `core/signal_scorer.rank_signals`.

Идея: команда `/signal` уже даёт top-N setups по score 0-100, но пользователь
должен сам её нажать. Юзер: «если лучшая сделка набирает свои 60 из 100 очков
чтобы она приходила пользователю сама а не по вызову кнопки».

Этот модуль:
  - Раз в `INTERVAL_SEC` (default 1h) тянет live-prices и пускает `rank_signals`.
  - Если есть tradable_setups (score ≥ DEFAULT_MIN_SCORE=60) — шлёт алерт
    подписчикам с **топ-N** (default 3) setup'ами в одном сообщении.
  - Анти-спам: пара (asset, direction) не отправляется чаще раз в `COOLDOWN_HOURS`,
    кроме случая когда score прыгнул на ≥SCORE_BUMP пунктов (significant strengthening).
    Если хотя бы 1 из N setup'ов «новый» (cooldown прошёл или score bump) — шлём
    батч из ВСЕХ текущих топ-N. Иначе — пропуск.
  - Никакой автоматической торговли. Только информация + дисклеймер.

Юзер: «Лучшая сделка должна включать в себя больше 1 актива, хотя бы 2-3».
Раньше: per-юзер `user_top = setup; break` после первого совпадения — 1 сетап.
Сейчас: per-юзер собираем до `BEST_DEAL_ALERT_TOP_N` setup'ов (default 3) из
`tradable_setups`, фильтруем по allowed-list, рендерим батчем с медалями
🥇🥈🥉.

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

# Сколько setup'ов показываем в одном алерте (1..5 разумно). Юзер: «лучшая
# сделка должна включать в себя 2-3 актива». 3 — золотая середина: видно
# top-3 setup без перегруза.
DEFAULT_TOP_N = 3


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


def get_top_n() -> int:
    """Сколько setup'ов рендерим в одном auto-push сообщении (1..5)."""
    return _env_int(
        "BEST_DEAL_ALERT_TOP_N",
        DEFAULT_TOP_N,
        min_val=1,
        max_val=5,
    )


@dataclass(frozen=True)
class _LastAlert:
    asset: str
    direction: str
    score: int
    fired_at: datetime


_MEDALS: tuple[str, ...] = ("🥇", "🥈", "🥉")


def _format_single_setup(setup, *, idx: int, total: int) -> list[str]:
    """Один setup-блок в формате батч-алерта. Возвращает list строк.

    idx начинается с 1. Если total == 1 — без медали, иначе медаль по
    позиции (🥇/🥈/🥉/🔸).
    """
    arrow = "↑" if setup.direction == "LONG" else "↓"
    direction_emoji = "🟢" if setup.direction == "LONG" else "🔴"
    sigma = getattr(setup, "sigma_1d_pct", None)
    rr = getattr(setup, "rr", None)
    # `rank_signals.SignalSetup.rr_ratio` — fallback если поле `rr` отсутствует.
    if rr is None:
        rr = getattr(setup, "rr_ratio", None)
    reason = getattr(setup, "reason", "") or ""
    if not reason:
        # SignalSetup использует поле `reasons` (list); join первые 2 ради краткости.
        reasons_list = getattr(setup, "reasons", None) or []
        if isinstance(reasons_list, (list, tuple)) and reasons_list:
            reason = "; ".join(str(r) for r in reasons_list[:2])

    sigma_str = f" · σ̂₁d={sigma:.2f}%" if isinstance(sigma, (int, float)) else ""
    rr_str = f" · R/R={rr:.2f}" if isinstance(rr, (int, float)) else ""

    if total == 1:
        head_prefix = direction_emoji
    else:
        head_prefix = _MEDALS[idx - 1] if 1 <= idx <= len(_MEDALS) else "🔸"

    out: list[str] = [
        f"{head_prefix} *{setup.asset}* {arrow} *{setup.direction}* · "
        f"score *{setup.score}/100*{sigma_str}{rr_str}",
        f"  Entry: `${setup.entry:,.4f}`  ·  Stop: `${setup.stop:,.4f}`  ·  "
        f"Target: `${setup.target:,.4f}`",
    ]
    if reason:
        out.append(f"  _{reason}_")
    return out


def _format_alert(setups) -> str:
    """Рендерит 1..N SignalSetup'ов в Telegram-сообщение (Markdown).

    Принимает как одиночный setup (backward-compat для старых тестов), так
    и list/tuple setup'ов (новый формат «топ-N»). Если N >= 2 — заголовок
    «ТОП-N лучших setups», иначе «лучший setup».
    """
    if not isinstance(setups, (list, tuple)):
        setups_list = [setups]
    else:
        setups_list = list(setups)
    if not setups_list:
        return ""

    total = len(setups_list)
    if total == 1:
        head_emoji = "🟢" if setups_list[0].direction == "LONG" else "🔴"
        title = f"{head_emoji} *AUTO-PUSH: лучший setup score ≥ 60*"
    else:
        title = f"⭐ *AUTO-PUSH: ТОП-{total} setups score ≥ 60*"

    lines: list[str] = [
        title,
        f"_{datetime.now().strftime('%d.%m.%Y %H:%M UTC')}_",
        "",
    ]
    for idx, setup in enumerate(setups_list, start=1):
        lines.extend(_format_single_setup(setup, idx=idx, total=total))
        lines.append("")

    lines.extend(
        [
            "⏳ *Горизонт:* 1-3 дня (vol-target sizing, не позиция «купи и держи»).",
            "",
            "💡 _Это не приказ войти, а наш scoring (0-100, ≥60 = сильный сигнал). "
            "Решение — за тобой. Если /daily вердикт NEUTRAL — взвесь дважды._",
            "",
            "⚠️ _Вход — подтверждение свечой за уровнем + стоп на месте. DYOR._",
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

        Каждому подписчику собираем **топ-N** setup'ов (`BEST_DEAL_ALERT_TOP_N`,
        default 3) среди активов, на которые он подписан, и отправляем одним
        батч-сообщением. Если у пользователя пустой allowed-list (явно «Снять
        все») — пропускаем.

        Cooldown: per-(user, asset, direction). Шлём батч, если **хотя бы
        один** из топ-N setup'ов прошёл cooldown (новый или score bump'нул).
        Иначе — батч пропускаем целиком (чтобы не дублировать тот же расклад
        каждый час).
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

        top_n_cfg = get_top_n()
        cooldown_h = get_cooldown_hours()
        bump = get_score_bump()
        now = datetime.now()

        sent = 0
        for user in subscribers:
            user_id = user.get("user_id")
            if user_id is None:
                continue
            allowed = self._user_allowed_assets(user)
            if allowed is not None and not allowed:
                # Юзер явно «Снять все» — ничего не шлём.
                continue
            # Собираем топ-N setup'ов по allowed-фильтру, сохраняя порядок score↓.
            user_setups: list = []
            for s in tradable:
                if not isinstance(s, SignalSetup):
                    continue
                if allowed is not None and s.asset.upper() not in allowed:
                    continue
                user_setups.append(s)
                if len(user_setups) >= top_n_cfg:
                    break
            if not user_setups:
                continue
            # Cooldown-фильтр: если ВСЕ setup'ы юзера уже отправлены недавно
            # и без score-bump'а — пропускаем весь батч (не дублируем расклад).
            any_new = False
            for s in user_setups:
                cooldown_key = f"{user_id}:{s.asset}:{s.direction}"
                prev = self._last.get(cooldown_key)
                if prev is None:
                    any_new = True
                    break
                hours_passed = (now - prev.fired_at).total_seconds() / 3600
                if hours_passed >= cooldown_h or s.score >= prev.score + bump:
                    any_new = True
                    break
            if not any_new:
                continue

            message = _format_alert(user_setups)
            try:
                await self.bot.send_message(user_id, message, parse_mode="Markdown")
                sent += 1
                # Маркируем ВСЕ setup'ы из батча как отправленные.
                for s in user_setups:
                    cooldown_key = f"{user_id}:{s.asset}:{s.direction}"
                    self._last[cooldown_key] = _LastAlert(
                        asset=s.asset,
                        direction=s.direction,
                        score=int(s.score),
                        fired_at=now,
                    )
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(
                    "best-deal alert send error user %s: %s", user_id, e
                )

        if sent:
            logger.info("best-deal alert: sent batch to %s users", sent)
        return sent


__all__ = [
    "BestDealAlertSystem",
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_INTERVAL_SEC",
    "DEFAULT_SCORE_BUMP",
    "DEFAULT_TOP_N",
    "feature_enabled",
    "get_check_interval_sec",
    "get_cooldown_hours",
    "get_score_bump",
    "get_top_n",
]
