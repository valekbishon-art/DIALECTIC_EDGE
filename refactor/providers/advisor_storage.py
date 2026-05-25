"""Persistent storage for advisor plans + virtual portfolio (M2).

Поведение:
- Каждый успешный ``/advise`` вызов сохраняется как plan (auto_saved).
  Юзер может нажать кнопку «📥 В портфель» → plan promote'ится в
  is_portfolio=1 и попадает под watcher (см. scheduler).
- ``/portfolio`` возвращает список активных портфельных позиций с
  live PnL (entry vs текущая цена).
- Watcher периодически проверяет цены и закрывает позиции по
  SL/TP с уведомлением юзеру через bot.send_message.

Schema живёт в ``database.py:init_db`` (новая таблица ``advisor_plans``).
Этот модуль — pure-CRUD над ней, без Telegram/HTTP-зависимостей.

Per AGENTS.md:
- Фичефлаг ``FEATURE_ADVISOR_PORTFOLIO`` (default 0).
- Не трогает торговую логику (signal_trader/auto_tracker).
- Тесты в ``tests/test_advisor_storage.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)


STATUS_ACTIVE = "active"
STATUS_STOPPED = "stopped"   # SL hit
STATUS_TP1 = "tp1"            # TP1 hit
STATUS_TP2 = "tp2"            # TP2 hit
STATUS_TP3 = "tp3"            # TP3 hit (full close)
STATUS_CLOSED = "closed"     # manually closed


def feature_enabled() -> bool:
    """M2 portfolio toggle. Off by default — фича за фичефлагом."""
    raw = os.getenv("FEATURE_ADVISOR_PORTFOLIO", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass
class StoredPlan:
    """Snapshot of an AdvisorPlan in storage. ``id`` is None for fresh inserts.

    Поля 1:1 с таблицей advisor_plans. ``tp_levels`` / ``rationale`` —
    JSON-encoded списки (см. ``_serialize``/``_deserialize``).
    """

    id: Optional[int] = None
    user_id: int = 0
    asset: str = ""
    action: str = ""
    direction: str = ""
    confidence_pct: int = 0
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    stop_distance_pct: Optional[float] = None
    risk_reward: Optional[float] = None
    tp_levels: list[dict] = field(default_factory=list)
    position_usd: Optional[float] = None
    position_pct_of_capital: Optional[float] = None
    capital_usd: Optional[float] = None
    horizon_human: str = ""
    invalidation: str = ""
    rationale: list[str] = field(default_factory=list)
    btc_overlay_note: str = ""
    risk_profile: str = ""
    narrative: Optional[str] = None
    is_portfolio: int = 0
    status: str = STATUS_ACTIVE
    created_at: int = 0
    closed_at: Optional[int] = None
    close_price: Optional[float] = None
    close_reason: Optional[str] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None


def _row_to_plan(row: aiosqlite.Row) -> StoredPlan:
    return StoredPlan(
        id=row["id"],
        user_id=row["user_id"],
        asset=row["asset"],
        action=row["action"],
        direction=row["direction"] or "",
        confidence_pct=row["confidence_pct"],
        entry_price=row["entry_price"],
        stop_price=row["stop_price"],
        stop_distance_pct=row["stop_distance_pct"],
        risk_reward=row["risk_reward"],
        tp_levels=json.loads(row["tp_levels_json"]) if row["tp_levels_json"] else [],
        position_usd=row["position_usd"],
        position_pct_of_capital=row["position_pct_of_capital"],
        capital_usd=row["capital_usd"],
        horizon_human=row["horizon_human"] or "",
        invalidation=row["invalidation"] or "",
        rationale=json.loads(row["rationale_json"]) if row["rationale_json"] else [],
        btc_overlay_note=row["btc_overlay_note"] or "",
        risk_profile=row["risk_profile"] or "",
        narrative=row["narrative"],
        is_portfolio=row["is_portfolio"],
        status=row["status"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        close_price=row["close_price"],
        close_reason=row["close_reason"],
        pnl_usd=row["pnl_usd"],
        pnl_pct=row["pnl_pct"],
    )


def _direction_from_action(action: str) -> str:
    """Derive LONG/SHORT/WAIT label from advisor action (BUY/SELL/HOLD/WAIT)."""
    a = (action or "").upper()
    if a == "BUY":
        return "LONG"
    if a == "SELL":
        return "SHORT"
    if a in {"HOLD", "WAIT"}:
        return "WAIT"
    return a or ""


def _serialize_tp_levels(plan) -> str:
    """Pack AdvisorPlan.tp_levels tuple → JSON-safe list[dict]."""
    out: list[dict] = []
    for lvl in getattr(plan, "tp_levels", ()) or ():
        out.append({
            "price": getattr(lvl, "price", None),
            "r_multiple": getattr(lvl, "r_multiple", None),
            "close_pct": getattr(lvl, "close_pct", None),
        })
    return json.dumps(out, ensure_ascii=False)


async def save_plan(
    user_id: int,
    plan,
    capital_usd: Optional[float] = None,
    *,
    is_portfolio: bool = False,
    db_path: Optional[str] = None,
) -> int:
    """Persist plan from core.advisor.AdvisorPlan. Returns inserted row id.

    Если ``is_portfolio=True`` — позиция сразу попадает под watcher (SL/TP).
    Иначе сохраняется как «последний план» для history/explain.
    """
    path = db_path or DB_PATH
    now = int(time.time())
    direction = _direction_from_action(getattr(plan, "action", ""))
    tp_json = _serialize_tp_levels(plan)
    rationale_json = json.dumps(list(getattr(plan, "rationale", ()) or ()), ensure_ascii=False)

    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            """
            INSERT INTO advisor_plans (
                user_id, asset, action, direction, confidence_pct,
                entry_price, stop_price, stop_distance_pct, risk_reward,
                tp_levels_json, position_usd, position_pct_of_capital,
                capital_usd, horizon_human, invalidation, rationale_json,
                btc_overlay_note, risk_profile, is_portfolio, status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                getattr(plan, "asset", ""),
                getattr(plan, "action", ""),
                direction,
                int(getattr(plan, "confidence_pct", 0) or 0),
                getattr(plan, "entry_price", None),
                getattr(plan, "stop_price", None),
                getattr(plan, "stop_distance_pct", None),
                getattr(plan, "risk_reward", None),
                tp_json,
                getattr(plan, "position_usd", None),
                getattr(plan, "position_pct_of_capital", None),
                capital_usd,
                getattr(plan, "horizon_human", "") or "",
                getattr(plan, "invalidation", "") or "",
                rationale_json,
                getattr(plan, "btc_overlay_note", "") or "",
                getattr(plan, "risk_profile", "") or "",
                1 if is_portfolio else 0,
                STATUS_ACTIVE,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_last_plan(
    user_id: int,
    asset: Optional[str] = None,
    *,
    db_path: Optional[str] = None,
) -> Optional[StoredPlan]:
    """Latest plan (любой is_portfolio) for user. Опционально по asset."""
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        if asset:
            cursor = await db.execute(
                "SELECT * FROM advisor_plans WHERE user_id=? AND asset=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, asset.upper()),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM advisor_plans WHERE user_id=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
        row = await cursor.fetchone()
        return _row_to_plan(row) if row else None


async def get_plan_by_id(
    plan_id: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[StoredPlan]:
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM advisor_plans WHERE id=?", (plan_id,)
        )
        row = await cursor.fetchone()
        return _row_to_plan(row) if row else None


async def list_active_portfolio(
    user_id: int,
    *,
    db_path: Optional[str] = None,
) -> list[StoredPlan]:
    """Return active portfolio positions for user (is_portfolio=1, status=active)."""
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM advisor_plans WHERE user_id=? AND is_portfolio=1 "
            "AND status=? ORDER BY created_at DESC",
            (user_id, STATUS_ACTIVE),
        )
        rows = await cursor.fetchall()
        return [_row_to_plan(r) for r in rows]


async def list_all_active(
    *,
    db_path: Optional[str] = None,
) -> list[StoredPlan]:
    """Return ALL active portfolio plans (across users). Used by watcher."""
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM advisor_plans WHERE is_portfolio=1 AND status=? "
            "ORDER BY created_at ASC",
            (STATUS_ACTIVE,),
        )
        rows = await cursor.fetchall()
        return [_row_to_plan(r) for r in rows]


async def promote_to_portfolio(
    plan_id: int,
    *,
    db_path: Optional[str] = None,
) -> bool:
    """Promote a saved plan into portfolio. Returns True if updated."""
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "UPDATE advisor_plans SET is_portfolio=1 "
            "WHERE id=? AND status=?",
            (plan_id, STATUS_ACTIVE),
        )
        await db.commit()
        return cursor.rowcount > 0


def compute_pnl(
    plan: StoredPlan,
    current_price: float,
) -> tuple[Optional[float], Optional[float]]:
    """Return (pnl_usd, pnl_pct) given an active plan and current price.

    LONG: pnl = (current - entry) / entry * position_usd
    SHORT: pnl = (entry - current) / entry * position_usd

    Возвращает (None, None) если данных не хватает (нет entry/position).
    """
    entry = plan.entry_price
    pos = plan.position_usd
    direction = (plan.direction or "").upper()
    if entry is None or entry <= 0:
        return None, None
    if direction not in {"LONG", "SHORT"}:
        return None, None
    if direction == "LONG":
        pct = (current_price - entry) / entry * 100.0
    else:
        pct = (entry - current_price) / entry * 100.0
    pnl_usd = None
    if pos is not None and pos > 0:
        pnl_usd = pct / 100.0 * pos
    return pnl_usd, pct


def check_close_trigger(
    plan: StoredPlan,
    current_price: float,
) -> Optional[tuple[str, str]]:
    """Check if current price triggers SL/TP close.

    Returns (new_status, reason) tuple if triggered, None otherwise.

    LONG: SL = current <= stop; TP_N = current >= tp_N price
    SHORT: SL = current >= stop; TP_N = current <= tp_N price
    """
    direction = (plan.direction or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return None
    if current_price <= 0:
        return None

    # SL check
    if plan.stop_price is not None and plan.stop_price > 0:
        if direction == "LONG" and current_price <= plan.stop_price:
            return (STATUS_STOPPED, f"SL hit @ {current_price:.4g}")
        if direction == "SHORT" and current_price >= plan.stop_price:
            return (STATUS_STOPPED, f"SL hit @ {current_price:.4g}")

    # TP check — naive: close fully on first TP hit. Real-life would do
    # partial closes per TPLevel.close_pct but virtual portfolio is
    # informational, not a live broker — keeping it simple.
    if plan.tp_levels:
        for idx, tp in enumerate(plan.tp_levels, start=1):
            price = tp.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            hit = (
                (direction == "LONG" and current_price >= price)
                or (direction == "SHORT" and current_price <= price)
            )
            if hit:
                status_map = {1: STATUS_TP1, 2: STATUS_TP2, 3: STATUS_TP3}
                new_status = status_map.get(idx, STATUS_TP3)
                return (new_status, f"TP{idx} hit @ {current_price:.4g}")

    return None


async def close_plan(
    plan_id: int,
    *,
    new_status: str,
    close_price: float,
    close_reason: str,
    db_path: Optional[str] = None,
) -> Optional[StoredPlan]:
    """Mark plan as closed (status / close_price / pnl). Returns updated plan."""
    path = db_path or DB_PATH
    now = int(time.time())
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM advisor_plans WHERE id=?", (plan_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        plan = _row_to_plan(row)
        pnl_usd, pnl_pct = compute_pnl(plan, close_price)
        await db.execute(
            "UPDATE advisor_plans SET status=?, closed_at=?, close_price=?, "
            "close_reason=?, pnl_usd=?, pnl_pct=? WHERE id=?",
            (new_status, now, close_price, close_reason, pnl_usd, pnl_pct, plan_id),
        )
        await db.commit()
        # Re-read for accurate return
        cursor = await db.execute(
            "SELECT * FROM advisor_plans WHERE id=?", (plan_id,)
        )
        row = await cursor.fetchone()
        return _row_to_plan(row) if row else None


async def update_narrative(
    plan_id: int,
    narrative: str,
    *,
    db_path: Optional[str] = None,
) -> bool:
    """Cache AI-generated narrative for plan. Returns True if updated."""
    path = db_path or DB_PATH
    async with aiosqlite.connect(path) as db:
        cursor = await db.execute(
            "UPDATE advisor_plans SET narrative=? WHERE id=?",
            (narrative, plan_id),
        )
        await db.commit()
        return cursor.rowcount > 0


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_STOPPED",
    "STATUS_TP1",
    "STATUS_TP2",
    "STATUS_TP3",
    "STATUS_CLOSED",
    "StoredPlan",
    "check_close_trigger",
    "close_plan",
    "compute_pnl",
    "feature_enabled",
    "get_last_plan",
    "get_plan_by_id",
    "list_active_portfolio",
    "list_all_active",
    "promote_to_portfolio",
    "save_plan",
    "update_narrative",
]
