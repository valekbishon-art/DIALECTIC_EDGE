"""scripts/fetch_daily_klines.py — многолетние ДНЕВНЫЕ цены с Binance Vision (S3-дампы
доступны; live api геоблокнут). Под стат-арб коинтеграцию нужна длинная история (месяцы-годы),
а klines_1m у нас только 45д. Пишет data/daily/<ASSET>.json = {ts_ms: close, ...}.
"""
import io, json, os, urllib.error, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor

OUT = "data/daily"
BASE = "https://data.binance.vision/data/spot/monthly/klines"
# широкий коррелированный юниверс под парный стат-арб: мейджоры + L1/L2 + сектора
ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "LINK", "DOT", "TRX",
          "TON", "LTC", "NEAR", "SUI", "DOGE", "ATOM", "APT", "ARB", "OP", "INJ",
          "TIA", "SEI", "FET", "RENDER", "AAVE", "UNI", "MKR", "LDO", "FIL", "ETC",
          "BCH", "ICP", "HBAR", "ALGO", "VET"]
FROM = "2021-01"
TO = "2026-06"


def months(frm, to):
    y0, m0 = map(int, frm.split("-")); y1, m1 = map(int, to.split("-"))
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1; y += 1


def fetch_month(asset, ym):
    url = f"{BASE}/{asset}USDT/1d/{asset}USDT-1d-{ym}.zip"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=30).read()
            z = zipfile.ZipFile(io.BytesIO(raw))
            out = {}
            for ln in z.read(z.namelist()[0]).decode("utf-8", "ignore").splitlines():
                p = ln.split(",")
                if len(p) < 5 or not p[0].lstrip("-").isdigit():
                    continue
                try:
                    out[int(p[0])] = float(p[4])     # open_time_ms -> close
                except ValueError:
                    pass
            return out
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
        except Exception:
            import time; time.sleep(0.5 * (attempt + 1))
    return {}


def fetch_asset(asset):
    series = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for md in ex.map(lambda ym: fetch_month(asset, ym), list(months(FROM, TO))):
            series.update(md)
    if not series:
        return asset, 0
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{asset}.json"), "w") as f:
        json.dump(series, f)
    return asset, len(series)


def main():
    print(f"Daily closes {FROM}..{TO}, {len(ASSETS)} assets -> {OUT}/", flush=True)
    ok = 0
    for a in ASSETS:
        asset, n = fetch_asset(a)
        print(f"  {asset:8s} {n:5d} days", flush=True)
        if n:
            ok += 1
    print(f"done: {ok}/{len(ASSETS)}", flush=True)


if __name__ == "__main__":
    main()
