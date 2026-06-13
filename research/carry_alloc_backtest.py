"""research/carry_alloc_backtest.py — what-if для carry-оптимизатора (#4).

Честная оценка прироста risk-aware аллокации над «равным объёмом» при ОДНОМ И
ТОМ ЖЕ риск-бюджете. Два режима:

  1. real  — читает carry_opportunities.csv (снимки фандинга, что логирует бот)
             и считает uplift на каждом снимке. Запускай, когда накопятся данные.
  2. synth — Монте-Карло по реалистичному распределению годового фандинга
             (большинство мелкие, изредка PRIME-зона). Для оффлайн-проверки.

ВАЖНО (честно): прирост — это вклад yield-взвешивания при равном риске. Он
скромный (единицы %) и растёт с разбросом доходностей и шириной лимита; при
cap = 1/n_legs аллокации совпадают и uplift = 0. Главная ценность оптимизатора —
не «больше доходность», а контроль концентрации + отсечение под-костовых ног.
Полную многолетнюю реализованную доходность даст только real-режим на
разблокированной истории фандинга Binance (в песочнице 451-геоблок).

Запуск:  python research/carry_alloc_backtest.py [--mode synth|real] [--n 5000]
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.carry_signal import CarryOpportunity, optimize_carry_allocation  # noqa: E402

CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "carry_opportunities.csv")


def _opp(asset: str, annual: float) -> CarryOpportunity:
    return CarryOpportunity(symbol=f"{asset}USDT", asset=asset, rate=0.0,
                            interval_h=8.0, annual_pct=annual, positive=annual > 0)


def _sample_snapshot(rng: random.Random, n_assets: int = 15) -> list[CarryOpportunity]:
    """Реалистичный снимок: фандинг лог-нормальный, иногда PRIME, редкий минус."""
    opps = []
    for i in range(n_assets):
        # базовый годовой фандинг: лог-нормаль ~ медиана 12%, тяжёлый хвост
        base = rng.lognormvariate(mu=2.5, sigma=0.9)        # ~ e^2.5 ≈ 12%
        if rng.random() < 0.10:
            base *= rng.uniform(2.0, 4.0)                   # изредка прайм-выброс
        if rng.random() < 0.08:
            base = -base * rng.uniform(0.3, 1.0)            # редкий обратный carry
        opps.append(_opp(f"A{i}", round(base, 2)))
    return opps


def run_synth(n: int, capital: float, max_weight: float, seed: int = 7) -> None:
    rng = random.Random(seed)
    uplifts, opt_nets, base_nets, n_legs = [], [], [], []
    binding = 0
    for _ in range(n):
        opps = _sample_snapshot(rng)
        # рассматриваем только положительные/обратные с |ann| >= STRONG (как scan_carry)
        opps = [o for o in opps if abs(o.annual_pct) >= 20.0]
        if not opps:
            continue
        plan = optimize_carry_allocation(opps, capital, max_weight=max_weight)
        if plan["n_legs"] == 0:
            continue
        uplifts.append(plan["uplift_pct"])
        opt_nets.append(plan["port_net_year_usd"])
        base_nets.append(plan["baseline_net_year_usd"])
        n_legs.append(plan["n_legs"])
        if plan["uplift_pct"] <= 0.01:
            binding += 1

    _report("SYNTH", capital, max_weight, uplifts, opt_nets, base_nets, n_legs, binding)


def run_real(capital: float, max_weight: float) -> None:
    if not os.path.exists(CSV_PATH):
        print(f"[real] нет {CSV_PATH} — запусти бота, чтобы накопить снимки, "
              f"или используй --mode synth")
        return
    snaps: dict[str, list[CarryOpportunity]] = {}
    with open(CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ann = float(row["annual_pct"])
            except (KeyError, ValueError):
                continue
            snaps.setdefault(row["ts_utc"], []).append(
                _opp(row.get("symbol", "?").replace("USDT", ""), ann))
    uplifts, opt_nets, base_nets, n_legs = [], [], [], []
    binding = 0
    for opps in snaps.values():
        opps = [o for o in opps if abs(o.annual_pct) >= 20.0]
        if not opps:
            continue
        plan = optimize_carry_allocation(opps, capital, max_weight=max_weight)
        if plan["n_legs"] == 0:
            continue
        uplifts.append(plan["uplift_pct"])
        opt_nets.append(plan["port_net_year_usd"])
        base_nets.append(plan["baseline_net_year_usd"])
        n_legs.append(plan["n_legs"])
        if plan["uplift_pct"] <= 0.01:
            binding += 1
    print(f"[real] снимков: {len(snaps)}")
    _report("REAL", capital, max_weight, uplifts, opt_nets, base_nets, n_legs, binding)


def _report(tag, capital, max_weight, uplifts, opt_nets, base_nets, n_legs, binding):
    if not uplifts:
        print(f"[{tag}] нет пригодных снимков (нет возможностей >= STRONG).")
        return
    m = len(uplifts)
    print(f"\n=== {tag}: carry-оптимизатор vs равный объём ===")
    print(f"капитал=${capital:,.0f} · cap/актив={max_weight:.0%} · снимков={m}")
    print(f"uplift net $/год: median {statistics.median(uplifts):+.1f}% · "
          f"mean {statistics.mean(uplifts):+.1f}% · "
          f"p90 {sorted(uplifts)[int(0.9*m)-1]:+.1f}%")
    print(f"снимков с uplift≈0 (cap связал всё): {binding}/{m} = {binding/m:.0%}")
    print(f"опт net $/год: median ${statistics.median(opt_nets):,.0f} · "
          f"равный объём: median ${statistics.median(base_nets):,.0f}")
    print(f"ног в портфеле: median {statistics.median(n_legs):.0f}")
    print("ЧЕСТНО: прирост — вклад yield-взвешивания при равном риске; "
          "главная ценность — контроль концентрации + отсечение под-костовых ног.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["synth", "real"], default="synth")
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--max-weight", type=float, default=0.25)
    args = ap.parse_args()
    if args.mode == "synth":
        run_synth(args.n, args.capital, args.max_weight)
    else:
        run_real(args.capital, args.max_weight)


if __name__ == "__main__":
    main()
