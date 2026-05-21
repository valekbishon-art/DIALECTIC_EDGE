"""I/O для liquidation magnet — Binance fapi + Bybit v5.

Источники (публичные, без ключей):
    Binance USDS-M futures:
      GET /futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=N
        → historical OI snapshots (ts, sumOpenInterest, sumOpenInterestValue).
      GET /futures/data/topLongShortPositionRatio?symbol=BTCUSDT&period=1h&limit=1
        → top trader position ratio (longShortRatio, longAccount, shortAccount).
    Bybit v5 (fallback если Binance недоступен):
      GET /v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1h
        → list of OI history (ts, openInterest).
      Bybit не отдаёт top-trader L/S ratio публично → используется только OI.

Стратегия:
    1. Параллельно тянем Binance OI history + top L/S ratio + Bybit OI history.
    2. Предпочитаем Binance (есть L/S). Если Binance fail, Bybit OI + None L/S.
    3. Без L/S ratio → label = UNKNOWN (не делаем «slепые» прогнозы по одной OI).

Всё DI-based: HTTP-клиент инжектится в `fetch_liquidation_magnet_signal`;
тесты подменяют его моком. Production использует aiohttp-обёртку.

Не пересекается с smart_money.py (тот — funding/coinbase-premium), отдельный
изолированный путь.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable

from market_indicators.liquidation_magnet import (
    DEFAULT_LS_LONG_EXTREME,
    DEFAULT_LS_LONG_HEAVY,
    DEFAULT_LS_SHORT_EXTREME,
    DEFAULT_LS_SHORT_HEAVY,
    DEFAULT_OI_BUILDUP_PCT,
    DEFAULT_OI_BUILDUP_STRONG_PCT,
    LABEL_DOWN_MAGNET,
    LABEL_NEUTRAL,
    LABEL_UNKNOWN,
    LABEL_UP_MAGNET,
    LiquidationMagnetSignal,
    OIHistoryPoint,
    TopTraderRatio,
    build_liquidation_magnet_signal,
)

logger = logging.getLogger(__name__)

#: Callable интерфейс HTTP-клиента (как в funding_term_io / regime_io).
HttpClient = Callable[..., Awaitable[Any]]


# ─── Binance fapi endpoints ─────────────────────────────────────────────────


def _binance_oi_hist_args(*, symbol: str, period: str, limit: int) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": "https://fapi.binance.com/futures/data/openInterestHist",
        "params": {
            "symbol": symbol.upper(),
            "period": period,
            "limit": int(limit),
        },
    }


def _binance_top_ls_ratio_args(*, symbol: str, period: str, limit: int = 1) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        "params": {
            "symbol": symbol.upper(),
            "period": period,
            "limit": int(limit),
        },
    }


def _parse_binance_oi_hist(payload: Any) -> list[OIHistoryPoint]:
    """Binance возвращает массив объектов с полями
    {symbol, sumOpenInterest, sumOpenInterestValue, timestamp}.
    sumOpenInterest — в контрактах; sumOpenInterestValue — в USDT.
    """
    if not isinstance(payload, list):
        # Иногда Binance возвращает dict с {"code": ..., "msg": ...} при ошибке.
        if isinstance(payload, dict) and "code" in payload:
            logger.warning(
                "binance oi-hist error: code=%s msg=%s",
                payload.get("code"), payload.get("msg"),
            )
        return []
    out: list[OIHistoryPoint] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            ts = int(row.get("timestamp") or 0)
            oi = float(row.get("sumOpenInterest") or 0.0)
            oi_usd = float(row.get("sumOpenInterestValue") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts < 0 or oi < 0:
            continue
        out.append(OIHistoryPoint(timestamp_ms=ts, oi_contracts=oi, oi_usd=oi_usd))
    return out


def _parse_binance_top_ls_ratio(payload: Any) -> TopTraderRatio | None:
    """Binance возвращает массив; берём последнюю запись.
    Поля: {symbol, longShortRatio, longAccount, shortAccount, timestamp}.
    """
    if not isinstance(payload, list) or not payload:
        if isinstance(payload, dict) and "code" in payload:
            logger.warning(
                "binance top-ls-ratio error: code=%s msg=%s",
                payload.get("code"), payload.get("msg"),
            )
        return None
    row = payload[-1]
    if not isinstance(row, dict):
        return None
    try:
        ts = int(row.get("timestamp") or 0)
        ratio = float(row.get("longShortRatio") or 0.0)
        long_pct = float(row.get("longAccount") or 0.0)
        short_pct = float(row.get("shortAccount") or 0.0)
    except (TypeError, ValueError):
        return None
    if ratio <= 0:
        return None
    return TopTraderRatio(
        timestamp_ms=ts,
        long_account_pct=long_pct,
        short_account_pct=short_pct,
        long_short_ratio=ratio,
    )


# ─── Bybit v5 endpoints (fallback OI only) ───────────────────────────────────


def _bybit_oi_hist_args(*, symbol: str, interval: str, limit: int) -> dict[str, Any]:
    """Bybit interval values: 5min,15min,30min,1h,4h,1d."""
    return {
        "method": "GET",
        "url": "https://api.bybit.com/v5/market/open-interest",
        "params": {
            "category": "linear",
            "symbol": symbol.upper(),
            "intervalTime": interval,
            "limit": int(limit),
        },
    }


def _parse_bybit_oi_hist(payload: Any) -> list[OIHistoryPoint]:
    """Bybit возвращает {result: {list: [{timestamp, openInterest}, ...]}}.
    timestamp в миллисекундах строкой; openInterest строкой в контрактах.
    """
    out: list[OIHistoryPoint] = []
    try:
        if not isinstance(payload, dict):
            return []
        result = payload.get("result") or {}
        items = result.get("list") or []
    except (AttributeError, TypeError):
        return []
    for row in items:
        if not isinstance(row, dict):
            continue
        try:
            ts = int(str(row.get("timestamp") or "0"))
            oi = float(str(row.get("openInterest") or "0"))
        except (TypeError, ValueError):
            continue
        if ts < 0 or oi < 0:
            continue
        out.append(OIHistoryPoint(timestamp_ms=ts, oi_contracts=oi, oi_usd=0.0))
    return out


# ─── Top-level fetcher ───────────────────────────────────────────────────────


async def _safe_fetch(
    http_client: HttpClient,
    *,
    args: dict[str, Any],
    timeout: float,
    name: str,
) -> Any | None:
    try:
        return await asyncio.wait_for(http_client(**args), timeout=timeout)
    except (asyncio.TimeoutError, RuntimeError) as e:
        logger.warning("liquidation-magnet %s fetch failed: %s", name, e)
        return None


async def fetch_liquidation_magnet_signal(
    http_client: HttpClient | None = None,
    *,
    symbol: str | None = None,
    period: str | None = None,
    lookback_hours: int | None = None,
    timeout: float = 8.0,
) -> LiquidationMagnetSignal:
    """Главная entry-point: дёрнуть OI + L/S ratio и вернуть aggregated signal.

    Логика fallback:
      1. Параллельно: Binance OI hist + Binance top L/S + Bybit OI hist.
      2. Если Binance OI получен → используем его + L/S → polnayy signal.
      3. Если Binance OI fail → Bybit OI без L/S → label = UNKNOWN
         (без L/S не можем классифицировать magnet).
      4. Если оба fail → UNKNOWN.

    Args:
        http_client:    DI HTTP-client. Если None — production aiohttp фабрика.
        symbol:         Default BTCUSDT (через env LIQUIDATION_MAGNET_SYMBOL).
        period:         Binance period: "5m","15m","30m","1h","2h","4h","6h","12h","1d".
                        Bybit использует свой mapping. Default "1h".
        lookback_hours: Окно для OI velocity (default 24, через env).
        timeout:        per-call timeout.

    Returns:
        LiquidationMagnetSignal. Никогда не raise — graceful UNKNOWN при fail.
    """
    sym = (symbol or get_symbol()).upper()
    period_str = period or get_period()
    lookback = lookback_hours if lookback_hours is not None else get_lookback_hours()
    now_ms = int(time.time() * 1000)

    # Решаем сколько OI-точек надо запросить, чтобы покрыть lookback окно.
    # Binance period="1h" → limit = lookback+1; "4h" → ceil(lookback/4)+1, и т.д.
    limit = _compute_oi_limit(period_str, lookback)

    if http_client is None:
        try:
            import aiohttp
            session = aiohttp.ClientSession()
            http_client = make_aiohttp_http_client(session)
            owns_session = True
        except ImportError:
            logger.warning("[LIQ-MAGNET] aiohttp not available")
            return LiquidationMagnetSignal(
                symbol=sym, oi_lookback_hours=lookback,
                timestamp_ms=now_ms, label=LABEL_UNKNOWN,
            )
    else:
        owns_session = False
        session = None

    try:
        # Параллельно — все 3 запроса.
        binance_oi_args = _binance_oi_hist_args(symbol=sym, period=period_str, limit=limit)
        binance_ls_args = _binance_top_ls_ratio_args(symbol=sym, period=period_str, limit=1)
        bybit_oi_args = _bybit_oi_hist_args(
            symbol=sym, interval=_bybit_interval_for(period_str), limit=limit,
        )

        b_oi_payload, b_ls_payload, bb_oi_payload = await asyncio.gather(
            _safe_fetch(http_client, args=binance_oi_args, timeout=timeout, name="binance-oi"),
            _safe_fetch(http_client, args=binance_ls_args, timeout=timeout, name="binance-ls"),
            _safe_fetch(http_client, args=bybit_oi_args, timeout=timeout, name="bybit-oi"),
        )
    finally:
        if owns_session and session is not None:
            await session.close()

    binance_oi = _parse_binance_oi_hist(b_oi_payload) if b_oi_payload is not None else []
    binance_ls = _parse_binance_top_ls_ratio(b_ls_payload) if b_ls_payload is not None else None
    bybit_oi = _parse_bybit_oi_hist(bb_oi_payload) if bb_oi_payload is not None else []

    # Выбираем какой OI history использовать.
    if binance_oi:
        oi_history = binance_oi
        venue = "binance"
    elif bybit_oi:
        oi_history = bybit_oi
        venue = "bybit"
    else:
        return LiquidationMagnetSignal(
            symbol=sym, oi_lookback_hours=lookback,
            timestamp_ms=now_ms, label=LABEL_UNKNOWN,
        )

    if binance_ls is not None and bybit_oi:
        venue = "binance+bybit" if binance_oi else "bybit"

    return build_liquidation_magnet_signal(
        oi_history=oi_history,
        top_trader_ratio=binance_ls,
        venue=venue,
        symbol=sym,
        lookback_hours=lookback,
        timestamp_ms=now_ms,
        oi_buildup_pct=get_oi_buildup_pct(),
        oi_buildup_strong_pct=get_oi_buildup_strong_pct(),
        ls_long_heavy=get_ls_long_heavy(),
        ls_long_extreme=get_ls_long_extreme(),
        ls_short_heavy=get_ls_short_heavy(),
        ls_short_extreme=get_ls_short_extreme(),
    )


# ─── Helpers ────────────────────────────────────────────────────────────────


def _compute_oi_limit(period: str, lookback_hours: int) -> int:
    """Сколько точек запросить чтобы покрыть lookback с запасом."""
    period_to_hours = {
        "5m": 1 / 12, "15m": 0.25, "30m": 0.5,
        "1h": 1, "2h": 2, "4h": 4, "6h": 6, "12h": 12,
        "1d": 24,
    }
    hours_per_pt = period_to_hours.get(period, 1.0)
    n = int(lookback_hours / hours_per_pt) + 2
    return min(max(n, 2), 500)  # Binance limit max=500


def _bybit_interval_for(binance_period: str) -> str:
    """Mapping Binance period → Bybit intervalTime."""
    table = {
        "5m": "5min", "15m": "15min", "30m": "30min",
        "1h": "1h", "2h": "1h",  # Bybit no 2h, fallback 1h
        "4h": "4h", "6h": "4h",  # Bybit no 6h
        "12h": "4h", "1d": "1d",
    }
    return table.get(binance_period, "1h")


# ─── Score contribution & formatting ─────────────────────────────────────────


def liquidation_magnet_score_contribution(
    signal: LiquidationMagnetSignal,
) -> tuple[int, list[str], list[str]]:
    """Свести signal к (score_delta, bullish_reasons, bearish_reasons).

    Веса консервативные (±2 max) — это leveraged-positioning indicator, шум есть.
      UP_MAGNET strong   → +2 bullish (shorts squeeze likely)
      UP_MAGNET weak     → +1 bullish
      DOWN_MAGNET strong → -2 bearish (longs flush likely)
      DOWN_MAGNET weak   → -1 bearish
      NEUTRAL / UNKNOWN  → 0
    """
    if signal is None or signal.label in (LABEL_NEUTRAL, LABEL_UNKNOWN):
        return (0, [], [])
    if signal.label == LABEL_UP_MAGNET:
        delta = 2 if signal.is_strong_signal else 1
        ratio = signal.top_long_short_ratio or 0.0
        reason = (
            f"liquidation magnet: shorts перегружены (L/S={ratio:.2f}, "
            f"OI +{signal.oi_change_pct:.1f}% за {signal.oi_lookback_hours}ч) → "
            f"возможен short squeeze"
        )
        return (delta, [reason], [])
    if signal.label == LABEL_DOWN_MAGNET:
        delta = -2 if signal.is_strong_signal else -1
        ratio = signal.top_long_short_ratio or 0.0
        reason = (
            f"liquidation magnet: longs перегружены (L/S={ratio:.2f}, "
            f"OI +{signal.oi_change_pct:.1f}% за {signal.oi_lookback_hours}ч) → "
            f"возможен flush down"
        )
        return (delta, [], [reason])
    return (0, [], [])


def format_liquidation_magnet_for_agents(signal: LiquidationMagnetSignal) -> str:
    """Markdown-блок для дебатёров."""
    if signal is None or signal.label == LABEL_UNKNOWN:
        return "🧲 **Liquidation Magnet:** нет данных (feature off / fetch failed)"
    label_text = {
        LABEL_UP_MAGNET: "🟢 UP MAGNET (shorts squeezable)",
        LABEL_DOWN_MAGNET: "🔴 DOWN MAGNET (longs liquidatable)",
        LABEL_NEUTRAL: "⚪ NEUTRAL",
    }.get(signal.label, "❔ unknown")
    strong = " (strong)" if signal.is_strong_signal else ""
    lines = [
        "🧲 **Liquidation Magnet (leveraged positioning):**",
        f"   • Symbol: {signal.symbol}  Venue: {signal.venue}",
        f"   • Aggregate: {label_text}{strong}",
        f"   • OI change: {signal.oi_change_pct:+.2f}% за {signal.oi_lookback_hours}ч "
        f"(now {signal.oi_now_contracts:,.0f} → baseline {signal.oi_baseline_contracts:,.0f} контрактов)",
    ]
    if signal.top_long_short_ratio is not None:
        lines.append(
            f"   • Top trader L/S: {signal.top_long_short_ratio:.2f}  "
            f"(longs {signal.top_long_account_pct*100:.1f}% / "
            f"shorts {signal.top_short_account_pct*100:.1f}%)"
        )
    else:
        lines.append("   • Top trader L/S: n/a (Binance unavailable)")
    return "\n".join(lines)


# ─── Env parsers ─────────────────────────────────────────────────────────────


def feature_enabled() -> bool:
    """Проверить FEATURE_LIQUIDATION_MAGNET env-флаг. Default OFF."""
    return os.environ.get("FEATURE_LIQUIDATION_MAGNET", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _env_str(name: str, default: str, *, allowed: set[str] | None = None) -> str:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if allowed is not None and raw not in allowed:
        logger.warning(
            "[LIQ-MAGNET] %s=%r not in %s — using default %s",
            name, raw, sorted(allowed), default,
        )
        return default
    return raw


def _env_float(name: str, default: float, *, min_val: float, max_val: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("[LIQ-MAGNET] %s=%r not a float — using default %s", name, raw, default)
        return default
    if v < min_val or v > max_val:
        logger.warning(
            "[LIQ-MAGNET] %s=%s outside [%s, %s] — using default %s",
            name, v, min_val, max_val, default,
        )
        return default
    return v


def _env_int(name: str, default: int, *, min_val: int, max_val: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("[LIQ-MAGNET] %s=%r not an int — using default %s", name, raw, default)
        return default
    if v < min_val or v > max_val:
        logger.warning(
            "[LIQ-MAGNET] %s=%s outside [%s, %s] — using default %s",
            name, v, min_val, max_val, default,
        )
        return default
    return v


VALID_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


def get_symbol() -> str:
    raw = os.environ.get("LIQUIDATION_MAGNET_SYMBOL", "BTCUSDT").strip()
    return raw.upper() if raw else "BTCUSDT"


def get_period() -> str:
    return _env_str("LIQUIDATION_MAGNET_PERIOD", "1h", allowed=VALID_PERIODS)


def get_lookback_hours() -> int:
    return _env_int("LIQUIDATION_MAGNET_LOOKBACK_HOURS", 24, min_val=1, max_val=168)


def get_oi_buildup_pct() -> float:
    return _env_float(
        "LIQUIDATION_MAGNET_OI_BUILDUP_PCT",
        DEFAULT_OI_BUILDUP_PCT, min_val=0.0, max_val=200.0,
    )


def get_oi_buildup_strong_pct() -> float:
    return _env_float(
        "LIQUIDATION_MAGNET_OI_BUILDUP_STRONG_PCT",
        DEFAULT_OI_BUILDUP_STRONG_PCT, min_val=0.0, max_val=500.0,
    )


def get_ls_long_heavy() -> float:
    return _env_float(
        "LIQUIDATION_MAGNET_LS_LONG_HEAVY",
        DEFAULT_LS_LONG_HEAVY, min_val=1.0, max_val=10.0,
    )


def get_ls_long_extreme() -> float:
    return _env_float(
        "LIQUIDATION_MAGNET_LS_LONG_EXTREME",
        DEFAULT_LS_LONG_EXTREME, min_val=1.0, max_val=20.0,
    )


def get_ls_short_heavy() -> float:
    return _env_float(
        "LIQUIDATION_MAGNET_LS_SHORT_HEAVY",
        DEFAULT_LS_SHORT_HEAVY, min_val=0.05, max_val=1.0,
    )


def get_ls_short_extreme() -> float:
    return _env_float(
        "LIQUIDATION_MAGNET_LS_SHORT_EXTREME",
        DEFAULT_LS_SHORT_EXTREME, min_val=0.01, max_val=1.0,
    )


# ─── Production aiohttp factory ──────────────────────────────────────────────


def make_aiohttp_http_client(session: Any) -> HttpClient:
    """Обёртка aiohttp.ClientSession под наш HttpClient interface."""

    async def _call(*, method: str, url: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        async with session.request(method, url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"{url} HTTP {resp.status}")
            return await resp.json(content_type=None)

    return _call
