"""Telegram command for P2P arbitrage monitoring.

Provides network adapters for Binance P2P and Bybit P2P and a handler
that composes them. Uses a shared aiohttp session and retries on 429/5xx.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from p2p_arbitrage import (
    P2PAdvert,
    P2POpportunity,
    bybit_enabled,
    feature_enabled,
    find_p2p_opportunities,
    format_p2p_report,
    get_alert_cooldown_sec,
    get_assets,
    get_button_scan_assets,
    get_button_scan_fiats,
    get_fiats,
    get_max_results,
    get_min_completion_rate_pct,
    get_min_orders,
    get_min_spread_pct,
    get_pay_types,
    get_scan_concurrency,
    get_settlement_buffer_pct,
    merchant_only,
    okx_enabled,
    opportunity_key,
    parse_binance_ad,
    parse_bybit_ad,
    parse_okx_ad,
)

from refactor.services import JsonAlertStore

logger = logging.getLogger(__name__)

BINANCE_P2P_SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
BYBIT_P2P_SEARCH_URL = "https://api2.bybit.com/fiat/otc/item/online"
OKX_P2P_SEARCH_URL = "https://www.okx.com/v3/c2c/tradingOrders/books"
DEFAULT_ROWS_PER_SIDE = 20

# Bybit `side` имеет обратный смысл от Binance `tradeType`. На Bybit P2P API:
#   side=0 → BID-ads (мейкер хочет купить USDT, тейкер продаёт)  → цены НИЖЕ спота
#   side=1 → ASK-ads (мейкер хочет продать USDT, тейкер покупает) → цены ВЫШЕ спота
# Семантика бота (см. `P2PAdvert.side_label`):
#   trade_type="BUY"  = тейкер покупает USDT  = ASK-сторона = Bybit side=1
#   trade_type="SELL" = тейкер продаёт USDT   = BID-сторона = Bybit side=0
# До этого PR маппинг был инвертирован, из-за чего find_p2p_opportunities
# скрещивал BID-стакан с ASK-стаканом и регулярно «находил» фантомные
# спреды +10–14% (на деле это убытки, потому что обе цены не исполнимы как
# заявлено). См. test_bybit_side_mapping_matches_orderbook_polarity ниже.
BYBIT_SIDE_BY_TRADE_TYPE = {
    "BUY": "1",
    "SELL": "0",
}

# OKX P2P `side` query parameter: семантика как у Bybit — обозначает
# сторону MAKER'а, а не TAKER'а.
#   side=buy  → makers хотят купить USDT → taker продаёт → BID-сторона (наш SELL)
#   side=sell → makers хотят продать USDT → taker покупает → ASK-сторона (наш BUY)
# Live-проверено: USDT/MXN side=buy top price 17.24 (ниже спота 17.28 → BIDs),
# side=sell top price 17.35 (выше спота → ASKs). Mapping инвертирован
# относительно Binance (`tradeType="BUY"` = taker buys = ASK = OKX side=sell).
OKX_SIDE_BY_TRADE_TYPE = {
    "BUY": "sell",
    "SELL": "buy",
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
    # Server-side merchant filter: pass vaMaker=True when merchant_only() is on so
    # Bybit returns only verified-account makers (saves traffic vs. post-fetch filter).
    only_merchants = merchant_only()
    payload = {
        "userId": "",
        "tokenId": asset.upper(),
        "currencyId": fiat.upper(),
        "payment": _bybit_payment_filter(pay_types),
        "side": _bybit_side_for_trade_type(trade_type),
        "size": str(rows),
        "page": "1",
        "amount": "",
        "vaMaker": only_merchants,
        "bulkMaker": False,
        "canTrade": False,
        "verificationFilter": 1 if only_merchants else 0,
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


def _okx_side_for_trade_type(trade_type: str) -> str:
    return OKX_SIDE_BY_TRADE_TYPE.get(trade_type.upper(), "sell")


def _extract_okx_rows(
    data: Any,
    *,
    side: str,
    trade_type: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Достаёт массив advert'ов из OKX P2P response.

    OKX отдаёт оба `data.buy` и `data.sell` независимо от `side` — но
    только запрошенный side содержит данные. Берём ту сторону, которая
    соответствует нашему запросу.
    """
    if not isinstance(data, dict):
        return [], f"OKX {trade_type} malformed response"
    code = data.get("code")
    if code not in (0, "0", None):
        msg = str(data.get("msg") or data.get("message") or "unknown error")[:120]
        return [], f"OKX {trade_type} API error code={code}: {msg}"
    payload = data.get("data")
    if not isinstance(payload, dict):
        return [], f"OKX {trade_type} malformed response (data not dict)"
    rows = payload.get(side)
    if rows is None:
        # OKX иногда возвращает `data.buy=null` если ничего нет.
        return [], None
    if not isinstance(rows, list):
        return [], f"OKX {trade_type} malformed response (data.{side} not list)"
    return rows, None


async def _fetch_okx_p2p_side(
    session: aiohttp.ClientSession,
    *,
    trade_type: str,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...],
    rows: int = DEFAULT_ROWS_PER_SIDE,
) -> tuple[list[dict[str, Any]], str | None]:
    """OKX endpoint — публичный, без авторизации. Использует GET с query
    params; `t=<ms>` это cache-buster (OKX UI добавляет его в каждый запрос).
    """
    side = _okx_side_for_trade_type(trade_type)
    # OKX endpoint ничего не делает с `paymentMethod=all` для multi-фильтра —
    # отдаёт всё и фильтрует на клиенте. Для нас это OK, мы фильтруем дальше
    # сами через `_payment_intersection`.
    params: dict[str, Any] = {
        "t": str(int(asyncio.get_event_loop().time() * 1000)),
        "quoteCurrency": fiat.upper(),
        "baseCurrency": asset.upper(),
        "side": side,
        "paymentMethod": "all",
        "userType": "all",
        "showTrade": "true",
        "showFollow": "false",
        "showAlreadyTraded": "false",
        "isAbleFilter": "false",
        "limit": str(rows),
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "DialecticEdge/1.0",
    }
    attempts = 3
    backoff_base = 0.5
    data: Any = None
    for attempt in range(attempts):
        try:
            async with session.get(
                OKX_P2P_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception:
                        return [], f"OKX {trade_type} malformed response"
                    break
                if resp.status == 429 or resp.status >= 500:
                    if attempt < attempts - 1:
                        await asyncio.sleep(backoff_base * (2 ** attempt))
                        continue
                    return [], f"OKX {trade_type} HTTP {resp.status}: {text[:120]}"
                return [], f"OKX {trade_type} HTTP {resp.status}: {text[:120]}"
        except Exception as exc:
            if attempt < attempts - 1:
                await asyncio.sleep(backoff_base * (2 ** attempt))
                continue
            return [], f"OKX {trade_type} fetch failed: {exc}"
    return _extract_okx_rows(data, side=side, trade_type=trade_type.upper())


async def fetch_okx_p2p_ads(
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...] = (),
    rows: int = DEFAULT_ROWS_PER_SIDE,
    session: aiohttp.ClientSession | None = None,
) -> tuple[list[P2PAdvert], list[P2PAdvert], tuple[str, ...]]:
    """OKX P2P fetcher.

    Эндпойнт ``https://www.okx.com/v3/c2c/tradingOrders/books`` публичный,
    без авторизации. Семантика `side` обратная Binance: см. комментарий
    у ``OKX_SIDE_BY_TRADE_TYPE``.
    """
    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True
    try:
        buy_raw, sell_raw = await asyncio.gather(
            _fetch_okx_p2p_side(
                session,
                trade_type="BUY",
                asset=asset,
                fiat=fiat,
                pay_types=pay_types,
                rows=rows,
            ),
            _fetch_okx_p2p_side(
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
            parse_okx_ad(row, trade_type="BUY", asset=asset, fiat=fiat)
            for row in buy_rows
        )
        if ad is not None
    ]
    sell_ads = [
        ad for ad in (
            parse_okx_ad(row, trade_type="SELL", asset=asset, fiat=fiat)
            for row in sell_rows
        )
        if ad is not None
    ]
    errors = tuple(err for err in (buy_err, sell_err) if err)
    return buy_ads, sell_ads, errors


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


def _has_explicit_pair(text: str) -> bool:
    """`/p2p USDT RUB` (explicit) или `🧭 P2P арбитраж` (button) ?

    Только командный синтаксис `/p2p ...` с хотя бы одним тикерным
    аргументом считается explicit single-pair режимом. Persistent
    button шлёт текст без `/`-префикса → multi-pair scan.
    """
    parts = (text or "").split()
    if len(parts) < 2:
        return False
    first = parts[0]
    # Persistent button = текст без `/`-префикса → button mode.
    if not first.startswith("/"):
        return False
    token = parts[1].strip()
    if not token:
        return False
    # Тикер: 2-6 ASCII букв, любой регистр (USDT/usdt/Usdt).
    if not token.isascii():
        return False
    if not (2 <= len(token) <= 6):
        return False
    return token.isalpha()


def _filter_pay_types_for_fiat(pay_types: tuple[str, ...], fiat: str) -> tuple[str, ...]:
    """В multi-pair scan не передаём RU-banks-фильтр в TRY/ARS/etc.

    Юзер мог установить ``P2P_ARBITRAGE_PAY_TYPES=sber,tinkoff`` для RUB —
    но эти банки не существуют в TRY-маркете, и фильтр оставит 0 ads.
    Поэтому для multi-pair: если ``pay_types`` непуст и fiat ≠ RUB,
    игнорируем pay_types для этой пары (graceful degradation).
    """
    if not pay_types:
        return ()
    if fiat.upper() == "RUB":
        return pay_types
    return ()


async def scan_all_pairs(
    *,
    assets: tuple[str, ...] | None = None,
    fiats: tuple[str, ...] | None = None,
    pay_types: tuple[str, ...] = (),
    rows: int = DEFAULT_ROWS_PER_SIDE,
    concurrency: int | None = None,
    per_pair_timeout_sec: float = 8.0,
) -> tuple[list[tuple[str, str, P2POpportunity]], list[str], str]:
    """Multi-pair P2P scan: проходит по всем парам (asset × fiat) параллельно.

    Возвращает:
      • list[(asset, fiat, opportunity)] — все opportunities со всех пар,
        отсортированные по net_spread_pct desc (топ снаружи)
      • list[str] — accumulated errors per pair
      • str — source description ("Binance + Bybit")

    Юзер: «расширить p2p до всех валютных пар в мире — а то кнопка не
    показывает выгоду». Раньше button сканировал ОДНУ пару USDT/RUB и в
    эффективном RU-market'е почти никогда не находил арб. Теперь сканер
    проходит ~60 пар (CIS + LATAM + MENA + AFRICA × USDT/USDC/FDUSD) и
    показывает топ-N арб-окон по всему миру.

    Параллелизм: semaphore=N (default 5) ограничивает одновременные
    fetch'и чтобы не словить 429 от Binance/Bybit. Per-pair timeout
    защищает от висящих запросов в slow-fiat-markets.
    """
    assets_list = assets if assets is not None else get_button_scan_assets()
    fiats_list = fiats if fiats is not None else get_button_scan_fiats()
    sem = asyncio.Semaphore(max(1, concurrency if concurrency is not None else get_scan_concurrency()))

    aggregated: list[tuple[str, str, P2POpportunity]] = []
    errors: list[str] = []
    sources_seen: list[str] = []

    async def _scan_one_pair(asset: str, fiat: str) -> None:
        async with sem:
            try:
                buy_ads, sell_ads, pair_errors, source = await asyncio.wait_for(
                    fetch_p2p_ads(
                        asset=asset,
                        fiat=fiat,
                        pay_types=_filter_pay_types_for_fiat(pay_types, fiat),
                        rows=rows,
                    ),
                    timeout=per_pair_timeout_sec,
                )
            except asyncio.TimeoutError:
                errors.append(f"{asset}/{fiat}: timeout {per_pair_timeout_sec}s")
                return
            except Exception as e:
                errors.append(f"{asset}/{fiat}: fetch failed: {e}")
                return

            if source and source not in sources_seen:
                sources_seen.append(source)
            for pe in pair_errors:
                errors.append(f"{asset}/{fiat}: {pe}")

            try:
                opps = find_p2p_opportunities(
                    buy_ads,
                    sell_ads,
                    min_spread_pct=get_min_spread_pct(),
                    settlement_buffer_pct=get_settlement_buffer_pct(),
                    min_completion_rate_pct=get_min_completion_rate_pct(),
                    min_orders=get_min_orders(),
                    merchant_required=merchant_only(),
                    preferred_pay_types=_filter_pay_types_for_fiat(pay_types, fiat),
                    max_results=get_max_results(),
                )
            except Exception as e:
                errors.append(f"{asset}/{fiat}: find_opportunities error: {e}")
                return

            for opp in opps:
                aggregated.append((asset, fiat, opp))

    await asyncio.gather(
        *(_scan_one_pair(a, f) for a in assets_list for f in fiats_list),
        return_exceptions=False,
    )

    # Sort by raw spread (highest profit first).
    aggregated.sort(key=lambda triple: triple[2].net_spread_pct, reverse=True)
    source_str = " + ".join(sources_seen) or "Binance"
    return aggregated, errors, source_str


P2P_DIVIDER = "─" * 22


def _spread_emoji(net_spread_pct: float) -> str:
    """Эмодзи-индикатор по величине спреда.

    После M9-F (cap 8%) реалистичный профит ~1–3%, всё что выше — либо
    тонкий рынок (UAH/TRY с устойчивой премией), либо опасное окно которое
    закроется до settlement'а. Юзер должен сразу видеть «зелёное безопасное»
    vs «жёлтое сомнительное» на сканировании.
    """
    if net_spread_pct >= 5.0:
        return "🔥"
    if net_spread_pct >= 2.0:
        return "✅"
    return "🟡"


def _venue_label(venue: str | None) -> str:
    """«Binance P2P» → «Binance», единый style across providers."""
    return (venue or "?").replace(" P2P", "")


def _format_multipair_report(
    triples: list[tuple[str, str, P2POpportunity]],
    *,
    pair_count_scanned: int,
    errors: list[str],
    source: str,
    top_n: int = 10,
) -> str:
    """Компактный мульти-пар отчёт: топ-N opportunities + сводка.

    Layout: header c сводкой → разделитель → entries (2 строки на entry) →
    разделитель → footer с подсказками. Каждая entry имеет:
      • эмодзи-индикатор риска по net spread (🔥 ≥ 5%, ✅ 2–5%, 🟡 < 2%)
      • пара / spread / cross-venue route в одной строке
      • buy/sell prices с delta vs spot в отдельной строке (выровнено)
    """
    if not triples:
        return (
            f"*🧭 P2P arbitrage scanner*\n\n"
            f"Сканировал *{pair_count_scanned}* пар ({source}) — пока никаких "
            f"арб-окон выше порога нет.\n\n"
            f"_Попробуй позже или `/p2p USDT TRY` (или другую пару напрямую)._"
        )

    shown = min(top_n, len(triples))
    lines: list[str] = [
        f"*🧭 P2P arbitrage — топ-{shown} окон по миру*",
        P2P_DIVIDER,
        f"🔍 Сканировано: *{pair_count_scanned} пар* — _{source}_",
        f"📊 Найдено: *{len(triples)}* opportunit" + ("y" if len(triples) == 1 else "ies"),
    ]
    if errors:
        lines.append(f"⚠️ Пропущено: *{len(errors)}* пар (нет ads / timeout)")
    lines.append(P2P_DIVIDER)
    lines.append("")

    for idx, (asset, fiat, opp) in enumerate(triples[:top_n], start=1):
        buy_src = _venue_label(opp.buy_ad.venue)
        sell_src = _venue_label(opp.sell_ad.venue)
        route_arrow = "↻" if buy_src == sell_src else "⇄"
        # delta vs spot FX (реальный forex-курс) — anchor показывает,
        # насколько ad дороже/дешевле рынка. «vs spot» ≠ «vs median».
        buy_delta = opp.buy_vs_median_pct
        sell_delta = opp.sell_vs_median_pct
        buy_delta_str = f" _({buy_delta:+.1f}%)_" if buy_delta is not None else ""
        sell_delta_str = f" _({sell_delta:+.1f}%)_" if sell_delta is not None else ""
        emoji = _spread_emoji(opp.net_spread_pct)
        lines.append(
            f"`#{idx:<2}` {emoji} *{asset}/{fiat}*  —  *{opp.net_spread_pct:+.2f}%*  "
            f"`{buy_src} {route_arrow} {sell_src}`"
        )
        lines.append(
            f"       💰 buy `{opp.buy_ad.price:,.4g}`{buy_delta_str}  →  "
            f"💵 sell `{opp.sell_ad.price:,.4g}`{sell_delta_str}"
        )
        lines.append("")

    lines.append(P2P_DIVIDER)
    lines.append("_📖 Жми «Гайд» ниже — пошаговая инструкция как исполнить._")
    lines.append(
        "_🔎 Детали по паре: `/p2p USDT TRY` (или любая другая)._"
    )
    return "\n".join(lines)


def _multipair_inline_kb() -> InlineKeyboardMarkup:
    """Inline-клавиатура под сообщение топ-N окон.

    Сейчас одна кнопка — «Гайд» (показывает как реально исполнить арб).
    Сюда же можно навешать «Refresh» / «Filters» в будущем.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Гайд: как исполнить", callback_data="p2p:guide")],
    ])


P2P_GUIDE_TEXT = (
    "*📖 P2P-арбитраж — как исполнить*\n"
    + P2P_DIVIDER
    + "\n\n"
    "*Что это?*\n"
    "Покупаешь USDT/USDC на бирже A по низкой цене, переводишь в внутренний\n"
    "кошелёк, продаёшь на бирже B по высокой. Профит = разница − fees − slippage.\n\n"

    "*① Перед сделкой — проверь что окно живое*\n"
    "• Открой обе ad'ы (buy + sell) на их биржах прямо сейчас.\n"
    "• Цены могут уйти за 30–60 сек после моего скана — пересчитай.\n"
    "• Если spread схлопнулся ниже *2%* — пропускай, slippage съест профит.\n\n"

    "*② Проверь обоих контрагентов*\n"
    "• ≥ *500* завершённых сделок\n"
    "• ≥ *98%* completion rate\n"
    "• Verified merchant (бейдж) — без него выше риск freeze/appeal\n"
    "• Метод оплаты — тот, что у тебя реально есть на банке/кошельке\n\n"

    "*③ Размер позиции*\n"
    "• Не более *availableAmount* у обоих ad'ов\n"
    "• Бери небольшую долю — если spread схлопнется пока ждёшь settlement,\n"
    "  большая сумма даст большой убыток\n\n"

    "*④ Тайминг*\n"
    "• Buy-сторона: bank transfer 5–15 мин, USDT release сразу после\n"
    "• Перевод внутри биржи: instant\n"
    "• Sell-сторона: тот же 5–15 мин на release\n"
    "• *Окно может уехать пока ты ждёшь.* Это главный риск.\n\n"

    "*🚫 Когда пропускать (несмотря на красивый %)*\n"
    "• Spread > *8%* без объяснения — wishlist-ad, не исполнится\n"
    "• Экзотический метод оплаты (rare bank, intermediary)\n"
    "• Сделок < 50 у advertiser'а\n"
    "• Ad висит < 5 минут (без истории)\n"
    "• Capital-control fiat (RUB, IRR, SDG) — риск bank freeze\n\n"

    "*⚡ Реальный профит*\n"
    "`Net % ≈ scanned_spread − 0.5%` (slippage за время settlement)\n"
    "• Binance/Bybit/OKX P2P maker fee = *0*\n"
    "• Внутрибиржевой перевод = *0*\n"
    "• Окно +1.5% реально даёт ~*+1%*, +3% → ~*+2.5%*\n\n"

    "*🚨 Главные риски*\n"
    "• *Price moved* — sell-ad уехал вверх пока ждал settlement\n"
    "• *Counterparty cancel* — ты уже отправил fiat, открываешь appeal\n"
    "• *Bank freeze* — регулярные P2P-операции могут пометиться как подозрительные\n"
    "• *Exchange KYC* — большие объёмы триггерят дополнительную верификацию\n\n"

    + P2P_DIVIDER
    + "\n_Не финансовый совет. P2P-арбитраж — это работа, не easy money._\n"
    "_Начни с минимального лота чтобы прочувствовать механику._"
)


async def handle_p2p_guide_callback(callback: CallbackQuery) -> None:
    """Inline-кнопка «📖 Гайд» под сообщением P2P top-N — показывает
    пошаговый guide по исполнению P2P-арба.
    """
    try:
        await callback.answer()
    except Exception:
        logger.debug("failed to ack p2p:guide callback", exc_info=True)
    target = callback.message
    if target is None:
        return
    try:
        await target.answer(
            P2P_GUIDE_TEXT,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception:
        # Markdown иногда падает на спецсимволах — fallback в plain text.
        await target.answer(P2P_GUIDE_TEXT)


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

    raw_text = message.text or ""
    # Button click (persistent keyboard, без явных аргументов) — multi-pair
    # глобальный скан. /p2p USDT RUB — single-pair (backward compat).
    if not _has_explicit_pair(raw_text):
        await _handle_p2p_multipair(message)
        return

    asset, fiat, pay_types = _parse_p2p_command(raw_text)
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

    # Persist surfaced opportunities into the self-audit log if the feature is on.
    # Backcheck loop in scheduler will revisit them after the configured delay.
    try:
        from p2p_audit import feature_enabled as _audit_enabled
        from p2p_audit_io import persist_opportunities_for_audit

        if _audit_enabled() and send_ops:
            await persist_opportunities_for_audit(send_ops)
    except Exception:
        logger.exception("failed to persist P2P opportunities for self-audit")


async def _handle_p2p_multipair(message: Message) -> None:
    """Button-click handler: multi-pair scan ~60 пар, топ-N opportunities.

    Юзер: «расширить p2p до всех валютных пар в мире — кнопка ничего
    не показывает». Раньше button = single-pair USDT/RUB scan = почти
    никогда не находил арб (RU market эффективен). Теперь = global scan
    по высокоарбитражным регионам.
    """
    assets_list = get_button_scan_assets()
    fiats_list = get_button_scan_fiats()
    pay_types = get_pay_types()
    pair_count = len(assets_list) * len(fiats_list)

    source_hint = "Binance + Bybit" if bybit_enabled() else "Binance"
    wait_msg = await message.answer(
        f"⏳ Сканирую {pair_count} P2P пар по миру ({source_hint})...\n"
        f"_~10-15 сек, не уходи._",
        parse_mode="Markdown",
    )

    try:
        triples, errors, source = await scan_all_pairs(
            assets=assets_list,
            fiats=fiats_list,
            pay_types=pay_types,
        )
    except Exception as e:
        logger.exception("p2p multipair scan failed")
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await _answer_md(message, f"*🧭 P2P arbitrage*\n\nСканер упал: `{e}`")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    # Dedup через JsonAlertStore: каждое opportunity получает свой ключ,
    # чтобы не спамить одинаковыми окнами в течение cooldown'а.
    store = JsonAlertStore()
    cooldown = get_alert_cooldown_sec()
    surfaced: list[tuple[str, str, P2POpportunity]] = []
    surfaced_keys: list[str] = []
    suppressed = 0
    for asset, fiat, opp in triples:
        key = opportunity_key(opp)
        if store.should_alert(key, cooldown):
            surfaced.append((asset, fiat, opp))
            surfaced_keys.append(key)
        else:
            suppressed += 1

    if not surfaced:
        await _answer_md(
            message,
            _format_multipair_report(
                [],
                pair_count_scanned=pair_count,
                errors=errors,
                source=source,
            )
            + (
                f"\n\n_({suppressed} окон подавлено cooldown'ом {cooldown}s.)_"
                if suppressed
                else ""
            ),
        )
        return

    text = _format_multipair_report(
        surfaced,
        pair_count_scanned=pair_count,
        errors=errors,
        source=source,
    )
    if suppressed:
        text += f"\n_({suppressed} окон подавлено cooldown'ом {cooldown}s.)_"
    try:
        await message.answer(
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=_multipair_inline_kb(),
        )
    except Exception:
        await message.answer(text, reply_markup=_multipair_inline_kb())
    for key in surfaced_keys:
        try:
            store.record_alert(key)
        except Exception:
            logger.exception("failed to record alert %s", key)

    # Self-audit: персистим все surfaced opportunities, если фича on.
    try:
        from p2p_audit import feature_enabled as _audit_enabled
        from p2p_audit_io import persist_opportunities_for_audit

        if _audit_enabled() and surfaced:
            await persist_opportunities_for_audit([opp for _, _, opp in surfaced])
    except Exception:
        logger.exception("failed to persist multi-pair opportunities for self-audit")


async def handle_p2p_audit_command(message: Message) -> None:
    """``/p2paudit`` — последние 100 показанных opportunities + рекомендация по порогу."""
    try:
        from p2p_audit import feature_enabled as _audit_enabled
        from p2p_audit_io import format_audit_report
    except Exception:
        await _answer_md(message, "*📊 P2P self-audit*\n\nМодуль аудита недоступен.")
        return

    if not _audit_enabled():
        await _answer_md(
            message,
            "*📊 P2P self-audit*\n\n"
            "Фича выключена. Включи `FEATURE_P2P_SELF_AUDIT=1` чтобы бот логировал "
            "показанные opportunities и через `P2P_AUDIT_BACKCHECK_DELAY_MIN` мин "
            "перепроверял, не схлопнулся ли спред.",
        )
        return

    try:
        report = await format_audit_report(limit=100)
    except Exception:
        logger.exception("p2p audit report generation failed")
        await _answer_md(message, "*📊 P2P self-audit*\n\nОшибка при чтении журнала.")
        return
    await _answer_md(message, report)


def register_p2p_arbitrage_handlers(dp) -> None:
    dp.message.register(handle_p2p_command, Command("p2p"))
    dp.message.register(handle_p2p_command, Command("p2parb"))
    dp.message.register(handle_p2p_audit_command, Command("p2paudit"))
    dp.callback_query.register(handle_p2p_guide_callback, F.data == "p2p:guide")
