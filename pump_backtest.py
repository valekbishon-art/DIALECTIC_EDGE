"""
pump_backtest.py — бэктест памп-детектора на исторических 1m-свечах.

Ключевая идея: бэктест использует ТУ ЖЕ функцию evaluate_pump из pump_scanner,
что и боевой скан — значит результаты теста 1-в-1 отражают прод-логику.

Что делает:
  - идёт walk-forward по 1m-серии каждой монеты (без заглядывания в будущее);
  - на каждом шаге собирает трейлинг-окно + суточный объём + дневные закрытия и
    зовёт evaluate_pump; на 'pump' (и опц. 'early') фиксирует сигнал;
  - считает forward-доходность на +1ч/+4ч/+24ч, MFE (max favorable) и MAE
    (max adverse) после входа;
  - агрегирует: число сигналов, win-rate, средняя/медианная доходность, MFE/MAE.

Данные (без сети — сеть тут не нужна):
  --data-dir DIR : папка с CSV по монетам. Формат CSV (с заголовком или без):
        timestamp,close,volume   (timestamp опционален)
     Имя файла = тикер (BASE), например DOGE.csv
  --demo         : синтетические монеты с вшитыми пампами — доказательство
                   работоспособности пайплайна прямо здесь.

Ограничения честно: глубокую 1m-историю за 2020-2026 по ВСЕМ монетам бесплатно
не достать (биржи лимитируют окно; многие монеты тогда не существовали —
survivorship bias). Качать историю (Binance Data dumps / Tardis и т.п.) и
кладёшь в --data-dir; сам прогон — там где есть данные.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import random
import statistics
from dataclasses import dataclass, field
from typing import Optional

from pump_scanner import (
    MINUTES_PER_DAY,
    PumpConfig,
    classify_signal,
    evaluate_pump,
)

logger = logging.getLogger(__name__)

DEFAULT_HORIZONS_MIN = (60, 240, 1440)  # +1ч, +4ч, +24ч


@dataclass
class BacktestTrade:
    asset: str
    index: int                 # индекс свечи где сработал сигнал
    tier: str
    entry_price: float
    pump_pct: float
    vol_ratio: float
    predictive_score: float
    fwd_returns: dict = field(default_factory=dict)   # horizon_min -> %
    mfe_pct: dict = field(default_factory=dict)        # horizon_min -> %
    mae_pct: dict = field(default_factory=dict)        # horizon_min -> %


def forward_return(closes: list, i: int, horizon: int) -> Optional[float]:
    """% изменения цены через `horizon` свечей после индекса i. None если нет данных."""
    j = i + horizon
    if j >= len(closes) or i >= len(closes):
        return None
    base = closes[i]
    if base <= 0:
        return None
    return (closes[j] - base) / base * 100.0


def excursions(closes: list, i: int, horizon: int) -> tuple:
    """(MFE%, MAE%) — макс. рост и макс. просадка в окне (i, i+horizon]."""
    base = closes[i] if i < len(closes) else 0.0
    if base <= 0:
        return None, None
    end = min(i + horizon, len(closes) - 1)
    if end <= i:
        return None, None
    seg = closes[i + 1:end + 1]
    if not seg:
        return None, None
    hi = max(seg)
    lo = min(seg)
    return (hi - base) / base * 100.0, (lo - base) / base * 100.0


def _daily_closes_from_1m(closes: list, upto: int, prior_days: int) -> list:
    """Приблизительные дневные закрытия из 1m-серии до индекса upto (вкл.)."""
    out: list = []
    start = max(0, upto - prior_days * MINUTES_PER_DAY)
    k = start
    while k <= upto:
        out.append(closes[min(k, len(closes) - 1)])
        k += MINUTES_PER_DAY
    if not out or out[-1] != closes[upto]:
        out.append(closes[upto])
    return out


def backtest_series(
    asset: str,
    closes: list,
    vols: list,
    *,
    cfg: Optional[PumpConfig] = None,
    horizons=DEFAULT_HORIZONS_MIN,
    include_early: bool = False,
    cooldown_min: int = 60,
    step: int = 1,
) -> list:
    """Walk-forward по одной монете. Возвращает список BacktestTrade.

    Без заглядывания в будущее: сигнал в точке i использует только closes[:i+1];
    forward-доходности берут closes[i+1:].
    """
    cfg = cfg or PumpConfig()
    trades: list = []
    win = cfg.window_candles
    n = len(closes)
    start = max(win + 1, MINUTES_PER_DAY // 24)  # нужно немного истории
    last_fire = -10 ** 9
    i = start
    while i < n:
        if i - last_fire < cooldown_min:
            i += step
            continue
        window_closes = closes[: i + 1]
        window_vols = vols[: i + 1]
        vol_24h_total = sum(window_vols[-MINUTES_PER_DAY:])
        daily = _daily_closes_from_1m(closes, i, cfg.prior_days)
        is_pump, m, fails = evaluate_pump(
            window_closes, window_vols, vol_24h_total, daily,
            price=closes[i], mcap=None, cfg=cfg)
        tier = classify_signal(is_pump, m.predictive_score, fails,
                               early_threshold=cfg.early_threshold)
        fire = tier == "pump" or (include_early and tier == "early")
        if fire:
            tr = BacktestTrade(
                asset=asset, index=i, tier=tier, entry_price=closes[i],
                pump_pct=m.pump_pct, vol_ratio=m.vol_ratio,
                predictive_score=m.predictive_score)
            for h in horizons:
                fr = forward_return(closes, i, h)
                mfe, mae = excursions(closes, i, h)
                if fr is not None:
                    tr.fwd_returns[h] = fr
                if mfe is not None:
                    tr.mfe_pct[h] = mfe
                if mae is not None:
                    tr.mae_pct[h] = mae
            trades.append(tr)
            last_fire = i
        i += step
    return trades


def aggregate(trades: list, horizons=DEFAULT_HORIZONS_MIN) -> dict:
    """Сводная статистика по списку трейдов."""
    stats: dict = {
        "signals": len(trades),
        "by_tier": {},
        "horizons": {},
    }
    for t in trades:
        stats["by_tier"][t.tier] = stats["by_tier"].get(t.tier, 0) + 1
    for h in horizons:
        rets = [t.fwd_returns[h] for t in trades if h in t.fwd_returns]
        mfes = [t.mfe_pct[h] for t in trades if h in t.mfe_pct]
        maes = [t.mae_pct[h] for t in trades if h in t.mae_pct]
        if rets:
            wins = sum(1 for r in rets if r > 0)
            stats["horizons"][h] = {
                "n": len(rets),
                "win_rate": wins / len(rets),
                "avg_return": statistics.fmean(rets),
                "median_return": statistics.median(rets),
                "avg_mfe": statistics.fmean(mfes) if mfes else None,
                "avg_mae": statistics.fmean(maes) if maes else None,
            }
        else:
            stats["horizons"][h] = {"n": 0}
    return stats


def run_backtest_on_dir(
    data_dir: str,
    *,
    cfg: Optional[PumpConfig] = None,
    horizons=DEFAULT_HORIZONS_MIN,
    include_early: bool = False,
) -> dict:
    """Прогон по всем CSV в папке. Возвращает {'stats':..., 'trades':[...]}."""
    cfg = cfg or PumpConfig()
    all_trades: list = []
    files = [f for f in sorted(os.listdir(data_dir)) if f.lower().endswith(".csv")]
    for fn in files:
        asset = os.path.splitext(fn)[0].upper()
        closes, vols = load_candles_csv(os.path.join(data_dir, fn))
        if len(closes) < cfg.window_candles + 2:
            continue
        all_trades.extend(backtest_series(
            asset, closes, vols, cfg=cfg, horizons=horizons,
            include_early=include_early))
    return {"stats": aggregate(all_trades, horizons), "trades": all_trades}


def load_candles_csv(path: str) -> tuple:
    """Грузит CSV -> (closes, vols). Допускает колонки timestamp,close,volume или
    close,volume. Заголовок определяется автоматически."""
    closes: list = []
    vols: list = []
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return [], []
    start = 0
    # детект заголовка
    first = rows[0]
    try:
        float(first[-1])
    except (ValueError, IndexError):
        start = 1
    for r in rows[start:]:
        if not r:
            continue
        try:
            if len(r) >= 3:
                closes.append(float(r[-2]))
                vols.append(float(r[-1]))
            elif len(r) == 2:
                closes.append(float(r[0]))
                vols.append(float(r[1]))
        except ValueError:
            continue
    return closes, vols


# ───────────────────────────────── demo ─────────────────────────────────────
def _synth_series(n: int, *, base_price: float, seed: int,
                  inject_pump_at: Optional[int] = None,
                  pump_pct: float = 8.0, vol_spike: float = 6.0) -> tuple:
    """Генерит случайное блуждание + опционально вшитый памп на окне 30 свечей."""
    rnd = random.Random(seed)
    closes: list = []
    vols: list = []
    p = base_price
    base_vol = 100.0
    for k in range(n):
        p *= (1.0 + rnd.gauss(0, 0.0008))
        v = base_vol * (0.7 + rnd.random() * 0.6)
        if inject_pump_at is not None and inject_pump_at <= k < inject_pump_at + 30:
            frac = (k - inject_pump_at + 1) / 30.0
            p *= (1.0 + (pump_pct / 100.0) / 30.0)
            v = base_vol * vol_spike * (0.8 + frac)
        closes.append(p)
        vols.append(v)
    return closes, vols


def demo() -> dict:
    """Синтетический прогон: часть монет с вшитыми пампами, часть — шум."""
    cfg = PumpConfig()
    trades: list = []
    n = MINUTES_PER_DAY * 2  # 2 дня минуток
    pumped = 0
    for c in range(6):
        inject = (MINUTES_PER_DAY + 200 * c) if c < 4 else None
        closes, vols = _synth_series(
            n, base_price=0.5 + c, seed=1000 + c, inject_pump_at=inject,
            pump_pct=9.0, vol_spike=7.0)
        tr = backtest_series(f"DEMO{c}", closes, vols, cfg=cfg,
                             include_early=True)
        if inject is not None:
            pumped += 1
        trades.extend(tr)
    stats = aggregate(trades)
    stats["_demo_coins"] = 6
    stats["_demo_with_injected_pump"] = pumped
    return {"stats": stats, "trades": trades}


def _print_stats(stats: dict) -> None:
    print("=" * 56)
    print(f"Сигналов всего: {stats['signals']}  | по тирам: {stats['by_tier']}")
    if "_demo_coins" in stats:
        print(f"(demo: {stats['_demo_coins']} монет, "
              f"{stats['_demo_with_injected_pump']} с вшитым пампом)")
    for h, s in stats["horizons"].items():
        if s.get("n"):
            print(f"  +{h:>4}мин: n={s['n']:<4} win={s['win_rate']:.0%} "
                  f"avg={s['avg_return']:+.2f}% med={s['median_return']:+.2f}% "
                  f"MFE={s['avg_mfe']:+.2f}% MAE={s['avg_mae']:+.2f}%")
        else:
            print(f"  +{h:>4}мин: нет данных (серия короче горизонта)")
    print("=" * 56)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Бэктест памп-детектора")
    ap.add_argument("--data-dir", help="папка с CSV свечей (BASE.csv)")
    ap.add_argument("--demo", action="store_true", help="синтетический прогон без сети")
    ap.add_argument("--early", action="store_true", help="включить early-tier сигналы")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    if args.demo or not args.data_dir:
        if not args.demo and not args.data_dir:
            print("Нет --data-dir, запускаю --demo. Используй --data-dir DIR для реальных данных.")
        res = demo()
        _print_stats(res["stats"])
        return 0
    if not os.path.isdir(args.data_dir):
        print(f"Папка не найдена: {args.data_dir}")
        return 2
    res = run_backtest_on_dir(args.data_dir, include_early=args.early)
    _print_stats(res["stats"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
