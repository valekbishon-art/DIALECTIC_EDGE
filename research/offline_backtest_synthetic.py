"""Офлайн-бэктест НА СИНТЕТИЧЕСКИХ ДАННЫХ (сети нет → Yahoo недоступен).

Гоняем ТУ ЖЕ логику (run_edge / run_baseline / run_hodl / robustness из
`halal_edge_backtest.py`, которая зовёт `halal_edge.edge_signal`), но на
детерминированно сгенерированном полном цикле.
Это НЕ реальные исторические доходности — это проверка механики/устойчивости.
"""
import os, sys, math, random
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "research"))

import halal_edge_backtest as bt
from halal_edge import DEFAULT_CFG, ENHANCED_CFG, UNIVERSE

random.seed(42)

# ── генератор полного цикла (общий фактор + беты + идиосинкразия) ──
START = date(2020, 4, 1)
NDAYS = 2270  # до ~2026-06

def regime(d: date):
    if d < date(2021, 1, 1):  return 0.0015, 0.030   # прогрев
    if d < date(2022, 1, 1):  return 0.0042, 0.034    # бык 2021
    if d < date(2023, 1, 1):  return -0.0045, 0.050   # медведь 2022
    if d < date(2024, 1, 1):  return 0.0026, 0.034    # восстановление
    if d < date(2025, 1, 1):  return 0.0030, 0.040    # бык 2024
    return 0.0006, 0.045                              # боковик 2025-26

BETA = {"BTC":1.0,"ETH":1.1,"BNB":0.9,"SOL":1.6,"XRP":1.0,"ADA":1.2,
        "AVAX":1.7,"LINK":1.3,"DOT":1.2,"LTC":0.9}
IDIO = {"BTC":0.0,"ETH":0.018,"BNB":0.020,"SOL":0.030,"XRP":0.026,"ADA":0.024,
        "AVAX":0.032,"LINK":0.026,"DOT":0.024,"LTC":0.020}

days = [(START + timedelta(days=k)).isoformat() for k in range(NDAYS)]
factor = []          # общий рыночный log-return (= BTC)
for k in range(NDAYS):
    mu, sg = regime(START + timedelta(days=k))
    factor.append(random.gauss(mu, sg))

series = {}
for sym in UNIVERSE:
    b, idio = BETA.get(sym, 1.0), IDIO.get(sym, 0.025)
    price, arr = 100.0, []
    for k in range(NDAYS):
        r = b * factor[k] + (random.gauss(0.0, idio) if idio else 0.0)
        price *= math.exp(r)
        arr.append(price)
    series[sym] = arr

# ── гоняем ровно ту же логику, что и прод-бэктест ──
edge   = bt.run_edge(days, series, DEFAULT_CFG)
enh    = bt.run_edge(days, series, ENHANCED_CFG)
base   = bt.run_baseline(days, series)
si     = days.index(edge["start_day"])
btc    = bt.run_hodl(days, series, ["BTC"], si)
basket = bt.run_hodl(days, series, list(series.keys()), si)
rob    = bt.robustness(days, series)
rob_e  = bt.robustness(days, series, base_cfg=ENHANCED_CFG)

pc = lambda x: f"{x*100:+.1f}%"
print("\n===== СИНТЕТИЧЕСКИЙ ПОЛНЫЙ ЦИКЛ (seed=42) — НЕ реальные данные =====")
print(f"Период {edge['start_day']} → {edge['end_day']}  (~{edge['years']:.1f} г., {edge['n_days']} дней)")
print(f"Юниверс: {', '.join(series.keys())}\n")
rows = [
    ("EDGE (база)",   edge),
    ("EDGE+ (новые)",  enh),
    ("Baseline тренд", base),
    ("BTC HODL",       btc),
    ("Корзина HODL",   basket),
]
print(f"{'Стратегия':<16}{'Доход':>10}{'CAGR':>9}{'MDD':>9}{'Sharpe':>8}{'В рынке':>9}")
for name, m in rows:
    print(f"{name:<16}{pc(m['total']):>10}{pc(m['cagr']):>9}{m['mdd']*100:>8.1f}%{m['sharpe']:>8.2f}{m.get('exposure',1.0)*100:>8.0f}%")
print("\n--- РОБАСТНОСТЬ (медиана по 18 конфигам) ---")
print(f"EDGE  : CAGR мед {pc(rob['cagr_med'])}  MDD мед {rob['mdd_med']*100:.1f}%  Sharpe мед {rob['sharpe_med']:.2f}")
print(f"EDGE+ : CAGR мед {pc(rob_e['cagr_med'])}  MDD мед {rob_e['mdd_med']*100:.1f}%  Sharpe мед {rob_e['sharpe_med']:.2f}")

better = (enh['sharpe'] >= edge['sharpe']) and (rob_e['sharpe_med'] >= rob['sharpe_med'])
print("\nВЕРДИКТ (на синтетике): " + (
    "EDGE+ ≥ базы и по Sharpe на цикле, и по медиане сетки." if better
    else "EDGE+ не показал устойчивого преимущества на этом seed."))
print("\n⚠️ Данные СИНТЕТИЧЕСКИЕ (GBM + режимы). Реальные цифры — только python research/halal_edge_backtest.py с сетью.")
