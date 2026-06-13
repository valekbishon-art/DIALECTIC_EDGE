"""Tests for the live edge-ledger: formal certificate, record/resolve, stats.

No network or real Postgres. SQLite uses a temp file; candle fetch is mocked.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch


# ── Formal certificate (#2) ──────────────────────────────────────────────────
class TestCertificate(unittest.TestCase):
    def _score(self, **pts):
        from core.signal_scorer import AssetScore, ScoreBreakdown
        b = ScoreBreakdown()
        for k, v in pts.items():
            setattr(b, k, v)
        return AssetScore(asset="BTC", direction="LONG", breakdown=b, reasons=[])

    def test_certificate_flags(self):
        from core.signal_scorer import build_certificate
        sc = self._score(trend_alignment=25, complexity_hint=20,
                         vrt_structure=0, markov_state=15, raw_tradeable=10)
        cert = build_certificate(sc, rr_ratio=2.5)
        self.assertTrue(cert["trend_aligned"])
        self.assertTrue(cert["complexity_trending"])
        self.assertFalse(cert["vrt_structure_ok"])
        self.assertTrue(cert["markov_aligned"])
        self.assertTrue(cert["rr_ge_2"])
        self.assertFalse(cert["rr_ge_3"])
        self.assertTrue(cert["score_ge_60"])  # total = 70
        self.assertFalse(cert["score_ge_75"])

    def test_setup_carries_certificate(self):
        # make_setup should attach a non-empty certificate.
        from core.signal_scorer import AssetScore, ScoreBreakdown, make_setup
        b = ScoreBreakdown(trend_alignment=30, complexity_hint=20,
                           vrt_structure=10, markov_state=15, raw_tradeable=10)
        sc = AssetScore(asset="BTC", direction="LONG", breakdown=b, reasons=["x"])
        p = {"price": 50000.0, "vol_sigma_1d_pct": 3.0}
        setup = make_setup(sc, p, capital=100.0)
        self.assertIsNotNone(setup)
        self.assertIsInstance(setup.certificate, dict)
        self.assertTrue(setup.certificate["trend_aligned"])
        self.assertEqual(setup.certificate["score_ge_60"], setup.score >= 60)


# ── resolve_pending logic (mocked DB + candles) ──────────────────────────────
def _candle(ts, high, low, close):
    return types.SimpleNamespace(timestamp=ts, open=close, high=high,
                                 low=low, close=close, volume=0.0)


class TestResolvePending(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_tp(self):
        import core.edge_ledger as el
        import database as db

        emitted = datetime(2026, 1, 1, 0, 0, 0)
        row = {"id": 1, "asset": "BTC", "direction": "LONG", "entry": 100.0,
               "target": 110.0, "stop": 95.0, "horizon_hours": 336.0,
               "emitted_at": emitted.isoformat()}
        candles = [
            _candle(emitted + timedelta(days=1), high=105, low=99, close=104),
            _candle(emitted + timedelta(days=2), high=112, low=104, close=111),  # hits TP
        ]
        mark = AsyncMock()
        with patch.object(db, "edge_get_pending", AsyncMock(return_value=[row])), \
             patch.object(db, "edge_mark_resolved", mark), \
             patch.object(el, "_fetch_candles_naive_utc", AsyncMock(return_value=candles)):
            summary = await el.resolve_pending()
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["tp"], 1)
        mark.assert_awaited_once()
        self.assertEqual(mark.await_args.args[1], "tp")

    async def test_still_pending_when_no_candles(self):
        import core.edge_ledger as el
        import database as db
        row = {"id": 2, "asset": "BTC", "direction": "LONG", "entry": 100.0,
               "target": 110.0, "stop": 95.0, "horizon_hours": 336.0,
               "emitted_at": datetime(2026, 1, 1).isoformat()}
        with patch.object(db, "edge_get_pending", AsyncMock(return_value=[row])), \
             patch.object(db, "edge_mark_resolved", AsyncMock()), \
             patch.object(el, "_fetch_candles_naive_utc", AsyncMock(return_value=[])):
            summary = await el.resolve_pending()
        self.assertEqual(summary["resolved"], 0)
        self.assertEqual(summary["still_pending"], 1)


# ── End-to-end SQLite CRUD + condition stats ─────────────────────────────────
class TestEdgeDbStats(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._patch = patch("database.DB_PATH", self.tmp.name)
        self._patch.start()
        import aiosqlite
        async with aiosqlite.connect(self.tmp.name) as conn:
            await conn.execute("""
                CREATE TABLE edge_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT, source TEXT, asset TEXT, direction TEXT,
                    entry REAL, target REAL, stop REAL, horizon_hours REAL,
                    emitted_at TEXT, score INTEGER, rr_ratio REAL,
                    certificate TEXT, reasons TEXT, status TEXT DEFAULT 'pending',
                    exit_price REAL, pnl_pct REAL, exit_at TEXT, resolved_at TEXT
                )
            """)
            await conn.commit()

    async def asyncTearDown(self):
        self._patch.stop()
        os.unlink(self.tmp.name)

    async def test_insert_resolve_stats(self):
        import database as db
        # Winning signal: trend_aligned True, markov_aligned True
        wid = await db.edge_insert_signal(
            source="t", asset="BTC", direction="LONG", entry=100.0, target=110.0,
            stop=95.0, horizon_hours=336.0, emitted_at="2026-01-01T00:00:00",
            score=72, rr_ratio=2.5,
            certificate={"trend_aligned": True, "markov_aligned": True,
                         "vrt_structure_ok": False},
            reasons=["r"])
        # Losing signal: trend_aligned True, markov_aligned False
        lid = await db.edge_insert_signal(
            source="t", asset="ETH", direction="LONG", entry=100.0, target=110.0,
            stop=95.0, horizon_hours=336.0, emitted_at="2026-01-02T00:00:00",
            score=61, rr_ratio=2.0,
            certificate={"trend_aligned": True, "markov_aligned": False,
                         "vrt_structure_ok": True},
            reasons=["r"])

        pending = await db.edge_get_pending()
        self.assertEqual(len(pending), 2)

        await db.edge_mark_resolved(wid, "tp", 110.0, 9.8, "2026-01-03T00:00:00")
        await db.edge_mark_resolved(lid, "sl", 95.0, -5.2, "2026-01-04T00:00:00")

        overall = await db.edge_overall_stats()
        self.assertEqual(overall["resolved"], 2)
        self.assertEqual(overall["tp"], 1)
        self.assertEqual(overall["sl"], 1)
        self.assertEqual(overall["pending"], 0)

        stats = {c["condition"]: c for c in await db.edge_condition_stats()}
        # trend_aligned held in both → 1 win of 2 = 50%
        self.assertEqual(stats["trend_aligned"]["n"], 2)
        self.assertAlmostEqual(stats["trend_aligned"]["win_rate"], 50.0)
        # markov_aligned held only in winner → 100%
        self.assertEqual(stats["markov_aligned"]["n"], 1)
        self.assertAlmostEqual(stats["markov_aligned"]["win_rate"], 100.0)
        # vrt_structure_ok held only in loser → 0%
        self.assertEqual(stats["vrt_structure_ok"]["n"], 1)
        self.assertAlmostEqual(stats["vrt_structure_ok"]["win_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
