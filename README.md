# Dialectic Edge — AI Trading System

> Autonomous AI trading system на **smart-money signals + adaptive Kelly + self-audit**. Не retail sentiment как у конкурентов.

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/) [![License](https://img.shields.io/badge/License-Private-red)]() [![Status](https://img.shields.io/badge/Status-Production-success)]() [![Deploy](https://img.shields.io/badge/Deploy-Railway-purple)](https://railway.app/) [![Telegram](https://img.shields.io/badge/Frontend-Telegram-blue)](https://telegram.org/)

---
## 🎯 Что это

**Dialectic Edge** — автономная торговая система, которая работает на принципах системного фонда, а не retail-трейдера:

| Слой | Источники / методы |
|------|--------------------|
| **🏛️ Smart-money signals** *(NEW)* | Top-trader L/S ratio, Coinbase Premium, CME basis, Funding dispersion |
| **📊 Multi-agent AI debate** | 4 агента (Bull / Bear / Verifier / Synth) на разных моделях через OpenRouter |
| **⚖️ Adaptive risk** *(NEW)* | Vol-targeting (CTA-стиль) + dynamic Kelly на real win-rate, persistent state |
| **🛡️ Macro regime** | S&P EMA200 / SMA50, breadth, DXY, VIX → blocks trades against trend |
| **🔄 On-chain** | MVRV, SOPR, Exchange Reserves, Whale Detection |
| **🔍 AI self-audit** *(NEW)* | LLM пишет performance review закрытых сделок раз в неделю |
| **📡 Signal trader** | 5-мин loop, vol-target sizing, ATR stops, Split TP, Trailing |

**Pitch line:** *«мы — vol-targeted CTA-фонд + Kelly на реальных метриках, а не retail trader с 2% от капитала на каждой сделке»*.

## 🚀 Quick demo

| Команда | Что показывает |
|---------|----------------|
| `/daily` | Полный AI-анализ рынка с дебатами, smart-money сигналами, торговый план |
| `/markets` | Real-time контекст + сигналы + цены |
| `/p2p [ASSET] [FIAT] [payments]` | P2P arbitrage scanner: net spread, лимиты, payment overlap, риск контрагентов |
| `/autotrade_status` | Performance: PnL, win-rate, R-ratio, Kelly, vol-target, drawdown |
| `/audit [N дней]` | AI-аудит закрытых сделок: «что работает / что нет / правило на завтра» |
| `/usage` | Расход AI-токенов по провайдерам |
| `/why BTC` | Почему открыта позиция: входной отчёт + текущее состояние |

## 🏆 Что отличает от конкурентов

1. **Smart-money first**: 4 institutional indicator'а (top-trader, Coinbase premium, CME basis, funding dispersion) применяются к scoring **до** retail sentiment'а. Большинство retail-ботов начинают с Twitter / Reddit sentiment.

2. **Adaptive Kelly на реальной истории**: размер позиции считается из `wins / losses / avg_win / avg_loss` собственной торговой истории, persisted в `risk_state.json`. Не статичные «2% риска».

3. **Vol-targeting** *(институциональный стандарт)*: размер позиции обратно пропорционален реализованной волатильности. Quiet day → 2x, panic day → 0.37x. Это ровно то что делают AQR / Renaissance / vol-targeted CTA.

4. **AI self-audit**: LLM раз в неделю смотрит на закрытые сделки, выдаёт performance review с конкретным правилом на следующую неделю. «AI которая учится на своих ошибках».

5. **Multi-provider AI router**: 6 провайдеров (Cerebras, Groq, Mistral, OpenRouter, Together, Gemini), per-role routing — Bull/Bear/Verifier/Synth каждый на оптимальной для его задачи модели. Если один падает — fallback цепочка.

6. **Honest UX**: `/audit` без воды («сегодня винрейт 38% — это плохо, причина X, правило Y»), `/usage` показывает сколько токенов реально потратили, графики с реальными цифрами без cherry-picking.

## ⚙️ Tech stack

| Слой | Технология |
|------|-----------|
| Backend | Python 3.12, asyncio, aiohttp, aiosqlite |
| AI | OpenRouter, Cerebras, Groq, Mistral, Together, Gemini |
| Frontend | Telegram Bot API (aiogram 3) |
| Хранение | SQLite (state) + GitHub Markdown (BACKTEST.md, DIGEST_CACHE.md) |
| Charting | matplotlib (DejaVu Sans) |
| Deployment | Railway (worker dyno + cron) |
| ML/NLP | FinBERT (sentiment classification) |

---

*English version below Russian*

---

## 🇷🇺 РУССКАЯ ВЕРСИЯ

### Описание

**Dialectic Edge** — это полностью автономная торговая система, которая:

1. **Анализирует рынок** через мультиагентные AI-дебаты (Bull vs Bear vs Verifier vs Synth)
2. **Следит за сигналами** от профессиональных трейдеров (Bybit Markets Signals)
3. **Адаптируется к режиму рынка** (UPTREND / SIDEWAYS / HIGH_VOL / DOWNTREND)
4. **Управляет рисками** через Kelly Criterion, ATR, Trailing Stop, Split TP
5. **Торгует виртуально** на Railway (paper trading) с автосохранением состояния на GitHub

### Архитектура — как работает система

```
 ПОЛЬЗОВАТЕЛЬ (Telegram)
        │
        ▼
    ┌─────────┐
    │ main.py │  ← 3512 строк, точка входа
    │ Telegram│     все команды: /daily /analyze /starttrade /screener
    └────┬────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  analysis_service.py                              │
    │  • Собирает данные: news + prices + market_data │
    │  • Запускает AI-дебаты                           │
    │  • Формирует отчёт + торговый план               │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
│  agents.py — МУЛЬТИАГЕНТНЫЕ ДЕБАТЫ               │
     │                                                │
     │  🐂 Bull Researcher   — ищет бычьи аргументы   │
     │  🐻 Bear Skeptic       — ищет медвежьи аргументы│
     │  🔍 Data Verifier     — ловит галлюцинации     │
     │  ⚖️ Consensus Synth    — вердикт (JSON)        │
     │  ✍️ Speechwriter       — форматирует JSON в    │
     │                        читаемый план          │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  ai_provider.py — РОУТЕР МОДЕЛЕЙ                 │
    │                                                │
    │  Cerebras → Groq → Mistral → OpenRouter →       │
    │  Together → Gemini → Free models fallback        │
    │  (все бесплатные, 4+ провайдера)                │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  web_search.py — СБОРЩИК ДАННЫХ                 │
    │                                                │
    │  • Binance: BTC, ETH, SOL цены + объёмы        │
    │  • Yahoo Finance: SPX, NDX, VIX, DXY, GOLD    │
    │  • Alternative.me: Fear & Greed index          │
    │  • FRED (Federal Reserve): Fed Rate, CPI,       │
    │    Yield Curve, Fed Balance                     │
    │  • CoinGecko: BTC on-chain (MVRV, SOPR)        │
    │  • GDELT: геополитические события              │
    │  • Finnhub: market sentiment (опционально)       │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  market_indicators/ — СИСТЕМА БАЛЛОВ             │
    │                                                │
    │  • onchain.py: MVRV, SOPR, Exchange Reserves   │
    │  • macro_extended.py: QE/QT, Yield Curve,     │
    │    Credit Spreads                               │
    │  • scorer.py: Market Score (0-100),             │
    │    критические стоп-факторы                    │
    │  • aggregator.py: всё вместе → контекст для AI │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  signal_trader.py — АВТОТРЕЙДЕР (5 мин цикл)   │
    │                                                │
    │  ИСТОЧНИКИ СИГНАЛОВ:                           │
    │  1. Markets Signals (Bybit) — funding, OI, whales│
    │  2. Наши дайджесты (digest_score)              │
    │                                                │
    │  ФИЛЬТРЫ:                                      │
    │  • MVRV > 3.5 → не открываем LONG             │
    │  • MVRV < 1.0 → не открываем SHORT            │
    │  • Defense Mode → стоп всех позиций            │
    │  • Correlation Matrix → не дублируем риск    │
    │  • Adaptive thresholds (ChatGPT):              │
    │    HIGH_VOL → порог выше, позиция меньше     │
    │    SIDEWAYS → порог ещё выше                 │
    │    UPTREND → порог ниже (легче открыть)     │
    │                                                │
    │  ВЫХОД:                                       │
    │  • Split TP: 50% при +2% → full TP           │
    │  • Trailing Stop: активируется при +3%,      │
    │    движется за ценой (1.5%)                  │
    │  • Hard Stop: SL по ATR × regime              │
    └────┬──────────────────────────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │  TELEGRAM АЛЕРТЫ (автоматические)              │
    │                                                │
    │  • 🚨 MVRV > 4.0 — переоценён               │
    │  • 🔵 MVRV < 1.0 — историческое дно          │
    │  • 🛡️ Defense Mode активирован                 │
    │  • 📊 QE → QT смена                           │
    │  • 📈 Market Score ±8                          │
    │  • 🎯 Позиция открыта / закрыта              │
    └────────────────────────────────────────────────┘

### Конфигурация (Environment Variables)

Скопируй `.env.example` → `.env` и заполни ключи:

```env
# AI Модели (бесплатные)
GROQ_API_KEY=gsk_...
OPENROUTER_API_KEY=sk-or-v1-...
TOGETHER_API_KEY=tgp_v1_...
GEMINI_API_KEY=AIza...
CEREBRAS_API_KEY=csk-...

# Данные
FINNHUB_API_KEY=...
FRED_API_KEY=3b3477...
ALPHA_VANTAGE_API_KEY=...
TAVILY_API_KEY=tvly-...

# Telegram
BOT_TOKEN=123456:ABC-...

# GitHub (для state persistence)
GITHUB_TOKEN=ghp_...
GITHUB_REPO=ANAEHY/dialectic_edge
```

### Запуск

```bash
# Локально
python main.py

# Railway (автоматически)
# просто push → deploy
```

### Команды Telegram

| Команда | Описание |
|---------|----------|
| `/daily` | Полный AI-анализ рынка с дебатами |
| `/analyze <текст>` | Анализ конкретной новости |
| `/starttrade` | Запуск автотрейдера |
| `/stop` | Остановка автотрейдера |
| `/screener` | Сканер аномалий TOP-15 монет |
| `/p2p [ASSET] [FIAT] [payments]` | P2P-арбитраж: buy→sell spread после buffer + фильтры качества |
| `/why <SYMBOL>` | Почему открыта эта позиция |
| `/close <SYMBOL>` | Закрыть позицию вручную |
| `/health` | Health check (БД + GitHub + uptime) |
| `/stats` | Статистика бота |

---

## 📁 Структура файлов (подробно)

### Корневые файлы (точка входа)

| Файл | Строк | Назначение |
|------|-------|-----------|
| `main.py` | 3512 | Telegram bot, все handlers, диспетчер команд |
| `config.py` | — | Загрузка environment variables |
| `analysis_service.py` | 286 | Оркестратор: собирает данные → запускает дебаты → формирует отчёт |
| `ai_provider.py` | 765 | Роутер AI-моделей с fallback на 6 провайдеров |
| `agents.py` | 661 | 4 агента + Speechwriter + DebateOrchestrator |

### Торговля

| Файл | Назначение |
|------|------------|
| `signal_trader.py` | Автотрейдер: 5-мин цикл, открытие/закрытие позиций, trailing stop, split TP |
| `backtester.py` | Бэктест на исторических свечах (OHLC) |
| `signals.py` | Markets Signals API (Bybit): funding, OI, account ratio |
| `database.py` | SQLite: trade history, predictions, track record, backtest config |
| `session_manager.py` | Persistence виртуального капитала |
| `github_export.py` | Экспорт BACKTEST.md, FORECASTS.md, DIGEST_CACHE.md, MARKET_CACHE.md |

### AI и данные

| Файл | Назначение | Источники / API |
|------|------------|-----------------|
| `web_search.py` | Realtime prices (Binance, Yahoo), Fear & Greed, макро с FRED | Binance, Yahoo Finance, Alternative.me, FRED |
| `data_sources.py` | Геополитика (GDELT), CPI YoY, Fear & Greed, сырьё, глобальные рынки, Finnhub, Alpha Vantage | GDELT, Investing.com RSS, Finnhub, Alpha Vantage |
| `cot_data.py` | Commitments of Traders (COT) от CFTC — недельные данные | CFTC.gov (disagg + fin datasets, zip archives) |
| `etf_flows.py` | ETF flows (SPY, QQQ, GLD) + market breadth | Yahoo Finance chart API |
| `chart_generator.py` | Генерация изображений графиков | matplotlib / PIL |
| `pipeline.py` | Валидация сигналов, конвертация ideas → signals | — |
| `market_data.py` | OHLCV candles fetcher для всех символов | Binance klines API |
| `sentiment.py` | Новостной сентимент (Finnhub) | Finnhub news-sentiment API |
| `news_fetcher.py` | Новостной фетчер | Tavily / GDELT |

### Модули `core/` (бизнес-логика)

| Файл | Назначение | Ключевая логика |
|------|------------|-----------------|
| `regime_detector.py` | Определяет режим: UPTREND / SIDEWAYS / HIGH_VOL / DOWNTREND | MA50/MA200, ADX, ATR, RSI, Volume trend |
| `dynamic_risk.py` | Kelly Criterion + ATR-based position sizing | Kelly %, ATR стопы, drawdown protection, correlation penalty |
| `confluence.py` | Confluence Score (0-100) из 10 источников | Factor-weighted scoring: Regime 30%, RSI 20%, Whales 20%, Macro 15%, TF 15% |
| `whale_detector.py` | Whale order detection (Binance recent trades) | > $500k сделки, buy/sell объём, sentiment (BULLISH/NEUTRAL/BEARISH) |
| `correlation.py` | Correlation Matrix — блокирует дублирующие позиции | Pearson correlation на returns, threshold > 0.85 |
| `event_defense.py` | Event Defense: стоп торговли перед high-impact новостями | Regex триггеры: CRITICAL (rate hike, ban crypto, hack, war), HIGH (recession, sanctions), MEDIUM (volatility, inflation) |
| `screener.py` | Market Screener: TOP-20 монет на аномалии | Volume Spike > 200%, RSI extremes, Funding anomaly |
| `multi_tf.py` | Multi-timeframe analysis (1h, 4h, 1D) | MA alignment, RSI, Volume confirmation, alignment score |
| `data_enricher.py` | Обогащение candles техническими индикаторами | Funding Rate, OI, Liquidations, DXY/US10Y/SPX, Fear & Greed |
| `economic_calendar.py` | Календарь макро-событий (FOMC, CPI, NFP) | Keyword scan из новостей, 24h risk window |
| `digest_context.py` | Контекст прошлых дайджестов для сравнения | История дайджестов (последние 14) |
| `analysis_ideas_adapter.py` | Нормализация идей из отчётов в структуру | — |
| `signal.py` | Конвертация идей в Signal объекты | — |
| `decision_engine.py` | Сигнальный фильтр и ранжирование | Confidence + R/R ratio filtering |

### Модули `market_indicators/` (система баллов)

| Файл | Назначение | Данные |
|------|------------|--------|
| `onchain.py` | BTC MVRV, SOPR, Exchange Reserves, Active Addresses | CoinGecko API (free) |
| `macro_extended.py` | Fed Balance Sheet, QE/QT, Yield Curve, Credit Spreads | FRED API (WALCL, DGS10, DGS2, HYCD) |
| `scorer.py` | Market Score (0-100): макро + ончейн + техника + сентимент; стоп-факторы | MVRV>3.5, VIX>40, F&G<25, MVRV<1.0 |
| `aggregator.py` | Собирает всё → контекст для AI | build_enriched_context(), enrich_prices_with_scores() |

### Поддержка

| Файл | Назначение |
|------|------------|
| `scheduler.py` | Планировщик daily отчётов |
| `debate_storage.py` | Хранилище дебатов для листания |
| `learning.py` | Feedback система — улучшение моделей |
| `user_profile.py` | Профили пользователей |
| `alert_system.py` | Система алертов |
| `streamlit_app.py` | Web dashboard (PnL, trades, charts) |
| `weekly_report.py` | Еженедельный отчёт |
| `russia_agents.py` | Russia Edge — дополнительные агенты |
| `meta_analyst.py` | Мета-анализ точности прогнозов |
| `results_export.py` | Экспорт результатов бэктестов |
| `russia_data.py` | Данные для Russia Edge агентов |
| `prompt_versions.py` | Управление версиями промптов |
| `report_sanitizer.py` | Санитизация отчётов |
| `cpi_config.py` | Конфигурация CPI данных |
| `decision_engine.py` (root) | Risk/reward filtering для сигналов |

### Папки

| Папка | Назначение |
|-------|------------|
| `core/` | 15 модулей бизнес-логики |
| `market_indicators/` | 5 файлов on-chain + macro + scoring |
| `refactor/` | Рефакторинг: handlers/, interfaces/, prompts/, providers/ |
| `trading_system/` | CLI утилиты, batch runner, risk tools, dashboard |
| `scripts/` | run_quick_backtest.py — быстрый бэктест |

---

## 🔑 Словарь терминов

### Режимы рынка

| Режим | Описание | Поведение автотрейдера |
|-------|---------|----------------------|
| **UPTREND** | Бычий тренд, MA50 > MA200 | Порог открытия ниже, позиции крупнее |
| **SIDEWAYS** | Консолидация, низкая волатильность | Порог выше, entry уже, позиции меньше |
| **HIGH_VOL** | Высокая волатильность (>5%) | Порог +4×conf + volatility penalty, entry шире, позиция -50% |
| **DOWNTREND** | Медвежий тренд | Порог выше, только SHORT или CASH |

### Индикаторы

| Индикатор | Источник | Описание |
|-----------|----------|----------|
| **MVRV** | CoinGecko (估算) | Market/Realized Value — переоценён > 3.5, дно < 1.0 |
| **SOPR** | CoinGecko | Spent Output Profit Ratio — фиксация > 1.05 |
| **Funding Rate** | Bybit | > 0.1% = быки платят медведям |
| **OI (Open Interest)** | Bybit | Рост OI + рост цены = подтверждение тренда |
| **VIX** | Yahoo (`^VIX`) | > 40 = кризис, < 15 = крайний оптимизм |
| **QE / QT** | FRED (WALCL) | QE = ликвидность растёт (бычий), QT = уходит (медвежий) |
| **Yield Curve** | FRED (DGS10-DGS2) | Инверсия < -0.5% = рецессия risk |
| **HY Spread** | FRED (HYCD) | > 5% = стресс на рынке |
| **Fear & Greed** | Alternative.me | < 25 = экстремальный страх (бычий), > 75 = жадность |
| **COT (Commitments of Traders)** | CFTC | Позиции крупных спекулянтов |

### Стратегия выхода

| Механизм | Описание |
|----------|----------|
| **Split TP** | 50% позиции закрывается при +2% profit |
| **Trailing Stop** | Активируется при +3% profit, движется за ценой (1.5% буфер) |
| **Hard Stop** | SL по ATR × regime multiplier |
| **Signal Reversal** | Закрытие при смене сигнала на противоположный |

### AI Агенты (дебаты)

| Агент | Промпт | Задача |
|-------|--------|--------|
| **Bull** | `BULL_SYSTEM` | Найти бычьи аргументы с цифрами из данных |
| **Bear** | `BEAR_SYSTEM` | Найти медвежьи аргументы с цифрами |
| **Verifier** | `VERIFIER_SYSTEM` | Удалить галлюцинации (статистика без источника) |
| **Synth** | `SYNTH_SYSTEM` | Итоговый вердикт (compact JSON) + планы |
| **Speechwriter** | `SPEECHWRITER_SYSTEM` | Превращает JSON в читаемый торговый план |

---

## 📊 Как работает каждый цикл автотрейдера

```
КАЖДЫЕ 5 МИНУТ:

1. build_consensus()
   → Markets Signals (funding, OI, whales) → candidates
   → Дайджесты (digest_score, regime, volatility) → candidates
   → MVRV + Fed Balance + Yield → candidates
   → Итог: список отранжированных кандидатов

2. _close_position_if_needed()
   → Проверяет: TP hit? SL hit? Trailing stop? Partial TP?
   → Сохраняет в БД, пишет BACKTEST.md, шлёт алерт в Telegram

3. rank_trade_candidates()
   → _score_candidate() с адаптивными порогами
   → HIGH_VOL → threshold +4×conf + volatility penalty
   → SIDEWAYS → threshold +3×conf
   → UPTREND → threshold -2×conf

4. MVRV hard-stop
   → MVRV > 3.5 + LONG → пропускаем
   → MVRV < 1.0 + SHORT → пропускаем

5. Open positions (до 5)
   → quantity_pct safety check (< 1e-4 → skip)
   → Volatility > 5% → size × 0.5
   → R/R < 1.5 → skip
   → Correlation conflict → skip
   → Defense mode → skip

6. save_market_cache()
   → MARKET_CACHE.md на GitHub (20 мин TTL)
```

---

## 🌍 ETF, COT, Funding Rate — зачем они нужны

### ETF (Exchange-Traded Funds)

ETF — это фонд который торгуется как акция. Важные ETF для крипто-трейдинга:

| ETF | Тикер | Что показывает |
|-----|-------|---------------|
| S&P 500 | SPY / ^GSPC | Здоровье экономики США |
| Nasdaq 100 | QQQ / ^NDX | Технологический сектор |
| Золото | GLD | Safe haven / инфляция |
| Нефть | USO / CL=F | Сырьё / геополитика |
| 20+ лет облигации | TLT | Процентные ставки |
| High Yield облигации | HYG | Кредитный риск / стресс |
| Russell 2000 | IWM | Малые компании (риск-аппетит) |

**Как использовать:** когда SPY падает — крипта тоже падает (корреляция). Когда золото растёт — может означать risk-off. Когда TLT растёт — ставки падают (бычий для риска). ETF данные берём из Yahoo Finance.

`etf_flows.py` отслеживает: SPY, QQQ, IWM, GLD, SLV, USO, VWO, EFA, TLT, HYG. 5-дневные изменения объёма и цены → институциональные потоки.

### COT (Commitments of Traders)

COT — еженедельный отчёт от CFTC (Commodity Futures Trading Commission). Показывает позиции трейдеров на фьючерсных рынках.

| Группа | Кто | Что показывает |
|--------|-----|---------------|
| **Commercial** | Хеджеры (банки, компании) | Страхуют свой бизнес |
| **Non-Commercial** | Крупные спекулянты | Большие деньги ставят |
| **Non-Reportable** | Мелкие | Обычно против тренда |

`cot_data.py` загружает COT для: Bitcoin (fin dataset, код 133741), Gold (disagg, код 088691), Silver (084691), Crude Oil (067651), S&P 500 (fin, код 13874A), DXY (fin, код 098662), Euro (fin, код 099741). Данные берутся из zip-архивов CFTC (fut_disagg_txt_{year}.zip, fut_fin_txt_{year}.zip). Если крупные спекулянты NET SHORT — медвежий сигнал.

### Funding Rate (Bybit/Binance)

Funding rate — это плата которую трейдеры платят друг другу каждые 8 часов. Если ставка **положительная** — длинные платят коротим → быки агрессивны. Если **отрицательная** — наоборот.

| Funding | Значение | Сигнал |
|---------|----------|--------|
| > +0.1% | Быки доминируют | Вероятность сброса |
| < -0.1% | Медведи доминируют | Сжимание шортов |

`signals.py` получает funding rate с Binance Futures API. `core/data_enricher.py` обогащает контекст: funding rate + статус (Overheated/Normal/Negative).

### Open Interest (OI)

Open Interest — общее количество открытых фьючерсных контрактов. Рост OI + рост цены = подтверждение тренда. Рост OI + падение цены = дивергенция (пузырь). `signals.py` получает OI с Binance Futures `/fapi/v1/openInterest`. `core/data_enricher.py` показывает recent liquidations (longs vs shorts).

### Global Markets Breadth

`data_sources.py` сканирует мировые индексы: Nikkei 225, Hang Seng, FTSE 100, DAX, RTS (Россия). Если большинство в зелёном — глобальный риск-аппетит позитивный. Если красные — бегство от риска.

---

## 🔗 Взаимосвязь файлов (dependency graph)

```
ДАННЫЕ → ПРЕДПРОЦЕССИНГ → СИСТЕМА БАЛЛОВ → AI АГЕНТЫ → ВЕРДИКТ
```

### Уровень 1: Сырые данные (real-time, бесплатные API)

| Источник | Данные | Файл | Период |
|----------|--------|------|--------|
| **Binance** | Цены BTC/ETH/SOL, объёмы, funding rate, OI | `signals.py`, `web_search.py` | 15 мин |
| **Yahoo Finance** | SPX, NDX, VIX, DXY, GOLD, нефть, медь, газ | `web_search.py` | real-time |
| **Alternative.me** | Fear & Greed Index | `web_search.py`, `data_sources.py` | daily |
| **CoinGecko** | MVRV, SOPR, Exchange Reserves, Active Addresses | `market_indicators/onchain.py` | 10 мин |
| **FRED (St. Louis Fed)** | Fed Rate, CPI, Fed Balance, 10Y/2Y Yield, HY Spread | `market_indicators/macro_extended.py` | real-time |
| **CFTC** | COT: позиции хеджеров и спекулянтов (BTC, Gold, Oil) | `cot_data.py` | weekly |
| **GDELT** | Геополитические события (война, санкции, торговля) | `data_sources.py` | 24h |
| **Finnhub** | Новостной сентимент, earnings calendar, insider trades | `data_sources.py` | real-time |
| **Alpha Vantage** | RSI, MACD (BTC, SPY) | `data_sources.py` | daily |
| **ETF.com/Yahoo** | SPY, QQQ, GLD, TLT, HYG объёмы и изменения | `etf_flows.py` | 5d |
| **Etherscan** | ETH Gas Price (Gwei) | `data_sources.py` | real-time |
| **Blockchain.info** | BTC транзакции, hash rate, mempool | `data_sources.py` | real-time |
| **CoinGecko Trends** | Trending крипта | `data_sources.py` | daily |
| **Investing.com RSS** | Экономический календарь (FOMC, CPI, NFP) | `data_sources.py` | daily |
| **SEC / OpenInsider** | Инсайдерские сделки | `data_sources.py` | weekly |

### Уровень 2: Обогащение и агрегация

| Модуль | Что делает | Выход |
|--------|-----------|-------|
| `market_indicators/onchain.py` | MVRV, SOPR → сигнальныйInterpretation | "ПЕРЕОЦЕНЁН", "ИСТОРИЧЕСКОЕ ДНО", "HODLing" |
| `market_indicators/macro_extended.py` | Fed Balance → QE/QT режим, Yield → рецессия risk | "QE", "QT", "Инверсия кривой" |
| `market_indicators/scorer.py` | Все метрики → единый Market Score (0-100) | Баллы + стоп-факторы |
| `market_indicators/aggregator.py` | Объединяет on-chain + macro + prices → контекст для AI | `EnrichedData` dataclass |
| `core/data_enricher.py` | Funding, OI, Liquidations → derivatives контекст | "Overheated", "Normal", "Negative" |
| `core/confluence.py` | 5 факторов → Confluence Score (0-100) | "STRONG BUY", "SELL", "NEUTRAL" |
| `core/regime_detector.py` | MA50/MA200, ADX, ATR, RSI → режим | "UPTREND", "HIGH_VOL", "DOWNTREND" |

### Уровень 3: Система баллов (scorer.py)

Каждый индикатор получает **баллы**:

```
total_score = macro_score + onchain_score + technical_score + sentiment_score

БЫЧЬИ сигналы:
  • VIX < 15              → +2
  • Fed Rate ↓            → +2
  • QE mode               → +2
  • MVRV < 1.0            → +3
  • RSI < 35              → +2
  • F&G < 25              → +2
  • Whale buy pressure >70% → +2

МЕДВЕЖИЙ сигналы:
  • VIX > 30              → -2
  • QT mode               → -2
  • Yield инверсия        → -2
  • MVRV > 3.5            → -3
  • RSI > 75              → -2
  • F&G > 70 (пузырь)     → -2
```

**Стоп-факторы** (автоматический вердикт):
- MVRV > 3.5 → 🚨 МЕДВЕЖИЙ СТОП (не открывать LONG)
- MVRV < 1.0 → 🔵 БЫЧИЙ СТОП (не открывать SHORT)
- VIX > 40 → 🚨 КРИЗИС
- VIX < 15 + F&G > 70 → 🚨 ПУЗЫРЬ

### Уровень 4: AI-агенты (дебаты)

```
Bull Agent → находит бычьи аргументы из данных
Bear Agent → находит медвежьи аргументы из данных  
Verifier   → удаляет галлюцинации
Synth      → итоговый вердикт (compact JSON)
Speechwriter → форматирует JSON в читаемый план

Каждый агент получает:
  1. Рыночные цены (web_search.py)
  2. Макро данные (FRED)
  3. Ончейн метрики (CoinGecko)
  4. СИГНАЛЫ ДЛЯ ДЕБАТОВ (scorer.py — структурированный блок)
  5. СИСТЕМА БАЛЛОВ (scorer.py — категории)
```

### Уровень 5: Автотрейдер (signal_trader.py)

```
Цикл 5 мин:
  1. build_consensus()
       → Markets Signals (funding, OI, whales)      [signals.py]
       → AI Digest вердикт                           [github_export.py]
       → MVRV + Fed Balance + Yield                 [market_indicators/]
  2. _close_position_if_needed()
       → Trailing stop (активируется +3%), Split TP (+2%)
  3. rank_trade_candidates()
       → Адаптивные пороги (ChatGPT):
         HIGH_VOL  → threshold +4×conf + vol penalty
         SIDEWAYS  → threshold +3×conf
         UPTREND   → threshold -2×conf
  4. MVRV hard-stop
       → MVRV>3.5 блокирует LONG
       → MVRV<1.0 блокирует SHORT
  5. Открытие позиций
       → Max 5, Kelly sizing, ATR stop, correlation check
  6. Telegram алерты
       → MVRV>4, MVRV<1, Defense Mode, QE→QT, Score±8
```

### Ключевые файлы данных

| Файл | Роль в системе |
|------|---------------|
| `signals.py` | Bybit/Binance данные для UI и автотрейдера |
| `cot_data.py` | COT — позиции хеджеров vs спекулянтов (weekly) |
| `etf_flows.py` | ETF — институциональные потоки (SPY, QQQ, GLD, TLT) |
| `web_search.py` | Цены, Fear & Greed, макро FRED |
| `market_indicators/onchain.py` | MVRV, SOPR, Reserves — фундаментальные метрики |
| `market_indicators/macro_extended.py` | QE/QT, Yield, Credit Spreads — макро режим |
| `data_sources.py` | Геополитика, Finnhub, Alpha Vantage, commodities |

---

## 🔗 Взаимосвязь файлов (dependency graph)

```
main.py
  ├── analysis_service.py
  │     ├── agents.py (DebateOrchestrator)
  │     │     └── ai_provider.py
  │     ├── web_search.py (get_full_realtime_context)
  │     ├── market_indicators/ (build_enriched_context)
  │     │     ├── onchain.py (fetch_btc_onchain)
  │     │     ├── macro_extended.py (fetch_extended_macro)
  │     │     └── scorer.py (calculate_market_score)
  │     └── data_sources.py
  │
  ├── signal_trader.py
  │     ├── signals.py (build_signal_bias_map)
  │     ├── core/regime_detector.py
  │     ├── core/whale_detector.py
  │     ├── core/correlation.py
  │     ├── core/event_defense.py
  │     └── github_export.py (MARKET_CACHE.md)
  │
  ├── database.py
  │     ├── init_db()
  │     ├── get_backtest_signals()
  │     └── append_trade_decision_log()
  │
  └── scheduler.py
        └── github_export.py (DIGEST_CACHE.md, FORECASTS.md)
```

---

## ⚙️ Адаптивные параметры автотрейдера

| Параметр | Базовое | UPTREND | SIDEWAYS | HIGH_VOL |
|----------|---------|---------|----------|----------|
| `OPEN_SCORE_THRESHOLD` | 12.0 | 10.4 | 16.5 | 18+ |
| `ENTRY_TOLERANCE_PCT` | 2% | 2% | 1.2% | 3% |
| Позиция при vol>5% | 100% | 100% | 100% | 50% |

---

## 📦 GitHub файлы (автообновляемые)

| Файл | Обновляется | Содержит |
|------|-------------|----------|
| `BACKTEST.md` | Каждая закрытая сделка | История P&L |
| `FORECASTS.md` | После /daily | Track record всех прогнозов |
| `DIGEST_CACHE.md` | После /daily | Последние 14 дайджестов |
| `MARKET_CACHE.md` | Каждый цикл | MVRV, QE/QT, VIX, Score |

---

## 🇬🇧 ENGLISH VERSION

### Dialectic Edge — AI Trading System

**What it does:**
1. **Analyzes market** via multi-agent AI debates
2. **Monitors** Bybit Markets Signals (funding, OI, whales)
3. **Adapts** to market regime (UPTREND / SIDEWAYS / HIGH_VOL)
4. **Manages risk** via Kelly Criterion, ATR, Trailing Stop, Split TP
5. **Paper trades** on Railway with GitHub state persistence

### Architecture

```
User → main.py (Telegram)
         ↓
  analysis_service.py
         ↓
  agents.py (Bull/Bear/Verifier/Synth)
         ↓
  ai_provider.py (Cerebras/Groq/Mistral/OpenRouter/Together/Gemini)
         ↓
  web_search.py (Binance/Yahoo/FRED/CoinGecko/GDELT)
         ↓
  market_indicators/ (on-chain + macro + scoring)
         ↓
  signal_trader.py (auto-trader, 5-min cycle)
         ↓
  GitHub (BACKTEST.md / FORECASTS.md / MARKET_CACHE.md)
```

### Key Features

- **10 elite core modules** (regime, risk, confluence, whale, correlation, etc.)
- **100% free AI models** (Cerebras, Groq, Mistral, OpenRouter, Together, Gemini)
- **Adaptive thresholds** based on market regime + volatility
- **Trailing stop** + **Split TP** for optimal exits
- **MVRV hard-stops** (no LONG when >3.5, no SHORT when <1.0)
- **Event Defense** blocks trades before high-impact macro events
- **GitHub persistence** survives Railway redeployments
- **Telegram alerts** on MVRV, Defense Mode, QE/QT changes

### Setup

```bash
cp .env.example .env
# fill in API keys
python main.py
```

### Commands

| Command | Description |
|---------|-------------|
| `/daily` | Full AI market analysis |
| `/starttrade` | Start autotrader |
| `/screener` | Anomaly scanner TOP-15 |
| `/health` | Health check |

---

## 📜 Disclaimer

⚠️ **This is a paper trading / educational project.**  
All trades are simulated. Nothing here is financial advice.  
Past performance does not guarantee future results.  
DYOR (Do Your Own Research).
