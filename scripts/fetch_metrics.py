"""scripts/fetch_metrics.py — снапшот OI + top-trader L/S из Binance Vision.

Live fapi заблокирован (451), но статические metrics-дампы (посуточно, с 2020-09)
доступны. Каждый дневной файл = 288 строк (5-мин). Даунсэмплим в 1 запись/день
(среднее OI и среднее top-trader L/S за день).

Колонки файла: create_time, symbol, sum_open_interest, sum_open_interest_value,
count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
count_long_short_ratio, sum_taker_long_short_vol_ratio.

Бот использует top-trader position ratio → берём sum_toptrader_long_short_ratio.

Запуск: python scripts/fetch_metrics.py [--assets BTC ETH ...] [--from 2024-01-01 --to 2026-04-30]
Пишет data/backtest_derivs/<ASSET>_metrics.json = {"YYYY-MM-DD": {"oi":x,"ls":y}, ...}
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
import zipfile
from datetime import date, timedelta

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

OUT_DIR = os.path.join(_REPO_ROOT, "data", "backtest_derivs")
BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
CORE = ["BTC", "ETH", "SOL", "BNB", "XRP"]


def _daterange(frm: str, to: str):
    y0, m0, d0 = map(int, frm.split("-"))
    y1, m1, d1 = map(int, to.split("-"))
    cur, end = date(y0, m0, d0), date(y1, m1, d1)
    while cur <= end:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def _fetch_day(asset: str, day: str) -> dict | None:
    """Возвращает {'oi':mean, 'ls':mean, 'taker':mean} за день или None.

    taker = sum_taker_long_short_vol_ratio (col 7) — агрессивный поток ордеров
    (buy taker vol / sell taker vol). Отличается от ls (позиционирование).
    """
    url = f"{BASE}/{asset}USDT/{asset}USDT-metrics-{day}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=25).read()
        z = zipfile.ZipFile(io.BytesIO(raw))
        lines = z.read(z.namelist()[0]).decode("utf-8", "ignore").splitlines()
        ois, lss, tks = [], [], []
        for ln in lines[1:]:
            p = ln.split(",")
            if len(p) < 6:
                continue
            try:
                ois.append(float(p[2]))          # sum_open_interest
                lss.append(float(p[5]))           # sum_toptrader_long_short_ratio
            except ValueError:
                continue
            if len(p) >= 8:
                try:
                    tks.append(float(p[7]))       # sum_taker_long_short_vol_ratio
                except ValueError:
                    pass
        if not ois:
            return None
        rec = {"oi": sum(ois) / len(ois), "ls": sum(lss) / len(lss)}
        if tks:
            rec["taker"] = sum(tks) / len(tks)
        return rec
    except Exception:  # noqa: BLE001 — 404 для будущих дат и т.п., молча
        return None


def fetch_asset(asset: str, frm: str, to: str) -> int:
    from concurrent.futures import ThreadPoolExecutor

    days = list(_daterange(frm, to))
    out = {}
    miss = 0
    # Параллельная загрузка — ~4250 файлов последовательно = час, в 24 потока = мин.
    with ThreadPoolExecutor(max_workers=24) as ex:
        for day, rec in zip(days, ex.map(lambda d: _fetch_day(asset, d), days)):
            if rec:
                out[day] = rec
            else:
                miss += 1
    if not out:
        print(f"  [skip] {asset}: ноль дней (miss={miss})")
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{asset}_metrics.json")
    # MERGE — не теряем дни вне текущего диапазона.
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:  # noqa: BLE001
            existing = {}
    existing.update(out)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f)
    print(f"  [ok] {asset}: +{len(out)} (miss={miss}), {len(existing)} total -> {path}")
    return len(existing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", nargs="*", default=CORE)
    ap.add_argument("--from", dest="frm", default="2024-01-01")
    ap.add_argument("--to", dest="to", default="2026-04-30")
    args = ap.parse_args()
    print(f"Metrics: {len(args.assets)} активов, {args.frm}..{args.to}")
    for a in args.assets:
        fetch_asset(a, args.frm, args.to)
    print("Готово.")


if __name__ == "__main__":
    main()
