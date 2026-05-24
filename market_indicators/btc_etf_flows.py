"""BTC spot-ETF activity tracker.

Pulls daily OHLCV for the main US-listed BTC spot-ETFs from Yahoo Finance and
derives a per-day directional signal. We do NOT have a free-tier source for
actual creation/redemption USD flow, so this module uses price action across
the basket as a *proxy*: if the basket is dropping with high volume across
consecutive sessions, that is consistent with institutional redemption
pressure (which is what historically precedes BTC spot flushes).

For authoritative $-amount flows the recommended source is Farside Investors
(<https://farside.co.uk/btc/>). The module also exposes the underlying per-day
close/volume so a future integration can replace the proxy in place.

Public surface:
- ``feature_enabled()``: ``FEATURE_ALERT_BTC_ETF`` flag.
- ``fetch_btc_etf_dailies()``: returns a list of per-ETF daily snapshots.
- ``aggregate_basket_flows()``: collapses snapshots into per-day basket avg.
- ``detect_outflow_signal()``: returns a structured signal if streak/threshold
  conditions trip, else ``None``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
DEFAULT_TICKERS = ("IBIT", "FBTC", "BITB", "ARKB", "BTCO")
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_OUTFLOW_DAY_PCT = 1.5  # avg basket close-to-close drop counted as "outflow day"
DEFAULT_OUTFLOW_STREAK_DAYS = 3
DEFAULT_BIG_DAY_DROP_PCT = 4.0  # single-session drop threshold
USER_AGENT = "DialecticEdge/1.0"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return default
    return max(minimum, v)


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 30) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(float(raw))
    except (ValueError, TypeError):
        return default
    return max(minimum, min(maximum, v))


def feature_enabled() -> bool:
    return _env_flag("FEATURE_ALERT_BTC_ETF", True)


def get_tickers() -> tuple[str, ...]:
    raw = os.getenv("ALERT_BTC_ETF_TICKERS", "")
    if not raw.strip():
        return DEFAULT_TICKERS
    pieces = tuple(p.strip().upper() for p in raw.split(",") if p.strip())
    return pieces or DEFAULT_TICKERS


def get_outflow_day_pct() -> float:
    return _env_float("ALERT_BTC_ETF_OUTFLOW_DAY_PCT", DEFAULT_OUTFLOW_DAY_PCT)


def get_outflow_streak_days() -> int:
    return _env_int(
        "ALERT_BTC_ETF_STREAK_DAYS", DEFAULT_OUTFLOW_STREAK_DAYS, minimum=2, maximum=14
    )


def get_big_day_drop_pct() -> float:
    return _env_float("ALERT_BTC_ETF_BIG_DAY_DROP_PCT", DEFAULT_BIG_DAY_DROP_PCT)


def get_cooldown_sec() -> int:
    return _env_int("ALERT_BTC_ETF_COOLDOWN_SEC", 21600, minimum=600, maximum=86400)


@dataclass(frozen=True)
class EtfDaily:
    ticker: str
    close: float
    prev_close: float | None
    volume: float
    change_pct: float | None


@dataclass(frozen=True)
class OutflowSignal:
    severity: str  # "WARN" or "CRIT"
    streak_days: int
    avg_basket_change_pct: float
    worst_day_pct: float
    tickers_considered: tuple[str, ...]
    summary: str
    dedup_key: str


def _parse_yahoo_chart(payload: Any, ticker: str) -> list[EtfDaily]:
    """Convert raw Yahoo `/chart` payload into per-day EtfDaily rows."""
    try:
        results = payload.get("chart", {}).get("result", [])
        if not results:
            return []
        node = results[0]
        indicators = node.get("indicators", {}).get("quote", [{}])[0]
        closes = [float(c) for c in indicators.get("close", []) if c is not None]
        volumes = [float(v) for v in indicators.get("volume", []) if v is not None]
    except (AttributeError, TypeError, ValueError):
        return []

    if len(closes) < 2:
        return []

    out: list[EtfDaily] = []
    for idx in range(1, len(closes)):
        close = closes[idx]
        prev = closes[idx - 1]
        if close <= 0 or prev <= 0:
            continue
        change_pct = (close - prev) / prev * 100.0
        vol = volumes[idx] if idx < len(volumes) else 0.0
        out.append(
            EtfDaily(
                ticker=ticker,
                close=close,
                prev_close=prev,
                volume=vol,
                change_pct=change_pct,
            )
        )
    return out


async def fetch_btc_etf_dailies(
    *,
    http_get,
    tickers: Sequence[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[EtfDaily]:
    """Fetch daily OHLCV per ticker.

    ``http_get(url, params)`` must be an awaitable returning a dict-like JSON
    payload (or ``None`` on failure). Injecting it as a callable keeps the
    module HTTP-library-agnostic and trivial to unit-test.
    """
    tickers = tuple(tickers or get_tickers())
    out: list[EtfDaily] = []
    for ticker in tickers:
        url = YAHOO_CHART_URL.format(ticker=ticker)
        try:
            payload = await http_get(
                url,
                {"range": f"{max(2, lookback_days)}d", "interval": "1d"},
            )
        except Exception as exc:
            logger.debug("btc_etf_flows fetch %s failed: %s", ticker, exc)
            continue
        if not payload:
            continue
        out.extend(_parse_yahoo_chart(payload, ticker))
    return out


def aggregate_basket_flows(rows: Iterable[EtfDaily]) -> list[dict[str, Any]]:
    """Collapse per-ETF dailies into per-day basket-average snapshots.

    Each output row is ordered chronologically (oldest first) and contains:
    ``{"day_idx", "avg_change_pct", "tickers_seen", "volumes_sum"}``.

    We don't have explicit dates from Yahoo's chart payload here, so days are
    aligned by index *within each ticker's series*. We expect Yahoo to return
    the same number of points across tickers for the same `range`/`interval`.
    """
    grouped: dict[str, list[EtfDaily]] = {}
    for row in rows:
        grouped.setdefault(row.ticker, []).append(row)

    if not grouped:
        return []

    min_len = min(len(v) for v in grouped.values())
    if min_len == 0:
        return []

    out: list[dict[str, Any]] = []
    for day_idx in range(min_len):
        changes: list[float] = []
        volumes: list[float] = []
        seen: list[str] = []
        for ticker, series in grouped.items():
            row = series[day_idx]
            if row.change_pct is None:
                continue
            changes.append(row.change_pct)
            volumes.append(row.volume)
            seen.append(ticker)
        if not changes:
            continue
        out.append(
            {
                "day_idx": day_idx,
                "avg_change_pct": sum(changes) / len(changes),
                "tickers_seen": tuple(seen),
                "volumes_sum": sum(volumes),
            }
        )
    return out


def detect_outflow_signal(
    basket: Sequence[dict[str, Any]],
    *,
    outflow_day_pct: float | None = None,
    streak_days: int | None = None,
    big_day_drop_pct: float | None = None,
) -> OutflowSignal | None:
    """Decide if the basket flow pattern triggers an alert.

    Returns ``None`` when no alert should fire.
    """
    if not basket:
        return None

    day_threshold = outflow_day_pct if outflow_day_pct is not None else get_outflow_day_pct()
    streak_threshold = streak_days if streak_days is not None else get_outflow_streak_days()
    big_threshold = big_day_drop_pct if big_day_drop_pct is not None else get_big_day_drop_pct()

    streak = 0
    streak_sum = 0.0
    for day in basket:
        if day["avg_change_pct"] <= -day_threshold:
            streak += 1
            streak_sum += day["avg_change_pct"]
        else:
            streak = 0
            streak_sum = 0.0
    worst = min((d["avg_change_pct"] for d in basket), default=0.0)
    tickers = tuple(sorted({t for d in basket for t in d.get("tickers_seen", ())}))

    if worst <= -big_threshold:
        return OutflowSignal(
            severity="CRIT",
            streak_days=streak,
            avg_basket_change_pct=streak_sum / streak if streak else worst,
            worst_day_pct=worst,
            tickers_considered=tickers,
            summary=(
                f"BTC ETF basket: одна сессия {worst:.2f}% (порог "
                f"{big_threshold:.1f}%). Возможен redemption-каскад."
            ),
            dedup_key=f"big-day:{round(worst, 2)}",
        )

    if streak >= streak_threshold:
        return OutflowSignal(
            severity="WARN",
            streak_days=streak,
            avg_basket_change_pct=streak_sum / streak,
            worst_day_pct=worst,
            tickers_considered=tickers,
            summary=(
                f"BTC ETF basket: {streak}+ дн. подряд отток "
                f"(средн {streak_sum / streak:.2f}% / день). Похоже на 6-day "
                f"ETF-streak который предшествовал недавнему BTC-флэшу."
            ),
            dedup_key=f"streak:{streak}",
        )

    return None
