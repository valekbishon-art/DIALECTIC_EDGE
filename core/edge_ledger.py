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


async def record_signal(setup, source: str = "") -> None:
    """[ЗАГЛУШКА] Зафиксировать live-сигнал в edge-леджере.

    Вызывается под FEATURE_EDGE_LEDGER из best_deal_alert.py в guarded
    try/except. Безопасная заглушка: ничего не персистит, только debug-лог.
    Замени реальной записью pending-сигнала в БД при включении фичи.
    """
    try:
        asset = getattr(setup, "asset", "?")
        direction = getattr(setup, "direction", "?")
    except Exception:
        asset = direction = "?"
    logger.debug(
        "edge_ledger.record_signal STUB: source=%s asset=%s dir=%s (no-op)",
        source, asset, direction,
    )
    return None


async def resolve_pending() -> dict:
    """[ЗАГЛУШКА] Резолвнуть pending-сигналы edge-леджера.

    Вызывается раз в EDGE_RESOLVE_INTERVAL_SEC из scheduler._edge_resolve_loop в
    guarded try/except. Безопасная заглушка: нет pending — нечего резолвить.
    Замени реальной логикой (чтение pending из БД + resolve_against_candles).
    """
    return {"resolved": 0, "tp": 0, "sl": 0, "expired": 0, "still_pending": 0}
