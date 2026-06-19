"""
edge_feature_search.py — систематический поиск НОВЫХ улучшений EDGE.

Идея: не гадать, а перебрать набор экономически осмысленных механик-кандидатов
и дать ДАННЫМ решить. Каждый кандидат = базовый EDGE + одна (или пара) новых
опций. Для каждого считаем:
  • полный цикл (total / CAGR / MDD / Sharpe / exposure);
  • робастность (та же сетка 18 конфигов → медиана Sharpe/CAGR/MDD).

Кандидат ПРИНИМАЕТСЯ только если он строго лучше базы И по Sharpe полного цикла,
И по медиане Sharpe сетки, и при этом НЕ роняет CAGR более чем на 15%.
Это защита от оверфита под одну точку.

Те же реальные цены, что и в бэктесте: prices_cache.json (офлайн) или Yahoo.

Запуск:  python research/edge_feature_search.py
Выход:   docs/EDGE_SEARCH.md  (+ печать рейтинга в консоль)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
DOCS = ROOT / "docs"

from halal_edge import DEFAULT_CFG  # noqa: E402
from halal_edge_backtest import load_data, run_edge, robustness  # noqa: E402


# ── Кандидаты: имя → переопределения поверх DEFAULT_CFG ──
# Каждый — отдельная гипотеза с экономическим смыслом.
CANDIDATES: list[tuple[str, dict, str]] = [
    ("crash_sma150",    dict(sma_btc=150), "Быстрее выходим в обвал (краш-фильтр 150 вместо 200)"),
    ("crash_sma100",    dict(sma_btc=100), "Очень быстрый краш-фильтр (100)"),
    ("mom_power_1.5",   dict(mom_power=1.5), "Сильнее концентрируем вес на топ-импульсе"),
    ("mom_power_2.0",   dict(mom_power=2.0), "Ещё сильнее концентрируем (степень 2)"),
    ("abs_mom_5pct",    dict(abs_mom_min=0.05), "Не входим в слабые тренды (порог импульса 5%)"),
    ("abs_mom_10pct",   dict(abs_mom_min=0.10), "Порог импульса 10%"),
    ("trend_slope_20",  dict(trend_slope=True, slope_lb=20), "Монета входит только при растущем SMA"),
    ("fast_mom",        dict(mom_lb=(20, 60, 120)), "Быстрее ловим развороты (окна 20/60/120)"),
    ("slow_mom",        dict(mom_lb=(60, 120, 240)), "Медленнее, меньше пилы (окна 60/120/240)"),
    ("cap_weight_40",   dict(max_weight=0.40), "Потолок 40% на одну монету (диверсификация)"),
    ("rebal_3d",        dict(rebal=3), "Чаще ребаланс (раз в 3 дня)"),
    ("rebal_14d",       dict(rebal=14), "Реже ребаланс (раз в 2 недели, меньше комиссий)"),
    ("topk_6",          dict(top_k=6), "Шире корзина (6 монет)"),
    ("topk_3",          dict(top_k=3), "Уже корзина (3 монеты, концентрация)"),
    # ── комбо потенциально дополняющих фишек ──
    ("crash150+mompow15", dict(sma_btc=150, mom_power=1.5), "Быстрый краш-фильтр + концентрация"),
    ("crash150+absmom5",  dict(sma_btc=150, abs_mom_min=0.05), "Быстрый краш-фильтр + фильтр слабых"),
    ("mompow15+cap40",    dict(mom_power=1.5, max_weight=0.40), "Концентрация по импульсу + потолок 40%"),
    # ── КОМБО из реальных победителей (проверяем, складываются ли фишки) ──
    ("V2_slowmom+mompow15",      dict(mom_lb=(60, 120, 240), mom_power=1.5), "Лидеры: медленный импульс + концентрация"),
    ("V2_slowmom+cap40",         dict(mom_lb=(60, 120, 240), max_weight=0.40), "Медленный импульс + потолок 40%"),
    ("V2_slowmom+topk3",         dict(mom_lb=(60, 120, 240), top_k=3), "Медленный импульс + узкая корзина"),
    ("V2_slowmom+mompow15+cap40", dict(mom_lb=(60, 120, 240), mom_power=1.5, max_weight=0.40), "Медленный + концентрация + потолок"),
    ("V2_slowmom+crash100",      dict(mom_lb=(60, 120, 240), sma_btc=100), "Медленный импульс + быстрый краш-фильтр (агрессивно)"),
    ("V2_slowmom+mompow15+topk3", dict(mom_lb=(60, 120, 240), mom_power=1.5, top_k=3), "Медленный + концентрация + узкая корзина"),
]

CAGR_TOLERANCE = 0.85  # кандидат не должен ронять CAGR ниже 85% от базы


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def evaluate(days, series, cfg: dict) -> dict:
    m = run_edge(days, series, cfg)
    return {
        "total": m["total"], "cagr": m["cagr"], "mdd": m["mdd"],
        "sharpe": m["sharpe"], "exposure": m["exposure"],
    }


def main() -> None:
    days, series = load_data()

    print("\n=== БАЗА (DEFAULT_CFG) ===")
    base = evaluate(days, series, DEFAULT_CFG)
    base_rob = robustness(days, series, base_cfg=DEFAULT_CFG)
    print(f"  cycle: total {_fmt_pct(base['total'])}  CAGR {_fmt_pct(base['cagr'])}  "
          f"MDD {base['mdd']*100:.1f}%  Sharpe {base['sharpe']:.2f}  expo {base['exposure']*100:.0f}%")
    print(f"  robust: Sharpe med {base_rob['sharpe_med']:.2f}  CAGR med {_fmt_pct(base_rob['cagr_med'])}")

    rows = []
    print("\n=== КАНДИДАТЫ ===")
    for name, override, desc in CANDIDATES:
        cfg = dict(DEFAULT_CFG, **override)
        cyc = evaluate(days, series, cfg)
        # робастность считаем для всех — она дешёвая и даёт честный антиоверфит
        rob = robustness(days, series, base_cfg=cfg)
        better_cycle = cyc["sharpe"] >= base["sharpe"]
        better_rob = rob["sharpe_med"] >= base_rob["sharpe_med"]
        keeps_cagr = cyc["cagr"] >= base["cagr"] * CAGR_TOLERANCE
        accept = better_cycle and better_rob and keeps_cagr
        rows.append({
            "name": name, "desc": desc, "cyc": cyc, "rob": rob,
            "accept": accept,
            "d_sharpe": cyc["sharpe"] - base["sharpe"],
            "d_rob_sharpe": rob["sharpe_med"] - base_rob["sharpe_med"],
            "d_mdd": cyc["mdd"] - base["mdd"],
        })
        flag = "✅ ЛУЧШЕ" if accept else "  —"
        print(f"  {name:<20} Sharpe {cyc['sharpe']:.2f} (Δ{cyc['sharpe']-base['sharpe']:+.2f})  "
              f"robSh {rob['sharpe_med']:.2f} (Δ{rob['sharpe_med']-base_rob['sharpe_med']:+.2f})  "
              f"CAGR {_fmt_pct(cyc['cagr'])}  MDD {cyc['mdd']*100:.1f}%  {flag}")

    # сортировка: сперва принятые, потом по Sharpe цикла
    rows.sort(key=lambda r: (r["accept"], r["cyc"]["sharpe"]), reverse=True)
    winners = [r for r in rows if r["accept"]]

    print("\n=== ИТОГ ===")
    if winners:
        print(f"Найдено улучшений: {len(winners)}")
        for r in winners:
            print(f"  ✅ {r['name']}: Sharpe {r['cyc']['sharpe']:.2f}, "
                  f"robSh {r['rob']['sharpe_med']:.2f}, CAGR {_fmt_pct(r['cyc']['cagr'])}, "
                  f"MDD {r['cyc']['mdd']*100:.1f}%")
    else:
        print("Устойчивых улучшений над базовым EDGE не найдено — база остаётся лучшей.")

    _write_md(base, base_rob, rows, days)
    print(f"\nОтчёт: {DOCS / 'EDGE_SEARCH.md'}")


def _write_md(base, base_rob, rows, days) -> None:
    p = _fmt_pct
    lines = []
    lines.append("# 🔬 Поиск новых улучшений EDGE (feature search)")
    lines.append("")
    lines.append(f"> Реальные данные · период {days[0]}→{days[-1]} · "
                 f"кандидатов: {len(rows)} · сетка робастности: 18 конфигов на каждого.")
    lines.append("")
    lines.append("**Правило приёмки (анти-оверфит):** кандидат принимается только если "
                 "Sharpe полного цикла **И** медиана Sharpe сетки **≥ базы**, "
                 "и CAGR не падает ниже 85% от базы.")
    lines.append("")
    lines.append("## База (DEFAULT_CFG)")
    lines.append(f"- Цикл: total **{p(base['total'])}**, CAGR **{p(base['cagr'])}**, "
                 f"MDD **{base['mdd']*100:.1f}%**, Sharpe **{base['sharpe']:.2f}**, "
                 f"в рынке {base['exposure']*100:.0f}%.")
    lines.append(f"- Робастность: Sharpe med **{base_rob['sharpe_med']:.2f}**, "
                 f"CAGR med **{p(base_rob['cagr_med'])}**, MDD med **{base_rob['mdd_med']*100:.1f}%**.")
    lines.append("")
    lines.append("## Кандидаты (по убыванию Sharpe цикла)")
    lines.append("")
    lines.append("| Кандидат | Идея | Sharpe | ΔSharpe | robSharpe | CAGR | MDD | Вердикт |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        verdict = "✅ лучше" if r["accept"] else "—"
        lines.append(
            f"| `{r['name']}` | {r['desc']} | {r['cyc']['sharpe']:.2f} | "
            f"{r['d_sharpe']:+.2f} | {r['rob']['sharpe_med']:.2f} | "
            f"{p(r['cyc']['cagr'])} | {r['cyc']['mdd']*100:.1f}% | {verdict} |"
        )
    lines.append("")
    winners = [r for r in rows if r["accept"]]
    lines.append("## Вывод")
    if winners:
        lines.append(f"Найдено **{len(winners)}** устойчивых улучшений:")
        for r in winners:
            lines.append(f"- **`{r['name']}`** — {r['desc']}. "
                         f"Sharpe {r['cyc']['sharpe']:.2f}, robSharpe {r['rob']['sharpe_med']:.2f}, "
                         f"CAGR {p(r['cyc']['cagr'])}, MDD {r['cyc']['mdd']*100:.1f}%.")
        lines.append("")
        lines.append("Эти опции можно включить в прод-пресет после ручной перепроверки.")
    else:
        lines.append("**Устойчивых улучшений над базовым EDGE не найдено.** "
                     "Все кандидаты либо проигрывают по Sharpe, либо неустойчивы по сетке, "
                     "либо роняют доходность. Базовый EDGE остаётся лучшей конфигурацией — "
                     "это сильный результат: стратегия уже близка к локальному оптимуму "
                     "в пространстве daily-close-only механик.")
    lines.append("")
    lines.append("> ⚠️ Историческая симуляция на дневных данных. Survivorship bias. "
                 "Не гарантия будущего, не инвестсовет.")
    DOCS.mkdir(exist_ok=True)
    (DOCS / "EDGE_SEARCH.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
