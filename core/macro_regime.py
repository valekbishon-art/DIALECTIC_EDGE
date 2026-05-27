"""
macro_regime.py — Макро-режим рынка (равно как «фон»).

Считаем три независимых сигнала, объединяем в один MacroRegime:
1. Тренд S&P 500 (через ^GSPC): EMA200 + SMA50 + текущая цена.
2. Breadth: % акций S&P-500 выше 50DMA (тикер `^A50` на Yahoo Finance,
   при ошибке — fallback-прокси по корзине крупных ETF/Mag-7).
3. DXY (доллар) — растёт вверх → headwind для рисковых активов,
   падает → tailwind.

Объединение:
  RISK_ON  — S&P в тренде вверх, breadth здоровый, DXY не растёт.
  RISK_OFF — S&P в тренде вниз ИЛИ breadth слабый ИЛИ DXY ракетой.
  NEUTRAL  — всё остальное (один сигнал «за», другой «против»).

Используется автотрейдером (фильтр LONG/SHORT и множитель размера) и
кнопкой «🎯 Стратегия» (показываем юзеру что соответствует текущему фону).

Без сторонних зависимостей: Yahoo Finance chart API через aiohttp
(идентично market_data.py). Все запросы с таймаутом и обработкой ошибок.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, asdict
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────
_YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_YF_HEADERS = {"User-Agent": "Mozilla/5.0"}
_YF_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Bucket для breadth-fallback: крупные сектора + Mag-7 — простой proxy на
# «широта рынка». Не идеален, но работает без платных API.
_BREADTH_FALLBACK_TICKERS = [
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLB",
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
]


@dataclass
class MacroRegime:
    """Свёрнутый макро-режим рынка."""
    regime: str            # RISK_ON | RISK_OFF | NEUTRAL
    sp_trend: str          # BULL | BEAR | MIXED | UNKNOWN
    sp_price: float        # последнее close SPY
    sp_sma50: float
    sp_ema200: float
    dxy_trend: str         # RISING | FALLING | FLAT | UNKNOWN
    dxy_price: float
    dxy_sma50: float
    breadth_pct: float     # 0..100, % бумаг выше 50DMA
    breadth_source: str    # ^A50 | proxy | unknown
    position_size_mult: float  # 0.4..1.0
    allow_longs: bool
    allow_shorts: bool
    recommendation: str    # человекочитаемая фраза

    def to_dict(self) -> dict:
        return asdict(self)

    def short_summary(self) -> str:
        """Однострочная сводка для логов / Telegram."""
        return (
            f"{self.regime} | S&P {self.sp_trend} | "
            f"DXY {self.dxy_trend} | breadth {self.breadth_pct:.0f}%"
        )


# ─── Загрузка свечей ─────────────────────────────────────────────────────────

async def _fetch_yahoo_closes(
    session: aiohttp.ClientSession, ticker: str, range_: str = "1y", interval: str = "1d"
) -> list[float]:
    """Достаём список close-цен из Yahoo chart API.

    Пустой список — значит данные недоступны (API упал, тикер невалидный,
    rate-limit). Не бросаем — макро-режим должен деградировать мягко.
    """
    try:
        url = _YF_CHART_URL.format(ticker=ticker)
        params = {"interval": interval, "range": range_, "includePrePost": "false"}
        async with session.get(
            url, params=params, headers=_YF_HEADERS, timeout=_YF_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
        result = (data.get("chart", {}).get("result") or [{}])[0]
        indicators = result.get("indicators", {}) or {}
        quote = (indicators.get("quote") or [{}])[0]
        closes_raw = quote.get("close") or []
        closes = [float(c) for c in closes_raw if c is not None]
        return closes
    except Exception as e:
        logger.debug("yahoo closes fetch failed for %s: %s", ticker, e)
        return []


async def _fetch_last_close(session: aiohttp.ClientSession, ticker: str) -> Optional[float]:
    """Последний close (для breadth-proxy)."""
    closes = await _fetch_yahoo_closes(session, ticker, range_="3mo", interval="1d")
    if not closes:
        return None
    return closes[-1]


# ─── Индикаторы ──────────────────────────────────────────────────────────────

def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    # seed = SMA первых period значений
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


# ─── Breadth ─────────────────────────────────────────────────────────────────

async def _fetch_breadth(session: aiohttp.ClientSession) -> tuple[float, str]:
    """% акций S&P-500 выше 50DMA.

    Сначала пробуем ^A50 (NYSE-индекс % above 50DMA), потом ^S5FI/$S5FI,
    потом fallback-proxy: считаем сами по корзине ETF+Mag-7.
    """
    for ticker in ("^A50", "S5FI", "$S5FI"):
        last = await _fetch_last_close(session, ticker)
        if last is not None and 0 <= last <= 100:
            return float(last), ticker

    above = 0
    total = 0
    for sym in _BREADTH_FALLBACK_TICKERS:
        closes = await _fetch_yahoo_closes(session, sym, range_="6mo", interval="1d")
        if len(closes) < 50:
            continue
        sma50 = _sma(closes, 50)
        if sma50 is None:
            continue
        total += 1
        if closes[-1] > sma50:
            above += 1
    if total == 0:
        return 50.0, "unknown"
    return (above / total) * 100, "proxy"


# ─── Главный вызов ───────────────────────────────────────────────────────────

async def detect_macro_regime() -> MacroRegime:
    """Считает макро-режим. Никогда не бросает: при сетевых сбоях возвращает
    NEUTRAL с пометкой UNKNOWN."""
    async with aiohttp.ClientSession() as session:
        spy_task = _fetch_yahoo_closes(session, "%5EGSPC", range_="2y", interval="1d")
        dxy_task = _fetch_yahoo_closes(session, "DX-Y.NYB", range_="1y", interval="1d")
        breadth_task = _fetch_breadth(session)
        spy_closes, dxy_closes, (breadth_pct, breadth_src) = await asyncio.gather(
            spy_task, dxy_task, breadth_task, return_exceptions=False
        )

    # ── S&P trend ──
    sp_trend = "UNKNOWN"
    sp_price = sp_sma50 = sp_ema200 = 0.0
    if len(spy_closes) >= 200:
        sp_price = spy_closes[-1]
        sp_sma50 = _sma(spy_closes, 50) or 0.0
        sp_ema200 = _ema(spy_closes, 200) or 0.0
        if sp_price > sp_sma50 > sp_ema200:
            sp_trend = "BULL"
        elif sp_price < sp_sma50 < sp_ema200:
            sp_trend = "BEAR"
        else:
            sp_trend = "MIXED"

    # ── DXY trend ──
    dxy_trend = "UNKNOWN"
    dxy_price = dxy_sma50 = 0.0
    if len(dxy_closes) >= 50:
        dxy_price = dxy_closes[-1]
        dxy_sma50 = _sma(dxy_closes, 50) or 0.0
        # 1% буфер чтобы не дёргалось у самой средней
        if dxy_price > dxy_sma50 * 1.01:
            dxy_trend = "RISING"
        elif dxy_price < dxy_sma50 * 0.99:
            dxy_trend = "FALLING"
        else:
            dxy_trend = "FLAT"

    # ── Свод ──
    bull_votes = 0
    bear_votes = 0
    if sp_trend == "BULL":
        bull_votes += 2
    elif sp_trend == "BEAR":
        bear_votes += 2
    elif sp_trend == "MIXED":
        bull_votes += 0  # без голоса

    if breadth_pct >= 60:
        bull_votes += 1
    elif breadth_pct <= 40:
        bear_votes += 1

    if dxy_trend == "FALLING":
        bull_votes += 1
    elif dxy_trend == "RISING":
        bear_votes += 1

    if bull_votes >= 3 and bear_votes == 0:
        regime = "RISK_ON"
        position_size_mult = 1.0
        allow_longs, allow_shorts = True, False
        recommendation = (
            "RISK_ON: S&P в восходящем тренде, breadth здоровый, доллар не давит. "
            "Разрешены LONG, размер обычный. Шорты — нет."
        )
    elif bear_votes >= 3 and bull_votes == 0:
        regime = "RISK_OFF"
        position_size_mult = 0.4
        allow_longs, allow_shorts = False, True
        recommendation = (
            "RISK_OFF: S&P в нисходящем тренде, breadth слабый, доллар вверх. "
            "LONG запрещены. Можно только шорты, размер ×0.4."
        )
    elif bear_votes >= 2 and bear_votes > bull_votes:
        regime = "RISK_OFF"
        position_size_mult = 0.5
        allow_longs, allow_shorts = False, True
        recommendation = (
            "RISK_OFF (умеренный): большинство фильтров против лонгов. "
            "LONG не открываем, шорты — размер ×0.5."
        )
    elif bull_votes >= 2 and bull_votes > bear_votes:
        regime = "RISK_ON"
        position_size_mult = 0.85
        allow_longs, allow_shorts = True, False
        recommendation = (
            "RISK_ON (умеренный): большинство фильтров за лонги. "
            "Лонги ×0.85, шорты не открываем."
        )
    else:
        regime = "NEUTRAL"
        position_size_mult = 0.6
        allow_longs, allow_shorts = True, True
        recommendation = (
            "NEUTRAL: фильтры противоречат друг другу. "
            "Любая сторона — размер ×0.6, готовь короткие стопы."
        )

    macro = MacroRegime(
        regime=regime,
        sp_trend=sp_trend,
        sp_price=round(sp_price, 2),
        sp_sma50=round(sp_sma50, 2),
        sp_ema200=round(sp_ema200, 2),
        dxy_trend=dxy_trend,
        dxy_price=round(dxy_price, 3),
        dxy_sma50=round(dxy_sma50, 3),
        breadth_pct=round(breadth_pct, 1),
        breadth_source=breadth_src,
        position_size_mult=position_size_mult,
        allow_longs=allow_longs,
        allow_shorts=allow_shorts,
        recommendation=recommendation,
    )
    logger.info("MacroRegime: %s", macro.short_summary())
    return macro


# ─── Кэш ─────────────────────────────────────────────────────────────────────
# Не дёргаем Yahoo каждый цикл — макро меняется медленно. TTL 30 минут.

_cache_value: Optional[MacroRegime] = None
_cache_ts: float = 0.0
_CACHE_TTL = 30 * 60  # 30 минут


async def get_macro_regime(force_refresh: bool = False) -> MacroRegime:
    """Кэшированный доступ к макро-режиму. Используй из горячего пути."""
    global _cache_value, _cache_ts
    now = _time.time()
    if not force_refresh and _cache_value is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache_value
    try:
        macro = await detect_macro_regime()
        _cache_value = macro
        _cache_ts = now
        return macro
    except Exception as e:
        logger.warning("detect_macro_regime failed: %s", e)
        if _cache_value is not None:
            return _cache_value
        # совсем плохой кейс — возвращаем безопасный NEUTRAL
        return MacroRegime(
            regime="NEUTRAL",
            sp_trend="UNKNOWN",
            sp_price=0.0,
            sp_sma50=0.0,
            sp_ema200=0.0,
            dxy_trend="UNKNOWN",
            dxy_price=0.0,
            dxy_sma50=0.0,
            breadth_pct=50.0,
            breadth_source="unknown",
            position_size_mult=0.6,
            allow_longs=True,
            allow_shorts=True,
            recommendation="Макро-данные недоступны — режим NEUTRAL по умолчанию.",
        )


def format_macro_block(macro: MacroRegime) -> str:
    """Готовый markdown-блок для вставки в digest / money button."""
    emoji = {"RISK_ON": "🟢", "RISK_OFF": "🔴", "NEUTRAL": "⚪️"}.get(macro.regime, "⚪️")
    lines = [
        f"{emoji} *Макро: {macro.regime}*",
        f"• S&P: *{macro.sp_trend}* (цена ${macro.sp_price:,.2f}, SMA50 ${macro.sp_sma50:,.2f}, EMA200 ${macro.sp_ema200:,.2f})",
        f"• DXY: *{macro.dxy_trend}* (${macro.dxy_price:,.2f} vs SMA50 ${macro.dxy_sma50:,.2f})",
        f"• Breadth: *{macro.breadth_pct:.0f}%* выше 50DMA ({macro.breadth_source})",
        f"• Стратегия: {macro.recommendation}",
    ]
    return "\n".join(lines)
