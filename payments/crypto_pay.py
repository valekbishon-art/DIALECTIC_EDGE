"""
payments/crypto_pay.py — CryptoBot (Crypto Pay) integration for subscriptions.

API docs: https://help.crypt.bot/crypto-pay-api

Flow:
  1. User clicks "Оплатить" → bot calls create_invoice() → returns pay_url
  2. User pays in CryptoBot wallet
  3. Bot polls check_invoice() periodically OR uses webhook
  4. On confirmed payment → grant_vip(user_id, 30)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CRYPTOBOT_API_TOKEN = os.getenv("CRYPTOBOT_API_TOKEN", "")
# Testnet: https://testnet-pay.crypt.bot  |  Production: https://pay.crypt.bot
_BASE_URL = "https://pay.crypt.bot/api"

# Subscription price (configurable via env).
SUB_PRICE_AMOUNT = os.getenv("SUB_PRICE_AMOUNT", "5")  # default $5
SUB_PRICE_ASSET = os.getenv("SUB_PRICE_ASSET", "USDT")  # USDT / TON / BTC
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))


def is_enabled() -> bool:
    return bool(CRYPTOBOT_API_TOKEN)


async def _api_request(method: str, params: Optional[dict] = None) -> Optional[dict]:
    """Make authenticated request to Crypto Pay API."""
    if not is_enabled():
        return None
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_API_TOKEN}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_BASE_URL}/{method}",
                headers=headers,
                json=params or {},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.error("CryptoPay %s error: %s", method, data)
                    return None
                return data.get("result")
    except Exception as e:
        logger.error("CryptoPay %s request failed: %s", method, e)
        return None


async def get_me() -> Optional[dict]:
    """Test API connection — returns app info."""
    return await _api_request("getMe")


async def create_invoice(
    user_id: int,
    amount: Optional[str] = None,
    asset: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[str]:
    """Create payment invoice. Returns pay_url or None on failure.

    The user_id is stored in payload for matching on webhook/poll.
    """
    params = {
        "asset": asset or SUB_PRICE_ASSET,
        "amount": amount or SUB_PRICE_AMOUNT,
        "description": description or f"Dialectic Edge VIP — {SUB_DAYS} дней",
        "payload": str(user_id),
        "expires_in": 3600,  # 1 hour to pay
    }
    result = await _api_request("createInvoice", params)
    if not result:
        return None
    invoice_id = result.get("invoice_id")
    pay_url = result.get("pay_url") or result.get("bot_invoice_url")
    logger.info(
        "CryptoPay invoice created: id=%s user=%s amount=%s %s",
        invoice_id, user_id, params["amount"], params["asset"],
    )
    return pay_url


async def check_invoice(invoice_id: int) -> Optional[dict]:
    """Check single invoice status. Returns invoice dict or None."""
    result = await _api_request("getInvoices", {"invoice_ids": str(invoice_id)})
    if not result or not isinstance(result, dict):
        return None
    items = result.get("items") or []
    return items[0] if items else None


async def get_paid_invoices(offset: int = 0, count: int = 100) -> list[dict]:
    """List recently paid invoices (for polling fallback)."""
    result = await _api_request("getInvoices", {
        "status": "paid",
        "offset": offset,
        "count": count,
    })
    if not result or not isinstance(result, dict):
        return []
    return result.get("items") or []
