"""
pump_scanner.py — детектор пампов по всем спот-парам USDT кросс-биржево.

Фича ПАМП (юзер, скрин из Telegram-сканера):
  «хочу чтобы каждую монету так тестили» — сканируем ВЕСЬ спот-рынок
  (Bybit + MEXC + Binance), ищем монеты, которые ПРЯМО СЕЙЧАС резко
  растут на повышенном объёме, и шлём алерт с графиком и кнопками
  на биржи где монета листится.

ПАМП = совпадение ВСЕХ 3 условий одновременно:
  1. Цена выросла на >= PUMP_MIN_PCT (%, default 5) за последние
     PUMP_WINDOW_MIN минут (default 30), считаем по 1m свечам.
  2. Объём за текущее окно (30 мин) >= PUMP_VOL_MULT (default 3x) выше
     среднего 30-минутного объёма за последние 24 часа.
  3. Монета НЕ пампила > PUMP_MAX_PRIOR_PCT (%, default 10) за последние
     3 дня — исключаем уже разогретые монеты (по дневным свечам).

Доп. фильтры мусора (юзер):
  - только спот пары к USDT;
  - market cap монеты в диапазоне [PUMP_MCAP_MIN, PUMP_MCAP_MAX]
    (default 10M..500M USD), если капитализация известна;
  - цена > PUMP_PRICE_FLOOR (default 0.001) — отсекаем шиткоины за доли копейки.

Дизайн:
  - Ядро (детект + фильтры) — чистые stdlib-функции, полностью покрыты
    юнит-тестами (tests/test_pump_scanner.py). Сеть НЕ нужна для тестов.
  - Оркестрация фетча (async, aiohttp) — best-effort и non-fatal: любая
    ошибка по бирже/монете логируется и пропускается, скан не падает целиком.
  - Никакой авто-торговли. Только информация.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MINUTES_PER_DAY = 1440


# ──────────────────────────────── конфиг ────────────────────────────────────
def _env_float(name: str, default: float, *, min_val: float = 0.0,
               max_val: float = 1e12) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return max(min_val, min(max_val, v))


def _env_int(name: str, default: int, *, min_val: int = 0,
             max_val: int = 10 ** 9) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(min_val, min(max_val, v))


@dataclass(frozen=True)
class PumpConfig:
    """Пороги детекта. Берутся из env с разумными дефолтами."""
    min_pct: float = 5.0           # +>=5% за окно
    window_min: int = 30           # окно в минутах
    vol_mult: float = 3.0          # объём окна >= 3x среднего
    max_prior_pct: float = 10.0    # не пампила >10% за 3 дня
    prior_days: int = 3
    price_floor: float = 0.001     # цена > 0.001
    mcap_min: float = 10_000_000.0
    mcap_max: float = 500_000_000.0

    @property
    def window_candles(self) -> int:
        """Сколько 1m свечей в окне."""
        return max(1, int(self.window_min))

    @classmethod
    def from_env(cls) -> "PumpConfig":
        return cls(
            min_pct=_env_float("PUMP_MIN_PCT", 5.0, min_val=0.1, max_val=1000.0),
            window_min=_env_int("PUMP_WINDOW_MIN", 30, min_val=1, max_val=720),
            vol_mult=_env_float("PUMP_VOL_MULT", 3.0, min_val=1.0, max_val=100.0),
            max_prior_pct=_env_float("PUMP_MAX_PRIOR_PCT", 10.0, min_val=0.0,
                                     max_val=1000.0),
            prior_days=_env_int("PUMP_PRIOR_DAYS", 3, min_val=1, max_val=30),
            price_floor=_env_float("PUMP_PRICE_FLOOR", 0.001, min_val=0.0,
                                   max_val=1e9),
            mcap_min=_env_float("PUMP_MCAP_MIN", 10_000_000.0, min_val=0.0),
            mcap_max=_env_float("PUMP_MCAP_MAX", 500_000_000.0, min_val=0.0,
                                max_val=1e15),
        )


# ───────────────────────── биржи / торговые ссылки ──────────────────────────
# Человекочитаемое имя биржи -> функция строящая ссылку на спот-пару BASE/USDT.
EXCHANGE_TRADE_URL = {
    "Bybit": lambda base: f"https://www.bybit.com/en/trade/spot/{base}/USDT",
    "MEXC": lambda base: f"https://www.mexc.com/exchange/{base}_USDT",
    "Binance": lambda base: f"https://www.binance.com/en/trade/{base}_USDT",
    "Gate": lambda base: f"https://www.gate.io/trade/{base}_USDT",
}


def trade_url(exchange: str, base: str) -> Optional[str]:
    fn = EXCHANGE_TRADE_URL.get(exchange)
    if fn is None:
        return None
    try:
        return fn(base.upper())
    except Exception:
        return None


# ──────────────────────────── чистая математика ─────────────────────────────
def pct_change(old: Optional[float], new: Optional[float]) -> float:
    """Процентное изменение new относительно old. 0 при некорректном old."""
    if old is None or new is None or old == 0:
        return 0.0
    return (new - old) / old * 100.0


def window_pump_pct(closes: list[float], window_candles: int) -> float:
    """Рост цены за последние window_candles свечей: (last - anchor)/anchor*100.

    anchor — close на старте окна. Если данных меньше окна, берём самый
    ранний доступный close как якорь.
    """
    if not closes or len(closes) < 2:
        return 0.0
    n = min(int(window_candles), len(closes) - 1)
    anchor = closes[-(n + 1)]
    return pct_change(anchor, closes[-1])


def window_anchor_price(closes: list[float], window_candles: int) -> Optional[float]:
    """Цена-якорь на старте окна (для отображения 'from -> to')."""
    if not closes:
        return None
    if len(closes) == 1:
        return closes[0]
    n = min(int(window_candles), len(closes) - 1)
    return closes[-(n + 1)]


def volume_ratio(window_volume: float, vol_24h_total: float, window_min: int,
                 day_min: int = MINUTES_PER_DAY) -> float:
    """Во сколько раз объём окна выше среднего объёма такого же окна за сутки.

    avg_window_vol = vol_24h_total * (window_min / day_min)
    ratio = window_volume / avg_window_vol
    """
    if vol_24h_total <= 0 or window_min <= 0:
        return 0.0
    avg_window_vol = vol_24h_total * (float(window_min) / float(day_min))
    if avg_window_vol <= 0:
        return 0.0
    return window_volume / avg_window_vol


def max_rise_pct(closes: list[float]) -> float:
    """Максимальный run-up (%) на серии: max по j>i от (close[j]-min_до_j)/min*100.

    Используем дневные закрытия за prior_days дней, чтобы понять — монета уже
    разогрета (сильно росла) или ещё нет. O(n), один проход.
    """
    best = 0.0
    min_so_far: Optional[float] = None
    for c in closes:
        if c is None:
            continue
        if min_so_far is None or c < min_so_far:
            min_so_far = c
        if min_so_far and min_so_far > 0:
            rise = (c - min_so_far) / min_so_far * 100.0
            if rise > best:
                best = rise
    return best


def passes_static_filters(price: Optional[float], mcap: Optional[float], *,
                          cfg: PumpConfig) -> bool:
    """Цена и market cap фильтры. Неизвестный mcap не отсекаем (best-effort)."""
    if price is None or price < cfg.price_floor:
        return False
    if mcap is not None:
        if mcap < cfg.mcap_min or mcap > cfg.mcap_max:
            return False
    return True


@dataclass
class PumpMetrics:
    pump_pct: float
    vol_ratio: float
    prior_pct: float
    price_from: Optional[float]
    price_to: Optional[float]


def evaluate_pump(
    closes_1m: list[float],
    vols_1m: list[float],
    vol_24h_total: float,
    daily_closes: list[float],
    *,
    price: Optional[float] = None,
    mcap: Optional[float] = None,
    cfg: Optional[PumpConfig] = None,
) -> tuple[bool, PumpMetrics, list[str]]:
    """Главный детектор. Возвращает (is_pump, метрики, список причин-отказов).

    is_pump=True только если выполнены ВСЕ 3 условия И пройдены статические фильтры.
    """
    cfg = cfg or PumpConfig()
    fails: list[str] = []

    last_price = price if price is not None else (closes_1m[-1] if closes_1m else None)
    anchor = window_anchor_price(closes_1m, cfg.window_candles)

    pump = window_pump_pct(closes_1m, cfg.window_candles)
    win = cfg.window_candles
    window_volume = sum(vols_1m[-win:]) if vols_1m else 0.0
    vr = volume_ratio(window_volume, vol_24h_total, cfg.window_min)
    prior = max_rise_pct(daily_closes)

    metrics = PumpMetrics(
        pump_pct=pump, vol_ratio=vr, prior_pct=prior,
        price_from=anchor, price_to=last_price,
    )

    if not passes_static_filters(last_price, mcap, cfg=cfg):
        fails.append("static_filters")
    if pump < cfg.min_pct:
        fails.append("pump_pct")
    if vr < cfg.vol_mult:
        fails.append("vol_ratio")
    if prior > cfg.max_prior_pct:
        fails.append("already_heated")

    return (len(fails) == 0, metrics, fails)


# ─────────────────────────────── модель сигнала ─────────────────────────────
@dataclass
class PumpSignal:
    asset: str                       # BASE (например 0PN)
    pump_pct: float
    vol_ratio: float
    prior_pct: float
    price_from: Optional[float]
    price_to: Optional[float]
    window_min: int
    venues: list[str] = field(default_factory=list)   # где листится
    mcap: Optional[float] = None
    closes: list[float] = field(default_factory=list)  # для графика
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def venue_buttons(self) -> list[tuple[str, str]]:
        """[(подпись, url)] для inline-кнопок на биржи."""
        out: list[tuple[str, str]] = []
        for v in self.venues:
            url = trade_url(v, self.asset)
            if url:
                out.append((f"Биржа {v.upper()}", url))
        return out


def _fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "?"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    return f"{p:.6f}".rstrip("0").rstrip(".")


def format_pump_alert(sig: PumpSignal) -> str:
    """Текст алерта (Markdown) в стиле скрина сканера."""
    venues = " · ".join(v.upper() for v in sig.venues) if sig.venues else "—"
    lines = [
        f"🚀 *{sig.asset}*",
        f"Pump: *{sig.pump_pct:.2f}%* за {sig.window_min} мин",
        f"{_fmt_price(sig.price_from)} → {_fmt_price(sig.price_to)}",
        f"📊 Объём: x{sig.vol_ratio:.1f} от среднего",
    ]
    if sig.mcap:
        lines.append(f"💰 MCap: ${sig.mcap/1_000_000:.0f}M")
    lines.append(f"🏦 Биржи: {venues}")
    lines.append("")
    lines.append("⚠️ _Это не сигнал на покупку. Памп — высокий риск, DYOR._")
    return "\n".join(lines)


# ─────────────────────────── оркестрация фетча (async) ──────────────────────
# Всё ниже — best-effort через aiohttp. Импортим лениво, чтобы модуль грузился
# даже без сети (для юнит-тестов ядра aiohttp не нужен).
_BYBIT_SPOT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=spot"
_BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
_BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"
_BINANCE_KLINE = "https://api.binance.com/api/v3/klines"
_MEXC_24H = "https://api.mexc.com/api/v3/ticker/24hr"
_MEXC_KLINE = "https://api.mexc.com/api/v3/klines"
_COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"


def _base_from_usdt_symbol(symbol: str) -> Optional[str]:
    """'0PNUSDT' -> '0PN'. Только чистые спот-USDT, без leveraged-токенов."""
    s = symbol.upper()
    if not s.endswith("USDT"):
        return None
    base = s[:-4]
    # отсекаем плечевые токены и мусор: 3L/3S/5L/5S/UP/DOWN/BULL/BEAR
    for suf in ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR"):
        if base.endswith(suf):
            return None
    if not base or not base.replace("_", "").isalnum():
        return None
    return base


@dataclass
class _Ticker:
    base: str
    price: float
    quote_vol_24h: float
    venues: set


async def _fetch_json(session, url: str, *, params: dict | None = None,
                      timeout: int = 12):
    import aiohttp
    async with session.get(url, params=params,
                           timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def _universe_binance_like(session, url: str, venue: str) -> dict:
    """Binance / MEXC: /ticker/24hr -> {base: _Ticker}. Совместимый формат."""
    out: dict[str, _Ticker] = {}
    try:
        data = await _fetch_json(session, url)
    except Exception as e:  # noqa: BLE001
        logger.warning("pump: %s tickers failed: %s", venue, e)
        return out
    if not isinstance(data, list):
        return out
    for t in data:
        try:
            base = _base_from_usdt_symbol(t.get("symbol", ""))
            if not base:
                continue
            price = float(t.get("lastPrice") or 0)
            qv = float(t.get("quoteVolume") or 0)
            if price <= 0:
                continue
            out[base] = _Ticker(base=base, price=price, quote_vol_24h=qv,
                                venues={venue})
        except (ValueError, TypeError, AttributeError):
            continue
    return out


async def _universe_bybit(session) -> dict:
    out: dict[str, _Ticker] = {}
    try:
        data = await _fetch_json(session, _BYBIT_SPOT_TICKERS)
    except Exception as e:  # noqa: BLE001
        logger.warning("pump: bybit tickers failed: %s", e)
        return out
    rows = (data or {}).get("result", {}).get("list", []) if isinstance(data, dict) else []
    for t in rows:
        try:
            base = _base_from_usdt_symbol(t.get("symbol", ""))
            if not base:
                continue
            price = float(t.get("lastPrice") or 0)
            qv = float(t.get("turnover24h") or 0)
            if price <= 0:
                continue
            out[base] = _Ticker(base=base, price=price, quote_vol_24h=qv,
                                venues={"Bybit"})
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def merge_universes(*universes: dict) -> dict:
    """Сливает {base:_Ticker} с разных бирж. venues объединяются, цена/объём —
    берём максимум объёма как репрезентативные (для отображения)."""
    merged: dict[str, _Ticker] = {}
    for uni in universes:
        for base, tk in uni.items():
            if base not in merged:
                merged[base] = _Ticker(base=base, price=tk.price,
                                       quote_vol_24h=tk.quote_vol_24h,
                                       venues=set(tk.venues))
            else:
                m = merged[base]
                m.venues |= tk.venues
                if tk.quote_vol_24h > m.quote_vol_24h:
                    m.quote_vol_24h = tk.quote_vol_24h
                    m.price = tk.price
    return merged


async def _fetch_mcap_map(session, pages: int = 2) -> dict:
    """CoinGecko {SYMBOL_UPPER: market_cap}. Best-effort, без ключа."""
    out: dict[str, float] = {}
    for page in range(1, max(1, pages) + 1):
        try:
            data = await _fetch_json(
                session, _COINGECKO_MARKETS,
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 250, "page": page},
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("pump: coingecko page %s failed: %s", page, e)
            break
        if not isinstance(data, list) or not data:
            break
        for c in data:
            try:
                sym = str(c.get("symbol", "")).upper()
                mc = c.get("market_cap")
                if sym and mc:
                    out[sym] = float(mc)
            except (ValueError, TypeError):
                continue
    return out


async def _fetch_klines(session, venue: str, base: str, interval: str,
                        limit: int) -> tuple[list[float], list[float]]:
    """(closes, volumes) для пары BASE/USDT. Поддержка Binance/MEXC/Bybit."""
    symbol = f"{base}USDT"
    try:
        if venue == "Bybit":
            iv = {"1m": "1", "5m": "5", "1d": "D"}.get(interval, "1")
            data = await _fetch_json(session, _BYBIT_KLINE, params={
                "category": "spot", "symbol": symbol, "interval": iv,
                "limit": limit})
            rows = (data or {}).get("result", {}).get("list", [])
            rows = list(reversed(rows))  # bybit отдаёт новые->старые
            closes = [float(r[4]) for r in rows]
            vols = [float(r[5]) for r in rows]
            return closes, vols
        url = _MEXC_KLINE if venue == "MEXC" else _BINANCE_KLINE
        data = await _fetch_json(session, url, params={
            "symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(data, list):
            return [], []
        closes = [float(k[4]) for k in data]
        vols = [float(k[5]) for k in data]
        return closes, vols
    except Exception as e:  # noqa: BLE001
        logger.debug("pump: klines %s %s failed: %s", venue, base, e)
        return [], []


async def scan_pumps(*, cfg: Optional[PumpConfig] = None,
                     max_symbols: int = 0,
                     concurrency: int = 12) -> list[PumpSignal]:
    """Полный кросс-биржевой скан. Возвращает найденные PumpSignal (отсортированы
    по pump_pct убыв.). Non-fatal: ошибки логируются, скан не падает.

    max_symbols=0 -> без лимита (скан всех монет). >0 -> топ N по объёму
    (защита для on-demand /pump чтобы не ждать минуты).
    """
    cfg = cfg or PumpConfig.from_env()
    try:
        import aiohttp
    except Exception:  # pragma: no cover
        logger.warning("pump: aiohttp недоступен — скан невозможен")
        return []

    signals: list[PumpSignal] = []
    async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 DialecticEdge/pump"}) as session:
        uni_b, uni_m, uni_by = await asyncio.gather(
            _universe_binance_like(session, _BINANCE_24H, "Binance"),
            _universe_binance_like(session, _MEXC_24H, "MEXC"),
            _universe_bybit(session),
            return_exceptions=False,
        )
        universe = merge_universes(uni_b, uni_m, uni_by)
        mcap_map = await _fetch_mcap_map(session)

        # предфильтр: цена > floor; сортируем по объёму, опционально лимитируем
        candidates = [
            tk for tk in universe.values()
            if tk.price >= cfg.price_floor
        ]
        candidates.sort(key=lambda t: t.quote_vol_24h, reverse=True)
        if max_symbols and max_symbols > 0:
            candidates = candidates[:max_symbols]

        win = cfg.window_candles
        prior_days = cfg.prior_days
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _probe(tk: _Ticker) -> Optional[PumpSignal]:
            async with sem:
                venue = next(iter(tk.venues)) if tk.venues else "Binance"
                # 1m свечи на окно (+запас), и дневные за prior_days
                closes_1m, vols_1m = await _fetch_klines(
                    session, venue, tk.base, "1m", min(1000, win + 5))
                if len(closes_1m) < 2:
                    return None
                daily, _ = await _fetch_klines(
                    session, venue, tk.base, "1d", prior_days + 1)
                mcap = mcap_map.get(tk.base.upper())
                is_pump, metrics, _fails = evaluate_pump(
                    closes_1m, vols_1m, tk.quote_vol_24h, daily,
                    price=tk.price, mcap=mcap, cfg=cfg)
                if not is_pump:
                    return None
                return PumpSignal(
                    asset=tk.base, pump_pct=metrics.pump_pct,
                    vol_ratio=metrics.vol_ratio, prior_pct=metrics.prior_pct,
                    price_from=metrics.price_from, price_to=metrics.price_to,
                    window_min=cfg.window_min, venues=sorted(tk.venues),
                    mcap=mcap, closes=closes_1m,
                )

        results = await asyncio.gather(
            *[_probe(tk) for tk in candidates], return_exceptions=True)
        for r in results:
            if isinstance(r, PumpSignal):
                signals.append(r)
            elif isinstance(r, Exception):
                logger.debug("pump: probe error: %s", r)

    signals.sort(key=lambda s: s.pump_pct, reverse=True)
    return signals


__all__ = [
    "PumpConfig",
    "PumpMetrics",
    "PumpSignal",
    "pct_change",
    "window_pump_pct",
    "window_anchor_price",
    "volume_ratio",
    "max_rise_pct",
    "passes_static_filters",
    "evaluate_pump",
    "format_pump_alert",
    "merge_universes",
    "trade_url",
    "scan_pumps",
    "EXCHANGE_TRADE_URL",
]
