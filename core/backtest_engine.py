"""core/backtest_engine.py — воспроизводимый walk-forward бэктест стратегии.

ЗАЧЕМ. Фаза 1 (`core/edge_ledger`) измеряет edge живых сигналов, но копит
выборку ~2 недели. Этот модуль даёт ту же картину СРАЗУ, прогоняя стратегию по
истории. Цель — измерить реальное матожидание (expectancy) сигналов бота.

КЛЮЧЕВОЙ ПРИНЦИП — повторяем ЖИВУЮ логику, не выдуманную:
  • Метрики (trend/MA/σ̂/hurst/VRT/markov) считаем той же чистой функцией, что
    бот гоняет в проде — `web_search.compute_trend_fields`.
  • Скоринг и построение SL/TP — тем же `core.signal_scorer.rank_signals`.
  • Исход сделки (TP/SL/expired) — тем же `core.edge_ledger.resolve_against_candles`
    (пессимизм same-candle, no look-ahead, комиссия 0.2%).

ГРАНИЦЫ (честная рамка). Бэктест покрывает ТОЛЬКО OHLCV-часть стратегии.
Live-модификаторы score (funding, top-trader L/S, whale, новости) в истории не
сохранены и НЕ воспроизводятся — edge меряется без них. Это явно в отчёте.

LOOK-AHEAD GUARD (критичный инвариант):
  • На дне T метрики считаются по окну closes[:T+1] (T включительно, будущего нет).
  • Резолв — строго по свечам со timestamp > T (момент выпуска = open дня T+0,
    мы входим по close дня T → emitted_dt = ts дня T).
Тесты в tests/test_backtest_engine.py это фиксируют.

Детерминизм: на одном снапшоте свечей два прогона дают идентичный отчёт.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Переиспользуем существующий dataclass Candle (поля .timestamp/.high/.low/.close —
# ровно то, что нужно resolve_against_candles).
from backtester import Candle
from core.edge_ledger import resolve_against_candles
from core.signal_scorer import (
    ASSET_TICK_SIZE,
    SL_SIGMA_MULT,
    TP_SIGMA_MULT,
    TRADABLE_ASSETS,
    _round_to_tick,
    rank_signals,
)
from quant_filter import quant_verdict
from web_search import compute_trend_fields

# Дефолтный горизонт — как в Фазе 1 (EDGE_DEFAULT_HORIZON_HOURS=336 = 14 дней).
DEFAULT_HORIZON_HOURS = 336
DEFAULT_MIN_SCORE = 60
DEFAULT_CAPITAL = 123.0
# Минимум баров для скоринга: make_setup требует σ̂, complexity-поля надёжны
# от ~60 баров; MA200 требует 200. Берём 200, чтобы тренд-метка была честной.
WARMUP_BARS = 200

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "backtest_candles",
)


# ──────────────────────────── загрузка снапшота ────────────────────────────


def load_candles(asset: str, data_dir: str = DATA_DIR) -> list[Candle]:
    """Читает снапшот `data/backtest_candles/<asset>.json` → list[Candle].

    Timestamps — UTC-aware (Binance отдаёт open-time в ms UTC). Это важно:
    resolve_against_candles сравнивает c.timestamp с emitted_dt, и оба обязаны
    быть одинаковой tz-осведомлённости, иначе Python кинет TypeError.
    Возвращает [] если файла нет.
    """
    path = os.path.join(data_dir, f"{asset}.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    out: list[Candle] = []
    for k in payload.get("klines", []):
        ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
        out.append(Candle(
            timestamp=ts,
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]) if len(k) > 5 else 0.0,
        ))
    out.sort(key=lambda c: c.timestamp)
    return out


# ──────────────────────────── модель сделки ────────────────────────────────


@dataclass
class BacktestTrade:
    """Одна симулированная сделка."""
    asset: str
    direction: str          # LONG / SHORT
    score: int              # 0..100
    entry: float
    stop: float
    target: float
    rr_ratio: float
    day_index: int          # индекс дня входа в ряду свечей
    emitted_at: str         # ISO ts дня входа
    status: str             # tp / sl / expired (pending отбрасываем)
    pnl_pct: float          # чистый PnL % после комиссии
    exit_at: str | None


@dataclass
class BacktestReport:
    """Агрегированный результат прогона."""
    n_trades: int = 0
    n_resolved: int = 0          # tp + sl + expired (без pending)
    wins: int = 0                # pnl > 0
    losses: int = 0              # pnl <= 0
    win_rate: float = 0.0        # %
    expectancy_pct: float = 0.0  # средний PnL %/сделку — ГЛАВНАЯ цифра
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    payoff: float = 0.0          # |avg_win / avg_loss| — нужен ≥2 при WR~37%
    profit_factor: float = 0.0   # сумма выигрышей / |сумма проигрышей|
    max_drawdown_pct: float = 0.0
    total_pnl_pct: float = 0.0
    by_asset: dict = field(default_factory=dict)
    by_score_bucket: dict = field(default_factory=dict)
    by_direction: dict = field(default_factory=dict)
    by_year: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "n_resolved": self.n_resolved,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 2),
            "expectancy_pct": round(self.expectancy_pct, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "payoff": round(self.payoff, 3),
            "profit_factor": round(self.profit_factor, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "total_pnl_pct": round(self.total_pnl_pct, 4),
            "by_asset": self.by_asset,
            "by_score_bucket": self.by_score_bucket,
            "by_direction": self.by_direction,
            "by_year": self.by_year,
            "params": self.params,
        }


# ──────────────────────────── ядро ─────────────────────────────────────────


def _score_bucket(score: int) -> str:
    if score >= 85:
        return "85+"
    if score >= 70:
        return "70-84"
    return "60-69"


@dataclass
class _Setup:
    """Лёгкий сетап для бэктеста (унифицирует trend и quant источники)."""
    asset: str
    direction: str
    entry: float
    stop: float
    target: float
    rr_ratio: float
    score: int


def _make_quant_setup(asset: str, direction: str, price: float, sigma_pct: float) -> _Setup | None:
    """Строит сетап для quant-сигнала ТЕМИ ЖЕ σ̂-правилами, что и trend-ядро.

    Единственное отличие от make_setup — направление берётся из quant_verdict,
    а не из тренда. SL/TP = entry × (1 ± k·σ̂), R/R≈2, округление до tick.
    Так сравнение quant vs trend честное: разный сигнал входа, одинаковый риск.
    """
    if not isinstance(price, (int, float)) or not isinstance(sigma_pct, (int, float)):
        return None
    sigma = float(sigma_pct) / 100.0
    if sigma <= 0:
        return None
    sl_dist = SL_SIGMA_MULT * sigma
    tp_dist = TP_SIGMA_MULT * sigma
    if direction == "LONG":
        stop_price = price * (1.0 - sl_dist)
        target_price = price * (1.0 + tp_dist)
    else:  # SHORT
        stop_price = price * (1.0 + sl_dist)
        target_price = price * (1.0 - tp_dist)
    tick = ASSET_TICK_SIZE.get(asset, 0.0001)
    entry_r = _round_to_tick(float(price), tick)
    stop_r = _round_to_tick(stop_price, tick)
    target_r = _round_to_tick(target_price, tick)
    risk = abs(entry_r - stop_r)
    reward = abs(target_r - entry_r)
    if risk <= 0 or reward <= 0:
        return None
    return _Setup(asset, direction, entry_r, stop_r, target_r, round(reward / risk, 2), 0)


def _build_prices_at(candles_by_asset: dict[str, list[Candle]], upto_idx: int) -> dict:
    """Собирает prices-dict на день T (=upto_idx) по окну [:T+1].

    Для каждого актива берёт closes/highs/lows ДО дня T включительно (без
    будущего), зовёт ту же compute_trend_fields, что и live-бот, и проставляет
    price = close[T]. Активы короче WARMUP_BARS пропускаются.
    """
    prices: dict = {}
    for asset, candles in candles_by_asset.items():
        if upto_idx + 1 < WARMUP_BARS or upto_idx >= len(candles):
            continue
        window = candles[: upto_idx + 1]
        closes = [c.close for c in window]
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        fields = compute_trend_fields(closes, highs, lows)
        if not fields:
            continue
        fields["price"] = closes[-1]
        prices[asset] = fields
    return prices


def run_backtest(
    candles_by_asset: dict[str, list[Candle]],
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    capital: float = DEFAULT_CAPITAL,
    start_index: int | None = None,
    end_index: int | None = None,
    quant_gate: bool = False,
    strategy: str = "trend",
) -> tuple[list[BacktestTrade], BacktestReport]:
    """Walk-forward прогон. Возвращает (список сделок, отчёт).

    strategy:
      "trend" — ядро бота: rank_signals по trend/MA/complexity (по умолчанию).
      "quant" — mean-reversion из quant_filter.quant_verdict (BB+Donchian+RSI
                с BTC-regime gate). SL/TP строятся теми же σ̂-правилами →
                сравнение с trend честное.

    quant_gate=True (только для strategy='trend') — берём trend-setup ТОЛЬКО
    если quant_verdict согласен с направлением (confluence-фильтр). Поскольку
    quant — mean-reversion, а trend — следование, они почти всегда расходятся,
    и gate отсекает почти всё (это эмпирический вывод, не баг).

    Сделки 'pending' (горизонт упёрся в конец данных) отбрасываем.
    """
    if not candles_by_asset:
        return [], BacktestReport()

    max_len = max(len(c) for c in candles_by_asset.values())
    lo = max(WARMUP_BARS - 1, start_index if start_index is not None else 0)
    # Последний день, после которого ещё есть хотя бы 1 свеча для резолва.
    hi = (end_index if end_index is not None else max_len) - 1

    btc_candles = candles_by_asset.get("BTC")

    trades: list[BacktestTrade] = []
    for t in range(lo, hi):
        prices = _build_prices_at(candles_by_asset, t)
        if not prices:
            continue
        btc_closes = None
        if (quant_gate or strategy == "quant") and btc_candles and t < len(btc_candles):
            btc_closes = [c.close for c in btc_candles[: t + 1]]

        # ── собираем сетапы дня по выбранной стратегии ──
        setups: list[_Setup] = []
        if strategy in ("always_short", "always_long"):
            # Наивный направленный бейзлайн — для alpha-vs-beta теста.
            # Открывает фикс-направление КАЖДЫЙ день по каждому активу с σ̂.
            # Если quant SHORT не бьёт always_short — у quant нет альфы, только бета.
            fixed_dir = "SHORT" if strategy == "always_short" else "LONG"
            for asset, p in prices.items():
                s = _make_quant_setup(asset, fixed_dir, p.get("price"), p.get("vol_sigma_1d_pct"))
                if s is not None:
                    setups.append(s)
        elif strategy == "quant":
            for asset, p in prices.items():
                closes = [c.close for c in candles_by_asset[asset][: t + 1]]
                qv = quant_verdict(closes, btc_closes)
                direction = qv.get("verdict")
                if direction not in ("LONG", "SHORT"):
                    continue
                s = _make_quant_setup(asset, direction, p.get("price"), p.get("vol_sigma_1d_pct"))
                if s is not None:
                    s.score = int(qv.get("confidence", 0))
                    setups.append(s)
        else:
            result = rank_signals(prices, capital=capital, min_score=min_score)
            for setup in result["tradable_setups"]:
                if quant_gate:
                    closes = [c.close for c in candles_by_asset[setup.asset][: t + 1]]
                    if quant_verdict(closes, btc_closes).get("verdict") != setup.direction:
                        continue
                setups.append(_Setup(
                    setup.asset, setup.direction, setup.entry, setup.stop,
                    setup.target, setup.rr_ratio, setup.score,
                ))

        # ── резолвим (общий путь для обеих стратегий) ──
        for setup in setups:
            candles = candles_by_asset.get(setup.asset)
            if not candles or t >= len(candles):
                continue
            emitted_dt = candles[t].timestamp
            future = candles[t + 1:]
            if not future:
                continue
            status, exit_price, pnl_pct, exit_at = resolve_against_candles(
                setup.direction, setup.entry, setup.target, setup.stop,
                future, emitted_dt, horizon_hours,
            )
            if status == "pending":
                continue
            trades.append(BacktestTrade(
                asset=setup.asset,
                direction=setup.direction,
                score=setup.score,
                entry=setup.entry,
                stop=setup.stop,
                target=setup.target,
                rr_ratio=setup.rr_ratio,
                day_index=t,
                emitted_at=emitted_dt.isoformat(),
                status=status,
                pnl_pct=pnl_pct if pnl_pct is not None else 0.0,
                exit_at=exit_at,
            ))

    report = summarize(trades, params={
        "strategy": strategy,
        "min_score": min_score,
        "horizon_hours": horizon_hours,
        "capital": capital,
        "warmup_bars": WARMUP_BARS,
        "quant_gate": quant_gate,
        "note": "edge измерен без live-модификаторов score (funding/L-S/whale/news)",
    })
    return trades, report


DERIVS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "backtest_derivs",
)
FUNDING_THRESHOLD = 0.0001  # как в боте (signals.FUNDING_THRESHOLD = 0.01%)


def load_funding(asset: str, data_dir: str = DERIVS_DIR) -> dict:
    """Читает {date: funding_mean} из снапшота. {} если файла нет."""
    path = os.path.join(data_dir, f"{asset}_funding.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_funding_backtest(
    candles_by_asset: dict[str, list[Candle]],
    funding_by_asset: dict[str, dict],
    *,
    mode: str = "follow",
    threshold: float = FUNDING_THRESHOLD,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    start_index: int | None = None,
    end_index: int | None = None,
) -> tuple[list[BacktestTrade], BacktestReport]:
    """Бэктест funding-сигнала на истории.

    mode='follow'  — логика бота: funding>0 (лонги платят) → LONG, funding<0 → SHORT.
    mode='contra'  — классический edge: экстремум funding фейдим (перегруженная
                     толпа). funding>0 → SHORT, funding<0 → LONG.

    Сигнал срабатывает при |funding| >= threshold. SL/TP — та же σ̂-разметка,
    что и в quant/trend (честное сравнение). funding на день T берётся как
    среднее за этот день (из снапшота), без look-ahead.
    """
    if not candles_by_asset:
        return [], BacktestReport()
    max_len = max(len(c) for c in candles_by_asset.values())
    lo = max(WARMUP_BARS - 1, start_index if start_index is not None else 0)
    hi = (end_index if end_index is not None else max_len) - 1

    trades: list[BacktestTrade] = []
    for t in range(lo, hi):
        prices = _build_prices_at(candles_by_asset, t)
        if not prices:
            continue
        for asset, p in prices.items():
            fmap = funding_by_asset.get(asset)
            if not fmap:
                continue
            candles = candles_by_asset[asset]
            date = candles[t].timestamp.strftime("%Y-%m-%d")
            f = fmap.get(date)
            if f is None or abs(f) < threshold:
                continue
            if mode == "follow":
                direction = "LONG" if f > 0 else "SHORT"
            else:  # contra
                direction = "SHORT" if f > 0 else "LONG"
            s = _make_quant_setup(asset, direction, p.get("price"), p.get("vol_sigma_1d_pct"))
            if s is None:
                continue
            future = candles[t + 1:]
            if not future:
                continue
            status, _, pnl_pct, exit_at = resolve_against_candles(
                s.direction, s.entry, s.target, s.stop,
                future, candles[t].timestamp, horizon_hours,
            )
            if status == "pending":
                continue
            trades.append(BacktestTrade(
                asset=asset, direction=direction,
                score=int(round(abs(f) * 1e6)),  # |funding| в условных ед. для бакетов
                entry=s.entry, stop=s.stop, target=s.target, rr_ratio=s.rr_ratio,
                day_index=t, emitted_at=candles[t].timestamp.isoformat(),
                status=status, pnl_pct=pnl_pct if pnl_pct is not None else 0.0, exit_at=exit_at,
            ))

    report = summarize(trades, params={
        "strategy": f"funding_{mode}",
        "threshold": threshold,
        "horizon_hours": horizon_hours,
        "note": "funding из Binance Vision дампов; SL/TP по σ̂",
    })
    return trades, report


def load_metrics(asset: str, data_dir: str = DERIVS_DIR) -> dict:
    """Читает {date: {oi, ls}} из снапшота metrics. {} если файла нет."""
    path = os.path.join(data_dir, f"{asset}_metrics.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Пороги top-trader L/S как в боте (market_indicators: 1.7/0.6 magnet, 1.5/0.7 smart_money).
LS_LONG_HEAVY = 1.3   # ratio>1.3 → толпа в лонге (~57% long)
LS_SHORT_HEAVY = 0.77  # ratio<0.77 → толпа в шорте
OI_BUILDUP_PCT = 10.0  # рост OI за окно для магнита (DEFAULT_OI_BUILDUP_PCT)


def run_ls_backtest(
    candles_by_asset: dict[str, list[Candle]],
    metrics_by_asset: dict[str, dict],
    *,
    mode: str = "follow",
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    oi_lookback_days: int = 1,
) -> tuple[list[BacktestTrade], BacktestReport]:
    """Бэктест top-trader L/S сигнала.

    mode='follow' — логика бота: толпа в лонге (ratio>1.3) → LONG, в шорте → SHORT.
    mode='contra' — фейдим толпу: ratio>1.3 → SHORT, ratio<0.77 → LONG.
    mode='oi_magnet' — контрарианский магнит ликвидаций бота: ТОЛЬКО когда L/S
        экстремален И OI вырос (lookback) → ставим против перегруженной стороны.

    ls = sum_toptrader_long_short_ratio (position ratio, как в боте).
    """
    if not candles_by_asset:
        return [], BacktestReport()
    max_len = max(len(c) for c in candles_by_asset.values())
    lo = max(WARMUP_BARS - 1, 0)
    hi = max_len - 1

    trades: list[BacktestTrade] = []
    for t in range(lo, hi):
        prices = _build_prices_at(candles_by_asset, t)
        if not prices:
            continue
        for asset, p in prices.items():
            mmap = metrics_by_asset.get(asset)
            if not mmap:
                continue
            candles = candles_by_asset[asset]
            date = candles[t].timestamp.strftime("%Y-%m-%d")
            rec = mmap.get(date)
            if not rec:
                continue
            ls = rec.get("ls")
            if ls is None:
                continue

            direction = None
            if mode == "oi_magnet":
                # нужен рост OI за lookback
                prev_date = candles[t - oi_lookback_days].timestamp.strftime("%Y-%m-%d") if t - oi_lookback_days >= 0 else None
                prev = mmap.get(prev_date) if prev_date else None
                if not prev or not prev.get("oi"):
                    continue
                oi_change = (rec["oi"] - prev["oi"]) / prev["oi"] * 100.0
                if oi_change < OI_BUILDUP_PCT:
                    continue
                # перегруженные лонги + рост OI → DOWN_MAGNET (SHORT); наоборот → LONG
                if ls >= 1.7:
                    direction = "SHORT"
                elif ls <= 0.6:
                    direction = "LONG"
                else:
                    continue
            else:
                if ls >= LS_LONG_HEAVY:
                    crowd = "LONG"
                elif ls <= LS_SHORT_HEAVY:
                    crowd = "SHORT"
                else:
                    continue
                if mode == "follow":
                    direction = crowd
                else:  # contra
                    direction = "SHORT" if crowd == "LONG" else "LONG"

            s = _make_quant_setup(asset, direction, p.get("price"), p.get("vol_sigma_1d_pct"))
            if s is None:
                continue
            future = candles[t + 1:]
            if not future:
                continue
            status, _, pnl_pct, exit_at = resolve_against_candles(
                s.direction, s.entry, s.target, s.stop,
                future, candles[t].timestamp, horizon_hours,
            )
            if status == "pending":
                continue
            trades.append(BacktestTrade(
                asset=asset, direction=direction, score=int(round(ls * 100)),
                entry=s.entry, stop=s.stop, target=s.target, rr_ratio=s.rr_ratio,
                day_index=t, emitted_at=candles[t].timestamp.isoformat(),
                status=status, pnl_pct=pnl_pct if pnl_pct is not None else 0.0, exit_at=exit_at,
            ))

    report = summarize(trades, params={
        "strategy": f"ls_{mode}", "horizon_hours": horizon_hours,
        "note": "top-trader L/S из Binance Vision metrics; SL/TP по σ̂",
    })
    return trades, report


def run_oi_divergence_backtest(
    candles_by_asset: dict[str, list[Candle]],
    metrics_by_asset: dict[str, dict],
    *,
    mode: str = "trend",
    lookback_days: int = 7,
    oi_min_pct: float = 5.0,
    price_min_pct: float = 2.0,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> tuple[list[BacktestTrade], BacktestReport]:
    """Бэктест OI×price divergence (связка открытого интереса и направления).

    Классическая интерпретация по квадрантам за lookback:
      OI↑ & price↑ — новые деньги в лонг, тренд подтверждён → продолжение LONG
      OI↑ & price↓ — новые деньги в шорт, тренд подтверждён → продолжение SHORT
      OI↓ & price↑ — шорт-сквиз (закрытие шортов), ралли без топлива → слабость
      OI↓ & price↓ — лонг-ликвидация, падение без топлива → слабость

    mode='trend'  — торгуем подтверждённый тренд (OI↑): следуем направлению цены.
    mode='fade'   — фейдим слабое движение (OI↓): против направления цены.
    Берём только когда |ΔOI|>=oi_min_pct И |Δprice|>=price_min_pct. SL/TP по σ̂.
    """
    if not candles_by_asset:
        return [], BacktestReport()
    max_len = max(len(c) for c in candles_by_asset.values())
    lo = max(WARMUP_BARS - 1, lookback_days)
    hi = max_len - 1

    trades: list[BacktestTrade] = []
    for t in range(lo, hi):
        prices = _build_prices_at(candles_by_asset, t)
        if not prices:
            continue
        for asset, p in prices.items():
            mmap = metrics_by_asset.get(asset)
            if not mmap:
                continue
            candles = candles_by_asset[asset]
            date = candles[t].timestamp.strftime("%Y-%m-%d")
            prev_date = candles[t - lookback_days].timestamp.strftime("%Y-%m-%d")
            rec, prev = mmap.get(date), mmap.get(prev_date)
            if not rec or not prev or not prev.get("oi") or not rec.get("oi"):
                continue
            oi_change = (rec["oi"] - prev["oi"]) / prev["oi"] * 100.0
            price_now = candles[t].close
            price_prev = candles[t - lookback_days].close
            if not price_prev:
                continue
            price_change = (price_now - price_prev) / price_prev * 100.0
            if abs(oi_change) < oi_min_pct or abs(price_change) < price_min_pct:
                continue

            oi_up = oi_change > 0
            price_up = price_change > 0
            if mode == "trend":
                if not oi_up:           # тренд торгуем только при росте OI
                    continue
                direction = "LONG" if price_up else "SHORT"
            else:  # fade — только при падении OI (движение без топлива)
                if oi_up:
                    continue
                direction = "SHORT" if price_up else "LONG"

            s = _make_quant_setup(asset, direction, p.get("price"), p.get("vol_sigma_1d_pct"))
            if s is None:
                continue
            future = candles[t + 1:]
            if not future:
                continue
            status, _, pnl_pct, exit_at = resolve_against_candles(
                s.direction, s.entry, s.target, s.stop,
                future, candles[t].timestamp, horizon_hours,
            )
            if status == "pending":
                continue
            trades.append(BacktestTrade(
                asset=asset, direction=direction, score=int(round(oi_change)),
                entry=s.entry, stop=s.stop, target=s.target, rr_ratio=s.rr_ratio,
                day_index=t, emitted_at=candles[t].timestamp.isoformat(),
                status=status, pnl_pct=pnl_pct if pnl_pct is not None else 0.0, exit_at=exit_at,
            ))

    report = summarize(trades, params={
        "strategy": f"oi_div_{mode}", "lookback_days": lookback_days,
        "horizon_hours": horizon_hours, "note": "OI×price divergence; SL/TP по σ̂",
    })
    return trades, report


def run_carry_backtest(
    funding_by_asset: dict[str, dict],
    *,
    mode: str = "always",
    hold_days: int = 14,
    step_days: int = 14,
    fundings_per_day: int = 3,
    fee_per_leg: float = 0.0005,
    entry_threshold_annual: float = 0.0,
) -> tuple[list[dict], dict]:
    """Дельта-нейтральный funding carry: лонг-спот + шорт-перп, собираем фандинг.

    Рыночный риск захеджирован (ноги гасятся) → PnL ≈ Σ собранного фандинга − косты.
    Шорт-перп получает фандинг когда funding>0 (структурно ~70% времени в крипте).

    Хранимый funding = средний rate за день (за интервал). Дневной сбор шорт-перпа
    = fundings_per_day × rate (3 интервала/день). Косты = 4 ноги (вход спот+перп,
    выход спот+перп) × fee_per_leg.

    mode='always'    — держим всегда, платим когда funding<0 (чистая премия за цикл).
    mode='selective' — входим ТОЛЬКО когда аннуализированный funding на входе
                       выше entry_threshold_annual (избегаем отрицательных периодов).

    Возвращает (сделки, агрегаты). Сделка = один holding-период по одному активу.
    """
    cost = 4 * fee_per_leg  # round-trip 2 ноги × 2 стороны
    trades = []
    for asset, fmap in funding_by_asset.items():
        dates = sorted(fmap.keys())
        for i in range(0, len(dates) - hold_days, step_days):
            window = dates[i:i + hold_days]
            rates = [fmap[d] for d in window if fmap.get(d) is not None]
            if len(rates) < hold_days * 0.7:  # пропускаем дырявые окна
                continue
            entry_rate = rates[0]
            if mode == "selective":
                entry_annual = entry_rate * fundings_per_day * 365
                if entry_annual <= entry_threshold_annual:
                    continue
            collected = sum(r * fundings_per_day for r in rates)  # шорт-перп сбор
            pnl_pct = (collected - cost) * 100.0
            # аннуализируем для контекста
            ann = (collected - cost) * (365.0 / hold_days) * 100.0
            trades.append({
                "asset": asset, "date": window[0], "pnl_pct": pnl_pct,
                "annual_pct": ann, "entry_funding": entry_rate,
            })

    pnls = [t["pnl_pct"] for t in trades]
    anns = [t["annual_pct"] for t in trades]
    agg = {
        "n": len(trades), "mode": mode, "hold_days": hold_days,
        "mean_pct_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "mean_annual_pct": round(sum(anns) / len(anns), 2) if anns else 0.0,
    }
    return trades, agg


def run_carry_dynamic_backtest(
    funding_by_asset: dict[str, dict],
    *,
    entry_threshold_annual: float = 0.10,
    exit_threshold_annual: float = 0.0,
    fundings_per_day: int = 3,
    fee_per_leg: float = 0.0005,
    max_hold_days: int = 365,
    min_hold_days: int = 1,
) -> tuple[list[dict], dict]:
    """Carry с ДИНАМИЧЕСКИМ выходом — не фикс-холд, а удержание по состоянию фандинга.

    Вход: когда аннуализированный funding дня >= entry_threshold_annual (жирный).
    Удержание: пока funding остаётся выше exit_threshold_annual (по умолчанию 0 →
        держим, пока фандинг положительный; выходим как только нормализуется/
        переворачивается). Косты платятся ОДИН раз за позицию (вход+выход = 4 ноги),
        поэтому длинные жирные стрики (2021) амортизируют косты, а в медведе (2022)
        выходим быстро и не платим отрицательный фандинг.

    Одна позиция на актив за раз; после выхода ищем следующий вход вперёд.
    max_hold_days — кап на длину (риск-менеджмент). min_hold_days — отсечь дребезг.
    Возвращает (сделки, агрегаты). Сделка = один цикл вход→выход по одному активу.
    """
    cost = 4 * fee_per_leg  # round-trip: 2 ноги × 2 стороны, ОДИН раз за позицию
    ann_k = fundings_per_day * 365.0  # rate → annualized
    trades = []
    for asset, fmap in funding_by_asset.items():
        dates = sorted(fmap.keys())
        i = 0
        n = len(dates)
        while i < n:
            r0 = fmap.get(dates[i])
            if r0 is None or r0 * ann_k < entry_threshold_annual:
                i += 1
                continue
            # вход — собираем, пока фандинг выше порога выхода или до капа
            collected = 0.0
            held = 0
            j = i
            while j < n and held < max_hold_days:
                r = fmap.get(dates[j])
                if r is None:
                    j += 1
                    continue
                # выход когда фандинг нормализовался (но не раньше min_hold)
                if held >= min_hold_days and r * ann_k < exit_threshold_annual:
                    break
                collected += r * fundings_per_day
                held += 1
                j += 1
            if held < min_hold_days:
                i += 1
                continue
            pnl_pct = (collected - cost) * 100.0
            ann = (collected - cost) * (365.0 / held) * 100.0
            trades.append({
                "asset": asset, "date": dates[i], "pnl_pct": pnl_pct,
                "annual_pct": ann, "hold_days": held, "entry_funding": r0,
            })
            i = j + 1  # следующий вход после выхода (без перекрытия)

    pnls = [t["pnl_pct"] for t in trades]
    holds = [t["hold_days"] for t in trades]
    total_days = sum(holds)
    # ЧЕСТНАЯ аннуализация — портфельная (доход на день-в-рынке × 365). Среднее
    # per-trade annual_pct ВРЁТ при переменном холде: 365/held взрывает короткие
    # позиции в дикие выбросы. Считаем total_pnl / total_days_in_market.
    agg = {
        "n": len(trades), "mode": "dynamic",
        "entry_threshold_annual": entry_threshold_annual,
        "exit_threshold_annual": exit_threshold_annual,
        "mean_pct_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "annual_pct_per_day_in_market": round(sum(pnls) / total_days * 365.0, 2) if total_days else 0.0,
        "mean_hold_days": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "total_pnl_pct": round(sum(pnls), 2),
    }
    return trades, agg


def run_xsection_backtest(
    candles_by_asset: dict[str, list[Candle]],
    *,
    signal: str = "mom",
    metrics_by_asset: dict[str, dict] | None = None,
    lookback_days: int = 30,
    hold_days: int = 14,
    top_k: int = 1,
    fee_pct: float = 0.001,
    step_days: int = 1,
) -> tuple[list[dict], dict]:
    """Рыночно-НЕЙТРАЛЬНЫЙ cross-sectional бэктест (long/short спред).

    Каждый день: считаем сигнал по каждому активу, ранжируем, ЛОНГ top_k +
    ШОРТ bottom_k (равный объём → бета рынка вычитается). PnL дня = средний
    forward-return лонг-ноги МИНУС шорт-ноги (close-to-close за hold_days),
    минус косты (4 стороны: вход+выход × 2 ноги).

    signal:
      'mom'    — momentum: past lookback-return (лонг победителей).
      'rev'    — reversal: наоборот (лонг лузеров).
      'ls_low' — лонг наименее перегруженного (низкий L/S), шорт наиболее.
      'ls_high'— наоборот (лонг crowded).
      'taker_high' — лонг с макс агрессивным buy-flow, шорт с мин (follow flow).
      'taker_low'  — наоборот (fade flow).
    Убирает рыночную бету → изолирует ОТНОСИТЕЛЬНЫЙ edge сигнала.
    Возвращает (список дневных сделок-спредов, агрегаты).
    """
    # date -> {asset: close}
    closes_by_date: dict[str, dict] = {}
    for a, cs in candles_by_asset.items():
        for c in cs:
            closes_by_date.setdefault(c.timestamp.strftime("%Y-%m-%d"), {})[a] = c.close
    dates = sorted(closes_by_date.keys())
    idx = {d: i for i, d in enumerate(dates)}

    use_ls = signal in ("ls_low", "ls_high")
    use_taker = signal in ("taker_high", "taker_low")
    cost = fee_pct * 2 * 2  # 2 стороны × 2 ноги

    def _sig(asset, d) -> float | None:
        i = idx[d]
        if use_ls or use_taker:
            m = (metrics_by_asset or {}).get(asset) or {}
            rec = m.get(d)
            if not rec:
                return None
            return rec.get("taker") if use_taker else rec.get("ls")
        # momentum/reversal: return за lookback
        if i - lookback_days < 0:
            return None
        prev = closes_by_date[dates[i - lookback_days]].get(asset)
        cur = closes_by_date[d].get(asset)
        if prev and cur and prev > 0:
            return (cur - prev) / prev
        return None

    trades = []
    for i in range(0, len(dates), step_days):
        d = dates[i]
        if i + hold_days >= len(dates):
            break
        fwd_date = dates[i + hold_days]
        # активы с сигналом И forward-ценой
        scored = []
        for a in candles_by_asset:
            sg = _sig(a, d)
            entry = closes_by_date[d].get(a)
            exit_ = closes_by_date[fwd_date].get(a)
            if sg is None or not entry or not exit_:
                continue
            fwd_ret = (exit_ - entry) / entry
            scored.append((a, sg, fwd_ret))
        if len(scored) < 2 * top_k:
            continue
        scored.sort(key=lambda x: x[1])  # по сигналу возрастание
        if signal in ("mom", "ls_high", "taker_high"):
            longs = scored[-top_k:]   # высокий сигнал → long
            shorts = scored[:top_k]
        else:  # rev, ls_low, taker_low → low signal → long
            longs = scored[:top_k]
            shorts = scored[-top_k:]
        long_ret = sum(x[2] for x in longs) / len(longs)
        short_ret = sum(x[2] for x in shorts) / len(shorts)
        spread = (long_ret - short_ret) - cost
        trades.append({
            "date": d, "pnl_pct": spread * 100,
            "longs": [x[0] for x in longs], "shorts": [x[0] for x in shorts],
        })

    pnls = [t["pnl_pct"] for t in trades]
    agg = {
        "n": len(trades),
        "mean_pct": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "signal": signal, "lookback_days": lookback_days, "hold_days": hold_days, "top_k": top_k,
    }
    return trades, agg


def _rolling_pct_signals(metrics: dict, window: int, top_pct: float, min_window: int) -> dict:
    """date -> 'SHORT'|'LONG'|None по СКОЛЬЗЯЩЕМУ перцентилю L/S (стационарно).

    Для каждого дня: ранг сегодняшнего L/S в трейлинг-окне `window`. L/S в топ-
    `top_pct` (толпа аномально в лонге ОТНОСИТЕЛЬНО недавнего) → SHORT (фейд);
    в нижних top_pct → LONG. Это переносится между эпохами (в отличие от
    абсолютного порога, который сломался на bull-тесте: шкала L/S дрейфует).
    """
    dates = sorted(metrics.keys())
    ls = [metrics[d].get("ls") for d in dates]
    out = {}
    for i, d in enumerate(dates):
        if i < min_window or ls[i] is None:
            continue
        lo = max(0, i - window + 1)
        win = [x for x in ls[lo:i + 1] if x is not None]
        if len(win) < min_window:
            continue
        rank = sum(1 for x in win if x <= ls[i]) / len(win)
        if rank >= 1.0 - top_pct:
            out[d] = "SHORT"
        elif rank <= top_pct:
            out[d] = "LONG"
    return out


def run_contrarian_pct_backtest(
    candles_by_asset: dict[str, list[Candle]],
    metrics_by_asset: dict[str, dict],
    *,
    window: int = 90,
    top_pct: float = 0.10,
    min_window: int = 30,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> tuple[list[BacktestTrade], BacktestReport]:
    """Contrarian L/S со СКОЛЬЗЯЩИМ ПЕРЦЕНТИЛЕМ (стационарный порог).

    Фейдим толпу, когда её перекос аномален ОТНОСИТЕЛЬНО недавней истории
    самого актива, а не по фикс-числу. Решающий тест: переносится ли edge на
    bull 2021 (фикс-порог 1.6 туда не переносился — провалил)."""
    if not candles_by_asset:
        return [], BacktestReport()
    sig_by_asset = {a: _rolling_pct_signals(m, window, top_pct, min_window)
                    for a, m in metrics_by_asset.items()}
    max_len = max(len(c) for c in candles_by_asset.values())
    lo = max(WARMUP_BARS - 1, 0)
    hi = max_len - 1

    trades: list[BacktestTrade] = []
    for t in range(lo, hi):
        prices = _build_prices_at(candles_by_asset, t)
        if not prices:
            continue
        for asset, p in prices.items():
            sigs = sig_by_asset.get(asset)
            if not sigs:
                continue
            candles = candles_by_asset[asset]
            date = candles[t].timestamp.strftime("%Y-%m-%d")
            direction = sigs.get(date)
            if direction not in ("LONG", "SHORT"):
                continue
            s = _make_quant_setup(asset, direction, p.get("price"), p.get("vol_sigma_1d_pct"))
            if s is None:
                continue
            future = candles[t + 1:]
            if not future:
                continue
            status, _, pnl_pct, exit_at = resolve_against_candles(
                s.direction, s.entry, s.target, s.stop,
                future, candles[t].timestamp, horizon_hours,
            )
            if status == "pending":
                continue
            trades.append(BacktestTrade(
                asset=asset, direction=direction, score=0,
                entry=s.entry, stop=s.stop, target=s.target, rr_ratio=s.rr_ratio,
                day_index=t, emitted_at=candles[t].timestamp.isoformat(),
                status=status, pnl_pct=pnl_pct if pnl_pct is not None else 0.0, exit_at=exit_at,
            ))

    report = summarize(trades, params={
        "strategy": "contrarian_pct", "window": window, "top_pct": top_pct,
        "horizon_hours": horizon_hours, "note": "rolling-percentile L/S fade (стационарный)",
    })
    return trades, report


def run_contrarian_backtest(
    candles_by_asset: dict[str, list[Candle]],
    funding_by_asset: dict[str, dict],
    metrics_by_asset: dict[str, dict],
    *,
    use_funding: bool = True,
    use_ls: bool = True,
    require_confluence: bool = False,
    ls_long_heavy: float = LS_LONG_HEAVY,
    ls_short_heavy: float = LS_SHORT_HEAVY,
    funding_threshold: float = FUNDING_THRESHOLD,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
) -> tuple[list[BacktestTrade], BacktestReport]:
    """Комбинированная contrarian-стратегия: фейдим толпу по L/S + funding.

    Каждый сигнал контрарианский (против перегруженной стороны):
      funding>0 (лонги платят) → SHORT; funding<0 → LONG.
      L/S>long_heavy (толпа в лонге) → SHORT; L/S<short_heavy → LONG.

    require_confluence=True — берём ТОЛЬКО когда оба сигнала есть и согласны
    (фильтр качества). False — берём если сигналы не конфликтуют (хотя бы один,
    при конфликте skip). SL/TP — σ̂-разметка. Без look-ahead (день T).
    """
    if not candles_by_asset:
        return [], BacktestReport()
    max_len = max(len(c) for c in candles_by_asset.values())
    lo = max(WARMUP_BARS - 1, 0)
    hi = max_len - 1

    trades: list[BacktestTrade] = []
    for t in range(lo, hi):
        prices = _build_prices_at(candles_by_asset, t)
        if not prices:
            continue
        for asset, p in prices.items():
            candles = candles_by_asset[asset]
            date = candles[t].timestamp.strftime("%Y-%m-%d")

            votes = []  # contrarian directions
            if use_funding:
                fmap = funding_by_asset.get(asset) or {}
                f = fmap.get(date)
                if f is not None and abs(f) >= funding_threshold:
                    votes.append("SHORT" if f > 0 else "LONG")
            if use_ls:
                mmap = metrics_by_asset.get(asset) or {}
                rec = mmap.get(date)
                ls = rec.get("ls") if rec else None
                if ls is not None:
                    if ls >= ls_long_heavy:
                        votes.append("SHORT")
                    elif ls <= ls_short_heavy:
                        votes.append("LONG")

            if not votes:
                continue
            longs = votes.count("LONG")
            shorts = votes.count("SHORT")
            if longs and shorts:
                continue  # конфликт сигналов — skip
            if require_confluence and len(votes) < 2:
                continue  # нужно подтверждение обоих
            direction = "LONG" if longs else "SHORT"

            s = _make_quant_setup(asset, direction, p.get("price"), p.get("vol_sigma_1d_pct"))
            if s is None:
                continue
            future = candles[t + 1:]
            if not future:
                continue
            status, _, pnl_pct, exit_at = resolve_against_candles(
                s.direction, s.entry, s.target, s.stop,
                future, candles[t].timestamp, horizon_hours,
            )
            if status == "pending":
                continue
            trades.append(BacktestTrade(
                asset=asset, direction=direction, score=len(votes),
                entry=s.entry, stop=s.stop, target=s.target, rr_ratio=s.rr_ratio,
                day_index=t, emitted_at=candles[t].timestamp.isoformat(),
                status=status, pnl_pct=pnl_pct if pnl_pct is not None else 0.0, exit_at=exit_at,
            ))

    report = summarize(trades, params={
        "strategy": "contrarian",
        "use_funding": use_funding, "use_ls": use_ls,
        "require_confluence": require_confluence,
        "ls_long_heavy": ls_long_heavy, "ls_short_heavy": ls_short_heavy,
        "horizon_hours": horizon_hours,
        "note": "contrarian fade толпы (L/S+funding инвертированы); SL/TP по σ̂",
    })
    return trades, report


def summarize(trades: list[BacktestTrade], *, params: dict | None = None) -> BacktestReport:
    """Считает агрегаты. Сделки уже без 'pending'.

    Equity-кривая строится в порядке входа (day_index, затем asset) — для
    стабильного max drawdown при детерминированном прогоне.
    """
    report = BacktestReport(params=params or {})
    if not trades:
        return report

    ordered = sorted(trades, key=lambda x: (x.day_index, x.asset, x.direction))
    pnls = [x.pnl_pct for x in ordered]

    wins_pnl = [p for p in pnls if p > 0]
    losses_pnl = [p for p in pnls if p <= 0]

    report.n_trades = len(trades)
    report.n_resolved = len(trades)
    report.wins = len(wins_pnl)
    report.losses = len(losses_pnl)
    report.win_rate = 100.0 * report.wins / report.n_trades
    report.total_pnl_pct = sum(pnls)
    report.expectancy_pct = report.total_pnl_pct / report.n_trades
    report.avg_win_pct = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0.0
    report.avg_loss_pct = sum(losses_pnl) / len(losses_pnl) if losses_pnl else 0.0
    report.payoff = (
        abs(report.avg_win_pct / report.avg_loss_pct) if report.avg_loss_pct else 0.0
    )
    gross_win = sum(wins_pnl)
    gross_loss = abs(sum(losses_pnl))
    report.profit_factor = (gross_win / gross_loss) if gross_loss else 0.0

    # Max drawdown по кумулятивной equity (в пунктах PnL %).
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    report.max_drawdown_pct = max_dd

    # Разбивки.
    def _bucketize(key_fn) -> dict:
        agg: dict = {}
        for x in ordered:
            k = key_fn(x)
            b = agg.setdefault(k, {"n": 0, "wins": 0, "sum_pnl": 0.0})
            b["n"] += 1
            b["wins"] += 1 if x.pnl_pct > 0 else 0
            b["sum_pnl"] += x.pnl_pct
        for k, b in agg.items():
            b["win_rate"] = round(100.0 * b["wins"] / b["n"], 2)
            b["expectancy_pct"] = round(b["sum_pnl"] / b["n"], 4)
            b["sum_pnl"] = round(b["sum_pnl"], 4)
        return agg

    report.by_asset = _bucketize(lambda x: x.asset)
    report.by_score_bucket = _bucketize(lambda x: _score_bucket(x.score))
    report.by_direction = _bucketize(lambda x: x.direction)
    # год входа из ISO-ts (emitted_at = '2023-05-01T...') — режим рынка.
    report.by_year = _bucketize(lambda x: x.emitted_at[:4])
    return report


def load_all_candles(assets=None, data_dir: str = DATA_DIR) -> dict[str, list[Candle]]:
    """Загружает снапшоты по списку активов (по умолчанию TRADABLE_ASSETS)."""
    assets = assets or sorted(TRADABLE_ASSETS)
    out: dict[str, list[Candle]] = {}
    for a in assets:
        c = load_candles(a, data_dir=data_dir)
        if c:
            out[a] = c
    return out
