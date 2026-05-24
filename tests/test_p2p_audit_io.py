"""Integration tests for p2p_audit_io (SQLite + backcheck loop)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

from p2p_arbitrage import P2PAdvert, P2POpportunity, opportunity_key
from p2p_audit import (
    STATUS_AMPLIFIED,
    STATUS_CONFIRMED,
    STATUS_DECAYED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_VANISHED,
)


def _ad(*, trade_type="BUY", price=100.0, asset="USDT", fiat="RUB", venue="Binance P2P"):
    return P2PAdvert(
        venue=venue,
        trade_type=trade_type,
        asset=asset,
        fiat=fiat,
        price=price,
        min_amount_fiat=1000.0,
        max_amount_fiat=50_000.0,
        is_merchant=True,
        completed_orders=200,
        completion_rate_pct=99.0,
    )


def _opp(buy, sell, *, gross=2.0, net=1.5, asset="USDT", fiat="RUB"):
    return P2POpportunity(
        asset=asset,
        fiat=fiat,
        buy_ad=buy,
        sell_ad=sell,
        gross_spread_pct=gross,
        buffer_pct=gross - net,
        net_spread_pct=net,
        executable_fiat=10_000.0,
        executable_asset=100.0,
        shared_payment_methods=("sber",),
        risk_level="LOW",
    )


class _AuditDBContext:
    """Spin up a tmp SQLite, patch DB_PATH everywhere, ensure table exists."""

    def __init__(self):
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self._tmp.close()
        self.path = self._tmp.name
        self._patchers = []

    async def __aenter__(self):
        import p2p_audit_io

        for mod in ("p2p_audit_io",):
            p = mock.patch.object(__import__(mod), "DB_PATH", self.path)
            p.start()
            self._patchers.append(p)
        await p2p_audit_io.ensure_audit_table_exists(self.path)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for p in self._patchers:
            p.stop()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class TestEnvHelpers(unittest.TestCase):
    def test_feature_disabled_by_default(self):
        from p2p_audit import feature_enabled

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FEATURE_P2P_SELF_AUDIT", None)
            self.assertFalse(feature_enabled())

    def test_feature_enabled_via_env(self):
        from p2p_audit import feature_enabled

        with mock.patch.dict(os.environ, {"FEATURE_P2P_SELF_AUDIT": "1"}):
            self.assertTrue(feature_enabled())

    def test_backcheck_delay_default(self):
        from p2p_audit import get_backcheck_delay_min

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("P2P_AUDIT_BACKCHECK_DELAY_MIN", None)
            self.assertEqual(get_backcheck_delay_min(), 60)

    def test_backcheck_delay_clamped(self):
        from p2p_audit import get_backcheck_delay_min

        with mock.patch.dict(os.environ, {"P2P_AUDIT_BACKCHECK_DELAY_MIN": "0"}):
            self.assertEqual(get_backcheck_delay_min(), 60)
        with mock.patch.dict(os.environ, {"P2P_AUDIT_BACKCHECK_DELAY_MIN": "120"}):
            self.assertEqual(get_backcheck_delay_min(), 120)


class TestPersistAndLoad(unittest.IsolatedAsyncioTestCase):
    async def test_persist_then_load(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                load_recent_audit_records,
                persist_opportunity_for_audit,
            )

            buy = _ad(trade_type="BUY", price=100.0)
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell, gross=2.0, net=1.5)
            row_id = await persist_opportunity_for_audit(opp, shown_at_ms=1_700_000_000_000, db_path=ctx.path)
            self.assertGreater(row_id, 0)

            records = await load_recent_audit_records(db_path=ctx.path)
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertEqual(rec.asset, "USDT")
            self.assertEqual(rec.fiat, "RUB")
            self.assertEqual(rec.buy_price, 100.0)
            self.assertEqual(rec.sell_price, 102.0)
            self.assertEqual(rec.net_spread_pct, 1.5)
            self.assertEqual(rec.risk_level, "LOW")
            self.assertEqual(rec.status, STATUS_PENDING)

    async def test_bulk_persist(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                load_recent_audit_records,
                persist_opportunities_for_audit,
            )

            ops = []
            for i in range(5):
                buy = _ad(trade_type="BUY", price=100.0 + i)
                sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0 + i)
                ops.append(_opp(buy, sell, gross=2.0, net=1.5))
            n = await persist_opportunities_for_audit(ops, db_path=ctx.path)
            self.assertEqual(n, 5)
            records = await load_recent_audit_records(db_path=ctx.path)
            self.assertEqual(len(records), 5)

    async def test_bulk_persist_empty(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import persist_opportunities_for_audit

            n = await persist_opportunities_for_audit([], db_path=ctx.path)
            self.assertEqual(n, 0)

    async def test_load_pending_filters_by_delay(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                load_pending_audit_records,
                persist_opportunity_for_audit,
            )

            now_ms = int(time.time() * 1000)
            buy = _ad(trade_type="BUY")
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell)
            # One older than delay, one too recent.
            await persist_opportunity_for_audit(opp, shown_at_ms=now_ms - 70 * 60 * 1000, db_path=ctx.path)
            await persist_opportunity_for_audit(opp, shown_at_ms=now_ms - 10 * 60 * 1000, db_path=ctx.path)
            pending = await load_pending_audit_records(
                now_ms=now_ms, backcheck_delay_min=60, db_path=ctx.path
            )
            self.assertEqual(len(pending), 1)


class TestMarkResolved(unittest.IsolatedAsyncioTestCase):
    async def test_mark_pending_resolved(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                load_recent_audit_records,
                mark_audit_record_resolved,
                persist_opportunity_for_audit,
            )

            shown_ms = 1_700_000_000_000
            buy = _ad(trade_type="BUY")
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell)
            await persist_opportunity_for_audit(opp, shown_at_ms=shown_ms, db_path=ctx.path)
            key = opportunity_key(opp)
            ok = await mark_audit_record_resolved(
                opportunity_key=key,
                shown_at_ms=shown_ms,
                status=STATUS_CONFIRMED,
                realised_spread_pct=1.5,
                realised_at_ms=shown_ms + 3600 * 1000,
                db_path=ctx.path,
            )
            self.assertTrue(ok)
            records = await load_recent_audit_records(db_path=ctx.path)
            self.assertEqual(records[0].status, STATUS_CONFIRMED)
            self.assertEqual(records[0].realised_spread_pct, 1.5)

    async def test_mark_twice_no_op(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                mark_audit_record_resolved,
                persist_opportunity_for_audit,
            )

            shown_ms = 1_700_000_000_000
            buy = _ad(trade_type="BUY")
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell)
            await persist_opportunity_for_audit(opp, shown_at_ms=shown_ms, db_path=ctx.path)
            key = opportunity_key(opp)
            ok1 = await mark_audit_record_resolved(
                opportunity_key=key,
                shown_at_ms=shown_ms,
                status=STATUS_DECAYED,
                realised_spread_pct=0.5,
                db_path=ctx.path,
            )
            ok2 = await mark_audit_record_resolved(
                opportunity_key=key,
                shown_at_ms=shown_ms,
                status=STATUS_CONFIRMED,
                realised_spread_pct=1.5,
                db_path=ctx.path,
            )
            self.assertTrue(ok1)
            self.assertFalse(ok2)


class TestCleanup(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_old_records(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                cleanup_old_audit_records,
                load_recent_audit_records,
                persist_opportunity_for_audit,
            )

            now_ms = int(time.time() * 1000)
            buy = _ad(trade_type="BUY")
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell)
            await persist_opportunity_for_audit(opp, shown_at_ms=now_ms - 30 * 24 * 60 * 60 * 1000, db_path=ctx.path)
            await persist_opportunity_for_audit(opp, shown_at_ms=now_ms - 1 * 24 * 60 * 60 * 1000, db_path=ctx.path)
            deleted = await cleanup_old_audit_records(retention_days=14, db_path=ctx.path)
            self.assertEqual(deleted, 1)
            remaining = await load_recent_audit_records(db_path=ctx.path)
            self.assertEqual(len(remaining), 1)


class TestRunBackcheckPass(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_round_trip(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                load_recent_audit_records,
                persist_opportunity_for_audit,
                run_audit_backcheck_pass,
            )

            buy = _ad(trade_type="BUY", price=100.0)
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell, gross=2.0, net=1.5)
            now_ms = int(time.time() * 1000)
            shown_ms = now_ms - 70 * 60 * 1000
            await persist_opportunity_for_audit(opp, shown_at_ms=shown_ms, db_path=ctx.path)

            async def fake_fetch(*, asset, fiat, pay_types=()):
                return [_ad(trade_type="BUY", price=100.0)], [_ad(venue="Bybit P2P", trade_type="SELL", price=102.0)], (), "fake"

            counters = await run_audit_backcheck_pass(
                fetch_p2p_ads=fake_fetch,
                now_ms=now_ms,
                db_path=ctx.path,
                price_tolerance_pct=1.0,
                backcheck_delay_min=60,
                expire_after_min=24 * 60,
            )
            self.assertEqual(counters.get("checked"), 1)
            self.assertEqual(counters.get(STATUS_CONFIRMED), 1)

            records = await load_recent_audit_records(db_path=ctx.path)
            self.assertEqual(records[0].status, STATUS_CONFIRMED)

    async def test_vanished_when_no_ads(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                persist_opportunity_for_audit,
                run_audit_backcheck_pass,
            )

            buy = _ad(trade_type="BUY", price=100.0)
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell)
            now_ms = int(time.time() * 1000)
            shown_ms = now_ms - 70 * 60 * 1000
            await persist_opportunity_for_audit(opp, shown_at_ms=shown_ms, db_path=ctx.path)

            async def empty_fetch(*, asset, fiat, pay_types=()):
                return [], [], (), "fake"

            counters = await run_audit_backcheck_pass(
                fetch_p2p_ads=empty_fetch,
                now_ms=now_ms,
                db_path=ctx.path,
                backcheck_delay_min=60,
                expire_after_min=24 * 60,
            )
            self.assertEqual(counters.get(STATUS_VANISHED), 1)

    async def test_expired_when_too_old(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                persist_opportunity_for_audit,
                run_audit_backcheck_pass,
            )

            buy = _ad(trade_type="BUY", price=100.0)
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell)
            now_ms = int(time.time() * 1000)
            shown_ms = now_ms - 7 * 24 * 60 * 60 * 1000  # 7 days ago

            async def empty_fetch(*, asset, fiat, pay_types=()):
                return [], [], (), "fake"

            await persist_opportunity_for_audit(opp, shown_at_ms=shown_ms, db_path=ctx.path)
            counters = await run_audit_backcheck_pass(
                fetch_p2p_ads=empty_fetch,
                now_ms=now_ms,
                db_path=ctx.path,
                backcheck_delay_min=60,
                expire_after_min=60,
            )
            self.assertEqual(counters.get(STATUS_EXPIRED), 1)

    async def test_amplified_classification(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import (
                persist_opportunity_for_audit,
                run_audit_backcheck_pass,
            )

            buy = _ad(trade_type="BUY", price=100.0)
            sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
            opp = _opp(buy, sell, gross=2.0, net=1.5)
            now_ms = int(time.time() * 1000)
            shown_ms = now_ms - 70 * 60 * 1000
            await persist_opportunity_for_audit(opp, shown_at_ms=shown_ms, db_path=ctx.path)

            async def amplified_fetch(*, asset, fiat, pay_types=()):
                # Sell price moved up → spread widened
                return [_ad(trade_type="BUY", price=100.0)], [_ad(venue="Bybit P2P", trade_type="SELL", price=103.5)], (), "fake"

            counters = await run_audit_backcheck_pass(
                fetch_p2p_ads=amplified_fetch,
                now_ms=now_ms,
                db_path=ctx.path,
                price_tolerance_pct=2.0,
                decay_threshold_pct=25.0,
                backcheck_delay_min=60,
                expire_after_min=24 * 60,
            )
            self.assertEqual(counters.get(STATUS_AMPLIFIED), 1)


class TestFormatAuditReport(unittest.IsolatedAsyncioTestCase):
    async def test_empty_report(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import format_audit_report

            out = await format_audit_report(db_path=ctx.path)
            self.assertIn("P2P self-audit", out)

    async def test_get_audit_stats_returns_recommendation(self):
        async with _AuditDBContext() as ctx:
            from p2p_audit_io import get_audit_stats

            records, recommendation = await get_audit_stats(db_path=ctx.path)
            self.assertEqual(len(records), 0)
            self.assertEqual(recommendation.direction, "hold")


if __name__ == "__main__":
    unittest.main()
