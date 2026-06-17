"""
fetch_klines.py — тянет РЕАЛЬНЫЕ 1m OHLCV-свечи с бирж для бэктеста.

Зачем: без живых данных любой бэктест памп-детектора — это самообман
(в pump_backtest.demo() только синтетика). Этот скрипт качает настоящую
1m-историю и кладёт в CSV, который ест pump_ignition_backtest.py.

Источники (без авторизации, публичные):
  - Binance  (primary):  api.binance.com/api/v3/klines
  - MEXC     (fallback): api.mexc.com/api/v3/klines
Оба отдают одинаковый формат массива [openTime, open, high, low, close, vol, ...],
индексы 0..5 совпадают — парсер один.

Формат выходного CSV (с заголовком):
    timestamp,open,high,low,close,volume
timestamp — миллисекунды UTC (openTime свечи).

Использование:
    python scripts/fetch_klines.py --days 30
    python scripts/fetch_klines.py --days 45 --symbols BTC,ETH,SOL,PEPE,WIF
    python scripts/fetch_klines.py --out data/klines_1m --days 30

Ограничения честно: глубокую 1m-историю биржи лимитируют окном (Binance отдаёт
~до нескольких лет назад, но молодые монеты — только с листинга). Survivorship
bias неизбежен (берём то, что торгуется сейчас). Это первый честный срез, не
истина в последней инстанции.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

MINUTE_MS = 60_000
MINUTES_PER_DAY = 1440
MAX_PER_REQ = 1000  # лимит обеих бирж на 1m-свечи за один запрос

# Корзина по умолчанию: BTC (для режима) + майоры + классические «пампилки»/мемы.
# Тикеры без USDT — суффикс добавляется на лету. Несуществующие на бирже —
# скипаются (best-effort).
DEFAULT_SYMBOLS = [
    "BTC",   # нужен ОБЯЗАТЕЛЬНО — по нему считается режим рынка
    "ETH", "SOL", "BNB", "XRP", "DOGE",
    "PEPE", "WIF", "FLOKI", "BONK", "SHIB",
    "ORDI", "FET", "INJ", "SEI", "TIA",
    "JUP", "PYTH", "JTO", "WLD",
]

BINANCE_URL = "https://api.binance.com/api/v3/klines"
MEXC_URL = "https://api.mexc.com/api/v3/klines"


def _http_get_json(url: str, params: dict, *, timeout: float = 15.0):
    """GET -> распарсенный JSON. None при любой сетевой/HTTP-ошибке."""
    qs = urllib.parse.urlencode(params)
    full = url + "?" + qs
    req = urllib.request.Request(full, headers={"User-Agent": "dialectic-edge-backtest/1.0"})
    # ConnectionResetError/socket-сбои — это OSError, НЕ URLError: ловим широко,
    # иначе плотный скрейп рвёт весь прогон на случайном reset'е от биржи.
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug("GET %s failed: %s", full[:80], e)
        return None


def _fetch_page(base_url: str, symbol_usdt: str, start_ms: int, end_ms: int) -> Optional[list]:
    """Одна страница до MAX_PER_REQ свечей начиная со start_ms (включ.)."""
    params = {
        "symbol": symbol_usdt,
        "interval": "1m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": MAX_PER_REQ,
    }
    return _http_get_json(base_url, params)


def fetch_symbol(base: str, *, days: int, base_url: str, now_ms: int,
                 pause: float = 0.20) -> list:
    """Тянет 1m OHLCV за последние `days` дней. Возвращает список строк
    [ts, open, high, low, close, volume] (float, кроме ts=int). Пусто при фейле."""
    symbol_usdt = base.upper() + "USDT"
    start_ms = now_ms - days * 24 * 60 * MINUTE_MS
    out: list = []
    cursor = start_ms
    empty_streak = 0
    while cursor < now_ms:
        # до 3 попыток с растущим бэкоффом — биржа периодически рвёт соединение
        page = None
        for attempt in range(3):
            page = _fetch_page(base_url, symbol_usdt, cursor, now_ms)
            if page is not None:
                break
            time.sleep(0.5 * (attempt + 1))
        if not page:  # None (3 фейла) или [] (нет свечей в окне)
            empty_streak += 1
            if empty_streak >= 2:
                break
            cursor += MAX_PER_REQ * MINUTE_MS
            continue
        empty_streak = 0
        for k in page:
            try:
                ts = int(k[0])
                o, h, l, c, v = (float(k[1]), float(k[2]), float(k[3]),
                                 float(k[4]), float(k[5]))
                # индекс 9 = taker buy base volume (агрессивные покупки те��керов).
                # delta = 2*taker_buy - v; продажи = v - taker_buy. Order-flow бесплатно.
                taker_buy = float(k[9]) if len(k) > 9 else 0.0
            except (ValueError, IndexError, TypeError):
                continue
            out.append([ts, o, h, l, c, v, taker_buy])
        last_ts = int(page[-1][0])
        nxt = last_ts + MINUTE_MS
        if nxt <= cursor:  # нет прогресса — выходим, чтобы не зациклиться
            break
        cursor = nxt
        time.sleep(pause)
    # дедуп по ts (на стыках страниц) + сортировка
    seen = set()
    dedup = []
    for row in sorted(out, key=lambda r: r[0]):
        if row[0] in seen:
            continue
        seen.add(row[0])
        dedup.append(row)
    return dedup


def fetch_with_fallback(base: str, *, days: int, now_ms: int) -> tuple:
    """Пробует Binance, затем MEXC. Возвращает (rows, venue)."""
    rows = fetch_symbol(base, days=days, base_url=BINANCE_URL, now_ms=now_ms)
    if len(rows) >= 100:
        return rows, "binance"
    rows_mexc = fetch_symbol(base, days=days, base_url=MEXC_URL, now_ms=now_ms)
    if len(rows_mexc) >= 100:
        return rows_mexc, "mexc"
    # вернём что длиннее (даже если коротко) — пусть caller решает
    return (rows if len(rows) >= len(rows_mexc) else rows_mexc), (
        "binance" if len(rows) >= len(rows_mexc) else "mexc")


def save_csv(path: str, rows: list) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume",
                    "taker_buy_base"])
        for r in rows:
            tb = r[6] if len(r) > 6 else 0.0
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], tb])


def _server_now_ms() -> int:
    """Время сервера Binance (мс). Фолбэк на последний свечной ts если недоступно.
    Date.now() в этом окружении недоступен, поэтому берём время с биржи."""
    j = _http_get_json("https://api.binance.com/api/v3/time", {})
    if isinstance(j, dict) and "serverTime" in j:
        try:
            return int(j["serverTime"])
        except (ValueError, TypeError):
            pass
    # запасной путь: последняя 1m-свеча BTC -> её closeTime
    page = _fetch_page(BINANCE_URL, "BTCUSDT", 0, 9_999_999_999_999)
    if page:
        return int(page[-1][0]) + MINUTE_MS
    raise RuntimeError("не удалось получить текущее время с биржи")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Качает реальные 1m-свечи для бэктеста")
    ap.add_argument("--symbols", help="список тикеров через запятую (без USDT). "
                    "По умолчанию — встроенная корзина.")
    ap.add_argument("--days", type=int, default=30, help="глубина истории в днях")
    ap.add_argument("--out", default="data/klines_1m", help="папка вывода")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else list(DEFAULT_SYMBOLS))
    if "BTC" not in symbols:
        symbols.insert(0, "BTC")  # BTC обязателен для режима

    os.makedirs(args.out, exist_ok=True)
    now_ms = _server_now_ms()
    logger.info("Тяну %d тикеров x %d дней 1m в %s", len(symbols), args.days, args.out)

    expected_min = int(args.days * MINUTES_PER_DAY * 0.6)  # ~60% покрытия = «готово»
    ok, fail = 0, []
    for base in symbols:
        path_existing = os.path.join(args.out, base + ".csv")
        if os.path.exists(path_existing):
            try:
                with open(path_existing) as fh:
                    have = sum(1 for _ in fh) - 1
            except OSError:
                have = 0
            if have >= expected_min:
                logger.info("  %-6s ↩ уже есть (%d свечей) — пропуск", base, have)
                ok += 1
                continue
        rows, venue = fetch_with_fallback(base, days=args.days, now_ms=now_ms)
        if len(rows) < 100:
            logger.info("  %-6s ✗ мало данных (%d свечей)", base, len(rows))
            fail.append(base)
            continue
        path = os.path.join(args.out, base + ".csv")
        save_csv(path, rows)
        span_h = (rows[-1][0] - rows[0][0]) / 3_600_000.0
        logger.info("  %-6s ✓ %6d свечей (%.0fч) via %s", base, len(rows), span_h, venue)
        ok += 1

    logger.info("Готово: %d/%d тикеров. Не удалось: %s",
                ok, len(symbols), ", ".join(fail) if fail else "—")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
