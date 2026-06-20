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

    # ── 1b. Initialize the local SQLite DB (database.py) ──
    # The analysis pipeline also writes to SQLite tables (daily_context,
    # predictions, ai_call_metrics, ...). On a fresh GitHub Actions runner the
    # SQLite file is empty, so we must create the schema here. On Railway the
    # file persists, so init_db() is an idempotent no-op there.
    try:
        from database import init_db
        await init_db()
    except Exception as e:
        logger.error("SQLite init_db failed: %s", e)
        return 1
    # ai_call_metrics lives in its own module; non-fatal if it can't init.
    try:
        from core.ai_metrics import init_ai_metrics_db
        await init_ai_metrics_db()
    except Exception as e:
        logger.warning("ai_metrics init failed (non-fatal): %s", e)

    # ── 2. Run the full analysis pipeline (same as /daily) ──
    # run_full_analysis(user_id, ...) requires a user_id and returns a
    # (report, prices) tuple. For the cron run there is no requesting user,
    # so we generate the canonical digest "as the bot owner": the first id in
    # ADMIN_IDS, falling back to 0 (get_profile handles a missing profile).
    def _cron_user_id() -> int:
        raw = os.getenv("ADMIN_IDS", "") or ""
        for tok in raw.replace(";", ",").split(","):
            tok = tok.strip()
            if tok.lstrip("-").isdigit():
                return int(tok)
        return 0

    logger.info("Starting AI analysis pipeline...")
    try:
        from analysis_service import run_full_analysis
        report, _prices = await run_full_analysis(_cron_user_id())
    except Exception as e:
        logger.error("Analysis pipeline failed: %s", e)
        return 1

    if not report:
        logger.error("Analysis returned empty result")
        return 1

    full_report = report
    logger.info("Analysis complete (%d chars)", len(full_report))

    # ── 3. Build digest context + short report ──
    try:
        from core.digest_context import build_digest_context
        ctx = build_digest_context(full_report)
    except Exception as e:
        logger.warning("build_digest_context failed: %s — saving raw report", e)
        ctx = {}

    # Build short report (Telegram-formatted digest) the SAME way the bot does
    # in send_daily_digest_bundle: parse_report_parts -> extract stars/pct ->
    # build_short_report(parts, stars, pct, horizon=swing). The result is a list
    # of message chunks; we join them for storage / serving.
    short_report = ""
    try:
        from main import (
            build_short_report,
            parse_report_parts,
            extract_signal_pct_and_stars,
        )
        from core.horizons import get_horizon, DEFAULT_HORIZON_KEY

        parts = parse_report_parts(full_report)
        pct_val, stars_str = extract_signal_pct_and_stars(full_report)
        pack = get_horizon(DEFAULT_HORIZON_KEY)
        messages = build_short_report(parts, stars_str, pct_val, horizon=pack, prices={})
        if isinstance(messages, (list, tuple)):
            short_report = "\n\n".join(m for m in messages if m)
        else:
            short_report = str(messages or "")
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
    else:
        logger.error("Failed to save digest to PostgreSQL")

    # ── 5. Fallback: push the digest to the GitHub data table ──
    # PostgreSQL (Neon) is the primary store; the GitHub commit is a redundant
    # fallback so the bot can still serve today's digest if the DB is down.
    github_ok = False
    try:
        from github_export import push_digest_cache
        github_ok = await push_digest_cache(
            report=full_report,
            date_str=today.isoformat(),
            full_debates=full_report,
        )
        if github_ok:
            logger.info("Digest also pushed to GitHub table for %s", today)
        else:
            logger.warning("GitHub digest push returned False (check GITHUB_TOKEN)")
    except Exception as e:
        logger.warning("GitHub digest push failed: %s", e)

    # Success if the digest landed in at least one store.
    return 0 if (ok or github_ok) else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
