#!/usr/bin/env python3
"""
cron_digest.py — Standalone script for pre-generating daily digest.

Designed to run on GitHub Actions (cron schedule) or manually.
Connects to PostgreSQL (DATABASE_URL) and saves the generated digest
for the bot to serve instantly without burning AI tokens per-user.

Usage:
  DATABASE_URL=... python cron_digest.py

Flow:
  1. Fetch market data (same as /daily)
  2. Run multi-agent debate (Bull/Bear/Verifier/Synth)
  3. Build digest context + short report
  4. Save to PostgreSQL daily_digests table
  5. Exit (GitHub Actions runner shuts down)

Requirements:
  pip install -r requirements.txt  (includes asyncpg, sqlalchemy)
  Environment variables: DATABASE_URL + AI provider keys (see .env.example)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date

# Load .env if present (local runs).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cron_digest")


async def main() -> int:
    """Generate and cache today's digest. Returns 0 on success, 1 on failure."""
    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL not set — cannot cache digest")
        return 1

    # ── 1. Initialize PostgreSQL ──
    from payments.db import init_postgres, save_digest
    pg_ok = await init_postgres()
    if not pg_ok:
        logger.error("PostgreSQL init failed")
        return 1

    # ── 2. Run the full analysis pipeline (same as /daily) ──
    logger.info("Starting AI analysis pipeline...")
    try:
        from analysis_service import run_full_analysis
        result = await run_full_analysis()
    except Exception as e:
        logger.error("Analysis pipeline failed: %s", e)
        return 1

    if not result or not result.get("full_report"):
        logger.error("Analysis returned empty result")
        return 1

    full_report = result["full_report"]
    logger.info("Analysis complete (%d chars)", len(full_report))

    # ── 3. Build digest context + short report ──
    try:
        from core.digest_context import build_digest_context
        ctx = build_digest_context(full_report)
    except Exception as e:
        logger.warning("build_digest_context failed: %s — saving raw report", e)
        ctx = {}

    # Build short report (Telegram-formatted digest).
    short_report = ""
    try:
        # Import build_short_report from main.py — it's defined locally there.
        # We replicate the minimal call here.
        from main import build_short_report
        short_report = build_short_report(full_report) or ""
    except Exception as e:
        logger.warning("build_short_report failed: %s", e)

    market_regime = ctx.get("regime", "")

    # ── 4. Save to PostgreSQL ──
    today = date.today()
    ok = await save_digest(
        digest_date=today,
        digest_text=full_report,
        short_report=short_report,
        market_regime=market_regime,
    )
    if ok:
        logger.info("Digest saved to PostgreSQL for %s", today)
        return 0
    else:
        logger.error("Failed to save digest to PostgreSQL")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
