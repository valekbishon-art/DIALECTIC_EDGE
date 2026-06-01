"""scripts/research_smallcap_mom.py — cross-sectional momentum на ШИРОКОМ юниверсе.

Пробел прошлых тестов: гоняли momentum только на 5-15 МАЖОРАХ (самые эффективные).
Retail-неэффективность живёт в долгом хвосте. Тут — топ-120 ликвидных перпов
(вкл. мелкие альты). Cross-sectional long top-decile / short bottom-decile,
рыночно-нейтрально. Главный критерий: жив ли в 2025-2026 (на мажорах momentum
сдох именно там).

Источник: Binance fapi 24hr ticker (отбор ликвидных) + daily klines.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass
import numpy as np  # noqa: E402
from core.backtest_validate import bootstrap_ci, mean  # noqa: E402

FAPI = "https://fapi.binance.com"
TOP_N = 120        # сколько ликвидных перпов берём
LOOKBACK = 30      # momentum-окно
HOLD = 14          # период удержания (и шаг = неперекрытие)
DECILE = 0.20      # доля в long/short ноге (0.2 = top/bottom 20%)
FEE = 0.0005


def _get(path):
    req = urllib.request.Request(FAPI + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def liquid_symbols(n):
    t = _get("/fapi/v1/ticker/24hr")
    usdt = [(x["symbol"], float(x["quoteVolume"])) for x in t
            if x["symbol"].endswith("USDT")]
    usdt.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in usdt[:n]]


def fetch_daily(sym):
    out = {}
    try:
        k = _get(f"/fapi/v1/klines?symbol={sym}&interval=1d&limit=1500")
        for row in k:
            d = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            out[d] = float(row[4])
    except Exception:  # noqa: BLE001
        pass
    return sym, out


def main():
    syms = liquid_symbols(TOP_N)
    print(f"Ликвидных перпов: {len(syms)} (топ по объёму)")
    with ThreadPoolExecutor(max_workers=16) as ex:
        data = dict(ex.map(fetch_daily, syms))
    data = {s: v for s, v in data.items() if len(v) > 200}
    print(f"С историей >200д: {len(data)}")

    # общая ось дат
    all_dates = sorted(set().union(*[set(v) for v in data.values()]))
    closes = {s: {d: v.get(d) for d in all_dates} for s, v in data.items()}
    n = len(all_dates)
    print(f"Дней: {n} ({all_dates[0]}..{all_dates[-1]})")

    def ret(s, i, j):
        a, b = closes[s].get(all_dates[i]), closes[s].get(all_dates[j])
        if a and b and a > 0:
            return (b - a) / a
        return None

    cost = FEE * 2 * 2
    for signal in ("mom", "rev"):
        trades = []
        for i in range(LOOKBACK, n - HOLD, HOLD):
            scored = []
            for s in data:
                past = ret(s, i - LOOKBACK, i)
                fwd = ret(s, i, i + HOLD)
                if past is None or fwd is None:
                    continue
                scored.append((s, past, fwd))
            if len(scored) < 20:
                continue
            scored.sort(key=lambda x: x[1])
            k = max(1, int(len(scored) * DECILE))
            if signal == "mom":
                longs, shorts = scored[-k:], scored[:k]
            else:
                longs, shorts = scored[:k], scored[-k:]
            lr = mean([x[2] for x in longs])
            sr = mean([x[2] for x in shorts])
            trades.append((all_dates[i], ((lr - sr) - cost) * 100.0))
        if not trades:
            continue
        pnls = [t[1] for t in trades]
        b = bootstrap_ci(pnls)
        print(f"\n=== {signal.upper()} cross-sectional (top/bot {int(DECILE*100)}%, "
              f"{len(data)} активов, hold={HOLD}d неперекрытие) ===")
        print(f"N={b['n']}  mean {b['mean']:+.3f}%  CI[{b['ci_low']:+.2f};{b['ci_high']:+.2f}]  "
              f"p+ {b['p_positive']:.2f}  total {sum(pnls):+.0f}%")
        for yr in ("2021", "2022", "2023", "2024", "2025", "2026"):
            seg = [p for (d, p) in trades if d[:4] == yr]
            if len(seg) >= 3:
                by = bootstrap_ci(seg)
                print(f"  {yr}: n={len(seg):3d} mean {by['mean']:+.2f} p+ {by['p_positive']:.2f}")


if __name__ == "__main__":
    main()
