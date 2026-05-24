"""Advisor layer — turns raw signals + BTC outlook + risk profile into a concrete
actionable plan ("buy here, stop here, exit conditions, size").

Pure logic — no I/O, no Telegram. The handler in ``refactor/handlers/advisor_handler.py``
collects all inputs (price/ATR via ``web_search.fetch_realtime_prices``, BTC
outlook via ``btc_handler.fetch_btc_outlook_inputs``, user profile via
``profile_handler``) and feeds them in.

Design notes:
- "Биток вниз — всё идёт вниз" → BTC outlook acts as a confidence dampener /
  veto for altcoin trades when the BTC lean is strong and contradictory.
- Position sizing follows the same risk-per-trade approach as
  ``core/dynamic_risk.py`` (we deliberately do NOT import that module — the
  user explicitly forbids touching trading logic; we duplicate just the
  simple position_size = (capital × risk_pct) / stop_distance_pct formula).
- All numbers are deterministic given inputs — fully unit-testable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


ACTION_BUY = "BUY"
ACTION_SELL = "SELL"
ACTION_HOLD = "HOLD"
ACTION_WAIT = "WAIT"

RISK_CONSERVATIVE = "conservative"
RISK_MODERATE = "moderate"
RISK_AGGRESSIVE = "aggressive"

HORIZON_SHORT = "short_term"
HORIZON_MEDIUM = "medium_term"
HORIZON_LONG = "long_term"


# Per-profile knobs. Conservative: tight risk, wide stops (less stop-out).
# Aggressive: bigger risk, tighter stops (more stop-outs but bigger wins).
PROFILE_RISK_PCT: dict[str, float] = {
    RISK_CONSERVATIVE: 0.5,
    RISK_MODERATE: 1.0,
    RISK_AGGRESSIVE: 2.0,
}
PROFILE_ATR_MULT: dict[str, float] = {
    RISK_CONSERVATIVE: 2.5,
    RISK_MODERATE: 2.0,
    RISK_AGGRESSIVE: 1.5,
}
HORIZON_HUMAN: dict[str, str] = {
    HORIZON_SHORT: "1-3 дня",
    HORIZON_MEDIUM: "1-2 недели",
    HORIZON_LONG: "1-3 месяца",
}

# Default risk-per-trade if profile unknown.
DEFAULT_RISK_PCT = 1.0
DEFAULT_ATR_MULT = 2.0

# BTC confidence threshold above which the lean acts as veto for altcoin
# trades that go against it. Below this, BTC outlook is just informational.
BTC_VETO_CONFIDENCE_MIN = 65

# Minimum confidence for the plan to be actionable. Below this we emit WAIT.
MIN_ACTIONABLE_CONFIDENCE = 35


@dataclass(frozen=True)
class AdvisorInputs:
    """Everything the advisor needs to produce a plan. All fields optional —
    advisor degrades gracefully on missing inputs."""

    asset: str = "BTC"
    entry_price: float | None = None
    atr_14d_usd: float | None = None
    atr_14d_pct: float | None = None
    rsi_14d: float | None = None
    trend: str | None = None  # "UPTREND" / "DOWNTREND" / "SIDEWAYS"
    quant_verdict: str | None = None  # "LONG" / "SHORT" / "NEUTRAL"
    quant_confidence: float | None = None  # 0..1
    btc_lean: str | None = None  # "BULL" / "BEAR" / "NEUTRAL"
    btc_confidence_pct: int | None = None  # 0..100
    risk_profile: str = RISK_MODERATE
    time_horizon: str = HORIZON_MEDIUM
    capital_usd: float | None = None  # if None, position_usd will be None


@dataclass(frozen=True)
class TPLevel:
    """One take-profit rung. ``close_pct`` = % of position to close at this level."""

    price: float
    r_multiple: float  # 1R, 2R, 3R
    close_pct: int  # 30 / 40 / 30


@dataclass(frozen=True)
class AdvisorPlan:
    """Concrete actionable plan."""

    asset: str
    action: str  # BUY/SELL/HOLD/WAIT
    confidence_pct: int

    entry_price: float | None = None
    entry_zone_low: float | None = None
    entry_zone_high: float | None = None
    stop_price: float | None = None
    stop_distance_pct: float | None = None
    risk_reward: float | None = None

    tp_levels: tuple[TPLevel, ...] = ()
    position_usd: float | None = None
    position_pct_of_capital: float | None = None

    horizon_human: str = ""
    invalidation: str = ""
    rationale: tuple[str, ...] = field(default_factory=tuple)
    btc_overlay_note: str = ""
    risk_profile: str = RISK_MODERATE


def feature_enabled() -> bool:
    raw = os.getenv("FEATURE_ADVISOR", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _resolve_risk_pct(profile: str) -> float:
    return PROFILE_RISK_PCT.get(profile, DEFAULT_RISK_PCT)


def _resolve_atr_mult(profile: str) -> float:
    return PROFILE_ATR_MULT.get(profile, DEFAULT_ATR_MULT)


def _direction_from_quant(verdict: str | None) -> int:
    if not verdict:
        return 0
    v = verdict.upper()
    if v in {"LONG", "BUY"}:
        return 1
    if v in {"SHORT", "SELL"}:
        return -1
    return 0


def _direction_from_trend(trend: str | None) -> int:
    if not trend:
        return 0
    t = trend.upper()
    if t == "UPTREND":
        return 1
    if t == "DOWNTREND":
        return -1
    return 0


def _direction_from_btc(lean: str | None) -> int:
    if not lean:
        return 0
    L = lean.upper()
    if L == "BULL":
        return 1
    if L == "BEAR":
        return -1
    return 0


def _safe_pct(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator * 100.0


def _build_rationale(
    inputs: AdvisorInputs,
    asset_dir: int,
    btc_dir: int,
    veto_triggered: bool,
) -> tuple[tuple[str, ...], str]:
    """Construct rationale bullets + BTC overlay note."""
    bullets: list[str] = []

    if inputs.quant_verdict and inputs.quant_confidence is not None:
        bullets.append(
            f"Quant ensemble: {inputs.quant_verdict.upper()} "
            f"(сила {round(inputs.quant_confidence * 100)}%)"
        )

    if inputs.trend:
        bullets.append(f"Дневной тренд: {inputs.trend}")

    if inputs.rsi_14d is not None:
        zone = "перегрев" if inputs.rsi_14d >= 70 else "перепродан" if inputs.rsi_14d <= 30 else "норма"
        bullets.append(f"RSI(14d)={round(inputs.rsi_14d, 1)} — {zone}")

    if inputs.atr_14d_pct is not None:
        vol_zone = (
            "высокая воля"
            if inputs.atr_14d_pct >= 4
            else "тихо"
            if inputs.atr_14d_pct <= 1.5
            else "норма"
        )
        bullets.append(f"ATR(14d)={inputs.atr_14d_pct:.1f}% — {vol_zone}")

    overlay_note = ""
    is_btc = inputs.asset.upper() == "BTC"
    if not is_btc and inputs.btc_lean and inputs.btc_confidence_pct is not None:
        if veto_triggered:
            overlay_note = (
                f"⚠️ BTC {inputs.btc_lean} {inputs.btc_confidence_pct}% против сетапа — "
                f"вето на сделку (alt'ы не растут когда BTC льёт)."
            )
        elif asset_dir != 0 and btc_dir != 0 and asset_dir == btc_dir:
            overlay_note = (
                f"✅ BTC {inputs.btc_lean} {inputs.btc_confidence_pct}% совпадает — "
                f"усиление сигнала."
            )
        elif inputs.btc_lean.upper() == "NEUTRAL":
            overlay_note = (
                f"BTC NEUTRAL {inputs.btc_confidence_pct}% — нейтрально, без усиления."
            )
        else:
            overlay_note = (
                f"BTC {inputs.btc_lean} {inputs.btc_confidence_pct}% (слабый сигнал, без veto)."
            )

    return tuple(bullets), overlay_note


def recommend(inputs: AdvisorInputs) -> AdvisorPlan:
    """Produce a concrete plan from raw inputs.

    Algorithm:
    1. Pick asset-level direction from quant verdict + trend (vote-weighted).
    2. Apply BTC overlay (only for non-BTC assets, only above veto threshold).
    3. If no actionable direction → WAIT plan with rationale.
    4. Compute stop = entry ± ATR×profile_mult.
    5. Compute 3-level TP at 1R/2R/3R.
    6. Compute position_usd from capital × risk_pct / stop_distance_pct.
    7. Compose invalidation / horizon / rationale.
    """
    # ── Step 1: asset-level direction (quant primary, trend secondary).
    quant_dir = _direction_from_quant(inputs.quant_verdict)
    trend_dir = _direction_from_trend(inputs.trend)
    quant_strength = float(inputs.quant_confidence or 0.0)

    # Vote: quant has weight 2 when confident, trend weight 1.
    quant_weight = max(0.5, quant_strength * 2.0)
    score = quant_dir * quant_weight + trend_dir * 1.0
    if score > 0:
        asset_dir = 1
    elif score < 0:
        asset_dir = -1
    else:
        asset_dir = 0

    # ── Step 2: BTC overlay (veto/boost) — only for non-BTC assets.
    btc_dir = _direction_from_btc(inputs.btc_lean)
    btc_conf = int(inputs.btc_confidence_pct or 0)
    is_btc = inputs.asset.upper() == "BTC"
    veto_triggered = False
    if not is_btc and asset_dir != 0 and btc_dir != 0 and btc_dir != asset_dir:
        if btc_conf >= BTC_VETO_CONFIDENCE_MIN:
            veto_triggered = True

    # ── Step 3: compute raw confidence (0..100) from quant + trend agreement.
    base_conf = quant_strength * 70.0  # quant alone caps at 70%
    if quant_dir != 0 and trend_dir == quant_dir:
        base_conf += 15  # trend confirms
    elif quant_dir != 0 and trend_dir == -quant_dir:
        base_conf -= 15  # trend contradicts
    if not is_btc and btc_dir != 0 and asset_dir != 0:
        if btc_dir == asset_dir:
            # Cap BTC boost so we don't overconfidence-bull an alt purely on BTC.
            base_conf += min(15.0, btc_conf * 0.15)
        else:
            base_conf -= min(20.0, btc_conf * 0.20)
    confidence_pct = int(max(0, min(100, round(base_conf))))

    rationale, overlay_note = _build_rationale(inputs, asset_dir, btc_dir, veto_triggered)

    # ── Step 4: decide action.
    if veto_triggered or asset_dir == 0 or confidence_pct < MIN_ACTIONABLE_CONFIDENCE:
        action = ACTION_WAIT
        return AdvisorPlan(
            asset=inputs.asset,
            action=action,
            confidence_pct=confidence_pct,
            entry_price=inputs.entry_price,
            horizon_human=HORIZON_HUMAN.get(inputs.time_horizon, ""),
            invalidation=(
                "Сетап невалиден — нет согласованного сигнала или BTC veto."
                if veto_triggered
                else "Нет чёткого направления — ждём пока тренд/quant согласуются."
            ),
            rationale=rationale,
            btc_overlay_note=overlay_note,
            risk_profile=inputs.risk_profile,
        )

    action = ACTION_BUY if asset_dir > 0 else ACTION_SELL

    # ── Step 5: stop + TPs. Require entry_price + ATR for actionable levels.
    entry = inputs.entry_price
    atr_usd = inputs.atr_14d_usd
    if entry is None or entry <= 0:
        return AdvisorPlan(
            asset=inputs.asset,
            action=ACTION_WAIT,
            confidence_pct=confidence_pct,
            horizon_human=HORIZON_HUMAN.get(inputs.time_horizon, ""),
            invalidation="Нет цены — невозможно построить уровни.",
            rationale=rationale,
            btc_overlay_note=overlay_note,
            risk_profile=inputs.risk_profile,
        )

    atr_mult = _resolve_atr_mult(inputs.risk_profile)
    if atr_usd is None or atr_usd <= 0:
        # Fallback: 2% stop distance.
        stop_distance_usd = entry * 0.02
    else:
        stop_distance_usd = atr_usd * atr_mult

    if action == ACTION_BUY:
        stop_price = entry - stop_distance_usd
        tp1 = entry + stop_distance_usd * 1.0
        tp2 = entry + stop_distance_usd * 2.0
        tp3 = entry + stop_distance_usd * 3.0
        entry_zone_low = entry * 0.995
        entry_zone_high = entry * 1.003  # slight bias above for limit fills
        invalidation = (
            f"Закрытие 4h-свечи ниже ${stop_price:,.2f} — выход из сделки полностью."
        )
    else:  # SELL
        stop_price = entry + stop_distance_usd
        tp1 = entry - stop_distance_usd * 1.0
        tp2 = entry - stop_distance_usd * 2.0
        tp3 = entry - stop_distance_usd * 3.0
        entry_zone_low = entry * 0.997
        entry_zone_high = entry * 1.005
        invalidation = (
            f"Закрытие 4h-свечи выше ${stop_price:,.2f} — выход из сделки полностью."
        )

    stop_distance_pct = _safe_pct(stop_distance_usd, entry)
    risk_reward = 2.0  # weighted: (0.3*1 + 0.4*2 + 0.3*3) = 2.0

    tp_levels = (
        TPLevel(price=tp1, r_multiple=1.0, close_pct=30),
        TPLevel(price=tp2, r_multiple=2.0, close_pct=40),
        TPLevel(price=tp3, r_multiple=3.0, close_pct=30),
    )

    # ── Step 6: position sizing.
    position_usd: float | None = None
    position_pct: float | None = None
    if inputs.capital_usd is not None and inputs.capital_usd > 0 and stop_distance_pct > 0:
        base_risk_pct = _resolve_risk_pct(inputs.risk_profile)
        # Confidence multiplier — at 35% conf use 0.4× size, at 100% use 1.0×.
        conf_mult = max(0.4, min(1.0, confidence_pct / 100.0))
        effective_risk_pct = base_risk_pct * conf_mult
        risk_dollars = inputs.capital_usd * (effective_risk_pct / 100.0)
        position_usd = risk_dollars / (stop_distance_pct / 100.0)
        # Clamp: no single position > 25% of capital regardless of math.
        max_position = inputs.capital_usd * 0.25
        if position_usd > max_position:
            position_usd = max_position
        position_pct = _safe_pct(position_usd, inputs.capital_usd)

    return AdvisorPlan(
        asset=inputs.asset,
        action=action,
        confidence_pct=confidence_pct,
        entry_price=entry,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        stop_price=stop_price,
        stop_distance_pct=stop_distance_pct,
        risk_reward=risk_reward,
        tp_levels=tp_levels,
        position_usd=position_usd,
        position_pct_of_capital=position_pct,
        horizon_human=HORIZON_HUMAN.get(inputs.time_horizon, ""),
        invalidation=invalidation,
        rationale=rationale,
        btc_overlay_note=overlay_note,
        risk_profile=inputs.risk_profile,
    )


def _money(x: float | None) -> str:
    if x is None:
        return "—"
    if x >= 1000:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def format_advisor_markdown(plan: AdvisorPlan) -> str:
    """Render an ``AdvisorPlan`` as Telegram-friendly Markdown."""
    action_emoji = {
        ACTION_BUY: "🟢",
        ACTION_SELL: "🔴",
        ACTION_HOLD: "🟡",
        ACTION_WAIT: "⚪",
    }.get(plan.action, "•")

    lines: list[str] = []
    lines.append(f"{action_emoji} *{plan.asset} — {plan.action}* ({plan.confidence_pct}%)")
    lines.append(f"_Профиль: {plan.risk_profile}, горизонт: {plan.horizon_human or '—'}_")
    lines.append("")

    if plan.action in (ACTION_BUY, ACTION_SELL) and plan.entry_price is not None:
        zone = ""
        if plan.entry_zone_low is not None and plan.entry_zone_high is not None:
            zone = f" (зона {_money(plan.entry_zone_low)} – {_money(plan.entry_zone_high)})"
        lines.append(f"*Вход:* {_money(plan.entry_price)}{zone}")
        if plan.stop_price is not None and plan.stop_distance_pct is not None:
            lines.append(
                f"*Стоп:* {_money(plan.stop_price)} "
                f"(дистанция {plan.stop_distance_pct:.2f}%)"
            )
        if plan.tp_levels:
            lines.append("*Тейк (split):*")
            for tp in plan.tp_levels:
                lines.append(
                    f"  • TP{int(tp.r_multiple)}: {_money(tp.price)} — "
                    f"закрыть {tp.close_pct}% позиции"
                )
        if plan.risk_reward is not None:
            lines.append(f"*Средний R/R:* 1:{plan.risk_reward:.1f}")
        if plan.position_usd is not None and plan.position_pct_of_capital is not None:
            lines.append(
                f"*Размер:* {_money(plan.position_usd)} "
                f"({plan.position_pct_of_capital:.1f}% капитала)"
            )
        lines.append("")

    if plan.rationale:
        lines.append("*Почему:*")
        for r in plan.rationale:
            lines.append(f"  • {r}")
        lines.append("")

    if plan.btc_overlay_note:
        lines.append(plan.btc_overlay_note)
        lines.append("")

    if plan.invalidation:
        lines.append(f"*Инвалидация:* {plan.invalidation}")

    return "\n".join(lines).rstrip()


__all__ = [
    "ACTION_BUY",
    "ACTION_SELL",
    "ACTION_HOLD",
    "ACTION_WAIT",
    "RISK_CONSERVATIVE",
    "RISK_MODERATE",
    "RISK_AGGRESSIVE",
    "HORIZON_SHORT",
    "HORIZON_MEDIUM",
    "HORIZON_LONG",
    "BTC_VETO_CONFIDENCE_MIN",
    "MIN_ACTIONABLE_CONFIDENCE",
    "AdvisorInputs",
    "AdvisorPlan",
    "TPLevel",
    "feature_enabled",
    "recommend",
    "format_advisor_markdown",
]
