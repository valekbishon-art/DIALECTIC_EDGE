"""scripts/fetch_funding.py — снапшот исторического funding rate.

Live-API деривативов (fapi.binance.com, Bybit) геоблокированы (451/403), но
СТАТИЧЕСКИЕ дампы Binance Vision (data.binance.vision, S3) доступны. Тянем
помесячные funding-файлы и агрегируем в дневной ряд {date: funding_mean}.

Funding в файле: calc_time(ms), funding_interval_hours, last_funding_rate.
Каждые 8ч → 3 значения/день. Берём среднее за день.

Запуск: python scripts/fetch_funding.py [--assets BTC ETH ...] [--from 2023-09 --to 2026-05]
Пишет data/backtest_derivs/<ASSET>_funding.json = {"YYYY-MM-DD": funding_mean, ...}
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.signal_scorer import TRADABLE_ASSETS  # noqa: E402

OUT_DIR = os.path.join(_REPO_ROOT, "data", "backtest_derivs")
BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
CORE = ["BTC", "ETH", "SOL", "BNB", "XRP"]


def _months(frm: str, to: str):
    y0, m0 = map(int, frm.split("-"))
    y1, m1 = map(int, to.split("-"))
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1
            y += 1


def _fetch_month(asset: str, ym: str) -> dict:
    """Возвращает {date: [rates]} для одного месяца, или {} при ошибке."""
    url = f"{BASE}/{asset}USDT/{asset}USDT-fundingRate-{ym}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=25).read()
        z = zipfile.ZipFile(io.BytesIO(raw))
        lines = z.read(z.namelist()[0]).decode("utf-8", "ignore").splitlines()
        out = defaultdict(list)
        for ln in lines[1:]:  # skip header
            parts = ln.split(",")
            if len(parts) < 3:
                continue
            try:
                ts_ms = int(parts[0])
                rate = float(parts[2])
            except ValueError:
                continue
            day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            out[day].append(rate)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"    [skip] {asset} {ym}: {e}")
        return {}


def fetch_asset(asset: str, frm: str, to: str) -> int:
    from concurrent.futures import ThreadPoolExecutor

    months = list(_months(frm, to))
    daily = {}
    # Параллельная загрузка помесячно — 24 потока вместо последовательного часа.
    with ThreadPoolExecutor(max_workers=24) as ex:
        for month in ex.map(lambda ym: _fetch_month(asset, ym), months):
            for day, rates in month.items():
                daily[day] = sum(rates) / len(rates)
    if not daily:
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{asset}_funding.json")
    # MERGE, не перезапись: подгружаем существующий файл, обновляем новыми днями.
    # Иначе докачка 2020-2023 затрёт уже скачанные 2023-09+.
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:  # noqa: BLE001
            existing = {}
    existing.update(daily)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f)
    print(f"  [ok] {asset}: +{len(daily)} new, {len(existing)} total -> {path}")
    return len(existing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", nargs="*", default=CORE)
    ap.add_argument("--from", dest="frm", default="2023-09")
    ap.add_argument("--to", dest="to", default="2026-05")
    args = ap.parse_args()
    assets = [a for a in args.assets if a in TRADABLE_ASSETS]
    print(f"Funding: {len(assets)} активов, {args.frm}..{args.to}")
    total = 0
    for a in assets:
        total += 1 if fetch_asset(a, args.frm, args.to) else 0
    print(f"Готово: {total}/{len(assets)} активов")


if __name__ == "__main__":
    main()
