"""I/O для options skew (Deribit).

Deribit публичный API (без ключей):
  GET /public/get_book_summary_by_currency?currency=BTC&kind=option
    → массив всех живых option-инструментов с mark_iv (в процентах!),
      underlying_price, last_price, instrument_name.
  GET /public/get_index_price?index_name=btc_usd
    → spot index (на случай если mark_iv-snapshot пустой).

Всё DI-based: тесты подменяют HTTP-клиент моком, без сетевых вызовов.
Не дублирует signals.py / market_indicators/funding_term_io.py — отдельный
изолированный путь, чтобы не задеть торговую логику.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

from market_indicators.options_skew import (
    OptionQuote,
    OptionsSkewSignal,
    build_options_skew,
    parse_deribit_option_name,
)

logger = logging.getLogger(__name__)

#: Callable интерфейс HTTP-клиента (как в funding_term_io). Тесты замокивают.
HttpClient = Callable[..., Awaitable[Any]]


# ─── Deribit ─────────────────────────────────────────────────────────────────


def _deribit_book_summary_args(currency: str) -> dict[str, Any]:
    """Deribit: book summary по всем option-инструментам данной валюты."""
    return {
        "method": "GET",
        "url": "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        "params": {"currency": currency.upper(), "kind": "option"},
    }


def _deribit_index_args(currency: str) -> dict[str, Any]:
    """Deribit: текущий index price (btc_usd / eth_usd)."""
    return {
        "method": "GET",
        "url": "https://www.deribit.com/api/v2/public/get_index_price",
        "params": {"index_name": f"{currency.lower()}_usd"},
    }


def _parse_deribit_index(payload: Any) -> float | None:
    try:
        result = payload.get("result") or {}
        idx = result.get("index_price")
        if idx is None:
            return None
        v = float(idx)
        return v if v > 0 else None
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.warning("deribit index parse failed: %s", e)
        return None


def _parse_deribit_options(
    payload: Any, *, currency: str,
) -> tuple[list[OptionQuote], float | None]:
    """Из book_summary вытащить список OptionQuote + spot (medianized).

    Deribit отдаёт `mark_iv` в **процентах** (65.0 = 65% годовых) — конвертим
    в долю. `underlying_price` есть в каждой строке (одинаковый для всех опций
    одного underlying'а), берём медиану как fallback для spot.
    """
    out: list[OptionQuote] = []
    spots: list[float] = []
    try:
        rows = payload.get("result") if isinstance(payload, dict) else payload
        rows = rows or []
        cur_upper = currency.upper()
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("instrument_name") or "")
            parsed = parse_deribit_option_name(name)
            if parsed is None or parsed["currency"] != cur_upper:
                continue
            try:
                mark_iv_pct = float(row.get("mark_iv") or 0.0)
            except (TypeError, ValueError):
                continue
            if mark_iv_pct <= 0.0:
                continue
            try:
                underlying = float(row.get("underlying_price") or 0.0)
            except (TypeError, ValueError):
                underlying = 0.0
            if underlying > 0:
                spots.append(underlying)
            expiry_dt = parsed["expiry"]
            expiry_ms = int(expiry_dt.timestamp() * 1000) if hasattr(expiry_dt, "timestamp") else 0
            out.append(OptionQuote(
                instrument_name=name,
                currency=cur_upper,
                kind=str(parsed["kind"]),
                strike=float(parsed["strike"]),
                expiry_ms=expiry_ms,
                mark_iv=mark_iv_pct / 100.0,
                underlying_price=underlying,
            ))
    except (AttributeError, KeyError, TypeError) as e:
        logger.warning("deribit options parse failed: %s", e)
        return ([], None)

    spot_fallback: float | None = None
    if spots:
        spots_sorted = sorted(spots)
        mid = len(spots_sorted) // 2
        if len(spots_sorted) % 2 == 1:
            spot_fallback = spots_sorted[mid]
        else:
            spot_fallback = 0.5 * (spots_sorted[mid - 1] + spots_sorted[mid])
    return (out, spot_fallback)


# ─── End-to-end fetch ───────────────────────────────────────────────────────


async def _call_http(http_client: HttpClient, args: dict[str, Any]) -> Any:
    return await http_client(**args)


async def fetch_options_skew(
    *, currency: str, http_client: HttpClient, now: datetime | None = None,
) -> OptionsSkewSignal:
    """Pull Deribit book summary + index, собрать OptionsSkewSignal.

    Per-venue error isolation: если index endpoint упал, используем медиану
    underlying_price из option-quotes. Если и опции пусты — возвращаем
    OptionsSkewSignal со всеми None полями.
    """
    moment = now or datetime.utcnow()
    timestamp_ms = int(moment.timestamp() * 1000)
    cur = currency.upper()

    quotes: list[OptionQuote] = []
    spot_from_quotes: float | None = None
    spot: float | None = None

    try:
        opts_payload = await _call_http(http_client, _deribit_book_summary_args(cur))
        quotes, spot_from_quotes = _parse_deribit_options(opts_payload, currency=cur)
    except Exception as e:  # noqa: BLE001 — per-venue isolation
        logger.warning("options skew: deribit book summary (%s) failed: %s", cur, e)

    try:
        idx_payload = await _call_http(http_client, _deribit_index_args(cur))
        spot = _parse_deribit_index(idx_payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("options skew: deribit index (%s) failed: %s", cur, e)

    if spot is None or spot <= 0:
        spot = spot_from_quotes

    if not quotes or spot is None or spot <= 0:
        return OptionsSkewSignal(
            currency=cur,
            timestamp_ms=timestamp_ms,
            underlying_price=float(spot or 0.0),
            near_expiry_days=None,
            near_atm_iv=None,
            near_rr_25d=None,
            far_expiry_days=None,
            far_atm_iv=None,
            far_rr_25d=None,
            atm_iv_term_slope=None,
            skew_class="unknown",
            venues_used=("deribit",),
        )

    return build_options_skew(
        currency=cur,
        quotes=quotes,
        timestamp_ms=timestamp_ms,
        underlying_price=float(spot),
        now=moment,
    )


# ─── Persistence + previous lookup ──────────────────────────────────────────


async def persist_signal(signal: OptionsSkewSignal) -> None:
    """Сохранить OptionsSkewSignal в БД."""
    from database import save_options_skew_snapshot  # noqa: PLC0415
    await save_options_skew_snapshot(
        currency=signal.currency,
        timestamp_ms=signal.timestamp_ms,
        underlying_price=signal.underlying_price,
        near_expiry_days=signal.near_expiry_days,
        near_atm_iv=signal.near_atm_iv,
        near_rr_25d=signal.near_rr_25d,
        far_expiry_days=signal.far_expiry_days,
        far_atm_iv=signal.far_atm_iv,
        far_rr_25d=signal.far_rr_25d,
        atm_iv_term_slope=signal.atm_iv_term_slope,
        skew_class=signal.skew_class,
        venues_csv=",".join(signal.venues_used),
    )


async def get_previous_signal(
    *, currency: str,
) -> OptionsSkewSignal | None:
    """Загрузить предыдущий снимок (для event detection)."""
    from database import get_recent_options_skew_snapshots  # noqa: PLC0415
    rows = await get_recent_options_skew_snapshots(currency=currency, limit=5)
    for row in rows:
        try:
            return OptionsSkewSignal(
                currency=str(row["currency"]),
                timestamp_ms=int(row["timestamp_ms"]),
                underlying_price=float(row.get("underlying_price") or 0.0),
                near_expiry_days=(
                    int(row["near_expiry_days"])
                    if row.get("near_expiry_days") is not None else None
                ),
                near_atm_iv=(
                    float(row["near_atm_iv"])
                    if row.get("near_atm_iv") is not None else None
                ),
                near_rr_25d=(
                    float(row["near_rr_25d"])
                    if row.get("near_rr_25d") is not None else None
                ),
                far_expiry_days=(
                    int(row["far_expiry_days"])
                    if row.get("far_expiry_days") is not None else None
                ),
                far_atm_iv=(
                    float(row["far_atm_iv"])
                    if row.get("far_atm_iv") is not None else None
                ),
                far_rr_25d=(
                    float(row["far_rr_25d"])
                    if row.get("far_rr_25d") is not None else None
                ),
                atm_iv_term_slope=(
                    float(row["atm_iv_term_slope"])
                    if row.get("atm_iv_term_slope") is not None else None
                ),
                skew_class=str(row.get("skew_class") or "unknown"),
                venues_used=tuple(
                    str(row.get("venues_csv") or "").split(",")
                ) if row.get("venues_csv") else (),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return None


# ─── Env-flags ───────────────────────────────────────────────────────────────


def feature_enabled() -> bool:
    return os.getenv("FEATURE_OPTIONS_SKEW", "0").strip() in {"1", "true", "True", "yes"}


def get_currencies() -> tuple[str, ...]:
    raw = os.getenv("OPTIONS_SKEW_SYMBOLS", "BTC,ETH")
    parts = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return tuple(dict.fromkeys(parts)) if parts else ("BTC", "ETH")


def get_interval_seconds() -> int:
    try:
        return max(300, int(os.getenv("OPTIONS_SKEW_INTERVAL_SEC", "1800")))
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
        else:
            raise ValueError(f"unsupported method: {method}")

    return _call
