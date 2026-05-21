"""Liquidation magnet — чистая математика (stdlib only).

Идея:
    Когда рынок перекошен в позиционировании (большинство лонгуют ИЛИ большинство
    шортят) И при этом OI быстро растёт (накапливается leverage), цена имеет
    тенденцию двигаться к ценовым уровням массовой ликвидации. Это создаёт
    «магнит» — гравитационное притяжение к зоне ликвидаций:

      * Тop-трейдеры сильно лонгуют + высокий OI → leverage на лонг-сайде →
        **DOWN MAGNET** (price likely flush down to liquidate longs) → bearish ST.
      * Top-трейдеры сильно шортят + высокий OI → leverage на шорт-сайде →
        **UP MAGNET** (price likely squeeze up to liquidate shorts) → bullish ST.

    Это **контрарианский** сигнал: экстремальное позиционирование предсказывает
    движение в ПРОТИВОПОЛОЖНОМ направлении (по принципу cascade liquidation).

Данные:
    1. OI history (Binance fapi или Bybit v5) — изменение OI за lookback окно.
       OI velocity (% change) = индикатор скорости накопления leverage.
    2. Top trader long/short position ratio (Binance only — Bybit free tier
       не отдаёт). Pro-positioning сильнее предсказывает чем retail account-ratio.

Что НЕ делает (намеренно):
    * Не пытается получить «liquidation heatmap» с раскладкой по уровням цен —
      такие данные платные (Coinglass, CryptoQuant).
    * Не считает фактические forced-liquidations — Binance forceOrders требует
      ключ, а WebSocket-стрим не подходит для polling-архитектуры.
    * Не использует numpy / pandas — pure stdlib.

Конвенция знаков:
    UP_MAGNET = bullish short-term (+score contribution).
    DOWN_MAGNET = bearish short-term (-score contribution).
    NEUTRAL = 0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Пороги для OI velocity (изменение OI за lookback окно).
#: >10% за 24ч = «значимое накопление leverage»; >25% = экстремальное.
DEFAULT_OI_BUILDUP_PCT = 10.0
DEFAULT_OI_BUILDUP_STRONG_PCT = 25.0

#: Пороги для top trader L/S ratio.
#: >1.7 = heavily long; >2.5 = extremely long.
#: <0.6 = heavily short; <0.4 = extremely short.
DEFAULT_LS_LONG_HEAVY = 1.7
DEFAULT_LS_LONG_EXTREME = 2.5
DEFAULT_LS_SHORT_HEAVY = 0.6
DEFAULT_LS_SHORT_EXTREME = 0.4

#: Labels.
LABEL_UP_MAGNET = "up_magnet"      # shorts squeezable → bullish ST
LABEL_DOWN_MAGNET = "down_magnet"  # longs liquidatable → bearish ST
LABEL_NEUTRAL = "neutral"          # balanced or weak signal
LABEL_UNKNOWN = "unknown"          # not enough data


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OIHistoryPoint:
    """Один срез открытого интереса в момент времени."""

    timestamp_ms: int
    oi_contracts: float   # nominal OI в контрактах (Bybit/Binance variant)
    oi_usd: float = 0.0   # USD-номинированный OI (опционально, если venue отдаёт)


@dataclass(frozen=True)
class TopTraderRatio:
    """Top-trader long/short position ratio (Binance fapi)."""

    timestamp_ms: int
    long_account_pct: float   # доля шорт-аккаунтов (0..1)
    short_account_pct: float  # доля лонг-аккаунтов (0..1)
    long_short_ratio: float   # longs / shorts; > 1 = heavily long


@dataclass
class LiquidationMagnetSignal:
    """Aggregate signal от liquidation magnet analysis."""

    # OI velocity.
    oi_now_contracts: float = 0.0
    oi_baseline_contracts: float = 0.0  # ~lookback ago
    oi_change_pct: float = 0.0
    oi_lookback_hours: int = 24

    # Top trader positioning.
    top_long_short_ratio: float | None = None  # None если data unavailable
    top_long_account_pct: float = 0.0  # 0..1
    top_short_account_pct: float = 0.0  # 0..1

    # Classification.
    label: str = LABEL_UNKNOWN
    is_strong_signal: bool = False

    # Meta.
    venue: str = ""        # "binance" | "bybit" | "binance+bybit"
    symbol: str = ""       # "BTCUSDT"
    timestamp_ms: int = 0


# ─── Math helpers ────────────────────────────────────────────────────────────


def compute_oi_change_pct(
    history: list[OIHistoryPoint],
    *,
    lookback_hours: int = 24,
) -> tuple[float, float, float]:
    """Из временного ряда OI вернуть (oi_now, oi_baseline, change_pct).

    Логика:
      * history отсортирован по timestamp_ms ASC (старое → новое).
      * oi_now = последняя точка.
      * oi_baseline = первая точка чей timestamp >= (now - lookback_hours*3600*1000).
        Если такой не нашли — берём самую раннюю точку в окне.
      * change_pct = (oi_now - oi_baseline) / oi_baseline * 100.

    Если history пустой или содержит < 2 точек → возвращает (0.0, 0.0, 0.0).
    """
    if not history or len(history) < 2:
        return (0.0, 0.0, 0.0)
    pts = sorted(history, key=lambda p: p.timestamp_ms)
    now_pt = pts[-1]
    cutoff_ms = now_pt.timestamp_ms - lookback_hours * 3600 * 1000
    # Найти первую точку >= cutoff.
    baseline = pts[0]  # fallback на самую раннюю
    for p in pts:
        if p.timestamp_ms >= cutoff_ms:
            baseline = p
            break
    oi_now = float(now_pt.oi_contracts or 0.0)
    oi_base = float(baseline.oi_contracts or 0.0)
    if oi_base <= 0:
        return (oi_now, oi_base, 0.0)
    change_pct = (oi_now - oi_base) / oi_base * 100.0
    return (oi_now, oi_base, change_pct)


def classify_liquidation_magnet(
    *,
    oi_change_pct: float,
    top_long_short_ratio: float | None,
    oi_buildup_pct: float = DEFAULT_OI_BUILDUP_PCT,
    oi_buildup_strong_pct: float = DEFAULT_OI_BUILDUP_STRONG_PCT,
    ls_long_heavy: float = DEFAULT_LS_LONG_HEAVY,
    ls_long_extreme: float = DEFAULT_LS_LONG_EXTREME,
    ls_short_heavy: float = DEFAULT_LS_SHORT_HEAVY,
    ls_short_extreme: float = DEFAULT_LS_SHORT_EXTREME,
) -> tuple[str, bool]:
    """Классифицировать magnet на основе OI velocity и top trader L/S.

    Логика:
      * Если top_long_short_ratio is None → UNKNOWN.
      * Если L/S > long_heavy И OI buildup > oi_buildup_pct → DOWN_MAGNET.
      * Если L/S < short_heavy И OI buildup > oi_buildup_pct → UP_MAGNET.
      * is_strong_signal = (L/S за extreme threshold) И (OI buildup за strong threshold).
      * Иначе → NEUTRAL.

    Note: OI buildup может быть отрицательным (deleveraging). В этом случае
    leverage снижается → magnet ослабевает → NEUTRAL.
    """
    if top_long_short_ratio is None:
        return (LABEL_UNKNOWN, False)

    # Deleveraging (OI падает) — magnet не работает.
    if oi_change_pct < oi_buildup_pct:
        return (LABEL_NEUTRAL, False)

    if top_long_short_ratio >= ls_long_heavy:
        strong = (
            top_long_short_ratio >= ls_long_extreme
            and oi_change_pct >= oi_buildup_strong_pct
        )
        return (LABEL_DOWN_MAGNET, strong)
    if top_long_short_ratio <= ls_short_heavy:
        strong = (
            top_long_short_ratio <= ls_short_extreme
            and oi_change_pct >= oi_buildup_strong_pct
        )
        return (LABEL_UP_MAGNET, strong)
    return (LABEL_NEUTRAL, False)


def build_liquidation_magnet_signal(
    *,
    oi_history: list[OIHistoryPoint],
    top_trader_ratio: TopTraderRatio | None,
    venue: str,
    symbol: str,
    lookback_hours: int = 24,
    timestamp_ms: int = 0,
    oi_buildup_pct: float = DEFAULT_OI_BUILDUP_PCT,
    oi_buildup_strong_pct: float = DEFAULT_OI_BUILDUP_STRONG_PCT,
    ls_long_heavy: float = DEFAULT_LS_LONG_HEAVY,
    ls_long_extreme: float = DEFAULT_LS_LONG_EXTREME,
    ls_short_heavy: float = DEFAULT_LS_SHORT_HEAVY,
    ls_short_extreme: float = DEFAULT_LS_SHORT_EXTREME,
) -> LiquidationMagnetSignal:
    """Свести raw inputs (OI history + L/S ratio) в LiquidationMagnetSignal."""
    oi_now, oi_base, change_pct = compute_oi_change_pct(
        oi_history, lookback_hours=lookback_hours,
    )

    ls_ratio: float | None = None
    long_pct = 0.0
    short_pct = 0.0
    if top_trader_ratio is not None:
        ls_ratio = top_trader_ratio.long_short_ratio
        long_pct = top_trader_ratio.long_account_pct
        short_pct = top_trader_ratio.short_account_pct

    label, strong = classify_liquidation_magnet(
        oi_change_pct=change_pct,
        top_long_short_ratio=ls_ratio,
        oi_buildup_pct=oi_buildup_pct,
        oi_buildup_strong_pct=oi_buildup_strong_pct,
        ls_long_heavy=ls_long_heavy,
        ls_long_extreme=ls_long_extreme,
        ls_short_heavy=ls_short_heavy,
        ls_short_extreme=ls_short_extreme,
    )

    return LiquidationMagnetSignal(
        oi_now_contracts=oi_now,
        oi_baseline_contracts=oi_base,
        oi_change_pct=change_pct,
        oi_lookback_hours=lookback_hours,
        top_long_short_ratio=ls_ratio,
        top_long_account_pct=long_pct,
        top_short_account_pct=short_pct,
        label=label,
        is_strong_signal=strong,
        venue=venue,
        symbol=symbol,
        timestamp_ms=timestamp_ms,
    )
