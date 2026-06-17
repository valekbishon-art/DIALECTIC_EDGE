"""stock_screener.py — скринер акций (секторный + балансовый фильтр) + трендовый сигнал.

Две ступени:
  СТУПЕНЬ 1 — скрин:
    (а) секторный: исключаем компании, чей основной доход в исключённых категориях —
        алкоголь, табак/вейпы, азартные игры/казино/беттинг, банки/страхование/потребкредит
        (процентное кредитование), оружие/оборона, взрослый контент, рекреационная марихуана;
    (б) балансовый (консервативные пороги): процентный долг/кап < 33%, (кэш+процентные
        бумаги)/кап < 33%, примесь исключённой выручки < 5%. Живых финотчётов нет (только
        stdlib), поэтому зашит КУРИРУЕМЫЙ вотчлист крупных компаний, обычно проходящих такой
        скрин; коэффициенты ПЕРЕПРОВЕРЯТЬ по свежему отчёту.
  СТУПЕНЬ 2 — тренд: дневные цены с Yahoo (без ключа), SMA(N), кто СЕЙЧАС в аптренде (price>SMA).
        Итог — вотчлист + текущие пики равным весом. Спот, только лонг, без плеча/шорта.

Не инвестиционный совет. Чистый stdlib. Launcher: py.
    py stock_screener.py            # скрин + сигнал, SMA50
    py stock_screener.py --sma 100
    py stock_screener.py --no-trend # только вотчлист, без сети
"""
from __future__ import annotations
import argparse
import json
import statistics
import time
import urllib.request
from datetime import datetime, timezone

# Исключённые сектора (секторный скрин) — основной доход в этих категориях не берём.
EXCLUDED_SECTORS = {
    "Алкоголь":                 "производство/продажа спиртного.",
    "Табак / вейпы":            "табачная продукция.",
    "Азартные игры / казино":   "казино, лотереи, беттинг, гэмблинг.",
    "Банки (процентные)":       "основной доход — процентное кредитование.",
    "Страхование (классич.)":   "процент + высокая неопределённость контракта.",
    "Потреб-кредит / BNPL":     "процентное кредитование.",
    "Оружие / оборона":         "военная продукция — исключаем из осторожности.",
    "Взрослый контент":         "порнография и сопутствующее.",
    "Рекреационная марихуана":  "рекреационные интоксиканты.",
}

# Курируемый вотчлист (~28): тикер -> (название, сектор, заметка/что сверить в отчёте).
# Намеренно ИСКЛЮЧЕНЫ: банки (JPM, BAC), страховка, алко (BUD), табак (MO/PM), казино, оборона.
WATCHLIST = {
    "AAPL": ("Apple",            "Технологии",      "железо/услуги; долг умеренный — сверить кэш/кап."),
    "MSFT": ("Microsoft",        "ПО / облако",     "софт/Azure; следить за долгом после крупных M&A."),
    "GOOGL":("Alphabet",         "Интернет/реклама","реклама-поиск; почти без долга, много кэша — сверить."),
    "NVDA": ("NVIDIA",           "Полупроводники",  "GPU/ИИ; низкий долг."),
    "AMD":  ("AMD",              "Полупроводники",  "CPU/GPU; долг низкий."),
    "AVGO": ("Broadcom",         "Полупроводники",  "чипы/ПО; ВЫСОКИЙ долг после M&A — обязательно сверить."),
    "QCOM": ("Qualcomm",         "Полупроводники",  "мобильные чипы/лицензии."),
    "TXN":  ("Texas Instruments","Полупроводники",  "аналоговые чипы; стабильный баланс."),
    "AMAT": ("Applied Materials","Оборуд. для п/п", "оборудование для фабрик чипов."),
    "ADBE": ("Adobe",            "ПО",              "Creative/Document Cloud; мало долга."),
    "CRM":  ("Salesforce",       "ПО / SaaS",       "CRM-облако; сверить долг после поглощений."),
    "ORCL": ("Oracle",           "ПО / БД",         "БД/облако; долг ВЫСОКИЙ — внимательно сверить долг/кап."),
    "CSCO": ("Cisco",            "Сетевое железо",  "сети; исторически много кэша — сверить кэш/кап."),
    "TSLA": ("Tesla",            "Автопром / EV",   "электромобили; долг низкий."),
    "NKE":  ("Nike",             "Одежда / обувь",  "спорттовары; сверить долг."),
    "PG":   ("Procter & Gamble", "Потреб. товары",  "товары для дома; стабильно, сверить долг/кап."),
    "PEP":  ("PepsiCo",          "Напитки/снеки",   "безалкогольные ок; долг повышен — сверить."),
    "COST": ("Costco",           "Ритейл",          "склад-ритейл; примесь исключённых категорий обычно <5% — проверить долю."),
    "HD":   ("Home Depot",       "Ритейл DIY",      "товары для дома; долг повышен — сверить долг/кап."),
    "UNH":  ("UnitedHealth",     "Здравоохранение", "ВНИМАНИЕ: страховой компонент спорен — некоторые скрины исключают; валидировать."),
    "JNJ":  ("Johnson&Johnson",  "Фарма / здоровье","лекарства/товары; сверить долг."),
    "PFE":  ("Pfizer",           "Фарма",           "лекарства; долг повышен после M&A — сверить."),
    "MRK":  ("Merck",            "Фарма",           "лекарства; сверить долг/кап."),
    "ABBV": ("AbbVie",           "Фарма",           "лекарства; долг ВЫСОКИЙ — обязательно сверить."),
    "XOM":  ("ExxonMobil",       "Энергетика",      "нефть/газ; долг низкий — сверить."),
    "CVX":  ("Chevron",          "Энергетика",      "нефть/газ; баланс крепкий — сверить."),
    "CAT":  ("Caterpillar",      "Машиностроение",  "ВНИМАНИЕ: финплечо CAT Financial — сверить долг/кап."),
    "UNP":  ("Union Pacific",    "Ж/д транспорт",   "грузовые ж/д; долг повышен — сверить долг/кап."),
}

THRESH_DEBT = 0.33
THRESH_CASH = 0.33
THRESH_IMPURE = 0.05

YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={rng}"


def fetch_closes(symbol, rng="1y", retries=3, pause=1.0):
    url = YF_URL.format(sym=symbol, rng=rng)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            res = data["chart"]["result"][0]
            ts = res["timestamp"]
            raw = res["indicators"]["quote"][0]["close"]
            pairs = [(int(t), float(c)) for t, c in zip(ts, raw) if c is not None and c > 0]
            if len(pairs) < 5:
                last_err = "слишком мало точек"
                continue
            return [t for t, _ in pairs], [c for _, c in pairs]
        except Exception as e:
            last_err = e
            time.sleep(pause * (attempt + 1))
    print(f"   ! {symbol}: не удалось получить цены ({last_err})")
    return None, None


def sma(values, n):
    return statistics.fmean(values[-n:]) if len(values) >= n else None


def print_screen():
    print("=" * 78)
    print("ИСКЛЮЧЁННЫЕ СЕКТОРА (секторный скрин — основной доход в этих категориях не берём):")
    for sector, why in EXCLUDED_SECTORS.items():
        print(f"   ✗ {sector:<24s} — {why}")
    print()
    print("БАЛАНСОВЫЕ ПОРОГИ (консервативные — ПРОВЕРЯТЬ по свежему отчёту):")
    print(f"   • процентный долг / рыночная кап.            < {THRESH_DEBT*100:.0f}%")
    print(f"   • (кэш + процентные бумаги) / рыночная кап.  < {THRESH_CASH*100:.0f}%")
    print(f"   • примесь исключённой выручки                < {THRESH_IMPURE*100:.0f}%")


def print_watchlist():
    print()
    print("=" * 78)
    print(f"ВОТЧЛИСТ (курируемый, {len(WATCHLIST)} компаний — обычно проходят скрин):")
    print("(коэффициенты НАДО перепроверить по свежему финотчёту — не инвестсовет)")
    print("-" * 78)
    print(f"   {'ТИКЕР':<6s} {'КОМПАНИЯ':<20s} {'СЕКТОР':<18s} ЗАМЕТКА / ЧТО СВЕРИТЬ")
    for tic, (name, sector, note) in WATCHLIST.items():
        print(f"   {tic:<6s} {name:<20s} {sector:<18s} {note}")


def trend_signal(symbols, n):
    print()
    print("=" * 78)
    print(f"ТРЕНДОВЫЙ СИГНАЛ (Yahoo daily, SMA{n}; спот/лонг/без плеча)")
    print("-" * 78)
    uptrend, downtrend, failed = [], [], []
    asof = None
    for tic in symbols:
        ts, closes = fetch_closes(tic, rng="1y")
        if not closes or len(closes) < n + 1:
            failed.append(tic)
            continue
        ma = sma(closes, n)
        price = closes[-1]
        ext = price / ma - 1.0
        day = datetime.fromtimestamp(ts[-1], tz=timezone.utc).strftime("%Y-%m-%d")
        asof = day if asof is None else max(asof, day)
        (uptrend if price > ma else downtrend).append((tic, ext))
        time.sleep(0.15)
    uptrend.sort(key=lambda x: x[1], reverse=True)
    downtrend.sort(key=lambda x: x[1], reverse=True)
    scanned = len(uptrend) + len(downtrend)
    print(f"\nas of {asof or '?'}  |  в АПТРЕНДЕ {len(uptrend)} из {scanned} просканированных")
    if uptrend:
        w = 100.0 / len(uptrend)
        print(f"\nВ АПТРЕНДЕ (price > SMA{n}), равный вес {w:.1f}% каждая:")
        for tic, ext in uptrend:
            print(f"   🟢 {tic:<6s} {WATCHLIST[tic][0]:<20s} +{ext*100:5.1f}% над SMA{n}")
    else:
        print("\nСейчас ни одна бумага не в аптренде → аллокация = 0 (ждём).")
    if downtrend:
        print(f"\nНИЖЕ SMA{n} (ждём разворот вверх, не покупаем):")
        print("   " + ", ".join(f"{t}({e*100:+.0f}%)" for t, e in downtrend))
    if failed:
        print(f"\nНет данных / мало истории: {', '.join(failed)}")
    print(f"\nПРАВИЛО: купить спот равным весом только те, что выше SMA{n}; упала ниже → продать.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Скринер акций (сектор+баланс) + трендовый сигнал.")
    ap.add_argument("--sma", type=int, default=50)
    ap.add_argument("--no-trend", action="store_true", help="только вотчлист, без сети")
    args = ap.parse_args(argv)
    print("#" * 78)
    print("#  СКРИНЕР АКЦИЙ — не инвестиционный совет. Курируемый список;")
    print("#  коэффициенты (долг/кап, кэш/кап, примесь выручки) проверяйте сами по отчёту.")
    print("#" * 78)
    print()
    print_screen()
    print_watchlist()
    if args.no_trend:
        print("\n(--no-trend: сетевой сигнал пропущен.)")
        return 0
    trend_signal(list(WATCHLIST.keys()), args.sma)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
