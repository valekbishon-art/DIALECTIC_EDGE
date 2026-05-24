"""``/btc`` command: comprehensive BTC outlook + next-move forecast.

Aggregates many independent signals (price, funding, OI, top-trader L/S,
dominance, US BTC spot-ETF basket, fear&greed, quant ensemble) into a single
bull/bear/neutral verdict via :mod:`core.btc_outlook`. Each fetcher is
gracefully degradable — if a source fails, that signal is just dropped from
the breakdown.

Rationale: "Биток вниз — всё идёт вниз". A single coherent BTC verdict is
more actionable than per-asset snippets scattered around ``/daily``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp
from aiogram.filters import Command
from aiogram.types import Message

from core.btc_outlook import (
    BTCOutlookInputs,
    compute_btc_outlook,
    format_btc_outlook_markdown,
)

logger = logging.getLogger(__name__)

BINANCE_FAPI = "https://fapi.binance.com"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
FNG_URL = "https://api.alternative.me/fng/"

DEFAULT_TIMEOUT_SEC = 6.0


def _http_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SEC)


async def _fetch_binance_btc_core(session: aiohttp.ClientSession) -> dict[str, Any]:
    """Return ``{price, change_pct, volume, funding_rate_pct}`` from Binance perp."""
    out: dict[str, Any] = {}
    try:
        async with session.get(
            f"{BINANCE_FAPI}/fapi/v1/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            timeout=_http_timeout(),
        ) as resp:
            if resp.status == 200:
                t = await resp.json()
                out["price"] = float(t.get("lastPrice", 0)) or None
                out["change_pct"] = float(t.get("priceChangePercent", 0))
                out["volume"] = float(t.get("quoteVolume", 0))
    except Exception as exc:
        logger.debug("btc_handler: binance 24hr fetch failed: %s", exc)

    try:
        async with session.get(
            f"{BINANCE_FAPI}/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=_http_timeout(),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data:
                    # Binance returns fundingRate as fraction (0.0001 = 0.01%).
                    out["funding_rate_pct"] = float(data[0].get("fundingRate", 0)) * 100.0
    except Exception as exc:
        logger.debug("btc_handler: binance funding fetch failed: %s", exc)

    return out


async def _fetch_btc_oi_change_24h(session: aiohttp.ClientSession) -> float | None:
    """Return 24h % change in BTC perp open interest via ``openInterestHist``."""
    try:
        async with session.get(
            f"{BINANCE_FAPI}/futures/data/openInterestHist",
            params={"symbol": "BTCUSDT", "period": "1h", "limit": 25},
            timeout=_http_timeout(),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as exc:
        logger.debug("btc_handler: openInterestHist fetch failed: %s", exc)
        return None

    if not isinstance(data, list) or len(data) < 2:
        return None
    try:
        first = float(data[0].get("sumOpenInterest", 0))
        last = float(data[-1].get("sumOpenInterest", 0))
    except (TypeError, ValueError):
        return None
    if first <= 0:
        return None
    return (last - first) / first * 100.0


async def _fetch_btc_top_trader_ls(session: aiohttp.ClientSession) -> float | None:
    """Bybit / Binance top-trader long-short ratio (most recent sample)."""
    try:
        async with session.get(
            f"{BINANCE_FAPI}/futures/data/topLongShortAccountRatio",
            params={"symbol": "BTCUSDT", "period": "1h", "limit": 1},
            timeout=_http_timeout(),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as exc:
        logger.debug("btc_handler: ls ratio fetch failed: %s", exc)
        return None
    if not isinstance(data, list) or not data:
        return None
    try:
        return float(data[0].get("longShortRatio", 0))
    except (TypeError, ValueError):
        return None


async def _fetch_btc_dominance(session: aiohttp.ClientSession) -> float | None:
    try:
        async with session.get(COINGECKO_GLOBAL, timeout=_http_timeout()) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
    except Exception as exc:
        logger.debug("btc_handler: coingecko global fetch failed: %s", exc)
        return None
    try:
        return float(payload["data"]["market_cap_percentage"]["btc"])
    except (KeyError, TypeError, ValueError):
        return None


async def _fetch_fear_greed(session: aiohttp.ClientSession) -> int | None:
    try:
        async with session.get(FNG_URL, params={"limit": 1}, timeout=_http_timeout()) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
    except Exception as exc:
        logger.debug("btc_handler: fear/greed fetch failed: %s", exc)
        return None
    try:
        return int(payload["data"][0]["value"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


async def _fetch_btc_daily_closes(
    session: aiohttp.ClientSession, limit: int = 210
) -> list[float]:
    try:
        async with session.get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": limit},
            timeout=_http_timeout(),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception as exc:
        logger.debug("btc_handler: klines fetch failed: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    closes: list[float] = []
    for row in data:
        try:
            closes.append(float(row[4]))
        except (TypeError, ValueError, IndexError):
            continue
    return closes


def _quant_inputs_from_closes(closes: list[float]) -> tuple[str | None, float | None]:
    if not closes:
        return None, None
    try:
        from quant_filter import quant_verdict
    except Exception as exc:
        logger.debug("btc_handler: quant_filter import failed: %s", exc)
        return None, None
    try:
        verdict = quant_verdict(closes, closes)
    except Exception as exc:
        logger.debug("btc_handler: quant_verdict failed: %s", exc)
        return None, None
    direction = verdict.get("verdict")
    confidence = verdict.get("confidence", 0) or 0
    try:
        strength = max(0.0, min(1.0, float(confidence) / 100.0))
    except (TypeError, ValueError):
        strength = 0.0
    return (direction if direction in ("LONG", "SHORT", "NEUTRAL") else None), strength


async def _fetch_etf_signals(
    session: aiohttp.ClientSession,
) -> tuple[float | None, str | None]:
    try:
        from market_indicators.btc_etf_flows import (
            aggregate_basket_flows,
            detect_outflow_signal,
            fetch_btc_etf_dailies,
        )
    except Exception as exc:
        logger.debug("btc_handler: btc_etf_flows import failed: %s", exc)
        return None, None

    async def http_get(url: str, params: dict[str, Any]) -> Any:
        try:
            async with session.get(url, params=params, timeout=_http_timeout()) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as exc:
            logger.debug("btc_handler: yahoo fetch %s failed: %s", url, exc)
            return None

    try:
        rows = await fetch_btc_etf_dailies(http_get=http_get)
    except Exception as exc:
        logger.debug("btc_handler: btc_etf_dailies failed: %s", exc)
        return None, None
    if not rows:
        return None, None

    basket = aggregate_basket_flows(rows)
    if not basket:
        return None, None
    recent = basket[-5:] if len(basket) >= 5 else basket
    avg_pct = sum(b.get("avg_change_pct", 0.0) for b in recent) / max(1, len(recent))

    severity: str | None = None
    try:
        sig = detect_outflow_signal(basket)
        if sig is not None:
            severity = sig.severity
    except Exception as exc:
        logger.debug("btc_handler: detect_outflow_signal failed: %s", exc)

    return avg_pct, severity


async def _fetch_ai_narrative(verdict_summary: str, signals_blob: str) -> str | None:
    """Optional: ask the synth agent for a 2-3 sentence narrative."""
    if os.getenv("FEATURE_BTC_OUTLOOK_AI", "1") != "1":
        return None
    try:
        from ai_provider import ai
    except Exception as exc:
        logger.debug("btc_handler: ai_provider import failed: %s", exc)
        return None
    system = (
        "Ты криптотрейдинг-аналитик. Дай 2-3 коротких предложения по-русски, "
        "проясняющих следующий ход BTC. Используй переданные сигналы, не "
        "выдумывай новые. Не давай прямого финсовета."
    )
    prompt = (
        f"BTC outlook движка: {verdict_summary}\n"
        f"Активные сигналы:\n{signals_blob}\n\n"
        "Сформулируй короткое narrative-резюме: что вероятнее в ближайшие "
        "24-72 часа и какой главный риск этого взгляда."
    )
    try:
        text = await ai.synth(prompt, system=system)
    except Exception as exc:
        logger.debug("btc_handler: ai.synth failed: %s", exc)
        return None
    if not text:
        return None
    # Trim — синт-агенты иногда длиннят.
    text = text.strip()
    if len(text) > 700:
        text = text[:700].rsplit(" ", 1)[0] + "…"
    return text


async def fetch_btc_outlook_inputs() -> BTCOutlookInputs:
    """Fetch every signal we can; return an inputs bundle.

    Each fetcher fails silently on its own — missing signals just drop out of
    the verdict's breakdown rather than blocking the whole report.
    """
    async with aiohttp.ClientSession() as session:
        (
            binance_core,
            oi_change,
            ls_ratio,
            dominance,
            fng,
            closes,
            etf_result,
        ) = await asyncio.gather(
            _fetch_binance_btc_core(session),
            _fetch_btc_oi_change_24h(session),
            _fetch_btc_top_trader_ls(session),
            _fetch_btc_dominance(session),
            _fetch_fear_greed(session),
            _fetch_btc_daily_closes(session),
            _fetch_etf_signals(session),
            return_exceptions=False,
        )

    quant_dir, quant_strength = _quant_inputs_from_closes(closes)
    etf_avg, etf_sig = etf_result

    return BTCOutlookInputs(
        btc_price_usd=binance_core.get("price"),
        price_change_24h_pct=binance_core.get("change_pct"),
        funding_rate_8h_pct=binance_core.get("funding_rate_pct"),
        oi_change_24h_pct=oi_change,
        top_trader_ls_ratio=ls_ratio,
        btc_dominance_pct=dominance,
        dominance_change_7d_pct=None,
        etf_basket_change_5d_avg_pct=etf_avg,
        etf_outflow_signal=etf_sig,
        stablecoin_supply_delta_24h_pct=None,
        options_skew_25d=None,
        fear_greed_index=fng,
        quant_verdict_direction=quant_dir,
        quant_verdict_strength=quant_strength,
        regime=None,
    )


async def build_btc_outlook_message() -> str:
    inputs = await fetch_btc_outlook_inputs()
    verdict = compute_btc_outlook(inputs)
    signals_blob = "\n".join(
        f"- {c.label} {c.raw_value} ({'bull' if c.direction > 0 else 'bear' if c.direction < 0 else 'flat'}, w={c.weight:.2f})"
        for c in verdict.contributions
    )
    ai_narrative = None
    if verdict.contributions:
        ai_narrative = await _fetch_ai_narrative(verdict.summary, signals_blob)
    return format_btc_outlook_markdown(verdict, inputs, ai_narrative=ai_narrative)


async def handle_btc_command(message: Message) -> None:
    wait_msg = await message.answer("⏳ Собираю BTC сигналы (price, funding, OI, ETF, dominance, F&G, quant)…")
    try:
        text = await build_btc_outlook_message()
    except Exception as exc:
        logger.exception("btc_handler: build_btc_outlook_message failed: %s", exc)
        text = (
            "⚠️ Не получилось собрать BTC outlook — все источники упали или таймаут. "
            "Попробуй через 1-2 минуты."
        )
    try:
        await wait_msg.delete()
    except Exception:
        pass
    try:
        await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception:
        await message.answer(text)


def register_btc_handlers(dp) -> None:
    dp.message.register(handle_btc_command, Command("btc"))
    dp.message.register(handle_btc_command, Command("bitcoin"))
