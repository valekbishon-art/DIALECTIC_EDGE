"""BTC spot-ETF outflow rule.

Surfaces an alert when the BTC ETF basket shows either a multi-day outflow
streak or a single-session large drop. Backed by
``market_indicators.btc_etf_flows``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from market_indicators.btc_etf_flows import (
    aggregate_basket_flows,
    detect_outflow_signal,
    feature_enabled,
    fetch_btc_etf_dailies,
    get_cooldown_sec,
)
from refactor.services.alert_engine import AlertCard

logger = logging.getLogger(__name__)

YAHOO_TIMEOUT_SEC = 8.0


async def _default_http_get(url: str, params: dict[str, Any]) -> Any:
    """Minimal aiohttp-backed JSON GET. Imported lazily so the module stays
    importable without aiohttp."""
    try:
        import aiohttp
    except Exception:  # pragma: no cover - aiohttp is in requirements but be safe
        return None

    headers = {
        "User-Agent": "DialecticEdge/1.0",
        "Accept": "application/json,text/javascript,*/*;q=0.1",
    }
    timeout = aiohttp.ClientTimeout(total=YAHOO_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    return None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("btc_etf_outflow http_get unexpected error: %s", exc)
        return None


@dataclass
class BtcEtfOutflowRule:
    rule_id: str = "btc_etf_outflow"
    cooldown_sec: int = 0
    _http_get: Any = None  # injectable for tests

    @classmethod
    def build(cls) -> "BtcEtfOutflowRule":
        return cls(cooldown_sec=get_cooldown_sec(), _http_get=_default_http_get)

    async def check(self) -> list[AlertCard]:
        if not feature_enabled():
            return []
        http_get = self._http_get or _default_http_get
        try:
            rows = await fetch_btc_etf_dailies(http_get=http_get)
        except Exception as exc:
            logger.info("btc_etf_outflow rule: fetch failed: %s", exc)
            return []
        basket = aggregate_basket_flows(rows)
        signal = detect_outflow_signal(basket)
        if signal is None:
            return []

        tickers_disp = ", ".join(signal.tickers_considered) or "—"
        body = (
            f"{signal.summary}\n\n"
            f"_basket: {tickers_disp}_\n"
            f"_worst day: {signal.worst_day_pct:.2f}% · streak: {signal.streak_days}d_\n"
            f"Authoritative $-flows: https://farside.co.uk/btc/"
        )
        title = "BTC ETF outflow"
        return [
            AlertCard(
                rule_id=self.rule_id,
                severity=signal.severity,
                title=title,
                body=body,
                dedup_key=signal.dedup_key,
            )
        ]
