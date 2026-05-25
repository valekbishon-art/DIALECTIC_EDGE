"""AI-narrative для /advise — объяснение плана как в /btc.

Логика:
- На вход — ``StoredPlan`` (из advisor_storage) + опциональный market_snapshot
  (BTC verdict, текущий тренд, RSI). На выход — короткий текст-объяснение
  («почему сейчас, какие риски, что может пойти не так»).
- Сначала проверяем кэш в plan.narrative — если есть, возвращаем сразу.
  Иначе зовём AgentProvider.complete() и сохраняем через
  ``advisor_storage.update_narrative``.

Per AGENTS.md: фичефлаг ``FEATURE_ADVISOR_NARRATIVE`` (default 0).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def feature_enabled() -> bool:
    """Narrative toggle. По умолчанию OFF — стоит денег у AI-провайдеров."""
    raw = os.getenv("FEATURE_ADVISOR_NARRATIVE", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_SYSTEM_PROMPT = (
    "Ты — опытный криптотрейдер. На входе — конкретный торговый план "
    "(asset/direction/entry/stop/TP) который пользователь рассматривает. "
    "Твоя задача — объяснить план: ПОЧЕМУ сейчас, какие РИСКИ, на что "
    "СМОТРЕТЬ для invalidation. Пиши коротко (~120-180 слов), без воды, "
    "по-русски. Не повторяй цифры — юзер их и так видит. Без markdown — "
    "только plain text + emoji для акцентов."
)


def _fmt_price(value: float) -> str:
    """Human-readable price без научной нотации. >=100 → no decimals,
    >=1 → 4 знака, <1 → 6 знаков (микрокоины типа SHIB).
    """
    if value is None:
        return "?"
    abs_v = abs(value)
    if abs_v >= 100:
        return f"{value:,.2f}"
    if abs_v >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def _build_plan_summary(plan) -> str:
    """Compact text representation of the plan for the AI prompt."""
    lines = [
        f"Актив: {plan.asset}",
        f"Направление: {plan.direction or plan.action}",
        f"Уверенность: {plan.confidence_pct}%",
    ]
    if plan.entry_price is not None:
        lines.append(f"Вход: {_fmt_price(plan.entry_price)}")
    if plan.stop_price is not None:
        lines.append(f"Стоп: {_fmt_price(plan.stop_price)}")
    if plan.tp_levels:
        tps = [
            f"TP{i+1}={_fmt_price(tp.get('price'))}"
            for i, tp in enumerate(plan.tp_levels)
            if tp.get("price")
        ]
        if tps:
            lines.append("Тейки: " + ", ".join(tps))
    if plan.risk_reward is not None:
        lines.append(f"R/R: {plan.risk_reward:.2f}")
    if plan.position_usd is not None:
        lines.append(f"Размер: ${plan.position_usd:.0f}")
    if plan.horizon_human:
        lines.append(f"Горизонт: {plan.horizon_human}")
    if plan.btc_overlay_note:
        lines.append(f"BTC overlay: {plan.btc_overlay_note}")
    if plan.rationale:
        lines.append("Сигналы: " + "; ".join(plan.rationale[:5]))
    return "\n".join(lines)


def _build_market_snapshot(market_ctx: Optional[dict]) -> str:
    if not market_ctx:
        return "(контекст рынка не передан)"
    parts = []
    btc_lean = market_ctx.get("btc_lean")
    btc_conf = market_ctx.get("btc_confidence_pct")
    if btc_lean and btc_conf is not None:
        parts.append(f"BTC outlook: {btc_lean} (уверенность {btc_conf}%)")
    trend = market_ctx.get("trend")
    if trend:
        parts.append(f"Тренд актива: {trend}")
    rsi = market_ctx.get("rsi")
    if rsi is not None:
        parts.append(f"RSI: {rsi:.0f}")
    fng = market_ctx.get("fear_greed")
    if fng is not None:
        parts.append(f"Fear&Greed: {fng}")
    return "\n".join(parts) if parts else "(контекст рынка не передан)"


def _build_prompt(plan, market_ctx: Optional[dict]) -> str:
    """Compose user prompt for narrative generation."""
    return (
        "Объясни этот план так, чтобы юзер понял: почему сейчас "
        "вход осмыслен и где может развалиться.\n\n"
        f"=== ПЛАН ===\n{_build_plan_summary(plan)}\n\n"
        f"=== РЫНОК ===\n{_build_market_snapshot(market_ctx)}\n\n"
        "Структура ответа (без markdown заголовков):\n"
        "📍 Контекст — 1-2 предложения, что сейчас на рынке.\n"
        "✅ Почему этот сетап — 2-3 пункта.\n"
        "⚠️ Риски — что может пойти не так, конкретно.\n"
        "🎯 На что смотреть — какие сигналы подтверждают/опровергают.\n"
    )


async def generate_plan_narrative(
    plan,
    market_ctx: Optional[dict] = None,
    *,
    agent_provider=None,
) -> Optional[str]:
    """Generate AI explanation of the plan. Returns text or None on failure.

    Использует AgentProvider.complete() (роутер на 5 провайдеров с
    fallback). Если фича выключена — возвращает None. Не raises.
    """
    if not feature_enabled():
        logger.debug("advisor_narrative: feature off, skipping")
        return None
    # Prefer cached narrative if already on the plan.
    cached = getattr(plan, "narrative", None)
    if cached and isinstance(cached, str) and cached.strip():
        return cached

    if agent_provider is None:
        try:
            from ai_provider import AgentProvider

            agent_provider = AgentProvider()
        except Exception as exc:
            logger.warning("advisor_narrative: AgentProvider unavailable: %s", exc)
            return None

    prompt = _build_prompt(plan, market_ctx)
    try:
        text = await agent_provider.complete(
            prompt=prompt, system=_SYSTEM_PROMPT, temperature=0.4,
        )
        if not text or not text.strip():
            return None
        return text.strip()
    except Exception as exc:
        logger.warning("advisor_narrative: AI call failed: %s", exc)
        return None


__all__ = [
    "feature_enabled",
    "generate_plan_narrative",
]
