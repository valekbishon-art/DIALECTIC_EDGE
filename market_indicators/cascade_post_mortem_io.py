"""Cascade post-mortem (I/O module).

WS-listener для публичных liquidation streams (Binance + Bybit) +
agg-loop, который раз в N секунд проверяет триггер, собирает snapshot
индикаторов и пишет post-mortem в SQLite + Telegram.

Архитектура:

    [Binance WS !forceOrder@arr] ──┐
                                    ├──► insert_liquidation_event() ──► SQLite (liquidation_events)
    [Bybit WS allLiquidation.*]  ──┘
                                                                            │
    [scheduler agg-loop, каждые AGG_INTERVAL_S сек]                          │
                                                                            ▼
        fetch_window_aggregates() ──► should_trigger() ──► fetch_snapshot() ──► persist_cascade_post_mortem() ──► TG post

WS-клиент использует aiohttp.WSConnection (уже зависимость репо), без
новых pip-зависимостей. Auto-reconnect с экспоненциальным backoff.

Feature flag: `FEATURE_CASCADE_POST_MORTEM=0` (default OFF).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

import aiosqlite

from config import DB_PATH

from .cascade_post_mortem import (
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_THRESHOLD_24H_USD,
    DEFAULT_THRESHOLD_4H_ACUTE_USD,
    SIDE_LONG,
    SIDE_SHORT,
    CascadeSnapshot,
    LiquidationEvent,
    TriggerDecision,
    aggregate_24h,
    aggregate_4h,
    format_post_mortem_markdown,
    should_trigger,
)

logger = logging.getLogger(__name__)


# ─── ENV / FEATURE FLAG ─────────────────────────────────────────────────────


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return default
    return max(minimum, v)


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int = 86400) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(float(raw))
    except (ValueError, TypeError):
        return default
    return max(minimum, min(maximum, v))


def feature_enabled() -> bool:
    return _env_flag("FEATURE_CASCADE_POST_MORTEM", False)


def get_threshold_24h_usd() -> float:
    return _env_float(
        "POST_MORTEM_THRESHOLD_USD",
        DEFAULT_THRESHOLD_24H_USD,
        minimum=1_000_000.0,
    )


def get_threshold_4h_acute_usd() -> float:
    return _env_float(
        "POST_MORTEM_ACUTE_THRESHOLD_USD",
        DEFAULT_THRESHOLD_4H_ACUTE_USD,
        minimum=1_000_000.0,
    )


def get_cooldown_hours() -> int:
    return _env_int(
        "POST_MORTEM_COOLDOWN_HOURS",
        DEFAULT_COOLDOWN_HOURS,
        minimum=1,
        maximum=72,
    )


def get_agg_interval_seconds() -> int:
    return _env_int(
        "POST_MORTEM_AGG_INTERVAL_S",
        60,
        minimum=15,
        maximum=600,
    )


def get_retention_days() -> int:
    return _env_int(
        "POST_MORTEM_RETENTION_DAYS",
        7,
        minimum=2,
        maximum=90,
    )


def get_bybit_symbols() -> tuple[str, ...]:
    """Символы для подписки Bybit allLiquidation.<SYMBOL>.

    Bybit liquidation stream — per-symbol (unlike Binance, который шлёт
    ARR из всех символов). Дефолт — топ-5 ликвидных.
    """
    raw = os.getenv("POST_MORTEM_BYBIT_SYMBOLS", "")
    if raw.strip():
        symbols = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
        if symbols:
            return symbols
    return ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")


def get_binance_ws_url() -> str:
    return os.getenv(
        "POST_MORTEM_BINANCE_WS_URL",
        "wss://fstream.binance.com/ws/!forceOrder@arr",
    )


def get_bybit_ws_url() -> str:
    return os.getenv(
        "POST_MORTEM_BYBIT_WS_URL",
        "wss://stream.bybit.com/v5/public/linear",
    )


def bybit_enabled() -> bool:
    """Bybit подписка — отдельный sub-flag (можно отключить, если flaky).

    Default ON (если основная фича включена).
    """
    return _env_flag("POST_MORTEM_BYBIT_ENABLED", True)


def binance_enabled() -> bool:
    """Binance подписка — sub-flag. Default ON."""
    return _env_flag("POST_MORTEM_BINANCE_ENABLED", True)


# ─── ПАРСЕРЫ WS-СООБЩЕНИЙ ───────────────────────────────────────────────────


def parse_binance_force_order(payload: dict) -> Optional[LiquidationEvent]:
    """Парсит одно сообщение Binance !forceOrder@arr.

    Format:
        {
            "e": "forceOrder",
            "E": <event time ms>,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",      # SELL → ликвидация лонга
                "o": "LIMIT",
                "ap": "20000",    # average filled price
                "q": "0.5",       # original quantity
                "ot": "MARKET",
                "T": <transaction time ms>,
                ...
            }
        }

    Returns None при некорректном payload'е (graceful skip).
    """
    if not isinstance(payload, dict):
        return None
    o = payload.get("o")
    if not isinstance(o, dict):
        return None

    try:
        symbol = str(o.get("s") or "").upper()
        side_binance = str(o.get("S") or "").upper()
        avg_price = float(o.get("ap") or 0)
        qty = float(o.get("q") or 0)
        ts_ms = int(o.get("T") or payload.get("E") or 0)
    except (ValueError, TypeError):
        return None

    if not symbol or ts_ms <= 0 or avg_price <= 0 or qty <= 0:
        return None

    # SELL ⇒ ликвидация лонга; BUY ⇒ ликвидация шорта.
    if side_binance == "SELL":
        side = SIDE_LONG
    elif side_binance == "BUY":
        side = SIDE_SHORT
    else:
        return None

    value_usd = avg_price * qty
    if value_usd <= 0:
        return None

    return LiquidationEvent(
        timestamp_ms=ts_ms,
        venue="binance",
        symbol=symbol,
        side=side,
        value_usd=value_usd,
    )


def parse_bybit_liquidation(payload: dict) -> Optional[LiquidationEvent]:
    """Парсит Bybit v5 allLiquidation.SYMBOL message.

    Format:
        {
          "topic": "allLiquidation.BTCUSDT",
          "type": "snapshot",
          "ts": 1234567890123,
          "data": [
            {
              "T": 1234567890123,
              "s": "BTCUSDT",
              "S": "Sell",         # Sell → ликвидация лонга
              "v": "0.5",          # filled quantity (BTC)
              "p": "20000"         # bankruptcy price (фактически avg fill)
            }, ...
          ]
        }

    Возвращает первое валидное событие из массива (для batch — см.
    parse_bybit_liquidation_batch).
    """
    items = parse_bybit_liquidation_batch(payload)
    return items[0] if items else None


def parse_bybit_liquidation_batch(payload: dict) -> list[LiquidationEvent]:
    """Парсит Bybit batch — может содержать несколько ликвидаций."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    out: list[LiquidationEvent] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            symbol = str(item.get("s") or "").upper()
            side_raw = str(item.get("S") or "").lower()
            qty = float(item.get("v") or 0)
            price = float(item.get("p") or 0)
            ts_ms = int(item.get("T") or payload.get("ts") or 0)
        except (ValueError, TypeError):
            continue

        if not symbol or ts_ms <= 0 or price <= 0 or qty <= 0:
            continue

        if side_raw == "sell":
            side = SIDE_LONG
        elif side_raw == "buy":
            side = SIDE_SHORT
        else:
            continue

        value_usd = price * qty
        if value_usd <= 0:
            continue

        out.append(
            LiquidationEvent(
                timestamp_ms=ts_ms,
                venue="bybit",
                symbol=symbol,
                side=side,
                value_usd=value_usd,
            )
        )
    return out


# ─── SQLITE ПЕРСИСТ ─────────────────────────────────────────────────────────


async def insert_liquidation_event(event: LiquidationEvent) -> None:
    """Пишет один event. Идемпотентность не гарантируется (дубли могут
    случиться если WS отдал повторный snapshot после reconnect — это OK,
    24ч агрегации это не ломает критично, к тому же дубли редки).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO liquidation_events
                (timestamp_ms, venue, symbol, side, value_usd)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.timestamp_ms,
                event.venue,
                event.symbol,
                event.side,
                event.value_usd,
            ),
        )
        await db.commit()


async def insert_liquidation_events_batch(
    events: Sequence[LiquidationEvent],
) -> int:
    """Пишет batch. Возвращает кол-во вставленных записей."""
    if not events:
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """
            INSERT INTO liquidation_events
                (timestamp_ms, venue, symbol, side, value_usd)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (e.timestamp_ms, e.venue, e.symbol, e.side, e.value_usd)
                for e in events
            ],
        )
        await db.commit()
    return len(events)


async def load_recent_liquidation_events(
    *,
    now_ms: int,
    lookback_seconds: int = 24 * 3600,
) -> list[LiquidationEvent]:
    """Достаёт events за последние lookback_seconds."""
    cutoff = now_ms - lookback_seconds * 1000
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT timestamp_ms, venue, symbol, side, value_usd
            FROM liquidation_events
            WHERE timestamp_ms >= ?
            ORDER BY timestamp_ms DESC
            """,
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        LiquidationEvent(
            timestamp_ms=int(r["timestamp_ms"]),
            venue=str(r["venue"]),
            symbol=str(r["symbol"]),
            side=str(r["side"]),
            value_usd=float(r["value_usd"]),
        )
        for r in rows
    ]


async def cleanup_old_liquidation_events(*, retention_days: int) -> int:
    """Удаляет старые events. Возвращает кол-во удалённых строк."""
    if retention_days < 1:
        retention_days = 1
    cutoff_ms = (
        int(datetime.now(timezone.utc).timestamp() * 1000)
        - retention_days * 24 * 3600 * 1000
    )
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM liquidation_events WHERE timestamp_ms < ?",
            (cutoff_ms,),
        )
        await db.commit()
        return cursor.rowcount or 0


async def get_last_post_mortem_triggered_ms() -> Optional[int]:
    """Возвращает timestamp_ms последнего поста-мортема (для cooldown).

    Returns None если ни одного ещё не было.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT triggered_at FROM cascade_post_mortems "
            "ORDER BY triggered_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
    if not row or not row[0]:
        return None
    try:
        dt = datetime.fromisoformat(str(row[0]).replace(" ", "T"))
        # SQLite datetime('now') — UTC без tz info; treat as UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


async def persist_cascade_post_mortem(snapshot: CascadeSnapshot, summary_md: str) -> int:
    """Пишет cascade_post_mortem row. Возвращает rowid."""
    w = snapshot.window
    snapshot_dict = {
        "triggered_at_iso": snapshot.triggered_at_iso,
        "triggered_at_ms": snapshot.triggered_at_ms,
        "window": asdict(w),
        "indicators": snapshot.indicators,
        "debate_excerpt": snapshot.debate_excerpt,
    }
    snapshot_json = json.dumps(snapshot_dict, ensure_ascii=False, default=str)

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO cascade_post_mortems
                (triggered_at, window_type, window_hours,
                 total_liq_usd, long_liq_usd, short_liq_usd,
                 snapshot_json, summary_md, posted_to_tg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                snapshot.triggered_at_iso,
                w.window_type,
                w.window_hours,
                w.total_usd,
                w.long_usd,
                w.short_usd,
                snapshot_json,
                summary_md,
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def mark_post_mortem_posted(post_mortem_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cascade_post_mortems SET posted_to_tg = 1 WHERE id = ?",
            (post_mortem_id,),
        )
        await db.commit()


async def list_recent_cascade_post_mortems(limit: int = 10) -> list[dict]:
    """Возвращает recent post-mortems (от нового к старому)."""
    if limit <= 0:
        limit = 1
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, triggered_at, window_type, window_hours,
                   total_liq_usd, long_liq_usd, short_liq_usd,
                   posted_to_tg
            FROM cascade_post_mortems
            ORDER BY triggered_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_cascade_post_mortem_by_id(post_mortem_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM cascade_post_mortems WHERE id = ?",
            (post_mortem_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def find_cascade_post_mortem_by_date(date_iso: str) -> Optional[dict]:
    """Ищет post-mortem по дате (YYYY-MM-DD). Возвращает first match."""
    if not date_iso or len(date_iso) < 10:
        return None
    prefix = date_iso[:10]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM cascade_post_mortems
            WHERE substr(triggered_at, 1, 10) = ?
            ORDER BY triggered_at DESC
            LIMIT 1
            """,
            (prefix,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


# ─── СБОР ИНДИКАТОРОВ ───────────────────────────────────────────────────────


async def collect_indicator_snapshot() -> dict:
    """Собирает snapshot всех индикаторов в момент срабатывания.

    Использует ТОЛЬКО ленивые импорты — модули могут отсутствовать в
    minimal-build (unit-fast). Каждый индикатор обёрнут в try/except —
    если упал, в snapshot уходит {"error": "..."}.

    Возвращает dict со структурой:
        {
            "regime": {"label": "...", "score_delta": ...},
            "liquidation_magnet": {"label": "...", "is_strong_signal": ...},
            "smart_money_wallets": {"label": "...", ...},
            "btc_etf_flow": {"streak_days": N, "severity": "..."},
            "funding_term": {"is_inverted": bool, ...},
            "options_skew": {"skew_class": "..."},
        }
    """
    snapshot: dict[str, Any] = {}

    # Regime classifier
    try:
        from market_indicators.regime_io import (  # noqa: PLC0415
            fetch_regime_signals,
            feature_enabled as regime_enabled,
        )

        if regime_enabled():
            rg = await fetch_regime_signals()
            snapshot["regime"] = {
                "label": getattr(rg, "label", "unknown"),
                "is_strong_signal": getattr(rg, "is_strong_signal", False),
            }
        else:
            snapshot["regime"] = {"label": "unknown", "disabled": True}
    except Exception as exc:  # noqa: BLE001
        logger.debug("cascade snapshot: regime failed: %s", exc)
        snapshot["regime"] = {"label": "unknown", "error": str(exc)[:120]}

    # Liquidation magnet
    try:
        from market_indicators.liquidation_magnet_io import (  # noqa: PLC0415
            fetch_liquidation_magnet_signal,
            feature_enabled as lm_enabled,
        )

        if lm_enabled():
            lm = await fetch_liquidation_magnet_signal()
            snapshot["liquidation_magnet"] = {
                "label": getattr(lm, "label", "unknown"),
                "is_strong_signal": getattr(lm, "is_strong_signal", False),
                "oi_change_pct": getattr(lm, "oi_change_pct", 0.0),
                "top_long_short_ratio": getattr(lm, "top_long_short_ratio", None),
            }
        else:
            snapshot["liquidation_magnet"] = {"label": "unknown", "disabled": True}
    except Exception as exc:  # noqa: BLE001
        logger.debug("cascade snapshot: liquidation_magnet failed: %s", exc)
        snapshot["liquidation_magnet"] = {"label": "unknown", "error": str(exc)[:120]}

    # Smart-money wallets
    try:
        from market_indicators.smart_money_wallets_io import (  # noqa: PLC0415
            fetch_smart_money_wallet_flows,
            feature_enabled as smw_enabled,
        )

        if smw_enabled():
            smw = await fetch_smart_money_wallet_flows()
            snapshot["smart_money_wallets"] = {
                "label": getattr(smw, "label", "unknown"),
                "is_strong_signal": getattr(smw, "is_strong_signal", False),
                "aggregate_eth_flow": getattr(smw, "aggregate_eth_flow", 0.0),
            }
        else:
            snapshot["smart_money_wallets"] = {"label": "unknown", "disabled": True}
    except Exception as exc:  # noqa: BLE001
        logger.debug("cascade snapshot: smw failed: %s", exc)
        snapshot["smart_money_wallets"] = {"label": "unknown", "error": str(exc)[:120]}

    # BTC ETF flow (последний signal)
    try:
        snapshot["btc_etf_flow"] = await _collect_btc_etf_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cascade snapshot: btc_etf failed: %s", exc)
        snapshot["btc_etf_flow"] = {"error": str(exc)[:120]}

    # Funding term structure (последний snapshot из SQLite)
    try:
        snapshot["funding_term"] = await _collect_funding_term_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cascade snapshot: funding_term failed: %s", exc)
        snapshot["funding_term"] = {"error": str(exc)[:120]}

    # Options skew (последний snapshot из SQLite)
    try:
        snapshot["options_skew"] = await _collect_options_skew_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cascade snapshot: options_skew failed: %s", exc)
        snapshot["options_skew"] = {"error": str(exc)[:120]}

    return snapshot


async def _collect_btc_etf_snapshot() -> dict:
    """Тянет последний BTC ETF outflow signal."""
    try:
        from market_indicators.btc_etf_flows import (  # noqa: PLC0415
            aggregate_basket_flows,
            detect_outflow_signal,
            feature_enabled as etf_enabled,
            fetch_btc_etf_dailies,
        )
    except ImportError:
        return {"disabled": True}

    if not etf_enabled():
        return {"disabled": True}

    # Простой http_get-врапер на aiohttp
    try:
        import aiohttp  # noqa: PLC0415

        async def _http_get(url: str, params: dict) -> dict | None:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url, params=params) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()

        rows = await fetch_btc_etf_dailies(http_get=_http_get)
        basket = aggregate_basket_flows(rows)
        signal = detect_outflow_signal(basket)
        if signal is None:
            return {"severity": "NONE", "streak_days": 0}
        return {
            "severity": signal.severity,
            "streak_days": signal.streak_days,
            "avg_basket_change_pct": signal.avg_basket_change_pct,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}


async def _collect_funding_term_snapshot() -> dict:
    """Тянет последний funding_term snapshot из SQLite (для BTC)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT asset, timestamp_ms, slope_annual, is_inverted,
                   spot_funding_annual
            FROM funding_term_snapshots
            WHERE asset = 'BTC'
            ORDER BY timestamp_ms DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return {"no_data": True}
    return {
        "asset": row["asset"],
        "is_inverted": bool(row["is_inverted"]),
        "slope_annual": row["slope_annual"],
        "spot_funding_annual": row["spot_funding_annual"],
    }


async def _collect_options_skew_snapshot() -> dict:
    """Тянет последний options_skew snapshot из SQLite (для BTC)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT currency, timestamp_ms, skew_class, near_atm_iv,
                   near_rr_25d
            FROM options_skew_snapshots
            WHERE currency = 'BTC'
            ORDER BY timestamp_ms DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        return {"no_data": True}
    return {
        "currency": row["currency"],
        "skew_class": row["skew_class"],
        "near_atm_iv": row["near_atm_iv"],
        "near_rr_25d": row["near_rr_25d"],
    }


# ─── WS LISTENERS ───────────────────────────────────────────────────────────


_WS_BACKOFF_INITIAL_S = 1.0
_WS_BACKOFF_MAX_S = 60.0


async def binance_ws_listener(
    *,
    stop_event: asyncio.Event,
    on_event: Callable[[LiquidationEvent], Awaitable[None]] | None = None,
    ws_url: Optional[str] = None,
) -> None:
    """Постоянный listener Binance !forceOrder@arr.

    Auto-reconnect с экспоненциальным backoff. Завершается по stop_event.set().
    on_event — опциональный callback (для тестов). По умолчанию пишет в SQLite.
    """
    try:
        import aiohttp  # noqa: PLC0415
    except ImportError:
        logger.warning("cascade post-mortem: aiohttp недоступен — skipping Binance WS")
        return

    url = ws_url or get_binance_ws_url()
    backoff = _WS_BACKOFF_INITIAL_S

    async def _default_on_event(ev: LiquidationEvent) -> None:
        await insert_liquidation_event(ev)

    handler = on_event or _default_on_event

    while not stop_event.is_set():
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    logger.info("🔗 Binance forceOrder WS connected")
                    backoff = _WS_BACKOFF_INITIAL_S
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except (ValueError, TypeError):
                            continue
                        ev = parse_binance_force_order(payload)
                        if ev is None:
                            continue
                        try:
                            await handler(ev)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Binance WS handler error: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Binance WS disconnected: %s; retry in %.1fs", exc, backoff)
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, _WS_BACKOFF_MAX_S)


async def bybit_ws_listener(
    *,
    stop_event: asyncio.Event,
    symbols: Sequence[str] | None = None,
    on_event: Callable[[LiquidationEvent], Awaitable[None]] | None = None,
    ws_url: Optional[str] = None,
) -> None:
    """Bybit v5 allLiquidation.<SYMBOL> listener (per-symbol).

    Bybit требует subscription на каждый symbol отдельно (в отличие от
    Binance ARR). Использует один WS connection, шлёт subscribe-message
    при подключении.
    """
    try:
        import aiohttp  # noqa: PLC0415
    except ImportError:
        logger.warning("cascade post-mortem: aiohttp недоступен — skipping Bybit WS")
        return

    url = ws_url or get_bybit_ws_url()
    syms = tuple(s.upper() for s in (symbols or get_bybit_symbols()))
    if not syms:
        return

    backoff = _WS_BACKOFF_INITIAL_S

    async def _default_on_event(ev: LiquidationEvent) -> None:
        await insert_liquidation_event(ev)

    handler = on_event or _default_on_event

    while not stop_event.is_set():
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    sub_msg = {
                        "op": "subscribe",
                        "args": [f"allLiquidation.{s}" for s in syms],
                    }
                    await ws.send_json(sub_msg)
                    logger.info(
                        "🔗 Bybit allLiquidation WS connected (symbols=%s)",
                        ",".join(syms),
                    )
                    backoff = _WS_BACKOFF_INITIAL_S
                    async for msg in ws:
                        if stop_event.is_set():
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except (ValueError, TypeError):
                            continue
                        topic = payload.get("topic") or ""
                        if not topic.startswith("allLiquidation"):
                            continue
                        events = parse_bybit_liquidation_batch(payload)
                        for ev in events:
                            try:
                                await handler(ev)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("Bybit WS handler error: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Bybit WS disconnected: %s; retry in %.1fs", exc, backoff)
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, _WS_BACKOFF_MAX_S)


# ─── ОСНОВНОЙ AGG-LOOP (вызывается из scheduler.py) ────────────────────────


async def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


async def evaluate_and_maybe_trigger(
    *,
    now_ms: Optional[int] = None,
    threshold_24h_usd: Optional[float] = None,
    threshold_4h_usd: Optional[float] = None,
    cooldown_hours: Optional[int] = None,
    snapshot_collector: Callable[[], Awaitable[dict]] | None = None,
) -> Optional[tuple[CascadeSnapshot, str, int]]:
    """Один tick детектора.

    1. Загружает события за 24ч.
    2. Считает rolling 24h + 4h.
    3. Проверяет should_trigger().
    4. Если да — собирает snapshot, форматирует summary, пишет в SQLite.

    Returns (CascadeSnapshot, summary_md, post_mortem_id) если триггер
    сработал, иначе None.
    """
    now = now_ms or await _now_ms()
    threshold_24h = threshold_24h_usd if threshold_24h_usd is not None else get_threshold_24h_usd()
    threshold_4h = threshold_4h_usd if threshold_4h_usd is not None else get_threshold_4h_acute_usd()
    cooldown = cooldown_hours if cooldown_hours is not None else get_cooldown_hours()

    events = await load_recent_liquidation_events(
        now_ms=now,
        lookback_seconds=24 * 3600,
    )
    agg24 = aggregate_24h(events, now_ms=now)
    agg4 = aggregate_4h(events, now_ms=now)

    last_triggered = await get_last_post_mortem_triggered_ms()

    decision: TriggerDecision = should_trigger(
        agg_24h=agg24,
        agg_4h=agg4,
        threshold_24h_usd=threshold_24h,
        threshold_4h_usd=threshold_4h,
        now_ms=now,
        last_triggered_ms=last_triggered,
        cooldown_hours=cooldown,
    )

    if not decision.should_fire or decision.window is None:
        logger.debug("cascade post-mortem: no trigger (%s)", decision.reason)
        return None

    # Триггер сработал — собираем snapshot
    collector = snapshot_collector or collect_indicator_snapshot
    indicators = await collector()

    iso = datetime.fromtimestamp(now / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    snapshot = CascadeSnapshot(
        triggered_at_iso=iso,
        triggered_at_ms=now,
        window=decision.window,
        indicators=indicators,
        debate_excerpt=None,
    )
    summary_md = format_post_mortem_markdown(snapshot)
    post_mortem_id = await persist_cascade_post_mortem(snapshot, summary_md)

    logger.warning(
        "🔥 Cascade post-mortem TRIGGERED: %s (id=%d)",
        decision.reason,
        post_mortem_id,
    )

    return snapshot, summary_md, post_mortem_id


async def cascade_post_mortem_loop(
    *,
    stop_event: asyncio.Event,
    send_telegram: Callable[[str], Awaitable[bool]] | None = None,
    agg_interval_s: Optional[int] = None,
) -> None:
    """Основной agg-loop. Запускается из scheduler.py если фича включена.

    Каждые agg_interval_s секунд:
    1. Вызывает evaluate_and_maybe_trigger()
    2. Если триггер сработал — шлёт summary_md в TG через send_telegram
    3. Раз в сутки чистит старые liquidation_events (retention)
    """
    interval = agg_interval_s or get_agg_interval_seconds()
    retention = get_retention_days()
    last_cleanup_day: str | None = None

    # стартовая пауза, чтобы WS-listener'ы успели подключиться
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=60.0)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        started_loop = asyncio.get_event_loop().time()
        try:
            result = await evaluate_and_maybe_trigger()
            if result is not None:
                snapshot, summary_md, post_mortem_id = result
                if send_telegram is not None:
                    try:
                        posted = await send_telegram(summary_md)
                        if posted:
                            await mark_post_mortem_posted(post_mortem_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "cascade post-mortem: TG send failed: %s", exc
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("cascade post-mortem agg-loop error: %s", exc)

        # daily cleanup
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if last_cleanup_day != today:
            try:
                deleted = await cleanup_old_liquidation_events(
                    retention_days=retention
                )
                if deleted:
                    logger.info(
                        "🧹 cascade post-mortem cleanup: удалено %d events "
                        "(retention=%dd)",
                        deleted,
                        retention,
                    )
                last_cleanup_day = today
            except Exception as exc:  # noqa: BLE001
                logger.warning("cascade post-mortem cleanup failed: %s", exc)

        elapsed = asyncio.get_event_loop().time() - started_loop
        sleep_for = max(0.5, interval - elapsed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
            break
        except asyncio.TimeoutError:
            continue


# ─── ФОРМАТТЕР СПИСКА (для /postmortem list) ────────────────────────────────


def format_post_mortem_list(rows: Iterable[dict]) -> str:
    """Форматирует список post-mortem'ов в markdown-таблицу для TG."""
    lst = list(rows)
    if not lst:
        return "_Каскадных post-mortem'ов ещё не было._"
    lines = ["*Recent cascade post-mortems:*", ""]
    for r in lst:
        triggered = str(r.get("triggered_at") or "")[:19]
        wt = "4h" if r.get("window_type") == "rolling_4h_acute" else "24h"
        total_m = float(r.get("total_liq_usd") or 0) / 1e6
        rid = r.get("id")
        lines.append(f"• `#{rid}` {triggered} · {wt} · ${total_m:.0f}M")
    lines.append("")
    lines.append("_Используй_ `/postmortem <id>` _или_ `/postmortem YYYY-MM-DD`")
    return "\n".join(lines)


def format_post_mortem_full(row: dict) -> str:
    """Возвращает summary_md из записи (для /postmortem <id> или <date>)."""
    if not row:
        return "_Post-mortem не найден._"
    return str(row.get("summary_md") or "_(пустой post-mortem)_")
