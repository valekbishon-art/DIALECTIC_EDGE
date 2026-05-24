"""Self-audit loop for P2P arbitrage opportunities.

Pure-math (stdlib-only) helpers that:

1. Compare a previously surfaced :class:`P2POpportunity` against the current
   orderbook → estimates *realised* spread.
2. Aggregate realised vs. shown spreads over the last N audited records and
   recommends adaptive threshold adjustments (raise/lower
   ``P2P_ARBITRAGE_MIN_SPREAD_PCT``).
3. Format human-readable audit summaries for ``/p2paudit`` Telegram command.

The persistence/HTTP side lives in :mod:`p2p_audit_io` — this module is
exchange-agnostic and free of I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Sequence

from p2p_arbitrage import P2PAdvert, P2POpportunity


# ─── Status constants ───────────────────────────────────────────────────────

STATUS_PENDING = "pending"
"""Record awaiting backcheck (within delay window)."""

STATUS_CONFIRMED = "confirmed"
"""Backcheck found a spread within tolerance of original net_spread_pct."""

STATUS_DECAYED = "decayed"
"""Backcheck found a strictly smaller spread (opportunity faded)."""

STATUS_AMPLIFIED = "amplified"
"""Backcheck found a strictly larger spread (we surfaced too late)."""

STATUS_VANISHED = "vanished"
"""Backcheck found no matching pair (ads gone, no opportunity left)."""

STATUS_EXPIRED = "expired"
"""Backcheck delay passed without a confirmed match — stale record."""

ALL_RESOLVED_STATUSES = (
    STATUS_CONFIRMED,
    STATUS_DECAYED,
    STATUS_AMPLIFIED,
    STATUS_VANISHED,
    STATUS_EXPIRED,
)


# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_BACKCHECK_DELAY_MIN = 60
"""Wait this long after surfacing an opportunity before running backcheck."""

DEFAULT_BACKCHECK_INTERVAL_MIN = 15
"""Scheduler tick interval — process pending records every N minutes."""

DEFAULT_PRICE_TOLERANCE_PCT = 0.5
"""Match the same advertiser-side by price within this percent."""

DEFAULT_DECAY_THRESHOLD_PCT = 25.0
"""If realised spread drops more than this % of net_spread_pct vs shown → decayed."""

DEFAULT_THRESHOLD_ADJUST_PCT = 0.1
"""How much to nudge ``P2P_ARBITRAGE_MIN_SPREAD_PCT`` per recommendation."""

DEFAULT_THRESHOLD_ADJUST_MIN_SAMPLES = 10
"""Don't recommend adjustments below this sample count."""

DEFAULT_AUDIT_RETENTION_DAYS = 14
"""Hard retention for audit log (records past this age get cleaned)."""


# ─── Env helpers (stdlib only) ───────────────────────────────────────────────


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, min_val: float, max_val: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < min_val or value > max_val:
        return default
    return value


def _env_int(name: str, default: int, *, min_val: int, max_val: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < min_val or value > max_val:
        return default
    return value


def feature_enabled() -> bool:
    """`FEATURE_P2P_SELF_AUDIT=1` enables persist + backcheck loop."""
    return _env_bool("FEATURE_P2P_SELF_AUDIT", False)


def get_backcheck_delay_min() -> int:
    return _env_int(
        "P2P_AUDIT_BACKCHECK_DELAY_MIN",
        DEFAULT_BACKCHECK_DELAY_MIN,
        min_val=5,
        max_val=24 * 60,
    )


def get_backcheck_interval_min() -> int:
    return _env_int(
        "P2P_AUDIT_BACKCHECK_INTERVAL_MIN",
        DEFAULT_BACKCHECK_INTERVAL_MIN,
        min_val=1,
        max_val=240,
    )


def get_price_tolerance_pct() -> float:
    return _env_float(
        "P2P_AUDIT_PRICE_TOLERANCE_PCT",
        DEFAULT_PRICE_TOLERANCE_PCT,
        min_val=0.05,
        max_val=5.0,
    )


def get_decay_threshold_pct() -> float:
    return _env_float(
        "P2P_AUDIT_DECAY_THRESHOLD_PCT",
        DEFAULT_DECAY_THRESHOLD_PCT,
        min_val=5.0,
        max_val=90.0,
    )


def get_threshold_adjust_pct() -> float:
    return _env_float(
        "P2P_AUDIT_THRESHOLD_ADJUST_PCT",
        DEFAULT_THRESHOLD_ADJUST_PCT,
        min_val=0.01,
        max_val=1.0,
    )


def get_threshold_adjust_min_samples() -> int:
    return _env_int(
        "P2P_AUDIT_THRESHOLD_ADJUST_MIN_SAMPLES",
        DEFAULT_THRESHOLD_ADJUST_MIN_SAMPLES,
        min_val=3,
        max_val=500,
    )


def get_retention_days() -> int:
    return _env_int(
        "P2P_AUDIT_RETENTION_DAYS",
        DEFAULT_AUDIT_RETENTION_DAYS,
        min_val=1,
        max_val=365,
    )


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OpportunityAuditRecord:
    """One persisted P2P opportunity tracked through its lifecycle.

    Created when an opportunity is surfaced to the user; resolved by the
    backcheck loop once `shown_at_ms + backcheck_delay_min` has elapsed.
    """

    opportunity_key: str
    asset: str
    fiat: str
    venue_buy: str
    venue_sell: str
    buy_price: float
    sell_price: float
    gross_spread_pct: float
    net_spread_pct: float
    risk_level: str
    shown_at_ms: int
    realised_at_ms: int | None = None
    realised_spread_pct: float | None = None
    status: str = STATUS_PENDING

    @property
    def is_resolved(self) -> bool:
        return self.status in ALL_RESOLVED_STATUSES

    @property
    def realised_delta_pct(self) -> float | None:
        """How much the realised spread differed from what we showed (% of shown)."""
        if self.realised_spread_pct is None or self.net_spread_pct <= 0:
            return None
        return ((self.realised_spread_pct - self.net_spread_pct) / self.net_spread_pct) * 100.0


@dataclass(frozen=True)
class BackcheckResult:
    """Output of :func:`compute_realised_spread`."""

    status: str
    realised_spread_pct: float | None
    matched_buy_price: float | None
    matched_sell_price: float | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThresholdAdjustmentRecommendation:
    """Result of :func:`recommend_threshold_adjustment`."""

    direction: str  # "raise", "lower", "hold"
    delta_pct: float
    reason: str
    sample_size: int
    decay_rate: float
    amplify_rate: float


# ─── Backcheck math ──────────────────────────────────────────────────────────


def make_opportunity_audit_record(
    opportunity: P2POpportunity,
    *,
    opportunity_key: str,
    shown_at_ms: int,
) -> OpportunityAuditRecord:
    """Snapshot an :class:`P2POpportunity` into a pending audit record."""
    return OpportunityAuditRecord(
        opportunity_key=opportunity_key,
        asset=opportunity.asset,
        fiat=opportunity.fiat,
        venue_buy=opportunity.buy_ad.venue,
        venue_sell=opportunity.sell_ad.venue,
        buy_price=opportunity.buy_ad.price,
        sell_price=opportunity.sell_ad.price,
        gross_spread_pct=opportunity.gross_spread_pct,
        net_spread_pct=opportunity.net_spread_pct,
        risk_level=opportunity.risk_level,
        shown_at_ms=shown_at_ms,
        status=STATUS_PENDING,
    )


def _pick_best_buy(
    candidates: Iterable[P2PAdvert],
    *,
    asset: str,
    fiat: str,
    target_price: float,
    tolerance_pct: float,
) -> P2PAdvert | None:
    """Pick the cheapest BUY-side ad on the same (asset, fiat) close to target."""
    eligible: list[P2PAdvert] = []
    asset_up = asset.upper()
    fiat_up = fiat.upper()
    tolerance_abs = target_price * (tolerance_pct / 100.0)
    for ad in candidates:
        if ad.trade_type != "BUY":
            continue
        if ad.asset.upper() != asset_up or ad.fiat.upper() != fiat_up:
            continue
        if ad.price <= 0:
            continue
        if abs(ad.price - target_price) > tolerance_abs:
            continue
        eligible.append(ad)
    if not eligible:
        return None
    # Cheapest is always best (BUY side: lower price = better).
    return min(eligible, key=lambda a: a.price)


def _pick_best_sell(
    candidates: Iterable[P2PAdvert],
    *,
    asset: str,
    fiat: str,
    target_price: float,
    tolerance_pct: float,
) -> P2PAdvert | None:
    """Pick the most expensive SELL-side ad close to target."""
    eligible: list[P2PAdvert] = []
    asset_up = asset.upper()
    fiat_up = fiat.upper()
    tolerance_abs = target_price * (tolerance_pct / 100.0)
    for ad in candidates:
        if ad.trade_type != "SELL":
            continue
        if ad.asset.upper() != asset_up or ad.fiat.upper() != fiat_up:
            continue
        if ad.price <= 0:
            continue
        if abs(ad.price - target_price) > tolerance_abs:
            continue
        eligible.append(ad)
    if not eligible:
        return None
    return max(eligible, key=lambda a: a.price)


def compute_realised_spread(
    record: OpportunityAuditRecord,
    *,
    current_buy_ads: Sequence[P2PAdvert],
    current_sell_ads: Sequence[P2PAdvert],
    price_tolerance_pct: float | None = None,
    decay_threshold_pct: float | None = None,
) -> BackcheckResult:
    """Re-check whether the spread we surfaced still exists.

    Strategy:
    1. Find a current BUY-side advert close to the original buy price
       (cheaper-or-equal preferred).
    2. Find a current SELL-side advert close to the original sell price
       (more-expensive-or-equal preferred).
    3. If both exist, recompute the gross spread (no fees/buffer applied
       — those are *static* approximations so removing them lets us isolate
       the price-side decay).
    4. Compare to ``record.net_spread_pct`` (note: shown spread *includes*
       the static buffer, so realised may look slightly higher) and decide:
       - ``vanished``: at least one side missing
       - ``decayed``: realised < shown - decay_pct
       - ``amplified``: realised > shown + decay_pct
       - ``confirmed``: within ±decay_pct band
    """
    tol = price_tolerance_pct if price_tolerance_pct is not None else get_price_tolerance_pct()
    decay = decay_threshold_pct if decay_threshold_pct is not None else get_decay_threshold_pct()

    notes: list[str] = []

    matched_buy = _pick_best_buy(
        current_buy_ads,
        asset=record.asset,
        fiat=record.fiat,
        target_price=record.buy_price,
        tolerance_pct=tol,
    )
    matched_sell = _pick_best_sell(
        current_sell_ads,
        asset=record.asset,
        fiat=record.fiat,
        target_price=record.sell_price,
        tolerance_pct=tol,
    )

    if matched_buy is None and matched_sell is None:
        notes.append("оба адверта пропали")
        return BackcheckResult(
            status=STATUS_VANISHED,
            realised_spread_pct=None,
            matched_buy_price=None,
            matched_sell_price=None,
            notes=tuple(notes),
        )
    if matched_buy is None:
        notes.append("buy-сторона исчезла")
        return BackcheckResult(
            status=STATUS_VANISHED,
            realised_spread_pct=None,
            matched_buy_price=None,
            matched_sell_price=matched_sell.price if matched_sell else None,
            notes=tuple(notes),
        )
    if matched_sell is None:
        notes.append("sell-сторона исчезла")
        return BackcheckResult(
            status=STATUS_VANISHED,
            realised_spread_pct=None,
            matched_buy_price=matched_buy.price,
            matched_sell_price=None,
            notes=tuple(notes),
        )

    if matched_sell.price <= matched_buy.price:
        notes.append("спред схлопнулся (sell ≤ buy)")
        return BackcheckResult(
            status=STATUS_DECAYED,
            realised_spread_pct=0.0,
            matched_buy_price=matched_buy.price,
            matched_sell_price=matched_sell.price,
            notes=tuple(notes),
        )

    realised_gross = ((matched_sell.price - matched_buy.price) / matched_buy.price) * 100.0
    # Subtract the same buffer that was used at the time of showing
    # (gross_spread_pct - net_spread_pct = buffer + fees).
    buffer_at_show = record.gross_spread_pct - record.net_spread_pct
    realised_net = realised_gross - max(buffer_at_show, 0.0)

    decay_band = (decay / 100.0) * record.net_spread_pct
    delta = realised_net - record.net_spread_pct

    if delta < -decay_band:
        status = STATUS_DECAYED
        notes.append(f"спред сузился: {realised_net:.2f}% vs показанных {record.net_spread_pct:.2f}%")
    elif delta > decay_band:
        status = STATUS_AMPLIFIED
        notes.append(f"спред расширился: {realised_net:.2f}% vs показанных {record.net_spread_pct:.2f}%")
    else:
        status = STATUS_CONFIRMED
        notes.append(f"спред совпал: {realised_net:.2f}% vs показанных {record.net_spread_pct:.2f}%")

    return BackcheckResult(
        status=status,
        realised_spread_pct=realised_net,
        matched_buy_price=matched_buy.price,
        matched_sell_price=matched_sell.price,
        notes=tuple(notes),
    )


# ─── Adaptive threshold logic ────────────────────────────────────────────────


def _classify_records(records: Sequence[OpportunityAuditRecord]) -> dict[str, int]:
    """Count records by terminal status."""
    counts = {s: 0 for s in (STATUS_CONFIRMED, STATUS_DECAYED, STATUS_AMPLIFIED, STATUS_VANISHED, STATUS_EXPIRED, STATUS_PENDING)}
    for r in records:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def recommend_threshold_adjustment(
    records: Sequence[OpportunityAuditRecord],
    *,
    delta_pct: float | None = None,
    min_samples: int | None = None,
) -> ThresholdAdjustmentRecommendation:
    """Decide whether to nudge ``P2P_ARBITRAGE_MIN_SPREAD_PCT``.

    Heuristic:
    - Look at resolved records only (non-pending).
    - If ``decayed + vanished`` rate > 50 % → raise threshold (we're surfacing
      noise that doesn't materialise).
    - If ``amplified`` rate > 50 % → lower threshold (we're missing real
      opportunities that grow stronger).
    - Otherwise hold.

    Returns a recommendation with sample sizes; caller decides whether to
    apply or just log.
    """
    nudge = delta_pct if delta_pct is not None else get_threshold_adjust_pct()
    min_n = min_samples if min_samples is not None else get_threshold_adjust_min_samples()

    resolved = [r for r in records if r.is_resolved]
    n = len(resolved)
    if n < min_n:
        return ThresholdAdjustmentRecommendation(
            direction="hold",
            delta_pct=0.0,
            reason=f"мало данных ({n}/{min_n})",
            sample_size=n,
            decay_rate=0.0,
            amplify_rate=0.0,
        )

    counts = _classify_records(resolved)
    decay_n = counts.get(STATUS_DECAYED, 0) + counts.get(STATUS_VANISHED, 0)
    amplify_n = counts.get(STATUS_AMPLIFIED, 0)
    decay_rate = decay_n / n
    amplify_rate = amplify_n / n

    if decay_rate > 0.5:
        return ThresholdAdjustmentRecommendation(
            direction="raise",
            delta_pct=nudge,
            reason=f"{decay_n}/{n} ({decay_rate * 100:.0f}%) opportunities decayed/vanished — поднять порог",
            sample_size=n,
            decay_rate=decay_rate,
            amplify_rate=amplify_rate,
        )
    if amplify_rate > 0.5:
        return ThresholdAdjustmentRecommendation(
            direction="lower",
            delta_pct=nudge,
            reason=f"{amplify_n}/{n} ({amplify_rate * 100:.0f}%) opportunities amplified — снизить порог",
            sample_size=n,
            decay_rate=decay_rate,
            amplify_rate=amplify_rate,
        )

    return ThresholdAdjustmentRecommendation(
        direction="hold",
        delta_pct=0.0,
        reason=f"баланс: decay={decay_rate * 100:.0f}%, amplify={amplify_rate * 100:.0f}% — держать",
        sample_size=n,
        decay_rate=decay_rate,
        amplify_rate=amplify_rate,
    )


# ─── Format helpers ──────────────────────────────────────────────────────────


def _escape_md(text: str) -> str:
    if not text:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def format_audit_summary(
    records: Sequence[OpportunityAuditRecord],
    *,
    recommendation: ThresholdAdjustmentRecommendation | None = None,
) -> str:
    """Markdown summary for `/p2paudit`."""
    if not records:
        return "*📊 P2P self-audit*\n\nНет записей в журнале — фича выключена или нет недавних сигналов."

    counts = _classify_records(records)
    n = len(records)
    resolved_n = sum(counts.get(s, 0) for s in ALL_RESOLVED_STATUSES)
    pending_n = counts.get(STATUS_PENDING, 0)

    lines = [
        "*📊 P2P self-audit*",
        "",
        f"Всего записей: *{n}* (резолвлено {resolved_n}, ожидает {pending_n})",
        "",
    ]
    if resolved_n:
        lines.append(
            "└ confirmed *{conf}* · amplified *{amp}* · decayed *{dec}* · vanished *{van}* · expired *{exp}*".format(
                conf=counts.get(STATUS_CONFIRMED, 0),
                amp=counts.get(STATUS_AMPLIFIED, 0),
                dec=counts.get(STATUS_DECAYED, 0),
                van=counts.get(STATUS_VANISHED, 0),
                exp=counts.get(STATUS_EXPIRED, 0),
            )
        )
        lines.append("")

    if recommendation is not None:
        emoji = {"raise": "📈", "lower": "📉", "hold": "🟰"}.get(recommendation.direction, "•")
        lines.append(f"{emoji} *Рекомендация:* {recommendation.direction.upper()}")
        if recommendation.delta_pct:
            sign = "+" if recommendation.direction == "raise" else "-"
            lines.append(f"  └ Δ `P2P_ARBITRAGE_MIN_SPREAD_PCT {sign}{recommendation.delta_pct:.2f}%`")
        lines.append(f"  └ {_escape_md(recommendation.reason)}")
        lines.append("")

    # Last 5 resolved records, newest first
    recent_resolved = [r for r in records if r.is_resolved][:5]
    if recent_resolved:
        lines.append("*Последние резолвы:*")
        for r in recent_resolved:
            status_emoji = {
                STATUS_CONFIRMED: "✅",
                STATUS_AMPLIFIED: "📈",
                STATUS_DECAYED: "📉",
                STATUS_VANISHED: "🚫",
                STATUS_EXPIRED: "⌛",
            }.get(r.status, "•")
            realised = (
                f"{r.realised_spread_pct:.2f}%"
                if r.realised_spread_pct is not None
                else "n/a"
            )
            lines.append(
                f"{status_emoji} `{r.asset}/{r.fiat}` shown *{r.net_spread_pct:.2f}%* → realised *{realised}*  ({r.risk_level})"
            )

    return "\n".join(lines)


__all__ = [
    "ALL_RESOLVED_STATUSES",
    "BackcheckResult",
    "DEFAULT_AUDIT_RETENTION_DAYS",
    "DEFAULT_BACKCHECK_DELAY_MIN",
    "DEFAULT_BACKCHECK_INTERVAL_MIN",
    "DEFAULT_DECAY_THRESHOLD_PCT",
    "DEFAULT_PRICE_TOLERANCE_PCT",
    "DEFAULT_THRESHOLD_ADJUST_MIN_SAMPLES",
    "DEFAULT_THRESHOLD_ADJUST_PCT",
    "OpportunityAuditRecord",
    "STATUS_AMPLIFIED",
    "STATUS_CONFIRMED",
    "STATUS_DECAYED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STATUS_VANISHED",
    "ThresholdAdjustmentRecommendation",
    "compute_realised_spread",
    "feature_enabled",
    "format_audit_summary",
    "get_backcheck_delay_min",
    "get_backcheck_interval_min",
    "get_decay_threshold_pct",
    "get_price_tolerance_pct",
    "get_retention_days",
    "get_threshold_adjust_min_samples",
    "get_threshold_adjust_pct",
    "make_opportunity_audit_record",
    "recommend_threshold_adjustment",
]
