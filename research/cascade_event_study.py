"""
Backtest of DIALECTIC_EDGE's BTC ETF-outflow / cascade predict.

Replicates the LIVE rule (market_indicators/btc_etf_flows.detect_outflow_signal)
on the real ETF basket proxy + BTC price, runs an event study of forward BTC
returns, and overlays CFTC COT (leveraged-money) positioning.

Data (all free/public):
  - BTC daily close: Yahoo BTC-USD (long history) + OKX BTC-USDT (exchange x-check)
  - ETF basket: Yahoo IBIT/FBTC/BITB/ARKB/BTCO (since Jan-2024)
  - COT: CFTC annual fin zips, Bitcoin CME code 133741 (weekly)
"""
from __future__ import annotations
import urllib.request, json, ssl, io, zipfile, csv, time, statistics as st
from datetime import datetime, timezone, date

CTX = ssl.create_default_context()
UA = {"User-Agent": "Mozilla/5.0 (DialecticEdge-Bot/1.0)"}

def _get(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=CTX)

# ───────────────────────── data fetch ─────────────────────────
def fetch_yahoo_daily(ticker: str, rng="11y"):
    """Return list of (date, close) sorted asc."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={rng}&interval=1d"
    d = json.load(_get(url))
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(t, tz=timezone.utc).date(), float(c)))
    out.sort()
    return out

def fetch_okx_daily(inst="BTC-USDT", max_pages=20):
    """OKX history-candles paginated back. Return list (date, close) asc."""
    out = {}
    after = ""
    for _ in range(max_pages):
        url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1Dutc&limit=100"
        if after:
            url += f"&after={after}"
        try:
            d = json.load(_get(url))
        except Exception:
            break
        rows = d.get("data", [])
        if not rows:
            break
        for r in rows:
            ms = int(r[0]); c = float(r[4])
            dt = datetime.fromtimestamp(ms/1000, tz=timezone.utc).date()
            out[dt] = c
        after = rows[-1][0]
        time.sleep(0.15)
    return sorted(out.items())

def fetch_cot_btc(years):
    """CFTC Bitcoin (CME) weekly COT. Return list of dicts asc by date."""
    recs = []
    for y in years:
        url = f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{y}.zip"
        try:
            raw = _get(url, timeout=120).read()
            z = zipfile.ZipFile(io.BytesIO(raw))
            name = [n for n in z.namelist() if n.lower().endswith(".txt")][0]
            rows = list(csv.DictReader(z.read(name).decode("utf-8", "replace").splitlines()))
        except Exception as e:
            print("  COT", y, "fail", repr(e)[:80]); continue
        for r in rows:
            code = str(r.get("CFTC_Contract_Market_Code", "")).strip().upper()
            if code != "133741":
                continue
            def i(k):
                try: return int(float(str(r.get(k, "0")).replace(",", "")))
                except: return 0
            ll = i("Lev_Money_Positions_Long_All"); lsh = i("Lev_Money_Positions_Short_All")
            al = i("Asset_Mgr_Positions_Long_All"); ash = i("Asset_Mgr_Positions_Short_All")
            recs.append({
                "date": str(r.get("Report_Date_as_YYYY-MM-DD", "")).strip(),
                "oi": i("Open_Interest_All"),
                "lev_net": ll - lsh, "lev_long": ll, "lev_short": lsh,
                "am_net": al - ash,
            })
    recs = [r for r in recs if r["date"]]
    recs.sort(key=lambda r: r["date"])
    return recs

# ───────────────────────── live-rule replica ─────────────────────────
BASKET = ["IBIT", "FBTC", "BITB", "ARKB", "BTCO"]
CRIT_DAY = 4.0     # single-session basket drop %
WARN_DAY = 1.5     # outflow-day threshold
WARN_STREAK = 3    # consecutive outflow days

def basket_daily_change(etf_series: dict):
    """Align ETF closes by DATE, compute basket-avg close-to-close % per day."""
    # union of dates where >=3 tickers have a value
    by_date = {}
    for tk, series in etf_series.items():
        prev = None
        for dt, c in series:
            if prev is not None and prev > 0:
                by_date.setdefault(dt, []).append((c - prev) / prev * 100.0)
            prev = c
    out = []
    for dt in sorted(by_date):
        ch = by_date[dt]
        if len(ch) >= 3:
            out.append((dt, sum(ch) / len(ch)))
    return out

def triggers_from_basket(basket_changes):
    """Return dict date->severity replicating detect_outflow_signal (per-day eval)."""
    crit, warn = set(), set()
    streak = 0
    for dt, ch in basket_changes:
        # CRIT: single big down session
        if ch <= -CRIT_DAY:
            crit.add(dt)
        # WARN: rolling streak of outflow days
        if ch <= -WARN_DAY:
            streak += 1
        else:
            streak = 0
        if streak >= WARN_STREAK:
            warn.add(dt)
    return crit, warn

# ───────────────────────── event study ─────────────────────────
def fwd_returns(btc: list, idx_by_date: dict, dates, horizons=(1, 3, 7, 14)):
    closes = [c for _, c in btc]
    res = {h: [] for h in horizons}
    mfe, mae = [], []
    for d in dates:
        i = idx_by_date.get(d)
        if i is None or i + max(horizons) >= len(closes):
            continue
        base = closes[i]
        for h in horizons:
            res[h].append((closes[i + h] - base) / base * 100.0)
        path = closes[i + 1:i + 1 + max(horizons)]
        if path:
            mfe.append((max(path) - base) / base * 100.0)
            mae.append((min(path) - base) / base * 100.0)
    return res, mfe, mae

def summ(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return {
        "n": len(xs), "mean": round(st.mean(xs), 2),
        "median": round(st.median(xs), 2),
        "p_neg": round(sum(1 for x in xs if x < 0) / len(xs) * 100, 1),
        "min": round(xs[0], 2), "max": round(xs[-1], 2),
    }

def baseline(btc, horizons=(1, 3, 7, 14)):
    closes = [c for _, c in btc]
    res = {h: [] for h in horizons}
    for i in range(len(closes) - max(horizons)):
        base = closes[i]
        for h in horizons:
            res[h].append((closes[i + h] - base) / base * 100.0)
    return res

def big_down_days(btc, thr=4.0):
    """Long-history proxy: BTC single-day close-to-close <= -thr%."""
    dates = []
    for i in range(1, len(btc)):
        prev = btc[i-1][1]; cur = btc[i][1]
        if prev > 0 and (cur - prev) / prev * 100.0 <= -thr:
            dates.append(btc[i][0])
    return dates

def main():
    print("Fetching BTC (Yahoo)..."); btc = fetch_yahoo_daily("BTC-USD", "11y")
    print("  BTC days:", len(btc), btc[0][0], "->", btc[-1][0])
    print("Fetching BTC (OKX exchange)..."); okx = fetch_okx_daily()
    print("  OKX days:", len(okx), okx[0][0] if okx else "-", "->", okx[-1][0] if okx else "-")
    print("Fetching ETF basket (Yahoo)...")
    etf = {}
    for tk in BASKET:
        try:
            etf[tk] = fetch_yahoo_daily(tk, "3y")
            print("  ", tk, len(etf[tk]), "days")
        except Exception as e:
            print("  ", tk, "fail", repr(e)[:60])
    print("Fetching COT (CFTC)..."); cot = fetch_cot_btc(range(2020, 2027))
    print("  COT weekly BTC rows:", len(cot), cot[0]["date"] if cot else "-", "->", cot[-1]["date"] if cot else "-")

    idx_by_date = {d: i for i, (d, _) in enumerate(btc)}
    horizons = (1, 3, 7, 14)

    # ── basket proxy rule (live replica), ETF era ──
    bc = basket_daily_change(etf)
    crit, warn = triggers_from_basket(bc)
    print("\nETF era:", bc[0][0], "->", bc[-1][0], "| trigger days CRIT", len(crit), "WARN", len(warn))

    out = {"meta": {"btc_days": len(btc), "btc_range": [str(btc[0][0]), str(btc[-1][0])],
                    "etf_era": [str(bc[0][0]), str(bc[-1][0])],
                    "crit_days": len(crit), "warn_days": len(warn),
                    "okx_days": len(okx), "cot_rows": len(cot)}}

    base = baseline(btc, horizons)
    out["baseline"] = {f"+{h}d": summ(base[h]) for h in horizons}

    for label, dates in [("CRIT_etf_basket", sorted(crit)), ("WARN_etf_streak", sorted(warn))]:
        r, mfe, mae = fwd_returns(btc, idx_by_date, dates, horizons)
        out[label] = {f"+{h}d": summ(r[h]) for h in horizons}
        out[label]["MFE_14d"] = summ(mfe); out[label]["MAE_14d"] = summ(mae)

    # ── long-history BTC big-down-day (proxy reduces to this) ──
    for thr in (4.0, 6.0, 8.0):
        dd = big_down_days(btc, thr)
        r, mfe, mae = fwd_returns(btc, idx_by_date, dd, horizons)
        out[f"BTC_downday_<=-{thr:.0f}pct"] = {"n_events": len(dd),
            **{f"+{h}d": summ(r[h]) for h in horizons},
            "MFE_14d": summ(mfe), "MAE_14d": summ(mae)}

    # ── COT overlay: bucket big-down-days by lev-money net positioning ──
    if cot:
        cot_dates = [datetime.strptime(c["date"], "%Y-%m-%d").date() for c in cot]
        lev = [c["lev_net"] for c in cot]
        def lev_at(d):
            # most recent COT report on/before d
            best = None
            for cd, lv in zip(cot_dates, lev):
                if cd <= d: best = lv
                else: break
            return best
        levs = [l for l in lev if l is not None]
        med = st.median(levs)
        dd = big_down_days(btc, 4.0)
        crowd_long, crowd_short = [], []
        for d in dd:
            lv = lev_at(d)
            if lv is None: continue
            (crowd_long if lv > med else crowd_short).append(d)
        for nm, ds in [("downday_levmoney_NETLONG", crowd_long), ("downday_levmoney_NETSHORT", crowd_short)]:
            r, mfe, mae = fwd_returns(btc, idx_by_date, ds, horizons)
            out[f"COT_{nm}"] = {"n_events": len(ds),
                **{f"+{h}d": summ(r[h]) for h in horizons}, "MAE_14d": summ(mae)}
        out["meta"]["lev_net_median"] = med
        out["meta"]["lev_net_latest"] = lev[-1]

    with open("backtest_result.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\n=== RESULT (written to backtest_result.json) ===")
    print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__":
    main()
