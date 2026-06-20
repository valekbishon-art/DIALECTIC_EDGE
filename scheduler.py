"""
scheduler.py — Фоновые задачи по расписанию.

ИСПРАВЛЕНО v2:
- export_now() больше НЕ вызывается после каждого /daily.
  Это вызывало бесконечный цикл: /daily → GitHub коммит → Railway деплой →
  бот рестартует → /daily по расписанию → GitHub коммит → Railway деплой...

- GitHub экспорт теперь происходит только 1 раз в сутки (в 00:05 UTC),
  а не после каждого запроса пользователя.

- Добавлена защита от двойного запуска экспорта (_last_export_date).
"""
import asyncio
import logging
import os
from datetime import datetime, date, time, timedelta, timezone
from config import ADMIN_IDS
from database import (
    get_daily_subscribers,
    reset_daily_counts,
    get_signals_subscribers,
)

try:
    from alert_system import AlertSystem
    ALERT_SYSTEM_ENABLED = True
except ImportError:
    ALERT_SYSTEM_ENABLED = False

try:
    from signals import SignalsSystem
    SIGNALS_SYSTEM_ENABLED = True
except ImportError:
    SIGNALS_SYSTEM_ENABLED = False

try:
    from auto_tracker import AutoTracker
    AUTO_TRACKER_ENABLED = True
except ImportError:
    AUTO_TRACKER_ENABLED = False

try:
    from halal_alerts import HalalAlertSystem
    from database import get_halal_alert_subscribers
    HALAL_ALERT_ENABLED = True
except ImportError:
    HALAL_ALERT_ENABLED = False

try:
    from smart_money_alert import SmartMoneyAlertSystem
    SMART_MONEY_ALERT_ENABLED = True
except ImportError:
    SMART_MONEY_ALERT_ENABLED = False

try:
    from best_deal_alert import (
        BestDealAlertSystem,
        feature_enabled as best_deal_feature_enabled,
        get_check_interval_sec as best_deal_interval_sec,
    )
    BEST_DEAL_ALERT_ENABLED = True
except ImportError:
    BEST_DEAL_ALERT_ENABLED = False

# Фича ПАМП: кросс-биржевой сканер пампов. Импорт через try/except,
# чтобы старый код без pump_alert.py продолжал стартовать.
try:
    from pump_alert import (
        PumpAlertSystem,
        feature_enabled as pump_feature_enabled,
        get_interval_sec as pump_interval_sec,
    )
    PUMP_ALERT_ENABLED = True
except ImportError:
    PUMP_ALERT_ENABLED = False

# Фича ДЕПЕГ: алерт о депеге фиат-обеспеченных стейблов (возможен возврат к $1).
# Импорт через try/except — старый код без depeg_monitor.py продолжает стартовать.
# Подписчиков берём из спот-автоалертов (get_halal_alert_subscribers).
try:
    from depeg_monitor import (
        DepegAlertSystem,
        feature_enabled as depeg_feature_enabled,
        get_interval_seconds as depeg_interval_seconds,
    )
    from database import get_halal_alert_subscribers  # подписчики спот-алертов
    DEPEG_ALERT_ENABLED = True
except ImportError:
    DEPEG_ALERT_ENABLED = False

# Post-mortem loop (24h-later анализ дайджеста).  Импорт через try/except,
# чтобы старый код, без `core/post_mortem.py`, продолжал стартовать.
try:
    from config import FEATURE_POST_MORTEM, POST_MORTEM_RUN_TIME_UTC
    from core.post_mortem import run_post_mortem as _run_post_mortem
    POST_MORTEM_ENABLED = True
except ImportError:
    POST_MORTEM_ENABLED = False
    FEATURE_POST_MORTEM = False
    POST_MORTEM_RUN_TIME_UTC = "23:50"

# Per-agent calibration loop (резолв probabilistic forecast'ов агентов).
# Сам модуль stdlib-only, но MarketDataChain нужен только в рантайме —
# импорт лениво в самой задаче.
try:
    from core.agent_calibration_io import (
        evaluate_pending_predictions as _evaluate_agent_predictions,
        feature_enabled as _agent_calib_enabled,
    )
    AGENT_CALIB_ENABLED = True
except ImportError:
    AGENT_CALIB_ENABLED = False

# Cross-exchange microstructure loop. Опять же stdlib-only ядро, aiohttp нужен
# только при настоящем запросе venue — импортится внутри loop'а.
try:
    from market_indicators.microstructure_io import (
        compute_microstructure_signal,
        feature_enabled as _microstructure_enabled,
        get_baseline_depth,
        get_enabled_venues,
        get_interval_seconds,
        get_symbols,
        make_aiohttp_http_client,
        persist_signal as _persist_microstructure_signal,
    )
    MICROSTRUCTURE_ENABLED = True
except ImportError:
    MICROSTRUCTURE_ENABLED = False

# Narrative drift tracker (Killer #3). Embedding API (Gemini/Mistral) +
# online clustering поверх SQLite. Ядро stdlib-only, HTTP — лениво в loop'е.
try:
    from market_indicators.narratives_io import (
        SqliteNarrativeDBAdapter,
        feature_enabled as _narrative_enabled,
        format_drift_summary,
        get_active_provider as _narrative_get_active_provider,
        get_interval_seconds as _narrative_get_interval_seconds,
        get_retention_days as _narrative_get_retention_days,
        ingest_documents,
        make_embedding_client,
    )
    NARRATIVE_ENABLED = True
except ImportError:
    NARRATIVE_ENABLED = False

# Funding term structure (Tier A #4). Bybit + Binance funding + deliverable
# фьючерсы. Lazy aiohttp.
try:
    from market_indicators.funding_term_io import (
        feature_enabled as _funding_term_enabled,
        fetch_term_structure as _fetch_term_structure,
        format_term_summary,
        get_interval_seconds as _funding_term_get_interval_seconds,
        get_previous_signal as _funding_term_get_previous_signal,
        get_symbols as _funding_term_get_symbols,
        make_aiohttp_http_client as _make_funding_term_http_client,
        persist_signal as _persist_funding_term_signal,
    )
    from market_indicators.funding_term_structure import detect_inversion_event
    FUNDING_TERM_ENABLED = True
except ImportError:
    FUNDING_TERM_ENABLED = False

# Options skew (Tier A #7). Deribit public API: 25-delta risk reversal +
# ATM IV term structure. Lazy aiohttp.
try:
    from market_indicators.options_skew_io import (
        feature_enabled as _options_skew_enabled,
        fetch_options_skew as _fetch_options_skew,
        get_currencies as _options_skew_get_currencies,
        get_interval_seconds as _options_skew_get_interval_seconds,
        get_previous_signal as _options_skew_get_previous_signal,
        make_aiohttp_http_client as _make_options_skew_http_client,
        persist_signal as _persist_options_skew_signal,
    )
    from market_indicators.options_skew import (
        detect_skew_event,
        format_skew_summary,
    )
    OPTIONS_SKEW_ENABLED = True
except ImportError:
    OPTIONS_SKEW_ENABLED = False

# Stablecoin flows (Tier A #8). Etherscan (USDT/USDC on Ethereum) + Tronscan
# (USDT/USDC on Tron). Total supply mints/redemptions как leading indicator.
try:
    from market_indicators.stablecoin_flows_io import (
        feature_enabled as _stablecoin_enabled,
        fetch_stablecoin_snapshots as _fetch_stablecoin_snapshots,
        get_etherscan_api_key as _stablecoin_get_etherscan_key,
        get_interval_seconds as _stablecoin_get_interval_seconds,
        get_previous_flow_signal as _stablecoin_get_previous_flow_signal,
        get_previous_supply_usd as _stablecoin_get_previous_supply_usd,
        get_tokens as _stablecoin_get_tokens,
        make_aiohttp_http_client as _make_stablecoin_http_client,
        persist_flow_signal as _persist_stablecoin_flow_signal,
        persist_supply_snapshots as _persist_stablecoin_supply_snapshots,
    )
    from market_indicators.stablecoin_flows import (
        build_flow_signal as _build_stablecoin_flow_signal,
        detect_flow_event as _detect_stablecoin_flow_event,
        format_flow_summary as _format_stablecoin_flow_summary,
    )
    STABLECOIN_ENABLED = True
except ImportError:
    STABLECOIN_ENABLED = False

# Auto-alert engine: anomaly screener + BTC ETF flow watcher + liquidation
# magnet. Each rule has its own sub-flag.
try:
    from refactor.services import (
        AlertEngine as _AlertEngine,
        JsonAlertStore as _AlertJsonStore,
        alert_engine_chat_ids as _alert_engine_chat_ids,
        alert_engine_enabled as _alert_engine_enabled,
        alert_engine_interval_sec as _alert_engine_interval_sec,
        format_alert_card as _format_alert_card,
    )
    from refactor.services.alert_rules import (
        BtcEtfOutflowRule as _BtcEtfOutflowRule,
        ScreenerAnomalyRule as _ScreenerAnomalyRule,
    )
    from refactor.services.alert_rules.btc_etf_outflow import feature_enabled as _alert_btc_etf_enabled
    # liquidation_cluster удалён (деривативы вне спот-режима).
    from refactor.services.alert_rules.screener_anomaly import (
        feature_enabled as _alert_screener_enabled,
    )
    ALERT_ENGINE_LOADED = True
except ImportError:
    ALERT_ENGINE_LOADED = False

# P2P arbitrage alerts. Manual command lives in refactor/handlers; scheduler
# only sends admin alerts when a clean window appears.
try:
    from p2p_arbitrage import (
        alerts_enabled as _p2p_alerts_enabled,
        feature_enabled as _p2p_feature_enabled,
        find_p2p_opportunities as _find_p2p_opportunities,
        format_p2p_report as _format_p2p_report,
        get_alert_chat_ids as _p2p_get_alert_chat_ids,
        get_alert_cooldown_sec as _p2p_get_alert_cooldown_sec,
        get_alert_interval_sec as _p2p_get_alert_interval_sec,
        get_assets as _p2p_get_assets,
        get_fiats as _p2p_get_fiats,
        get_scan_throttle_sec as _p2p_get_scan_throttle_sec,
        get_max_results as _p2p_get_max_results,
        get_min_completion_rate_pct as _p2p_get_min_completion_rate_pct,
        get_min_orders as _p2p_get_min_orders,
        get_min_spread_pct as _p2p_get_min_spread_pct,
        get_pay_types as _p2p_get_pay_types,
        get_settlement_buffer_pct as _p2p_get_settlement_buffer_pct,
        merchant_only as _p2p_merchant_only,
    )
    from refactor.handlers.p2p_arbitrage_handler import fetch_p2p_ads as _fetch_p2p_ads
    P2P_ARBITRAGE_ENABLED = True
except ImportError:
    P2P_ARBITRAGE_ENABLED = False

# Cascade post-mortem (auto-log публичных liquidations cascade'ов).
# Включается фичефлагом FEATURE_CASCADE_POST_MORTEM=1, default OFF.
# Запускает 3 task'а: Binance WS, Bybit WS, agg-loop.
try:
    from market_indicators.cascade_post_mortem_io import (
        binance_enabled as _cpm_binance_enabled,
        binance_ws_listener as _cpm_binance_ws,
        bybit_enabled as _cpm_bybit_enabled,
        bybit_ws_listener as _cpm_bybit_ws,
        cascade_post_mortem_loop as _cpm_loop,
        feature_enabled as _cpm_enabled,
    )
    CASCADE_POST_MORTEM_ENABLED = True
except ImportError:
    CASCADE_POST_MORTEM_ENABLED = False

# P2P self-audit (backcheck whether surfaced opportunities materialised).
# Enabled by FEATURE_P2P_SELF_AUDIT=1, default OFF.
try:
    from p2p_audit import feature_enabled as _p2p_audit_enabled
    from p2p_audit_io import (
        ensure_audit_table_exists as _p2p_audit_ensure_table,
        p2p_audit_loop as _p2p_audit_loop,
    )

    P2P_AUDIT_ENABLED = True
except ImportError:
    P2P_AUDIT_ENABLED = False

# M2 advisor portfolio watcher (закрывает позиции по SL/TP, шлёт алерты).
# Включается FEATURE_ADVISOR_PORTFOLIO=1, default OFF.
try:
    from refactor.providers.advisor_storage import (
        feature_enabled as _advisor_portfolio_enabled,
    )
    ADVISOR_PORTFOLIO_ENABLED = True
except ImportError:
    ADVISOR_PORTFOLIO_ENABLED = False

# BTC outlook auto-alerts. Periodically computes the same verdict as /btc and
# proactively sends a Telegram alert when lean flips or confidence jumps,
# subject to cooldown. Feature flag FEATURE_BTC_OUTLOOK_ALERTS (default ON).
try:
    from core.btc_alerts import (
        BTCAlertSnapshot as _BTCAlertSnapshot,
        feature_enabled as _btc_alerts_enabled,
        format_btc_alert_headline as _btc_format_headline,
        get_alert_chat_ids as _btc_alert_chat_ids,
        get_alert_interval_sec as _btc_alert_interval_sec,
        should_fire_btc_alert as _btc_should_fire,
    )
    from core.btc_outlook import compute_btc_outlook as _btc_compute_outlook
    from core.btc_outlook import format_btc_outlook_markdown as _btc_format_markdown
    from refactor.handlers.btc_handler import (
        fetch_btc_outlook_inputs as _btc_fetch_inputs,
    )
    BTC_OUTLOOK_ALERTS_LOADED = True
except ImportError:
    BTC_OUTLOOK_ALERTS_LOADED = False

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, bot, send_daily_fn, check_predictions_fn, broadcast_daily_fn=None):
        self.bot = bot
        self.send_daily = send_daily_fn
        self.check_predictions = check_predictions_fn
        # Рассылка утреннего Дайджеста Диалектики в 09:00 MSK (premium/trial).
        self.broadcast_daily = broadcast_daily_fn
        self._last_broadcast_date: date | None = None
        self._running = False
        self._last_export_date: date | None = None
        self._last_p2p_alert_keys: dict[str, datetime] = {}
        # BTC outlook alerts: in-memory snapshot of last fired verdict. Reset on
        # restart, which is fine — first run после рестарта всё равно пойдёт
        # по first-fire ветке если confidence ≥ min.
        self._last_btc_alert: _BTCAlertSnapshot | None = None
        self._alert_system = None
        self._signals_system = None
        # Cascade post-mortem: shared stop-event для WS-listener'ов и agg-loop.
        # Заполняется при start() если фича включена.
        self._cpm_stop_event: asyncio.Event | None = None
        # P2P self-audit: shared stop-event для backcheck-loop'а.
        self._p2p_audit_stop_event: asyncio.Event | None = None

        if ALERT_SYSTEM_ENABLED:
            try:
                github_repo = os.getenv("GITHUB_REPO", "ANAEHY/dialectic_edge")
                self._alert_system = AlertSystem(self.bot, github_repo)
                logger.info("✅ Alert system инициализирован")
            except Exception as e:
                logger.warning(f"Alert system init error: {e}")

        if SIGNALS_SYSTEM_ENABLED:
            try:
                github_repo = os.getenv("GITHUB_REPO", "ANAEHY/dialectic_edge")
                self._signals_system = SignalsSystem(self.bot, github_repo)
                logger.info("✅ Signals system инициализирован")
            except Exception as e:
                logger.warning(f"Signals system init error: {e}")

        self._auto_tracker = None
        if AUTO_TRACKER_ENABLED:
            try:
                self._auto_tracker = AutoTracker()
                logger.info("✅ Auto tracker инициализирован")
            except Exception as e:
                logger.warning(f"Auto tracker init error: {e}")

        self._halal_alert = None
        if HALAL_ALERT_ENABLED:
            try:
                self._halal_alert = HalalAlertSystem(self.bot)
                logger.info("✅ Спот-автоалерты инициализированы")
            except Exception as e:
                logger.warning(f"Spot alert init error: {e}")

        self._smart_money_alert = None
        if SMART_MONEY_ALERT_ENABLED:
            try:
                self._smart_money_alert = SmartMoneyAlertSystem(self.bot)
                logger.info("✅ Smart-money alert system инициализирован")
            except Exception as e:
                logger.warning(f"Smart-money alert init error: {e}")

        self._best_deal_alert = None
        if BEST_DEAL_ALERT_ENABLED and best_deal_feature_enabled():
            try:
                self._best_deal_alert = BestDealAlertSystem(self.bot)
                logger.info("✅ Best-deal auto-push alert инициализирован")
            except Exception as e:
                logger.warning(f"Best-deal alert init error: {e}")

        # Фича ПАМП: кросс-биржевой авто-сканер пампов. По умолчанию ВЫКЛ.
        self._pump_alert = None
        if PUMP_ALERT_ENABLED and pump_feature_enabled():
            try:
                self._pump_alert = PumpAlertSystem(self.bot)
                logger.info("✅ Памп-сканер (PUMP) инициализирован")
            except Exception as e:
                logger.warning(f"Pump alert init error: {e}")

        # Фича ДЕПЕГ: авто-алерт о депеге стейблов. По умолчанию ВЫКЛ
        # (FEATURE_DEPEG_ALERT=1). Команда /depeg работает всегда.
        self._depeg_alert = None
        if DEPEG_ALERT_ENABLED and depeg_feature_enabled():
            try:
                self._depeg_alert = DepegAlertSystem(self.bot)
                logger.info("✅ Депег-алерт стейблов инициализирован")
            except Exception as e:
                logger.warning(f"Depeg alert init error: {e}")

    async def start(self):
        self._running = True
        logger.info("⏰ Scheduler запущен")

        tasks = [
            self._daily_digest_loop(),
            self._prediction_checker_loop(),
            self._midnight_reset_loop(),
            self._daily_github_export_loop(),
        ]

        # Рассылка утреннего Дайджеста Диалектики в 09:00 MSK (premium/trial).
        if self.broadcast_daily is not None:
            tasks.append(self._dialectica_broadcast_loop())
            logger.info("📬 Dialectica broadcast loop включён (09:00 MSK)")

        if ALERT_SYSTEM_ENABLED and self._alert_system:
            tasks.append(self._alert_checker_loop())

        if SIGNALS_SYSTEM_ENABLED and self._signals_system:
            tasks.append(self._signals_checker_loop())

        # Фича ПАМП: фоновый авто-цикл (только если FEATURE_PUMP_SCANNER=1).
        if PUMP_ALERT_ENABLED and self._pump_alert is not None:
            tasks.append(self._pump_scanner_loop())

        # Фича ДЕПЕГ: фоновый авто-алерт (только если FEATURE_DEPEG_ALERT=1).
        if DEPEG_ALERT_ENABLED and self._depeg_alert is not None:
            tasks.append(self._depeg_alert_loop())
            logger.info("⚖️ Депег-алерт стейблов: loop запущен (interval=%ss)",
                        depeg_interval_seconds())

        if AUTO_TRACKER_ENABLED and self._auto_tracker:
            tasks.append(self._auto_tracker_loop())

        if HALAL_ALERT_ENABLED and self._halal_alert:
            tasks.append(self._halal_alert_loop())
            logger.info("🔔 Спот-автоалерты: loop запущен (каждые 6ч)")

        if SMART_MONEY_ALERT_ENABLED and self._smart_money_alert:
            tasks.append(self._smart_money_alert_loop())

        # «Лучшая сделка» (best_deal auto-push) ОТКЛЮЧЕНА как directional-пережиток:
        # бэктест 2020-26 показал, что её score-сигналы робастно убыточны. Заменена
        # carry-брифингом (_carry_briefing_loop). Чтобы вернуть — раскомментируй +
        # FEATURE_BEST_DEAL_AUTO_PUSH=1. Класс/тесты best_deal_alert оставлены.
        # if (
        #     BEST_DEAL_ALERT_ENABLED
        #     and best_deal_feature_enabled()
        #     and self._best_deal_alert is not None
        # ):
        #     tasks.append(self._best_deal_alert_loop())

        if POST_MORTEM_ENABLED and FEATURE_POST_MORTEM:
            tasks.append(self._post_mortem_loop())
            logger.info(
                "🔬 Post-mortem loop включён (запуск в %s UTC ежедневно)",
                POST_MORTEM_RUN_TIME_UTC,
            )

        if AGENT_CALIB_ENABLED and _agent_calib_enabled():
            tasks.append(self._agent_calibration_loop())
            logger.info(
                "📊 Agent calibration loop включён "
                "(резолв probabilistic forecast'ов раз в 30 мин)"
            )

        if MICROSTRUCTURE_ENABLED and _microstructure_enabled():
            tasks.append(self._microstructure_loop())
            logger.info(
                "🌊 Microstructure loop включён "
                "(snapshot L2-orderbook'ов %s раз в %ss)",
                "/".join(get_enabled_venues()),
                get_interval_seconds(),
            )

        if NARRATIVE_ENABLED and _narrative_enabled():
            tasks.append(self._narrative_drift_loop())
            logger.info(
                "🌐 Narrative drift loop включён "
                "(provider=%s, interval=%ss)",
                _narrative_get_active_provider(),
                _narrative_get_interval_seconds(),
            )

        if FUNDING_TERM_ENABLED and _funding_term_enabled():
            tasks.append(self._funding_term_loop())
            logger.info(
                "📉 Funding term structure loop включён "
                "(symbols=%s, interval=%ss)",
                ",".join(_funding_term_get_symbols()),
                _funding_term_get_interval_seconds(),
            )

        if OPTIONS_SKEW_ENABLED and _options_skew_enabled():
            tasks.append(self._options_skew_loop())
            logger.info(
                "🎲 Options skew loop включён "
                "(symbols=%s, interval=%ss)",
                ",".join(_options_skew_get_currencies()),
                _options_skew_get_interval_seconds(),
            )

        if STABLECOIN_ENABLED and _stablecoin_enabled():
            tasks.append(self._stablecoin_flows_loop())
            logger.info(
                "💵 Stablecoin flows loop включён "
                "(tokens=%s, interval=%ss)",
                ",".join(_stablecoin_get_tokens()),
                _stablecoin_get_interval_seconds(),
            )

        if P2P_ARBITRAGE_ENABLED and _p2p_feature_enabled() and _p2p_alerts_enabled():
            tasks.append(self._p2p_arbitrage_alert_loop())
            logger.info(
                "🧭 P2P arbitrage alerts включены "
                "(assets=%s, fiats=%s, interval=%ss)",
                ",".join(_p2p_get_assets()),
                ",".join(_p2p_get_fiats()),
                _p2p_get_alert_interval_sec(),
            )

        # BTC outlook alert ОТКЛЮЧЁН как directional-пережиток: шлёт «BULL/BEAR
        # confidence X%» — прогноз направления, который бэктест 2020-26 опроверг.
        # «100%» вводит в заблуждение (это доля слабых сигналов, не вероятность).
        # Реальный edge — /carry и /arb. Вернуть: раскомментируй + FEATURE_BTC_OUTLOOK_ALERTS=1.
        # if BTC_OUTLOOK_ALERTS_LOADED and _btc_alerts_enabled():
        #     tasks.append(self._btc_outlook_alert_loop())
        #     logger.info(
        #         "🟧 BTC outlook alerts включены (interval=%ss)",
        #         _btc_alert_interval_sec(),
        #     )

        if ALERT_ENGINE_LOADED and _alert_engine_enabled():
            tasks.append(self._alert_engine_loop())
            active_rules = [
                name for name, ok in (
                    ("screener", _alert_screener_enabled()),
                    ("btc_etf", _alert_btc_etf_enabled()),
                    # liquidation rule удалён (деривативы вне спот-режима).
                ) if ok
            ]
            logger.info(
                "🔔 Alert engine включён (interval=%ss, rules=%s)",
                _alert_engine_interval_sec(),
                ",".join(active_rules) or "none",
            )

        if CASCADE_POST_MORTEM_ENABLED and _cpm_enabled():
            self._cpm_stop_event = asyncio.Event()
            active_venues: list[str] = []
            if _cpm_binance_enabled():
                tasks.append(
                    _cpm_binance_ws(stop_event=self._cpm_stop_event)
                )
                active_venues.append("binance")
            if _cpm_bybit_enabled():
                tasks.append(
                    _cpm_bybit_ws(stop_event=self._cpm_stop_event)
                )
                active_venues.append("bybit")
            tasks.append(
                _cpm_loop(
                    stop_event=self._cpm_stop_event,
                    send_telegram=self._cpm_send_telegram,
                )
            )
            logger.info(
                "🔥 Cascade post-mortem loop включён (venues=%s)",
                ",".join(active_venues) or "none",
            )

        if ADVISOR_PORTFOLIO_ENABLED and _advisor_portfolio_enabled():
            tasks.append(self._advisor_portfolio_watcher_loop())
            logger.info(
                "📂 Advisor portfolio watcher включён "
                "(SL/TP алерты, interval=%ss)",
                int(os.getenv("ADVISOR_PORTFOLIO_WATCH_INTERVAL_SEC", "300")),
            )

        if P2P_AUDIT_ENABLED and _p2p_audit_enabled():
            try:
                await _p2p_audit_ensure_table()
            except Exception as exc:  # noqa: BLE001
                logger.warning("p2p audit: ensure_audit_table_exists failed: %s", exc)
            try:
                from refactor.handlers.p2p_arbitrage_handler import (
                    fetch_p2p_ads as _p2p_fetch_ads,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "p2p audit: handler fetch_p2p_ads import failed (%s) — audit loop disabled",
                    exc,
                )
            else:
                self._p2p_audit_stop_event = asyncio.Event()
                tasks.append(
                    _p2p_audit_loop(
                        fetch_p2p_ads=_p2p_fetch_ads,
                        stop_event=self._p2p_audit_stop_event,
                    )
                )
                logger.info("📊 P2P self-audit loop включён")

        # edge-леджер: резолвер pending-сигналов (под флагом FEATURE_EDGE_LEDGER)
        try:
            from config import FEATURE_EDGE_LEDGER, EDGE_RESOLVE_INTERVAL_SEC
            if FEATURE_EDGE_LEDGER:
                tasks.append(self._edge_resolve_loop())
                logger.info(
                    "📐 Edge-леджер резолвер включён (interval=%ss)",
                    EDGE_RESOLVE_INTERVAL_SEC,
                )
        except Exception:
            logger.debug("edge_ledger loop registration skipped", exc_info=True)

        # carry-брифинг: режим + пошаговая carry-сделка + листинги (FEATURE_CARRY_BRIEFING)
        if os.getenv("FEATURE_CARRY_BRIEFING", "1").strip().lower() in {"1", "true", "yes", "on"}:
            tasks.append(self._carry_briefing_loop())
            logger.info(
                "💱 Carry monitor+briefing включён (monitor=%ss, briefing=%ss)",
                os.getenv("CARRY_MONITOR_INTERVAL_SEC", str(30 * 60)),
                os.getenv("CARRY_BRIEFING_INTERVAL_SEC", str(6 * 3600)),
            )

        await asyncio.gather(*tasks)

    async def _cpm_send_telegram(self, text: str) -> bool:
        """Шлёт каскадный post-mortem в TG-чаты.

        Использует тот же chat-resolver, что и alert_engine (ALERT_ENGINE_CHAT_IDS
        env с fallback на ADMIN_IDS). Возвращает True если хотя бы одно
        сообщение успешно ушло.
        """
        try:
            if ALERT_ENGINE_LOADED:
                chat_ids = _alert_engine_chat_ids(ADMIN_IDS)
            else:
                chat_ids = list(ADMIN_IDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cascade post-mortem: chat-id resolve failed: %s", exc)
            chat_ids = list(ADMIN_IDS)

        if not chat_ids:
            logger.info("cascade post-mortem: нет ADMIN_IDS — skipping TG send")
            return False

        sent_ok = False
        for chat_id in chat_ids:
            try:
                await self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                sent_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cascade post-mortem TG send failed chat_id=%s: %s",
                    chat_id,
                    exc,
                )
        return sent_ok

    async def _carry_briefing_loop(self):
        """Мониторинг открытых carry/арб позиций + периодический полный брифинг.

        ЗАКРЫТИЯ/ПЕРЕВОРОТЫ детектятся БЫСТРЫМ тиком (CARRY_MONITOR_INTERVAL_SEC,
        дефолт 30 мин) — диф против ПРЕДЫДУЩЕГО тика, а не раз в 6ч. Иначе фандинг
        схлопывается, а юзер узнаёт об этом через часы и пачкой (баг latency-аудита).
        Полный брифинг (режим рынка + пошаговая carry-сделка + листинги) тяжёлый и
        шлётся РЕЖЕ — раз в CARRY_BRIEFING_INTERVAL_SEC (дефолт 6ч).

        Состояние открытых позиций ПЕРСИСТИТСЯ на диск (DATA_DIR/том Railway):
        рестарт не теряет позиции и не делает первый тик слепым.
        Non-fatal: ошибки логируются, луп живёт. Состояние обновляем только при
        надёжных данных (health-guard) — пустой фетч не даёт ложный масс-выход.
        """
        return  # [removed] carry/арб-мониторинг отключён (деривативы/фандинг/процент)
        from core.carry_briefing import (arb_close_alerts, build_briefing,
                                          cap_state, close_alerts, load_monitor_state,
                                          save_monitor_state, scan_carry_open,
                                          select_tracked_arb, select_tracked_carry)
        from core.carry_signal import fetch_funding

        monitor_interval = max(60, int(os.getenv("CARRY_MONITOR_INTERVAL_SEC", str(30 * 60))))
        briefing_interval = max(monitor_interval,
                                int(os.getenv("CARRY_BRIEFING_INTERVAL_SEC", str(6 * 3600))))
        capital = float(os.getenv("CARRY_BRIEFING_CAPITAL", "1000"))
        ticks_per_briefing = max(1, round(briefing_interval / monitor_interval))
        # Сколько позиций РЕАЛЬНО трекать (= сколько рекомендуем). Анти-спам:
        # раньше трекалось всё из скана (15-20) → пачка «ЗАКРЫВАЙ/ПЕРЕВОРОТ».
        carry_max = max(1, int(os.getenv("CARRY_MAX_TRACK", "3")))
        arb_max = max(1, int(os.getenv("ARB_MAX_TRACK", "3")))

        # Файл состояния — в DATA_DIR (том Railway переживает рестарт), иначе рядом с БД.
        try:
            from pathlib import Path
            from config import DB_PATH
            state_path = str(Path(DB_PATH).resolve().parent / "carry_monitor_state.json")
        except Exception:  # noqa: BLE001
            state_path = "carry_monitor_state.json"

        # Восстанавливаем прошлое состояние (закрытия за время даунтайма не теряются).
        # Капаем старое раздутое состояние до лимита — иначе первый тик дал бы бурст.
        _c, _a = load_monitor_state(state_path)
        self._carry_open = cap_state(_c, carry_max)
        self._arb_open = cap_state(_a, arb_max, by_spread=True)
        logger.info(
            "💱 Carry monitor: tick=%ss, briefing каждые %s тиков (~%ss); "
            "восстановлено carry=%d arb=%d из %s",
            monitor_interval, ticks_per_briefing, briefing_interval,
            len(self._carry_open), len(self._arb_open), state_path,
        )

        await asyncio.sleep(900)  # warm-up 15 мин, не толкаемся на старте
        tick = 0
        while self._running:
            try:
                # Один скан на тик — общий и для детекта закрытий, и (если пора)
                # для брифинга. Кросс-арб сканируем ОДИН раз: иначе briefing и
                # сигнал берут разные биржи в одной позиции (баг TRX).
                arb, arb_healthy = [], True
                try:
                    from core.cross_exchange import scan_with_health
                    arb, arb_healthy = await asyncio.to_thread(scan_with_health)
                except Exception:  # noqa: BLE001
                    logger.debug("arb scan skipped", exc_info=True)
                    arb_healthy = False
                fund = await asyncio.to_thread(fetch_funding)
                cur_open_full, _pos, carry_healthy = scan_carry_open(data=fund)
                # Кросс-арб: храним спред+направление+фандинг ног (ловим и переворот
                # ног, И смену знака фандинга на ноге — баг DOT).
                cur_arb_full = {o.asset: (o.spread, o.short_venue, o.long_venue,
                                          o.short_ann, o.long_ann) for o in arb}

                # АНТИ-СПАМ: трекаем только то, что реально рекомендуем (вход на
                # STRONG, держим пока ≥ THIN, кап top-N) — не весь скан из 15-20.
                tracked_carry = select_tracked_carry(self._carry_open, cur_open_full,
                                                     max_track=carry_max)
                tracked_arb = select_tracked_arb(self._arb_open, cur_arb_full,
                                                 max_track=arb_max)

                # Детект против прошлого тика, с health-guard на обеих стратегиях.
                closes = close_alerts(self._carry_open, tracked_carry, cur_healthy=carry_healthy)
                closes = closes + arb_close_alerts(self._arb_open, tracked_arb,
                                                   cur_healthy=arb_healthy)

                # Состояние обновляем и персистим ТОЛЬКО при надёжных данных —
                # иначе пустой фетч затёр бы открытые позиции и дал ложный масс-выход.
                changed = False
                if carry_healthy:
                    self._carry_open = tracked_carry
                    changed = True
                if arb_healthy:
                    self._arb_open = tracked_arb
                    changed = True
                if changed:
                    save_monitor_state(state_path, self._carry_open, self._arb_open)

                # Полный брифинг — раз в ticks_per_briefing тиков (и на старте, tick=0).
                text = None
                if tick % ticks_per_briefing == 0:
                    try:
                        text, _ = await asyncio.to_thread(
                            build_briefing, capital, arb_opps=arb, funding=fund)
                    except Exception as e:  # noqa: BLE001
                        logger.error("carry briefing build error: %s", e)

                if closes or text:
                    try:
                        chat_ids = (_alert_engine_chat_ids(ADMIN_IDS)
                                    if ALERT_ENGINE_LOADED else list(ADMIN_IDS))
                    except Exception:  # noqa: BLE001
                        chat_ids = list(ADMIN_IDS)
                    for chat_id in chat_ids:
                        try:
                            # СНАЧАЛА закрытия/переворот, ПОТОМ брифинг (новое открытие) —
                            # чтобы старую позицию закрыли до новой.
                            for cm in closes:
                                await self.bot.send_message(chat_id, cm, parse_mode="HTML")
                            if text:
                                await self.bot.send_message(
                                    chat_id, text, parse_mode="HTML",
                                    disable_web_page_preview=True)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("carry monitor send failed chat=%s: %s", chat_id, exc)
            except Exception as e:  # noqa: BLE001
                logger.error("carry monitor loop error: %s", e)
            tick += 1
            await asyncio.sleep(monitor_interval)

    async def _daily_digest_loop(self):
        """Каждую минуту проверяет — не пора ли слать дайджест подписчикам."""
        while self._running:
            try:
                now = datetime.now()
                current_time = now.strftime("%H:%M")
                subscribers = await get_daily_subscribers()
                for user in subscribers:
                    sub_time = user.get("sub_time", "08:00")
                    if sub_time == current_time:
                        logger.info(f"📬 Отправляю дайджест пользователю {user['user_id']}")
                        try:
                            await self.send_daily(user["user_id"])
                        except Exception as e:
                            logger.warning(f"Ошибка рассылки для {user['user_id']}: {e}")
            except Exception as e:
                logger.error(f"Daily digest loop error: {e}")
            await asyncio.sleep(60)

    async def _dialectica_broadcast_loop(self):
        """Рассылает утренний Дайджест Диалектики в 09:00 MSK раз в сутки.

        Получатели — пользователи с активным премиумом ИЛИ фри-триалом
        (см. broadcast_dialectica_digest в main.py). Дайджест готовит cron в
        08:50 MSK и кладёт в PostgreSQL/Neon, поэтому к 09:00 он уже готов.

        Москва — фиксированный UTC+3 (без переходов на летнее время), поэтому
        целевое время считаем как UTC+3 без зависимости от таймзоны сервера.
        """
        if self.broadcast_daily is None:
            return
        # Небольшая задержка на старте, чтобы БД успела инициализироваться.
        await asyncio.sleep(45)
        while self._running:
            try:
                msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
                today = msk_now.date()
                if (
                    msk_now.hour == 9
                    and msk_now.minute < 5
                    and self._last_broadcast_date != today
                ):
                    self._last_broadcast_date = today
                    logger.info("📬 09:00 MSK — запускаю рассылку Дайджеста Диалектики")
                    try:
                        sent = await self.broadcast_daily()
                        logger.info("📬 Рассылка завершена: %s получателей", sent)
                    except Exception as e:
                        logger.error("Dialectica broadcast error: %s", e)
            except Exception as e:
                logger.error("Dialectica broadcast loop error: %s", e)
            await asyncio.sleep(60)

    async def _prediction_checker_loop(self):
        """Проверяет прогнозы каждые 6 часов."""
        while self._running:
            try:
                logger.info("🔍 Проверяю прогнозы агентов...")
                checked = await self.check_predictions()
                logger.info(f"Проверено прогнозов: {checked}")
            except Exception as e:
                logger.error(f"Prediction checker error: {e}")
            await asyncio.sleep(6 * 3600)

    async def _edge_resolve_loop(self):
        """Резолвит pending-сигналы edge-леджера по свечам (TP/SL/expired).

        Под флагом FEATURE_EDGE_LEDGER (регистрация в start()). Раз в
        EDGE_RESOLVE_INTERVAL_SEC проходит по всем pending-сигналам и
        обновляет статус тех, что уже отыграли.
        """
        from config import EDGE_RESOLVE_INTERVAL_SEC

        await asyncio.sleep(300)  # warm-up: не толкаемся с другими loop'ами на старте
        while self._running:
            try:
                from core.edge_ledger import resolve_pending

                summary = await resolve_pending()
                if summary.get("resolved"):
                    logger.info(
                        "📐 edge-леджер: резолвнуто %s (tp=%s sl=%s exp=%s, pending=%s)",
                        summary["resolved"], summary.get("tp", 0),
                        summary.get("sl", 0), summary.get("expired", 0),
                        summary.get("still_pending", 0),
                    )
            except Exception as e:
                logger.error(f"Edge resolve loop error: {e}")
            await asyncio.sleep(EDGE_RESOLVE_INTERVAL_SEC)

    async def _midnight_reset_loop(self):
        """Сбрасывает счётчики запросов в полночь."""
        while self._running:
            now = datetime.now()
            seconds_to_midnight = (datetime.combine(date.today(), time(0)) + timedelta(days=1) - now).total_seconds()
            await asyncio.sleep(seconds_to_midnight)
            try:
                await reset_daily_counts()
                logger.info("🌙 Счётчики запросов сброшены (полночь)")
            except Exception as e:
                logger.error(f"Midnight reset error: {e}")

    async def _daily_github_export_loop(self):
        """
        Экспортирует track record на GitHub ОДИН РАЗ В СУТКИ в 00:05 UTC.

        ИСПРАВЛЕНО: раньше export_now() вызывался после каждого /daily,
        что создавало GitHub коммит → Railway триггерился на новый коммит →
        бесконечный цикл деплоев.

        Теперь:
        - Экспорт только в 00:05 UTC (один раз в сутки)
        - Защита _last_export_date исключает двойной запуск
        - Никаких коммитов от пользовательских запросов
        """
        # Небольшая задержка при старте чтобы БД успела инициализироваться
        await asyncio.sleep(30)

        while self._running:
            try:
                now = datetime.now()
                _today = now.date()  # noqa: F841 — оставлен для возможного включения экспорта

                # Экспорт ОТКЛЮЧЕН — теперь вручную
                # Включить: раскомментировать ниже
                # if (now.hour == 0 and now.minute == 5
                #         and self._last_export_date != today):
                #     from github_export import export_to_github
                #     success = await export_to_github()
                #     if success:
                #         self._last_export_date = today
                #         logger.info("✅ Track record экспортирован на GitHub (ежесуточно)")
                #     else:
                #         logger.warning("⚠️ GitHub export не выполнен — проверь GITHUB_TOKEN")
                pass

            except Exception as e:
                logger.error(f"GitHub export error: {e}")

            # Проверяем каждую минуту (синхронизируемся с минутным циклом)
            await asyncio.sleep(60)

    async def _alert_checker_loop(self):
        """Проверяет серию вердиктов и шлёт алерт подписчикам каждые 4 часа."""
        await asyncio.sleep(300)  # ждём 5 минут при старте

        while self._running:
            try:
                if self._alert_system is None:
                    await asyncio.sleep(3600)
                    continue

                subscribers = await get_daily_subscribers()
                if subscribers:
                    sent = await self._alert_system.check_and_alert(subscribers)
                    if sent > 0:
                        logger.info(f"📢 Алерты отправлены: {sent}")
            except Exception as e:
                logger.error(f"Alert checker error: {e}")

            await asyncio.sleep(4 * 3600)  # каждые 4 часа

    async def _signals_checker_loop(self):
        """Проверяет сигналы и отправляет подписчикам каждые 2 часа."""
        await asyncio.sleep(600)  # ждём 10 минут при старте

        while self._running:
            try:
                if self._signals_system is None:
                    await asyncio.sleep(3600)
                    continue

                subscribers = await get_signals_subscribers()
                if subscribers:
                    sent = await self._signals_system.check_and_send_signals(subscribers)
                    if sent > 0:
                        logger.info(f"📡 Сигналы отправлены: {sent}")
            except Exception as e:
                logger.error(f"Signals checker error: {e}")

            await asyncio.sleep(2 * 3600)  # каждые 2 часа

    async def _best_deal_alert_loop(self):
        """Авто-push: лучший setup из ``rank_signals`` если score ≥ 60.

        Юзер: «если лучшая сделка набирает свои 60 из 100 очков чтобы она
        приходила пользователю сама а не по вызову кнопки». Переиспользуем
        подписку «Сигналы» (get_signals_subscribers).
        """
        await asyncio.sleep(1200)  # 20 мин при старте

        while self._running:
            try:
                if self._best_deal_alert is None:
                    await asyncio.sleep(3600)
                    continue

                subscribers = await get_signals_subscribers()
                if subscribers:
                    sent = await self._best_deal_alert.check_and_alert(subscribers)
                    if sent > 0:
                        logger.info(f"🎯 Best-deal auto-push отправлен: {sent}")
            except Exception as e:
                logger.error(f"Best-deal alert loop error: {e}")

            await asyncio.sleep(best_deal_interval_sec())

    async def _pump_scanner_loop(self):
        """Фоновый памп-сканер (фича ПАМП).

        Каждые PUMP_SCAN_INTERVAL_SEC секунд сканируем весь спот-рынок
        (Bybit + MEXC + Binance) и рассылаем пампы подписчикам «Сигналов».
        Анти-спам и фильтры — внутри PumpAlertSystem / core.pump_scanner.
        """
        await asyncio.sleep(300)  # 5 мин при старте — пусть бот прогреется

        while self._running:
            try:
                if self._pump_alert is None:
                    await asyncio.sleep(3600)
                    continue

                subscribers = await get_signals_subscribers()
                if subscribers:
                    sent = await self._pump_alert.check_and_alert(subscribers)
                    if sent > 0:
                        logger.info(f"🚀 Памп-алерты отправлены: {sent}")
            except Exception as e:
                logger.error(f"Pump scanner loop error: {e}")

            await asyncio.sleep(pump_interval_sec())

    async def _smart_money_alert_loop(self):
        """Проверяет smart-money convergence каждый час.

        Шлёт алерт подписчикам сигналов только когда ≥ 2 институциональных
        индикаторов синхронно показывают один же direction. Анти-спам внутри.
        """
        await asyncio.sleep(900)  # ждём 15 минут при ст��рте — пусть система прогреется

        while self._running:
            try:
                if self._smart_money_alert is None:
                    await asyncio.sleep(3600)
                    continue

                subscribers = await get_signals_subscribers()
                if subscribers:
                    sent = await self._smart_money_alert.check_and_alert(subscribers)
                    if sent > 0:
                        logger.info(f"🐋 Smart-money convergence алерт отправлен: {sent}")
            except Exception as e:
                logger.error(f"Smart-money alert loop error: {e}")

            await asyncio.sleep(3600)  # каждый час

    async def _p2p_arbitrage_alert_loop(self):
        """Сканирует P2P окна и шлёт только новые clean alerts админам."""
        await asyncio.sleep(120)

        while self._running:
            try:
                chat_ids = _p2p_get_alert_chat_ids(ADMIN_IDS)
                if not chat_ids:
                    logger.info("p2p alerts: нет ADMIN_IDS/P2P_ARBITRAGE_ALERT_CHAT_IDS")
                    await asyncio.sleep(_p2p_get_alert_interval_sec())
                    continue

                sent = await self._run_p2p_alert_scan(chat_ids)
                if sent:
                    logger.info("🧭 P2P arbitrage alerts отправлены: %s", sent)
            except Exception as e:
                logger.error(f"P2P arbitrage alert loop error: {e}")

            await asyncio.sleep(_p2p_get_alert_interval_sec())

    async def _run_p2p_alert_scan(self, chat_ids: tuple[int, ...]) -> int:
        sent = 0
        now = datetime.now()
        pay_types = _p2p_get_pay_types()
        throttle_sec = _p2p_get_scan_throttle_sec()
        is_first = True
        for asset in _p2p_get_assets():
            for fiat in _p2p_get_fiats():
                # Дроссель между парами — растягиваем 42 пары на ~15s, чтобы
                # не словить 429 от Binance/Bybit при широком скане (CIS фиаты
                # × 7 assets). На узком override эффект незаметен.
                if not is_first and throttle_sec > 0:
                    await asyncio.sleep(throttle_sec)
                is_first = False
                buy_ads, sell_ads, errors, source = await _fetch_p2p_ads(
                    asset=asset,
                    fiat=fiat,
                    pay_types=pay_types,
                )
                opportunities = _find_p2p_opportunities(
                    buy_ads,
                    sell_ads,
                    min_spread_pct=_p2p_get_min_spread_pct(),
                    settlement_buffer_pct=_p2p_get_settlement_buffer_pct(),
                    min_completion_rate_pct=_p2p_get_min_completion_rate_pct(),
                    min_orders=_p2p_get_min_orders(),
                    merchant_required=_p2p_merchant_only(),
                    preferred_pay_types=pay_types,
                    max_results=_p2p_get_max_results(),
                )
                if not opportunities:
                    if errors:
                        logger.info("p2p alerts %s/%s source errors: %s", asset, fiat, errors)
                    continue

                best = opportunities[0]
                alert_key = (
                    f"{asset}:{fiat}:{best.buy_ad.advertiser}:{best.sell_ad.advertiser}:"
                    f"{round(best.net_spread_pct, 2)}"
                )
                last_sent = self._last_p2p_alert_keys.get(alert_key)
                if last_sent and (now - last_sent).total_seconds() < _p2p_get_alert_cooldown_sec():
                    continue

                text = "🚨 *P2P arbitrage alert*\n\n" + _format_p2p_report(
                    opportunities,
                    asset=asset,
                    fiat=fiat,
                    pay_types=pay_types,
                    source=source,
                    errors=errors,
                )
                for chat_id in chat_ids:
                    try:
                        await self.bot.send_message(
                            chat_id,
                            text,
                            parse_mode="Markdown",
                            disable_web_page_preview=True,
                        )
                        sent += 1
                    except Exception as exc:
                        logger.warning("p2p alert send failed chat_id=%s: %s", chat_id, exc)
                self._last_p2p_alert_keys[alert_key] = now
        return sent

    async def _btc_outlook_alert_loop(self):
        """Periodically compute BTC outlook and push alerts on flips / jumps.

        Идея: «биток вниз — всё идёт вниз». Не ждём команды `/btc` — сами
        мониторим verdict и пушим как только confidence ≥ min и lean
        изменился (или confidence резко прыгнул). Cooldown'ом давим спам.
        """
        # Warm-up: дать другим источникам прогреться + не штурмовать Binance
        # при старте бота (когда AI debate и /daily тоже стартуют).
        await asyncio.sleep(180)

        while self._running:
            try:
                chat_ids = _btc_alert_chat_ids() or tuple(ADMIN_IDS)
                if not chat_ids:
                    logger.info("btc alerts: нет ADMIN_IDS/BTC_OUTLOOK_ALERT_CHAT_IDS — sleep")
                    await asyncio.sleep(_btc_alert_interval_sec())
                    continue

                inputs = await _btc_fetch_inputs()
                verdict = _btc_compute_outlook(inputs)

                now_ts = asyncio.get_event_loop().time()
                decision = _btc_should_fire(
                    current=verdict,
                    previous=self._last_btc_alert,
                    now_ts=now_ts,
                )
                if not decision.should_fire:
                    if decision.suppressed_reason:
                        logger.debug(
                            "btc alerts hold: %s (lean=%s, conf=%s%%)",
                            decision.suppressed_reason,
                            verdict.lean,
                            verdict.confidence_pct,
                        )
                    await asyncio.sleep(_btc_alert_interval_sec())
                    continue

                headline = _btc_format_headline(decision, verdict)
                body = _btc_format_markdown(verdict, inputs, ai_narrative=None)
                text = headline + "\n" + body

                sent = 0
                for chat_id in chat_ids:
                    try:
                        await self.bot.send_message(
                            chat_id,
                            text,
                            parse_mode="Markdown",
                            disable_web_page_preview=True,
                        )
                        sent += 1
                    except Exception as exc:
                        logger.warning("btc alert send failed chat_id=%s: %s", chat_id, exc)

                if sent:
                    self._last_btc_alert = _BTCAlertSnapshot(
                        lean=verdict.lean,
                        confidence_pct=verdict.confidence_pct,
                        fired_at_ts=now_ts,
                    )
                    logger.info(
                        "🟧 BTC outlook alert отправлен (%s, %s%%, reason=%s, sent=%s)",
                        verdict.lean,
                        verdict.confidence_pct,
                        decision.reason,
                        sent,
                    )
            except Exception as e:
                logger.error(f"BTC outlook alert loop error: {e}")

            await asyncio.sleep(_btc_alert_interval_sec())

    async def _alert_engine_loop(self):
        """Background loop: evaluates configured AlertRules and sends cards.

        Each rule is independent and cooldown'd by ``JsonAlertStore``. Loop
        keeps running on rule-level failures (logged and swallowed inside the
        engine).
        """
        await asyncio.sleep(180)  # warm-up: дать другим источникам прогреться

        store = _AlertJsonStore()
        rules: list = []
        if _alert_screener_enabled():
            rules.append(_ScreenerAnomalyRule.build())
        if _alert_btc_etf_enabled():
            rules.append(_BtcEtfOutflowRule.build())
        # LiquidationClusterRule удалён (деривативы вне спот-режима).

        if not rules:
            logger.info(
                "Alert engine: все правила выключены (FEATURE_ALERT_*=0) — exit",
            )
            return

        engine = _AlertEngine(rules=rules, store=store)

        while self._running:
            try:
                chat_ids = _alert_engine_chat_ids(ADMIN_IDS)
                if not chat_ids:
                    logger.info(
                        "Alert engine: нет ADMIN_IDS/ALERT_ENGINE_CHAT_IDS — sleep",
                    )
                    await asyncio.sleep(_alert_engine_interval_sec())
                    continue

                cards = await engine.evaluate_all()
                if not cards:
                    await asyncio.sleep(_alert_engine_interval_sec())
                    continue

                sent_total = 0
                for card in cards:
                    text = _format_alert_card(card)
                    for chat_id in chat_ids:
                        try:
                            await self.bot.send_message(
                                chat_id,
                                text,
                                parse_mode="Markdown",
                                disable_web_page_preview=True,
                            )
                            sent_total += 1
                        except Exception as exc:
                            logger.warning(
                                "Alert engine send failed chat_id=%s rule=%s: %s",
                                chat_id,
                                card.rule_id,
                                exc,
                            )
                if sent_total:
                    logger.info(
                        "🔔 Alert engine отправил %s сообщений (%s карточек)",
                        sent_total,
                        len(cards),
                    )
            except Exception as exc:
                logger.error("Alert engine loop error: %s", exc)

            await asyncio.sleep(_alert_engine_interval_sec())

    async def _auto_tracker_loop(self):
        """Проверяет прогнозы в 00:10 UTC (через 10 минут после дайджеста)."""
        await asyncio.sleep(120)  # ждём 2 минуты при старте

        while self._running:
            try:
                now = datetime.now()
                current_time = now.strftime("%H:%M")

                # Запускаем в 00:10 UTC каждый день
                if current_time == "00:10":
                    logger.info("🔄 Запускаю авто-проверку прогнозов...")

                    results = await self._auto_tracker.check_all_forecasts()

                    if results:
                        md = self._auto_tracker.generate_markdown(results)
                        await self._auto_tracker.upload_to_github(md, "AUTO_TRACK.md")
                        logger.info(f"✅ Auto track обновлён")

                    # Ждём минуту чтобы не запустить дважды
                    await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"Auto tracker error: {e}")

            # Проверяем каждую минуту
            await asyncio.sleep(60)

    async def _halal_alert_loop(self):
        """Спот-автоалерты: проверяет смену режима тренда каждые 6 часов."""
        await asyncio.sleep(420)  # warm-up: ~7 минут при старте

        while self._running:
            try:
                if self._halal_alert is not None:
                    subscribers = await get_halal_alert_subscribers()
                    sent = await self._halal_alert.check_and_alert(subscribers)
                    if sent > 0:
                        logger.info(f"🔔 Спот-автоалерты отправлены: {sent}")
            except Exception as e:
                logger.error(f"Spot alert loop error: {e}")

            await asyncio.sleep(6 * 3600)  # каждые 6 часов

    async def _depeg_alert_loop(self):
        """Депег-алерт: проверяет цены стейблов и шлёт алерт при новом депеге.
        Подписчиков берём из спот-автоалертов (get_halal_alert_subscribers)."""
        await asyncio.sleep(300)  # warm-up: 5 минут при старте
        interval = depeg_interval_seconds()
        while self._running:
            try:
                if self._depeg_alert is not None:
                    subscribers = await get_halal_alert_subscribers()
                    sent = await self._depeg_alert.check_and_alert(subscribers)
                    if sent > 0:
                        logger.info(f"⚖️ Депег-алерты отправлены: {sent}")
            except Exception as e:
                logger.error(f"Depeg alert loop error: {e}")
            await asyncio.sleep(interval)

    async def _advisor_portfolio_watcher_loop(self):
        """M2: watcher для виртуального портфеля advisor-планов.

        Каждые ADVISOR_PORTFOLIO_WATCH_INTERVAL_SEC (default 300s) проходит по
        всем активным портфельным позициям (is_portfolio=1, status=active),
        пуллит текущие цены через web_search.fetch_realtime_prices, проверяет
        SL/TP-триггеры. Если хит — закрывает позицию (status → stopped/tp1/
        tp2/tp3, пересчитывает PnL) и шлёт алерт юзеру.

        Не торгует — только мониторит план юзера. Per AGENTS.md, торговая
        логика автотрейдера (`signal_trader.py`) НЕ затронута.
        """
        # Стартуем с задержкой чтобы дать другим loop'ам подняться.
        await asyncio.sleep(90)
        interval = int(os.getenv("ADVISOR_PORTFOLIO_WATCH_INTERVAL_SEC", "300"))
        interval = max(60, min(3600, interval))

        while self._running:
            try:
                await self._run_advisor_portfolio_scan()
            except Exception as e:
                logger.error(f"Advisor portfolio watcher error: {e}")

            await asyncio.sleep(interval)

    async def _run_advisor_portfolio_scan(self) -> int:
        """Один проход watcher'а. Returns count of closed positions."""
        try:
            from refactor.providers.advisor_storage import (
                check_close_trigger,
                close_plan,
                list_all_active,
            )
        except ImportError:
            return 0

        plans = await list_all_active()
        if not plans:
            return 0

        # Pull current prices once per scan (one batch fetch).
        try:
            from web_search import fetch_realtime_prices

            prices = await fetch_realtime_prices()
        except Exception as e:
            logger.warning("portfolio watcher: fetch_realtime_prices failed: %s", e)
            return 0

        closed_count = 0
        for plan in plans:
            block = prices.get(plan.asset.upper()) or {}
            current_price = block.get("price")
            if not isinstance(current_price, (int, float)) or current_price <= 0:
                continue
            trigger = check_close_trigger(plan, float(current_price))
            if trigger is None:
                continue
            new_status, reason = trigger
            try:
                closed = await close_plan(
                    plan.id, new_status=new_status,
                    close_price=float(current_price), close_reason=reason,
                )
            except Exception as e:
                logger.warning("portfolio watcher: close_plan failed: %s", e)
                continue
            if closed is None:
                continue
            closed_count += 1
            await self._send_advisor_close_alert(closed, reason)
        return closed_count

    async def _send_advisor_close_alert(self, closed_plan, reason: str) -> None:
        """Notify user that their advisor position auto-closed."""
        try:
            direction = closed_plan.direction or "—"
            emoji = "🟢" if (closed_plan.pnl_pct or 0) >= 0 else "🔴"
            pnl_pct_str = (
                f"{closed_plan.pnl_pct:+.2f}%" if closed_plan.pnl_pct is not None else "—"
            )
            pnl_usd_str = (
                f"${closed_plan.pnl_usd:+,.2f}" if closed_plan.pnl_usd is not None else ""
            )
            close_price_str = (
                f"{closed_plan.close_price:,.4g}" if closed_plan.close_price else "—"
            )
            text = (
                f"📂 *Advisor portfolio* — позиция закрыта\n\n"
                f"#{closed_plan.id} {direction} *{closed_plan.asset}*\n"
                f"Причина: {reason}\n"
                f"Цена закрытия: {close_price_str}\n"
                f"PnL: {emoji} {pnl_pct_str} {pnl_usd_str}"
            )
            await self.bot.send_message(
                chat_id=closed_plan.user_id, text=text, parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("portfolio watcher: alert send failed: %s", e)

    async def _post_mortem_loop(self):
        """Каждый день в POST_MORTEM_RUN_TIME_UTC (дефолт 23:50) — анализ
        вчерашнего дайджеста.  Цикл проверяет минуту, запускает один раз,
        логирует hit-rate и пишет labels в predictions.

        Дайджест публикуется по подписке в произвольный sub_time, поэтому мы
        не привязываемся к нему, а используем «самый свежий дайджест ≥24ч
        назад» (логика в `core.post_mortem.pick_target_digest`).
        """
        await asyncio.sleep(240)  # ждём 4 минуты при старте, не толкаемся с auto_tracker

        while self._running:
            try:
                now = datetime.now()
                current_time = now.strftime("%H:%M")

                if current_time == POST_MORTEM_RUN_TIME_UTC:
                    logger.info("🔬 Запускаю пост-мортем дайджеста (24h post-mortem)...")

                    report = await _run_post_mortem()
                    if report is None:
                        logger.info("post_mortem: нет подходящего дайджеста — пропускаю")
                    else:
                        stats = report.stats
                        hr = stats.get("hit_rate")
                        hr_str = f"{hr * 100:.1f}%" if hr is not None else "—"
                        logger.info(
                            "✅ Post-mortem дайджеста %s: hit-rate=%s "
                            "(wins=%d / resolved=%d, flat=%d, no_data=%d)",
                            report.digest_date,
                            hr_str,
                            stats["wins"],
                            stats["resolved"],
                            stats["flat"],
                            stats["no_data"],
                        )

                    # Ждём минуту чтобы не запустить дважды.
                    await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"Post-mortem loop error: {e}")

            await asyncio.sleep(60)

    async def _agent_calibration_loop(self):
        """Каждые 30 минут резолвит «созревшие» прогнозы агентов.

        Используется только если FEATURE_AGENT_CALIBRATION=1. Внутри:
          1. Лениво создаёт MarketDataChain (Binance → Yahoo fallback).
          2. evaluate_pending_predictions() сам читает pending из БД,
             фетчит реализованные цены, считает Brier, помечает resolved.
          3. Кеширование цены on a per-asset basis внутри одной итерации
             избегает повторных HTTP-запросов.

        Loop устойчив к ошибкам price-провайдеров — failed → skip строки.
        """
        # Лениво импортируем market provider — он зависит от aiohttp, который
        # есть в unit-fast, но сам класс может ругнуться при инициализации
        # в полностью пустом окружении.
        try:
            from refactor.providers.market_providers import MarketDataChain  # noqa: PLC0415
        except Exception as e:
            logger.error(
                "Agent calibration loop отключён: MarketDataChain недоступен (%s)",
                e,
            )
            return

        chain = MarketDataChain()

        async def _fetch_price(asset: str) -> float | None:
            try:
                data = await chain.get_price(asset)
                return float(data.price) if data and data.price else None
            except Exception as e:
                logger.warning("Calib price fetch для %s упал: %s", asset, e)
                return None

        # Sleep на запуске чтобы не толкаться с другими loop'ами.
        await asyncio.sleep(120)

        while self._running:
            try:
                result = await _evaluate_agent_predictions(
                    price_fetcher=_fetch_price,
                    max_per_run=50,
                )
                if result.resolved or result.skipped or result.failed:
                    logger.info(
                        "📊 Agent calib resolve: resolved=%d skipped=%d failed=%d",
                        result.resolved, result.skipped, result.failed,
                    )
            except Exception as e:
                logger.error("Agent calibration loop error: %s", e)
            await asyncio.sleep(30 * 60)  # 30 минут

        # graceful shutdown
        try:
            await chain.binance.close()
        except Exception:
            pass

    async def _microstructure_loop(self):
        """Каждые `MICROSTRUCTURE_INTERVAL_SEC` секунд снимает L2-стакан с
        Binance/Bybit/OKX/Bitget/Hyperliquid и пишет агрегированный snapshot
        в `microstructure_snapshots`. Запускается только если
        FEATURE_MICROSTRUCTURE=1.

        Внутри:
          1. Лениво подключаем aiohttp.ClientSession (одну на loop).
          2. Для каждого asset из MICROSTRUCTURE_SYMBOLS:
             a. compute_microstructure_signal(...) → MicrostructureSignal.
             b. persist_signal в БД.
          3. Если vacuum_flag — логируем WARNING (через logger), будущий PR
             прокинет в alert_system.

        Loop устойчив к ошибкам venue: одна биржа упала — остальные продолжают.
        """
        try:
            import aiohttp  # noqa: PLC0415 — local import: optional dep
        except ImportError:
            logger.error("Microstructure loop отключён: aiohttp недоступен")
            return

        symbols = get_symbols()
        interval = get_interval_seconds()

        if not symbols:
            logger.warning("Microstructure loop: MICROSTRUCTURE_SYMBOLS пуст, exit")
            return

        # Sleep чтобы не толкаться с другими loop'ами на старте.
        await asyncio.sleep(90)

        session = aiohttp.ClientSession()
        try:
            http_client = await make_aiohttp_http_client(session)

            while self._running:
                started = asyncio.get_event_loop().time()
                for asset in symbols:
                    try:
                        signal = await compute_microstructure_signal(
                            asset=asset,
                            http_client=http_client,
                            baseline_provider=get_baseline_depth,
                        )
                        if signal is None:
                            logger.info(
                                "🌊 microstructure %s: нет данных ни от одного venue",
                                asset,
                            )
                            continue
                        await _persist_microstructure_signal(signal)
                        if signal.vacuum:
                            logger.warning(
                                "⚠️ microstructure VACUUM %s: drop=%.1f%% "
                                "(depth=$%.0fM, baseline=$%.0fM, bias=%+d, sev=%.2f)",
                                asset,
                                float(signal.drop_pct_observed or 0.0),
                                signal.aggregate.total_depth_usd() / 1e6,
                                (signal.baseline_depth_usd or 0.0) / 1e6,
                                signal.direction_bias,
                                signal.severity,
                            )
                        else:
                            logger.info(
                                "🌊 microstructure %s: bias=%+d sev=%.2f "
                                "depth=$%.0fM spread=%.2fbps venues=%d",
                                asset,
                                signal.direction_bias,
                                signal.severity,
                                signal.aggregate.total_depth_usd() / 1e6,
                                signal.aggregate.quoted_spread_bps_weighted,
                                signal.aggregate.venue_count,
                            )
                    except Exception as e:  # noqa: BLE001 — per-asset isolation
                        logger.warning(
                            "microstructure loop: %s провалился: %s", asset, e
                        )

                elapsed = asyncio.get_event_loop().time() - started
                await asyncio.sleep(max(5, interval - int(elapsed)))
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def _funding_term_loop(self):
        """Каждые `FUNDING_TERM_INTERVAL_SEC` секунд собирает funding rate term
        structure (spot perp funding + 30d / 90d basis carry) с Bybit и Binance,
        пишет в `funding_term_snapshots` и логирует inversion onset/recovery
        события. Запускается только если FEATURE_FUNDING_TERM=1.

        Per-asset error isolation: один упавший asset не блокирует loop.
        """
        try:
            import aiohttp  # noqa: PLC0415 — local import: optional dep
        except ImportError:
            logger.error("Funding term loop отключён: aiohttp недоступен")
            return

        symbols = _funding_term_get_symbols()
        interval = _funding_term_get_interval_seconds()
        if not symbols:
            logger.warning("Funding term loop: FUNDING_TERM_SYMBOLS пуст, exit")
            return

        await asyncio.sleep(120)  # stagger от других loop'ов на старте

        session = aiohttp.ClientSession()
        try:
            http_client = await _make_funding_term_http_client(session)

            while self._running:
                started = asyncio.get_event_loop().time()
                for asset in symbols:
                    try:
                        prev = await _funding_term_get_previous_signal(asset=asset)
                        current = await _fetch_term_structure(
                            asset=asset, http_client=http_client,
                        )
                        await _persist_funding_term_signal(current)

                        event = detect_inversion_event(
                            current=current, previous=prev,
                        )
                        if event == "inversion_onset":
                            logger.warning(
                                "⚠️ %s",
                                format_term_summary(current, event=event),
                            )
                        elif event == "inversion_recovery":
                            logger.info(
                                "✅ %s",
                                format_term_summary(current, event=event),
                            )
                        else:
                            logger.info(
                                "%s", format_term_summary(current),
                            )
                    except Exception as e:  # noqa: BLE001 — per-asset isolation
                        logger.warning(
                            "funding term loop: %s провалился: %s", asset, e,
                        )

                elapsed = asyncio.get_event_loop().time() - started
                await asyncio.sleep(max(10, interval - int(elapsed)))
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def _options_skew_loop(self):
        """Каждые `OPTIONS_SKEW_INTERVAL_SEC` секунд собирает options skew
        (Deribit): ATM IV near/far и 25Δ risk-reversal, пишет в
        `options_skew_snapshots` и логирует put_skew/call_skew onset/recovery.
        Запускается только если FEATURE_OPTIONS_SKEW=1.

        Per-currency error isolation: одна валюта не блокирует loop.
        """
        try:
            import aiohttp  # noqa: PLC0415 — local import: optional dep
        except ImportError:
            logger.error("Options skew loop отключён: aiohttp недоступен")
            return

        currencies = _options_skew_get_currencies()
        interval = _options_skew_get_interval_seconds()
        if not currencies:
            logger.warning("Options skew loop: OPTIONS_SKEW_SYMBOLS пуст, exit")
            return

        await asyncio.sleep(150)  # stagger от других loop'ов на старте

        session = aiohttp.ClientSession()
        try:
            http_client = await _make_options_skew_http_client(session)

            while self._running:
                started = asyncio.get_event_loop().time()
                for currency in currencies:
                    try:
                        prev = await _options_skew_get_previous_signal(currency=currency)
                        current = await _fetch_options_skew(
                            currency=currency, http_client=http_client,
                        )
                        await _persist_options_skew_signal(current)

                        event = detect_skew_event(current=current, previous=prev)
                        if event in {"put_skew_onset", "call_skew_onset"}:
                            logger.warning(
                                "⚠️ %s",
                                format_skew_summary(current, event=event),
                            )
                        elif event in {"put_skew_recovery", "call_skew_recovery"}:
                            logger.info(
                                "🟢 %s",
                                format_skew_summary(current, event=event),
                            )
                        else:
                            logger.info(
                                "%s", format_skew_summary(current),
                            )
                    except Exception as e:  # noqa: BLE001 — per-currency isolation
                        logger.warning(
                            "options skew loop: %s провалился: %s", currency, e,
                        )

                elapsed = asyncio.get_event_loop().time() - started
                await asyncio.sleep(max(10, interval - int(elapsed)))
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def _stablecoin_flows_loop(self):
        """Каждые `STABLECOIN_FLOWS_INTERVAL_SEC` секунд снимает totalSupply
        стейблов (USDT/USDC) per-chain (Ethereum через Etherscan, Tron через
        Tronscan), пишет supply_snapshots + flow_snapshots с delta_24h vs
        предыдущим окном. Запускается только если FEATURE_STABLECOIN_FLOWS=1.

        Per-token error isolation: один токен не блокирует loop.
        ETHERSCAN_API_KEY обязателен только для ethereum chain — без него
        работает только Tron-часть.
        """
        try:
            import aiohttp  # noqa: PLC0415 — local import: optional dep
        except ImportError:
            logger.error("Stablecoin flows loop отключён: aiohttp недоступен")
            return

        tokens = _stablecoin_get_tokens()
        interval = _stablecoin_get_interval_seconds()
        etherscan_key = _stablecoin_get_etherscan_key()
        if not tokens:
            logger.warning(
                "Stablecoin flows loop: STABLECOIN_FLOWS_TOKENS пуст, exit",
            )
            return
        if etherscan_key is None:
            logger.warning(
                "Stablecoin flows loop: ETHERSCAN_API_KEY не задан, "
                "ethereum chain будет пропущен (только Tron source активен).",
            )

        await asyncio.sleep(180)  # stagger от других loop'ов на старте

        session = aiohttp.ClientSession()
        try:
            http_client = await _make_stablecoin_http_client(session)

            while self._running:
                started = asyncio.get_event_loop().time()
                for token in tokens:
                    try:
                        snapshots = await _fetch_stablecoin_snapshots(
                            token=token,
                            http_client=http_client,
                            etherscan_api_key=etherscan_key,
                        )
                        if not snapshots:
                            logger.warning(
                                "stablecoin loop: %s — пусто, skip", token,
                            )
                            continue

                        await _persist_stablecoin_supply_snapshots(snapshots)

                        previous_supply_usd = await _stablecoin_get_previous_supply_usd(
                            token=token, hours_ago=24.0,
                        )
                        timestamp_ms = snapshots[0].timestamp_ms
                        current_flow = _build_stablecoin_flow_signal(
                            token=token,
                            current_snapshots=snapshots,
                            previous_supply_usd=previous_supply_usd,
                            timestamp_ms=timestamp_ms,
                        )

                        prev_flow = await _stablecoin_get_previous_flow_signal(token=token)
                        await _persist_stablecoin_flow_signal(current_flow)

                        event = _detect_stablecoin_flow_event(
                            current=current_flow, previous=prev_flow,
                        )
                        summary = _format_stablecoin_flow_summary(
                            current_flow, event=event,
                        )
                        if event in {"mint_burst", "redeem_burst"}:
                            logger.warning("⚠️ %s", summary)
                        elif event in {"mint_cooldown", "redeem_cooldown"}:
                            logger.info("🟢 %s", summary)
                        else:
                            logger.info("%s", summary)
                    except Exception as e:  # noqa: BLE001 — per-token isolation
                        logger.warning(
                            "stablecoin flows loop: %s провалился: %s", token, e,
                        )

                elapsed = asyncio.get_event_loop().time() - started
                await asyncio.sleep(max(10, interval - int(elapsed)))
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def _narrative_drift_loop(self):
        """Каждый час подтягивает свежие новости через TavilyProvider (если
        ключ задан), embed'ит через Gemini/Mistral, кластеризует онлайн и
        пишет в narrative_documents/narrative_clusters. Раз в сутки также
        прогоняет cleanup старых документов.

        Под фичефлагом FEATURE_NARRATIVE_DRIFT — loop регистрируется только
        если включён. Без новых deps в requirements.txt.
        """
        try:
            import aiohttp  # noqa: PLC0415
        except ImportError:
            logger.error("Narrative drift loop отключён: aiohttp недоступен")
            return

        from datetime import datetime  # noqa: PLC0415

        tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not tavily_key:
            logger.warning(
                "Narrative drift: TAVILY_API_KEY не задан — "
                "источник документов отсутствует, loop пропускается"
            )
            return

        await asyncio.sleep(180)  # сдвиг от стартовых loop'ов

        interval = _narrative_get_interval_seconds()
        retention = _narrative_get_retention_days()
        db_adapter = SqliteNarrativeDBAdapter()
        last_cleanup_day: str | None = None

        session = aiohttp.ClientSession()
        try:
            embedding_client = make_embedding_client(http_session=session)

            from refactor.providers.news_providers import TavilyProvider  # noqa: PLC0415
            from market_indicators.narratives_io import (  # noqa: PLC0415
                NarrativeDocument,
                classify_asset_hint,
                make_doc_id,
            )

            tavily = TavilyProvider(api_key=tavily_key)
            tavily.session = session  # переиспользуем уже открытый session

            queries = [
                ("BTC", "Bitcoin ETF flows on-chain whales price news"),
                ("ETH", "Ethereum spot ETF restaking layer-2 news"),
                (None, "crypto market liquidations regulation macro"),
            ]

            while self._running:
                started = asyncio.get_event_loop().time()
                aggregated_docs: list = []

                for asset, query in queries:
                    try:
                        articles = await tavily.search_news(
                            query=query, max_results=15, search_depth="basic",
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("narrative tavily %r упал: %s", query, e)
                        continue

                    for art in articles:
                        try:
                            published = art.publish_date or datetime.utcnow()
                            asset_hint = (
                                asset
                                or classify_asset_hint(
                                    art.title or "", art.content or ""
                                )
                            )
                            doc = NarrativeDocument(
                                doc_id=make_doc_id(
                                    source="tavily",
                                    url=art.url or "",
                                    title=art.title or "",
                                ),
                                source="tavily",
                                title=art.title or "",
                                content=art.content or "",
                                published_at=published,
                                asset_hint=asset_hint,
                            )
                            aggregated_docs.append(doc)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("narrative parse failed: %s", e)
                            continue

                if aggregated_docs:
                    try:
                        result = await ingest_documents(
                            docs=aggregated_docs,
                            embedding_client=embedding_client,
                            db_adapter=db_adapter,
                        )
                        logger.info(
                            "🌐 narrative: ingest %d docs "
                            "(skip_dup=%d, new=%d, joined=%d, drift=%d)",
                            result.docs_processed, result.docs_skipped_dup,
                            result.new_clusters, result.joined_existing,
                            len(result.drift_events),
                        )
                        for drift in result.drift_events:
                            logger.warning(format_drift_summary(drift))
                    except Exception as e:  # noqa: BLE001
                        logger.exception("narrative ingest_documents упал: %s", e)
                else:
                    logger.info("🌐 narrative: новых документов нет")

                # Daily cleanup (раз в сутки)
                today = datetime.utcnow().strftime("%Y-%m-%d")
                if last_cleanup_day != today:
                    try:
                        from database import cleanup_old_narrative_data  # noqa: PLC0415
                        deleted = await cleanup_old_narrative_data(retention_days=retention)
                        logger.info(
                            "🌐 narrative cleanup: удалено %d строк (retention=%dd)",
                            deleted, retention,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("narrative cleanup упал: %s", e)
                    last_cleanup_day = today

                elapsed = asyncio.get_event_loop().time() - started
                await asyncio.sleep(max(60, interval - int(elapsed)))
        finally:
            try:
                await session.close()
            except Exception:
                pass

    async def export_now(self):
        """
        ИСПРАВЛЕНО: метод оставлен для обратной совместимости,
        но теперь НЕ делает ничего чтобы не триггерить Railway деплои.

        Если нужен ручной экспорт — используй /admin команду или
        запусти github_export.py напрямую локально.
        """
        logger.debug("export_now() вызван но пропущен (отключено для предотвращения Railway loop)")
        pass
