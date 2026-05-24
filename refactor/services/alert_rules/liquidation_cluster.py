"""Liquidation magnet rule.

Surfaces an alert when ``market_indicators.liquidation_magnet`` classifies the
current state as a strong UP or DOWN magnet. These are predictive setups —
a heavy long bias plus a steep OI buildup historically precedes long-side
flushes (and vice versa for shorts).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

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
    return _env_flag("FEATURE_ALERT_LIQUIDATION_CLUSTER", True)


def get_cooldown_sec() -> int:
    return _env_int("ALERT_LIQUIDATION_COOLDOWN_SEC", 7200, minimum=600, maximum=86400)


def get_alert_neutral() -> bool:
    """If true, also emit a quiet INFO card on neutral classification."""
    return _env_flag("ALERT_LIQUIDATION_NOTIFY_NEUTRAL", False)


def _label_to_severity(label: str, *, is_strong: bool) -> str | None:
    if label == "down_magnet":
        return "CRIT" if is_strong else "WARN"
    if label == "up_magnet":
        return "CRIT" if is_strong else "WARN"
    return None


def _label_text(label: str) -> str:
    return {
        "down_magnet": "📉 Down magnet (longs liquidatable)",
        "up_magnet": "📈 Up magnet (shorts squeezable)",
        "neutral": "neutral",
        "unknown": "unknown",
    }.get(label, label)


@dataclass
class LiquidationClusterRule:
    rule_id: str = "liquidation_cluster"
    cooldown_sec: int = 0
    _fetch_signal: Any = None  # injectable for tests

    @classmethod
    def build(cls) -> "LiquidationClusterRule":
        return cls(cooldown_sec=get_cooldown_sec(), _fetch_signal=None)

    async def _resolve_fetch(self):
        if self._fetch_signal is not None:
            return self._fetch_signal
        try:
            from market_indicators.liquidation_magnet_io import (
                fetch_liquidation_magnet_signal,
            )
        except Exception as exc:
            logger.debug("liquidation_cluster: import failed: %s", exc)
            return None
        return fetch_liquidation_magnet_signal

    async def check(self) -> list[AlertCard]:
        if not feature_enabled():
            return []
        fetch = await self._resolve_fetch()
        if fetch is None:
            return []
        try:
            signal = await fetch()
        except Exception as exc:
            logger.info("liquidation_cluster rule: fetch failed: %s", exc)
            return []

        label = getattr(signal, "label", "unknown")
        severity = _label_to_severity(label, is_strong=getattr(signal, "is_strong_signal", False))
        if severity is None:
            return []

        ls = getattr(signal, "top_long_short_ratio", None)
        oi_change = getattr(signal, "oi_change_pct", 0.0)
        venue = getattr(signal, "venue", "") or "?"
        symbol = getattr(signal, "symbol", "") or "?"

        lines = [
            f"Symbol: `{symbol}` ({venue})",
            f"Label: *{_label_text(label)}*",
            f"OI change ({getattr(signal, 'oi_lookback_hours', 24)}h): {oi_change:+.2f}%",
        ]
        if isinstance(ls, (int, float)):
            lines.append(f"Top-trader L/S: {ls:.2f}")
        if getattr(signal, "is_strong_signal", False):
            lines.append("⚠️ *strong* signal — high conviction")

        body = "\n".join(lines)
        title = "Liquidation magnet"
        dedup = f"{symbol}:{label}:{'strong' if signal.is_strong_signal else 'soft'}"
        return [
            AlertCard(
                rule_id=self.rule_id,
                severity=severity,
                title=title,
                body=body,
                dedup_key=dedup,
            )
        ]
