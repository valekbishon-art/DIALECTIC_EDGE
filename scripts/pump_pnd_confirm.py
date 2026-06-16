"""scripts/pump_pnd_confirm.py — ПОДТВЕРЖДАЮЩИЙ бэктест строгого P&D фейда.

Цель: на 83 днях строгий P&D-фейд выживал слиппедж ТОЛЬКО в средней полосе
ликвидности ($50k-1M/ч), но выборка была мала (~33 сделки). Здесь берём ~333
дня (4× история) через 4-часовые свечи — ОДИН запрос на монету (без пагинации,
обходим троттлинг Gate) — чтобы довести рабочую полосу до ~100+ сделок и
увидеть, держится ли эдж.

4h-разрешение грубее для стоп/цель, чем 1h — это компромисс ради объёма данных
и обхода троттла. Слиппедж и фандинг — как в pump_pnd_backtest (слиппедж по
тирам ликвидности, фандинг-тейлвинд опускаем = консервативно).

Запуск:  python scripts/pump_pnd_confirm.py
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
BAR = 4  # часов в баре


def get(u, t=30):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=t).read())


def universe():
    return [c["name"] for c in get(G + "/futures/usdt/contracts") if not c.get("in_delisting")]


def fetch_4h(name):
    """~333 дня 4-часовых свечей одним запросом (limit 2000)."""
    try:
        k = get(f"{G}/futures/usdt/candlesticks?contract={name}&interval=4h&limit=2000")
    except Exception:  # noqa: BLE001
        return None
    return [(int(x["t"]), float(x["o"]), float(x["h"]), float(x["l"]),
             float(x["c"]), float(x["sum"])) for x in k]


def slippage(liq_hourly: float) -> float:
    if liq_hourly < 50_000:
        return 0.030
    if liq_hourly < 200_000:
        return 0.015
    if liq_hourly < 1_000_000:
        return 0.007
    return 0.003


FEES = 0.0012

# окна в БАРАХ (1 бар = 4ч)
TRAIL = 42      # 7 дней
PRIOR = 18      # 3 дня
HOLD = 18       # 72 часа


def detect(rows, *, W_bars, thr, volmult, minliq, ext_max, dedup_bars):
    ev = []
    last = -10 ** 9
    n = len(rows)
    for i in range(TRAIL + PRIOR, n - 1):
        if i - last < dedup_bars:
            continue
        c = rows[i][4]
        c_w = rows[i - W_bars][4]
        if c_w <= 0 or c / c_w - 1.0 < thr:
            continue
        liq = statistics.fmean([rows[k][5] for k in range(i - TRAIL, i)]) / BAR or 1.0  # $/ч
        if liq < minliq:
            continue
        win_qv = sum(rows[k][5] for k in range(i - W_bars + 1, i + 1))
        base_window = (statistics.fmean([rows[k][5] for k in range(i - TRAIL, i)])) * W_bars
        if win_qv < volmult * base_window:
            continue
        prior = c_w / rows[i - W_bars - PRIOR][4] - 1.0
        if prior > ext_max:
            continue
        ev.append({"i": i, "t": rows[i][0], "p0": c, "liq": liq})
        last = i
    return ev


def short_sim(rows, ev, *, target, stop):
    out = []
    n = len(rows)
    for e in ev:
        i, p0 = e["i"], e["p0"]
        tgt, stp = p0 * (1 - target), p0 * (1 + stop)
        g = None
        for j in range(i + 1, min(i + 1 + HOLD, n)):
            if rows[j][2] >= stp:
                g = (p0 - stp) / p0
                break
            if rows[j][3] <= tgt:
                g = (p0 - tgt) / p0
                break
        if g is None:
            g = (p0 - rows[min(i + HOLD, n - 1)][4]) / p0
        out.append((g, e["t"], e["liq"]))
    return out


def agg(trades, *, with_slip):
    if not trades:
        return None
    nets = [g - FEES - (slippage(liq) if with_slip else 0.003) for g, _, liq in trades]
    m = statistics.fmean(nets)
    sd = statistics.pstdev(nets) if len(nets) > 1 else 0.0
    return {"n": len(nets), "mean": m, "win": sum(1 for x in nets if x > 0) / len(nets),
            "se": sd / (len(nets) ** 0.5) if nets else 0.0}


def main():
    print("=" * 78, flush=True)
    print("ПОДТВЕРЖДАЮЩИЙ P&D ФЕЙД — Gate, ~333д (4h), слиппедж, полоса ликвидности", flush=True)
    print("=" * 78, flush=True)
    t0 = time.time()
    syms = universe()
    print(f"Юниверс: {len(syms)}. Тяну ~333д 4h-свечей (1 запрос/монета)...", flush=True)
    data = {}
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_4h, s): s for s in syms}
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if r and len(r) > TRAIL + PRIOR + HOLD + 20:
                data[futs[f]] = r
            if done % 150 == 0:
                print(f"  ...{done}/{len(syms)}", flush=True)
    span = max((len(r) for r in data.values()), default=0)
    print(f"Данные: {len(data)} монет, до {span} баров (~{span*BAR/24:.0f}д), {time.time()-t0:.0f}с\n", flush=True)

    LOCKED = dict(W_bars=2, thr=0.30, volmult=5, minliq=20000, ext_max=0.2, dedup_bars=3)
    SIM = dict(target=0.10, stop=0.30)

    allt = []
    for rows in data.values():
        allt += short_sim(rows, detect(rows, **LOCKED), **SIM)
    print(f"LOCKED (+30%/8ч, vol≥5×, из базы): всего {len(allt)} событий", flush=True)
    allt.sort(key=lambda x: x[1])
    mid = allt[len(allt) // 2][1]
    tr = [x for x in allt if x[1] < mid]
    te = [x for x in allt if x[1] >= mid]
    print("\nTRAIN/TEST (обе должны быть + → эдж):", flush=True)
    for slip in (False, True):
        a, b = agg(tr, with_slip=slip), agg(te, with_slip=slip)
        ok = "✅" if (a["mean"] > 0.005 and b["mean"] > 0.005) else "❌"
        tag = "С слиппеджем" if slip else "без слип.   "
        print(f"  {ok} {tag}: TRAIN {a['mean']*100:+.2f}%±{a['se']*100:.1f} win{a['win']*100:.0f}% n{a['n']}  |  "
              f"TEST {b['mean']*100:+.2f}%±{b['se']*100:.1f} win{b['win']*100:.0f}% n{b['n']}", flush=True)

    print("\nПО ПОЛОСЕ ЛИКВИДНОСТИ (со слиппеджем) — где живёт эдж и сколько сделок:", flush=True)
    tiers = {"<$50k/ч": [], "$50-200k/ч": [], "$200k-1M/ч": [], ">$1M/ч": []}
    for g, _, liq in allt:
        k = ("<$50k/ч" if liq < 50000 else "$50-200k/ч" if liq < 200000
             else "$200k-1M/ч" if liq < 1e6 else ">$1M/ч")
        tiers[k].append(g - FEES - slippage(liq))
    for k, v in tiers.items():
        if v:
            m = statistics.fmean(v)
            sd = statistics.pstdev(v) if len(v) > 1 else 0
            se = sd / (len(v) ** 0.5)
            print(f"  {k:11}: n={len(v):4} mean={m*100:+.2f}%±{se*100:.1f}  win={sum(1 for x in v if x>0)/len(v)*100:.0f}%", flush=True)

    # тот же тир split train/test — устойчив ли эдж средней полосы во ВРЕМЕНИ
    print("\nСРЕДНЯЯ ПОЛОСА ($50k-1M/ч) train/test (со слиппеджем) — финальный тест:", flush=True)
    band = [x for x in allt if 50000 <= x[2] < 1e6]
    band.sort(key=lambda x: x[1])
    if len(band) >= 40:
        bm = band[len(band) // 2][1]
        a = agg([x for x in band if x[1] < bm], with_slip=True)
        b = agg([x for x in band if x[1] >= bm], with_slip=True)
        ok = "ДА ✅" if a["mean"] > 0.005 and b["mean"] > 0.005 else "НЕТ ❌"
        print(f"  TRAIN {a['mean']*100:+.2f}%±{a['se']*100:.1f} n{a['n']}  |  TEST {b['mean']*100:+.2f}%±{b['se']*100:.1f} n{b['n']}", flush=True)
        print(f"  ЭДЖ ПОДТВЕРЖДЁН: {ok}", flush=True)
    else:
        print(f"  событий в полосе мало ({len(band)}) — нужно ещё истории", flush=True)
    print("=" * 78, flush=True)


if __name__ == "__main__":
    main()
