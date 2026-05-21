"""Telegram command for P2P arbitrage monitoring."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from aiogram.filters import Command
from aiogram.types import Message

from p2p_arbitrage import (
    feature_enabled,
    find_p2p_opportunities,
    format_p2p_report,
    get_assets,
    get_fiats,
    get_max_results,
    get_min_completion_rate_pct,
    get_min_orders,
    get_min_spread_pct,
    get_pay_types,
    get_settlement_buffer_pct,
    merchant_only,
    parse_binance_ad,
)

logger = logging.getLogger(__name__)

BINANCE_P2P_SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
DEFAULT_ROWS_PER_SIDE = 20


async def _fetch_binance_p2p_side(
    session: aiohttp.ClientSession,
    *,
    trade_type: str,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...],
    rows: int = DEFAULT_ROWS_PER_SIDE,
) -> tuple[list[dict[str, Any]], str | None]:
    payload = {
        "asset": asset.upper(),
        "fiat": fiat.upper(),
        "merchantCheck": False,
        "page": 1,
        "payTypes": list(pay_types),
        "publisherType": None,
        "rows": rows,
        "tradeType": trade_type.upper(),
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DialecticEdge/1.0",
    }
    try:
        async with session.post(
            BINANCE_P2P_SEARCH_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                return [], f"{trade_type} HTTP {resp.status}: {text[:120]}"
            data = await resp.json()
    except Exception as exc:
        return [], f"{trade_type} fetch failed: {exc}"
    rows_raw = data.get("data")
    if not isinstance(rows_raw, list):
        return [], f"{trade_type} malformed response"
    return rows_raw, None


async def fetch_binance_p2p_ads(
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...] = (),
    rows: int = DEFAULT_ROWS_PER_SIDE,
) -> tuple[list, list, tuple[str, ...]]:
    async with aiohttp.ClientSession() as session:
        buy_raw, sell_raw = await asyncio.gather(
            _fetch_binance_p2p_side(
                session,
                trade_type="BUY",
                asset=asset,
                fiat=fiat,
                pay_types=pay_types,
                rows=rows,
            ),
            _fetch_binance_p2p_side(
                session,
                trade_type="SELL",
                asset=asset,
                fiat=fiat,
                pay_types=pay_types,
                rows=rows,
            ),
        )
    buy_rows, buy_err = buy_raw
    sell_rows, sell_err = sell_raw
    buy_ads = [
        ad for ad in (
            parse_binance_ad(row, trade_type="BUY", asset=asset, fiat=fiat)
            for row in buy_rows
        )
        if ad is not None
    ]
    sell_ads = [
        ad for ad in (
            parse_binance_ad(row, trade_type="SELL", asset=asset, fiat=fiat)
            for row in sell_rows
        )
        if ad is not None
    ]
    errors = tuple(err for err in (buy_err, sell_err) if err)
    return buy_ads, sell_ads, errors


def _parse_p2p_command(text: str) -> tuple[str, str, tuple[str, ...]]:
    parts = (text or "").split()
    asset = get_assets()[0]
    fiat = get_fiats()[0]
    pay_types = get_pay_types()
    if len(parts) >= 2 and parts[1].strip():
        asset = parts[1].strip().upper()
    if len(parts) >= 3 and parts[2].strip():
        fiat = parts[2].strip().upper()
    if len(parts) >= 4:
        pay_types = tuple(p.strip() for p in " ".join(parts[3:]).split(",") if p.strip())
    return asset, fiat, pay_types


async def _answer_md(message: Message, text: str) -> None:
    try:
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await message.answer(text)


async def handle_p2p_command(message: Message) -> None:
    if not feature_enabled():
        await _answer_md(
            message,
            "*🧭 P2P arbitrage выключен*\n\n"
            "Включи `FEATURE_P2P_ARBITRAGE=1`. Пока фича OFF, бот не дергает P2P endpoints.",
        )
        return

    asset, fiat, pay_types = _parse_p2p_command(message.text or "")
    wait_msg = await message.answer("⏳ Сканирую P2P стакан Binance...")
    buy_ads, sell_ads, errors = await fetch_binance_p2p_ads(
        asset=asset,
        fiat=fiat,
        pay_types=pay_types,
    )
    opportunities = find_p2p_opportunities(
        buy_ads,
        sell_ads,
        min_spread_pct=get_min_spread_pct(),
        settlement_buffer_pct=get_settlement_buffer_pct(),
        min_completion_rate_pct=get_min_completion_rate_pct(),
        min_orders=get_min_orders(),
        merchant_required=merchant_only(),
        preferred_pay_types=pay_types,
        max_results=get_max_results(),
    )
    try:
        await wait_msg.delete()
    except Exception:
        pass
    await _answer_md(
        message,
        format_p2p_report(
            opportunities,
            asset=asset,
            fiat=fiat,
            pay_types=pay_types,
            errors=errors,
        ),
    )


def register_p2p_arbitrage_handlers(dp) -> None:
    dp.message.register(handle_p2p_command, Command("p2p"))
    dp.message.register(handle_p2p_command, Command("p2parb"))
