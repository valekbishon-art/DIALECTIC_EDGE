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
from datetime import datetime, date, time, timedelta
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
    from smart_money_alert import SmartMoneyAlertSystem
    SMART_MONEY_ALERT_ENABLED = True
except ImportError:
    SMART_MONEY_ALERT_ENABLED = False

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
        LiquidationClusterRule as _LiquidationClusterRule,
        ScreenerAnomalyRule as _ScreenerAnomalyRule,
    )
    from refactor.services.alert_rules.btc_etf_outflow import feature_enabled as _alert_btc_etf_enabled
    from refactor.services.alert_rules.liquidation_cluster import (
        feature_enabled as _alert_liq_enabled,
    )
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

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, bot, send_daily_fn, check_predictions_fn):
        self.bot = bot
        self.send_daily = send_daily_fn
        self.check_predictions = check_predictions_fn
        self._running = False
        self._last_export_date: date | None = None
        self._last_p2p_alert_keys: dict[str, datetime] = {}
        self._alert_system = None
        self._signals_system = None

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

        self._smart_money_alert = None
        if SMART_MONEY_ALERT_ENABLED:
            try:
                self._smart_money_alert = SmartMoneyAlertSystem(self.bot)
                logger.info("✅ Smart-money alert system инициализирован")
            except Exception as e:
                logger.warning(f"Smart-money alert init error: {e}")

    async def start(self):
        self._running = True
        logger.info("⏰ Scheduler запущен")

        tasks = [
            self._daily_digest_loop(),
            self._prediction_checker_loop(),
            self._midnight_reset_loop(),
            self._daily_github_export_loop(),
        ]

        if ALERT_SYSTEM_ENABLED and self._alert_system:
            tasks.append(self._alert_checker_loop())

        if SIGNALS_SYSTEM_ENABLED and self._signals_system:
            tasks.append(self._signals_checker_loop())

        if AUTO_TRACKER_ENABLED and self._auto_tracker:
            tasks.append(self._auto_tracker_loop())

        if SMART_MONEY_ALERT_ENABLED and self._smart_money_alert:
            tasks.append(self._smart_money_alert_loop())

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

        if ALERT_ENGINE_LOADED and _alert_engine_enabled():
            tasks.append(self._alert_engine_loop())
            active_rules = [
                name for name, ok in (
                    ("screener", _alert_screener_enabled()),
                    ("btc_etf", _alert_btc_etf_enabled()),
                    ("liquidation", _alert_liq_enabled()),
                ) if ok
            ]
            logger.info(
                "🔔 Alert engine включён (interval=%ss, rules=%s)",
                _alert_engine_interval_sec(),
                ",".join(active_rules) or "none",
            )

        await asyncio.gather(*tasks)

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

    async def _smart_money_alert_loop(self):
        """Проверяет smart-money convergence каждый час.

        Шлёт алерт подписчикам сигналов только когда ≥ 2 институциональных
        индикаторов синхронно показывают один же direction. Анти-спам внутри.
        """
        await asyncio.sleep(900)  # ждём 15 минут при старте — пусть система прогреется

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
        for asset in _p2p_get_assets():
            for fiat in _p2p_get_fiats():
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
        if _alert_liq_enabled():
            rules.append(_LiquidationClusterRule.build())

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
