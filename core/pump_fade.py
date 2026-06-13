"""core/pump_fade.py — превращает ДЕТЕКТ пампа в СТРУКТУРНЫЙ FADE-эдж.

Памп сам по себе — НЕ эдж. «Купить памп» = отрицательно-EV гэмблинг (входишь
поздно, памп мин-ревертит, асимметрия против тебя). Но у пампа есть структурный
противо-эдж: рынок СИСТЕМАТИЧЕСКИ откатывает резкие памп-выбросы. Мы его НЕ
угадываем — мы его измерили.

БЭКТЕСТ (Gate USDT-перпы, 219 памп-событий +20%/24ч с подтверждением по объёму,
~5 мес; Binance гео-блокнут в песочнице). Шорт на закрытии памп-свечи, после
0.3% round-trip костов:
    горизонт   mean    median   win%
      24ч      +1.2%   +1.8%    59%
      48ч      +2.8%   +4.6%    62%
      72ч      +3.8%   +5.0%    60%
Контринтуитивно: ТЕСНЫЙ стоп (10-15%) ВРЕДИТ — выбивает шумом на волатильном
альте ДО отката (при стопе 10%/72ч медиана падает до −9%). Риск-контроль здесь
= МАЛЫЙ РАЗМЕР + ШИРОКАЯ инвалидация + ДИВЕРСИФИКАЦИЯ по многим событиям, НЕ
тесный стоп. Эдж шумный (mean/sd ~0.2 на сделку) → нужно МНОГО сделок. Survivor-
ship делает оценку КОНСЕРВАТИВНОЙ (раг-пампы в ноль уже делистнуты — это были бы
победы шорта). Хвост реальный: p90 неблагоприятного хода ~+27% (новый памп-лег).

Гипотеза «памп → спайк фандинга → carry» на этих данных НЕ подтвердилась
(0/23 события дошли до carry-зоны) — поэтому не строим. Только fade.

Дизайн как у pump_scanner: ядро — чистые stdlib-функции, полностью тестируемо,
сеть не нужна. Никакой автоторговли — только структурный план сделки + честные
статы + предупреждение о хвосте. Логируется в edge-леджер (проверяемый трек).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Бэктест-провенанс (горизонт 72ч, после 0.3% костов, широкая/без стопа) ────
# Источник цифр — pump_fade backtest на Gate (см. docstring). Меняешь логику —
# перепроверь бэктестом, а не на глаз.
BT_WIN_RATE = 0.60        # доля прибыльных шортов на 72ч
BT_MEDIAN_RET = 0.050     # медианный профит шорта, 72ч
BT_MEAN_RET = 0.038       # средний профит шорта, 72ч (хвост его съедает)
BT_TAIL_P90 = 0.27        # p90 неблагоприятного хода (новый памп-лег = риск шорта)
BT_SAMPLE = "Gate, 219 событий, ~5 мес, 1 биржа"


def _env_float(name: str, default: float, *, lo: float = 0.0, hi: float = 1e12) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        return default


@dataclass(frozen=True)
class FadeConfig:
    """Параметры fade-плана. Дефолты из бэктеста; всё переопределяется env."""
    min_runup_pct: float = 15.0      # фейдим только СУЩЕСТВЕННЫЙ ран-ап (>=15%)
    target_pct: float = 0.06         # цель: откат на ~6% (≈ медиана отката)
    invalidation_pct: float = 0.30   # ШИРОКАЯ инвалидация: +30% над входом
    size_fraction: float = 0.02      # МАЛЫЙ размер: 2% книги на событие
    horizon_hours: float = 72.0      # держим до 72ч
    min_vol_ratio: float = 3.0       # подтверждение по объёму для «качества»
    price_floor: float = 0.001

    @classmethod
    def from_env(cls) -> "FadeConfig":
        return cls(
            min_runup_pct=_env_float("PUMP_FADE_MIN_RUNUP_PCT", 15.0),
            target_pct=_env_float("PUMP_FADE_TARGET_PCT", 0.06, hi=0.95),
            invalidation_pct=_env_float("PUMP_FADE_INVALIDATION_PCT", 0.30, lo=0.05),
            size_fraction=_env_float("PUMP_FADE_SIZE_FRACTION", 0.02, hi=1.0),
            horizon_hours=_env_float("PUMP_FADE_HORIZON_HOURS", 72.0, lo=1.0),
            min_vol_ratio=_env_float("PUMP_FADE_MIN_VOL_RATIO", 3.0),
        )


@dataclass
class FadePlay:
    """Структурный fade-сетап. Поля совместимы с edge_ledger.record_signal
    (asset/direction/entry/target/stop/score/rr_ratio/certificate/reasons),
    чтобы план логировался в трек-рекорд как любой другой сигнал."""
    asset: str
    entry: float
    target: float
    stop: float                       # = инвалидация (широкая), НЕ тесный стоп
    runup_pct: float
    horizon_hours: float
    size_fraction: float
    vol_ratio: Optional[float] = None
    mcap: Optional[float] = None
    venues: list = field(default_factory=list)
    certificate: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # — поля-синонимы под контракт edge_ledger —
    direction: str = "SHORT"
    score: float = BT_WIN_RATE

    @property
    def rr_ratio(self) -> float:
        """Reward/risk на сделку. Здесь <1 НАМЕРЕННО: эдж в ВЫСОКОМ вин-рейте
        и частоте, не в RR. Прибыль идёт от медианного отката × малого размера ×
        диверсификации, а широкая инвалидация лишь режет катастрофу-хвост."""
        risk = self.invalidation_pct
        return round(self.target_pct / risk, 3) if risk else 0.0

    @property
    def target_pct(self) -> float:
        return round(1.0 - self.target / self.entry, 4) if self.entry else 0.0

    @property
    def invalidation_pct(self) -> float:
        return round(self.stop / self.entry - 1.0, 4) if self.entry else 0.0


def build_fade_play(
    asset: str,
    last_price: Optional[float],
    runup_pct: float,
    *,
    vol_ratio: Optional[float] = None,
    mcap: Optional[float] = None,
    venues: Optional[list] = None,
    cfg: Optional[FadeConfig] = None,
) -> Optional[FadePlay]:
    """Строит fade-план, ЕСЛИ ран-ап достаточно велик (истощённый памп).

    Возвращает None когда фейдить нечего: нет цены / цена-пыль / ран-ап ниже
    порога. `runup_pct` — на сколько % актив уже вырос (например prior_pct из
    pump_scanner — макс. рост за 3 дня). Это структурный противо-сигнал к
    разогретой монете, которую buy-детектор как раз ОТСЕИВАЕТ (already_heated).
    """
    cfg = cfg or FadeConfig()
    if last_price is None or last_price <= cfg.price_floor:
        return None
    if runup_pct < cfg.min_runup_pct:
        return None

    entry = float(last_price)
    target = entry * (1.0 - cfg.target_pct)
    stop = entry * (1.0 + cfg.invalidation_pct)

    vol_ok = (vol_ratio is None) or (vol_ratio >= cfg.min_vol_ratio)
    certificate = {
        "runup_ge_min": True,
        "volume_confirmed": bool(vol_ok),
        "price_above_floor": True,
        "structural_fade": True,
    }
    reasons = [
        f"Ран-ап +{runup_pct:.0f}% ≥ порога {cfg.min_runup_pct:.0f}% → истощённый памп",
        f"Структурный fade: рынок исторически откатывает резкие выбросы "
        f"(бэктест win {BT_WIN_RATE:.0%}, медиана +{BT_MEDIAN_RET:.0%}/72ч)",
        "Риск-контроль: малый размер + широкая инвалидация + диверсификация "
        "(тесный стоп здесь ВРЕДИТ — выбивает шумом)",
    ]
    if not vol_ok:
        reasons.append("⚠️ объём не подтверждён (vol_ratio < порога) — ниже качество")

    return FadePlay(
        asset=asset, entry=entry, target=target, stop=stop,
        runup_pct=float(runup_pct), horizon_hours=cfg.horizon_hours,
        size_fraction=cfg.size_fraction, vol_ratio=vol_ratio, mcap=mcap,
        venues=list(venues or []), certificate=certificate, reasons=reasons,
    )


def _fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "?"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    return f"{p:.6f}".rstrip("0").rstrip(".")


def format_fade_play(play: FadePlay, *, capital: Optional[float] = None) -> str:
    """Markdown fade-плана. ЧЁТКО как структурный шорт/мин-реверсия (НЕ «покупай»),
    с честными статами и предупреждением о хвосте."""
    size_line = f"{play.size_fraction:.0%} книги (малый размер!)"
    if capital:
        size_line = f"≈ ${capital * play.size_fraction:,.0f} ({play.size_fraction:.0%} книги, малый размер!)"
    lines = [
        f"🔻 *{play.asset} — FADE (структурный шорт отката)*",
        f"Памп уже +{play.runup_pct:.0f}% → истощение. Фейдим откат, дельта-направленно.",
        "",
        f"• Вход: ШОРТ перп у `{_fmt_price(play.entry)}`",
        f"• Цель: `{_fmt_price(play.target)}` (откат ~{play.target_pct:.0%})",
        f"• Инвалидация (ШИРОКАЯ): `{_fmt_price(play.stop)}` (+{play.invalidation_pct:.0%}) "
        f"— это где тезис сломан, НЕ тесный стоп",
        f"• Горизонт: до {play.horizon_hours:.0f}ч  ·  Размер: {size_line}",
        "",
        f"📊 Бэктест ({BT_SAMPLE}): win *{BT_WIN_RATE:.0%}*, медиана *+{BT_MEDIAN_RET:.0%}*, "
        f"среднее +{BT_MEAN_RET:.0%} за 72ч (после костов).",
        f"⚠️ *Хвост реальный:* p90 неблагоприятного хода ~+{BT_TAIL_P90:.0%} (новый памп-лег "
        f"может выбить). Поэтому размер МАЛЫЙ и диверсификация по МНОГИМ событиям — "
        f"эдж в частоте, не в одной сделке. RR={play.rr_ratio} (намеренно <1).",
        "",
        "_Не финсовет. Структурный edge тонкий и шумный — работает только на дистанции._",
    ]
    return "\n".join(lines)


async def log_fade_play(play: FadePlay) -> Optional[int]:
    """Залогировать fade-план в edge-леджер (под FEATURE_EDGE_LEDGER).
    Безопасно: любой сбой проглатывается, прод-путь не падает."""
    try:
        from config import FEATURE_EDGE_LEDGER
    except Exception:
        FEATURE_EDGE_LEDGER = False
    if not FEATURE_EDGE_LEDGER:
        return None
    try:
        from core.edge_ledger import record_signal
        return await record_signal(play, source="pump_fade")
    except Exception:
        logger.debug("pump_fade.log_fade_play failed", exc_info=True)
        return None


__all__ = [
    "FadeConfig", "FadePlay", "build_fade_play", "format_fade_play",
    "log_fade_play", "BT_WIN_RATE", "BT_MEDIAN_RET", "BT_MEAN_RET", "BT_TAIL_P90",
]
