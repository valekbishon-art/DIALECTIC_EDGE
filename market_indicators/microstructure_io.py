"""I/O для cross-exchange microstructure forensics.

Отделено от чистой математики (`microstructure.py`), чтобы математика
оставалась stdlib-only и тестировалась без сети / БД. Здесь:

  * Per-venue REST-парсеры (Binance, Bybit, OKX, Bitget, Hyperliquid).
  * Async fetcher для всех venue с timeout + per-venue isolation
    (один глюк не валит остальных).
  * Wrappers вокруг database.py для save/baseline.
  * `feature_enabled()` — единый источник правды для `FEATURE_MICROSTRUCTURE`.

Все внешние deps — через DI (injected callables), чтобы тесты могли
запускаться без aiohttp / aiosqlite.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from market_indicators.microstructure import (
    DEFAULT_BAND_PCT,
    DEFAULT_MIN_VENUES_FOR_AGGREGATE,
    DEFAULT_VACUUM_DROP_PCT,
    MicrostructureSignal,
    OrderbookLevel,
    VenueMicrostructure,
    build_venue_snapshot,
    classify_signal,
    compute_aggregate,
    normalize_levels,
)

logger = logging.getLogger(__name__)


# ─── Поддерживаемые venue + URLs ─────────────────────────────────────────────

#: Имена venue в lowercase. Используются как ключи в DB и в env-переменных.
SUPPORTED_VENUES: tuple[str, ...] = (
    "binance",
    "bybit",
    "okx",
    "bitget",
    "hyperliquid",
)

#: Дефолтный depth-limit, который мы запрашиваем у venue. Берём 20 уровней
#: — этого хватает на полосе 0.5% для большинства ликвидных пар.
DEFAULT_DEPTH_LIMIT = 20

#: Тип http-клиента: callable, который принимает (method, url, params, json_body)
#: и возвращает распарсенный JSON (dict|list). Используем DI чтобы тесты
#: не зависели от aiohttp.
HttpClient = Callable[..., Awaitable[Any]]


# ─── HTTP fetch для каждого venue (без зависимостей от aiohttp) ──────────────


@dataclass(frozen=True)
class VenueEndpoint:
    """Спецификация REST endpoint'а одного venue.

    `format_symbol` — функция, которая из asset='BTC' делает venue-symbol.
    `method` — GET (большинство) или POST (Hyperliquid).
    `make_request_args` — (asset, depth_limit) -> kwargs для http_client.
    """

    name: str
    method: str
    base_url: str
    format_symbol: Callable[[str], str]
    make_request_args: Callable[[str, int], dict]
    parse_levels: Callable[[Any], tuple[tuple[OrderbookLevel, ...], tuple[OrderbookLevel, ...]]]


def _binance_args(asset: str, limit: int) -> dict:
    symbol = f"{asset.upper()}USDT"
    return {
        "url": "https://api.binance.com/api/v3/depth",
        "params": {"symbol": symbol, "limit": int(limit)},
    }


def _parse_binance(payload: Any) -> tuple[tuple[OrderbookLevel, ...], tuple[OrderbookLevel, ...]]:
    """Binance spot/futures depth → (bids, asks).

    Формат: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}.
    """
    if not isinstance(payload, dict):
        return (), ()
    return (
        normalize_levels(payload.get("bids") or []),
        normalize_levels(payload.get("asks") or []),
    )


def _bybit_args(asset: str, limit: int) -> dict:
    symbol = f"{asset.upper()}USDT"
    return {
        "url": "https://api.bybit.com/v5/market/orderbook",
        "params": {"category": "linear", "symbol": symbol, "limit": int(limit)},
    }


def _parse_bybit(payload: Any) -> tuple[tuple[OrderbookLevel, ...], tuple[OrderbookLevel, ...]]:
    """Bybit V5 orderbook → (bids, asks).

    Формат: {"result": {"b": [[price, size], ...], "a": [[price, size], ...]}, ...}.
    """
    if not isinstance(payload, dict):
        return (), ()
    result = payload.get("result")
    if not isinstance(result, dict):
        return (), ()
    return (
        normalize_levels(result.get("b") or []),
        normalize_levels(result.get("a") or []),
    )


def _okx_args(asset: str, limit: int) -> dict:
    inst = f"{asset.upper()}-USDT-SWAP"
    return {
        "url": "https://www.okx.com/api/v5/market/books",
        "params": {"instId": inst, "sz": int(limit)},
    }


def _parse_okx(payload: Any) -> tuple[tuple[OrderbookLevel, ...], tuple[OrderbookLevel, ...]]:
    """OKX swap depth → (bids, asks).

    Формат: {"data": [{"bids": [[px, sz, _, _], ...], "asks": [[px, sz, _, _], ...]}]}.
    Игнорируем 3-й/4-й elements (числа орд'еров на уровне).
    """
    if not isinstance(payload, dict):
        return (), ()
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return (), ()
    first = data[0]
    if not isinstance(first, dict):
        return (), ()
    return (
        normalize_levels([row[:2] for row in (first.get("bids") or []) if len(row) >= 2]),
        normalize_levels([row[:2] for row in (first.get("asks") or []) if len(row) >= 2]),
    )


def _bitget_args(asset: str, limit: int) -> dict:
    symbol = f"{asset.upper()}USDT"
    # USDT-margined perpetual futures.
    return {
        "url": "https://api.bitget.com/api/v2/mix/market/merge-depth",
        "params": {
            "symbol": symbol,
            "productType": "usdt-futures",
            "precision": "scale0",
            "limit": str(int(limit)),
        },
    }


def _parse_bitget(payload: Any) -> tuple[tuple[OrderbookLevel, ...], tuple[OrderbookLevel, ...]]:
    """Bitget merge-depth → (bids, asks).

    Формат: {"data": {"bids": [[price, size], ...], "asks": [[price, size], ...]}}.
    """
    if not isinstance(payload, dict):
        return (), ()
    data = payload.get("data")
    if not isinstance(data, dict):
        return (), ()
    return (
        normalize_levels(data.get("bids") or []),
        normalize_levels(data.get("asks") or []),
    )


def _hyperliquid_args(asset: str, _limit: int) -> dict:
    # Hyperliquid info endpoint, POST с JSON body. `_limit` игнорируется
    # (HL отдаёт фиксированный набор уровней).
    return {
        "url": "https://api.hyperliquid.xyz/info",
        "json": {"type": "l2Book", "coin": asset.upper()},
    }


def _parse_hyperliquid(
    payload: Any,
) -> tuple[tuple[OrderbookLevel, ...], tuple[OrderbookLevel, ...]]:
    """Hyperliquid L2 book → (bids, asks).

    Формат: {"levels": [[{"px": "x", "sz": "y", "n": k}, ...], [...]], "coin": ...}.
    levels[0] — bids, levels[1] — asks.
    """
    if not isinstance(payload, dict):
        return (), ()
    levels = payload.get("levels")
    if not isinstance(levels, list) or len(levels) < 2:
        return (), ()

    def _pairs(side: Any) -> list[tuple[float, float]]:
        if not isinstance(side, list):
            return []
        out: list[tuple[float, float]] = []
        for lvl in side:
            if not isinstance(lvl, dict):
                continue
            raw_px = lvl.get("px")
            raw_sz = lvl.get("sz")
            if raw_px is None or raw_sz is None:
                continue
            try:
                px = float(raw_px)
                sz = float(raw_sz)
            except (TypeError, ValueError):
                continue
            out.append((px, sz))
        return out

    return (
        normalize_levels(_pairs(levels[0])),
        normalize_levels(_pairs(levels[1])),
    )


VENUES: dict[str, VenueEndpoint] = {
    "binance": VenueEndpoint(
        name="binance",
        method="GET",
        base_url="https://api.binance.com",
        format_symbol=lambda a: f"{a.upper()}USDT",
        make_request_args=_binance_args,
        parse_levels=_parse_binance,
    ),
    "bybit": VenueEndpoint(
        name="bybit",
        method="GET",
        base_url="https://api.bybit.com",
        format_symbol=lambda a: f"{a.upper()}USDT",
        make_request_args=_bybit_args,
        parse_levels=_parse_bybit,
    ),
    "okx": VenueEndpoint(
        name="okx",
        method="GET",
        base_url="https://www.okx.com",
        format_symbol=lambda a: f"{a.upper()}-USDT-SWAP",
        make_request_args=_okx_args,
        parse_levels=_parse_okx,
    ),
    "bitget": VenueEndpoint(
        name="bitget",
        method="GET",
        base_url="https://api.bitget.com",
        format_symbol=lambda a: f"{a.upper()}USDT",
        make_request_args=_bitget_args,
        parse_levels=_parse_bitget,
    ),
    "hyperliquid": VenueEndpoint(
        name="hyperliquid",
        method="POST",
        base_url="https://api.hyperliquid.xyz",
        format_symbol=lambda a: a.upper(),
        make_request_args=_hyperliquid_args,
        parse_levels=_parse_hyperliquid,
    ),
}


# ─── Feature flag + config ───────────────────────────────────────────────────


def feature_enabled() -> bool:
    """`FEATURE_MICROSTRUCTURE=1` включает. Дефолт — OFF."""
    return os.getenv("FEATURE_MICROSTRUCTURE", "0").strip() in {"1", "true", "True", "yes"}


def _parse_csv_env(name: str, default: Sequence[str]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return tuple(default)
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def get_enabled_venues() -> tuple[str, ...]:
    """Из env `MICROSTRUCTURE_VENUES=binance,bybit,...`. По умолчанию — все."""
    requested = _parse_csv_env("MICROSTRUCTURE_VENUES", SUPPORTED_VENUES)
    return tuple(v for v in requested if v in VENUES)


def get_symbols() -> tuple[str, ...]:
    """Из env `MICROSTRUCTURE_SYMBOLS=BTC,ETH`. По умолчанию BTC, ETH."""
    return tuple(s.upper() for s in _parse_csv_env("MICROSTRUCTURE_SYMBOLS", ("BTC", "ETH")))


def get_band_pct() -> float:
    try:
        return float(os.getenv("MICROSTRUCTURE_BAND_PCT", str(DEFAULT_BAND_PCT)))
    except (TypeError, ValueError):
        return DEFAULT_BAND_PCT


def get_vacuum_drop_pct() -> float:
    try:
        return float(os.getenv("MICROSTRUCTURE_VACUUM_DROP_PCT", str(DEFAULT_VACUUM_DROP_PCT)))
    except (TypeError, ValueError):
        return DEFAULT_VACUUM_DROP_PCT


def get_interval_seconds() -> int:
    try:
        return max(60, int(os.getenv("MICROSTRUCTURE_INTERVAL_SEC", "300")))
    except (TypeError, ValueError):
        return 300


# ─── Per-venue fetch ─────────────────────────────────────────────────────────


async def fetch_venue_snapshot(
    *,
    venue_name: str,
    asset: str,
    http_client: HttpClient,
    depth_limit: int = DEFAULT_DEPTH_LIMIT,
    band_pct: float = DEFAULT_BAND_PCT,
    timestamp_ms: int | None = None,
    timeout_sec: float = 5.0,
) -> VenueMicrostructure | None:
    """Fetch одного venue с timeout-isolation. None при любой ошибке.

    http_client должен принимать kwargs: method, url, params (optional),
    json (optional), timeout_sec, и возвращать парсенный JSON.
    """
    if venue_name not in VENUES:
        logger.warning("microstructure: unknown venue %r — skipping", venue_name)
        return None

    endpoint = VENUES[venue_name]
    req = endpoint.make_request_args(asset, depth_limit)
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

    try:
        payload = await asyncio.wait_for(
            http_client(method=endpoint.method, timeout_sec=timeout_sec, **req),
            timeout=timeout_sec + 1.0,
        )
    except asyncio.TimeoutError:
        logger.warning("microstructure %s/%s: timeout (%ss)", venue_name, asset, timeout_sec)
        return None
    except Exception as e:  # noqa: BLE001 — venue HTTP может выдать что угодно
        logger.warning("microstructure %s/%s: fetch error %s", venue_name, asset, e)
        return None

    bids, asks = endpoint.parse_levels(payload)
    if not bids or not asks:
        logger.info(
            "microstructure %s/%s: пустой/невалидный стакан (bids=%d, asks=%d)",
            venue_name, asset, len(bids), len(asks),
        )
        return None

    return build_venue_snapshot(
        venue=venue_name,
        bids=bids,
        asks=asks,
        band_pct=band_pct,
        timestamp_ms=ts,
    )


async def gather_all_venues(
    *,
    asset: str,
    http_client: HttpClient,
    venues: Sequence[str] | None = None,
    band_pct: float | None = None,
    depth_limit: int = DEFAULT_DEPTH_LIMIT,
    timeout_sec: float = 5.0,
) -> list[VenueMicrostructure]:
    """Запросить все venue параллельно через asyncio.gather. None отфильтрованы."""
    venue_list = list(venues or get_enabled_venues())
    band = band_pct if band_pct is not None else get_band_pct()
    ts = int(time.time() * 1000)
    tasks = [
        fetch_venue_snapshot(
            venue_name=v,
            asset=asset,
            http_client=http_client,
            depth_limit=depth_limit,
            band_pct=band,
            timestamp_ms=ts,
            timeout_sec=timeout_sec,
        )
        for v in venue_list
    ]
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[VenueMicrostructure] = []
    for v_name, res in zip(venue_list, raw):
        if isinstance(res, BaseException):
            logger.warning("microstructure %s/%s: gather exception %s", v_name, asset, res)
            continue
        if isinstance(res, VenueMicrostructure):
            out.append(res)
    return out


# ─── Aggregation pipeline ────────────────────────────────────────────────────


async def compute_microstructure_signal(
    *,
    asset: str,
    http_client: HttpClient,
    baseline_provider: Callable[[str], Awaitable[float | None]] | None = None,
    venues: Sequence[str] | None = None,
    band_pct: float | None = None,
    vacuum_drop_pct: float | None = None,
    depth_limit: int = DEFAULT_DEPTH_LIMIT,
    timeout_sec: float = 5.0,
    min_venues: int = DEFAULT_MIN_VENUES_FOR_AGGREGATE,
) -> MicrostructureSignal | None:
    """End-to-end pipeline для одного asset'а.

    1. Fetch все venue.
    2. Compute aggregate.
    3. Получить baseline через baseline_provider (DI).
    4. Classify в MicrostructureSignal.
    """
    snapshots = await gather_all_venues(
        asset=asset,
        http_client=http_client,
        venues=venues,
        band_pct=band_pct,
        depth_limit=depth_limit,
        timeout_sec=timeout_sec,
    )
    if not snapshots:
        return None

    ts = snapshots[0].timestamp_ms
    aggregate = compute_aggregate(
        snapshots, asset=asset, timestamp_ms=ts, min_venues=min_venues
    )
    if aggregate is None:
        return None

    baseline: float | None = None
    if baseline_provider is not None:
        try:
            baseline = await baseline_provider(asset)
        except Exception as e:  # noqa: BLE001 — baseline fetch can fail
            logger.warning("microstructure baseline fetch для %s упал: %s", asset, e)
            baseline = None

    drop_pct = vacuum_drop_pct if vacuum_drop_pct is not None else get_vacuum_drop_pct()
    return classify_signal(
        aggregate,
        baseline_depth_usd=baseline,
        vacuum_drop_pct=drop_pct,
    )


# ─── DB persistence wrappers ─────────────────────────────────────────────────
#
# Изолированы за try/except — модуль может быть импортирован без БД (например,
# в тестах, которые используют моки).


async def persist_signal(signal: MicrostructureSignal) -> None:
    """Сохранить snapshot в SQLite. Silently no-op если БД недоступна."""
    try:
        from database import save_microstructure_snapshot  # noqa: PLC0415
    except ImportError:
        logger.debug("microstructure: database module unavailable, skipping persist")
        return

    aggregate = signal.aggregate
    try:
        await save_microstructure_snapshot(
            asset=aggregate.asset,
            timestamp_ms=aggregate.timestamp_ms,
            mid_price=aggregate.mid_price_weighted,
            bid_depth_usd=aggregate.bid_depth_usd_total,
            ask_depth_usd=aggregate.ask_depth_usd_total,
            asymmetry=aggregate.asymmetry_weighted,
            quoted_spread_bps=aggregate.quoted_spread_bps_weighted,
            venue_count=aggregate.venue_count,
            venues_csv=",".join(aggregate.venues),
            vacuum_flag=bool(signal.vacuum),
            direction_bias=int(signal.direction_bias),
            severity=float(signal.severity),
            baseline_depth_usd=signal.baseline_depth_usd,
            drop_pct_observed=signal.drop_pct_observed,
        )
    except Exception as e:  # noqa: BLE001 — DB failure shouldn't kill loop
        logger.warning("microstructure persist для %s упал: %s", aggregate.asset, e)


async def get_baseline_depth(asset: str, lookback_hours: int = 24) -> float | None:
    """Среднее total_depth_usd за окно. None если нет данных в БД."""
    try:
        from database import get_microstructure_baseline_depth  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return await get_microstructure_baseline_depth(asset=asset, lookback_hours=lookback_hours)
    except Exception as e:  # noqa: BLE001
        logger.warning("microstructure baseline для %s упал: %s", asset, e)
        return None


# ─── aiohttp-based default http client (опциональный) ────────────────────────


async def make_aiohttp_http_client(session: Any) -> HttpClient:
    """Wrap aiohttp.ClientSession в нашу HttpClient signature.

    session должен быть открытой ClientSession. Используется в production
    `_microstructure_loop`. Возвращает callable, который можно передать в
    `fetch_venue_snapshot`.
    """

    async def _call(
        *,
        method: str,
        url: str,
        params: dict | None = None,
        json: dict | None = None,
        timeout_sec: float = 5.0,
    ) -> Any:
        import aiohttp  # noqa: PLC0415 — local import: scheduler уже грузит aiohttp

        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        m = (method or "GET").upper()
        if m == "GET":
            async with session.get(url, params=params, timeout=timeout) as resp:
                resp.raise_for_status()
                return await resp.json()
        elif m == "POST":
            async with session.post(url, json=json, timeout=timeout) as resp:
                resp.raise_for_status()
                return await resp.json()
        raise ValueError(f"Unsupported method: {method!r}")

    return _call
