#!/usr/bin/env python3
"""Диагностический логгер сырого Yahoo chart-response по вотчлисту.

Цель: зафиксировать, ЧТО именно возвращает API в выходные/праздники:
тот же официальный close четверга или формирующийся live-бар.
Логирует по каждому тикеру: дату последнего бара, его close, meta.regularMarketPrice,
кол-во None, gmtoffset, и хвост timestamp/close.

ЗАПУСК (в боевой среде бота, С СЕТЬЮ):
    python3 tools/log_yahoo_raw.py            # один снимок сейчас
    python3 tools/log_yahoo_raw.py --watch 1800   # каждые 30 мин (чтобы снять 18/19/20/21)

ВАЖНО: Yahoo chart НЕ хранит историю «живых» regularMarketPrice за прошлые
выходные — снимок нужно делать ИМЕННО в те даты. Поэтому в проде стоит
оставить этот логгер по крону рядом с постингом карточки.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    from datetime import timedelta
    ET = timezone(timedelta(hours=-5))

try:
    from stock_screener import WATCHLIST
    TICKERS = list(WATCHLIST.keys())
except Exception:
    TICKERS = ["AMD", "AMAT", "NVDA", "TXN", "GOOGL", "AAPL", "MSFT", "JNJ"]

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
UA = {"User-Agent": "Mozilla/5.0"}


def et_date(epoch):
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(ET).date().isoformat()


def snapshot():
    now = datetime.now(ET).isoformat(timespec="seconds")
    print(f"\n##### SNAPSHOT @ {now} (ET) #####")
    for sym in TICKERS:
        try:
            req = urllib.request.Request(URL.format(sym=sym), headers=UA)
            raw = urllib.request.urlopen(req, timeout=15).read()
            d = json.loads(raw)
            res = d["chart"]["result"][0]
            meta = res.get("meta", {})
            ts = res.get("timestamp") or []
            closes = res["indicators"]["quote"][0]["close"]
            n_none = sum(1 for c in closes if c is None)
            last_dt = et_date(ts[-1]) if ts else "?"
            rec = {
                "symbol": sym,
                "bars": len(closes),
                "none_closes": n_none,
                "last_bar_date_ET": last_dt,
                "last_close": closes[-1],
                "regularMarketPrice": meta.get("regularMarketPrice"),
                "regularMarketTime_ET": (et_date(meta["regularMarketTime"])
                                          if meta.get("regularMarketTime") else None),
                "gmtoffset": meta.get("gmtoffset"),
                "exchangeTimezoneName": meta.get("exchangeTimezoneName"),
                "tail_dates": [et_date(t) for t in ts[-4:]],
                "tail_closes": closes[-4:],
            }
            # КЛЮЧЕВОЙ ИНДИКАТОР: last_close != regularMarketPrice или last_bar — не торговый день
            print(json.dumps(rec, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"symbol": sym, "error": repr(e)}, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--watch":
        period = int(sys.argv[2])
        while True:
            snapshot()
            time.sleep(period)
    else:
        snapshot()
