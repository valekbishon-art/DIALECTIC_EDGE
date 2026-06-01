"""scripts/research_cointegration.py — стат-арб на коинтегрированных парах.

Рыночно-нейтральный mean-reversion спреда. Для каждой пары (A,B):
  • rolling-OLS hedge-ratio beta на окне W (только прошлое, без look-ahead)
  • spread S = logA - beta*logB; z-score по rolling mean/std (окно W)
  • вход когда |z|>=Z_ENTRY (флэт), выход когда |z|<=Z_EXIT или |z|>=Z_STOP
  • PnL спреда (long/short-нейтрал) минус косты (4 ноги)

Трейдим ВСЕ пары без отбора ex-post (иначе multiple-testing). Честный критерий:
агрегат по всем парам положителен в И бык, И медведь. По годам + bootstrap.
"""
from __future__ import annotations

import os
import sys
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402

from core.backtest_engine import DATA_DIR, load_candles  # noqa: E402
from core.backtest_validate import bootstrap_ci, mean  # noqa: E402

ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX",
          "LINK", "DOT", "TRX", "TON", "LTC", "NEAR", "SUI"]
BULL = os.path.join(os.path.dirname(DATA_DIR), "backtest_bull")

W = 60            # окно для beta и z-score
Z_ENTRY = 2.0
Z_EXIT = 0.5
Z_STOP = 4.0
FEE = 0.001       # на ногу; round-trip спреда = 4 ноги
COST = FEE * 4


def merged(a):
    out = {}
    for d in (BULL, DATA_DIR):
        try:
            for c in load_candles(a, data_dir=d):
                out[c.timestamp.strftime("%Y-%m-%d")] = c.close
        except Exception:  # noqa: BLE001
            pass
    return out


def adf_proxy_tstat(spread: np.ndarray) -> float:
    """ADF-подобный t-стат: Δs_t = α + γ·s_{t-1}. γ<0 и |t| велик → mean-reverting.

    Возвращает t-стат γ (сильно отрицательный = коинтеграция/стационарность).
    """
    s_lag = spread[:-1]
    ds = np.diff(spread)
    x = np.vstack([np.ones_like(s_lag), s_lag]).T
    try:
        coef, *_ = np.linalg.lstsq(x, ds, rcond=None)
        resid = ds - x @ coef
        dof = len(ds) - 2
        if dof <= 0:
            return 0.0
        s2 = (resid @ resid) / dof
        xtx_inv = np.linalg.inv(x.T @ x)
        se_gamma = np.sqrt(s2 * xtx_inv[1, 1])
        if se_gamma <= 0:
            return 0.0
        return coef[1] / se_gamma
    except Exception:  # noqa: BLE001
        return 0.0


ADF_T = -2.8   # порог стационарности (грубый ADF-крит ~ -2.86 @5%)


def main():
    closes = {a: merged(a) for a in ASSETS}
    closes = {a: v for a, v in closes.items() if len(v) > 300}
    print(f"Активов: {len(closes)}")

    trades = []  # (exit_date, pnl_pct, pair)
    filtered_in = 0
    pairs = list(combinations(sorted(closes.keys()), 2))
    for a, b in pairs:
        # ПЕР-ПАРНЫЕ общие даты (не теряем историю из-за молодых активов)
        common = sorted(set(closes[a]) & set(closes[b]))
        if len(common) < W + 50:
            continue
        la = np.array([np.log(closes[a][d]) for d in common])
        lb = np.array([np.log(closes[b][d]) for d in common])
        n = len(common)
        pos = 0
        s_entry = 0.0
        for t in range(W, n):
            wa, wb = la[t - W:t], lb[t - W:t]
            vb = np.var(wb)
            if vb <= 0:
                continue
            beta = np.cov(wa, wb)[0, 1] / vb
            spread_win = wa - beta * wb
            mu, sd = spread_win.mean(), spread_win.std()
            if sd <= 0:
                continue
            s_now = la[t] - beta * lb[t]
            z = (s_now - mu) / sd
            if pos == 0:
                # ФИЛЬТР КОИНТЕГРАЦИИ: вход только если спред стационарен в окне
                if adf_proxy_tstat(spread_win) > ADF_T:
                    continue
                if z >= Z_ENTRY:
                    pos, s_entry = -1, s_now
                    filtered_in += 1
                elif z <= -Z_ENTRY:
                    pos, s_entry = +1, s_now
                    filtered_in += 1
            else:
                hit_exit = abs(z) <= Z_EXIT
                hit_stop = abs(z) >= Z_STOP
                if hit_exit or hit_stop or t == n - 1:
                    pnl = (s_now - s_entry) * pos
                    trades.append((common[t], (pnl - COST) * 100.0, f"{a}/{b}"))
                    pos = 0

    if not trades:
        print("Нет сделок.")
        return
    pnls = [t[1] for t in trades]
    b = bootstrap_ci(pnls)
    wins = sum(1 for p in pnls if p > 0)
    print(f"\n=== COINTEGRATION pairs (все {len(pairs)} пар, W={W}, z>={Z_ENTRY}) ===")
    print(f"N сделок: {len(trades)} | win%: {100*wins//len(trades)} | "
          f"mean: {b['mean']:+.3f}%  CI[{b['ci_low']:+.2f};{b['ci_high']:+.2f}]  p+ {b['p_positive']:.2f}")
    total = sum(pnls)
    print(f"total PnL (сумма всех сделок): {total:+.1f}%")
    print("\nпо годам:")
    for yr in ("2021", "2022", "2023", "2024", "2025", "2026"):
        seg = [p for (d, p, _) in trades if d[:4] == yr]
        if len(seg) >= 10:
            by = bootstrap_ci(seg)
            print(f"  {yr}: n={len(seg):4d}  mean {by['mean']:+.3f}  total {sum(seg):+7.1f}  p+ {by['p_positive']:.2f}")


if __name__ == "__main__":
    main()
