"""
signals.py — Сигналы на основе данных Binance/Bybit и вердиктов Dialectic Edge.

Логика:
1. Получаем данные Bybit (позиции трейдеров) если есть API ключ
2. Иначе используем публичный Binance API
3. Читаем вердикт из DIGEST_CACHE
4. Анализируем и генерируем сигналы
5. Отправляем подписчикам через scheduler
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
import aiohttp
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# API URLs
BINANCE_FUTURES_URL = "https://fapi.binance.com"
BINANCE_SPOT_URL = "https://api.binance.com"
BYBIT_URL = "https://api.bybit.com"
COINGECKO_URL = "https://api.coingecko.com/api/v3"

# GitHub URLs
GITHUB_REPO = os.getenv("GITHUB_REPO", "ANAEHY/dialectic_edge")
DIGEST_CACHE_URL = "https://raw.githubusercontent.com/{repo}/main/DIGEST_CACHE.md"

# CoinGecko ID для криптовалют
COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "BNBUSDT": "binancecoin",
    "SOLUSDT": "solana",
}
# CoinGecko ID для UI /markets и автотрейдера (BTC/ETH/SOL/BNB)
DEFAULT_FUTURES_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

# Пороги для сигналов
PRICE_CHANGE_THRESHOLD = 2.0
FUNDING_THRESHOLD = 0.0001
TOP_TRADERS_THRESHOLD = 60  # 60%+ трейдеров в одну сторону


def get_bybit_keys() -> tuple:
    """Получает API ключи Bybit из переменных окружения."""
    api_key = os.getenv("BYBIT_API_KEY", "")
    secret = os.getenv("BYBIT_SECRET_KEY", "")
    return api_key, secret


async def fetch_bybit_long_short_ratio(symbols: list[str] = ["BTCUSDT", "ETHUSDT"]) -> dict:
    """Получает данные позиций трейдеров с Bybit API."""
    api_key, secret = get_bybit_keys()
    results = {}

    if not api_key or not secret:
        logger.info("Bybit API ключи не найдены, используем Binance")
        return {}

    for symbol in symbols:
        try:
            # Bybit V5 API для account ratio
            endpoint = "/v5/market/account-ratio"
            params = {
                "category": "linear",
                "symbol": symbol,
                "interval": "15min",  # 15 минут
                "limit": 1
            }

            # Генерируем подпись
            timestamp = str(int(time.time() * 1000))
            query_string = "&".join([f"{k}={v}" for k, v in params.items()])
            sign = hmac.new(
                secret.encode(),
                f"{timestamp}{api_key}{query_string}".encode(),
                hashlib.sha256
            ).hexdigest()

            headers = {
                "X-BAPI-API-KEY": api_key,
                "X-BAPI-SIGN": sign,
                "X-BAPI-SIGN-TYPE": "HmacSHA256",
                "X-BAPI-TIMESTAMP": timestamp,
            }

            async with aiohttp.ClientSession() as session:
                url = f"{BYBIT_URL}{endpoint}"
                async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                            item = data["result"]["list"][0]
                            long_ratio = float(item.get("longAccount", 0)) * 100
                            short_ratio = float(item.get("shortAccount", 0)) * 100
                            results[symbol] = {
                                "long": round(long_ratio, 1),
                                "short": round(short_ratio, 1),
                                "dominant": "LONG" if long_ratio > short_ratio else "SHORT"
                            }
                            logger.info(f"Bybit data for {symbol}: long={long_ratio}%, short={short_ratio}%")
                    else:
                        logger.warning(f"Bybit API error: {resp.status}")

        except Exception as e:
            logger.warning(f"Bybit fetch error for {symbol}: {e}")

    return results


async def _fetch_daily_closes(session: aiohttp.ClientSession, symbol: str, limit: int = 210) -> list[float]:
    """Лёгкий fetch только списка daily закрытий через Binance klines API.

    Используется для квант-фильтра (BB+Donchian+RSI ансамбль + BTC regime gate).
    Зеркалирует `web_search._fetch_trend_data`, но без вычислений MA/тренда —
    нам тут нужен ТОЛЬКО ряд closes для `quant_filter.quant_verdict()`. Возвращает
    пустой список при ошибке/таймауте, чтобы вызывающая сторона графciously
    деградировала до старого поведения (один MA50/200-триггер).
    """
    try:
        url = f"{BINANCE_SPOT_URL}/api/v3/klines"
        params = {"symbol": symbol, "interval": "1d", "limit": limit}
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=8)
        ) as r:
            if r.status != 200:
                return []
            klines = await r.json()
            if not klines:
                return []
            return [float(k[4]) for k in klines]
    except Exception as e:
        logger.debug(f"daily closes fetch {symbol}: {e}")
        return []


async def fetch_binance_signals(symbols: list[str] | None = None) -> dict:
    """Получает данные: Bybit (если есть ключи) + Binance (fallback)."""
    if symbols is None:
        symbols = list(DEFAULT_FUTURES_SYMBOLS)
    results = {}

    # Сначала пробуем Bybit (позиции трейдеров)
    bybit_data = await fetch_bybit_long_short_ratio(symbols)

    # Пробуем Binance Futures, если не получится — Spot API
    async with aiohttp.ClientSession() as session:
        for symbol in symbols:
            try:
                # Пробуем Futures API
                ticker_url = f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr"
                async with session.get(ticker_url, params={"symbol": symbol}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        ticker = await resp.json()
                        price_change = float(ticker.get("priceChangePercent", 0))
                        results[symbol] = {
                            "price_change": round(price_change, 2),
                            "volume": float(ticker.get("quoteVolume", 0)),
                            "last_price": float(ticker.get("lastPrice", 0)),
                        }
                    else:
                        raise ValueError(f"Futures API returned {resp.status}")

                # Funding rate
                funding_url = f"{BINANCE_FUTURES_URL}/fapi/v1/fundingRate"
                async with session.get(funding_url, params={"symbol": symbol, "limit": 1}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            funding = float(data[0].get("fundingRate", 0))
                            results[symbol]["funding_rate"] = funding
                            results[symbol]["funding_direction"] = "LONG" if funding > 0 else "SHORT"

            except Exception:
                # Fallback: пробуем Spot API
                try:
                    spot_url = f"{BINANCE_SPOT_URL}/api/v3/ticker/24hr"
                    async with session.get(spot_url, params={"symbol": symbol}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            ticker = await resp.json()
                            price_change = float(ticker.get("priceChangePercent", 0))
                            results[symbol] = {
                                "price_change": round(price_change, 2),
                                "volume": float(ticker.get("quoteVolume", 0)),
                                "last_price": float(ticker.get("lastPrice", 0)),
                                "funding_rate": 0,
                                "funding_direction": "NEUTRAL",
                            }
                        else:
                            raise ValueError(f"Spot API returned {resp.status}")
                except Exception as e:
                    logger.warning(f"Binance fallback error for {symbol}: {e}")
                    continue

            # Если есть Bybit данные - мержим
            if symbol in bybit_data:
                results[symbol]["long"] = bybit_data[symbol]["long"]
                results[symbol]["short"] = bybit_data[symbol]["short"]
                results[symbol]["dominant"] = bybit_data[symbol]["dominant"]
                results[symbol]["has_traders_data"] = True

        # ── Квант-фильтр: BB+Donchian+RSI ансамбль + BTC regime gate ──
        # Тащим 210 daily closes для каждого символа (Binance klines), кормим
        # quant_filter.quant_verdict() и кладём результат в каждую запись.
        # Бэктест: 65.9% hit-rate на 1-5д vs 49.6% MA50/200 (см. docs/quant_research_v2.md).
        # Это дополнительный сигнал поверх Bybit/funding — build_signal_bias_map
        # читает quant_verdict из dict'а и может задемоутить direction до
        # NEUTRAL при сильном конфликте.
        try:
            from quant_filter import quant_verdict as _quant_verdict

            closes_map: dict[str, list[float]] = {}
            tasks = [
                _fetch_daily_closes(session, sym)
                for sym in results.keys()
            ]
            closes_results = await asyncio.gather(*tasks, return_exceptions=True)
            for sym, cl in zip(list(results.keys()), closes_results):
                if isinstance(cl, list) and cl:
                    closes_map[sym] = cl

            btc_closes = closes_map.get("BTCUSDT")
            for sym, sym_data in results.items():
                own_closes = closes_map.get(sym)
                if not own_closes:
                    continue
                try:
                    qv = _quant_verdict(
                        own_closes,
                        btc_closes if sym != "BTCUSDT" else None,
                    )
                except Exception as e:
                    logger.debug(f"quant_verdict {sym}: {e}")
                    continue
                sym_data["quant_verdict"] = qv.get("verdict", "NEUTRAL")
                sym_data["quant_confidence"] = qv.get("confidence", 0)
                sym_data["quant_reason"] = qv.get("reason", "")
                sym_data["quant_components"] = qv.get("components", {})
                sym_data["quant_status"] = qv.get("status", "ok")
        except ImportError:
            logger.debug("quant_filter module not available; skipping")
        except Exception as e:
            logger.warning(f"quant_filter signals pass error: {e}")

    return results


async def fetch_verdict(github_repo: str) -> Optional[dict]:
    """Читает последний вердикт из DIGEST_CACHE."""
    url = DIGEST_CACHE_URL.format(repo=github_repo)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                content = await resp.text()
    except Exception as e:
        logger.warning(f"DIGEST_CACHE fetch error: {e}")
        return None

    # Ищем вердикт
    verdict = None
    for line in content.split('\n'):
        line_upper = line.upper()
        if "ВЕРДИКТ" in line_upper or "VERDICT" in line_upper:
            if "БЫЧ" in line_upper or "BULL" in line_upper:
                verdict = "BULLISH"
            elif "МЕДВЕЖ" in line_upper or "BEAR" in line_upper:
                verdict = "BEARISH"
            elif "NEUTRAL" in line_upper or "CASH" in line_upper:
                verdict = "NEUTRAL"
            break

    return {"verdict": verdict, "content": content[:500]}


def analyze_signals(binance_data: dict, verdict: Optional[dict]) -> list:
    """Анализирует данные и генерирует сигналы."""
    signals = []

    for symbol, data in binance_data.items():
        price_change = data.get("price_change", 0)
        funding = data.get("funding_rate", 0)

        # Сигнал 1: Bybit позиции трейдеров (приоритет!)
        long_pct = data.get("long", 0)
        short_pct = data.get("short", 0)

        if long_pct >= TOP_TRADERS_THRESHOLD:
            signals.append({
                "type": "BYBIT_TRADERS",
                "symbol": symbol,
                "direction": "LONG",
                "confidence": long_pct,
                "reason": f"{long_pct}% трейдеров в лонге"
            })
        elif short_pct >= TOP_TRADERS_THRESHOLD:
            signals.append({
                "type": "BYBIT_TRADERS",
                "symbol": symbol,
                "direction": "SHORT",
                "confidence": short_pct,
                "reason": f"{short_pct}% трейдеров в шорте"
            })

        # Сигнал 2: Сильное изменение цены (fallback если нет Bybit)
        elif abs(price_change) >= PRICE_CHANGE_THRESHOLD:
            direction = "LONG" if price_change > 0 else "SHORT"
            confidence = min(abs(price_change) * 10, 95)
            signals.append({
                "type": "PRICE_MOVE",
                "symbol": symbol,
                "direction": direction,
                "confidence": round(confidence),
                "reason": f"{price_change:+.2f}% за 24ч"
            })

        # Сигнал 3: Funding rate
        if abs(funding) >= FUNDING_THRESHOLD:
            direction = "LONG" if funding > 0 else "SHORT"
            confidence = min(abs(funding) * 100000, 80)
            signals.append({
                "type": "FUNDING",
                "symbol": symbol,
                "direction": direction,
                "confidence": round(confidence),
                "reason": f"Funding: {funding*100:.4f}%"
            })

        # Сигнал 3: Совпадение с вердиктом
        if verdict and verdict.get("verdict"):
            v = verdict["verdict"]

            if v == "BULLISH" and price_change > 1:
                signals.append({
                    "type": "VERDICT_MATCH",
                    "symbol": symbol,
                    "direction": "LONG",
                    "confidence": 75,
                    "reason": "Наш вердикт: БЫЧИЙ + рост"
                })
            elif v == "BEARISH" and price_change < -1:
                signals.append({
                    "type": "VERDICT_MATCH",
                    "symbol": symbol,
                    "direction": "SHORT",
                    "confidence": 75,
                    "reason": "Наш вердикт: МЕДВЕЖИЙ + падение"
                })

    return signals


def pick_top_signals(
    signals: list,
    binance_data: dict,
    verdict: Optional[dict] = None,
    *,
    top_n: int = 3,
    min_score: float = 50.0,
) -> list[dict]:
    """Скорит все сигналы по R-системе и возвращает топ-N по r_score.

    Как `pick_best_signal`, но вернёт list лучших сигналов (отсорт. по
    r_score ↓, отфильтрованы по порогу `min_score`). Используется
    рендером `build_signals_message` чтобы показать юзеру блок «⋆ ТОП-N
    СДЕЛОК СЕЙЧАС» (по запросу юзера: «пусть лучшая сделка обрабатывает
    больше 1 позиции»).

    Скоринг идентичен `pick_best_signal` — тот же base+bonuses-penalties
    алгоритм, тот же bias_map вычисляется одним вызовом, порог 50.
    Backward-compat: `pick_best_signal()` делегирует сюда и возвращает
    первый элемент list'а (или None).

    Returns:
        list из до `top_n` сигнал-dict'ов с доп. полями r_score,
        r_ratio, bias_alignment, quant_confirmed, bias_score. Сортировка
        по r_score ↓, только элементы с r_score >= min_score.
    """
    if not signals:
        return []

    bias_map = build_signal_bias_map(binance_data or {}, verdict)

    def _short_symbol(sym: str) -> str:
        return sym.replace("USDT", "").upper()

    scored: list[dict] = []

    for sig in signals:
        symbol_short = _short_symbol(sig.get("symbol", ""))
        direction = sig.get("direction", "NEUTRAL")
        confidence = float(sig.get("confidence", 0) or 0)

        bias = bias_map.get(symbol_short) or {}
        bias_direction = bias.get("direction", "NEUTRAL")
        bias_score_val = float(bias.get("score", 0) or 0)
        quant_verdict_v = (bias.get("quant_verdict") or "NEUTRAL").upper()
        quant_blocked = bool(bias.get("quant_blocked"))

        r_score = confidence

        # Alignment bonuses — direction matches independently computed bias map.
        bias_alignment = direction != "NEUTRAL" and direction == bias_direction
        if bias_alignment:
            r_score += 12

        # Quant verdict confirmation: BB+Donchian+RSI ensemble agrees.
        quant_confirmed = (
            quant_verdict_v in ("LONG", "SHORT") and quant_verdict_v == direction
        )
        if quant_confirmed:
            r_score += 8

        if sig.get("type") == "VERDICT_MATCH":
            r_score += 6

        if sig.get("type") == "BYBIT_TRADERS" and confidence >= 70:
            r_score += 4

        # Penalties: quant safety gate already blocked direction.
        if quant_blocked:
            r_score -= 35

        # Direction conflicts with the bias-map sign at meaningful magnitude.
        if abs(bias_score_val) >= 8:
            sign = 1 if bias_score_val > 0 else -1
            if (direction == "LONG" and sign < 0) or (direction == "SHORT" and sign > 0):
                r_score -= 18

        # R/R hint: ATR availability bonus.
        has_atr = bool(
            (binance_data or {}).get(sig.get("symbol", ""), {})
            .get("quant_components")
        )
        if has_atr:
            r_score += 3
        r_ratio = 2.0  # σ̂-based default; conservative 2:1

        enriched = dict(sig)
        enriched["r_score"] = round(r_score, 1)
        enriched["r_ratio"] = r_ratio
        enriched["bias_alignment"] = bias_alignment
        enriched["quant_confirmed"] = quant_confirmed
        enriched["bias_score"] = round(bias_score_val, 1)
        scored.append(enriched)

    # Фильтр + сортировка. min_score=50 — порог качества, ниже не рекомендуем.
    qualified = [s for s in scored if s["r_score"] >= min_score]
    qualified.sort(key=lambda s: s["r_score"], reverse=True)
    return qualified[: max(0, int(top_n))]


def pick_best_signal(
    signals: list,
    binance_data: dict,
    verdict: Optional[dict] = None,
) -> Optional[dict]:
    """Выбирает один лучший сигнал среди кандидатов по R-системе рисков.

    R-система = риск/доход: для каждого кандидата считаем композитный
    R-score, где база — confidence из `analyze_signals`, плюс бонусы за
    выравнивание с другими сигналами (трейдеры Bybit, quant_verdict,
    вердикт `/daily`) и штрафы за конфликты. Используем уже посчитанный
    `build_signal_bias_map`, чтобы не дублировать логику взвешивания
    funding/traders/quant.

    Алгоритм:
      • Считаем bias_map один раз — это per-symbol композитный score
        (см. `build_signal_bias_map`).
      • Для каждого сигнала:
          base = float(confidence)
          + 12 если direction совпадает с bias_map[symbol].direction
          + 8 если quant_verdict == direction (доп. подтверждение)
          + 6 если type == "VERDICT_MATCH" (alignment с /daily)
          + 4 если type == "BYBIT_TRADERS" и confidence ≥ 70
          − 35 если bias_map[symbol].quant_blocked (quant-блок)
          − 18 если direction конфликтует с знаком bias_score (≥8 по модулю)
      • Implicit R:R = 2:1 (SL = 1.5·σ, TP = 3·σ — как в core/signal_scorer).
        Если есть quant_components → ATR, ещё +3 (есть данные для стопа).
      • Возвращаем dict с расширенными полями ⭐ (или None, если пусто).

    Returns:
        Лучший signal dict с доп. полями `r_score`, `r_ratio`,
        `bias_alignment`, `quant_confirmed`. None если кандидатов нет.
    """
    # Делегируем на pick_top_signals(top_n=1) чтобы не дублировать логику.
    top = pick_top_signals(signals, binance_data, verdict, top_n=1, min_score=50.0)
    return top[0] if top else None


def build_signal_bias_map(binance_data: dict, verdict: Optional[dict] = None) -> dict:
    """Collapse raw signal inputs into a per-symbol directional bias map."""
    bias_map = {}

    for raw_symbol, data in (binance_data or {}).items():
        symbol = raw_symbol.replace("USDT", "").upper()
        score = 0.0
        reasons = []

        long_pct = float(data.get("long", 0) or 0)
        short_pct = float(data.get("short", 0) or 0)
        if long_pct or short_pct:
            traders_edge = long_pct - short_pct
            if abs(traders_edge) >= 5:
                score += traders_edge
                dominant = "LONG" if traders_edge > 0 else "SHORT"
                reasons.append(f"traders {dominant} {abs(traders_edge):.1f}%")

        price_change = float(data.get("price_change", 0) or 0)
        if abs(price_change) >= 0.75:
            move_score = min(abs(price_change) * 6, 20)
            score += move_score if price_change > 0 else -move_score
            reasons.append(f"24h {price_change:+.2f}%")

        funding = float(data.get("funding_rate", 0) or 0)
        if abs(funding) >= FUNDING_THRESHOLD:
            funding_score = min(abs(funding) * 200000, 12)
            score += funding_score if funding > 0 else -funding_score
            reasons.append(f"funding {funding * 100:+.4f}%")

        if verdict and verdict.get("verdict"):
            verdict_name = verdict["verdict"]
            if verdict_name == "BULLISH":
                score += 8
                reasons.append("digest bullish")
            elif verdict_name == "BEARISH":
                score -= 8
                reasons.append("digest bearish")

        # ── Квант-фильтр: BB+Donchian+RSI ансамбль + BTC regime gate ──
        # На бэктесте даёт +16п.п. hit-rate (49.6% → 65.9%) на 1-5д
        # горизонте — см. docs/quant_research_v2.md. Логика:
        #   • LONG quant + score>=0 → boost +6 (двойное подтверждение)
        #   • SHORT quant + score<=0 → boost −6
        #   • Сильный конфликт (quant LONG vs score<=−8, или quant SHORT vs
        #     score>=+8) → штраф −10 в абсолюте, чтобы вытолкнуть в NEUTRAL.
        # Не падаем если поля отсутствуют (graceful-degradation).
        quant_v = (data.get("quant_verdict") or "").upper()
        quant_conf = float(data.get("quant_confidence") or 0)
        if quant_v in ("LONG", "SHORT") and quant_conf >= 50:
            quant_dir_score = 6.0 if quant_v == "LONG" else -6.0
            if (score >= 0 and quant_v == "LONG") or (score <= 0 and quant_v == "SHORT"):
                score += quant_dir_score
                reasons.append(f"quant {quant_v} ({quant_conf:.0f}%)")
            elif (score >= 8 and quant_v == "SHORT") or (score <= -8 and quant_v == "LONG"):
                # Конфликт с уже сильным сигналом → демоутим, чтобы не
                # ловить ножи против mean-reversion.
                penalty = 10.0 if score > 0 else -10.0
                score -= penalty
                reasons.append(f"quant CONFLICT {quant_v} (−)")

        direction = "NEUTRAL"
        if score >= 8:
            direction = "LONG"
        elif score <= -8:
            direction = "SHORT"

        # Финальный safety-гейт: если у quant сильный анти-сигнал,
        # перебрасываем direction на NEUTRAL (но score не трогаем — пусть
        # внешние слои видят сырой счёт). Применяем только если quant
        # действительно сработал (confidence ≥ 70).
        quant_blocked = False
        if quant_v in ("LONG", "SHORT") and quant_conf >= 70:
            if direction == "LONG" and quant_v == "SHORT":
                direction = "NEUTRAL"
                quant_blocked = True
                reasons.append(f"quant ⛔ блок LONG")
            elif direction == "SHORT" and quant_v == "LONG":
                direction = "NEUTRAL"
                quant_blocked = True
                reasons.append(f"quant ⛔ блок SHORT")

        bias_map[symbol] = {
            "symbol": symbol,
            "score": round(score, 2),
            "direction": direction,
            "strength": round(min(abs(score), 100), 1),
            "reasons": reasons,
            "price_change": price_change,
            "funding_rate": funding,
            "last_price": float(data.get("last_price", 0) or 0),
            "long": long_pct,
            "short": short_pct,
            "quant_verdict": quant_v or "NEUTRAL",
            "quant_confidence": quant_conf,
            "quant_reason": data.get("quant_reason", ""),
            "quant_blocked": quant_blocked,
        }

    return bias_map


def build_signals_message(signals: list, binance_data: dict, verdict: Optional[dict]) -> str:
    """Формирует красивое сообщение с сигналами."""
    lines = [
        "📡 *MARKET SIGNALS*",
        f"_{datetime.now().strftime('%d.%m %H:%M UTC')}_",
        "",
    ]

    # Данные рынка (Bybit если есть, иначе Binance)
    has_bybit = any(data.get("has_traders_data") for data in binance_data.values()) if binance_data else False
    source = "Bybit" if has_bybit else "Binance"
    lines.append(f"📊 *ТРЕЙДЕРЫ ({source})*")

    if not binance_data:
        lines.append("Ситуация неопределена")
    else:
        for symbol, data in binance_data.items():
            name = symbol.replace("USDT", "")
            price_change = data.get("price_change", 0)
            funding = data.get("funding_rate", 0)
            long_pct = data.get("long", 0)
            short_pct = data.get("short", 0)

            # Если есть данные Bybit трейдеров
            if long_pct > 0 or short_pct > 0:
                dominant = "🟢" if long_pct > short_pct else "🔴"
                lines.append(f"{name}:")
                lines.append(f"  🔼 Лонг: {long_pct}%")
                lines.append(f"  🔽 Шорт: {short_pct}%")
                lines.append(f"  {dominant} Доминирование")
            else:
                # Fallback на цену
                emoji = "🟢" if price_change > 0 else "🔴" if price_change < 0 else "⚪️"
                change_str = f"{emoji} {price_change:+.2f}%"
                funding_str = f"Funding: {'🔼' if funding > 0 else '🔽'}{funding*100:.4f}%"

                lines.append(f"{name}:")
                lines.append(f"  {change_str}")
                lines.append(f"  {funding_str}")
            lines.append("")

    # Вердикт
    if verdict and verdict.get("verdict"):
        v = verdict["verdict"]
        emoji = "🐂" if v == "BULLISH" else "🐻" if v == "BEARISH" else "⚪️"
        lines.append(f"{emoji} *НАШ ВЕРДИКТ*")
        lines.append(v)
    else:
        lines.append("🎯 *НАШ ВЕРДИКТ*")
        lines.append("Ситуация неопределена")

    lines.append("")

    # Сигналы
    if signals:
        # ── Лучшие сделки (R-система рисков) ─────────────────────────────────
        # Скорим каждый сигнал по composite R-score и показываем топ-3 ⭐
        # перед списком. Если score < 50 — отсекаем (порог качества).
        # Раньше показывали только №1, теперь юзер видит несколько
        # подтверждённых сетапов («лучшая сделка обрабатывает больше 1
        # позиции» — запрос юзера).
        top_signals = pick_top_signals(signals, binance_data, verdict, top_n=3)
        best_keys: set[tuple] = set()
        if top_signals:
            best_keys = {
                (s.get("symbol"), s.get("direction"), s.get("type"))
                for s in top_signals
            }

            # Header: «ЛУЧШАЯ СДЕЛКА СЕЙЧАС» (если одна) или «ТОП-N СДЕЛОК
            # СЕЙЧАС» (если 2+). Это даёт юзеру понятный сигнал «сколько
            # подтверждённых сетапов нашли».
            if len(top_signals) == 1:
                lines.append("⭐ *ЛУЧШАЯ СДЕЛКА СЕЙЧАС*")
            else:
                lines.append(f"⭐ *ТОП-{len(top_signals)} СДЕЛОК СЕЙЧАС*")

            for idx, sig in enumerate(top_signals, start=1):
                if idx <= 1:
                    medal = "🥇"
                elif idx == 2:
                    medal = "🥈"
                elif idx == 3:
                    medal = "🥉"
                else:
                    medal = "🔸"
                arrow = "📈" if sig.get("direction") == "LONG" else "📉"
                sym_short = sig.get("symbol", "").replace("USDT", "")

                confirm_bits: list[str] = []
                if sig.get("bias_alignment"):
                    confirm_bits.append("bias")
                if sig.get("quant_confirmed"):
                    confirm_bits.append("quant")
                if sig.get("type") == "VERDICT_MATCH":
                    confirm_bits.append("digest")
                if sig.get("type") == "BYBIT_TRADERS":
                    confirm_bits.append("traders")
                confirm_str = ", ".join(confirm_bits) if confirm_bits else "single"

                # Только головной сетап получает «#1» префикс — для
                # остальных medal сам по себе указывает место.
                rank_prefix = "" if len(top_signals) == 1 else f"#{idx} "
                lines.append(
                    f"{medal} {rank_prefix}{arrow} *{sym_short}* → "
                    f"*{sig.get('direction')}*  "
                    f"R/R≈{sig.get('r_ratio', 2.0):.1f}  "
                    f"score {sig.get('r_score', 0):.0f}/100"
                )
                lines.append(f"   {sig.get('reason', '')}")
                lines.append(f"   _Подтверждения: {confirm_str}_")
            lines.append("")

        lines.append("🔔 *СИГНАЛЫ*")

        verdict_value = (verdict or {}).get("verdict") if verdict else None
        if verdict_value not in ("BULLISH", "BEARISH"):
            tag = "NEUTRAL" if verdict_value == "NEUTRAL" else "не определён"
            lines.append("")
            lines.append(
                f"⚠️ _Вердикт `/daily` — {tag}. Сигналы ниже — это инфа для "
                f"наблюдения, не приглашение войти. Решение за тобой._"
            )
            lines.append("")

        for s in signals:
            emoji = "🟢" if s["direction"] == "LONG" else "🔴"
            conf = s["confidence"]
            conf_emoji = "✅" if conf >= 70 else "⚠️"

            # Помечаем ★ сигналы, которые R-система отметила как топ-3.
            # Это даёт юзеру визуальную привязку: «вот эти из списка ниже
            # — те же что в блоке ТОП-N СДЕЛОК выше».
            key = (s.get("symbol"), s.get("direction"), s.get("type"))
            star = " ⭐" if key in best_keys else ""

            lines.append(
                f"{emoji} {s['symbol']} → {s['direction']} {conf_emoji}{conf}%{star}"
            )
            lines.append(f"   {s['reason']}")
            lines.append("")
    else:
        lines.append("⚪️ *СИГНАЛЫ*")
        lines.append("Ситуация неопределена")

    lines.extend([
        "",
        "⚠️ _Это информация, не финансовый совет._",
        "_DYOR._"
    ])

    return "\n".join(lines)


async def fetch_markets_bundle(github_repo: str | None = None) -> dict:
    """Один набор данных: Binance/Bybit + вердикт из DIGEST_CACHE + текст сигналов.

    Используется в /markets, рассылке подписчикам и автотрейдере (тот же контур, что у UI).

    Provenance: после вычисления best-signal через R-систему мы замораживаем
    решение в decision_provenance (см. core/provenance.py). Это асинхронная,
    fire-and-forget запись — если БД недоступна, фоновое сообщение не падает.
    """
    repo = github_repo or os.getenv("GITHUB_REPO", "ANAEHY/dialectic_edge")
    binance_data = await fetch_binance_signals()
    verdict = await fetch_verdict(repo)
    sigs = analyze_signals(binance_data, verdict)
    msg = build_signals_message(sigs, binance_data, verdict)

    # ── Provenance: замораживаем best-signal (если есть) для последующего replay ──
    # Мы повторно вызываем pick_best_signal/build_signal_bias_map потому что
    # build_signals_message их инкапсулирует. Стоимость минимальна (чистые
    # функции, ~0.1мс), а профит — полный snapshot input'ов + R-score
    # компонентов на момент решения. Без exception bubble: если provenance
    # сломается, рассылка сигналов не должна падать.
    try:
        bias_map = build_signal_bias_map(binance_data, verdict)
        best = pick_best_signal(sigs, binance_data, verdict)
        from core.provenance import freeze_pick_best_decision  # local import: avoid CI smoke
        await freeze_pick_best_decision(best, sigs, binance_data, verdict, bias_map)
    except Exception as exc:
        logger.warning(f"provenance freeze (pick_best) skipped: {exc}")

    return {
        "binance_data": binance_data,
        "verdict": verdict,
        "signals": sigs,
        "signals_message": msg,
        "github_repo": repo,
    }


# ─── Минималистичный /markets с выбором секции ────────────────────────────────
# Раньше /markets возвращал ~5к символов мульти-сообщения (живой контекст +
# smart-money + сигналы). Юзер: «нажимаю по 5 раз и листать неудобно». Идея:
# первый экран /markets теперь — короткая сводка (крипта + сигналы), а
# дальше — кнопки выбора секции (Крипта / Макро / Индексы / Сырьё / COT /
# ETF / Сигналы / Всё). Реализация переиспользует уже отлажённые функции из
# `web_search` (per-section minimal renderer) и `signals.fetch_markets_bundle`
# (binance + verdict + signals_message).

MARKETS_SECTIONS: tuple[str, ...] = (
    "summary",  # default: крипта (minimal) + сигналы
    "crypto",
    "macro",
    "indices",
    "commod",
    "cot",
    "etf",
    "signals",
    "all",
)


def _markets_header(section: str) -> str:
    titles = {
        "summary": "📊 *Рынки — сводка*",
        "crypto": "💲 *Рынки — крипта*",
        "macro": "🌐 *Рынки — макро*",
        "indices": "📈 *Рынки — индексы*",
        "commod": "⛽ *Рынки — сырьё*",
        "cot": "📊 *Рынки — COT*",
        "etf": "💼 *Рынки — ETF потоки*",
        "signals": "📡 *Рынки — сигналы*",
        "all": "🏛 *Рынки — всё*",
    }
    now = datetime.now().strftime("%d.%m %H:%M")
    return f"{titles.get(section, '📊 *Рынки*')} — _{now}_"


async def build_markets_section_message(
    github_repo: str | None = None,
    *,
    section: str = "summary",
) -> tuple[list[str], dict]:
    """Per-секционный /markets для нового inline-меню выбора секции.

    Возвращает (`messages`, `bundle`) где `messages` — список Telegram-чанков
    (для совместимости с `cmd_markets`), `bundle` — словарь с raw данными
    (`binance_data`, `signals`, `verdict`, `prices`, `cot`, `etf`, …) для
    дальнейшего использования вызывающим кодом.

    section:
      • ``summary`` (default) — короткий экран: крипта (minimal) + сигналы
      • ``crypto`` / ``macro`` / ``indices`` / ``commod`` — одна секция цен
      • ``cot``     — COT данные (CFTC)
      • ``etf``     — ETF flows + market breadth
      • ``signals`` — best trade + market signals (как `build_signals_message`)
      • ``all``     — всё разом (как старый /markets)
    """
    section = section if section in MARKETS_SECTIONS else "summary"
    header = _markets_header(section)
    bundle: dict = {"section": section, "github_repo": github_repo}

    # Если секция касается сигналов или summary — нужны binance_data + verdict.
    needs_signals = section in ("summary", "signals", "all")
    # Если секция касается цен (крипта/макро/индексы/сырьё/all) — нужен prices.
    needs_prices = section in ("summary", "crypto", "macro", "indices", "commod", "all")

    # Параллельный fetch — тащим только то, что нужно для секции.
    # `format_prices_section` отдаёт ПОЛНЫЙ рич-формат (24ч/7д/30д, MA-триггеры,
    # SL/TP LONG/SHORT, Quant-вердикт, ТРЕНД+MA50/200, Random walk/Markov,
    # объём) — фильтрует только по выбранной секции. Юзер просил вернуть
    # детальный формат, оставив выбор секции.
    from web_search import fetch_realtime_prices, format_prices_section

    tasks: dict[str, asyncio.Task] = {}
    if needs_signals:
        tasks["bundle"] = asyncio.create_task(fetch_markets_bundle(github_repo))
    if needs_prices:
        tasks["prices"] = asyncio.create_task(fetch_realtime_prices())
    if section in ("cot", "all"):
        async def _fetch_cot():
            try:
                from cot_data import format_cot_for_agents, get_cot_for_assets

                cot_data = await get_cot_for_assets(["Bitcoin", "Gold", "Crude Oil"])
                return cot_data, format_cot_for_agents(cot_data)
            except Exception as e:
                logger.warning(f"COT fetch error: {e}")
                return {}, "COT данные временно недоступны."
        tasks["cot"] = asyncio.create_task(_fetch_cot())
    if section in ("etf", "all"):
        async def _fetch_etf():
            try:
                from etf_flows import (
                    format_etf_flows_for_agents,
                    get_etf_flows,
                    get_market_breadth,
                )

                flows = await get_etf_flows()
                breadth = await get_market_breadth()
                return flows, format_etf_flows_for_agents(flows), breadth
            except Exception as e:
                logger.warning(f"ETF fetch error: {e}")
                return {}, "ETF данные временно недоступны.", {}
        tasks["etf"] = asyncio.create_task(_fetch_etf())

    results: dict = {}
    if tasks:
        done = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for key, value in zip(tasks.keys(), done):
            if isinstance(value, Exception):
                logger.warning(f"build_markets_section_message[{key}]: {value}")
                results[key] = None
            else:
                results[key] = value

    md_bundle = results.get("bundle") or {}
    prices = results.get("prices") or {}
    cot_pair = results.get("cot") or ({}, "")
    etf_triple = results.get("etf") or ({}, "", {})

    bundle.update(md_bundle)
    bundle["prices"] = prices
    bundle["cot"] = cot_pair[0] if isinstance(cot_pair, tuple) else {}
    bundle["etf_flows"] = etf_triple[0] if isinstance(etf_triple, tuple) else {}
    bundle["market_breadth"] = etf_triple[2] if isinstance(etf_triple, tuple) and len(etf_triple) > 2 else {}

    # ── Рендер тела секции ───────────────────────────────────────────────
    body_parts: list[str] = []

    if section == "summary":
        # Сводка = крипта с S/R. Сигналы убраны — они живут в отдельной
        # вкладке «📡 Сигналы» (section="signals") и в /signal. Дублировать
        # их в summary перегружало текст и пушило за 4096 → клавиатура
        # уезжала на второе сообщение.
        body_parts.append(format_prices_section(prices, section="crypto") or "Нет данных")

    elif section == "crypto":
        body_parts.append(format_prices_section(prices, section="crypto") or "Нет данных")

    elif section == "macro":
        body_parts.append(format_prices_section(prices, section="macro") or "Нет данных")

    elif section == "indices":
        body_parts.append(format_prices_section(prices, section="indices") or "Нет данных")

    elif section == "commod":
        body_parts.append(format_prices_section(prices, section="commod") or "Нет данных")

    elif section == "cot":
        body_parts.append(cot_pair[1] if isinstance(cot_pair, tuple) else "")

    elif section == "etf":
        body_parts.append(etf_triple[1] if isinstance(etf_triple, tuple) else "")
        # Market breadth — короткой строкой
        breadth = bundle.get("market_breadth") or {}
        if breadth.get("breadth"):
            body_parts.append("")
            body_parts.append(
                f"_Breadth: {breadth['breadth']}_  "
                f"SPY {breadth.get('spy_5d', 0):+.2f}%  "
                f"QQQ {breadth.get('qqq_5d', 0):+.2f}%  "
                f"IWM {breadth.get('iwm_5d', 0):+.2f}%"
            )

    elif section == "signals":
        body_parts.append(md_bundle.get("signals_message", "Нет данных"))

    elif section == "all":
        # Все секции по очереди — но в минимальном стиле, чтобы было удобно
        # листать. COT/ETF — отдельным блоком.
        from web_search import format_prices_minimal
        body_parts.append(format_prices_minimal(prices, section="all", include_title=True))
        body_parts.append("")
        if isinstance(cot_pair, tuple) and cot_pair[1]:
            body_parts.append("📊 *COT*")
            body_parts.append(cot_pair[1])
            body_parts.append("")
        if isinstance(etf_triple, tuple) and etf_triple[1]:
            body_parts.append("💼 *ETF потоки*")
            body_parts.append(etf_triple[1])
            body_parts.append("")
        body_parts.append("📡 *Сигналы*")
        body_parts.append(md_bundle.get("signals_message", "Нет данных"))

    text = header + "\n\n" + "\n".join(p for p in body_parts if p is not None).rstrip()

    # Режем по тем же границам что и full /markets — секции и активы.
    messages = _split_markets_message(text, max_len=4000)
    return messages, bundle


async def build_markets_panel_message(github_repo: str | None = None) -> tuple[list[str], dict]:
    """Текст для команды /markets: живой контекст + smart-money + сигналы.

    Раньше возвращал одну строку и резал хвост по `max_len=3900` — из-за чего
    блок «🏛 SMART-MONEY» обрезался посередине, когда live-контекст распухал
    (5 крипто + макро + индексы + сырьё + COT + ETF flows = ~3.5k символов).
    Теперь возвращаем **список сообщений**: первое — живой контекст, второе —
    smart-money + сигналы. Caller отправляет их последовательно, клавиатура
    цепляется к последнему.
    """
    from web_search import get_full_realtime_context
    try:
        from market_indicators.smart_money import (
            fetch_smart_money_signals,
            format_smart_money_compact,
        )
        _smart_money_available = True
    except Exception:
        _smart_money_available = False

    # Тянем bundle, live-контекст и smart-money параллельно — суммарно ~1-2с.
    # `for_user=True` — компактный формат для /markets: без дубль-заголовка,
    # без AI-инструкций, с пустыми строками между активами.
    if _smart_money_available:
        bundle, live_result, smart_money = await asyncio.gather(
            fetch_markets_bundle(github_repo),
            get_full_realtime_context(for_user=True),
            fetch_smart_money_signals(),
            return_exceptions=False,
        )
        sm_block: Optional[str] = None
        try:
            sm_block = format_smart_money_compact(smart_money)
        except Exception as e:
            logger.warning(f"format_smart_money_compact error: {e}")
    else:
        bundle, live_result = await asyncio.gather(
            fetch_markets_bundle(github_repo),
            get_full_realtime_context(for_user=True),
            return_exceptions=False,
        )
        sm_block = None

    _, live_formatted = live_result
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    separator = "━━━━━━━━━━━━━━━━━━━━━━━━━"

    # ── Сообщение 1: заголовок + живой контекст ───────────────────────────
    msg_live_header = "\n".join([
        f"📊 *РЫНКИ И СИГНАЛЫ* — _{now}_",
        "",
        separator,
        "🌍 *Живой контекст*",
        separator,
        "",
    ])
    msg_live = msg_live_header + live_formatted

    # ── Сообщение 2: smart-money + сигналы ────────────────────────────────
    extra_parts: list[str] = []
    if sm_block:
        extra_parts.extend([separator, sm_block, ""])
    extra_parts.extend([separator, bundle["signals_message"]])
    msg_extra = "\n".join(extra_parts)

    # Telegram cap = 4096; если live-контекст распух — режем по границе
    # секций (`[КРИПТОРЫНОК]` / `[МАКРО...]` / `[ФОНДОВЫЕ...]` / `[СЫРЬЁ...]`),
    # а не в середине актива. Раньше резалось страшным "…часть текста скрыта"
    # и юзер не видел нефть/индексы целиком — теперь все секции попадают.
    messages: list[str] = []
    for chunk in (msg_live, msg_extra):
        for part in _split_markets_message(chunk, max_len=4000):
            messages.append(part)
    return messages, bundle


def _split_markets_message(text: str, *, max_len: int = 4000) -> list[str]:
    """Режет /markets-сообщение по границам секций, чтобы не обрезать
    в середине актива.

    Стратегия:
      1. Если text ≤ max_len — возвращаем как есть.
      2. Иначе ищем разделители (`\n\n[`) — это начало новой секции
         (`[КРИПТОРЫНОК]`, `[МАКРОЭКОНОМИКА США]`, `[ФОНДОВЫЕ ИНДЕКСЫ]`,
         `[СЫРЬЁ И ВАЛЮТЫ]`). Накапливаем секции в текущий chunk пока
         он ≤ max_len, иначе flush.
      3. Если одна секция сама > max_len — режем по asset-границам
         (`\n  ` — двойной пробел перед label = новый актив).
      4. Fallback: hard cut по max_len (этого почти не должно случаться).
    """
    if len(text) <= max_len:
        return [text]

    # Шаг 1: разбиваем на секции
    sections = text.split("\n\n[")
    # Первая часть не имеет "[" префикса; остальным возвращаем
    parts: list[str] = [sections[0]] + [f"[{s}" for s in sections[1:]]

    chunks: list[str] = []
    cur = ""
    for s in parts:
        candidate = (cur + "\n\n" + s) if cur else s
        if len(candidate) <= max_len:
            cur = candidate
            continue
        # cur слишком большой — flush, а текущую секцию пытаемся положить отдельно
        if cur:
            chunks.append(cur)
        if len(s) <= max_len:
            cur = s
        else:
            # Секция сама больше max_len — режем по asset-границам
            chunks.extend(_split_by_assets(s, max_len=max_len))
            cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def _split_by_assets(section: str, *, max_len: int = 4000) -> list[str]:
    """Режет секцию (`[КРИПТОРЫНОК]\n  Bitcoin (BTC): ...`) по границам
    активов. Каждый актив начинается со строки `\n  ` (2 пробела).
    """
    lines = section.split("\n")
    chunks: list[str] = []
    cur_lines: list[str] = []
    for line in lines:
        candidate = "\n".join(cur_lines + [line])
        if len(candidate) <= max_len:
            cur_lines.append(line)
            continue
        # flush
        if cur_lines:
            chunks.append("\n".join(cur_lines))
        cur_lines = [line]
    if cur_lines:
        chunks.append("\n".join(cur_lines))
    return chunks


class SignalsSystem:
    def __init__(self, bot, github_repo: str):
        self.bot = bot
        self.github_repo = github_repo
        self._last_signal_time: Optional[datetime] = None

    async def check_and_send_signals(self, subscribers: list[dict]) -> int:
        """Проверяет сигналы и отправляет подписчикам."""
        if not subscribers:
            return 0

        bundle = await fetch_markets_bundle(self.github_repo)
        binance_data = bundle["binance_data"]

        if not binance_data:
            logger.warning("No Binance data received")
            return 0

        message = bundle["signals_message"]

        # Отправляем
        sent = 0
        for user in subscribers:
            try:
                await self.bot.send_message(user["user_id"], message, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"Signal send error user {user['user_id']}: {e}")

        self._last_signal_time = datetime.now()
        logger.info(f"✅ Signals sent: {sent}")
        return sent
