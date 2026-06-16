"""scripts/pump_edge_search.py — СИСТЕМНЫЙ поиск памп-эджа на MEXC + Gate.

Ищем ЛЮБОЙ устойчивый эдж в пампах: оба направления (фейд-шорт И моментум-лонг),
6 типов входа × несколько выходов, на ДВУХ биржах независимо.

Анти-самообман (против оверфита/p-hacking): конфиг считаем ЭДЖЕМ только если он
плюсовой со слиппеджем на ТЕСТЕ Gate И на ТЕСТЕ MEXC одновременно (+ train).
Две независимые биржи, разные монеты, разделение по времени = 4 подтверждения.

4h-свечи, ~333 дня, 1 запрос/монета. Слиппедж по тирам ликвидности. Фандинг-
тейлвинд опускаем (консервативно). Без внешних зависимостей.

Запуск:  python scripts/pump_edge_search.py
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

BAR = 4
TRAIL, PRIOR, FEES = 42, 18, 0.0012


def get(u, t=30):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=t).read())


def slippage(liq):
    return 0.030 if liq < 50_000 else 0.015 if liq < 200_000 else 0.007 if liq < 1_000_000 else 0.003


# ── Gate ──
def gate_universe():
    return [c["name"] for c in get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
            if not c.get("in_delisting")]


def gate_fetch(name):
    try:
        k = get(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={name}&interval=4h&limit=2000")
    except Exception:  # noqa: BLE001
        return None
    return sorted([(int(x["t"]), float(x["o"]), float(x["h"]), float(x["l"]),
                    float(x["c"]), float(x["sum"])) for x in k])


# ── MEXC ──
def mexc_universe():
    d = get("https://contract.mexc.com/api/v1/contract/detail")
    return [c["symbol"] for c in d["data"] if c.get("quoteCoin") == "USDT" and c.get("state") == 0]


def mexc_fetch(name):
    now = int(time.time())
    try:
        k = get(f"https://contract.mexc.com/api/v1/contract/kline/{name}?interval=Hour4&start={now-2000*4*3600}&end={now}")
        d = k["data"]
        rows = list(zip([int(t) for t in d["time"]], map(float, d["open"]), map(float, d["high"]),
                        map(float, d["low"]), map(float, d["close"]), map(float, d["amount"])))
        return sorted(rows)
    except Exception:  # noqa: BLE001
        return None


def pull(universe_fn, fetch_fn, label):
    syms = universe_fn()
    print(f"  {label}: {len(syms)} перпов, тяну 4h...", flush=True)
    data = {}
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_fn, s): s for s in syms}
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if r and len(r) > TRAIL + PRIOR + 30:
                data[futs[f]] = r
            if done % 200 == 0:
                print(f"    {label} ...{done}/{len(syms)}", flush=True)
    print(f"  {label}: {len(data)} монет загружено", flush=True)
    return data


def detect(rows, *, W, thr, volmult, ext_max):
    ev = []
    last = -10 ** 9
    n = len(rows)
    for i in range(TRAIL + PRIOR, n - 1):
        if i - last < 3:
            continue
        c, c_w = rows[i][4], rows[i - W][4]
        if c_w <= 0 or c / c_w - 1.0 < thr:
            continue
        liq = statistics.fmean([rows[k][5] for k in range(i - TRAIL, i)]) / BAR or 1.0
        if liq < 20000:
            continue
        win = sum(rows[k][5] for k in range(i - W + 1, i + 1))
        if win < volmult * statistics.fmean([rows[k][5] for k in range(i - TRAIL, i)]) * W:
            continue
        if c_w / rows[i - W - PRIOR][4] - 1.0 > ext_max:
            continue
        ev.append((i, c, liq, rows[i][0]))
        last = i
    return ev


def sim(rows, events, *, direction, target, stop, hold):
    out = []
    n = len(rows)
    for i, p0, liq, t in events:
        if direction == "short":
            tgt, stp = p0 * (1 - target), p0 * (1 + stop)
            g = None
            for j in range(i + 1, min(i + 1 + hold, n)):
                if rows[j][2] >= stp:
                    g = (p0 - stp) / p0
                    break
                if rows[j][3] <= tgt:
                    g = (p0 - tgt) / p0
                    break
            if g is None:
                g = (p0 - rows[min(i + hold, n - 1)][4]) / p0
        else:  # long / momentum
            tgt, stp = p0 * (1 + target), p0 * (1 - stop)
            g = None
            for j in range(i + 1, min(i + 1 + hold, n)):
                if rows[j][3] <= stp:
                    g = (stp - p0) / p0
                    break
                if rows[j][2] >= tgt:
                    g = (tgt - p0) / p0
                    break
            if g is None:
                g = (rows[min(i + hold, n - 1)][4] - p0) / p0
        out.append((g - FEES - slippage(liq), t))
    return out


def split_stats(trades):
    if len(trades) < 30:
        return None
    trades = sorted(trades, key=lambda x: x[1])
    mid = trades[len(trades) // 2][1]
    def st(sub):
        r = [x[0] for x in sub]
        return (statistics.fmean(r), len(r)) if r else (0, 0)
    return st([x for x in trades if x[1] < mid]), st([x for x in trades if x[1] >= mid])


ENTRIES = {
    "E1 +30%/8ч vol5 база":  dict(W=2, thr=0.30, volmult=5, ext_max=0.2),
    "E2 +30%/4ч vol5 база":  dict(W=1, thr=0.30, volmult=5, ext_max=0.2),
    "E3 +50%/8ч vol5 база":  dict(W=2, thr=0.50, volmult=5, ext_max=0.2),
    "E4 +20%/8ч vol3 база":  dict(W=2, thr=0.20, volmult=3, ext_max=0.2),
    "E5 +30%/8ч vol10 клим": dict(W=2, thr=0.30, volmult=10, ext_max=10.0),
    "E6 +30%/8ч vol5 любая": dict(W=2, thr=0.30, volmult=5, ext_max=10.0),
}
EXITS = {
    "short": {
        "S-fast t4/s10/12ч":  dict(target=0.04, stop=0.10, hold=3),
        "S-med  t6/s20/24ч":  dict(target=0.06, stop=0.20, hold=6),
        "S-wide t10/s30/72ч": dict(target=0.10, stop=0.30, hold=18),
        "S-time 24ч":         dict(target=0.99, stop=0.99, hold=6),
    },
    "long": {
        "L-scalp t8/s5/12ч":  dict(target=0.08, stop=0.05, hold=3),
        "L-ride t20/s10/72ч": dict(target=0.20, stop=0.10, hold=18),
        "L-mom  t15/s8/24ч":  dict(target=0.15, stop=0.08, hold=6),
        "L-time 24ч":         dict(target=9.0, stop=0.99, hold=6),
    },
}


def main():
    print("=" * 92, flush=True)
    print("СИСТЕМНЫЙ ПОИСК ПАМП-ЭДЖА — MEXC + Gate, оба направления, кросс-биржевая OOS-валидация", flush=True)
    print("=" * 92, flush=True)
    t0 = time.time()
    gate = pull(gate_universe, gate_fetch, "Gate")
    mexc = pull(mexc_universe, mexc_fetch, "MEXC")
    print(f"Загрузка {time.time()-t0:.0f}с\n", flush=True)

    # детект событий по входам один раз на биржу
    gate_ev = {e: {c: detect(r, **cfg) for c, r in gate.items()} for e, cfg in ENTRIES.items()}
    mexc_ev = {e: {c: detect(r, **cfg) for c, r in mexc.items()} for e, cfg in ENTRIES.items()}

    print(f"{'КОНФИГ':38} {'GATE tr/te (n)':22} {'MEXC tr/te (n)':22} ЭДЖ", flush=True)
    print("-" * 92, flush=True)
    winners = []
    for direction in ("short", "long"):
        for ename in ENTRIES:
            for xname, xcfg in EXITS[direction].items():
                gt = [t for c, r in gate.items() for t in sim(r, gate_ev[ename][c], direction=direction, **xcfg)]
                mt = [t for c, r in mexc.items() for t in sim(r, mexc_ev[ename][c], direction=direction, **xcfg)]
                gs, ms = split_stats(gt), split_stats(mt)
                if not gs or not ms:
                    continue
                (gtr, gtrn), (gte, gten) = gs
                (mtr, mtrn), (mte, mten) = ms
                edge = all(v > 0.005 for v in (gtr, gte, mtr, mte)) and min(gten, mten) >= 25
                tag = "✅✅✅✅" if edge else ("✅te" if gte > 0.005 and mte > 0.005 else "")
                label = f"{direction[0].upper()}|{ename[:14]}|{xname[:14]}"
                print(f"{label:38} {gtr*100:+5.1f}/{gte*100:+5.1f}({gten:4}) "
                      f"{mtr*100:+5.1f}/{mte*100:+5.1f}({mten:4}) {tag}", flush=True)
                if edge:
                    winners.append((label, gte, mte, gten, mten))

    print("=" * 92, flush=True)
    if winners:
        print(f"🎯 НАЙДЕНО {len(winners)} конфигов с эджем на ОБЕИХ биржах (train+test):", flush=True)
        for w in sorted(winners, key=lambda x: -min(x[1], x[2])):
            print(f"   {w[0]}  Gate-test {w[1]*100:+.2f}% (n{w[3]})  MEXC-test {w[2]*100:+.2f}% (n{w[4]})", flush=True)
    else:
        print("❌ НИ ОДИН конфиг не дал эдж на обеих биржах в обеих половинах. Эджа нет.", flush=True)
    print("=" * 92, flush=True)


if __name__ == "__main__":
    main()
