"""
stock_edge_backtest.py — бектест спот-EDGE на АКЦИЯХ.

Использует ту же сигнальную функцию `halal_edge.edge_signal()`, что и крипто-edge
(research/halal_edge_backtest.py) и команда /edge в боте. Никаких двойных стандартов:
что бот советует — то и тестируем. Единственные отличия от крипто-версии:

  • Универс: 15 ликвидных US large-cap по секторам (вместо монет).
  • Актив краш-фильтра режима: SPY < SMA200 (вместо BTC < SMA200).
    Реализовано подменой ключа "BTC" -> SPY (edge_signal читает регим из series["BTC"]).
  • Бенчмарк: SPY HODL (вместо BTC HODL) + равновзвешенная корзина.
  • Sharpe annualization √252 (торговые дни), не √365.

Данные: daily adjusted close через yfinance. Комиссия 0.1% на оборот.

Запуск:  python research/stock_edge_backtest.py
Выход:   docs/BACKTEST_STOCKS_RESULTS.md, docs/backtest_stocks_equity.png
"""
from __future__ import annotations
import sys, math, statistics, itertools
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DOCS = ROOT / "docs"

from halal_edge import edge_signal, max_drawdown, max_lookback, FEE  # noqa: E402
from halal_edge import DEFAULT_CFG, EDGE_V2_CFG, EDGE_V2_CONSERVATIVE_CFG  # noqa: E402

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "V",
            "UNH", "XOM", "JNJ", "WMT", "PG", "HD", "AVGO"]
BENCH = "SPY"
START_DATE = "2018-01-01"


def sharpe252(daily):
    if len(daily) < 2:
        return 0.0
    sd = statistics.pstdev(daily)
    return (statistics.fmean(daily) / sd) * math.sqrt(252) if sd else 0.0


def cagr(eq, n):
    yrs = n / 252.0
    return eq[-1] ** (1 / yrs) - 1 if yrs > 0 and eq[-1] > 0 else 0.0


def load_data():
    import yfinance as yf
    import warnings
    warnings.filterwarnings("ignore")
    raw = yf.download(UNIVERSE + [BENCH], start="2017-06-01", end=None,
                      interval="1d", auto_adjust=True, progress=False)["Close"]
    raw = raw[raw[BENCH].notna()]
    days = [d.strftime("%Y-%m-%d") for d in raw.index]

    def col(sym):
        return [float(x) if x == x else None for x in raw[sym].tolist()]  # x==x filters NaN

    series = {"BTC": col(BENCH)}          # регим-ключ = SPY (зеркало BTC)
    for s in UNIVERSE:
        series[s] = col(s)
    return days, series


def start_index(days, cfg):
    warmup = max_lookback(cfg) + 1
    for k, d in enumerate(days):
        if d >= START_DATE:
            return max(k, warmup)
    return warmup


def run_edge(days, series, cfg):
    rebal = cfg["rebal"]
    start = start_index(days, cfg)
    eq, daily, prev, weights = [1.0], [], {}, {}
    in_market, held, turnover = 0, [], 0.0
    for i in range(start, len(days) - 1):
        if (i - start) % rebal == 0:
            sig = edge_signal(series, i, cfg)
            weights = dict(sig["weights"])
            held.append(len(weights))
            allk = set(weights) | set(prev)
            delta = sum(abs(weights.get(k, 0.0) - prev.get(k, 0.0)) for k in allk)
            turnover += delta
            cost = delta * FEE
            prev = dict(weights)
        else:
            cost = 0.0
        if weights:
            in_market += 1
        port = -cost
        for s, wt in weights.items():
            p0, p1 = series[s][i], series[s][i + 1]
            if p0 and p1:
                port += wt * (p1 / p0 - 1.0)
        daily.append(port)
        eq.append(eq[-1] * (1.0 + port))
    n = len(daily)
    return dict(eq=eq, total=eq[-1] - 1, cagr=cagr(eq, n), mdd=max_drawdown(eq),
                sharpe=sharpe252(daily), exposure=in_market / max(1, n),
                avg_held=statistics.fmean(held) if held else 0, start=start, n=n)


def run_hodl(days, series, sym, cfg):
    start = start_index(days, cfg)
    eq, daily = [1.0], []
    for i in range(start, len(days) - 1):
        p0, p1 = series[sym][i], series[sym][i + 1]
        r = (p1 / p0 - 1.0) if (p0 and p1) else 0.0
        daily.append(r)
        eq.append(eq[-1] * (1 + r))
    n = len(daily)
    return dict(eq=eq, total=eq[-1] - 1, cagr=cagr(eq, n),
                mdd=max_drawdown(eq), sharpe=sharpe252(daily), n=n)


def robustness(days, series):
    grid = dict(sma_trend=[100, 150, 200],
                mom_lb=[(30, 90, 180), (60, 120, 240), (20, 60, 120)],
                top_k=[3, 4, 5], rebal=[5, 7, 10],
                weight_mode=["equal", "mom", "invvol"])
    keys = list(grid)
    sh = []
    for combo in itertools.product(*[grid[k] for k in keys]):
        cfg = dict(DEFAULT_CFG)
        cfg.update(dict(zip(keys, combo)))
        try:
            sh.append(run_edge(days, series, cfg)["sharpe"])
        except Exception:
            pass
    sh.sort()
    return dict(n=len(sh), med=statistics.median(sh), lo=sh[0], hi=sh[-1])


def main():
    days, series = load_data()
    base = run_edge(days, series, DEFAULT_CFG)
    v2 = run_edge(days, series, EDGE_V2_CFG)
    cons = run_edge(days, series, EDGE_V2_CONSERVATIVE_CFG)
    spy = run_hodl(days, series, "BTC", DEFAULT_CFG)
    rob = robustness(days, series)

    def pp(x):
        return f"{x*100:+.1f}%"
    print(f"Период: {days[base['start']]} → {days[-1]} ({base['n']} дней)\n")
    print(f"EDGE base : {pp(base['total'])}  CAGR {pp(base['cagr'])}  "
          f"MDD {base['mdd']*100:.1f}%  Sharpe {base['sharpe']:.2f}  expo {base['exposure']*100:.0f}%")
    print(f"EDGE V2   : {pp(v2['total'])}  CAGR {pp(v2['cagr'])}  MDD {v2['mdd']*100:.1f}%  Sharpe {v2['sharpe']:.2f}")
    print(f"EDGE cons : {pp(cons['total'])}  CAGR {pp(cons['cagr'])}  MDD {cons['mdd']*100:.1f}%  Sharpe {cons['sharpe']:.2f}")
    print(f"SPY HODL  : {pp(spy['total'])}  CAGR {pp(spy['cagr'])}  MDD {spy['mdd']*100:.1f}%  Sharpe {spy['sharpe']:.2f}")
    print(f"Robust    : Sharpe med {rob['med']:.2f} [{rob['lo']:.2f}..{rob['hi']:.2f}] over {rob['n']} configs")


if __name__ == "__main__":
    main()
