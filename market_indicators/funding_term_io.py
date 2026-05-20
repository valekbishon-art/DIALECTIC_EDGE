"""I/O для funding term structure.

Bybit V5 + Binance USDS-M futures: текущий perp funding rate + цены deliverable
фьючерсов (квартальных и месячных). Используем для построения term structure.

Всё DI-based: тесты подменяют HTTP-клиент моком, без сетевых вызовов.
Не дублируем signals.py — отдельный изолированный путь, чтобы не задеть
торговую логику.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

from market_indicators.funding_term_structure import (
    BasisPoint,
    DEFAULT_QUARTERLY_DAYS_MAX,
    FundingRateSnapshot,
    TermStructureSignal,
    build_term_structure,
    estimate_days_to_expiry,
    parse_bybit_quarterly_symbol,
)

logger = logging.getLogger(__name__)

#: Callable интерфейс HTTP-клиента (как в microstructure_io). Тесты замокивают.
HttpClient = Callable[..., Awaitable[Any]]


# ─── Bybit V5 ────────────────────────────────────────────────────────────────


def _bybit_funding_args(asset: str) -> dict[str, Any]:
    """Bybit V5: текущий funding rate с tickers endpoint."""
    return {
        "method": "GET",
        "url": "https://api.bybit.com/v5/market/tickers",
        "params": {"category": "linear", "symbol": f"{asset.upper()}USDT"},
    }


def _parse_bybit_funding(payload: Any, *, asset: str) -> FundingRateSnapshot | None:
    try:
        result = payload.get("result") or {}
        items = result.get("list") or []
        if not items:
            return None
        row = items[0]
        rate = float(row.get("fundingRate") or 0.0)
        next_ts = row.get("nextFundingTime")
        try:
            next_ts_int = int(next_ts) if next_ts else None
        except (TypeError, ValueError):
            next_ts_int = None
        return FundingRateSnapshot(
            venue="bybit",
            symbol=str(row.get("symbol") or f"{asset.upper()}USDT"),
            asset=asset.upper(),
            rate=rate,
            period_hours=8.0,  # Bybit linear USDT = 8h
            next_funding_time_ms=next_ts_int,
            timestamp_ms=int(datetime.utcnow().timestamp() * 1000),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.warning("bybit funding parse failed: %s", e)
        return None


def _bybit_deliverable_args() -> dict[str, Any]:
    """Все linear delivery контракты Bybit (BTC/ETH в основном)."""
    return {
        "method": "GET",
        "url": "https://api.bybit.com/v5/market/tickers",
        "params": {"category": "linear"},
    }


def _parse_bybit_deliverable(
    payload: Any,
    *,
    asset: str,
    spot_price: float,
    now: datetime | None = None,
) -> list[BasisPoint]:
    """Из tickers'а linear отфильтровать deliverable-фьючерсы по нужному asset.

    Bybit linear делится на perp (BTCUSDT) и delivery (BTCUSDT-26DEC25 или
    BTC-31JAN26). Берём только delivery.
    """
    if spot_price <= 0:
        return []
    moment = now or datetime.utcnow()
    out: list[BasisPoint] = []
    try:
        result = payload.get("result") or {}
        items = result.get("list") or []
        prefix = asset.upper()
        for row in items:
            sym = str(row.get("symbol") or "")
            if "-" not in sym or not sym.startswith(prefix):
                continue
            expiry = parse_bybit_quarterly_symbol(sym)
            if expiry is None:
                continue
            d2e = estimate_days_to_expiry(expiry_date=expiry, now=moment)
            if d2e <= 0 or d2e > DEFAULT_QUARTERLY_DAYS_MAX + 60:
                continue
            try:
                last = float(row.get("lastPrice") or 0.0)
                if last <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            out.append(BasisPoint(
                venue="bybit", symbol=sym, asset=asset.upper(),
                futures_price=last, spot_price=spot_price,
                days_to_expiry=d2e,
            ))
    except (AttributeError, KeyError, TypeError) as e:
        logger.warning("bybit deliverable parse failed: %s", e)
    return out


# ─── Binance USDS-M ─────────────────────────────────────────────────────────


def _binance_funding_args(asset: str) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": "https://fapi.binance.com/fapi/v1/premiumIndex",
        "params": {"symbol": f"{asset.upper()}USDT"},
    }


def _parse_binance_funding(payload: Any, *, asset: str) -> FundingRateSnapshot | None:
    try:
        rate = float(payload.get("lastFundingRate") or 0.0)
        next_ts = payload.get("nextFundingTime")
        try:
            next_ts_int = int(next_ts) if next_ts else None
        except (TypeError, ValueError):
            next_ts_int = None
        return FundingRateSnapshot(
            venue="binance",
            symbol=str(payload.get("symbol") or f"{asset.upper()}USDT"),
            asset=asset.upper(),
            rate=rate,
            period_hours=8.0,
            next_funding_time_ms=next_ts_int,
            timestamp_ms=int(datetime.utcnow().timestamp() * 1000),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.warning("binance funding parse failed: %s", e)
        return None


def _binance_spot_price_args(asset: str) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": "https://api.binance.com/api/v3/ticker/price",
        "params": {"symbol": f"{asset.upper()}USDT"},
    }


def _parse_binance_spot_price(payload: Any) -> float:
    try:
        return float(payload.get("price") or 0.0)
    except (AttributeError, KeyError, TypeError, ValueError):
        return 0.0


def _binance_deliverable_args() -> dict[str, Any]:
    """Coin-M deliverable contracts: BTCUSD_QUARTERLY (квартальные) и
    BTCUSD_NEXT_QUARTER (ближайший квартал).

    Binance USDS-M в основном perp; для quarterly basis carry используется
    Coin-M (Inverse contracts: BTCUSD_240627 и т.д.).
    """
    return {
        "method": "GET",
        "url": "https://dapi.binance.com/dapi/v1/premiumIndex",
        "params": {},
    }


def _parse_binance_deliverable(
    payload: Any,
    *,
    asset: str,
    spot_price: float,
    now: datetime | None = None,
) -> list[BasisPoint]:
    """Из массива premiumIndex Coin-M отфильтровать deliverable контракты
    по asset.

    Binance Coin-M deliverable symbol: 'BTCUSD_240927' (YYMMDD).
    """
    if spot_price <= 0:
        return []
    moment = now or datetime.utcnow()
    out: list[BasisPoint] = []
    try:
        items = payload if isinstance(payload, list) else []
        prefix = f"{asset.upper()}USD_"
        for row in items:
            sym = str(row.get("symbol") or "")
            if not sym.startswith(prefix):
                continue
            # YYMMDD после "_"
            tail = sym.split("_")[-1]
            if not tail.isdigit() or len(tail) != 6:
                continue
            try:
                year = 2000 + int(tail[:2])
                month = int(tail[2:4])
                day = int(tail[4:6])
                if not (1 <= month <= 12 and 1 <= day <= 31):
                    continue
                expiry = datetime(year, month, day, 8, 0, 0)
            except ValueError:
                continue

            d2e = estimate_days_to_expiry(expiry_date=expiry, now=moment)
            if d2e <= 0 or d2e > DEFAULT_QUARTERLY_DAYS_MAX + 60:
                continue
            try:
                mark = float(row.get("markPrice") or 0.0)
                if mark <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            out.append(BasisPoint(
                venue="binance", symbol=sym, asset=asset.upper(),
                futures_price=mark, spot_price=spot_price,
                days_to_expiry=d2e,
            ))
    except (AttributeError, KeyError, TypeError) as e:
        logger.warning("binance deliverable parse failed: %s", e)
    return out


# ─── Fetcher с DI ───────────────────────────────────────────────────────────


async def _call_http(http_client: HttpClient, args: dict[str, Any]) -> Any:
    """Универсальный HTTP-вызов через DI-клиент. Возвращает json/dict.

    http_client signature: (method, url, params=None, json=None, timeout=...) → Any.
    """
    return await http_client(
        method=args["method"],
        url=args["url"],
        params=args.get("params"),
        json=args.get("json"),
        timeout=args.get("timeout", 8.0),
    )


async def fetch_term_structure(
    *,
    asset: str,
    http_client: HttpClient,
    now: datetime | None = None,
) -> TermStructureSignal:
    """Полный pipeline для одного актива: вызывает Bybit + Binance,
    собирает funding-snapshots и basis-points, строит TermStructureSignal.

    Per-call try/except — если один venue упал, продолжаем с остальными.
    """
    moment = now or datetime.utcnow()
    timestamp_ms = int(moment.timestamp() * 1000)

    funding_snaps: list[FundingRateSnapshot] = []
    basis_points: list[BasisPoint] = []
    spot_price = 0.0

    # 1. Spot price (нужен для basis carry — futures / spot).
    try:
        sp = await _call_http(http_client, _binance_spot_price_args(asset))
        spot_price = _parse_binance_spot_price(sp)
    except Exception as e:  # noqa: BLE001
        logger.warning("term structure spot price (%s) failed: %s", asset, e)

    # 2. Bybit funding (perp).
    try:
        bb = await _call_http(http_client, _bybit_funding_args(asset))
        snap = _parse_bybit_funding(bb, asset=asset)
        if snap is not None:
            funding_snaps.append(snap)
    except Exception as e:  # noqa: BLE001
        logger.warning("term structure bybit funding (%s) failed: %s", asset, e)

    # 3. Binance funding (perp).
    try:
        bn = await _call_http(http_client, _binance_funding_args(asset))
        snap = _parse_binance_funding(bn, asset=asset)
        if snap is not None:
            funding_snaps.append(snap)
    except Exception as e:  # noqa: BLE001
        logger.warning("term structure binance funding (%s) failed: %s", asset, e)

    # 4. Bybit delivery (deliverable futures).
    if spot_price > 0:
        try:
            bb_d = await _call_http(http_client, _bybit_deliverable_args())
            basis_points.extend(_parse_bybit_deliverable(
                bb_d, asset=asset, spot_price=spot_price, now=moment,
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning("term structure bybit deliverable (%s) failed: %s", asset, e)

    # 5. Binance Coin-M deliverable.
    if spot_price > 0:
        try:
            bn_d = await _call_http(http_client, _binance_deliverable_args())
            basis_points.extend(_parse_binance_deliverable(
                bn_d, asset=asset, spot_price=spot_price, now=moment,
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning("term structure binance deliverable (%s) failed: %s", asset, e)

    return build_term_structure(
        asset=asset,
        funding_snapshots=funding_snaps,
        basis_points=basis_points,
        timestamp_ms=timestamp_ms,
    )


# ─── Persistence + previous lookup ──────────────────────────────────────────


async def persist_signal(signal: TermStructureSignal) -> None:
    """Сохранить TermStructureSignal в БД."""
    from database import save_funding_term_snapshot  # noqa: PLC0415
    await save_funding_term_snapshot(
        asset=signal.asset,
        timestamp_ms=signal.timestamp_ms,
        spot_funding_annual=signal.spot_funding_annual,
        monthly_basis_annual=signal.monthly_basis_annual,
        quarterly_basis_annual=signal.quarterly_basis_annual,
        slope_annual=signal.slope_annual,
        is_inverted=int(signal.is_inverted),
        venues_csv=",".join(signal.venues_used),
    )


async def get_previous_signal(
    *, asset: str, lookback_hours: float = 24,
) -> TermStructureSignal | None:
    """Загрузить предыдущий снимок по asset (для inversion detection)."""
    from database import get_recent_funding_term_snapshots  # noqa: PLC0415
    rows = await get_recent_funding_term_snapshots(asset=asset, limit=10)
    for row in rows:
        try:
            return TermStructureSignal(
                asset=str(row["asset"]),
                timestamp_ms=int(row["timestamp_ms"]),
                spot_funding_annual=(
                    float(row["spot_funding_annual"])
                    if row.get("spot_funding_annual") is not None else None
                ),
                monthly_basis_annual=(
                    float(row["monthly_basis_annual"])
                    if row.get("monthly_basis_annual") is not None else None
                ),
                quarterly_basis_annual=(
                    float(row["quarterly_basis_annual"])
                    if row.get("quarterly_basis_annual") is not None else None
                ),
                slope_annual=(
                    float(row["slope_annual"])
                    if row.get("slope_annual") is not None else None
                ),
                is_inverted=bool(row.get("is_inverted") or 0),
                venues_used=tuple(
                    str(row.get("venues_csv") or "").split(",")
                ) if row.get("venues_csv") else (),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return None


# ─── Env-flags ───────────────────────────────────────────────────────────────


def feature_enabled() -> bool:
    return os.getenv("FEATURE_FUNDING_TERM", "0").strip() in {"1", "true", "True", "yes"}


def get_symbols() -> tuple[str, ...]:
    raw = os.getenv("FUNDING_TERM_SYMBOLS", "BTC,ETH")
    parts = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return tuple(dict.fromkeys(parts)) if parts else ("BTC", "ETH")


def get_interval_seconds() -> int:
    try:
        return max(300, int(os.getenv("FUNDING_TERM_INTERVAL_SEC", "1800")))
    except (TypeError, ValueError):
        return 1800


# ─── Aiohttp factory (для scheduler — НЕ для тестов) ────────────────────────


async def make_aiohttp_http_client(session: Any) -> HttpClient:
    """Создать HttpClient над уже открытым aiohttp.ClientSession.

    Полностью изолирован от тестов: тесты используют моки.
    """
    async def _call(*, method: str, url: str, params=None, json=None, timeout=8.0):
        import aiohttp  # noqa: PLC0415
        to = aiohttp.ClientTimeout(total=float(timeout))
        if method.upper() == "GET":
            async with session.get(url, params=params, timeout=to) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return await resp.json()
        elif method.upper() == "POST":
            async with session.post(url, params=params, json=json, timeout=to) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return await resp.json()
        raise RuntimeError(f"Unsupported method {method}")
    return _call


# ─── Logging helper ─────────────────────────────────────────────────────────


def format_term_summary(signal: TermStructureSignal, *, event: str | None = None) -> str:
    """Удобная строка для логов."""
    def _pct(x: float | None) -> str:
        if x is None:
            return "n/a"
        return f"{x * 100:.2f}%"
    prefix = f"📉 term-structure {signal.asset}"
    if event:
        prefix = f"⚠️ {event} {signal.asset}"
    return (
        f"{prefix}: spot_f={_pct(signal.spot_funding_annual)} "
        f"30d_b={_pct(signal.monthly_basis_annual)} "
        f"90d_b={_pct(signal.quarterly_basis_annual)} "
        f"slope={_pct(signal.slope_annual)} "
        f"inv={'Y' if signal.is_inverted else 'N'} "
        f"venues={','.join(signal.venues_used) or 'none'}"
    )
