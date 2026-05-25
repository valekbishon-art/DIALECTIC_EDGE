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


def _format_multipair_report(
    triples: list[tuple[str, str, P2POpportunity]],
    *,
    pair_count_scanned: int,
    errors: list[str],
    source: str,
    top_n: int = 10,
) -> str:
    """Компактный мульти-пар отчёт: топ-N opportunities + сводка."""
    if not triples:
        return (
            f"*🧭 P2P arbitrage scanner*\n\n"
            f"Сканировал {pair_count_scanned} пар ({source}) — пока никаких "
            f"арб-окон выше порога нет.\n\n"
            f"Источники: {source}.\n"
            f"_Попробуй позже или /p2p USDT TRY (или другую пару напрямую)._"
        )

    lines: list[str] = [
        f"*🧭 P2P arbitrage — топ-{min(top_n, len(triples))} окон по миру*",
        "",
        f"_Сканировал {pair_count_scanned} пар ({source}) — найдено "
        f"{len(triples)} opportunities._",
        "",
    ]
    for idx, (asset, fiat, opp) in enumerate(triples[:top_n], start=1):
        # Per-line: ранг + пара + спред + площадки. Компактный single-line.
        buy_src = opp.buy_ad.venue or "?"
        sell_src = opp.sell_ad.venue or "?"
        # Сокращаем «Binance P2P» / «Bybit P2P» до «Binance»/«Bybit».
        buy_src = buy_src.replace(" P2P", "")
        sell_src = sell_src.replace(" P2P", "")
        # M9-E: показываем delta vs spot FX (реальный forex-курс), чтобы
        # пользователь видел «buy на 25% ниже рынка = подозрительно».
        # «vs spot» вместо «vs med» — это РЕАЛЬНЫЙ forex-курс, не median.
        buy_delta = opp.buy_vs_median_pct
        sell_delta = opp.sell_vs_median_pct
        buy_delta_str = f" ({buy_delta:+.1f}% vs spot)" if buy_delta is not None else ""
        sell_delta_str = f" ({sell_delta:+.1f}% vs spot)" if sell_delta is not None else ""
        lines.append(
            f"`{idx:2d}.` *{asset}/{fiat}* — *{opp.net_spread_pct:+.2f}%* "
            f"({buy_src}→{sell_src})  "
            f"buy {opp.buy_ad.price:,.4g}{buy_delta_str} → sell {opp.sell_ad.price:,.4g}{sell_delta_str}"
        )

    lines.append("")
    lines.append(
        f"_Для деталей по конкретной паре: `/p2p USDT RUB`, "
        f"`/p2p USDT TRY`, и т.д._"
    )
    if errors:
        skipped = len(errors)
        lines.append("")
        lines.append(f"_⚠ {skipped} пар пропущено (timeout/нет ads/etc)._")
    return "\n".join(lines)


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
    await _answer_md(message, text)
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
