"""Options skew + ATM IV term structure — pure math (stdlib only).

Зачем:
  Спотовый рынок реагирует пост-фактум. Опционные рынки прайсят будущий риск
  раньше — это leading-indicator уровня TradFi (VIX, SKEW index у CBOE).
  Особенно характерен **25-delta risk-reversal** = (call_IV at Δ=+0.25) -
  (put_IV at Δ=-0.25). Отрицательный RR (≤ -3 vol points) = market платит
  премию за put'ы → crash hedging / страх. Положительный RR = call skew →
  euphoria / FOMO. Аналог equity SKEW index, но для крипты.

  Также строим **ATM IV term structure**: σ(7d) vs σ(30d). Если ближняя
  выше дальней (term backwardation) — рынок ждёт impulse в ближайшие дни.

Что НЕ делает (намеренно):
  * Не трогает trading-tables, signals.py, dynamic_risk.py.
  * Не использует numpy / scipy / pandas. Stdlib only — math.erf хватает.
  * Не делает full vol-surface fitting (это +1000 строк и new deps).

Black-Scholes без процентной ставки (r=0 — стандартное допущение для крипты
у Deribit; они котируют опционы quote'ом в самом базовом активе, без
funding cost):
    d1 = (ln(S/K) + 0.5 σ² T) / (σ √T)
    Δ_call = N(d1)
    Δ_put  = Δ_call - 1

Конвенция знаков:
    risk_reversal_25d > 0  → call skew → bullish / FOMO
    risk_reversal_25d < 0  → put skew  → bearish / crash hedging
    atm_iv_term_slope > 0  → backwardation → near-term stress
    atm_iv_term_slope < 0  → standard contango → calm

Внешние зависимости: только stdlib.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Target delta для risk-reversal (стандарт у Deribit и TradFi).
TARGET_DELTA = 0.25

#: Бакеты для term structure (days to expiry).
DEFAULT_NEAR_DAYS_MIN = 3
DEFAULT_NEAR_DAYS_MAX = 14
DEFAULT_FAR_DAYS_MIN = 21
DEFAULT_FAR_DAYS_MAX = 45

#: Пороги классификации RR_25d (в долях, не vol points). 0.03 = 3 vol points.
RR_PUT_SKEW_EXTREME = -0.05
RR_PUT_SKEW = -0.02
RR_CALL_SKEW = 0.02
RR_CALL_SKEW_EXTREME = 0.05

#: Sanity bounds на IV (доля): 5% — 500% годовых.
SANITY_IV_MIN = 0.05
SANITY_IV_MAX = 5.0


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptionQuote:
    """Одна котировка опциона (как из Deribit get_book_summary_by_currency).

    `mark_iv` — implied vol в долях (0.65 = 65% годовых).
    `kind`    — 'C' (call) или 'P' (put).
    """

    instrument_name: str       # 'BTC-26DEC25-100000-C'
    currency: str              # 'BTC'
    kind: str                  # 'C' | 'P'
    strike: float              # 100000.0
    expiry_ms: int             # unix ms
    mark_iv: float             # доля (0.65 = 65% годовых)
    underlying_price: float    # spot/index в момент снимка


@dataclass(frozen=True)
class OptionsSkewSignal:
    """Снимок options skew по одному активу.

    `*_iv` — в долях (0.65 = 65% годовых).
    `risk_reversal_*` — call_iv - put_iv (в долях, 0.03 = 3 vol points).
    """

    currency: str
    timestamp_ms: int
    underlying_price: float

    near_expiry_days: int | None
    near_atm_iv: float | None
    near_rr_25d: float | None

    far_expiry_days: int | None
    far_atm_iv: float | None
    far_rr_25d: float | None

    atm_iv_term_slope: float | None  # far_atm_iv - near_atm_iv (доля)
    skew_class: str  # см. classify_skew_class
    venues_used: tuple[str, ...] = ()


# ─── Парсер instrument_name ──────────────────────────────────────────────────

_DERIBIT_RE = re.compile(
    r"^(?P<cur>[A-Z]+)-(?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<yr>\d{2})-"
    r"(?P<strike>\d+(?:\.\d+)?)-(?P<kind>[CP])$"
)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_deribit_option_name(name: str) -> dict[str, object] | None:
    """Распарсить 'BTC-26DEC25-100000-C' → dict с currency/strike/expiry/kind.

    Возвращает dict или None при невалидном формате.
    """
    if not name:
        return None
    m = _DERIBIT_RE.match(str(name).strip().upper())
    if not m:
        return None
    try:
        day = int(m.group("day"))
        mon = _MONTHS.get(m.group("mon"))
        yr = 2000 + int(m.group("yr"))
        strike = float(m.group("strike"))
        if mon is None or day < 1 or day > 31:
            return None
        # Deribit settles на 08:00 UTC в день экспирации.
        expiry = datetime(yr, mon, day, 8, 0, 0)
    except (ValueError, TypeError):
        return None
    return {
        "currency": m.group("cur"),
        "kind": m.group("kind"),
        "strike": strike,
        "expiry": expiry,
    }


def estimate_days_to_expiry(*, expiry_date: datetime, now: datetime | None = None) -> int:
    """Целое число дней до экспирации (ceil). 0 если уже прошёл."""
    moment = now or datetime.utcnow()
    delta = expiry_date - moment
    secs = int(delta.total_seconds())
    if secs <= 0:
        return 0
    return max(1, (secs + 86_399) // 86_400)


# ─── Black-Scholes (r=0) ─────────────────────────────────────────────────────


def norm_cdf(x: float) -> float:
    """Cumulative normal distribution Φ(x) — через math.erf."""
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def bs_d1(*, spot: float, strike: float, t_years: float, iv: float) -> float:
    """Black-Scholes d1 при r=0 (стандарт для крипты на Deribit).

    d1 = (ln(S/K) + 0.5 σ² T) / (σ √T)
    """
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        raise ValueError(
            f"bs_d1 bad args: S={spot} K={strike} T={t_years} σ={iv}"
        )
    return (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))


def call_delta(*, spot: float, strike: float, t_years: float, iv: float) -> float:
    """Δ_call = N(d1). r=0."""
    return norm_cdf(bs_d1(spot=spot, strike=strike, t_years=t_years, iv=iv))


def put_delta(*, spot: float, strike: float, t_years: float, iv: float) -> float:
    """Δ_put = N(d1) - 1. r=0."""
    return call_delta(spot=spot, strike=strike, t_years=t_years, iv=iv) - 1.0


# ─── Selection по delta ──────────────────────────────────────────────────────


def _iv_sane(iv: float) -> bool:
    if not math.isfinite(iv):
        return False
    return SANITY_IV_MIN <= iv <= SANITY_IV_MAX


def find_atm_iv(
    *, quotes: Sequence[OptionQuote], spot: float,
) -> float | None:
    """ATM IV: средний mark_iv колла и пута, чьи strike'и ближе всего к spot.

    Бережёмся от выбросов: возвращаем None если не нашли валидных IV.
    """
    if spot <= 0:
        return 0.0 if False else None  # unreachable but explicit
    calls = [q for q in quotes if q.kind == "C" and _iv_sane(q.mark_iv)]
    puts = [q for q in quotes if q.kind == "P" and _iv_sane(q.mark_iv)]
    best_call = min(calls, key=lambda q: abs(q.strike - spot)) if calls else None
    best_put = min(puts, key=lambda q: abs(q.strike - spot)) if puts else None
    ivs: list[float] = []
    if best_call is not None:
        ivs.append(float(best_call.mark_iv))
    if best_put is not None:
        ivs.append(float(best_put.mark_iv))
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def find_delta_target_iv(
    *,
    quotes: Sequence[OptionQuote],
    spot: float,
    t_years: float,
    target_delta: float,
    kind: str,
) -> float | None:
    """Найти IV опциона нужного `kind` ('C'|'P'), Δ которого ближе всего
    к `target_delta` (по абсолютной величине).

    `target_delta` ожидается:
      kind='C' → 0.25 (стандартный 25Δ call)
      kind='P' → -0.25 (стандартный 25Δ put)

    Если spot/t_years/iv невалидные — пропускаем такой quote. Возвращаем
    None если не нашли ни одного подходящего.
    """
    if spot <= 0 or t_years <= 0:
        return None
    candidates: list[tuple[float, float]] = []  # (|delta - target|, iv)
    for q in quotes:
        if q.kind != kind:
            continue
        if not _iv_sane(q.mark_iv):
            continue
        try:
            if kind == "C":
                delta = call_delta(
                    spot=spot, strike=q.strike, t_years=t_years, iv=q.mark_iv,
                )
            else:
                delta = put_delta(
                    spot=spot, strike=q.strike, t_years=t_years, iv=q.mark_iv,
                )
        except (ValueError, ZeroDivisionError):
            continue
        if not math.isfinite(delta):
            continue
        candidates.append((abs(delta - float(target_delta)), float(q.mark_iv)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def risk_reversal_25d(
    *, quotes: Sequence[OptionQuote], spot: float, t_years: float,
) -> float | None:
    """RR_25d = IV(25Δ call) - IV(25Δ put). Доля, не vol points.

    None если не нашли пары call/put с валидной IV.
    """
    call_iv = find_delta_target_iv(
        quotes=quotes, spot=spot, t_years=t_years,
        target_delta=TARGET_DELTA, kind="C",
    )
    put_iv = find_delta_target_iv(
        quotes=quotes, spot=spot, t_years=t_years,
        target_delta=-TARGET_DELTA, kind="P",
    )
    if call_iv is None or put_iv is None:
        return None
    return float(call_iv) - float(put_iv)


# ─── Классификация ──────────────────────────────────────────────────────────


def classify_skew_class(rr_25d: float | None) -> str:
    """Категория skew по уровню RR_25d.

    'extreme_put_skew' / 'put_skew' / 'neutral' / 'call_skew' /
    'extreme_call_skew' / 'unknown'.
    """
    if rr_25d is None or not math.isfinite(rr_25d):
        return "unknown"
    rr = float(rr_25d)
    if rr <= RR_PUT_SKEW_EXTREME:
        return "extreme_put_skew"
    if rr <= RR_PUT_SKEW:
        return "put_skew"
    if rr >= RR_CALL_SKEW_EXTREME:
        return "extreme_call_skew"
    if rr >= RR_CALL_SKEW:
        return "call_skew"
    return "neutral"


# ─── Term structure aggregation ─────────────────────────────────────────────


def _bucket_quotes(
    quotes: Sequence[OptionQuote],
    *,
    days_min: int,
    days_max: int,
    now: datetime | None = None,
) -> tuple[list[OptionQuote], int | None]:
    """Отфильтровать quotes по бакету days_to_expiry ∈ [days_min, days_max].

    Если в бакете несколько expir'ов — берём тот, где больше всего опционов
    (ликвидность). Возвращает (filtered_quotes, expiry_days_used).
    """
    moment = now or datetime.utcnow()
    by_expiry: dict[int, list[OptionQuote]] = {}
    for q in quotes:
        d2e = estimate_days_to_expiry(
            expiry_date=datetime.utcfromtimestamp(q.expiry_ms / 1000.0),
            now=moment,
        )
        if days_min <= d2e <= days_max:
            by_expiry.setdefault(d2e, []).append(q)
    if not by_expiry:
        return ([], None)
    best_day = max(by_expiry.keys(), key=lambda d: len(by_expiry[d]))
    return (by_expiry[best_day], best_day)


def build_options_skew(
    *,
    currency: str,
    quotes: Sequence[OptionQuote],
    timestamp_ms: int,
    underlying_price: float,
    near_days: tuple[int, int] = (DEFAULT_NEAR_DAYS_MIN, DEFAULT_NEAR_DAYS_MAX),
    far_days: tuple[int, int] = (DEFAULT_FAR_DAYS_MIN, DEFAULT_FAR_DAYS_MAX),
    venues: Sequence[str] = ("deribit",),
    now: datetime | None = None,
) -> OptionsSkewSignal:
    """Собрать OptionsSkewSignal по списку опционных котировок.

    Бакеты дней:
      near (~7d):  default 3..14d
      far  (~30d): default 21..45d

    Если в бакете нет квотов — соответствующие поля None.
    Slope = far_atm_iv - near_atm_iv (None если хотя бы один None).
    skew_class определяется по far_rr_25d (более стабильный 30d сигнал).
    """
    rel = [q for q in quotes if q.currency.upper() == currency.upper()]

    near_quotes, near_days_used = _bucket_quotes(
        rel, days_min=near_days[0], days_max=near_days[1], now=now,
    )
    far_quotes, far_days_used = _bucket_quotes(
        rel, days_min=far_days[0], days_max=far_days[1], now=now,
    )

    near_atm = find_atm_iv(quotes=near_quotes, spot=underlying_price)
    far_atm = find_atm_iv(quotes=far_quotes, spot=underlying_price)

    near_rr = None
    if near_days_used is not None:
        near_rr = risk_reversal_25d(
            quotes=near_quotes, spot=underlying_price,
            t_years=float(near_days_used) / 365.0,
        )
    far_rr = None
    if far_days_used is not None:
        far_rr = risk_reversal_25d(
            quotes=far_quotes, spot=underlying_price,
            t_years=float(far_days_used) / 365.0,
        )

    slope: float | None = None
    if near_atm is not None and far_atm is not None:
        slope = float(far_atm) - float(near_atm)

    return OptionsSkewSignal(
        currency=currency.upper(),
        timestamp_ms=int(timestamp_ms),
        underlying_price=float(underlying_price),
        near_expiry_days=near_days_used,
        near_atm_iv=near_atm,
        near_rr_25d=near_rr,
        far_expiry_days=far_days_used,
        far_atm_iv=far_atm,
        far_rr_25d=far_rr,
        atm_iv_term_slope=slope,
        skew_class=classify_skew_class(far_rr if far_rr is not None else near_rr),
        venues_used=tuple(venues),
    )


# ─── Event detection ─────────────────────────────────────────────────────────


def detect_skew_event(
    *,
    current: OptionsSkewSignal,
    previous: OptionsSkewSignal | None,
    cross_threshold: float = abs(RR_PUT_SKEW),
) -> str | None:
    """Сравнить current vs previous и вернуть тип события:

      * 'put_skew_onset'     — нейтрал/call → put_skew.
      * 'put_skew_recovery'  — put_skew → нейтрал/call.
      * 'call_skew_onset'    — нейтрал/put → call_skew.
      * 'call_skew_recovery' — call_skew → нейтрал/put.
      * None — нет состояния.

    cross_threshold — зона, ниже которой RR считается нейтральным.
    """
    if previous is None:
        return None
    cur = current.far_rr_25d if current.far_rr_25d is not None else current.near_rr_25d
    prev = previous.far_rr_25d if previous.far_rr_25d is not None else previous.near_rr_25d
    if cur is None or prev is None:
        return None
    thr = max(0.0, float(cross_threshold))
    cur_put = cur <= -thr
    cur_call = cur >= thr
    prev_put = prev <= -thr
    prev_call = prev >= thr
    if cur_put and not prev_put:
        return "put_skew_onset"
    if prev_put and not cur_put:
        return "put_skew_recovery"
    if cur_call and not prev_call:
        return "call_skew_onset"
    if prev_call and not cur_call:
        return "call_skew_recovery"
    return None


# ─── Formatters ──────────────────────────────────────────────────────────────


def format_skew_summary(
    signal: OptionsSkewSignal, *, event: str | None = None,
) -> str:
    """Однострочный summary для логов."""

    def _vp(x: float | None) -> str:
        return f"{x * 100:+.2f}vp" if x is not None and math.isfinite(x) else "n/a"

    def _pct(x: float | None) -> str:
        return f"{x * 100:.1f}%" if x is not None and math.isfinite(x) else "n/a"

    near_iv = _pct(signal.near_atm_iv)
    far_iv = _pct(signal.far_atm_iv)
    near_rr = _vp(signal.near_rr_25d)
    far_rr = _vp(signal.far_rr_25d)
    slope = _vp(signal.atm_iv_term_slope)
    tag = f" event={event}" if event else ""
    return (
        f"options-skew {signal.currency} "
        f"px={signal.underlying_price:.2f} "
        f"near({signal.near_expiry_days or 0}d) iv={near_iv} rr25={near_rr} | "
        f"far({signal.far_expiry_days or 0}d) iv={far_iv} rr25={far_rr} | "
        f"term_slope={slope} class={signal.skew_class}{tag}"
    )
