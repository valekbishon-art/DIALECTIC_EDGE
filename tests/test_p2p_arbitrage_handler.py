from __future__ import annotations

import os
import json
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "test:test")

try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

if HAS_AIOGRAM:
    from refactor.handlers.p2p_arbitrage_handler import (
        _bybit_payment_filter,
        _extract_bybit_rows,
        _fetch_bybit_p2p_side,
        _parse_p2p_command,
        fetch_bybit_p2p_ads,
    )


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestP2PCommandParsing(unittest.TestCase):
    def test_defaults(self):
        asset, fiat, pay_types = _parse_p2p_command("/p2p")
        self.assertEqual(asset, "USDT")
        self.assertEqual(fiat, "RUB")
        self.assertEqual(pay_types, ())

    def test_asset_fiat_and_payments(self):
        asset, fiat, pay_types = _parse_p2p_command("/p2p usdt rub TinkoffNew,RosBankNew")
        self.assertEqual(asset, "USDT")
        self.assertEqual(fiat, "RUB")
        self.assertEqual(pay_types, ("TinkoffNew", "RosBankNew"))


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestBybitProviderParsing(unittest.TestCase):
    def test_bybit_payment_filter_keeps_numeric_ids(self):
        self.assertEqual(
            _bybit_payment_filter(("TinkoffNew", "bybit:40", "40", "14")),
            ["40", "14"],
        )

    def test_extract_bybit_rows_happy(self):
        rows, error = _extract_bybit_rows(load_fixture("bybit_p2p_buy.json"), trade_type="BUY")
        self.assertIsNone(error)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "1918875499790905344")

    def test_extract_bybit_rows_empty(self):
        rows, error = _extract_bybit_rows(load_fixture("bybit_p2p_empty.json"), trade_type="BUY")
        self.assertIsNone(error)
        self.assertEqual(rows, [])

    def test_extract_bybit_rows_error(self):
        rows, error = _extract_bybit_rows(load_fixture("bybit_p2p_error.json"), trade_type="BUY")
        self.assertEqual(rows, [])
        self.assertIn("Bybit BUY error 10001", error or "")


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestBybitFetch(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_bybit_ads_happy_from_fixtures(self):
        async def fake_side(*args, **kwargs):
            name = "bybit_p2p_buy.json" if kwargs["trade_type"] == "BUY" else "bybit_p2p_sell.json"
            rows, error = _extract_bybit_rows(load_fixture(name), trade_type=kwargs["trade_type"])
            return rows, error

        with patch(
            "refactor.handlers.p2p_arbitrage_handler._fetch_bybit_p2p_side",
            new=fake_side,
        ):
            buy_ads, sell_ads, errors = await fetch_bybit_p2p_ads(asset="USDT", fiat="RUB")

        self.assertEqual(errors, ())
        self.assertEqual(len(buy_ads), 1)
        self.assertEqual(len(sell_ads), 1)
        self.assertEqual(buy_ads[0].venue, "Bybit P2P")
        self.assertEqual(sell_ads[0].venue, "Bybit P2P")

    async def test_fetch_bybit_ads_propagates_side_errors(self):
        async def fake_side(*args, **kwargs):
            if kwargs["trade_type"] == "BUY":
                return [], "Bybit BUY error 10001: parameter error"
            rows, error = _extract_bybit_rows(load_fixture("bybit_p2p_empty.json"), trade_type="SELL")
            return rows, error

        with patch(
            "refactor.handlers.p2p_arbitrage_handler._fetch_bybit_p2p_side",
            new=fake_side,
        ):
            buy_ads, sell_ads, errors = await fetch_bybit_p2p_ads(asset="USDT", fiat="RUB")

        self.assertEqual(buy_ads, [])
        self.assertEqual(sell_ads, [])
        self.assertEqual(errors, ("Bybit BUY error 10001: parameter error",))


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestBybitMerchantServerSideFilter(unittest.IsolatedAsyncioTestCase):
    """Soft #3 — when merchant_only=True we must pass vaMaker server-side."""

    async def _capture_payload(self, env_merchant: str) -> dict:
        captured: dict = {}

        class _FakeResp:
            status = 200

            async def text(self):
                return "{}"

            async def json(self):
                return {"result": {"items": []}}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        class _FakeSession:
            def post(self, url, json=None, headers=None, timeout=None):
                captured["url"] = url
                captured["payload"] = json
                return _FakeResp()

        with patch.dict(os.environ, {"P2P_ARBITRAGE_MERCHANT_ONLY": env_merchant}, clear=False):
            await _fetch_bybit_p2p_side(
                _FakeSession(),
                trade_type="BUY",
                asset="USDT",
                fiat="RUB",
                pay_types=(),
            )
        return captured

    async def test_passes_vamaker_when_merchant_only(self):
        captured = await self._capture_payload("1")
        self.assertIn("payload", captured)
        self.assertEqual(captured["payload"].get("vaMaker"), True)
        self.assertEqual(captured["payload"].get("verificationFilter"), 1)

    async def test_no_vamaker_when_merchant_off(self):
        captured = await self._capture_payload("0")
        self.assertIn("payload", captured)
        self.assertEqual(captured["payload"].get("vaMaker"), False)
        self.assertEqual(captured["payload"].get("verificationFilter"), 0)


if __name__ == "__main__":
    unittest.main()
