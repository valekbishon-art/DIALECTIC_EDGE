"""Tests for :mod:`core.ai_metrics`.

Uses an in-memory-ish on-disk SQLite (tempfile) for isolation. The module
under test accepts a ``db_path`` override per call, so we never touch the
real ``DB_PATH``.

Coverage:

* ``init_ai_metrics_db`` is idempotent (call twice → no error).
* ``record_ai_call`` inserts a row and silently no-ops on a bad path.
* ``track_ai_call`` records latency + ok=True on a clean exit.
* ``track_ai_call`` records ok=False with exception class on raise — and re-raises.
* ``track_ai_call`` honours mid-call ``ctx["model"]`` override.
* ``fetch_recent_metrics`` returns ordered, filtered rows.
* ``fetch_recent_metrics`` returns ``[]`` (not raise) on a non-existent DB.
* ``summarise_recent`` returns per-provider aggregates with correct calls/ok/fail and percentiles.
* Long strings (provider/model/role/error) are truncated to schema limits.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from core import ai_metrics


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(prefix="ai_metrics_", suffix=".db")
    os.close(fd)
    return path


class AiMetricsInitTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_init_idempotent(self) -> None:
        db = _tmp_db()
        try:
            await ai_metrics.init_ai_metrics_db(db_path=db)
            await ai_metrics.init_ai_metrics_db(db_path=db)  # must not raise
        finally:
            os.unlink(db)

    async def test_init_on_unwritable_path_is_swallowed(self) -> None:
        # /proc is a kernel-managed VFS — we cannot create files in it.
        # Should log a warning but not raise.
        await ai_metrics.init_ai_metrics_db(db_path="/proc/definitely-not-writable.db")


class AiMetricsRecordTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _tmp_db()
        await ai_metrics.init_ai_metrics_db(db_path=self.db)

    async def asyncTearDown(self) -> None:
        os.unlink(self.db)

    async def test_record_inserts_row(self) -> None:
        await ai_metrics.record_ai_call(
            provider="cerebras", model="qwen-3", role="bull",
            latency_ms=123, ok=True, db_path=self.db,
        )
        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], "cerebras")
        self.assertEqual(row["model"], "qwen-3")
        self.assertEqual(row["role"], "bull")
        self.assertEqual(row["latency_ms"], 123)
        self.assertTrue(row["ok"])
        self.assertIsNone(row["error"])

    async def test_record_failure_with_error(self) -> None:
        await ai_metrics.record_ai_call(
            provider="mistral", model="mistral-small", role="synth",
            latency_ms=8000, ok=False, error="HTTPError 429: rate-limited",
            db_path=self.db,
        )
        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertEqual(rows[0]["ok"], False)
        self.assertIn("429", rows[0]["error"])

    async def test_record_truncates_long_strings(self) -> None:
        await ai_metrics.record_ai_call(
            provider="x" * 200,
            model="m" * 500,
            role="r" * 200,
            latency_ms=1,
            ok=True,
            error="e" * 2000,
            db_path=self.db,
        )
        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertLessEqual(len(rows[0]["provider"]), 64)
        self.assertLessEqual(len(rows[0]["model"]), 128)
        self.assertLessEqual(len(rows[0]["role"]), 32)

    async def test_record_negative_latency_clamped(self) -> None:
        await ai_metrics.record_ai_call(
            provider="groq", model="llama", role="bear",
            latency_ms=-99, ok=True, db_path=self.db,
        )
        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertEqual(rows[0]["latency_ms"], 0)


class AiMetricsTrackTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _tmp_db()
        await ai_metrics.init_ai_metrics_db(db_path=self.db)

    async def asyncTearDown(self) -> None:
        os.unlink(self.db)

    async def test_success_recorded(self) -> None:
        async with ai_metrics.track_ai_call(
            provider="groq", model="llama-3.3", role="verifier", db_path=self.db,
        ):
            await asyncio.sleep(0)  # fake work

        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["provider"], "groq")
        self.assertEqual(rows[0]["role"], "verifier")

    async def test_exception_recorded_and_reraised(self) -> None:
        with self.assertRaises(ValueError):
            async with ai_metrics.track_ai_call(
                provider="openrouter", model="nemotron-120b",
                role="bull", db_path=self.db,
            ):
                raise ValueError("boom")

        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("ValueError", rows[0]["error"])
        self.assertIn("boom", rows[0]["error"])

    async def test_model_override_mid_call(self) -> None:
        async with ai_metrics.track_ai_call(
            provider="openrouter", model="initial", role="bear", db_path=self.db,
        ) as ctx:
            # Simulate fallback to a different model mid-call.
            ctx["model"] = "actually-used-llama"

        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertEqual(rows[0]["model"], "actually-used-llama")


class AiMetricsFetchTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _tmp_db()
        await ai_metrics.init_ai_metrics_db(db_path=self.db)
        # Populate a small dataset.
        for prov, model, role, lat, ok in [
            ("cerebras", "qwen-3", "bull", 100, True),
            ("cerebras", "qwen-3", "bear", 200, True),
            ("cerebras", "qwen-3", "bull", 50, False),
            ("groq", "llama-3.3", "verifier", 300, True),
            ("groq", "llama-3.3", "verifier", 400, False),
        ]:
            await ai_metrics.record_ai_call(
                provider=prov, model=model, role=role,
                latency_ms=lat, ok=ok, db_path=self.db,
            )

    async def asyncTearDown(self) -> None:
        os.unlink(self.db)

    async def test_fetch_all(self) -> None:
        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        self.assertEqual(len(rows), 5)

    async def test_fetch_filter_by_provider(self) -> None:
        rows = await ai_metrics.fetch_recent_metrics(
            db_path=self.db, hours=0, provider="cerebras",
        )
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["provider"] == "cerebras" for r in rows))

    async def test_fetch_filter_by_role(self) -> None:
        rows = await ai_metrics.fetch_recent_metrics(
            db_path=self.db, hours=0, role="verifier",
        )
        self.assertEqual(len(rows), 2)

    async def test_fetch_respects_limit(self) -> None:
        rows = await ai_metrics.fetch_recent_metrics(
            db_path=self.db, hours=0, limit=2,
        )
        self.assertEqual(len(rows), 2)

    async def test_fetch_descending_order(self) -> None:
        rows = await ai_metrics.fetch_recent_metrics(db_path=self.db, hours=0)
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids, reverse=True))

    async def test_fetch_missing_db_returns_empty(self) -> None:
        rows = await ai_metrics.fetch_recent_metrics(
            db_path="/nonexistent/path/x.db", hours=0,
        )
        self.assertEqual(rows, [])


class AiMetricsSummariseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _tmp_db()
        await ai_metrics.init_ai_metrics_db(db_path=self.db)
        for prov, lat, ok in [
            ("cerebras", 100, True),
            ("cerebras", 200, True),
            ("cerebras", 300, False),
            ("groq", 50, True),
            ("groq", 60, True),
        ]:
            await ai_metrics.record_ai_call(
                provider=prov, model="x", role="bull",
                latency_ms=lat, ok=ok, db_path=self.db,
            )

    async def asyncTearDown(self) -> None:
        os.unlink(self.db)

    async def test_summarise_per_provider(self) -> None:
        summary = await ai_metrics.summarise_recent(db_path=self.db, hours=24)
        self.assertIn("cerebras", summary)
        self.assertIn("groq", summary)
        self.assertEqual(summary["cerebras"]["calls"], 3)
        self.assertEqual(summary["cerebras"]["ok"], 2)
        self.assertEqual(summary["cerebras"]["fail"], 1)
        self.assertEqual(summary["groq"]["calls"], 2)
        self.assertEqual(summary["groq"]["ok"], 2)
        self.assertEqual(summary["groq"]["fail"], 0)
        self.assertGreaterEqual(summary["cerebras"]["p95"], summary["cerebras"]["p50"])

    async def test_summarise_missing_db(self) -> None:
        result = await ai_metrics.summarise_recent(db_path="/nonexistent.db")
        self.assertEqual(result, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
