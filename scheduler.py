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

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, bot, send_daily_fn, check_predictions_fn):
        self.bot = bot
        self.send_daily = send_daily_fn
        self.check_predictions = check_predictions_fn
        self._running = False
        self._last_export_date: date | None = None
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

    async def export_now(self):
        """
        ИСПРАВЛЕНО: метод оставлен для обратной совместимости,
        но теперь НЕ делает ничего чтобы не триггерить Railway деплои.

        Если нужен ручной экспорт — используй /admin команду или
        запусти github_export.py напрямую локально.
        """
        logger.debug("export_now() вызван но пропущен (отключено для предотвращения Railway loop)")
        pass
