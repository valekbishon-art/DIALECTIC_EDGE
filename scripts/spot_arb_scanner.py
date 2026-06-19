"""scripts/spot_arb_scanner.py — спот-арбитраж сканер (cross-exchange).

Купить монету дешевле на бирже A, продать дороже на B (с владением, без плеча/шорта/деривативов).
Тянет СПОТ-цены одного актива на нескольких биржах (Gate + MEXC — доступны; Binance геоблок),
считает спред, флагует где спред > порога ПОСЛЕ грубых костов (комса 2 ноги + вывод/перевод).

⚠️ Это СКАНЕР возможностей, не автотрейд. Спред на ликвиде мал (биржи арбят друг друга) и эдж —
в скорости/комиссиях/выводе. Решение и исполнение — руками, с проверкой ликвидности обеих книг.

Запуск:  py scripts/spot_arb_scanner.py [--min 0.5]   # порог нетто-спреда %
"""
from __future__ import annotations
import argparse, json, sys, time, urllib.request

FEE_LEG = 0.10        # % спот-комса за ногу (taker)
TRANSFER = 0.10       # % грубая оценка вывода/перевода между биржами


def get(u, t=20):
    last = None
    for a in range(3):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=t).read())
        except Exception as e:
            last = e; time.sleep(0.6 * (a + 1))
    raise last


def gate_spot():
    """{BASE: last_price} по *_USDT спот-парам Gate."""
    out = {}
    for x in get("https://api.gateio.ws/api/v4/spot/tickers"):
        cp = x.get("currency_pair", "")
        if cp.endswith("_USDT"):
            try:
                out[cp[:-5]] = float(x["last"])
            except (KeyError, ValueError, TypeError):
                pass
    return out


def mexc_spot():
    """{BASE: price} по *USDT спот-парам MEXC."""
    out = {}
    for x in get("https://api.mexc.com/api/v3/ticker/price"):
        s = x.get("symbol", "")
        if s.endswith("USDT"):
            try:
                out[s[:-4]] = float(x["price"])
            except (KeyError, ValueError, TypeError):
                pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=0.5, help="порог НЕТТО-спреда, %%")
    ap.add_argument("--max", type=float, default=8.0,
                    help="потолок спреда %%: выше = почти точно КОЛЛИЗИЯ ТИКЕРА (разные токены) "
                         "или мёртвый рынок, не арбитраж")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("СПОТ-АРБ · Gate ↔ MEXC · спот, без плеча/шорта", flush=True)
    g, m = gate_spot(), mexc_spot()
    cost = FEE_LEG * 2 + TRANSFER
    common = sorted(set(g) & set(m))
    print(f"общих спот-активов: {len(common)} | кост-оценка {cost:.2f}% (2 комсы + вывод) | "
          f"порог нетто {args.min}%\n", flush=True)
    opps = []
    collisions = 0
    for a in common:
        pg, pm = g[a], m[a]
        if pg <= 0 or pm <= 0:
            continue
        lo, hi = (pg, pm) if pg < pm else (pm, pg)
        spread = (hi / lo - 1) * 100
        if spread > args.max:           # потолок: отсекаем коллизии тикеров / мёртвые рынки
            collisions += 1
            continue
        net = spread - cost
        if net >= args.min:
            buy = "Gate" if pg < pm else "MEXC"
            sell = "MEXC" if pg < pm else "Gate"
            opps.append((net, a, spread, buy, sell, lo, hi))
    opps.sort(reverse=True)
    print(f"отброшено как коллизия тикера/мёртвый рынок (спред>{args.max}%): {collisions}\n", flush=True)
    if not opps:
        print("💤 Нетто-спредов выше порога нет (норма для ликвида — биржи арбят друг друга).")
        print("   Спот-арб живёт в РЕДКИХ дислокациях/тонких парах + быстром исполнении.")
        return 0
    print(f"⚡ {len(opps)} возможностей (нетто после костов):")
    print(f"  {'актив':8s}{'нетто%':>8s}{'спред%':>8s}  купить→продать")
    for net, a, spread, buy, sell, lo, hi in opps[:25]:
        print(f"  {a:8s}{net:+7.2f}%{spread:7.2f}%  КУПИ спот на {buy} (${lo:.6g}) → "
              f"ПРОДАЙ на {sell} (${hi:.6g})")
    print("\n⚠️ Проверь руками: ликвидность ОБЕИХ книг, реальный вывод/сеть, мин-объёмы, "
          "не скам-листинг ли тонкая пара. Спред на бумаге ≠ исполнимый.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
