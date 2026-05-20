"""I/O layer для per-agent calibration: extractor, evaluator, runtime hooks.

Разделение ответственности:
  * `core/agent_calibration.py` — чистая математика (Brier, shrinkage, weights).
  * `core/agent_calibration_io.py` (этот файл) — DB + AI + market data вызовы.
  * `database.py` — CRUD по таблице agent_predictions.
  * `scheduler.py` — фоновая задача, вызывающая `evaluate_pending`.

Dependency injection: все внешние зависимости (ai_callable, price_fetcher)
принимаются параметрами, чтобы юнит-тесты могли подсунуть моки без monkeypatch.

Феатурфлаг: всё gated за `FEATURE_AGENT_CALIBRATION` (env), default OFF (0).
Если выключено — функции возвращают early, ничего не пишут.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Iterable, Optional

from core.agent_calibration import (
    AgentCalibrationStats,
    compute_agent_stats,
)

logger = logging.getLogger(__name__)


# ─── Env-flags / defaults ────────────────────────────────────────────────────

#: Master switch. По умолчанию OFF — не пишем prediction'ы, не дёргаем AI.
FEATURE_FLAG_ENV = "FEATURE_AGENT_CALIBRATION"

#: Горизонт прогноза по умолчанию (мин). 24h — компромисс между объёмом данных
#: и осмысленностью (агенты говорят про «next 24h» естественно). Override
#: через env `AGENT_CALIB_HORIZON_MIN`.
DEFAULT_HORIZON_MINUTES = 24 * 60

#: Порог «реализации» в %. Любое движение цены >= +0.5% в течение horizon
#: засчитывается как y=1. Override через `AGENT_CALIB_THRESHOLD_PCT`.
DEFAULT_THRESHOLD_PCT = 0.5

#: Список агентов, у которых запрашиваем калибровку. Verifier — отдельный
#: случай: его роль — fact-checking, а не predict; включаем для полноты, но
#: предсказание будет шумным. Synth — итоговый «голос», самый важный.
DEFAULT_AGENT_ROLES = ("bull", "bear", "verifier", "synth")

#: Окно lookback для статистики калибровки агента, в днях.
DEFAULT_LOOKBACK_DAYS = 30


def feature_enabled() -> bool:
    """True если FEATURE_AGENT_CALIBRATION=1 (или 'true'/'yes'/'on')."""
    raw = os.environ.get(FEATURE_FLAG_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int for env %s=%r, fallback to %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for env %s=%r, fallback to %s", name, raw, default)
        return default


# ─── Probability parsing ─────────────────────────────────────────────────────

# Строгая инструкция агенту: «ONLY a single number 0..1». Но LLM шумит —
# может выдать «0.65 (high conviction)» или «P(up)=0.7 because ...». Парсим
# толерантно: ищем первое число, конвертируем в [0, 1].
_PROB_PATTERN = re.compile(r"(?<![\w\.])(\d{1,3}(?:\.\d+)?|\.\d+)\s*%?")


def parse_probability(text: str) -> Optional[float]:
    """Извлекает probability ∈ [0, 1] из ответа LLM.

    Эвристика:
      * Берём первое число в тексте.
      * Если оно > 1 — интерпретируем как процент (делим на 100). Это покрывает
        случаи «70%», «70», «0.7».
      * Clip в [0, 1].
      * Если число > 100 или невалидное — return None.

    Returns:
        float ∈ [0, 1] или None если не нашли осмысленное число.
    """
    if not text or not isinstance(text, str):
        return None
    for match in _PROB_PATTERN.finditer(text):
        raw = match.group(1)
        try:
            v = float(raw)
        except ValueError:
            continue
        # «70», «70.5», «70%» → 0.70 — интерпретируем как процент
        if v > 1.0 and v <= 100.0:
            v = v / 100.0
        elif v > 100.0:
            # бессмысленно
            continue
        # «0.7», «0.65», «.85» — interpret as-is
        if 0.0 <= v <= 1.0:
            return v
    return None


# ─── Probability prompt ──────────────────────────────────────────────────────

_PROBABILITY_PROMPT_TEMPLATE = """🎯 КАЛИБРОВОЧНЫЙ ВОПРОС (отдельно от дебатов).

Ты — {agent_role_ru}. На основе дебатов и данных выше дай **одно число от 0 до 1**:
вероятность того, что **{asset} вырастет на >= {threshold_pct}%** в течение
следующих **{horizon_hours} часов** от текущей цены ({ref_price}).

ПРАВИЛА:
  - Только число (например: 0.62)
  - Никаких объяснений, формул, скобок
  - Если у тебя НЕТ уверенности — выдай 0.5
  - НЕ повторяй prompt, не пиши «P(up) =»

Твой ответ:"""

_AGENT_ROLE_RU = {
    "bull": "Bull Researcher",
    "bear": "Bear Skeptic",
    "verifier": "Data Verifier",
    "synth": "Consensus Synthesizer",
}


def build_probability_prompt(
    *,
    agent_role: str,
    asset: str,
    threshold_pct: float,
    horizon_minutes: int,
    ref_price: float,
) -> str:
    """Строит prompt для запроса P(up) у конкретного агента."""
    horizon_hours = horizon_minutes / 60.0
    return _PROBABILITY_PROMPT_TEMPLATE.format(
        agent_role_ru=_AGENT_ROLE_RU.get(agent_role, agent_role),
        asset=asset.upper(),
        threshold_pct=f"{threshold_pct:.2f}",
        horizon_hours=f"{horizon_hours:.1f}".rstrip("0").rstrip("."),
        ref_price=f"{ref_price:.4f}".rstrip("0").rstrip("."),
    )


# ─── Extractor ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentProbabilityRequest:
    """Параметры запроса калибровочной вероятности у агента."""

    asset: str
    agent_role: str
    horizon_minutes: int
    threshold_pct: float
    ref_price: float


async def extract_probability(
    *,
    request: AgentProbabilityRequest,
    news_context: str,
    debate_summary: str,
    ai_callable: Callable[[str, str], Awaitable[str]],
    system_prompt: str = "",
) -> Optional[float]:
    """Запрашивает у LLM probability для конкретного агента/актива/горизонта.

    Args:
        request: параметры калибровочного запроса.
        news_context: тот же контекст, что подавался дебатам.
        debate_summary: краткий саммари дебатов (history.context_for_agent()
            или последний раунд). Не пихаем full history — слишком много токенов.
        ai_callable: async (prompt, system) → str. Обычно `ai.bull` / `ai.bear` ...
        system_prompt: system prompt для LLM (опционально, иначе пустой).

    Returns:
        float ∈ [0, 1] либо None если parse failed.
    """
    prompt = (
        f"КОНТЕКСТ И ДАННЫЕ:\n{news_context[:4000]}\n\n"
        f"ИТОГ ДЕБАТОВ:\n{debate_summary[:4000]}\n\n"
        + build_probability_prompt(
            agent_role=request.agent_role,
            asset=request.asset,
            threshold_pct=request.threshold_pct,
            horizon_minutes=request.horizon_minutes,
            ref_price=request.ref_price,
        )
    )
    try:
        response = await ai_callable(prompt, system_prompt)
    except Exception as e:
        logger.warning(
            "extract_probability: AI call failed for %s: %s",
            request.agent_role,
            e,
        )
        return None
    p = parse_probability(response)
    if p is None:
        logger.warning(
            "extract_probability: failed to parse number from %s response: %r",
            request.agent_role,
            (response or "")[:200],
        )
        return None
    return p


# ─── Persist after debate ────────────────────────────────────────────────────


async def save_post_debate_predictions(
    *,
    asset: str,
    ref_price: float,
    extracted: dict[str, float],
    debate_id: str | None = None,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    now: datetime | None = None,
    saver: Callable[..., Awaitable[int]] | None = None,
) -> list[int]:
    """Сохраняет в БД prediction'ы каждого агента. Возвращает список row_id.

    Args:
        asset: символ актива (BTC, ETH, ...).
        ref_price: цена в момент дебата.
        extracted: {agent_role: p_up} — что выдал extractor.
        debate_id: опциональный идентификатор дебата для join'ов.
        horizon_minutes / threshold_pct: те же что задавались extractor'у.
        now: для тестов — fixed timestamp. None → datetime.now(timezone.utc).
        saver: для тестов — мок save_agent_prediction.

    Returns:
        list ids сохранённых строк.
    """
    if not extracted:
        return []
    if saver is None:
        from database import save_agent_prediction as saver  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    resolve_at = (now + timedelta(minutes=horizon_minutes)).strftime("%Y-%m-%d %H:%M:%S")

    saved_ids: list[int] = []
    for role, p_up in extracted.items():
        if p_up is None:
            continue
        try:
            rid = await saver(
                debate_id=debate_id,
                asset=asset.upper(),
                agent_role=role,
                horizon_minutes=horizon_minutes,
                p_up=float(p_up),
                threshold_pct=float(threshold_pct),
                ref_price=float(ref_price),
                resolve_at=resolve_at,
            )
            saved_ids.append(rid)
        except Exception as e:
            logger.warning("save_agent_prediction[%s] failed: %s", role, e)
    return saved_ids


# ─── Evaluator (резолв pending прогнозов) ────────────────────────────────────


@dataclass(frozen=True)
class EvaluationResult:
    """Итог одной итерации resolve loop'а."""

    resolved: int
    skipped: int
    failed: int


async def evaluate_pending_predictions(
    *,
    price_fetcher: Callable[[str], Awaitable[Optional[float]]],
    pending_loader: Callable[..., Awaitable[list[dict]]] | None = None,
    resolver: Callable[..., Awaitable[None]] | None = None,
    max_per_run: int = 50,
    now_iso: str | None = None,
) -> EvaluationResult:
    """Резолвит все прогнозы у которых resolve_at <= now_iso (default now).

    Args:
        price_fetcher: async (asset) → current price (float) | None.
        pending_loader: для тестов — мок get_pending_agent_predictions.
        resolver: для тестов — мок resolve_agent_prediction.
        max_per_run: rate-limit на одну итерацию loop'а.
        now_iso: для тестов — fixed ISO timestamp.

    Returns:
        EvaluationResult — сколько разрешили / пропустили / упало.
    """
    if pending_loader is None:
        from database import get_pending_agent_predictions as pending_loader  # noqa
    if resolver is None:
        from database import resolve_agent_prediction as resolver  # noqa

    pending = await pending_loader(now_iso=now_iso, limit=max_per_run)
    if not pending:
        return EvaluationResult(resolved=0, skipped=0, failed=0)

    resolved = 0
    skipped = 0
    failed = 0
    # Кешируем цены по asset чтобы не дёргать market provider в цикле.
    price_cache: dict[str, Optional[float]] = {}

    for row in pending:
        asset = row["asset"]
        if asset not in price_cache:
            try:
                price_cache[asset] = await price_fetcher(asset)
            except Exception as e:
                logger.warning("evaluate: price_fetcher failed for %s: %s", asset, e)
                price_cache[asset] = None
        realized_price = price_cache[asset]
        if realized_price is None or realized_price <= 0:
            skipped += 1
            continue

        ref_price = float(row["ref_price"])
        if ref_price <= 0:
            skipped += 1
            continue
        threshold_pct = float(row["threshold_pct"])
        p_up = float(row["p_up"])

        actual_pct = (realized_price - ref_price) / ref_price * 100.0
        realized_y = actual_pct >= threshold_pct
        # Brier: (p - y)^2 для одного прогноза.
        brier = (p_up - (1.0 if realized_y else 0.0)) ** 2

        try:
            await resolver(
                prediction_id=int(row["id"]),
                realized_price=realized_price,
                realized_y=realized_y,
                brier_score=brier,
            )
            resolved += 1
        except Exception as e:
            logger.warning("evaluate: resolver failed for id=%s: %s", row.get("id"), e)
            failed += 1

    return EvaluationResult(resolved=resolved, skipped=skipped, failed=failed)


# ─── Per-agent stats (агрегатор) ─────────────────────────────────────────────


async def get_all_agent_stats(
    *,
    roles: Iterable[str] = DEFAULT_AGENT_ROLES,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    asset: str | None = None,
    history_loader: Callable[..., Awaitable[list[dict]]] | None = None,
) -> dict[str, AgentCalibrationStats]:
    """Считает калибровочные статы для всех агентов из истории."""
    if history_loader is None:
        from database import get_agent_calibration_history as history_loader  # noqa

    result: dict[str, AgentCalibrationStats] = {}
    for role in roles:
        try:
            rows = await history_loader(
                agent_role=role,
                asset=asset,
                lookback_days=lookback_days,
            )
        except Exception as e:
            logger.warning("get_agent_calibration_history[%s] failed: %s", role, e)
            rows = []
        ps = [float(r["p_up"]) for r in rows]
        ys = [bool(r["realized_y"]) for r in rows]
        result[role] = compute_agent_stats(
            agent_role=role,
            predicted_probabilities=ps,
            realized_outcomes=ys,
        )
    return result


__all__ = [
    "DEFAULT_AGENT_ROLES",
    "DEFAULT_HORIZON_MINUTES",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_THRESHOLD_PCT",
    "FEATURE_FLAG_ENV",
    "AgentProbabilityRequest",
    "EvaluationResult",
    "build_probability_prompt",
    "evaluate_pending_predictions",
    "extract_probability",
    "feature_enabled",
    "get_all_agent_stats",
    "parse_probability",
    "save_post_debate_predictions",
]
