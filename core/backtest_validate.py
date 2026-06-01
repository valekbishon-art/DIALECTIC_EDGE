"""core/backtest_validate.py — валидация edge: робастность, косты, значимость.

ЗАЧЕМ. Бэктест может показать плюсовое среднее, которое на деле — шум или
артефакт нулевых костов. Этот модуль отвечает на единственный честный вопрос:
**отличим ли edge от нуля после реальных издержек?**

Инструменты:
  • cost_haircut() — слиппаж. Слиппаж s на сторону уменьшает PnL каждой сделки
    ровно на 2·s (вход+выход), т.к. resolve считает pnl = (... − fee·2). Поэтому
    стресс по костам применяется пост-фактум, без перегона бэктеста.
  • bootstrap_ci() — ресэмплинг сделок с возвращением → 95% ДИ среднего и доля
    бутстрэп-выборок с E>0. Если ДИ накрывает 0 — edge статистически не доказан.
  • segment() — нарезка сделок (по направлению, году, активу) для проверки,
    не держится ли весь «плюс» на одном кармане.

Косты по умолчанию: база (как в resolve, 0.1%/сторона) + стресс 0.05% и 0.10%
слиппажа/сторону (taker-реализм для majors / alts).
"""

from __future__ import annotations

import random


def cost_haircut(pnls: list[float], slip_per_side_pct: float) -> list[float]:
    """Уменьшает каждый PnL% на 2·slip (вход+выход). slip в % (0.05 = 0.05%)."""
    cut = 2.0 * slip_per_side_pct
    return [p - cut for p in pnls]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def bootstrap_ci(
    pnls: list[float],
    *,
    n_boot: int = 5000,
    seed: int = 42,
    alpha: float = 0.05,
) -> dict:
    """95% ДИ среднего PnL через bootstrap + доля выборок с E>0.

    Детерминирован при фикс. seed. Возвращает {mean, ci_low, ci_high, p_positive, n}.
    p_positive ≈ 1 (или 0) → edge уверенный; ≈0.5 → чистый шум.
    """
    N = len(pnls)
    if N == 0:
        return {"mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_positive": 0.5, "n": 0}
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(N):
            s += pnls[rng.randrange(N)]
        means.append(s / N)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot)]
    p_pos = sum(1 for m in means if m > 0) / n_boot
    return {
        "mean": round(mean(pnls), 4),
        "ci_low": round(lo, 4),
        "ci_high": round(hi, 4),
        "p_positive": round(p_pos, 4),
        "n": N,
    }


def bootstrap_diff(
    a: list[float],
    b: list[float],
    *,
    n_boot: int = 5000,
    seed: int = 42,
    alpha: float = 0.05,
) -> dict:
    """ДИ разницы средних (a − b) через независимый bootstrap двух выборок.

    Альфа-тест: a = quant-отобранные сделки, b = наивный бейзлайн (always_*).
    Если ДИ разницы целиком > 0 → отбор quant добавляет ценность (альфа).
    Если накрывает 0 → quant не бьёт направленную ставку (только бета).
    """
    if not a or not b:
        return {"diff": 0.0, "ci_low": 0.0, "ci_high": 0.0, "p_a_better": 0.5, "na": len(a), "nb": len(b)}
    rng = random.Random(seed)
    Na, Nb = len(a), len(b)
    diffs = []
    for _ in range(n_boot):
        sa = sum(a[rng.randrange(Na)] for _ in range(Na)) / Na
        sb = sum(b[rng.randrange(Nb)] for _ in range(Nb)) / Nb
        diffs.append(sa - sb)
    diffs.sort()
    lo = diffs[int((alpha / 2) * n_boot)]
    hi = diffs[int((1 - alpha / 2) * n_boot)]
    p_better = sum(1 for d in diffs if d > 0) / n_boot
    return {
        "diff": round(mean(a) - mean(b), 4),
        "ci_low": round(lo, 4),
        "ci_high": round(hi, 4),
        "p_a_better": round(p_better, 4),
        "na": Na,
        "nb": Nb,
    }


def segment(trades, *, direction=None, year=None, asset=None) -> list[float]:
    """Возвращает список pnl_pct сделок, удовлетворяющих фильтрам."""
    out = []
    for t in trades:
        if direction and t.direction != direction:
            continue
        if year and not t.emitted_at.startswith(year):
            continue
        if asset and t.asset != asset:
            continue
        out.append(t.pnl_pct)
    return out


def validate_segment(trades, *, label: str, slippages=(0.0, 0.05, 0.10), **filters) -> dict:
    """Полный валидационный отчёт по сегменту: ДИ + стресс по костам."""
    base = segment(trades, **filters)
    res = {"label": label, "n": len(base), "boot": bootstrap_ci(base)}
    cost = {}
    for s in slippages:
        haircut = cost_haircut(base, s)
        cost[f"slip_{s:.2f}pct"] = {
            "mean": round(mean(haircut), 4),
            "p_positive": bootstrap_ci(haircut)["p_positive"],
        }
    res["cost_stress"] = cost
    return res


def verdict(boot: dict) -> str:
    """Человеческий вердикт по bootstrap-результату."""
    if boot["n"] < 30:
        return "🟡 мало данных (N<30)"
    p = boot["p_positive"]
    lo, hi = boot["ci_low"], boot["ci_high"]
    if lo > 0:
        return f"🟢 РОБАСТНЫЙ плюс (95% ДИ [{lo:+.2f};{hi:+.2f}], весь >0)"
    if hi < 0:
        return f"🔴 робастный МИНУС (95% ДИ [{lo:+.2f};{hi:+.2f}], весь <0)"
    return f"⚪ НЕ отличим от нуля (95% ДИ [{lo:+.2f};{hi:+.2f}], p(E>0)={p:.0%})"
