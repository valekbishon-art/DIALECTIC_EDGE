"""core/regime_radar.py — радар режима рынка по ВОЛАТИЛЬНОСТИ.

Бэктест доказал: НАПРАВЛЕНИЕ цены непредсказуемо (IC≈0), а ВОЛАТИЛЬНОСТЬ —
предсказуема (IC +0.1..0.4 каждый год, vol clustering). Поэтому радар говорит
НЕ «куда пойдёт цена» (это ложь), а «спокойно / трясёт / перегрев» → и что
с этим делать (какой размер carry безопасен, лезть ли в риск).

Live: Binance spot klines (без ключей). Без внешних зависимостей.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.request

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0"}


def _try(url: str, parse, timeout: int = 15, post: dict | None = None):
    data = json.dumps(post).encode() if post is not None else None
    hdr = dict(UA)
    if post is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr)
    return parse(json.loads(urllib.request.urlopen(req, timeout=timeout).read()))


def _hyperliquid_closes(base: str, limit: int) -> list[float]:
    """Дневные close с Hyperliquid (POST candleSnapshot). Достижим с Railway."""
    import time
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (limit + 5) * 86400 * 1000
    body = {"type": "candleSnapshot",
            "req": {"coin": base, "interval": "1d", "startTime": start_ms, "endTime": now_ms}}
    return _try("https://api.hyperliquid.xyz/info",
                lambda d: [float(c["c"]) for c in d], post=body)


def _daily_closes(symbol: str = "BTCUSDT", limit: int = 400) -> list[float]:
    """Дневные close BTC. Фолбэк-цепочка по убыванию доступности с Railway, где
    Binance часто 451 (cloud IP геоблок). Gate/Hyperliquid доказанно достижимы
    (на них же работает кросс-арб), поэтому они в цепочке — иначе режим «N/A»."""
    base = symbol.replace("USDT", "").replace("USD", "") or "BTC"
    gate_pair = f"{base}_USDT"
    sources = [
        # Binance первым (если регион не геоблокнут — самые точные данные)
        (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1d&limit={limit}",
         lambda d: [float(r[4]) for r in d]),
        (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={limit}",
         lambda d: [float(r[4]) for r in d]),
        # Gate — реальный фолбэк для Railway (close = индекс 2, порядок по возрастанию)
        (f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={gate_pair}&interval=1d&limit={min(limit,1000)}",
         lambda d: [float(r[2]) for r in d]),
        (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit={min(limit,1000)}",
         lambda d: [float(r[4]) for r in reversed(d.get("result", {}).get("list", []))]),
    ]
    for url, parse in sources:
        try:
            closes = _try(url, parse)
            if closes and len(closes) >= 60:
                return closes
        except Exception as e:  # noqa: BLE001
            logger.debug("regime closes source failed (%s): %s", url[:40], e)
    # последний фолбэк — Hyperliquid (POST, тоже достижим с Railway)
    try:
        closes = _hyperliquid_closes(base, limit)
        if closes and len(closes) >= 60:
            return closes
    except Exception as e:  # noqa: BLE001
        logger.debug("regime closes hyperliquid failed: %s", e)
    return []


def _rv(rets: list[float], annualize: bool = True) -> float:
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return sd * math.sqrt(365) * 100 if annualize else sd


def regime_now(symbol: str = "BTCUSDT") -> dict:
    """Текущий режим волатильности + готовый текст «что делать».

    Возвращает dict: label, emoji, rv30 (%год), pct (перцентиль 0-1),
    rising (bool), carry_size (1.0/0.7/0.5 — доля размера carry), action (текст).
    {} если данных нет.
    """
    try:
        closes = _daily_closes(symbol)
    except Exception as e:  # noqa: BLE001
        logger.warning("regime: closes fetch failed: %s", e)
        return {}
    if len(closes) < 60:
        return {}
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0]
    rv30 = _rv(rets[-30:])
    rv7 = _rv(rets[-7:])
    # перцентиль текущей RV30 в истории (скользящие 30-дн окна)
    hist = [_rv(rets[i - 30:i]) for i in range(30, len(rets))]
    pct = (sum(1 for x in hist if x <= rv30) / len(hist)) if hist else 0.5
    rising = rv7 > rv30 * 1.15

    if pct < 0.33:
        label, emoji, size = "СПОКОЙНЫЙ рынок", "🟢", 1.0
        action = ("Волатильность низкая и предсказуемо останется низкой. "
                  "Безопасно держать carry на ПОЛНЫЙ размер. Резких движений не ждём.")
    elif pct < 0.66:
        label, emoji, size = "ОБЫЧНЫЙ рынок", "🟡", 0.7
        action = ("Волатильность средняя. Carry держим, но размер ~70% — оставь запас "
                  "по марже на случай рывка.")
    else:
        label, emoji, size = "ВЫСОКАЯ волатильность", "🔴", 0.5
        action = ("Сильно трясёт. Carry — половина размера или жди (риск ликвидации "
                  "шорт-ноги выше). В новые шорты листингов НЕ лезь.")
    if rising:
        action += " ⚠️ Волатильность РАСТЁТ — будь осторожнее обычного."

    return {"label": label, "emoji": emoji, "rv30": round(rv30, 1),
            "rv7": round(rv7, 1), "pct": round(pct, 2), "rising": rising,
            "carry_size": size, "action": action}


def format_regime_md(r: dict) -> str:
    """Готовый Telegram-блок про режим (HTML parse_mode)."""
    if not r:
        return "📊 <b>Режим рынка:</b> данные недоступны."
    arrow = " ↑растёт" if r["rising"] else ""
    # Явно указываем ОКНО: 30д — основная метрика режима, 7д — краткосрочная
    # (часто заметно ниже в штиле). Без окна число вводит в заблуждение.
    rv7 = r.get("rv7")
    short = f" · 7д: {rv7}%" if rv7 is not None else ""
    return (f"📊 <b>РЕЖИМ РЫНКА: {r['emoji']} {r['label']}</b>\n"
            f"Реализ. волатильность BTC (годовых): 30д: {r['rv30']}%{short}\n"
            f"30д выше {int(r['pct']*100)}% дней истории{arrow}\n"
            f"➡️ {r['action']}")


__all__ = ["regime_now", "format_regime_md"]
