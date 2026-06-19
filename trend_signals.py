"""trend_signals.py — трендовая спот-система (только лонг, без плеча/шорта).

Стратегия: держим спот-актив пока цена > SMA(N), иначе сидим в стейбле (0%). Режет просадку
~вдвое против buy-and-hold. Юниверс — отфильтрованный список из asset_filter.eligible_universe()
(по экономике токена). Два режима:
  • сигнал сейчас — что держать (равный вес среди активов в аптренде);
  • бэктест — CAGR / maxDD / по годам на data/daily.

Чистый stdlib. Данные: data/daily/<ASSET>.json (качать: py scripts/fetch_daily_klines.py).
    py trend_signals.py            # сигнал + краткий бэктест
    py trend_signals.py --sma 100
"""
from __future__ import annotations
import argparse, json, os, statistics
from datetime import datetime, timezone

try:
    from asset_filter import eligible_universe
    UNIVERSE = eligible_universe()
except Exception:
    UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "LINK", "DOT", "TRX",
                "LTC", "NEAR", "ATOM", "FIL", "ETC", "BCH", "ICP", "HBAR", "ALGO", "VET"]

DDIR = "data/daily"
SWITCH_COST = 0.0020


def to_sec(ts):
    ts = int(ts)
    if ts > 10 ** 16: return ts / 1e9
    if ts > 10 ** 13: return ts / 1e6
    if ts > 10 ** 10: return ts / 1e3
    return ts


def load(asset):
    p = os.path.join(DDIR, f"{asset}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        d = json.load(f)
    return sorted((to_sec(ts), float(c)) for ts, c in d.items() if float(c) > 0)


def sma(vals, n):
    return statistics.fmean(vals[-n:]) if len(vals) >= n else None


def signal_now(universe, N):
    print(f"=== ТРЕНД-СИГНАЛ СЕЙЧАС (SMA{N}, спот/лонг) ===")
    hold, cash = [], []
    for a in universe:
        s = load(a)
        if not s or len(s) < N + 1:
            continue
        closes = [c for _, c in s]
        ma = sma(closes, N)
        price = closes[-1]
        (hold if price > ma else cash).append((a, price / ma - 1))
    hold.sort(key=lambda x: x[1], reverse=True)
    btc = load("BTC")
    asof = datetime.fromtimestamp(btc[-1][0], tz=timezone.utc).strftime("%Y-%m-%d") if btc else "?"
    print(f"as of {asof}  | в аптренде {len(hold)} из {len(hold)+len(cash)}")
    if hold:
        w = 100.0 / len(hold)
        print(f"\nДЕРЖАТЬ (равный вес {w:.1f}% каждый, спот):")
        for a, ext in hold:
            print(f"   🟢 {a:6s}  +{ext*100:4.0f}% над SMA{N}")
    if cash:
        print(f"\nВ КЭШ/СТЕЙБЛ (ниже SMA{N}): " + ", ".join(a for a, _ in cash))
    print(f"\nПравило: выше SMA{N} → купи спот равным весом; ниже → продай в стейбл.")


def metrics(eq):
    v0, v1 = eq[0][1], eq[-1][1]
    yrs = (eq[-1][0] - eq[0][0]) / (365.25 * 86400)
    cagr = ((v1 / v0) ** (1 / yrs) - 1) * 100 if yrs > 0 and v0 > 0 else 0
    peak, mdd = -1e9, 0
    for _, v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v - peak) / peak)
    return cagr, mdd * 100


def backtest(universe, N):
    series = {a: load(a) for a in universe if load(a) and len(load(a)) > N + 50}
    if not series:
        print("\n(нет данных для бэктеста)")
        return
    dates = sorted(set(s for pts in series.values() for s, _ in pts))
    px = {a: dict(series[a]) for a in series}
    val, eqT = 1.0, [(dates[0], 1.0)]
    val_bh, eqBH = 1.0, [(dates[0], 1.0)]
    state = {a: 0 for a in series}
    hist = {a: [] for a in series}
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        rt, rb = [], []
        for a in series:
            p0, p1 = px[a].get(d0), px[a].get(d1)
            if p0 is None:
                continue
            hist[a].append(p0)
            if p1 and p0 > 0:
                rb.append(p1 / p0 - 1)
            if len(hist[a]) < N:
                continue
            want = 1 if p0 > statistics.fmean(hist[a][-N:]) else 0
            cost = SWITCH_COST if want != state[a] else 0.0
            state[a] = want
            if p1 and p0 > 0:
                rt.append(want * (p1 / p0 - 1) - cost)
        if rt:
            val *= (1 + statistics.fmean(rt))
        if rb:
            val_bh *= (1 + statistics.fmean(rb))
        eqT.append((d1, val)); eqBH.append((d1, val_bh))
    cT, mT = metrics(eqT); cB, mB = metrics(eqBH)
    print(f"\n=== БЭКТЕСТ ({len(series)} активов, {len(dates)} дней) ===")
    print(f"  ТРЕНД SMA{N}:   CAGR {cT:+.1f}%  maxDD {mT:.1f}%")
    print(f"  buy&hold EW:   CAGR {cB:+.1f}%  maxDD {mB:.1f}%")
    print(f"  → тренд режет просадку на {abs(mB)-abs(mT):.0f} п.п. ценой части доходности.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Трендовая спот-система (лонг/кэш).")
    ap.add_argument("--sma", type=int, default=50)
    ap.add_argument("--no-backtest", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.isdir(DDIR):
        print("Нет data/daily. Сначала: py scripts/fetch_daily_klines.py"); return 2
    uni = [x for x in UNIVERSE if load(x)]
    signal_now(uni, a.sma)
    if not a.no_backtest:
        backtest(uni, a.sma)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
