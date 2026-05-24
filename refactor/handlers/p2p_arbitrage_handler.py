"""Telegram command for P2P arbitrage monitoring.

Provides network adapters for Binance P2P and Bybit P2P and a handler
that composes them. Uses a shared aiohttp session and retries on 429/5xx.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from aiogram.filters import Command
from aiogram.types import Message

from p2p_arbitrage import (
    P2PAdvert,
    P2POpportunity,
    bybit_enabled,
    feature_enabled,
    find_p2p_opportunities,
    format_p2p_report,
    get_alert_cooldown_sec,
    get_assets,
    get_fiats,
    get_max_results,
    get_min_completion_rate_pct,
    get_min_orders,
    get_min_spread_pct,
    get_pay_types,
    get_settlement_buffer_pct,
    merchant_only,
    okx_enabled,
    opportunity_key,
    parse_binance_ad,
    parse_bybit_ad,
)

from refactor.services import JsonAlertStore

logger = logging.getLogger(__name__)

BINANCE_P2P_SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
BYBIT_P2P_SEARCH_URL = "https://api2.bybit.com/fiat/otc/item/online"
DEFAULT_ROWS_PER_SIDE = 20

BYBIT_SIDE_BY_TRADE_TYPE = {
    "BUY": "0",
    "SELL": "1",
}


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
        "merchantCheck": merchant_only(),
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

    attempts = 3
    backoff_base = 0.5
    for attempt in range(attempts):
        try:
            async with session.post(
                BINANCE_P2P_SEARCH_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception:
                        return [], f"{trade_type} malformed response"
                    rows_raw = data.get("data")
                    if not isinstance(rows_raw, list):
                        return [], f"{trade_type} malformed response"
                    return rows_raw, None
                if resp.status == 429 or resp.status >= 500:
                    if attempt < attempts - 1:
                        await asyncio.sleep(backoff_base * (2 ** attempt))
                        continue
                    return [], f"{trade_type} HTTP {resp.status}: {text[:120]}"
                return [], f"{trade_type} HTTP {resp.status}: {text[:120]}"
        except Exception as exc:  # network / timeout
            if attempt < attempts - 1:
                await asyncio.sleep(backoff_base * (2 ** attempt))
                continue
            return [], f"{trade_type} fetch failed: {exc}"


async def fetch_binance_p2p_ads(
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...] = (),
    rows: int = DEFAULT_ROWS_PER_SIDE,
    session: aiohttp.ClientSession | None = None,
) -> tuple[list[P2PAdvert], list[P2PAdvert], tuple[str, ...]]:
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True
    try:
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
    finally:
        if own_session:
            await session.close()

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


def _bybit_side_for_trade_type(trade_type: str) -> str:
    return BYBIT_SIDE_BY_TRADE_TYPE.get(trade_type.upper(), "0")


def _bybit_payment_filter(pay_types: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for pay_type in pay_types:
        value = str(pay_type or "").strip()
        if value.lower().startswith("bybit:"):
            value = value.split(":", 1)[1].strip()
        if not value.isdigit() or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _extract_bybit_rows(data: dict[str, Any], *, trade_type: str) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(data, dict):
        return [], f"Bybit {trade_type} malformed response"
    ret_code = data.get("ret_code")
    if ret_code not in (0, "0", None):
        message = str(data.get("ret_msg") or data.get("retMsg") or "unknown error")
        return [], f"Bybit {trade_type} error {ret_code}: {message[:120]}"
    result = data.get("result") or {}
    if not isinstance(result, dict):
        return [], f"Bybit {trade_type} malformed response"
    rows_raw = result.get("items")
    if rows_raw is None:
        return [], None
    if not isinstance(rows_raw, list):
        return [], f"Bybit {trade_type} malformed response"
    return rows_raw, None


async def _fetch_bybit_p2p_side(
    session: aiohttp.ClientSession,
    *,
    trade_type: str,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...],
    rows: int = DEFAULT_ROWS_PER_SIDE,
) -> tuple[list[dict[str, Any]], str | None]:
    payload = {
        "userId": "",
        "tokenId": asset.upper(),
        "currencyId": fiat.upper(),
        "payment": _bybit_payment_filter(pay_types),
        "side": _bybit_side_for_trade_type(trade_type),
        "size": str(rows),
        "page": "1",
        "amount": "",
        "vaMaker": False,
        "bulkMaker": False,
        "canTrade": False,
        "verificationFilter": 0,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "DialecticEdge/1.0",
    }

    attempts = 3
    backoff_base = 0.5
    data = None
    for attempt in range(attempts):
        try:
            async with session.post(
                BYBIT_P2P_SEARCH_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception:
                        return [], f"Bybit {trade_type} malformed response"
                    break
                if resp.status == 429 or resp.status >= 500:
                    if attempt < attempts - 1:
                        await asyncio.sleep(backoff_base * (2 ** attempt))
                        continue
                    return [], f"Bybit {trade_type} HTTP {resp.status}: {text[:120]}"
                return [], f"Bybit {trade_type} HTTP {resp.status}: {text[:120]}"
        except Exception as exc:
            if attempt < attempts - 1:
                await asyncio.sleep(backoff_base * (2 ** attempt))
                continue
            return [], f"Bybit {trade_type} fetch failed: {exc}"
    return _extract_bybit_rows(data, trade_type=trade_type.upper())


async def fetch_bybit_p2p_ads(
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...] = (),
    rows: int = DEFAULT_ROWS_PER_SIDE,
    session: aiohttp.ClientSession | None = None,
) -> tuple[list[P2PAdvert], list[P2PAdvert], tuple[str, ...]]:
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True
    try:
        buy_raw, sell_raw = await asyncio.gather(
            _fetch_bybit_p2p_side(
                session,
                trade_type="BUY",
                asset=asset,
                fiat=fiat,
                pay_types=pay_types,
                rows=rows,
            ),
            _fetch_bybit_p2p_side(
                session,
                trade_type="SELL",
                asset=asset,
                fiat=fiat,
                pay_types=pay_types,
                rows=rows,
            ),
        )
    finally:
        if own_session:
            await session.close()

    buy_rows, buy_err = buy_raw
    sell_rows, sell_err = sell_raw
    buy_ads = [
        ad for ad in (
            parse_bybit_ad(row, trade_type="BUY", asset=asset, fiat=fiat)
            for row in buy_rows
        )
        if ad is not None
    ]
    sell_ads = [
        ad for ad in (
            parse_bybit_ad(row, trade_type="SELL", asset=asset, fiat=fiat)
            for row in sell_rows
        )
        if ad is not None
    ]
    errors = tuple(err for err in (buy_err, sell_err) if err)
    return buy_ads, sell_ads, errors


async def fetch_okx_p2p_ads(
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...] = (),
    rows: int = DEFAULT_ROWS_PER_SIDE,
    session: aiohttp.ClientSession | None = None,
) -> tuple[list[P2PAdvert], list[P2PAdvert], tuple[str, ...]]:
    """Placeholder OKX fetcher — best-effort stub until public API mapping is known.

    Returns empty lists and no errors to avoid breaking the handler.
    TODO: implement OKX P2P API fetch when stable endpoint is identified.
    """
    return [], [], ()


async def fetch_p2p_ads(
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...] = (),
    rows: int = DEFAULT_ROWS_PER_SIDE,
) -> tuple[list[P2PAdvert], list[P2PAdvert], tuple[str, ...], str]:
    provider_calls = []
    async with aiohttp.ClientSession() as session:
        provider_calls = [
            ("Binance P2P", fetch_binance_p2p_ads(
                asset=asset,
                fiat=fiat,
                pay_types=pay_types,
                rows=rows,
                session=session,
            )),
        ]
        if bybit_enabled():
            provider_calls.append((
                "Bybit P2P",
                fetch_bybit_p2p_ads(
                    asset=asset,
                    fiat=fiat,
                    pay_types=pay_types,
                    rows=rows,
                    session=session,
                ),
            ))
        if okx_enabled():
            provider_calls.append((
                "OKX P2P",
                fetch_okx_p2p_ads(
                    asset=asset,
                    fiat=fiat,
                    pay_types=pay_types,
                    rows=rows,
                    session=session,
                ),
            ))

        results = await asyncio.gather(*(call for _, call in provider_calls), return_exceptions=True)

    buy_ads: list[P2PAdvert] = []
    sell_ads: list[P2PAdvert] = []
    errors: list[str] = []
    sources: list[str] = []
    for (source, _), result in zip(provider_calls, results, strict=False):
        sources.append(source)
        if isinstance(result, Exception):
            errors.append(f"{source} fetch failed: {result}")
            continue
        provider_buy_ads, provider_sell_ads, provider_errors = result
        buy_ads.extend(provider_buy_ads)
        sell_ads.extend(provider_sell_ads)
        errors.extend(provider_errors)
    return buy_ads, sell_ads, tuple(errors), " + ".join(sources)


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
    source_hint = "Binance + Bybit + OKX" if (bybit_enabled() or okx_enabled()) else "Binance"
    wait_msg = await message.answer(f"⏳ Сканирую P2P стакан {source_hint}...")
    buy_ads, sell_ads, errors, source = await fetch_p2p_ads(
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

    # dedupe via JSON alert store + cooldown
    store = JsonAlertStore()
    cooldown = get_alert_cooldown_sec()
    to_alert: list[tuple[P2POpportunity, str]] = []
    suppressed = 0
    for opp in opportunities:
        key = opportunity_key(opp)
        if store.should_alert(key, cooldown):
            to_alert.append((opp, key))
        else:
            suppressed += 1

    if not to_alert:
        if suppressed:
            await _answer_md(message, f"Новых сигналов нет — {suppressed} сигналов подавлено cooldown {cooldown} сек.")
        else:
            await _answer_md(message, "Новых сигналов нет.")
        return

    send_ops = [opp for opp, _ in to_alert]
    await _answer_md(
        message,
        format_p2p_report(
            send_ops,
            asset=asset,
            fiat=fiat,
            pay_types=pay_types,
            source=source,
            errors=errors,
        ),
    )
    for _, key in to_alert:
        try:
            store.record_alert(key)
        except Exception:
            logger.exception("failed to record alert %s", key)


def register_p2p_arbitrage_handlers(dp) -> None:
    dp.message.register(handle_p2p_command, Command("p2p"))
    dp.message.register(handle_p2p_command, Command("p2parb"))
