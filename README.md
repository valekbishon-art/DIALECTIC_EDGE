# Dialectic Edge — AI Trading System

> Автономная AI-система анализа крипто/макро-рынка на **smart-money signals + multi-agent debate + adaptive Kelly + vol-targeting + self-audit**. Не retail-sentiment, как у конкурентов.

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/) [![Frontend](https://img.shields.io/badge/Frontend-Telegram%20(aiogram%203)-blue)](https://telegram.org/) [![Deploy](https://img.shields.io/badge/Deploy-Railway-purple)](https://railway.app/) [![Tests](https://img.shields.io/badge/Tests-74%20suites-success)]() [![License](https://img.shields.io/badge/License-Private-red)]() [![Status](https://img.shields.io/badge/Status-Production-success)]()

> **Ветка `spot-only`.** Это рабочая ветка проекта. Документ описывает фактическое состояние кода в этой ветке.

---

## 📑 Содержание

- [Что это](#-что-это)
- [Quick start](#-quick-start)
- [Команды Telegram](#-команды-telegram)
- [Архитектура](#-архитектура)
- [Поток данных (5 уровней)](#-поток-данных-5-уровней)
- [Автотрейдер](#-автотрейдер-signal_traderpy)
- [Advisor (планы сделок)](#-advisor--портфель-планов)
- [P2P-арбитраж](#-p2p-арбитраж-сканер)
- [Killer-фичи за фичефлагами](#-killer-фичи-за-фичефлагами)
- [Alert-движок и авто-алерты](#-alert-движок-и-авто-алерты)
- [Подписки и пейволл](#-подписки-и-пейволл)
- [Структура репозитория](#-структура-репозитория)
- [Tech stack](#-tech-stack)
- [Хранение состояния](#-хранение-состояния)
- [Конфигурация (env)](#-конфигурация-env)
- [Деплой](#-деплой)
- [Разработка, тесты, CI](#-разработка-тесты-ci)
- [Словарь терминов](#-словарь-терминов)
- [Disclaimer](#-disclaimer)

---

## 🎯 Что это

**Dialectic Edge** — single-tenant Telegram-бот (один владелец = один инстанс), который работает по принципам системного фонда, а не retail-трейдера:

| Слой | Источники / методы |
|------|--------------------|
| **🏛️ Smart-money signals** | Top-trader L/S ratio, Coinbase premium, CME basis, funding dispersion, on-chain ETH-потоки институциональных кошельков |
| **📊 Multi-agent AI debate** | 5 ролей (Bull / Bear / Verifier / Synth / Speechwriter), каждая на оптимальной модели, маршрутизация через мульти-провайдерный роутер |
| **⚖️ Adaptive risk** | Vol-targeting (CTA-стиль) + dynamic Kelly на реальном win-rate, persistent state в `risk_state.json` / `sizing_state.json` |
| **🛡️ Macro regime** | S&P EMA200/SMA50, breadth, DXY, VIX, QE/QT, yield curve → блокирует сделки против тренда |
| **🔄 On-chain** | MVRV, SOPR, Exchange Reserves, Whale Detection, stablecoin supply flows |
| **🔍 AI self-audit** | LLM пишет performance-review закрытых сделок и выдаёт правило на неделю (`/audit`) |
| **📡 Signal trader** | Бумажный автотрейдер: цикл по таймеру, vol-target sizing, ATR-стопы, Split TP, trailing, anti-whiplash |
| **🧮 Quant-слой** | Калибровка агентов (Brier), walk-forward бэктест, BOCPD/Markov regime, support/resistance, volatility forecast |

**Pitch:** *«мы — vol-targeted CTA-фонд + Kelly на реальных метриках, а не retail-трейдер с фиксированными 2% риска на сделку».*

---

## 🚀 Quick start

```bash
# 1. Python 3.12 (важно: НЕ 3.13 — см. nixpacks.toml / runtime.txt)
python --version   # 3.12.x

# 2. Зависимости
pip install -r requirements.txt

# 3. Конфиг
cp .env.example .env
# заполни как минимум BOT_TOKEN и ADMIN_IDS, плюс хотя бы один AI-ключ
# (GEMINI_API_KEY / GROQ_API_KEY / ...). Остальное опционально.

# 4. Запуск
python main.py
```

Точка входа — `async def main()` в нижней части `main.py`. Она поднимает `dp.start_polling(bot)` параллельно с `Scheduler` и (если `FEATURE_AUTOTRADE=1`) циклом автотрейдера.

Минимум для старта: `BOT_TOKEN`, `ADMIN_IDS` и один AI-ключ. Без `TAVILY_API_KEY`/`FRED_API_KEY`/`FINNHUB_API_KEY` соответствующие источники просто деградируют на fallback.

---

## 💬 Команды Telegram

### Анализ и рынок
| Команда | Описание |
|---------|----------|
| `/daily` | Полный AI-анализ рынка: дебаты + smart-money + торговый план (кэш по TTL) |
| `/analyze <текст>` | Анализ конкретной новости/тезиса через дебаты |
| `/markets`, `/market` | Real-time контекст: цены, сигналы, режим, scoring |
| `/btc`, `/bitcoin` | BTC outlook: вердикт + confidence |
| `/screener` | Сканер аномалий TOP-монет (volume spike, RSI extremes, funding) |
| `/signal`, `/signals` | Текущие сигналы Bybit/Binance (funding, OI, L/S, whales) |
| `/trend` | Трендовые сигналы |
| `/stocks` | Скринер акций |
| `/why <SYMBOL>` | Почему открыта позиция: входной отчёт + текущее состояние |
| `/pump` | Памп-радар (факт движения, НЕ торговый сигнал) |
| `/depeg` | Монитор депега стейблов |

### Торговля и план
| Команда | Описание |
|---------|----------|
| `/starttrade` | Запуск бумажного автотрейдера |
| `/stop` | Остановка автотрейдера |
| `/papertrader` | Статус бумажного трейдера |
| `/autotrade_status` | PnL, win-rate, R-ratio, Kelly, vol-target, drawdown |
| `/autotrade_reset` | Сброс состояния автотрейда |
| `/close <SYMBOL>` | Закрыть позицию вручную |
| `/portfolio` | Портфель позиций |
| `/advise [ASSET] [CAPITAL]` | Конкретный план: вход / стоп / split TP / размер |
| `/myplans` | Виртуальный портфель advisor-планов с live-PnL |
| `/plan` | Текущий торговый план |
| `/dca` | DCA-помощник |

### Бэктест и аналитика
| Команда | Описание |
|---------|----------|
| `/backtest`, `/backtest_capital`, `/backtest_toggle`, `/backtest_clear` | Управление бэктестом |
| `/wfbacktest` | Walk-forward бэктест |
| `/calibration` | Калибровка прогнозов агентов (Brier) |
| `/edge` | Edge ledger (журнал эджа) |
| `/eval` | Оценка точности прогнозов |
| `/provenance` | Происхождение данных в отчёте |
| `/audit [N]` | AI-аудит закрытых сделок за N дней |
| `/postmortem [id\|date]` | Разбор каскадных ликвидаций |
| `/trackrecord`, `/trackrecordglobal`, `/trackrecordrussia` | Track record прогнозов |
| `/weeklyreport` | Еженедельный отчёт |
| `/usage` | Расход AI-токенов по провайдерам |
| `/retro` | Ретро-анализ |

### P2P / арбитраж
| Команда | Описание |
|---------|----------|
| `/p2p [ASSET] [FIAT] [payments]`, `/p2parb` | P2P-арбитраж: net spread, лимиты, payment overlap, риск контрагента |
| `/p2paudit` | Self-audit журнал P2P-окон + рекомендация по порогу |

### Подписки и сервис
| Команда | Описание |
|---------|----------|
| `/subscribe`, `/premium`, `/vipinfo` | Подписка (CryptoBot Crypto Pay) |
| `/profile`, `/newbie` | Профиль пользователя / гайд новичка |
| `/russia` | Russia Edge — доп. агенты и данные |
| `/pitch` | Питч системы |
| `/health` | Health check (БД + GitHub + uptime) |
| `/stats`, `/status`, `/sysinfo`, `/logs` | Статистика и диагностика |
| `/alerts` | Управление алертами |
| `/start`, `/help` | Старт / справка |

### Админ
`/admin`, `/ban`, `/unban`, `/revoke`, `/add`, `/remove`, `/instruction` — управление доступом и инстансом (только `ADMIN_IDS`).

> Часть хендлеров (advisor, btc, p2p, retro, подписки) уже вынесена в `refactor/handlers/*` и регистрируется через `register(dp)`. Остальные ~70 `@dp.message(...)` пока живут в `main.py` (идёт миграция).

---

## 🏗️ Архитектура

```
                         ПОЛЬЗОВАТЕЛЬ (Telegram)
                                  │
                                  ▼
                            ┌───────────┐
                            │  main.py  │  ~7274 строк — bootstrap + хендлеры (god-object)
                            └─────┬─────┘
            ┌─────────────────────┼───────────────────────┐
            ▼                     ▼                         ▼
   ┌─────────────────┐   ┌────────────────┐        ┌───────────────┐
   │ analysis_service│   │ signal_trader  │        │   scheduler   │
   │   (/daily)      │   │  (автотрейдер) │        │ (cron-задачи) │
   └───────┬─────────┘   └───────┬────────┘        └───────┬───────┘
           ▼                     │                         │
   ┌─────────────────┐           │                         │
   │    agents.py    │  Bull / Bear / Verifier / Synth / Speechwriter
   └───────┬─────────┘           │                         │
           ▼                     │                         │
   ┌─────────────────┐           │                         │
   │  ai_provider.py │  Cerebras → Groq → Mistral → OpenRouter → Together → Gemini → local
   └───────┬─────────┘           │                         │
           ▼                     ▼                         ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  ДАННЫЕ: web_search.py · data_sources.py · signals.py ·        │
   │  cot_data.py · etf_flows.py · market_data.py · news_fetcher.py │
   │  market_indicators/ (onchain · macro · scorer · aggregator)    │
   └──────────────────────────────────────────────────────────────┘
           │                     │                         │
           ▼                     ▼                         ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  STATE: SQLite (dialectic_edge.db) + risk_state.json +        │
   │  sizing_state.json + git-markdown (DIGEST_CACHE / FORECASTS /  │
   │  AUTO_TRACK / BACKTEST / MARKET_CACHE) + опц. Postgres/Redis    │
   └──────────────────────────────────────────────────────────────┘
           │
           ▼
   📡 TELEGRAM АЛЕРТЫ (MVRV, Defense Mode, QE→QT, Score±8, открытие/закрытие)
```

---

## 🔗 Поток данных (5 уровней)

```
СЫРЫЕ ДАННЫЕ → ОБОГАЩЕНИЕ → СИСТЕМА БАЛЛОВ → AI-ДЕБАТЫ → ВЕРДИКТ/СДЕЛКА
```

**Уровень 1 — сырые данные (публичные/free API):** Binance (цены, объёмы, funding, OI), Yahoo Finance (SPX/NDX/VIX/DXY/GOLD/нефть), Alternative.me (Fear & Greed), CoinGecko (MVRV/SOPR/reserves), FRED (Fed rate, CPI, баланс ФРС, кривая, HY spread), CFTC (COT), GDELT (геополитика), Finnhub (sentiment), Alpha Vantage (RSI/MACD), Etherscan/Tron (stablecoin supply, smart-money кошельки), Deribit (IV/skew), Tavily (новости).

**Уровень 2 — обогащение:** `market_indicators/onchain.py`, `macro_extended.py`, `core/data_enricher.py`, `core/confluence.py`, `core/regime_detector.py` → derivatives-контекст, режим рынка, confluence score.

**Уровень 3 — система баллов:** `market_indicators/scorer.py` собирает macro + onchain + technical + sentiment в единый Market Score (0–100) и считает стоп-факторы (MVRV>3.5, VIX>40, F&G<25, MVRV<1.0). `aggregator.py` упаковывает всё в контекст для AI.

**Уровень 4 — AI-дебаты:** Bull → бычьи аргументы, Bear → медвежьи, Verifier → удаляет галлюцинации, Synth → итоговый compact-JSON вердикт, Speechwriter → читаемый план.

**Уровень 5 — автотрейдер:** `signal_trader.py` строит consensus из сигналов + дайджестов + macro, применяет адаптивные пороги, MVRV hard-stop, vol-target sizing, открывает/закрывает позиции, шлёт алерты.

---

## 📡 Автотрейдер (`signal_trader.py`)

Бумажный (paper) трейдер, цикл по `AUTOTRADE_INTERVAL_SEC`. Логика каждого тика:

1. **build_consensus()** — Markets Signals (funding/OI/whales) + AI-дайджест + MVRV/Fed/Yield → ранжированные кандидаты.
2. **_close_position_if_needed()** — самая горячая функция: проверка TP/SL, Split TP (+2%), Trailing (активация +3%, буфер 1.5%), signal reversal. Пишет `BACKTEST.md`, шлёт алерт.
3. **rank_trade_candidates()** — адаптивные пороги по режиму: HIGH_VOL → порог +4×conf + vol penalty; SIDEWAYS → +3×conf; UPTREND → −2×conf.
4. **MVRV hard-stop** — MVRV>3.5 блокирует LONG, MVRV<1.0 блокирует SHORT.
5. **Open positions** — Kelly + vol-target sizing, ATR-стоп, correlation check, defense mode, R/R≥1.5.
6. **save_market_cache()** — `MARKET_CACHE.md` (TTL).

**Anti-whiplash защита капитала:** `AUTOTRADE_MIN_HOLD_MINUTES`, `AUTOTRADE_REVERSAL_STRENGTH_DELTA`, `AUTOTRADE_REENTRY_COOLDOWN_MIN`. Состояние Kelly/sizing персистится между рестартами.

| Параметр | Базовое | UPTREND | SIDEWAYS | HIGH_VOL |
|----------|---------|---------|----------|----------|
| `OPEN_SCORE_THRESHOLD` | 12.0 | 10.4 | 16.5 | 18+ |
| `ENTRY_TOLERANCE_PCT` | 2% | 2% | 1.2% | 3% |
| Размер при vol>5% | 100% | 100% | 100% | 50% |

---

## 🧭 Advisor — портфель планов

- **`/advise [ASSET] [CAPITAL]`** (`core/advisor.py`) — pure-logic (без LLM) план: вход, стоп, split TP 30/40/30, размер под капитал. BTC outlook работает как veto для альтов при конфликте и confidence ≥ 65.
- **AI-narrative** (`core/advisor_narrative.py`, флаг `FEATURE_ADVISOR_NARRATIVE`) — кнопка «💬 Объяснить» зовёт LLM и кэширует объяснение.
- **Виртуальный портфель** (`FEATURE_ADVISOR_PORTFOLIO`) — кнопка «📥 В портфель», watcher в scheduler следит за SL/TP и шлёт алерт при закрытии. `/myplans` — открытые позиции с live-PnL. Реальные деньги не двигаются.

---

## 💱 P2P-арбитраж (сканер)

`p2p_arbitrage.py` + `refactor/handlers/p2p_arbitrage_handler.py`. Сканирует публичные P2P-стаканы **Binance и Bybit** (read-only), считает net spread после buffer/fee-модели, фильтрует по качеству контрагента и payment overlap.

- Включён по умолчанию (`FEATURE_P2P_ARBITRAGE=1`) — осознанное отступление от правила «фичи OFF по умолчанию».
- Глобальное покрытие: ~10 активов × ~55 фиатов (CIS / LATAM / ASIA / MENA / AFRICA / EUROPE / majors).
- Защита от фейков: внешний forex-anchor (open.er-api.com), outlier-band, hard-cap на спред, price-aware dedup.
- Risk scoring по completion rate / orders / account age / TIER-1 банкам.
- **Self-audit** (`p2p_audit.py`, `FEATURE_P2P_SELF_AUDIT`): логирует показанные окна, через N минут перечитывает стакан и сверяет realised vs shown spread → `/p2paudit` + рекомендация по `P2P_ARBITRAGE_MIN_SPREAD_PCT`.
- Авто-алерты прибыльных окон (`FEATURE_P2P_ARBITRAGE_ALERTS`).

Реальную сделку юзер делает руками после проверки мерчанта/банка/лимитов.

---

## 🧪 Killer-фичи за фичефлагами

Все по умолчанию **OFF** (кроме помеченных). Включаются через env, требуют 1–6 недель накопления baseline. Каждая пишет снапшоты в SQLite.

| Флаг | Модуль | Что делает |
|------|--------|-----------|
| `FEATURE_AGENT_CALIBRATION` | `core/agent_calibration*.py` | Per-agent probabilistic forecast'ы → Brier score → калибровка каждого голоса |
| `FEATURE_MICROSTRUCTURE` | `market_indicators/microstructure*.py` | L2-стаканы Binance/Bybit/OKX/Bitget/Hyperliquid → depth, asymmetry, spread, liquidity vacuum |
| `FEATURE_NARRATIVE_DRIFT` | `market_indicators/narratives*.py` | Эмбеддинги новостей (Gemini/Mistral) → онлайн-кластеризация → velocity/reach/drift |
| `FEATURE_FUNDING_TERM` | (funding term snapshots) | Perp funding + 30d/90d basis carry → slope, contango↔backwardation inversion |
| `FEATURE_CARRY_BRIEFING` *(ON)* | `CARRY_SCANNER.md` логика | Мониторинг carry/арб-позиций (быстрый тик) + полный брифинг (6ч) |
| `FEATURE_PUMP_SCANNER` | (pump radar) | Радар движений (честно: НЕ эдж, только факт) |
| `FEATURE_OPTIONS_SKEW` | (Deribit) | ATM IV, 25Δ risk reversal, term slope (Black-Scholes без r) |
| `FEATURE_STABLECOIN_FLOWS` | `market_indicators/stablecoin_flows*.py` | totalSupply USDT/USDC (Ethereum+Tron) → mint/redeem классификация |
| `FEATURE_REGIME_CLASSIFIER` | `market_indicators/regime*.py`, `core/markov_regime.py` | BOCPD (Adams & MacKay) над BTC log-returns → trending/ranging/volatile/crisis |
| `FEATURE_SMART_MONEY_WALLETS` | `market_indicators/smart_money_wallets*.py` | Net ETH flow по институциональным кошелькам (Etherscan v2) |
| `FEATURE_LIQUIDATION_MAGNET` | (Binance/Bybit OI+L/S) | Контрарианский «магнит» к ценам массовой ликвидации |
| `FEATURE_CASCADE_POST_MORTEM` | `core/post_mortem.py` | WS-listener ликвидаций → авто-разбор каскадов в TG → `/postmortem` |
| `FEATURE_ADVISOR` *(ON)* | `core/advisor.py` | План сделки `/advise` |
| `FEATURE_BTC_OUTLOOK_ALERTS` *(ON)* | `core/btc_alerts.py`, `core/btc_outlook.py` | Авто-алерты при смене lean / скачке confidence |

---

## 🔔 Alert-движок и авто-алерты

`refactor/services/alert_engine.py` (`FEATURE_ALERT_ENGINE`) — generic-фреймворк правил по расписанию, cooldown через `JsonAlertStore`:

- **screener anomaly** (`FEATURE_ALERT_SCREENER`) — RSI extremes + volume spikes + funding anomalies.
- **BTC ETF outflow** (`FEATURE_ALERT_BTC_ETF`) — IBIT/FBTC/BITB/ARKB/BTCO basket, streak/big-drop детект.
- **liquidation magnet** (`FEATURE_ALERT_LIQUIDATION_CLUSTER`) — OI buildup + L/S extreme.

Отдельно: P2P-алерты, BTC-outlook-алерты, депег-монитор (`depeg_monitor.py`).

---

## 💳 Подписки и пейволл

`payments/` (CryptoBot Crypto Pay) + `refactor/handlers/subscription_handler.py` + `refactor/middleware/subscription_guard.py`. Без `CRYPTOBOT_API_TOKEN` платёжка отключена и пейволл не работает. Опциональный VIP-кеш дайджестов — через `DATABASE_URL` (Postgres). Rate-limiter — `refactor/middleware/rate_limiter.py`.

---

## 📁 Структура репозитория

```
.
├── main.py                 # bootstrap + ~70 @dp.message хендлеров (god-object, ~7274 строк)
├── analysis_service.py     # pipeline /daily: данные → дебаты → план
├── agents.py               # multi-agent debate (Bull/Bear/Verifier/Synth/Speechwriter)
├── ai_provider.py          # роутер LLM-провайдеров с fallback
├── signal_trader.py        # бумажный автотрейдер (vol-target, ATR, Split TP, trailing)
├── signals.py              # Bybit/Binance: funding, OI, top-trader L/S, whales
├── web_search.py           # сборщик: Yahoo, FRED, CoinGecko, GDELT, Tavily
├── data_sources.py         # геополитика, Finnhub, Alpha Vantage, commodities, breadth
├── cot_data.py             # CFTC Commitments of Traders (weekly)
├── etf_flows.py            # ETF потоки + market breadth
├── market_data.py          # OHLCV свечи (Binance klines)
├── news_fetcher.py         # новости (Tavily/GDELT)
├── sentiment.py            # новостной сентимент (Finnhub / FinBERT)
├── database.py             # SQLite-обёртка (позиции, trade_decision_log, digest)
├── session_manager.py      # persistence виртуального капитала
├── github_export.py        # экспорт md-кэшей в git через GitHub API
├── scheduler.py            # cron: daily digest, audit, carry, watchers
├── chart_generator.py      # графики (matplotlib)
├── backtester.py           # бэктест на OHLC (заготовка)
├── depeg_monitor.py        # монитор депега стейблов
├── p2p_arbitrage.py        # P2P-сканер (Binance + Bybit)
├── p2p_audit.py / p2p_audit_io.py  # self-audit P2P
├── halal_edge.py / halal_signals.py / halal_alerts.py / halal_strategies  # Halal Edge
├── russia_agents.py / russia_data.py                                       # Russia Edge
├── stock_screener.py / trend_signals.py / trading_signal.py / quant_filter.py
│
├── core/                   # ~50 модулей бизнес-логики
│   ├── advisor.py / advisor_narrative.py      # планы сделок
│   ├── dynamic_risk.py / sizing_state.py / position_calc.py   # Kelly + vol-target
│   ├── regime_detector.py / markov_regime.py / macro_regime.py / regime_radar.py
│   ├── confluence.py / correlation.py / multi_tf.py / support_resistance.py
│   ├── whale_detector.py / event_defense.py / screener.py / decision_engine.py
│   ├── agent_calibration*.py / calibration*.py / recalibration.py / ai_metrics.py
│   ├── walk_forward.py / backtest_engine.py / backtest_validate.py / volatility_forecast.py
│   ├── btc_outlook.py / btc_regime.py / btc_alerts.py / horizons.py
│   ├── post_mortem.py / edge_ledger.py / provenance.py / retro_analysis.py
│   ├── track_record.py / market_complexity.py / economic_calendar.py / digest_context.py
│   └── healthz.py / audit.py / signal.py / signal_scorer.py / data_enricher.py
│
├── market_indicators/      # on-chain + macro + scoring + killer-фичи I/O
│   ├── onchain.py / macro_extended.py / scorer.py / aggregator.py / smart_money.py
│   ├── microstructure*.py / narratives*.py / regime*.py
│   ├── stablecoin_flows*.py / smart_money_wallets*.py / btc_etf_flows.py / fiat_fx.py
│
├── refactor/               # целевая модульная архитектура
│   ├── handlers/           # advisor, btc, p2p, retro, market, portfolio, profile, admin, subscription
│   ├── providers/          # ai / cache / database / market / news / storage / advisor_storage
│   ├── interfaces/ models.py utils.py examples.py
│   ├── middleware/         # rate_limiter, subscription_guard
│   ├── observability/      # logging_setup, sentry_setup
│   ├── prompts/            # market, russia
│   └── services/           # alert_engine + alert_rules (screener_anomaly, btc_etf_outflow) + alert_store_json
│
├── payments/               # CryptoBot Crypto Pay (crypto_pay.py, db.py)
├── trading_system/         # CLI + dashboard + risk + batch_runner + equity_metrics
├── scripts/                # fetch_klines, run_quick_backtest, spot_arb_scanner, preflight_guards и др.
├── research/               # halal_edge_backtest, predict_regime_backtest
├── docs/                   # BEGINNER_GUIDE, BACKTEST_RESULTS, quant_research
├── tests/                  # 74 файла тестов (unittest), + fixtures
├── config.py               # центральная конфигурация (env)
├── config/trading_config.json
│
├── .github/workflows/      # lint.yml, tests.yml, digest_cron.yml
├── pyproject.toml          # ruff (soft E/F), mypy (soft), pytest (asyncio strict)
├── .pre-commit-config.yaml
├── requirements.txt        # aiogram 3.13.1, aiohttp, aiosqlite, matplotlib, ...
├── runtime.txt / .python-version / nixpacks.toml   # Python 3.12
│
└── *.md (state в git — НЕ править руками):
    DIGEST_CACHE.md · FORECASTS.md · AUTO_TRACK.md · BACKTEST.md · MARKET_CACHE.md · AUDIT_REPORT.md
```

---

## 🧰 Tech stack

| Слой | Технология |
|------|-----------|
| Backend | Python 3.12, asyncio, aiohttp, aiosqlite |
| Frontend | Telegram Bot API (aiogram 3.13.1) |
| AI | Gemini · Groq · Mistral · OpenRouter · Together · Cerebras · локальные (Ollama / OpenAI-compat) |
| ML/NLP | FinBERT (sentiment), эмбеддинги Gemini/Mistral (narrative drift) |
| Хранение | SQLite (state) + JSON (risk/sizing/alerts) + git-markdown кэш; опц. Postgres (asyncpg + SQLAlchemy), Redis |
| Платежи | CryptoBot Crypto Pay |
| Charting | matplotlib |
| Dashboard | Streamlit (`streamlit_app.py`, `trading_system/dashboard_app.py`) |
| Observability | structured logging, опц. Sentry |
| Deploy | Railway (worker + cron), Nixpacks (Python 3.12) |
| Quality | ruff, mypy, pre-commit, pytest/unittest, GitHub Actions |

---

## 💾 Хранение состояния

| Хранилище | Содержит |
|-----------|----------|
| `dialectic_edge.db` (SQLite) | Позиции, trade_decision_log, дайджесты, snapshot'ы killer-фич, калибровка |
| `risk_state.json` / `sizing_state.json` | Adaptive Kelly + vol-target калибровка |
| `carry_monitor_state.json` | Открытые carry/арб-позиции |
| Git-markdown (через GitHub API) | `DIGEST_CACHE.md`, `FORECASTS.md`, `AUTO_TRACK.md`, `BACKTEST.md`, `MARKET_CACHE.md` |
| Redis *(опц.)* | Снапшоты дебатов (кнопка «листать») при >1 воркера / рестартах |
| Postgres *(опц.)* | VIP-подписки, кеш дайджестов |

На Railway монтируется Volume — путь приходит из `RAILWAY_VOLUME_MOUNT_PATH`, и SQLite/cache ложатся туда. ⚠️ Без тома файлы эфемерны и теряются при деплое.

---

## ⚙️ Конфигурация (env)

Полный список с комментариями — в [`.env.example`](.env.example). Ключевые группы:

```env
# Telegram
BOT_TOKEN=123456:ABC-...
ADMIN_IDS=0

# AI provider routing (хватит одного ключа, остальные — fallback)
AI_PROVIDER=gemini
GEMINI_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
TOGETHER_API_KEY=
CEREBRAS_API_KEY=
MISTRAL_API_KEY=
DEBATE_ROUNDS=3
MAX_TOKENS=1500
AGENT_TEMP=0.7

# Источники данных (free tier)
FINNHUB_API_KEY=
ALPHA_VANTAGE_API_KEY=
FRED_API_KEY=
TAVILY_API_KEY=
ETHERSCAN_API_KEY=

# State persistence в git
GITHUB_TOKEN=
GITHUB_REPO=valekbishon-art/DIALECTIC_EDGE
GITHUB_DEFAULT_BRANCH=master

# Хранилище
DATA_DIR=
DB_PATH=dialectic_edge.db
REDIS_URL=
DATABASE_URL=

# Платежи
CRYPTOBOT_API_TOKEN=

# Автотрейд
FEATURE_AUTOTRADE=1
AUTOTRADE_START_CAPITAL=500.0
AUTOTRADE_INTERVAL_SEC=60
AUTOTRADE_MIN_HOLD_MINUTES=15
AUTOTRADE_REENTRY_COOLDOWN_MIN=30
```

Все фичи — за флагом `FEATURE_XXX` (по умолчанию OFF, кроме P2P/Advisor/BTC-alerts/Carry). Полный перечень флагов killer-фич — в разделе [выше](#-killer-фичи-за-фичефлагами) и в `.env.example`.

---

## 🚢 Деплой

**Railway (рекомендуется):**
1. Подключи репозиторий, выбери ветку `spot-only`.
2. Создай Volume (Ctrl/⌘+K → «Volume»), привяжи к сервису бота → `RAILWAY_VOLUME_MOUNT_PATH` подхватится автоматически.
3. Задай переменные в Settings → Variables (минимум `BOT_TOKEN`, `ADMIN_IDS`, один AI-ключ).
4. (опц.) Redis / Postgres через New → Database, прокинь `REDIS_URL` / `DATABASE_URL` через Reference.
5. Push → деплой. Nixpacks ставит Python 3.12 (НЕ 3.13 — `nixpacks.toml`).

Cron-дайджест — `.github/workflows/digest_cron.yml` (+ `cron_digest.py`).

---

## 🛠️ Разработка, тесты, CI

```bash
# Тесты (252+ кейса в 74 файлах)
python -m unittest discover -s tests -p "test_*.py"
# или pytest
pytest

# Линт/типы (soft)
ruff check .
mypy refactor/

# Pre-commit
pre-commit run --all-files
```

**CI (GitHub Actions):**
- `lint.yml` — ruff + mypy (по `refactor/`), non-blocking.
- `tests.yml` — два job'а: `unit-fast` (minimal deps, ~30с) и `unit-full` (полный `requirements.txt` + smoke-import 13 модулей + все тесты, ~1.5–2 мин).
- `digest_cron.yml` — плановый дайджест.

**Правила (см. [`AGENTS.md`](AGENTS.md) и [`CONTRIBUTING.md`](CONTRIBUTING.md)):**
- ❌ Не трогать торговую логику (`signal_trader.py`, `signals.py`, `core/dynamic_risk.py`, `auto_tracker.py`) без тестов.
- ❌ Не править md-кэши руками (их пишет бот через GitHub API).
- ❌ Не добавлять `@dp.message` в `main.py` — новые хендлеры в `refactor/handlers/*` через `register(dp)`.
- ❌ Не вводить новые зависимости без обсуждения; не force-push в master; не коммитить секреты.
- ✅ Любая фича — за `FEATURE_XXX` (default OFF). Один PR — одна тема.

---

## 📖 Словарь терминов

**Режимы рынка:** `UPTREND` (MA50>MA200, порог ниже), `SIDEWAYS` (порог выше, entry уже), `HIGH_VOL` (>5% vol, позиция −50%), `DOWNTREND` (только SHORT/CASH).

**Индикаторы:** MVRV (>3.5 переоценён, <1.0 дно), SOPR (>1.05 фиксация), Funding (>0.1% быки платят), OI (рост+цена = тренд), VIX (>40 кризис, <15 оптимизм), QE/QT (FRED WALCL), Yield Curve (инверсия <−0.5%), HY Spread (>5% стресс), Fear & Greed (<25 страх, >75 жадность), COT (позиции спекулянтов).

**Стратегия выхода:** Split TP (50% при +2%), Trailing Stop (активация +3%, буфер 1.5%), Hard Stop (ATR × regime), Signal Reversal.

**AI-агенты:** Bull (`BULL_SYSTEM`), Bear (`BEAR_SYSTEM`), Verifier (`VERIFIER_SYSTEM`, чистит галлюцинации), Synth (`SYNTH_SYSTEM`, compact-JSON вердикт), Speechwriter (`SPEECHWRITER_SYSTEM`, читаемый план).

---

## 📜 Disclaimer

⚠️ **Это paper-trading / образовательный проект.**
Все сделки симулированы. Ничего здесь не является финансовым советом.
Прошлые результаты не гарантируют будущих. Рынок непредсказуем, агенты могут ошибаться.
Используй как один из инструментов мышления, не как сигнал к действию. **DYOR.**

---

## 🔗 Ссылки

- Репозиторий: <https://github.com/valekbishon-art/DIALECTIC_EDGE> (ветка `spot-only`)
- Карта для AI-агентов: [AGENTS.md](AGENTS.md)
- Переменные окружения: [.env.example](.env.example)
- Контрибуция: [CONTRIBUTING.md](CONTRIBUTING.md)
- Гайд новичка: [docs/BEGINNER_GUIDE.md](docs/BEGINNER_GUIDE.md)
