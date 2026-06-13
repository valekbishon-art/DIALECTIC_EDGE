"""
config.py — Центральная конфигурация Dialectic Edge.

ИСПРАВЛЕНО v2:
- FRED_API_KEY убран из кода в переменные окружения (был захардкожен и виден на GitHub)
- Добавлен DB_PATH — единый путь к БД для всех модулей
  (раньше learning.py использовал "dialectic.db" вместо "dialectic_edge.db")
"""

import os
from pathlib import Path

# ─── ПЕРСИСТЕНТНОЕ ХРАНИЛИЩЕ (Railway volume / любой VPS) ─────────────────────
# Без тома файлы живут в эфемерной ФС контейнера — после деплоя cache.json и БД пустые.
# Railway: том НЕ в Settings → Variables. Создание: Ctrl+K (⌘K на Mac) → набери "Volume"
#   ИЛИ правый клик по пустому месту на схеме проекта → Volume → привяжи к сервису бота,
#   укажи mount path (например /data). Тогда Railway сам выставит RAILWAY_VOLUME_MOUNT_PATH —
#   бот подхватит его без ручного DATA_DIR. Вручную: Variables → DATA_DIR=/data
# В один каталог кладём SQLite и cache.json (дебаты + last_report + user_debates).
_DATA_DIR = (
    os.getenv("DATA_DIR", "").strip()
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
)
if _DATA_DIR:
    _data_root = Path(_DATA_DIR)
    _data_root.mkdir(parents=True, exist_ok=True)
    DB_PATH = str(_data_root / "dialectic_edge.db")
    CACHE_FILE = str(_data_root / "cache.json")
    USING_DATA_DIR = True
else:
    DB_PATH = os.getenv("DB_PATH", "dialectic_edge.db")
    CACHE_FILE = os.getenv("CACHE_FILE", "cache.json")
    USING_DATA_DIR = False

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# ID администраторов
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()]

# Redis: снимки дебатов для кнопки «листать» — переживают рестарт и несколько воркеров.
# Railway: в проекте + New → Database → Redis. В СЕРВИСЕ БОТА: Variables → New Variable →
# Reference → Redis → REDIS_URL. URL в чат не шли — только в панели Railway.
REDIS_URL = os.getenv("REDIS_URL", "")

# ─── FRED API ─────────────────────────────────────────────────────────────────
# ИСПРАВЛЕНО: ключ перенесён в переменные окружения Railway
# Получить бесплатный ключ: https://fred.stlouisfed.org/docs/api/api_key.html
# В Railway: Settings → Variables → FRED_API_KEY=твой_ключ
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# ─── AI ПРОВАЙДЕР ─────────────────────────────────────────────────────────────
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

OPENAI_COMPAT_BASE_URL = os.getenv("OPENAI_COMPAT_BASE_URL", "http://localhost:1234/v1")
OPENAI_COMPAT_API_KEY  = os.getenv("OPENAI_COMPAT_API_KEY", "lm-studio")
OPENAI_COMPAT_MODEL    = os.getenv("OPENAI_COMPAT_MODEL", "local-model")

# ─── ДЕБАТЫ ───────────────────────────────────────────────────────────────────
DEBATE_ROUNDS        = int(os.getenv("DEBATE_ROUNDS", "3"))
MAX_TOKENS_PER_AGENT = int(os.getenv("MAX_TOKENS", "1500"))
AGENT_TEMPERATURE    = float(os.getenv("AGENT_TEMP", "0.7"))
# Первый провайдер для Bull/Bear/Verifier/Synth (ai_provider.py):
#   AI_DEBATE_PRIMARY=mistral|groq|openrouter|together|gemini
# Модели: GROQ_MODEL, OPENROUTER_MODEL, TOGETHER_MODEL, MISTRAL_MODEL, MISTRAL_SYNTH_MODEL, GEMINI_MODEL
# Росстат на Railway: при SSLCertVerificationError — RUSSIA_ROSSTAT_INSECURE_SSL=1 (только этот URL, на свой риск)

# ─── НОВОСТИ ──────────────────────────────────────────────────────────────────
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

# RSS: Reuters (feeds.reuters.com) часто недоступен из облаков — добавлены BBC, Guardian, MarketWatch и др.
RSS_FEEDS = {
    "BBC Business":       "https://feeds.bbci.co.uk/news/business/rss.xml",
    "Guardian Business":  "https://www.theguardian.com/business/rss",
    "Guardian World":     "https://www.theguardian.com/world/rss",
    "MarketWatch":        "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CNBC Markets":       "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135",
    "Yahoo Finance":      "https://finance.yahoo.com/news/rssindex",
    "Investing.com Eco":  "https://www.investing.com/rss/news_14.rss",
    "FT Markets":         "https://www.ft.com/rss/home/uk",
    "CoinDesk":           "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph":      "https://cointelegraph.com/rss",
}

MAX_NEWS_PER_FEED = int(os.getenv("MAX_NEWS_PER_FEED", "3"))
MAX_TOTAL_NEWS    = int(os.getenv("MAX_TOTAL_NEWS", "20"))

# ─── ХРАНИЛИЩЕ (CACHE_FILE / DB_PATH заданы выше; при DATA_DIR — внутри тома) ─
# Повторный /daily отдаёт тот же отчёт без вызова AI, пока не истечёт TTL (экономия токенов).
# Раньше по умолчанию было 2 ч.; сутки — разумный баланс. Переопределение: CACHE_TTL_HOURS=6
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "24"))
# Снимок дебатов для кнопки «листать раунды» (JSON + SQLite; переживает другой воркер при общем диске)
DEBATE_SNAPSHOT_HOURS = int(os.getenv("DEBATE_SNAPSHOT_HOURS", "72"))

# ─── ФОРМАТИРОВАНИЕ ───────────────────────────────────────────────────────────
DISCLAIMER = (
    "\n\n─────────────────────────\n"
    "🤝 *Честно о боте:*\n"
    "Это AI-анализ на основе публичных данных — не предсказание будущего.\n"
    "Рынок непредсказуем. Агенты могут ошибаться и иногда ошибаются.\n"
    "Где данных не хватало — агенты должны были это указать явно.\n"
    "Используй как один из инструментов мышления, не как сигнал к действию.\n\n"
    "⚠️ *Не является финансовым советом. DYOR. Торговля = риск потери капитала.*"
)

# ─── ФИЧЕФЛАГИ И ЛИМИТЫ (одно место; переопределение через env) ─────────────
# Глобально выключить цикл бумажного автотрейда (даже если backtest enabled в БД)
FEATURE_AUTOTRADE = os.getenv("FEATURE_AUTOTRADE", "1").strip().lower() in ("1", "true", "yes", "on")
# Источники данных для bias крипты в автотрейде
DATA_SOURCE_BINANCE_SIGNALS = os.getenv("DATA_SOURCE_BINANCE_SIGNALS", "1").strip().lower() in ("1", "true", "yes", "on")
# Писать в trade_decision_log каждый пропуск цикла (шумно; для отладки)
LOG_AUTOTRADE_SKIPS = os.getenv("LOG_AUTOTRADE_SKIPS", "0").strip().lower() in ("1", "true", "yes", "on")
# Параметры автотрейда (раньше были константами в signal_trader)
AUTOTRADE_INTERVAL_SEC = int(os.getenv("AUTOTRADE_INTERVAL_SEC", "60"))
AUTOTRADE_RECENT_CONTEXT_LIMIT = int(os.getenv("AUTOTRADE_RECENT_CONTEXT_LIMIT", "3"))
AUTOTRADE_CONTEXT_MAX_AGE_HOURS = int(os.getenv("AUTOTRADE_CONTEXT_MAX_AGE_HOURS", "72"))
AUTOTRADE_ENTRY_TOLERANCE_PCT = float(os.getenv("AUTOTRADE_ENTRY_TOLERANCE_PCT", "0.02"))
AUTOTRADE_OPEN_SCORE_THRESHOLD = float(os.getenv("AUTOTRADE_OPEN_SCORE_THRESHOLD", "18"))
AUTOTRADE_REVERSAL_SCORE_THRESHOLD = float(os.getenv("AUTOTRADE_REVERSAL_SCORE_THRESHOLD", "16"))
AUTOTRADE_SIGNAL_BIAS_CACHE_SEC = int(os.getenv("AUTOTRADE_SIGNAL_BIAS_CACHE_SEC", "300"))
# При NEUTRAL (или нет кандидатов из дайджеста) — добавлять кандидатов из /signals bias + рынок
AUTOTRADE_FOLLOW_SIGNALS_WHEN_NEUTRAL = os.getenv("AUTOTRADE_FOLLOW_SIGNALS_WHEN_NEUTRAL", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
AUTOTRADE_NEUTRAL_MIN_BIAS_SCORE = float(os.getenv("AUTOTRADE_NEUTRAL_MIN_BIAS_SCORE", "14"))
AUTOTRADE_NEUTRAL_TP_PCT = float(os.getenv("AUTOTRADE_NEUTRAL_TP_PCT", "0.04"))
AUTOTRADE_NEUTRAL_SL_PCT = float(os.getenv("AUTOTRADE_NEUTRAL_SL_PCT", "0.02"))
# Anti-whiplash: позиция должна прожить минимум столько минут, чтобы её
# можно было закрыть по «развороту сигнала». Меньше — это шум: вход и выход
# на одном и том же signal_bias за один цикл (60 сек).
AUTOTRADE_MIN_HOLD_MINUTES = int(os.getenv("AUTOTRADE_MIN_HOLD_MINUTES", "15"))
# Чтобы закрыть на развороте, текущий |score| сигнала должен превысить
# сохранённый при входе |score| хотя бы на эту дельту. Иначе «разворот» —
# это та же самая истерика, что нас сюда привела.
AUTOTRADE_REVERSAL_STRENGTH_DELTA = float(os.getenv("AUTOTRADE_REVERSAL_STRENGTH_DELTA", "6.0"))
# Если позиция уже в плюсе хотя бы на эти % — не закрываем по reversal,
# отдаём на откуп trailing-стопу из _close_position_if_needed.
AUTOTRADE_REVERSAL_PROFIT_GUARD_PCT = float(os.getenv("AUTOTRADE_REVERSAL_PROFIT_GUARD_PCT", "1.5"))
# Cooldown на повторный вход в тот же символ после reversal-close (мин).
AUTOTRADE_REENTRY_COOLDOWN_MIN = int(os.getenv("AUTOTRADE_REENTRY_COOLDOWN_MIN", "30"))
# Контр-трендовый buy-the-dip: открываем BUY только если RSI ≤ этого
# порога (oversold). SELL-the-rip — только если RSI ≥ (100 - порог).
# Без этого ловим «падающие ножи» в сильных нисходящих трендах.
AUTOTRADE_CONTRARIAN_RSI_OVERSOLD = float(os.getenv("AUTOTRADE_CONTRARIAN_RSI_OVERSOLD", "35"))
AUTOTRADE_CONTRARIAN_RSI_OVERBOUGHT = float(os.getenv("AUTOTRADE_CONTRARIAN_RSI_OVERBOUGHT", "65"))
# Ограничить размер JSON-снимка контекста модели в БД (символов)
DIGEST_SNAPSHOT_MAX_CHARS = int(os.getenv("DIGEST_SNAPSHOT_MAX_CHARS", "12000"))

# ─── POST-MORTEM (24h после дайджеста) ────────────────────────────────────────
# Через сутки после публикации /daily — сверяем вердикт с реальным движением.
# Пишет labels в `predictions` для /calibration (см. core/post_mortem.py).
# По дефолту 1: это feature-flag для аварийного выключения, не для постепенного
# rollout — модуль безопасный, чистая запись новых строк в таблицу predictions.
FEATURE_POST_MORTEM = os.getenv("FEATURE_POST_MORTEM", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
# UTC-время дневного запуска post-mortem.  По дефолту 23:50 — за 10 минут до
# нового дайджеста, когда вчерашний имеет реальный 24h-горизонт.
POST_MORTEM_RUN_TIME_UTC = os.getenv("POST_MORTEM_RUN_TIME_UTC", "23:50").strip()

# ─── EDGE-ЛЕДЖЕР (измерение edge живых сигналов) ───────────────────────
# ВОССТАНОВЛЕНО при дебаге: эти символы импортировались в best_deal_alert.py
# и scheduler.py, но отсутствовали в config.py — из-за чего фича никогда не
# могла включиться (ImportError молча гасился guarded try/except).
# По умолчанию ВЫКЛ: live-запись edge-леджера сейчас реализована заглушками
# в core/edge_ledger.py (record_signal/resolve_pending). Включай после того, как
# заполнишь их реальной персистентностью. На backtest_engine не влияет
# (тот использует только resolve_against_candles, без этих флагов).
FEATURE_EDGE_LEDGER = os.getenv("FEATURE_EDGE_LEDGER", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
# Интервал (сек) фонового резолвера pending-сигналов edge-леджера.
EDGE_RESOLVE_INTERVAL_SEC = int(os.getenv("EDGE_RESOLVE_INTERVAL_SEC", "3600"))
# Горизонт (часы) по которому резолвится исход live-сигнала (TP/SL/expired).
# 336ч = 14 дней — тот же дефолт, что в core/backtest_engine.py (Фаза 1).
EDGE_DEFAULT_HORIZON_HOURS = int(os.getenv("EDGE_DEFAULT_HORIZON_HOURS", "336"))

# ── Carry-оптимизатор (#4): risk-aware аллокация капитала по funding-carry ──
# По умолчанию ВЫКЛ — текущая выдача «равный объём» не меняется без явного
# включения. При 1: secция carry в Лучшей сделке показывает оптимизированную
# аллокацию (net-доходность, лимит концентрации, косты) на CARRY_CAPITAL_USD.
FEATURE_CARRY_OPTIMIZER = os.getenv("FEATURE_CARRY_OPTIMIZER", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
CARRY_CAPITAL_USD = float(os.getenv("CARRY_CAPITAL_USD", "1000"))
# Лимит доли капитала на одну carry-сделку (контроль концентрации). При 0.25 и
# ≥4 возможностях лимит связывает всё → yield-взвешивание не даёт прироста (вся
# ценность в отсечении под-костовых ног). Подними к ~0.35-0.40, чтобы включить
# yield-взвешивание (~+6% медиана, до ~12% при разбросе) ценой концентрации.
CARRY_MAX_WEIGHT = float(os.getenv("CARRY_MAX_WEIGHT", "0.25"))

# ── Фича ПАМП: кросс-биржевой сканер резких ростов на повышенном объёме ──
# Детект + фильтры живут в core/pump_scanner.py, рассылка — pump_alert.py,
# команда /pump — refactor/handlers/pump_handler.py. По умолчанию ВЫКЛ, чтобы
# не менять поведение прод-бота до явного включения (FEATURE_PUMP_SCANNER=1).
FEATURE_PUMP_SCANNER = os.getenv("FEATURE_PUMP_SCANNER", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
# Интервал (сек) фонового памп-цикла. 180с — компромисс свежесть/нагрузка.
PUMP_SCAN_INTERVAL_SEC = int(os.getenv("PUMP_SCAN_INTERVAL_SEC", "180"))
# Анти-спам: одну и ту же монету не чаще раза в N минут.
PUMP_COOLDOWN_MIN = int(os.getenv("PUMP_COOLDOWN_MIN", "60"))
# Лимит монет на on-demand /pump (0 в фоне = все). Топ N по обороту.
PUMP_ONDEMAND_MAX_SYMBOLS = int(os.getenv("PUMP_ONDEMAND_MAX_SYMBOLS", "400"))
# Порог ликвидности: мин. 24h оборот пары в USDT. Отсекает полумёртвые пары,
# где +5% делается одной сделкой (главный источник ложных пампов). Применяется
# в core/pump_scanner.py до фетча свечей.
PUMP_MIN_QUOTE_VOL_24H = float(os.getenv("PUMP_MIN_QUOTE_VOL_24H", "300000"))
