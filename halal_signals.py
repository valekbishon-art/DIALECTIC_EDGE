"""halal_signals.py — спот-сигналы для бота (только лонг, без плеча/деривативов).
​
Две команды-фичи:
  • build_stocks_card()  — скрин акций из курируемого вотчлиста + тренд (SMA) и
    моментум (полугодовая доходность); красивая карточка с топ-пиками.
  • build_crypto_trend_card() — какие крупные спот-монеты сейчас в аптренде (price>SMA),
    равный вес; спарклайны цены.
  • build_dca_plan() — план усреднения (DCA): разбивка депозита на N траншей.
​
Данные: дневные close c Yahoo chart API (без ключа). Сеть — через asyncio.to_thread,
чтобы не блокировать луп. Никакой религиозной терминологии в выводе — только рынок.
Не инвестиционный совет.
"""
from __future__ import annotations
​
import asyncio
import json
import statistics
import urllib.request
from datetime import date, datetime, time as _dtime, timedelta, timezone
from typing import NamedTuple, Sequence
​
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 — фолбэк на фикс. EST, если нет tzdata
    _ET = timezone(timedelta(hours=-5))
​
import links
import ui_kit
​
​
class CardResult(NamedTuple):
    """Результат сборки карточки: текст + топ-тикеры для inline-кнопок.
​
    text  — готовый markdown для Telegram.
    picks — символы топ-пиков (крипта: 'BTC'; акции: 'AAPL'), по которым
            main.py строит URL-кнопки на график. Может быть пустым.
    """
​
    text: str
    picks: list[str]
​
_YH = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
_UA = {"User-Agent": "Mozilla/5.0"}
​
# Крупные спот-монеты для тренд-сканера (Yahoo тикеры «<COIN>-USD»).
CRYPTO_UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "LINK",
                   "DOT", "LTC", "ATOM", "BCH", "NEAR", "ICP", "FIL", "ALGO"]
​
​
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
​
​
async def fetch_closes(symbol: str, rng: str = "1y"):
    return await asyncio.to_thread(_fetch_closes_sync, symbol, rng)
​
​
# ───────────────────────────────────────────────────────────
# Торговый календарь US (NYSE/NASDAQ) + session-anchored загрузка акций.
# Зачем: Yahoo range=1y — СКОЛЬЗЯЩЕЕ окно, привязанное к моменту запроса,
# и в выходные/после праздника оно может дописывать формирующийся «live»-бар
# (close = regularMarketPrice, не официальный close). Старый _fetch_closes_sync
# брал только массив close без дат — лишний/сдвинутый бар сдвигал ВСЕ
# позиционные индексы -> SMA50 и 6м-моментум менялись БЕЗ новых торгов.
# Ниже — привязка к ДАТАМ и отброс незавершённых сессий.
# ───────────────────────────────────────────────────────────
# Праздники NYSE/NASDAQ (полные закрытия). Достаточно для гейта is_trading_day;
# при желании можно заменить на pandas_market_calendars без правок вызовов.
_US_MARKET_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}
​
​
def is_us_trading_day(d: date) -> bool:
    """True, если d — торговый день NYSE/NASDAQ (будни минус праздники)."""
    if d.weekday() >= 5:            # сб/вс
        return False
    return d.isoformat() not in _US_MARKET_HOLIDAYS
​
​
def _now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(_ET)
​
​
def _et_date(epoch: int) -> date:
    """Дата бара в US/Eastern (Yahoo штампует дневные свечи по открытию сессии)."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(_ET).date()
​
​
def _session_complete(d: date, now_et: datetime) -> bool:
    """Сессия дня d завершена (close финальный)?
​
    Торговый день в прошлом → да. Сегодня → только после 16:00 ET (закрытие NYSE).
    Неторговый/будущий день → нет. Это и есть отброс «live»/формирующегося бара.
    """
    if not is_us_trading_day(d):
        return False
    today = now_et.date()
    if d < today:
        return True
    if d == today:
        return now_et.time() >= _dtime(16, 0)
    return False
​
​
def _fetch_stock_daily_sync(symbol: str, rng: str = "1y", retries: int = 3):
    """Дневные (dates, closes) акции с Yahoo, привязанные к ЗАВЕРШЁННЫМ сессиям.
​
    В отличие от _fetch_closes_sync (крипта):
      • читает timestamp (а не только close);
      • схлопывает в один close на календарный день (US/Eastern), последний за день;
      • отбрасывает формирующийся/«live»-бар и любые незавершённые/неторговые
        хвостовые бары → closes[-1] = последняя ОФИЦИАЛЬНАЯ дневная сессия.
    Возвращает (dates, closes) равной длины или (None, None).
    """
    url = _YH.format(sym=symbol, rng=rng)
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            raw = urllib.request.urlopen(req, timeout=15).read()
            d = json.loads(raw)
            res = d["chart"]["result"][0]
            ts = res.get("timestamp") or []
            quotes = res["indicators"]["quote"][0]["close"]
            by_day: dict[date, float] = {}
            for t, c in zip(ts, quotes):
                if c is None:
                    continue
                by_day[_et_date(t)] = float(c)   # последняя котировка дня перетирает
            if not by_day:
                continue
            now_et = _now_et()
            days = [dd for dd in sorted(by_day) if _session_complete(dd, now_et)]
            closes = [by_day[dd] for dd in days]
            if len(closes) <= 5:
                continue
            return [dd.isoformat() for dd in days], closes
        except Exception:  # noqa: BLE001
            continue
    return None, None
​
​
async def fetch_stock_daily(symbol: str, rng: str = "1y"):
    return await asyncio.to_thread(_fetch_stock_daily_sync, symbol, rng)
​
​
def _sma(vals: Sequence[float], n: int):
    return statistics.fmean(vals[-n:]) if len(vals) >= n else None
​
​
def momentum(closes: Sequence[float], lookback: int = 126) -> float | None:
    """Доходность за lookback дней (≈полгода = 126 торговых дней для акций)."""
    if len(closes) <= lookback or closes[-lookback - 1] <= 0:
        return None
    return closes[-1] / closes[-lookback - 1] - 1.0
​
​
def trend_extension(closes: Sequence[float], n: int):
    """(в аптренде?, насколько price над SMA)."""
    ma = _sma(closes, n)
    if ma is None or ma <= 0:
        return None, None
    return closes[-1] > ma, closes[-1] / ma - 1.0
​
​
def _spark(closes: Sequence[float], points: int = 24) -> str:
    """Спарклайн последних точек (прорежаем до points)."""
    if not closes:
        return ""
    tail = closes[-points * 3:] if len(closes) > points * 3 else closes
    step = max(1, len(tail) // points)
    return ui_kit.sparkline(tail[::step])
​
​
async def build_stocks_card(sma: int = 50, top: int = 8) -> CardResult:
    """Карточка по акциям: топ по моментуму среди тех, кто в аптренде.
​
    Возвращает CardResult(text, picks) — picks = тикеры топ-пиков для кнопок.
    """
    try:
        from stock_screener import WATCHLIST
    except Exception:  # noqa: BLE001
        WATCHLIST = {}
    symbols = list(WATCHLIST.keys()) or ["AAPL", "MSFT", "GOOGL", "NVDA", "AMD"]
​
    results = await asyncio.gather(*[fetch_stock_daily(s, "1y") for s in symbols])
    rows_data = []
    as_of = None                      # дата последней ЗАВЕРШЁННОЙ сессии в данных
    for sym, (dates, closes) in zip(symbols, results):
        if not closes:
            continue
        up, ext = trend_extension(closes, sma)
        mom = momentum(closes, 126)
        if up is None or mom is None:
            continue
        if dates:
            as_of = dates[-1] if as_of is None else max(as_of, dates[-1])
        rows_data.append((sym, closes, up, ext, mom))
​
    if not rows_data:
        return CardResult(
            "📈 *Акции — скринер*\n\nНе удалось получить котировки (сеть). "
            "Попробуй позже.", [])
​
    # Сортируем: сначала в аптренде, потом по моментуму.
    rows_data.sort(key=lambda r: (r[2], r[4]), reverse=True)
    holds = [r for r in rows_data if r[2]]
    pick = (holds or rows_data)[:top]
​
    rows = [
        "ℹ️ *Что это:* акции, отсортированные по силе — полугодовой рост "
        "(моментум) среди тех, кто торгуется выше средней за 50 дней (SMA50).",
        "🎯 *Что делать:* верх списка = самые сильные в аптренде. Спот/лонг, "
        "равный вес. Жми кнопки графиков под карточкой.",
        "",
    ]
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
    # Гейт trading-calendar: если сегодня биржи закрыты — явно пишем, что новых
    # торгов не было. Сами цифры уже session-anchored → не меняются до новой сессии.
    if as_of and not is_us_trading_day(_now_et().date()):
        rows.insert(0, f"🗓 _Биржи US сегодня закрыты — данные на закрытие {as_of}, "
                       f"новых торгов не было._\n")
    asof_txt = f" Данные на закрытие {as_of}." if as_of else ""
    footer = (f"_В аптренде {n_up} из {len(rows_data)}. Равный вес среди зелёных, "
              f"спот/лонг.{asof_txt} Не инвест-совет; цифры по балансу сверяй по отчёту._")
    text = ui_kit.card(f"📈 Акции — топ по силе (SMA{sma}, 6м моментум)", rows, footer)
    picks = [r[0] for r in pick[:3]]
    return CardResult(text, picks)
​
​
async def build_crypto_trend_card(sma: int = 50, universe: Sequence[str] | None = None) -> CardResult:
    """Карточка крипто-тренда: кто сейчас в аптренде (price>SMA), равный вес.
​
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
​
    if not hold and not cash:
        return CardResult(
            "🧭 *Крипто-тренд*\n\nНе удалось получить котировки (сеть). "
            "Попробуй позже.", [])
​
    hold.sort(key=lambda x: x[1], reverse=True)
    rows = [
        "ℹ️ *Что это:* крупные спот-монеты, которые сейчас в восходящем тренде "
        "(цена выше средней за 50 дней, SMA50).",
        "🎯 *Что делать:* что выше линии — держи в споте равным весом; что ниже "
        "— жди в стейбле. Без плеча и шортов.",
        "",
    ]
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
​
​
# Расширенный список для сканера «что разгоня��тся» — мейджоры + крупные альты.
PUMP_UNIVERSE = CRYPTO_UNIVERSE + ["DOGE", "TON", "TRX", "APT", "ARB",
                                   "OP", "INJ", "SUI", "SEI", "RNDR"]
​
​
def _ret(closes: Sequence[float], n: int) -> float | None:
    """Доходность за последние n дней (без сглаживания)."""
    if len(closes) <= n or closes[-n - 1] <= 0:
        return None
    return closes[-1] / closes[-n - 1] - 1.0
​
​
async def build_pump_card(top: int = 8) -> CardResult:
    """«🚀 Что разгоняется» — спот-сканер моментума: монеты с сильным ростом
    за последнюю неделю, подтверждённым аптрендом (цена выше SMA20).
​
    Это НЕ ставка против движения и не плечо — только лонг-моментум на споте:
    показываем, что уже растёт и держится выше короткой средней.
    Возвращает CardResult(text, picks).
    """
    uni = list(PUMP_UNIVERSE)
    results = await asyncio.gather(*[fetch_closes(f"{c}-USD", "3mo") for c in uni])
    rows_data = []
    for coin, closes in zip(uni, results):
        if not closes or len(closes) < 22:
            continue
        r7 = _ret(closes, 7)
        r30 = _ret(closes, 30)
        ma20 = _sma(closes, 20)
        if r7 is None or ma20 is None or ma20 <= 0:
            continue
        above = closes[-1] > ma20          # подтверждение аптренда
        rows_data.append((coin, closes, r7, r30, above))
​
    # Разгон = рост за неделю > 0 И цена выше SMA20 (только лонг-моментум).
    movers = [r for r in rows_data if r[2] > 0 and r[4]]
    movers.sort(key=lambda r: r[2], reverse=True)
​
    if not rows_data:
        return CardResult(
            "🚀 *Что разгоняется*\n\nНе удалось получить котировки (сеть). "
            "Попробуй позже.", [])
​
    rows = [
        "ℹ️ *Что это:* спот-сканер моментума — монеты, которые сильнее всего "
        "выросли за неделю и держатся выше средней за 20 дней (SMA20).",
        "🎯 *Что делать:* это лонг-идеи «что уже разгоняется». Без плеча и "
        "шортов. Не гонись за резким пиком — сверяйся с трендом (/trend).",
        "",
    ]
    if movers:
        pick = movers[:top]
        for i, (coin, closes, r7, r30, _above) in enumerate(pick, 1):
            r30s = f" · 30д {ui_kit.pct(r30)}" if r30 is not None else ""
            rows.append(
                f"{ui_kit.rank_emoji(i)} *{coin:<5}* `{_spark(closes)}`  "
                f"7д {ui_kit.pct(r7)}{r30s}"
            )
            rows.append(f"        {links.crypto_line(coin)}")
        picks = [c for c, *_ in pick[:3]]
    else:
        rows.append("_Сейчас никто не разгоняется: ничего не растёт за неделю "
                    "выше SMA20. Рынок вялый — лучше подождать._")
        picks = []
​
    footer = ("_Моментум = что уже растёт. Сигнал, не приказ: вход частями "
              "(см. /dca), риск контролируй сам. Спот/лонг. Не инвест-совет._")
    text = ui_kit.card("🚀 Что разгоняется (спот, 7-дн моментум)", rows, footer)
    return CardResult(text, picks)
​
​
def build_dca_plan(deposit: float, tranches: int = 6, days: int = 5) -> str:
    """План усреднения (DCA): равные транши с интервалом. Чистый расчёт, без сети."""
    tranches = max(2, min(24, int(tranches)))
    per = deposit / tranches
    rows = [
        "ℹ️ *Что это:* DCA — заход в позицию частями, а не всё сразу.",
        "🎯 *Что делать:* покупай спот по этому графику — равные суммы через "
        "равные интервалы. Так усредняешь цену входа и не «ловишь пик».",
        "",
        f"*Депозит:* {ui_kit.money(deposit)} → *{tranches}* транша(ей) "
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
​
