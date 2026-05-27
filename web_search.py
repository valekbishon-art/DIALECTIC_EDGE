"""
web_search.py — Реалтайм данные + реальный веб-поиск через Tavily.

Источники:
  BTC/ETH/SOL   → Binance + CoinGecko fallback
  S&P 500        → Yahoo ^GSPC (индекс, не ETF SPY)
  Nasdaq 100     → Yahoo ^NDX  (индекс, не ETF QQQ)
  VIX            → Yahoo ^VIX
  DXY            → Yahoo DX-Y.NYB
  Нефть WTI      → Yahoo CL=F + fallback BNO
  Золото         → Yahoo GC=F + XAUUSD=X fallback
  Макро          → FRED API (CPI пересчитывается в YoY %)
  Веб-новости    → Tavily API (реальный поиск, не DDG-пустышка)
  COT           → CFTC.gov (фьючерсное позиционирование)
  ETF Flows     → Yahoo (институциональные потоки)
"""

import asyncio
import logging
import os
import aiohttp
from datetime import datetime
from config import FRED_API_KEY

logger = logging.getLogger(__name__)

TIMEOUT       = aiohttp.ClientTimeout(total=15)
HEADERS       = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ─── Константы ────────────────────────────────────────────────────────────────
CPI_BASE_YEAR_AGO    = 319.8   # CPI март 2025 по BLS → YoY ≈ 2.4%
FED_INFLATION_TARGET = 2.0

PRICE_SANITY = {
    "BTC":     (20_000,  250_000),
    "ETH":     (500,     30_000),
    "SOL":     (10,      3_000),
    "BNB":     (100,     5_000),
    "XRP":     (0.2,     50),
    # Расширение крипто-листа /markets и /signal до 15 активов (top по
    # mcap, доступны на Binance Spot и Bybit Spot). Юзер: «расширить
    # крипту в маркетс до 15-20 чтобы листать можно было». Диапазоны
    # умышленно широкие, чтобы не выкидывать актив на pump/dump.
    "ADA":     (0.05,    50),
    "DOGE":    (0.005,   5),
    "AVAX":    (5,       2_000),
    "LINK":    (3,       500),
    "DOT":     (1,       500),
    "TRX":     (0.01,    5),
    "TON":     (0.5,     100),
    "LTC":     (20,      5_000),
    "NEAR":    (1,       500),
    "SUI":     (0.1,     100),
    "SPX":     (4_000,   12_000),
    "NDX":     (10_000,  35_000),
    "VIX":     (5,       90),
    "DXY":     (80,      130),
    "OIL_WTI": (30,      200),
    "GOLD":    (2_000,   8_000),
}

# Расширенный список крипто-активов (Binance Spot symbol → ключ в prices dict).
# Используется как fetch_realtime_prices, так и postprocessing'ом. Юзер
# просил расширение до 10-15+, мы кладём 15.
EXTENDED_CRYPTO_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("BTCUSDT",  "BTC"),
    ("ETHUSDT",  "ETH"),
    ("SOLUSDT",  "SOL"),
    ("BNBUSDT",  "BNB"),
    ("XRPUSDT",  "XRP"),
    ("ADAUSDT",  "ADA"),
    ("DOGEUSDT", "DOGE"),
    ("AVAXUSDT", "AVAX"),
    ("LINKUSDT", "LINK"),
    ("DOTUSDT",  "DOT"),
    ("TRXUSDT",  "TRX"),
    ("TONUSDT",  "TON"),
    ("LTCUSDT",  "LTC"),
    ("NEARUSDT", "NEAR"),
    ("SUIUSDT",  "SUI"),
)

CRYPTO_KEYS: tuple[str, ...] = tuple(k for _, k in EXTENDED_CRYPTO_SYMBOLS)

# CoinGecko ID-fallback для расширенного списка.
CRYPTO_COINGECKO_IDS: dict[str, str] = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "BNB":  "binancecoin",
    "XRP":  "ripple",
    "ADA":  "cardano",
    "DOGE": "dogecoin",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "DOT":  "polkadot",
    "TRX":  "tron",
    "TON":  "the-open-network",
    "LTC":  "litecoin",
    "NEAR": "near",
    "SUI":  "sui",
}

# Yahoo Finance fallback для MA50/MA200, когда Binance klines падает
# (часто блокируется в РФ или rate-limit).
CRYPTO_YAHOO_TICKERS: dict[str, str] = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "BNB":  "BNB-USD",
    "XRP":  "XRP-USD",
    "ADA":  "ADA-USD",
    "DOGE": "DOGE-USD",
    "AVAX": "AVAX-USD",
    "LINK": "LINK-USD",
    "DOT":  "DOT-USD",
    "TRX":  "TRX-USD",
    "TON":  "TON11419-USD",
    "LTC":  "LTC-USD",
    "NEAR": "NEAR-USD",
    "SUI":  "SUI20947-USD",
}

# Лейблы (Asset → human-readable name) для UI-рендера.
CRYPTO_LABELS: dict[str, str] = {
    "BTC":  "Bitcoin",
    "ETH":  "Ethereum",
    "SOL":  "Solana",
    "BNB":  "BNB",
    "XRP":  "XRP",
    "ADA":  "Cardano",
    "DOGE": "Dogecoin",
    "AVAX": "Avalanche",
    "LINK": "Chainlink",
    "DOT":  "Polkadot",
    "TRX":  "Tron",
    "TON":  "Toncoin",
    "LTC":  "Litecoin",
    "NEAR": "Near",
    "SUI":  "Sui",
}

def _sane(key: str, price: float) -> bool:
    if not price or price <= 0:
        return False
    lo, hi = PRICE_SANITY.get(key, (0, 999_999_999))
    ok = lo <= price <= hi
    if not ok:
        logger.warning(f"[sanity] {key}: {price:.2f} вне [{lo}, {hi}]")
    return ok


def _fmt_change(ch: float | int | None, *, decimals: int = 2) -> str:
    """Эмодзи + знак для процентного изменения. `0.00%` → ⚪ (нет данных/флэт),
    а не зелёный `🟢 +0.00%` — тот выглядит как маленький рост, но почти
    всегда означает что Yahoo вернул `previousClose=None` или `change_24h`
    не посчитался. Порог 0.005% — ниже округление до двух знаков покажет
    `0.00%` в любом случае.
    """
    try:
        v = float(ch) if ch is not None else 0.0
    except (TypeError, ValueError):
        v = 0.0
    if abs(v) < 0.005:
        return f"⚪ {v:+.{decimals}f}%"
    emoji = "🟢" if v > 0 else "🔴"
    return f"{emoji} {v:+.{decimals}f}%"


def _fmt_money(value: float | int | None, *, prefix: str = "") -> str:
    """Адаптивная точность для денежных значений.

    Большой бан XRP-precision: при ${value:,.0f} XRP MA200 = $1.65 рендерится
    как `$2`, MA50 = $1.30 → `$1`. Все per-asset планы агенту/юзеру приходят
    с обрезанными до целого триггерами, и Synth дословно пишет «$2 (MA200)».

    Точность подбираем по абсолютной величине:
      |v| >= 1000   → 0 знаков ($82,308)
      |v| >= 100    → 0 знаков ($769)
      |v| >= 10     → 2 знака  ($94.87, $18.35)
      |v| >= 1      → 2 знака  ($1.65, $1.30)
      |v| <  1      → 4 знака  ($0.0123)
    Хвостовые нули у <10 не режем — `$1.30` информативнее, чем `$1.3`.
    """
    if value is None:
        return "N/A"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    abs_v = abs(v)
    if abs_v < 1:
        body = f"{v:,.4f}"
    elif abs_v < 100:
        body = f"{v:,.2f}"
    else:
        body = f"{v:,.0f}"
    return f"{prefix}{body}"


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    r"""RSI(14) по Wilder-smoothing. Возвращает None при недостатке данных.

    Используется для дайджеста (PNG-карточки): раньше chart_generator
    регекспом тащил RSI прямо из текста LLM (`r"RSI[^\d]*BTC[^\d]*(\d+\.?\d*)"`),
    и брал любое число после слов RSI/BTC. Так RSI прыгал с 14 на 60 за
    1.5ч — это была не реальная метрика, а первая попавшаяся цифра из
    свободного текста модели. Считаем сами по дневным закрытиям.
    """
    if not closes or len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


# ─── Tavily — реальный веб-поиск ──────────────────────────────────────────────

async def search_tavily(query: str, max_results: int = 3) -> str:
    """
    Реальный поиск через Tavily API.
    Возвращает сниппеты новостей/аналитики для агентов.
    """
    if not TAVILY_API_KEY:
        return ""
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key":      TAVILY_API_KEY,
            "query":        query,
            "max_results":  max_results,
            "search_depth": "basic",
            "include_answer": True,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    parts = []
                    # Если есть прямой ответ — берём его
                    if data.get("answer"):
                        parts.append(f"[Ответ]: {data['answer'][:300]}")
                    # Плюс топ-3 результата
                    for res in data.get("results", [])[:max_results]:
                        title   = res.get("title", "")[:80]
                        content = res.get("content", "")[:200]
                        url_s   = res.get("url", "")
                        parts.append(f"• {title}\n  {content}\n  ({url_s})")
                    return "\n\n".join(parts)
    except Exception as e:
        logger.debug(f"Tavily '{query}': {e}")
    return ""


async def get_news_context(topics: list[str]) -> str:
    """
    Собирает свежие новости по списку тем через Tavily.
    Используется в run_full_analysis для обогащения контекста агентов.
    """
    if not TAVILY_API_KEY:
        return "⚠️ Tavily API не настроен — веб-поиск недоступен."

    queries = [
        "Fed interest rates inflation latest news today",
        "geopolitical risk oil markets today",
        "Bitcoin crypto market news today",
        "S&P 500 stock market outlook today",
    ]
    # Добавляем пользовательские темы
    for t in topics:
        if t:
            queries.append(f"{t} financial market impact today")

    tasks   = [search_tavily(q, max_results=2) for q in queries[:6]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sections = []
    for q, res in zip(queries, results):
        if isinstance(res, str) and res.strip():
            sections.append(f"=== {q} ===\n{res}")

    if not sections:
        return "Свежих новостей не найдено."

    return "=== АКТУАЛЬНЫЕ НОВОСТИ (Tavily) ===\n\n" + "\n\n".join(sections)


async def search_news_context(topic: str) -> str:
    """Для /analyze — поиск по конкретной теме."""
    result = await search_tavily(f"{topic} market financial impact analysis", max_results=4)
    return result if result else "Свежих новостей по теме не найдено."


# ─── Binance ──────────────────────────────────────────────────────────────────

async def _binance(session, symbol: str, key: str) -> dict | None:
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        async with session.get(url, timeout=TIMEOUT) as r:
            if r.status == 200:
                d      = await r.json()
                price  = float(d["lastPrice"])
                change = float(d["priceChangePercent"])
                volume = float(d.get("quoteVolume", 0))  # объём в USD
                if _sane(key, price):
                    return {
                        "price": price,
                        "change_24h": round(change, 3),
                        "volume_24h_usd": round(volume / 1_000_000, 1),  # в млн $
                        "source": "Binance"
                    }
    except Exception as e:
        logger.debug(f"Binance {symbol}: {e}")
    return None


async def _coingecko_crypto(session) -> dict:
    out = {}
    try:
        url    = "https://api.coingecko.com/api/v3/simple/price"
        ids_csv = ",".join(CRYPTO_COINGECKO_IDS.values())
        params = {"ids": ids_csv,
                  "vs_currencies": "usd",
                  "include_24hr_change": "true"}
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            if r.status == 200:
                data = await r.json()
                for key, cg_id in CRYPTO_COINGECKO_IDS.items():
                    if cg_id in data:
                        p = float(data[cg_id].get("usd", 0))
                        c = float(data[cg_id].get("usd_24h_change", 0))
                        if _sane(key, p):
                            out[key] = {"price": p, "change_24h": round(c, 3),
                                        "source": "CoinGecko"}
    except Exception as e:
        logger.debug(f"CoinGecko crypto: {e}")
    return out




# ─── Тренд: 7d/30d изменение + MA50 + структура ──────────────────────────────


def _compute_complexity_fields(closes) -> dict:
    """Полный пакет «торгуемости» для дневного ряда закрытий.

    Считает (всё опционально, отсутствующие поля просто не попадут в dict):

    1) **Hurst + Shannon entropy** (core.market_complexity.analyze_complexity)
       — поля `hurst`, `entropy_normalized`, `tradeable_score`,
       `complexity_hint`.
    2) **Variance Ratio Test (Lo–MacKinlay)** и
       **Permutation Entropy (Bandt–Pompe)** — `vrt_ratio`, `vrt_zstat`,
       `vrt_pvalue`, `vrt_random_walk`, `perm_entropy`. Дешёвый sanity-check
       на random-walk + улучшение Shannon entropy на коротких рядах.
    3) **Markov regime (3-state)** (core.markov_regime) — поля
       `markov_state`, `markov_next_probs`, `markov_dwell_bars`.
       Дискретная цепь Маркова на квантилях returns, даёт next-bar probs
       и expected dwell.
    4) **EWMA volatility forecast** (core.volatility_forecast) — поля
       `vol_sigma_1d_pct`, `vol_sigma_annual_pct`, `vol_realized_1d_pct`.
       Forward-looking σ_{t+1} вместо backward-looking ATR.

    Returns {} при импорт-фейле всего пакета. Если отдельный компонент
    возвращает None (короткая выборка / вырожденный ряд) — соответствующие
    поля просто отсутствуют и `_complexity_line` их пропустит.

    Импорты ленивые: web_search.py подгружается на старте бота, мы не хотим
    делать core.* хардом-зависимостью для bootstrap.
    """
    out: dict = {}

    # Hurst + Shannon entropy + интегральный complexity_hint
    try:
        from core.market_complexity import analyze_complexity
        c = analyze_complexity(closes)
        if c is not None:
            out.update({
                "hurst": round(c.hurst, 3) if c.hurst is not None else None,
                "entropy_normalized": (
                    round(c.entropy_normalized, 3)
                    if c.entropy_normalized is not None
                    else None
                ),
                "tradeable_score": round(c.tradeable_score, 3),
                "complexity_hint": c.regime_hint,
            })
    except Exception as e:
        logger.debug(f"analyze_complexity failed: {e}")

    # Returns переиспользуем для VRT / PE / Markov / EWMA — пересчитываем
    # один раз (дешёво, ~200 вычитаний для крипты).
    try:
        from core.market_complexity import compute_returns
        returns = compute_returns(closes)
    except Exception as e:
        logger.debug(f"compute_returns failed: {e}")
        returns = []

    if returns:
        # VRT(k=2) — k=2 даёт максимальную чувствительность к
        # day-to-day автокорреляции. Параметры дальше можно тюнить.
        try:
            from core.market_complexity import variance_ratio_test
            vrt = variance_ratio_test(returns, k=2)
            if vrt is not None:
                out["vrt_ratio"] = round(vrt.vr, 3)
                out["vrt_zstat"] = round(vrt.z_stat, 2)
                out["vrt_pvalue"] = round(vrt.p_value, 4)
                out["vrt_random_walk"] = vrt.random_walk
        except Exception as e:
            logger.debug(f"variance_ratio_test failed: {e}")

        # Permutation entropy (order=3) — устойчивее Shannon на коротких рядах.
        try:
            from core.market_complexity import permutation_entropy
            pe = permutation_entropy(returns, order=3)
            if pe is not None:
                out["perm_entropy"] = round(pe, 3)
        except Exception as e:
            logger.debug(f"permutation_entropy failed: {e}")

        # Markov 3-state regime на returns
        try:
            from core.markov_regime import analyze_markov_regime
            mk = analyze_markov_regime(returns)
            if mk is not None:
                out["markov_state"] = mk.current_state
                out["markov_next_probs"] = {
                    k: round(v, 3) for k, v in mk.next_step_probs.items()
                }
                # Конечный dwell нам интересен только для текущего состояния,
                # иначе агентам слишком много чисел.
                d = mk.expected_dwell_bars.get(mk.current_state)
                if d is not None and d != float("inf"):
                    out["markov_dwell_bars"] = round(d, 1)
        except Exception as e:
            logger.debug(f"analyze_markov_regime failed: {e}")

        # EWMA volatility forecast (RiskMetrics λ=0.94, 365-day annualization)
        try:
            from core.volatility_forecast import forecast_volatility_ewma
            vf = forecast_volatility_ewma(returns)
            if vf is not None:
                out["vol_sigma_1d_pct"] = round(vf.sigma_1d_pct, 2)
                out["vol_sigma_annual_pct"] = round(vf.sigma_annualized_pct, 1)
                out["vol_realized_1d_pct"] = round(vf.realized_1d_pct, 2)
        except Exception as e:
            logger.debug(f"forecast_volatility_ewma failed: {e}")

    return out


async def _fetch_trend_data(session, symbol_binance: str, key: str) -> dict:
    """
    Получает расширенные данные тренда через Binance klines API (бесплатно):
    - Изменение за 7 дней и 30 дней
    - MA50 (скользящая средняя 50 дней)
    - MA200 (скользящая средняя 200 дней)
    - Метка тренда: UPTREND / DOWNTREND / SIDEWAYS
    - Структура: HH/HL (Higher High/Higher Low) или LH/LL
    """
    result = {}
    try:
        # 200 дневных свечей достаточно для MA50 и MA200
        url = f"https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol_binance, "interval": "1d", "limit": 200}
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            if r.status != 200:
                return result
            klines = await r.json()
            if not klines or len(klines) < 50:
                return result

        closes = [float(k[4]) for k in klines]  # индекс 4 = close price
        current = closes[-1]

        # Изменение за 7 и 30 дней
        if len(closes) >= 8:
            price_7d_ago = closes[-8]
            result["change_7d"] = round((current - price_7d_ago) / price_7d_ago * 100, 2)
        if len(closes) >= 31:
            price_30d_ago = closes[-31]
            result["change_30d"] = round((current - price_30d_ago) / price_30d_ago * 100, 2)

        # MA50 и MA200
        if len(closes) >= 50:
            ma50 = sum(closes[-50:]) / 50
            result["ma50"] = round(ma50, 2)
            result["above_ma50"] = current > ma50
        if len(closes) >= 200:
            ma200 = sum(closes[-200:]) / 200
            result["ma200"] = round(ma200, 2)
            result["above_ma200"] = current > ma200

        # Структура тренда — смотрим последние 14 свечей
        highs = [float(k[2]) for k in klines[-14:]]  # индекс 2 = high
        lows  = [float(k[3]) for k in klines[-14:]]  # индекс 3 = low

        # Считаем Higher Highs / Higher Lows
        hh = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
        hl = sum(1 for i in range(1, len(lows))  if lows[i]  > lows[i-1])
        lh = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
        ll = sum(1 for i in range(1, len(lows))  if lows[i]  < lows[i-1])

        # Определяем тренд
        bull_score = hh + hl
        bear_score = lh + ll
        above_ma50  = result.get("above_ma50", True)
        change_7d   = result.get("change_7d", 0)

        if bull_score > bear_score + 3 and above_ma50 and change_7d > 2:
            trend = "UPTREND"
            trend_emoji = "📈"
        elif bear_score > bull_score + 3 and not above_ma50 and change_7d < -2:
            trend = "DOWNTREND"
            trend_emoji = "📉"
        elif abs(change_7d) < 3 and abs(result.get("change_30d", 0)) < 8:
            trend = "SIDEWAYS"
            trend_emoji = "↔️"
        elif change_7d > 0 and above_ma50:
            trend = "UPTREND"
            trend_emoji = "📈"
        elif change_7d < 0 and not above_ma50:
            trend = "DOWNTREND"
            trend_emoji = "📉"
        else:
            trend = "SIDEWAYS"
            trend_emoji = "↔️"

        # ── ATR(14d) — Average True Range для SL-guard (pre-live-hardening) ──
        # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        if len(klines) >= 15:
            true_ranges = []
            for i in range(-14, 0):
                h = float(klines[i][2])
                l = float(klines[i][3])
                prev_c = float(klines[i - 1][4])
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                true_ranges.append(tr)
            atr_14 = sum(true_ranges) / len(true_ranges)
            result["atr_14d"] = round(atr_14, 2)
            result["atr_14d_pct"] = round(atr_14 / current * 100, 2)

        # RSI(14d) — считаем сами по дневным закрытиям. Раньше chart_generator
        # регекспом тащил RSI из текста LLM, отсюда прыжки 14→60 за 1.5ч.
        rsi = _compute_rsi(closes, 14)
        if rsi is not None:
            result["rsi_14d"] = rsi

        result["trend"] = trend
        result["trend_emoji"] = trend_emoji
        result["hh"] = hh
        result["hl"] = hl
        result["lh"] = lh
        result["ll"] = ll

        # Hurst + Shannon entropy. Дешёво (~1 мс на 200 баров) и даёт агентам
        # независимый сигнал, торгуется ли вообще этот ряд (random_walk =
        # сигналы по MA = монетка).
        result.update(_compute_complexity_fields(closes))

        # Сохраняем последние N daily closes для квант-фильтра (quant_filter.py).
        # Используется в `fetch_realtime_prices` для расчёта BB/Donchian/RSI
        # ансамбля с BTC-regime gate. Не отдаём агентам/юзеру — служебное поле
        # с подчёркиванием. ~250 закрытий → ~2KB на актив, безопасно.
        result["_closes_daily"] = closes[-250:]
        # Дополнительно тянем highs/lows для S/R-детекции (core.support_resistance).
        # Свинг-пивоты считаются по wicks, не по closes — иначе пропускаем
        # intraday-разворот.
        result["_highs_daily"] = [float(k[2]) for k in klines][-250:]
        result["_lows_daily"]  = [float(k[3]) for k in klines][-250:]

    except Exception as e:
        logger.debug(f"Trend data {symbol_binance}: {e}")
    return result


# ─── Yahoo Finance ────────────────────────────────────────────────────────────

async def _fetch_yahoo_trend(session, ticker: str, key: str) -> dict:
    """MA50/MA200/ATR(14d) для макро-активов через Yahoo Finance.

    Используется для SPX/NDX/VIX/DXY/OIL/GOLD — у крипты MA-метрики уже
    приходят из Binance через `_fetch_trend_data`. Аналогичная структура
    результата: `ma50`, `ma200`, `above_ma50`, `above_ma200`, `atr_14d`,
    `change_7d`, `change_30d`, `trend`/`trend_emoji`.

    Yahoo `range=1y` → ~250 дневных свечей → MA200 считается корректно.
    """
    result = {}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        async with session.get(url, params={"interval": "1d", "range": "1y"},
                               timeout=TIMEOUT) as r:
            if r.status != 200:
                return result
            data = await r.json()
            chart = data.get("chart") or {}
            results = chart.get("result") or []
            if not results:
                return result
            r0 = results[0]
            ind = r0.get("indicators") or {}
            quotes = (ind.get("quote") or [{}])[0]
            closes_raw = quotes.get("close") or []
            highs_raw = quotes.get("high") or []
            lows_raw = quotes.get("low") or []
            # Yahoo может вернуть None для нерабочих дней
            closes = [c for c in closes_raw if c is not None]
            highs = [h for h in highs_raw if h is not None]
            lows = [l for l in lows_raw if l is not None]
            if len(closes) < 50:
                return result
            current = closes[-1]

            if len(closes) >= 8:
                result["change_7d"] = round((current - closes[-8]) / closes[-8] * 100, 2)
            if len(closes) >= 31:
                result["change_30d"] = round((current - closes[-31]) / closes[-31] * 100, 2)

            if len(closes) >= 50:
                ma50 = sum(closes[-50:]) / 50
                result["ma50"] = round(ma50, 2)
                result["above_ma50"] = current > ma50
            if len(closes) >= 200:
                ma200 = sum(closes[-200:]) / 200
                result["ma200"] = round(ma200, 2)
                result["above_ma200"] = current > ma200

            # ATR(14d) — нужно совпадение длин закрытий с high/low
            if len(closes) >= 15 and len(highs) >= 15 and len(lows) >= 15:
                # Сводим списки к одинаковой длине (берём последние 15)
                n = min(len(closes), len(highs), len(lows))
                if n >= 15:
                    cs = closes_raw[-n:]
                    hs = highs_raw[-n:]
                    ls = lows_raw[-n:]
                    trs = []
                    for i in range(-14, 0):
                        try:
                            h = float(hs[i]); l = float(ls[i]); prev_c = float(cs[i - 1])
                            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
                        except (TypeError, ValueError):
                            continue
                    if trs:
                        atr_14 = sum(trs) / len(trs)
                        result["atr_14d"] = round(atr_14, 4)
                        result["atr_14d_pct"] = round(atr_14 / current * 100, 2)

            # RSI(14d) для макро-активов — те же причины, что и для крипты.
            rsi = _compute_rsi(closes, 14)
            if rsi is not None:
                result["rsi_14d"] = rsi

            # Hurst + энтропия — нужны и для крипты с Yahoo-фоллбэка (когда
            # Binance падает), и для макро. Yahoo `range=1y` гарантирует
            # ~250 баров что выше MIN_BARS_FOR_HURST=64.
            result.update(_compute_complexity_fields(closes))

            # Highs/lows для S/R-детекции (core.support_resistance).
            # Yahoo иногда даёт None в high/low массивах (полу-выходные дни) —
            # фильтруем как для closes выше.
            result["_highs_daily"] = highs[-250:]
            result["_lows_daily"]  = lows[-250:]

            # Простой trend label (как в _fetch_trend_data, но без HH/HL)
            ch_7d = result.get("change_7d", 0) or 0
            ch_30d = result.get("change_30d", 0) or 0
            above_ma50 = result.get("above_ma50", True)
            if ch_7d > 2 and above_ma50:
                result["trend"], result["trend_emoji"] = "UPTREND", "📈"
            elif ch_7d < -2 and not above_ma50:
                result["trend"], result["trend_emoji"] = "DOWNTREND", "📉"
            elif abs(ch_7d) < 3 and abs(ch_30d) < 8:
                result["trend"], result["trend_emoji"] = "SIDEWAYS", "↔️"
            elif ch_7d >= 0:
                result["trend"], result["trend_emoji"] = "UPTREND", "📈"
            else:
                result["trend"], result["trend_emoji"] = "DOWNTREND", "📉"

    except Exception as e:
        logger.debug(f"Yahoo trend {ticker}: {e}")
    return result


async def _yahoo(session, ticker: str, key: str) -> dict | None:
    """Цена + 24ч-изменение через Yahoo Finance v8 chart API.

    Раньше change оставался `0.0` для **фьючерсов и DX-Y.NYB** потому что
    Yahoo для них возвращает `meta.previousClose == None` (фьючерсы CL=F/GC=F
    закрываются на выходных, индекс DX-Y.NYB вообще не имеет «previous close»
    в том же смысле что у акций). Старый код брал `closes[-2]` только если
    оно not None, а иначе падал в ветку с `previousClose` — и если тот None,
    то prev=price, change=0.

    Новая логика устойчивее:
      1. Фильтруем None из массива closes. Если осталось ≥2 валидных свечи —
         используем `valid[-2]` (или `valid[-1]` если `valid[-1] != price`).
      2. Если валидная свеча одна — пробуем `chartPreviousClose` и
         `previousClose` (в таком порядке: `chartPreviousClose` это
         фиксированный «close перед началом диапазона», работает у всех
         тикеров, включая фьючерсы и индексы валют).
      3. Если closes вообще пустой — те же fallback'и.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        async with session.get(url, params={"interval": "1d", "range": "5d"},
                               timeout=TIMEOUT) as r:
            if r.status == 200:
                data   = await r.json()
                result = data["chart"]["result"][0]
                meta   = result["meta"]
                price  = float(meta.get("regularMarketPrice", 0))

                change = 0.0
                quotes = (result.get("indicators") or {}).get("quote") or [{}]
                closes_raw = quotes[0].get("close") or []
                valid = [float(c) for c in closes_raw if c is not None]

                # `interval=1d, range=5d` → 4-5 дневных свечей. Последняя
                # свеча — сегодняшний бар (intraday для открытых рынков,
                # или close если рынок закрыт). Предпоследняя — вчерашний
                # close, нужный нам как референс для 24h change.
                #
                # Раньше код пытался выяснить «last == price» с порогом
                # `1e-6`, но Yahoo отдаёт `closes` с full-precision float
                # (101.47000122) при том что `regularMarketPrice` округлён
                # (101.47). Разница ~1e-6 пограничная, и код падал в ветку
                # `prev_price = valid[-1]` (т.е. = price) → change = 0.
                # Это давало `+0.00%` для CL=F, GC=F, DX-Y.NYB.
                prev_price: float | None = None
                if len(valid) >= 2:
                    prev_price = valid[-2]
                elif valid:
                    # Только один валидный close — пробуем meta-поля.
                    for cand in (meta.get("previousClose"),
                                  meta.get("chartPreviousClose")):
                        if cand is not None:
                            prev_price = float(cand)
                            break
                else:
                    for cand in (meta.get("previousClose"),
                                  meta.get("chartPreviousClose")):
                        if cand is not None:
                            prev_price = float(cand)
                            break

                if prev_price and abs(prev_price - price) > 1e-9:
                    change = (price - prev_price) / prev_price * 100

                if _sane(key, price):
                    return {"price": round(price, 2),
                            "change_24h": round(change, 3),
                            "source": f"Yahoo ({ticker})"}
    except Exception as e:
        logger.debug(f"Yahoo {ticker}: {e}")
    return None


async def _gold(session) -> dict | None:
    """GC=F → XAUUSD=X → предупреждение."""
    for ticker in ["GC=F", "XAUUSD=X"]:
        r = await _yahoo(session, ticker, "GOLD")
        if r:
            logger.info(f"Золото {ticker}: ${r['price']:,.2f}")
            return r
    logger.warning("Золото: все источники недоступны")
    return None


async def _oil(session) -> dict | None:
    """CL=F → BNO → предупреждение."""
    for ticker, key in [("CL=F", "OIL_WTI"), ("BNO", "OIL_WTI")]:
        r = await _yahoo(session, ticker, key)
        if r:
            return r
    return None


# ─── FRED ─────────────────────────────────────────────────────────────────────

async def _fred(session, series_id: str) -> str:
    if not FRED_API_KEY or FRED_API_KEY in ("", "твой_ключ", "YOUR_KEY"):
        return "N/A"
    try:
        url    = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series_id, "api_key": FRED_API_KEY,
                  "file_type": "json", "limit": 1, "sort_order": "desc"}
        async with session.get(url, params=params, timeout=TIMEOUT) as r:
            if r.status == 200:
                data = await r.json()
                val  = data["observations"][0]["value"]
                return val if val != "." else "N/A"
    except Exception as e:
        logger.debug(f"FRED {series_id}: {e}")
    return "N/A"


async def _fear_greed(session) -> dict:
    try:
        async with session.get("https://api.alternative.me/fng/?limit=8",
                               timeout=TIMEOUT) as r:
            if r.status == 200:
                d     = await r.json()
                items = d.get("data", [])
                if items:
                    cur  = items[0]
                    prev = int(items[1]["value"]) if len(items) > 1 else int(cur["value"])
                    val  = int(cur["value"])
                    # 7-дневный тренд F&G (pre-live-hardening)
                    history_7d = [int(it["value"]) for it in items[:7] if it.get("value")]
                    trend_7d = ""
                    if len(history_7d) >= 5:
                        avg_recent = sum(history_7d[:3]) / 3
                        avg_older = sum(history_7d[4:7]) / max(len(history_7d[4:7]), 1)
                        if avg_recent > avg_older + 5:
                            trend_7d = "RISING"
                        elif avg_recent < avg_older - 5:
                            trend_7d = "FALLING"
                        else:
                            trend_7d = "STABLE"
                    return {"val": val, "status": cur["value_classification"],
                            "change": val - prev, "trend_7d": trend_7d,
                            "history_7d": history_7d}
    except Exception as e:
        logger.debug(f"F&G: {e}")
    return {"val": "N/A", "status": "Unknown", "change": 0, "trend_7d": "", "history_7d": []}


# ─── Агрегатор ────────────────────────────────────────────────────────────────

async def fetch_realtime_prices() -> dict:
    prices: dict = {}
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        # ── Tier 0: per-asset crypto tickers + trend (EXTENDED_CRYPTO_SYMBOLS) ──
        # Список расширен с 5 до 15 активов (юзер просил «расширить крипту в
        # маркетс до 15-20»). Запросы идут параллельно, поэтому wall-time не
        # увеличивается значимо (~+200мс на новые tickers vs 5).
        crypto_price_tasks = [
            _binance(session, sym, key) for sym, key in EXTENDED_CRYPTO_SYMBOLS
        ]
        crypto_trend_tasks = [
            _fetch_trend_data(session, sym, key)
            for sym, key in EXTENDED_CRYPTO_SYMBOLS
        ]

        (
            crypto_prices,
            crypto_trends,
            spx, ndx, vix, dxy, oil, gold,
            fed_rate, cpi_raw, fng,
            trend_spx, trend_ndx, trend_vix, trend_dxy, trend_oil, trend_gold,
        ) = await asyncio.gather(
            asyncio.gather(*crypto_price_tasks, return_exceptions=True),
            asyncio.gather(*crypto_trend_tasks, return_exceptions=True),
            _yahoo(session, "^GSPC",    "SPX"),
            _yahoo(session, "^NDX",     "NDX"),
            _yahoo(session, "^VIX",     "VIX"),
            _yahoo(session, "DX-Y.NYB", "DXY"),
            _oil(session),
            _gold(session),
            _fred(session, "FEDFUNDS"),
            _fred(session, "CPIAUCSL"),
            _fear_greed(session),
            _fetch_yahoo_trend(session, "^GSPC",    "SPX"),
            _fetch_yahoo_trend(session, "^NDX",     "NDX"),
            _fetch_yahoo_trend(session, "^VIX",     "VIX"),
            _fetch_yahoo_trend(session, "DX-Y.NYB", "DXY"),
            _fetch_yahoo_trend(session, "CL=F",     "OIL_WTI"),
            _fetch_yahoo_trend(session, "GC=F",     "GOLD"),
            return_exceptions=True,
        )

        # Крипта с fallback. CRYPTO_KEYS — упорядоченный список ключей,
        # zip с crypto_prices/crypto_trends сохраняет соответствие.
        missing: list[str] = []
        for key, val in zip(CRYPTO_KEYS, crypto_prices):
            if val and not isinstance(val, Exception):
                prices[key] = val
            else:
                missing.append(key)
        if missing:
            cg = await _coingecko_crypto(session)
            for k in missing:
                if k in cg:
                    prices[k] = cg[k]

        # Добавляем тренд+MA к крипто данным
        for key, trend_val in zip(CRYPTO_KEYS, crypto_trends):
            if key in prices and trend_val and not isinstance(trend_val, Exception) and trend_val:
                prices[key].update(trend_val)
                # Прокидываем ATR в top-level для SL-guard (market_prices["ATR_BTC"])
                if "atr_14d" in trend_val:
                    prices[f"ATR_{key}"] = trend_val["atr_14d"]
                # Прокидываем MA200/MA50 в top-level — Synth use'ит их в plans
                if "ma200" in trend_val:
                    prices[f"MA200_{key}"] = trend_val["ma200"]
                if "ma50" in trend_val:
                    prices[f"MA50_{key}"] = trend_val["ma50"]

        # ── Fallback на Yahoo для крипты, если Binance API не отдал MA ──
        # Symptom: в Telegram видно "Bitcoin (BTC) ... CoinGecko" без строки
        # "ТРЕНД: ... | MA50 ... | MA200 ...". Причина: Binance klines API
        # упал/таймаут (часто блокируется в РФ или rate-limit), `_fetch_trend_data`
        # тихо вернул {}. CoinGecko даёт цену но не MA. Подстраховка — Yahoo.
        crypto_missing_ma = [
            k for k in CRYPTO_KEYS
            if k in prices and "ma200" not in prices[k] and k in CRYPTO_YAHOO_TICKERS
        ]
        if crypto_missing_ma:
            logger.debug(
                "Crypto MA fallback to Yahoo for: %s", crypto_missing_ma
            )
            yahoo_results = await asyncio.gather(
                *[
                    _fetch_yahoo_trend(session, CRYPTO_YAHOO_TICKERS[k], k)
                    for k in crypto_missing_ma
                ],
                return_exceptions=True,
            )
            for k, yres in zip(crypto_missing_ma, yahoo_results):
                if not yres or isinstance(yres, Exception):
                    continue
                # Мерджим только новые поля — не затираем уже что-то от Binance
                for field, val in yres.items():
                    if field not in prices[k]:
                        prices[k][field] = val
                if "ma200" in yres:
                    prices[f"MA200_{k}"] = yres["ma200"]
                if "ma50" in yres:
                    prices[f"MA50_{k}"] = yres["ma50"]
                if "atr_14d" in yres:
                    prices[f"ATR_{k}"] = yres["atr_14d"]

        # ── Квант-фильтр (deterministic mean-rev + BTC regime gate) ──
        # Берёт сохранённые `_closes_daily` для каждой крипты, считает
        # BB(20,2σ)+Donchian(20)+RSI(14)-ансамбль и гейтит BTC-трендом.
        # На бэктесте 5 крипто × 365 дней даёт 65.9% hit-rate (vs 49.6% MA50/200),
        # walk-forward стабильно ≥60% — см. ``docs/quant_research_v2.md``.
        # Не падаем если модуль/closes недоступны.
        try:
            from quant_filter import quant_verdict as _quant_verdict
            btc_closes = (
                prices.get("BTC", {}).get("_closes_daily")
                if "BTC" in prices
                else None
            )
            for k in CRYPTO_KEYS:
                if k not in prices:
                    continue
                own_closes = prices[k].get("_closes_daily")
                if not own_closes:
                    continue
                try:
                    qv = _quant_verdict(
                        own_closes,
                        btc_closes if k != "BTC" else None,
                    )
                except Exception as e:
                    logger.debug(f"quant_verdict {k}: {e}")
                    continue
                prices[k]["quant_verdict"] = qv.get("verdict", "NEUTRAL")
                prices[k]["quant_confidence"] = qv.get("confidence", 0)
                prices[k]["quant_reason"] = qv.get("reason", "")
                prices[k]["quant_components"] = qv.get("components", {})
                prices[k]["quant_status"] = qv.get("status", "ok")
        except ImportError:
            logger.debug("quant_filter module not available, skipping")
        except Exception as e:
            logger.warning(f"Quant filter pass error: {e}")

        # Макро: prices + MA-тренд
        for key, val in [("SPX", spx), ("NDX", ndx), ("VIX", vix),
                         ("DXY", dxy), ("OIL_WTI", oil), ("GOLD", gold)]:
            if val and not isinstance(val, Exception):
                prices[key] = val
        for key, trend_val in [("SPX", trend_spx), ("NDX", trend_ndx), ("VIX", trend_vix),
                                ("DXY", trend_dxy), ("OIL_WTI", trend_oil), ("GOLD", trend_gold)]:
            if key in prices and trend_val and not isinstance(trend_val, Exception) and trend_val:
                prices[key].update(trend_val)
                if "ma200" in trend_val:
                    prices[f"MA200_{key}"] = trend_val["ma200"]
                if "ma50" in trend_val:
                    prices[f"MA50_{key}"] = trend_val["ma50"]
                if "atr_14d" in trend_val:
                    prices[f"ATR_{key}"] = trend_val["atr_14d"]

        prices["MACRO"] = {
            "fed_rate": fed_rate if not isinstance(fed_rate, Exception) else "N/A",
            "cpi_raw":  cpi_raw  if not isinstance(cpi_raw, Exception)  else "N/A",
            "fng":      fng      if not isinstance(fng, Exception) else
                        {"val": "N/A", "status": "Unknown", "change": 0},
        }

    got     = [k for k in prices if k != "MACRO"]
    missing = [k for k in ["BTC","ETH","SPX","NDX","GOLD","OIL_WTI"] if k not in prices]
    logger.info(f"✅ Цены: {got}")
    if missing:
        logger.warning(f"❌ Не получены: {missing}")
    return prices


# ─── CPI → YoY % ──────────────────────────────────────────────────────────────

def _cpi_yoy(raw: str) -> str:
    try:
        v   = float(raw)
        yoy = (v - CPI_BASE_YEAR_AGO) / CPI_BASE_YEAR_AGO * 100
        gap = yoy - FED_INFLATION_TARGET
        g_s = f"+{gap:.1f}%" if gap > 0 else f"{gap:.1f}%"
        status = ("выше таргета" if gap > 1.0 else
                  "незначительно выше таргета" if gap > 0.3 else
                  "близко к таргету")
        return f"~{yoy:.1f}% YoY — {status} (таргет 2.0%, отклонение {g_s})"
    except (ValueError, TypeError):
        return "нет данных"


# ─── Форматирование для агентов ───────────────────────────────────────────────


_COMPLEXITY_LABELS = {
    "TRENDING":       "trending",
    "MEAN_REVERTING": "mean-reverting",
    "RANDOM_WALK":    "random walk",
    "CHAOTIC":        "chaotic",
    "MIXED":          "mixed",
    "UNKNOWN":        "unknown",
}


_MARKOV_STATE_LABELS = {
    "DOWN": "падение",
    "FLAT": "флэт",
    "UP":   "рост",
}

# Эмодзи-вердикты по `complexity_hint`. Видимый верх-«знак» строки сразу
# даёт пользователю и LLM-агенту понятный signal: персистентный тренд /
# mean-reversion / случайное блуждание / хаос. ⚠ на untradeable.
_REGIME_EMOJI = {
    "TRENDING":       "📈",
    "MEAN_REVERTING": "🔄",
    "RANDOM_WALK":    "🪙",
    "CHAOTIC":        "🌪️",
    "MIXED":          "⚖️",
    "UNKNOWN":        "❓",
}

# Подпись к режиму на русском. Достаточно короткая чтобы не съедать
# полстроки. «↑» / «↓» как дополнительный визуальный маркер направления
# не ставим — направление будет в Markov-строке (current_state).
_REGIME_RU = {
    "TRENDING":       "ТРЕНД",
    "MEAN_REVERTING": "MEAN-REVERTING",
    "RANDOM_WALK":    "RANDOM WALK",
    "CHAOTIC":        "ХАОС",
    "MIXED":          "МИКС",
    "UNKNOWN":        "?",
}


def _complexity_line(p: dict, indent: str = "    ") -> str | None:
    """Однострочный «вердикт» режима + ключевые цифры.

    Сжатая замена бывших 4 отдельных строк (Hurst-блок + VRT + EWMA). Идея:
    одна строка-«заголовок» с вердиктом и плотным набором цифр,
    подсвеченным эмодзи. Все цифры по-прежнему доступны агентам.

    Returned format example:
        📈 ТРЕНД  H=0.56  PE=0.99  score=0.60  VR=1.42 (H0 отвергнут)  σ̂=1.84% (год.35%)

    Returns None when complexity_hint is missing or UNKNOWN — обе поверхности
    (/markets и /daily) просто пропустят строку. Это сохраняет
    additive-семантику на активах с короткой историей (<64 баров).
    """
    hint = p.get("complexity_hint")
    if not hint or hint == "UNKNOWN":
        return None

    h = p.get("hurst")
    pe = p.get("perm_entropy")
    e = p.get("entropy_normalized")
    s = p.get("tradeable_score")
    vr = p.get("vrt_ratio")
    vr_rw = p.get("vrt_random_walk")
    vol_1d = p.get("vol_sigma_1d_pct")
    vol_ann = p.get("vol_sigma_annual_pct")

    # Цифры, дробью-разделителем '  ' (двойной пробел) — визуально проще
    # читать чем 'X | Y | Z'.
    parts: list[str] = []
    if isinstance(h, (int, float)):
        parts.append(f"H={h:.2f}")
    # Permutation entropy предпочтительнее Шенноновской (устойчивее на
    # коротких рядах); если её нет — fallback на Шеннон.
    if isinstance(pe, (int, float)):
        parts.append(f"PE={pe:.2f}")
    elif isinstance(e, (int, float)):
        parts.append(f"энтр={e:.2f}")
    if isinstance(s, (int, float)):
        parts.append(f"score={s:.2f}")
    if isinstance(vr, (int, float)):
        rw_tag = "H0 не отвергнут" if vr_rw else "H0 отвергнут"
        parts.append(f"VR={vr:.2f} ({rw_tag})")
    if isinstance(vol_1d, (int, float)):
        if isinstance(vol_ann, (int, float)):
            parts.append(f"σ̂={vol_1d:.2f}% (год.{vol_ann:.0f}%)")
        else:
            parts.append(f"σ̂={vol_1d:.2f}%")

    if not parts:
        return None

    emoji = _REGIME_EMOJI.get(hint, "•")
    ru = _REGIME_RU.get(hint, hint)
    warn = ""
    if isinstance(s, (int, float)) and s < 0.3:
        # Score <0.3 means random_walk/chaotic regime — agents must NOT take
        # an MA-based directional bet here. Loud warning so it gets noticed.
        warn = " ⚠️ untradeable"
    return f"{indent}{emoji} {ru}{warn}  " + "  ".join(parts)


def _markov_line(p: dict, indent: str = "    ") -> str | None:
    """Markov 3-state — компактная вторая строка.

    Пример:
        🎲 Markov FLAT (~1.6 баров)  UP 27% / FLAT 37% / DOWN 36%

    Returns None если состояние или next_probs отсутствуют (данных мало).
    """
    state = p.get("markov_state")
    nxt = p.get("markov_next_probs")
    dwell = p.get("markov_dwell_bars")
    if not state or not isinstance(nxt, dict):
        return None

    def _pct(x: float) -> str:
        return f"{x * 100:.0f}%"

    nxt_parts: list[str] = []
    for s in ("UP", "FLAT", "DOWN"):
        v = nxt.get(s)
        if isinstance(v, (int, float)):
            nxt_parts.append(f"{s} {_pct(v)}")

    dwell_tag = ""
    if isinstance(dwell, (int, float)):
        dwell_tag = f" (~{dwell:.1f} баров)"

    body = f"Markov {state}{dwell_tag}"
    if nxt_parts:
        body += "  " + " / ".join(nxt_parts)
    return f"{indent}🎲 {body}"


def _quant_lines(p: dict, indent: str = "    ") -> list[str]:
    """Возвращает компактные «количественные» строки для одного актива.

    Замена бывших 4 строк (Hurst / VRT / Markov / EWMA): теперь максимум
    2 строки на актив:
        строка 1 — вердикт режима + Hurst/PE/score/VRT/σ̂ inline;
        строка 2 — Markov: текущее состояние + next-bar probs + dwell.

    Каждая опциональна — если поля отсутствуют (короткий ряд / вырожденный
    return-series / упавший фетчер), соответствующая строка пропадает.
    Это сохраняет gracefuldegradation, как было до сжатия.
    """
    out: list[str] = []
    for fn in (_complexity_line, _markov_line):
        line = fn(p, indent=indent)
        if line:
            out.append(line)
    return out


def _fmt_pct(v: float) -> str:
    """`+5.0%` / `−2.5%` — минус юникодный (U+2212), как в `signal_trader._fmt_pct`."""
    s = f"{v:+.1f}%"
    return s.replace("-", "−")


def _sl_tp_lines(
    p: dict,
    asset: str,
    indent: str = "    ",
    prefix: str = "$",
) -> list[str]:
    """Две строки SL/TP — для LONG и для SHORT — от текущей цены, по σ̂.

    Формат::

        🎯 LONG  TP $82,512 (+4.9%)  SL $77,160 (−2.5%)  R/R 2:1
        🎯 SHORT TP $75,200 (−4.9%)  SL $81,076 (+2.5%)  R/R 2:1

    Идея: пользователь в `/markets` сразу видит конкретные уровни SL/TP
    для входа в обе стороны — если он хочет войти сейчас или если по
    нашему анализу будет пробой в любую сторону. Без этой строки юзеру
    приходилось вручную считать `entry × (1 ± k·σ̂)` от глаза.

    Формула 1:1 совпадает с `core.signal_scorer.make_setup`:
      LONG:  SL = price·(1 − 1.5σ̂),  TP = price·(1 + 3σ̂)
      SHORT: SL = price·(1 + 1.5σ̂),  TP = price·(1 − 3σ̂)
      R/R = 2.0x всегда (TP_dist = 2·SL_dist).

    Округляем до tick'а биржи через `ASSET_TICK_SIZE`, иначе при копи-
    пасте в Bybit-Spot брокер отвергает ордер из-за лишних знаков (мы
    уже наступали на XRP — 1 знак после точки).

    Возвращает [] если:
      • `vol_sigma_1d_pct` отсутствует (короткий ряд / упавший фетчер);
      • цена ≤ 0 (плохие данные);
      • `core.signal_scorer` недоступен (graceful-degradation).
    """
    try:
        from core.signal_scorer import (
            ASSET_TICK_SIZE,
            SL_SIGMA_MULT,
            TP_SIGMA_MULT,
            _round_to_tick,
        )
    except ImportError:
        return []

    sigma_pct = p.get("vol_sigma_1d_pct")
    price = p.get("price")
    if not isinstance(sigma_pct, (int, float)) or sigma_pct <= 0:
        return []
    if not isinstance(price, (int, float)) or price <= 0:
        return []

    sigma_frac = float(sigma_pct) / 100.0
    sl_pct = SL_SIGMA_MULT * sigma_frac
    tp_pct = TP_SIGMA_MULT * sigma_frac

    tick = ASSET_TICK_SIZE.get(asset, 0.0001)
    price_f = float(price)

    long_tp = _round_to_tick(price_f * (1.0 + tp_pct), tick)
    long_sl = _round_to_tick(price_f * (1.0 - sl_pct), tick)
    short_tp = _round_to_tick(price_f * (1.0 - tp_pct), tick)
    short_sl = _round_to_tick(price_f * (1.0 + sl_pct), tick)

    long_line = (
        f"{indent}🎯 LONG  TP {_fmt_money(long_tp, prefix=prefix)} ({_fmt_pct(tp_pct * 100)})  "
        f"SL {_fmt_money(long_sl, prefix=prefix)} ({_fmt_pct(-sl_pct * 100)})  R/R 2:1"
    )
    short_line = (
        f"{indent}🎯 SHORT TP {_fmt_money(short_tp, prefix=prefix)} ({_fmt_pct(-tp_pct * 100)})  "
        f"SL {_fmt_money(short_sl, prefix=prefix)} ({_fmt_pct(sl_pct * 100)})  R/R 2:1"
    )
    return [long_line, short_line]


def _quant_verdict_line(p: dict, indent: str = "    ") -> str | None:
    """Одна строка с вердиктом квант-фильтра (BB+Donchian+RSI + BTC gate).

    Формат::

        🟢 Quant: LONG (90%)  BB≤0.1, RSI<30, Donchian-20 пробой вниз; BTC LONG подтверждает
        ⚪️ Quant: NEUTRAL (0%)  без конфлюэнции (NEUTRAL)

    Показываем под trigger/SL-TP-блоком как «дополнительный сигнал». Это
    deterministic-вердикт, который на бэктесте показывал 65.9% hit-rate на
    1-5д горизонте (vs MA50/200 → 49.6%). Используется и для отображения, и
    для post-processing LLM-вердикта в /daily (см. quant_filter.reconcile_with_llm).

    Возвращает None если поля отсутствуют (короткий ряд, упал фетчер).
    """
    verdict = p.get("quant_verdict")
    if not verdict:
        return None
    confidence = p.get("quant_confidence", 0)
    reason = p.get("quant_reason", "")
    if verdict == "LONG":
        emoji = "🟢"
    elif verdict == "SHORT":
        emoji = "🔴"
    else:
        emoji = "⚪️"
    line = f"{indent}{emoji} Quant: {verdict} ({confidence}%)"
    if reason:
        line += f"  {reason}"
    return line


def _trigger_lines(
    p: dict,
    indent: str = "    ",
    prefix: str = "$",
) -> list[str]:
    """Две строки с MA-триггерами LONG / SHORT для одного актива.

    Формат::

        ▲ выше $81,963 (MA200) → LONG
        ▼ ниже $74,969 (MA50) → SHORT

    Выбор уровней:
      • верхний (▲) = max(MA50, MA200) — закрытие выше → LONG-сетап
      • нижний (▼) = min(MA50, MA200) — закрытие ниже → SHORT-сетап

    Логика совпадает с `build_short_report` (main.py) и Synth-агентом — это
    стандартный per-asset double-trigger CASH-план в нашей системе. В
    /markets эти строки делают намерение визуально явным: пользователь
    сразу видит ДВА конкретных уровня, не выискивая их в trend-строке.

    Возвращает [] если хотя бы одного из MA нет (короткий ряд / упавший
    фетчер). Это сохраняет graceful-degradation: не падаем, просто
    скрываем триггеры.
    """
    ma50 = p.get("ma50")
    ma200 = p.get("ma200")
    if not isinstance(ma50, (int, float)) or not isinstance(ma200, (int, float)):
        return []
    if ma200 >= ma50:
        up_level, up_tag = ma200, "MA200"
        dn_level, dn_tag = ma50, "MA50"
    else:
        up_level, up_tag = ma50, "MA50"
        dn_level, dn_tag = ma200, "MA200"
    up = _fmt_money(up_level, prefix=prefix)
    dn = _fmt_money(dn_level, prefix=prefix)
    return [
        f"{indent}▲ выше {up} ({up_tag}) → LONG",
        f"{indent}▼ ниже {dn} ({dn_tag}) → SHORT",
    ]


def _sr_lines(
    p: dict,
    indent: str = "    ",
    prefix: str = "$",
) -> list[str]:
    """Две строки с уровнями сопротивления (R) и поддержки (S) для одного актива.

    Формат::

        🎯 R: $81,956 MA200 +3.6%  │  $82,500 свинг-15д +4.3%
           S: $74,968 MA50 −5.2%   │  $73,200 свинг-22д −7.5%

    Выбор уровней:
      • R₁/R₂ — ближайшие к текущей цене сверху (после strength-ранжирования)
      • S₁/S₂ — ближайшие снизу
      • метка `MA200` / `MA50` если уровень в пределах 0.5% от MA (confluence)
      • иначе «свинг-Nд» где N — баров назад был последний пивот

    Возвращает [] если данных мало (нет `_highs_daily`/`_lows_daily`).
    Это сохраняет graceful-degradation для активов без OHLC (например
    F&G, MACRO-агрегаторы).
    """
    highs = p.get("_highs_daily")
    lows  = p.get("_lows_daily")
    price = p.get("price")
    if not isinstance(highs, list) or not isinstance(lows, list):
        return []
    if not isinstance(price, (int, float)) or price <= 0:
        return []
    # Минимум 2*lookback+1 = 11 баров для lookback=5. На практике хотим
    # хотя бы ~30 чтобы было где появиться пивотам с обоих сторон.
    if len(highs) < 30 or len(lows) < 30:
        return []

    # Импорт внутри функции — избегаем кругового импорта core/* ⇄ web_search
    # в случае если core.support_resistance в будущем подтянет что-то отсюда.
    from core.support_resistance import compute_sr_levels, label_level_source

    sr = compute_sr_levels(
        highs, lows,
        current_price=float(price),
        lookback=5,
        tolerance_pct=0.5,
        recency_halflife=30.0,
        num_each_side=2,
    )
    if not sr.resistances and not sr.supports:
        return []

    ma50 = p.get("ma50") if isinstance(p.get("ma50"), (int, float)) else None
    ma200 = p.get("ma200") if isinstance(p.get("ma200"), (int, float)) else None

    def _fmt_level(lv) -> str:
        """`$81,956 MA200 +3.6%` / `$74,968 свинг-15д −5.2%`."""
        src = label_level_source(lv, ma50=ma50, ma200=ma200)
        pct = (lv.price - float(price)) / float(price) * 100
        sign = "+" if pct >= 0 else "−"
        return f"{_fmt_money(lv.price, prefix=prefix)} {src} {sign}{abs(pct):.1f}%"

    out: list[str] = []
    # R-строка
    if sr.resistances:
        r_strs = [_fmt_level(lv) for lv in sr.resistances]
        out.append(f"{indent}🎯 R: " + "  │  ".join(r_strs))
    # S-строка — выравниваем «S:» под «R:» (на месте 🎯 — пробелы).
    if sr.supports:
        s_strs = [_fmt_level(lv) for lv in sr.supports]
        # 4 пробела чтобы выровнять под «🎯 R:» (эмодзи занимает ~2 знакоместа).
        out.append(f"{indent}   S: " + "  │  ".join(s_strs))
    return out


def format_prices_for_agents(
    prices: dict,
    *,
    for_user: bool = False,
    skip_sr: bool = False,
    crypto_assets: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Рендерит prices dict в текст для агентов или пользователя.

    Параметры:
      for_user=False (default) — формат для Bull/Bear/Synth AI-агентов:
        ─ заголовок «=== ВЕРИФИЦИРОВАННЫЕ РЫНОЧНЫЕ ДАННЫЕ (timestamp) ===»
        ─ инструкция «⚠️ используй ТОЛЬКО эти цифры» снизу
        ─ без пустых строк между активами (компактно для LLM context)
      for_user=True — формат для команды /markets (Telegram-юзер):
        ─ без дубль-заголовка (уже есть в обёртке `📊 РЫНКИ И СИГНАЛЫ`)
      skip_sr=True — не рендерить S/R-строки (🎯 R: / S:). Используется
        в summary-экране /markets, чтобы текст влезал в одно сообщение;
        при клике на «💲 Крипта» S/R появляются полностью.
        ─ без AI-инструкции снизу
        ─ пустая строка между активами (для читаемости)
      crypto_assets — если передан, в блоке [КРИПТОРЫНОК] покажем только
        эти тикеры (в том порядке, в каком они есть в CRYPTO_KEYS). Нужно
        для пагинации в /markets (15 активов не влезают в одно
        Telegram-сообщение и неудобно листаются). Для AI-агентов
        этот параметр не трогаем — им нужен полный контекст.
    """
    if not prices:
        return "Рыночные данные временно недоступны."

    now   = datetime.now().strftime("%d.%m.%Y %H:%M UTC")
    # Скользящее 24ч-окно считается от момента запроса, а не от полуночи.
    # Юзер видел "+0.24% (24ч)" в 08:38 и "-0.05% (24ч)" в 07:04 для BTC и
    # удивлялся "почему 24ч разные". В подпись добавляем HH:MM, чтобы
    # очевидно было, что окно скользит.
    now_hm = datetime.now().strftime("%H:%M")
    lines: list[str] = []
    if not for_user:
        lines.append(f"=== ВЕРИФИЦИРОВАННЫЕ РЫНОЧНЫЕ ДАННЫЕ ({now}) ===")

    lines.append("\n[КРИПТОРЫНОК]")
    if crypto_assets is None:
        crypto_keys_to_use = CRYPTO_KEYS
    else:
        # Сохраняем порядок CRYPTO_KEYS, отфильтровывая по
        # переданному набору. Баха «юзер передал НЕИЗВЕСТНЫЙ
        # тикер» избегаем — неизвестные просто выпадают. Из пустого
        # набора выпадает весь блок (только заголовок
        # [КРИПТОРЫНОК]) — нормально для ситуации «нет
        # активов для этой страницы».
        asset_set = set(crypto_assets)
        crypto_keys_to_use = tuple(k for k in CRYPTO_KEYS if k in asset_set)
    crypto_iter: list[tuple[str, str]] = [
        (k, CRYPTO_LABELS.get(k, k)) for k in crypto_keys_to_use
    ]
    for k, label in crypto_iter:
        if k in prices:
            p  = prices[k]
            ch = p["change_24h"]
            ch7  = p.get("change_7d")
            ch30 = p.get("change_30d")
            ma50 = p.get("ma50")
            ma200 = p.get("ma200")
            above50  = p.get("above_ma50")
            above200 = p.get("above_ma200")
            trend    = p.get("trend", "")
            trend_e  = p.get("trend_emoji", "")
            vol      = p.get("volume_24h_usd")

            # Базовая строка. _fmt_money — адаптивная точность, иначе XRP
            # ($1.46) с ${...:,.2f} нормально, но MA ниже проблема.
            line = (f"  {label} ({k}): {_fmt_money(p['price'], prefix='$')}  "
                    f"{_fmt_change(ch)} (24ч на {now_hm})")
            if ch7 is not None:
                line += f"  {_fmt_change(ch7, decimals=1)} (7д)"
            if ch30 is not None:
                line += f"  {_fmt_change(ch30, decimals=1)} (30д)"
            line += f"  [{p['source']}]"
            lines.append(line)

            # MA-триггеры LONG/SHORT — две строки перед trend-блоком. Это
            # визуально приклеивает per-asset план («▲ выше $X → LONG; ▼ ниже
            # $Y → SHORT») к цене, не дожидаясь дайджеста. Совпадает с
            # форматом, который генерит Synth-агент в `plans[].trigger`.
            for t in _trigger_lines(p):
                lines.append(t)

            # S/R-уровни: 2 сопротивления выше цены + 2 поддержки ниже.
            if os.getenv("FEATURE_SR_LEVELS", "1") != "0" and not skip_sr:
                for t in _sr_lines(p):
                    lines.append(t)

            # SL/TP-уровни от текущей цены для LONG и SHORT — чтобы юзер
            # сразу видел, куда ставить стоп и тейк, без ручного счёта
            # `price × (1 ± k·σ̂)`. Появляется только если σ̂ посчиталась
            # (вместе со строкой `🔄 ... σ̂=...%`).
            for t in _sl_tp_lines(p, asset=k):
                lines.append(t)

            # Квант-фильтр (BB+Donchian+RSI ансамбль + BTC regime gate).
            # На бэктесте 65.9% hit-rate vs 49.6% MA50/200. Появляется
            # только если есть достаточно истории (60+ дневок); см.
            # ``quant_filter.py`` и ``docs/quant_research_v2.md``.
            ql = _quant_verdict_line(p)
            if ql:
                lines.append(ql)

            # Тренд и MA — адаптивная точность через _fmt_money:
            # XRP MA50=$1.30 раньше рендерился как `$1`, Synth писал «$1 (MA50)».
            if trend:
                trend_line = f"    {trend_e} ТРЕНД: {trend}"
                if ma50 is not None:
                    pos50 = "выше" if above50 else "ниже"
                    pct50 = ((p['price'] - ma50) / ma50 * 100)
                    trend_line += f" | MA50: {_fmt_money(ma50, prefix='$')} ({pos50}, {pct50:+.1f}%)"
                if ma200 is not None:
                    pos200 = "выше" if above200 else "ниже"
                    pct200 = ((p['price'] - ma200) / ma200 * 100)
                    trend_line += f" | MA200: {_fmt_money(ma200, prefix='$')} ({pos200}, {pct200:+.1f}%)"
                lines.append(trend_line)

            # Полный пакет «количественных» метрик: Hurst+entropy, VRT+perm_entropy,
            # Markov state + next-bar probs, EWMA σ-forecast. Bull/Bear/Synth
            # видят эти строки в РЕАЛЬНЫХ РЫНОЧНЫХ ДАННЫХ и могут отказаться
            # от направленного тезиса при random_walk / chaotic / vrt не отвергает H0.
            for q in _quant_lines(p):
                lines.append(q)

            # Объём
            if vol:
                lines.append(f"    Объём 24ч: ${vol:,.0f}M USD")

            # Между активами — пустая строка только для пользовательской
            # ветки (читаемость). У агентов компактный формат.
            if for_user:
                lines.append("")

    if "MACRO" in prices:
        m   = prices["MACRO"]
        fng = m.get("fng", {})
        fv, fs = fng.get("val","N/A"), fng.get("status","")
        fc = fng.get("change", 0)
        lines.append("\n[МАКРОЭКОНОМИКА США]")
        lines.append(f"  Ставка ФРС:   {m['fed_rate']}%  [FRED]")
        lines.append(f"  Инфляция CPI: {_cpi_yoy(m.get('cpi_raw','N/A'))}  [FRED]")
        lines.append(
            f"  Fear & Greed: {fv}/100 ({fs})  "
            f"{'🟢' if fc > 0 else '🔴' if fc < 0 else '➡️'} {fc:+d} за сутки  "
            f"[Источник: Alternative.me Crypto F&G — НЕ FRED]"
        )
        # F&G 7-дневный тренд (pre-live-hardening)
        fng_trend = fng.get("trend_7d", "")
        fng_hist = fng.get("history_7d", [])
        if fng_trend and fng_hist:
            trend_label = {"RISING": "↗️ растёт", "FALLING": "↘️ падает", "STABLE": "→ стабилен"}.get(fng_trend, fng_trend)
            lines.append(f"  F&G тренд 7д: {trend_label} ({' → '.join(str(x) for x in fng_hist[:5])})")
        lines.append("  [!] CPI = индекс (~323), НЕ %. Инфляция = YoY (выше).")

    # MA200/MA50 для макро — нужны Synth-агенту, чтобы строить per-asset планы
    # с двумя триггерами (закрытие выше MA200 → LONG; ниже MA50 → SHORT).
    def _macro_trend_line(p: dict, unit: str = "") -> str:
        ma50 = p.get("ma50"); ma200 = p.get("ma200")
        above50 = p.get("above_ma50"); above200 = p.get("above_ma200")
        trend = p.get("trend", ""); trend_e = p.get("trend_emoji", "")
        chunks = []
        if trend:
            chunks.append(f"{trend_e} ТРЕНД: {trend}")
        if ma50 is not None:
            pos50 = "выше" if above50 else "ниже"
            pct50 = ((p['price'] - ma50) / ma50 * 100) if ma50 else 0
            chunks.append(f"MA50: {_fmt_money(ma50)}{unit} ({pos50}, {pct50:+.1f}%)")
        if ma200 is not None:
            pos200 = "выше" if above200 else "ниже"
            pct200 = ((p['price'] - ma200) / ma200 * 100) if ma200 else 0
            chunks.append(f"MA200: {_fmt_money(ma200)}{unit} ({pos200}, {pct200:+.1f}%)")
        return " | ".join(chunks)

    lines.append("\n[ФОНДОВЫЕ ИНДЕКСЫ]")
    for k, label in [("SPX","S&P 500"),("NDX","Nasdaq 100"),("VIX","VIX")]:
        if k in prices:
            p  = prices[k]
            ch = p["change_24h"]
            lines.append(f"  {label}: {p['price']:,.2f}  "
                         f"{_fmt_change(ch)}  [{p['source']}]")
            # SPX/NDX/VIX рендерим без $-префикса — это индексы, не доллары.
            for t in _trigger_lines(p, prefix=""):
                lines.append(t)
            if os.getenv("FEATURE_SR_LEVELS", "1") != "0" and not skip_sr:
                for t in _sr_lines(p, prefix=""):
                    lines.append(t)
            tl = _macro_trend_line(p)
            if tl:
                lines.append(f"    {tl}")
            for q in _quant_lines(p):
                lines.append(q)
            if for_user:
                lines.append("")

    lines.append("\n[СЫРЬЁ И ВАЛЮТЫ]")
    for k, label, unit in [("OIL_WTI","Нефть WTI","$/барр"),
                            ("GOLD","Золото","$/унц"),
                            ("DXY","Индекс доллара","")]:
        if k in prices:
            p  = prices[k]
            ch = p["change_24h"]
            u  = f" {unit}" if unit else ""
            lines.append(f"  {label}: {p['price']:,.2f}{u}  "
                         f"{_fmt_change(ch)}  [{p['source']}]")
            # OIL/GOLD — в долларах ($/барр, $/унц); DXY — индекс без $.
            trig_prefix = "$" if k in ("OIL_WTI", "GOLD") else ""
            for t in _trigger_lines(p, prefix=trig_prefix):
                lines.append(t)
            if os.getenv("FEATURE_SR_LEVELS", "1") != "0" and not skip_sr:
                for t in _sr_lines(p, prefix=trig_prefix):
                    lines.append(t)
            tl = _macro_trend_line(p, unit=u if k == "OIL_WTI" or k == "GOLD" else "")
            if tl:
                lines.append(f"    {tl}")
            for q in _quant_lines(p):
                lines.append(q)
            if for_user:
                lines.append("")

    if not for_user:
        lines.append("\n⚠️ ИНСТРУКЦИЯ: используй ТОЛЬКО эти цифры. "
                     "Если актива нет — пиши 'нет данных'.")
    # Сжимаем хвостовые пустые строки (от for_user=True) — не более одной.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# ─── /markets minimal renderer ────────────────────────────────────────────────
# Используется в новом меню /markets, где юзер выбирает секцию
# (Крипта/Макро/Индексы/Сырьё) и получает короткую сводку: цена + MA-триггеры
# (▲/▼). Без эмодзи-«покраски» (🟢🔴) и квант-блока — это «минимализм»
# отображения, тот же data-источник что и в `format_prices_for_agents`.

ASSET_ICONS_MIN: dict[str, str] = {
    "BTC":  "₿",
    "ETH":  "Ξ",
    "SOL":  "◎",
    "BNB":  "◆",
    "XRP":  "✕",
    "ADA":  "₳",
    "DOGE": "Ð",
    "AVAX": "▲",
    "LINK": "⛓",
    "DOT":  "●",
    "TRX":  "Ⓣ",
    "TON":  "◈",
    "LTC":  "Ł",
    "NEAR": "Ⓝ",
    "SUI":  "≈",
}


def _fmt_change_min(ch: float | int | None, *, decimals: int = 2) -> str:
    """`+1.20%` / `−0.50%` без эмодзи. Минус — юникодный U+2212, как в `_fmt_pct`."""
    try:
        v = float(ch) if ch is not None else 0.0
    except (TypeError, ValueError):
        v = 0.0
    if abs(v) < 0.005:
        return f"{v:+.{decimals}f}%".replace("-", "−")
    return f"{v:+.{decimals}f}%".replace("-", "−")


def _crypto_lines_minimal(prices: dict) -> list[str]:
    lines: list[str] = []
    for k in CRYPTO_KEYS:
        if k not in prices:
            continue
        p = prices[k]
        icon = ASSET_ICONS_MIN.get(k, "•")
        change = p.get("change_24h")
        price = p.get("price")
        lines.append(
            f"{icon} {k} — {_fmt_money(price, prefix='$')}   {_fmt_change_min(change)} 24ч"
        )
        # ▲/▼ MA-триггеры (та же логика что в `_trigger_lines`).
        for trig in _trigger_lines(p, indent="   "):
            lines.append(trig)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _macro_lines_minimal(prices: dict) -> list[str]:
    if "MACRO" not in prices:
        return []
    m = prices["MACRO"]
    fng = m.get("fng", {}) or {}
    lines: list[str] = [
        f"ФРС   {m.get('fed_rate', 'N/A')}%",
        f"CPI   {_cpi_yoy(m.get('cpi_raw', 'N/A'))} YoY",
    ]
    fv = fng.get("val", "N/A")
    fs = fng.get("status", "")
    fc = int(fng.get("change", 0) or 0)
    arrow = "↗" if fc > 0 else "↘" if fc < 0 else "→"
    fs_part = f" ({fs})" if fs else ""
    lines.append(f"F&G   {fv}/100{fs_part}   {arrow} {fc:+d}/сут")
    return lines


def _indices_lines_minimal(prices: dict) -> list[str]:
    lines: list[str] = []
    for k, label in (("SPX", "S&P 500"), ("NDX", "Nasdaq 100"), ("VIX", "VIX")):
        if k not in prices:
            continue
        p = prices[k]
        ch = p.get("change_24h", 0)
        try:
            price = float(p.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        lines.append(f"{label}   {price:,.2f}   {_fmt_change_min(ch)} 24ч")
        for trig in _trigger_lines(p, indent="   ", prefix=""):
            lines.append(trig)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _commod_lines_minimal(prices: dict) -> list[str]:
    lines: list[str] = []
    for k, label, unit in (
        ("OIL_WTI", "Нефть WTI", "$/барр"),
        ("GOLD", "Золото", "$/унц"),
        ("DXY", "DXY", ""),
    ):
        if k not in prices:
            continue
        p = prices[k]
        ch = p.get("change_24h", 0)
        try:
            price = float(p.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        unit_part = f" {unit}" if unit else ""
        lines.append(f"{label}   {price:,.2f}{unit_part}   {_fmt_change_min(ch)} 24ч")
        prefix = "$" if k in ("OIL_WTI", "GOLD") else ""
        for trig in _trigger_lines(p, indent="   ", prefix=prefix):
            lines.append(trig)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


SECTION_TITLES_MIN: dict[str, str] = {
    "crypto": "💲 *Крипта*",
    "macro": "🌐 *Макро*",
    "indices": "📈 *Индексы*",
    "commod": "⛽ *Сырьё*",
}


def format_prices_minimal(
    prices: dict, *, section: str = "all", include_title: bool = True
) -> str:
    """Минималистичный per-секционный рендер цен.

    `section`:
      • ``crypto`` — 15 топ-активов (BTC/ETH/SOL/BNB/XRP + ADA/DOGE/AVAX/LINK/DOT/TRX/TON/LTC/NEAR/SUI) + ▲/▼ MA-триггеры
      • ``macro``  — ФРС / CPI / F&G
      • ``indices``— SPX / NDX / VIX + ▲/▼
      • ``commod`` — Oil WTI / Gold / DXY + ▲/▼
      • ``all``    — все секции по очереди
    """
    if not prices:
        return "Рыночные данные временно недоступны."

    blocks: list[tuple[str, list[str]]] = []
    if section in ("crypto", "all"):
        blocks.append(("crypto", _crypto_lines_minimal(prices)))
    if section in ("macro", "all"):
        blocks.append(("macro", _macro_lines_minimal(prices)))
    if section in ("indices", "all"):
        blocks.append(("indices", _indices_lines_minimal(prices)))
    if section in ("commod", "all"):
        blocks.append(("commod", _commod_lines_minimal(prices)))

    parts: list[str] = []
    for key, body in blocks:
        if not body:
            continue
        if include_title and section == "all":
            parts.append(SECTION_TITLES_MIN.get(key, key) + "\n" + "\n".join(body))
        else:
            parts.append("\n".join(body))
    return "\n\n".join(parts)


# ─── /markets per-section full renderer ───────────────────────────────────────
# Юзер: «было детальнее, верни всю инфу — но per-section». Идея: тот же
# рич-формат, что и `format_prices_for_agents(for_user=True)`, только
# фильтруем по секции (КРИПТОРЫНОК / МАКРОЭКОНОМИКА США / ФОНДОВЫЕ ИНДЕКСЫ /
# СЫРЬЁ И ВАЛЮТЫ). Не дублирую логику — режу готовый текст по заголовкам.
_SECTION_HEADER_RAW: dict[str, str] = {
    "crypto":  "[КРИПТОРЫНОК]",
    "macro":   "[МАКРОЭКОНОМИКА США]",
    "indices": "[ФОНДОВЫЕ ИНДЕКСЫ]",
    "commod":  "[СЫРЬЁ И ВАЛЮТЫ]",
}


def format_prices_section(
    prices: dict,
    *,
    section: str = "all",
    skip_sr: bool = False,
    crypto_assets: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Per-секционный рендер цен с полной информацией (24ч/7д/30д, MA-триггеры,
    SL/TP LONG/SHORT, Quant-вердикт, ТРЕНД+MA50/200, Random walk/Markov, объём).

    `section`:
      • ``crypto`` — 15 топ-активов (BTC/ETH/SOL/BNB/XRP + ADA/DOGE/AVAX/LINK/DOT/TRX/TON/LTC/NEAR/SUI)
      • ``macro``  — ФРС / CPI / F&G
      • ``indices``— SPX / NDX / VIX
      • ``commod`` — Oil WTI / Gold / DXY
      • ``all``    — всё подряд (то же что `format_prices_for_agents(for_user=True)`)
    `skip_sr`: пропустить S/R-строки (для summary-экрана, где текст должен
    влезать в одно сообщение).
    Возвращает текст с пустыми строками между активами и заголовком секции
    (`[КРИПТОРЫНОК]` и т.д.) — совместимо со старым форматом /markets.
    """
    if not prices:
        return "Рыночные данные временно недоступны."
    full = format_prices_for_agents(
        prices, for_user=True, skip_sr=skip_sr, crypto_assets=crypto_assets,
    )
    if section == "all":
        return full.lstrip("\n")
    header = _SECTION_HEADER_RAW.get(section)
    if not header:
        return ""
    marker = "\n" + header
    idx = full.find(marker)
    if idx < 0:
        return ""
    start = idx + 1  # пропускаем ведущий "\n", оставляем "[ЗАГОЛОВОК]"
    # Конец секции — начало следующего заголовка (любой `\n[...]`).
    rest = full[start + len(header):]
    next_idx = rest.find("\n[")
    end = start + len(header) + (next_idx if next_idx >= 0 else len(rest))
    return full[start:end].rstrip()


async def get_full_realtime_context(*, for_user: bool = False) -> tuple[dict, str]:
    """Тянет prices + рендерит. `for_user=True` использует читаемый формат
    для команды /markets (без AI-инструкций / дубль-заголовка, с пустыми
    строками между активами)."""
    prices    = await fetch_realtime_prices()
    formatted = format_prices_for_agents(prices, for_user=for_user)

    try:
        from cot_data import format_cot_for_agents, get_cot_for_assets
        cot_data = await get_cot_for_assets(["Bitcoin", "Gold", "Crude Oil"])
        cot_formatted = format_cot_for_agents(cot_data)
        formatted += "\n\n" + cot_formatted
    except Exception as e:
        logger.warning(f"COT data error: {e}")

    try:
        from etf_flows import format_etf_flows_for_agents, get_market_breadth, get_etf_flows
        etf_data = await get_etf_flows()
        etf_formatted = format_etf_flows_for_agents(etf_data)
        formatted += "\n\n" + etf_formatted

        breadth = await get_market_breadth()
        prices["BREADTH"] = breadth
    except Exception as e:
        logger.warning(f"ETF flows error: {e}")

    return prices, formatted
