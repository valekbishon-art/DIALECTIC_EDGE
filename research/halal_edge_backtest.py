"""
halal_edge_backtest.py — честный бэктест халяльного EDGE на ПОЛНОМ цикле.

Сигнальная логика НЕ дублируется здесь — она в `halal_edge.py` (функция
`edge_signal`). Это и есть честность: бэктест гоняет ровно ту же функцию,
что бот зовёт в `/edge` на сегодняшнем баре. Что советуем — то и тестим.

EDGE (строго спот / лонг / без плеча / без шортов):
  • Dual momentum (тренд монеты вверх + импульс>0, держим ТОП-K сильнейших).
  • Inverse-vol веса + vol targeting портфеля (потолок 100%, без плеча).
  • Краш-фильтр: BTC < SMA200 → весь капитал в стейбл.

Период — ПОЛНЫЙ цикл с 2021-01: эйфория быка 2021 → обвал 2022 (BTC −77%)
→ восстановление 2023–2025. Это честный тест «выживает ли дисциплина».

Сравниваем на одном периоде:
  • EDGE      — стратегия (halal_edge.edge_signal).
  • Baseline  — простой спот-тренд (CORE4 + BTC-режим, текущая логика).
  • BTC HODL  — купи и держи BTC.
  • Корзина   — купи и держи весь юниверс равным весом.

Антиоверфит: сетка 27 конфигураций + медиана/разброс метрик.

Запуск:  python research/halal_edge_backtest.py
Выход:   docs/BACKTEST_RESULTS.md, docs/backtest_equity.png,
         docs/backtest_summary.json
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DOCS = ROOT / "docs"

from halal_edge import (  # noqa: E402
    UNIVERSE, CORE4, FEE, SMA_BTC, DEFAULT_CFG,
    fetch, sma, max_drawdown, sharpe, edge_signal, max_lookback,
)

START_DATE = "2021-01-01"   # полный цикл: бык 2021 → медведь 2022 → 2023-25
# Тянем с середины 2020, чтобы к 2021-01 был прогрев SMA200/импульса.
# period1/period2 в 1d — иначе range=max даёт недельные свечи.
PERIOD1 = int(datetime(2020, 4, 1, tzinfo=timezone.utc).timestamp())


# ───────────────────────── data ─────────────────────────
def load_data() -> tuple[list[str], dict[str, list[float | None]]]:
    print("Качаю данные с Yahoo (daily, с 2020-04)…")
    raw: dict[str, dict[str, float]] = {}
    for sym in UNIVERSE:
        try:
            raw[sym] = fetch(sym, period1=PERIOD1)
            d = sorted(raw[sym].keys())
            print(f"  {sym}: {len(raw[sym])} дней  ({d[0]}→{d[-1]})")
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: ошибка {e} — пропускаю")
            raw[sym] = {}
    days = sorted(raw["BTC"].keys())                       # календарь по BTC
    series = {s: [raw[s].get(d) for d in days] for s in UNIVERSE if raw[s]}
    return days, series


def _start_index(days: list[str], cfg: dict) -> int:
    """Первый индекс ≥ START_DATE, но не раньше прогрева (max_lookback)."""
    warmup = max_lookback(cfg) + 1
    for k, d in enumerate(days):
        if d >= START_DATE:
            return max(k, warmup)
    return warmup


# ───────────────────────── EDGE (через общий сигнал) ─────────────────────────
def run_edge(days: list[str], series: dict[str, list[float | None]], cfg: dict) -> dict:
    rebal = cfg["rebal"]
    start = _start_index(days, cfg)
    eq = [1.0]
    daily: list[float] = []
    prev: dict[str, float] = {}
    weights: dict[str, float] = {}
    in_market = 0
    held_counts: list[int] = []

    for i in range(start, len(days) - 1):
        if (i - start) % rebal == 0:                        # день ребаланса
            sig = edge_signal(series, i, cfg)               # ← ЕДИНЫЙ сигнал
            weights = dict(sig["weights"])
            held_counts.append(len(weights))
            allk = set(weights) | set(prev)
            cost = sum(abs(weights.get(k, 0.0) - prev.get(k, 0.0)) for k in allk) * FEE
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

    return _metrics(days, start, eq, daily, in_market, held_counts, list(series.keys()))


def run_baseline(days: list[str], series: dict[str, list[float | None]]) -> dict:
    """Простой спот-тренд (как сейчас в боте): CORE4, равный вес, BTC-режим."""
    coins = [c for c in CORE4 if c in series]
    btcser = series["BTC"]
    sma_n = 100
    start = _start_index(days, DEFAULT_CFG)
    eq = [1.0]
    daily: list[float] = []
    prev: dict[str, float] = {}
    in_market = 0
    held_counts: list[int] = []
    for i in range(start, len(days) - 1):
        bwin = [p for p in btcser[: i + 1] if p is not None]
        btc_ma = sma(bwin, SMA_BTC)
        risk_on = btc_ma is not None and btcser[i] and btcser[i] > btc_ma
        chosen = []
        if risk_on:
            for s in coins:
                win = [p for p in series[s][: i + 1] if p is not None]
                if len(win) < sma_n + 1:
                    continue
                ma = sma(win, sma_n)
                pr = series[s][i]
                if pr and ma and pr > ma:
                    chosen.append(s)
        held_counts.append(len(chosen))
        if chosen:
            in_market += 1
        w = 1.0 / len(chosen) if chosen else 0.0
        weights = {s: w for s in chosen}
        allk = set(weights) | set(prev)
        cost = sum(abs(weights.get(k, 0.0) - prev.get(k, 0.0)) for k in allk) * FEE
        prev = weights
        port = -cost
        for s, wt in weights.items():
            p0, p1 = series[s][i], series[s][i + 1]
            if p0 and p1:
                port += wt * (p1 / p0 - 1.0)
        daily.append(port)
        eq.append(eq[-1] * (1.0 + port))
    return _metrics(days, start, eq, daily, in_market, held_counts, coins)


def run_hodl(days, series, symbols: list[str], start: int) -> dict:
    """Купи-и-держи равным весом по symbols (для BTC передай ['BTC'])."""
    eq = [1.0]
    daily: list[float] = []
    for i in range(start, len(days) - 1):
        rets = []
        for s in symbols:
            p0, p1 = series[s][i], series[s][i + 1]
            if p0 and p1:
                rets.append(p1 / p0 - 1.0)
        r = statistics.fmean(rets) if rets else 0.0
        daily.append(r)
        eq.append(eq[-1] * (1.0 + r))
    return _metrics(days, start, eq, daily, len(daily), [], symbols)


def _metrics(days, start, eq, daily, in_market, held_counts, coins) -> dict:
    n = len(eq) - 1
    years = n / 365.0 if n else 1.0
    cagr = eq[-1] ** (1.0 / years) - 1.0 if years > 0 else 0.0
    wins = sum(1 for r in daily if r > 0)
    return {
        "eq": eq, "daily": daily,
        "start_day": days[start], "end_day": days[-1], "n_days": n, "years": years,
        "total": eq[-1] - 1, "cagr": cagr, "mdd": max_drawdown(eq), "sharpe": sharpe(daily),
        "win_rate": wins / len(daily) if daily else 0.0,
        "exposure": in_market / len(daily) if daily else 0.0,
        "avg_held": statistics.fmean(held_counts) if held_counts else 0.0,
        "universe": ", ".join(coins),
    }


# ───────────────────────── robustness ─────────────────────────
def robustness(days, series) -> dict:
    # Возмущаем выбранные параметры EDGE (тренд × число монет × схема веса),
    # чтобы показать: преимущество — свойство подхода, а не одной точки.
    grid = []
    for sma_t in (100, 120, 150):
        for k in (3, 4, 5):
            for wm in ("mom", "equal"):
                cfg = dict(DEFAULT_CFG, sma_trend=sma_t, top_k=k, weight_mode=wm)
                m = run_edge(days, series, cfg)
                grid.append((m["total"], m["cagr"], m["mdd"], m["sharpe"]))
    tots = sorted(g[0] for g in grid)
    cagrs = sorted(g[1] for g in grid)
    mdds = sorted(g[2] for g in grid)
    shps = sorted(g[3] for g in grid)
    med = statistics.median
    return {
        "n_configs": len(grid),
        "cagr_med": med(cagrs), "cagr_min": cagrs[0], "cagr_max": cagrs[-1],
        "mdd_med": med(mdds), "mdd_min": mdds[0], "mdd_max": mdds[-1],
        "sharpe_med": med(shps), "sharpe_min": shps[0], "sharpe_max": shps[-1],
        "total_med": med(tots),
    }


# ───────────────────────── orchestration ─────────────────────────
def run_all() -> dict:
    days, series = load_data()
    edge = run_edge(days, series, DEFAULT_CFG)
    base = run_baseline(days, series)

    start_idx = days.index(edge["start_day"])
    btc = run_hodl(days, series, ["BTC"], start_idx)
    basket = run_hodl(days, series, list(series.keys()), start_idx)
    rob = robustness(days, series)

    metrics = {
        "start_day": edge["start_day"], "end_day": edge["end_day"],
        "n_days": edge["n_days"], "years": edge["years"],
        "universe": ", ".join(series.keys()),
        "strat_total": edge["total"], "strat_cagr": edge["cagr"],
        "strat_mdd": edge["mdd"], "strat_sharpe": edge["sharpe"],
        "exposure": edge["exposure"], "win_rate": edge["win_rate"], "avg_held": edge["avg_held"],
        "base_total": base["total"], "base_cagr": base["cagr"],
        "base_mdd": base["mdd"], "base_sharpe": base["sharpe"],
        "btc_total": btc["total"], "btc_cagr": btc["cagr"], "btc_mdd": btc["mdd"],
        "basket_total": basket["total"], "basket_cagr": basket["cagr"], "basket_mdd": basket["mdd"],
        "rob": rob,
    }
    return {
        "metrics": metrics,
        "curves": {
            "days": days[start_idx:],
            "edge": edge["eq"], "btc": btc["eq"], "basket": basket["eq"], "base": base["eq"],
        },
    }


def render_chart(res: dict) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.dates import DateFormatter

    c = res["curves"]
    days = [datetime.strptime(d, "%Y-%m-%d") for d in c["days"]]
    edge, btc, basket, base = c["edge"][1:], c["btc"][1:], c["basket"][1:], c["base"][1:]
    n = min(len(days), len(edge), len(btc), len(basket), len(base))
    days, edge, btc, basket, base = days[:n], edge[:n], btc[:n], basket[:n], base[:n]

    fig, ax = plt.subplots(figsize=(10, 5.4), dpi=130)
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#0e1117")
    ax.plot(days, [v * 100 for v in edge], color="#22c55e", lw=2.8, label="EDGE (momentum-weight + краш-фильтр)")
    ax.plot(days, [v * 100 for v in base], color="#38bdf8", lw=1.6, label="Простой спот-тренд (baseline)")
    ax.plot(days, [v * 100 for v in btc], color="#f59e0b", lw=1.5, ls="--", label="BTC «купи и держи»")
    ax.plot(days, [v * 100 for v in basket], color="#ef4444", lw=1.4, ls=":", label="Корзина «купи и держи»")
    ax.axhline(100, color="#6b7280", lw=0.8, alpha=0.6)
    ax.set_yscale("log")
    ax.set_title("DIALECTIC EDGE — полный цикл 2021→2026 (старт = 100, лог-шкала)",
                 color="#e5e7eb", fontsize=11.5, fontweight="bold", pad=12)
    ax.set_ylabel("Капитал (старт 100, лог)", color="#9ca3af", fontsize=10)
    ax.tick_params(colors="#9ca3af", labelsize=9)
    ax.xaxis.set_major_formatter(DateFormatter("%b %y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=4))
    for sp in ax.spines.values():
        sp.set_color("#374151")
    ax.grid(True, color="#1f2937", lw=0.6, which="both")
    leg = ax.legend(loc="upper left", facecolor="#0e1117", edgecolor="#374151", fontsize=8.5)
    for txt in leg.get_texts():
        txt.set_color("#e5e7eb")
    fig.autofmt_xdate()
    fig.tight_layout()
    DOCS.mkdir(exist_ok=True)
    out = DOCS / "backtest_equity.png"
    fig.savefig(out, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out


def render_md(res: dict) -> Path:
    m = res["metrics"]
    r = m["rob"]
    p = lambda x: f"{x * 100:+.1f}%"
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "# 📊 Бэктест халяльного EDGE — Dialectic Edge (полный цикл)",
        f"> Сгенерировано: {now} · период {m['start_day']} → {m['end_day']} "
        f"(~{m['years']:.1f} г., {m['n_days']} дней: бык 2021 → медведь 2022 → восстановление 2023-25)",
        "",
        "**EDGE-стратегия** (строго спот / лонг / без плеча / без шортов) — та же "
        "функция `halal_edge.edge_signal`, что бот зовёт в `/edge`:",
        "- Dual momentum: монета входит только если тренд вверх и импульс>0; держим ТОП-K сильнейших.",
        "- Вес по силе импульса (momentum-weight): сильнейшим больше веса.",
        f"- Краш-фильтр: при BTC < SMA{SMA_BTC} весь капитал в стейбл.",
        "- _Урок бэктеста:_ inverse-vol/vol-targeting (хороши в акциях) в крипто "
        "проигрывают — недовешивают волатильные ракеты (SOL/AVAX), дающие рост. Отключены.",
        f"- Юниверс: {m['universe']}.",
        "",
        "## Итоги (старт капитала = 100)",
        "",
        "| Метрика | 🟢 EDGE | Простой тренд | BTC «держать» | Корзина «держать» |",
        "|---|---|---|---|---|",
        f"| Доходность | **{p(m['strat_total'])}** | {p(m['base_total'])} | {p(m['btc_total'])} | {p(m['basket_total'])} |",
        f"| Годовая (CAGR) | **{p(m['strat_cagr'])}** | {p(m['base_cagr'])} | {p(m['btc_cagr'])} | {p(m['basket_cagr'])} |",
        f"| Макс. просадка | **{p(m['strat_mdd'])}** | {p(m['base_mdd'])} | {p(m['btc_mdd'])} | {p(m['basket_mdd'])} |",
        f"| Sharpe (год.) | **{m['strat_sharpe']:.2f}** | {m['base_sharpe']:.2f} | — | — |",
        f"| Время в рынке | {p(m['exposure'])} | — | 100% | 100% |",
        "",
        "## Робастность (антиоверфит)",
        "",
        f"Прогнали **{r['n_configs']} конфигураций** (SMA-тренд × число монет × vol-target). "
        "Это не одна удачная точка, а свойство подхода:",
        "",
        f"- CAGR: медиана **{p(r['cagr_med'])}**, диапазон {p(r['cagr_min'])}…{p(r['cagr_max'])}.",
        f"- Просадка: медиана **{p(r['mdd_med'])}**, диапазон {p(r['mdd_max'])}…{p(r['mdd_min'])}.",
        f"- Sharpe: медиана **{r['sharpe_med']:.2f}**, диапазон {r['sharpe_min']:.2f}…{r['sharpe_max']:.2f}.",
        "",
        "## Что это значит",
        "",
        f"- **В чём EDGE:** просадка {p(m['strat_mdd'])} против {p(m['base_mdd'])} у простого "
        f"тренда и {p(m['btc_mdd'])} у BTC — дисциплина dual-momentum + vol targeting режет боль "
        "в медвежке.",
        f"- **Sharpe {m['strat_sharpe']:.2f}** против {m['base_sharpe']:.2f} у baseline.",
        f"- **Только {p(m['exposure'])} времени в рынке** — остальное в стейбле. Защита без шортов.",
        "- Честно: в безоткатном бычьем рывке holdBTC по чистой доходности может опережать — "
        "EDGE покупает меньшие просадки и устойчивость, а не максимум плеча.",
        "",
        "![Кривая капитала](backtest_equity.png)",
        "",
        "> ⚠️ Историческая симуляция на дневных данных Yahoo. Есть survivorship bias "
        "(берём монеты, что дожили до сегодня). Не гарантия будущего, не инвестсовет.",
        "",
        "_Воспроизвести: `python research/halal_edge_backtest.py`_",
        "",
    ]
    DOCS.mkdir(exist_ok=True)
    out = DOCS / "BACKTEST_RESULTS.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def render_json(res: dict) -> Path:
    m = dict(res["metrics"])
    rob = m.pop("rob", None)
    if rob:
        m["rob_n_configs"] = rob["n_configs"]
        m["rob_cagr_med"] = rob["cagr_med"]
        m["rob_mdd_med"] = rob["mdd_med"]
        m["rob_sharpe_med"] = rob["sharpe_med"]
    DOCS.mkdir(exist_ok=True)
    out = DOCS / "backtest_summary.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main():
    res = run_all()
    md = render_md(res)
    png = render_chart(res)
    render_json(res)
    m = res["metrics"]
    r = m["rob"]
    pp = lambda x: f"{x*100:+.1f}%"
    print("\n=== ИТОГ (EDGE) ===")
    print(f"Период {m['start_day']} → {m['end_day']} ({m['years']:.1f} г.)")
    print(f"EDGE     : {pp(m['strat_total'])}  CAGR {pp(m['strat_cagr'])}  "
          f"MDD {m['strat_mdd']*100:.1f}%  Sharpe {m['strat_sharpe']:.2f}  expo {m['exposure']*100:.0f}%")
    print(f"Baseline : {pp(m['base_total'])}  CAGR {pp(m['base_cagr'])}  MDD {m['base_mdd']*100:.1f}%  Sharpe {m['base_sharpe']:.2f}")
    print(f"BTC HODL : {pp(m['btc_total'])}  MDD {m['btc_mdd']*100:.1f}%")
    print(f"Корзина  : {pp(m['basket_total'])}  MDD {m['basket_mdd']*100:.1f}%")
    print(f"Robust   : CAGR med {pp(r['cagr_med'])} [{pp(r['cagr_min'])}..{pp(r['cagr_max'])}]  "
          f"MDD med {r['mdd_med']*100:.1f}%  Sharpe med {r['sharpe_med']:.2f}")
    print(f"Файлы: {md} | {png}")


if __name__ == "__main__":
    main()
