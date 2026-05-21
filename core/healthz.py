"""Lightweight ``/healthz`` HTTP endpoint.

Used for Railway restart policy / external liveness probes / Devin Review
smoke-tests after deploy. Stays intentionally minimal:

  * No DB queries on the hot path (handler must return in <50 ms even when
    SQLite is locked by signal_trader).
  * No imports of heavy modules at handler time (only stdlib + aiohttp).
  * Exposes only non-sensitive booleans for env-flags (never values).

The server binds to ``0.0.0.0:$PORT`` (Railway convention) and serves the
same JSON status at ``/healthz``, ``/health`` and ``/``.

Modules that own runtime state (signal_trader, scheduler) can later push
heartbeats via :func:`mark_autotrade_heartbeat` and :func:`mark_digest_sent`.
Until they do, those fields are reported as ``None`` — that is by design and
not an error.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# Monotonic seconds since process start; set on import so ``uptime_s`` works
# even before main() runs (useful in tests).
_START_TS: float = time.monotonic()

# Most recent heartbeat timestamps (monotonic seconds). ``None`` = never seen.
_AUTOTRADE_HEARTBEAT_TS: Optional[float] = None
_LAST_DIGEST_TS: Optional[float] = None


def mark_autotrade_heartbeat() -> None:
    """Record that the autotrade main loop has just ticked.

    Safe to call from any coroutine; non-blocking. Signal trader is expected
    to call this at the top of each loop iteration in a future PR.
    """
    global _AUTOTRADE_HEARTBEAT_TS
    _AUTOTRADE_HEARTBEAT_TS = time.monotonic()


def mark_digest_sent() -> None:
    """Record that a daily digest delivery succeeded."""
    global _LAST_DIGEST_TS
    _LAST_DIGEST_TS = time.monotonic()


def _is_truthy(val: Optional[str]) -> bool:
    if not val:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def build_status() -> dict[str, Any]:
    """Pure function that returns the current health snapshot as a dict.

    Kept pure (no I/O) so unit tests can call it directly without a running
    aiohttp server.
    """
    now = time.monotonic()
    uptime_s = max(0, int(now - _START_TS))
    autotrade_age: Optional[int] = (
        int(now - _AUTOTRADE_HEARTBEAT_TS) if _AUTOTRADE_HEARTBEAT_TS is not None else None
    )
    digest_age: Optional[int] = (
        int(now - _LAST_DIGEST_TS) if _LAST_DIGEST_TS is not None else None
    )
    return {
        "status": "ok",
        "service": "dialectic-edge",
        "uptime_s": uptime_s,
        # Feature/flag visibility — booleans only, never values.
        "feature_autotrade": _is_truthy(os.getenv("FEATURE_AUTOTRADE", "0")),
        "bot_token_set": bool(os.getenv("BOT_TOKEN")),
        "github_token_set": bool(os.getenv("GITHUB_TOKEN")),
        "redis_url_set": bool((os.getenv("REDIS_URL") or "").strip()),
        # Heartbeats — ages in seconds, ``None`` until first tick observed.
        "autotrade_loop_age_s": autotrade_age,
        "last_digest_age_s": digest_age,
    }


async def _handler(_request: web.Request) -> web.Response:
    return web.json_response(build_status())


def build_app() -> web.Application:
    """Construct the aiohttp Application that serves /healthz.

    Exposed as a top-level function so unit tests can attach it to
    ``aiohttp.test_utils.TestServer`` without spinning a real port.
    """
    app = web.Application()
    app.router.add_get("/healthz", _handler)
    app.router.add_get("/health", _handler)
    app.router.add_get("/", _handler)
    return app


async def run_healthz_server(port: Optional[int] = None) -> None:
    """Run the /healthz server until the surrounding task is cancelled.

    Args:
        port: TCP port to bind. Defaults to ``int($PORT)`` (Railway convention)
            or 8080 if ``$PORT`` is unset/invalid.
    """
    if port is None:
        raw_port = os.getenv("PORT", "8080") or "8080"
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 8080
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    try:
        await site.start()
    except OSError as exc:
        # Port already in use — log loudly but do NOT crash the whole bot.
        logger.warning("healthz: cannot bind 0.0.0.0:%s (%s); endpoint disabled", port, exc)
        await runner.cleanup()
        return
    logger.info("healthz: listening on 0.0.0.0:%s (/healthz)", port)
    try:
        # Block until the surrounding gather() is cancelled.
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
