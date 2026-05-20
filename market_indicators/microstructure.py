"""Cross-exchange microstructure forensics — чистая математика.

Зачем:
  Существующий сигнал-стек (`signals.py`) читает funding/OI/L-S с **одной**
  биржи. Микроструктура (Bid/ask depth, quoted spread, liquidity vacuum)
  даёт **leading** информацию о price impact ещё до того, как сигналы
  funding/OI отреагируют. Профи (Hyblock, Velodata) делают это за >$300/мес,
  у retail-ботов этого нет.

Что считаем (всё на пер-venue snapshot'е и затем aggregate):
  1. **Depth-in-band**: суммарная стоимость USD (price × size) bid- и
     ask-side в пределах ±band_pct% от mid-price. Стандарт band=0.5%.
  2. **Depth asymmetry**: (bid_usd − ask_usd) / (bid_usd + ask_usd) ∈ [−1, +1].
     >0 — рынок ловит шорты (bid-heavy), <0 — рынок ловит лонги.
  3. **Quoted spread** в bps: (best_ask − best_bid) / mid × 10_000.
  4. **Liquidity vacuum**: текущая суммарная depth (bid+ask USD в полосе)
     ниже скользящего 24h-baseline на ≥ `vacuum_drop_pct` %. Это leading
     signal: depth тает за 30–120 сек до flash-crash'а (см. arxiv
     1612.02649, López de Prado et al.).
  5. **Volume-weighted aggregate**: метрики из разных venue свешиваются
     по их 24h-volume. Без волюмов — простое среднее.

Что НЕ делает (намеренно):
  * Не лезет в `signal_trader.py`, `signals.py`, `core/dynamic_risk.py`
    (per AGENTS.md: торговую логику не трогаем без явной просьбы).
  * Не использует numpy/scipy/pandas — только stdlib. Чтобы можно было
    гонять в unit-fast CI job.
  * Не интегрирует в Bull/Bear prompts — это инфраструктурный PR. Подача
    в дебаты — отдельный PR после накопления baseline'а.
  * Не делает full L2 reconstruction (Kyle's λ, VPIN, Amihud) — заготовка
    на следующий PR (PR #5 «Microstructure forensics — advanced metrics»).

Внешние зависимости: только stdlib.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Стандартная полоса вокруг mid для расчёта depth. 0.5% — это типичный
#: spread «маркет-мейкер коридор» на BTC/ETH; за этой полосой ликвидность
#: размазана тонко и шумит.
DEFAULT_BAND_PCT = 0.5

#: Порог падения depth, ниже которого считаем «liquidity vacuum». 40% от
#: baseline — эмпирически отделяет normal microstructure noise от
#: реальной эвакуации make'еров (см. README sources).
DEFAULT_VACUUM_DROP_PCT = 40.0

#: Минимальное число venue в aggregate, ниже которого aggregate помечается
#: как `partial=True` (доверять выводам осторожно).
DEFAULT_MIN_VENUES_FOR_AGGREGATE = 2

#: Sentinel для «нет данных от venue» в asymmetry — используем NaN-like float.
NO_DATA = float("nan")


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrderbookLevel:
    """Уровень стакана: цена + объём в монетах (size)."""

    price: float
    size: float

    def notional_usd(self) -> float:
        """Стоимость уровня в USD = price × size. Стейблкоин-парные книги."""
        return self.price * self.size


@dataclass(frozen=True)
class VenueMicrostructure:
    """Метрики одной биржи в момент snapshot'а.

    `bid_depth_usd` и `ask_depth_usd` — суммарная стоимость USD в полосе
    [mid×(1−band), mid×(1+band)]. Если стакан пустой / venue не ответил —
    venue не попадает в aggregate (см. compute_aggregate).
    """

    venue: str
    mid_price: float
    best_bid: float
    best_ask: float
    bid_depth_usd: float
    ask_depth_usd: float
    band_pct: float
    quoted_spread_bps: float
    asymmetry: float  # (bid−ask)/(bid+ask), ∈ [−1, +1] или NaN при empty
    timestamp_ms: int  # unix-ms когда снят snapshot
    volume_24h_usd: float | None = None  # для volume-weighted aggregate

    def total_depth_usd(self) -> float:
        """Сумма bid+ask depth в полосе. Используется vacuum detection."""
        return float(self.bid_depth_usd) + float(self.ask_depth_usd)


@dataclass(frozen=True)
class AggregateMicrostructure:
    """Сводка по всем venue. Volume-weighted если волюмы заданы, иначе
    арифметическое среднее. `partial=True` если venue < min_venues."""

    asset: str
    mid_price_weighted: float
    bid_depth_usd_total: float
    ask_depth_usd_total: float
    asymmetry_weighted: float
    quoted_spread_bps_weighted: float
    venue_count: int
    partial: bool
    timestamp_ms: int
    venues: tuple[str, ...] = field(default_factory=tuple)

    def total_depth_usd(self) -> float:
        return float(self.bid_depth_usd_total) + float(self.ask_depth_usd_total)


@dataclass(frozen=True)
class MicrostructureSignal:
    """Финальная классификация микроструктуры в момент времени.

    `direction_bias` ∈ {−1, 0, +1}: знак asymmetry (с порогом). +1 = bid-heavy
    (рынок ловит шорты), −1 = ask-heavy. 0 — нейтрально или partial.

    `vacuum` — True если совокупный depth ниже baseline на drop_pct.

    `severity` ∈ [0, 1] — насколько глубоко мы в vacuum / насколько сильна
    asymmetry. Используется в Bull/Bear prompts как численный multiplier.
    """

    aggregate: AggregateMicrostructure
    direction_bias: int
    vacuum: bool
    severity: float
    baseline_depth_usd: float | None = None
    drop_pct_observed: float | None = None


# ─── Math primitives ─────────────────────────────────────────────────────────


def compute_depth_in_band(
    levels: Sequence[OrderbookLevel],
    *,
    mid_price: float,
    band_pct: float,
    side: str,
) -> float:
    """Суммарная стоимость USD на одной стороне стакана в пределах ±band_pct%.

    Args:
        levels: уровни стакана (отсортированные best→worst, но мы фильтруем
            по цене, так что порядок не критичен).
        mid_price: mid = (best_bid + best_ask) / 2.
        band_pct: ширина полосы в процентах (0.5 = 0.5%).
        side: "bid" (берём уровни с price ≥ lower_bound) или
              "ask" (берём уровни с price ≤ upper_bound).

    Returns:
        Сумма price × size по подходящим уровням. 0.0 если levels пуст.
    """
    if not levels or mid_price <= 0 or band_pct <= 0:
        return 0.0

    band_frac = float(band_pct) / 100.0
    lower = mid_price * (1.0 - band_frac)
    upper = mid_price * (1.0 + band_frac)

    if side == "bid":
        # bid'ы ниже mid → берём те, что в пределах [lower, mid].
        return sum(lvl.notional_usd() for lvl in levels if lvl.price >= lower)
    if side == "ask":
        # ask'и выше mid → берём те, что в пределах [mid, upper].
        return sum(lvl.notional_usd() for lvl in levels if lvl.price <= upper)
    raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")


def compute_depth_asymmetry(bid_depth_usd: float, ask_depth_usd: float) -> float:
    """(bid − ask) / (bid + ask). Возвращает NaN если суммарная depth = 0."""
    total = float(bid_depth_usd) + float(ask_depth_usd)
    if total <= 0.0:
        return NO_DATA
    return (float(bid_depth_usd) - float(ask_depth_usd)) / total


def compute_quoted_spread_bps(best_bid: float, best_ask: float, mid_price: float) -> float:
    """Quoted spread в bps. NaN при некорректных входах."""
    if best_bid <= 0 or best_ask <= 0 or mid_price <= 0:
        return NO_DATA
    if best_ask < best_bid:
        # Crossed book — артефакт; не паникуем, возвращаем 0.
        return 0.0
    return (best_ask - best_bid) / mid_price * 10_000.0


def build_venue_snapshot(
    *,
    venue: str,
    bids: Sequence[OrderbookLevel],
    asks: Sequence[OrderbookLevel],
    band_pct: float = DEFAULT_BAND_PCT,
    timestamp_ms: int,
    volume_24h_usd: float | None = None,
) -> VenueMicrostructure | None:
    """Собрать VenueMicrostructure из сырых levels. None при битых данных."""
    if not bids or not asks:
        return None

    # Берём top-уровни для best_bid/best_ask. Предполагаем bids
    # отсортированы по убыванию цены, asks — по возрастанию (стандарт REST).
    best_bid = max(lvl.price for lvl in bids if lvl.price > 0)
    best_ask = min(lvl.price for lvl in asks if lvl.price > 0)
    if best_bid <= 0 or best_ask <= 0:
        return None
    mid = 0.5 * (best_bid + best_ask)
    if mid <= 0:
        return None

    bid_usd = compute_depth_in_band(bids, mid_price=mid, band_pct=band_pct, side="bid")
    ask_usd = compute_depth_in_band(asks, mid_price=mid, band_pct=band_pct, side="ask")
    asym = compute_depth_asymmetry(bid_usd, ask_usd)
    spread = compute_quoted_spread_bps(best_bid, best_ask, mid)

    return VenueMicrostructure(
        venue=venue,
        mid_price=float(mid),
        best_bid=float(best_bid),
        best_ask=float(best_ask),
        bid_depth_usd=float(bid_usd),
        ask_depth_usd=float(ask_usd),
        band_pct=float(band_pct),
        quoted_spread_bps=float(spread),
        asymmetry=float(asym),
        timestamp_ms=int(timestamp_ms),
        volume_24h_usd=float(volume_24h_usd) if volume_24h_usd is not None else None,
    )


# ─── Aggregate across venues ─────────────────────────────────────────────────


def _is_finite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))


def compute_aggregate(
    snapshots: Sequence[VenueMicrostructure],
    *,
    asset: str,
    timestamp_ms: int,
    min_venues: int = DEFAULT_MIN_VENUES_FOR_AGGREGATE,
) -> AggregateMicrostructure | None:
    """Свести VenueMicrostructure из разных бирж в одну сводку.

    Веса:
      * Если у всех venue есть volume_24h_usd → volume-weighted.
      * Иначе → арифметическое среднее (равные веса).

    `partial=True` если venue < min_venues. Возвращаем сводку всё равно,
    но caller'у стоит делать выводы осторожно.
    Возвращает None если venue=0 (нечего агрегировать).
    """
    snaps = [s for s in snapshots if s is not None]
    if not snaps:
        return None

    # Определяем weights. Если хотя бы один venue без volume — fallback на 1.0
    # для всех (равные веса), чтобы не было silently-skipped venue.
    all_have_volume = all(
        s.volume_24h_usd is not None and s.volume_24h_usd > 0 for s in snaps
    )
    if all_have_volume:
        weights = [float(s.volume_24h_usd or 0.0) for s in snaps]
    else:
        weights = [1.0] * len(snaps)

    total_weight = sum(weights)
    if total_weight <= 0:
        weights = [1.0] * len(snaps)
        total_weight = float(len(snaps))

    # Weighted averages (mid, spread, asymmetry). Bid/ask depth — суммируем
    # (это абсолютные значения, имеющие смысл при aggregation).
    weighted_mid = sum(s.mid_price * w for s, w in zip(snaps, weights)) / total_weight

    bid_total = sum(s.bid_depth_usd for s in snaps)
    ask_total = sum(s.ask_depth_usd for s in snaps)

    # asymmetry усредняем только по venue с конечным значением (без NaN).
    asym_pairs = [
        (s.asymmetry, w) for s, w in zip(snaps, weights) if _is_finite(s.asymmetry)
    ]
    if asym_pairs:
        asym_sum_w = sum(w for _, w in asym_pairs)
        asym_weighted = (
            sum(a * w for a, w in asym_pairs) / asym_sum_w if asym_sum_w > 0 else NO_DATA
        )
    else:
        asym_weighted = NO_DATA

    spread_pairs = [
        (s.quoted_spread_bps, w)
        for s, w in zip(snaps, weights)
        if _is_finite(s.quoted_spread_bps)
    ]
    if spread_pairs:
        spread_sum_w = sum(w for _, w in spread_pairs)
        spread_weighted = (
            sum(sp * w for sp, w in spread_pairs) / spread_sum_w
            if spread_sum_w > 0
            else NO_DATA
        )
    else:
        spread_weighted = NO_DATA

    return AggregateMicrostructure(
        asset=str(asset),
        mid_price_weighted=float(weighted_mid),
        bid_depth_usd_total=float(bid_total),
        ask_depth_usd_total=float(ask_total),
        asymmetry_weighted=float(asym_weighted),
        quoted_spread_bps_weighted=float(spread_weighted),
        venue_count=len(snaps),
        partial=len(snaps) < int(min_venues),
        timestamp_ms=int(timestamp_ms),
        venues=tuple(s.venue for s in snaps),
    )


# ─── Vacuum detection & signal classification ────────────────────────────────


def detect_liquidity_vacuum(
    current_depth_usd: float,
    baseline_depth_usd: float | None,
    *,
    drop_pct: float = DEFAULT_VACUUM_DROP_PCT,
) -> tuple[bool, float | None]:
    """Сравнение текущей depth со скользящим baseline'ом.

    Returns:
        (vacuum_flag, drop_pct_observed). Если baseline None или ≤0 —
        возвращаем (False, None) (нет данных для сравнения).

    Vacuum = current < baseline × (1 − drop_pct/100). Например, при
    drop_pct=40 — depth упала ≥40% от 24h-baseline.
    """
    if baseline_depth_usd is None or baseline_depth_usd <= 0:
        return False, None
    if current_depth_usd < 0:
        return False, None

    ratio = float(current_depth_usd) / float(baseline_depth_usd)
    drop_observed = (1.0 - ratio) * 100.0  # % падения от baseline
    threshold = float(drop_pct)
    return drop_observed >= threshold, float(drop_observed)


#: Порог по asymmetry (в абсолютном значении), ниже которого считаем bias=0.
#: |asym|=0.10 — это уже довольно заметный перекос (60/40 USD сплит).
ASYMMETRY_NEUTRAL_THRESHOLD = 0.10


def classify_signal(
    aggregate: AggregateMicrostructure,
    *,
    baseline_depth_usd: float | None,
    vacuum_drop_pct: float = DEFAULT_VACUUM_DROP_PCT,
    asymmetry_threshold: float = ASYMMETRY_NEUTRAL_THRESHOLD,
) -> MicrostructureSignal:
    """Финальная классификация: direction_bias / vacuum / severity ∈ [0,1].

    `severity` = смесь магнитуды asymmetry и глубины vacuum'а. Это значение
    Bull/Bear агенты потом используют как probability multiplier в prompts
    (отдельный PR — здесь только готовим metric).
    """
    asym = float(aggregate.asymmetry_weighted)
    if not _is_finite(asym):
        bias = 0
    elif asym >= asymmetry_threshold:
        bias = +1
    elif asym <= -asymmetry_threshold:
        bias = -1
    else:
        bias = 0

    current_depth = aggregate.total_depth_usd()
    vacuum, drop = detect_liquidity_vacuum(
        current_depth, baseline_depth_usd, drop_pct=vacuum_drop_pct
    )

    # Severity: |asymmetry| (0…1) и drop_pct/100 (0…1). Берём max — оба
    # сигнала по-разному информативны, но любой из них strong = severity high.
    asym_mag = abs(asym) if _is_finite(asym) else 0.0
    drop_mag = (drop or 0.0) / 100.0 if vacuum and drop is not None else 0.0
    severity = max(min(asym_mag, 1.0), min(max(drop_mag, 0.0), 1.0))

    # Partial aggregates понижаем severity на 30% — не доверяем 1-venue-only.
    if aggregate.partial:
        severity *= 0.7

    return MicrostructureSignal(
        aggregate=aggregate,
        direction_bias=int(bias),
        vacuum=bool(vacuum),
        severity=float(max(0.0, min(1.0, severity))),
        baseline_depth_usd=baseline_depth_usd,
        drop_pct_observed=drop,
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────


def normalize_levels(
    raw_levels: Sequence[tuple[float, float]],
) -> tuple[OrderbookLevel, ...]:
    """Конвертит сырые [(price, size), ...] из REST API в OrderbookLevel.

    Игнорирует строки с не-числами / отрицательными / нулевыми значениями.
    Это защита от мусора в ответах (Hyperliquid иногда отдаёт пустые слои,
    Bybit — нулевые после большой свечи).
    """
    out: list[OrderbookLevel] = []
    for row in raw_levels:
        try:
            price = float(row[0])
            size = float(row[1])
        except (ValueError, TypeError, IndexError):
            continue
        if price > 0 and size > 0 and math.isfinite(price) and math.isfinite(size):
            out.append(OrderbookLevel(price=price, size=size))
    return tuple(out)
