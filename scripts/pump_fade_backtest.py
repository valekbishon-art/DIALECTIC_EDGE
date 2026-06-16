"""scripts/pump_fade_backtest.py — РЕАЛЬНЫЙ out-of-sample бэктест pump-fade.

Зачем: цифры в core/pump_fade.py (win 0.60 / median +5%) — захардкоженные
константы из ВНЕШНЕГО исследования (Gate, 1 биржа, survivorship-bias). Код их
не пересчитывает. Этот скрипт честно проверяет эдж на НЕЗАВИСИМЫХ данных:
другая биржа (Binance USDT-перпы), свежий период, логика считается с нуля.

Стратегия (как в core/pump_fade.py):
  • Событие: монета закрыла день с ран-апом >= RUNUP (close/prev_close - 1).
  • Вход: ШОРТ на закрытии памп-дня.
  • Выход: цель -TARGET (откат вниз), инвалидация +STOP (вверх), или время HOLD дней.
  • Косты: COST round-trip (комса+слипедж). Фандинг не моделируем (для шорта в
    памп-эйфории он обычно попутный — консервативно опускаем = занижаем эдж).

Внутридневная неоднозначность (день задел И цель, И стоп) трактуется КОНСЕРВАТИВНО
как срабатывание стопа (худший случай) — чтобы не завысить результат.

Без внешних зависимостей (urllib + json + statistics). Сеть: Binance fapi public.

Запуск:
    python scripts/pump_fade_backtest.py                  # дефолтный прогон + свип
    python scripts/pump_fade_backtest.py --days 700 --workers 8
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

BINANCE = "https://fapi.binance.com"


def _get(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def perp_universe() -> list[str]:
    """Все торгуемые USDT-перпы Binance (survivorship-bias: делистнутые исключены)."""
    info = _get(BINANCE + "/fapi/v1/exchangeInfo")
    out = []
    for s in info.get("symbols", []):
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            out.append(s["symbol"])
    return out


def fetch_daily(symbol: str, days: int):
    """Дневные свечи. Возвращает список dict или None при ошибке."""
    url = f"{BINANCE}/fapi/v1/klines?symbol={symbol}&interval=1d&limit={min(days, 1500)}"
    try:
        kl = _get(url)
    except Exception:  # noqa: BLE001
        return None
    rows = []
    for k in kl:
        try:
            rows.append({"o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                         "c": float(k[4]), "qv": float(k[7])})  # k[7] = quote volume
        except (ValueError, IndexError):
            continue
    return rows


def simulate(rows, *, runup, target, stop, cost, hold, min_qvol, optimistic=False):
    """Список net-доходностей шорта по каждому памп-событию в одной монете.

    optimistic=False (дефолт): спорный день (задет И стоп, И цель) = стоп (худший
    случай, нижняя граница). optimistic=True: спорный день = цель (верхняя граница).
    """
    trades = []
    if not rows or len(rows) < 3:
        return trades
    n = len(rows)
    for i in range(1, n - 1):
        prev, cur = rows[i - 1], rows[i]
        if prev["c"] <= 0 or cur["c"] <= 0:
            continue
        if cur["qv"] < min_qvol:           # ликвидностный фильтр по обороту памп-дня
            continue
        ru = cur["c"] / prev["c"] - 1.0
        if ru < runup:
            continue
        p0 = cur["c"]
        tgt = p0 * (1.0 - target)          # цель шорта (цена вниз)
        stp = p0 * (1.0 + stop)            # инвалидация шорта (цена вверх)
        gross = None
        outcome = None
        for j in range(i + 1, min(i + 1 + hold, n)):
            day = rows[j]
            hit_stop = day["h"] >= stp
            hit_tgt = day["l"] <= tgt
            if hit_stop and hit_tgt and optimistic:
                gross = (p0 - tgt) / p0    # оптимистично: цель раньше стопа
                outcome = "target"
                break
            if hit_stop:                   # консервативно: стоп раньше цели
                gross = (p0 - stp) / p0
                outcome = "stop"
                break
            if hit_tgt:
                gross = (p0 - tgt) / p0
                outcome = "target"
                break
        if gross is None:                  # выход по времени на последней свече окна
            jlast = min(i + hold, n - 1)
            gross = (p0 - rows[jlast]["c"]) / p0
            outcome = "time"
        trades.append((gross - cost, outcome, ru))
    return trades


def stats(trades) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t[0] for t in trades]
    wins = sum(1 for r in rets if r > 0)
    rets_sorted = sorted(rets)
    def pct(p):
        idx = max(0, min(len(rets_sorted) - 1, int(p * len(rets_sorted))))
        return rets_sorted[idx]
    outc = {}
    for _, o, _ in trades:
        outc[o] = outc.get(o, 0) + 1
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    return {
        "n": len(rets),
        "win": wins / len(rets),
        "mean": mean,
        "median": statistics.median(rets),
        "sd": sd,
        "sharpe_trade": (mean / sd) if sd else 0.0,
        "p10": pct(0.10),
        "p90": pct(0.90),
        "worst": min(rets),
        "best": max(rets),
        "sum": sum(rets),
        "outcomes": outc,
    }


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "  нет событий"
    return (f"  N={s['n']:<5} win={s['win']*100:5.1f}%  "
            f"mean={s['mean']*100:+5.2f}%  median={s['median']*100:+5.2f}%  "
            f"sd={s['sd']*100:4.1f}%  sharpe/trade={s['sharpe_trade']:+.3f}\n"
            f"        p10={s['p10']*100:+.1f}%  p90={s['p90']*100:+.1f}%  "
            f"worst={s['worst']*100:+.1f}%  Σ(equal-size)={s['sum']*100:+.0f}%  "
            f"outcomes={s['outcomes']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=700)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-symbols", type=int, default=0, help="0 = все")
    args = ap.parse_args()

    print("=" * 78)
    print("PUMP-FADE РЕАЛЬНЫЙ БЭКТЕСТ — Binance USDT-перпы (out-of-sample vs Gate-research)")
    print("=" * 78)
    t0 = time.time()
    syms = perp_universe()
    if args.max_symbols:
        syms = syms[: args.max_symbols]
    print(f"Юниверс: {len(syms)} USDT-перпов, тянем ~{args.days} дневных свечей...")

    data = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_daily, s, args.days): s for s in syms}
        for f in as_completed(futs):
            s = futs[f]
            rows = f.result()
            done += 1
            if rows:
                data[s] = rows
            if done % 75 == 0:
                print(f"  ...{done}/{len(syms)}")
    span = max((len(r) for r in data.values()), default=0)
    print(f"Данные: {len(data)} монет, до {span} дней/монета, "
          f"загрузка {time.time()-t0:.0f}с\n")

    # Базовая конфигурация = ровно параметры pump_fade.py / исследования.
    BASE = dict(runup=0.20, target=0.06, stop=0.30, cost=0.003, hold=3, min_qvol=5_000_000)

    def run(label, **over):
        cfg = {**BASE, **over}
        all_tr = []
        for rows in data.values():
            all_tr += simulate(rows, **cfg)
        s = stats(all_tr)
        c = ", ".join(f"{k}={v}" for k, v in over.items()) or "BASE"
        print(f"▶ {label}  [{c}]")
        print(fmt(s))
        print()
        return s

    print("─" * 78)
    print("ГЛАВНЫЙ ТЕСТ (параметры как в pump_fade.py: ран-ап 20%/24ч, цель -6%, "
          "стоп +30%, 0.3% косты, 72ч)")
    print("─" * 78)
    base = run("BASE (консервативно: спорный день=стоп)")
    run("BASE ОПТИМИСТИЧНО (спорный день=цель, верхняя граница)", optimistic=True)

    print("─" * 78)
    print("СВИП ПАРАМЕТРОВ (робастность эджа)")
    print("─" * 78)
    run("ран-ап 15%", runup=0.15)
    run("ран-ап 30%", runup=0.30)
    run("ран-ап 50%", runup=0.50)
    run("косты 0.5%", cost=0.005)
    run("БЕЗ стопа (исслед.: тесный стоп вредит)", stop=10.0)
    run("ТЕСНЫЙ стоп +15%", stop=0.15)
    run("горизонт 24ч", hold=1)
    run("горизонт 7д", hold=7)
    run("ликвидность >$20M", min_qvol=20_000_000)

    print("=" * 78)
    print("ВЕРДИКТ (по BASE):")
    if base.get("n", 0) < 30:
        print("  ⚠️ Мало событий — статистически незначимо.")
    else:
        edge = base["mean"] > 0 and base["win"] > 0.5
        print(f"  События: {base['n']}, win {base['win']*100:.1f}%, "
              f"median {base['median']*100:+.2f}%, mean {base['mean']*100:+.2f}%/сделка")
        print(f"  Исследование Gate заявляло: win ~60%, median +5%, mean +3.8%")
        print(f"  Эдж РЕПЛИЦИРУЕТСЯ независимо: {'ДА ✅' if edge else 'НЕТ ❌'}")
    print("=" * 78)


if __name__ == "__main__":
    main()
