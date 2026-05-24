"""Comprehensive BTC outlook / next-move forecast.

Aggregates many independent signals into a single bull/bear/neutral lean with
explicit confidence and a per-signal breakdown. Pure-logic module — no I/O.
Network adapters live in ``refactor/handlers/btc_handler.py`` and feed the
inputs here.

Why this exists
---------------

User thesis: "Биток вниз — всё идёт вниз". BTC dominance bleeds into alt-
performance, so a single coherent BTC outlook is more useful than per-asset
snippets scattered across ``/daily``. This module combines:

* Spot-price action (24h % move).
* Perp funding rate (level + delta) — proxy for crowding / contrarian edge.
* Open Interest delta — distinguishes genuine breakouts from squeezes.
* Top-trader long/short ratio (Bybit / Binance retail-vs-pro proxy).
* BTC dominance + 7-day drift.
* US BTC spot-ETF basket flows (5-day avg change + active outflow signal).
* Stablecoin supply delta on Ethereum (USDT+USDC mint/redeem proxy).
* Options 25-delta skew (put-premium = downside protection demand).
* Fear & Greed Index — contrarian at extremes.
* Quant ensemble verdict (BB+Donchian+RSI) from ``quant_filter.quant_verdict``.
* Markov regime label (BULL / BEAR / RANGE).

Each signal contributes a ``direction ∈ {-1, 0, +1}`` and a ``weight ∈ [0, 1]``
to a net score. Lean is decided from the net score; confidence is the absolute
net score normalised by the sum of contributing weights.

Conventions:

* ``funding_rate_8h_pct`` is in percent (Binance returns ``0.0001`` for 0.01%
  — callers must multiply by 100 before passing).
* Percentages are in ``pct`` units (5.0 = 5%), not fractions.
* Any input may be ``None``; missing signals are skipped, not penalised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

LEAN_BULL = "BULL"
LEAN_BEAR = "BEAR"
LEAN_NEUTRAL = "NEUTRAL"

# Net-score thresholds for lean classification. Below these the verdict is
# NEUTRAL — most signals must agree to flip the bias.
NEUTRAL_BAND = 0.18


@dataclass(frozen=True)
class BTCOutlookInputs:
    """All numeric inputs the verdict engine needs. Every field is optional."""

    btc_price_usd: float | None = None
    """Spot reference price — purely for display, not scored."""

    price_change_24h_pct: float | None = None
    """24h % change in BTC spot price (e.g. -3.2 means -3.2%)."""

    funding_rate_8h_pct: float | None = None
    """Most recent Binance/Bybit funding rate in percent (0.01 = 0.01%)."""

    funding_change_24h_pct: float | None = None
    """Delta vs 24h average funding — not currently scored but kept for ctx."""

    oi_change_24h_pct: float | None = None
    """24h % change in BTC perp open interest (e.g. +4.5 = +4.5%)."""

    top_trader_ls_ratio: float | None = None
    """Bybit/Binance top-trader long/short ratio (>1 = net long)."""

    btc_dominance_pct: float | None = None
    """BTC dominance (% of total crypto market cap), 0..100."""

    dominance_change_7d_pct: float | None = None
    """7-day change in dominance (pp). +1.0 = dominance grew by 1 percentage point."""

    etf_basket_change_5d_avg_pct: float | None = None
    """Avg 5-day % change of the BTC spot-ETF basket (proxy for net flow)."""

    etf_outflow_signal: str | None = None
    """``"WARN"`` / ``"CRIT"`` / ``None`` from ``btc_etf_flows.detect_outflow_signal``."""

    stablecoin_supply_delta_24h_pct: float | None = None
    """24h % change in combined USDT+USDC supply (mint = bull, redeem = bear)."""

    options_skew_25d: float | None = None
    """25-delta put-call skew (positive → put premium / downside hedging)."""

    fear_greed_index: int | None = None
    """Alternative.me Fear & Greed Index, 0..100."""

    quant_verdict_direction: str | None = None
    """``"LONG"`` / ``"SHORT"`` / ``"NEUTRAL"`` from ``quant_filter.quant_verdict``."""

    quant_verdict_strength: float | None = None
    """Strength of quant verdict, 0..1."""

    regime: str | None = None
    """Markov regime label: ``"BULL"`` / ``"BEAR"`` / ``"RANGE"``."""

    asof_ts: int = 0


@dataclass(frozen=True)
class BTCSignalContribution:
    name: str
    label: str
    raw_value: str
    direction: int  # +1 bull / -1 bear / 0 neutral
    weight: float
    explanation: str = ""


@dataclass(frozen=True)
class BTCOutlookVerdict:
    lean: str
    confidence_pct: int
    net_score: float
    bull_score: float
    bear_score: float
    contributions: tuple[BTCSignalContribution, ...]
    timeframe_hours: int = 48
    summary: str = ""
    fallback_used: bool = False
    inputs_seen: int = 0
    inputs_total: int = field(default=12)


def _scaled_weight(value: float, scale: float, cap: float = 1.0) -> float:
    if scale <= 0:
        return cap
    return max(0.0, min(cap, abs(value) / scale))


def _direction_from_signed(value: float, dead_band: float = 0.0) -> int:
    if value > dead_band:
        return 1
    if value < -dead_band:
        return -1
    return 0


def _score_price_change(value: float | None) -> BTCSignalContribution | None:
    if value is None or not math.isfinite(value):
        return None
    direction = _direction_from_signed(value, dead_band=0.4)
    weight = _scaled_weight(value, scale=4.0, cap=0.8)
    if direction == 0:
        explanation = "цена около плоска — слабый сигнал"
    elif direction > 0:
        explanation = "цена движется вверх — momentum bull"
    else:
        explanation = "цена движется вниз — momentum bear"
    return BTCSignalContribution(
        name="price_24h",
        label="Цена 24ч",
        raw_value=f"{value:+.2f}%",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_funding(value: float | None) -> BTCSignalContribution | None:
    if value is None or not math.isfinite(value):
        return None
    abs_val = abs(value)
    if abs_val >= 0.05:
        # Экстремальный funding — обычно контр-сигнал (squeeze risk).
        direction = -1 if value > 0 else 1
        weight = _scaled_weight(abs_val, scale=0.12, cap=0.7)
        explanation = (
            "толпа лонгует, перегрев — risk-off"
            if value > 0
            else "толпа в шорте, потенциальный squeeze вверх"
        )
    elif abs_val >= 0.02:
        # Умеренный bias — сонаправленный сигнал (рынок верит в trend).
        direction = 1 if value > 0 else -1
        weight = _scaled_weight(abs_val, scale=0.06, cap=0.5)
        explanation = (
            "умеренный long-bias, тренд жив"
            if value > 0
            else "умеренный short-bias, давление вниз"
        )
    else:
        direction = 0
        weight = 0.25
        explanation = "funding почти ноль — рынок остыл, ловушка для брейкаута"
    return BTCSignalContribution(
        name="funding",
        label="Funding 8h",
        raw_value=f"{value:+.4f}%",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_oi_price_combo(
    oi: float | None,
    price: float | None,
) -> BTCSignalContribution | None:
    if oi is None or not math.isfinite(oi):
        return None
    if price is None or not math.isfinite(price):
        # OI без направления цены: рост = плечо в системе (риск), падение = деливерейдж.
        direction = -1 if oi > 1.0 else (1 if oi < -1.0 else 0)
        weight = _scaled_weight(oi, scale=8.0, cap=0.4)
        explanation = (
            "плечо растёт без направления цены — нестабильность"
            if oi > 0
            else "деливерейдж — система остывает"
        )
        return BTCSignalContribution(
            name="oi_solo",
            label="OI 24ч",
            raw_value=f"{oi:+.2f}%",
            direction=direction,
            weight=weight,
            explanation=explanation,
        )

    if oi > 0.5 and price > 0.4:
        direction = 1
        weight = 0.75
        explanation = "OI↑ price↑ — настоящий брейкаут, новые деньги долго"
    elif oi > 0.5 and price < -0.4:
        direction = -1
        weight = 0.70
        explanation = "OI↑ price↓ — short build-up, давление вниз сильное"
    elif oi < -0.5 and price > 0.4:
        direction = 1
        weight = 0.45
        explanation = "OI↓ price↑ — short squeeze, движение хрупкое"
    elif oi < -0.5 and price < -0.4:
        direction = -1
        weight = 0.55
        explanation = "OI↓ price↓ — капитуляция, deleverage"
    else:
        direction = 0
        weight = 0.25
        explanation = "OI и цена незначительно меняются"
    return BTCSignalContribution(
        name="oi_price",
        label="OI×Цена",
        raw_value=f"OI {oi:+.2f}% · P {price:+.2f}%",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_ls_ratio(value: float | None) -> BTCSignalContribution | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    # Top-trader extremes — обычно контр-сигнал.
    if value >= 2.5:
        direction = -1
        weight = 0.5
        explanation = "топ-трейдеры экстремально long — risk of crowded longs"
    elif value >= 1.6:
        direction = 1
        weight = 0.3
        explanation = "топ-трейдеры умеренно long — sentiment ok"
    elif value <= 0.4:
        direction = 1
        weight = 0.5
        explanation = "топ-трейдеры экстремально short — squeeze setup"
    elif value <= 0.65:
        direction = -1
        weight = 0.3
        explanation = "топ-трейдеры умеренно short — давление вниз"
    else:
        direction = 0
        weight = 0.15
        explanation = "L/S около 1 — sentiment сбалансирован"
    return BTCSignalContribution(
        name="ls_ratio",
        label="Top L/S",
        raw_value=f"{value:.2f}",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_dominance(
    current_pct: float | None,
    change_7d_pp: float | None,
) -> BTCSignalContribution | None:
    if change_7d_pp is None or not math.isfinite(change_7d_pp):
        if current_pct is None or not math.isfinite(current_pct):
            return None
        # Без 7д-дрейфа — только статика, слабый сигнал.
        return BTCSignalContribution(
            name="btc_dominance",
            label="BTC.D",
            raw_value=f"{current_pct:.2f}%",
            direction=0,
            weight=0.15,
            explanation="dominance без дрейфа — нейтрально",
        )
    direction = _direction_from_signed(change_7d_pp, dead_band=0.3)
    weight = _scaled_weight(change_7d_pp, scale=2.0, cap=0.6)
    explanation = (
        "dominance растёт — деньги стекаются в BTC (alt-выкос, но BTC устойчив)"
        if change_7d_pp > 0
        else (
            "dominance падает — risk-on в альты, BTC под давлением"
            if change_7d_pp < 0
            else "dominance плоский"
        )
    )
    raw = f"{current_pct:.2f}%" if current_pct is not None else "?"
    return BTCSignalContribution(
        name="btc_dominance",
        label="BTC.D 7д",
        raw_value=f"{raw} ({change_7d_pp:+.2f}пп/7д)",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_etf_basket(
    avg_change_5d: float | None,
    signal: str | None,
) -> BTCSignalContribution | None:
    if avg_change_5d is None and not signal:
        return None
    if signal:
        sig = signal.upper()
        if sig == "CRIT":
            return BTCSignalContribution(
                name="etf",
                label="BTC ETF",
                raw_value="CRIT outflow",
                direction=-1,
                weight=0.85,
                explanation="ETF basket: критический отток — инст. деньги бегут",
            )
        if sig == "WARN":
            return BTCSignalContribution(
                name="etf",
                label="BTC ETF",
                raw_value="WARN outflow",
                direction=-1,
                weight=0.55,
                explanation="ETF basket: предупредительный отток — давление вниз",
            )
    if avg_change_5d is None or not math.isfinite(avg_change_5d):
        return None
    direction = _direction_from_signed(avg_change_5d, dead_band=0.15)
    weight = _scaled_weight(avg_change_5d, scale=1.5, cap=0.6)
    explanation = (
        "ETF basket в плюсе — инст. деньги покупают"
        if avg_change_5d > 0
        else (
            "ETF basket в минусе — инст. деньги продают"
            if avg_change_5d < 0
            else "ETF basket плоский"
        )
    )
    return BTCSignalContribution(
        name="etf",
        label="ETF basket 5д",
        raw_value=f"{avg_change_5d:+.2f}%/день",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_stable_supply(value: float | None) -> BTCSignalContribution | None:
    if value is None or not math.isfinite(value):
        return None
    direction = _direction_from_signed(value, dead_band=0.05)
    weight = _scaled_weight(value, scale=0.6, cap=0.55)
    explanation = (
        "USDT+USDC mint — новые деньги в системе, бычий сетап"
        if value > 0
        else (
            "USDT+USDC redeem — деньги выходят, медвежий сетап"
            if value < 0
            else "supply стейблов плоский"
        )
    )
    return BTCSignalContribution(
        name="stable_supply",
        label="USDT+USDC 24ч",
        raw_value=f"{value:+.2f}%",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_options_skew(value: float | None) -> BTCSignalContribution | None:
    if value is None or not math.isfinite(value):
        return None
    # 25-delta put-call skew. Positive = puts стоят дороже calls (страх).
    direction = _direction_from_signed(value, dead_band=2.0)
    if direction != 0:
        direction = -direction  # инвертируем: put premium = bear
    weight = _scaled_weight(value, scale=15.0, cap=0.45)
    explanation = (
        "put premium — хедж от падения, downside страх"
        if value > 0
        else (
            "call premium — speculative upside выкуплен"
            if value < 0
            else "skew плоский"
        )
    )
    return BTCSignalContribution(
        name="opt_skew",
        label="Options 25Δ skew",
        raw_value=f"{value:+.2f}",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_fear_greed(value: int | None) -> BTCSignalContribution | None:
    if value is None:
        return None
    if value < 0 or value > 100:
        return None
    # Контр-сигнал у крайностей.
    if value <= 20:
        direction = 1
        weight = 0.5
        explanation = "extreme fear — исторически дно близко, contra-bull"
    elif value <= 35:
        direction = 1
        weight = 0.25
        explanation = "fear — лёгкий contra-bull"
    elif value >= 80:
        direction = -1
        weight = 0.5
        explanation = "extreme greed — топ близко, contra-bear"
    elif value >= 65:
        direction = -1
        weight = 0.25
        explanation = "greed — лёгкий contra-bear"
    else:
        direction = 0
        weight = 0.1
        explanation = "neutral sentiment"
    return BTCSignalContribution(
        name="fear_greed",
        label="Fear&Greed",
        raw_value=f"{value}/100",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_quant_verdict(
    direction_label: str | None,
    strength: float | None,
) -> BTCSignalContribution | None:
    if not direction_label:
        return None
    label = direction_label.upper()
    if label not in ("LONG", "SHORT", "NEUTRAL"):
        return None
    direction = 1 if label == "LONG" else (-1 if label == "SHORT" else 0)
    if strength is None or not math.isfinite(strength):
        strength = 0.5
    strength = max(0.0, min(1.0, float(strength)))
    weight = 0.25 + 0.45 * strength  # 0.25..0.70
    explanation = (
        f"quant ensemble: {label} @ strength={strength:.2f}"
        if direction != 0
        else "quant: NEUTRAL"
    )
    return BTCSignalContribution(
        name="quant",
        label="Quant ensemble",
        raw_value=f"{label} ({strength:.2f})",
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _score_regime(value: str | None) -> BTCSignalContribution | None:
    if not value:
        return None
    label = value.upper()
    if label in ("BULL", "UPTREND"):
        direction, weight, explanation = 1, 0.4, "Markov regime: BULL"
    elif label in ("BEAR", "DOWNTREND"):
        direction, weight, explanation = -1, 0.4, "Markov regime: BEAR"
    elif label in ("RANGE", "CHOP", "NEUTRAL"):
        direction, weight, explanation = 0, 0.15, "Markov regime: RANGE/CHOP"
    else:
        return None
    return BTCSignalContribution(
        name="regime",
        label="Regime",
        raw_value=label,
        direction=direction,
        weight=weight,
        explanation=explanation,
    )


def _summary_text(verdict: str, confidence: int) -> str:
    if verdict == LEAN_BULL:
        if confidence >= 70:
            return "Сильный bull-сетап — большинство сигналов вверх"
        if confidence >= 45:
            return "Умеренный bull-bias — больше сигналов вверх, но не все"
        return "Слабый bull-bias — сигналы поровну, чуть-чуть вверх"
    if verdict == LEAN_BEAR:
        if confidence >= 70:
            return "Сильный bear-сетап — большинство сигналов вниз, риск каскада"
        if confidence >= 45:
            return "Умеренный bear-bias — давление вниз, осторожность"
        return "Слабый bear-bias — сигналы спорят, лёгкая просадка вероятнее"
    return "Сигналы конфликтуют — биток в зоне неопределённости"


def compute_btc_outlook(inputs: BTCOutlookInputs) -> BTCOutlookVerdict:
    """Aggregate ``inputs`` into a ``BTCOutlookVerdict``."""
    contributions: list[BTCSignalContribution] = []
    candidate_factories = (
        _score_price_change(inputs.price_change_24h_pct),
        _score_funding(inputs.funding_rate_8h_pct),
        _score_oi_price_combo(inputs.oi_change_24h_pct, inputs.price_change_24h_pct),
        _score_ls_ratio(inputs.top_trader_ls_ratio),
        _score_dominance(inputs.btc_dominance_pct, inputs.dominance_change_7d_pct),
        _score_etf_basket(inputs.etf_basket_change_5d_avg_pct, inputs.etf_outflow_signal),
        _score_stable_supply(inputs.stablecoin_supply_delta_24h_pct),
        _score_options_skew(inputs.options_skew_25d),
        _score_fear_greed(inputs.fear_greed_index),
        _score_quant_verdict(inputs.quant_verdict_direction, inputs.quant_verdict_strength),
        _score_regime(inputs.regime),
    )
    for c in candidate_factories:
        if c is None:
            continue
        contributions.append(c)

    bull = sum(c.weight for c in contributions if c.direction > 0)
    bear = sum(c.weight for c in contributions if c.direction < 0)
    net = bull - bear
    total_weight = bull + bear

    if total_weight <= 0:
        lean = LEAN_NEUTRAL
        confidence = 0
    else:
        if net > NEUTRAL_BAND:
            lean = LEAN_BULL
        elif net < -NEUTRAL_BAND:
            lean = LEAN_BEAR
        else:
            lean = LEAN_NEUTRAL
        confidence = int(round(min(1.0, abs(net) / max(total_weight, 1e-9)) * 100))

    summary = _summary_text(lean, confidence)

    return BTCOutlookVerdict(
        lean=lean,
        confidence_pct=confidence,
        net_score=round(net, 3),
        bull_score=round(bull, 3),
        bear_score=round(bear, 3),
        contributions=tuple(contributions),
        timeframe_hours=48,
        summary=summary,
        fallback_used=total_weight <= 0,
        inputs_seen=len(contributions),
    )


def format_btc_outlook_markdown(
    verdict: BTCOutlookVerdict,
    inputs: BTCOutlookInputs,
    *,
    ai_narrative: str | None = None,
) -> str:
    """Telegram-friendly Markdown rendering of the verdict."""
    icon = {LEAN_BULL: "🟢", LEAN_BEAR: "🔴", LEAN_NEUTRAL: "⚪"}.get(verdict.lean, "⚪")
    header_price = ""
    if inputs.btc_price_usd is not None and math.isfinite(inputs.btc_price_usd):
        header_price = f" · BTC ${inputs.btc_price_usd:,.0f}"
    lines: list[str] = [
        f"{icon} *BTC outlook ({verdict.timeframe_hours}ч)*{header_price}",
        f"*Lean:* {verdict.lean} · *Confidence:* {verdict.confidence_pct}%",
        f"_{verdict.summary}_",
        "",
    ]

    if verdict.fallback_used or not verdict.contributions:
        lines.append("Нет сигналов — все источники недоступны. Повтори через 5-10 минут.")
        return "\n".join(lines)

    lines.append(f"*Сигналы* ({verdict.inputs_seen} активных):")
    for c in verdict.contributions:
        arrow = {1: "↑", -1: "↓", 0: "·"}[c.direction]
        lines.append(
            f"{arrow} `{c.label}` {c.raw_value} _(w={c.weight:.2f})_ — {c.explanation}"
        )

    lines.append("")
    lines.append(
        f"_Score:_ bull `{verdict.bull_score}` · bear `{verdict.bear_score}` "
        f"· net `{verdict.net_score:+.2f}`"
    )

    if ai_narrative:
        lines.append("")
        lines.append("*AI synth:*")
        lines.append(ai_narrative.strip())

    lines.append("")
    lines.append(
        "_Не финсовет. Сигналы — статистика прошлого, не гарантия. Решение остаётся за тобой._"
    )
    return "\n".join(lines)
