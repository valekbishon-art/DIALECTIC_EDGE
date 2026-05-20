"""Funding rate term structure + perp basis carry — чистая математика.

Зачем:
  `signals.py` читает spot/perp funding с одной точки временной кривой (текущий
  8h-funding на perpetual swap). Этого недостаточно — рынок имеет **term
  structure**: funding на 8h, 1M-fwd implied funding, 3M-quarterly basis-carry.

  Когда дальняя кривая (3M) cменяет contango на backwardation, при том что
  ближняя (8h) ещё contango — это **stress signal**, как inverted yield curve
  (3M-funding < spot-funding → market прайсит будущий de-risking / либо
  squeeze, либо bearish move). Аналог VIX и нефтяной curve.

Что НЕ делает (намеренно):
  * Не лезет в signals.py / signal_trader.py / agents.py / dynamic_risk.py.
  * Не трогает trading-tables. Хранит снимки в отдельной таблице.
  * Не использует numpy/pandas. Stdlib only — там 4 числа в формуле.
  * Не делает full vol-surface inversion (это для опционов, отдельный PR).

Math:
  annualized_funding_rate:
    spot 8h funding f → annual = f × (365 × 24 / 8) = f × 1095 (доля, не %)
  basis_carry_annualized:
    annualized = (perp_price - spot_price) / spot_price × (365 / days_to_expiry)
    (для бессрочных perps — days_to_expiry = ∞ → carry = funding × periods/year)
  term_structure_slope_bps:
    = (far_funding_annual - near_funding_annual) × 10_000

Конвенция знаков:
  funding > 0 → лонги платят шортам → contango (рынок bullish, лонги перевешены).
  funding < 0 → шорты платят лонгам → backwardation (рынок bearish, шортов перевес).
  slope > 0 → дальние > ближних → "стандартный" contango term structure.
  slope < 0 → inverted → дальние backwardation при ближних contango → stress.

Внешние зависимости: только stdlib.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Periods/year для 8-часового funding (Binance / Bybit). 365 × 24 / 8.
PERIODS_PER_YEAR_8H = 365.0 * 24.0 / 8.0

#: Periods/year для 1-часового funding (Hyperliquid). 365 × 24.
PERIODS_PER_YEAR_1H = 365.0 * 24.0

#: Slope > этого (annual %) → contango. Slope < -этого → backwardation.
#: Между ними — flat. 5 bps annual ≈ 0.05% / год = шум.
DEFAULT_SLOPE_NEUTRAL_BPS = 5.0

#: Сколько days_to_expiry считаем «3M» (90 ± 15).
DEFAULT_QUARTERLY_DAYS_MIN = 60
DEFAULT_QUARTERLY_DAYS_MAX = 120

#: При abs(annualized) выше — clamp в логах (формирование защиты от глюков
#: типа funding 100x от типичного).
SANITY_MAX_ANNUAL_FUNDING = 5.0  # ±500% годовых


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FundingRateSnapshot:
    """Один срез funding rate с биржи.

    `rate` — funding per period (доля, не процент). Например, 0.0001 на
    Binance 8h = 0.01% за 8 часов.
    """

    venue: str  # 'binance', 'bybit', 'okx', 'hyperliquid', ...
    symbol: str  # 'BTCUSDT', 'BTC-PERP', ...
    asset: str  # base asset symbol normalized ('BTC')
    rate: float  # funding per period (decimal, e.g. 0.0001 = 1bp)
    period_hours: float  # обычно 8.0
    next_funding_time_ms: int | None = None
    timestamp_ms: int = 0


@dataclass(frozen=True)
class BasisPoint:
    """Точка на term structure: цена deliverable-фьючерса.

    `days_to_expiry` — целое число дней до экспирации. 0 — для perp (бесконечный
    срок), caller обязан передать perp_price ≈ spot и days_to_expiry=0.
    """

    venue: str
    symbol: str  # 'BTCUSDM26', 'BTC-30JUN26', ...
    asset: str
    futures_price: float
    spot_price: float
    days_to_expiry: int  # 0 для perp, ≈30 для месячного, ≈90 для квартального


@dataclass(frozen=True)
class TermStructureSignal:
    """Снимок термной структуры по одному активу.

    Все annualized — в долях (0.10 = 10% годовых, не bps).
    """

    asset: str
    timestamp_ms: int
    spot_funding_annual: float | None  # ~30%/год для perp BTC при funding 0.01%/8h
    monthly_basis_annual: float | None  # 30-day, если есть
    quarterly_basis_annual: float | None  # 90-day, если есть
    slope_annual: float | None  # quarterly - spot (None если одна из ног None)
    is_inverted: bool  # quarterly < spot (annualized)
    venues_used: tuple[str, ...] = ()


# ─── Annualization ───────────────────────────────────────────────────────────


def annualized_funding_rate(rate_per_period: float, *, period_hours: float = 8.0) -> float:
    """Annualize per-period funding rate to /год (decimal).

    Примеры:
      Binance 8h funding 0.0001 → 0.0001 × 1095 = 0.1095 = 10.95% / год
      Hyperliquid 1h funding 0.00001 → 0.00001 × 8760 = 0.0876 = 8.76% / год
    """
    if period_hours <= 0:
        raise ValueError(f"period_hours must be > 0, got {period_hours}")
    if not math.isfinite(rate_per_period):
        return 0.0
    periods_per_year = (365.0 * 24.0) / float(period_hours)
    annual = float(rate_per_period) * periods_per_year
    # Sanity clamp для отчётов (не критично для расчёта slope)
    if abs(annual) > SANITY_MAX_ANNUAL_FUNDING:
        logger.warning(
            "funding annualized OOB: %.4f (rate=%.6f, period=%.1fh)",
            annual, rate_per_period, period_hours,
        )
    return annual


def basis_carry_annualized(
    *, futures_price: float, spot_price: float, days_to_expiry: int,
) -> float:
    """Annualized basis carry для deliverable-фьючерса.

    carry_annual = (F - S) / S × (365 / days_to_expiry)
    Если days_to_expiry <= 0 → возвращаем 0.0 (perp = no expiry).
    """
    if spot_price <= 0:
        return 0.0
    if days_to_expiry <= 0:
        return 0.0
    if not (math.isfinite(futures_price) and math.isfinite(spot_price)):
        return 0.0
    raw = (float(futures_price) - float(spot_price)) / float(spot_price)
    return raw * (365.0 / float(days_to_expiry))


# ─── Term structure aggregation ──────────────────────────────────────────────


def _classify_days_to_expiry(d: int) -> str | None:
    """'monthly' (15-45), 'quarterly' (60-120). None для всего остального."""
    if 15 <= d <= 45:
        return "monthly"
    if DEFAULT_QUARTERLY_DAYS_MIN <= d <= DEFAULT_QUARTERLY_DAYS_MAX:
        return "quarterly"
    return None


def build_term_structure(
    *,
    asset: str,
    funding_snapshots: Sequence[FundingRateSnapshot],
    basis_points: Sequence[BasisPoint],
    timestamp_ms: int,
) -> TermStructureSignal:
    """Собрать TermStructureSignal по активу.

    spot_funding_annual: усреднено по всем venue'ам funding_snapshots.
    monthly_basis_annual: basis на ближайший фьючерс с days_to_expiry ∈ [15,45].
    quarterly_basis_annual: фьючерс с days_to_expiry ∈ [60,120].
    slope: quarterly - spot (annualized). is_inverted: quarterly < spot.

    Если venue'ов нет для какой-то ноги — возвращаем None в соответствующем
    поле, slope None, is_inverted=False.
    """
    rel_funding = [s for s in funding_snapshots if s.asset.upper() == asset.upper()]
    rel_basis = [b for b in basis_points if b.asset.upper() == asset.upper()]

    venues_used: list[str] = []

    # spot_funding_annual — среднее по всем venue'ам (perp swap).
    if rel_funding:
        annuals: list[float] = []
        for s in rel_funding:
            try:
                annuals.append(annualized_funding_rate(s.rate, period_hours=s.period_hours))
                venues_used.append(s.venue)
            except (ValueError, TypeError):
                continue
        spot_funding_annual = sum(annuals) / len(annuals) if annuals else None
    else:
        spot_funding_annual = None

    monthly_annual: float | None = None
    quarterly_annual: float | None = None
    for b in rel_basis:
        bucket = _classify_days_to_expiry(b.days_to_expiry)
        carry = basis_carry_annualized(
            futures_price=b.futures_price,
            spot_price=b.spot_price,
            days_to_expiry=b.days_to_expiry,
        )
        if bucket == "monthly" and monthly_annual is None:
            monthly_annual = carry
            venues_used.append(b.venue)
        elif bucket == "quarterly" and quarterly_annual is None:
            quarterly_annual = carry
            venues_used.append(b.venue)

    slope: float | None = None
    inverted = False
    if quarterly_annual is not None and spot_funding_annual is not None:
        slope = float(quarterly_annual) - float(spot_funding_annual)
        inverted = bool(quarterly_annual < spot_funding_annual)

    return TermStructureSignal(
        asset=asset.upper(),
        timestamp_ms=int(timestamp_ms),
        spot_funding_annual=spot_funding_annual,
        monthly_basis_annual=monthly_annual,
        quarterly_basis_annual=quarterly_annual,
        slope_annual=slope,
        is_inverted=inverted,
        venues_used=tuple(dict.fromkeys(venues_used)),  # dedupe сохраняя порядок
    )


# ─── Inversion detection over time ──────────────────────────────────────────


def detect_inversion_event(
    *,
    current: TermStructureSignal,
    previous: TermStructureSignal | None,
    neutral_bps: float = DEFAULT_SLOPE_NEUTRAL_BPS,
) -> str | None:
    """Сравнить current vs previous и вернуть тип события:

      * 'inversion_onset' — было contango, стало backwardation.
      * 'inversion_recovery' — было backwardation, стало contango.
      * None — нет смены состояния или одна из точек неполная.

    neutral_bps определяет «нейтральную зону» вокруг 0, в которой переход
    не считаем сменой режима (защита от микро-flip-flop).
    """
    if current.slope_annual is None or previous is None or previous.slope_annual is None:
        return None
    neutral = max(0.0, float(neutral_bps)) / 10000.0
    cur = float(current.slope_annual)
    prev = float(previous.slope_annual)

    was_contango = prev > neutral
    was_backwardation = prev < -neutral
    is_contango = cur > neutral
    is_backwardation = cur < -neutral

    if was_contango and is_backwardation:
        return "inversion_onset"
    if was_backwardation and is_contango:
        return "inversion_recovery"
    return None


def classify_stress_level(signal: TermStructureSignal) -> str:
    """Простой классификатор стресса по signal.

      * 'panic' — backwardation, |slope| > 0.10 (10% годовых перекос).
      * 'stress' — backwardation, любая глубина.
      * 'neutral' — slope в [-0.01, +0.01].
      * 'normal_contango' — contango, slope > 0.01.
    """
    if signal.slope_annual is None:
        return "unknown"
    slope = float(signal.slope_annual)
    if slope < -0.10:
        return "panic"
    if slope < -0.01:
        return "stress"
    if slope > 0.01:
        return "normal_contango"
    return "neutral"


# ─── Helpers для derivative deliverable symbols ─────────────────────────────


def estimate_days_to_expiry(*, expiry_date: datetime, now: datetime) -> int:
    """Дней до экспирации (округление вниз). 0 если истекло."""
    delta = expiry_date - now
    days = int(delta.total_seconds() // 86400)
    return max(0, days)


def parse_bybit_quarterly_symbol(symbol: str) -> datetime | None:
    """Bybit linear delivery символы: BTC-26DEC25 (формат). Возвращает datetime
    экспирации или None если не парсится.

    Note: Bybit использует разные форматы. Здесь покрываем основные:
      'BTC-26DEC25' → 2025-12-26
      'BTC-31JAN26' → 2026-01-31
    """
    if not symbol or "-" not in symbol:
        return None
    parts = symbol.split("-")
    if len(parts) < 2:
        return None
    tail = parts[-1].upper()
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    if len(tail) < 7:
        return None
    try:
        day = int(tail[:-5])
        month_str = tail[-5:-2]
        year = int(tail[-2:]) + 2000
        month = months.get(month_str)
        if month is None:
            return None
        return datetime(year, month, day, 8, 0, 0)  # экспирация UTC 08:00
    except (ValueError, KeyError):
        return None
