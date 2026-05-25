"""Advisor portfolio handler — /myplans command + save/close callbacks.

Команды:
- ``/myplans`` — список активных advisor-планов (is_portfolio=1, status=active)
  с live PnL (entry vs current price).

Callbacks (на inline-кнопках после /advise):
- ``advisor:save:<plan_id>`` → promote plan в портфель.
- ``advisor:close:<plan_id>`` → закрыть позицию вручную (status=closed,
  пересчитать pnl по текущей цене).
- ``advisor:explain:<plan_id>`` → AI-narrative объяснение плана (см. core/advisor_narrative).

Отдельно от уже существующего `portfolio_handler.py`, который ведёт
ручной портфель (coin/amount/entry_price без TP/SL). M2 portfolio —
торговый журнал с автоматическим watcher'ом по advisor-планам.

Per AGENTS.md: новые хендлеры в refactor/handlers/*; фичефлаг
FEATURE_ADVISOR_PORTFOLIO; не трогает торговую логику.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from refactor.providers.advisor_storage import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    StoredPlan,
    close_plan,
    compute_pnl,
    feature_enabled,
    get_plan_by_id,
    list_active_portfolio,
    promote_to_portfolio,
    update_narrative,
)

logger = logging.getLogger(__name__)


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "—"
    abs_v = abs(value)
    if abs_v >= 100:
        return f"{value:,.2f}"
    if abs_v >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"${value:+,.2f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _direction_arrow(direction: str) -> str:
    d = (direction or "").upper()
    if d == "LONG":
        return "🟢 LONG"
    if d == "SHORT":
        return "🔴 SHORT"
    return d or "—"


async def _fetch_current_price(asset: str) -> Optional[float]:
    """Get current price for asset. Falls back gracefully if web_search fails."""
    try:
        from web_search import fetch_realtime_prices

        prices = await fetch_realtime_prices()
        block = prices.get(asset.upper()) or {}
        price = block.get("price")
        if isinstance(price, (int, float)) and price > 0:
            return float(price)
    except Exception as exc:
        logger.debug("advisor portfolio: fetch price for %s failed: %s", asset, exc)
    return None


def _build_plan_line(plan: StoredPlan, current_price: Optional[float]) -> str:
    """One-line summary of a plan with live PnL."""
    head = f"{_direction_arrow(plan.direction)} *{plan.asset}*"
    entry = f"вход {_fmt_price(plan.entry_price)}"
    stop = f"SL {_fmt_price(plan.stop_price)}" if plan.stop_price else "SL —"
    tps = (
        ", ".join(_fmt_price(tp.get("price")) for tp in plan.tp_levels if tp.get("price"))
        if plan.tp_levels else "—"
    )

    if current_price is not None:
        pnl_usd, pnl_pct = compute_pnl(plan, current_price)
        emoji = "🟢" if (pnl_pct or 0) >= 0 else "🔴"
        pnl_line = (
            f"  Сейчас: {_fmt_price(current_price)} | "
            f"PnL: {emoji} {_fmt_pct(pnl_pct)}"
        )
        if pnl_usd is not None:
            pnl_line += f" ({_fmt_usd(pnl_usd)})"
    else:
        pnl_line = "  Сейчас: цена недоступна"

    return "\n".join([
        f"#{plan.id}: {head} — {entry}, {stop}, TP: {tps}",
        pnl_line,
    ])


async def _render_portfolio(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build /myplans message + inline keyboard."""
    plans = await list_active_portfolio(user_id)
    if not plans:
        text = (
            "📂 *Мои планы* (advisor portfolio)\n\n"
            "Пока пусто. После `/advise BTC` нажми кнопку «📥 В портфель» "
            "чтобы добавить план под watcher (SL/TP алёрты)."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        return text, kb

    # Fetch prices for unique assets in parallel
    unique_assets = sorted({p.asset for p in plans})
    price_map: dict[str, Optional[float]] = {}
    for asset in unique_assets:
        price_map[asset] = await _fetch_current_price(asset)

    lines = [f"📂 *Мои планы* — {len(plans)} акт. поз."]
    for plan in plans:
        lines.append("")
        lines.append(_build_plan_line(plan, price_map.get(plan.asset)))

    # Inline keyboard: one row per plan with [Close] [Explain] buttons
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"❌ Закрыть #{plan.id}",
                callback_data=f"advisor:close:{plan.id}",
            ),
            InlineKeyboardButton(
                text=f"💬 Объяснить #{plan.id}",
                callback_data=f"advisor:explain:{plan.id}",
            ),
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    return "\n".join(lines), kb


async def handle_myplans_command(message: Message) -> None:
    """`/myplans` — show active advisor portfolio positions."""
    if not feature_enabled():
        await message.answer(
            "Виртуальный портфель advisor'а выключен "
            "(FEATURE_ADVISOR_PORTFOLIO=0)."
        )
        return
    user_id = message.from_user.id if message.from_user else 0
    text, kb = await _render_portfolio(user_id)
    try:
        await message.answer(
            text, parse_mode="Markdown", reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        await message.answer(text, reply_markup=kb)


def _parse_callback(data: Optional[str]) -> tuple[str, Optional[int]]:
    """Parse 'advisor:<action>:<plan_id>' callback data."""
    parts = (data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        plan_id: Optional[int] = int(parts[2]) if len(parts) > 2 and parts[2] else None
    except ValueError:
        plan_id = None
    return action, plan_id


async def handle_save_callback(callback: CallbackQuery) -> None:
    """`advisor:save:<plan_id>` → promote plan to portfolio."""
    if not feature_enabled():
        await callback.answer("Портфель advisor'а выключен.", show_alert=True)
        return
    action, plan_id = _parse_callback(callback.data)
    if action != "save" or plan_id is None:
        await callback.answer("Неверный callback.", show_alert=True)
        return
    plan = await get_plan_by_id(plan_id)
    user_id = callback.from_user.id if callback.from_user else 0
    if plan is None or plan.user_id != user_id:
        await callback.answer("План не найден.", show_alert=True)
        return
    if plan.is_portfolio:
        await callback.answer("Уже в портфеле ✓", show_alert=False)
        return
    if plan.status != STATUS_ACTIVE:
        await callback.answer("План уже закрыт.", show_alert=True)
        return
    ok = await promote_to_portfolio(plan_id)
    if ok:
        await callback.answer("📥 Добавлено в портфель", show_alert=False)
    else:
        await callback.answer("Не удалось добавить.", show_alert=True)


async def handle_close_callback(callback: CallbackQuery) -> None:
    """`advisor:close:<plan_id>` → manually close position with current price."""
    if not feature_enabled():
        await callback.answer("Портфель advisor'а выключен.", show_alert=True)
        return
    action, plan_id = _parse_callback(callback.data)
    if action != "close" or plan_id is None:
        await callback.answer("Неверный callback.", show_alert=True)
        return
    plan = await get_plan_by_id(plan_id)
    user_id = callback.from_user.id if callback.from_user else 0
    if plan is None or plan.user_id != user_id:
        await callback.answer("План не найден.", show_alert=True)
        return
    if plan.status != STATUS_ACTIVE:
        await callback.answer("План уже закрыт.", show_alert=True)
        return
    current_price = await _fetch_current_price(plan.asset)
    if current_price is None:
        await callback.answer("Цена недоступна, попробуй позже.", show_alert=True)
        return
    closed = await close_plan(
        plan_id, new_status=STATUS_CLOSED, close_price=current_price,
        close_reason=f"manual close @ {_fmt_price(current_price)}",
    )
    if closed is None:
        await callback.answer("Не удалось закрыть.", show_alert=True)
        return
    pnl_text = (
        f"{_fmt_pct(closed.pnl_pct)} ({_fmt_usd(closed.pnl_usd)})"
        if closed.pnl_pct is not None else "—"
    )
    await callback.answer(f"❌ Закрыт. PnL: {pnl_text}", show_alert=True)


async def handle_explain_callback(callback: CallbackQuery) -> None:
    """`advisor:explain:<plan_id>` → AI-narrative объяснение плана."""
    action, plan_id = _parse_callback(callback.data)
    if action != "explain" or plan_id is None:
        await callback.answer("Неверный callback.", show_alert=True)
        return
    plan = await get_plan_by_id(plan_id)
    user_id = callback.from_user.id if callback.from_user else 0
    if plan is None or plan.user_id != user_id:
        await callback.answer("План не найден.", show_alert=True)
        return

    # Lazy import to avoid loading ai_provider at module load time.
    from core.advisor_narrative import (
        feature_enabled as narrative_enabled,
        generate_plan_narrative,
    )

    if not narrative_enabled():
        await callback.answer(
            "AI-объяснение выключено (FEATURE_ADVISOR_NARRATIVE=0).",
            show_alert=True,
        )
        return

    # Build minimal market context from BTC outlook (graceful degrade if fails)
    market_ctx: dict = {}
    try:
        from core.btc_outlook import compute_btc_outlook
        from refactor.handlers.btc_handler import fetch_btc_outlook_inputs

        inputs = await fetch_btc_outlook_inputs()
        verdict = compute_btc_outlook(inputs)
        market_ctx["btc_lean"] = verdict.lean
        market_ctx["btc_confidence_pct"] = verdict.confidence_pct
    except Exception as exc:
        logger.debug("explain: BTC overlay fetch failed: %s", exc)

    await callback.answer("⏳ Генерирую объяснение…", show_alert=False)
    narrative = await generate_plan_narrative(plan, market_ctx)
    if not narrative:
        try:
            await callback.message.answer(
                "⚠️ Не удалось сгенерировать объяснение. AI-провайдеры "
                "недоступны или вернули пустой ответ."
            )
        except Exception:
            pass
        return
    # Cache the narrative so subsequent /explain on same plan is free.
    try:
        await update_narrative(plan_id, narrative)
    except Exception:
        pass
    try:
        await callback.message.answer(
            f"💬 *Объяснение плана #{plan_id}*\n\n{narrative}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        await callback.message.answer(narrative)


def register_advisor_portfolio_handlers(dp) -> None:
    """Register /myplans command + advisor:* callback handlers."""
    dp.message.register(handle_myplans_command, Command("myplans"))
    dp.callback_query.register(
        handle_save_callback,
        lambda c: bool(c.data) and c.data.startswith("advisor:save:"),
    )
    dp.callback_query.register(
        handle_close_callback,
        lambda c: bool(c.data) and c.data.startswith("advisor:close:"),
    )
    dp.callback_query.register(
        handle_explain_callback,
        lambda c: bool(c.data) and c.data.startswith("advisor:explain:"),
    )


__all__ = [
    "handle_close_callback",
    "handle_explain_callback",
    "handle_myplans_command",
    "handle_save_callback",
    "register_advisor_portfolio_handlers",
]
