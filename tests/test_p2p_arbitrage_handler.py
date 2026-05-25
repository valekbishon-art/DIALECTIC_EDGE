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
        BYBIT_SIDE_BY_TRADE_TYPE,
        OKX_SIDE_BY_TRADE_TYPE,
        _bybit_payment_filter,
        _extract_bybit_rows,
        _extract_okx_rows,
        _fetch_bybit_p2p_side,
        _fetch_okx_p2p_side,
        _okx_side_for_trade_type,
        _parse_p2p_command,
        fetch_bybit_p2p_ads,
        fetch_okx_p2p_ads,
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


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestBybitSideMappingPolarity(unittest.IsolatedAsyncioTestCase):
    """Regression guard: Bybit ``side`` имеет ОБРАТНЫЙ смысл от Binance ``tradeType``.

    Bybit P2P:
      side=0 → BID-ads (мейкер хочет купить USDT, тейкер ПРОДАЁТ) → цены НИЖЕ спота
      side=1 → ASK-ads (мейкер хочет продать USDT, тейкер ПОКУПАЕТ) → цены ВЫШЕ спота

    Семантика бота (см. ``P2PAdvert.side_label``):
      trade_type="BUY"  = тейкер покупает USDT  → должен брать ASK-сторону = Bybit side=1
      trade_type="SELL" = тейкер продаёт USDT   → должен брать BID-сторону = Bybit side=0

    Исторически здесь была инверсия (``{"BUY":"0","SELL":"1"}``): бот скрещивал
    BID-стакан с ASK-стаканом и регулярно «находил» фантомные спреды +10–14%
    (на деле -10% убытки, если их пытаться исполнить). Тест ловит регрессию
    на нескольких уровнях: константа, HTTP-payload, end-to-end полярность.
    """

    def test_mapping_constants_buy_to_1_sell_to_0(self):
        # Маппинг — единственная точка истины в коде. Если кто-то снова
        # «починит» его обратно на 0/1 — этот тест упадёт первым.
        self.assertEqual(BYBIT_SIDE_BY_TRADE_TYPE["BUY"], "1")
        self.assertEqual(BYBIT_SIDE_BY_TRADE_TYPE["SELL"], "0")

    async def _capture_payload_side(self, trade_type: str) -> str:
        captured: dict = {}

        class _FakeResp:
            status = 200

            async def text(self) -> str:
                return "{}"

            async def json(self) -> dict:
                return {"result": {"items": []}}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        class _FakeSession:
            def post(self, url, json=None, headers=None, timeout=None):
                captured["payload"] = json
                return _FakeResp()

        await _fetch_bybit_p2p_side(
            _FakeSession(),
            trade_type=trade_type,
            asset="USDC",
            fiat="MXN",
            pay_types=(),
        )
        return captured["payload"]["side"]

    async def test_buy_request_uses_side_1_in_payload(self):
        self.assertEqual(await self._capture_payload_side("BUY"), "1")

    async def test_sell_request_uses_side_0_in_payload(self):
        self.assertEqual(await self._capture_payload_side("SELL"), "0")

    async def test_orderbook_polarity_after_mapping(self):
        """End-to-end: BUY-ads (asks) должны быть ДОРОЖЕ SELL-ads (bids).

        Полярность реального стакана — лучший детектор инверсии. Если она
        сломается, ``find_p2p_opportunities`` снова начнёт скрещивать
        BID×ASK с одной площадки и плодить фантомные «арбитражи».
        """
        ask_fixture = {
            "ret_code": 0,
            "ret_msg": "SUCCESS",
            "result": {
                "items": [
                    {
                        "id": "ask1",
                        "price": "17.89",
                        "minAmount": "1000",
                        "maxAmount": "10000",
                        "nickName": "asker",
                        "tokenId": "USDC",
                        "currencyId": "MXN",
                        "side": 1,
                        "finishNum": 100,
                        "recentExecuteRate": 99,
                        "payments": [],
                        "lastQuantity": "1000",
                    }
                ]
            },
        }
        bid_fixture = {
            "ret_code": 0,
            "ret_msg": "SUCCESS",
            "result": {
                "items": [
                    {
                        "id": "bid1",
                        "price": "17.19",
                        "minAmount": "1000",
                        "maxAmount": "10000",
                        "nickName": "bidder",
                        "tokenId": "USDC",
                        "currencyId": "MXN",
                        "side": 0,
                        "finishNum": 100,
                        "recentExecuteRate": 99,
                        "payments": [],
                        "lastQuantity": "1000",
                    }
                ]
            },
        }

        async def fake_side(*_, **kwargs):
            # После фикса: trade_type=BUY (тейкер покупает) ходит за ASK-фикстурой,
            # trade_type=SELL (тейкер продаёт) — за BID. До фикса было наоборот,
            # и assertGreater ниже падал.
            fixture = ask_fixture if kwargs["trade_type"] == "BUY" else bid_fixture
            rows, error = _extract_bybit_rows(fixture, trade_type=kwargs["trade_type"])
            return rows, error

        with patch(
            "refactor.handlers.p2p_arbitrage_handler._fetch_bybit_p2p_side",
            new=fake_side,
        ):
            buy_ads, sell_ads, errors = await fetch_bybit_p2p_ads(asset="USDC", fiat="MXN")

        self.assertEqual(errors, ())
        self.assertEqual(len(buy_ads), 1)
        self.assertEqual(len(sell_ads), 1)
        self.assertGreater(
            buy_ads[0].price,
            sell_ads[0].price,
            "BUY-ads (asks) must be priced HIGHER than SELL-ads (bids); "
            "inverted polarity means BYBIT_SIDE_BY_TRADE_TYPE is bugged again.",
        )


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestOkxSideMapping(unittest.TestCase):
    """OKX P2P `side` имеет ту же семантику что у Bybit (MAKER-side) —
    инвертирована относительно Binance `tradeType` (TAKER-side).

    Live-проверено USDT/MXN:
      side=buy  → data.buy   top 17.24 (ниже спота 17.28) → BIDs
      side=sell → data.sell  top 17.35 (выше спота) → ASKs

    Семантика бота (`P2PAdvert.trade_type`):
      "BUY"  = taker buys USDT = ASK-side = OKX side="sell"
      "SELL" = taker sells USDT = BID-side = OKX side="buy"

    Эти проверки — guard против инверсии, аналогичной баге Bybit (PR #38).
    """

    def test_mapping_constants_buy_to_sell_sell_to_buy(self):
        self.assertEqual(OKX_SIDE_BY_TRADE_TYPE["BUY"], "sell")
        self.assertEqual(OKX_SIDE_BY_TRADE_TYPE["SELL"], "buy")

    def test_helper_returns_inverted_side(self):
        self.assertEqual(_okx_side_for_trade_type("BUY"), "sell")
        self.assertEqual(_okx_side_for_trade_type("SELL"), "buy")
        # case-insensitive
        self.assertEqual(_okx_side_for_trade_type("buy"), "sell")
        self.assertEqual(_okx_side_for_trade_type("sell"), "buy")
        # неизвестное значение → fallback на "sell" (ASK-сторону, более «дорогую»)
        self.assertEqual(_okx_side_for_trade_type("garbage"), "sell")


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestOkxResponseExtraction(unittest.TestCase):
    """`_extract_okx_rows` фильтрует API-ответ в плоский список row'ов и
    рапортует error'ы вне happy-path."""

    def _resp(self, *, side: str, rows: list[dict] | None) -> dict:
        return {"code": 0, "msg": None, "data": {side: rows, "recommend": [], "tagged": []}}

    def test_happy_returns_requested_side(self):
        # Запрашиваем side="sell" → ожидаем `data.sell`, игнорируем `data.buy`.
        payload = {
            "code": 0,
            "data": {
                "buy": [{"price": "10.0", "id": "B1"}],
                "sell": [{"price": "11.0", "id": "S1"}, {"price": "11.5", "id": "S2"}],
            },
        }
        rows, err = _extract_okx_rows(payload, side="sell", trade_type="BUY")
        self.assertIsNone(err)
        self.assertEqual([r["id"] for r in rows], ["S1", "S2"])

    def test_null_side_returns_empty_no_error(self):
        # `data.buy=null` (OKX так делает когда ничего нет) → пустой list, no error
        payload = {"code": 0, "data": {"buy": None, "sell": []}}
        rows, err = _extract_okx_rows(payload, side="buy", trade_type="SELL")
        self.assertIsNone(err)
        self.assertEqual(rows, [])

    def test_error_code_propagated(self):
        # OKX restricts trading в каких-то регионах (RUB, например):
        payload = {"code": 17007, "msg": "region restricted", "data": None}
        rows, err = _extract_okx_rows(payload, side="buy", trade_type="SELL")
        self.assertEqual(rows, [])
        self.assertIn("17007", err or "")
        self.assertIn("region restricted", err or "")

    def test_malformed_response_reported(self):
        rows, err = _extract_okx_rows("not a dict", side="buy", trade_type="SELL")
        self.assertEqual(rows, [])
        self.assertIn("malformed", err or "")


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestOkxFetchPolarity(unittest.IsolatedAsyncioTestCase):
    """End-to-end: после `fetch_okx_p2p_ads` BUY-ads (ASKs) должны быть
    дороже SELL-ads (BIDs). Любая будущая инверсия в OKX_SIDE_BY_TRADE_TYPE
    провалит этот тест.
    """

    BUY_ADS_RAW = [
        {
            "price": "17.35",
            "quoteMinAmountPerOrder": "500",
            "quoteMaxAmountPerOrder": "100000",
            "availableAmount": "1000",
            "paymentMethods": ["bank"],
            "nickName": "AskMerchant1",
            "completedOrderQuantity": 2000,
            "completedRate": "0.99",
            "creatorType": "certified",
            "paymentTimeoutMinutes": 15,
            "baseCurrency": "usdt",
            "quoteCurrency": "mxn",
            "id": "ask1",
        },
        {
            "price": "17.40",
            "quoteMinAmountPerOrder": "100",
            "quoteMaxAmountPerOrder": "50000",
            "paymentMethods": ["bank"],
            "nickName": "AskMerchant2",
            "completedOrderQuantity": 500,
            "completedRate": "0.97",
            "creatorType": "certified",
            "baseCurrency": "usdt",
            "quoteCurrency": "mxn",
            "id": "ask2",
        },
    ]
    SELL_ADS_RAW = [
        {
            "price": "17.24",
            "quoteMinAmountPerOrder": "200",
            "quoteMaxAmountPerOrder": "50000",
            "paymentMethods": ["bank"],
            "nickName": "BidMerchant1",
            "completedOrderQuantity": 3000,
            "completedRate": "0.995",
            "creatorType": "certified",
            "baseCurrency": "usdt",
            "quoteCurrency": "mxn",
            "id": "bid1",
        },
        {
            "price": "17.20",
            "quoteMinAmountPerOrder": "300",
            "quoteMaxAmountPerOrder": "30000",
            "paymentMethods": ["bank"],
            "nickName": "BidMerchant2",
            "completedOrderQuantity": 800,
            "completedRate": "0.98",
            "creatorType": "certified",
            "baseCurrency": "usdt",
            "quoteCurrency": "mxn",
            "id": "bid2",
        },
    ]

    async def test_fetch_okx_ads_polarity(self):
        # Симулируем _fetch_okx_p2p_side: trade_type=BUY должен запросить
        # `side="sell"` (где живут ASK-ads), trade_type=SELL → `side="buy"`.
        captured_sides: list[str] = []

        async def fake_side(session, *, trade_type, asset, fiat, pay_types, rows=20):
            side = _okx_side_for_trade_type(trade_type)
            captured_sides.append((trade_type, side))
            if trade_type == "BUY":
                return list(self.BUY_ADS_RAW), None
            return list(self.SELL_ADS_RAW), None

        with patch(
            "refactor.handlers.p2p_arbitrage_handler._fetch_okx_p2p_side",
            new=fake_side,
        ):
            buy_ads, sell_ads, errors = await fetch_okx_p2p_ads(asset="USDT", fiat="MXN")

        # Каждый side был запрошен с инвертированным OKX-side
        self.assertIn(("BUY", "sell"), captured_sides)
        self.assertIn(("SELL", "buy"), captured_sides)
        self.assertEqual(errors, ())

        # Все BUY-ads (ASKs) ≥ всех SELL-ads (BIDs) — ключевой инвариант
        self.assertTrue(buy_ads, "должны быть buy_ads")
        self.assertTrue(sell_ads, "должны быть sell_ads")
        min_ask = min(a.price for a in buy_ads)
        max_bid = max(a.price for a in sell_ads)
        self.assertGreater(
            min_ask,
            max_bid,
            f"OKX polarity broken: min_ask={min_ask} ≤ max_bid={max_bid} — инверсия!",
        )

    async def test_fetch_okx_propagates_errors(self):
        async def fake_side(session, *, trade_type, asset, fiat, pay_types, rows=20):
            if trade_type == "SELL":
                return [], "OKX SELL API error code=17007: region restricted"
            return list(self.BUY_ADS_RAW), None

        with patch(
            "refactor.handlers.p2p_arbitrage_handler._fetch_okx_p2p_side",
            new=fake_side,
        ):
            buy_ads, sell_ads, errors = await fetch_okx_p2p_ads(asset="USDT", fiat="RUB")

        self.assertEqual(sell_ads, [])
        self.assertGreater(len(buy_ads), 0)
        self.assertEqual(
            errors, ("OKX SELL API error code=17007: region restricted",)
        )


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestOkxFetchHttpParams(unittest.IsolatedAsyncioTestCase):
    """Verify HTTP-level: `_fetch_okx_p2p_side` шлёт GET с правильными
    query-параметрами (включая инвертированный `side`).
    """

    async def _capture_request(self, *, trade_type: str) -> dict:
        captured: dict = {}

        class _FakeResp:
            status = 200

            async def text(self):
                return "{}"

            async def json(self):
                return {"code": 0, "data": {"buy": [], "sell": []}}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        class _FakeSession:
            def get(self, url, params=None, headers=None, timeout=None):
                captured["url"] = url
                captured["params"] = params
                return _FakeResp()

        await _fetch_okx_p2p_side(
            _FakeSession(),
            trade_type=trade_type,
            asset="USDT",
            fiat="MXN",
            pay_types=(),
        )
        return captured

    async def test_buy_request_uses_side_sell(self):
        captured = await self._capture_request(trade_type="BUY")
        self.assertEqual(captured["url"].endswith("/v3/c2c/tradingOrders/books"), True)
        self.assertEqual(captured["params"]["side"], "sell")
        self.assertEqual(captured["params"]["baseCurrency"], "USDT")
        self.assertEqual(captured["params"]["quoteCurrency"], "MXN")

    async def test_sell_request_uses_side_buy(self):
        captured = await self._capture_request(trade_type="SELL")
        self.assertEqual(captured["params"]["side"], "buy")


if __name__ == "__main__":
    unittest.main()
