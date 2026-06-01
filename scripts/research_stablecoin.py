"""scripts/research_stablecoin.py — stablecoin supply как сигнал «сухого пороха».

Гипотеза: рост stablecoin supply (минты USDT/USDC) = деньги заходят в крипту →
предсказывает forward-доходность рынка. Это НАПРАВЛЕННЫЙ сигнал (тайминг рынка).

Тест:
  1. IC (Spearman) supply-Δ vs forward BTC-return — по годам.
  2. Стратегия long-when-growing vs buy&hold — переживает ли бык И медведь.
Источник: DefiLlama totalCirculatingUSD.peggedUSD (дневной, с 2017).
"""
from __future__ import annotations

import os
import sys
import urllib.request
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass
import numpy as np  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402

from core.backtest_engine import DATA_DIR, load_candles  # noqa: E402

BULL = os.path.join(os.path.dirname(DATA_DIR), "backtest_bull")
LOOKBACK = 30   # окно изменения supply
HORIZON = 14    # forward-горизонт доходности


def stablecoin_series() -> dict:
    url = "https://stablecoins.llama.fi/stablecoincharts/all"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    out = {}
    for pt in data:
        try:
            d = datetime.fromtimestamp(int(pt["date"]), tz=timezone.utc).strftime("%Y-%m-%d")
            usd = pt["totalCirculatingUSD"].get("peggedUSD")
            if usd:
                out[d] = float(usd)
        except Exception:  # noqa: BLE001
            pass
    return out


def btc_series() -> dict:
    out = {}
    for d in (BULL, DATA_DIR):
        try:
            for c in load_candles("BTC", data_dir=d):
                out[c.timestamp.strftime("%Y-%m-%d")] = c.close
        except Exception:  # noqa: BLE001
            pass
    return out


def main():
    sc = stablecoin_series()
    btc = btc_series()
    dates = sorted(set(sc) & set(btc))
    print(f"Stablecoin supply: {len(sc)} дней | BTC: {len(btc)} | общих: {len(dates)} "
          f"({dates[0]}..{dates[-1]})")
    sup = np.array([sc[d] for d in dates])
    px = np.array([btc[d] for d in dates])
    n = len(dates)

    rows = []  # (date, supply_chg, fwd_ret)
    for t in range(LOOKBACK, n - HORIZON):
        if sup[t - LOOKBACK] <= 0 or px[t] <= 0:
            continue
        sup_chg = (sup[t] - sup[t - LOOKBACK]) / sup[t - LOOKBACK]
        fwd = (px[t + HORIZON] - px[t]) / px[t]
        rows.append((dates[t], sup_chg, fwd))

    sc_chg = np.array([r[1] for r in rows])
    fwd = np.array([r[2] for r in rows])
    ic, p = spearmanr(sc_chg, fwd)
    print(f"\n=== IC (Spearman supply-Δ{LOOKBACK}d vs fwd-ret {HORIZON}d) ===")
    print(f"overall IC={ic:+.3f}  p={p:.4f}  N={len(rows)}")
    print("по годам:")
    for yr in ("2021", "2022", "2023", "2024", "2025", "2026"):
        seg = [(c, f) for (d, c, f) in rows if d[:4] == yr]
        if len(seg) >= 30:
            c = np.array([x[0] for x in seg]); f = np.array([x[1] for x in seg])
            ic_y, p_y = spearmanr(c, f)
            print(f"  {yr}: IC={ic_y:+.3f}  p={p_y:.3f}  n={len(seg)}")

    # стратегия: long BTC когда supply растёт (>0 за lookback), иначе кэш. Non-overlap.
    print(f"\n=== стратегия long-when-supply-growing (вход раз в {HORIZON}d, vs buy&hold) ===")
    strat, bh = [], []
    for t in range(LOOKBACK, n - HORIZON, HORIZON):
        if sup[t - LOOKBACK] <= 0:
            continue
        grow = (sup[t] - sup[t - LOOKBACK]) > 0
        fwd_ret = (px[t + HORIZON] - px[t]) / px[t] * 100.0
        bh.append(fwd_ret)
        strat.append(fwd_ret if grow else 0.0)
    import numpy as _np
    print(f"  buy&hold:        ср/период {_np.mean(bh):+.2f}%  total {sum(bh):+.0f}%  N={len(bh)}")
    print(f"  long-when-grow:  ср/период {_np.mean(strat):+.2f}%  total {sum(strat):+.0f}%  "
          f"(в рынке {sum(1 for s in strat if s!=0)}/{len(strat)} периодов)")
    print("  по годам (strat / buy&hold ср.период):")
    for yr in ("2021", "2022", "2023", "2024", "2025", "2026"):
        idx = [i for i, t in enumerate(range(LOOKBACK, n - HORIZON, HORIZON)) if dates[t][:4] == yr]
        # пересоберём по годам аккуратно
    # year breakdown
    by = {}
    j = 0
    for t in range(LOOKBACK, n - HORIZON, HORIZON):
        if sup[t - LOOKBACK] <= 0:
            continue
        yr = dates[t][:4]
        grow = (sup[t] - sup[t - LOOKBACK]) > 0
        fwd_ret = (px[t + HORIZON] - px[t]) / px[t] * 100.0
        by.setdefault(yr, []).append((fwd_ret if grow else 0.0, fwd_ret))
    for yr in sorted(by):
        s = [x[0] for x in by[yr]]; h = [x[1] for x in by[yr]]
        print(f"    {yr}: strat {_np.mean(s):+.2f}  buy&hold {_np.mean(h):+.2f}  (n{len(s)})")


if __name__ == "__main__":
    main()
