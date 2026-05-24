"""Advisor handler — `/advise` command.

Collects all the inputs an ``AdvisorInputs`` needs (price/ATR via existing
``web_search.fetch_realtime_prices``, BTC outlook via ``btc_handler``,
risk profile via ``profile_handler``), feeds them into ``core.advisor``,
and posts the resulting plan as Telegram-friendly Markdown.

Usage:
- ``/advise``           — top majors (BTC + ETH + SOL).
- ``/advise BTC``       — single asset.
- ``/advise SOL``       — single asset.

Capital for position-sizing comes from env var ``ADVISOR_DEFAULT_CAPITAL_USD``
(default 1000). If user wants their own number, they can pass it as second arg:
``/advise BTC 5000`` → position sizing relative to $5 000.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from aiogram.filters import Command
from aiogram.types import Message

from core.advisor import (
    HORIZON_LONG,
    HORIZON_MEDIUM,
    HORIZON_SHORT,
    RISK_AGGRESSIVE,
    RISK_CONSERVATIVE,
    RISK_MODERATE,
    AdvisorInputs,
    feature_enabled,
    format_advisor_markdown,
    recommend,
)

logger = logging.getLogger(__name__)


DEFAULT_ASSETS: tuple[str, ...] = ("BTC", "ETH", "SOL")
SUPPORTED_ASSETS: tuple[str, ...] = ("BTC", "ETH", "SOL", "BNB", "XRP")


def _get_default_capital_usd() -> float:
    raw = os.getenv("ADVISOR_DEFAULT_CAPITAL_USD", "1000").strip()
    try:
        v = float(raw)
    except ValueError:
        return 1000.0
    return max(0.0, min(10_000_000.0, v))


def _parse_args(text: str | None) -> tuple[tuple[str, ...], float | None]:
    """Parse ``/advise [ASSET] [CAPITAL]`` arguments.

    Returns (assets, capital_usd_override). ``capital_usd_override=None`` means
    use the env default.
    """
    if not text:
        return DEFAULT_ASSETS, None
    parts = text.strip().split()
    if len(parts) <= 1:
        return DEFAULT_ASSETS, None

    assets: tuple[str, ...] = DEFAULT_ASSETS
    capital: float | None = None

    candidate = parts[1].upper()
    if candidate in SUPPORTED_ASSETS:
        assets = (candidate,)
    elif candidate == "ALL":
        assets = DEFAULT_ASSETS

    if len(parts) >= 3:
        try:
            capital = float(parts[2])
            capital = max(0.0, min(10_000_000.0, capital))
        except ValueError:
            capital = None

    return assets, capital


def _map_profile(risk_profile_enum_value: str) -> str:
    v = (risk_profile_enum_value or "").strip().lower()
    if v in {"conservative", "cons", "low"}:
        return RISK_CONSERVATIVE
    if v in {"aggressive", "agg", "high"}:
        return RISK_AGGRESSIVE
    return RISK_MODERATE


def _map_horizon(horizon_enum_value: str) -> str:
    v = (horizon_enum_value or "").strip().lower()
    if v in {"short", "short_term"}:
        return HORIZON_SHORT
    if v in {"long", "long_term"}:
        return HORIZON_LONG
    return HORIZON_MEDIUM


async def _load_user_settings(user_id: int) -> tuple[str, str]:
    """Load (risk_profile, time_horizon) from profile storage. Falls back to
    moderate/medium if anything goes wrong."""
    try:
        from refactor.handlers.profile_handler import get_profile_handler

        profile = await get_profile_handler().load_profile(user_id)
        risk = (
            profile.risk_profile.value
            if hasattr(profile.risk_profile, "value")
            else str(profile.risk_profile)
        )
        horizon = (
            profile.time_horizon.value
            if hasattr(profile.time_horizon, "value")
            else str(profile.time_horizon)
        )
        return _map_profile(risk), _map_horizon(horizon)
    except Exception as exc:
        logger.debug("advisor: cannot load profile for %s: %s", user_id, exc)
        return RISK_MODERATE, HORIZON_MEDIUM


def _extract_asset_block(prices: dict, asset: str) -> dict:
    """``fetch_realtime_prices`` returns a flat dict — pull the asset's block."""
    key = asset.upper()
    block = prices.get(key) or prices.get(key.lower()) or {}
    if not isinstance(block, dict):
        return {}
    return block


def _build_inputs_from_prices(
    asset: str,
    prices: dict,
    btc_lean: str | None,
    btc_confidence: int | None,
    risk_profile: str,
    horizon: str,
    capital_usd: float,
) -> AdvisorInputs:
    """Translate ``fetch_realtime_prices`` output into ``AdvisorInputs``."""
    block = _extract_asset_block(prices, asset)

    try:
        entry_price = float(block.get("price")) if block.get("price") else None
    except (TypeError, ValueError):
        entry_price = None

    atr_usd = block.get("atr_14d")
    atr_pct = block.get("atr_14d_pct")
    rsi = block.get("rsi_14d")
    trend = block.get("trend")

    # Quant verdict — derive from trend + RSI as a coarse fallback if no
    # dedicated quant signal is present. Real quant pipeline lives in
    # quant_filter.py and is consumed by analysis_service — we don't import
    # it here to keep advisor handler thin.
    quant_verdict: str | None = None
    quant_confidence: float | None = None
    if trend == "UPTREND":
        quant_verdict = "LONG"
        quant_confidence = 0.6
    elif trend == "DOWNTREND":
        quant_verdict = "SHORT"
        quant_confidence = 0.6
    # RSI extremes bump confidence in the trend direction.
    if rsi is not None:
        try:
            r = float(rsi)
            if quant_verdict == "LONG" and r < 30:
                quant_confidence = min(0.9, (quant_confidence or 0.6) + 0.2)
            elif quant_verdict == "SHORT" and r > 70:
                quant_confidence = min(0.9, (quant_confidence or 0.6) + 0.2)
        except (TypeError, ValueError):
            pass

    return AdvisorInputs(
        asset=asset.upper(),
        entry_price=entry_price,
        atr_14d_usd=float(atr_usd) if atr_usd else None,
        atr_14d_pct=float(atr_pct) if atr_pct else None,
        rsi_14d=float(rsi) if rsi else None,
        trend=trend,
        quant_verdict=quant_verdict,
        quant_confidence=quant_confidence,
        btc_lean=btc_lean,
        btc_confidence_pct=btc_confidence,
        risk_profile=risk_profile,
        time_horizon=horizon,
        capital_usd=capital_usd,
    )


async def _fetch_btc_outlook_snapshot() -> tuple[str | None, int | None]:
    """Compute BTC verdict same way ``/btc`` does. Returns (lean, conf_pct).

    Failure → (None, None) so advisor degrades gracefully (no overlay).
    """
    try:
        from core.btc_outlook import compute_btc_outlook
        from refactor.handlers.btc_handler import fetch_btc_outlook_inputs

        inputs = await fetch_btc_outlook_inputs()
        verdict = compute_btc_outlook(inputs)
        return verdict.lean, verdict.confidence_pct
    except Exception as exc:
        logger.debug("advisor: BTC overlay fetch failed: %s", exc)
        return None, None


def _format_combined_message(
    assets: Iterable[str],
    plans_md: dict[str, str],
    capital_usd: float,
    risk_profile: str,
) -> str:
    """Concatenate per-asset plans into one Telegram message."""
    header = (
        f"🎯 *Advisor* — план на {capital_usd:,.0f}$ "
        f"(профиль: {risk_profile})\n"
    )
    body_parts: list[str] = []
    for a in assets:
        if a in plans_md:
            body_parts.append(plans_md[a])
    return header + "\n\n────────\n\n".join(body_parts)


async def handle_advise_command(message: Message) -> None:
    """Aiogram handler for `/advise [ASSET] [CAPITAL]`."""
    if not feature_enabled():
        await message.answer("Advisor выключен (FEATURE_ADVISOR=0).")
        return

    assets, capital_override = _parse_args(message.text)
    capital_usd = capital_override if capital_override is not None else _get_default_capital_usd()

    user_id = message.from_user.id if message.from_user else 0
    risk_profile, horizon = await _load_user_settings(user_id)

    wait = await message.answer(
        "⏳ Собираю план: цены/ATR/RSI + BTC outlook + твой риск-профиль…"
    )

    try:
        from web_search import fetch_realtime_prices

        prices = await fetch_realtime_prices()
    except Exception as exc:
        logger.exception("advisor: fetch_realtime_prices failed: %s", exc)
        try:
            await wait.delete()
        except Exception:
            pass
        await message.answer(
            "⚠️ Не получилось забрать цены/индикаторы. Попробуй через 1-2 минуты."
        )
        return

    btc_lean, btc_confidence = await _fetch_btc_outlook_snapshot()

    plans_md: dict[str, str] = {}
    for asset in assets:
        try:
            inputs = _build_inputs_from_prices(
                asset=asset,
                prices=prices,
                btc_lean=btc_lean,
                btc_confidence=btc_confidence,
                risk_profile=risk_profile,
                horizon=horizon,
                capital_usd=capital_usd,
            )
            plan = recommend(inputs)
            plans_md[asset] = format_advisor_markdown(plan)
        except Exception as exc:
            logger.exception("advisor: plan failed for %s: %s", asset, exc)
            plans_md[asset] = f"⚠️ *{asset}* — не удалось построить план."

    text = _format_combined_message(assets, plans_md, capital_usd, risk_profile)

    try:
        await wait.delete()
    except Exception:
        pass

    try:
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await message.answer(text)


def register_advisor_handlers(dp) -> None:
    dp.message.register(handle_advise_command, Command("advise"))
    dp.message.register(handle_advise_command, Command("advisor"))


__all__ = [
    "DEFAULT_ASSETS",
    "SUPPORTED_ASSETS",
    "handle_advise_command",
    "register_advisor_handlers",
]
