"""
screener.py — Умный сканер рынка в реальном времени.

Сканирует ТОП монеты на наличие аномалий:
1. Volume Spike (Объем > 200% от среднего)
2. RSI Extremes (<30 или >70)
3. Price Momentum (Изменение цены > 5% за 1ч)
4. Funding Rate Anomaly
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import aiohttp

from backtester import get_candles

logger = logging.getLogger(__name__)

BINANCE_API = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"


class MarketScreener:
    def __init__(self, top_n: int = 20):
        self.top_n = top_n
        # Базовые пары для скрининга
        self.base_pairs = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
            "ADAUSDT", "AVAXUSDT", "DOGEUSDT", "DOTUSDT", "MATICUSDT",
            "LINKUSDT", "SHIBUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT",
            "UNIUSDT", "ETCUSDT", "XLMUSDT", "FILUSDT", "TRXUSDT"
        ]

    async def scan(self) -> List[dict]:
        """
        Запустить сканирование.
        Возвращает список найденных аномалий.
        """
        opportunities = []

        # 1. Получаем 24ч тикеры для объема и цены
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BINANCE_API}/api/v3/ticker/24hr") as resp:
                    if resp.status == 200:
                        tickers = await resp.json()
                        # Фильтруем только USDT пары и сортируем по объему
                        usdt_tickers = [t for t in tickers if t["symbol"].endswith("USDT")]
                        usdt_tickers.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
                        targets = [t["symbol"] for t in usdt_tickers[:self.top_n]]
                    else:
                        targets = self.base_pairs[:self.top_n]
        except Exception as e:
            logger.error(f"Ticker fetch error: {e}")
            targets = self.base_pairs[:self.top_n]

        # 2. Параллельно проверяем каждую монету
        tasks = [self._check_symbol(sym) for sym in targets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, dict) and res.get("signals"):
                opportunities.append(res)

        return opportunities

    async def _check_symbol(self, symbol: str) -> dict:
        """Проверить одну монету на аномалии."""
        signals = []

        # Данные
        rsi = await self._get_rsi(symbol)
        vol_spike = await self._check_volume_spike(symbol)
        funding = await self._get_funding_rate(symbol)

        # Логика. Convention в остальном коде (data_sources.py, scorer.py):
        # RSI ≤ 30 = oversold = 🟢 «Перепродан» (потенциал отскока, bull bias).
        # RSI ≥ 70 = overbought = 🔴 «Перекуплен» (потенциал коррекции, bear bias).
        if rsi:
            if rsi < 30:
                signals.append(f"🟢 RSI Перепродан ({rsi:.1f})")
            elif rsi > 70:
                signals.append(f"🔴 RSI Перекуплен ({rsi:.1f})")

        if vol_spike:
            signals.append(f"🔥 Объем x{vol_spike:.1f}")

        if funding:
            if funding > 0.002: # > 0.2% за 8ч — очень много
                signals.append(f"⚠️ Funding перегрет ({funding*100:.3f}%)")
            elif funding < -0.002:
                signals.append(f"❄️ Funding негативный ({funding*100:.3f}%)")

        if signals:
            return {
                "symbol": symbol.replace("USDT", ""),
                "signals": signals,
                "rsi": rsi,
                "funding": funding,
                "vol_spike": vol_spike,
            }
        return {"symbol": symbol.replace("USDT", ""), "signal": None}

    async def _get_rsi(self, symbol: str, timeframe: str = "4h") -> Optional[float]:
        try:
            candles = await get_candles(symbol.replace("USDT", ""), 4, limit=20) # 4h ~ 4 часа
            if len(candles) < 15:
                return None
            closes = [c.close for c in candles]
            return self._calc_rsi(closes)
        except Exception:
            return None

    async def _check_volume_spike(self, symbol: str) -> Optional[float]:
        try:
            # Сравниваем объем последней свечи с средней за 20 свечей
            candles = await get_candles(symbol.replace("USDT", ""), 1, limit=21) # 1h
            if len(candles) < 21:
                return None
            current_vol = candles[-1].volume
            avg_vol = sum(c.volume for c in candles[:-1]) / 20
            if avg_vol > 0 and current_vol > avg_vol * 2:
                return current_vol / avg_vol
        except Exception:
            return None
        return None

    async def _get_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{BINANCE_FUTURES}/fapi/v1/premiumIndex", params={"symbol": symbol}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get("lastFundingRate", 0))
        except Exception:
            return None
        return None

    @staticmethod
    def _calc_rsi(closes: List[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i-1]
            gains.append(max(0, delta))
            losses.append(max(0, -delta))

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0: return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
