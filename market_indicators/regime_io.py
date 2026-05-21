"""I/O для regime classifier (BOCPD + label).

Отделено от чистой математики (`regime.py`), чтобы математика оставалась
stdlib-only и тестировалась без сети. Здесь:

  * Fetcher для BTC 1h closes (Binance public spot API, без ключей).
  * `fetch_regime_signals()` — high-level entry с DI HTTP-клиентом.
  * `regime_score_contribution()` — вклад в общий market score.
  * `format_regime_for_agents()` — текст для AI-дебатов.
  * `feature_enabled()` — единый источник правды для `FEATURE_REGIME_CLASSIFIER`.

DI-pattern (тот же что у `options_skew_io`): HTTP-клиент инжектируется как
callable, тесты подменяют моком без aiohttp.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from market_indicators.regime import (
    ALL_LABELS,
    DEFAULT_HAZARD_RATE,
    DEFAULT_LABEL_WINDOW,
    DEFAULT_VOL_HIGH_ANNUALIZED,
    LABEL_CRISIS,
    LABEL_RANGING,
    LABEL_TRENDING,
    LABEL_UNKNOWN,
    LABEL_VOLATILE,
    RegimeClassification,
    classify_regime,
)

logger = logging.getLogger(__name__)


# ─── Endpoints / типы ────────────────────────────────────────────────────────

#: Binance Spot data-api (доступен из geo-restricted регионов).
BINANCE_SPOT_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"

#: Сколько 1h-баров запрашиваем. 500 ≈ 21 день, хватает для стабильного
#: posterior'а BOCPD + достаточно недавнего окна для labeling.
DEFAULT_KLINES_LIMIT = 500

#: HTTP-клиент: callable, возвращающий распарсенный JSON (см. options_skew_io).
HttpClient = Callable[..., Awaitable[Any]]


# ─── Output dataclass ────────────────────────────────────────────────────────


@dataclass
class RegimeSignals:
    """Wrapper для интеграции в aggregator. Сейчас — только BTC, легко расширить."""

    # Главный output — классификация для BTC.
    btc: RegimeClassification = field(default_factory=RegimeClassification)

    # Когда был fetch (epoch ms) — для дебаг'а и timestamping в дайджесте.
    timestamp_ms: Optional[int] = None

    # Источник данных (для transparency в дайджесте).
    source: str = "binance_spot"


# ─── Binance fetcher (DI-based) ──────────────────────────────────────────────


def _binance_klines_args(*, symbol: str, interval: str, limit: int) -> dict[str, Any]:
    """Аргументы для HttpClient: Binance Spot klines.

    Binance возвращает массив массивов; индекс 4 = close, 0 = open_time.
    """
    return {
        "method": "GET",
        "url": BINANCE_SPOT_KLINES_URL,
        "params": {"symbol": symbol, "interval": interval, "limit": int(limit)},
    }


def _parse_binance_closes(payload: Any) -> list[float]:
    """Из binance klines payload вытащить closes (индекс 4)."""
    if not isinstance(payload, list):
        return []
    closes: list[float] = []
    for row in payload:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            c = float(row[4])
        except (TypeError, ValueError):
            continue
        if c <= 0.0:
            continue
        closes.append(c)
    return closes


async def fetch_btc_hourly_closes(
    *, http_client: HttpClient, limit: int = DEFAULT_KLINES_LIMIT,
) -> list[float]:
    """Получить closes BTC за последние `limit` часов.

    Per-venue error isolation: при любой ошибке — пустой список (caller сам
    решает, что делать).
    """
    args = _binance_klines_args(symbol="BTCUSDT", interval="1h", limit=limit)
    try:
        payload = await http_client(**args)
    except (asyncio.TimeoutError, OSError, RuntimeError, ValueError) as e:
        logger.debug("[REGIME] binance klines fetch failed: %s", e)
        return []
    except Exception as e:  # noqa: BLE001
        logger.debug("[REGIME] binance klines unexpected error: %s", e)
        return []
    return _parse_binance_closes(payload)


# ─── High-level entry ────────────────────────────────────────────────────────


async def fetch_regime_signals(
    *,
    http_client: Optional[HttpClient] = None,
    limit: int = DEFAULT_KLINES_LIMIT,
    hazard_rate: float = DEFAULT_HAZARD_RATE,
    label_window: int = DEFAULT_LABEL_WINDOW,
    vol_high_annualized: float = DEFAULT_VOL_HIGH_ANNUALIZED,
) -> RegimeSignals:
    """Главный entry-point. Тянет BTC closes → BOCPD → label → RegimeSignals.

    Если `http_client` не передан, создаём дефолтный над свежей aiohttp-сессией
    (production-path). В тестах — всегда передавайте мок.
    """
    import time

    signals = RegimeSignals(timestamp_ms=int(time.time() * 1000))

    own_session = http_client is None
    session = None
    if own_session:
        try:
            import aiohttp  # noqa: PLC0415
        except ImportError:
            logger.warning("[REGIME] aiohttp недоступен, signals пустые")
            return signals
        session = aiohttp.ClientSession()
        http_client = await make_aiohttp_http_client(session)

    try:
        closes = await fetch_btc_hourly_closes(http_client=http_client, limit=limit)
    finally:
        if own_session and session is not None:
            await session.close()

    if not closes:
        logger.info("[REGIME] no closes fetched, returning unknown signal")
        return signals

    signals.btc = classify_regime(
        closes,
        hazard_rate=hazard_rate,
        label_window=label_window,
        vol_high_annualized=vol_high_annualized,
    )
    logger.info(
        "[REGIME] BTC regime=%s p_cp=%.2f vol_ann=%.2f drift_ann=%+.2f n=%d",
        signals.btc.label,
        signals.btc.p_changepoint,
        signals.btc.recent_volatility_annualized,
        signals.btc.recent_drift_annualized,
        signals.btc.n_observations,
    )
    return signals


# ─── Score contribution ──────────────────────────────────────────────────────


def regime_score_contribution(s: RegimeSignals) -> tuple[int, list[str], list[str]]:
    """Вклад regime classifier'а в общий market score.

    Returns: (score_delta, bullish_reasons, bearish_reasons)

    Консервативные веса (±1 max за PR — собираем baseline, потом докрутим):
      * crisis → -1 (de-risk bias, недавний shock + высокая волатильность)
      * trending + drift>0 → +1 (momentum confirm)
      * trending + drift<0 → -1 (downtrend confirm)
      * volatile → 0 (направление неясно, но добавляем reason для transparency)
      * ranging → 0
      * unknown → 0
    """
    score = 0
    bullish: list[str] = []
    bearish: list[str] = []

    cls = s.btc
    label = cls.label

    if label == LABEL_CRISIS:
        score -= 1
        bearish.append(
            f"Regime=CRISIS (p_cp={cls.p_changepoint:.2f}, "
            f"vol_ann={cls.recent_volatility_annualized:.2f}) — de-risk bias"
        )
    elif label == LABEL_TRENDING:
        if cls.direction_bias > 0:
            score += 1
            bullish.append(
                f"Regime=TRENDING up (drift_ann={cls.recent_drift_annualized:+.2f}, "
                f"ac1={cls.recent_autocorr_lag1:+.2f}) — momentum confirm"
            )
        elif cls.direction_bias < 0:
            score -= 1
            bearish.append(
                f"Regime=TRENDING down (drift_ann={cls.recent_drift_annualized:+.2f}, "
                f"ac1={cls.recent_autocorr_lag1:+.2f}) — downtrend confirm"
            )
    elif label == LABEL_VOLATILE:
        # Не двигаем score, но даём дебатёрам флаг.
        bearish.append(
            f"Regime=VOLATILE (vol_ann={cls.recent_volatility_annualized:.2f}) — "
            "elevated risk, no clear direction"
        )
    # ranging / unknown — никаких добавок.

    return score, bullish, bearish


# ─── Formatter для AI ────────────────────────────────────────────────────────


def format_regime_for_agents(s: RegimeSignals) -> str:
    """Текстовый блок для AI-дебатов."""
    cls = s.btc
    lines = ["🎯 РЕЖИМ РЫНКА (источник: BOCPD по BTC 1h close):"]

    if cls.label == LABEL_UNKNOWN or cls.n_observations < 12:
        lines.append("• Недостаточно данных для классификации (нужно ≥12 часовых баров).")
        return "\n".join(lines)

    label_desc = {
        LABEL_TRENDING: "TRENDING — есть направленный momentum",
        LABEL_RANGING: "RANGING — рынок в боковике / mean-reversion",
        LABEL_VOLATILE: "VOLATILE — высокая волатильность, направление неясно",
        LABEL_CRISIS: "CRISIS — недавний changepoint + высокая волатильность",
    }.get(cls.label, cls.label.upper())

    lines.append(f"• Текущий режим: {label_desc}")
    lines.append(
        f"• Drift (annualized): {cls.recent_drift_annualized:+.1%} | "
        f"Vol (annualized): {cls.recent_volatility_annualized:.1%}"
    )
    lines.append(
        f"• P(recent changepoint) = {cls.p_changepoint:.2f} | "
        f"E[run-length] = {cls.expected_run_length:.1f} bars"
    )
    lines.append(
        f"• Autocorr lag-1 = {cls.recent_autocorr_lag1:+.2f} | "
        f"Drift/Vol = {cls.drift_to_vol_ratio:+.3f}"
    )
    lines.append("")
    lines.append(
        "💡 Интерпретация: в trending-режиме momentum-сигналы (funding, OI-build) "
        "работают; в ranging — наоборот, contrarian / mean-reversion. "
        "В crisis-режиме предпочесть defensive sizing."
    )
    return "\n".join(lines)


# ─── Env-flags ───────────────────────────────────────────────────────────────


def feature_enabled() -> bool:
    """`FEATURE_REGIME_CLASSIFIER=1` включает. Дефолт — OFF."""
    return os.getenv("FEATURE_REGIME_CLASSIFIER", "0").strip() in {
        "1", "true", "True", "yes",
    }


def get_label_window() -> int:
    """`REGIME_LABEL_WINDOW` в часах. Минимум 12, максимум 168 (неделя)."""
    try:
        raw = int(os.getenv("REGIME_LABEL_WINDOW", str(DEFAULT_LABEL_WINDOW)))
    except (TypeError, ValueError):
        return DEFAULT_LABEL_WINDOW
    return max(12, min(168, raw))


def get_hazard_rate() -> float:
    """`REGIME_HAZARD_RATE` — кастомный hazard. По дефолту 1/200."""
    try:
        raw = float(os.getenv("REGIME_HAZARD_RATE", str(DEFAULT_HAZARD_RATE)))
    except (TypeError, ValueError):
        return DEFAULT_HAZARD_RATE
    if raw <= 0.0 or raw >= 1.0:
        return DEFAULT_HAZARD_RATE
    return raw


def get_vol_high_annualized() -> float:
    """`REGIME_VOL_HIGH_ANNUALIZED` — порог volatility для VOLATILE/CRISIS."""
    try:
        raw = float(os.getenv("REGIME_VOL_HIGH_ANNUALIZED", str(DEFAULT_VOL_HIGH_ANNUALIZED)))
    except (TypeError, ValueError):
        return DEFAULT_VOL_HIGH_ANNUALIZED
    if raw <= 0.0:
        return DEFAULT_VOL_HIGH_ANNUALIZED
    return raw


def get_klines_limit() -> int:
    """`REGIME_KLINES_LIMIT` — сколько 1h-баров тянуть. Min 50, max 1000."""
    try:
        raw = int(os.getenv("REGIME_KLINES_LIMIT", str(DEFAULT_KLINES_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_KLINES_LIMIT
    return max(50, min(1000, raw))


# Sanity: ALL_LABELS экспортится из regime.py, чтобы тесты могли проверить
# что мы не возвращаем чего-то непонятного — переэкспортим явно.
__all__ = [
    "ALL_LABELS",
    "BINANCE_SPOT_KLINES_URL",
    "DEFAULT_KLINES_LIMIT",
    "HttpClient",
    "LABEL_CRISIS",
    "LABEL_RANGING",
    "LABEL_TRENDING",
    "LABEL_UNKNOWN",
    "LABEL_VOLATILE",
    "RegimeSignals",
    "RegimeClassification",
    "_binance_klines_args",
    "_parse_binance_closes",
    "fetch_btc_hourly_closes",
    "fetch_regime_signals",
    "format_regime_for_agents",
    "feature_enabled",
    "get_hazard_rate",
    "get_klines_limit",
    "get_label_window",
    "get_vol_high_annualized",
    "regime_score_contribution",
]


# ─── Aiohttp factory (для production-вызова, НЕ для тестов) ──────────────────


async def make_aiohttp_http_client(session: Any) -> HttpClient:
    """Совместимая с options_skew_io / microstructure_io обвязка aiohttp."""
    async def _call(*, method: str, url: str, params=None, json=None, timeout=8.0):
        import aiohttp  # noqa: PLC0415
        to = aiohttp.ClientTimeout(total=float(timeout))
        if method.upper() == "GET":
            async with session.get(url, params=params, timeout=to) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return await resp.json(content_type=None)
        elif method.upper() == "POST":
            async with session.post(url, params=params, json=json, timeout=to) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return await resp.json(content_type=None)
        else:
            raise ValueError(f"unsupported method: {method}")

    return _call
