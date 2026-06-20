"""
Dialectic Edge v7.1 — UX + FinBERT async + РФ-график.
- Одно сообщение вместо 6 (краткая выжимка + Synth)
- Кнопка "📖 Полные дебаты" — листаешь раунды по одному
- Простой язык в выводах для обычных людей
"""

import re
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

from config import (
    BOT_TOKEN,
    ADMIN_IDS,
    CACHE_TTL_HOURS,
    REDIS_URL,
    CACHE_FILE,
    DB_PATH,
    USING_DATA_DIR,
    DEBATE_SNAPSHOT_HOURS,
)
from web_search import get_full_realtime_context
from report_sanitizer import sanitize_full_report
from chart_generator import generate_main_chart, generate_russia_chart, generate_trading_plan_png
from core import digest_context
from core.digest_context import (
    _plan_line as _digest_plan_line,
    build_digest_context,
    format_digest_telegram_summary,
)
from core.horizons import (
    DEFAULT_HORIZON_KEY,
    HORIZONS as HORIZON_PACKS,
    HorizonPack,
    all_horizon_keys,
    get_horizon,
    speechwriter_horizon_line,
)
from storage import Storage
from analysis_service import (
    run_full_analysis as analysis_service_run_full_analysis,
    _fetcher as news_fetcher,
    build_digest_persist_metadata,
)
# fetch_full_context из старого файла data_sources.py
from data_sources import fetch_full_context
from meta_analyst import get_meta_context
from github_export import get_previous_digest, push_digest_cache
from sentiment import analyze_and_filter_async, format_for_agents
from user_profile import build_profile_instruction
from news_fetcher import NewsFetcher
from agents import DebateOrchestrator
from tracker import save_predictions_from_report
from database import log_report
from web_search import search_news_context
from database import (
    init_db, upsert_user, get_user, increment_requests,
    save_debate_session,
    set_daily_sub,
    get_track_record, save_feedback, get_feedback_stats,
    import_forecasts_from_markdown,
    get_signals_subscribers, set_signals_sub, get_user_signals_status,
    get_user_edge_profile, set_user_edge_profile,
    add_trade, close_trade, get_trades, get_open_trade_by_symbol,
    delete_trade, get_trade_stats,
    get_user_signals_assets, set_user_signals_assets, toggle_user_signal_asset,
    add_portfolio_position, get_portfolio, remove_portfolio_position,
    add_backtest_signal, close_backtest_signal, get_backtest_signals, get_backtest_stats,
    get_backtest_config, update_backtest_capital, set_backtest_enabled,
    clear_backtest_signals,
    save_daily_context, get_daily_context,
    get_predictions_summary,
)
from tracker import check_pending_predictions
from core.healthz import run_healthz_server
from scheduler import Scheduler
from user_profile import (
    init_profiles_table, get_profile,
    RISK_PROFILES, HORIZONS,
    format_profile_card, save_profile
)
from weekly_report import build_weekly_report
from russia_data import fetch_russia_context, fetch_cbr_data
from russia_agents import run_russia_analysis
from debate_storage import ping_redis, save_debate_redis
from refactor.middleware.rate_limiter import RateLimitMiddleware

# Phase 3 Handler Imports — Market, Debate, Profile, Admin
from refactor.handlers import (
    get_debate_handler,
    handle_market_command,
    store_and_link_debate,
    handle_debate_navigation_callback,
    show_profile_settings,
    handle_profile_callback,
    show_portfolio as show_portfolio_view,
    handle_portfolio_callback as handle_portfolio_action,
    handle_portfolio_text_input as handle_portfolio_input,
    cmd_add_portfolio as add_portfolio_command,
    cmd_remove_portfolio as remove_portfolio_command,
    setup_admins,
    is_admin,
    handle_stats_command,
    handle_health_command,
    handle_logs_command,
    handle_sysinfo_command,
    handle_edge_command,
    handle_btc_command,
    handle_funding_command,
    handle_p2p_command,
    register_btc_handlers,
    register_funding_handlers,
    register_p2p_arbitrage_handlers,
    register_postmortem_handlers,
    register_retro_handlers,
)
from refactor.handlers.advisor_handler import register_advisor_handlers
from refactor.handlers.advisor_portfolio_handler import (
    register_advisor_portfolio_handlers,
)
from refactor.handlers.subscription_handler import (
    cmd_premium,
    register as register_subscription_handlers,
    require_vip,
)

# Phase 4 Provider Imports — AI, Cache, Database, Market Data, News, Storage
# ВАЖНО: build_short_report, parse_report_parts, extract_signal_pct_and_stars,
# hydrate_debate_from_report, main_report_keyboard ОПРЕДЕЛЕНЫ ЛОКАЛЬНО НИЖЕ (после импортов).
# Импорты из utils.py НЕ используются т.к. локальные определения перекрывают их.
from refactor.handlers.utils import (
    clean_markdown,
    debate_plain_text,
    split_message,
    strip_digest_summary_text,
)

from refactor.observability.logging_setup import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

from refactor.observability.sentry_setup import setup_sentry  # noqa: E402
setup_sentry()  # no-op if SENTRY_DSN unset or sentry-sdk not installed

bot: Optional[Bot] = None
dp = Dispatcher()
storage = Storage()

FREE_DAILY_LIMIT = 5

scheduler: Scheduler = None

# Хранилище дебатов для листания по кнопкам
# {user_id: {"rounds": [...], "full_report": str}}

# Кэш РФ анализа (обновляется вместе с /daily)
russia_cache: dict = {}  # {"report": str, "timestamp": str, "sections": {...}, "ts": float}

# debate_cache: один и тот же dict с refactor.handlers.debate_handler.
# Раньше тут был свой `debate_cache: dict = {}` — отдельная in-memory копия.
# Из-за этого после /daily кнопка «🎯 Стратегия по рынку» (callback money:*),
# которая читает кэш через `get_debate_handler().get_debate(user_id)`, не
# находила свежий дебат и писала «Сначала запусти /daily» — хотя дайджест
# секунду назад приходил. Шарим один и тот же dict — теперь обе стороны
# видят одинаковое состояние.
from refactor.handlers.debate_handler import debate_cache, show_debate_round  # noqa: E402  # {user_id: {"rounds": [...], "full": str}}

# PR #34: кэш для кнопки «📊 Показать таблицу плана». Храним (plans, prices)
# в момент рендера дайджеста, потом callback `plantable:UID` берёт это и
# отдаёт PNG через generate_trading_plan_png. Не сохраняем в Redis/SQLite —
# таблица всегда актуальна на момент /daily, после рестарта Railway просто
# перепрогоняется /daily. Поэтому in-memory dict — нормально.
_plan_table_cache: dict[int, tuple[list, dict]] = {}


def _quant_map_from_prices(prices: dict | None) -> dict[str, dict]:
    """Извлекает per-symbol quant verdicts из словаря цен.

    ``web_search.fetch_realtime_prices`` обогащает каждый актив полями
    ``quant_verdict``/``quant_confidence``/``quant_reason``/``quant_components``/
    ``quant_status`` (см. quant_filter.py). Здесь сжимаем в формат,
    понятный для ``core.digest_context.build_digest_context`` (передаётся
    в ``quant_verdict_map=``).

    Только crypto-активы (5 штук) учитываем — для акций / commodities
    quant-фильтр не имеет смысла (другой режим, другие индикаторы).
    Если quant_verdict отсутствует — пропускаем; пустой dict означает «не
    применять фильтр» (graceful-degradation до сырого LLM-вердикта).
    """
    if not prices:
        return {}
    crypto_keys = ("BTC", "ETH", "SOL", "BNB", "XRP")
    out: dict[str, dict] = {}
    for key in crypto_keys:
        p = prices.get(key) if isinstance(prices, dict) else None
        if not isinstance(p, dict):
            continue
        verdict = p.get("quant_verdict")
        if not verdict:
            continue
        out[key] = {
            "verdict": verdict,
            "confidence": p.get("quant_confidence", 0),
            "reason": p.get("quant_reason", ""),
            "components": p.get("quant_components", {}),
        }
    return out


def get_bot() -> Bot:
    global bot
    if bot is None:
        bot = Bot(token=BOT_TOKEN)
    return bot


# ─── Утилиты вынесены в refactor/handlers/utils.py ───────────────────────────────────


async def check_limit(user_id: int) -> bool:
    user = await get_user(user_id)
    if not user:
        return True
    if user.get("tier") == "pro":
        return True
    return user.get("requests_today", 0) < FREE_DAILY_LIMIT


def feedback_keyboard(report_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Полезно", callback_data=f"fb:1:{report_type}"),
        InlineKeyboardButton(text="👎 Мимо",    callback_data=f"fb:-1:{report_type}"),
    ]])


# ─── Persistent ReplyKeyboard ────────────────────────────────────────────────
# Постоянное меню снизу — заменяет QWERTY-клавиатуру на 4 главных кнопки.
# Юзеру не надо помнить /команды — он просто тыкает в нижний ряд.
# Подписи к кнопкам строго совпадают с тем что обрабатывают
# `_PERSISTENT_KB_TRIGGERS` ниже (любое расхождение → кнопка не сработает).
PERSISTENT_BTN_DAILY    = "🧠 Диалектика"
PERSISTENT_BTN_PITCH    = "💎 Питч"   # legacy: pitch lives only in /start now
PERSISTENT_BTN_PUMP     = "🚀 Памп"
PERSISTENT_BTN_MARKETS  = "🏛 Рынки"
PERSISTENT_BTN_SETTINGS = "⚙️ Настройки"
PERSISTENT_BTN_SIGNAL   = "🎯 Лучшая сделка"
PERSISTENT_BTN_SCREENER = "🧪 Скринер"
PERSISTENT_BTN_P2P      = "🧭 P2P арбитраж"
PERSISTENT_BTN_STOCKS   = "📈 Акции"
PERSISTENT_BTN_TREND    = "🧭 Тренд"
PERSISTENT_BTN_CARRY    = "💱 Carry"
PERSISTENT_BTN_ARB      = "🔀 Кросс-арб"
PERSISTENT_BTN_BASIS    = "🗓 Базис"
PERSISTENT_BTN_CALC     = "🧮 Калькулятор"
PERSISTENT_BTN_HELP     = "❓ Помощь"
# Полный набор для нижнего меню (зеркало inline-меню /start)
PERSISTENT_BTN_DCA      = "💰 DCA"
PERSISTENT_BTN_ALERTS   = "🔔 Алерты"
PERSISTENT_BTN_SIGSTAT  = "🤖 Автоторговля"
PERSISTENT_BTN_BACKTEST = "🧪 Бэктест"
PERSISTENT_BTN_TRACK    = "📊 Трек-рекорд"
PERSISTENT_BTN_VIP      = "💎 VIP"
PERSISTENT_BTN_WHATIDO  = "💎 Что я умею"
PERSISTENT_BTN_NEWBIE   = "🆕 Новичок"
PERSISTENT_BTN_GUIDE    = "📘 Команды"
# Категории-разделы нижнего меню (открывают тематические подменю отдельным сообщением)
PERSISTENT_BTN_ANALYSIS = "🧭 Анализ"
PERSISTENT_BTN_TOOLS    = "💼 Инструменты"
PERSISTENT_BTN_MORE     = "⚙️ Ещё"


def persistent_kb() -> ReplyKeyboardMarkup:
    """Главное меню снизу — минимум кнопок. Только самое важное:
    лучшая сделка, прогноз, рынки + 3 раздела (Анализ / Инструменты / Ещё),
    которые открывают тематические подменю отдельным сообщением.
    Так нижняя панель чистая, а всё остальное — на расстоянии одного тапа."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=PERSISTENT_BTN_SIGNAL),
                KeyboardButton(text=PERSISTENT_BTN_DAILY),
                KeyboardButton(text=PERSISTENT_BTN_MARKETS),
            ],
            [
                KeyboardButton(text=PERSISTENT_BTN_ANALYSIS),
                KeyboardButton(text=PERSISTENT_BTN_TOOLS),
                KeyboardButton(text=PERSISTENT_BTN_MORE),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Тыкай кнопку или пиши команду…",
    )


def signal_to_stars(confidence) -> str:
    mapping = {"HIGH": 0.85, "MEDIUM": 0.55, "LOW": 0.25, "EXTREME": 0.95}
    if isinstance(confidence, str):
        confidence = mapping.get(confidence.upper(), 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    stars = max(1, min(5, round(confidence * 5)))
    return "⭐" * stars + "☆" * (5 - stars)


def extract_signal_pct_and_stars(report: str) -> tuple[int, str]:
    """
    Процент в отчёте — это шкала уверенности FinBERT в классификации тона новостей
    (маппинг HIGH/MEDIUM/LOW → 85/55/25), а не «уверенность в направлении рынка».
    """
    m = re.search(r"Уровень\s+сигнала[^\d(]*\((\d+)%", report, re.IGNORECASE)
    if not m:
        m = re.search(r"📶[^\n]{0,160}\((\d+)%", report)
    pct = int(m.group(1)) if m else 50
    pct = max(0, min(100, pct))
    return pct, signal_to_stars(pct / 100)


SIGNAL_PCT_EXPLAINED = (
    "Число % — уверенность FinBERT в тоне новостей "
    "(EXTREME≈95%, HIGH≈85%, MEDIUM≈55%, LOW≈25%), "
    "не прогноз «рынок пойдёт вверх/вниз». Звёзды — наглядная шкала той же метрики.\n"
    "Если ниже FinBERT = NEUTRAL/MIXED, процент — насколько модель уверена именно в этой метке тона, "
    "а не «сила бычьего/медвежьего тренда»."
)


# Маркеры должны совпадать с `DebateOrchestrator._format_report` в agents.py
# и со старыми отчётами в кэше.
_SYNTH_START_MARKERS = (
    "⚖️ *ВЕРДИКТ И ТОРГОВЫЙ ПЛАН*",
    "⚖️ ВЕРДИКТ И ТОРГОВЫЙ ПЛАН",
    "⚖️ *ИТОГОВЫЙ СИНТЕЗ И РЕКОМЕНДАЦИИ*",
    "⚖️ ИТОГОВЫЙ СИНТЕЗ И РЕКОМЕНДАЦИИ",
    "ИТОГОВЫЙ СИНТЕЗ",
)
_DEBATE_START_MARKERS = (
    "🗣 *ДЕБАТЫ АГЕНТОВ*",
    "🗣 *ХОД ДЕБАТОВ*",
    "🗣 ХОД ДЕБАТОВ",
    "🗣 ДЕБ����ТЫ АГЕНТОВ",
)
_ROUND_HEADER_RE = re.compile(r"──\s*Раунд\s+\d+")

# Где начинается блок дебатов (жёсткие строки + запасные варианты — модель/парсер могли слегка сменить разметку)
_DEBATE_START_RES = (
    re.compile(r"🗣\s*\*?\s*ХОД\s+ДЕБАТОВ", re.IGNORECASE),
    re.compile(r"🗣\s*\*?\s*ДЕБАТЫ\s+АГЕНТОВ", re.IGNORECASE),
    re.compile(r"\*?──\*?\s*Раунд\s+1\b"),
    re.compile(r"──\s*Раунд\s+1\b"),
    re.compile(r"🐂\s*Bull\s+Researcher"),
)


def find_debate_start_index(text: str) -> Optional[int]:
    """Индекс начала блока дебатов; None если не найден."""
    hit = _find_first_marker(text, _DEBATE_START_MARKERS)
    if hit:
        return hit[0]
    best: Optional[int] = None
    for rx in _DEBATE_START_RES:
        m = rx.search(text)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def _find_first_marker(text: str, markers: Tuple[str, ...]) -> Optional[Tuple[int, str]]:
    best: Optional[Tuple[int, str]] = None
    for m in markers:
        i = text.find(m)
        if i != -1 and (best is None or i < best[0]):
            best = (i, m)
    return best


# ─── Парсинг отчёта на части ──────────────────────────────────────────────────

def parse_report_parts(report: str) -> dict:
    """
    Разбивает полный отчёт на:
    - header: шапка с датой и звёздами
    - rounds: список раундов дебатов [раунд1, раунд2, раунд3]
    - synthesis: итоговый синтез Synth
    - disclaimer: нижний дисклеймер
    """
    parts = {
        "header": "",
        "rounds": [],
        "synthesis": "",
        "disclaimer": "",
        "full": report
    }

    # Вытаскиваем дисклеймер — пробуем несколько вариантов маркера
    for disc_marker in [
        "─────────────────────────\n🤝 Честно о боте:",
        "─────────────────────────\n🤝 *Честно о боте:*",
        "🤝 Честно о боте:",
        "🤝 *Честно о боте:*",
    ]:
        if disc_marker in report:
            idx = report.find(disc_marker)
            parts["disclaimer"] = report[idx:]
            report = report[:idx]
            break

    # Вытаскиваем синтез — пробуем несколько вариантов маркера (v7 отчёты + старые)
    synth_hit = _find_first_marker(report, _SYNTH_START_MARKERS)
    if synth_hit:
        idx, _ = synth_hit
        parts["synthesis"] = report[idx:].strip()
        report = report[:idx]

    # Вытаскиваем раунды
    round_markers_legacy = (
        "── Раунд 1:",
        "── Раунд 2:",
        "── Раунд 3:",
    )

    debate_idx = find_debate_start_index(report)
    if debate_idx is not None:
        parts["header"] = report[:debate_idx].strip()
        debate_section = report[debate_idx:]

        # Разбиваем на раунды
        current_round = ""
        current_round_num = 0
        for line in debate_section.split("\n"):
            is_round_header = bool(_ROUND_HEADER_RE.search(line)) or any(
                m in line for m in round_markers_legacy
            )
            if is_round_header:
                if current_round.strip() and current_round_num > 0:
                    parts["rounds"].append(current_round.strip())
                current_round = line + "\n"
                current_round_num += 1
            else:
                current_round += line + "\n"

        if current_round.strip() and current_round_num > 0:
            parts["rounds"].append(current_round.strip())

        if not parts["rounds"]:
            parts["rounds"] = [debate_section]
    else:
        parts["header"] = report.strip()

    return parts


def hydrate_debate_from_report(full_report: str) -> dict | None:
    """
    rounds + full для листания дебатов. Если parse_report_parts не выделил раунды,
    берём целиком блок от 🗣 до ⚖️ ВЕРДИКТ (одна «страница» вместо пустого кэша).
    """
    if not full_report or not full_report.strip():
        return None
    parts = parse_report_parts(full_report)
    if parts.get("rounds"):
        return {"rounds": parts["rounds"], "full": parts.get("full", full_report)}
    start = find_debate_start_index(full_report)
    if start is None:
        return None
    tail = full_report[start:]
    synth_hit = _find_first_marker(tail, _SYNTH_START_MARKERS)
    if synth_hit:
        section = tail[: synth_hit[0]].strip()
    else:
        disc_snip = "\n\n─────────────────────────"
        di = tail.find(disc_snip)
        section = tail[:di].strip() if di != -1 else tail.strip()
    if len(section) < 80:
        return None
    return {"rounds": [section], "full": full_report}


def extract_verdict_from_report(report: str) -> str | None:
    """Extract verdict from report synthesis section.

    Delegates to ``digest_context.extract_verdict`` which looks at the
    explicit ``ВЕРДИКТ СУДЬИ: <X>`` line first instead of scanning the whole
    synthesis block. The naive substring scan used to flip ``МЕДВЕЖИЙ``
    to ``BUY`` whenever the verdict reasoning mentioned the word
    ``бычий`` (e.g. ``FinBERT не подтверждает бычий настрой``),
    producing a digest header that contradicted the trade plan below.
    """
    if not report or not report.strip():
        return None
    return digest_context.extract_verdict(report) or None


def extract_symbols_from_report(report: str, prices: dict) -> tuple[dict, dict, dict, dict]:
    """
    Extract symbols, entry prices, stop losses, targets from report.
    ИСПРАВЛЕНО: парсит реальный формат дайджеста:
    - Актив: BTC
    - Вход: $73,779
    - Цель: $80,000
    - Стоп: $65,000
    """
    entries = {}
    stop_losses = {}
    targets = {}
    timeframes = {}

    # Парсим блоки "Актив: X ... Вход/Цель/Стоп"
    asset_blocks = re.split(r'[-•]\s*(?:Актив|Asset)\s*:', report, flags=re.IGNORECASE)
    for block in asset_blocks[1:]:
        lines = block.strip().split("\n")
        sym_raw = lines[0].strip().upper().split()[0] if lines else ""
        sym = re.sub(r'[^A-Z]', '', sym_raw)
        if not sym or len(sym) > 5:
            continue
        for line in lines:
            m = re.search(r'(?:Вход|Entry)\s*:\s*\$?([\d,\.]+)', line, re.IGNORECASE)
            if m and sym not in entries:
                try: entries[sym] = float(m.group(1).replace(",", ""))
                except: pass
            m = re.search(r'(?:Цель|Target|Тейк)\s*:\s*\$?([\d,\.]+)', line, re.IGNORECASE)
            if m and sym not in targets:
                try: targets[sym] = float(m.group(1).replace(",", ""))
                except: pass
            m = re.search(r'(?:Стоп|Stop)\s*:\s*\$?([\d,\.]+)', line, re.IGNORECASE)
            if m and sym not in stop_losses:
                try: stop_losses[sym] = float(m.group(1).replace(",", ""))
                except: pass
            m = re.search(r'(?:Горизонт|Horizon)\s*:\s*(.+)', line, re.IGNORECASE)
            if m and sym not in timeframes:
                timeframes[sym] = m.group(1).strip()[:20]

    # Fallback: если планов нет — берём текущие цены как entry
    for sym, price in prices.items():
        if sym not in entries and isinstance(price, (int, float)) and price > 0:
            entries[sym] = price
        if sym not in timeframes:
            timeframes[sym] = "1d"

    return entries, stop_losses, targets, timeframes


_SM_CARD_SYMBOL_ICONS = {
    "BTCUSDT": "₿",
    "ETHUSDT": "Ξ",
    "SOLUSDT": "◎",
    "BNBUSDT": "🅱",
    "XRPUSDT": "✕",
}
_SM_CARD_LS_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")


def _sm_ls_tag(ls: float) -> tuple[str, str]:
    if ls >= 1.5:
        return "🟢", "лонгят сильно"
    if ls >= 1.2:
        return "🟢", "лонгят"
    if ls <= 0.7:
        return "🔴", "шортят сильно"
    if ls <= 0.85:
        return "🔴", "шортят"
    return "⚪️", "нейтрал"


def _format_smart_money_card(prices: dict | None) -> str | None:
    """Делает короткую карточку институциональных сигналов для пользователя.
    Использует SM_* ключи которые уже есть в prices_dict (заполняются
    `enrich_prices_with_scores` в market_indicators/aggregator.py).

    Возвращает None если данные недоступны (карточка не показывается).
    """
    if not prices:
        return None

    ls = prices.get("SM_TOP_TRADER_LS")
    ls_per_symbol = prices.get("SM_TOP_TRADER_LS_PER_SYMBOL") or {}
    cb_prem = prices.get("SM_COINBASE_PREMIUM")
    cme_basis = prices.get("SM_CME_BASIS")
    funding_avg = prices.get("SM_FUNDING_AVG")
    funding_align = prices.get("SM_FUNDING_ALIGN")

    bullets: list[str] = []

    # Top-trader L/S — компактный per-symbol блок по 5 основным парам.
    # Если per-symbol нет, fallback на старую одно-строчную BTC-форму.
    if isinstance(ls_per_symbol, dict) and ls_per_symbol:
        bullets.append("📊 *Top-trader L/S по парам:*")
        for sym in _SM_CARD_LS_SYMBOLS:
            ratio = ls_per_symbol.get(sym)
            name = sym.replace("USDT", "")
            icon = _SM_CARD_SYMBOL_ICONS.get(sym, "•")
            if not isinstance(ratio, (int, float)):
                bullets.append(f"  {icon} {name}: N/A")
                continue
            emoji, tag = _sm_ls_tag(float(ratio))
            bullets.append(f"  {icon} {name}: `{ratio:.2f}` {emoji} {tag}")
    elif isinstance(ls, (int, float)):
        emoji, tag = _sm_ls_tag(float(ls))
        bullets.append(f"{emoji} *Top-trader L/S (BTC):* {ls:.2f} → {tag}")

    if isinstance(cb_prem, (int, float)):
        if cb_prem >= 0.20:
            tag = "🇺🇸 US-биды (бычий)"
        elif cb_prem >= 0.05:
            tag = "🇺🇸 US-bid pressure"
        elif cb_prem <= -0.20:
            tag = "🇺🇸 US-sell (медвежий)"
        elif cb_prem <= -0.05:
            tag = "🇺🇸 US-sell pressure"
        else:
            tag = "нейтрал"
        bullets.append(f"  *Coinbase Premium:* {cb_prem:+.2f}% — {tag}")

    if isinstance(cme_basis, (int, float)):
        if cme_basis >= 0.30:
            tag = "📜 contango (бычий)"
        elif cme_basis <= -0.30:
            tag = "📜 backwardation (медвежий)"
        else:
            tag = "📜 нейтрал"
        bullets.append(f"  *CME Basis:* {cme_basis:+.2f}% — {tag}")

    if isinstance(funding_avg, (int, float)) and funding_align:
        align = str(funding_align).upper()
        if align == "ALL_LONG" and funding_avg > 0.05:
            tag = "⚠️ перегретый лонг — squeeze risk"
        elif align == "ALL_SHORT" and funding_avg < -0.005:
            tag = "⚡ массовый шорт — contrarian-бычий"
        elif align == "ALL_LONG":
            tag = "лонг-настроение"
        elif align == "ALL_SHORT":
            tag = "шорт-настроение"
        elif align == "MIXED":
            tag = "нет консенсуса"
        else:
            tag = align.lower()
        bullets.append(f"  *Funding:* {funding_avg:+.4f}% [{align}] — {tag}")

    if not bullets:
        return None

    return "\n".join(["🏛 *Институциональные сигналы (Smart-money):*", *bullets])


# ── Группированный торговый план (PR #34) ──────────────────────────────────
# Asset → (emoji-маркер, человеческое название). emoji-маркеры повторяют
# смысл в `_SM_CARD_SYMBOL_ICONS` (Top-trader L/S), плюс макро-набор. Не
# подмешиваем в один блок — у юзера должно быть чёткое разделение крипта
# vs макро, иначе 11 одинаковых строк подряд читать невозможно.
_TRADING_PLAN_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("🪙 *КРИПТО*", [
        ("BTC",     "₿", "BTC"),
        ("ETH",     "Ξ", "ETH"),
        ("SOL",     "◎", "SOL"),
        ("BNB",     "🅱", "BNB"),
        ("XRP",     "✕", "XRP"),
    ]),
    ("📈 *МАКРО*", [
        ("SPX",     "📊", "S&P 500"),
        ("NDX",     "💻", "Nasdaq 100"),
        ("GOLD",    "🥇", "Gold"),
        ("OIL_WTI", "🛢", "WTI Oil"),
        ("DXY",     "💵", "DXY"),
        ("VIX",     "😱", "VIX"),
    ]),
]

# Synth иногда называет активы по-другому (SPY=SPX, WTI=OIL_WTI и т.д.) —
# мапим к каноничным ключам prices_dict, чтобы группировка работала.
_PLAN_SYMBOL_ALIASES: dict[str, str] = {
    "BITCOIN": "BTC", "BTCUSD": "BTC", "BTCUSDT": "BTC",
    "ETHEREUM": "ETH", "ETHUSD": "ETH", "ETHUSDT": "ETH",
    "SOLANA": "SOL", "SOLUSDT": "SOL",
    "BNBUSDT": "BNB",
    "XRPUSDT": "XRP",
    "S&P": "SPX", "S&P500": "SPX", "SP500": "SPX", "SPY": "SPX", "^GSPC": "SPX",
    "NASDAQ": "NDX", "QQQ": "NDX", "^NDX": "NDX",
    "XAU": "GOLD", "GLD": "GOLD", "XAUUSD": "GOLD",
    "OILWTI": "OIL_WTI", "WTI": "OIL_WTI", "USO": "OIL_WTI", "CL=F": "OIL_WTI", "OIL": "OIL_WTI",
    "DX-Y.NYB": "DXY",
    "^VIX": "VIX",
}


def _fmt_money_compact(value) -> str:
    """Markdown-safe адаптивная цена. Зеркало web_search._fmt_money."""
    if value is None:
        return "—"
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
    return body


def _normalize_plan_symbol(raw: str | None) -> str | None:
    if not raw:
        return None
    up = str(raw).upper().strip()
    if up in _PLAN_SYMBOL_ALIASES:
        return _PLAN_SYMBOL_ALIASES[up]
    return up


def _trading_plan_grouped_lines(plans: list[dict] | None, prices: dict | None) -> list[str]:
    """Группированный торговый план: крипта / макро, по 2-3 строки на актив.

    Источник истины — `prices_dict` (MA50/MA200 из web_search.py:_fetch_*).
    `plans[]` (Synth output) используется только чтобы определить, какие
    активы Synth решил включить в ��лан. Раньше рендер был «11 одинаковых
    bullet-строк подряд», читать тяжело; плюс из-за `${ma:.0f}` в
    `format_prices_for_agents` XRP-триггеры приходили как `$1/$2` —
    теперь берём MA-уровни напрямую из структурированных данных.
    """
    prices = prices or {}
    plans = plans or []

    # Множество символов, которые Synth включил в план (после нормализации).
    plan_symbols: set[str] = set()
    for plan in plans:
        sym = _normalize_plan_symbol(plan.get("symbol") or plan.get("label"))
        if sym:
            plan_symbols.add(sym)

    out: list[str] = []
    for group_title, assets in _TRADING_PLAN_GROUPS:
        group_lines: list[str] = []
        for key, emoji, label in assets:
            if plan_symbols and key not in plan_symbols:
                continue
            entry = prices.get(key)
            if not isinstance(entry, dict):
                continue
            price = entry.get("price")
            ma50 = entry.get("ma50")
            ma200 = entry.get("ma200")
            if price is None or ma50 is None or ma200 is None:
                continue

            try:
                price_f = float(price); ma50_f = float(ma50); ma200_f = float(ma200)
            except (TypeError, ValueError):
                continue

            # Решаем какой MA-уровень является LONG-триггером (выше цены),
            # а какой SHORT-триггером (ниже цены). Если оба с одной стороны
            # (uptrend/downtrend), ближайший = текущий стоп-трейл, дальний =
            # подтверждающий уровень. Для unification рендерим всегда два
            # уровня — юзер видит структуру MA50/MA200 относительно цены.
            ma_a, tag_a = (ma200_f, "MA200")
            ma_b, tag_b = (ma50_f, "MA50")
            up_level, up_tag = (ma_a, tag_a) if ma_a >= ma_b else (ma_b, tag_b)
            dn_level, dn_tag = (ma_b, tag_b) if ma_a >= ma_b else (ma_a, tag_a)

            head = f"{emoji} *{label}* — `${_fmt_money_compact(price_f)}`"
            up = f"   ▲ выше `${_fmt_money_compact(up_level)}` ({up_tag}) → покупка спот"
            dn = f"   ▼ ниже `${_fmt_money_compact(dn_level)}` ({dn_tag}) → выход в стейбл"
            group_lines.extend([head, up, dn])

        if group_lines:
            if out:
                out.append("")
            out.append(group_title)
            out.extend(group_lines)
    return out


def _format_debate_summary_block(
    *,
    debate_summary: dict | None,
    verdict_reason: str = "",
    plain_language: str = "",
) -> list[str]:
    """Рендерит блок «🧠 О чём спорил ИИ сегодня» в Telegram-дайджесте.

    Возвращает список строк блока (без ведущего blank-line — caller
    сам добавит разделитель). Если агентов нет в выжимке и
    ``verdict_reason``/``plain_language`` тоже пусты — возвращает [].
    """
    summary = debate_summary or {}
    bull = (summary.get("bull") or "").strip()
    bear = (summary.get("bear") or "").strip()
    verifier = (summary.get("verifier") or "").strip()
    reason = (verdict_reason or "").strip()
    plain = (plain_language or "").strip()

    if not (bull or bear) and not (reason and plain):
        return []

    lines: list[str] = ["🧠 *О чём спорил ИИ сегодня:*"]
    if bull:
        lines.append(f"🐂 *Бык:* {bull}")
    if bear:
        lines.append(f"🐻 *Медведь:* {bear}")
    if verifier:
        lines.append(f"🔍 *Скептик:* {verifier}")
    if reason:
        lines.append(f"⚖️ *Консенсус:* {reason}")
    if plain:
        lines.append(f"💬 *Простыми словами:* {plain}")
    return lines


def build_short_report(parts: dict, stars: str, pct: int, horizon: HorizonPack | None = None, prices: dict | None = None) -> list:
    """
    Собирает ОДНО сообщение для пользователя в фиксированном layout'е:

      📊 DIALECTIC EDGE — ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ
      🕒 dd.mm.yyyy HH:MM
      ⏱ Горизонт: <emoji> <label>           ← опционально, если передан horizon

      🎯 Вердикт: <emoji> <Бычий/Медвежий/Нейтральный>
      📊 Сигнал: ⭐⭐⭐⭐⭐ (NN%)

      🧠 Почему: …

      📋 Торговый план:
      • <symbol> <DIR> | вход $X | цель $Y | стоп $Z | горизонт N | триггер …

      👀 Точки наблюдения:
      • …

      💬 Простыми словами: …

      📜 Полный raw-ответ модели и полны�� дебаты доступны кнопками ниже.

    Возвращает список из одного элемента, чтобы существующие caller'ы
    (refactor/handlers/market_handler.py и т.п.) ломались только если
    реально расчитывают на конкретное число чанков. Полный raw-отчёт
    + полные дебаты пользователь забирает кнопками под сообщением.
    """
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    full = parts.get("full", "")

    # Квант-фильтр (BB+Donchian+RSI ансамбль + BTC gate) пост-обрабатывает
    # LLM-вердикт: при сильном конфликте overall quant ↔ LLM, вердикт демоутится
    # до NEUTRAL (см. core.digest_context.build_digest_context). Бэктест:
    # 65.9% hit-rate vs 49.6% MA50/200 — docs/quant_research_v2.md.
    quant_map = _quant_map_from_prices(prices)
    digest_ctx = build_digest_context(full, quant_verdict_map=quant_map)
    verdict_label = digest_ctx.get("verdict_label", "Нейтральный")
    verdict_emoji = digest_ctx.get("verdict_emoji", "⚪️")
    verdict_reason = digest_ctx.get("verdict_reason", "")
    debate_summary = digest_ctx.get("debate_summary") or {}
    plans = digest_ctx.get("plans") or []
    watch_levels = digest_ctx.get("watch_levels") or []
    monitoring_points = digest_ctx.get("monitoring_points") or []
    plain_language = digest_ctx.get("plain_language", "")
    eli5 = digest_ctx.get("eli5", "")
    key_trigger = digest_ctx.get("key_trigger", "")
    invalidation = digest_ctx.get("invalidation", "")
    only_watch = (not plans) and bool(watch_levels)

    lines: list[str] = [
        "📊 *DIALECTIC EDGE — ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ*",
        f"🕒 {now}",
    ]
    if isinstance(horizon, HorizonPack):
        # `label_pretty` уже содержит label («⚡ 1-3 дня»), не дублируем в скобках.
        lines.append(f"⏱ *Горизонт:* {horizon.label_pretty}")
    lines.extend([
        "",
        f"🎯 *��ердикт:* {verdict_emoji} *{verdict_label}*",
        f"📊 *Сигнал:* {stars} ({pct}%)",
    ])

    debate_block = _format_debate_summary_block(
        debate_summary=debate_summary,
        verdict_reason=verdict_reason,
        plain_language=plain_language,
    )
    if debate_block:
        lines.extend(["", *debate_block])
    elif verdict_reason:
        lines.extend(["", f"🧠 *Почему:* {verdict_reason}"])

    # Smart-money card (институциональные сигналы) — pitch differentiator.
    # Показываем после verdict/reason, перед планом. Если данных нет — пропускаем.
    sm_card = _format_smart_money_card(prices)
    if sm_card:
        lines.extend(["", sm_card])

    if only_watch:
        # Все «планы» — на самом деле watch-уровни. Меняем заголовок,
        # чтобы юзер не путал «у нас есть план» и «у нас нет плана,
        # просто следим за уровнями».
        lines.extend(["", "📊 *Сейчас не торгуем — следим за уровнями:*"])
        for w in watch_levels[:6]:
            chunks = []
            sym = (w.get("symbol") or "").strip()
            level = (w.get("level") or "").strip()
            note = (w.get("note") or "").strip()
            if sym:
                chunks.append(sym)
            if level:
                chunks.append(level)
            if note:
                chunks.append(note)
            if chunks:
                lines.append("• " + " | ".join(chunks))
    else:
        lines.extend(["", "📋 *Торговый план:*",
                      "_⚠️ Уровни ниже — для НАБЛЮДЕНИЯ, не сигналы: бэктест 2020-26 "
                      "показал, что MA-пробои на дневках убыточны. Что РЕАЛЬНО работает — "
                      "следование тренду на споте (см. низ дайджеста)._"])
        if plans:
            # Per-asset coverage: 5-6 крипто (BTC/ETH/SOL/BNB/XRP) + 6 макро
            # (SPX/NDX/GOLD/OIL/DXY/VIX) → до 11 планов в одном дайджесте.
            # PR #34: рендерим группами (Крипто / Макро) с MA-уровнями
            # из prices_dict напрямую — было 11 одинаковых bullet-строк,
            # стало читаемо. Fallback на старый рендер если prices пустой
            # или ни у одного актива нет MA (что не должно случаться, но
            # на всякий случай).
            grouped = _trading_plan_grouped_lines(plans, prices or {})
            if grouped:
                lines.extend(grouped)
            else:
                for plan in plans[:12]:
                    lines.append(f"• {_digest_plan_line(plan)}")
        elif key_trigger:
            lines.append(f"• {key_trigger}")
        else:
            lines.append("• Явной сделки нет — ждём подтверждения по триггерам.")

        if watch_levels:
            lines.extend(["", "👁 *Наблюдение (без сделки):*"])
            for w in watch_levels[:8]:
                chunks = []
                sym = (w.get("symbol") or "").strip()
                level = (w.get("level") or "").strip()
                note = (w.get("note") or "").strip()
                if sym:
                    chunks.append(sym)
                if level:
                    chunks.append(level)
                if note:
                    chunks.append(note)
                if chunks:
                    lines.append("• " + " | ".join(chunks))

    if key_trigger and not any(key_trigger.lower() in p.lower() for p in monitoring_points):
        lines.extend(["", f"👀 *Ключевой триггер:* {key_trigger}"])

    if invalidation:
        lines.extend(["", f"🛑 *Инвалидация сценария:* {invalidation}"])

    if monitoring_points:
        lines.extend(["", "👀 *Точки наблюдения:*"])
        for point in monitoring_points[:4]:
            lines.append(f"• {point}")

    if plain_language and not debate_block:
        lines.extend(["", f"💬 *Простыми словами:* {plain_language}"])

    if eli5:
        lines.extend(["", f"👶 *Как 5-летнему:* {eli5}"])

    # «Кто думал» — для пиtch'а: видно что это не один LLM, а debate из
    # 4 разных моделей по ролям (Bull/Bear/Verifier/Synth). Делаем
    # компактно одной строкой; полный _format_report() остаётся в
    # «Полные дебаты» с расширенной версией.
    try:
        from ai_provider import MODELS_USED
        roles = []
        for role_key, role_emoji in (
            ("bull", "🐂"),
            ("bear", "🐻"),
            ("verifier", "🔍"),
            ("synth", "⚖️"),
        ):
            model_label = MODELS_USED.get(role_key)
            if model_label:
                # Сокращаем длинные label'ы (например «OpenRouter/Llama 3.3 70B»
                # → «Llama-3.3-70B») чтобы строка влезала в одну Telegram-стрку.
                short = model_label.split("/", 1)[-1].split(" 🚀")[0].split(" 🧠")[0]
                roles.append(f"{role_emoji} {short}")
        if roles:
            lines.extend(["", "🤖 *Кто думал:* " + " · ".join(roles)])
    except Exception:
        pass

    lines.extend([
        "",
        "📜 Полный raw-ответ модели и полные дебаты доступны кнопками ниже.",
        "",
        "📈 *Что РЕАЛЬНО работает* (проверено бэктестом 2020-26, не угадайка цены): "
        "следование тренду на споте — держим активы выше SMA, уходим в стейбл ниже. Без плеча и шортов.",
    ])

    return ["\n".join(lines)]


async def send_debates_attachment(chat_id: int, rounds: list[str]) -> None:
    """
    Все раунды одним .txt в чат — не зависит от RAM/Redis/SQLite после редеплоя Railway.
    Пользователь всегда может открыть файл в истории сообщений.
    """
    if not rounds:
        return
    blocks: list[str] = []
    for i, r in enumerate(rounds, 1):
        blocks.append(f"{'═' * 12} Раунд {i} {'═' * 12}\n\n{debate_plain_text(r)}")
    body = "\n\n".join(blocks)
    raw = body.encode("utf-8")
    max_bytes = 48 * 1024 * 1024  # лимит Telegram ~50 MiB
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        body = raw.decode("utf-8", errors="ignore") + "\n\n…файл обрезан по лимиту Telegram"
        raw = body.encode("utf-8")
    fn = f"dialectic_debates_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt"
    try:
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(raw, filename=fn),
            caption=(
                "📖 Все раунды дебатов в файле — остаётся в этом чате даже если бот перезапустился."
            ),
        )
    except Exception as e:
        logger.warning("Не удалось отправить файл дебатов: %s", e)


async def send_full_report_attachment(chat_id: int, report: str) -> None:
    """Send the raw full model report as a text attachment."""
    if not report:
        return
    raw = report.encode("utf-8")
    max_bytes = 48 * 1024 * 1024
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        report = raw.decode("utf-8", errors="ignore") + "\n\n...[truncated by Telegram size limit]"
        raw = report.encode("utf-8")
    filename = f"dialectic_full_report_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt"
    try:
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(raw, filename=filename),
            caption="📜 Полный raw-ответ модели целиком.",
        )
    except Exception as e:
        logger.warning("РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ РїРѕР»РЅС‹Р№ СЃС‹СЂРѕР№ РѕС‚С‡С‘С‚: %s", e)


async def send_digest_chart(
    chat_id: int,
    report: str,
    prices_dict: dict,
    stars_str: str,
    pct_val: int,
) -> None:
    try:
        buf = generate_main_chart(report, prices_dict or {}, stars_str, pct_val)
        if not buf:
            return
        raw = buf.getvalue() if hasattr(buf, "getvalue") else buf.read()
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(raw, filename="dialectic_edge.png"),
        )
    except Exception as e:
        logger.warning("Карточка-график не отправлена: %s", e)


async def send_russia_chart_photo(chat_id: int, report: str) -> None:
    try:
        buf = generate_russia_chart(report)
        if not buf:
            return
        raw = buf.getvalue() if hasattr(buf, "getvalue") else buf.read()
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(raw, filename="russia_edge.png"),
        )
    except Exception as e:
        logger.warning("Карточка /russia не отправлена: %s", e)


async def send_daily_digest_bundle(
    chat_id: int,
    user_id: int,
    report: str,
    prices_dict: dict,
    horizon: HorizonPack | str | None = None,
) -> None:
    """Текст дайджеста + график (после первого блока) + клавиатура.

    `horizon` (если задан) рендерится отдельной строкой в шапке дайджеста,
    чтобы юзер видел под какой горизонт построены план/стопы/R/R.
    """
    parts = parse_report_parts(report)
    pct_val, stars_str = extract_signal_pct_and_stars(report)
    hid = hydrate_debate_from_report(report)
    if hid:
        # `total` нужен refactor-хэндлеру навигации по раундам.
        hid["total"] = len(hid.get("rounds", []) or [])
        debate_cache[user_id] = hid
    else:
        rounds_fb = parts["rounds"]
        debate_cache[user_id] = {"rounds": rounds_fb, "full": report, "total": len(rounds_fb or [])}
    try:
        await save_debate_session(user_id, report)
    except Exception as e:
        logger.warning("save_debate_session: %s", e)
    try:
        await save_debate_redis(user_id, report)
    except Exception as e:
        logger.warning("save_debate_redis: %s", e)
    try:
        storage.save_user_debate_snapshot(user_id, report)
    except Exception as e:
        logger.warning("save_user_debate_snapshot: %s", e)

    pack = horizon if isinstance(horizon, HorizonPack) else (
        get_horizon(horizon) if horizon is not None else None
    )
    messages = build_short_report(parts, stars_str, pct_val, horizon=pack, prices=prices_dict or {})
    logger.info(f"Отправляю {len(messages)} сообщений. Размеры: {[len(m) for m in messages]}")

    rounds_out = debate_cache.get(user_id, {}).get("rounds") or []

    # PR #34: кэшируем plans+prices для кнопки «📊 Показать таблицу плана».
    # plans берём из digest_context (он же используется в build_short_report
    # для рендера grouped-layout). Если plans пуст — кнопку не покажем,
    # чтобы не клацать впустую и не путать юзера. Передаём quant_verdict_map
    # для консистентности с основным digest-блоком (reconcile LLM↔quant).
    digest_ctx = build_digest_context(
        report or "",
        quant_verdict_map=_quant_map_from_prices(prices_dict),
    )
    plans_for_table = digest_ctx.get("plans") or []
    has_plan_table = bool(plans_for_table) and bool(prices_dict)
    if has_plan_table:
        _plan_table_cache[user_id] = (list(plans_for_table), dict(prices_dict))

    keyboard = main_report_keyboard(
        user_id,
        has_debates=bool(rounds_out),
        has_plan_table=has_plan_table,
    )

    # Один основной digest-блок: к нему прицепляем клавиатуру с двумя кнопками
    # ("📜 Показать всё" + "📖 Полные дебаты агентов") — чтобы строка
    # "Полный raw-ответ модели и полные дебаты доступны кнопками ниже"
    # действительно заканчивалась кнопками без отдельного "Полный анализ выше"
    # сообщения посередине.
    for i, msg in enumerate(messages):
        logger.info(f"Отправляю чанк {i+1}/{len(messages)}, размер: {len(msg)}")
        is_last = i == len(messages) - 1
        await bot.send_message(
            chat_id,
            clean_markdown(msg),
            parse_mode="Markdown",
            reply_markup=keyboard if is_last else None,
        )
        if i == 0:
            await send_digest_chart(chat_id, report, prices_dict or {}, stars_str, pct_val)
        if not is_last:
            await asyncio.sleep(0.3)

    # Полные дебаты файлом — пользователь забирает их и кнопкой "📖 Полные
    # дебаты агентов" (callback `debate:{user_id}:0`), и сразу здесь как
    # неубиваемое txt-вложение, чтобы рестарт Railway не уничтожил историю.
    if rounds_out:
        await asyncio.sleep(0.25)
        await send_debates_attachment(chat_id, rounds_out)


def main_report_keyboard(user_id: int, has_debates: bool = True, has_plan_table: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура под основным отчётом."""
    buttons = []
    # «🎯 Стратегия по рынку» наверху — главная action-кнопка: бот считает
    # макро-фон (S&P EMA200/SMA50 + breadth + DXY) и подбирает стратегию,
    # которая ему соответствует: либо конкретный план (вход/стоп/цель/
    # размер), либо чёткое «торговать не надо + условия флипа». Без воды.
    # Раньше кнопка называлась «БАБЛО» — оказалось это была фигура речи
    # юзера, не имя. Переименовали в нейтральное.
    buttons.append([
        InlineKeyboardButton(
            text="🎯 Стратегия по рынку",
            callback_data=f"money:{user_id}"
        )
    ])
    # PR #34: кнопка «📊 Показать таблицу плана» — рисует план в виде PNG.
    # Раньше план был 11 одинаковых bullet-строк, читать тяжело; новая
    # таблица сгруппирована (Крипта / Макро), color-coded по статусу.
    # Текстовый grouped-формат и так в дайджесте, но картинку удобнее
    # скриншотить / показывать другим.
    if has_plan_table:
        buttons.append([
            InlineKeyboardButton(
                text="📊 Показать таблицу плана",
                callback_data=f"plantable:{user_id}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="📜 Показать всё",
            callback_data=f"fullreport:{user_id}"
        )
    ])
    if has_debates:
        # Первый клик идёт в `debate_open:` (НЕ `debate:`!): он отправляет
        # НОВОЕ сообщение с раундом 1, а не редактирует дайджест. Раньше
        # callback был `debate:UID:0`, который попадал в общий nav-хэндлер
        # — тот делал `callback.message.edit_text(...)`, и дайджест
        # затирался первой страницей дебатов. Юзер терял вердикт/сигнал/
        # стратегию и не мог вернуться. Теперь дайджест остаётся,
        # а навигация работает в отдельном сообщении.
        buttons.append([
            InlineKeyboardButton(
                text="📖 Полные дебаты агентов",
                callback_data=f"debate_open:{user_id}"
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="👍 Полезно", callback_data=f"fb:1:daily"),
        InlineKeyboardButton(text="👎 Мимо",    callback_data=f"fb:-1:daily"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Обработчик листания дебатов ──────────────────────────────────────────────

@dp.callback_query(F.data.startswith("debate_open:"))
async def handle_debate_open_callback(callback: CallbackQuery):
    """Первый клик «📖 Полные дебаты агентов» с дайджеста.

    Отправляет НОВОЕ сообщение с первым раундом + nav-клавиатурой. Не
    редактирует дайджест-сообщение, поэтому вердикт/сигнал/стратегия
    остаются нетронутыми и доступными после возврата к чату.
    Дальнейшая навигация по раундам (callback `debate:UID:N`) уже
    редактирует это новое сообщение — не дайджест.
    """
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    try:
        kb_uid = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    if kb_uid != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return
    await callback.answer()
    await show_debate_round(callback.message, callback.from_user.id, 0)


@dp.callback_query(F.data.startswith("debate:"))
async def handle_debate_page(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    if parts[1] == "noop" or parts[2] == "noop":
        await callback.answer()
        return
    try:
        kb_uid = int(parts[1])
        round_idx = int(parts[2])
    except ValueError:
        await callback.answer()
        return
    if kb_uid != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return
    await handle_debate_navigation_callback(callback, callback.from_user.id, round_idx)


@dp.callback_query(F.data.startswith("fullreport:"))
async def handle_full_report_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    try:
        kb_uid = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    if kb_uid != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return

    debate = await get_debate_handler().get_debate(callback.from_user.id)
    full_report = (debate or {}).get("full", "")
    if not full_report:
        await callback.answer("Полный отчёт не найден", show_alert=True)
        return

    await callback.answer("Отправляю полный raw-отчёт")
    await send_full_report_attachment(callback.message.chat.id, full_report)


# PR #34: callback для кнопки «📊 Показать таблицу плана».
# Берёт (plans, prices) из in-memory `_plan_table_cache`, заполняемого в
# send_daily_digest_bundle, и рисует PNG через chart_generator. На рестарт
# Railway не рассчитываем — кэш умирает с процессом, юзер просто
# перезапустит /daily, что и так делает каждый день.
@dp.callback_query(F.data.startswith("plantable:"))
async def handle_plan_table_callback(callback: CallbackQuery):
    parts_ = callback.data.split(":")
    if len(parts_) != 2:
        await callback.answer()
        return
    try:
        kb_uid = int(parts_[1])
    except ValueError:
        await callback.answer()
        return
    if kb_uid != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return

    cached = _plan_table_cache.get(callback.from_user.id)
    if not cached:
        await callback.answer(
            "Таблица недоступна — запусти /daily заново",
            show_alert=True,
        )
        return

    plans, prices = cached
    try:
        buf = generate_trading_plan_png(prices, plans)
    except Exception as e:
        logger.warning("plan table png failed: %s", e)
        buf = None
    if not buf:
        await callback.answer("Не удалось собрать таблицу", show_alert=True)
        return

    raw = buf.getvalue() if hasattr(buf, "getvalue") else buf.read()
    await callback.answer("Отправляю таблицу плана")
    await bot.send_photo(
        callback.message.chat.id,
        photo=BufferedInputFile(raw, filename="trading_plan.png"),
        caption="📊 Торговый план — MA50 / MA200 триггеры",
    )


def _money_format_price(value) -> str:
    """Деньги: $79,502.20. None/мусор → «—»."""
    if value is None or value == "":
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)[:20] or "—"
    if f >= 1000:
        return f"${f:,.2f}"
    if f >= 1:
        return f"${f:,.4f}".rstrip("0").rstrip(".")
    return f"${f:.6f}".rstrip("0").rstrip(".")


def _eli5_for_actionable_trade(plan: dict) -> str:
    """Объясняет одну actionable-сделку «как пятилетнему».

    Rule-based, никаких LLM-вызовов — кнопка должна отвечать мгновенно.
    Берёт direction/entry/stop/target/size и собирает понятную ��разу.
    """
    sym = (plan.get("symbol") or "?").upper()
    direction = (plan.get("direction") or "").upper()
    entry = plan.get("entry")
    stop = plan.get("stop")
    target = plan.get("target")
    size = str(plan.get("size") or "").strip()

    # Имена в винительном падеже (объект действия) для разговорной речи.
    # «Покупаем биткоин», «шортим эфир» — звучит естестве��но.
    asset_accusative = {
        "BTC": "биткоин",
        "ETH": "эфир",
        "SOL": "солану",
        "XRP": "XRP",
        "BNB": "BNB",
        "DOGE": "додж",
        "ADA": "кардано",
        "TON": "тон",
    }.get(sym, sym)

    verb = "По����упаем" if direction == "LONG" else "Шортим"

    parts = [f"{verb} {asset_accusative} по {_money_format_price(entry)}."]

    if stop:
        if direction == "LONG":
            parts.append(
                f"Если упадёт до {_money_format_price(stop)} — выходим "
                f"(это страховка от убытка)."
            )
        else:
            parts.append(
                f"Если вырастет до {_money_format_price(stop)} — выходим "
                f"(страховка от убытка)."
            )

    if target:
        if direction == "LONG":
            parts.append(
                f"Если вырастет до {_money_format_price(target)} — "
                f"забираем профит."
            )
        else:
            parts.append(
                f"Если упадёт до {_money_format_price(target)} — "
                f"забираем профит."
            )

    if size:
        parts.append(f"Кладём {size} депозита, не больше.")

    return " ".join(parts)


def _eli5_for_watch_only(watch_levels: list[dict]) -> str:
    """Объясняет «торговать не надо + ждём триггер» как пятилетнему.

    Берёт первые 3 watch-уровня и собирает фразу «сидим, ждём, если X — то Y»."""
    asset_name = {
        "BTC": "биткоин",
        "ETH": "эфир",
        "SOL": "солана",
        "XRP": "XRP",
        "BNB": "BNB",
        "DOGE": "додж",
        "ADA": "кардано",
        "TON": "тон",
    }

    parts = ["Сейчас ничего не делаем — рынок без явного направления."]
    triggers_described = []
    for w in (watch_levels or [])[:3]:
        sym = (w.get("symbol") or "").strip().upper()
        if not sym:
            continue
        note = (w.get("note") or "").strip()
        level = (w.get("level") or "").strip()
        if not note and not level:
            continue
        name = asset_name.get(sym, sym)
        # Простая эвристика: понимаем "пробой $X вниз → откроем SHORT"
        # и переводим на разговорный.
        note_lower = note.lower()
        is_short_signal = (
            "shor" in note_lower or "вниз" in note_lower or
            "падени" in note_lower or "продад" in note_lower
        )
        is_long_signal = (
            "long" in note_lower or "вверх" in note_lower or
            "выше" in note_lower or "купим" in note_lower or
            "откроем long" in note_lower
        )
        # Цена ВСЕГДА берётся из поля `level` (там ре��льный уровень $82608),
        # а не из `note` (там может быть «MA200 — ключевое сопротивление…»,
        # и regex случайно вытаскивал «200» из «MA200» как цену → юзеру
        # показывалось «закроет свечу выше $200» вместо $82608. Если в level
        # цены нет (free-form watch) — фоллбэчим на $ из note (только со
        # знаком $, чтобы MA200/MA50/MA50W не ловились как цены).
        import re as _re
        price_str = ""
        # Match prices like "$80 167", "$1,670", "$2.528", "$106".
        # Space/thin-space between digit groups is common in Russian formatting.
        price_match = _re.search(
            r"\$\s*([\d][\d\s,]*[\d](?:\.\d+)?|\d+(?:\.\d+)?)\s*[KkКк]?",
            level,
        )
        if not price_match:
            price_match = _re.search(
                r"\$\s*([\d][\d\s,]*[\d](?:\.\d+)?|\d+(?:\.\d+)?)\s*[KkКк]?",
                note,
            )
        if price_match:
            try:
                raw_price = price_match.group(1).replace(",", "").replace(" ", "")
                p_val = float(raw_price)
                price_str = f" ${p_val:,.0f}" if p_val >= 100 else f" ${p_val:.2f}"
            except (ValueError, TypeError):
                pass

        if is_long_signal and price_str:
            triggers_described.append(
                f"если {name} закроет 4h-свечу выше{price_str} — "
                f"покупаем"
            )
        elif is_short_signal and price_str:
            triggers_described.append(
                f"если {name} упадёт ниже{price_str} — продаём (шорт)"
            )
        elif price_str:
            triggers_described.append(
                f"следим за {name}{price_str}"
            )

    if triggers_described:
        parts.append("Условия для входа: " + "; ".join(triggers_described) + ".")

    parts.append("До этого — сидим и не дёргаемся. «Не торговать» — это тоже решение.")
    return " ".join(parts)


def format_money_button_message(report_text: str, macro=None) -> str:
    """Сборка сообщения для кнопки «🎯 Стратегия по рынку».

    Логика:
    - Считаем макро-фон (S&P EMA200/SMA50 + breadth + DXY).
    - Если макро RISK_OFF → лонги отбрасываем, оставляем только шорты.
      Если RISK_ON → наоборот.
    - Если есть хоть один разрешённый LONG/SHORT-план → показываем
      КОНКРЕТНУЮ сделку (вход / стоп / цель / R/R / размер / какой ордер
      ставить).
    - Если планов нет / все CASH демоутнуты в watch / все планы зарезаны
      макро-фильтром → говорим «торговать НЕ надо» + условия флипа из
      watch-уровней.

    Без воды. Юзер хочет одной кнопкой увидеть «делать / не делать», и
    если делать — «куда жать». Бот не должен здесь рассуждать, только
    инструкция.
    """
    ctx = build_digest_context(report_text or "")
    plans = ctx.get("plans") or []
    watch_levels = list(ctx.get("watch_levels") or [])
    verdict_label = ctx.get("verdict_label") or "Нейтральный"
    verdict_emoji = ctx.get("verdict_emoji") or "⚪️"
    invalidation = (ctx.get("invalidation") or "").strip()

    actionable = []
    cash_plans = []
    for p in plans:
        if not isinstance(p, dict):
            continue
        d = (p.get("direction") or "").upper().strip()
        if d in {"LONG", "SHORT"}:
            # Защита: LONG/SHORT без entry/stop/target → это парсер-фантом,
            # неактивно как сделка, но триггер показываем в watch.
            entry = p.get("entry")
            stop = p.get("stop")
            target = p.get("target")
            if not entry and not stop and not target:
                cash_plans.append(p)
            else:
                actionable.append(p)
        elif d in {"CASH", "WATCH", "WAIT", "FLAT"}:
            cash_plans.append(p)

    # CASH/WATCH-пл��ны с триггерами → синтезируем в watch_levels (если их там
    # ещё нет). Иначе кнопка «Стратегия» не показывает условия флипа из CASH-планов.
    seen_watch_syms = {(w.get("symbol") or "").upper() for w in watch_levels}
    for p in cash_plans:
        sym = (p.get("symbol") or p.get("label") or "?").upper()
        trigger = str(p.get("trigger") or "").strip()
        if not trigger:
            continue
        if sym in seen_watch_syms:
            continue
        watch_levels.append({"symbol": sym, "level": "", "note": trigger})
        seen_watch_syms.add(sym)

    # Макро-фильтр: убираем планы, противоречащие текущему макро-режиму.
    macro_blocked: list[dict] = []
    if macro is not None:
        kept = []
        for p in actionable:
            d = (p.get("direction") or "").upper().strip()
            if d == "LONG" and not getattr(macro, "allow_longs", True):
                macro_blocked.append(p)
                continue
            if d == "SHORT" and not getattr(macro, "allow_shorts", True):
                macro_blocked.append(p)
                continue
            kept.append(p)
        actionable = kept

    out: list[str] = []
    out.append("🎯 *Стратегия по рынку — что делать прямо сейчас*")
    out.append(f"📍 Вердикт дайджеста: {verdict_emoji} *{verdict_label}*")
    if macro is not None:
        try:
            from core.macro_regime import format_macro_block
            out.append("")
            out.append(format_macro_block(macro))
        except Exception:
            pass
    out.append("")
    # Честная плашка: directional MA-уровни доказанно убыточны на дневках.
    # Реальный подход — следование тренду на споте.
    out.append("⚠️ _Уровни ниже — для НАБЛЮДЕНИЯ, не сигналы: бэктест 2020-26 показал, "
               "что MA-пробои на дневках убыточны. Что РЕАЛЬНО работает — следование "
               "тренду на споте (держим выше SMA, в стейбле ниже)._")
    out.append("")

    if actionable:
        out.append("✅ *Конкретная сделка:*")
        for p in actionable[:3]:
            sym = (p.get("symbol") or "?").upper()
            direction = (p.get("direction") or "").upper()
            entry = _money_format_price(p.get("entry"))
            stop = _money_format_price(p.get("stop"))
            target = _money_format_price(p.get("target"))
            rr = str(p.get("rr") or "").strip() or "—"
            size = str(p.get("size") or "").strip() or "—"
            trigger = str(p.get("trigger") or "").strip()
            out.append(
                f"• *{sym} {direction}* — вход {entry}, сто�� {stop}, цель {target}, R/R {rr}, размер {size} депозита"
            )
            if trigger:
                out.append(f"  Триггер: {trigger}")
            # «Как ставить» — детерминированный how-to. Без него юзер
            # начинает гадать «лимит или маркет», ловит проскальзывание.
            tf = "4h" if direction in {"LONG", "SHORT"} else "4h"
            out.append(
                f"  ⚙️ Как ставить: stop-limit на стоп {stop}, "
                f"entry — лимит {entry} (или ждать закрытия {tf}-свечи "
                f"за уровень и брать маркет с проскальзыванием ≤0.3%), "
                f"тейк {target}."
            )
        if invalidation:
            out.append("")
            out.append(f"🛑 *Инвалидация:* {invalidation}")
        # ELI5 — для тех кто не любит читать инструкции (а это все).
        out.append("")
        out.append("👶 *По-простому:*")
        out.append(_eli5_for_actionable_trade(actionable[0]))
        out.append("")
        out.append("⚠️ Считай размер от ТВОЕГО депозита. Не подгоняй стоп под лосс — двигай размер.")
        return "\n".join(out)

    # Нет actionable планов → объясняем условия флипа из watch.
    if macro_blocked:
        out.append(
            "⏳ *Торговать НЕ надо.* Идеи в дайджесте противоречат текущему "
            "макро-режиму — открывать против тренда S&P/breadth/DXY не будем."
        )
        out.append("")
        out.append("🚫 *Зарезано макро-фильтром:*")
        for p in macro_blocked[:3]:
            sym = (p.get("symbol") or "?").upper()
            d = (p.get("direction") or "?").upper()
            out.append(f"• {sym} {d} — против макро ({getattr(macro, 'regime', '—')})")
        out.append("")
    else:
        out.append("⏳ *Торговать НЕ надо.* Все идеи — без однозначного направления.")
        out.append("")
    if watch_levels:
        out.append("📊 *Когда вернёмся в рынок (условия флипа):*")
        for w in watch_levels[:5]:
            sym = (w.get("symbol") or "").strip() or "—"
            level = (w.get("level") or "").strip()
            note = (w.get("note") or "").strip()
            chunks = [f"*{sym}*"]
            if level:
                chunks.append(level)
            if note:
                chunks.append(note)
            out.append("• " + " — ".join(chunks))
        out.append("")
        out.append(
            "Правило: ждём ЗАКРЫТИЯ 4h-свечи за уровень "
            "(не «прокол хвостом») — только тогда новый сигнал. "
            "До этого — кеш."
        )
    else:
        out.append(
            "Нет ни одного триггера с положительным ожиданием. "
            "Сидим в кеше до следующего /daily."
        )

    if invalidation:
        out.append("")
        out.append(f"🛑 *Что отменит этот сценарий:* {invalidation}")

    # ELI5 «по-простому» — без него юзер тыкает в кнопку и закрывает,
    # потому что не понимает что значит «закрытие 4h-свечи за уровень».
    if watch_levels:
        out.append("")
        out.append("👶 *По-простому:*")
        out.append(_eli5_for_watch_only(watch_levels))

    out.append("")
    out.append("⚠️ Не натягивай сделку под скуку. «Не торговать» — это тоже решение.")
    return "\n".join(out)


@dp.callback_query(F.data.startswith("money:"))
async def handle_money_button_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    try:
        kb_uid = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    if kb_uid != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return

    debate = await get_debate_handler().get_debate(callback.from_user.id)
    full_report = (debate or {}).get("full", "")
    if not full_report:
        await callback.answer("Сначала запусти /daily", show_alert=True)
        return

    await callback.answer("Считаю что делать прямо сейчас")
    macro = None
    try:
        from core.macro_regime import get_macro_regime
        macro = await get_macro_regime()
    except Exception as e:
        logger.debug("macro_regime fetch failed: %s", e)
    try:
        msg = format_money_button_message(full_report, macro=macro)
    except Exception as e:
        logger.warning("format_money_button_message failed: %s", e)
        await bot.send_message(
            callback.message.chat.id,
            "Не смог распарсить план — попробуй /daily заново.",
        )
        return
    await bot.send_message(
        callback.message.chat.id,
        clean_markdown(msg),
        parse_mode="Markdown",
    )


def format_signal_trader_status_message(status: dict) -> str:
    msg = "📡 *СИГНАЛ ТРЕЙДЕР*\n"
    msg += "═" * 25 + "\n"
    msg += f"Статус: {'✅ Работает' if status['enabled'] else '❌ Остановлен'}\n"
    msg += f"Автоторг (фича): {'✅' if status.get('autotrade_feature_on') else '⏸ env FEATURE_AUTOTRADE=0'}\n"
    msg += f"Bias Binance/Bybit: {'✅' if status.get('binance_signals_enabled') else '⏸ DATA_SOURCE…=0'}\n"
    msg += f"💵 Баланс: ${status['capital']:,.2f}\n"

    # Show capital in positions
    active_positions = status.get("active_positions", []) or []
    capital_in_positions = 0.0
    for pos in active_positions:
        entry = pos.get("entry_price", 0) or 0
        qty = pos.get("quantity", 0) or 0
        capital_in_positions += entry * qty
    if capital_in_positions > 0:
        free = status['capital'] - capital_in_positions
        msg += f"📦 В позициях: ${capital_in_positions:,.2f} | Свободно: ${free:,.2f}\n"
    msg += f"🎯 Консенсус 2-3 дайджестов: *{status.get('consensus_verdict', 'NEUTRAL')}*\n"
    if status.get("signal_follow_active"):
        msg += "📡 _Режим:_ NEUTRAL или нет планов из дайджеста — кандидаты по рыночным сигналам (как в `/markets`) + цены.\n"

    pv = status.get("latest_digest_prompt_versions") or {}
    if pv:
        ver = pv.get("digest_pipeline_version", "—")
        msg += f"\n📌 *Версия пайплайна дайджеста:* `{ver}`\n"
        if status.get("latest_digest_snapshot_utc"):
            msg += f"_Снимок входов модели (UTC):_ `{status['latest_digest_snapshot_utc'][:19]}`\n"
    else:
        msg += "\n📌 Версии промптов появятся после следующего полного `/daily`\n"

    recent_contexts = status.get("recent_contexts", []) or []
    if recent_contexts:
        msg += "\n🧠 *Последние дайджесты:*\n"
        for row in recent_contexts[:3]:
            created_at = (row.get("created_at", "") or "")[:16].replace("T", " ")
            verdict = row.get("verdict", "NEUTRAL")
            symbols = ", ".join((row.get("symbols", []) or [])[:3]) or "—"
            msg += f"• {created_at} → {verdict} | {symbols}\n"
    else:
        msg += "\n💭 Нет свежих дайджестов — нужен /daily\n"

    active_positions = status.get("active_positions", []) or []
    if active_positions:
        msg += f"\n📍 *Открытые позиции ({len(active_positions)}):*\n"
        for pos in active_positions:
            qty = pos.get("quantity", 0)
            qty_str = f" ({qty:.6f} шт)" if qty > 0 else ""
            msg += f"• {pos['symbol']} {pos['direction']} @ ${pos['entry_price']:,.2f}{qty_str}\n"
            if pos.get("target"):
                msg += f"  тейк ${pos['target']:,.2f}"
                if pos.get("stop"):
                    msg += f" | стоп ${pos['stop']:,.2f}"
                msg += "\n"
    else:
        msg += "\n📭 Открытых позиций нет\n"

    top_candidates = status.get("top_candidates", []) or []
    if top_candidates:
        msg += "\n📊 *Лучшие кандидаты сейчас:*\n"
        for candidate in top_candidates[:3]:
            signal_dir = candidate.get("signal_direction", "NEUTRAL")
            ready_mark = "✅" if candidate.get("ready") else "⏳"
            sf = " (signals)" if candidate.get("signal_follow_only") else ""
            msg += (
                f"• {candidate['symbol']} {candidate['direction']} {ready_mark}{sf}\n"
                f"  вход ${candidate['entry']:,.2f} | цена ${candidate['current_price']:,.2f}\n"
                f"  score {candidate['total_score']:.1f} | signal {signal_dir}\n"
            )
    else:
        msg += "\n📊 Подходящих кандидатов пока нет\n"

    decisions = status.get("recent_decisions") or []
    if decisions:
        msg += "\n📜 *История действий:*\n"
        for row in decisions[:5]:
            created = (row.get("created_at", "") or "")[:16].replace("T", " ")
            ctype = row.get("cycle_type", "")
            payload = row.get("payload") or {}
            if ctype == "autotrade_opened":
                ch = payload.get("chosen") or {}
                sym = ch.get('symbol', '?')
                dir = ch.get('direction', '?')
                price = ch.get('entry', 0)
                action = "🔴 Продал (Short)" if dir == "SELL" else "🟢 Купил (Long)"
                msg += f"• {created}: {action} {sym} по ${price:,.2f}\n"
            elif ctype == "autotrade_closed":
                msg += f"• {created}: Закрыл позицию\n"
            elif ctype == "autotrade_skip_not_ready":
                pass  # Пропускаем логи пропусков
            else:
                msg += f"• {created}: {ctype}\n"

    msg += "\n" + "═" * 25 + "\n"
    msg += f"💰 Всего закрытых сделок: {status['total_trades']}\n"
    msg += f"📈 Total PnL: ${status['total_pnl']:+,.2f}\n"

    # Session info
    if status.get("session_id"):
        msg += "\n" + "═" * 25 + "\n"
        msg += f"🔄 Сессия #{status['session_id']}\n"
        if status.get("session_start"):
            msg += f"Старт: {status['session_start']}\n"
        msg += f"Сделок в сессии: {status.get('session_trades', 0)}\n"
        msg += f"PnL сессии: ${status.get('session_pnl', 0):+,.2f}\n"
        if status.get("past_sessions", 0) > 0:
            msg += f"Прошлых сессий: {status['past_sessions']}\n"

    # Adaptive params
    ap = status.get("adaptive_params") or {}
    if ap:
        msg += "\n" + "═" * 25 + "\n"
        msg += "⚙️ Адаптивные параметры:\n"
        if "open_score_threshold" in ap:
            msg += f"Порог входа: {ap['open_score_threshold']:.1f}\n"
        if "neutral_sl_pct" in ap:
            msg += f"Стоп: {ap['neutral_sl_pct']:.2%}\n"
        if "quantity_pct" in ap:
            msg += f"Размер позиции: {ap['quantity_pct']:.1%}\n"
    return msg


@dp.message(F.text.startswith("/signalstatus"))
async def cmd_signal_status(message: Message):
    """[в разработке] Раньше — панель paper-автотрейдера. Теперь заглушка."""
    await _send_autotrade_coming_soon(message)


def _format_autotrade_status_embed(risk_summary: dict, status: dict) -> str:
    """Красивый embed для /autotrade_status — performance + risk-state."""
    drawdown = risk_summary.get("drawdown_pct", 0)
    win_rate = risk_summary.get("win_rate", 0)
    total = risk_summary.get("total_trades", 0)
    wins = risk_summary.get("wins", 0)
    losses = risk_summary.get("losses", 0)
    avg_win = risk_summary.get("avg_win", 0)
    avg_loss = risk_summary.get("avg_loss", 0)
    kelly = risk_summary.get("kelly_pct", 0)
    using_history = risk_summary.get("kelly_using_history", False)
    target_vol = risk_summary.get("target_vol_pct", 3.0)
    capital = risk_summary.get("current_capital", 0)
    peak = risk_summary.get("peak_capital", 0)
    total_pnl = risk_summary.get("total_pnl", 0)

    # R-ratio: avg_win / avg_loss
    rr = (avg_win / avg_loss) if avg_loss else 0
    # Expectancy в процентах: p*W - (1-p)*L
    p = win_rate / 100
    expectancy = (p * avg_win - (1 - p) * avg_loss) if total else 0

    # Sharpe-эквивалент (упрощённо: avg_pnl / std). На малых выборках ничего не считаем.

    msg = "🎯 *AUTOTRADE — STATUS*\n"
    msg += "═" * 28 + "\n\n"

    # Capital
    msg += "💰 *Капитал*\n"
    msg += f"  Текущий: ${capital:,.2f}\n"
    msg += f"  Peak: ${peak:,.2f}\n"
    if drawdown > 0.1:
        emoji = "🔴" if drawdown > 15 else "🟡"
        msg += f"  {emoji} Drawdown: {drawdown:.1f}%\n"
    else:
        msg += f"  🟢 Drawdown: {drawdown:.1f}%\n"
    msg += f"  Cumulative PnL: {total_pnl:+.2f}%\n\n"

    # Performance
    msg += "📊 *Performance*\n"
    if total == 0:
        msg += "  _Нет закрытых сделок — нечего показать._\n"
        msg += "  Откроется автоматически при первой закрытой сделке.\n\n"
    else:
        emoji = "🟢" if win_rate >= 50 else "🔴"
        msg += f"  {emoji} Win-rate: {win_rate:.1f}% ({wins}W / {losses}L)\n"
        msg += f"  Avg win: +{avg_win:.2f}%  |  Avg loss: -{avg_loss:.2f}%\n"
        msg += f"  R-ratio: {rr:.2f}  |  Expectancy: {expectancy:+.2f}%\n\n"

    # Risk Engine
    msg += "⚙️ *Risk Engine*\n"
    if using_history:
        msg += f"  🟢 Kelly активен (на реальной истории): {kelly:.2f}%\n"
    else:
        msg += f"  🟡 Kelly: bootstrap-режим (база {kelly:.2f}%)\n"
        msg += f"  _Нужно ≥10 закрытых сделок для динамического Kelly._\n"
    msg += f"  Target vol (vol-targeting): {target_vol:.1f}%\n\n"

    # Active positions
    active = status.get("active_positions", []) or []
    if active:
        msg += f"📍 *Открытых позиций: {len(active)}*\n"
        for pos in active[:5]:
            msg += f"  • {pos['symbol']} {pos['direction']} @ ${pos.get('entry_price', 0):,.2f}\n"
    else:
        msg += "📭 Открытых позиций нет\n"

    return msg


@dp.message(Command("autotrade_status"))
async def cmd_autotrade_status(message: Message):
    """[в разработке] Раньше — performance paper-автотрейда. Теперь заглушка."""
    await _send_autotrade_coming_soon(message)


@dp.message(Command("audit"))
async def cmd_audit(message: Message):
    """AI self-audit — LLM смотрит на закрытые сделки за неделю и пишет review."""
    try:
        from core.audit import (
            parse_recent_trades_from_md,
            build_audit_prompt,
            format_audit_for_telegram,
        )
        from signal_trader import _risk_manager
        from ai_provider import AgentProvider

        # Парсим параметры: /audit или /audit 14
        parts = (message.text or "").split()
        days = 7
        if len(parts) > 1:
            try:
                days = max(1, min(90, int(parts[1])))
            except ValueError:
                pass
        period_str = f"{days} дней" if days != 7 else "неделю"

        backtest_path = Path(__file__).parent / "BACKTEST.md"
        trades = parse_recent_trades_from_md(str(backtest_path), days=days)

        if not trades:
            await message.answer(
                f"📊 *AI Self-Audit ({period_str})*\n\n"
                f"За {period_str} нет закрытых сделок — анализировать нечего.\n"
                f"Откроется при первых закрытиях.",
                parse_mode="Markdown",
            )
            return

        await message.answer(f"🔍 Анализирую {len(trades)} закрытых сделок за {period_str}…")

        risk_summary = _risk_manager.get_risk_summary()
        prompt = build_audit_prompt(trades, risk_summary=risk_summary, period=period_str)

        # Используем verifier-роль (gpt-oss 120B по дефолту) — для аудита нужен
        # точный, не bullish/bearish-агент.
        provider = AgentProvider()
        sys_msg = "Ты — risk officer количественного фонда. Отвечай по существу, на русском."
        try:
            audit_text = await provider.verifier(prompt=prompt, system=sys_msg, temperature=0.4)
        except Exception as agent_err:
            logger.warning(f"audit: verifier agent failed, fallback to synth: {agent_err}")
            audit_text = await provider.synth(prompt=prompt, system=sys_msg, temperature=0.4)

        msg = format_audit_for_telegram(audit_text, len(trades), period_str)
        await message.answer(msg, parse_mode="Markdown")
    except Exception as e:
        logger.exception("audit error")
        await message.answer(f"Ошибка self-audit: {e}")


@dp.message(Command("provenance"))
async def cmd_provenance(message: Message):
    """Декодирует «почему бот выбрал этот сигнал» — replay сохранённых решений.

    Usage:
      /provenance          — последние 5 решений по всем активам
      /provenance BTC      — последние 5 решений по BTC
      /provenance 42       — детально одно решение по ID

    Источник правды: таблица `decision_provenance` (см. core/provenance.py).
    Каждый /signal и /markets морозит свой snapshot — features, score
    breakdown, git SHA, σ̂, SL/TP. Без provenance вопрос «почему мы вошли
    в шорт SOL вчера» не имеет ответа.
    """
    try:
        from core.provenance import (
            format_provenance_telegram,
            get_provenance,
            get_recent_provenances,
        )

        parts = (message.text or "").split()
        arg = parts[1].strip().upper() if len(parts) > 1 else ""

        # Если аргумент — число, это ID конкретной записи.
        if arg.isdigit():
            prov_id = int(arg)
            prov = await get_provenance(prov_id)
            if not prov:
                await message.answer(f"⚠️ Provenance #{prov_id} не найдена.")
                return
            await message.answer(format_provenance_telegram(prov))
            return

        # Иначе — последние N (фильтр по asset если передан).
        asset_filter = arg if arg else None
        records = await get_recent_provenances(asset=asset_filter, limit=5)
        if not records:
            asset_str = f" для {asset_filter}" if asset_filter else ""
            await message.answer(
                f"📭 Provenance{asset_str} пуста. Запусти /signal или /markets — "
                f"и решения начнут писаться."
            )
            return

        lines = ["🔍 *Последние решения движка*", ""]
        for r in records:
            direction_emoji = "📈" if "LONG" in r["direction"] else "📉" if "SHORT" in r["direction"] else "⚪"
            lines.append(
                f"#{r['id']} {r['created_at'][:16]} "
                f"{direction_emoji} *{r['asset']}* → *{r['direction']}* "
                f"(score {r.get('score', 0)}, {r['decision_type']})"
            )
        lines.append("")
        lines.append("Чтобы посмотреть д��тали: `/provenance <ID>`")
        await message.answer("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.exception("provenance error")
        await message.answer(f"Ошибка provenance: {e}")


@dp.message(Command("calibration"))
async def cmd_calibration(message: Message):
    """Per-signal калибровка — отвечает на вопрос «какие сигналы реально работают».

    Usage:
      /calibration                — общая сводка за 30 дней
      /calibration 7              — за 7 дней
      /calibration drift          — concept drift (recent 14д vs baseline 60д)
      /calibration BTC            — фильтр по активу
      /calibration BTC 14         — BTC за 14 дней

    Источник данных: `decision_provenance` (PR #1) ⨝ `predictions` через
    prediction_id или fuzzy-join по asset+direction+time. Метрики:
      • Hit-rate (wins / wins+losses)
      • Brier score (≤ 0.20 = реально калиброван)
      • Reliability bins (10 бинов confidence vs realized hit-rate)
      • Signal attribution (какой компонент score breakdown'а лучше
        разделяет wins/losses)
    """
    try:
        from core.calibration import (
            breakdown_by_asset,
            breakdown_by_decision_type,
            breakdown_by_direction,
            breakdown_by_regime,
            compute_overall_stats,
            compute_reliability_diagram,
            compute_signal_attribution,
            detect_concept_drift,
            format_attribution_telegram,
            format_breakdown_telegram,
            format_drift_telegram,
            format_overall_telegram,
            format_reliability_telegram,
            link_provenance_outcomes,
        )

        parts = (message.text or "").split()
        args = [p.strip() for p in parts[1:]]

        # ─── /calibration drift ─────────────────────────────────────────
        if args and args[0].lower() == "drift":
            drift = await detect_concept_drift(
                recent_days=14, baseline_days=60
            )
            await message.answer(format_drift_telegram(drift), parse_mode="Markdown")
            return

        # ─── Parsing аргументов: actor (asset) + window_days ────────────
        asset_filter: Optional[str] = None
        window_days = 30
        for a in args:
            if a.isdigit():
                window_days = max(1, min(365, int(a)))
            else:
                # ASSET-фильтр: только UPPERCASE токены.
                asset_filter = a.upper()

        linked = await link_provenance_outcomes(
            window_days=window_days, asset=asset_filter
        )
        overall = compute_overall_stats(linked)

        if overall["n_total"] == 0:
            scope = f" по {asset_filter}" if asset_filter else ""
            await message.answer(
                f"📭 Provenance{scope} пуста за {window_days}д. Запусти /signal или "
                f"/markets — и решения начнут писаться, после закрытия prediction'ов "
                f"калибровка появится.",
                parse_mode="Markdown",
            )
            return

        blocks: list[str] = []
        blocks.append(format_overall_telegram(overall, window_days))

        if overall["is_reliable"]:
            blocks.append(
                format_breakdown_telegram(
                    breakdown_by_direction(linked),
                    title="По направлению (LONG vs SHORT)",
                )
            )
            blocks.append(
                format_breakdown_telegram(
                    breakdown_by_asset(linked), title="По активам"
                )
            )
            blocks.append(
                format_breakdown_telegram(
                    breakdown_by_decision_type(linked),
                    title="По типу решения",
                )
            )
            blocks.append(
                format_breakdown_telegram(
                    breakdown_by_regime(linked),
                    title="По режиму (тренд)",
                )
            )
            blocks.append(
                format_reliability_telegram(
                    compute_reliability_diagram(linked, n_bins=10)
                )
            )
            blocks.append(
                format_attribution_telegram(compute_signal_attribution(linked))
            )
            blocks.append(
                "💡 Команды: `/calibration drift` для concept-drift verdict."
            )

        text = "\n\n".join(b for b in blocks if b)
        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        logger.exception("calibration error")
        await message.answer(f"Ошибка calibration: {e}")


@dp.message(Command("wfbacktest"))
async def cmd_wfbacktest(message: Message):
    """Walk-forward backtest изотонической калибровки — честная OOS оценка.

    Usage:
      /wfbacktest                  — окно 30д, train 14д / test 7д / step 7д
      /wfbacktest 60               — окно 60 дней, дефолтные train/test/step
      /wfbacktest BTC              — фильтр по активу
      /wfbacktest 60 14 7          — окно 60д, train 14д, test 7д (step=test)

    Зачем: `/calibration` показывает калибровку **in-sample** (на всех закрытых
    сделках). Это переоценка — модель не училась исторически, она просто
    видела все точки сразу.

    `/backtest` делает rolling walk-forward: фитит isotonic на train-окне,
    применяет к следующему test-окну, считает out-of-sample Brier для raw vs
    calibrated. Затем агрегирует через все фолды.

    Verdict:
      * `CALIBRATION_HELPS` — средний OOS Brier с калибровкой < без + больше
        половины фолдов улучшились → можно включать в продакшен.
      * `RAW_BETTER` — калибровка не улучшает (рано включать).
      * `INSUFFICIENT_DATA` — мало resolved-сделок в окне.

    Источник данных: `decision_provenance` (PR #1) ⨝ `predictions`.
    """
    try:
        from core.walk_forward import (
            DEFAULT_TEST_DAYS,
            DEFAULT_TOTAL_DAYS,
            DEFAULT_TRAIN_DAYS,
            format_backtest_folds_telegram,
            format_backtest_telegram,
            walk_forward_backtest,
        )

        parts = (message.text or "").split()
        args = [p.strip() for p in parts[1:]]

        # Parsing: каждый аргумент либо число (день), либо актив (UPPERCASE).
        nums: list[int] = []
        asset_filter: Optional[str] = None
        for a in args:
            if a.isdigit():
                nums.append(max(1, min(365, int(a))))
            else:
                asset_filter = a.upper()

        # Распакуем числа: [total], [total, train], [total, train, test], ...
        window_days = nums[0] if len(nums) >= 1 else DEFAULT_TOTAL_DAYS
        train_days = nums[1] if len(nums) >= 2 else DEFAULT_TRAIN_DAYS
        test_days = nums[2] if len(nums) >= 3 else DEFAULT_TEST_DAYS
        step_days = nums[3] if len(nums) >= 4 else test_days

        result = await walk_forward_backtest(
            window_days=window_days,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            asset=asset_filter,
        )

        blocks = [format_backtest_telegram(result)]
        if result["folds"]:
            blocks.append(format_backtest_folds_telegram(result, limit=5))
        blocks.append(
            "💡 Когда verdict станет `CALIBRATION_HELPS` — можно поднять "
            "`FEATURE_RECALIBRATE=1` и применять калибровку к live-score."
        )

        await message.answer("\n\n".join(blocks), parse_mode="Markdown")
    except Exception as e:
        logger.exception("wfbacktest error")
        await message.answer(f"Ошибка wfbacktest: {e}")


@dp.message(Command("postmortem"))
async def cmd_postmortem(message: Message):
    """Post-mortem дайджеста: что мы сказали vs что произошло через 24ч.

    Usage:
      /postmortem                — анализ последнего дайджеста (≥24ч назад)
      /postmortem 18.05.2026     — анализ конкретного дня (DD.MM.YYYY)

    Зачем: до этого PR `/daily` выдавал вердикт и забывал. Калибровка
    (PR #24/#25) видела только `/signal` и `/markets`, не daily. Теперь
    каждый дайджест проверяется ровно через 24ч (или вручную), классифи-
    цируется как hit/miss/flat/no_data и попадает в `predictions` →
    `/calibration` начинает учитывать ещё и daily-direction-calls.

    Источник правды: парсер `auto_tracker.DigestParser` (тот же, что
    наполняет AUTO_TRACK.md, чтобы не плодить regex) + Yahoo entry/eval.

    Feature-flag: `FEATURE_POST_MORTEM=1` (включает scheduler-job, но не
    отключает эту команду — она всегда доступна).
    """
    try:
        from core.post_mortem import format_telegram, run_post_mortem

        parts = (message.text or "").split()
        target_date: Optional[str] = None
        if len(parts) > 1:
            arg = parts[1].strip()
            # Формат DD.MM.YYYY  (HH:MM — опционально, парсер режет по пробелу)
            if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", arg):
                target_date = arg

        await message.answer("🔬 Запускаю post-mortem... (тяну цены)")
        report = await run_post_mortem(target_date=target_date)
        if report is None:
            await message.answer(
                "📭 Не нашёл подходящего дай��жеста.\n\n"
                "Возможные причины:\n"
                "• Дайджест публикуется реже 24ч (или DIGEST_CACHE.md пуст).\n"
                "• Дата `DD.MM.YYYY` не совпадает ни с одной строкой `## 📊`.\n"
                "• PriceFetcher (Yahoo) недоступен — попробуй позже."
            )
            return

        await message.answer(format_telegram(report), parse_mode="Markdown")
    except Exception as e:
        logger.exception("postmortem error")
        await message.answer(f"Ошибка postmortem: {e}")


@dp.message(Command("usage"))
async def cmd_usage(message: Message):
    """Token usage по провайдерам с момента последнего рестарта."""
    try:
        from ai_provider import get_usage_stats

        stats = get_usage_stats()
        if not stats:
            await message.answer(
                "📊 *Token Usage*\n\nПока нет вызовов AI с момента старта."
            )
            return

        msg = "📊 *AI Token Usage* (с последнего рестарта)\n"
        msg += "═" * 28 + "\n\n"

        # Сортируем по total_tokens DESC
        providers_sorted = sorted(
            stats.items(),
            key=lambda kv: kv[1].get("total_tokens", 0),
            reverse=True,
        )

        grand_total_calls = 0
        grand_total_tokens = 0
        for provider, data in providers_sorted:
            calls = data.get("calls", 0)
            tt = data.get("total_tokens", 0)
            pt = data.get("prompt_tokens", 0)
            ct = data.get("completion_tokens", 0)
            grand_total_calls += calls
            grand_total_tokens += tt

            msg += f"*{provider}*: {calls} вызовов, {tt:,} tokens\n"
            msg += f"  └ in: {pt:,} | out: {ct:,}\n"

            by_model = data.get("by_model", {})
            if by_model and len(by_model) > 1:
                # Несколько моделей — покажем их разбивку
                for model, mdata in sorted(by_model.items(),
                                            key=lambda kv: kv[1].get("total_tokens", 0),
                                            reverse=True)[:3]:
                    msg += f"    • `{model}`: {mdata.get('calls', 0)} calls, {mdata.get('total_tokens', 0):,} tok\n"

        msg += "\n" + "─" * 25 + "\n"
        msg += f"*Итого:* {grand_total_calls} вызовов, {grand_total_tokens:,} tokens\n"

        await message.answer(msg, parse_mode="Markdown")
    except Exception as e:
        logger.exception("usage error")
        await message.answer(f"Ошибка: {e}")


def _autotrade_coming_soon_text() -> str:
    """Единый текст-заглушка про будущую автоторговлю по API биржи."""
    return (
        "🤖 *Автоторговля — скоро*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Скоро ты сможешь подключить *API своей биржи* (только спот, только лонг, "
        "без права вывода средств) — и бот будет торговать сам по своим сигналам: "
        "тренд, моментум, DCA.\n\n"
        "🔧 Функция в *активной разработке*.\n\n"
        "А пока:\n"
        "• 🧭 *Тренд* и 📈 *Акции* — смотри, что в аптренде;\n"
        "• 🎯 *Лучшая сделка* — идея прямо сейчас;\n"
        "• торгуй на бирже сам по диплинкам из карточек.\n\n"
        "🔒 Ключи — только с правом спот-торговли, без вывода средств. "
        "Не инвест-совет."
    )


async def _send_autotrade_coming_soon(message: Message) -> None:
    text = _autotrade_coming_soon_text()
    try:
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await message.answer(text)


@dp.message(Command("close"))
async def cmd_close_position(message: Message):
    """[в разработке] Раньше — ручное закрытие paper-позиции. Теперь заглушка."""
    await _send_autotrade_coming_soon(message)


@dp.message(Command("stop"))
async def cmd_stop_autotrade(message: Message):
    """[в разработке] Раньше — стоп автотрейда. Теперь заглушка."""
    await _send_autotrade_coming_soon(message)


@dp.message(Command("starttrade"))
async def cmd_start_autotrade(message: Message):
    """[в разработке] Раньше — старт автотрейда. Теперь заглушка."""
    await _send_autotrade_coming_soon(message)


@dp.message(Command("why"))
async def cmd_why_position(message: Message):
    """[в разработке] Раньше �� объяснение paper-позиции. Теперь заглушка."""
    await _send_autotrade_coming_soon(message)


@dp.message(Command("eval"))
async def cmd_eval_pipeline(message: Message):
    """Run the validation pipeline on recent signals: /eval"""
    try:
        from pipeline import run_full_evaluation
        await message.answer("🔄 Запускаю валидацию сигналов...")
        metrics = await run_full_evaluation(
            source="daily_context",
            limit=10,
            save_to_file="results.json",
        )
        await message.answer(
            metrics.summary(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"eval pipeline error: {e}")
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("screener"))
@require_vip
async def cmd_screener(message: Message):
    """Scan market for anomalies: /screener"""
    try:
        from core.screener import MarketScreener
        screener = MarketScreener(top_n=15)
        await message.answer("📡 Сканирую рынок на аномалии...")
        results = await screener.scan()

        if not results:
            await message.answer("📡 Сканер: Аномалий не обнаружено. Рынок спокоен.")
            return

        lines = ["📡 *РЫНОЧНЫЙ СКРИНЕР*\n"]
        for r in results:
            sym = r.get("symbol", "?")
            signals = r.get("signals", [])
            if signals:
                lines.append(f"*{sym}*")
                for s in signals:
                    lines.append(f"  ▫️ {s}")
                lines.append("")

        lines.append(f"Найдено аномалий: {len(results)}")
        msg = "\n".join(lines)
        await message.answer(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"screener error: {e}")
        await message.answer(f"Ошибка сканера: {e}")


def _halal_card_kb(kind: str, picks: list[str]) -> InlineKeyboardMarkup:
    """Inline-клавиатура под карточкой /stocks или /trend.

    kind: "stocks" | "trend".
    picks: топ-тикеры (до 3) — на каждый URL-кнопка «📈 SYM» на график.
    Плюс ряд действий: 🔄 Обновить, 🔔 Алерты, переход на другую карточку.
    """
    import links
    rows: list[list[InlineKeyboardButton]] = []
    pick_row: list[InlineKeyboardButton] = []
    for sym in (picks or [])[:3]:
        url = links.crypto_chart_url(sym) if kind == "trend" else links.stock_chart_url(sym)
        pick_row.append(InlineKeyboardButton(text=f"📈 {sym}", url=url))
    if pick_row:
        rows.append(pick_row)
    if kind == "trend":
        nav = InlineKeyboardButton(text="📊 Акции", callback_data="hsnav:stocks")
    else:
        nav = InlineKeyboardButton(text="🧭 Тренд", callback_data="hsnav:trend")
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"hsref:{kind}"),
        InlineKeyboardButton(text="🔔 Алерты", callback_data="hsalert"),
        nav,
    ])
    rows.append([
        InlineKeyboardButton(text="❓ Что это / Как читать", callback_data=f"explain:{kind}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pump_card_kb(picks: list[str]) -> InlineKeyboardMarkup:
    """Inline-клавиатура под карточкой «🚀 Что разгоняется».

    picks — топ-монеты (до 3) на график. Плюс 🔄 Обновить и переход на 🧭 Тренд.
    """
    import links
    rows: list[list[InlineKeyboardButton]] = []
    pick_row: list[InlineKeyboardButton] = []
    for sym in (picks or [])[:3]:
        pick_row.append(InlineKeyboardButton(text=f"📈 {sym}", url=links.crypto_chart_url(sym)))
    if pick_row:
        rows.append(pick_row)
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="pumpref"),
        InlineKeyboardButton(text="🧭 Тренд", callback_data="hsnav:trend"),
    ])
    rows.append([
        InlineKeyboardButton(text="❓ Что это / Как читать", callback_data="explain:pump"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _build_pump_card():
    """Собирает карточку «что разгоняется» (text, keyboard). Без сети-краша."""
    import halal_signals
    res = await halal_signals.build_pump_card()
    return res.text, _pump_card_kb(res.picks)


async def _build_halal_card(kind: str, sma: int = 50):
    """Собирает карточку (text, keyboard) для kind=stocks|trend. Без сети-краша."""
    import halal_signals
    if kind == "stocks":
        res = await halal_signals.build_stocks_card(sma=sma)
    else:
        res = await halal_signals.build_crypto_trend_card(sma=sma)
    return res.text, _halal_card_kb(kind, res.picks)


async def _send_halal_card(message: Message, text: str, kb):
    """Шлёт карточку с inline-кнопками, переживая сбой Markdown-парсинга.

    Важно: в тексте карточки есть deeplink-URL с '_' (BTC_USDT) — на legacy
    Markdown это иногда даёт «can't parse entities» и раньше уводило в fallback
    БЕЗ клавиатуры (кнопки пропадали). Теперь fallback всегда сохраняет kb.
    """
    try:
        return await message.answer(
            text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=kb
        )
    except Exception:  # noqa: BLE001 — Markdown подвёл, шлём без разметки, но С кнопками
        try:
            return await message.answer(
                text, disable_web_page_preview=True, reply_markup=kb
            )
        except Exception:  # noqa: BLE001 — крайний случай: хотя бы текст
            return await message.answer(text, reply_markup=kb)


@dp.message(Command("stocks"))
async def cmd_stocks(message: Message):
    """Скринер акций: курируемый вотчлист + тренд (SMA) и моментум. /stocks [sma]"""
    parts = (message.text or "").split()
    sma = 50
    try:
        if len(parts) >= 2:
            sma = max(10, min(200, int(parts[1])))
    except ValueError:
        pass
    wait = await message.answer("📈 Считаю силу акций (тренд + моментум)…")
    kb = None
    try:
        text, kb = await _build_halal_card("stocks", sma)
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ Не получилось собрать акции: {e}"
    try:
        await wait.delete()
    except Exception:  # noqa: BLE001
        pass
    await _send_halal_card(message, text, kb)


@dp.message(Command("trend"))
async def cmd_trend(message: Message):
    """Крипто-тренд: кто сейчас в аптренде (price>SMA), равный вес. /trend [sma]"""
    parts = (message.text or "").split()
    sma = 50
    try:
        if len(parts) >= 2:
            sma = max(10, min(200, int(parts[1])))
    except ValueError:
        pass
    wait = await message.answer("🧭 Сканирую тренд по крупным спот-монетам…")
    kb = None
    try:
        text, kb = await _build_halal_card("trend", sma)
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ Не получилось собрать тренд: {e}"
    try:
        await wait.delete()
    except Exception:  # noqa: BLE001
        pass
    await _send_halal_card(message, text, kb)


@dp.message(Command("dca"))
async def cmd_dca(message: Message):
    """План усреднения (DCA): /dca <депозит> [траншей] [интервал_дней]."""
    parts = (message.text or "").split()
    deposit, tranches, days = 1000.0, 6, 5
    # Аргументы парсим ТОЛЬКО если это реальная команда /dca ...
    # Тап по кнопке «💰 DCA» присылает текст "💰 DCA" — это не аргументы,
    # для него отдаём дефолтный план (а не ошибку формата).
    if parts and parts[0].lstrip().startswith("/"):
        try:
            if len(parts) >= 2:
                deposit = float(parts[1].replace("$", "").replace(",", ""))
            if len(parts) >= 3:
                tranches = int(parts[2])
            if len(parts) >= 4:
                days = max(1, int(parts[3]))
        except ValueError:
            await message.answer("Формат: /dca 5000 6 7  (депозит, траншей, интервал в днях)")
            return
    try:
        import halal_signals
        text = halal_signals.build_dca_plan(deposit, tranches, days)
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ Не получилось собрать DCA-план: {e}"
    dca_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❓ Что это / Как читать", callback_data="explain:dca"),
    ]])
    try:
        await message.answer(text, parse_mode="Markdown",
                             disable_web_page_preview=True, reply_markup=dca_kb)
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=dca_kb)


@dp.message(F.text == PERSISTENT_BTN_STOCKS)
async def _kb_stocks(message: Message):
    await cmd_stocks(message)


@dp.message(F.text == PERSISTENT_BTN_TREND)
async def _kb_trend(message: Message):
    await cmd_trend(message)


@dp.message(Command("alerts"))
async def cmd_alerts(message: Message):
    """Спот-автоалерты вкл/выкл: пуш при смене режима тренда (/trend, /stocks).

    /alerts        — показать статус и переключить
    /alerts on     — включить
    /alerts off    — выключить
    """
    from database import get_user_halal_alert_status, set_halal_alert_sub
    user_id = message.from_user.id
    parts = (message.text or "").split()
    arg = parts[1].lower() if len(parts) >= 2 else None

    current = await get_user_halal_alert_status(user_id)
    if arg in ("on", "вкл", "1"):
        new = True
    elif arg in ("off", "выкл", "0"):
        new = False
    else:
        new = not current  # без аргумента — переключаем

    await set_halal_alert_sub(user_id, new)
    if new:
        text = (
            "🔔 *Авто-алерты включены*\n\n"
            "Я сам пришлю пуш, когда:\n"
            "• монета входит/выходит из аптренда (по SMA50) — `/trend`;\n"
            "• акция входит/выходит из топа силы (моментум) — `/stocks`;\n"
            "• стейблкоин уходит в депег и возможен возврат к $1 — `/depeg`.\n\n"
            "В каждом алерте — диплинки на биржи, график и кнопка «❓ Что делать». "
            "Только спот/лонг, без плеча. Выключить: `/alerts off`. Не инвест-совет."
        )
    else:
        text = "🔕 Спот-автоалерты выключены. Включить обратно: `/alerts on`."
    try:
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await message.answer(text)


# ─── Inline-кнопки под карточками /stocks и /trend ────────────────────────────
@dp.callback_query(F.data.startswith("hsref:"))
async def _cb_halal_refresh(cb: CallbackQuery):
    """🔄 Обновить — пересобрать карточку на месте."""
    kind = (cb.data or "").split(":", 1)[1] if ":" in (cb.data or "") else "trend"
    if kind not in ("stocks", "trend"):
        kind = "trend"
    try:
        await cb.answer("Обновляю…")
    except Exception:  # noqa: BLE001
        pass
    try:
        text, kb = await _build_halal_card(kind, 50)
    except Exception:  # noqa: BLE001
        try:
            await cb.answer("Не удалось обновить, попробуй ещё раз", show_alert=False)
        except Exception:  # noqa: BLE001
            pass
        return
    # Markdown может упасть на deeplink-URL — тогда правим без разметки, но С кнопками.
    try:
        await cb.message.edit_text(
            text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=kb
        )
    except Exception:  # noqa: BLE001
        try:
            await cb.message.edit_text(
                text, disable_web_page_preview=True, reply_markup=kb
            )
        except Exception:  # noqa: BLE001
            pass


@dp.callback_query(F.data == "hsalert")
async def _cb_halal_alert_toggle(cb: CallbackQuery):
    """🔔 Алерты — переключить подписку на спот-автоалерты."""
    try:
        from database import get_user_halal_alert_status, set_halal_alert_sub
        uid = cb.from_user.id
        cur = await get_user_halal_alert_status(uid)
        await set_halal_alert_sub(uid, not cur)
        await cb.answer(
            "🔔 Автоалерты включены" if not cur else "🔕 Автоалерты выключены",
            show_alert=False,
        )
    except Exception:  # noqa: BLE001
        try:
            await cb.answer("Не получилось переключить алерты", show_alert=False)
        except Exception:  # noqa: BLE001
            pass


@dp.callback_query(F.data == "halert_keep")
async def _cb_halal_alert_keep(cb: CallbackQuery):
    """🔔 Оставить — подтвердить подписку на спот-автоалерты (под алертом)."""
    try:
        from database import set_halal_alert_sub
        await set_halal_alert_sub(cb.from_user.id, True)
        await cb.answer("🔔 Ок, автоалерты остаются включёнными", show_alert=False)
    except Exception:  # noqa: BLE001
        try:
            await cb.answer("Готово", show_alert=False)
        except Exception:  # noqa: BLE001
            pass


@dp.callback_query(F.data == "halert_off")
async def _cb_halal_alert_off(cb: CallbackQuery):
    """🔕 Отключить — отписать от спот-автоалертов (под алертом)."""
    try:
        from database import set_halal_alert_sub
        await set_halal_alert_sub(cb.from_user.id, False)
        await cb.answer("🔕 Автоалерты отключены. Включить обратно: /alerts",
                        show_alert=True)
    except Exception:  # noqa: BLE001
        try:
            await cb.answer("Не получилось отключить", show_alert=False)
        except Exception:  # noqa: BLE001
            pass


@dp.callback_query(F.data.startswith("hsnav:"))
async def _cb_halal_nav(cb: CallbackQuery):
    """Переход на соседнюю карточку (Акции ↔ Тренд) новым сообщением."""
    kind = (cb.data or "").split(":", 1)[1] if ":" in (cb.data or "") else "trend"
    if kind not in ("stocks", "trend"):
        kind = "trend"
    try:
        await cb.answer()
    except Exception:  # noqa: BLE001
        pass
    try:
        text, kb = await _build_halal_card(kind, 50)
    except Exception:  # noqa: BLE001
        return
    await _send_halal_card(cb.message, text, kb)


@dp.callback_query(F.data.startswith("explain:"))
async def _cb_explain(cb: CallbackQuery):
    """Кнопка «❓ Что это / Как читать» — шлёт простое объяснение фичи.

    Новое сообщение (не редактируем карточку), чтобы юзер видел и карточку,
    и пояснение рядом. Ключ после двоеточия: trend|stocks|pump|dca|backtest|
    debate|menu.
    """
    import explainers
    key = (cb.data or "explain:menu").split(":", 1)[1] or "menu"
    try:
        await cb.answer()
    except Exception:  # noqa: BLE001
        pass
    text = explainers.get(key)
    try:
        await bot.send_message(
            cb.message.chat.id, text,
            parse_mode="Markdown", disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001 — Markdown подвёл → шлём плоским текстом
        await bot.send_message(cb.message.chat.id, text, disable_web_page_preview=True)


def _backtest_caption() -> str:
    """Короткая подпись к графику бэктеста из docs/backtest_summary.json."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(__file__).resolve().parent / "docs" / "backtest_summary.json"
    try:
        m = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return (
            "*📊 Бэктест спот-стратегии*\n\n"
            "Дисциплинированный спот-тренд: держим крупные монеты в аптренде, "
            "уходим в стейбл в медвежке. Только лонг, без плеча и шортов.\n\n"
            "_Подробности — кнопка ❓ ниже._"
        )
    pc = lambda x: f"{x * 100:+.1f}%"
    base = m.get("base_total")
    base_line = (
        f"• 🔵 Простой тренд: {pc(m.get('base_total',0))}, просадка {pc(m.get('base_mdd',0))}, "
        f"Sharpe {m.get('base_sharpe',0):.2f}\n"
    ) if base is not None else ""
    rob_line = ""
    if m.get("rob_n_configs"):
        rob_line = (
            f"\n🧪 Робастность ({m['rob_n_configs']} конфигураций, не одна удачная точка): "
            f"CAGR медиана {pc(m.get('rob_cagr_med',0))}, просадка медиана {pc(m.get('rob_mdd_med',0))}.\n"
        )
    return (
        "*📊 Бэктест EDGE-стратегии* (полный цикл "
        f"{m.get('start_day','?')} → {m.get('end_day','?')}, ~{m.get('years',0):.1f} г.: "
        "бык 2021 → медведь 2022 → 2023-25)\n"
        "\n"
        f"Только спот/лонг, без плеча и шортов. Momentum-weight (самым сильным больше) + краш-фильтр (уход в стейбл при развороте BTC).\n"
        "\n"
        f"• 🟢 EDGE: *{pc(m.get('strat_total',0))}*, просадка {pc(m.get('strat_mdd',0))}, "
        f"Sharpe {m.get('strat_sharpe',0):.2f}\n"
        f"{base_line}"
        f"• 🟠 BTC «держать»: {pc(m.get('btc_total',0))}, просадка {pc(m.get('btc_mdd',0))}\n"
        f"• 🔴 Корзина «держать»: {pc(m.get('basket_total',0))}, просадка {pc(m.get('basket_mdd',0))}\n"
        f"{rob_line}"
        "\n"
        f"Главное: за {m.get('years',0):.1f} г. полного цикла EDGE обгоняет простой тренд и "
        f"«держать» — и по доходности, и по Sharpe. В рынке только {pc(m.get('exposure',0))} времени — "
        "остальное в стейбле. Цена за рост — просадка глубже, чем у наивного тренда.\n"
        "\n"
        "_История, не гарантия будущего. Не инвестсовет._"
    )


def _backtest_stocks_caption() -> str:
    """Короткая подпись к графику бэктеста по АКЦИЯМ из docs/backtest_stocks_summary.json."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(__file__).resolve().parent / "docs" / "backtest_stocks_summary.json"
    try:
        m = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return (
            "*📊 Бэктест EDGE по акциям*\n\n"
            "Та же логика, что и по крипте, но на 15 крупных акциях США. "
            "Только спот/лонг, без плеча и шортов.\n\n"
            "_Подробности — кнопка ❓ ниже._"
        )
    pc = lambda x: f"{x * 100:+.1f}%"
    rob_line = ""
    if m.get("rob_n_configs"):
        beat = m.get("rob_frac_beat", 0) * 100
        rob_line = (
            f"\n🧪 Робастность ({m['rob_n_configs']} конфигураций, не одна удачная точка): "
            f"Sharpe медиана {m.get('rob_sharpe_med',0):.2f}, "
            f"и {beat:.0f}% вариантов обошли SPY.\n"
        )
    return (
        "*📊 Бэктест EDGE по АКЦИЯМ* (полный цикл "
        f"{m.get('start_day','?')} → {m.get('end_day','?')}, ~{m.get('years',0):.1f} г.: "
        "covid-крах 2020 → медведь 2022 → рост 2023-25)\n"
        "\n"
        "15 US large-cap. Только спот/лонг, без плеча и шортов. Momentum-weight + "
        "краш-фильтр (уход в кэш при развороте рынка SPY).\n"
        "\n"
        f"• 🟢 EDGE: *{pc(m.get('strat_total',0))}*, просадка {pc(m.get('strat_mdd',0))}, "
        f"Sharpe {m.get('strat_sharpe',0):.2f}\n"
        f"• 🔵 Корзина 15 акций: {pc(m.get('basket_total',0))}, просадка {pc(m.get('basket_mdd',0))}\n"
        f"• 🔴 SPY «купи и держи»: {pc(m.get('spy_total',0))}, просадка {pc(m.get('spy_mdd',0))}, "
        f"Sharpe {m.get('spy_sharpe',0):.2f}\n"
        f"{rob_line}"
        "\n"
        f"Главное: за {m.get('years',0):.1f} г. EDGE обогнал индекс SPY и по доходности, и по "
        "Sharpe, И с меньшей просадкой. В рынке только "
        f"{m.get('exposure',0)*100:.0f}% времени — остальное в кэше.\n"
        "\n"
        "_История, не гарантия будущего. Не инвестсовет._"
    )


async def _send_backtest_asset(chat_id: int, asset: str):
    """Отправляет график+подпись бэктеста по выбранному активу: 'crypto' | 'stocks'."""
    from pathlib import Path as _Path
    base = _Path(__file__).resolve().parent / "docs"
    if asset == "stocks":
        png = base / "backtest_stocks_equity.png"
        caption = _backtest_stocks_caption()
        explain_key = "explain:backtest_stocks"
        fname = "backtest_stocks.png"
    else:  # crypto (по умолчанию)
        png = base / "backtest_equity.png"
        caption = _backtest_caption()
        explain_key = "explain:backtest"
        fname = "backtest.png"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Что это / Как читать", callback_data=explain_key)],
        [InlineKeyboardButton(text="🔁 Другой бэктест", callback_data="cmd:backtest")],
    ])
    if png.exists():
        try:
            await bot.send_photo(
                chat_id,
                photo=BufferedInputFile(png.read_bytes(), filename=fname),
                caption=caption, parse_mode="Markdown", reply_markup=kb,
            )
            return
        except Exception:  # noqa: BLE001 — например подпись слишком длинная
            pass
    try:
        await bot.send_message(chat_id, caption, parse_mode="Markdown",
                               disable_web_page_preview=True, reply_markup=kb)
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, caption, reply_markup=kb)


def _backtest_choose_kb() -> InlineKeyboardMarkup:
    """Меню выбора актива для бэктеста: крипта или акции."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 Крипта", callback_data="bt:crypto"),
            InlineKeyboardButton(text="📊 Акции", callback_data="bt:stocks"),
        ],
        [InlineKeyboardButton(text="❓ Как читать бэктест", callback_data="explain:backtest_stocks")],
    ])


@dp.message(Command("backtest"))
async def cmd_backtest(message: Message):
    """Бэктест спот-стратегии: выбор актива (крипта/акции). /backtest"""
    text = (
        "*📊 Бэктест EDGE-стратегии*\n"
        "\n"
        "Проверка стратегии на реальной истории цен. Выбери актив:\n"
        "\n"
        "• *📈 Крипта* — топ-монеты, фильтр режима по BTC.\n"
        "• *📊 Акции* — 15 крупных акций США, фильтр режима по SPY.\n"
        "\n"
        "Логика одинаковая — только спот/лонг, без плеча и шортов."
    )
    try:
        await message.answer(text, parse_mode="Markdown",
                             disable_web_page_preview=True, reply_markup=_backtest_choose_kb())
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=_backtest_choose_kb())


@dp.callback_query(F.data.startswith("bt:"))
async def handle_backtest_asset(callback: CallbackQuery):
    """Выбор актива бэктеста: bt:crypto | bt:stocks → шлёт график + подпись."""
    try:
        await callback.answer()
    except Exception:  # noqa: BLE001
        pass
    asset = (callback.data or "bt:crypto").split(":", 1)[1] or "crypto"
    try:
        await _send_backtest_asset(callback.message.chat.id, asset)
    except Exception as e:  # noqa: BLE001
        logger.error(f"backtest asset '{asset}' failed: {e}")
        try:
            await bot.send_message(callback.from_user.id,
                                   "⚠️ Не получилось открыть бэктест. Попробуй ещё раз.")
        except Exception:  # noqa: BLE001
            pass


# ─── EDGE: профиль стратегии (выбор пресета для /plan) ��───────────────
def _edge_plan_kb(active: str) -> InlineKeyboardMarkup:
    """Клавиатура под EDGE-планом: бэктест/пояснение + кнопка смены профиля."""
    import halal_edge
    meta = halal_edge.EDGE_PROFILES.get(active, halal_edge.EDGE_PROFILES["base"])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧪 Бэктест EDGE", callback_data="cmd:backtest"),
            InlineKeyboardButton(text="❓ Что это", callback_data="explain:edge"),
        ],
        [InlineKeyboardButton(text=f"⚙️ Профиль стратегии: {meta['short']}",
                              callback_data="edgeprof:menu")],
    ])


def _edge_profile_menu_kb(active: str) -> InlineKeyboardMarkup:
    """Меню выбора профиля EDGE."""
    import halal_edge
    rows = []
    for key, meta in halal_edge.EDGE_PROFILES.items():
        mark = "✅ " if key == active else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{meta['label']}",
                                          callback_data=f"edgeprof:set:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к плану", callback_data="edgeprof:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _build_edge_plan(user_id: int, deposit: float = 100.0):
    """(text, kb) EDGE-плана под выбранный профиль пользователя."""
    import halal_edge
    profile = await get_user_edge_profile(user_id)
    plan = await asyncio.to_thread(halal_edge.live_plan, None, deposit, profile)
    text = halal_edge.render_plan_text(plan, deposit)
    return text, _edge_plan_kb(profile)


@dp.message(Command("plan", "edgeplan"))
async def cmd_edgeplan(message: Message):
    """EDGE-план на сегодня — ведём за руку: /plan [��епоз��т]

    Та же логика, что в бэктесте (halal_edge.edge_signal): смотрим рынок
    сейчас и пошагово говорим, что купить, сколько в стейбле, когда выйти.
    """
    # депозит из текста: «/plan 1000» → суммы в $; по умолчанию доли на $100
    deposit = 100.0
    parts = (message.text or "").split()
    if len(parts) > 1:
        try:
            deposit = max(1.0, float(parts[1].replace(",", ".").replace("$", "")))
        except ValueError:
            deposit = 100.0

    try:
        text, kb = await _build_edge_plan(message.from_user.id, deposit)
    except Exception as e:  # noqa: BLE001 — сеть/данные подвели
        logging.warning("cmd_edgeplan failed: %s", e)
        await message.answer(
            "⚠️ Не смог получить свежие данные рынка для EDGE-плана. "
            "Попробуй ещё раз через минуту."
        )
        return

    try:
        await message.answer(text, parse_mode="Markdown",
                             disable_web_page_preview=True, reply_markup=kb)
    except Exception:  # noqa: BLE001 — Markdown подвёл → плоский текст
        await message.answer(text, disable_web_page_preview=True, reply_markup=kb)


@dp.callback_query(F.data.startswith("edgeprof:"))
async def handle_edge_profile(callback: CallbackQuery):
    """Выбор профиля стратегии EDGE: меню, сохранение, пере-рендер плана."""
    import halal_edge
    user_id = callback.from_user.id
    data = callback.data or ""

    if data == "edgeprof:menu":
        active = await get_user_edge_profile(user_id)
        lines = [
            "⚙️ *Профиль стратегии EDGE*",
            "",
            "По какой логике /plan подбирает монеты. Все варианты — спот, только лонг.",
            "",
        ]
        for key, meta in halal_edge.EDGE_PROFILES.items():
            mark = "✅ " if key == active else "• "
            lines.append(f"{mark}*{meta['label']}*\n   _{meta['desc']}_")
        try:
            await callback.message.edit_text(
                "\n".join(lines), parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=_edge_profile_menu_kb(active),
            )
        except Exception:  # noqa: BLE001
            pass
        await callback.answer()
        return

    if data.startswith("edgeprof:set:"):
        parts = data.split(":")
        key = parts[2] if len(parts) > 2 else ""
        if key not in halal_edge.EDGE_PROFILES:
            await callback.answer("Неизвестный профиль")
            return
        await set_user_edge_profile(user_id, key)
        await callback.answer(f"✅ Профиль: {halal_edge.EDGE_PROFILES[key]['short']}")

    # edgeprof:back или после set → показываем актуальный план
    try:
        text, kb = await _build_edge_plan(user_id, 100.0)
    except Exception as e:  # noqa: BLE001
        logging.warning("handle_edge_profile render failed: %s", e)
        await callback.answer("⚠️ Не смог обновить план — попробуй /plan", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            text, parse_mode="Markdown",
            disable_web_page_preview=True, reply_markup=kb,
        )
    except Exception:  # noqa: BLE001
        try:
            await callback.message.answer(
                text, disable_web_page_preview=True, reply_markup=kb)
        except Exception:  # noqa: BLE001
            pass


@dp.message(Command("depeg"))
async def cmd_depeg(message: Message):
    """Монитор депега стейблкоинов: /depeg — текущие цены USDC/TUSD/USDP/FDUSD
    и есть ли сейчас возможность «возврат к $1». Спот, без шортов/плеча."""
    try:
        import depeg_monitor
        prices = await depeg_monitor.fetch_prices()
        opps = depeg_monitor.detect_opportunities(prices)
        text = depeg_monitor.format_status(prices, opps)
    except Exception as e:  # noqa: BLE001 — сеть/данные подвели
        logging.warning("cmd_depeg failed: %s", e)
        await message.answer(
            "⚠️ Не смог получить свежие цены стейблкоинов. "
            "Попробуй ещё раз через минуту."
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❓ Что делать с этим", callback_data="explain:depeg"),
    ]])
    try:
        await message.answer(text, parse_mode="Markdown",
                             disable_web_page_preview=True, reply_markup=kb)
    except Exception:  # noqa: BLE001 — Markdown подвёл → плоский текст
        await message.answer(text, disable_web_page_preview=True, reply_markup=kb)


@dp.message(Command("instruction"))
async def cmd_instruction(message: Message):
    """Полнейшая инструкция как для пятилетнего: /instruction"""
    await _send_detailed_guide(message.chat.id)


@dp.message(Command("newbie"))
async def cmd_newbie(message: Message):
    """Гид для новичков — PDF + правила первой недели: /newbie"""
    await _send_newbie_guide(message.chat.id)


def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧭 EDGE-план: что купить сейчас", callback_data="cmd:edgeplan")],
        [InlineKeyboardButton(text="🎯 Лучшая ��делка сейчас", callback_data="cmd:signal")],
        [
            InlineKeyboardButton(text="📋 Дайджест", callback_data="cmd:daily"),
            InlineKeyboardButton(text="📊 Рынки + сигналы", callback_data="cmd:markets"),
        ],
        [
            InlineKeyboardButton(text="🧪 Скринер", callback_data="cmd:screener"),
            InlineKeyboardButton(text="🤖 Автоторговля", callback_data="cmd:signalstatus"),
        ],
        [
            InlineKeyboardButton(text="💰 Статус", callback_data="cmd:status"),
            InlineKeyboardButton(text="📈 Профиль", callback_data="cmd:profile"),
        ],
        [
            InlineKeyboardButton(text="📊 Трек-рекорд", callback_data="cmd:trackrecord"),
            InlineKeyboardButton(text="📊 Портфель", callback_data="portfolio:menu:"),
        ],
        [
            InlineKeyboardButton(text="🧪 Бэктест", callback_data="cmd:backtest"),
            InlineKeyboardButton(text="🔔 Подписка", callback_data="cmd:subscribe"),
        ],
        [
            InlineKeyboardButton(text="💎 VIP", callback_data="cmd:premium"),
        ],
        [
            InlineKeyboardButton(text="🌍 Global", callback_data="cmd:trackrecordglobal"),
            InlineKeyboardButton(text="🇷🇺 Россия", callback_data="cmd:trackrecordrussia"),
        ],
        [
            InlineKeyboardButton(text="🗓 Weekly", callback_data="cmd:weeklyreport"),
            InlineKeyboardButton(text="🚀 Памп", callback_data="cmd:pump"),
        ],
        [
            InlineKeyboardButton(text="📘 Инструкция", callback_data="cmd:guide"),
            InlineKeyboardButton(text="📖 Для чайников", callback_data="cmd:instruction"),
        ],
        [InlineKeyboardButton(text="❓ Help", callback_data="cmd:help")],
    ])


async def _send_bot_guide(chat_id: int) -> None:
    """Полный гид по командам — спот/лонг, без автотрейда (в разработк��)."""
    text = (
        "📘 *DIALECTIC EDGE — ГИД ПО КОМАНДАМ*\n"
        "━━━━━━━━���━━━━━━━━━━━━━━━━\n\n"
        "🧠 *Что это?*\n"
        "AI-аналитик рынков. Команда агентов (🐂 Bull · 🐻 Bear · 🔍 Verifier · "
        "⚖️ Synth) спорит на живых данных и выдаёт понятный план: что происходит "
        "и куда может пойти рынок. Только спот, только лонг — без плеча и шортов.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🚀 *С ЧЕГО НАЧАТЬ*\n"
        "• `/profile` — настрой риск-профиль и горизонт. Сделай *первым*: агенты "
        "подстроят анализ под тебя.\n"
        "• `🆕 Новичок` / `/newbie` — гид + PDF и правила выживания первой недели.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 *АНАЛИЗ РЫНКА*\n"
        "• `/daily` — главный прогноз: дебаты агентов → вердикт + график + куда "
        "смотреть. Что делать: читай вывод и направление, дальше решай сам.\n"
        "• `🎯 Лучшая сделка` / `/signal` — лучшая идея прямо сейчас (спот/лонг).\n"
        "• `🏛 Рынки` / `/markets` — живые цены + сигналы.\n"
        "• `📡 Скринер` / `/screener` — сканер аномалий по топ-монетам "
        "(всплеск объёма, перегретость).\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🧭 *ТРЕНД И МОМЕНТУМ*\n"
        "• `🧭 Тренд` / `/trend` — крипта в аптренде (по SMA50). Что делать: "
        "монеты выше линии — в восходящем тренде, ниже — слабые. Кнопки графиков "
        "и бирж прямо в карточке.\n"
        "• `📈 Акции` / `/stocks` — топ акций по силе (6-мес моментум).\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💰 *ИНСТРУМЕНТЫ*\n"
        "• `💰 DCA` / `/dca` — план усреднения: как заходить частями, а не всё "
        "сразу. Что делать: следуй шагам плана по своему депозиту.\n"
        "• `🧭 P2P` / `/p2p` — сканер P2P-спреда между площадками.\n"
        "• `🔔 Алерты` / `/alerts` — пуш при смене режима тренда. `on` / `off`.\n"
        "• `🧪 Бэктест` / `/backtest` — прогон стратегии на истории.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📈 *СТАТИСТИКА*\n"
        "• `📊 Трек-рекорд` / `/trackrecord` — точность прошлых прогнозов.\n"
        "• `💎 VIP` / `💎 Что я умею` — про премиум и возможности бота.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 *АВТОТОРГОВЛЯ — СКОРО*\n"
        "Скоро: подключаешь API биржи (только спот, только лонг, без права "
        "вывода средств) — и бот торгует сам по сво��м сигналам. Сейчас функция "
        "*в активной разработке*. Пока — смотри сигналы и торгуй на бирже сам "
        "по диплинкам из карточек.\n\n"
        "━━━━━━━━━━���━━━━━━━━━━━━━━\n\n"
        "⚠️ _Это аналитика, не финансовый совет. Рынок непредсказуем, "
        "агенты могут ошибаться. Только спот/лонг._"
    )
    await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=_main_menu_kb())


async def _send_detailed_guide(chat_id: int) -> None:
    """Полнейшая инструкция — объяснение каждой функции как пятилетнему."""
    part1 = (
        "📖 *ПОДРОБНАЯ ИНСТРУКЦИЯ (ЧАСТЬ 1/2)*\n"
        + "═" * 30 + "\n\n"
        "🧠 *ЧТО ТАКОЕ DIALECTIC EDGE?*\n"
        "Представь, что у тебя есть 4 умных друга, которые каждый день смотрят новости, "
        "график цен и данные с бирж. Они спорят друг с другом, а потом говорят тебе: "
        "\"Покупай\" или \"Продавай\" или \"Подожди\". Это и есть наш бот! 🤖\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 *КОМАНДЫ — ПРОСТЫМИ СЛОВАМИ*\n\n"
        "👤 `/profile` — *Настройки*\n"
        "👶 Как 5-летнему: \"Расскажи боту, какой ты смелый\"\n"
        "• Консерватор = боишься пот��рять деньги (мало рискуешь)\n"
        "• Умеренный = средний риск\n"
        "• Агрессивный = готов рисковать ради большой прибыли\n"
        "Сделай это ПЕРВЫМ, иначе бот не знает, как торговать!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 `/daily` — *Ежедневный анализ*\n"
        "👶 Как 5-летнему: \"Утренний прогноз погоды для денег\"\n"
        "Бот читает новости, смотрит цены, думает и говорит:\n"
        "• Куда пойдёт рынок? 📈 или 📉\n"
        "• Что покупать, что продавать?\n"
        "• По какой цене войти и выйти?\n"
        "Придёт кратко в чат + полный отчёт файлом .txt\n\n"
        "🔍 `/analyze <текст>` — *Разбор новости*\n"
        "👶 Как 5-летнему: \"Объясни мне эту новость\"\n"
        "Пример: `/analyze ФРС подняла ставку`\n"
        "Бот скажет, хорошо это или плохо для рынка.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📡 `/markets` — *Живые цены*\n"
        "👶 Как 5-летнему: \"Табло с ценами прямо сейчас\"\n"
        "Показывает цены Bitcoin, Ethereum и др. + сигналы.\n\n"
        "📊 `/status` — *Быстрый статус*\n"
        "👶 Как 5-летнему: \"Как дела у рынка?\"\n"
        "Короткий ответ: рынок растёт, падает или стоит на месте.\n\n"
        "🔎 `/screener` — *Сканер аномалий* 🆕\n"
        "👶 Как 5-летнему: \"Металлоискатель для денег\"\n"
        "Бот пробегает по ТОП-20 монетам и ищет странности:\n"
        "• 🔥 Объём вырос в 3 раза — кто-то крупный покупает!\n"
        "• 📉 RSI ниже 30 — цена упала слишком сильно, возможен отскок\n"
        "• 📈 RSI выше 70 — цена выросла слишком сильно, возможен откат\n"
        "• ⚠️ Funding аномальный — трейдеры слишком уверены в одном направлении\n"
    )
    await bot.send_message(chat_id, part1, parse_mode="Markdown")

    part2 = (
        "📖 *ПОДРОБНАЯ ИНСТРУКЦИЯ (ЧАСТЬ 2/2)*\n"
        + "═" * 30 + "\n\n"
        "🤖 *АВТОТОРГОВЛЯ — СКОРО*\n\n"
        "👶 Как 5-летнему: \"Скоро бот сможет сам нажимать кнопки на бирже\"\n"
        "Скоро ты подключишь *API своей биржи* (только спот, только лонг, без "
        "права вывода денег), и бот будет торговать сам по своим сигналам — "
        "тренд, моментум, DCA.\n"
        "🔧 Сейчас функция *в разработке*. Пока — смотри сигналы (🧭 Тренд, "
        "📈 Акции, 🎯 Лучшая сделка) и торгуй на бирже сам по диплинкам.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🛡️ *ЗАЩИТНЫЕ СИСТЕМЫ (10 МОДУЛЕЙ)*\n\n"
        "🌊 *1. Режим рынка (Regime Detector)*\n"
        "👶 Как 5-летнему: \"Бот смотрит, какая погода на рынке\"\n"
        "• ☀️ UPTREND = солнце — можно смело покупать\n"
        "• 🌧️ DOWNTREND = дождь — лучше продавать или сидеть в кэше\n"
        "• 🌫️ SIDEWAYS = туман — рынок не знает куда идти, осторожно\n"
        "• ⛈️ HIGH_VOL = шторм — цены скачут, уменьшаем ставки\n\n"
        "🐋 *2. Детектор Китов (Whale Detector)*\n"
        "👶 Как 5-летнему: \"Следим за большими дядями с миллионами\"\n"
        "Киты = люди с огромными деньгами. Когда они покупают — цена растёт. "
        "Бот видит их сделки и говорит: \"Киты покупают BTC, нам тоже стоит!\"\n\n"
        "🔗 *3. Матрица Корреляций*\n"
        "👶 Как 5-летнему: \"Не клади все яйца в одну корзину\"\n"
        "Если BTC и ETH двигаются одинаково (95% совпадение), "
        "то покупать оба — это как купить один и тот же товар дважды. "
        "Бот не даст тебе ошибиться!\n\n"
        "🚨 *4. Защита от Событий (Event Defense)*\n"
        "👶 Как 5-летнему: \"Сирена перед ураг��ном\"\n"
        "Если в новостях: \"ФРС\", \"Война\", \"Запрет крипты\" — "
        "бот кричит: \"ОПАСНО!\" и перестаёт торговать, пока не успокоится.\n\n"
        "📊 *5. Confluence Score* 🆕\n"
        "👶 Как 5-летнему: \"Оценка уверенности от 0 до 100\"\n"
        "Бот проверяет ВСЕ факторы сразу и ставит оценку:\n"
        "• 80-100 = СИЛЬНО ПОКУПАТЬ ✅✅✅\n"
        "• 60-80 = ПОКУПАТЬ ✅✅\n"
        "• 40-60 = ЖДАТЬ ⏸️\n"
        "• 20-40 = ПРОДАВАТЬ ❌\n"
        "• 0-20 = СИЛЬНО ПРОДАВАТЬ ❌❌❌\n"
        "Если оценка меньше 60 — бот НЕ войдёт в сделку!\n\n"
        "📅 *6. Экономический Календарь* 🆕\n"
        "👶 Как 5-летнему: \"Расписание опасных дней\"\n"
        "Бот знает, когда выходят важные новости (CPI, ставка ФРС) "
        "и НЕ торгует в эти дни, чтобы не потерять деньги на скачках.\n\n"
        "💰 *7. Kelly Criterion*\n"
        "👶 Как 5-летнему: \"Сколько денег ставить?\"\n"
        "Если бот часто выигрывает — ставит больше. Если проигрывает — меньше. "
        "Как умный игрок, который знает, когда рискнуть.\n\n"
        "📏 *8. ATR-стопы*\n"
        "👶 Как 5-летнему: \"Умная страховка\"\n"
        "Вместо фиксированного стопа 2%, бот смотрит, насколько сильно "
        "скачет цена СЕЙЧАС, и ставит стоп под эту волатильность.\n\n"
        "📈 *9. Multi-Timeframe* 🆕\n"
        "👶 Как 5-летнему: \"Спрашиваем 3 часов: день, 4 часа, 1 час\"\n"
        "Бот проверяет тренд на 3 разных масштабах. "
        "Если все 3 говорят \"вверх\" — покупаем. Если спорят — ждём.\n\n"
        "📡 *10. Data Enricher* 🆕\n"
        "👶 Как 5-летнему: \"Дополнительные очки зрения\"\n"
        "Бот смотрит не только на цену, но и на:\n"
        "• Funding Rate — кто платит кому на бирже\n"
        "• Open Interest — сколько денег в рынке\n"
        "• DXY (доллар) — сильный доллар = слабая крипта\n"
        "• Fear & Greed — люди боятся или жадничают?\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🧪 `/eval` — *Проверка точности*\n"
        "👶 Как 5-летнему: \"Проверка, не врёт ли бот?\"\n"
        "Бот берёт свои прошлые прогнозы, смотрит, что случилось на самом деле, "
        "и честно говорит: \"Я был прав в 60% случаев, заработал бы +12%\"\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ _Это аналитика и симуляция. Не финансовый совет._\n"
        "Рынок непредсказуем. Агенты могут ошибаться."
    )
    await bot.send_message(chat_id, part2, parse_mode="Markdown")


async def _send_newbie_guide(chat_id: int) -> None:
    """Гид для трейдеров-новичков. PDF + 3 inline-сообщения с выжимкой.

    Дополняет существующие _send_bot_guide / _send_detailed_guide:
      * _send_bot_guide       — справочник команд бота
      * _send_detailed_guide  — "как пятилетнему" объяснение функций
      * _send_newbie_guide    — РУКОВОДСТВО ПО ТОРГОВЛЕ для новичков:
                                когда запускать /daily, что НЕ делать
                                (Futures!), какой горизонт, правила
                                выживания п��рвой недели, walkthrough сделки

    Полная версия лежит в docs/BEGINNER_GUIDE.pdf — отправляется как файл.
    """
    pdf_path = Path(__file__).parent / "docs" / "BEGINNER_GUIDE.pdf"

    # 1. PDF (полный гид на 10 страниц).
    try:
        if pdf_path.exists():
            pdf_bytes = pdf_path.read_bytes()
            await bot.send_document(
                chat_id,
                document=BufferedInputFile(pdf_bytes, filename="Dialectic_Edge_Beginner_Guide.pdf"),
                caption=(
                    "📘 *Гид для новичков — полная версия PDF*\n\n"
                    "15-20 минут чтения. Скачай, прочти, перешли другу.\n"
                    "Краткая выжимка идёт следующими сообщениями ↓"
                ),
                parse_mode="Markdown",
            )
        else:
            logger.warning("BEGINNER_GUIDE.pdf not found at %s", pdf_path)
    except Exception as e:
        logger.error("send beginner guide PDF failed: %s", e)

    # 2. Краткая выжимка, разбитая на 3 inline-сообщения.
    part1 = (
        "🆕 *ГИД ДЛЯ НОВИЧКОВ — ЧАСТЬ 1/3*\n"
        + "═" * 28 + "\n\n"
        "📌 *В чём наш edge (и чего тут НЕТ)*\n\n"
        "Мы НЕ угадываем, куда пойдёт цена. Бэктест directional-сигналов "
        "(LONG/SHORT) на 2020–2026 показал: на дневках они *робастно убыточны*. "
        "Поэтому угадывание направления из бота удалено.\n\n"
        "Реальный edge бота — *спот + следование тренду с управлением риском*. "
        "Держишь актив, только пока он в восходящем тренде (цена выше скользящей "
        "средней SMA50); ушёл в нисходящий — сидишь в стейбле.\n\n"
        "*Только спот, без плеча, без шортов, без деривативов.*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🧲 *Зачем тренд-фильтр*\n\n"
        "На 5 годах данных это режет просадку почти *вдвое* (−55% против −84% у "
        "«купи и держи») при близкой доходности. Меньше боли в медвежьем рынке: "
        "когда актив падает ниже тренда — ты уже в стейбле, а не ловишь нож."
    )
    await bot.send_message(chat_id, part1, parse_mode="Markdown")

    part2 = (
        "🆕 *ГИД ДЛЯ НОВИЧКОВ — ЧАСТЬ 2/3*\n"
        + "═" * 28 + "\n\n"
        "🛠 *КАК ЭТО РАБОТАЕТ*\n\n"
        "📈 *Трендовый сигнал*\n"
        "Из отфильтрованного списка монет держишь равным весом те, что *сейчас "
        "��ыше SMA50*; упала ниже — продал в стейбл. Покупаешь силу, выходишь "
        "из слабости.\n\n"
        "🧮 *Фильтр активов*\n"
        "Берём только утилити/платёжные/инфраструктурные монеты. Исключаем "
        "кредит-протоколы, спекулятивные мем-коины и гемблинг-токены.\n\n"
        "📊 *Скринер акций*\n"
        "Крупные компании, прошедшие секторный + балансовый скрин (низкий долг), "
        "которые сейчас в аптренде.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━��━━━━\n\n"
        "🎯 *Главное правило*\n\n"
        "Купить *спот* равным весом то, что выше SMA; ушло ниже — продать. "
        "Никакого плеча, шортов и деривативов. Если ничего не в тренде — "
        "сидим в стейблах. Это валидный ответ, а не ошибка."
    )
    await bot.send_message(chat_id, part2, parse_mode="Markdown")

    part3 = (
        "🆕 *ГИД ДЛЯ НОВИЧКОВ — ЧАСТЬ 3/3*\n"
        + "═" * 28 + "\n\n"
        "💵 *Деньги на биржу*\n"
        "• `/p2p` → RUB→USDT через P2P. Бери мейкеров с большим числом сделок и "
        "completion ≥90%, не ведись на фейково «дешёвые» заявки.\n"
        "• Держишь USDT на *Spot* и покупаешь активы спотом — то, чем реально "
        "владеешь.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🛡 *ПРАВИЛА ВЫЖИВАНИЯ ПЕРВОЙ НЕДЕЛИ*\n\n"
        "*1.* Только *спот* — что купил, тем и владеешь. Без плеча.\n"
        "*2.* Размер — *макс 20–30% депо* на актив первую неделю, диверсифицируй "
        "по списку в тренде.\n"
        "*3.* Актив ушёл *ниже SMA* — продай, не «усредняйся вниз».\n"
        "*4.* Не гонись за иксами и мем-коинами — это азарт, не стратегия.\n"
        "*5.* Ничего не в тренде — сиди в стейбле, это нормальный ответ.\n"
        "*6.* Веди *журнал* с первой сделки: ожидаемое vs факт после костов.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔔 *Авто-алерты уже включены* — тебе НЕ нужно сидеть в боте 24/7. "
        "Я сам напишу, когда сменится тренд, появится лучшая сделка или "
        "стейблкоин уйдёт в депег (возможен возврат к $1). У каждого алерта "
        "есть кнопка «❓ Что делать». Выключить: `/alerts off`.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🚫 *ЧЕГО НЕ ДЕЛАТЬ:* плечо, шорты, деривативы, гонка за иксами, "
        "усреднение в падающем активе.\n\n"
        "⚠️ *Disclaimer:* это аналитический инструмент, не финансовый совет. "
        "Тренд-фильтр снижает просадку, но не убирает риск (резкие развороты, "
        "риск биржи/стейбла). Дисциплина и журнал важнее любого сигнала.\n\n"
        "📘 Полная версия в PDF ↑"
    )
    await bot.send_message(chat_id, part3, parse_mode="Markdown")


class _CallbackMessageProxy:
    """Мини-адаптер, чтобы переиспользовать cmd_* хендлеры из inline-кнопок."""

    def __init__(self, callback: CallbackQuery):
        self._cb = callback
        self.from_user = callback.from_user
        self.chat = callback.message.chat if callback.message else callback
        self.text = ""

    async def answer(self, text: str, **kwargs):
        return await bot.send_message(self._cb.from_user.id, text, **kwargs)


@dp.callback_query(F.data.startswith("cmd:"))
async def handle_cmd_shortcuts(callback: CallbackQuery):
    await callback.answer()
    cmd = (callback.data or "").split(":", 1)[1] if ":" in (callback.data or "") else ""
    proxy = _CallbackMessageProxy(callback)

    mapping = {
        "profile": cmd_profile,
        "dialectica": cmd_dialectica,
        "digest_base": cmd_digest_base,
        "daily": cmd_daily,
        "markets": cmd_markets,
        "status": cmd_status,
        "pitch": cmd_pitch,
        "trackrecord": cmd_trackrecord,
        "trackrecordglobal": lambda m: _cmd_trackrecord(m, report_type="global", title="GLOBAL", filter_type="all"),
        "trackrecordrussia": lambda m: _cmd_trackrecord(m, report_type="russia", title="РОССИЯ EDGE", filter_type="all"),
        "weeklyreport": cmd_weekly,
        "subscribe": cmd_subscribe,
        "premium": cmd_premium,
        "help": cmd_help,
        "signal": cmd_signal,
        "signalstatus": cmd_signal_status,
        "screener": cmd_screener,
        "pump": cmd_pump,
        "backtest": cmd_backtest,
        "edgeplan": cmd_edgeplan,
        "depeg": cmd_depeg,
        "guide": lambda m: _send_bot_guide(m.chat.id),
        "instruction": lambda m: _send_detailed_guide(m.chat.id),
        "newbie": lambda m: _send_newbie_guide(m.chat.id),
        "dca": cmd_dca,
        "p2p": handle_p2p_command,
        "alerts": cmd_alerts,
    }

    if cmd == "guide":
        await _send_bot_guide(callback.from_user.id)
        return

    if cmd == "newbie":
        await _send_newbie_guide(callback.from_user.id)
        return

    fn = mapping.get(cmd)
    if not fn:
        await bot.send_message(callback.from_user.id, "Команда не найдена в меню. Открой `/help`.", parse_mode="Markdown")
        return
    try:
        await fn(proxy)
    except Exception as e:  # noqa: BLE001 — одна кнопка не должна валить весь колбэк
        logger.error(f"inline cmd '{cmd}' failed: {e}")
        try:
            await bot.send_message(
                callback.from_user.id,
                f"⚠️ Не получилось открыть «{cmd}». Попробуй команду напрямую.",
            )
        except Exception:  # noqa: BLE001
            pass


# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await upsert_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or ""
    )
    name = message.from_user.first_name or "трейдер"

    # Карточка приветствия. Никаких списков команд — только 4 главных
    # action-кнопки. Юзер тыкает что хочет, инструкции для тех кто хочет
    # лежат под отдельной кнопкой.
    # 🆕-кнопка вверху — для новичка. Открывает PDF + 3-частевую выжимку
    # по торговой дисциплине (когда запускать /daily, только Spot, какой
    # горизонт, правила выживания первой недели). Опытному пользователю
    # можно сразу идти на «📊 Покажи прогноз сейчас» или ⚙️ Настройки.
    # Полное inline-меню: все функции бота прямо на /start (тренд, акции,
    # рынки, скринер, P2P, DCA, алерты, сигнал, бэктест, трек-рекорд и т.д.).
    # Компактный экран: 3 главных действия + 3 раздела. Остальное — внутри разделов,
    # чтобы не заваливать пользователя кнопками.
    welcome_inline = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Я новичок — гид + PDF",  callback_data="cmd:newbie")],
        [InlineKeyboardButton(text="🧭 EDGE-план: что купить сейчас", callback_data="cmd:edgeplan")],
        [
            InlineKeyboardButton(text="🎯 Сделка",             callback_data="cmd:signal"),
            InlineKeyboardButton(text="🧠 Диалектика",         callback_data="cmd:dialectica"),
            InlineKeyboardButton(text="🏛 Рынки",              callback_data="cmd:markets"),
        ],
        [
            InlineKeyboardButton(text="🧭 Анализ",             callback_data="menu:analysis"),
            InlineKeyboardButton(text="💼 Инструменты",        callback_data="menu:tools"),
            InlineKeyboardButton(text="⚙️ Ещё",                callback_data="menu:more"),
        ],
    ])

    # Сначала отдельным сообщением «приклеиваем» постоянное меню снизу —
    # дальше юзер видит его всегда вместо QWERTY.
    await message.answer(
        "🚀 _Подключаюсь к рынкам…_",
        reply_markup=persistent_kb(),
        parse_mode="Markdown",
    )

    await message.answer(
        f"👋 Привет, *{name}*!\n\n"
        "🧠 *Dialectic Edge* — честный AI-аналитик рынков.\n"
        "4 агента спорят на живых данных и выдают понятный план.\n\n"
        "🐂 Bull · 🐻 Bear · 🔍 Verifier · ⚖️ Synth\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 *С чего начать — 3 шага:*\n"
        "1️⃣ Жми «🧭 EDGE-план» — скажу, что купить прямо сейчас (или сидеть в стейбле).\n"
        "2️⃣ Купи спотом в указанных долях. Не уверен — сначала «🆕 Я новичок» (PDF-гид).\n"
        "3️⃣ Дальше расслабься — я *сам пришлю авто-алерт*, когда что-то изменится.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *Авто-алерты уже ВКЛючены* (смена тренда, лучшие сделки, депег стейблов) — "
        "ничего не пропустишь. Выключить: /alerts off.\n\n"
        "📘 *Команды и что они значат:*\n"
        "• /plan — что купить сейчас (EDGE-план) + доли. Стратегия меняется в ⚙️.\n"
        "• /signal — одна лучшая сделка прямо сейчас.\n"
        "• /daily — полный прогноз: спор 4 агентов.\n"
        "• /calc — 🧮 калькулятор сделок: записывай покупки/продажи, считаю профит.\n"
        "• /backtest — как стратегия торговала на истории.\n"
        "• /alerts — авто-уведомления · /profile — риск и стратегия · /help — всё.\n\n"
        "🧠 *Как читать:* «risk-on» = можно покупать, «risk-off» = сидим в стейбле. "
        "Доли (%) — сколько депозита в каждую монету. Всё спот, без плеча и шортов.\n\n"
        "👇 *Тыкни что нужно:*",
        reply_markup=welcome_inline,
        parse_mode="Markdown",
    )


# ─── ReplyKeyboard shortcuts ─────────────────────────────────────────────────
# Юзер тапнул на одну из 4 кнопок постоянного нижнего меню — Telegram
# присылает их подпись как обычное текстовое сообщение. Перехватываем
# по точному совпадению текста и проксируем в соответствующую команду,
# чтобы не дублировать логику.

@dp.message(F.text == PERSISTENT_BTN_DAILY)
async def _kb_daily(message: Message):
    await cmd_dialectica(message)




@dp.message(F.text == PERSISTENT_BTN_MARKETS)
async def _kb_markets(message: Message):
    await cmd_markets(message)


@dp.message(F.text == PERSISTENT_BTN_SETTINGS)
async def _kb_settings(message: Message):
    await cmd_profile(message)


@dp.message(F.text == PERSISTENT_BTN_SCREENER)
async def _kb_screener(message: Message):
    await cmd_screener(message)


@dp.message(F.text == PERSISTENT_BTN_P2P)
async def _kb_p2p(message: Message):
    await handle_p2p_command(message)


@dp.message(F.text == PERSISTENT_BTN_HELP)
async def _kb_help(message: Message):
    await cmd_help(message)


# ─── Нижнее меню: остальные функции (зеркало inline-меню /start) ──────────────
@dp.message(F.text == PERSISTENT_BTN_SIGNAL)
async def _kb_signal(message: Message):
    await cmd_signal(message)


@dp.message(F.text == PERSISTENT_BTN_DCA)
async def _kb_dca(message: Message):
    await cmd_dca(message)


@dp.message(F.text == PERSISTENT_BTN_ALERTS)
async def _kb_alerts(message: Message):
    await cmd_alerts(message)


@dp.message(F.text == PERSISTENT_BTN_SIGSTAT)
async def _kb_sigstatus(message: Message):
    await cmd_signal_status(message)


@dp.message(F.text == PERSISTENT_BTN_BACKTEST)
async def _kb_backtest(message: Message):
    await cmd_backtest(message)


@dp.message(F.text == PERSISTENT_BTN_TRACK)
async def _kb_track(message: Message):
    await cmd_trackrecord(message)


@dp.message(F.text == PERSISTENT_BTN_VIP)
async def _kb_vip(message: Message):
    await cmd_premium(message)


@dp.message(F.text == PERSISTENT_BTN_WHATIDO)
async def _kb_whatido(message: Message):
    await cmd_pitch(message)


@dp.message(F.text == PERSISTENT_BTN_NEWBIE)
async def _kb_newbie(message: Message):
    await _send_newbie_guide(message.chat.id)


@dp.message(F.text == PERSISTENT_BTN_GUIDE)
async def _kb_guide(message: Message):
    await _send_bot_guide(message.chat.id)


@dp.message(F.text == PERSISTENT_BTN_PUMP)
async def _kb_pump(message: Message):
    # Зарегистрирован ДО catch-all handle_text_input, иначе тот перехватит текст.
    await cmd_pump(message)


# ─── Разделы меню: тематические подменю (Анализ / Инструменты / Ещё) ──────────────────
# Нижняя панель держит только самое важное. Остальные функции сгруппированы по смыслу
# и открываются отдельным сообщением со своими inline-кнопками. Кнопки переиспользуют
# уже существующие callback'и (cmd:*, hsnav:*, calc:menu, tour:go) — ничего не дублируем.

def _menu_analysis_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧭 Тренд",          callback_data="hsnav:trend"),
            InlineKeyboardButton(text="📈 Акции",          callback_data="hsnav:stocks"),
        ],
        [
            InlineKeyboardButton(text="🧪 Скринер",        callback_data="cmd:screener"),
            InlineKeyboardButton(text="🚀 Что разгоняется", callback_data="cmd:pump"),
        ],
        [
            InlineKeyboardButton(text="🧪 Бэктест",        callback_data="cmd:backtest"),
            InlineKeyboardButton(text="⚖️ Депег стейблов",  callback_data="cmd:depeg"),
        ],
    ])


def _menu_tools_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧭 EDGE-план",      callback_data="cmd:edgeplan"),
            InlineKeyboardButton(text="🎯 Сделка сейчас",  callback_data="cmd:signal"),
        ],
        [
            InlineKeyboardButton(text="💰 DCA-план",       callback_data="cmd:dca"),
            InlineKeyboardButton(text="🧮 Калькулятор",    callback_data="calc:menu"),
        ],
        [
            InlineKeyboardButton(text="🤖 Автоторговля",   callback_data="cmd:signalstatus"),
            InlineKeyboardButton(text="🔔 Алерты",         callback_data="cmd:alerts"),
        ],
        [
            InlineKeyboardButton(text="🌐 P2P арбитраж",   callback_data="cmd:p2p"),
            InlineKeyboardButton(text="📊 Трек-рекорд",    callback_data="cmd:trackrecord"),
        ],
    ])


def _menu_more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🆕 Я новичок — гид", callback_data="cmd:newbie"),
            InlineKeyboardButton(text="🤝 Тур по боту",     callback_data="tour:go"),
        ],
        [
            InlineKeyboardButton(text="💎 Что я умею",      callback_data="cmd:pitch"),
            InlineKeyboardButton(text="💎 VIP-доступ",      callback_data="cmd:premium"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки",       callback_data="cmd:profile"),
            InlineKeyboardButton(text="📘 Команды",         callback_data="cmd:guide"),
        ],
        [
            InlineKeyboardButton(text="❓ Помощь",          callback_data="cmd:help"),
        ],
    ])


# Раздел → (заголовок, фабрика клавиатуры)
_MENU_SECTIONS = {
    "analysis": (
        "🧭 *Анализ рынка*\nЧто происходит на рынке прямо сейчас. Выбери инструмент:",
        _menu_analysis_kb,
    ),
    "tools": (
        "💼 *Инструменты и портфель*\nПлан сделок, расчёты, автоторговля и алерты:",
        _menu_tools_kb,
    ),
    "more": (
        "⚙️ *Ещё*\nГиды, профиль, подписка и помощь:",
        _menu_more_kb,
    ),
}


async def _send_menu_section(chat_id: int, section: str):
    """Шлёт тематическое подменю отдельным сообщением."""
    title, kb_fn = _MENU_SECTIONS.get(section, _MENU_SECTIONS["analysis"])
    try:
        await bot.send_message(chat_id, title, parse_mode="Markdown",
                               disable_web_page_preview=True, reply_markup=kb_fn())
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, title, reply_markup=kb_fn())


@dp.callback_query(F.data.startswith("menu:"))
async def handle_menu_section(callback: CallbackQuery):
    """Inline-кнопки разделов из /start: menu:analysis | menu:tools | menu:more."""
    try:
        await callback.answer()
    except Exception:  # noqa: BLE001
        pass
    section = (callback.data or "menu:analysis").split(":", 1)[1] or "analysis"
    await _send_menu_section(callback.message.chat.id, section)


@dp.message(F.text == PERSISTENT_BTN_ANALYSIS)
async def _kb_analysis(message: Message):
    await _send_menu_section(message.chat.id, "analysis")


@dp.message(F.text == PERSISTENT_BTN_TOOLS)
async def _kb_tools(message: Message):
    await _send_menu_section(message.chat.id, "tools")


@dp.message(F.text == PERSISTENT_BTN_MORE)
async def _kb_more(message: Message):
    await _send_menu_section(message.chat.id, "more")

# ─── /profile ─────────────────────────────────────────────────────────────────

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    await upsert_user(user_id)
    profile = await get_profile(user_id)

    risk_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛡️ Консерватор", callback_data="profile:risk:conservative"),
            InlineKeyboardButton(text="⚖️ Умеренный",   callback_data="profile:risk:moderate"),
            InlineKeyboardButton(text="🚀 Агрессивный", callback_data="profile:risk:aggressive"),
        ],
        [
            InlineKeyboardButton(text="⚡ Скальпинг", callback_data="profile:hz:scalp"),
            InlineKeyboardButton(text="📈 Свинг",     callback_data="profile:hz:swing"),
            InlineKeyboardButton(text="💎 Инвест",    callback_data="profile:hz:invest"),
        ],
        [
            InlineKeyboardButton(text="₿ Крипта",    callback_data="profile:mkt:crypto"),
            InlineKeyboardButton(text="📈 Акции",     callback_data="profile:mkt:stocks"),
            InlineKeyboardButton(text="🌍 Всё",       callback_data="profile:mkt:all"),
        ],
        [
            InlineKeyboardButton(text="🧭 Стратегия EDGE для /plan", callback_data="edgeprof:menu"),
        ],
    ])

    await message.answer(
        f"⚙️ *Настройка профиля*\n\n"
        f"{format_profile_card(profile)}\n\n"
        f"*Выбери параметры:*\n"
        f"_Строка 1_ — риск-профиль\n"
        f"_Строка 2_ — горизонт торговли\n"
        f"_Строка 3_ — рынки\n"
        f"_Строка 4_ — профиль стратегии EDGE для /plan (EDGE V2: база/баланс/агро/защита)\n\n"
        f"Агенты адаптируют анализ под твои настройки.",
        parse_mode="Markdown",
        reply_markup=risk_kb
    )


@dp.callback_query(F.data.startswith("profile:"))
async def handle_profile(callback: CallbackQuery):
    _, param_type, value = callback.data.split(":")
    user_id = callback.from_user.id
    profile = await get_profile(user_id)

    if param_type == "risk":
        profile["risk"] = value
    elif param_type == "hz":
        profile["horizon"] = value
    elif param_type == "mkt":
        profile["markets"] = value

    await save_profile(
        user_id,
        profile.get("risk", "moderate"),
        profile.get("horizon", "swing"),
        profile.get("markets", "all")
    )

    labels = {
        "conservative": "🛡️ Консерватор", "moderate": "⚖️ Умеренный",
        "aggressive": "🚀 Агрессивный",   "scalp": "⚡ Скальпинг",
        "swing": "📈 Свинг",              "invest": "💎 Инвестиции",
        "crypto": "₿ Крипта",             "stocks": "📈 Акции",
        "all": "🌍 Все рынки",
    }

    await callback.answer(f"✅ Сохранено: {labels.get(value, value)}")
    await callback.message.edit_text(
        f"✅ *Профиль обновлён*\n\n{format_profile_card(profile)}\n\n"
        f"Следующий анализ будет адаптирован под тебя.",
        parse_mode="Markdown"
    )


# ─── Ядро анализа ─────────────────────────────────────────────────────────────

async def legacy_run_full_analysis(
    user_id: int,
    custom_news: str = "",
    custom_mode: bool = False
) -> tuple[str, dict]:
    tasks = [
        news_fetcher.fetch_all(),
        fetch_full_context(),
        get_full_realtime_context(),
        get_profile(user_id),
        get_meta_context(),
        get_previous_digest(),
    ]

    news, geo_context, realtime_result, profile, meta_context, prev_digest = await asyncio.gather(
        *tasks, return_exceptions=True
    )

    if isinstance(prev_digest, Exception): prev_digest = ""

    if isinstance(realtime_result, Exception):
        prices_dict, live_prices = {}, ""
    elif isinstance(realtime_result, tuple) and len(realtime_result) == 2:
        prices_dict, live_prices = realtime_result
    else:
        prices_dict, live_prices = {}, ""

    if isinstance(news, Exception):         news = ""
    if isinstance(geo_context, Exception):  geo_context = ""
    if isinstance(live_prices, Exception):  live_prices = ""
    if isinstance(profile, Exception):      profile = {"risk": "moderate", "horizon": "swing", "markets": "all"}
    if isinstance(meta_context, Exception): meta_context = ""

    profile_instruction = build_profile_instruction(profile)

    if custom_mode and custom_news:
        web_context = await search_news_context(custom_news)
        news_context = (
            f"ТЕМА АНАЛИЗА: {custom_news}\n\n"
            f"{web_context}\n\n{geo_context}\n\n{meta_context}"
        )
    else:
        news_context = (
            f"{geo_context}\n\n=== НОВОСТИ ===\n{news}\n\n{meta_context}"
        )

    # Добавляем прошлый прогноз для сравнения агентами
    if prev_digest and not custom_mode:
        news_context += f"\n\n{prev_digest}"
        logger.info("Прошлый анализ передан агентам для сравнения")

    sentiment_result, confidence_instruction = await analyze_and_filter_async(
        news_context, str(live_prices)
    )
    sentiment_block = format_for_agents(sentiment_result, confidence_instruction)

    logger.info(
        f"Sentiment: {sentiment_result.label} | "
        f"Confidence: {sentiment_result.confidence} | "
        f"Score: {sentiment_result.score:+.2f}"
    )

    prices_dict = dict(prices_dict) if prices_dict else {}
    prices_dict["SENTIMENT"] = {
        "score": sentiment_result.score,
        "label": sentiment_result.label,
        "confidence": sentiment_result.confidence,
    }

    # Numeric prices for anti-stale-price guard in Speechwriter. Извлекаем
    # цены ровно для тех символов, которые могут попасть в торговый план.
    numeric_market_prices: dict[str, float] = {}
    for _sym in ("BTC", "ETH", "SOL", "BNB", "XRP", "SPX", "NDX", "VIX", "GOLD", "OIL_WTI", "DXY"):
        _entry = prices_dict.get(_sym)
        if isinstance(_entry, dict):
            _p = _entry.get("price")
            if isinstance(_p, (int, float)) and _p > 0:
                numeric_market_prices[_sym] = float(_p)
    # Алиасы которые иногда возвращает Synth: SPY=SPX, GLD=GOLD, USO/WTI=OIL_WTI
    if "SPX" in numeric_market_prices:
        numeric_market_prices.setdefault("SPY", numeric_market_prices["SPX"])
    if "GOLD" in numeric_market_prices:
        numeric_market_prices.setdefault("GLD", numeric_market_prices["GOLD"])
    if "OIL_WTI" in numeric_market_prices:
        numeric_market_prices.setdefault("WTI", numeric_market_prices["OIL_WTI"])
        numeric_market_prices.setdefault("USO", numeric_market_prices["OIL_WTI"])

    # ATR keys прокидываются отдельно (pre-live-hardening): web_search кладёт
    # их как top-level prices["ATR_BTC"] и т.д. — иначе ATR-aware SL guard
    # падает к fixed-fallback.
    for _sym in ("BTC", "ETH", "SOL", "BNB", "XRP"):
        _atr_key = f"ATR_{_sym}"
        _atr_val = prices_dict.get(_atr_key)
        if isinstance(_atr_val, (int, float)) and _atr_val > 0:
            numeric_market_prices[_atr_key] = float(_atr_val)

    orchestrator = DebateOrchestrator()
    report = await orchestrator.run_debate(
        news_context=news_context,
        live_prices=live_prices,
        profile_instruction=profile_instruction + sentiment_block,
        custom_mode=custom_mode,
        market_prices=numeric_market_prices,
    )
    report, _san_lines = sanitize_full_report(report)
    if _san_lines:
        logger.info("Пост-фильтр полного отчёта: удалено строк: %s", _san_lines)

    # ── Уровень сигнала ───────────────────────────────────────────────────────
    _conf_raw = sentiment_result.confidence
    _conf_map = {"HIGH": 0.85, "MEDIUM": 0.55, "LOW": 0.25, "EXTREME": 0.95}
    if isinstance(_conf_raw, str):
        _conf_num = _conf_map.get(_conf_raw.upper(), 0.5)
    else:
        try:
            _conf_num = float(_conf_raw)
        except (TypeError, ValueError):
            _conf_num = 0.5

    stars = signal_to_stars(_conf_num)
    pct   = int(_conf_num * 100)

    separator = "─" * 30 + "\n"
    signal_line = (
        f"📶 *Уровень сигнала:* {stars} ({pct}% — уверенность FinBERT в тоне новостей)\n"
        f"_Не направление рынка; расшифровка — в шапке дайджеста._\n\n"
    )
    report = report.replace(separator, separator + signal_line, 1)

    # ── Сохраняем прогнозы ────────────────────────────────────────────────────
    source = custom_news[:300] if custom_mode else str(news)[:300]
    _pv, _snap = build_digest_persist_metadata(
        custom_mode=custom_mode,
        news_context=news_context,
        live_prices=str(live_prices),
        profile=profile if isinstance(profile, dict) else {},
        sentiment_result=sentiment_result,
        prices_dict=prices_dict,
    )
    await save_predictions_from_report(
        report,
        source_news=source,
        bot=get_bot(),
        admin_ids=ADMIN_IDS,
        prompt_versions=_pv,
        model_inputs_snapshot=_snap,
    )
    await log_report(
        user_id,
        "analyze" if custom_mode else "daily",
        source,
        report[:500]
    )

    if not custom_mode:
        storage.cache_report(report, prices_dict, owner_user_id=user_id)
        if scheduler is not None:
            asyncio.create_task(scheduler.export_now())
        # Кэшируем дайджест на GitHub для отслеживания точности (п.6)
        try:
            date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
            parts = parse_report_parts(report)
            full_debates = ""
            if parts.get("rounds"):
                blocks = []
                for i, r in enumerate(parts["rounds"], 1):
                    blocks.append(f"{'='*12} Раунд {i} {'='*12}\n\n{r}")
                full_debates = "\n\n".join(blocks)
            asyncio.create_task(push_digest_cache(report, date_str, full_debates))
        except Exception as e:
            logger.warning(f"Digest cache error: {e}")

    return report, prices_dict


# ─── /daily ───────────────────────────────────────────────────────────────────

async def run_daily_analysis(user_id: int) -> str:
    report, _ = await analysis_service_run_full_analysis(user_id)
    return report


async def deliver_scheduled_daily(user_id: int) -> None:
    """Рассылка подписчикам: как /daily — сначала общий кэш (без токенов), иначе полный прогон.

    Шедулер всегда отдаёт swing-горизонт (DEFAULT_HORIZON_KEY): подписки до
    Tier-1 не имели понятия горизонта, и менять расписание под intraday/position
    мы будем уже в /subscribe (отдельный PR).
    """
    try:
        pack = get_horizon(DEFAULT_HORIZON_KEY)
        cached = storage.get_cached_report(horizon=pack.key)
        if cached:
            report = cached["report"]
            prices = cached.get("prices") or {}
            try:
                await save_predictions_from_report(report, source_news="")
            except Exception as e:
                logger.warning("deliver_scheduled_daily: sync daily_context failed: %s", e)
            await send_daily_digest_bundle(user_id, user_id, report, prices, horizon=pack)
            return
        report, prices = await analysis_service_run_full_analysis(user_id, horizon=pack)
        await send_daily_digest_bundle(user_id, user_id, report, prices, horizon=pack)
    except Exception as e:
        logger.warning("Рассылка дайджеста user %s: %s", user_id, e)


async def broadcast_dialectica_digest() -> int:
    """Рассылка утреннего Дайджеста Диалектики (09:00 MSK) всем, у кого есть доступ.

    Получатели — пользователи с активным премиумом *или* фри-триалом
    (payments.db.list_access_user_ids). У кого нет ни того, ни другого — не
    получают ничего.

    Дайджест берётся ОДИН РАЗ из дневного кэша PostgreSQL/Neon (его готовит
    cron в 08:50 MSK), а затем рассылается всем — без повторного прогона AI на
    каждого пользователя. По завершении день помечается broadcast_done=TRUE,
    чтобы GitHub-Actions фолбэк не дублировал отправку.

    Возвращает число пользователей, которым успешно ушёл дайджест.
    """
    from datetime import date as _date
    today = _date.today()

    # 0. Защита от дубля: если за сегодня дайджест уже разослан — выходим.
    #    (живой бот и GitHub-фолбэк координируются через флаг broadcast_done).
    try:
        from payments.db import is_digest_broadcast
        if await is_digest_broadcast(today):
            logger.info("broadcast: дайджест за %s уже разослан — пропуск", today)
            return 0
    except Exception as e:
        logger.warning("broadcast: is_digest_broadcast check failed: %s", e)

    # 1. Берём готовый дайджест из PostgreSQL (его кладёт cron_digest.py).
    report: str = ""
    prices: dict = {}
    try:
        from payments.db import get_today_digest
        cached = await get_today_digest()
        if cached:
            report = cached.get("digest_text") or cached.get("short_report") or ""
    except Exception as e:
        logger.warning("broadcast: get_today_digest failed: %s", e)

    # 2. Фолбэк на локальный кэш бота, если в БД пусто.
    if not report:
        try:
            pack = get_horizon(DEFAULT_HORIZON_KEY)
            local = storage.get_cached_report(horizon=pack.key)
            if local:
                report = local.get("report") or ""
                prices = local.get("prices") or {}
        except Exception as e:
            logger.warning("broadcast: local cache fallback failed: %s", e)

    if not report:
        logger.warning("broadcast: нет готового дайджеста на %s — рассылка пропущена", today)
        return 0

    # 3. Получатели: премиум ИЛИ активный триал.
    try:
        from payments.db import list_access_user_ids, mark_digest_broadcast
        recipients = await list_access_user_ids()
    except Exception as e:
        logger.error("broadcast: не удалось получить получателей: %s", e)
        return 0

    if not recipients:
        logger.info("broadcast: нет получателей с премиум/триалом")
        return 0

    pack = get_horizon(DEFAULT_HORIZON_KEY)
    sent = 0
    for uid in recipients:
        try:
            await send_daily_digest_bundle(uid, uid, report, prices, horizon=pack)
            sent += 1
        except Exception as e:
            logger.warning("broadcast: ошибка отправки user %s: %s", uid, e)
        # Throttle: ~20 msg/sec лимит Telegram. Держим запас.
        await asyncio.sleep(0.06)

    try:
        await mark_digest_broadcast(today)
    except Exception as e:
        logger.warning("broadcast: mark_digest_broadcast failed: %s", e)

    logger.info("📬 Дайджест Диалектики разослан: %s/%s получателей (%s)",
                sent, len(recipients), today)
    return sent


# ─── Multi-horizon picker ─────────────────────────────────────────────────────

# Алиасы CLI-аргументов /daily для обратной совместимости. `force/fresh/new/новый`
# поддерживаются как и раньше, плюс ключи горизонтов и человекочитаемые синонимы.
_HORIZON_ARG_ALIASES = {
    "intraday": "intraday",
    "intra": "intraday",
    "интрадей": "intraday",
    "fast": "intraday",
    "scalp": "intraday",
    "скальп": "intraday",
    "1-3": "intraday",
    "1-3д": "intraday",
    "1-3d": "intraday",
    "swing": "swing",
    "свинг": "swing",
    "default": "swing",
    "standard": "swing",
    "стандарт": "swing",
    "7-14": "swing",
    "7-14д": "swing",
    "7-14d": "swing",
    "position": "position",
    "позиция": "position",
    "позиционный": "position",
    "long": "position",
    "лонг": "position",
    "30+": "position",
    "30+д": "position",
    "30+d": "position",
}
_FORCE_TOKENS = {"force", "fresh", "новый", "new", "f"}


def _parse_daily_args(text: str) -> tuple[str | None, bool]:
    """`/daily intraday force` → ("intraday", True). Возвращает (horizon_key|None, force_fresh)."""
    horizon_key: str | None = None
    force_fresh = False
    for token in (text or "").split()[1:]:
        norm = token.strip().lower()
        if not norm:
            continue
        if norm in _FORCE_TOKENS:
            force_fresh = True
            continue
        mapped = _HORIZON_ARG_ALIASES.get(norm)
        if mapped and horizon_key is None:
            horizon_key = mapped
    return horizon_key, force_fresh


def _resolve_force_fresh(user_id: int, requested: bool) -> bool:
    """Force-fresh (пере-генерация дайджеста) — ТОЛЬКО админ.

    Дайджест генерится 1 раз в день (cron_digest.py утром) и кэшируется в PG.
    Обычные юзеры всегда получают этот дневной кэш — `force` для них недоступен
    (дорогие LLM-вызовы → защита от abuse и расхода токенов). Админ может
    форснуть свежую генерацию через `/daily [horizon] force`.
    """
    return bool(requested) and is_admin(user_id)


def _horizon_picker_keyboard(force_fresh: bool = False) -> InlineKeyboardMarkup:
    """3 кнопки выбора горизонта. `force` зашиваем в callback_data, чтобы
    обработчик не зависел от внешнего состояния."""
    suffix = ":f" if force_fresh else ""
    rows = []
    for key in all_horizon_keys():
        pack = HORIZON_PACKS[key]
        rows.append([
            InlineKeyboardButton(
                text=f"{pack.label_pretty}",
                callback_data=f"dh:{key}{suffix}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_horizon_picker(
    message: Message, force_fresh: bool = False, *, allow_force: bool = True,
) -> None:
    note = "" if not force_fresh else " (без кэша)"
    # Подсказку про `force` показываем только тем, кому она доступна (админу) —
    # обычным юзерам она вводит в заблуждение (дайджест 1×/день из кэша).
    force_hint = "\n`/daily force` — сбросить кэш (только админ)." if allow_force else ""
    await message.answer(
        "🎯 *Выбери горизонт планирования* ⤵️" + note + "\n\n"
        "⚡️ *1–3 дня* — стопы плотные, R/R от 1:1.5, доля депо мелкая.\n"
        "📈 *7–14 дней* — свинг, стандартный режим (по умолчанию).\n"
        "🏔 *30+ дней* — м��кро-позиция, R/R от 1:3, входим осторожнее.\n\n"
        "Можно сразу командой: `/daily intraday`, `/daily swing`, `/daily position`."
        + force_hint,
        parse_mode="Markdown",
        reply_markup=_horizon_picker_keyboard(force_fresh=force_fresh),
    )


async def _run_daily_for_horizon(
    chat_id: int,
    user_id: int,
    horizon_key: str,
    *,
    force_fresh: bool,
    wait_msg_id: int | None = None,
    reply_to: Message | None = None,
) -> None:
    """Общий движок /daily: используется и из Message, и из callback горизонт-пикера.

    `wait_msg_id` — ID сообщения «⏳ Запускаю анализ...», которое мы обновляем/удаляем.
    `reply_to` — Message от которого пришла команда (нужен для фолбэк-ответов на
    ошибках, когда edit_message_text недоступен).
    """
    pack = get_horizon(horizon_key)

    # Try PostgreSQL digest cache first (pre-generated by cron_digest.py).
    if not force_fresh and horizon_key in ("mid", ""):
        try:
            from payments.db import get_today_digest
            pg_digest = await get_today_digest()
            if pg_digest and pg_digest.get("short_report"):
                if wait_msg_id is not None:
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=wait_msg_id)
                    except Exception:
                        pass
                full = pg_digest["digest_text"] or ""
                await store_and_link_debate(user_id, full, full)
                await send_daily_digest_bundle(
                    chat_id, user_id, full, {}, horizon=pack,
                )
                await bot.send_message(
                    chat_id,
                    "⚡ Дайджест из кеша (сгенерирован утром). "
                    f"Свежий: `/daily {pack.key} force`",
                    parse_mode="Markdown",
                )
                return
        except Exception as e:
            logger.debug("PG digest cache miss: %s", e)

    cached = None if force_fresh else storage.get_cached_report(horizon=pack.key)
    if cached:
        report = cached["report"]
        prices = cached.get("prices") or {}
        try:
            await save_predictions_from_report(report, source_news="")
        except Exception as e:
            logger.warning("cmd_daily cache: sync daily_context failed: %s", e)
        # Удаляем «⏳ Запускаю» если он есть, чтобы не путать пользователя
        if wait_msg_id is not None:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=wait_msg_id)
            except Exception:
                pass
        await send_daily_digest_bundle(chat_id, user_id, report, prices, horizon=pack)
        await bot.send_message(
            chat_id,
            f"📦 Кэш {pack.label_pretty} от {cached['timestamp']}. "
            f"Повтор без AI до ~{CACHE_TTL_HOURS} ч. "
            f"Сброс: `/daily {pack.key} force`",
            parse_mode="Markdown",
        )
        return

    if wait_msg_id is None:
        wait = await bot.send_message(
            chat_id,
            f"⏳ *Запускаю анализ — {pack.label_pretty} ({pack.label})...*\n\n"
            "🔄 Живые цены → новости → геополитика → дебаты агентов\n"
            "_Займёт 2–5 минут..._",
            parse_mode="Markdown",
        )
        wait_msg_id = wait.message_id
    else:
        try:
            await bot.edit_message_text(
                f"⏳ *Запускаю анализ — {pack.label_pretty} ({pack.label})...*\n\n"
                "🔄 Живые цены → новости → геополитика → дебаты агентов\n"
                "_Займёт 2–5 минут..._",
                chat_id=chat_id,
                message_id=wait_msg_id,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    try:
        await increment_requests(user_id)
        report, prices = await analysis_service_run_full_analysis(user_id, horizon=pack)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=wait_msg_id)
        except Exception:
            pass
        await send_daily_digest_bundle(chat_id, user_id, report, prices, horizon=pack)
    except Exception as e:
        logger.error(f"Daily error (horizon={pack.key}): {e}", exc_info=True)
        try:
            await bot.edit_message_text(
                f"❌ *Ошибка ({pack.label_pretty}):* `{str(e)[:200]}`\n\n"
                "Проверь: API ключи, интернет, BOT_TOKEN.",
                chat_id=chat_id,
                message_id=wait_msg_id,
                parse_mode="Markdown",
            )
        except Exception:
            target = reply_to.answer if reply_to else lambda *a, **kw: bot.send_message(chat_id, *a, **kw)
            try:
                await target(
                    f"❌ *Ошибка ({pack.label_pretty}):* `{str(e)[:200]}`\n\n"
                    "Проверь: API ключи, интернет, BOT_TOKEN.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass


# ─── Диалектика (интро + база дайджестов) ────────────────────────────────────

_DIALECTICA_INTRO = (
    "🧠 *Диалектика*\n\n"
    "Это SaaS-система, внутри которой работает целая команда AI-агентов. "
    "Они круглосуточно собирают рыночные данные, новости и геополитику, "
    "спорят между собой (бык против медведя, проверяющий, синтезатор) и на "
    "выходе дают тебе взвешенный анализ по рынку и отдельным активам — "
    "без «угадайки» и хайпа.\n\n"
    "Каждое утро система готовит свежий *Дайджест Диалектики* на горизонт "
    "*7–14 дней*. Можешь запросить новый прямо сейчас или открыть базу "
    "дайджестов за последние 14 дней.\n\n"
    "Выбирай 👇"
)


def _dialectica_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Выдать новый Дайджест Диалектики",
                              callback_data="cmd:daily")],
        [InlineKeyboardButton(text="🗂 База Дайджестов",
                              callback_data="cmd:digest_base")],
    ])


async def cmd_dialectica(message: Message):
    """Интро-экран Диалектики с двумя кнопками: новый дайджест / база дайджестов."""
    await message.answer(
        _DIALECTICA_INTRO,
        parse_mode="Markdown",
        reply_markup=_dialectica_keyboard(),
    )


async def cmd_digest_base(message: Message):
    """«База Дайджестов» — список дайджестов за последние 14 дней из PostgreSQL (Neon)."""
    try:
        from payments.db import get_recent_digests
        digests = await get_recent_digests(14)
    except Exception as e:
        logger.error(f"digest_base fetch failed: {e}")
        digests = []

    if not digests:
        await message.answer(
            "🗂 *База Дайджестов*\n\n"
            "Пока пусто — за последние 14 дней нет сохранённых дайджестов.\n"
            "Они появляются автоматически каждое утро (или нажми "
            "«🆕 Выдать новый Дайджест Диалектики»).",
            parse_mode="Markdown",
            reply_markup=_dialectica_keyboard(),
        )
        return

    _RU_MONTHS = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
    }
    rows: list[list[InlineKeyboardButton]] = []
    for d in digests:
        dt = d["digest_date"]
        label = f"{dt.day:02d} {_RU_MONTHS.get(dt.month, '')}"
        regime = (d.get("market_regime") or "").strip()
        if regime:
            label += f" · {regime[:24]}"
        rows.append([InlineKeyboardButton(
            text=f"📄 {label}",
            callback_data=f"dgview:{dt.isoformat()}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к Диалектике",
                                      callback_data="cmd:dialectica")])

    await message.answer(
        f"🗂 *База Дайджестов* — последние 14 дней\n\n"
        f"Сохранено: *{len(digests)}*. Выбери дату, чтобы открыть дайджест 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("dgview:"))
async def handle_digest_view(callback: CallbackQuery):
    """Открывает сохранённый дайджест за выбранную дату из базы."""
    await callback.answer()
    date_str = (callback.data or "").split(":", 1)[1] if ":" in (callback.data or "") else ""
    try:
        from datetime import date as _date
        target = _date.fromisoformat(date_str)
    except Exception:
        await bot.send_message(callback.from_user.id, "⚠️ Не удалось разобрать дату дайджеста.")
        return

    try:
        from payments.db import get_digest_by_date
        digest = await get_digest_by_date(target)
    except Exception as e:
        logger.error(f"dgview fetch failed: {e}")
        digest = None

    if not digest:
        await bot.send_message(
            callback.from_user.id,
            f"⚠️ Дайджест за {date_str} не найден в базе.",
        )
        return

    body = (digest.get("short_report") or "").strip() or (digest.get("digest_text") or "").strip()
    if not body:
        body = "_(пустой дайджест)_"
    # Telegram-лимит сообщения ~4096 символов — режем с запасом.
    MAX = 3900
    header = f"📄 *Дайджест Диалектики — {date_str}*\n\n"
    if len(body) > MAX:
        body = body[:MAX] + "\n\n…(дайджест обрезан)"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К базе дайджестов", callback_data="cmd:digest_base")],
    ])
    await bot.send_message(
        callback.from_user.id,
        header + body,
        parse_mode="Markdown",
        reply_markup=back_kb,
        disable_web_page_preview=True,
    )


@dp.message(Command("daily"))
@require_vip
async def cmd_daily(message: Message):
    user_id = message.from_user.id
    await upsert_user(user_id, message.from_user.username or "")

    if not await check_limit(user_id):
        await message.answer(
            f"⛔ *Лимит* — {FREE_DAILY_LIMIT} запросов/день (free)\n"
            "Попробуй завтра или /subscribe для авторассылки.",
            parse_mode="Markdown"
        )
        return

    _, requested_force = _parse_daily_args(message.text or "")
    force_fresh = _resolve_force_fresh(user_id, requested_force)
    if requested_force and not force_fresh:
        # Юзер попросил force, но он не админ — отдаём дневной кэш и поясняем.
        await message.answer(
            "🔒 Принудительное обновление (`force`) доступно только админу.\n"
            "Дайджест генерится 1×/день — отдаю свежий из дневного кэша.",
            parse_mode="Markdown",
        )

    # Горизонт зафиксирован на swing (7–14 дней). Выбор горизонта убран —
    # любой переданный аргумент горизонта игнорируется, всегда отдаём swing.
    horizon_key = DEFAULT_HORIZON_KEY

    await _run_daily_for_horizon(
        chat_id=message.chat.id,
        user_id=user_id,
        horizon_key=horizon_key,
        force_fresh=force_fresh,
        reply_to=message,
    )


@dp.callback_query(F.data.startswith("dh:"))
async def handle_daily_horizon_pick(callback: CallbackQuery):
    """Колбэк горизонт-пикера: dh:{key} или dh:{key}:f."""
    await callback.answer()
    parts = (callback.data or "").split(":")
    if len(parts) < 2:
        return
    horizon_key = parts[1]
    requested_force = (len(parts) >= 3 and parts[2] == "f")
    if horizon_key not in HORIZON_PACKS:
        return

    user_id = callback.from_user.id
    # force — только админ (см. _resolve_force_fresh). Колбэк-кнопки с :f
    # генерятся лишь когда force_fresh уже True, но гейтим defensively.
    force_fresh = _resolve_force_fresh(user_id, requested_force)
    await upsert_user(user_id, callback.from_user.username or "")

    if not await check_limit(user_id):
        if callback.message:
            try:
                await callback.message.edit_text(
                    f"⛔ *Лимит* — {FREE_DAILY_LIMIT} запросов/день (free)\n"
                    "Попробуй завтра или /subscribe для авторассылки.",
                    parse_mode="Markdown",
                )
            except Exception:
                await bot.send_message(
                    user_id,
                    f"⛔ *Лимит* — {FREE_DAILY_LIMIT} запросов/день (free)\n"
                    "Попробуй завтра или /subscribe для авторассылки.",
                    parse_mode="Markdown",
                )
        return

    chat_id = callback.message.chat.id if callback.message else user_id
    wait_msg_id = callback.message.message_id if callback.message else None

    pack = HORIZON_PACKS[horizon_key]
    if wait_msg_id is not None:
        try:
            await bot.edit_message_text(
                f"⏳ *Запускаю анализ — {pack.label_pretty} ({pack.label})...*\n\n"
                "🔄 Живые цены → новости → геополитика → дебаты агентов\n"
                "_Займёт 2–5 минут..._",
                chat_id=chat_id,
                message_id=wait_msg_id,
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            wait_msg_id = None

    await _run_daily_for_horizon(
        chat_id=chat_id,
        user_id=user_id,
        horizon_key=horizon_key,
        force_fresh=force_fresh,
        wait_msg_id=wait_msg_id,
        reply_to=callback.message,
    )


# ─── /analyze ─────────────────────────────────────────────────────────────────

@dp.message(Command("analyze"))
async def cmd_analyze(message: Message):
    user_id = message.from_user.id
    await upsert_user(user_id, message.from_user.username or "")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "❗ *Укажи новость для анализа*\n\n"
            "Примеры:\n"
            "`/analyze Fed снизил ставку до 4.25%`\n"
            "`/analyze Binance заморозила вывод в США`\n"
            "`/analyze Китай ограничил экспорт редкоземельных металлов`",
            parse_mode="Markdown"
        )
        return

    if not await check_limit(user_id):
        await message.answer(
            f"⛔ *Лимит* — {FREE_DAILY_LIMIT} запросов/день (free)",
            parse_mode="Markdown"
        )
        return

    user_news = parts[1].strip()
    wait_msg = await message.answer(
        f"🔍 *Анализирую:*\n_{user_news[:150]}_\n\n"
        "⏳ Ищу контекст + запускаю дебаты...",
        parse_mode="Markdown"
    )

    try:
        await increment_requests(user_id)
        report, prices = await analysis_service_run_full_analysis(
            user_id, custom_news=user_news, custom_mode=True
        )
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=wait_msg.message_id)
        except Exception:
            pass  # сообщение уже удалено — не критично
        await send_daily_digest_bundle(message.chat.id, user_id, report, prices)

    except Exception as e:
        logger.error(f"Analyze error: {e}", exc_info=True)
        try:
            await bot.edit_message_text(
                f"❌ *Ошибка:* `{str(e)[:200]}`",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )
        except Exception:
            await message.answer(f"❌ *Ошибка:* `{str(e)[:200]}`", parse_mode="Markdown")



# ─── /russia ──────────────────────────────────────────────────────────────────

@dp.message(Command("russia"))
async def cmd_russia(message: Message):
    user_id = message.from_user.id
    await upsert_user(user_id, message.from_user.username or "")

    if not await check_limit(user_id):
        await message.answer(
            f"⛔ *Лимит* — {FREE_DAILY_LIMIT} запросов/день (free)",
            parse_mode="Markdown"
        )
        return

    # Проверяем кэш РФ (живёт 2 часа как основной)
    import time
    now_ts = time.time()
    if russia_cache.get("report") and (now_ts - russia_cache.get("ts", 0)) < 7200:
        cached_ru = russia_cache["report"]
        await send_russia_chart_photo(message.chat.id, cached_ru)
        for chunk in split_message(cached_ru):
            await message.answer(chunk, parse_mode="Markdown")
        await message.answer(
            f"📦 _Кэш от {russia_cache['timestamp']}. Новый через 2ч._",
            parse_mode="Markdown",
            reply_markup=feedback_keyboard("russia")
        )
        return

    # Нужен глобальный анализ как основа (last_report + fallback на отчёт этого user_id с /daily)
    cached = storage.get_cached_report()
    global_report = ""
    if cached and isinstance(cached.get("report"), str):
        global_report = cached["report"]
    if not global_report.strip():
        ur = storage.get_user_last_cached_report(user_id)
        if isinstance(ur, str) and ur.strip():
            global_report = ur

    # Если нет актуального дайджеста — предлагаем выбор
    if not global_report.strip():
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Сначала запущу /daily",
                callback_data="russia_choice:daily"
            ),
            InlineKeyboardButton(
                text="🚀 Запустить сейчас",
                callback_data="russia_choice:now"
            ),
        ]])
        await message.answer(
            "💡 *Совет перед запуском /russia:*\n\n"
            "Глобальный дайджест (/daily) даёт агентам полный контекст рынков.\n"
            "Без него анализ будет работать только на РФ данных.\n\n"
            "*Что делаем?*",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    wait_msg = await message.answer(
        "🇷🇺 *Запускаю анализ для России...*\n\n"
        "🔄 ЦБ РФ → Мосбиржа → РБК → Llama агенты → Mistral синтез\n"
        "_Займёт 1–3 минуты..._",
        parse_mode="Markdown"
    )

    try:
        await increment_requests(user_id)

        # Собираем РФ данные
        russia_context = await fetch_russia_context()

        # Запускаем диалектический анализ
        report = await run_russia_analysis(global_report, russia_context)

        # Санитайзер для russia — убирает галлюцинации (ставки банков и тд)
        report, _san_lines_ru = sanitize_full_report(report)
        if _san_lines_ru:
            logger.info("Russia пост-фильтр: удалено строк: %d", _san_lines_ru)

        # Кэшируем
        import time
        russia_cache["report"]    = report
        russia_cache["timestamp"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        russia_cache["ts"]        = time.time()

        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=wait_msg.message_id)
        except Exception:
            pass  # сообщение уже удалено — не критично

        await send_russia_chart_photo(message.chat.id, report)

        # Парсим секции для навигации (пробуем разные разделители)
        opportunities = ""
        risks = ""
        synthesis = ""

        # Пробуем разные разделители
        for sep in ["─" * 30, "---", "___"]:
            sections = report.split(sep)
            if len(sections) >= 4:
                opportunities = sections[1].strip() if len(sections) > 1 else ""
                risks = sections[2].strip() if len(sections) > 2 else ""
                synthesis = sections[3].strip() if len(sections) > 3 else ""
                break

        # Если не получилось парсить — сохраняем весь отчёт
        if not opportunities and not risks:
            opportunities = "Раздел возможностей"
            risks = "Раздел рисков"
            synthesis = synthesis if synthesis else "Раздел итогов"

        # Сохраняем секции в кэш для навигации
        russia_cache["sections"] = {
            "opportunities": opportunities,
            "risks": risks,
            "synthesis": synthesis
        }

        # Клавиатура навигации
        nav_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Возможности", callback_data="russia_nav:opp"),
                InlineKeyboardButton(text="🔴 Риски", callback_data="russia_nav:risk"),
            ],
            [
                InlineKeyboardButton(text="⚖️ Итог", callback_data="russia_nav:synth"),
                InlineKeyboardButton(text="📊 Полный", callback_data="russia_nav:full"),
            ]
        ])

        for chunk in split_message(report):
            await message.answer(clean_markdown(chunk), parse_mode="Markdown")

        await message.answer(
            "💬 *Был ли анализ полезным?*",
            parse_mode="Markdown",
            reply_markup=feedback_keyboard("russia")
        )

        await message.answer(
            "📍 *Навигация по разделам:*",
            parse_mode="Markdown",
            reply_markup=nav_keyboard
        )

    except Exception as e:
        logger.error(f"Russia error: {e}", exc_info=True)
        try:
            await bot.edit_message_text(
                f"❌ *��шибка:* `{str(e)[:200]}`",
                chat_id=message.chat.id,
                message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )
        except Exception:
            await message.answer(f"❌ *Ошибка:* `{str(e)[:200]}`", parse_mode="Markdown")



# ─── Выбор перед /russia ──────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("russia_nav:"))
async def handle_russia_nav(callback: CallbackQuery):
    await callback.answer()
    data = callback.data.split(":")
    section = data[1] if len(data) > 1 else "full"

    # Проверяем есть ли кэш
    if not russia_cache.get("report"):
        await callback.message.answer(
            "⚠️ Нет сохранённого отчёта.\nЗапусти /russia сначала!",
            parse_mode="Markdown"
        )
        return

    sections = russia_cache.get("sections", {})
    full_report = russia_cache.get("report", "")

    text = ""
    if section == "opp":
        text = sections.get("opportunities", "Раздел не найден. Запусти /russia заново.")
    elif section == "risk":
        text = sections.get("risks", "Раздел не найден. Запусти /russia заново.")
    elif section == "synth":
        text = sections.get("synthesis", "Раздел не найден. Запусти /russia заново.")
    elif section == "full":
        text = full_report[:3500] if full_report else "Отчёт не найден. Запусти /russia заново."
    else:
        text = "Выбери раздел:"

    nav_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Возможности", callback_data="russia_nav:opp"),
            InlineKeyboardButton(text="🔴 Риски", callback_data="russia_nav:risk"),
        ],
        [
            InlineKeyboardButton(text="⚖️ Итог", callback_data="russia_nav:synth"),
            InlineKeyboardButton(text="📊 Полный", callback_data="russia_nav:full"),
        ]
    ])

    await callback.message.answer(
        f"📍 *Раздел:* {section.upper()}\n\n{text[:3500]}",
        parse_mode="Markdown",
        reply_markup=nav_keyboard
    )


@dp.callback_query(F.data.startswith("russia_choice:"))
async def handle_russia_choice(callback: CallbackQuery):
    action = callback.data.split(":")[1]
    user_id = callback.from_user.id

    await callback.message.edit_reply_markup(reply_markup=None)

    if action == "daily":
        await callback.answer()
        await callback.message.answer(
            "✅ Отличный выбор! Запускай /daily — после него /russia выдаст максимум.",
            parse_mode="Markdown"
        )
        return

    # action == "now" — запускаем сразу
    await callback.answer("🚀 Запускаю!")

    wait_msg = await callback.message.answer(
        "🇷🇺 *Запускаю анализ для России...*\n\n"
        "🔄 ЦБ РФ → Мосбиржа → РБК → Llama агенты → Mistral синтез\n"
        "_Займёт 1–3 минуты..._",
        parse_mode="Markdown"
    )

    try:
        await increment_requests(user_id)
        global_report = "Глобальный анализ не запускался. Работаю только на данных РФ."
        russia_context = await fetch_russia_context()
        report = await run_russia_analysis(global_report, russia_context)

        import time
        russia_cache["report"]    = report
        russia_cache["timestamp"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        russia_cache["ts"]        = time.time()

        try:
            await bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=wait_msg.message_id
            )
        except Exception:
            pass  # сообщение уже удалено — не критично

        await send_russia_chart_photo(callback.message.chat.id, report)
        for chunk in split_message(report):
            await callback.message.answer(clean_markdown(chunk), parse_mode="Markdown")

        await callback.message.answer(
            "💬 *Был ли анализ полезным?*",
            parse_mode="Markdown",
            reply_markup=feedback_keyboard("russia")
        )

    except Exception as e:
        logger.error(f"Russia choice error: {e}", exc_info=True)
        try:
            await bot.edit_message_text(
                f"❌ *Ошибка:* `{str(e)[:200]}`",
                chat_id=callback.message.chat.id,
                message_id=wait_msg.message_id,
                parse_mode="Markdown"
            )
        except Exception:
            await callback.message.answer(f"❌ *Ошибка:* `{str(e)[:200]}`", parse_mode="Markdown")


# ─── /markets (живой контекст + сигналы Binance/Bybit, как в signals.py) ─────


# Минималистичный набор кнопок: выбор секции (`markets:section:*`) + действия.
# Юзер просил «меньше жмодци, только нужные, минимализм»: оставляем 8 кнопок
# (4 ряда по 2). Активная секция помечена точкой («• Крипта»), чтобы видеть
# где находишься без edit_message_text-навигации.

_SECTION_BUTTONS: tuple[tuple[str, str], ...] = (
    ("crypto", "💲 Крипта"),
    ("macro", "🌐 Макро"),
    ("indices", "📈 Индексы"),
    ("commod", "⛽ Сырьё"),
    ("cot", "📊 COT"),
    ("etf", "💼 ETF"),
    ("signals", "📡 Сигналы"),
    ("all", "🏛 Всё"),
)


def _section_label(key: str, label: str, current: str) -> str:
    return f"• {label}" if key == current else label


def _markets_section_keyboard(
    is_enabled: bool,
    current: str = "summary",
    user_id: int | None = None,
    *,
    current_page: int = 0,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # 4 ряда по 2 кнопки — выбор секции.
    pairs = list(zip(_SECTION_BUTTONS[0::2], _SECTION_BUTTONS[1::2]))
    for (k1, l1), (k2, l2) in pairs:
        rows.append([
            InlineKeyboardButton(
                text=_section_label(k1, l1, current),
                callback_data=f"markets:section:{k1}",
            ),
            InlineKeyboardButton(
                text=_section_label(k2, l2, current),
                callback_data=f"markets:section:{k2}",
            ),
        ])
    # Pagination row — показываем только если секция многострани��ная.
    # Юзер просил «листать как книжку»: ◀ Prev / i / N / Next ▶ на одном
    # сооб��ении (edit_message_text), без рассыпания на 3 портянки.
    if total_pages > 1:
        cur = max(0, min(int(current_page), total_pages - 1))
        prev_idx = (cur - 1) % total_pages
        next_idx = (cur + 1) % total_pages
        rows.append([
            InlineKeyboardButton(
                text="◀ Назад",
                callback_data=f"markets:page:{current}:{prev_idx}",
            ),
            InlineKeyboardButton(
                text=f"{cur + 1}/{total_pages}",
                callback_data="markets:noop",
            ),
            InlineKeyboardButton(
                text="Вперёд ▶",
                callback_data=f"markets:page:{current}:{next_idx}",
            ),
        ])
    # Управляющий ряд: лучшая сделка + обновить + сигналы on/off.
    rows.append([
        InlineKeyboardButton(text="🎯 Лучшая", callback_data="cmd:signal"),
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=f"markets:section:{current}",
        ),
        InlineKeyboardButton(
            text="🔕" if is_enabled else "🔔",
            callback_data="markets:disable" if is_enabled else "markets:enable",
        ),
    ])
    # Глоссарий «📖 Что значат эти слова?» — stateless, открывает разбор
    # терминов /markets (S/R, MA-триггеры, σ̂, Hurst, Markov, quant-метрики).
    # UID в callback_data чтоб чужой клик в групповом чате не выдавал от��ет
    # (тот же паттерн что у `sigexplain:` в `_signal_explain_keyboard`).
    if user_id is not None:
        rows.append([
            InlineKeyboardButton(
                text="📖 Что значат эти слова?",
                callback_data=f"mktexplain:{user_id}",
            ),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Обратная совместимость для старых хэндлеров (markets:check / backtest).
def _markets_signal_keyboard(is_enabled: bool) -> InlineKeyboardMarkup:
    return _markets_section_keyboard(is_enabled, current="summary")


async def _render_markets_section(
    *,
    chat_id: int,
    user_id: int,
    section: str,
    wait_message_id: int | None = None,
    page: int = 0,
) -> None:
    """Рендерит /markets для указанной секции с пагинацией.

    Раньше /markets с 15 активами рассыпался на 3 отдельных Telegram-сообщения
    («3 портянки в чат»). Теперь — одно сообщение которое юзер листает кнопками
    «◀ Назад / Вперёд ▶» (edit_message_text in-place, как книжка).

    Если задан ``wait_message_id`` — `edit_message_text` поверх него (так
    заменяем «⏳ Загружаю…» или предыдущий экран секции/страницы). Иначе —
    `send_message`. Клавиатура и status_text цепляются к этому одному
    сообщению; pagination row автоматически добавляется при total_pages > 1.
    """
    github_repo = os.getenv("GITHUB_REPO", "ANAEHY/dialectic_edge")
    from signals import build_markets_section_message

    messages, _bundle = await build_markets_section_message(github_repo, section=section)
    is_enabled = await get_user_signals_status(user_id)
    status_text = (
        "\n\n✅ _Сигналы вкл — пришлю на сильном сигнале_"
        if is_enabled
        else "\n\n🔔 _Нажми колокольчик — буду слать сильные сигналы_"
    )

    if not messages:
        text = "❌ Нет данных."
        kb = _markets_section_keyboard(is_enabled, current=section, user_id=user_id)
        if wait_message_id is not None:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=wait_message_id, reply_markup=kb
            )
        else:
            await bot.send_message(chat_id, text, reply_markup=kb)
        return

    total_pages = len(messages)
    cur = max(0, min(int(page), total_pages - 1))
    chunk = clean_markdown(messages[cur])
    text = chunk + status_text
    kb = _markets_section_keyboard(
        is_enabled,
        current=section,
        user_id=user_id,
        current_page=cur,
        total_pages=total_pages,
    )
    if wait_message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=wait_message_id,
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception as e:
            # Telegram «message is not modified» если тот же текст: проглатываем.
            if "not modified" not in str(e).lower():
                raise
    else:
        await bot.send_message(
            chat_id,
            text,
            parse_mode="Markdown",
            reply_markup=kb,
        )


@dp.message(Command("markets"))
@require_vip
async def cmd_markets(message: Message):
    user_id = message.from_user.id
    await upsert_user(user_id, message.from_user.username or "")
    wait_msg = await message.answer("⏳ Загружаю рынки...")
    try:
        # Первый клик → крипта с S/R. Сигналы — отдельная вкладка «📡».
        await _render_markets_section(
            chat_id=message.chat.id,
            user_id=user_id,
            section="crypto",
            wait_message_id=wait_msg.message_id,
        )
    except Exception as e:
        await bot.edit_message_text(
            f"❌ Ошибка: {e}",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id,
        )


def _signals_assets_master_list() -> list[str]:
    """Источник правды — список 15 активов из web_search.CRYPTO_KEYS."""
    try:
        from web_search import CRYPTO_KEYS
        return list(CRYPTO_KEYS)
    except Exception:
        # Fallback на случай если web_search упал
        return [
            "BTC", "ETH", "SOL", "BNB", "XRP",
            "ADA", "DOGE", "AVAX", "LINK", "DOT",
            "TRX", "TON", "LTC", "NEAR", "SUI",
        ]


def _signals_assets_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    """Inline-клавиатура: 15 активов в виде «✅ BTC / ⬜ ETH». 3 кнопки в ряду.

    Внизу — управляющие: «Все», «Снять все», «Закрыть».
    Callback: ``sigassets:<ASSET>`` (toggle) / ``sigassets:_all`` / ``sigassets:_none`` /
    ``sigassets:_close``.
    """
    all_assets = _signals_assets_master_list()
    sel = {a.upper() for a in selected}
    rows: list[list[InlineKeyboardButton]] = []
    cur_row: list[InlineKeyboardButton] = []
    for a in all_assets:
        mark = "✅" if a.upper() in sel else "⬜"
        cur_row.append(
            InlineKeyboardButton(text=f"{mark} {a}", callback_data=f"sigassets:{a}")
        )
        if len(cur_row) == 3:
            rows.append(cur_row)
            cur_row = []
    if cur_row:
        rows.append(cur_row)
    rows.append([
        InlineKeyboardButton(text="✅ Все",     callback_data="sigassets:_all"),
        InlineKeyboardButton(text="🗑 Снять все", callback_data="sigassets:_none"),
    ])
    rows.append([
        InlineKeyboardButton(text="✖ Закрыть", callback_data="sigassets:_close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _signals_picker_text(selected: list[str], is_subscribed: bool) -> str:
    all_assets = _signals_assets_master_list()
    is_all = set(a.upper() for a in selected) == set(a.upper() for a in all_assets)
    sub_line = "🟢 Подписка ВКЛ" if is_subscribed else "⚪ Подписка ВЫКЛ"
    if is_all:
        sel_line = f"_Отслеживаешь все {len(all_assets)} активов_"
    elif not selected:
        sel_line = "_Ничего не выбрано — сигналы не придут._"
    else:
        sel_line = "_Выбраны:_ " + ", ".join(sorted(set(a.upper() for a in selected)))
    return (
        "📡 *Сигналы — выбор активов*\n"
        f"{sub_line}\n\n"
        "Отмечай активы, по которым хочешь получать:\n"
        "• авто-push «лучшая сделка ≥ 60/100» (из `/signal`)\n"
        "• smart-money convergence + BTC outlook (по умолчанию шлются для BTC)\n\n"
        f"{sel_line}\n\n"
        "_Подписка вкл/выкл — кнопкой «🔔 Сигналы вкл/выкл» в `/markets`._"
    )


@dp.message(Command("signals"))
async def cmd_signals(message: Message):
    """Выбор активов для подписки на сигналы (auto-push best deal ≥60).

    Юзер: «лучше чтобы можно было бы выбирать пользователю самому какую крипту
    отслеживать».
    """
    user_id = message.from_user.id
    await upsert_user(user_id, message.from_user.username or "")
    selected = await get_user_signals_assets(user_id)
    all_assets = _signals_assets_master_list()
    if selected is None:
        selected = list(all_assets)
    is_subscribed = await get_user_signals_status(user_id)
    await message.answer(
        _signals_picker_text(selected, is_subscribed),
        parse_mode="Markdown",
        reply_markup=_signals_assets_keyboard(selected),
    )


@dp.callback_query(F.data.startswith("sigassets:"))
async def handle_signals_assets_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    payload = (callback.data or "").split(":", 1)[1] if callback.data else ""
    all_assets = _signals_assets_master_list()

    if payload == "_close":
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("Закрыто.")
        return
    if payload == "_all":
        await set_user_signals_assets(user_id, None)  # None = все
        new_selected = list(all_assets)
    elif payload == "_none":
        await set_user_signals_assets(user_id, [])
        new_selected = []
    else:
        # toggle одного актива (валидируем что входит в master-список)
        asset = payload.strip().upper()
        if asset not in {a.upper() for a in all_assets}:
            await callback.answer("Неизвестный актив.", show_alert=True)
            return
        new_selected = await toggle_user_signal_asset(
            user_id, asset, all_assets=all_assets
        )

    is_subscribed = await get_user_signals_status(user_id)
    try:
        await callback.message.edit_text(
            _signals_picker_text(new_selected, is_subscribed),
            parse_mode="Markdown",
            reply_markup=_signals_assets_keyboard(new_selected),
        )
    except Exception:
        # Без изменений (одинаковый markup) → молчим
        pass
    await callback.answer()


# ─── /signal — auto SL/TP recommender ─────────────────────────────────────────

def _signal_explain_keyboard(user_id: int, capital: float = 123.0) -> InlineKeyboardMarkup:
    """Кнопки под `/signal`: глоссарий + sniper limit levels.

    Открывает stateless-глоссарий (всё содержится в `_signal_glossary_text`)
    — поэтому не нужен in-memory кэш SignalSetup'а, как `_plan_table_cache`.
    UID в callback_data чтоб чужой не нажал.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📖 Что значат эти слова?",
            callback_data=f"sigexplain:{user_id}",
        ),
    ]])


def _signal_glossary_text() -> str:
    """Человечий перевод всех терминов из `_fmt_signal_message`.

    Формат — Markdown V1.  Все `_` вне backtick-code-span'ов экранированы
    бэкслэшем (`\\_`) — иначе Telegram parser трактует пару `_` как italic
    и при нечётном их количестве падает с «Can't find end of the entity».
    Внутри `\u200b\u200b`...`\u200b\u200b` (code-span) `_` можно оставлять как есть — внутри
    code'а MD V1 ничего не парсит.

    Текст stateless: одинаков для любого setup'а, потому что объясняет
    *слова*, а не *числа*.  Размер < 4096 для одной телеги-message.
    """
    return (
        "*📖 Глоссарий /signal — что значат эти слова*\n"
        "\n"
        "*🎯 АВТО-СИГНАЛ.* Бот посчитал по формулам score 0-100 для каждого "
        "актива.  Это *не* LLM-гадание — чистая математика по ценам.\n"
        "\n"
        "*Порог 60/100.*  Входим только если ≥60 — это «достаточно уверенно "
        "чтобы рисковать живыми деньгами».  Если порог не пройден — пишем "
        "*ЛУЧШИЙ КАНДИДАТ* и явно говорим «повышенный риск».\n"
        "\n"
        "*LONG / SHORT.*\n"
        "• *LONG 📈* — ставим что цена *вырастет*.  Покупаем дёшево, продаём дороже.\n"
        "• *SHORT 📉* — ставим что цена *упадёт*.  Зарабатываем на падении.\n"
        "\n"
        "*R/R 2:1 (Reward/Risk).*  Соотношение «сколько хочу заработать / "
        "сколько готов потерять».  R/R 2:1 = рискуешь $1, целишься в $2.\n"
        "• Математически выгодно если выигрываешь *хотя бы 1 из 3 сделок* "
        "(33% winrate).  У нас исторически 55-62% → плюсовая стратегия.\n"
        "\n"
        "*Entry / Stop / Target.*\n"
        "• *Entry* — цена входа (открытие позиции на рынке).\n"
        "• *Stop (SL)* — цена автоматического закрытия с у��ытком.  Если "
        "рынок дошёл до SL — мы ошиблись, выходим, лосс фиксированный.\n"
        "• *Target (TP)* — цена автоматического закрытия с прибылью.  "
        "Если рынок дошёл до TP — забираем профит.\n"
        "\n"
        "*σ̂ (сигма).*  Стандартное отклонение дневного движения.  Грубо — "
        "«насколько актив обычно колеблется за день».  BTC ≈ 1.5%, XRP ≈ "
        "2.0%, мелочь до 5%.\n"
        "• *Stop 1.5σ̂* — стоп поставлен на 1.5 обычны�� дневных движения "
        "выше шума.  Достаточно близко чтобы лосс был малень��ий, "
        "достаточно далеко чтобы случайная свеча не выбила.\n"
        "• *Target 3.0σ̂* — цель в 2 раза дальше стопа = R/R 2:1.\n"
        "\n"
        "*Size 25%.*  Размер позиции — четверть свободного капитала.  При "
        "$500 → $125 в позицию.  `Risk-per-trade = stop_pct × size` ≈ "
        "1% капитала (классическое Kelly-conservative).\n"
        "\n"
        "*Score breakdown (0-100):*\n"
        "• *Trend* (30 pts) — UPTREND ✓ если выше MA50 и MA200; "
        "DOWNTREND если ниже обеих; SIDEWAYS если в коридоре (тогда "
        "trade-кандидата нет).\n"
        "• *Complexity* (0-20 pts) — `TRENDING` (max 20) если ряд держит "
        "направление; `MEAN_REVERTING` (5) — рынок «дышит» вокруг "
        "средней цены; `RANDOM_WALK` / `CHAOTIC` (0) — высокая "
        "неопределённость.\n"
        "• *VRT (Variance Ratio Test, 0-15 pts).*  Статтест: «правда ли "
        "цена движется направленно?»  *Не отвергает H0* = математически "
        "не отличимо от случайного блуждания → слабый сигнал.\n"
        "• *Markov (0-15 pts).*  Сколько раз исторически дневное "
        "направление сохранялось на следующий день.\n"
        "• *Tradeable (0-20 pts).*  Объём, ликвидность, спред — "
        "торгуется ли вообще нормально.\n"
        "\n"
        "*Слабое место.*  Если score хороший (60+), но один из "
        "компонентов = 0 — выносим в риски.  Пример: high score за "
        "тренд, но `RANDOM_WALK` за complexity = «тренд есть, но шум "
        "доминирует, не входи на full size».\n"
        "\n"
        "*Что НЕ торгуется.*  VIX / GOLD / SPX часто в топе по score, "
        "но это индексы/сырьё — не торгуются на споте Bybit.  Бот "
        "пропускает их, ищет лучший среди 15 крипто-активов "
        "(`BTC/ETH/SOL/BNB/XRP/ADA/DOGE/AVAX/LINK/DOT/TRX/TON/LTC/NEAR/SUI`).\n"
        "\n"
        "*Главное правило.*  *⚠️ Это suggestion, не приказ.*  "
        "Подтверждай вход в Bybit вручную.  Если score < 60 — лучше "
        "пропустить чем входить «потому что хочется».  «Сегодня "
        "сидим» — это нормальный исход, не баг."
    )


def _tradable_assets_inline() -> str:
    """Список tradable-активов в виде `BTC/ETH/SOL/...` для UI-рендера.

    Порядок — по mcap (как в EXTENDED_CRYPTO_SYMBOLS) чтобы BTC был первым.
    Обычно оборачивается в backtick'и в кахка в тексте — MD V1 внутри
    code-span'а не парсит разметку, слеши безопасны.
    """
    from core.signal_scorer import TRADABLE_ASSETS

    try:
        from web_search import CRYPTO_KEYS
    except ImportError:
        CRYPTO_KEYS = tuple(sorted(TRADABLE_ASSETS))
    # Сохраняем порядок из CRYPTO_KEYS, фильтруем по TRADABLE_ASSETS —
    # так UI всегда совпадает с реальным list'ом из scorer'а.
    return "/".join(k for k in CRYPTO_KEYS if k in TRADABLE_ASSETS)


def _md_escape_underscores(s: str) -> str:
    """Экранирует `_` в Telegram Markdown V1.

    Неэкранированный `_` трактуется как italic-разметка — из-за этого
    `MEAN_REVERTING` приходил юзеру как `MEANREVERTING` (пара `_` съела
    всё между ними в italic). Лечится бэкслэшем — в MD V1 `\\_` рендерится
    как литеральный `_`.
    """
    return s.replace("_", r"\_")


def _render_setup_block(
    top,
    scored: list,
    capital: float,
    min_score: int,
    *,
    is_preview: bool,
) -> list[str]:
    """Рендерит блок «Почему / Вход-Stop-Target / Риски» для готового setup'а.

    Используется обеими ветками `_fmt_signal_message`:
      • is_preview=False — score ≥ порога, торгуемая рекомендация.
      • is_preview=True  — лучший tradable кандидат ниже порога; ставим
        предупреждение «повышенный риск» и в «Почему» объясняем чего не
        хватает (нулевые компоненты scoring'а).

    Возвращает список строк — caller клеит их в общее сообщение.
    """
    lines: list[str] = []
    emoji = "📈" if top.direction == "LONG" else "📉"
    stars = "⭐" * min(5, max(1, top.score // 20))

    # Заголовок: в preview ясно маркируем что это не торгуемая рекомендация.
    if is_preview:
        lines.append(
            f"🟡 *ЛУЧШИЙ КАНДИДАТ:* {top.asset} *{top.direction}* {emoji}"
        )
        lines.append(
            f"_(score {top.score}/{min_score} — ниже порога. Preview уровней; "
            f"повышенный риск входа.)_"
        )
    else:
        lines.append(f"🥇 *ТОП SETUP:* {top.asset} *{top.direction}* {emoji}")
    lines.append("")

    # ── Почему именно эта сделка ──
    # Сравниваем с #2: если есть отрыв — подсвечиваем; если top единственный
    # прошёл порог — говорим об этом. Это снимает вопрос «а почему не X?».
    runner_up = next(
        (s for s in scored if s.asset != top.asset and s.direction != "NONE"),
        None,
    )
    # Если над нами в scored есть кандидат с БОЛЬШИМ score, но он
    # отброшен make_setup (не tradable / нет σ̂) — нужно объяснить почему
    # «лучший по очкам» не = «лучший trade».
    higher_non_tradable = next(
        (
            s for s in scored
            if s.total > top.score and s.direction != "NONE" and s.asset != top.asset
        ),
        None,
    )

    lines.append("*Почему эта сделка:*")
    lines.append(
        f"• Score *{top.score}/100* {stars} — "
        f"{'лучший tradable среди' if is_preview else 'лучший среди'} "
        f"{len(scored)} сканированных."
    )
    if higher_non_tradable is not None:
        # VIX/GOLD/SPX и пр. — в топе по score, но не торгуются на споте Bybit.
        # backtick'и вокруг списка tradable-активов — MD V1 внутри
        # code-span'а не парсит разметку, так что слеши и прочее безопасны.
        lines.append(
            f"• Выше по score: {higher_non_tradable.asset} "
            f"{higher_non_tradable.total}/100 — но это индекс/сырьё, не торгуется "
            f"на споте Bybit (торгуем `{_tradable_assets_inline()}`)."
        )
    elif runner_up is not None:
        gap = top.score - runner_up.total
        if gap > 0:
            lines.append(
                f"• Отрыв от #2 ({runner_up.asset} {runner_up.total}/100): +{gap} pts."
            )
        else:
            lines.append(
                f"• #2 — {runner_up.asset} {runner_up.total}/100 (ничья, но "
                f"{top.asset} торгуется на споте Bybit)."
            )
    lines.append(
        f"• R/R = {top.rr_ratio}x: ловим в {top.rr_ratio:.1f} раза больше "
        f"чем рискуем — это «+EV» при winrate ≥ {100/(1+top.rr_ratio):.0f}%."
    )

    # Ключевые «почему» — первые 3 наиболее содержательных reason'a.
    # В preview-режиме это становится диагностикой «чего не хватает».
    # Reasons приходят из scorer'а и могут содержать `MEAN_REVERTING`,
    # `RANDOM_WALK` и т.п. — экранируем `_` чтобы Telegram MD V1
    # не трактовал их как italic-разметку.
    for r in top.reasons[:3]:
        lines.append(f"• {_md_escape_underscores(r)}")
    lines.append("")

    # ── Уровни SL/TP ──
    lines.append("*Вход / Stop / Target:*")
    lines.append("```")
    lines.append(f"Entry:   ${top.entry}   (рыночный)")
    sigma_pct = top.sigma_1d_pct or 1.0  # защита от деления на 0
    lines.append(
        f"Stop:    ${top.stop}   ({top.stop_pct:+.1f}% = "
        f"{top.stop_pct / sigma_pct:+.1f}σ̂)   — если хит, выходим"
    )
    lines.append(
        f"Target:  ${top.target}   ({top.target_pct:+.1f}% = "
        f"{top.target_pct / sigma_pct:+.1f}σ̂)   — фиксируем профит"
    )
    lines.append(f"R/R:     {top.rr_ratio}x")
    lines.append(
        f"Size:    ${top.size_usd}   ({top.size_usd / capital * 100:.0f}% от капитала)"
    )
    lines.append("```")

    # ── Риски этой сделки ──
    sl_loss_usd = top.size_usd * abs(top.stop_pct) / 100.0
    tp_gain_usd = top.size_usd * abs(top.target_pct) / 100.0
    sl_loss_pct = sl_loss_usd / capital * 100 if capital > 0 else 0.0
    tp_gain_pct = tp_gain_usd / capital * 100 if capital > 0 else 0.0
    lines.append("*Риски этой сделки:*")
    lines.append(
        f"• Если SL hit → потеря ≈ ${sl_loss_usd:.2f} "
        f"({sl_loss_pct:.1f}% от капитала)."
    )
    lines.append(
        f"• Если TP hit → прибыль ≈ ${tp_gain_usd:.2f} "
        f"({tp_gain_pct:.1f}% от капитала)."
    )
    lines.append(
        f"• Дневная σ̂ ≈ {top.sigma_1d_pct:.2f}%/день — "
        f"стоп даёт {abs(top.stop_pct / sigma_pct):.1f}σ запаса от обычного шума."
    )
    # Слабое место: если какой-то reason явно «нулевой» — выносим в риски.
    weak_marker = (
        " 0 pts", "не отвергает", "против trade", "нет edge", "trade-кандидата нет",
    )
    weak_reasons = [r for r in top.reasons if any(m in r for m in weak_marker)]
    if weak_reasons:
        lines.append(
            f"• Слабое место: {_md_escape_underscores(weak_reasons[0])}"
        )
    if is_preview:
        # В preview-режиме явно говорим «не торгуй» — это не торгуемая рекомендация.
        lines.append(
            f"• Score {top.score}/{min_score} — ниже порога. Если входишь "
            f"всё равно, уменьши size минимум вдвое."
        )
    lines.append("")

    lines.append("⚠️ _Это suggestion, не приказ. Подтверди вход в Bybit вручную._")
    lines.append("⚠️ _SL — рыночный. Округлено до tick биржи (XRP=0.0001, BTC=0.01 и т.д.)._")
    return lines


def _render_extra_setup_compact(
    setup,
    rank: int,
    capital: float,
    sl_pct_sigma: float | None = None,
) -> list[str]:
    """Компактный рендер дополнительного tradable setup'а (одна сделка из топа).

    Используется когда `tradable_setups` содержит больше одной сделки —
    лидер идёт полным блоком `_render_setup_block`, остальные сюда
    компактным «3-строчным» форматом, иначе Telegram 4096 limit утонет.

    Формат::

        🥈 #2 SOL LONG 📈 — score 68/100 (R/R 2.0x)
           Entry $215.40 | SL $208.50 (-3.2%) | TP $229.20 (+6.4%)
           Размер ~$30 | Топ-причина: UPTREND ✓ (vs MA50 +5.3%, MA200 +13.2%)
    """
    if rank <= 1:
        medal = "🥇"
    elif rank == 2:
        medal = "🥈"
    elif rank == 3:
        medal = "🥉"
    else:
        medal = "🔸"
    arrow = "📈" if setup.direction == "LONG" else "📉"
    top_reason = setup.reasons[0] if setup.reasons else "—"
    cap_pct = (setup.size_usd / capital * 100) if capital > 0 else 0.0
    return [
        f"{medal} *#{rank}* *{setup.asset}* *{setup.direction}* {arrow} — "
        f"score *{setup.score}/100* (R/R {setup.rr_ratio}x)",
        f"   Entry `${setup.entry}` | SL `${setup.stop}` ({setup.stop_pct:+.1f}%) | "
        f"TP `${setup.target}` ({setup.target_pct:+.1f}%)",
        f"   Размер ~${setup.size_usd} ({cap_pct:.0f}%) | "
        f"Топ-причина: {_md_escape_underscores(top_reason)}",
    ]


def _fmt_signal_message(result: dict) -> str:
    """Рендерит результат `rank_signals(...)` в Telegram-сообщение.

    Format:
      • Если есть ≥1 tradable setup → ТОП setup полным блоком + список
        ВСЕХ остальных tradable setups компактно (по запросу юзера:
        «показывал ВСЕ лучшие сделки, а не только 1-2»).
      • Если top == None И preview_top != None → preview-блок: те же
        уровни но с пометкой «🟡 ниже порога — повышенный риск».
      • Иначе → «сегодня сидим» + top-3 кандидатов по score
        (нет tradable кандидата вообще — все SIDEWAYS или non-TRADABLE).

    Это даёт пользователю либо полную картину торгуемых сделок (с
    компактным форматом для №2-N), либо preview лучшего варианта с
    уровнями, либо честный «сегодня нечего».
    """
    from core.signal_scorer import SignalSetup

    capital = result.get("capital", 123.0)
    min_score = result.get("min_score", 60)
    scored = result.get("scored") or []
    top = result.get("top")
    preview_top = result.get("preview_top")
    tradable_setups = result.get("tradable_setups") or []

    lines: list[str] = []
    lines.append("🎯 *АВТО-СИГНАЛ* (детерминированный scoring)")
    lines.append("")
    n_tradable = len(tradable_setups)
    if n_tradable >= 1:
        lines.append(
            f"Скан: *{len(scored)}* актив(ов) | Порог: *{min_score}/100* | "
            f"Tradable: *{n_tradable}*"
        )
    else:
        lines.append(f"Скан: *{len(scored)}* актив(ов) | Порог: *{min_score}/100*")
    lines.append("")

    if isinstance(top, SignalSetup):
        # ── Полноценный setup найден (score ≥ порога) ──
        lines.extend(
            _render_setup_block(
                top, scored, capital, min_score, is_preview=False,
            )
        )
        # Если есть другие tradable setup'ы — рендерим их компактно.
        # Telegram 4096 char limit: топ-блок ~25 строк ≈ 1500 chars,
        # каждый компактный entry ~3 строки ≈ 200 chars → запас на 12 entries
        # хватает с лихвой. На расширенной 15-крипто корзине в сильный тренд
        # реалистично 3-7 tradable одновременно.
        if n_tradable >= 2:
            lines.append("")
            lines.append(
                f"📋 *Ещё {n_tradable - 1} tradable сделок* (компактно, score ↓):"
            )
            lines.append("")
            for idx, extra in enumerate(tradable_setups[1:], start=2):
                lines.extend(_render_extra_setup_compact(extra, idx, capital))
                lines.append("")
            # убираем последнюю пустую строку перед следующим блоком
            if lines and lines[-1] == "":
                lines.pop()
            lines.append("")
            lines.append(
                "_Каждая сделка независимая — можно входить во все сразу "
                "(если хватает капитала) или выбрать одну/несколько._"
            )
    elif isinstance(preview_top, SignalSetup):
        # ── Preview: есть tradable кандидат с σ̂, но score ниже порога ──
        lines.append("⚪ *Чистого setup нет — score ниже порога.* Сидим.")
        lines.append("")
        lines.extend(
            _render_setup_block(
                preview_top, scored, capital, min_score, is_preview=True,
            )
        )
    else:
        # ── Вообще нет tradable кандидата (все SIDEWAYS / не-TRADABLE) ──
        lines.append("⚪ *Сегодня чистого setup нет.* Сидим.")
        lines.append("")
        if scored:
            lines.append("Топ-3 по trade-score (всё ниже порога):")
            for s in scored[:3]:
                top_reason = s.reasons[0] if s.reasons else "—"
                lines.append(
                    f"• *{s.asset}* {s.total}/100 — {_md_escape_underscores(top_reason)}"
                )
            lines.append("")
        lines.append("Запусти `/markets` чтобы посмотреть полную картину.")

    return "\n".join(lines)


@dp.message(Command("signal"))
@require_vip
async def cmd_signal(message: Message):
    """Команда `/signal` (кнопка «🎯 Лучшая сделка») — что РЕАЛЬНО делать сейчас.

    Угадывание направления (LONG/SHORT) убрано — бэктест 2020-26 показал, что
    на дневках это робастно убыточно. Подход системы: спот + следование тренду
    (держим активы выше SMA, в стейбле когда ниже). Без плеча, шортов, деривативов.
    """
    await upsert_user(message.from_user.id, message.from_user.username or "")
    text = (
        "🎯 *ЧТО ДЕЛАТЬ СЕЙЧАС*\n"
        + "═" * 22 + "\n\n"
        "Система не угадывает направление цены (это убыточно на дневках). "
        "Подход — *спот + следование тренду*:\n\n"
        "• держим равным весом активы, что *сейчас выше SMA50* (в восходящем тренде);\n"
        "• ушёл ниже SMA — продаём в стейбл;\n"
        "• ничего в тренде — сидим в стейбле, это нормальный ответ.\n\n"
        "📊 Свежий список активов в тренде — в дайджесте `/daily`.\n"
        "🧪 Аномалии рынка — `/screener`.\n\n"
        "_Без плеча, шортов и деривативов. Не финансовый совет._"
    )
    await message.answer(text, parse_mode="Markdown")


async def _freeze_signal_decision(result: dict) -> None:
    """Замораживает решение rank_signals в decision_provenance.

    Берёт `result["top"]` (или `preview_top` если top=None) и
    соответствующий AssetScore из `scored`, чтобы записать полный
    snapshot input'ов + score breakdown + SL/TP/σ̂. Никогда не падает —
    при любой ошибке логирует и возвращается (UI не должен страдать
    от проблемы с provenance).
    """
    try:
        from dataclasses import asdict

        from core.provenance import freeze_scorer_decision
        from core.signal_scorer import SignalSetup
        from web_search import fetch_realtime_prices  # noqa: F401

        top = result.get("top")
        preview_top = result.get("preview_top")
        setup = top if isinstance(top, SignalSetup) else preview_top
        scored = result.get("scored", [])

        if not isinstance(setup, SignalSetup):
            return  # Нечего замораживать — все SIDEWAYS / нет tradable.

        # Найдём соответствующий AssetScore (для breakdown).
        asset_score = next(
            (s for s in scored if s.asset == setup.asset), None
        )
        if asset_score is None:
            return

        # prices хранится в результате? Нет — поэтому повторно тянем.
        # Это OK: cache в web_search свежий (~10c), стоимость нулевая.
        prices = await fetch_realtime_prices()
        features = prices.get(setup.asset, {}) if isinstance(prices, dict) else {}

        weights = asdict(asset_score.breakdown)
        weights["total"] = setup.score

        direction = setup.direction if top else f"{setup.direction}_preview"

        await freeze_scorer_decision(
            asset=setup.asset,
            direction=direction,
            score=setup.score,
            entry_price=setup.entry,
            stop_loss=setup.stop,
            take_profit=setup.target,
            sigma_1d_pct=setup.sigma_1d_pct,
            features=features,
            weights=weights,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            f"provenance freeze (signal_scorer) skipped: {exc}"
        )


@dp.callback_query(F.data.startswith("sigexplain:"))
async def handle_signal_explain_callback(callback: CallbackQuery):
    """Кнопка «📖 Что значат эти слова?» под `/signal`.

    Отправляет НОВОЕ сообщение с глоссарием (не редактирует исходный
    сигнал — чтоб юзер мог видеть и сетап, и пояснение рядом).
    UID проверяется чтоб чужой клик в групповом чате не выдавал ответ.
    """
    parts = (callback.data or "").split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    try:
        kb_uid = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    if kb_uid != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return
    await callback.answer()
    await bot.send_message(
        callback.message.chat.id,
        _signal_glossary_text(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


def _markets_glossary_text() -> str:
    """Глоссарий /markets — что значат строки в выводе.

    Покрывает терминологию ВСЕХ блоков на актив:
      • MA-триггеры (▲/▼ выше/ниже MA200/MA50)
      • S/R-уровни (🎯 R: / S:) — сопротивление/поддержка
      • SL/TP-план (🎯 LONG/SHORT TP/SL)
      • Quant-вердикт (LONG/SHORT/NEUTRAL по BB+Donchian+RSI ансамблю)
      • Тренд (UPTREND/DOWNTREND/SIDEWAYS) и confluence MA50/MA200
      • Hurst (H), Permutation Entropy (PE), score, VR — стат-тесты
      • σ̂ — EWMA-прогноз волатильности (RiskMetrics)
      • Markov-режим — статистика дискретизованных состояний

    Формат MD V1. Все `_` вне backtick-code-span'ов экранированы.
    Длина < 4096 chars (одна Telegram-message).
    """
    return (
        "*📖 Глоссарий /markets — что значат эти слова*\n"
        "\n"
        "*🟢/🔴 24ч / 7д / 30д.*  Цвет = знак изменения цены. Зелёный — вырос, "
        "красный — упал. Числа в процентах от цены на начало периода.\n"
        "\n"
        "*▲ / ▼ MA-триггеры.*  Скользящие средние MA50 (последние 50 дней) и "
        "MA200 (200 дней) — базовые уровни тренда.\n"
        "• `▲ выше $X (MA200) → LONG` — если закрытие выше MA200, тренд "
        "восходящий, идея на покупку.\n"
        "• `▼ ниже $Y (MA50) → SHORT` — если закрытие ниже MA50, тренд "
        "падающий, идея на продажу.\n"
        "\n"
        "*🎯 R / S — Сопротивление и Поддержка.*  Горизонтальные уровни "
        "цены где рынок исторически разворачивался.\n"
        "• *R₁/R₂ (Resistance)* — уровни *выше* цены. Цена «упирается», "
        "продавцы активны. Часто = разворот вниз или хорошее место для TP.\n"
        "• *S₁/S₂ (Support)* — уровни *ниже* цены. Цена «отбивается», "
        "покупатели активны. Часто = разворот вверх или хорошее место для SL.\n"
        "• Метка `MA200` / `MA50` означает что уровень совпадает со "
        "скользящей средней (*confluence* — двойное подтверждение).\n"
        "• Метка `свинг-Nд` — последнее касание этого уровня было N дней "
        "назад. Чем меньше N, тем «свежее» уровень.\n"
        "• `+X.X% / −X.X%` — расстояние от текущей цены до уровня.\n"
        "\n"
        "*🎯 LONG / SHORT — план сделки.*  Готовая идея с расчёт��м риска.\n"
        "• `TP` (Target Profit) — цель прибыли. Закрытие позиции в плюс.\n"
        "• `SL` (Stop Loss) — цена закрытия в убыток если идея не сработала.\n"
        "• `R/R 2:1` — Reward/Risk: рискуем $1, цель $2. Безубыточно при "
        "winrate ≥ 33%.\n"
        "• SL/TP рассчитаны от σ̂ (см. ниже), а не фиксированный % — "
        "узкие стопы для спокойных активов, широкие для волатильных.\n"
        "\n"
        "*🟢/🔴/⚪️ Quant.*  Ансамбль из 3 фильтров (Bollinger Bands + Donchian + "
        "RSI) с BTC-regime-gate. На бэктесте 65.9% hit-rate vs 49.6% у "
        "простых MA-сигналов.\n"
        "• `LONG (70%)` — 7 из 10 dimensions говорят «покупай».\n"
        "• `NEUTRAL (0%)` — нет конфлюэнции, сидим вне рынка.\n"
        "\n"
        "*📈/📉/↔️ ТРЕНД.*  UPTREND / DOWNTREND / SIDEWAYS — итоговая метка по "
        "HH/HL count + MA + изменению цены за 7д.\n"
        "\n"
        "*🔄 MEAN-REVERTING / 📈 TRENDING / 🪙 RANDOM WALK.*  Стр��ктура ряда:\n"
        "• *TRENDING* — цена держит направление, тренд-стратегии работают.\n"
        "• *MEAN-REVERTING* — рынок «дышит» вокруг средней, скальп возле S/R.\n"
        "• *RANDOM WALK* — направление непредсказуемо, как монетка. Не "
        "торгуем.\n"
        "\n"
        "*H — Hurst Exponent.*  Мера «памяти» ряда [0..1]:\n"
        "• `H > 0.55` — трендовый (память есть, инерция).\n"
        "• `H ≈ 0.50` — random walk (нет памяти).\n"
        "• `H < 0.45` — mean-reverting (антипамять, откаты).\n"
        "\n"
        "*PE — Permutation Entropy.*  Энтропия порядка соседних значений "
        "[0..1]. `PE ≈ 1.0` = шум; `PE < 0.85` = есть структура.\n"
        "\n"
        "*score.*  Композитный рейтинг 0..1 — насколько актив торгуется. "
        "0 = шум; 1 = идеальный сетап. Порог 0.6 в нашей системе.\n"
        "\n"
        "*VR (Variance Ratio Test).*  Стат-тест Lo-MacKinlay: «правда ли "
        "цена движется направленно?»\n"
        "• `H0 не отвергнут` = математически не отличимо от случайного "
        "блуждания → не торгуем.\n"
        "• `H0 отвергнут` = есть статистически значимая структура → "
        "сигнал валиден.\n"
        "\n"
        "*σ̂ (сигма-форкаст).*  Прогноз дневной волатильности по EWMA с "
        "λ=0.94 (модель RiskMetrics). Грубо — «насколько актив "
        "обычно колеблется за день». BTC ≈ 1.5%, XRP ≈ 2.0%. Цифра "
        "`год.XX%` — annualized (σ̂ × √252).\n"
        "\n"
        "*🎲 Markov state.*  Дискретизуем дневные returns в 3 состояния "
        "(DOWN / FLAT / UP) и считаем матрицу переходов:\n"
        "• Текущее состояние + dwell-time (сколько баров в нём сидим).\n"
        "• `UP 35% / FLAT 22% / DOWN 43%` — вероятности следующего бара.\n"
        "• Если P(желаемое направление) > 50% — Markov даёт +pts в score.\n"
        "\n"
        "*Объём 24ч.*  Сумма USD-объёма сделок за последние 24 часа. Высокий "
        "объём = ликвидно, спред маленький. Низкий = осторожно (slippage).\n"
        "\n"
        "*Главное правило.*  Все цифры — *математика*, не приказ. "
        "Используй для рамки риска, не как «гарантированный сигнал».\n"
    )


@dp.callback_query(F.data.startswith("mktexplain:"))
async def handle_markets_explain_callback(callback: CallbackQuery):
    """Кнопка «📖 Что ��начат эти слова?» под /markets.

    Отправляет НОВОЕ сообщение с глоссарием — не редактирует исходный
    /markets-выкат (чтобы юзер мог видеть и данные, и пояснение рядом).
    UID проверяется чтоб чужой клик в групповом чате не выдавал ответ.
    Тот же паттерн что и `handle_signal_explain_callback` для /signal.
    """
    parts = (callback.data or "").split(":")
    if len(parts) != 2:
        await callback.answer()
        return
    try:
        kb_uid = int(parts[1])
    except ValueError:
        await callback.answer()
        return
    if kb_uid != callback.from_user.id:
        await callback.answer("Кнопка не с твоего аккаунта", show_alert=True)
        return
    await callback.answer()
    await bot.send_message(
        callback.message.chat.id,
        _markets_glossary_text(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    await upsert_user(message.from_user.id)
    wait_msg = await message.answer("⏳ Загружаю...")
    try:
        prices, _ = await get_full_realtime_context()
        cbr_data = await fetch_cbr_data()

        now = datetime.now().strftime("%d.%m %H:%M UTC")

        lines = [
            f"📊 СТАТУС РЫНКОВ",
            f"_{now}_",
            ""
        ]

        # Крипта
        lines.append("💰 КРИПТА")
        for k, label, icon in [
            ("BTC", "Bitcoin", "₿"),
            ("ETH", "Ethereum", "Ξ"),
        ]:
            if k in prices:
                p = prices[k]
                price = p.get("price", 0)
                change = p.get("change_24h", 0)
                emoji = "🟢" if change >= 0 else "🔴"
                lines.append(f"{icon} {label}: ${price:,.0f} {emoji}{change:+.1f}%")

        # Валюты
        if cbr_data:
            lines.append("")
            lines.append("💵 ВАЛЮТЫ (ЦБ РФ)")
            for line in cbr_data.strip().split('\n')[:3]:
                if line.strip():
                    lines.append(line)

        # Фондовые
        lines.append("")
        lines.append("📈 ИНДЕКСЫ")
        for k, label in [("SPX", "S&P"), ("NDX", "Nasdaq"), ("VIX", "VIX")]:
            if k in prices:
                p = prices[k]
                price = p.get("price", 0)
                change = p.get("change_24h", 0)
                emoji = "🟢" if change >= 0 else "🔴"
                lines.append(f"{label}: {price:,.0f} {emoji}{change:+.1f}%")

        # Макро
        if "MACRO" in prices:
            m = prices["MACRO"]
            fng = m.get("fng", {})
            fv = fng.get("val", "N/A")
            fs = fng.get("status", "")
            lines.append("")
            lines.append(f"F&Greed: {fv}/100 ({fs})")

        lines.append("")
        lines.append("⚠️ Не финансовый совет")

        await bot.edit_message_text(
            "\n".join(lines),
            chat_id=message.chat.id,
            message_id=wait_msg.message_id,
            parse_mode="Markdown"
        )
    except Exception as e:
        await bot.edit_message_text(
            f"❌ Ошибка: {e}",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id
        )


@dp.callback_query(F.data.startswith(("markets:", "signals:")))
async def cb_markets_signals(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data or ""
    if data.startswith("markets:"):
        action = data.split(":")[1] if ":" in data else ""
    elif data.startswith("signals:"):
        action = data.split(":")[1] if ":" in data else ""
    else:
        action = ""

    # markets:section:<key> — выбор секции (сбрасывает page=0).
    if data.startswith("markets:section:"):
        section = data.split(":", 2)[2] if data.count(":") >= 2 else "summary"
        await callback.answer("⏳")
        try:
            await _render_markets_section(
                chat_id=callback.message.chat.id,
                user_id=user_id,
                section=section,
                wait_message_id=callback.message.message_id,
                page=0,
            )
        except Exception as e:
            await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    # markets:page:<section>:<idx> — листание страниц внутри секции.
    # Юзер кликает «◀ Назад / Вперёд ▶» — редактируем то же сообщение,
    # не плодим новые в чат (см. _render_markets_section пагинацию).
    if data.startswith("markets:page:"):
        parts = data.split(":", 3)
        section = parts[2] if len(parts) >= 3 else "summary"
        try:
            page = int(parts[3]) if len(parts) >= 4 else 0
        except ValueError:
            page = 0
        await callback.answer()
        try:
            await _render_markets_section(
                chat_id=callback.message.chat.id,
                user_id=user_id,
                section=section,
                wait_message_id=callback.message.message_id,
                page=page,
            )
        except Exception as e:
            await callback.answer(f"Ошибка: {e}", show_alert=True)
        return

    # markets:noop — кнопка-индикатор страницы (i/N), без действия.
    if data == "markets:noop":
        await callback.answer()
        return

    if action == "enable":
        await set_signals_sub(user_id, True)
        await callback.answer("✅ Сигналы включены.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=_markets_signal_keyboard(True))

    elif action == "disable":
        await set_signals_sub(user_id, False)
        await callback.answer("🔕 Сигналы выключены.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=_markets_signal_keyboard(False))

    elif action == "check":
        await callback.answer("📡 Обновляю...")
        github_repo = os.getenv("GITHUB_REPO", "ANAEHY/dialectic_edge")
        try:
            from signals import build_markets_panel_message

            messages, _bundle = await build_markets_panel_message(github_repo)
            is_enabled = await get_user_signals_status(user_id)
            status_text = (
                "\n\n✅ *Сигналы включены* — при сильном сигнале пришлю отдельным сообщением"
                if is_enabled
                else (
                    "\n\n━━━━━━━━━━━━━━━━━━━━━\n"
                    "Нажми «Включить сигналы» — бот будет присылать при перекосе трейдеров "
                    "или ��овпадении с вердиктом из DIGEST_CACHE"
                )
            )
            if not messages:
                await callback.answer("Нет данных.", show_alert=True)
                return
            # Тот же раскат что и в cmd_markets — edit первое, остальные
            # отдельными send_message. Кнопка цепляется к последнему.
            await callback.message.edit_text(
                clean_markdown(messages[0]),
                parse_mode="Markdown",
            )
            for i, chunk in enumerate(messages[1:], start=1):
                is_last = (i == len(messages) - 1)
                body = clean_markdown(chunk) + (status_text if is_last else "")
                await bot.send_message(
                    callback.message.chat.id,
                    body,
                    parse_mode="Markdown",
                    reply_markup=_markets_signal_keyboard(is_enabled) if is_last else None,
                )
        except Exception as e:
            await callback.answer(f"Ошибка: {e}", show_alert=True)

    elif action == "backtest":
        signals_data = await get_backtest_signals()
        stats = await get_backtest_stats()

        total = stats.get("total", 0) or 0
        wins = stats.get("wins", 0) or 0
        total_pnl = stats.get("total_pnl", 0) or 0
        avg_pnl = stats.get("avg_pnl_pct", 0) or 0
        win_rate = (wins / total * 100) if total > 0 else 0

        msg = "📊 *БЭКТЕСТ РЕЗУЛЬТАТЫ*\n\n"
        msg += f"Всего сделок: {total}\n"
        msg += f"Win Rate: {win_rate:.1f}%\n"
        msg += f"Total PnL: ${total_pnl:+,.2f}\n"
        msg += f"Avg PnL: {avg_pnl:+.2f}%\n\n"
        msg += "Последние сделки:\n"

        for s in signals_data[:5]:
            symbol = s["symbol"]
            direction = s["direction"]
            pnl = s.get("pnl", 0) or 0
            emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            msg += f"{symbol} {direction} {emoji} ${pnl:+,.0f}\n"

        is_enabled = await get_user_signals_status(user_id)
        await callback.message.edit_text(
            msg,
            parse_mode="Markdown",
            reply_markup=_markets_signal_keyboard(is_enabled),
        )
    else:
        await callback.answer()


# ─── /trackrecord ─────────────────────────────────────────────────────────────

@dp.message(Command("market"))
async def cmd_market(message: Message):
    user_id = message.from_user.id
    await upsert_user(user_id, message.from_user.username or "")
    if not await check_limit(user_id):
        await message.answer(
            f"в›” *Р›РёРјРёС‚* вЂ” {FREE_DAILY_LIMIT} Р·Р°РїСЂРѕСЃРѕРІ/РґРµРЅСЊ (free)",
            parse_mode="Markdown"
        )
        return
    await increment_requests(user_id)
    await handle_market_command(message, message.text or "/market")


@require_vip
async def _cmd_trackrecord(message: Message, report_type: str = None, title: str = "АГЕНТОВ", filter_type: str = "all"):
    await upsert_user(message.from_user.id)
    try:
        import aiohttp
        import re

        content = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://raw.githubusercontent.com/ANAEHY/dialectic_edge/main/FORECASTS.md",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        content = await resp.text()
        except Exception as e:
            logger.warning(f"Failed to fetch FORECASTS.md: {e}")

        if not content:
            await message.answer("📊 Не удалось загрузить FORECASTS.md")
            return

        russia_keywords = ["руб", "рф", "россия", "сбер", "газпром", "лукойл", "роснефть", "мосбирж", "рбк", "офз", "usd/rub", "нефть"]

        last_update_match = re.search(r'Последнее обновление:\s*(\d{2}\.\d{2}\.\d{4})', content)
        last_update = last_update_match.group(1) if last_update_match else "—"

        total = 0
        wins = 0
        cautions = 0
        losses = 0
        winrate = 0
        winrate_conservative = 0
        protection = 0
        period = ""

        total_match = re.search(r'Всего прогнозов.*?\|.*?(\d+)', content)
        if total_match:
            total = int(total_match.group(1))

        wins_match = re.search(r'✅ Верно.*?\|.*?(\d+)', content)
        if wins_match:
            wins = int(wins_match.group(1))

        cautions_match = re.search(r'⚠️ Правильная осторожность.*?\|.*?(\d+)', content)
        if cautions_match:
            cautions = int(cautions_match.group(1))

        losses_match = re.search(r'❌ Неверно.*?\|.*?(\d+)', content)
        if losses_match:
            losses = int(losses_match.group(1))

        winrate_match = re.search(r'Точность \(с осторожностью\).*?\*\*(\d+\.?\d*)%', content)
        if winrate_match:
            winrate = float(winrate_match.group(1))

        winrate_conservative_match = re.search(r'Точность \(только направление\).*?\*\*(\d+\.?\d*)%', content)
        if winrate_conservative_match:
            winrate_conservative = float(winrate_conservative_match.group(1))

        protection_match = re.search(r'Защита капитала.*?\*\*(\d+\.?\d*)%', content)
        if protection_match:
            protection = float(protection_match.group(1))

        period_match = re.search(r'Период.*?(\d{2}\.\d{2}\.\d{4}.*\d{2}\.\d{2}\.\d{4})', content)
        if period_match:
            period = period_match.group(1)

        categories = []
        in_categories = False
        for line in content.split('\n'):
            if '## 📋 Точность по категориям' in line:
                in_categories = True
                continue
            if in_categories and line.strip().startswith('|') and '---' not in line and 'Категория' not in line:
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 3:
                    categories.append({"name": parts[0], "stats": parts[1], "accuracy": parts[2]})
            elif in_categories and (line.strip().startswith('##') or line.strip() == ''):
                if len(categories) > 0:
                    break

        predictions = []

        russia_keywords = ["руб", "рф", "россия", "сбер", "газпром", "лукойл", "роснефть", "мосбирж", "офз", "нефть", "росси"]

        in_forecasts = False
        for line in content.split('\n'):
            if '## 📝 Все прогнозы' in line:
                in_forecasts = True
                continue
            if in_forecasts and line.strip().startswith('|') and '---' not in line:
                if '№' in line or 'Дата' in line:
                    continue
                parts = [p.strip() for p in line.split('|')[1:-1]]
                if len(parts) >= 7:
                    try:
                        date = parts[1] if len(parts) > 1 else ""
                        pred_type = parts[2] if len(parts) > 2 else ""
                        asset = parts[3] if len(parts) > 3 else ""
                        forecast = parts[4] if len(parts) > 4 else ""
                        fact = parts[5] if len(parts) > 5 else ""
                        result = parts[6] if len(parts) > 6 else ""

                        is_russia = "Russia" in pred_type or any(kw in asset.lower() for kw in russia_keywords)

                        # Фильтрация по типу
                        if report_type == "global" and is_russia:
                            continue
                        if report_type == "russia" and not is_russia:
                            continue

                        predictions.append({
                            "date": date,
                            "type": pred_type,
                            "asset": asset,
                            "forecast": forecast[:30],
                            "fact": fact[:30],
                            "result": result,
                            "is_russia": is_russia
                        })
                    except:
                        pass
            if in_forecasts and line.strip().startswith('##') and 'Все прогнозы' not in line:
                break

        # Парсим статы из таблицы
        total_match = re.search(r'Всего прогнозов.*?(\d+)', content)
        if total_match:
            total = int(total_match.group(1))

        wins_match = re.search(r'Прибыльных.*?(\d+)', content)
        if wins_match:
            wins = int(wins_match.group(1))

        losses_match = re.search(r'Убыточных.*?(\d+)', content)
        if losses_match:
            losses = int(losses_match.group(1))

        # Фильтрация по типу
        if filter_type and filter_type != "all":
            filtered = []
            for p in predictions:
                result = p["result"]
                if filter_type == "win" and ("Верно" in result or "✅" in result):
                    filtered.append(p)
                elif filter_type == "loss" and ("Неверно" in result or "❌" in result):
                    filtered.append(p)
                elif filter_type == "caution" and ("Осторожность" in result or "⚠️" in result):
                    filtered.append(p)
            predictions = filtered

        # Считаем статистику из отфильтрованных прогнозов
        wins = sum(1 for p in predictions if "Верно" in p["result"] or "✅" in p["result"])
        cautions = sum(1 for p in predictions if "Осторожность" in p["result"] or "⚠️" in p["result"])
        losses = sum(1 for p in predictions if "Неверно" in p["result"] or "❌" in p["result"])
        total = wins + cautions + losses

        if total == 0:
            await message.answer(
                "📊 TRACK RECORD\n\nПрогнозов не найдено с таким фильтром.",
                parse_mode="Markdown"
            )
            return

        icon = "🌍" if report_type == "global" else "🇷🇺" if report_type == "russia" else "📊"

        filter_label = ""
        if filter_type and filter_type != "all":
            filter_label = f" [{filter_type.upper()}]"

        def make_bar(value: int, total: int, length: int = 10) -> str:
            if total == 0:
                return "░" * length
            pct = value / total
            filled = int(pct * length)
            return "█" * filled + "░" * (length - filled)

        finished = total
        lines = [
            f"{icon} 📊 DIALECTIC EDGE — TRACK RECORD{filter_label}",
            f"_{period}_" if period else f"_{last_update}_",
            "",
            "═" * 40,
            "🎯 ОБЩАЯ СТАТИСТИКА",
            "═" * 40,
        ]

        if finished > 0:
            win_bar = make_bar(wins, finished)
            loss_bar = make_bar(losses, finished)
            caution_bar = make_bar(cautions, finished)
            lines.extend([
                f"✅ WIN   [{win_bar}] {wins}/{finished} ({wins*100//finished}%)",
                f"⚠️ CAUT  [{caution_bar}] {cautions}/{finished} ({cautions*100//finished}%)",
                f"❌ LOSS  [{loss_bar}] {losses}/{finished} ({losses*100//finished}%)",
            ])

        # Точность только из отфильтрованных
        if finished > 0:
            winrate_calc = wins / finished * 100
            wr_emoji = "🟢" if winrate_calc >= 55 else "🟡" if winrate_calc >= 45 else "🔴"
            lines.append(f"Точность: {wr_emoji} {winrate_calc:.1f}%")

        # Категории показываем только без фильтра
        if categories and (not filter_type or filter_type == "all"):
            lines.append("")
            lines.append("📈 КАТЕГОРИИ")
            for cat in categories[:6]:
                lines.append(f"  {cat['name']}: {cat['accuracy']}")

        lines.append("")
        lines.append("📝 ПРОГНОЗЫ")

        for p in predictions:
            date = p.get("date", "")[:8]
            asset = p.get("asset", "")[:15]
            forecast = p.get("forecast", "")[:30]
            result = p.get("result", "")
            fact = p.get("fact", "")[:30]

            if "Верно" in result:
                res_emoji = "✅"
            elif "Неверно" in result:
                res_emoji = "❌"
            elif "Осторожность" in result:
                res_emoji = "⚠️"
            else:
                res_emoji = "⏳"

            # Для LOSS/CAUTION показываем больше инфы
            if filter_type and filter_type != "all" and fact:
                lines.append(f"{res_emoji} {date} {asset}")
                lines.append(f"   Прогноз: {forecast}")
                lines.append(f"   Факт:    {fact}")
            else:
                lines.append(f"{res_emoji} {date} {asset:<15} {forecast:<30}")

        lines.append("")
        lines.append("⚠️ Прошлые результаты не гарантируют будущих.")

        keyboard_buttons = []

        type_label = {"global": "GLOBAL", "russia": "РОССИЯ", None: "ВСЕ"}.get(report_type, "ВСЕ")

        keyboard_buttons.append([
            InlineKeyboardButton(text="🌍 Global", callback_data=f"tr_type:global"),
            InlineKeyboardButton(text="🇷🇺 Россия", callback_data=f"tr_type:russia"),
            InlineKeyboardButton(text="📊 Все", callback_data=f"tr_type:all"),
        ])

        keyboard_buttons.append([
            InlineKeyboardButton(text="✅ WIN", callback_data=f"tr_filter:win:{type_label}"),
            InlineKeyboardButton(text="❌ LOSS", callback_data=f"tr_filter:loss:{type_label}"),
            InlineKeyboardButton(text="⚠️ CAUTION", callback_data=f"tr_filter:caution:{type_label}"),
            InlineKeyboardButton(text="📋 Все", callback_data=f"tr_filter:all:{type_label}"),
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        full_text = "\n".join(lines)

        if len(full_text) > 4000:
            part1 = "\n".join(lines[:40])
            part2 = "\n".join(lines[40:])
            await message.answer(part1, parse_mode="Markdown")
            await message.answer(part2, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await message.answer(full_text, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Trackrecord error: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


@dp.callback_query(F.data.startswith("tr_type:"))
async def cb_tr_type(callback: CallbackQuery):
    await callback.answer()
    data = callback.data.split(":")
    report_type = data[1] if len(data) > 1 and data[1] != "all" else None
    title = "GLOBAL" if report_type == "global" else "РОССИЯ" if report_type == "russia" else "АГЕНТОВ"
    await _cmd_trackrecord(callback.message, report_type=report_type, title=title)


@dp.callback_query(F.data.startswith("tr_filter:"))
async def cb_tr_filter(callback: CallbackQuery):
    await callback.answer()
    data = callback.data.split(":")
    if len(data) < 3:
        return

    filter_type = data[1]
    type_label = data[2]

    report_type = "global" if type_label == "GLOBAL" else "russia" if type_label == "РОССИЯ" else None

    await _cmd_trackrecord(callback.message, report_type=report_type, title=f"{type_label} ({filter_type.upper()})", filter_type=filter_type)


@dp.message(Command("trackrecord"))
async def cmd_trackrecord(message: Message):
    await _cmd_trackrecord(message, report_type=None, title="АГЕНТОВ (ВСЕ)")


@dp.message(Command("trackrecordglobal"))
async def cmd_trackrecord_global(message: Message):
    await _cmd_trackrecord(message, report_type="global", title="GLOBAL")


@dp.message(Command("trackrecordrussia"))
async def cmd_trackrecord_russia(message: Message):
    await _cmd_trackrecord(message, report_type="russia", title="РОССИЯ EDGE")


# ─── /weeklyreport ────────────────────────────────────────────────────────────

@dp.message(Command("weeklyreport"))
@require_vip
async def cmd_weekly(message: Message):
    await upsert_user(message.from_user.id)
    wait_msg = await message.answer("⏳ Формирую отчёт за неделю...")
    try:
        report = await build_weekly_report()
        await bot.delete_message(chat_id=message.chat.id, message_id=wait_msg.message_id)
        await message.answer(report, parse_mode="Markdown")
    except Exception as e:
        await bot.edit_message_text(
            f"❌ Ошибка: {e}",
            chat_id=message.chat.id,
            message_id=wait_msg.message_id
        )


# ─── /subscribe ───────────────────────────────────────────────────────────────

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    user_id   = message.from_user.id
    await upsert_user(user_id)
    user      = await get_user(user_id)
    is_subbed = user.get("daily_sub", 0) if user else 0
    sub_time  = user.get("sub_time", "08:00") if user else "08:00"
    current_utc = datetime.utcnow().strftime("%H:%M UTC")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌅 06:00 UTC", callback_data="sub_time:06:00"),
            InlineKeyboardButton(text="🌅 08:00 UTC", callback_data="sub_time:08:00"),
        ],
        [
            InlineKeyboardButton(text="☀️ 10:00 UTC", callback_data="sub_time:10:00"),
            InlineKeyboardButton(text="☀️ 12:00 UTC", callback_data="sub_time:12:00"),
        ],
        [
            InlineKeyboardButton(text="💬 Своё время", callback_data="sub_time:custom"),
        ],
        [
            InlineKeyboardButton(text="❌ Отключить", callback_data="sub_time:off"),
        ]
    ])

    if is_subbed:
        status = f"✅ Активна в {sub_time} UTC"
    else:
        status = "❌ Отключена"

    await message.answer(
        f"📬 *Авторассылка*\n"
        f"Статус: {status}\n\n"
        f"⏰ Сейчас: {current_utc}\n\n"
        f"🌍 *Важно:* Бот работает по UTC.\n"
        f"Если тебе нужно 10:00 МСК → выбирай 07:00 UTC\n"
        f"Если нужно 10:00 (Минск/Алматы) → выбирай 07:00-08:00 UTC\n\n"
        f"Выбери время:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("sub_time:"))
async def cb_subscribe(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    data = callback.data.split(":")

    if len(data) < 2:
        return

    action = data[1]

    if action == "off":
        await set_daily_sub(user_id, False)
        await callback.message.edit_text(
            "❌ *Подписка отключена*",
            parse_mode="Markdown"
        )
        return

    if action == "custom":
        await callback.message.edit_text(
            "💬 *Введи время в формате HH:MM*\n\n"
            "Например: `09:30`\n\n"
            "Напоминаю: бот работает по UTC!",
            parse_mode="Markdown"
        )
        return

    time_str = action
    await set_daily_sub(user_id, True, time_str)

    await callback.message.edit_text(
        f"✅ *Подписка активана*\n\n"
        f"📬 Ежедневно в *{time_str} UTC*\n\n"
        f"❌ Отключить: нажми кнопку ниже",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отключить подписку", callback_data="sub_time:off")]
        ])
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_input(message: Message):
    """Handle portfolio input OR time subscription."""
    user_id = message.from_user.id
    text = message.text.strip()
    if await _handle_trade_text(message, user_id, text):
        return
    if await handle_portfolio_input(message):
        return

    # Check portfolio state first
    state = user_portfolio_state.get(user_id)
    if state:
        if state["step"] == "amount":
            try:
                amount = float(text.replace(",", "."))
                assert amount > 0
                state["amount"] = amount
                state["step"] = "price"
                await message.answer(f"По какой цене купил {state['symbol']}?\nВведи цену (например 65000)")
            except:
                await message.answer("Введи число, например 0.5")
            return
        elif state["step"] == "price":
            try:
                price = float(text.replace(",", "."))
                assert price > 0
                symbol = state["symbol"]
                amount = state["amount"]
                await add_portfolio_position(user_id, symbol, amount, price)
                await message.answer(f"✅ Добавлено: {symbol} | {amount} шт. | ${price:,.0f}")
                del user_portfolio_state[user_id]
            except:
                await message.answer("Введи цену, например 65000")
            return

    # Check time input (for subscription)
    user = await get_user(user_id)
    if not user:
        return

    if ":" in text and len(text) == 5:
        try:
            h, m = text.split(":")
            h, m = int(h), int(m)
            assert 0 <= h <= 23 and 0 <= m <= 59
            time_str = f"{h:02d}:{m:02d}"
            await set_daily_sub(user_id, True, time_str)
            await message.answer(f"✅ Подписка активана\n📬 Ежедневно в {time_str} UTC")
            return
        except:
            pass

    # Not portfolio and not time — silently ignore (don't spam "Формат: HH:MM"
    # for persistent keyboard buttons or arbitrary text).
    return


# ─── /stats ───────────────────────────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    await upsert_user(user_id)
    user    = await get_user(user_id)
    profile = await get_profile(user_id)

    if not user:
        await message.answer("Ошибка загрузки.")
        return

    fb           = await get_feedback_stats()
    total_fb     = fb.get("total") or 0
    pos_fb       = fb.get("positive") or 0
    satisfaction = (pos_fb / total_fb * 100) if total_fb > 0 else 0

    risk_name    = RISK_PROFILES.get(profile.get("risk", "moderate"), {}).get("name", "⚖️ Умеренный")
    horizon_name = HORIZONS.get(profile.get("horizon", "swing"), {}).get("name", "📈 Свинг")

    tr      = await get_track_record()
    tr_s    = tr["stats"]
    tr_wins = tr_s.get("wins") or 0
    tr_loss = tr_s.get("losses") or 0
    tr_wr   = (tr_wins / (tr_wins + tr_loss) * 100) if (tr_wins + tr_loss) > 0 else 0

    await message.answer(
        f"📈 *Моя статистика*\n\n"
        f"*Tier:* {'👑 PRO' if user.get('tier')=='pro' else '🆓 Free'}\n"
        f"*Запросов сегодня:* {user.get('requests_today',0)}/{FREE_DAILY_LIMIT}\n"
        f"*Запросов всего:* {user.get('requests_total',0)}\n"
        f"*Профиль:* {risk_name} | {horizon_name}\n"
        f"*Подписка:* {'✅' if user.get('daily_sub') else '❌'}\n\n"
        f"*🎯 Track Record бота:*\n"
        f"Прогнозов: {tr_s.get('total',0)} | Winrate: {tr_wr:.0f}%\n\n"
        f"*Оценки пользователей:*\n"
        f"Оценок: {total_fb} | Позитивных: {satisfaction:.0f}%\n\n"
        f"• /trackrecord — история точности (всё)\n"
        f"• /trackrecordglobal — Global\n"
        f"• /trackrecordrussia — Россия Edge 🇷🇺\n"
        f"• /weeklyreport — отчёт за неделю\n"
        f"• /profile — изменить профиль",
        parse_mode="Markdown"
    )


# ─── /help ────────────────────────────────────────────────────────────────────

def _markets_help_text() -> str:
    """Подробная справка по строкам `/markets`.

    Объясняет каждый блок: цену, MA-триггеры, тренд, complexity-вердикт,
    Markov и σ̂. Это сейчас «секретный» уровень — без шпаргалки юзер видит
    `H=0.42 PE=1.00 score=0.49 VR=0.90 σ̂=1.70%` и не понимает что куда. Этот
    текст возвращается командой `/help markets`.

    Длина < 4000 символов — укладывается в одно Telegram-сообщение
    (лимит 4096). Без расширений по запросу пользователя.
    """
    return (
        "📊 *Гайд по `/markets` — что значат все цифры*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "*1. Цена и изменения*\n"
        "`Bitcoin (BTC): $79,199  🔴 -2.79% (24ч)  🔴 -1.2% (7д)  🟢 +5.9% (30д)  [Binance]`\n"
        "• Текущая цена в долларах\n"
        "• 24ч / 7д / 30д — изменения за период\n"
        "• `[источник]` — откуда цифры (Binance, Yahoo, FRED)\n\n"
        "*2. MA-триггеры LONG / SHORT*\n"
        "`▲ выше $81,957 (MA200) → LONG`\n"
        "`▼ ниже $74,969 (MA50) → SHORT`\n"
        "• Верхний уровень = max(MA50, MA200) — закрытие 4h-свечи выше = "
        "потенциальный LONG-сетап\n"
        "• Нижний уровень = min(MA50, MA200) — закрытие ниже = "
        "потенциальный SHORT-сетап\n"
        "• Это не приказ войти — это уровни, при которых математика дает edge\n\n"
        "*3. SL / TP от текущей цены (только крипта)*\n"
        "`🎯 LONG  TP $82,512 (+4.9%)  SL $77,160 (−2.5%)  R/R 2:1`\n"
        "`🎯 SHORT TP $75,724 (−4.9%)  SL $81,076 (+2.5%)  R/R 2:1`\n"
        "• Готовые стопы / тейки если входишь *прямо сейчас* в обе стороны\n"
        "• Формула: `SL = price·(1 ∓ 1.5·σ̂)`, `TP = price·(1 ± 3·σ̂)`\n"
        "• R/R фиксирован 2:1 — при winrate 33% уже не в минусе\n"
        "• Уровни округлены до tick-size биржи (BTC=0.01, XRP=0.1)\n"
        "• Если `σ̂` нет (короткий ряд) — строка пропадает\n\n"
        "*4. Тренд*\n"
        "`↔️ ТРЕНД: SIDEWAYS | MA50: $74,969 (выше, +5.6%) | MA200: $81,957 (ниже, -3.4%)`\n"
        "• 📈 UPTREND — цена выше обеих MA\n"
        "• 📉 DOWNTREND — цена ниже обеих MA\n"
        "• ↔️ SIDEWAYS — цена между MA (как BTC сейчас)\n\n"
        "*5. Режим рынка (complexity)*\n"
        "`🔄 MEAN-REVERTING  H=0.42  PE=1.00  score=0.49  VR=0.90 (H0 не отвергнут)  σ̂=1.70% (год.32%)`\n"
        "Режимы:\n"
        "• 📈 *TRENDING* — H > 0.55, ходить по тренду\n"
        "• 🔄 *MEAN-REVERTING* — H < 0.45, играть откаты\n"
        "• 🎲 *RANDOM-WALK* — H ≈ 0.5, не торговать направленно\n"
        "• ⚡ *CHAOTIC* — низкая энтропия, шум, не торговать\n\n"
        "Метрики (все опциональны — отсутствуют на коротких рядах):\n"
        "• *H (Hurst)* — степень тренда vs возврат, 0–1. "
        "0.5 = случайный walk\n"
        "• *PE (Permutation Entropy)* — упорядоченность 0-1. "
        "1.0 = max случайность\n"
        "• *score* — итоговая оценка торгуемости 0-1. "
        "*<0.3* = untradeable ⚠️, *>0.6* = чистый edge\n"
        "• *VR (Variance Ratio)* — Lo–MacKinlay random-walk тест. "
        "«H0 отвергнут» = есть структура (тренд или mean-reversion). "
        "«H0 не отвергнут» = ряд похож на случайный\n"
        "• *σ̂ (сигма)* — EWMA forward-volatility (RiskMetrics λ=0.94). "
        "Дневная % и годовая %. Используется для расчёта SL/TP\n\n"
        "*6. Markov 3-state*\n"
        "`🎲 Markov DOWN (~1.8 баров)  UP 35% / FLAT 22% / DOWN 43%`\n"
        "Цепь Маркова на тертилях returns:\n"
        "• *Состояние* — UP / FLAT / DOWN (текущий бар)\n"
        "• *~1.8 баров* — ожидаемое dwell (сколько баров просидит в "
        "текущем состоянии)\n"
        "• *UP X% / FLAT Y% / DOWN Z%* — вероятности перехода в следующий бар\n\n"
        "*7. Объём (только крипта)*\n"
        "`Объём 24ч: $5,500M USD`\n"
        "• Долларовый оборот за 24ч\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "*Как читать всё вместе:*\n"
        "1️⃣ Совпадает ли *тренд* с *режимом*? UPTREND + TRENDING = силён. "
        "UPTREND + MEAN-REVERTING = жди отката\n"
        "2️⃣ *score > 0.6* + VRT *H0 отвергнут* + *Markov не FLAT* = "
        "чистый сигнал\n"
        "3️⃣ *σ̂* задаёт размер стопа: SL ≈ -1.5×σ̂, TP ≈ +3×σ̂\n"
        "4️⃣ Если *score < 0.3* — *не торгуй*. Это указание, не подсказка\n\n"
        "Связанные команды: /daily /help /pitch"
    )


async def _answer_md_safe(message: Message, text: str) -> None:
    """Отдаёт Markdown-сообщение, но если Telegram парсер ругнётся
    («can't parse entities» из-за непарных `*`/`_`/``` ` ```) — шлёт
    plain-text, чтобы юзер всё равно увидел текст а не молчание бота.

    Без этой обёртки баг в одном символе → команда «не работает».
    """
    try:
        await message.answer(text, parse_mode="Markdown")
    except TelegramBadRequest:
        await message.answer(text)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await upsert_user(message.from_user.id)
    # Поддерживаем `/help markets` для подробной справки по строкам /markets.
    # Любой другой аргумент (или его отсутствие) → общий help.
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    sub = parts[1].strip().lower() if len(parts) > 1 else ""
    if sub in ("markets", "/markets", "market", "маркет", "маркетс"):
        await _answer_md_safe(message, _markets_help_text())
        return
    await message.answer(
        "📖 *Dialectic Edge v7.1*\n\n"
        "*Что нового в v6:*\n"
        "• Один отчёт вместо 6 сообщений\n"
        "• Кнопка 📖 Полные дебаты — листай раунды\n"
        "• Простой язык в выводах\n"
        "• Умный Risk/Reward — если риск высокий, бот честно скажет 'ВНЕ РЫНКА'\n\n"
        "*Команды:*\n"
        "• `/profile` — настрой риск-профиль первым\n"
        "• `/daily` — дайджест (из кэша до суток без токенов)\n"
        "• `/daily force` — принудительно новый AI-прогон\n"
        "• `/analyze [текст]` — анализ новости\n"
        "• `/markets` — живой контекст + сигналы, кнопки подписки\n"
        "• `/help markets` — подробный гайд по цифрам в /markets 📊\n"
        "• `/signal [capital]` — auto SL/TP setup на основе нашего scoring 🎯\n"
        "• `/trackrecord` — история точности (всё)\n"
        "• `/trackrecordglobal` — Global\n"
        "• `/trackrecordrussia` — Россия Edge 🇷🇺\n"
        "• `/weeklyreport` — отчёт за неделю\n"
        "• `/subscribe on 08:00` — авторассылка\n"
        "• `/russia` — анализ для российского рынка 🇷🇺\n"
        "• `/stats` — твоя статистика\n"
        "• `/autotrade_status` — performance, win-rate, Kelly, vol-target 🎯\n"
        "• `/audit [N дней]` — AI-аудит закрытых сделок 📊\n"
        "• `/usage` — расход токенов по провайдерам\n"
        "• `/pitch` — investor 1-pager 💎\n\n"
        "⚠️ _Не финансовый совет. Будущее неизвестно никому._",
        parse_mode="Markdown"
    )


# ─── /pitch — investor 1-pager ────────────────────────────────────────────────


def _format_pitch_message() -> str:
    """1-message overview системы для инвестора. Читается за 30 сек.

    Структура: tagline → что делаем → отличия → live KPI → CTA.
    Все KPI собираются из реального state'а (session_manager + risk_manager).
    """
    # KPIs из live state'а
    capital_str = "—"
    pnl_pct_str = "—"
    win_rate_str = "—"
    trades_str = "—"
    kelly_status = "bootstrap"
    sessions_str = "—"
    try:
        from session_manager import session_manager, SESSION_START_CAPITAL
        from signal_trader import _risk_manager

        cur = session_manager.current_session
        if cur:
            capital = cur.current_capital or SESSION_START_CAPITAL
            start_cap = cur.start_capital or SESSION_START_CAPITAL
            pnl_pct = ((capital - start_cap) / start_cap * 100) if start_cap else 0
            capital_str = f"${capital:,.2f}"
            pnl_pct_str = f"{pnl_pct:+.2f}%"
            wins = int(cur.wins or 0)
            losses = int(cur.losses or 0)
            total = wins + losses
            if total:
                win_rate_str = f"{wins / total * 100:.0f}%"
                trades_str = f"{total} ({wins}W / {losses}L)"
            else:
                trades_str = "0 (новая сессия)"

        rs = _risk_manager.get_risk_summary()
        if rs.get("kelly_using_history"):
            kelly_status = f"активен ({rs.get('kelly_pct', 0):.2f}%)"
        else:
            kelly_status = f"bootstrap (база {rs.get('kelly_pct', 2):.2f}%)"

        past = session_manager.past_sessions or []
        sessions_str = f"{len(past) + 1} (текущая)"
    except Exception as e:
        logger.debug("pitch KPI fetch error: %s", e)

    msg = (
        "💎 *Dialectic Edge — investor pitch (30 sec)*\n"
        "═════════════════════════════\n\n"

        "🎯 *Что мы строим*\n"
        "Автономную AI-систему которая торгует крипто-активами на принципах "
        "_систематического фонда_, а не retail-трейдера. Pipeline: "
        "smart-money signals → 4-агентный AI debate → vol-targeted adaptive Kelly "
        "→ self-audit раз в неделю.\n\n"

        "🏆 *Чем отличаемся от 99% retail-ботов*\n"
        "1️⃣ *Smart-money first.* Top-trader L/S, Coinbase Premium, "
        "CME Basis, Funding dispersion — институциональные индикаторы _до_ "
        "retail sentiment. Не Twitter и не Reddit.\n"
        "2️⃣ *Adaptive Kelly + Vol-targeting.* Размер позиции — функция "
        "реализованной волатильности и собственного win-rate, "
        "persisted в `risk_state.json`. Не статичные «2% риска».\n"
        "3️⃣ *AI self-audit.* Раз в неделю LLM пишет performance review "
        "закрытых сделок: что работает, что не работает, правило на "
        "следующую неделю. AI которая учится на своих ошибках.\n"
        "4️⃣ *Multi-provider AI router.* 6 провайдеров, per-role routing, "
        "fallback цепочка. Никогда не падает целиком.\n\n"

        "📊 *Live KPI*\n"
        f"  • Капитал: *{capital_str}*\n"
        f"  • PnL текущей сессии: *{pnl_pct_str}*\n"
        f"  • Win-rate: *{win_rate_str}*\n"
        f"  • Закрытых сделок: *{trades_str}*\n"
        f"  • Kelly engine: *{kelly_status}*\n"
        f"  • Прошедших сессий: *{sessions_str}*\n\n"

        "🚀 *Попробуй сам*\n"
        "  • `/daily` — полный AI-анализ + торговый план\n"
        "  • `/autotrade_status` — performance dashboard\n"
        "  • `/audit` — AI-аудит закрытых сделок\n"
        "  • `/markets` — real-time контекст + сигналы"
    )
    return msg


@dp.message(Command("pitch"))
@require_vip
async def cmd_pitch(message: Message):
    """Investor pitch — 1-message overview системы.

    Питч больше НЕ висит кнопкой в меню — он показывается только из
    приветствия /start («💎 Что я умею»). Сама команда остаётся скрытой
    (не в /setMyCommands) на случай прямого вызова.
    """
    try:
        msg = _format_pitch_message()
        await message.answer(msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.exception("pitch error")
        await message.answer(f"Ошибка: {e}")


# ─── /pump — памп-сканер по запросу ───────────────────────────────────────────

# Сколько памп-сигналов показывать в ответ на ручной /pump (алерты-пуши
# отдельно живут в PumpAlertSystem). Env, чтобы крутить без деплоя.
def _pump_ondemand_limit() -> int:
    try:
        return max(1, int(os.getenv("PUMP_ONDEMAND_LIMIT", "5")))
    except (TypeError, ValueError):
        return 5


PUMP_ONDEMAND_LIMIT = _pump_ondemand_limit()


@dp.message(Command("pump"))
@require_vip
async def cmd_pump(message: Message):
    """🚀 «Что разгоняется» — спот-сканер моментума (лонг-only, без плеча/шортов).

    Раньше тут жил pump-fade (ставка против движения) ��� он удалён. Это новый,
    честный лонг-моментум: показываем монеты, которые уже растут и держатся
    выше короткой средней. Сигнал, не приказ.
    """
    try:
        from pump_scanner import PumpConfig, format_pump_alert, scan_pumps
    except Exception as e:  # noqa: BLE001
        logger.error("pump: import failed: %s", e)
        await message.answer("Памп-сканер временно недоступен.")
        return

    notice = await message.answer("🚀 Сканирую рынок на пампы…")
    try:
        signals = await scan_pumps(cfg=PumpConfig.from_env(), max_symbols=0)
    except Exception as e:  # noqa: BLE001
        logger.error("pump: scan failed: %s", e)
        await message.answer(f"Ошибка памп-сканера: {e}")
        return

    try:
        if notice is not None:
            await notice.delete()
    except Exception:  # noqa: BLE001 — удаление статус-сообщения best-effort
        pass

    if not signals:
        await message.answer(
            "🚀 *Памп-сканер*\n\nСейчас активных пампов не вижу — рынок спокоен.\n"
            "_Авто-алерты придут сами, как только что-то поедет._",
            parse_mode="Markdown",
        )
        return

    top = signals[:PUMP_ONDEMAND_LIMIT]
    await message.answer(
        f"🚀 *Памп-сканер* — нашёл *{len(signals)}*, показываю топ-{len(top)}:",
        parse_mode="Markdown",
    )
    for sig in top:
        try:
            text = format_pump_alert(sig)
            await message.answer(
                text, parse_mode="Markdown", disable_web_page_preview=True,
            )
        except Exception as e:  # noqa: BLE001 — один битый сигнал не рушит ответ
            logger.debug("pump: render failed for %s: %s", getattr(sig, "asset", "?"), e)


@dp.callback_query(F.data == "pumpref")
async def _cb_pump_refresh(cb: CallbackQuery):
    """🔄 Обновить карточку «что разгоняется» (новым сообщением)."""
    try:
        await cb.answer("Обновляю…")
    except Exception:  # noqa: BLE001
        pass
    try:
        text, kb = await _build_pump_card()
        await bot.send_message(
            cb.from_user.id, text, parse_mode="Markdown",
            disable_web_page_preview=True, reply_markup=kb,
        )
    except Exception:  # noqa: BLE001
        try:
            await bot.send_message(cb.from_user.id, "⚠️ Не получилось обновить, попробуй позже.")
        except Exception:  # noqa: BLE001
            pass


# ─── /admin ───────────────────────────────────────────────────────────────────

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    await handle_stats_command(message)


# ─── Управление доступом (только админ) ─────────────────────────────────────────

def _parse_target_user_id(message: Message) -> Optional[int]:
    """Вытащить user_id из аргумента команды: `/ban 12345` → 12345."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].strip())
    except (TypeError, ValueError):
        return None


@dp.message(Command("revoke"))
async def cmd_revoke(message: Message):
    """Снять VIP И обнулить триал у юзера. Использование: /revoke <user_id>.

    Чинит баг «поставил is_vip=false руками, а бот всё равно пускает» — триал
    тикал параллельно. Здесь гасим и VIP, и триал разом. Юзер может потом снова
    оплатить/получить триал — для перманентного бана используй /ban.
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Команда только для админов.")
        return
    uid = _parse_target_user_id(message)
    if uid is None:
        await message.answer("Использование: `/revoke <user_id>`", parse_mode="Markdown")
        return
    from payments.db import revoke_vip
    ok = await revoke_vip(uid)
    await message.answer(
        f"✅ VIP и триал сняты у `{uid}`." if ok
        else f"⚠️ Юзер `{uid}` не найден в базе (или БД выключена).",
        parse_mode="Markdown",
    )


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    """Перманентно заблокировать юзера. Использование: /ban <user_id>.

    Ставит blocked=TRUE — перебивает и VIP, и активный триал. Это надёжный
    рубильник: больше не нужно угадывать, какую колонку править в SQL.
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Команда только для админов.")
        return
    uid = _parse_target_user_id(message)
    if uid is None:
        await message.answer("Использо��ание: `/ban <user_id>`", parse_mode="Markdown")
        return
    if is_admin(uid):
        await message.answer("🛡 Нельзя забанить админа.")
        return
    from payments.db import block_user
    ok = await block_user(uid)
    await message.answer(
        f"🚫 Юзер `{uid}` заб��окирован (VIP и триал перебиты)." if ok
        else f"⚠️ Не удалось заблокировать `{uid}` (БД выключена?).",
        parse_mode="Markdown",
    )


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    """Снять блокировку. Использование: /unban <user_id>.

    VIP/триал НЕ восстанавливаются — юзер возвращается как обычный не-VIP.
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Команда только для админов.")
        return
    uid = _parse_target_user_id(message)
    if uid is None:
        await message.answer("Использование: `/unban <user_id>`", parse_mode="Markdown")
        return
    from payments.db import unblock_user
    ok = await unblock_user(uid)
    await message.answer(
        f"✅ Блокировка снята с `{uid}` (VIP/триал не восстановлены)." if ok
        else f"⚠️ Юзер `{uid}` не найден в базе (или БД выключена).",
        parse_mode="Markdown",
    )


@dp.message(Command("vipinfo"))
async def cmd_vipinfo(message: Message):
    """Показать статус доступа любого юзера. Использование: /vipinfo <user_id>."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Команда только для админов.")
        return
    uid = _parse_target_user_id(message)
    if uid is None:
        await message.answer("Использование: `/vipinfo <user_id>`", parse_mode="Markdown")
        return
    from payments.db import get_vip_info
    info = await get_vip_info(uid)
    if not info.get("pg_enabled"):
        await message.answer("⚙️ База подписок выключена (DATABASE_URL не задан).")
        return

    def _fmt(dt):
        return dt.strftime("%d.%m.%Y %H:%M") if dt else "—"

    blocked = info.get("blocked", False)
    trial_disabled = info.get("trial_disabled", False)
    trial_line = (
        "🎁 Триал: отключён (trial_disabled)" if trial_disabled
        else f"🎁 Триал: {'активен' if info.get('trial_active') else 'нет'} (до {_fmt(info.get('trial_end'))})"
    )
    await message.answer(
        f"👤 *Доступ юзера* `{uid}`\n\n"
        f"{'🚫 *ЗАБЛОКИРОВАН*' if blocked else '✅ Не заблокирован'}\n"
        f"💎 VIP: {'да' if info.get('is_vip') else 'нет'} (до {_fmt(info.get('subscription_end'))})\n"
        f"{trial_line}\n",
        parse_mode="Markdown",
    )


# ─── Фидбек ───────────────────────────────────────────────────────────────────

@dp.message(Command("health"))
async def cmd_health(message: Message):
    await handle_health_command(message)


@dp.message(Command("logs"))
async def cmd_logs(message: Message):
    await handle_logs_command(message)


@dp.message(Command("sysinfo"))
async def cmd_sysinfo(message: Message):
    await handle_sysinfo_command(message)


@dp.message(Command("edge"))
async def cmd_edge(message: Message):
    await handle_edge_command(message)


@dp.callback_query(F.data.startswith("fb:"))
async def handle_feedback(callback: CallbackQuery):
    _, rating_str, report_type = callback.data.split(":")
    await save_feedback(callback.from_user.id, report_type, int(rating_str))
    emoji = "🙏 Спасибо!" if int(rating_str) == 1 else "📝 Учтём!"
    await callback.answer(emoji)
    await callback.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data == "cmd_trackrecordglobal")
async def cb_trackrecord_global(callback: CallbackQuery):
    await callback.answer()
    await _cmd_trackrecord(callback.message, report_type="global", title="GLOBAL")


@dp.callback_query(F.data == "cmd_trackrecordrussia")
async def cb_trackrecord_russia(callback: CallbackQuery):
    await callback.answer()
    await _cmd_trackrecord(callback.message, report_type="russia", title="РОССИЯ EDGE")


@dp.callback_query(F.data == "cmd_trackrecord")
async def cb_trackrecord_all(callback: CallbackQuery):
    await callback.answer()
    await _cmd_trackrecord(callback.message, report_type=None, title="АГЕНТОВ (ВСЕ)")


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Перезапуск бота"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="daily", description="Дайджест рынков"),
        BotCommand(command="trackrecordglobal", description="🌍 Global прогнозы"),
        BotCommand(command="trackrecordrussia", description="🇷🇺 Россия Edge"),
        BotCommand(command="trackrecord", description="📊 Вся статистика"),
        BotCommand(command="markets", description="Рынки + сигналы, подписка"),
        BotCommand(command="p2p", description="🧭 P2P arbitrage scanner"),
        BotCommand(command="status", description="Краткий статус"),
        BotCommand(command="tt", description="🧪 Тест"),
        BotCommand(command="signalstatus", description="🤖 Автоторговля (скоро)"),
        BotCommand(command="screener", description="📡 Сканер аномалий"),
        BotCommand(command="pump", description="🚀 Что разгоняется (спот-моментум)"),
        BotCommand(command="stocks", description="📈 Акции: тренд + моментум"),
        BotCommand(command="trend", description="🧭 Крипто-тренд (спот/лонг)"),
        BotCommand(command="dca", description="💧 План усреднения (DCA)"),
        BotCommand(command="calc", description="🧮 Калькулятор сделок (профит)"),
        BotCommand(command="alerts", description="🔔 Автоалерты смены тренда"),
        BotCommand(command="newbie", description="🆕 Гид для новичков (PDF + правила)"),
        BotCommand(command="instruction", description="📖 Инструкция для чайников"),
        BotCommand(command="russia", description="Анализ РФ 🇷🇺"),
        BotCommand(command="profile", description="Настройки профиля"),
        BotCommand(command="subscribe", description="Авторассылка"),
        BotCommand(command="usage", description="🔢 Расход токенов"),
    ]
    await bot.set_my_commands(commands)


async def run_vip_notifier(bot: Bot) -> None:
    """Background loop: DM users whose VIP was granted by hand in Neon.

    The bot doesn't watch the database live, so when an admin flips ``is_vip``
    to TRUE directly in the Neon table editor there's no event to react to. This
    poller bridges that gap: every ``VIP_NOTIFY_INTERVAL`` seconds it asks the
    DB who is VIP-but-not-yet-told, sends each a one-time "you're VIP now" DM,
    and marks them notified. In-bot purchases pre-mark themselves notified
    (see ``grant_vip``), so only manual grants trigger a message here.

    No-op when ``DATABASE_URL`` is unset. Never raises — a bad poll is logged
    and the loop keeps going.
    """
    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
    from payments.db import (
        _is_enabled,
        pending_vip_notifications,
        mark_vip_notified,
        reset_stale_vip_notifications,
    )

    if not _is_enabled():
        logger.info("🔔 VIP notifier off (DATABASE_URL not set)")
        return

    try:
        interval = max(10, int(os.getenv("VIP_NOTIFY_INTERVAL", "30")))
    except (TypeError, ValueError):
        interval = 30
    logger.info("🔔 VIP notifier on (every %ss)", interval)

    while True:
        try:
            await reset_stale_vip_notifications()
            for item in await pending_vip_notifications():
                uid = item["user_id"]
                sub_end = item.get("subscription_end")
                end_str = sub_end.strftime("%d.%m.%Y") if sub_end else "∞ (бессрочно)"
                try:
                    await bot.send_message(
                        uid,
                        "🎉 *Поздравляем — у тебя теперь VIP!*\n\n"
                        f"💎 Доступ активен до: {end_str}\n"
                        "Открыты все функции бота. Спасибо, что с нами!",
                        parse_mode="Markdown",
                    )
                    await mark_vip_notified(uid)
                    logger.info("VIP notify sent: user=%s", uid)
                except (TelegramForbiddenError, TelegramBadRequest) as e:
                    # User blocked the bot / never started a chat — undeliverable.
                    # Mark notified so we don't retry this forever.
                    logger.info("VIP notify undeliverable user=%s: %s", uid, e)
                    await mark_vip_notified(uid)
                except Exception as e:  # noqa: BLE001 — transient: retry next cycle
                    logger.warning("VIP notify failed user=%s: %s — will retry", uid, e)
        except Exception as e:  # noqa: BLE001 — keep the loop alive no matter what
            logger.warning("VIP notifier cycle failed: %s", e)
        await asyncio.sleep(interval)


async def main():
    global scheduler
    global bot
    bot = get_bot()

    register_funding_handlers(dp)
    register_btc_handlers(dp)
    register_advisor_handlers(dp)
    register_advisor_portfolio_handlers(dp)
    register_p2p_arbitrage_handlers(dp)
    register_postmortem_handlers(dp)
    register_retro_handlers(dp)
    register_subscription_handlers(dp)

    _rate_limiter = RateLimitMiddleware()
    # Register on BOTH messages and inline callbacks so the global flood cap
    # can't be bypassed by mashing inline buttons.
    dp.message.middleware(_rate_limiter)
    dp.callback_query.middleware(_rate_limiter)
    if _rate_limiter.enabled:
        logger.info(
            "⏱ RateLimitMiddleware on: cmd-window=%ds (%s), flood-cap=%d/%ds",
            _rate_limiter.window_sec, ",".join(_rate_limiter.heavy_commands),
            _rate_limiter.max_per_window, _rate_limiter.flood_window_sec,
        )
    else:
        logger.info("⏱ RateLimitMiddleware off (FEATURE_RATE_LIMITER=0)")

    # Bot-wide paywall + free trial (closes every handler, not just @require_vip).
    from refactor.middleware.subscription_guard import SubscriptionMiddleware
    _paywall = SubscriptionMiddleware()
    dp.message.middleware(_paywall)
    dp.callback_query.middleware(_paywall)
    logger.info(
        "🔒 SubscriptionMiddleware %s (trial_days=%s)",
        "ON" if _paywall.enabled else "OFF (FEATURE_PAYWALL=0)",
        os.getenv("TRIAL_DAYS", "3"),
    )

    await set_bot_commands(bot)

    await init_db()
    from core.ai_metrics import init_ai_metrics_db
    await init_ai_metrics_db()
    # PostgreSQL for VIP subscriptions + digest cache (optional, no-op without DATABASE_URL).
    try:
        from payments.db import init_postgres
        pg_ok = await init_postgres()
        if pg_ok:
            logger.info("💳 PostgreSQL ready (VIP subscriptions + digest cache)")
    except Exception as e:
        logger.warning("PostgreSQL init skipped: %s", e)
    await import_forecasts_from_markdown()
    await init_profiles_table()
    setup_admins(ADMIN_IDS)
    logger.info("🚀 Dialectic Edge v7.1 starting...")
    if int(os.getenv("RAILWAY_REPLICA_COUNT", "1") or "1") > 1:
        logger.warning(
            "Railway: у сервиса бота >1 реплики — aiogram polling даёт TelegramConflictError. "
            "Scale → 1 или один процесс с BOT_TOKEN."
        )
    logger.info(
        "Подсказка: TelegramConflictError = второй процесс с тем же BOT_TOKEN "
        "(лишняя реплика Railway / локальный запуск)."
    )
    if USING_DATA_DIR:
        logger.info(
            "Постоянное хранилище: SQLite=%s | cache.json=%s",
            DB_PATH,
            CACHE_FILE,
        )
    if REDIS_URL.strip():
        if await ping_redis():
            logger.info(
                "Redis OK — полные дебаты переживут рестарт (TTL ≈ %s ч.)",
                DEBATE_SNAPSHOT_HOURS,
            )
        else:
            logger.warning(
                "REDIS_URL задан, но соединение не удалось — проверь Redis-плагин и что "
                "переменная подцеплена к сервису бота (Variables → shared / Reference)."
            )
    else:
        logger.warning(
            "REDIS_URL нет — после редеплоя кнопка «Полные дебаты» может быть пустой. "
            "Railway: New → Template → Redis ИЛИ + Database → Redis, затем в сервисе бота "
            "Variables → New Variable → Reference → Redis → REDIS_URL."
        )

    scheduler = Scheduler(
        bot=bot,
        send_daily_fn=deliver_scheduled_daily,
        check_predictions_fn=check_pending_predictions,
        broadcast_daily_fn=broadcast_dialectica_digest,
    )

    # Start signal trader in background
    from signal_trader import run_signal_trader, FEATURE_AUTOTRADE as _AT

    if _AT:
        signal_trader_task = asyncio.create_task(run_signal_trader(bot, ADMIN_IDS))
        logger.info("🤖 Signal trader запущен (FEATURE_AUTOTRADE=1)")
        await asyncio.gather(
            dp.start_polling(bot),
            scheduler.start(),
            signal_trader_task,   # ← теперь в gather — падение будет видно
            run_vip_notifier(bot),  # DM при ручной выдаче VIP в Neon
            run_healthz_server(),  # /healthz на $PORT для Railway restart policy
        )
    else:
        logger.info("⏸ Signal trader выключен (FEATURE_AUTOTRADE=0)")
        await asyncio.gather(
            dp.start_polling(bot),
            scheduler.start(),
            run_vip_notifier(bot),  # DM при ручной выдаче VIP в Neon
            run_healthz_server(),  # /healthz на $PORT для Railway restart policy
        )


# ─── Портфельный трекер ─────────────────────────────────────────────────────────

user_portfolio_state = {}  # user_id: {"symbol": str, "step": str}
user_trade_state = {}  # user_id: {"step": str, "symbol"?: str, "qty"?: float, "trade_id"?: int}


def portfolio_keyboard(has_positions: bool = True) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="portfolio:add_select:")],
    ]
    if has_positions:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data="portfolio:remove_select:")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="portfolio:refresh:")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def select_crypto_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="₿ Bitcoin", callback_data="portfolio:add_amount:BTC")],
        [InlineKeyboardButton(text="Ξ Ethereum", callback_data="portfolio:add_amount:ETH")],
        [InlineKeyboardButton(text="◎ Solana", callback_data="portfolio:add_amount:SOL")],
        [InlineKeyboardButton(text="🥇 Gold", callback_data="portfolio:add_amount:GOLD")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="portfolio:menu:")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_portfolio(event):
    """Show portfolio - works with both Message and CallbackQuery."""
    user_id = event.from_user.id

    positions = await get_portfolio(user_id)
    print(f"DEBUG: user_id={user_id}, positions={positions}")

    prices, _ = await get_full_realtime_context()

    symbol_map = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "GOLD": "GOLD"}

    lines = ["📊 ТВОЙ ПОРТФЕЛЬ", ""]
    total_pnl = 0
    total_value = 0

    for pos in positions:
        symbol = pos["symbol"]
        amount = pos["amount"]
        entry = pos["entry_price"]

        price_key = symbol_map.get(symbol, symbol)
        current_price = prices.get(price_key, {}).get("price", 0)

        if current_price:
            value = amount * current_price
            cost = amount * entry
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost > 0 else 0
            total_pnl += pnl
            total_value += value
            emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{symbol}: {amount} x ${current_price:,.0f} = ${value:,.0f}")
            lines.append(f"  Вход: ${entry:,.0f} | PnL: {emoji}${pnl:+,.0f} ({pnl_pct:+.1f}%)")
        else:
            cost = amount * entry
            total_value += cost
            lines.append(f"{symbol}: {amount} x $??? | Вход: ${entry:,.0f}")

    if not positions:
        lines.append("Портфель пуст")

    if total_value > 0:
        total_cost = total_value - total_pnl if total_pnl > 0 else total_value
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        lines.extend(["", f"📈 Итого: ${total_value:,.0f} | {emoji} {total_pnl:+,.0f} ({total_pnl_pct:+.1f}%)"])

    if hasattr(event, 'message'):
        await event.message.answer("\n".join(lines), reply_markup=portfolio_keyboard(bool(positions)))
    else:
        await event.answer("\n".join(lines), reply_markup=portfolio_keyboard(bool(positions)))


@dp.message(Command("portfolio"))
async def cmd_portfolio(message: Message):
    await upsert_user(message.from_user.id)
    await show_portfolio_view(message)


@dp.callback_query(F.data.startswith("portfolio:"))
async def handle_portfolio_callback(callback: CallbackQuery):
    await handle_portfolio_action(callback)


@dp.message(Command("add"))
async def cmd_add_portfolio(message: Message):
    """Add position to portfolio."""
    user_id = message.from_user.id
    await upsert_user(user_id)
    await add_portfolio_command(message)


@dp.message(Command("remove"))
async def cmd_remove_portfolio(message: Message):
    """Remove position from portfolio."""
    user_id = message.from_user.id
    await upsert_user(user_id)
    await remove_portfolio_command(message)


# ─── 🤝 Тур «веду за руку»: пошаговый онбординг новичка ───
def _tour_step(step: int):
    """Возвращает (текст, клавиатура) для шага тура. step>=99 — финал."""
    if step <= 1:
        text = (
            "🤝 *Веду за руку. Шаг 1 из 4*\n\n"
            "📊 Сначала поймём, что покупать. Жми «🧭 EDGE-план» — покажу монеты "
            "и доли депозита (или скажу пересидеть в стейбле).\n\n"
            "Глянул план — жми «Дальше ➡️»."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧭 Открыть EDGE-план", callback_data="cmd:edgeplan")],
            [InlineKeyboardButton(text="Дальше ➡️", callback_data="tour:2")],
        ])
    elif step == 2:
        text = (
            "🤝 *Шаг 2 из 4*\n\n"
            "🎯 Нужна одна конкретная идея? «Лучшая сделка сейчас» даст один точечный вход.\n\n"
            "Посмотрел — жми «Дальше ➡️»."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Лучшая сделка сейчас", callback_data="cmd:signal")],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="tour:1"),
                InlineKeyboardButton(text="Дальше ➡️", callback_data="tour:3"),
            ],
        ])
    elif step == 3:
        text = (
            "🤝 *Шаг 3 из 4 — самое важное*\n\n"
            "✅ Купил по плану? Сразу запиши покупку в калькулятор — я запомню цену "
            "и потом сам посчитаю профит.\n\n"
            "Жми «➕ Записать покупку» и просто отвечай на вопросы бота "
            "(какая монета → сколько → по какой цене)."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Записать покупку", callback_data="calc:buy")],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="tour:2"),
                InlineKeyboardButton(text="Дальше ➡️", callback_data="tour:4"),
            ],
        ])
    elif step == 4:
        text = (
            "🤝 *Шаг 4 из 4*\n\n"
            "💰 Продал монету? Закрой сделку — посчитаю профит по этой сделке, "
            "а ещё винрейт и ROI по всем сделкам.\n\n"
            "Калькулятор всегда под рукой: /calc или кнопка 🧮 в /start."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➖ Закрыть сделку", callback_data="calc:sell")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="calc:stats")],
            [InlineKeyboardButton(text="✅ Завершить тур", callback_data="tour:done")],
        ])
    else:
        text = (
            "🎉 *Готово, братишка — ты освоился!*\n\n"
            "Твой маршрут на каждый день:\n"
            "1️⃣ /plan — что купить сейчас\n"
            "2️⃣ купил → 🧮 записал покупку в /calc\n"
            "3️⃣ продал → закрыл сделку, смотрю профит\n"
            "4️⃣ остальное сам пришлю в авто-алертах\n\n"
            "Подсказки всегда тут: /start — меню · /calc — калькулятор · /help — всё."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧮 Открыть калькулятор", callback_data="calc:menu")],
        ])
    return text, kb


@dp.message(Command("tour", "guideme"))
async def cmd_tour(message: Message):
    """🤝 Пошаговый тур-онбординг: бот ведёт новичка за руку."""
    await upsert_user(message.from_user.id)
    text, kb = _tour_step(1)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data.startswith("tour:"))
async def handle_tour_callback(callback: CallbackQuery):
    """Навигация по туру. tour:go — старт новым сообщением, остальное — редактирование."""
    data = callback.data or ""
    arg = data.split(":", 1)[1] if ":" in data else ""
    if arg in ("go", "start"):
        text, kb = _tour_step(1)
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
        await callback.answer()
        return
    step = 99 if arg == "done" else (int(arg) if arg.isdigit() else 1)
    text, kb = _tour_step(step)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()


# ─── 🧮 Калькулятор/журнал сделок (покупки/продажи + профит в БД) ───
def _fmt_money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$0.00"


def _signed_money(x) -> str:
    x = float(x or 0)
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def _calc_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Купил", callback_data="calc:buy"),
            InlineKeyboardButton(text="➖ Продал", callback_data="calc:sell"),
        ],
        [
            InlineKeyboardButton(text="📋 История", callback_data="calc:list"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="calc:stats"),
        ],
        [
            InlineKeyboardButton(text="🧭 EDGE-план", callback_data="cmd:edgeplan"),
        ],
    ])


def _render_close_result(t: dict) -> str:
    pa = t.get("profit_abs", 0) or 0
    pp = t.get("profit_pct", 0) or 0
    emoji = "🟢" if pa >= 0 else "🔴"
    word = "Профит" if pa >= 0 else "Убыток"
    return (
        f"{emoji} Закрыл сделку `#{t['id']}` {t['symbol']}\n"
        f"Куплено: {t['qty']:g} @ {_fmt_money(t['entry_price'])}\n"
        f"Продано: @ {_fmt_money(t.get('exit_price'))}\n"
        f"Вложено {_fmt_money(t.get('invested'))} → получено {_fmt_money(t.get('proceeds'))}\n"
        f"*{word}: {_signed_money(pa)} ({pp:+.2f}%)*"
    )


async def _render_trade_list(user_id: int) -> str:
    opens = await get_trades(user_id, status="open", limit=30)
    closed = await get_trades(user_id, status="closed", limit=15)
    lines = ["📋 *История сделок*", ""]
    if opens:
        lines.append("🔵 *Открытые позиции:*")
        for t in opens:
            lines.append(
                f"  `#{t['id']}` {t['symbol']} · {t['qty']:g} @ {_fmt_money(t['entry_price'])} "
                f"· вложено {_fmt_money(t.get('invested'))}"
            )
        lines.append("")
    if closed:
        lines.append("✅ *Закрытые:*")
        for t in closed:
            pa = t.get("profit_abs", 0) or 0
            pp = t.get("profit_pct", 0) or 0
            e = "🟢" if pa >= 0 else "🔴"
            lines.append(f"  {e} `#{t['id']}` {t['symbol']} · {_signed_money(pa)} ({pp:+.1f}%)")
    if not opens and not closed:
        lines.append("Пока пусто. Добавь первую: `/calc buy BTC 0.5 60000`")
    return "\n".join(lines)


async def _render_trade_stats(user_id: int) -> str:
    st = await get_trade_stats(user_id)
    lines = ["📊 *Статистика сделок*", ""]
    lines.append(f"Закрыто сделок: *{st['closed']}*  ·  🟢 {st['wins']} / 🔴 {st['losses']}")
    lines.append(f"Винрейт: *{st['win_rate']:.0f}%*")
    lines.append(f"Вложено всего: {_fmt_money(st['total_invested'])}")
    lines.append(f"Итоговый профит: *{_signed_money(st['total_profit'])}*  ·  ROI *{st['roi']:+.1f}%*")
    if st['closed']:
        lines.append(f"Средняя сделка: {st['avg_pct']:+.1f}%")
        lines.append(f"Лучшая: {st['best_pct']:+.1f}%  ·  Худшая: {st['worst_pct']:+.1f}%")
    lines.append("")
    lines.append(f"Сейчас открыто: {st['open']} (вложено {_fmt_money(st['open_invested'])})")
    return "\n".join(lines)


async def _build_calc_summary(user_id: int) -> str:
    st = await get_trade_stats(user_id)
    opens = await get_trades(user_id, status="open", limit=20)
    lines = [
        "🧮 *Калькулятор сделок*",
        "",
        "Записываю каждую покупку и продажу и сам считаю профит. Спот, только лонг.",
        "",
    ]
    if opens:
        lines.append("🔵 *Открытые позиции:*")
        for t in opens:
            lines.append(
                f"  `#{t['id']}` {t['symbol']} · {t['qty']:g} @ {_fmt_money(t['entry_price'])} "
                f"· вложено {_fmt_money(t.get('invested'))}"
            )
    else:
        lines.append("📭 Открытых позиций пока нет.")
    lines.append("")
    lines.append(
        f"📊 Закрыто: *{st['closed']}* · винрейт *{st['win_rate']:.0f}%* · "
        f"профит *{_signed_money(st['total_profit'])}* (ROI {st['roi']:+.1f}%)"
    )
    lines.append("")
    lines.append("*Как пользоваться:*")
    lines.append("• Кнопки ниже — пошагово, бот сам спросит монету/кол-во/цену.")
    lines.append("• Или командой: `/calc buy BTC 0.5 60000` — купил 0.5 BTC по $60 000.")
    lines.append("• `/calc sell 12 65000` — продал сделку #12 по $65 000 (посчитаю профит).")
    lines.append("• `/calc list` — история · `/calc stats` — статистика · `/calc del 12` — удалить.")
    return "\n".join(lines)


async def show_calc_view(target_message, user_id: int):
    text = await _build_calc_summary(user_id)
    await target_message.answer(text, parse_mode="Markdown", reply_markup=_calc_menu_kb())


@dp.message(Command("calc", "journal", "trade", "calculator"))
async def cmd_calc(message: Message):
    """🧮 Калькулятор/журнал сделок — запись покупок/продаж и подсчёт профита."""
    user_id = message.from_user.id
    await upsert_user(user_id)
    parts = (message.text or "").split()
    args = parts[1:]
    if not args:
        user_trade_state.pop(user_id, None)
        await show_calc_view(message, user_id)
        return

    sub = args[0].lower()

    if sub in ("buy", "b", "купил", "куп", "покупка"):
        if len(args) < 4:
            await message.answer(
                "Формат: `/calc buy BTC 0.5 60000`\n(монета, кол-во, цена покупки)",
                parse_mode="Markdown")
            return
        symbol = args[1].upper().lstrip("$")
        try:
            qty = float(args[2].replace(",", "."))
            price = float(args[3].replace(",", "."))
            assert qty > 0 and price > 0
        except Exception:
            await message.answer("Кол-во и цена — положительные числа. Пример: `/calc buy BTC 0.5 60000`", parse_mode="Markdown")
            return
        note = " ".join(args[4:]) if len(args) > 4 else ""
        tid = await add_trade(user_id, symbol, qty, price, note)
        await message.answer(
            f"✅ ��аписал покупку `#{tid}`: {symbol} — {qty:g} @ {_fmt_money(price)}\n"
            f"Вложено: *{_fmt_money(qty * price)}*\n\n"
            f"Когда продашь: `/calc sell {tid} <цена>`",
            parse_mode="Markdown", reply_markup=_calc_menu_kb())
        return

    if sub in ("sell", "s", "продал", "прод", "продажа"):
        if len(args) < 3:
            await message.answer(
                "Формат: `/calc sell 12 65000`\n(номер сделки из `/calc list` и цена продажи)",
                parse_mode="Markdown")
            return
        target = args[1]
        try:
            price = float(args[2].replace(",", "."))
            assert price > 0
        except Exception:
            await message.answer("Цена продажи — положительное число. Пример: `/calc sell 12 65000`", parse_mode="Markdown")
            return
        if target.isdigit():
            t = await close_trade(user_id, int(target), price)
        else:
            ot = await get_open_trade_by_symbol(user_id, target.upper().lstrip("$"))
            t = await close_trade(user_id, ot["id"], price) if ot else None
        if not t:
            await message.answer("Не нашёл открытую сделку. Глянь номера в `/calc list`.", parse_mode="Markdown")
            return
        await message.answer(_render_close_result(t), parse_mode="Markdown", reply_markup=_calc_menu_kb())
        return

    if sub in ("list", "l", "история", "ист"):
        await message.answer(await _render_trade_list(user_id), parse_mode="Markdown", reply_markup=_calc_menu_kb())
        return

    if sub in ("stats", "stat", "статистика", "стата", "стат"):
        await message.answer(await _render_trade_stats(user_id), parse_mode="Markdown", reply_markup=_calc_menu_kb())
        return

    if sub in ("del", "delete", "rm", "remove", "удалить", "удали"):
        if len(args) < 2 or not args[1].isdigit():
            await message.answer("Формат: `/calc del 12` (номер сделки из `/calc list`)", parse_mode="Markdown")
            return
        await delete_trade(user_id, int(args[1]))
        await message.answer(f"🗑 Удалил запись `#{args[1]}`.", parse_mode="Markdown")
        return

    await show_calc_view(message, user_id)


@dp.callback_query(F.data.startswith("calc:"))
async def handle_calc_callback(callback: CallbackQuery):
    """Кнопки калькулятора сделок (пошаговый ввод через user_trade_state)."""
    user_id = callback.from_user.id
    data = callback.data or ""

    if data == "calc:menu":
        user_trade_state.pop(user_id, None)
        await show_calc_view(callback.message, user_id)
        await callback.answer()
        return

    if data == "calc:buy":
        user_trade_state[user_id] = {"step": "buy_symbol"}
        await callback.message.answer(
            "➕ *Новая покупка*\nКакую монету купил? Напиши тикер, например `BTC`.\n\n(отмена — /calc)",
            parse_mode="Markdown")
        await callback.answer()
        return

    if data == "calc:sell":
        opens = await get_trades(user_id, status="open", limit=20)
        if not opens:
            await callback.answer("Нет открытых позиций для продажи", show_alert=True)
            return
        rows = [[InlineKeyboardButton(
            text=f"#{t['id']} {t['symbol']} {t['qty']:g} @ ${t['entry_price']:,.0f}",
            callback_data=f"calc:sellpick:{t['id']}")] for t in opens]
        await callback.message.answer(
            "➖ Какую позицию закрываешь?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await callback.answer()
        return

    if data.startswith("calc:sellpick:"):
        sp = data.split(":")
        tid = sp[2] if len(sp) > 2 else ""
        opens = await get_trades(user_id, status="open", limit=50)
        t = next((x for x in opens if str(x["id"]) == tid), None)
        if not t:
            await callback.answer("Сделка не найдена", show_alert=True)
            return
        user_trade_state[user_id] = {"step": "sell_price", "trade_id": int(tid)}
        await callback.message.answer(
            f"По какой цене продал {t['symbol']} (`#{tid}`)?\nВведи число, например `65000`.",
            parse_mode="Markdown")
        await callback.answer()
        return

    if data == "calc:list":
        await callback.message.answer(await _render_trade_list(user_id), parse_mode="Markdown")
        await callback.answer()
        return

    if data == "calc:stats":
        await callback.message.answer(await _render_trade_stats(user_id), parse_mode="Markdown")
        await callback.answer()
        return

    await callback.answer()


async def _handle_trade_text(message: Message, user_id: int, text: str) -> bool:
    """Пошаговый ввод калькулятора сделок. True — если сообщение поглощено."""
    state = user_trade_state.get(user_id)
    if not state:
        return False
    step = state.get("step")

    if step == "buy_symbol":
        parts = text.strip().split()
        sym = parts[0].upper().strip("$").replace(",", "") if parts else ""
        if not sym.isalnum() or len(sym) > 12:
            await message.answer("Это не похоже на тикер. Напиши, например, `BTC`.", parse_mode="Markdown")
            return True
        state["symbol"] = sym
        # Пользователь мог сразу написать «BTC 0.5» — подхватим количество,
        # если второй токен это чистое число монет (без $, т.к. $-сумма ≠ qty).
        if len(parts) >= 2 and "$" not in parts[1]:
            try:
                qty = float(parts[1].replace(",", "."))
                assert qty > 0
                state["qty"] = qty
                state["step"] = "buy_price"
                await message.answer(f"По какой цене за 1 {sym}? Например `60000`.", parse_mode="Markdown")
                return True
            except Exception:
                pass
        state["step"] = "buy_qty"
        await message.answer(f"Сколько {sym} купил? Введи количество монет, например `0.5`.", parse_mode="Markdown")
        return True

    if step == "buy_qty":
        try:
            qty = float(text.replace(",", "."))
            assert qty > 0
        except Exception:
            await message.answer("Введи количество числом, например `0.5`.", parse_mode="Markdown")
            return True
        state["qty"] = qty
        state["step"] = "buy_price"
        await message.answer(f"По какой цене за 1 {state['symbol']}? Например `60000`.", parse_mode="Markdown")
        return True

    if step == "buy_price":
        try:
            price = float(text.replace(",", "."))
            assert price > 0
        except Exception:
            await message.answer("Введи цену числом, например `60000`.", parse_mode="Markdown")
            return True
        sym = state["symbol"]
        qty = state["qty"]
        tid = await add_trade(user_id, sym, qty, price)
        user_trade_state.pop(user_id, None)
        await message.answer(
            f"✅ Записал покупку `#{tid}`: {sym} — {qty:g} @ {_fmt_money(price)}\n"
            f"Вложено: *{_fmt_money(qty * price)}*\n\n"
            f"Когда продашь — жми ➖ Продал.",
            parse_mode="Markdown", reply_markup=_calc_menu_kb())
        return True

    if step == "sell_price":
        try:
            price = float(text.replace(",", "."))
            assert price > 0
        except Exception:
            await message.answer("Введи цену продажи числом, например `65000`.", parse_mode="Markdown")
            return True
        tid = state.get("trade_id")
        t = await close_trade(user_id, tid, price)
        user_trade_state.pop(user_id, None)
        if not t:
            await message.answer("Не нашёл эту сделку — возможно, она уже закрыта. Глянь /calc list.")
            return True
        await message.answer(_render_close_result(t), parse_mode="Markdown", reply_markup=_calc_menu_kb())
        return True

    return False


# ─── Backtest ───────────────────────────────────────────────────────────────────

backtest_enabled = True  # Global toggle for backtest recording


def backtest_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    buttons = []
    if enabled:
        buttons.append([InlineKeyboardButton(text="⏸ Остановить", callback_data="bt:toggle")])
    else:
        buttons.append([InlineKeyboardButton(text="▶️ Запустить", callback_data="bt:toggle")])
    buttons.append([InlineKeyboardButton(text="📋 История сделок", callback_data="bt:history")])
    buttons.append([InlineKeyboardButton(text="💰 Изменить баланс", callback_data="bt:capital")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("papertrader"))
@require_vip
async def cmd_papertrader(message: Message):
    """Бумажный трейдер (paper-trading), админ/диагностика: /papertrader.

    Раньше висел на /backtest, но /backtest теперь = исторический бэктест
    спот-стратегии (cmd_backtest выше). Эту фичу убрали из пользовательского
    меню (спот-онли, без автоторговли), но команду оставили для диагностики.
    """
    signals = await get_backtest_signals()
    stats = await get_backtest_stats()
    config = await get_backtest_config()

    total = stats.get("total", 0) or 0
    wins = stats.get("wins", 0) or 0
    losses = stats.get("losses", 0) or 0
    total_pnl = stats.get("total_pnl", 0) or 0
    avg_pnl = stats.get("avg_pnl_pct", 0) or 0

    win_rate = (wins / total * 100) if total > 0 else 0

    capital = config.get("capital", 100.0)
    enabled = config.get("enabled", 1)

    msg = "🤖 *ТЕСТОВЫЙ ТРЕЙДЕР*\n"
    msg += "═" * 25 + "\n"
    msg += f"Это бот который торгует по сигналам анализа.\n"
    msg += f"Начинает с виртуального баланса и фармит $$$\n\n"
    msg += f"💵 *Баланс:* `${capital:,.2f}`\n"
    msg += f"📊 *Всего сделок:* {total}\n"
    msg += f"🎯 *Win Rate:* {win_rate:.1f}%\n"
    msg += f"💰 *Total PnL:* `${total_pnl:+,.2f}`\n"
    msg += f"📈 *Avg PnL:* {avg_pnl:+.2f}%\n"
    msg += "═" * 25 + "\n"

    open_positions = [s for s in signals if s.get("status") == "open"]
    if open_positions:
        msg += "\n🔵 *Открытые позиции:*\n"
        for s in open_positions:
            symbol = s["symbol"]
            direction = s["direction"]
            entry = s.get("entry_price", 0)
            emoji = "🟢" if direction == "BUY" else "🔴"
            dir_text = "📈 ЛОНГ" if direction == "BUY" else "📉 ШОРТ"
            msg += f"  {emoji} {symbol} {dir_text} @ ${entry:,.2f}\n"
    else:
        msg += "\n📭 *Нет открытых позиций*\n"

    closed = [s for s in signals if s.get("status") == "closed"]
    if closed:
        msg += "\n📋 *Последние сделки:*\n"
        for s in closed[:5]:
            symbol = s["symbol"]
            direction = s["direction"]
            pnl = s.get("pnl", 0) or 0
            pnl_pct = s.get("pnl_pct", 0) or 0
            emoji = "🟢" if pnl > 0 else "🔴"
            dir_text = "📈" if direction == "BUY" else "📉"
            msg += f"  {emoji} {symbol} {dir_text} ${pnl:+,.2f} ({pnl_pct:+.1f}%)\n"

    status_text = "✅ Работает" if enabled else "❌ Остановлен"
    msg += "═" * 25 + "\n"
    msg += f"Статус: {status_text}"

    await message.answer(
        msg,
        parse_mode="Markdown",
        reply_markup=backtest_keyboard(bool(enabled))
    )

    # Also export to GitHub
    try:
        from github_export import export_backtest_to_github
        await export_backtest_to_github(signals, stats, config)
    except Exception as e:
        logger.warning(f"Backtest GitHub export failed: {e}")


@dp.message(Command("backtest_toggle"))
async def cmd_backtest_toggle(message: Message):
    """Toggle backtest recording using database."""
    config = await get_backtest_config()
    enabled = not bool(config.get("enabled", 1))
    await set_backtest_enabled(enabled)
    status = "включён" if enabled else "выключен"
    await message.answer(f"🤖 Бэктест {status}")


@dp.callback_query(F.data.startswith("bt:"))
async def cb_backtest(callback: CallbackQuery):
    """Handle backtest keyboard buttons."""
    action = callback.data.split(":")[1]
    user_id = callback.from_user.id

    if action == "toggle":
        config = await get_backtest_config()
        enabled = not bool(config.get("enabled", 1))
        await set_backtest_enabled(enabled)
        status = "✅ Работает" if enabled else "❌ Остановлен"
        await callback.message.edit_text(
            callback.message.text.split("Статус: ")[0] + f"Статус: {status}",
            parse_mode="Markdown",
            reply_markup=backtest_keyboard(bool(enabled))
        )
        await callback.answer(f"Бэктест {status}")

    elif action == "history":
        signals = await get_backtest_signals()
        closed = [s for s in signals if s.get("status") == "closed"]

        if not closed:
            await callback.answer("Нет закрытых сделок", show_alert=True)
            return

        msg = "📋 *История сделок*\n"
        msg += "═" * 25 + "\n"

        wins = 0
        losses = 0
        for s in closed:
            symbol = s["symbol"]
            direction = s["direction"]
            pnl = s.get("pnl", 0) or 0
            pnl_pct = s.get("pnl_pct", 0) or 0
            date = s.get("created_at", "")[:10]
            emoji = "🟢" if pnl > 0 else "🔴"
            if pnl > 0:
                wins += 1
            else:
                losses += 1
            dir_text = "📈" if direction == "BUY" else "📉"
            msg += f"{date} {emoji} {symbol} {dir_text} ${pnl:+,.2f} ({pnl_pct:+.1f}%)\n"

        msg += "═" * 25 + "\n"
        msg += f"Всего: {len(closed)} | 🟢 {wins} | 🔴 {losses}"

        await callback.message.answer(msg, parse_mode="Markdown")
        await callback.answer()

    elif action == "capital":
        await callback.message.answer(
            "💰 *Изменить баланс*\n\n"
            "Введите новую сумму:\n"
            "/backtest_capital 500\n"
            "или просто число",
            parse_mode="Markdown"
        )
        await callback.answer()

    else:
        await callback.answer()


@dp.message(Command("backtest_capital"))
async def cmd_backtest_capital(message: Message):
    """Set backtest capital."""
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /backtest_capital [сумма]\nПример: /backtest_capital 500")
            return
        new_capital = float(parts[1].replace(",", ""))
        if new_capital <= 0:
            await message.answer("Сумма должна быть больше 0")
            return
        config = await update_backtest_capital(new_capital)
        await message.answer(f"💵 Капитал изменён на ${config['capital']:,.2f}")
    except ValueError:
        await message.answer("Неверная сумма. Пример: /backtest_capital 500")


@dp.message(Command("backtest_clear"))
async def cmd_backtest_clear(message: Message):
    """Clear backtest signals and reset capital."""
    from session_manager import SESSION_START_CAPITAL as _SSC

    await clear_backtest_signals(reset_capital=_SSC)
    await message.answer(f"🗑 Бэктест очищен, капитал сброшен до ${_SSC:,.0f}")


@dp.message(Command("autotrade_reset"))
async def cmd_autotrade_reset(message: Message):
    """Полный сброс автотрейда: SQLite, сессии и BACKTEST.md на GitHub.

    Без этой команды /backtest_clear только чистит SQLite, но фоновый цикл
    автотрейда тут же подтягивает старый капитал из BACKTEST.md и продолжает
    топтаться на $51. Эта команда синхронно сбрасывает все три источника.
    """
    from session_manager import session_manager as _sm, SESSION_START_CAPITAL as _SSC

    parts = message.text.split()
    new_capital = _SSC
    if len(parts) >= 2:
        try:
            new_capital = float(parts[1].replace(",", ""))
            if new_capital <= 0:
                await message.answer(
                    f"Сумма должна быть больше 0. Пример: /autotrade_reset {_SSC:.0f}"
                )
                return
        except ValueError:
            await message.answer(
                f"Неверная сумма. Пример: /autotrade_reset {_SSC:.0f}"
            )
            return

    # 1. Чистим SQLite-сигналы и капитал
    await clear_backtest_signals(reset_capital=new_capital)
    # 2. Сбрасываем менеджер сессий полностью (чтобы _loaded=True не дал
    #    подтянуть старое из GitHub в следующем цикле автотрейдера)
    _sm.hard_reset(start_capital=new_capital)
    # 3. Пушим свежий BACKTEST.md — иначе цикл прочитает «��екущий: $51»
    pushed_ok = False
    try:
        from signal_trader import _export_backtest_snapshot
        await _export_backtest_snapshot()
        pushed_ok = True
    except Exception as e:
        logger.warning("autotrade_reset: BACKTEST.md push failed: %s", e)

    extra = "✅ BACKTEST.md обновлён на GitHub" if pushed_ok else "⚠️ BACKTEST.md не запушился (см. логи)"
    await message.answer(
        f"🔄 *Автотрейд сброшен*\n"
        f"• Капитал: ${new_capital:,.2f}\n"
        f"• Открытые позиции: 0 (все закрыты)\n"
        f"• Сессия #1, история обнулена\n"
        f"• {extra}\n\n"
        f"Следующий цикл автотрейда стартует с чистого листа.",
        parse_mode="Markdown",
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("analyze", "backtest", "report"):
        from trading_system.cli_main import run_cli

        raise SystemExit(run_cli(sys.argv[1:]))
    asyncio.run(main())
