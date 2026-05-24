"""Composite trade-signal scorer для команды /signal.

Берёт `prices` dict (тот же что у /markets / format_prices_for_agents)
и считает по каждому активу 0-100 «trade-score». Если максимальный score
≥ `min_score` (по умолчанию 60), формирует setup с σ̂-based stop-loss
и target.

Идея: пользователь жмёт `/signal`, видит ОДИН setup (или «сидим»)
с конкретными уровнями и обоснованием — без AI, на основе уже посчитанных
in /markets метрик. Это «детерминированный» путь:
- цена тренд → direction (LONG/SHORT)
- complexity / VRT / Markov → confidence
- σ̂ (EWMA) → SL/TP distances → R/R = 2.0x

Никакого LLM. Только числа из dataclass'ов.

Scoring (мах 100):

  Trend alignment    (30 pts) — UPTREND→LONG, DOWNTREND→SHORT;
                                SIDEWAYS / нет MA → 0 pts (нет кандидата).
  Complexity hint    (20 pts) — TRENDING=20, MEAN_REVERTING=5,
                                RANDOM_WALK/CHAOTIC=0, missing=5.
  VRT structure       (20 pts) — H0 отвергнут=20, H0 не отвергнут=0,
                                 missing=5.
  Markov state        (15 pts) — направление совпадает с trade=10 + 5
                                 если P(next желаемое) ≥ 0.4;
                                 FLAT=5, opposite=0, missing=5.
  Raw tradeable_score (15 pts) — 15 × tradeable_score (0..1, clamp).

Если SIDEWAYS — direction='NONE' и score=0 (без trend нет сетапа).

SL/TP считаем по σ̂ (vol_sigma_1d_pct):
  LONG:  entry, SL=entry*(1 - 1.5·σ̂), TP=entry*(1 + 3·σ̂)
  SHORT: entry, SL=entry*(1 + 1.5·σ̂), TP=entry*(1 - 3·σ̂)
  R/R = 2.0x всегда.

Если σ̂ нет — setup не строим (без stop'а не можем).

Округление до tick'а биржи — по asset table (BTC=1, XRP=0.1 и т.д.).
Это не идеально (тик-сайз меняется на биржах), но защищает от ситуации
«ввёл SL/TP с 4 знаками — Bybit Spot отверг» (как у XRP вчера).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Stop-loss multiplier на σ̂. 1.5 даёт ~90% «не выбьется» за 1-3 бара
# при гипотезе нормальности (хвост Φ(-1.5) ≈ 6.7%, симметричный + tail
# fatness ~12%). Эмпирически меньше всего whipsaw на наших activs.
SL_SIGMA_MULT = 1.5

# Target multiplier — 2x SL для R/R=2.0. Это классика portfolio-theory:
# при breakeven winrate 33% уже плюс. Меньше — нужен высокий winrate;
# больше — TP практически не достигается.
TP_SIGMA_MULT = 3.0

# Минимальный score чтобы считать setup «торгуемым».
DEFAULT_MIN_SCORE = 60

# Доля капитала в одной позиции. 25% позволяет иметь 4 позиции
# одновременно (или одну с большим cushion'ом). Меньше — позиция шумит на
# спреде; больше — концентрация рисков на одной asset.
DEFAULT_SIZE_FRACTION = 0.25

# Tick size (минимальный шаг цены) для округления SL/TP. Bybit Spot.
# Это HARDCODE на основе наших активов; реальные tick'и можно тянуть из
# биржи, но для v1 хватит. Если актива нет в таблице — fallback на
# 4 знака после точки.
# Реальные тики Bybit Spot (USDT-pair). Аудитировано 18.05.2026 после live-репорта
# что XRP setup имел entry=$1.4 и stop=$1.4 (tick 0.1 проглатывал σ̂≈3%/день).
# XRP/USDT на Bybit Spot имеет шаг 0.0001 USDT (4 знака после запятой) —
# смотри цену entry $1.3933 в открытой позиции у юзера. Остальные тики не
# трогаем, для них σ̂ × цена ≫ tick'a и округление не критично.
#
# Тики для расширенного списка (ADA..SUI) — консервативные оценки на
# основе шагов на Bybit Spot / Binance Spot. Для low-price coins (DOGE,
# TRX) ставим 0.00001 чтобы σ̂≈5% × цена ≫ tick'a.
ASSET_TICK_SIZE: dict[str, float] = {
    "BTC":  0.01,
    "ETH":  0.01,
    "SOL":  0.001,
    "BNB":  0.01,
    "XRP":  0.0001,    # Bybit Spot XRP/USDT — 4 знака после точки.
    "ADA":  0.0001,
    "DOGE": 0.00001,
    "AVAX": 0.001,
    "LINK": 0.001,
    "DOT":  0.001,
    "TRX":  0.00001,
    "TON":  0.001,
    "LTC":  0.01,
    "NEAR": 0.001,
    "SUI":  0.0001,
    "SPX":  0.01,
    "NDX":  0.01,
    "VIX":  0.01,
    "OIL_WTI": 0.01,
    "GOLD": 0.1,
    "DXY":  0.001,
}

# Активы для которых имеет смысл строить trading setup. Исключаем
# индексы и сырьё (на них надо CFD/фьючи — не у всех есть доступ);
# крипта доступна на любой бирже. Список расширен с 5 до 15 активов
# (юзер: «смотрел на всю крипту и говорил ВСЕ лучшие сделки»).
TRADABLE_ASSETS: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "ADA", "DOGE", "AVAX", "LINK", "DOT",
    "TRX", "TON", "LTC", "NEAR", "SUI",
})


@dataclass
class ScoreBreakdown:
    """Разбивка composite trade-score по компонентам.

    Каждое поле — int 0-{max}. Сумма — `total` (clamp 0..100).
    """
    trend_alignment: int = 0      # 0..30
    complexity_hint: int = 0      # 0..20
    vrt_structure: int = 0        # 0..20
    markov_state: int = 0         # 0..15
    raw_tradeable: int = 0        # 0..15

    @property
    def total(self) -> int:
        s = (
            self.trend_alignment
            + self.complexity_hint
            + self.vrt_structure
            + self.markov_state
            + self.raw_tradeable
        )
        return max(0, min(100, s))


@dataclass
class AssetScore:
    """Промежуточный результат скоринга одного актива.

    `direction` — 'LONG' / 'SHORT' / 'NONE' (SIDEWAYS или нет данных).
    """
    asset: str
    direction: str
    breakdown: ScoreBreakdown
    reasons: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.breakdown.total


@dataclass
class SignalSetup:
    """Готовый trade-setup с уровнями и пояснением.

    Все цены округлены до `ASSET_TICK_SIZE[asset]` чтобы соответствовать
    биржевым tick'ам (XRP=0.1, BTC=0.01 и т.д.).
    """
    asset: str
    direction: str             # "LONG" or "SHORT"
    entry: float
    stop: float
    target: float
    stop_pct: float            # знаковое смещение от entry (% от entry)
    target_pct: float          # знаковое смещение от entry (% от entry)
    rr_ratio: float            # |target-entry| / |entry-stop|
    sigma_1d_pct: float        # σ̂_1d, который использовался для размера стопа
    size_usd: float            # рекомендованный размер позиции в USD
    score: int                 # 0..100
    reasons: list[str]         # обоснование (human-readable)


def _direction_from_trend(p: dict) -> str:
    """Решает в какую сторону смотрим на основе тренда и MA.

    UPTREND  → LONG  (требует выше обеих MA — это уже учтено в `trend`)
    DOWNTREND → SHORT
    SIDEWAYS / нет тренда → NONE (setup не строим).
    """
    trend = (p.get("trend") or "").upper()
    if trend == "UPTREND":
        return "LONG"
    if trend == "DOWNTREND":
        return "SHORT"
    return "NONE"


def _score_trend(p: dict, direction: str) -> tuple[int, str]:
    """30 pts за наличие тренда + 0/30 split.

    Формат строки: «UPTREND ✓ (vs MA50 +5.3%, MA200 +13.2%)».
    `{:+.1f}` всегда даёт знак — `+5.3` или `-15.0` — поэтому невозможна
    деформированная строка вида «+-15.0%» (была раньше когда trend помечен
    UPTREND, но цена фактически ниже MA50 — рассинхрон данных).
    """
    if direction == "NONE":
        return 0, "SIDEWAYS / нет MA → trade-кандидата нет"
    ma50 = p.get("ma50")
    ma200 = p.get("ma200")
    price = p.get("price")
    if not all(isinstance(x, (int, float)) for x in (price, ma50, ma200)):
        return 0, "Нет MA50/MA200 для верификации тренда"
    # Знаковая дельта от MA: положительная = выше MA, отрицательная = ниже.
    # Считаем одинаково для LONG и SHORT — заголовок UPTREND/DOWNTREND и так
    # ясно показывает направление, а числа должны быть честные.
    pct50 = (price - ma50) / ma50 * 100
    pct200 = (price - ma200) / ma200 * 100
    label = "UPTREND" if direction == "LONG" else "DOWNTREND"
    return 30, f"{label} ✓ (vs MA50 {pct50:+.1f}%, MA200 {pct200:+.1f}%)"


def _score_complexity(p: dict) -> tuple[int, str]:
    """20 pts по complexity_hint."""
    hint = (p.get("complexity_hint") or "").upper()
    if hint == "TRENDING":
        h_val = p.get("hurst")
        h_str = f"H={h_val:.2f}" if isinstance(h_val, (int, float)) else "H=?"
        return 20, f"TRENDING ✓ ({h_str}, score={p.get('tradeable_score', 0):.2f})"
    if hint == "MEAN_REVERTING":
        return 5, "MEAN_REVERTING — counter-trend, weak (5 pts)"
    if hint in ("RANDOM_WALK", "CHAOTIC"):
        return 0, f"{hint} — нет edge"
    return 5, "complexity_hint неизвестен (нейтрально 5 pts)"


def _score_vrt(p: dict) -> tuple[int, str]:
    """20 pts если VRT отверг H0 (есть структура)."""
    rw = p.get("vrt_random_walk")
    if rw is False:
        vr = p.get("vrt_ratio")
        vr_str = f"VR={vr:.2f}" if isinstance(vr, (int, float)) else "VR=?"
        return 20, f"VRT H0 отвергнут ✓ ({vr_str}, есть структура)"
    if rw is True:
        return 0, "VRT не отвергает H0 (ряд похож на random walk)"
    return 5, "VRT не посчитан (нейтрально 5 pts)"


def _score_markov(p: dict, direction: str) -> tuple[int, str]:
    """15 pts за совпадение Markov состояния с trade-направлением."""
    state = (p.get("markov_state") or "").upper()
    if not state:
        return 5, "Markov не посчитан (нейтрально 5 pts)"
    next_probs = p.get("markov_next_probs") or {}
    target_state = "UP" if direction == "LONG" else "DOWN"
    target_prob = float(next_probs.get(target_state, 0.0))
    if state == target_state:
        if target_prob >= 0.4:
            return (
                15,
                f"Markov {state} ✓ (P(next {target_state})={target_prob*100:.0f}%)",
            )
        return 10, f"Markov {state} ✓ (P(next {target_state})={target_prob*100:.0f}%, слабо)"
    if state == "FLAT":
        return 5, "Markov FLAT (нейтрально 5 pts)"
    # Opposite state
    return 0, f"Markov {state} — против trade ({direction})"


def _score_tradeable(p: dict) -> tuple[int, str]:
    """15 pts × raw tradeable_score."""
    score = p.get("tradeable_score")
    if not isinstance(score, (int, float)):
        return 0, "tradeable_score не посчитан"
    pts = int(round(15 * max(0.0, min(1.0, float(score)))))
    return pts, f"raw score={score:.2f} → {pts} pts"


def score_asset(asset: str, p: dict) -> AssetScore:
    """Считает composite trade-score одного актива.

    Возвращает AssetScore. Если direction=NONE, остальные компоненты
    почти всегда 0 (нет сетапа в SIDEWAYS) — это by design.
    """
    direction = _direction_from_trend(p)
    breakdown = ScoreBreakdown()
    reasons: list[str] = []

    pts, why = _score_trend(p, direction)
    breakdown.trend_alignment = pts
    reasons.append(why)

    if direction == "NONE":
        # Без trend'а не считаем остальные компоненты — нет сетапа.
        return AssetScore(asset=asset, direction=direction, breakdown=breakdown, reasons=reasons)

    pts, why = _score_complexity(p)
    breakdown.complexity_hint = pts
    reasons.append(why)

    pts, why = _score_vrt(p)
    breakdown.vrt_structure = pts
    reasons.append(why)

    pts, why = _score_markov(p, direction)
    breakdown.markov_state = pts
    reasons.append(why)

    pts, why = _score_tradeable(p)
    breakdown.raw_tradeable = pts
    reasons.append(why)

    return AssetScore(asset=asset, direction=direction, breakdown=breakdown, reasons=reasons)


def _round_to_tick(price: float, tick: float) -> float:
    """Округляет цену до ближайшего кратного `tick`."""
    if tick <= 0:
        return round(price, 4)
    return round(round(price / tick) * tick, 8)


def make_setup(
    score: AssetScore,
    p: dict,
    capital: float = 123.0,
    min_score: int = DEFAULT_MIN_SCORE,
    size_fraction: float = DEFAULT_SIZE_FRACTION,
) -> Optional[SignalSetup]:
    """Строит SignalSetup если score ≥ min_score и есть σ̂.

    Возвращает None если:
      - score < min_score
      - direction == 'NONE'
      - vol_sigma_1d_pct отсутствует
      - asset не в TRADABLE_ASSETS (индексы/сырьё пропускаем — нет
        прямого доступа к спот-торговле для большинства юзеров)
      - после округления до tick'a stop_r == entry_r или target_r == entry_r
        (σ̂ × цена < tick — например слабоволатильный alt с грубым tick'ом).
    """
    if score.total < min_score:
        return None
    if score.direction == "NONE":
        return None
    if score.asset not in TRADABLE_ASSETS:
        return None

    entry = p.get("price")
    sigma_pct = p.get("vol_sigma_1d_pct")
    if not isinstance(entry, (int, float)) or not isinstance(sigma_pct, (int, float)):
        return None
    sigma = float(sigma_pct) / 100.0
    if sigma <= 0:
        return None

    sl_dist = SL_SIGMA_MULT * sigma
    tp_dist = TP_SIGMA_MULT * sigma

    if score.direction == "LONG":
        stop_price = entry * (1.0 - sl_dist)
        target_price = entry * (1.0 + tp_dist)
        stop_pct = -sl_dist * 100
        target_pct = tp_dist * 100
    else:  # SHORT
        stop_price = entry * (1.0 + sl_dist)
        target_price = entry * (1.0 - tp_dist)
        stop_pct = sl_dist * 100
        target_pct = -tp_dist * 100

    tick = ASSET_TICK_SIZE.get(score.asset, 0.0001)
    entry_r = _round_to_tick(float(entry), tick)
    stop_r = _round_to_tick(stop_price, tick)
    target_r = _round_to_tick(target_price, tick)

    # R/R после округления (для крупных активов как BTC округление почти
    # не влияет; для XRP/SOL — заметно, поэтому пересчитываем).
    risk = abs(entry_r - stop_r)
    reward = abs(target_r - entry_r)
    # Defensive: если tick проглотил σ̂ (бывает на дешёвых монетах с грубым
    # tick'ом и низкой волой), то после округления stop_r == entry_r → risk=0.
    # Возвращать «R/R = 0.0x: ловим в 0.0 раза…» нельзя — это junk-сигнал.
    # Сетап нерабочий, лучше тихо отбросить и пусть caller возьмёт следующий.
    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk

    size_usd = round(capital * size_fraction, 2)

    return SignalSetup(
        asset=score.asset,
        direction=score.direction,
        entry=entry_r,
        stop=stop_r,
        target=target_r,
        stop_pct=round(stop_pct, 2),
        target_pct=round(target_pct, 2),
        rr_ratio=round(rr, 2),
        sigma_1d_pct=round(float(sigma_pct), 2),
        size_usd=size_usd,
        score=score.total,
        reasons=list(score.reasons),
    )


def rank_signals(
    prices: dict,
    capital: float = 123.0,
    min_score: int = DEFAULT_MIN_SCORE,
    size_fraction: float = DEFAULT_SIZE_FRACTION,
) -> dict:
    """Сканит все активы в `prices`, возвращает ранжированный список.

    Returns dict:
      {
        "top":          Optional[SignalSetup],  # setup ≥ min_score, или None.
        "preview_top":  Optional[SignalSetup],  # лучший tradable ниже порога —
                                                 # используется UI для preview-блока
                                                 # «вот как выглядел бы вход, но
                                                 # сегодня сидим». None если top
                                                 # найден или нет ни одного
                                                 # tradable кандидата с σ̂.
        "tradable_setups": list[SignalSetup],   # ВСЕ tradable setup'ы (score ≥ min_score),
                                                 # отсортированы по score ↓. Используется
                                                 # /signal чтобы показать ВСЕ лучшие
                                                 # сделки, а не только №1.
        "scored":       list[AssetScore],       # все активы, по total ↓
        "capital":      float,
        "min_score":    int,
      }
    """
    scored: list[AssetScore] = []
    for asset, p in prices.items():
        if not isinstance(p, dict):
            continue
        # Пропускаем «вспомогательные» ключи (MA50_BTC, ATR_BTC, и т.д.)
        if asset.startswith(("MA50_", "MA200_", "ATR_")):
            continue
        # Цена обязательна — без неё актив не торгуется.
        if not isinstance(p.get("price"), (int, float)):
            continue
        scored.append(score_asset(asset, p))

    scored.sort(key=lambda s: s.total, reverse=True)

    # tradable_setups — ВСЕ setup'ы что прошли порог. На расширенной
    # криптокорзине (15 активов) сильный тренд может дать сразу 3-5
    # tradable кандидатов, и юзер хочет видеть весь список, а не одну
    # сделку. Сохраняем порядок по score ↓ (= порядок scored).
    tradable_setups: list[SignalSetup] = []
    for s in scored:
        setup = make_setup(s, prices[s.asset], capital, min_score, size_fraction)
        if setup is not None:
            tradable_setups.append(setup)

    top: Optional[SignalSetup] = tradable_setups[0] if tradable_setups else None

    # preview_top: лучший tradable кандидат с σ̂, даже если score ниже порога.
    # Нужен формату «Лучшая сделка» чтобы и в «сидим» режиме показывать
    # уровни SL/TP/explain — иначе кнопка ощущается как «висим без причины».
    # Если top уже найден — preview_top избыточен.
    preview_top: Optional[SignalSetup] = None
    if top is None:
        for s in scored:
            setup = make_setup(
                s, prices[s.asset], capital, min_score=0, size_fraction=size_fraction,
            )
            if setup is not None:
                preview_top = setup
                break

    return {
        "top": top,
        "preview_top": preview_top,
        "tradable_setups": tradable_setups,
        "scored": scored,
        "capital": float(capital),
        "min_score": int(min_score),
    }
