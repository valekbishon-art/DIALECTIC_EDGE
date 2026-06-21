#!/usr/bin/env python3
"""
cron_broadcast.py — GitHub-Actions FALLBACK for the 09:00 MSK digest broadcast.

The primary delivery path is the always-on bot (scheduler.py
_dialectica_broadcast_loop on Railway), which broadcasts the cached digest at
09:00 MSK and sets daily_digests.broadcast_done = TRUE.

This script is the safety net. It runs a few minutes later from GitHub Actions:

  1. Connect to PostgreSQL (DATABASE_URL).
  2. If today's digest is already marked broadcast_done -> exit (the live bot
     handled it; nothing to do).
  3. Otherwise reuse the bot's own broadcast_dialectica_digest() to send the
     cached digest (full parity: short report + chart + keyboard) to every user
     with active premium OR free trial, then mark broadcast_done.

Usage:
  DATABASE_URL=... BOT_TOKEN=... python cron_broadcast.py

Requirements:
  Environment: DATABASE_URL + BOT_TOKEN (plus AI keys only matter if the digest
  has to be regenerated from cache).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cron_broadcast")


async def main() -> int:
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL not set — cannot run broadcast fallback")
        return 1
    if not os.getenv("BOT_TOKEN"):
        logger.error("BOT_TOKEN not set — cannot send Telegram messages")
        return 1

    # ── 1. Init PostgreSQL ──
    from payments.db import init_postgres, is_digest_broadcast
    if not await init_postgres():
        logger.error("PostgreSQL init failed")
        return 1

    # MSK-дата (UTC+3, без DST) — единый ключ с scheduler._dialectica_broadcast_loop
    today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()

    # ── 2. Skip if the live bot already broadcast today's digest ──
    if await is_digest_broadcast(today):
        logger.info("Digest for %s already broadcast by the live bot — fallback skips", today)
        return 0

    logger.info("Live bot did NOT broadcast %s yet — running fallback broadcast", today)

    # ── 3. Reuse the bot's broadcast logic (full parity) ──
    import main as bot_main
    # Lazily create the aiogram Bot for this runner (no polling is started).
    bot_main.bot = bot_main.get_bot()
    try:
        sent = await bot_main.broadcast_dialectica_digest()
        logger.info("Fallback broadcast complete: %s recipients", sent)
    finally:
        try:
            await bot_main.bot.session.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
