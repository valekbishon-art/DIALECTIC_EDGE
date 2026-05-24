"""BTC outlook auto-alert state machine.

Pure logic — decides whether to fire an alert given a fresh ``BTCOutlookVerdict``
and the last fired snapshot. No I/O, no Telegram. Scheduler wraps it.

Triggers (any one suffices when cooldown elapsed):

1. **First high-confidence run**: previous snapshot is None and current
   ``confidence_pct >= min_confidence``.
2. **Lean flip**: previous lean differs from current and current
   ``confidence_pct >= min_confidence``.
3. **Confidence jump**: same lean, but current confidence exceeds previous
   by ``confidence_delta`` AND current ``confidence_pct >= min_confidence``.

NEUTRAL→NEUTRAL never fires (we don't alert on "still flat"). Cooldown is
enforced regardless of trigger — even a lean flip won't double-fire within
``cooldown_sec``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from core.btc_outlook import LEAN_NEUTRAL, BTCOutlookVerdict

logger = logging.getLogger(__name__)


DEFAULT_ALERT_INTERVAL_SEC = 1800
DEFAULT_ALERT_MIN_CONFIDENCE = 70
DEFAULT_ALERT_CONFIDENCE_DELTA = 15
DEFAULT_ALERT_COOLDOWN_SEC = 7200


@dataclass(frozen=True)
class BTCAlertSnapshot:
    """Last fired alert state — what scheduler persists between cycles."""

    lean: str
    confidence_pct: int
    fired_at_ts: float


@dataclass(frozen=True)
class BTCAlertDecision:
    """Output of ``should_fire_btc_alert``: yes/no + reason for logging."""

    should_fire: bool
    reason: str  # human-readable, also used as headline tag in the message
    suppressed_reason: str = ""  # if should_fire=False, why we held back


def _env_int(name: str, default: int, *, min_val: int = 0, max_val: int = 10**9) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(min_val, min(max_val, v))


def feature_enabled() -> bool:
    raw = os.getenv("FEATURE_BTC_OUTLOOK_ALERTS", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_alert_interval_sec() -> int:
    return _env_int(
        "BTC_OUTLOOK_ALERT_INTERVAL_SEC",
        DEFAULT_ALERT_INTERVAL_SEC,
        min_val=60,
        max_val=24 * 3600,
    )


def get_alert_min_confidence() -> int:
    return _env_int(
        "BTC_OUTLOOK_ALERT_MIN_CONFIDENCE",
        DEFAULT_ALERT_MIN_CONFIDENCE,
        min_val=0,
        max_val=100,
    )


def get_alert_confidence_delta() -> int:
    return _env_int(
        "BTC_OUTLOOK_ALERT_CONFIDENCE_DELTA",
        DEFAULT_ALERT_CONFIDENCE_DELTA,
        min_val=0,
        max_val=100,
    )


def get_alert_cooldown_sec() -> int:
    return _env_int(
        "BTC_OUTLOOK_ALERT_COOLDOWN_SEC",
        DEFAULT_ALERT_COOLDOWN_SEC,
        min_val=0,
        max_val=24 * 3600,
    )


def get_alert_chat_ids() -> tuple[int, ...]:
    """Comma-separated chat IDs from ``BTC_OUTLOOK_ALERT_CHAT_IDS``.

    Empty → caller should fall back to ``ADMIN_IDS``.
    """
    raw = os.getenv("BTC_OUTLOOK_ALERT_CHAT_IDS", "").strip()
    if not raw:
        return ()
    out: list[int] = []
    for chunk in raw.split(","):
        c = chunk.strip()
        if not c:
            continue
        try:
            out.append(int(c))
        except ValueError:
            continue
    return tuple(out)


def should_fire_btc_alert(
    *,
    current: BTCOutlookVerdict,
    previous: Optional[BTCAlertSnapshot],
    now_ts: float,
    min_confidence: int | None = None,
    confidence_delta: int | None = None,
    cooldown_sec: int | None = None,
) -> BTCAlertDecision:
    """Decide whether to fire an alert.

    Pure function — caller is responsible for persisting the snapshot after
    a successful send. ``current.contributions`` MUST have at least one entry;
    if empty (all sources failed), we always suppress.
    """
    mc = get_alert_min_confidence() if min_confidence is None else min_confidence
    delta = get_alert_confidence_delta() if confidence_delta is None else confidence_delta
    cooldown = get_alert_cooldown_sec() if cooldown_sec is None else cooldown_sec

    if not current.contributions:
        return BTCAlertDecision(False, "", "no signals captured")

    if current.lean == LEAN_NEUTRAL and (previous is None or previous.lean == LEAN_NEUTRAL):
        return BTCAlertDecision(False, "", "neutral→neutral, nothing to escalate")

    if current.confidence_pct < mc:
        return BTCAlertDecision(
            False,
            "",
            f"confidence {current.confidence_pct} < min {mc}",
        )

    # Cooldown — enforced regardless of trigger type.
    if previous is not None and (now_ts - previous.fired_at_ts) < cooldown:
        remaining = int(cooldown - (now_ts - previous.fired_at_ts))
        return BTCAlertDecision(
            False,
            "",
            f"cooldown {remaining}s remaining",
        )

    if previous is None:
        return BTCAlertDecision(True, "first-fire (warmup)")

    if previous.lean != current.lean:
        return BTCAlertDecision(
            True,
            f"lean flip: {previous.lean}→{current.lean}",
        )

    confidence_jump = current.confidence_pct - previous.confidence_pct
    if confidence_jump >= delta:
        return BTCAlertDecision(
            True,
            f"confidence jump: {previous.confidence_pct}→{current.confidence_pct} (+{confidence_jump})",
        )

    return BTCAlertDecision(
        False,
        "",
        (
            f"same lean {current.lean}, confidence Δ={confidence_jump} < {delta}"
        ),
    )


def format_btc_alert_headline(decision: BTCAlertDecision, verdict: BTCOutlookVerdict) -> str:
    """Short headline tag for the alert message.

    Used as the first line of the Telegram alert, on top of the regular
    formatted outlook body.
    """
    lean = verdict.lean
    emoji = "🟢" if lean == "BULL" else "🔴" if lean == "BEAR" else "⚪"
    return (
        f"🚨 *BTC outlook alert* {emoji}\n"
        f"_{decision.reason} — {lean} {verdict.confidence_pct}%_\n"
    )


__all__ = [
    "BTCAlertSnapshot",
    "BTCAlertDecision",
    "DEFAULT_ALERT_INTERVAL_SEC",
    "DEFAULT_ALERT_MIN_CONFIDENCE",
    "DEFAULT_ALERT_CONFIDENCE_DELTA",
    "DEFAULT_ALERT_COOLDOWN_SEC",
    "feature_enabled",
    "get_alert_interval_sec",
    "get_alert_min_confidence",
    "get_alert_confidence_delta",
    "get_alert_cooldown_sec",
    "get_alert_chat_ids",
    "should_fire_btc_alert",
    "format_btc_alert_headline",
]
