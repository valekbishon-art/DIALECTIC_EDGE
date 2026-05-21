"""Unit tests for ``core/healthz.py``.

Coverage:
  * :func:`build_status` returns the documented contract (keys + types).
  * Heartbeat markers turn ``None`` ages into ``int`` ages.
  * Env-flag booleans reflect the env without leaking values.
  * The aiohttp app serves JSON on ``/healthz``, ``/health`` and ``/``.
  * ``run_healthz_server`` honours custom port and degrades gracefully when
    the port is busy (does NOT raise).
"""
from __future__ import annotations

import asyncio
import socket
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase

from core import healthz


class BuildStatusTestCase(unittest.TestCase):
    """Pure-function tests — no event loop required."""

    def setUp(self) -> None:
        # Reset module-level heartbeat state between tests.
        healthz._AUTOTRADE_HEARTBEAT_TS = None
        healthz._LAST_DIGEST_TS = None

    def test_contract_keys_and_types(self) -> None:
        status = healthz.build_status()
        expected_keys = {
            "status", "service", "uptime_s",
            "feature_autotrade", "bot_token_set", "github_token_set", "redis_url_set",
            "autotrade_loop_age_s", "last_digest_age_s",
        }
        self.assertEqual(set(status.keys()), expected_keys)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["service"], "dialectic-edge")
        self.assertIsInstance(status["uptime_s"], int)
        self.assertGreaterEqual(status["uptime_s"], 0)
        self.assertIsInstance(status["feature_autotrade"], bool)
        self.assertIsInstance(status["bot_token_set"], bool)
        self.assertIsInstance(status["github_token_set"], bool)
        self.assertIsInstance(status["redis_url_set"], bool)
        self.assertIsNone(status["autotrade_loop_age_s"])
        self.assertIsNone(status["last_digest_age_s"])

    def test_env_flags_truthy(self) -> None:
        with patch.dict("os.environ", {
            "FEATURE_AUTOTRADE": "1",
            "BOT_TOKEN": "x",
            "GITHUB_TOKEN": "y",
            "REDIS_URL": "redis://localhost:6379/0",
        }, clear=False):
            status = healthz.build_status()
        self.assertTrue(status["feature_autotrade"])
        self.assertTrue(status["bot_token_set"])
        self.assertTrue(status["github_token_set"])
        self.assertTrue(status["redis_url_set"])

    def test_env_flags_falsy(self) -> None:
        with patch.dict("os.environ", {
            "FEATURE_AUTOTRADE": "0",
            "BOT_TOKEN": "",
            "GITHUB_TOKEN": "",
            "REDIS_URL": "   ",
        }, clear=False):
            status = healthz.build_status()
        self.assertFalse(status["feature_autotrade"])
        self.assertFalse(status["bot_token_set"])
        self.assertFalse(status["github_token_set"])
        self.assertFalse(status["redis_url_set"])

    def test_env_flags_no_value_leakage(self) -> None:
        """Whatever the value is, status must NOT include the raw string."""
        secret = "super-secret-token-xyz-12345"
        with patch.dict("os.environ", {"BOT_TOKEN": secret, "GITHUB_TOKEN": secret}, clear=False):
            status = healthz.build_status()
        flat = repr(status)
        self.assertNotIn(secret, flat)

    def test_heartbeat_marks_turn_into_int_ages(self) -> None:
        healthz.mark_autotrade_heartbeat()
        healthz.mark_digest_sent()
        # Sleep a tiny bit to guarantee at least 0 elapsed (int truncation).
        time.sleep(0.01)
        status = healthz.build_status()
        self.assertIsInstance(status["autotrade_loop_age_s"], int)
        self.assertIsInstance(status["last_digest_age_s"], int)
        self.assertGreaterEqual(status["autotrade_loop_age_s"], 0)
        self.assertGreaterEqual(status["last_digest_age_s"], 0)

    def test_truthy_parser_variants(self) -> None:
        cases = {
            "1": True, "true": True, "TRUE": True, "yes": True, "on": True, "Y": True, "t": True,
            "0": False, "false": False, "no": False, "off": False, "": False, "   ": False,
            None: False, "garbage": False,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(healthz._is_truthy(raw), expected)


class HealthzHTTPTestCase(AioHTTPTestCase):
    """End-to-end test over an in-process aiohttp server (no real port)."""

    async def get_application(self):  # type: ignore[override]
        return healthz.build_app()

    async def test_healthz_returns_json_200(self) -> None:
        resp = await self.client.request("GET", "/healthz")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")
        body = await resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "dialectic-edge")

    async def test_health_alias(self) -> None:
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)

    async def test_root_alias(self) -> None:
        resp = await self.client.request("GET", "/")
        self.assertEqual(resp.status, 200)


class RunHealthzServerTestCase(unittest.TestCase):
    """Smoke test for the server bootstrap — uses a real loopback socket."""

    def _free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_run_then_cancel(self) -> None:
        port = self._free_port()

        async def scenario() -> None:
            task = asyncio.create_task(healthz.run_healthz_server(port=port))
            try:
                # Wait for the server to bind.
                await asyncio.sleep(0.2)
                self.assertFalse(task.done(), "server task exited unexpectedly")
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(scenario())

    def test_busy_port_does_not_raise(self) -> None:
        port = self._free_port()
        # Occupy the port from another socket so the second bind fails.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind(("127.0.0.1", port))
            blocker.listen(1)

            async def scenario() -> None:
                # Should return cleanly (logging a warning) rather than raise.
                await asyncio.wait_for(healthz.run_healthz_server(port=port), timeout=2.0)

            asyncio.run(scenario())
        finally:
            blocker.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
