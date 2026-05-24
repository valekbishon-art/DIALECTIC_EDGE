"""Cascade post-mortem (pure-math module).

Auto-fires когда суммарные публичные ликвидации (Binance + Bybit) за
скользящее 24ч окно ≥ POST_MORTEM_THRESHOLD_USD (default $500M) или за
4ч ≥ POST_MORTEM_ACUTE_THRESHOLD_USD (default $200M). Цель — честный
ретроспективный «что мы видели / что пропустили» лог.

Этот модуль stdlib-only: dataclasses, агрегация по окнам, форматтер
markdown'а. Никакого I/O / SQLite / HTTP / WebSocket.

I/O (WS-listener, SQLite-персист, fetch индикаторов) — в
`cascade_post_mortem_io.py`. Скедулер интегрирован в `scheduler.py`
через фичефлаг `FEATURE_CASCADE_POST_MORTEM=0` (default OFF).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# ─── КОНСТАНТЫ ──────────────────────────────────────────────────────────────

# Окна (секунды).
WINDOW_24H_S = 24 * 3600
WINDOW_4H_S = 4 * 3600

# Дефолтные пороги. Тюнятся через env (см. cascade_post_mortem_io.py).
DEFAULT_THRESHOLD_24H_USD = 500_000_000.0
DEFAULT_THRESHOLD_4H_ACUTE_USD = 200_000_000.0
DEFAULT_COOLDOWN_HOURS = 6

# Имена окон — кладутся в SQLite (CHECK constraint).
WINDOW_TYPE_24H = "rolling_24h"
WINDOW_TYPE_4H_ACUTE = "rolling_4h_acute"

# Сторона позиции, которая была ликвидирована.
SIDE_LONG = "long"
SIDE_SHORT = "short"


# ─── DATACLASSES ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LiquidationEvent:
    """Один публичный liquidation event (forceOrder / liquidation feed).

    Конвенция side: что было ликвидировано.
    - side="long"  → ликвидирована длинная позиция (продажа по market'у)
    - side="short" → ликвидирована короткая позиция (покупка по market'у)

    На WS Binance это поле `S` (BUY/SELL) — нужно инвертировать:
    SELL → long (ликвидируется лонг), BUY → short (ликвидируется шорт).
    """

    timestamp_ms: int
    venue: str  # "binance" | "bybit"
    symbol: str  # "BTCUSDT", "ETHUSDT", ...
    side: str  # SIDE_LONG | SIDE_SHORT
    value_usd: float


@dataclass(frozen=True)
class WindowAggregate:
    """Агрегат ликвидаций за окно."""

    window_type: str
    window_hours: int
    total_usd: float
    long_usd: float
    short_usd: float
    event_count: int

    @property
    def long_share(self) -> float:
        """Доля long-ликвидаций (0..1). 0 если total=0."""
        if self.total_usd <= 0:
            return 0.0
        return self.long_usd / self.total_usd

    @property
    def dominant_side(self) -> str:
        """'long' | 'short' | 'mixed'.

        mixed — если ни одна сторона не доминирует ≥60%.
        """
        if self.total_usd <= 0:
            return "mixed"
        share = self.long_share
        if share >= 0.6:
            return SIDE_LONG
        if share <= 0.4:
            return SIDE_SHORT
        return "mixed"


@dataclass
class CascadeSnapshot:
    """Состояние мира на момент срабатывания триггера.

    indicators — словарь индикатор → структурированный результат. Заполняется
    в I/O-модуле через существующие fetcher'ы (regime, smart-money wallets,
    liquidation magnet, ETF flow streak, funding term, options skew).
    """

    triggered_at_iso: str  # ISO-8601 UTC
    triggered_at_ms: int
    window: WindowAggregate
    indicators: dict = field(default_factory=dict)
    debate_excerpt: str | None = None  # последний debate output если есть


# ─── АГРЕГАЦИЯ ──────────────────────────────────────────────────────────────


def aggregate_window(
    events: Iterable[LiquidationEvent],
    *,
    now_ms: int,
    window_seconds: int,
    window_type: str,
) -> WindowAggregate:
    """Суммирует ликвидации за окно [now - window, now].

    Аргументы должны быть в миллисекундах UTC. Окно — секунды.
    """
    window_ms = window_seconds * 1000
    cutoff = now_ms - window_ms
    if window_seconds <= 0:
        # защита: пустое окно
        return WindowAggregate(
            window_type=window_type,
            window_hours=0,
            total_usd=0.0,
            long_usd=0.0,
            short_usd=0.0,
            event_count=0,
        )

    total = 0.0
    longs = 0.0
    shorts = 0.0
    count = 0
    for ev in events:
        if ev.timestamp_ms < cutoff or ev.timestamp_ms > now_ms:
            continue
        if ev.value_usd <= 0:
            continue
        total += ev.value_usd
        if ev.side == SIDE_LONG:
            longs += ev.value_usd
        elif ev.side == SIDE_SHORT:
            shorts += ev.value_usd
        count += 1

    return WindowAggregate(
        window_type=window_type,
        window_hours=max(1, window_seconds // 3600),
        total_usd=total,
        long_usd=longs,
        short_usd=shorts,
        event_count=count,
    )


def aggregate_24h(events: Iterable[LiquidationEvent], *, now_ms: int) -> WindowAggregate:
    return aggregate_window(
        events,
        now_ms=now_ms,
        window_seconds=WINDOW_24H_S,
        window_type=WINDOW_TYPE_24H,
    )


def aggregate_4h(events: Iterable[LiquidationEvent], *, now_ms: int) -> WindowAggregate:
    return aggregate_window(
        events,
        now_ms=now_ms,
        window_seconds=WINDOW_4H_S,
        window_type=WINDOW_TYPE_4H_ACUTE,
    )


# ─── ТРИГГЕР ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TriggerDecision:
    """Результат проверки should_trigger()."""

    should_fire: bool
    window: WindowAggregate | None
    reason: str  # человеко-читаемая причина (для логов)


def should_trigger(
    *,
    agg_24h: WindowAggregate,
    agg_4h: WindowAggregate,
    threshold_24h_usd: float = DEFAULT_THRESHOLD_24H_USD,
    threshold_4h_usd: float = DEFAULT_THRESHOLD_4H_ACUTE_USD,
    now_ms: int,
    last_triggered_ms: int | None = None,
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS,
) -> TriggerDecision:
    """Решает, нужно ли запускать post-mortem.

    Приоритет: 4h acute > 24h rolling (acute = быстрее реагируем на острые
    каскады типа 3 марта 2024).

    Anti-spam: если предыдущий post-mortem был < cooldown_hours назад,
    не триггерим, даже если порог превышен.
    """
    # Anti-spam cooldown
    if last_triggered_ms is not None and cooldown_hours > 0:
        cooldown_ms = cooldown_hours * 3600 * 1000
        if now_ms - last_triggered_ms < cooldown_ms:
            return TriggerDecision(
                should_fire=False,
                window=None,
                reason=(
                    f"cooldown active "
                    f"({(now_ms - last_triggered_ms) // 60_000} min "
                    f"< {cooldown_hours}h)"
                ),
            )

    # 4h acute — приоритет (быстро реагируем на острые каскады)
    if agg_4h.total_usd >= threshold_4h_usd:
        return TriggerDecision(
            should_fire=True,
            window=agg_4h,
            reason=(
                f"4h acute trigger: "
                f"${agg_4h.total_usd / 1e6:.1f}M >= "
                f"${threshold_4h_usd / 1e6:.1f}M"
            ),
        )

    # 24h rolling
    if agg_24h.total_usd >= threshold_24h_usd:
        return TriggerDecision(
            should_fire=True,
            window=agg_24h,
            reason=(
                f"24h rolling trigger: "
                f"${agg_24h.total_usd / 1e6:.1f}M >= "
                f"${threshold_24h_usd / 1e6:.1f}M"
            ),
        )

    return TriggerDecision(
        should_fire=False,
        window=None,
        reason=(
            f"below thresholds: "
            f"24h=${agg_24h.total_usd / 1e6:.1f}M, "
            f"4h=${agg_4h.total_usd / 1e6:.1f}M"
        ),
    )


# ─── ИНДИКАТОР-АТРИБУЦИЯ ────────────────────────────────────────────────────


def attribute_signals(
    *,
    dominant_side: str,
    indicators: dict,
) -> tuple[list[str], list[str]]:
    """Раскладывает индикаторы на «что МЫ видели» vs «что ПРОПУСТИЛИ».

    Идея: если каскад завалил лонгов (dominant_side=long), то МЫ видели
    каскад, если индикаторы за 24-48ч ДО давали bearish/down-magnet/высокую
    L/S ratio (perfect predictor). ПРОПУСТИЛИ — если давали bullish.

    Конкретные правила (консервативные, чтобы не overclaim):

    Liquidation magnet:
        DOWN_MAGNET + dominant=long → «видели» (прямой hit)
        UP_MAGNET + dominant=short → «видели»
        Противоположное → «пропустили»
        NEUTRAL/UNKNOWN → не учитывается

    Regime:
        TRENDING_DOWN/CRISIS + dominant=long → «видели»
        TRENDING_UP/EUPHORIA + dominant=short → «видели»

    Smart-money wallets:
        DISTRIBUTING + dominant=long → «видели» (smart money продавали ETH/BTC)
        ACCUMULATING + dominant=short → «видели»

    ETF flow:
        outflow streak >= 3 дня + dominant=long → «видели»
        Сильный inflow + dominant=short → «видели»

    Funding term structure:
        inverted contango (basis < 0) + dominant=long → «видели»
        Сильный backwardation flip + dominant=short → «видели»

    Args:
        dominant_side: 'long' | 'short' | 'mixed'
        indicators: snapshot dict, ключи — имя индикатора, значение —
            подсловарь с полями label/severity/...

    Returns:
        (видели_список, пропустили_список) — каждый элемент это
        короткая markdown-строка с эмодзи.
    """
    saw: list[str] = []
    missed: list[str] = []

    # ─ Liquidation magnet ─
    lm = indicators.get("liquidation_magnet") or {}
    lm_label = (lm.get("label") or "").lower()
    lm_strong = bool(lm.get("is_strong_signal"))
    if lm_label == "down_magnet":
        line = "🎯 Liquidation magnet = DOWN (longs liquidatable)"
        if lm_strong:
            line += " — *strong*"
        if dominant_side == SIDE_LONG:
            saw.append(line)
        elif dominant_side == SIDE_SHORT:
            missed.append(line)
    elif lm_label == "up_magnet":
        line = "🎯 Liquidation magnet = UP (shorts squeezable)"
        if lm_strong:
            line += " — *strong*"
        if dominant_side == SIDE_SHORT:
            saw.append(line)
        elif dominant_side == SIDE_LONG:
            missed.append(line)

    # ─ Regime ─
    rg = indicators.get("regime") or {}
    rg_label = (rg.get("label") or "").lower()
    if rg_label in ("crisis", "trending"):
        line = f"📊 Regime = {rg_label.upper()}"
        # crisis или trending в любую сторону — predicts cascade direction
        # неизвестен из метки одной; смотрим dominant_side как hint
        if dominant_side in (SIDE_LONG, SIDE_SHORT):
            saw.append(line)
    elif rg_label == "volatile":
        line = "📊 Regime = VOLATILE (any-side cascade plausible)"
        saw.append(line)

    # ─ Smart-money wallets ─
    smw = indicators.get("smart_money_wallets") or {}
    smw_label = (smw.get("label") or "").lower()
    if smw_label == "distributing":
        line = "🐋 Smart-money wallets = DISTRIBUTING (offloading)"
        if dominant_side == SIDE_LONG:
            saw.append(line)
        elif dominant_side == SIDE_SHORT:
            missed.append(line)
    elif smw_label == "accumulating":
        line = "🐋 Smart-money wallets = ACCUMULATING"
        if dominant_side == SIDE_SHORT:
            saw.append(line)
        elif dominant_side == SIDE_LONG:
            missed.append(line)

    # ─ BTC ETF flow streak ─
    etf = indicators.get("btc_etf_flow") or {}
    etf_streak = int(etf.get("streak_days") or 0)
    etf_severity = (etf.get("severity") or "").upper()
    if etf_streak >= 3 and etf_severity in ("WARN", "CRIT"):
        line = (
            f"🏦 BTC ETF outflow streak = {etf_streak}d ({etf_severity})"
        )
        if dominant_side == SIDE_LONG:
            saw.append(line)
        elif dominant_side == SIDE_SHORT:
            missed.append(line)

    # ─ Funding term structure ─
    ft = indicators.get("funding_term") or {}
    ft_inverted = bool(ft.get("is_inverted"))
    if ft_inverted:
        line = "📉 Funding term structure = INVERTED (stress)"
        # inverted contango → bearish setup в большинстве случаев
        if dominant_side == SIDE_LONG:
            saw.append(line)

    # ─ Options skew ─
    skew = indicators.get("options_skew") or {}
    skew_class = (skew.get("skew_class") or "").lower()
    if skew_class in ("put_premium", "fear"):
        line = "🎲 Options skew = PUT-premium (hedging demand)"
        if dominant_side == SIDE_LONG:
            saw.append(line)
    elif skew_class in ("call_premium", "greed"):
        line = "🎲 Options skew = CALL-premium (FOMO)"
        if dominant_side == SIDE_SHORT:
            saw.append(line)

    return saw, missed


def derive_action_items(
    *,
    dominant_side: str,
    window_type: str,
    saw: Sequence[str],
    missed: Sequence[str],
) -> list[str]:
    """Из (что видели / что пропустили) выводит конкретные action items.

    Простые правила:
    - Если «пропустили» больше «видели» → нужна донастройка thresholds.
    - Если 4h acute и «видели» >= 1 → подтвердить, что система реагирует
      быстрее на острые каскады (subscribe для пуш-нотификаций).
    - Если dominant=mixed → пересмотреть classifier'ы (не должны давать
      direction'ный сигнал когда рынок mixed).
    """
    items: list[str] = []
    saw_n = len(saw)
    missed_n = len(missed)

    if missed_n > saw_n and missed_n > 0:
        items.append(
            f"🔧 Donастроить thresholds: пропустили {missed_n} сигналов, "
            f"видели только {saw_n}. Проверить пороги в .env."
        )

    if window_type == WINDOW_TYPE_4H_ACUTE and saw_n >= 1:
        items.append(
            "⚡ 4h acute caught: подтвердить, что push-alert"
            " (alert_engine) сработал перед каскадом."
        )

    if dominant_side == "mixed":
        items.append(
            "🎭 Mixed-side cascade: пересмотреть directional classifier'ы"
            " (regime/liq-magnet/smw) — не должны давать сильный directional"
            " сигнал при mixed-cascade."
        )

    if not items:
        items.append(
            "✅ Система отработала как ожидалось; пороги выглядят "
            "разумно для этого каскада."
        )

    return items


# ─── ФОРМАТТЕР ──────────────────────────────────────────────────────────────


def format_post_mortem_markdown(snapshot: CascadeSnapshot) -> str:
    """Строит TG-готовый markdown для каскадного post-mortem.

    Структура:
        🔥 Cascade $XYZm liquidated за {window}h
        Окно: rolling 24h | 4h acute
        Long flush: $Am / Short squeeze: $Bm

        Что МЫ видели:
        - ...

        Что ПРОПУСТИЛИ:
        - ...

        Action items:
        - ...

        _Triggered at ISO_UTC_
    """
    w = snapshot.window
    dominant = w.dominant_side
    saw, missed = attribute_signals(
        dominant_side=dominant,
        indicators=snapshot.indicators,
    )
    actions = derive_action_items(
        dominant_side=dominant,
        window_type=w.window_type,
        saw=saw,
        missed=missed,
    )

    window_human = (
        "4h acute" if w.window_type == WINDOW_TYPE_4H_ACUTE else "24h rolling"
    )
    total_m = w.total_usd / 1e6
    long_m = w.long_usd / 1e6
    short_m = w.short_usd / 1e6

    header = f"🔥 *Cascade ${total_m:.1f}M liquidated* ({window_human})"
    breakdown = (
        f"Long flush: `${long_m:.1f}M` · "
        f"Short squeeze: `${short_m:.1f}M` · "
        f"Events: `{w.event_count}` · "
        f"Dominant: *{dominant.upper()}*"
    )

    lines: list[str] = [header, breakdown, ""]

    lines.append("*Что МЫ видели (signals up to triggering):*")
    if saw:
        lines.extend(f"• {s}" for s in saw)
    else:
        lines.append("• _(никаких directional сигналов не подтвердилось)_")
    lines.append("")

    lines.append("*Что ПРОПУСТИЛИ (counter-trend signals):*")
    if missed:
        lines.extend(f"• {m}" for m in missed)
    else:
        lines.append("• _(чисто, противоположных сигналов не было)_")
    lines.append("")

    lines.append("*Action items:*")
    lines.extend(f"• {a}" for a in actions)

    if snapshot.debate_excerpt:
        lines.append("")
        lines.append("*Последний debate excerpt:*")
        # Цитируем как code block чтобы TG не интерпретировал markdown
        lines.append("```")
        # Обрезаем чтобы не раздувать TG сообщение
        excerpt = snapshot.debate_excerpt.strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:600] + "…"
        lines.append(excerpt)
        lines.append("```")

    lines.append("")
    lines.append(f"_Triggered at {snapshot.triggered_at_iso} UTC_")

    return "\n".join(lines)
