"""scripts/pump_pnd_backtest.py — РЕШАЮЩИЙ бэктест строгого pump-and-dump фейда.

Проверяет гипотезу: наивный памп эджа не даёт, но СТРОГОЕ определение P&D
(внезапный спайк + климакс объёма + из тихой базы) на микрокап-перпах может
давать +EV фейд (шорт отката). Честно, с защитой от оверфита и иллюзий:

  • Площадка: Gate USDT-перпы (там живут реальные памп-дампы; шорт через перп —
    борроу не нужен).
  • История: пагинация ~250 дней (разные режимы рынка), а не 83 дня.
  • Train/test split по ВРЕМЕНИ. Эдж засчитываем только если плюс в ОБЕИХ половинах.
  • Слиппедж: тиры по ликвидности монеты (на $20k/ч фил хуже экранной цены —
    главный убийца микрокап-стратегий). Считаем результат БЕЗ и С слиппеджем.
  • Фандинг-тейлвинд (шорт раздутого перпа собирает фандинг) НАМЕРЕННО не
    моделируем → результат консервативный (занижен).
  • Определение ЗАФИКСИРОВАНО (не перебираем пороги до победы).

Без внешних зависимостей. Сеть: Gate v4 public.

Запуск:  python scripts/pump_pnd_backtest.py
"""
from __future__ import annotations

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

G = "https://api.gateio.ws/api/v4"
HOUR = 3600


def get(u, t=30):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=t).read())


def universe():
    return [c["name"] for c in get(G + "/futures/usdt/contracts") if not c.get("in_delisting")]


def fetch_hist(name, chunks=3):
    """~250 дней часовых свечей через пагинацию (chunks × 2000ч). asc по времени."""
    out = {}
    to = int(time.time())
    for _ in range(chunks):
        try:
            k = get(f"{G}/futures/usdt/candlesticks?contract={name}&interval=1h&limit=2000&to={to}")
        except Exception:  # noqa: BLE001
            break
        if not k:
            break
        for x in k:
            t = int(x["t"])
            out[t] = (float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), float(x["sum"]))
        to = min(int(x["t"]) for x in k) - HOUR
        time.sleep(0.12)
    rows = [(t,) + out[t] for t in sorted(out)]
    return rows  # (t,o,h,l,c,qv)


def slippage(liq_hourly: float) -> float:
    """Round-trip слиппедж как функция среднечасового $-оборота монеты."""
    if liq_hourly < 50_000:
        return 0.030
    if liq_hourly < 200_000:
        return 0.015
    if liq_hourly < 1_000_000:
        return 0.007
    return 0.003


FEES = 0.0012  # round-trip комса (Gate taker ~0.05% × 2 фила)


def detect(rows, *, W, thr, volmult, minliq, ext_max, dedup_h):
    """Строгие P&D события. Возвращает list of dict."""
    ev = []
    last = -10 ** 9
    n = len(rows)
    for i in range(180, n - 1):
        if i - last < dedup_h:
            continue
        c = rows[i][4]
        c_w = rows[i - W][4]
        if c_w <= 0:
            continue
        if c / c_w - 1.0 < thr:                       # внезапный ран-ап за W часов
            continue
        liq = statistics.fmean([rows[k][5] for k in range(i - 168, i)]) or 1.0  # ср $/ч за 7д
        if liq < minliq:
            continue
        win_qv = sum(rows[k][5] for k in range(i - W + 1, i + 1))
        if win_qv < volmult * liq * W:                # климакс объёма
            continue
        if i - W - 72 >= 0:
            prior = c_w / rows[i - W - 72][4] - 1.0    # был ли уже разогнан ДО пампа
            if prior > ext_max:
                continue
        ev.append({"i": i, "t": rows[i][0], "p0": c, "liq": liq})
        last = i
    return ev


def short_sim(rows, ev, *, target, stop, hold):
    """Шорт: вход p0, стоп +stop, цель -target, до hold часов. Консервативно: стоп
    раньше цели в спорный час. Возвращает (gross, t, liq) по каждому событию."""
    out = []
    n = len(rows)
    for e in ev:
        i, p0 = e["i"], e["p0"]
        tgt, stp = p0 * (1 - target), p0 * (1 + stop)
        g = None
        for j in range(i + 1, min(i + 1 + hold, n)):
            hh, ll = rows[j][2], rows[j][3]
            if hh >= stp:
                g = (p0 - stp) / p0
                break
            if ll <= tgt:
                g = (p0 - tgt) / p0
                break
        if g is None:
            g = (p0 - rows[min(i + hold, n - 1)][4]) / p0
        out.append((g, e["t"], e["liq"]))
    return out


def agg(trades, *, with_slip):
    if not trades:
        return None
    nets = []
    for g, _, liq in trades:
        cost = FEES + (slippage(liq) if with_slip else 0.003)
        nets.append(g - cost)
    m = statistics.fmean(nets)
    sd = statistics.pstdev(nets) if len(nets) > 1 else 0.0
    se = sd / (len(nets) ** 0.5) if nets else 0.0
    return {"n": len(nets), "mean": m, "win": sum(1 for x in nets if x > 0) / len(nets),
            "med": statistics.median(nets), "se": se, "worst": min(nets), "sum": sum(nets)}


def main():
    print("=" * 80)
    print("РЕШАЮЩИЙ P&D ФЕЙД-БЭКТЕСТ — Gate микрокап-перпы, ~250д, train/test, слиппедж")
    print("=" * 80)
    t0 = time.time()
    syms = universe()
    print(f"Юниверс: {len(syms)} перпов. Тяну ~250д часовых (пагинация)...")
    data = {}
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_hist, s): s for s in syms}
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if r and len(r) > 300:
                data[futs[f]] = r
            if done % 150 == 0:
                print(f"  ...{done}/{len(syms)}")
    span = max((len(r) for r in data.values()), default=0)
    print(f"Данные: {len(data)} монет, до {span}ч (~{span/24:.0f}д), загрузка {time.time()-t0:.0f}с\n")

    # ЗАФИКСИРОВАННОЕ определение (по итогам 83д-разведки — лучший устойчивый класс):
    # внезапный +30% за 6ч, объём ≥5× нормы, из тихой базы (ext<0.2). Плюс 2 контроля.
    DEFS = {
        "LOCKED: +30%/6ч, vol≥5×, из базы(ext<0.2)": dict(W=6, thr=0.30, volmult=5, minliq=20000, ext_max=0.2, dedup_h=12),
        "контроль: резкий +30%/3ч, vol≥5×, ext<0.2": dict(W=3, thr=0.30, volmult=5, minliq=20000, ext_max=0.2, dedup_h=12),
        "контроль: климакс vol≥10×, ext<0.2": dict(W=6, thr=0.30, volmult=10, minliq=20000, ext_max=0.2, dedup_h=12),
    }
    SIM = dict(target=0.10, stop=0.30, hold=72)

    for name, dcfg in DEFS.items():
        trades = []
        for rows in data.values():
            trades += short_sim(rows, detect(rows, **dcfg), **SIM)
        if len(trades) < 60:
            print(f"▶ {name}: событий мало ({len(trades)})\n")
            continue
        trades.sort(key=lambda x: x[1])
        mid = trades[len(trades) // 2][1]
        tr = [x for x in trades if x[1] < mid]
        te = [x for x in trades if x[1] >= mid]
        print(f"▶ {name}   (всего {len(trades)} событий)")
        for slip in (False, True):
            tag = "С СЛИППЕДЖЕМ" if slip else "без слиппеджа "
            a, b = agg(tr, with_slip=slip), agg(te, with_slip=slip)
            ok = "✅" if (a and b and a["n"] >= 30 and b["n"] >= 30
                         and a["mean"] > 0.005 and b["mean"] > 0.005) else "❌"
            print(f"   {ok} {tag}: "
                  f"TRAIN mean={a['mean']*100:+.2f}%±{a['se']*100:.1f} win={a['win']*100:.0f}% n={a['n']}  |  "
                  f"TEST mean={b['mean']*100:+.2f}%±{b['se']*100:.1f} win={b['win']*100:.0f}% n={b['n']}")
        print()

    # Разбивка эджа по ликвидности (где он живёт и переживает ли слиппедж)
    print("─" * 80)
    print("LOCKED-определение: эдж по тирам ликвидности (С слиппеджем):")
    allt = []
    for rows in data.values():
        allt += short_sim(rows, detect(rows, **DEFS[next(iter(DEFS))]), **SIM)
    tiers = {"<$50k/ч": [], "$50-200k": [], "$200k-1M": [], ">$1M": []}
    for g, t, liq in allt:
        key = "<$50k/ч" if liq < 50000 else "$50-200k" if liq < 200000 else "$200k-1M" if liq < 1e6 else ">$1M"
        tiers[key].append(g - FEES - slippage(liq))
    for k, v in tiers.items():
        if v:
            print(f"   {k:9}: n={len(v):4} mean={statistics.fmean(v)*100:+.2f}%  win={sum(1 for x in v if x>0)/len(v)*100:.0f}%")
    print("=" * 80)
    print("ВЕРДИКТ: эдж РЕАЛЕН только если LOCKED ✅ С слиппеджем в ОБЕИХ половинах.")
    print("=" * 80)


if __name__ == "__main__":
    main()
