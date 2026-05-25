"""
database.py — SQLite база данных.

ИСПРАВЛЕНО v2:
- DB_PATH теперь импортируется из config.py (единый источник правды)
  Раньше был захардкожен здесь и не совпадал с learning.py
"""

import aiosqlite
import logging
from datetime import datetime
from typing import Optional

# ИСПРАВЛЕНО: импортируем из config чтобы все модули использовали один путь
from config import DB_PATH, DIGEST_SNAPSHOT_MAX_CHARS

logger = logging.getLogger(__name__)


async def init_db():
    """Создаёт все таблицы при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                tier        TEXT DEFAULT 'free',
                daily_sub   INTEGER DEFAULT 0,
                sub_time    TEXT DEFAULT '08:00',
                requests_today INTEGER DEFAULT 0,
                requests_total INTEGER DEFAULT 0,
                signals_sub INTEGER DEFAULT 0,
                last_active TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)

        # Добавляем колонку signals_sub если её нет (для обновления с существующей БД)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN signals_sub INTEGER DEFAULT 0")
        except Exception:
            pass  # Колонка уже существует

        await db.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at   TEXT DEFAULT (datetime('now')),
                asset        TEXT NOT NULL,
                direction    TEXT NOT NULL,
                entry_price  REAL,
                target_price REAL,
                stop_loss    REAL,
                timeframe    TEXT,
                source_news  TEXT,
                result       TEXT DEFAULT 'pending',
                result_price REAL,
                result_at    TEXT,
                pnl_pct      REAL,
                prediction_type TEXT,
                forecast     TEXT,
                fact         TEXT,
                report_type  TEXT DEFAULT 'global'
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                report_type TEXT,
                rating      INTEGER,
                comment     TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                report_type TEXT,
                news_used   TEXT,
                summary     TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS debate_sessions (
                user_id    INTEGER PRIMARY KEY,
                report     TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                amount      REAL NOT NULL,
                entry_price REAL NOT NULL,
                added_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, symbol)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS backtest_signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now')),
                symbol      TEXT NOT NULL,
                direction   TEXT NOT NULL,  -- BUY or SELL
                entry_price REAL,
                exit_price  REAL,
                status      TEXT DEFAULT 'open',  -- open, closed
                pnl         REAL DEFAULT 0,
                pnl_pct     REAL DEFAULT 0,
                signal_source TEXT,  -- daily, manual, etc
                notes       TEXT,
                quantity    REAL DEFAULT 0,  -- amount of asset
                trade_log   TEXT  -- JSON log of trade actions
            )
        """)

        # Backtest config table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS backtest_config (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                capital     REAL DEFAULT 100.0,
                enabled     INTEGER DEFAULT 1,
                last_updated TEXT DEFAULT (datetime('now'))
            )
        """)

        # Initialize default config if not exists
        await db.execute("""
            INSERT OR IGNORE INTO backtest_config (id, capital, enabled) VALUES (1, 100.0, 1)
        """)

        # Daily context table - stores verdict and price levels for signal trading
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_context (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT DEFAULT (datetime('now')),
                verdict         TEXT,  -- BUY, SELL, NEUTRAL
                symbols         TEXT,  -- JSON list of symbols to trade
                entries         TEXT,  -- JSON dict of entry prices
                stop_losses     TEXT,  -- JSON dict of stop loss prices
                targets         TEXT,  -- JSON dict of target prices
                timeframes      TEXT,  -- JSON dict of timeframes
                news_summary    TEXT,  -- brief news context
                expires_at      TEXT,  -- when this context expires (default 24h)
                prompt_versions TEXT,  -- JSON: версии пайплайна/промптов
                model_inputs_snapshot TEXT  -- JSON: усечённый снимок входов модели
            )
        """)

        async def _add_column_if_missing(table: str, column: str, decl: str):
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                cols = [row[1] for row in await cur.fetchall()]
            if column not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")

        await _add_column_if_missing("daily_context", "prompt_versions", "prompt_versions TEXT")
        await _add_column_if_missing("daily_context", "model_inputs_snapshot", "model_inputs_snapshot TEXT")
        await _add_column_if_missing("daily_context", "full_report", "full_report TEXT")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trade_decision_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now')),
                cycle_type  TEXT NOT NULL,
                signal_id   INTEGER,
                payload     TEXT NOT NULL
            )
        """)

        # ── Decision provenance (см. core/provenance.py) ──
        # Замораживает каждое торговое решение (signal_scorer + pick_best) с
        # feature-snapshot, score breakdown и git SHA. Источник правды для
        # "почему бот выбрал SHORT по SOL 18 мая 14:30". См. core/provenance.py.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS decision_provenance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                decision_type   TEXT    NOT NULL,
                asset           TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                score           INTEGER,
                entry_price     REAL,
                stop_loss       REAL,
                take_profit     REAL,
                sigma_1d_pct    REAL,
                features_json   TEXT    NOT NULL,
                weights_json    TEXT    NOT NULL,
                signals_json    TEXT,
                regime_json     TEXT,
                code_version    TEXT,
                schema_version  TEXT    NOT NULL DEFAULT '1.0',
                prediction_id   INTEGER,
                trade_log_id    INTEGER
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prov_asset    ON decision_provenance (asset)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prov_created  ON decision_provenance (created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prov_direction ON decision_provenance (direction)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prov_type     ON decision_provenance (decision_type)"
        )

        # ─── agent_predictions: per-agent probabilistic forecast tracking ────
        # Хранит probabilistic forecast'ы Bull/Bear/Verifier/Synth (а также
        # любых других ролей в будущем) после каждого дебата. Через `horizon_h`
        # часов фоновая задача в scheduler.py резолвит прогноз: фетчит
        # реализованную цену, считает Brier score. По истории считается
        # калибровка агента — см. core/agent_calibration.py.
        #
        # Отличие от существующей таблицы `predictions`:
        #   * `predictions` — это **итоговый** trade-call дебата (один на
        #     отчёт, с direction/entry/target/stop).
        #   * `agent_predictions` — **per-agent**, probabilistic (только p_up
        #     и threshold). Используется для калибровки конкретных голосов.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS agent_predictions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                debate_id       TEXT,
                asset           TEXT    NOT NULL,
                agent_role      TEXT    NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                p_up            REAL    NOT NULL,
                threshold_pct   REAL    NOT NULL,
                ref_price       REAL    NOT NULL,
                resolve_at      TEXT    NOT NULL,
                resolved        INTEGER NOT NULL DEFAULT 0,
                resolved_at     TEXT,
                realized_price  REAL,
                realized_y      INTEGER,
                brier_score     REAL,
                CHECK (p_up >= 0.0 AND p_up <= 1.0),
                CHECK (horizon_minutes > 0),
                CHECK (threshold_pct >= 0.0)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_pred_resolve_at "
            "ON agent_predictions (resolve_at) WHERE resolved = 0"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_pred_role_resolved "
            "ON agent_predictions (agent_role, resolved)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_pred_asset_created "
            "ON agent_predictions (asset, created_at)"
        )

        # ─── microstructure_snapshots: cross-exchange L2 depth metrics ───────
        # Каждый snapshot — это уже агрегированная сводка по всем venue
        # (Binance/Bybit/OKX/Bitget/Hyperliquid). Сырые per-venue стаканы
        # не сохраняем — они слишком объёмные. Здесь только финальные метрики.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS microstructure_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
                asset               TEXT    NOT NULL,
                timestamp_ms        INTEGER NOT NULL,
                mid_price           REAL    NOT NULL,
                bid_depth_usd       REAL    NOT NULL,
                ask_depth_usd       REAL    NOT NULL,
                asymmetry           REAL,
                quoted_spread_bps   REAL,
                venue_count         INTEGER NOT NULL,
                venues_csv          TEXT,
                vacuum_flag         INTEGER NOT NULL DEFAULT 0,
                direction_bias      INTEGER NOT NULL DEFAULT 0,
                severity            REAL    NOT NULL DEFAULT 0.0,
                baseline_depth_usd  REAL,
                drop_pct_observed   REAL,
                CHECK (mid_price >= 0),
                CHECK (bid_depth_usd >= 0),
                CHECK (ask_depth_usd >= 0),
                CHECK (venue_count >= 0),
                CHECK (severity >= 0 AND severity <= 1),
                CHECK (direction_bias IN (-1, 0, 1))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ms_snapshot_asset_ts "
            "ON microstructure_snapshots (asset, timestamp_ms)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ms_snapshot_asset_created "
            "ON microstructure_snapshots (asset, created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ms_snapshot_vacuum "
            "ON microstructure_snapshots (asset, vacuum_flag) WHERE vacuum_flag = 1"
        )

        # ─── narrative_documents / narrative_clusters (killer 3/8) ───────────
        # Хранит «документы» (статьи/посты) с их embeddings (JSON-list of floats)
        # и онлайн-кластеризованные нарративные треды (centroid, n_docs, reach,
        # anchor centroid для drift detection). См.
        # market_indicators/narratives.py для математики.

        await db.execute("""
            CREATE TABLE IF NOT EXISTS narrative_documents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                doc_id          TEXT    NOT NULL UNIQUE,
                source          TEXT    NOT NULL,
                title           TEXT    NOT NULL DEFAULT '',
                content         TEXT    NOT NULL DEFAULT '',
                asset_hint      TEXT,
                published_at    TEXT,
                embedding_json  TEXT    NOT NULL,
                cluster_id      INTEGER NOT NULL,
                CHECK (length(doc_id) > 0)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_narr_doc_cluster_created "
            "ON narrative_documents (cluster_id, created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_narr_doc_source_created "
            "ON narrative_documents (source, created_at)"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS narrative_clusters (
                cluster_id              INTEGER PRIMARY KEY,
                created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen_at            TEXT,
                centroid_json           TEXT NOT NULL,
                n_docs                  INTEGER NOT NULL DEFAULT 0,
                sources_json            TEXT NOT NULL DEFAULT '[]',
                anchor_centroid_json    TEXT,
                anchor_at               TEXT,
                label                   TEXT,
                CHECK (n_docs >= 0)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_narr_cluster_lastseen "
            "ON narrative_clusters (last_seen_at)"
        )

        # Snapshots of cluster centroids over time — для drift detection
        # (anchor — это centroid на момент N часов назад). Pruning делается
        # отдельным retention-job'ом.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS narrative_cluster_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                cluster_id      INTEGER NOT NULL,
                n_docs          INTEGER NOT NULL,
                centroid_json   TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_narr_snap_cluster_created "
            "ON narrative_cluster_snapshots (cluster_id, created_at)"
        )

        # ─── funding_term_snapshots (Tier A #4) ──────────────────────────────
        # Снимки funding rate term structure: spot perp funding, 30d basis,
        # 90d basis, slope, inversion флаг. См. market_indicators/
        # funding_term_structure.py для математики.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS funding_term_snapshots (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                asset                   TEXT NOT NULL,
                timestamp_ms            INTEGER NOT NULL,
                spot_funding_annual     REAL,
                monthly_basis_annual    REAL,
                quarterly_basis_annual  REAL,
                slope_annual            REAL,
                is_inverted             INTEGER NOT NULL DEFAULT 0,
                venues_csv              TEXT,
                CHECK (is_inverted IN (0, 1))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fts_asset_ts "
            "ON funding_term_snapshots (asset, timestamp_ms DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fts_inverted "
            "ON funding_term_snapshots (asset, is_inverted) WHERE is_inverted = 1"
        )

        # ─── options_skew_snapshots (Tier A #7) ──────────────────────────────
        # Снимки опционного skew (Deribit): ATM IV ближнего/дальнего expiry,
        # 25-delta risk reversal, term slope. См.
        # market_indicators/options_skew.py для математики.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS options_skew_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                currency            TEXT NOT NULL,
                timestamp_ms        INTEGER NOT NULL,
                underlying_price    REAL NOT NULL DEFAULT 0,
                near_expiry_days    INTEGER,
                near_atm_iv         REAL,
                near_rr_25d         REAL,
                far_expiry_days     INTEGER,
                far_atm_iv          REAL,
                far_rr_25d          REAL,
                atm_iv_term_slope   REAL,
                skew_class          TEXT NOT NULL DEFAULT 'unknown',
                venues_csv          TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_oskew_currency_ts "
            "ON options_skew_snapshots (currency, timestamp_ms DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_oskew_class "
            "ON options_skew_snapshots (currency, skew_class, created_at)"
        )

        # ─── stablecoin_supply_snapshots (Tier A #8) ────────────────────────
        # Снимки totalSupply стейблов (USDT/USDC) per-chain.
        # raw_supply_units_str — строкой, т.к. может превышать INT64.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stablecoin_supply_snapshots (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at            TEXT NOT NULL DEFAULT (datetime('now')),
                token                 TEXT NOT NULL,
                chain                 TEXT NOT NULL,
                raw_supply_units_str  TEXT NOT NULL,
                decimals              INTEGER NOT NULL,
                timestamp_ms          INTEGER NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sc_supply_token_chain_ts "
            "ON stablecoin_supply_snapshots (token, chain, timestamp_ms DESC)"
        )

        # ─── stablecoin_flow_snapshots (Tier A #8) ──────────────────────────
        # Аггрегированные flow signals по токену (по всем chains).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stablecoin_flow_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT NOT NULL DEFAULT (datetime('now')),
                token               TEXT NOT NULL,
                timestamp_ms        INTEGER NOT NULL,
                supply_total_usd    REAL NOT NULL DEFAULT 0,
                delta_24h_usd       REAL,
                delta_pct_24h       REAL,
                flow_class          TEXT NOT NULL DEFAULT 'unknown',
                chains_csv          TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sc_flow_token_ts "
            "ON stablecoin_flow_snapshots (token, timestamp_ms DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sc_flow_class "
            "ON stablecoin_flow_snapshots (token, flow_class, created_at)"
        )

        # ─── liquidation_events (Cascade post-mortem) ───────────────────────
        # Сырой стрим публичных liquidations (forceOrder) с Binance/Bybit.
        # Заполняется WS-listener'ом из cascade_post_mortem_io.py.
        # Очищается retention-job'ом (default 7 дней), т.к. для post-mortem
        # достаточно последних 48ч rolling-окна.
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_liq_events_ts "
            "ON liquidation_events (timestamp_ms DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_liq_events_venue_ts "
            "ON liquidation_events (venue, timestamp_ms DESC)"
        )

        # ─── cascade_post_mortems (Cascade post-mortem) ─────────────────────
        # История сработавших каскадных post-mortem'ов. summary_md — готовый
        # markdown для TG, snapshot_json — индикаторы на момент срабатывания
        # (regime/smart-money/liquidation magnet/ETF flow/funding/options skew).
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
                posted_to_tg    INTEGER NOT NULL DEFAULT 0,
                CHECK (window_type IN ('rolling_24h', 'rolling_4h_acute')),
                CHECK (posted_to_tg IN (0, 1))
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cpm_triggered_at "
            "ON cascade_post_mortems (triggered_at DESC)"
        )

        # ─── p2p_audit_log (P2P self-audit) ────────────────────────────────
        # Журнал показанных P2P opportunities. Backcheck-loop через
        # P2P_AUDIT_BACKCHECK_DELAY_MIN перечитывает orderbook и заполняет
        # realised_spread_pct + status. Используется в /p2paudit и
        # для адаптивной подстройки P2P_ARBITRAGE_MIN_SPREAD_PCT.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS p2p_audit_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_key   TEXT    NOT NULL,
                asset             TEXT    NOT NULL,
                fiat              TEXT    NOT NULL,
                venue_buy         TEXT    NOT NULL,
                venue_sell        TEXT    NOT NULL,
                buy_price         REAL    NOT NULL,
                sell_price        REAL    NOT NULL,
                gross_spread_pct  REAL    NOT NULL,
                net_spread_pct    REAL    NOT NULL,
                risk_level        TEXT    NOT NULL,
                shown_at_ms       INTEGER NOT NULL,
                realised_at_ms    INTEGER,
                realised_spread_pct REAL,
                status            TEXT    NOT NULL DEFAULT 'pending'
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_p2p_audit_shown_at "
            "ON p2p_audit_log (shown_at_ms DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_p2p_audit_status "
            "ON p2p_audit_log (status, shown_at_ms DESC)"
        )

        # ─── advisor_plans (M2 portfolio + /advise persistence) ────────────
        # Хранит снапшоты AdvisorPlan для:
        # 1) is_portfolio=0 — историю /advise вызовов (для /explain и
        #    «последний план»).
        # 2) is_portfolio=1 — активные виртуальные позиции для watcher'а
        #    (закрывает по SL/TP, шлёт алерты юзеру).
        # status переходит active → stopped/tp1/tp2/tp3/closed когда
        # текущая цена пересекает уровень. PnL пересчитывается в close.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS advisor_plans (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                  INTEGER NOT NULL,
                asset                    TEXT    NOT NULL,
                action                   TEXT    NOT NULL,
                direction                TEXT,
                confidence_pct           INTEGER NOT NULL DEFAULT 0,
                entry_price              REAL,
                stop_price               REAL,
                stop_distance_pct        REAL,
                risk_reward              REAL,
                tp_levels_json           TEXT,
                position_usd             REAL,
                position_pct_of_capital  REAL,
                capital_usd              REAL,
                horizon_human            TEXT,
                invalidation             TEXT,
                rationale_json           TEXT,
                btc_overlay_note         TEXT,
                risk_profile             TEXT,
                narrative                TEXT,
                is_portfolio             INTEGER NOT NULL DEFAULT 0,
                status                   TEXT    NOT NULL DEFAULT 'active',
                created_at               INTEGER NOT NULL,
                closed_at                INTEGER,
                close_price              REAL,
                close_reason             TEXT,
                pnl_usd                  REAL,
                pnl_pct                  REAL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_advisor_plans_user_active "
            "ON advisor_plans (user_id, is_portfolio, status, created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_advisor_plans_watcher "
            "ON advisor_plans (is_portfolio, status, asset)"
        )

        await db.commit()

    logger.info("✅ База данных инициализирована")


# ─── Пользователи ─────────────────────────────────────────────────────────────

async def upsert_user(user_id: int, username: str = "", first_name: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name, last_active, signals_sub)
            VALUES (?, ?, ?, datetime('now'), 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_active = datetime('now')
        """, (user_id, username or "", first_name or ""))
        await db.commit()


async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row)


def _row_to_dict(row):
    """Convert sqlite3.Row to dict - works with both old and new aiosqlite versions."""
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


async def increment_requests(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET
                requests_today = requests_today + 1,
                requests_total = requests_total + 1,
                last_active = datetime('now')
            WHERE user_id = ?
        """, (user_id,))
        await db.commit()


async def reset_daily_counts():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET requests_today = 0")
        await db.commit()


async def save_debate_session(user_id: int, report: str):
    """Снимок отчёта для листания дебатов после рестарта / другого воркера."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO debate_sessions (user_id, report, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                report = excluded.report,
                updated_at = datetime('now')
        """, (user_id, report))
        await db.commit()
    logger.info("debate_sessions сохранён user_id=%s (%s симв.)", user_id, len(report or ""))


async def get_debate_session(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT report FROM debate_sessions WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_daily_subscribers() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE daily_sub = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def set_daily_sub(user_id: int, enabled: bool, time: str = "08:00"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET daily_sub = ?, sub_time = ?
            WHERE user_id = ?
        """, (1 if enabled else 0, time, user_id))
        await db.commit()


async def get_signals_subscribers() -> list[dict]:
    """Возвращает пользователей с включёнными сигналами."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE signals_sub = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def set_signals_sub(user_id: int, enabled: bool):
    """Включить/выключить сигналы для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET signals_sub = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id)
        )
        await db.commit()


async def get_user_signals_status(user_id: int) -> bool:
    """Проверить статус подписки на сигналы."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT signals_sub FROM users WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] == 1 if row else False


# ─── Прогнозы / Track Record ──────────────────────────────────────────────────

async def save_prediction(
    asset: str,
    direction: str,
    entry_price: float,
    target_price: float,
    stop_loss: float,
    timeframe: str,
    source_news: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO predictions
                (asset, direction, entry_price, target_price, stop_loss, timeframe, source_news)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (asset, direction, entry_price, target_price, stop_loss, timeframe, source_news[:500]))
        await db.commit()
        return cursor.lastrowid


async def get_pending_predictions() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM predictions
            WHERE result = 'pending'
            AND created_at < datetime('now', '-1 day')
            ORDER BY created_at DESC
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_prediction_result(
    pred_id: int,
    result: str,
    result_price: float,
    pnl_pct: float,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE predictions SET
                result = ?,
                result_price = ?,
                result_at = datetime('now'),
                pnl_pct = ?
            WHERE id = ?
        """, (result, result_price, pnl_pct, pred_id))
        await db.commit()


async def import_forecasts_from_markdown():
    """Импорт прогнозов из локального FORECASTS.md в SQLite."""
    import re
    import os
    forecast_path = os.path.join(os.path.dirname(__file__), "FORECASTS.md")
    if not os.path.exists(forecast_path):
        logger.warning("FORECASTS.md не найден")
        return

    with open(forecast_path, "r", encoding="utf-8") as f:
        content = f.read()

    predictions = []
    table_match = re.search(r"\| № \| Дата \|.*?\n\|[-|]+\|.*?\n((?:\|.*?\n)+)", content, re.DOTALL)
    if not table_match:
        logger.warning("Таблица прогнозов не найдена в FORECASTS.md")
        return

    rows = table_match.group(1).strip().split("\n")
    for row in rows:
        parts = [p.strip() for p in row.split("|")[1:-1]]
        if len(parts) < 8:
            continue
        try:
            date_str = parts[1]
            pred_type = parts[2].strip()
            asset = parts[3].strip()
            forecast = parts[4].strip()
            fact = parts[5].strip()
            result_text = parts[6].strip().lower()
            accuracy_text = parts[7].strip().replace("%", "").replace("*", "")
            try:
                pnl_pct = float(accuracy_text)
            except (TypeError, ValueError):
                pnl_pct = 0.0
            if "неверно" in result_text:
                result = "loss"
            elif "осторожность" in result_text:
                result = "caution"
                pnl_pct = 100.0
            elif "верно" in result_text or "точ" in result_text:
                result = "win"
            else:
                result = "win"
            date_obj = datetime.strptime(date_str, "%d.%m.%Y")
            created_at = date_obj.strftime("%Y-%m-%d %H:%M:%S")

            if "russia" in pred_type.lower() or "edge" in pred_type.lower():
                report_type = "russia"
            else:
                report_type = "global"

            predictions.append({
                "created_at": created_at,
                "asset": asset,
                "direction": forecast,
                "result": result,
                "pnl_pct": pnl_pct,
                "prediction_type": pred_type,
                "forecast": forecast,
                "fact": fact,
                "report_type": report_type
            })
        except Exception as e:
            logger.debug(f"Ошибка парсинга строки: {e}")

    if predictions:
        async with aiosqlite.connect(DB_PATH) as db:
            # DATA MIGRATION: clear old predictions before re-import from FORECASTS.md
            await db.execute("DELETE FROM predictions WHERE created_at LIKE '2026-03-%'")
            await db.commit()
            for p in predictions:
                await db.execute("""
                    INSERT INTO predictions (created_at, asset, direction, entry_price, target_price, result, pnl_pct, prediction_type, forecast, fact, report_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    p["created_at"],
                    p["asset"],
                    p["direction"],
                    None,
                    None,
                    p["result"],
                    p["pnl_pct"],
                    p.get("prediction_type", ""),
                    p.get("forecast", ""),
                    p.get("fact", ""),
                    p.get("report_type", "global")
                ))
            await db.commit()
        logger.info(f"✅ Импортировано {len(predictions)} прогнозов из FORECASTS.md")
    else:
        logger.warning("Не удалось распарсить прогнозы из FORECASTS.md")


async def get_track_record(report_type: str = None) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        where_clause = ""
        params = []
        if report_type:
            where_clause = " AND report_type = ?"
            params = [report_type]

        async with db.execute(f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result = 'caution' THEN 1 ELSE 0 END) as cautions,
                SUM(CASE WHEN result = 'pending' THEN 1 ELSE 0 END) as pending,
                AVG(CASE WHEN result != 'pending' THEN pnl_pct END) as avg_pnl,
                MAX(pnl_pct) as best_call,
                MIN(pnl_pct) as worst_call
            FROM predictions
            WHERE result != 'expired'{where_clause}
        """, params) as cursor:
            stats = dict(await cursor.fetchone())

        async with db.execute(f"""
            SELECT asset, direction, entry_price, result, pnl_pct, created_at, prediction_type, forecast, fact
            FROM predictions
            WHERE result != 'pending'{where_clause}
            ORDER BY created_at DESC
            LIMIT 50
        """) as cursor:
            recent = [dict(r) for r in await cursor.fetchall()]

        async with db.execute(f"""
            SELECT asset,
                COUNT(*) as calls,
                SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                AVG(pnl_pct) as avg_pnl
            FROM predictions
            WHERE result IN ('win','loss'){where_clause}
            GROUP BY asset
            HAVING calls >= 2
            ORDER BY avg_pnl DESC
            LIMIT 5
        """) as cursor:
            by_asset = [dict(r) for r in await cursor.fetchall()]

        return {"stats": stats, "recent": recent, "by_asset": by_asset}


# ─── Per-agent calibration (agent_predictions) ───────────────────────────────
#
# См. подробное обоснование в core/agent_calibration.py docstring. CRUD-helpers
# для probabilistic forecast'ов отдельных агентов (Bull/Bear/Verifier/Synth).

async def save_agent_prediction(
    *,
    debate_id: str | None,
    asset: str,
    agent_role: str,
    horizon_minutes: int,
    p_up: float,
    threshold_pct: float,
    ref_price: float,
    resolve_at: str,
) -> int:
    """Сохранить probabilistic forecast агента. Возвращает id строки.

    resolve_at — ISO-timestamp когда прогноз должен быть резолвнут (created_at + horizon).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO agent_predictions
                (debate_id, asset, agent_role, horizon_minutes,
                 p_up, threshold_pct, ref_price, resolve_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (debate_id, asset, agent_role, int(horizon_minutes),
             float(p_up), float(threshold_pct), float(ref_price), resolve_at),
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_agent_predictions(
    *, now_iso: str | None = None, limit: int = 200
) -> list[dict]:
    """Прогнозы, у которых resolve_at уже прошёл, но resolved=0."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cond = "resolve_at <= ?" if now_iso else "resolve_at <= datetime('now')"
        params: tuple = (now_iso, limit) if now_iso else (limit,)
        async with db.execute(
            f"""
            SELECT * FROM agent_predictions
            WHERE resolved = 0 AND {cond}
            ORDER BY resolve_at ASC
            LIMIT ?
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def resolve_agent_prediction(
    *,
    prediction_id: int,
    realized_price: float,
    realized_y: bool,
    brier_score: float,
) -> None:
    """Помечает прогноз как resolved + пишет реализованную цену и Brier."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE agent_predictions SET
                resolved       = 1,
                resolved_at    = datetime('now'),
                realized_price = ?,
                realized_y     = ?,
                brier_score    = ?
            WHERE id = ? AND resolved = 0
            """,
            (float(realized_price), int(bool(realized_y)),
             float(brier_score), int(prediction_id)),
        )
        await db.commit()


async def get_agent_calibration_history(
    *,
    agent_role: str,
    asset: str | None = None,
    lookback_days: int = 30,
    limit: int = 500,
) -> list[dict]:
    """Resolved прогнозы агента за окно. Используется compute_agent_stats."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if asset:
            q = """
                SELECT * FROM agent_predictions
                WHERE resolved = 1 AND agent_role = ? AND asset = ?
                  AND resolved_at >= datetime('now', ?)
                ORDER BY resolved_at DESC
                LIMIT ?
            """
            params = (agent_role, asset, f"-{int(lookback_days)} days", limit)
        else:
            q = """
                SELECT * FROM agent_predictions
                WHERE resolved = 1 AND agent_role = ?
                  AND resolved_at >= datetime('now', ?)
                ORDER BY resolved_at DESC
                LIMIT ?
            """
            params = (agent_role, f"-{int(lookback_days)} days", limit)
        async with db.execute(q, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ─── Microstructure snapshots (cross-exchange L2 depth) ──────────────────────
#
# См. market_indicators/microstructure.py для смысла полей. Здесь — голые
# CRUD-обёртки. Хранятся уже agregated по venue метрики; сырые стаканы не
# сохраняем (слишком объёмные).

async def save_microstructure_snapshot(
    *,
    asset: str,
    timestamp_ms: int,
    mid_price: float,
    bid_depth_usd: float,
    ask_depth_usd: float,
    asymmetry: float | None,
    quoted_spread_bps: float | None,
    venue_count: int,
    venues_csv: str,
    vacuum_flag: bool,
    direction_bias: int,
    severity: float,
    baseline_depth_usd: float | None,
    drop_pct_observed: float | None,
) -> int:
    """Сохранить agregated microstructure snapshot. Возвращает id строки.

    NaN/inf в asymmetry/quoted_spread_bps конвертим в NULL (SQLite-friendly).
    """
    import math as _math

    def _nan_to_none(v: float | None) -> float | None:
        if v is None:
            return None
        try:
            f = float(v)
            return None if (_math.isnan(f) or _math.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO microstructure_snapshots (
                asset, timestamp_ms, mid_price,
                bid_depth_usd, ask_depth_usd,
                asymmetry, quoted_spread_bps,
                venue_count, venues_csv,
                vacuum_flag, direction_bias, severity,
                baseline_depth_usd, drop_pct_observed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(asset), int(timestamp_ms), float(mid_price),
                float(bid_depth_usd), float(ask_depth_usd),
                _nan_to_none(asymmetry), _nan_to_none(quoted_spread_bps),
                int(venue_count), str(venues_csv or ""),
                1 if vacuum_flag else 0,
                int(direction_bias),
                max(0.0, min(1.0, float(severity))),
                _nan_to_none(baseline_depth_usd),
                _nan_to_none(drop_pct_observed),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_microstructure_baseline_depth(
    *, asset: str, lookback_hours: int = 24
) -> float | None:
    """Среднее total_depth (bid+ask USD) за окно. None если < 3 точек."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT AVG(bid_depth_usd + ask_depth_usd) AS avg_depth,
                   COUNT(*) AS n
            FROM microstructure_snapshots
            WHERE asset = ?
              AND created_at >= datetime('now', ?)
            """,
            (str(asset), f"-{int(lookback_hours)} hours"),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            avg_depth, n = row
            if n is None or int(n) < 3 or avg_depth is None:
                return None
            return float(avg_depth)


async def get_recent_microstructure_snapshots(
    *, asset: str, limit: int = 50
) -> list[dict]:
    """Последние snapshot'ы (для /microstructure CLI / dashboard в будущем)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM microstructure_snapshots
            WHERE asset = ?
            ORDER BY timestamp_ms DESC
            LIMIT ?
            """,
            (str(asset), int(limit)),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


# ─── Narrative drift tracker (killer 3/8) ────────────────────────────────────
#
# См. market_indicators/narratives.py + narratives_io.py для семантики.
# Здесь — только тонкие async-обёртки над SQL.

async def narrative_document_exists(*, doc_id: str) -> bool:
    """True если doc_id уже сохранён (dedup)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM narrative_documents WHERE doc_id = ? LIMIT 1",
            (str(doc_id),),
        ) as cursor:
            return (await cursor.fetchone()) is not None


async def save_narrative_document(
    *,
    doc_id: str,
    source: str,
    title: str,
    content: str,
    asset_hint: str | None,
    published_at: str | None,
    embedding_json: str,
    cluster_id: int,
) -> int:
    """Сохранить документ. Возвращает id (или существующую запись если doc_id
    уже есть — INSERT OR IGNORE)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO narrative_documents (
                doc_id, source, title, content,
                asset_hint, published_at, embedding_json, cluster_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(doc_id), str(source), str(title or ""), str(content or ""),
                asset_hint, published_at, str(embedding_json), int(cluster_id),
            ),
        )
        await db.commit()
        rowid = cursor.lastrowid or 0
        if rowid == 0:
            # запись уже была — возьмём её id
            async with db.execute(
                "SELECT id FROM narrative_documents WHERE doc_id = ?",
                (str(doc_id),),
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0
        return int(rowid)


async def upsert_narrative_cluster(
    *,
    cluster_id: int,
    centroid_json: str,
    n_docs: int,
    sources_json: str,
    created_at: str | None,
    last_seen_at: str | None,
    label: str | None,
) -> None:
    """Upsert кластера. При insert также пишет snapshot центроида (для drift)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO narrative_clusters
                (cluster_id, centroid_json, n_docs, sources_json,
                 created_at, last_seen_at, label)
            VALUES (?, ?, ?, ?, COALESCE(?, datetime('now')), ?, ?)
            ON CONFLICT(cluster_id) DO UPDATE SET
                centroid_json   = excluded.centroid_json,
                n_docs          = excluded.n_docs,
                sources_json    = excluded.sources_json,
                last_seen_at    = excluded.last_seen_at,
                label           = COALESCE(excluded.label, label)
            """,
            (
                int(cluster_id), str(centroid_json), int(n_docs), str(sources_json),
                created_at, last_seen_at, label,
            ),
        )
        await db.execute(
            """
            INSERT INTO narrative_cluster_snapshots (cluster_id, n_docs, centroid_json)
            VALUES (?, ?, ?)
            """,
            (int(cluster_id), int(n_docs), str(centroid_json)),
        )
        await db.commit()


async def load_narrative_clusters(*, limit: int = 5000) -> list[dict]:
    """Все кластера для онлайн assignment (centroids нужны в памяти)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM narrative_clusters ORDER BY last_seen_at DESC LIMIT ?",
            (int(limit),),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def load_narrative_anchor_centroid(
    *, cluster_id: int, hours_ago: float
) -> list[float] | None:
    """Centroid из snapshot'а, который был ≥ `hours_ago` часов назад. Берём
    ближайший по времени (наиболее свежий из «достаточно старых»). None если
    нет такого snapshot'а."""
    import json as _json
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT centroid_json
            FROM narrative_cluster_snapshots
            WHERE cluster_id = ?
              AND created_at <= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (int(cluster_id), f"-{float(hours_ago)} hours"),
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row["centroid_json"]:
                return None
            try:
                vec = _json.loads(row["centroid_json"])
            except (_json.JSONDecodeError, TypeError):
                return None
            return [float(x) for x in vec]


async def get_recent_narrative_documents(
    *, cluster_id: int, limit: int = 20
) -> list[dict]:
    """Последние N документов в кластере. Используется CLI / future agents."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, created_at, source, title, content, asset_hint,
                   published_at, cluster_id
            FROM narrative_documents
            WHERE cluster_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(cluster_id), int(limit)),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_active_narratives(*, limit: int = 10) -> list[dict]:
    """Top-N кластеров по последней активности (last_seen_at DESC)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT cluster_id, n_docs, sources_json, label,
                   created_at, last_seen_at
            FROM narrative_clusters
            ORDER BY last_seen_at DESC NULLS LAST
            LIMIT ?
            """,
            (int(limit),),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def cleanup_old_narrative_data(*, retention_days: int = 180) -> int:
    """Удалить документы и snapshot'ы старше retention_days. Возвращает
    общее число удалённых строк."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur1 = await db.execute(
            "DELETE FROM narrative_documents WHERE created_at < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        cur2 = await db.execute(
            "DELETE FROM narrative_cluster_snapshots WHERE created_at < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        await db.commit()
        return int((cur1.rowcount or 0) + (cur2.rowcount or 0))


# ─── Funding term structure (Tier A #4) ──────────────────────────────────────


def _nan_to_none_real(value: float | None) -> float | None:
    """NaN / inf → None для безопасного INSERT в SQLite REAL."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


async def save_funding_term_snapshot(
    *,
    asset: str,
    timestamp_ms: int,
    spot_funding_annual: float | None,
    monthly_basis_annual: float | None,
    quarterly_basis_annual: float | None,
    slope_annual: float | None,
    is_inverted: int,
    venues_csv: str | None,
) -> int:
    """Вставка одного снимка term structure. NaN/inf → NULL."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO funding_term_snapshots (
                asset, timestamp_ms,
                spot_funding_annual, monthly_basis_annual,
                quarterly_basis_annual, slope_annual,
                is_inverted, venues_csv
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(asset).upper(), int(timestamp_ms),
                _nan_to_none_real(spot_funding_annual),
                _nan_to_none_real(monthly_basis_annual),
                _nan_to_none_real(quarterly_basis_annual),
                _nan_to_none_real(slope_annual),
                1 if is_inverted else 0,
                venues_csv,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_recent_funding_term_snapshots(
    *, asset: str, limit: int = 10
) -> list[dict]:
    """Последние снимки по asset (DESC по timestamp_ms)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM funding_term_snapshots
            WHERE asset = ?
            ORDER BY timestamp_ms DESC
            LIMIT ?
            """,
            (str(asset).upper(), int(limit)),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def count_funding_term_inversions(*, asset: str, lookback_hours: float = 168) -> int:
    """Счётчик inverted-снимков за последние N часов."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM funding_term_snapshots
            WHERE asset = ?
              AND is_inverted = 1
              AND created_at >= datetime('now', ?)
            """,
            (str(asset).upper(), f"-{float(lookback_hours)} hours"),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


# ─── Options skew (Tier A #7) ────────────────────────────────────────────────


async def save_options_skew_snapshot(
    *,
    currency: str,
    timestamp_ms: int,
    underlying_price: float,
    near_expiry_days: int | None,
    near_atm_iv: float | None,
    near_rr_25d: float | None,
    far_expiry_days: int | None,
    far_atm_iv: float | None,
    far_rr_25d: float | None,
    atm_iv_term_slope: float | None,
    skew_class: str,
    venues_csv: str | None,
) -> int:
    """Вставка одного снимка options skew. NaN/inf → NULL."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO options_skew_snapshots (
                currency, timestamp_ms, underlying_price,
                near_expiry_days, near_atm_iv, near_rr_25d,
                far_expiry_days, far_atm_iv, far_rr_25d,
                atm_iv_term_slope, skew_class, venues_csv
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(currency).upper(), int(timestamp_ms),
                _nan_to_none_real(underlying_price) or 0.0,
                int(near_expiry_days) if near_expiry_days is not None else None,
                _nan_to_none_real(near_atm_iv),
                _nan_to_none_real(near_rr_25d),
                int(far_expiry_days) if far_expiry_days is not None else None,
                _nan_to_none_real(far_atm_iv),
                _nan_to_none_real(far_rr_25d),
                _nan_to_none_real(atm_iv_term_slope),
                str(skew_class or "unknown"),
                venues_csv,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_recent_options_skew_snapshots(
    *, currency: str, limit: int = 10,
) -> list[dict]:
    """Последние снимки по currency (DESC по timestamp_ms)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM options_skew_snapshots
            WHERE currency = ?
            ORDER BY timestamp_ms DESC
            LIMIT ?
            """,
            (str(currency).upper(), int(limit)),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def count_options_skew_class(
    *, currency: str, skew_class: str, lookback_hours: float = 168,
) -> int:
    """Счётчик снимков нужного skew_class за последние N часов."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM options_skew_snapshots
            WHERE currency = ?
              AND skew_class = ?
              AND created_at >= datetime('now', ?)
            """,
            (str(currency).upper(), str(skew_class), f"-{float(lookback_hours)} hours"),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


# ─── Stablecoin flows (Tier A #8) ────────────────────────────────────────────


async def save_stablecoin_supply_snapshot(
    *,
    token: str,
    chain: str,
    raw_supply_units_str: str,
    decimals: int,
    timestamp_ms: int,
) -> int:
    """Вставка одного supply-snapshot'а (per token+chain).

    raw_supply_units_str — строкой, т.к. реальные значения USDT > 2^63.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO stablecoin_supply_snapshots (
                token, chain, raw_supply_units_str, decimals, timestamp_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(token).upper(),
                str(chain).lower(),
                str(raw_supply_units_str),
                int(decimals),
                int(timestamp_ms),
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_supply_snapshot_at_or_before(
    *, token: str, chain: str, hours_ago: float = 24.0,
) -> dict | None:
    """Ближайший snapshot до (now - hours_ago) для пары token+chain.

    Возвращает None если ничего нет.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM stablecoin_supply_snapshots
            WHERE token = ? AND chain = ?
              AND created_at <= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                str(token).upper(),
                str(chain).lower(),
                f"-{float(hours_ago)} hours",
            ),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_stablecoin_flow_snapshot(
    *,
    token: str,
    timestamp_ms: int,
    supply_total_usd: float,
    delta_24h_usd: float | None,
    delta_pct_24h: float | None,
    flow_class: str,
    chains_csv: str | None,
) -> int:
    """Вставка одного flow-snapshot'а. NaN/inf → NULL."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO stablecoin_flow_snapshots (
                token, timestamp_ms, supply_total_usd,
                delta_24h_usd, delta_pct_24h, flow_class, chains_csv
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(token).upper(), int(timestamp_ms),
                _nan_to_none_real(supply_total_usd) or 0.0,
                _nan_to_none_real(delta_24h_usd),
                _nan_to_none_real(delta_pct_24h),
                str(flow_class or "unknown"),
                chains_csv,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def get_recent_stablecoin_flow_snapshots(
    *, token: str, limit: int = 10,
) -> list[dict]:
    """Последние flow-снимки по токену (DESC по timestamp_ms)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM stablecoin_flow_snapshots
            WHERE token = ?
            ORDER BY timestamp_ms DESC
            LIMIT ?
            """,
            (str(token).upper(), int(limit)),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def count_stablecoin_flow_class(
    *, token: str, flow_class: str, lookback_hours: float = 168,
) -> int:
    """Счётчик flow-снимков нужного flow_class за последние N часов."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM stablecoin_flow_snapshots
            WHERE token = ?
              AND flow_class = ?
              AND created_at >= datetime('now', ?)
            """,
            (str(token).upper(), str(flow_class), f"-{float(lookback_hours)} hours"),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


# ─── Фидбек ───────────────────────────────────────────────────────────────────

async def save_feedback(user_id: int, report_type: str, rating: int, comment: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO feedback (user_id, report_type, rating, comment)
            VALUES (?, ?, ?, ?)
        """, (user_id, report_type, rating, comment))
        await db.commit()


async def get_feedback_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN rating =  1 THEN 1 ELSE 0 END) as positive,
                SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) as negative
            FROM feedback
        """) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row)


# ─── Отчёты ───────────────────────────────────────────────────────────────────

async def log_report(user_id: int, report_type: str, news_used: str, summary: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO reports (user_id, report_type, news_used, summary)
            VALUES (?, ?, ?, ?)
        """, (user_id, report_type, news_used[:1000], summary[:500]))
        await db.commit()


# ─── Статистика для админа ────────────────────────────────────────────────────

async def get_admin_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT COUNT(*) as total FROM users") as c:
            total_users = (await c.fetchone())["total"]

        async with db.execute("""
            SELECT COUNT(*) as active FROM users
            WHERE last_active > datetime('now', '-7 days')
        """) as c:
            active_week = (await c.fetchone())["active"]

        async with db.execute(
            "SELECT COUNT(*) as subs FROM users WHERE daily_sub = 1"
        ) as c:
            subscribers = (await c.fetchone())["subs"]

        async with db.execute("SELECT COUNT(*) as total FROM reports") as c:
            total_reports = (await c.fetchone())["total"]

        return {
            "total_users":   total_users,
            "active_week":   active_week,
            "subscribers":   subscribers,
            "total_reports": total_reports,
        }


async def add_portfolio_position(user_id: int, symbol: str, amount: float, entry_price: float) -> bool:
    """Add or update portfolio position."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO portfolio (user_id, symbol, amount, entry_price)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                amount = excluded.amount,
                entry_price = excluded.entry_price
        """, (user_id, symbol.upper(), amount, entry_price))
        await db.commit()
    return True


async def get_portfolio(user_id: int) -> list[dict]:
    """Get user portfolio."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT symbol, amount, entry_price, added_at
            FROM portfolio WHERE user_id = ?
            ORDER BY added_at DESC
        """, (user_id,)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def remove_portfolio_position(user_id: int, symbol: str) -> bool:
    """Remove position from portfolio."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM portfolio WHERE user_id = ? AND symbol = ?",
                        (user_id, symbol.upper()))
        await db.commit()
    return True


# ─── Backtest Signals ─────────────────────────────────────────────────────────────

async def add_backtest_signal(
    symbol: str,
    direction: str,
    entry_price: float,
    source: str = "daily",
    quantity_pct: float = 1.0,
    notes: str = "",
    trade_log: str = "",
) -> dict:
    """Open a paper trade without changing equity until the trade is closed."""
    config = await get_backtest_config()

    if not config.get("enabled", 1):
        return {"status": "disabled", "message": "Backtest is disabled"}

    if entry_price <= 0:
        return {"status": "invalid", "message": "Entry price must be positive"}

    direction = direction.upper()
    symbol = symbol.upper()
    quantity_pct = min(max(quantity_pct, 0.01), 0.15)  # Max 15% per position
    quantity = 0.0
    position_cost = 0.0
    capital = 0.0  # Will be read from DB

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get current config FIRST
        async with db.execute("SELECT capital FROM backtest_config WHERE id = 1") as cursor:
            config_row = await cursor.fetchone()
            if config_row:
                capital = float(config_row["capital"] if config_row["capital"] is not None else 100.0)
            else:
                await db.execute("INSERT INTO backtest_config (capital, enabled) VALUES (100.0, 1)")
                capital = 100.0

        logger.info(f"add_backtest_signal: capital from config={capital}")

        # Count open positions (max 5 for diversification)
        async with db.execute("""
            SELECT COUNT(*) as cnt FROM backtest_signals WHERE status = 'open'
        """) as cursor:
            count_row = await cursor.fetchone()
            open_count = count_row["cnt"] if count_row else 0

        if open_count >= 5:
            logger.info(f"add_backtest_signal: already {open_count} open positions (max 5), skipping")
            return {
                "status": "max_positions",
                "message": f"Already have {open_count} open positions (max 5)",
                "capital_before": capital,
                "capital_after": capital,
            }

        # Check for existing open position in same symbol
        async with db.execute("""
            SELECT * FROM backtest_signals
            WHERE symbol = ? AND status = 'open'
            ORDER BY created_at DESC LIMIT 1
        """, (symbol,)) as cursor:
            existing_open = await cursor.fetchone()

        if existing_open:
            logger.info(f"add_backtest_signal: already have open position in {symbol}, skipping")
            return {
                "status": "symbol_exists",
                "symbol": existing_open["symbol"],
                "direction": existing_open["direction"],
                "entry_price": existing_open["entry_price"],
                "quantity": existing_open["quantity"] or 0.0,
                "capital_before": capital,
                "capital_after": capital,
            }

        # Calculate quantity and cost
        quantity = (capital * quantity_pct) / entry_price if entry_price > 0 else 0.0
        position_cost = quantity * entry_price

        logger.info(f"add_backtest_signal: qty={quantity}, cost={position_cost}")

        # Deduct position cost from capital
        new_capital = capital - position_cost
        logger.info(f"add_backtest_signal: new_capital after deduct={new_capital}")

        cursor = await db.execute("""
            INSERT INTO backtest_signals (
                symbol, direction, entry_price, status, signal_source, notes, quantity, trade_log
            )
            VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
        """, (symbol, direction, entry_price, source, notes[:500], quantity, trade_log[:4000]))

        # Update capital
        await db.execute("UPDATE backtest_config SET capital = ?, last_updated = datetime('now') WHERE id = 1", (new_capital,))
        await db.commit()

    return {
        "status": "opened",
        "signal_id": cursor.lastrowid,
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "quantity": quantity,
        "capital_before": capital,
        "capital_after": new_capital,
    }


async def close_backtest_signal(signal_id: int, exit_price: float, reason: str = "") -> dict | None:
    """Close a paper trade, realize PnL, and update account equity."""
    if exit_price <= 0:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT * FROM backtest_signals WHERE id = ?", (signal_id,)) as cursor:
            signal = await cursor.fetchone()
            if not signal:
                return None

        async with db.execute("SELECT capital FROM backtest_config WHERE id = 1") as cursor:
            config_row = await cursor.fetchone()
            capital = float(config_row["capital"] if config_row and config_row["capital"] is not None else 100.0)

        if signal["status"] == "closed":
            return {
                "pnl": signal["pnl"] or 0.0,
                "pnl_pct": signal["pnl_pct"] or 0.0,
                "new_capital": capital,
            }

        entry_price = float(signal["entry_price"] or 0.0)
        direction = (signal["direction"] or "").upper()
        quantity = float(signal["quantity"] or 0.0)
        quantity = quantity if quantity > 0 else (capital / entry_price if entry_price > 0 else 0.0)

        if direction == "BUY":
            pnl_per_unit = exit_price - entry_price
        else:
            pnl_per_unit = entry_price - exit_price

        pnl_pct = (pnl_per_unit / entry_price * 100) if entry_price > 0 else 0.0
        pnl = pnl_per_unit * quantity
        new_capital = max(capital + pnl, 0.0)

        old_notes = signal["notes"] or ""
        final_notes = old_notes
        if reason:
            final_notes = f"{old_notes}\n{reason}".strip()

        await db.execute("""
            UPDATE backtest_signals
            SET status = 'closed', exit_price = ?, pnl = ?, pnl_pct = ?, notes = ?
            WHERE id = ?
        """, (exit_price, pnl, pnl_pct, final_notes[:500], signal_id))

        await db.execute("""
            UPDATE backtest_config SET capital = ?, last_updated = datetime('now') WHERE id = 1
        """, (new_capital,))
        await db.commit()

    return {
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "new_capital": new_capital,
        "quantity": quantity,
    }


async def get_backtest_signals() -> list[dict]:
    """Get all backtest signals."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM backtest_signals ORDER BY created_at DESC
        """) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_backtest_stats() -> dict:
    """Get backtest statistics."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl,
                AVG(pnl_pct) as avg_pnl_pct
            FROM backtest_signals WHERE status = 'closed'
        """) as cursor:
            row = await cursor.fetchone()
            if not row:
                return {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "avg_pnl_pct": 0.0}
            d = _row_to_dict(row)
            return {
                "total": d.get("total") or 0,
                "wins": d.get("wins") or 0,
                "losses": d.get("losses") or 0,
                "total_pnl": d.get("total_pnl") or 0.0,
                "avg_pnl_pct": d.get("avg_pnl_pct") or 0.0
            }


# ─── Backtest Config ─────────────────────────────────────────────────────────────

async def get_backtest_config() -> dict:
    """Get backtest configuration (capital, enabled)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM backtest_config WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            if not row:
                return {"capital": 100.0, "enabled": 1}
            d = _row_to_dict(row)
            d["capital"] = d.get("capital") if d.get("capital") is not None else 100.0
            d["enabled"] = d.get("enabled") if d.get("enabled") is not None else 1
            return d


async def update_backtest_capital(new_capital: float) -> dict:
    """Update backtest capital."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE backtest_config SET capital = ?, last_updated = datetime('now') WHERE id = 1
        """, (new_capital,))
        await db.commit()
    return await get_backtest_config()


async def set_backtest_enabled(enabled: bool) -> dict:
    """Enable or disable backtest."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE backtest_config SET enabled = ?, last_updated = datetime('now') WHERE id = 1
        """, (1 if enabled else 0,))
        await db.commit()
    return await get_backtest_config()


async def clear_backtest_signals(reset_capital: float = 500.0) -> None:
    """Wipe all backtest signals and reset capital — used by /backtest_clear."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM backtest_signals")
        await db.execute(
            "UPDATE backtest_config SET capital = ?, last_updated = datetime('now') WHERE id = 1",
            (reset_capital,),
        )
        await db.commit()


# ─── Daily Context ─────────────────────────────────────────────────────────────

def _decode_daily_context_row(row) -> dict | None:
    import json

    if not row:
        return None

    data = _row_to_dict(row)
    data["symbols"] = json.loads(data.get("symbols", "[]") or "[]")
    data["entries"] = json.loads(data.get("entries", "{}") or "{}")
    data["stop_losses"] = json.loads(data.get("stop_losses", "{}") or "{}")
    data["targets"] = json.loads(data.get("targets", "{}") or "{}")
    data["timeframes"] = json.loads(data.get("timeframes", "{}") or "{}")
    pv = data.get("prompt_versions") or "{}"
    try:
        data["prompt_versions"] = json.loads(pv) if isinstance(pv, str) else (pv or {})
    except Exception:
        data["prompt_versions"] = {}
    ms = data.get("model_inputs_snapshot") or "{}"
    try:
        data["model_inputs_snapshot"] = json.loads(ms) if isinstance(ms, str) else (ms or {})
    except Exception:
        data["model_inputs_snapshot"] = {}
    return data


async def save_daily_context(
    verdict: str,
    symbols: list,
    entries: dict,
    stop_losses: dict,
    targets: dict,
    timeframes: dict,
    news_summary: str = "",
    full_report: str = "",
    prompt_versions: dict | None = None,
    model_inputs_snapshot: dict | None = None,
) -> int:
    """Save daily context from /daily and keep recent history for consensus trading."""
    import json

    pv_json = json.dumps(prompt_versions or {}, ensure_ascii=False)[:8000]
    snap_json = json.dumps(model_inputs_snapshot or {}, ensure_ascii=False)[:DIGEST_SNAPSHOT_MAX_CHARS]

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO daily_context (
                verdict, symbols, entries, stop_losses, targets, timeframes, news_summary, expires_at,
                prompt_versions, model_inputs_snapshot, full_report
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+72 hours'), ?, ?, ?)
        """, (
            verdict,
            json.dumps(symbols),
            json.dumps(entries),
            json.dumps(stop_losses),
            json.dumps(targets),
            json.dumps(timeframes),
            news_summary[:1500],
            pv_json,
            snap_json,
            full_report,
        ))

        await db.execute("""
            DELETE FROM daily_context
            WHERE id NOT IN (
                SELECT id FROM daily_context ORDER BY created_at DESC, id DESC LIMIT 30
            )
        """)
        await db.commit()
        return cursor.lastrowid


async def get_daily_context() -> dict | None:
    """Get the latest saved daily context for signal trading."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM daily_context ORDER BY created_at DESC, id DESC LIMIT 1
        """) as cursor:
            row = await cursor.fetchone()
            return _decode_daily_context_row(row)


async def get_recent_daily_contexts(limit: int = 3, max_age_hours: int | None = 72) -> list[dict]:
    """Get several recent daily contexts for digest-consensus trading."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if max_age_hours is None:
            query = """
                SELECT * FROM daily_context
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """
            params = (limit,)
        else:
            query = f"""
                SELECT * FROM daily_context
                WHERE created_at >= datetime('now', '-{int(max_age_hours)} hours')
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """
            params = (limit,)

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [_decode_daily_context_row(row) for row in rows if row]


async def append_trade_decision_log(cycle_type: str, payload: dict, signal_id: int | None = None) -> int:
    """Аудит решений автотрейда / сигналов (JSON payload)."""
    import json

    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > 65000:
        raw = raw[:65000] + '"…[truncated]"}'

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO trade_decision_log (cycle_type, signal_id, payload)
            VALUES (?, ?, ?)
            """,
            (cycle_type, signal_id, raw),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_recent_trade_decisions(limit: int = 5) -> list[dict]:
    import json

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, created_at, cycle_type, signal_id, payload
            FROM trade_decision_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            out = []
            for row in rows:
                item = _row_to_dict(row)
                try:
                    item["payload"] = json.loads(item.get("payload") or "{}")
                except Exception:
                    item["payload"] = {}
                out.append(item)
            return out


# ─── Recent Predictions for Context ─────────────────────────────────────────────

async def get_recent_predictions(days: int = 5, limit: int = 10) -> list[dict]:
    """Get recent predictions for context in analysis."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"""
            SELECT * FROM predictions
            WHERE created_at > datetime('now', '-{days} days')
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_predictions_summary(days: int = 5) -> str:
    """Get formatted summary of recent predictions for AI context."""
    predictions = await get_recent_predictions(days=days, limit=20)

    if not predictions:
        return "Нет прошлых прогнозов за последние дни."

    lines = ["=== ПРОШЛЫЕ ПРОГНОЗЫ ==="]

    # Group by asset
    by_asset = {}
    for p in predictions:
        asset = p.get("asset", "UNKNOWN")
        if asset not in by_asset:
            by_asset[asset] = []
        by_asset[asset].append(p)

    for asset, preds in by_asset.items():
        lines.append(f"\n{asset}:")
        for p in preds[:3]:  # Max 3 per asset
            direction = p.get("direction", "")
            entry = p.get("entry_price") or 0
            target = p.get("target_price") or 0
            result = p.get("result", "pending")
            date = p.get("created_at", "")[:10]

            if result == "pending":
                lines.append(f"  {date}: {direction} вход=${entry:.0f} цель=${target:.0f} — в ожидании")
            elif result == "win":
                lines.append(f"  {date}: {direction} вход=${entry:.0f} цель=${target:.0f} — ✅ WIN")
            elif result == "loss":
                lines.append(f"  {date}: {direction} вход=${entry:.0f} цель=${target:.0f} — 🔴 LOSS")
            else:
                lines.append(f"  {date}: {direction} вход=${entry:.0f} цель=${target:.0f} — {result}")

    # Calculate accuracy
    closed = [p for p in predictions if p.get("result") in ("win", "loss")]
    wins = len([p for p in closed if p.get("result") == "win"])
    accuracy = (wins / len(closed) * 100) if closed else 0

    lines.append(f"\nТочность: {wins}/{len(closed)} = {accuracy:.0f}%")
    lines.append("=========================")

    return "\n".join(lines)
