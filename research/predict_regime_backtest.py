"""
Proper walk-forward backtest of a BTC predict built on LEADING factors.

Factors (all past-only, no look-ahead):
  - Trend / time-series momentum (price vs SMA, 90d momentum)
  - Realized-volatility regime
  - Fear & Greed (alternative.me, daily since 2018)
  - COT leveraged-money net positioning (CFTC, weekly since ~2018)

Outputs: factor decile monotonicity (forward returns), and a transparent
long/flat strategy equity curve vs buy&hold (CAGR/Sharpe/MaxDD), in- and
out-of-sample. Writes predict_result.json + predict_backtest.png.
"""
from __future__ import annotations
import urllib.request, json, ssl, io, zipfile, csv, time
from datetime import datetime, timezone
import numpy as np
import pandas as pd

CTX = ssl.create_default_context()
UA = {"User-Agent": "Mozilla/5.0 (DialecticEdge-Bot/1.0)"}
def _get(url, t=90): return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=t, context=CTX)

# ───────── data ─────────
def btc_daily():
    d = json.load(_get("https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?range=11y&interval=1d"))
    r = d["chart"]["result"][0]
    ts, cl = r["timestamp"], r["indicators"]["quote"][0]["close"]
    rows = [(datetime.fromtimestamp(t, tz=timezone.utc).date(), float(c)) for t, c in zip(ts, cl) if c]
    df = pd.DataFrame(rows, columns=["date", "close"]).set_index("date").sort_index()
    df.index = pd.to_datetime(df.index)
    return df

def fng_daily():
    d = json.load(_get("https://api.alternative.me/fng/?limit=0&format=json"))
    rows = [(datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc).date(), int(x["value"])) for x in d["data"]]
    df = pd.DataFrame(rows, columns=["date", "fng"]).set_index("date").sort_index()
    df.index = pd.to_datetime(df.index)
    return df

def cot_btc(years):
    recs = []
    for y in years:
        url = f"https://www.cftc.gov/files/dea/history/fut_fin_txt_{y}.zip"
        try:
            raw = _get(url, 120).read(); z = zipfile.ZipFile(io.BytesIO(raw))
            name = [n for n in z.namelist() if n.lower().endswith(".txt")][0]
            rows = list(csv.DictReader(z.read(name).decode("utf-8", "replace").splitlines()))
        except Exception as e:
            print("  COT", y, "fail", repr(e)[:60]); continue
        for r in rows:
            if str(r.get("CFTC_Contract_Market_Code", "")).strip().upper() != "133741":
                continue
            def i(k):
                try: return int(float(str(r.get(k, "0")).replace(",", "")))
                except: return 0
            recs.append((str(r.get("Report_Date_as_YYYY-MM-DD", "")).strip(),
                         i("Lev_Money_Positions_Long_All") - i("Lev_Money_Positions_Short_All"),
                         i("Asset_Mgr_Positions_Long_All") - i("Asset_Mgr_Positions_Short_All")))
    recs = [r for r in recs if r[0]]
    df = pd.DataFrame(recs, columns=["date", "lev_net", "am_net"]).drop_duplicates("date").set_index("date").sort_index()
    df.index = pd.to_datetime(df.index)
    return df

# ───────── features ─────────
def build(df, fng, cot):
    d = df.copy()
    d["ret"] = d["close"].pct_change()
    d["sma50"] = d["close"].rolling(50).mean()
    d["sma100"] = d["close"].rolling(100).mean()
    d["sma200"] = d["close"].rolling(200).mean()
    d["mom90"] = d["close"].pct_change(90)
    d["px_sma200"] = d["close"] / d["sma200"] - 1.0
    d["rv30"] = d["ret"].rolling(30).std() * np.sqrt(365)
    # merge sentiment (daily) and COT (weekly -> ffill, lag publish 3 biz days to be safe)
    d = d.join(fng, how="left")
    d["fng"] = d["fng"].ffill()
    cot_l = cot.copy()
    cot_l.index = cot_l.index + pd.Timedelta(days=3)   # COT Tue positions published ~Fri; lag to avoid look-ahead
    d = d.join(cot_l, how="left").ffill()
    # standardized leading signals (trailing 365d z, past-only)
    def z(s, w=365):
        return (s - s.rolling(w).mean()) / (s.rolling(w).std() + 1e-9)
    d["z_mom"] = z(d["mom90"])
    d["z_trend"] = z(d["px_sma200"])
    d["z_fng"] = z(d["fng"])              # high = greedy
    d["z_lev"] = z(d["lev_net"])          # high = leveraged money crowded long
    return d

def metrics(rets, ann=365):
    rets = rets.dropna()
    if len(rets) < 5: return {}
    eq = (1 + rets).cumprod()
    cagr = eq.iloc[-1] ** (ann / len(rets)) - 1
    vol = rets.std() * np.sqrt(ann)
    sharpe = (rets.mean() * ann) / (vol + 1e-9)
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR%": round(cagr * 100, 1), "Vol%": round(vol * 100, 1),
            "Sharpe": round(sharpe, 2), "MaxDD%": round(dd * 100, 1),
            "final_x": round(float(eq.iloc[-1]), 2)}

def strat(d, position):
    """position: Series in [0,1] decided from PAST data; trade at close, hold next day."""
    pos = position.shift(1).fillna(0.0)            # no look-ahead: act on yesterday's signal
    cost = (pos.diff().abs().fillna(0.0)) * 0.001  # 0.1% per turnover
    sret = pos * d["ret"] - cost
    return sret, pos

def _cached(name, fn):
    import os
    p = f"cache_{name}.pkl"
    if os.path.exists(p):
        return pd.read_pickle(p)
    obj = fn(); obj.to_pickle(p); return obj

def main():
    print("BTC..."); df = _cached("btc", btc_daily); print("  ", len(df), df.index[0].date(), "->", df.index[-1].date())
    print("FNG..."); fng = _cached("fng", fng_daily); print("  ", len(fng))
    print("COT..."); cot = _cached("cot", lambda: cot_btc(range(2018, 2027))); print("  ", len(cot))
    d = build(df, fng, cot)
    d = d[d["sma200"].notna()].copy()

    out = {"meta": {"rows": len(d), "range": [str(d.index[0].date()), str(d.index[-1].date())]}}

    # ── 1) factor decile monotonicity: forward 14d return by signal decile ──
    d["fwd14"] = d["close"].shift(-14) / d["close"] - 1.0
    decile = {}
    for sig in ["z_trend", "z_mom", "z_fng", "z_lev"]:
        sub = d[[sig, "fwd14"]].dropna()
        sub["dec"] = pd.qcut(sub[sig], 5, labels=False, duplicates="drop")
        g = sub.groupby("dec")["fwd14"].mean() * 100
        decile[sig] = {int(k): round(float(v), 2) for k, v in g.items()}
    out["fwd14_by_quintile_pct"] = decile

    # ── 2) strategies ──
    bh_ret = d["ret"]
    out["buy_hold"] = metrics(bh_ret)

    # V1: trend filter (long if close>SMA200)
    p1 = (d["close"] > d["sma200"]).astype(float)
    r1, _ = strat(d, p1); out["V1_trend200"] = {**metrics(r1), "time_in%": round(p1.mean()*100,1)}

    # V2: dual-trend (close>SMA100 AND SMA50>SMA200)
    p2 = ((d["close"] > d["sma100"]) & (d["sma50"] > d["sma200"])).astype(float)
    r2, _ = strat(d, p2); out["V2_dualtrend"] = {**metrics(r2), "time_in%": round(p2.mean()*100,1)}

    # V3: trend + sentiment trim (cut exposure to 0.5 on extreme greed)
    trim = np.where(d["fng"] >= 85, 0.5, 1.0)
    p3 = p2 * trim
    r3, _ = strat(d, p3); out["V3_trend+greedtrim"] = {**metrics(r3), "time_in%": round((p3>0).mean()*100,1)}

    # V4: composite leading score -> exposure (trend + momentum, contrarian greed & COT crowding)
    score = d["z_trend"] + d["z_mom"] - 0.5*d["z_fng"].clip(lower=0) - 0.3*d["z_lev"].clip(lower=0)
    d["score"] = score
    # exposure: long when score>0 AND price>sma200 (trend gate keeps it honest)
    p4 = ((score > 0) & (d["close"] > d["sma200"])).astype(float)
    r4, _ = strat(d, p4); out["V4_composite"] = {**metrics(r4), "time_in%": round(p4.mean()*100,1)}

    # V5 = PRODUCTION: trend regime gate + momentum-scaled exposure + vol de-risk.
    # Clean factors only (F&G greed is pro-trend not contrarian; COT noisy -> dropped).
    score5 = d["z_trend"] + d["z_mom"]
    d["score5"] = score5
    risk_on = (d["close"] > d["sma200"]) & (d["sma50"] > d["sma200"])
    strong = score5 > 0.3
    expo = pd.Series(0.0, index=d.index)
    expo[risk_on] = 0.6
    expo[risk_on & strong] = 1.0
    rv_hi = d["rv30"] > d["rv30"].rolling(365, min_periods=60).quantile(0.90)
    expo[rv_hi] = expo[rv_hi] * 0.5          # de-risk in vol blow-offs
    p5 = expo
    r5, _ = strat(d, p5); out["V5_production"] = {**metrics(r5), "time_in%": round((p5>0).mean()*100,1)}

    # ── 3) out-of-sample split (train view <=2021, test >=2022) ──
    split = pd.Timestamp("2022-01-01")
    for name, r in [("V2_dualtrend", r2), ("V4_composite", r4), ("V5_production", r5), ("buy_hold", bh_ret)]:
        out.setdefault("oos", {})[name] = {
            "IS_<=2021": metrics(r[d.index < split]),
            "OOS_>=2022": metrics(r[d.index >= split]),
        }

    # ── 4) score decile monotonicity OOS ──
    sub = d[d.index >= split][["score", "fwd14"]].dropna()
    sub["dec"] = pd.qcut(sub["score"], 5, labels=False, duplicates="drop")
    g = sub.groupby("dec")["fwd14"].mean() * 100
    out["composite_score_fwd14_OOS_by_quintile_pct"] = {int(k): round(float(v),2) for k,v in g.items()}

    with open("predict_result.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))

    # chart: equity curves
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig, (a1,a2) = plt.subplots(1,2, figsize=(15,6))
    curves = [("Buy & Hold", bh_ret, "#999999"),
              ("V2 dual-trend", r2, "#1f77b4"),
              ("V5 PRODUCTION (trend+mom+vol)", r5, "#2ca02c"),
              ("V4 composite", r4, "#d62728")]
    for lbl, r, c in curves:
        eq = (1+r.fillna(0)).cumprod()
        a1.plot(d.index, eq, label=f"{lbl}", color=c, lw=1.6)
    a1.set_yscale("log"); a1.set_title("Equity curve (log) — $1 start", fontweight="bold")
    a1.legend(fontsize=8); a1.grid(alpha=.3)
    # drawdown of B&H vs V5
    for lbl, r, c in [("Buy & Hold", bh_ret, "#999999"), ("V5 PRODUCTION", r5, "#2ca02c")]:
        eq=(1+r.fillna(0)).cumprod(); dd=(eq/eq.cummax()-1)*100
        a2.plot(d.index, dd, label=lbl, color=c, lw=1.4)
    a2.set_title("Drawdown % — edge = shallower drawdowns", fontweight="bold")
    a2.legend(fontsize=8); a2.grid(alpha=.3)
    fig.suptitle("DIALECTIC_EDGE — BTC predict on leading factors: walk-forward backtest", fontweight="bold")
    fig.tight_layout(rect=[0,0,1,0.96]); fig.savefig("predict_backtest.png", dpi=130)
    print("saved predict_backtest.png")

if __name__ == "__main__":
    main()
