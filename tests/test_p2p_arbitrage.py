from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from p2p_arbitrage import (
    P2PAdvert,
    feature_enabled,
    find_p2p_opportunities,
    format_p2p_report,
    get_assets,
    parse_binance_ad,
)


class TestP2PEnv(unittest.TestCase):
    def test_feature_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_enabled())

    def test_feature_enabled(self):
        with patch.dict(os.environ, {"FEATURE_P2P_ARBITRAGE": "1"}, clear=True):
            self.assertTrue(feature_enabled())

    def test_assets_dedup_uppercase(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_ASSETS": "usdt,btc,USDT"}, clear=True):
            self.assertEqual(get_assets(), ("USDT", "BTC"))


class TestBinanceParser(unittest.TestCase):
    def test_parse_binance_ad(self):
        row = {
            "adv": {
                "advNo": "123",
                "price": "100.50",
                "minSingleTransAmount": "5000",
                "maxSingleTransAmount": "100000",
                "surplusAmount": "1000",
                "tradeMethods": [
                    {"identifier": "TinkoffNew"},
                    {"tradeMethodName": "RosBankNew"},
                ],
            },
            "advertiser": {
                "nickName": "maker",
                "monthOrderCount": "321",
                "monthFinishRate": 0.987,
                "userType": "merchant",
            },
        }
        ad = parse_binance_ad(row, trade_type="BUY", asset="USDT", fiat="RUB")
        self.assertIsNotNone(ad)
        assert ad is not None
        self.assertEqual(ad.price, 100.5)
        self.assertEqual(ad.payment_methods, ("TinkoffNew", "RosBankNew"))
        self.assertEqual(ad.completed_orders, 321)
        self.assertAlmostEqual(ad.completion_rate_pct or 0, 98.7)
        self.assertTrue(ad.is_merchant)

    def test_bad_price_returns_none(self):
        row = {"adv": {"price": "bad"}, "advertiser": {}}
        self.assertIsNone(parse_binance_ad(row, trade_type="BUY", asset="USDT", fiat="RUB"))


class TestP2POpportunities(unittest.TestCase):
    def _ad(
        self,
        trade_type: str,
        price: float,
        *,
        methods: tuple[str, ...] = ("TinkoffNew",),
        min_fiat: float = 1_000,
        max_fiat: float = 100_000,
        orders: int = 250,
        rate: float = 98.0,
        merchant: bool = True,
    ) -> P2PAdvert:
        return P2PAdvert(
            venue="Binance P2P",
            trade_type=trade_type,
            asset="USDT",
            fiat="RUB",
            price=price,
            min_amount_fiat=min_fiat,
            max_amount_fiat=max_fiat,
            payment_methods=methods,
            advertiser=f"{trade_type}-{price}",
            completed_orders=orders,
            completion_rate_pct=rate,
            is_merchant=merchant,
        )

    def test_finds_net_spread_after_buffer(self):
        opportunities = find_p2p_opportunities(
            [self._ad("BUY", 100.0)],
            [self._ad("SELL", 103.0)],
            min_spread_pct=1.0,
            settlement_buffer_pct=0.35,
        )
        self.assertEqual(len(opportunities), 1)
        self.assertAlmostEqual(opportunities[0].gross_spread_pct, 3.0)
        self.assertAlmostEqual(opportunities[0].net_spread_pct, 2.65)
        self.assertEqual(opportunities[0].shared_payment_methods, ("TinkoffNew",))

    def test_filters_low_quality_counterparty(self):
        opportunities = find_p2p_opportunities(
            [self._ad("BUY", 100.0, orders=5)],
            [self._ad("SELL", 103.0)],
            min_orders=50,
        )
        self.assertEqual(opportunities, [])

    def test_preferred_payment_must_overlap(self):
        opportunities = find_p2p_opportunities(
            [self._ad("BUY", 100.0, methods=("TinkoffNew",))],
            [self._ad("SELL", 103.0, methods=("RosBankNew",))],
            preferred_pay_types=("TinkoffNew",),
        )
        self.assertEqual(opportunities, [])

    def test_report_explains_signal(self):
        opportunities = find_p2p_opportunities(
            [self._ad("BUY", 100.0)],
            [self._ad("SELL", 103.0)],
        )
        text = format_p2p_report(
            opportunities,
            asset="USDT",
            fiat="RUB",
            pay_types=("TinkoffNew",),
        )
        self.assertIn("P2P arbitrage", text)
        self.assertIn("Net", text)
        self.assertIn("Как читать", text)

    def test_empty_report_is_clear(self):
        text = format_p2p_report([], asset="USDT", fiat="RUB", pay_types=())
        self.assertIn("чистого арбитражного окна нет", text)


if __name__ == "__main__":
    unittest.main()
