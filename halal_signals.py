"""halal_signals.py — спот-сигналы для бота (только лонг, без плеча/деривативов).

Две команды-фичи:
  • build_stocks_card()  — скрин акций из курируемого вотчлиста + тренд (SMA) и
    моментум (полугодовая доходность); красивая карточка с топ-пиками.
  • build_crypto_trend_card() — какие крупные спот-монеты сейчас в аптренде (price>SMA),
    равный вес; спарклайны цены.
  • build_dca_plan() — план усреднения (DCA): разбивка депозита на N траншей.

Данные: дневные close c Yahoo chart API (без ключа). Сеть — через asyncio.to_thread,
чтобы не блокировать луп. Никакой религиозной терминологии в выводе — только рынок.
Не инвестиционный совет.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import urllib.request
from typing import NamedTuple, Sequence

import links
import ui_kit


class CardResult(NamedTuple):
    """Результат сборки карточки: текст + топ-тикеры для inline-кнопок.

    text  — готовый markdown для Telegram.
    picks — символы топ-пиков (крипта: 'BTC'; акции: 'AAPL'), по которым
            main.py строит URL-кнопки на график. Может быть пустым.
    """

    text: str
    picks: list[str]

_YH = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
_UA = {"User-Agent": "Mozilla/5.0"}

# Крупные спот-монеты для тренд-сканера (Yahoo тикеры «<COIN>-USD»).
CRYPTO_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "LINK",
                   "DOT", "LTC", "ATOM", "BCH", "NEAR", "ICP", "FIL", "ALGO"]


def _fetch_closes_sync(symbol: str, rng: str = "1y", retries: int = 3):
    """Дневные close для тикера (Yahoo). Возвращает список float или None."""
    url = _YH.format(sym=symbol, rng=rng)
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            raw = urllib.request.urlopen(req, timeout=15).read()
            d = json.loads(raw)
            res = d["chart"]["result"][0]
            quotes = res["indicators"]["quote"][0]["close"]
            closes = [float(c) for c in quotes if c is not None]
            return closes if len(closes) > 5 else None
        except Exception:  # noqa: BLE001
            continue
    return None


async def fetch_closes(symbol: str, rng: str = "1y"):
    return await asyncio.to_thread(_fetch_closes_sync, symbol, rng)


def _sma(vals: Sequence[float], n: int):
    return statistics.fmean(vals[-n:]) if len(vals) >= n else None


def momentum(closes: Sequence[float], lookback: int = 126) -> float | None:
    """Доходность за lookback дней (≈полгода = 126 торговых дней для акций)."""
    if len(closes) <= lookback or closes[-lookback - 1] <= 0:
        return None
    return closes[-1] / closes[-lookback - 1] - 1.0


def trend_extension(closes: Sequence[float], n: int):
    """(в аптренде?, насколько price над SMA)."""
    ma = _sma(closes, n)
    if ma is None or ma <= 0:
        return None, None
    return closes[-1] > ma, closes[-1] / ma - 1.0


def _spark(closes: Sequence[float], points: int = 24) -> str:
    """Спарклайн последних точек (прорежаем до points)."""
    if not closes:
        return ""
    tail = closes[-points * 3:] if len(closes) > points * 3 else closes
    step = max(1, len(tail) // points)
    return ui_kit.sparkline(tail[::step])


async def build_stocks_card(sma: int = 50, top: int = 8) -> CardResult:
    """Карточка по акциям: топ по моментуму среди тех, кто в аптренде.

    Возвращает CardResult(text, picks) — picks = тикеры топ-пиков для кнопок.
    """
    try:
        from stock_screener import WATCHLIST
    except Exception:  # noqa: BLE001
        WATCHLIST = {}
    symbols = list(WATCHLIST.keys()) or ["AAPL", "MSFT", "GOOGL", "NVDA", "AMD"]

    results = await asyncio.gather(*[fetch_closes(s, "1y") for s in symbols])
    rows_data = []
    for sym, closes in zip(symbols, results):
        if not closes:
            continue
        up, ext = trend_extension(closes, sma)
        mom = momentum(closes, 126)
        if up is None or mom is None:
            continue
        rows_data.append((sym, closes, up, ext, mom))

    if not rows_data:
        return CardResult(
            "📈 *Акции — скринер*\n\nНе удалось получить котировки (сеть). "
            "Попробуй позже.", [])

    # Сортируем: сначала в аптренде, потом по моментуму.
    rows_data.sort(key=lambda r: (r[2], r[4]), reverse=True)
    holds = [r for r in rows_data if r[2]]
    pick = (holds or rows_data)[:top]

    rows = []
    for i, (sym, closes, up, ext, mom) in enumerate(pick, 1):
        name = WATCHLIST.get(sym, (sym,))[0] if WATCHLIST else sym
        rows.append(
            f"{ui_kit.rank_emoji(i)} *{sym}* · {name}\n"
            f"    `{_spark(closes)}`  {ui_kit.chip(mom)} _6м_\n"
            f"    тренд: {'🟢 выше' if up else '🔴 ниже'} SMA{sma} "
            f"({ui_kit.pct(ext)})\n"
            f"    {links.stock_line(sym)}"
        )
    n_up = len(holds)
    footer = (f"_В аптренде {n_up} из {len(rows_data)}. Равный вес среди зелёных, "
              f"спот/лонг. Не инвест-совет; цифры по балансу сверяй по отчёту._")
    text = ui_kit.card(f"📈 Акции — топ по силе (SMA{sma}, 6м моментум)", rows, footer)
    picks = [r[0] for r in pick[:3]]
    return CardResult(text, picks)


async def build_crypto_trend_card(sma: int = 50, universe: Sequence[str] | None = None) -> CardResult:
    """Карточка крипто-тренда: кто сейчас в аптренде (price>SMA), равный вес.

    Возвращает CardResult(text, picks) — picks = монеты в аптренде для кнопок.
    """
    uni = list(universe or CRYPTO_UNIVERSE)
    results = await asyncio.gather(*[fetch_closes(f"{c}-USD", "1y") for c in uni])
    hold, cash = [], []
    sparks = {}
    for coin, closes in zip(uni, results):
        if not closes:
            continue
        up, ext = trend_extension(closes, sma)
        if up is None:
            continue
        sparks[coin] = _spark(closes)
        (hold if up else cash).append((coin, ext))

    if not hold and not cash:
        return CardResult(
            "🧭 *Крипто-тренд*\n\nНе удалось получить котировки (сеть). "
            "Попробуй позже.", [])

    hold.sort(key=lambda x: x[1], reverse=True)
    rows = []
    if hold:
        w = 100.0 / len(hold)
        rows.append(f"*Держать (спот, равный вес {w:.0f}%):*")
        for i, (coin, ext) in enumerate(hold, 1):
            rows.append(f"   🟢 *{coin:<5}* `{sparks.get(coin,'')}`  +{ext*100:.0f}% над SMA{sma}")
            rows.append(f"        {links.crypto_line(coin)}")
    if cash:
        rows.append("")
        rows.append("*В стейбл (ниже SMA, ждём):* " + ", ".join(c for c, _ in cash))
    footer = (f"_В аптренде {len(hold)} из {len(hold)+len(cash)}. Правило: выше SMA{sma} → "
              f"купи спот равным весом; ниже → продай в стейбл. Не инвест-совет._")
    text = ui_kit.card(f"🧭 Крипто-тренд (SMA{sma}, спот/лонг)", rows, footer)
    picks = [coin for coin, _ in hold[:3]]
    return CardResult(text, picks)


def build_dca_plan(deposit: float, tranches: int = 6, days: int = 5) -> str:
    """План усреднения (DCA): равные транши с интервалом. Чистый расчёт, без сети."""
    tranches = max(2, min(24, int(tranches)))
    per = deposit / tranches
    rows = [f"*Депозит:* {ui_kit.money(deposit)} → *{tranches}* транша(ей) "
            f"по {ui_kit.money(per)} каждые *{days} дн.*", ""]
    acc = 0.0
    for i in range(1, tranches + 1):
        acc += per
        filled = i / tranches
        rows.append(f"`{ui_kit.bar(filled, 12)}` день {(i-1)*days:>3}: "
                    f"+{ui_kit.money(per)}  (накоплено {ui_kit.money(acc)})")
    footer = ("_DCA сглаживает цену входа: покупаешь спот частями вместо одного «угадал/нет». "
              "Без плеча. Не инвест-совет._")
    return ui_kit.card("💧 План усреднения (DCA)", rows, footer)
