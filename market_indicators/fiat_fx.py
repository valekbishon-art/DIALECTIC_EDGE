"""FX spot-rates для fiat-валют (USD-anchored).

Используется как **рыночный якорь** для P2P outlier-фильтра: вместо опоры на
median одной из сторон (которая bias'ится wishlist-ads), мы берём настоящий
forex-курс с открытого API и сравниваем P2P-цены против него.

Источник: ``open.er-api.com`` (бесплатный, без ключа, обновляется ~раз в час).
Cache: 4 часа в памяти. Hardcoded fallback для известных USD-пегов (SAR, AED,
HKD, OMR, BHD, JOD, QAR) на случай downtime'а API.

Семантика: ``get_usd_fiat_rate("ILS") -> 2.90`` означает «1 USD = 2.90 ILS».
Для USDT/USDC (≈ 1 USD по дизайну) это и есть market anchor для P2P-цены в
fiat. Для BTC/ETH функция вернёт None (нужен отдельный spot crypto/fiat).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

_LOG = logging.getLogger(__name__)

# In-memory cache: fiat (UPPER) -> (rate_per_usd, fetched_at_unix).
_CACHE: dict[str, tuple[float, float]] = {}

# 4 часа — forex меняется медленно, fiat-rate в течение дня дрейфует на <1%.
_DEFAULT_CACHE_TTL_SEC = 4 * 3600

# Hardcoded fallback для жёстких USD-пегов. Если API недоступен, эти курсы
# валидны >99% времени (пеги поддерживаются ЦБ десятилетиями).
# Цифры — официальные пеги на 2025 год.
_PEG_FALLBACK: dict[str, float] = {
    "SAR": 3.7500,   # Saudi Arabia, peg since 1986
    "AED": 3.6725,   # UAE, peg since 1997
    "QAR": 3.6400,   # Qatar, peg since 2001
    "OMR": 0.3850,   # Oman, peg since 1986
    "BHD": 0.3760,   # Bahrain, peg since 1980
    "JOD": 0.7090,   # Jordan, peg since 1995
    "HKD": 7.8000,   # Hong Kong, peg band 7.75-7.85
    "LBP": 89500.0,  # Lebanon (official peg, parallel market wildly different)
    "PAB": 1.0,      # Panama (uses USD as currency)
    "DJF": 177.72,   # Djibouti, peg since 1949
}

# Stablecoins, для которых spot fiat-rate валиден как market anchor.
# USDC/USDT очень близки к 1 USD (deviation < 0.5% при здоровом рынке).
_USD_PEGGED_STABLES: set[str] = {"USDT", "USDC", "DAI", "TUSD", "FDUSD"}


def _fetch_remote(timeout_sec: float = 8.0) -> dict[str, float] | None:
    """Тащит свежие USD-курсы с open.er-api.com. None при ошибке."""
    url = "https://open.er-api.com/v6/latest/USD"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DIALECTIC_EDGE/1.0 (+p2p-outlier)"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        _LOG.warning("fiat_fx: open.er-api.com fetch failed: %s", exc)
        return None
    if not isinstance(payload, dict) or payload.get("result") != "success":
        _LOG.warning("fiat_fx: unexpected payload (result=%s)", payload.get("result") if isinstance(payload, dict) else "n/a")
        return None
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        return None
    # Преобразуем все в float и UPPER ключи.
    out: dict[str, float] = {}
    for code, value in rates.items():
        try:
            out[str(code).upper()] = float(value)
        except (TypeError, ValueError):
            continue
    return out or None


def _cache_ttl_sec() -> float:
    raw = os.getenv("P2P_FX_CACHE_TTL_SEC", "")
    if not raw:
        return float(_DEFAULT_CACHE_TTL_SEC)
    try:
        return max(60.0, float(raw))
    except ValueError:
        return float(_DEFAULT_CACHE_TTL_SEC)


def get_usd_fiat_rate(fiat: str, *, now: float | None = None) -> float | None:
    """Возвращает «сколько `fiat` стоит 1 USD» (e.g. USD→ILS ≈ 2.90).

    Логика:
      1. Свежий cache hit (< TTL) → возвращаем.
      2. Тянем remote → кэшируем все курсы.
      3. Если remote упал и есть hardcoded peg → возвращаем peg.
      4. Иначе None (caller должен skip-нуть outlier фильтр).

    Параметр ``now`` нужен для детерминированных тестов.
    """
    code = (fiat or "").strip().upper()
    if not code or code == "USD":
        return 1.0 if code == "USD" else None
    ts = time.time() if now is None else now
    ttl = _cache_ttl_sec()

    cached = _CACHE.get(code)
    if cached and (ts - cached[1]) < ttl:
        return cached[0]

    if _remote_fetch_enabled():
        remote = _fetch_remote()
        if remote:
            for k, v in remote.items():
                _CACHE[k] = (v, ts)
            if code in _CACHE:
                return _CACHE[code][0]

    # Remote недоступен и в кэше нет → hardcoded peg fallback.
    if code in _PEG_FALLBACK:
        rate = _PEG_FALLBACK[code]
        # Запишем в кэш с укороченным TTL, чтобы ретрай через час.
        _CACHE[code] = (rate, ts - max(0.0, ttl - 3600))
        return rate

    return None


# Module-level test override. Используется в тестах через ``set_test_mode``
# чтобы defeat-нуть ``patch.dict(os.environ, ..., clear=True)`` который
# стирает env-флаг ``P2P_FX_DISABLE_REMOTE``.
_TEST_MODE_OFFLINE: bool = False


def set_test_mode(offline: bool) -> None:
    """Принудительно (de)активирует offline-режим (без HTTP-fetch) на уровне
    процесса. В offline-режиме ``_remote_fetch_enabled()`` всегда возвращает
    False независимо от env. Используется в тестах.
    """
    global _TEST_MODE_OFFLINE
    _TEST_MODE_OFFLINE = bool(offline)


def _remote_fetch_enabled() -> bool:
    """Позволяет отключить network fetch (для тестов / offline сценариев)."""
    if _TEST_MODE_OFFLINE:
        return False
    raw = os.getenv("P2P_FX_DISABLE_REMOTE", "0").strip()
    return raw not in ("1", "true", "yes", "on")


def is_usd_pegged_stable(asset: str) -> bool:
    """Можно ли использовать USD-fiat spot как market anchor для данного asset'а.

    Для USDT/USDC/DAI etc — да (≈ 1 USD).
    Для BTC/ETH/etc — нет (анкор пришлось бы отдельно тянуть, отложено).
    """
    return (asset or "").strip().upper() in _USD_PEGGED_STABLES


def market_anchor_for_pair(asset: str, fiat: str, *, now: float | None = None) -> float | None:
    """Market anchor для P2P-пары (asset, fiat).

    Если asset — USD-stable, anchor = spot USD-fiat rate.
    Иначе None (outlier-фильтр deinit'ится для этой пары).
    """
    if not is_usd_pegged_stable(asset):
        return None
    return get_usd_fiat_rate(fiat, now=now)


def reset_cache() -> None:
    """Утилита для тестов: сбрасывает in-memory cache."""
    _CACHE.clear()
