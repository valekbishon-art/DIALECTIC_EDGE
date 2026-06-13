"""core/edge_ledger.py — Леджер измерения edge живых сигналов.

ВОССТАНОВЛЕН при дебаге. Модуль отсутствовал в репозитории, из-за чего
`core/backtest_engine.py` падал на верхнеуровневом импорте
`from core.edge_ledger import resolve_against_candles` (ModuleNotFoundError),
а вместе с ним — scripts/research_basis_carry.py, research_cointegration.py,
research_stablecoin.py. В best_deal_alert.py и scheduler.py импорт был в guarded
try/except, поэтому live edge-леджер просто молча не работал.

Содержит:
  • resolve_against_candles(...) — детерминированный резолвер исхода сделки по
    свечам (TP/SL/expired). Реализован по контракту backtester.py:
    same-candle пессимизм, no look-ahead, комиссия 0.2% (0.1%/сторона).
  • record_signal(...) / resolve_pending() — БЕЗОПАСНЫЕ ЗАГЛУШКИ для live-пути
    (вызываются под FEATURE_EDGE_LEDGER в guarded try/except). Не пишут в БД
    и возвращают пустой результат — заполни реальной персистентностью
    перед включением фичи (FEATURE_EDGE_LEDGER=1).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# 0.1% за сторону → round-trip 0.2% (как в backtester.py: FEE_PCT = 0.001)
FEE_PCT = 0.001


def resolve_against_candles(
    direction: str,
    entry: float,
    target: float,
    stop: float,
    candles: Sequence,
    emitted_dt: datetime,
    horizon_hours: float,
) -> tuple[str, Optional[float], Optional[float], Optional[str]]:
    """Резолвит исход сделки по будущим свечам.

    Возвращает (status, exit_price, pnl_pct, exit_at):
      status     — "tp" | "sl" | "expired" | "pending"
      exit_price — цена выхода (None для pending)
      pnl_pct    — чистый % после комиссии 0.2% (None для pending)
      exit_at    — ISO-время свечи выхода (None для pending)

    Контракт (как в backtester.py):
      • рассматриваются только свечи в окне (emitted_dt, emitted_dt+horizon]
      • LONG:  TP если high>=target, SL если low<=stop
      • SHORT: TP если low<=target,  SL если high>=stop
      • same-candle пессимизм: если в одной свече задеты и TP и SL → считаем SL
      • no look-ahead: идём по свечам строго по возрастанию времени
      • expired: если до конца окна ничего не сработало — выход по close
        последней свечи окна
      • pending: в окне нет ни одной свечи (рано судить)

    ПРИМЕЧАНИЕ: вход считается исполненным в момент emitted_dt (entry =
    цена эмиссии) — именно так backtest_engine использует этот резолвер
    для измерения edge сигнала (без модели исполнения entry-ордера).
    """
    direction = (direction or "").upper()
    if direction not in ("LONG", "SHORT"):
        return "pending", None, None, None
    if not entry or entry <= 0:
        return "pending", None, None, None

    deadline = emitted_dt + timedelta(hours=float(horizon_hours))
    window = [c for c in candles if emitted_dt < c.timestamp <= deadline]
    if not window:
        return "pending", None, None, None

    round_trip_fee_pct = FEE_PCT * 2 * 100.0  # в процентных пунктах

    def _net_pnl_pct(exit_price: float) -> float:
        if direction == "LONG":
            gross = (exit_price / entry - 1.0) * 100.0
        else:
            gross = (1.0 - exit_price / entry) * 100.0
        return gross - round_trip_fee_pct

    for c in window:
        if direction == "LONG":
            hit_sl = c.low <= stop
            hit_tp = c.high >= target
        else:
            hit_sl = c.high >= stop
            hit_tp = c.low <= target
        # same-candle пессимизм: SL имеет приоритет
        if hit_sl:
            return "sl", stop, _net_pnl_pct(stop), c.timestamp.isoformat()
        if hit_tp:
            return "tp", target, _net_pnl_pct(target), c.timestamp.isoformat()

    last = window[-1]
    return "expired", last.close, _net_pnl_pct(last.close), last.timestamp.isoformat()


async def record_signal(setup, source: str = "") -> Optional[int]:
    """Зафиксировать live-сигнал в edge-леджере (pending) + формальный сертификат.

    Вызывается под FEATURE_EDGE_LEDGER из best_deal_alert.py. Пишет pending-строку
    в SQLite (`edge_signals`) вместе с `setup.certificate` — бинарным чек-листом
    условий, которые держали в момент эмиссии (#2). Фоновый резолвер потом
    проставит исход, а condition-win-rate покажет, какие условия несут edge (#5).

    Возвращает row id или None при ошибке (caller оборачивает в try/except).
    """
    try:
        from datetime import timezone as _tz
        from database import edge_insert_signal
    except Exception:
        logger.debug("edge_ledger.record_signal: import failed", exc_info=True)
        return None

    try:
        horizon = _default_horizon_hours()
        emitted_at = datetime.now(_tz.utc).replace(tzinfo=None).isoformat()
        rid = await edge_insert_signal(
            source=source,
            asset=getattr(setup, "asset", "?"),
            direction=getattr(setup, "direction", "?"),
            entry=float(getattr(setup, "entry", 0.0) or 0.0),
            target=getattr(setup, "target", None),
            stop=getattr(setup, "stop", None),
            horizon_hours=horizon,
            emitted_at=emitted_at,
            score=getattr(setup, "score", None),
            rr_ratio=getattr(setup, "rr_ratio", None),
            certificate=dict(getattr(setup, "certificate", {}) or {}),
            reasons=list(getattr(setup, "reasons", []) or []),
        )
        logger.info(
            "edge_ledger: recorded #%s %s %s @%.6g (source=%s)",
            rid, getattr(setup, "asset", "?"), getattr(setup, "direction", "?"),
            float(getattr(setup, "entry", 0.0) or 0.0), source,
        )
        return rid
    except Exception:
        logger.warning("edge_ledger.record_signal failed", exc_info=True)
        return None


def _default_horizon_hours() -> float:
    try:
        from config import EDGE_DEFAULT_HORIZON_HOURS
        return float(EDGE_DEFAULT_HORIZON_HOURS)
    except Exception:
        return 336.0


async def _fetch_candles_naive_utc(asset: str, horizon_hours: float):
    """Свечи для резолва с НАИВНЫМИ UTC-таймстампами (как emitted_at).

    Важно: emitted_at пишется как naive-UTC, поэтому и свечи должны быть
    naive-UTC — иначе resolve_against_candles кинет TypeError при сравнении
    aware/naive. Берём дневные свечи (горизонт live-сигналов = 14д), глубины
    с запасом, чтобы покрыть и давно висящие pending.
    """
    import aiohttp

    horizon_days = max(1, int(horizon_hours // 24) + 1)
    limit = min(1000, horizon_days + 25)
    interval = "1d" if horizon_hours >= 72 else "1h"
    if interval == "1h":
        limit = min(1000, int(horizon_hours) + 48)

    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": f"{asset}USDT", "interval": interval, "limit": limit}
    out = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return out
                data = await resp.json()
    except Exception:
        logger.debug("edge_ledger: klines fetch failed for %s", asset, exc_info=True)
        return out

    class _C:
        __slots__ = ("timestamp", "open", "high", "low", "close", "volume")

    for k in data:
        c = _C()
        c.timestamp = datetime.utcfromtimestamp(k[0] / 1000)  # naive UTC
        c.open, c.high, c.low, c.close = (
            float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        c.volume = float(k[5]) if len(k) > 5 else 0.0
        out.append(c)
    out.sort(key=lambda c: c.timestamp)
    return out


async def resolve_pending() -> dict:
    """Резолвнуть pending-сигналы edge-леджера по свечам (TP/SL/expired).

    Вызывается раз в EDGE_RESOLVE_INTERVAL_SEC из scheduler._edge_resolve_loop.
    Читает pending из БД, тянет свечи (одна выборка на asset), резолвит каждый
    сигнал детерминированным `resolve_against_candles`, обновляет статус.
    """
    summary = {"resolved": 0, "tp": 0, "sl": 0, "expired": 0, "still_pending": 0}
    try:
        from database import edge_get_pending, edge_mark_resolved
    except Exception:
        logger.debug("edge_ledger.resolve_pending: import failed", exc_info=True)
        return summary

    try:
        pending = await edge_get_pending()
    except Exception:
        logger.warning("edge_ledger.resolve_pending: read failed", exc_info=True)
        return summary

    if not pending:
        return summary

    # Свечи кэшируем по (asset, horizon-bucket) — один сетевой запрос на asset.
    candle_cache: dict[str, list] = {}
    for row in pending:
        asset = row.get("asset") or ""
        horizon = float(row.get("horizon_hours") or _default_horizon_hours())
        if asset not in candle_cache:
            candle_cache[asset] = await _fetch_candles_naive_utc(asset, horizon)
        candles = candle_cache.get(asset) or []
        if not candles:
            summary["still_pending"] += 1
            continue

        try:
            emitted_dt = datetime.fromisoformat(row["emitted_at"])
            if emitted_dt.tzinfo is not None:
                emitted_dt = emitted_dt.replace(tzinfo=None)
        except Exception:
            summary["still_pending"] += 1
            continue

        status, exit_price, pnl_pct, exit_at = resolve_against_candles(
            row.get("direction", ""),
            float(row.get("entry") or 0.0),
            float(row.get("target") or 0.0),
            float(row.get("stop") or 0.0),
            candles,
            emitted_dt,
            horizon,
        )
        if status == "pending":
            summary["still_pending"] += 1
            continue

        try:
            await edge_mark_resolved(row["id"], status, exit_price, pnl_pct, exit_at)
        except Exception:
            logger.warning("edge_ledger: mark_resolved failed id=%s", row.get("id"),
                           exc_info=True)
            summary["still_pending"] += 1
            continue

        summary["resolved"] += 1
        summary[status] = summary.get(status, 0) + 1

    return summary


async def condition_winrates(min_n: int = 1) -> list:
    """Win-rate по каждому условию сертификата (для /edge и тюнинга)."""
    try:
        from database import edge_condition_stats
        return await edge_condition_stats(min_n=min_n)
    except Exception:
        logger.debug("edge_ledger.condition_winrates failed", exc_info=True)
        return []
