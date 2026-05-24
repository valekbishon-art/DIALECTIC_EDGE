"""I/O tests для cascade_post_mortem_io.

Тестируем:
- env-helpers
- WS-парсеры (Binance / Bybit)
- SQLite persist / load / cleanup
- evaluate_and_maybe_trigger() end-to-end (с in-memory SQLite + mock snapshot collector)
- list / find / get queries
- format_post_mortem_list / format_post_mortem_full
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

# --- Подготавливаем in-process SQLite ДО импорта тестируемого модуля ----------

_tmp_dir = tempfile.mkdtemp(prefix="cpm_test_")
_TEST_DB = os.path.join(_tmp_dir, "test_cpm.db")
os.environ["DB_PATH"] = _TEST_DB

from market_indicators.cascade_post_mortem import (  # noqa: E402
    LiquidationEvent,
    SIDE_LONG,
    SIDE_SHORT,
    WINDOW_TYPE_24H,
    WINDOW_TYPE_4H_ACUTE,
)
from market_indicators.cascade_post_mortem_io import (  # noqa: E402
    binance_enabled,
    bybit_enabled,
    cleanup_old_liquidation_events,
    evaluate_and_maybe_trigger,
    feature_enabled,
    find_cascade_post_mortem_by_date,
    format_post_mortem_full,
    format_post_mortem_list,
    get_agg_interval_seconds,
    get_bybit_symbols,
    get_cascade_post_mortem_by_id,
    get_cooldown_hours,
    get_last_post_mortem_triggered_ms,
    get_retention_days,
    get_threshold_24h_usd,
    get_threshold_4h_acute_usd,
    insert_liquidation_event,
    insert_liquidation_events_batch,
    list_recent_cascade_post_mortems,
    load_recent_liquidation_events,
    mark_post_mortem_posted,
    parse_binance_force_order,
    parse_bybit_liquidation,
    parse_bybit_liquidation_batch,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _init_test_db() -> None:
    """Создаёт нужные нам таблицы (только cascade-post-mortem-related) в test DB."""
    import aiosqlite  # noqa: PLC0415

    async with aiosqlite.connect(_TEST_DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS liquidation_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ms  INTEGER NOT NULL,
                venue         TEXT    NOT NULL,
                symbol        TEXT    NOT NULL,
                side          TEXT    NOT NULL,
                value_usd     REAL    NOT NULL,
                CHECK (side IN ('long', 'short')),
                CHECK (value_usd >= 0)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cascade_post_mortems (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                triggered_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                window_type     TEXT    NOT NULL,
                window_hours    INTEGER NOT NULL,
                total_liq_usd   REAL    NOT NULL,
                long_liq_usd    REAL    NOT NULL DEFAULT 0,
                short_liq_usd   REAL    NOT NULL DEFAULT 0,
                snapshot_json   TEXT    NOT NULL,
                summary_md      TEXT    NOT NULL,
                posted_to_tg    INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS funding_term_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT, timestamp_ms INTEGER,
                spot_funding_annual REAL, monthly_basis_annual REAL,
                quarterly_basis_annual REAL, slope_annual REAL,
                is_inverted INTEGER, venues_csv TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS options_skew_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                currency TEXT, timestamp_ms INTEGER,
                underlying_price REAL, near_expiry_days INTEGER,
                near_atm_iv REAL, near_rr_25d REAL,
                far_expiry_days INTEGER, far_atm_iv REAL,
                far_rr_25d REAL, atm_iv_term_slope REAL,
                skew_class TEXT, venues_csv TEXT
            )
        """)
        await db.commit()


async def _reset_test_db() -> None:
    import aiosqlite  # noqa: PLC0415

    async with aiosqlite.connect(_TEST_DB) as db:
        await db.execute("DELETE FROM liquidation_events")
        await db.execute("DELETE FROM cascade_post_mortems")
        await db.commit()


# Initialize once
_run(_init_test_db())


# ─── Patch DB_PATH в module-scope ────────────────────────────────────────────


def _patch_db_path(target_module_name: str):
    """Patch DB_PATH в указанном модуле."""
    return patch(f"{target_module_name}.DB_PATH", _TEST_DB)


class TestEnvHelpers(unittest.TestCase):
    def setUp(self) -> None:
        # очищаем env
        for k in (
            "FEATURE_CASCADE_POST_MORTEM",
            "POST_MORTEM_THRESHOLD_USD",
            "POST_MORTEM_ACUTE_THRESHOLD_USD",
            "POST_MORTEM_COOLDOWN_HOURS",
            "POST_MORTEM_AGG_INTERVAL_S",
            "POST_MORTEM_RETENTION_DAYS",
            "POST_MORTEM_BINANCE_ENABLED",
            "POST_MORTEM_BYBIT_ENABLED",
            "POST_MORTEM_BYBIT_SYMBOLS",
        ):
            os.environ.pop(k, None)

    def test_feature_disabled_by_default(self) -> None:
        self.assertFalse(feature_enabled())

    def test_feature_enabled_by_env(self) -> None:
        os.environ["FEATURE_CASCADE_POST_MORTEM"] = "1"
        self.assertTrue(feature_enabled())

    def test_thresholds_default(self) -> None:
        self.assertEqual(get_threshold_24h_usd(), 500_000_000.0)
        self.assertEqual(get_threshold_4h_acute_usd(), 200_000_000.0)

    def test_thresholds_overridden_by_env(self) -> None:
        os.environ["POST_MORTEM_THRESHOLD_USD"] = "100000000"
        os.environ["POST_MORTEM_ACUTE_THRESHOLD_USD"] = "50000000"
        self.assertEqual(get_threshold_24h_usd(), 100_000_000.0)
        self.assertEqual(get_threshold_4h_acute_usd(), 50_000_000.0)

    def test_thresholds_minimum_enforced(self) -> None:
        os.environ["POST_MORTEM_THRESHOLD_USD"] = "1"  # below min 1M
        self.assertEqual(get_threshold_24h_usd(), 1_000_000.0)

    def test_cooldown_default(self) -> None:
        self.assertEqual(get_cooldown_hours(), 6)

    def test_cooldown_overridden(self) -> None:
        os.environ["POST_MORTEM_COOLDOWN_HOURS"] = "12"
        self.assertEqual(get_cooldown_hours(), 12)

    def test_cooldown_minimum_one(self) -> None:
        os.environ["POST_MORTEM_COOLDOWN_HOURS"] = "0"
        self.assertEqual(get_cooldown_hours(), 1)

    def test_agg_interval_default(self) -> None:
        self.assertEqual(get_agg_interval_seconds(), 60)

    def test_retention_days_default(self) -> None:
        self.assertEqual(get_retention_days(), 7)

    def test_bybit_symbols_default(self) -> None:
        syms = get_bybit_symbols()
        self.assertIn("BTCUSDT", syms)
        self.assertGreaterEqual(len(syms), 3)

    def test_bybit_symbols_override(self) -> None:
        os.environ["POST_MORTEM_BYBIT_SYMBOLS"] = "BTCUSDT, ETHUSDT"
        self.assertEqual(get_bybit_symbols(), ("BTCUSDT", "ETHUSDT"))

    def test_binance_bybit_subflags_default_on(self) -> None:
        self.assertTrue(binance_enabled())
        self.assertTrue(bybit_enabled())

    def test_binance_subflag_off(self) -> None:
        os.environ["POST_MORTEM_BINANCE_ENABLED"] = "0"
        self.assertFalse(binance_enabled())

    def test_invalid_env_falls_back_to_default(self) -> None:
        os.environ["POST_MORTEM_THRESHOLD_USD"] = "not-a-number"
        self.assertEqual(get_threshold_24h_usd(), 500_000_000.0)


class TestBinanceParser(unittest.TestCase):
    def test_parses_sell_as_long_liquidation(self) -> None:
        payload = {
            "e": "forceOrder",
            "E": 1_700_000_000_000,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "ap": "20000",
                "q": "0.5",
                "T": 1_700_000_000_500,
            },
        }
        ev = parse_binance_force_order(payload)
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.symbol, "BTCUSDT")
        self.assertEqual(ev.side, SIDE_LONG)
        self.assertEqual(ev.value_usd, 10_000.0)
        self.assertEqual(ev.venue, "binance")
        self.assertEqual(ev.timestamp_ms, 1_700_000_000_500)

    def test_parses_buy_as_short_liquidation(self) -> None:
        payload = {
            "o": {
                "s": "ETHUSDT",
                "S": "BUY",
                "ap": "2000",
                "q": "1",
                "T": 1_700_000_000_000,
            }
        }
        ev = parse_binance_force_order(payload)
        assert ev is not None
        self.assertEqual(ev.side, SIDE_SHORT)
        self.assertEqual(ev.value_usd, 2_000.0)

    def test_rejects_empty_payload(self) -> None:
        self.assertIsNone(parse_binance_force_order({}))
        self.assertIsNone(parse_binance_force_order({"o": "not-a-dict"}))

    def test_rejects_unknown_side(self) -> None:
        payload = {
            "o": {
                "s": "BTCUSDT",
                "S": "HOLD",  # invalid
                "ap": "20000",
                "q": "0.5",
                "T": 1_700_000_000_000,
            }
        }
        self.assertIsNone(parse_binance_force_order(payload))

    def test_rejects_zero_price_or_qty(self) -> None:
        for ap, q in [("0", "0.5"), ("20000", "0"), ("20000", "-1")]:
            payload = {
                "o": {
                    "s": "BTCUSDT",
                    "S": "SELL",
                    "ap": ap,
                    "q": q,
                    "T": 1_700_000_000_000,
                }
            }
            self.assertIsNone(parse_binance_force_order(payload))

    def test_rejects_zero_timestamp(self) -> None:
        payload = {
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "ap": "20000",
                "q": "0.5",
                "T": 0,
            }
        }
        self.assertIsNone(parse_binance_force_order(payload))

    def test_falls_back_to_event_time_when_no_T(self) -> None:
        payload = {
            "E": 1_700_000_000_000,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "ap": "20000",
                "q": "0.5",
            },
        }
        ev = parse_binance_force_order(payload)
        assert ev is not None
        self.assertEqual(ev.timestamp_ms, 1_700_000_000_000)


class TestBybitParser(unittest.TestCase):
    def test_parses_single_event(self) -> None:
        payload = {
            "topic": "allLiquidation.BTCUSDT",
            "ts": 1_700_000_000_000,
            "data": [
                {
                    "T": 1_700_000_000_500,
                    "s": "BTCUSDT",
                    "S": "Sell",
                    "v": "0.5",
                    "p": "20000",
                }
            ],
        }
        ev = parse_bybit_liquidation(payload)
        assert ev is not None
        self.assertEqual(ev.symbol, "BTCUSDT")
        self.assertEqual(ev.side, SIDE_LONG)
        self.assertEqual(ev.value_usd, 10_000.0)
        self.assertEqual(ev.venue, "bybit")

    def test_parses_batch(self) -> None:
        payload = {
            "topic": "allLiquidation.BTCUSDT",
            "ts": 1_700_000_000_000,
            "data": [
                {
                    "T": 1_700_000_000_001,
                    "s": "BTCUSDT",
                    "S": "Sell",
                    "v": "0.5",
                    "p": "20000",
                },
                {
                    "T": 1_700_000_000_002,
                    "s": "BTCUSDT",
                    "S": "Buy",
                    "v": "1.0",
                    "p": "20000",
                },
            ],
        }
        events = parse_bybit_liquidation_batch(payload)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].side, SIDE_LONG)
        self.assertEqual(events[1].side, SIDE_SHORT)
        self.assertEqual(events[1].value_usd, 20_000.0)

    def test_rejects_empty_data(self) -> None:
        self.assertEqual(parse_bybit_liquidation_batch({"data": []}), [])
        self.assertEqual(parse_bybit_liquidation_batch({}), [])

    def test_rejects_malformed_items(self) -> None:
        payload = {
            "data": [
                {"s": "BTCUSDT"},  # missing fields
                "not-a-dict",
                {"s": "BTCUSDT", "S": "Sell", "v": "0", "p": "20000", "T": 1},
            ]
        }
        self.assertEqual(parse_bybit_liquidation_batch(payload), [])


class TestPersistAndLoad(unittest.TestCase):
    def setUp(self) -> None:
        _run(_reset_test_db())

    def _ev(self, **kw) -> LiquidationEvent:
        return LiquidationEvent(
            timestamp_ms=kw.get("ts", 1_700_000_000_000),
            venue=kw.get("venue", "binance"),
            symbol=kw.get("symbol", "BTCUSDT"),
            side=kw.get("side", SIDE_LONG),
            value_usd=kw.get("value", 1_000_000),
        )

    def test_insert_and_load_single_event(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                await insert_liquidation_event(self._ev())
                events = await load_recent_liquidation_events(
                    now_ms=1_700_000_000_100, lookback_seconds=3600
                )
                return events

        events = _run(run())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].venue, "binance")

    def test_batch_insert(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                count = await insert_liquidation_events_batch([
                    self._ev(ts=1_700_000_000_000),
                    self._ev(ts=1_700_000_000_100),
                ])
                events = await load_recent_liquidation_events(
                    now_ms=1_700_000_000_200, lookback_seconds=3600
                )
                return count, events

        count, events = _run(run())
        self.assertEqual(count, 2)
        self.assertEqual(len(events), 2)

    def test_empty_batch_returns_zero(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await insert_liquidation_events_batch([])

        self.assertEqual(_run(run()), 0)

    def test_load_excludes_old_events(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                # старый event (вне окна)
                old_ts = 1_700_000_000_000
                # новый (в окне)
                new_ts = old_ts + 3700 * 1000  # позже на час+
                await insert_liquidation_event(self._ev(ts=old_ts))
                await insert_liquidation_event(self._ev(ts=new_ts))
                # окно 60 минут
                events = await load_recent_liquidation_events(
                    now_ms=new_ts, lookback_seconds=3600
                )
                return events

        events = _run(run())
        self.assertEqual(len(events), 1)

    def test_cleanup_removes_old(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                from datetime import datetime, timezone

                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                # event 10 дней назад
                old_ts = now_ms - 10 * 24 * 3600 * 1000
                await insert_liquidation_event(self._ev(ts=old_ts))
                # event 1 день назад
                fresh_ts = now_ms - 24 * 3600 * 1000
                await insert_liquidation_event(self._ev(ts=fresh_ts))
                deleted = await cleanup_old_liquidation_events(retention_days=7)
                events = await load_recent_liquidation_events(
                    now_ms=now_ms, lookback_seconds=15 * 24 * 3600
                )
                return deleted, events

        deleted, events = _run(run())
        self.assertEqual(deleted, 1)
        self.assertEqual(len(events), 1)


class TestEvaluateAndTrigger(unittest.TestCase):
    def setUp(self) -> None:
        _run(_reset_test_db())

    def _seed_events(self, *, total_usd: float, now_ms: int, span_h: float = 1.0):
        """Создаёт N событий чтобы дать сумму total_usd за span_h часов."""
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                # 100 events
                per_event = total_usd / 100
                ms_per_event = int(span_h * 3600 * 1000 / 100)
                events = []
                for i in range(100):
                    events.append(LiquidationEvent(
                        timestamp_ms=now_ms - (99 - i) * ms_per_event,
                        venue="binance",
                        symbol="BTCUSDT",
                        side=SIDE_LONG,
                        value_usd=per_event,
                    ))
                await insert_liquidation_events_batch(events)
        _run(run())

    def test_no_trigger_below_threshold(self) -> None:
        now = 1_700_000_000_000
        self._seed_events(total_usd=100_000_000, now_ms=now)

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await evaluate_and_maybe_trigger(
                    now_ms=now,
                    threshold_24h_usd=500_000_000,
                    threshold_4h_usd=200_000_000,
                    snapshot_collector=lambda: _async_dict({}),
                )

        self.assertIsNone(_run(run()))

    def test_trigger_on_24h_writes_post_mortem(self) -> None:
        now = 1_700_000_000_000
        self._seed_events(total_usd=600_000_000, now_ms=now, span_h=23)

        async def fake_snap():
            return {"liquidation_magnet": {"label": "down_magnet"}}

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                result = await evaluate_and_maybe_trigger(
                    now_ms=now,
                    threshold_24h_usd=500_000_000,
                    threshold_4h_usd=200_000_000,
                    snapshot_collector=fake_snap,
                )
                rows = await list_recent_cascade_post_mortems(limit=5)
                return result, rows

        result, rows = _run(run())
        self.assertIsNotNone(result)
        assert result is not None
        snapshot, summary, pm_id = result
        self.assertGreater(pm_id, 0)
        self.assertIn("Cascade", summary)
        self.assertIn("DOWN", summary)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["window_type"], WINDOW_TYPE_24H)

    def test_trigger_on_4h_acute_priority(self) -> None:
        now = 1_700_000_000_000
        # 600M в 3.5 часа — 4h acute должен сработать
        self._seed_events(total_usd=600_000_000, now_ms=now, span_h=3.5)

        async def fake_snap():
            return {}

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await evaluate_and_maybe_trigger(
                    now_ms=now,
                    threshold_24h_usd=500_000_000,
                    threshold_4h_usd=200_000_000,
                    snapshot_collector=fake_snap,
                )

        result = _run(run())
        self.assertIsNotNone(result)
        assert result is not None
        snapshot, summary, pm_id = result
        self.assertEqual(snapshot.window.window_type, WINDOW_TYPE_4H_ACUTE)

    def test_cooldown_blocks_repeat_trigger(self) -> None:
        now = 1_700_000_000_000
        self._seed_events(total_usd=600_000_000, now_ms=now, span_h=23)

        async def fake_snap():
            return {}

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                r1 = await evaluate_and_maybe_trigger(
                    now_ms=now,
                    threshold_24h_usd=500_000_000,
                    threshold_4h_usd=200_000_000,
                    cooldown_hours=6,
                    snapshot_collector=fake_snap,
                )
                # Сразу повторно — должен быть заблокирован cooldown'ом
                r2 = await evaluate_and_maybe_trigger(
                    now_ms=now + 60 * 1000,
                    threshold_24h_usd=500_000_000,
                    threshold_4h_usd=200_000_000,
                    cooldown_hours=6,
                    snapshot_collector=fake_snap,
                )
                return r1, r2

        r1, r2 = _run(run())
        self.assertIsNotNone(r1)
        self.assertIsNone(r2)


class TestPostMortemQueries(unittest.TestCase):
    def setUp(self) -> None:
        _run(_reset_test_db())

    def _seed_and_trigger(self, *, total_usd: float = 600_000_000):
        now = 1_700_000_000_000
        per = total_usd / 100

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                events = [
                    LiquidationEvent(
                        timestamp_ms=now - (99 - i) * 60_000,
                        venue="binance",
                        symbol="BTCUSDT",
                        side=SIDE_LONG,
                        value_usd=per,
                    )
                    for i in range(100)
                ]
                await insert_liquidation_events_batch(events)
                return await evaluate_and_maybe_trigger(
                    now_ms=now,
                    threshold_24h_usd=500_000_000,
                    threshold_4h_usd=2_000_000_000,  # disable 4h
                    snapshot_collector=lambda: _async_dict({}),
                )

        return _run(run())

    def test_list_recent_post_mortems(self) -> None:
        self._seed_and_trigger()

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await list_recent_cascade_post_mortems(limit=5)

        rows = _run(run())
        self.assertEqual(len(rows), 1)
        self.assertIn("total_liq_usd", rows[0])

    def test_get_post_mortem_by_id(self) -> None:
        result = self._seed_and_trigger()
        assert result is not None
        _, _, pm_id = result

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await get_cascade_post_mortem_by_id(pm_id)

        row = _run(run())
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("summary_md", row)

    def test_get_post_mortem_by_id_not_found(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await get_cascade_post_mortem_by_id(99999)

        self.assertIsNone(_run(run()))

    def test_mark_post_mortem_posted(self) -> None:
        result = self._seed_and_trigger()
        assert result is not None
        _, _, pm_id = result

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                await mark_post_mortem_posted(pm_id)
                return await get_cascade_post_mortem_by_id(pm_id)

        row = _run(run())
        assert row is not None
        self.assertEqual(row["posted_to_tg"], 1)

    def test_find_by_date(self) -> None:
        self._seed_and_trigger()

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                # достаём первую запись чтобы узнать дату
                rows = await list_recent_cascade_post_mortems(limit=1)
                date_iso = str(rows[0]["triggered_at"])[:10]
                row = await find_cascade_post_mortem_by_date(date_iso)
                return row

        row = _run(run())
        self.assertIsNotNone(row)

    def test_find_by_date_not_found(self) -> None:
        self._seed_and_trigger()

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await find_cascade_post_mortem_by_date("1999-01-01")

        self.assertIsNone(_run(run()))

    def test_find_by_date_invalid_format(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await find_cascade_post_mortem_by_date("not-a-date")

        # Не должен крашиться, возвращает None или пустую запись
        result = _run(run())
        self.assertIsNone(result)

    def test_last_triggered_ms_returns_none_when_empty(self) -> None:
        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await get_last_post_mortem_triggered_ms()

        self.assertIsNone(_run(run()))

    def test_last_triggered_ms_returns_after_persist(self) -> None:
        self._seed_and_trigger()

        async def run():
            with _patch_db_path("market_indicators.cascade_post_mortem_io"):
                return await get_last_post_mortem_triggered_ms()

        ms = _run(run())
        self.assertIsNotNone(ms)
        assert ms is not None
        self.assertGreater(ms, 0)


class TestFormatters(unittest.TestCase):
    def test_format_empty_list(self) -> None:
        text = format_post_mortem_list([])
        self.assertIn("не было", text)

    def test_format_non_empty_list(self) -> None:
        rows = [
            {
                "id": 1,
                "triggered_at": "2025-05-21 08:00:00",
                "window_type": WINDOW_TYPE_24H,
                "total_liq_usd": 600_000_000,
            },
            {
                "id": 2,
                "triggered_at": "2025-05-22 14:30:00",
                "window_type": WINDOW_TYPE_4H_ACUTE,
                "total_liq_usd": 300_000_000,
            },
        ]
        text = format_post_mortem_list(rows)
        self.assertIn("Recent cascade", text)
        self.assertIn("#1", text)
        self.assertIn("$600M", text)
        self.assertIn("$300M", text)
        self.assertIn("24h", text)
        self.assertIn("4h", text)

    def test_format_full_returns_summary(self) -> None:
        row = {"summary_md": "🔥 hello world"}
        self.assertEqual(format_post_mortem_full(row), "🔥 hello world")

    def test_format_full_empty_row(self) -> None:
        self.assertIn("не найден", format_post_mortem_full({}))

    def test_format_full_none(self) -> None:
        # type: ignore — намеренно None
        self.assertIn("не найден", format_post_mortem_full(None))  # type: ignore[arg-type]


# helper для async-mock
async def _async_dict(d: dict) -> dict:
    return d


if __name__ == "__main__":
    unittest.main()
