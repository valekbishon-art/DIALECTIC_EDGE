"""Screener anomaly rule.

Wraps ``core.screener.MarketScreener``. Runs the same scan that backs the
``/screener`` Telegram command, but in the background loop and surfaced as
alerts when conviction is high enough.

A "high conviction" anomaly is defined as a coin with ≥ 2 independent signals
(volume spike, RSI extreme, funding anomaly) and at least one strong threshold
breach (volume × > 3, RSI < 20 or > 80, funding > 0.4% per 8h).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable

from refactor.services.alert_engine import AlertCard

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(float(raw))
    except (ValueError, TypeError):
        return default
    return max(minimum, min(maximum, v))


def feature_enabled() -> bool:
    return _env_flag("FEATURE_ALERT_SCREENER", True)


def get_top_n() -> int:
    return _env_int("ALERT_SCREENER_TOP_N", 15, minimum=5, maximum=50)


def get_cooldown_sec() -> int:
    return _env_int("ALERT_SCREENER_COOLDOWN_SEC", 3600, minimum=300, maximum=86400)


STRONG_VOL_X = 3.0
STRONG_RSI_LOW = 20.0
STRONG_RSI_HIGH = 80.0
STRONG_FUNDING_PCT_8H = 0.4  # absolute %


def _is_high_conviction(item: dict[str, Any]) -> bool:
    signals = item.get("signals") or []
    if len(signals) < 2:
        return False
    rsi = item.get("rsi")
    vol = item.get("vol_spike")
    funding = item.get("funding")
    strong = False
    if isinstance(rsi, (int, float)) and (rsi <= STRONG_RSI_LOW or rsi >= STRONG_RSI_HIGH):
        strong = True
    if isinstance(vol, (int, float)) and vol >= STRONG_VOL_X:
        strong = True
    if isinstance(funding, (int, float)) and abs(funding) * 100 >= STRONG_FUNDING_PCT_8H:
        strong = True
    return strong


def _severity_for(item: dict[str, Any]) -> str:
    rsi = item.get("rsi")
    vol = item.get("vol_spike")
    funding = item.get("funding")
    # CRIT when two of three strong thresholds tripped simultaneously.
    strong_flags = 0
    if isinstance(rsi, (int, float)) and (rsi <= STRONG_RSI_LOW or rsi >= STRONG_RSI_HIGH):
        strong_flags += 1
    if isinstance(vol, (int, float)) and vol >= STRONG_VOL_X:
        strong_flags += 1
    if isinstance(funding, (int, float)) and abs(funding) * 100 >= STRONG_FUNDING_PCT_8H:
        strong_flags += 1
    return "CRIT" if strong_flags >= 2 else "WARN"


def _build_body(item: dict[str, Any]) -> str:
    signals = item.get("signals") or []
    parts = ["• " + str(s) for s in signals]
    rsi = item.get("rsi")
    funding = item.get("funding")
    vol = item.get("vol_spike")
    extras: list[str] = []
    if isinstance(rsi, (int, float)):
        extras.append(f"RSI={rsi:.1f}")
    if isinstance(vol, (int, float)):
        extras.append(f"vol×{vol:.1f}")
    if isinstance(funding, (int, float)):
        extras.append(f"funding={funding * 100:.3f}%")
    if extras:
        parts.append("`" + " · ".join(extras) + "`")
    return "\n".join(parts) if parts else "high-conviction anomaly detected"


@dataclass
class ScreenerAnomalyRule:
    """AlertRule that surfaces high-conviction anomalies from MarketScreener."""

    rule_id: str = "screener_anomaly"
    cooldown_sec: int = 0  # filled by ``build()``

    @classmethod
    def build(cls) -> "ScreenerAnomalyRule":
        return cls(cooldown_sec=get_cooldown_sec())

    async def check(self) -> list[AlertCard]:
        if not feature_enabled():
            return []
        try:
            from core.screener import MarketScreener  # lazy import
        except Exception as exc:
            logger.debug("screener rule: cannot import MarketScreener: %s", exc)
            return []

        try:
            scanner = MarketScreener(top_n=get_top_n())
            scan_results: Iterable[dict[str, Any]] = await scanner.scan()
        except Exception as exc:
            logger.info("screener rule: scan failed: %s", exc)
            return []

        cards: list[AlertCard] = []
        for item in scan_results or []:
            if not _is_high_conviction(item):
                continue
            symbol = str(item.get("symbol") or "?").upper()
            severity = _severity_for(item)
            title = f"Anomaly: {symbol}"
            body = _build_body(item)
            cards.append(
                AlertCard(
                    rule_id=self.rule_id,
                    severity=severity,
                    title=title,
                    body=body,
                    dedup_key=f"{symbol}:{severity}",
                )
            )
        return cards
