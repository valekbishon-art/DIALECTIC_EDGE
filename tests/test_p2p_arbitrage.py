from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from p2p_arbitrage import (
    P2PAdvert,
    alerts_enabled,
    bybit_enabled,
    canonical_payment_method,
    feature_enabled,
    find_p2p_opportunities,
    format_p2p_report,
    get_alert_chat_ids,
    get_alert_cooldown_sec,
    get_alert_interval_sec,
    get_assets,
    parse_binance_ad,
    parse_bybit_ad,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestP2PEnv(unittest.TestCase):
    def test_feature_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_enabled())

    def test_feature_enabled(self):
        with patch.dict(os.environ, {"FEATURE_P2P_ARBITRAGE": "1"}, clear=True):
            self.assertTrue(feature_enabled())

    def test_alerts_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(alerts_enabled())

    def test_alert_env_helpers(self):
        env = {
            "FEATURE_P2P_ARBITRAGE_ALERTS": "1",
            "P2P_ARBITRAGE_ALERT_INTERVAL_SEC": "600",
            "P2P_ARBITRAGE_ALERT_COOLDOWN_SEC": "900",
            "P2P_ARBITRAGE_ALERT_CHAT_IDS": "123,bad,456,123",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(alerts_enabled())
            self.assertEqual(get_alert_interval_sec(), 600)
            self.assertEqual(get_alert_cooldown_sec(), 900)
            self.assertEqual(get_alert_chat_ids([999]), (123, 456))

    def test_alert_chat_ids_fallback_to_admins(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_alert_chat_ids([0, 123, 123]), (123,))

    def test_assets_dedup_uppercase(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_ASSETS": "usdt,btc,USDT"}, clear=True):
            self.assertEqual(get_assets(), ("USDT", "BTC"))

    def test_bybit_provider_default_on_under_parent_feature(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(bybit_enabled())

    def test_bybit_provider_can_be_disabled(self):
        with patch.dict(os.environ, {"FEATURE_P2P_BYBIT": "0"}, clear=True):
            self.assertFalse(bybit_enabled())


class TestBinanceParser(unittest.TestCase):
    def test_parse_binance_ad(self):
        row = {
            "adv": {
                "advNo": "123",
                "price": "100.50",
                "minSingleTransAmount": "5000",
                "maxSingleTransAmount": "100000",
                "surplusAmount": "1000",
                "payTimeLimit": "15",
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
        self.assertGreater(ad.fetched_at, 0)
        self.assertEqual(ad.payment_window_min, 15)

    def test_bad_price_returns_none(self):
        row = {"adv": {"price": "bad"}, "advertiser": {}}
        self.assertIsNone(parse_binance_ad(row, trade_type="BUY", asset="USDT", fiat="RUB"))


class TestBybitParser(unittest.TestCase):
    def test_parse_bybit_buy_ad(self):
        row = load_fixture("bybit_p2p_buy.json")["result"]["items"][0]
        ad = parse_bybit_ad(row, trade_type="BUY", asset="USDT", fiat="RUB")
        self.assertIsNotNone(ad)
        assert ad is not None
        self.assertEqual(ad.venue, "Bybit P2P")
        self.assertEqual(ad.trade_type, "BUY")
        self.assertEqual(ad.price, 81.86)
        self.assertEqual(ad.min_amount_fiat, 10_000.0)
        self.assertEqual(ad.max_amount_fiat, 300_000.0)
        self.assertAlmostEqual(ad.available_asset or 0, 98_735.9392)
        self.assertEqual(ad.payment_methods, ("bybit:18", "bybit:40", "bybit:14"))
        self.assertEqual(ad.completed_orders, 656)
        self.assertAlmostEqual(ad.completion_rate_pct or 0, 100.0)
        self.assertTrue(ad.is_merchant)
        self.assertEqual(ad.advert_id, "1918875499790905344")
        self.assertGreater(ad.fetched_at, 0)
        self.assertEqual(ad.payment_window_min, 15)

    def test_parse_bybit_sell_ad(self):
        row = load_fixture("bybit_p2p_sell.json")["result"]["items"][0]
        ad = parse_bybit_ad(row, trade_type="SELL", asset="USDT", fiat="RUB")
        self.assertIsNotNone(ad)
        assert ad is not None
        self.assertEqual(ad.venue, "Bybit P2P")
        self.assertEqual(ad.trade_type, "SELL")
        self.assertEqual(ad.price, 72.19)
        self.assertEqual(ad.payment_methods, ("bybit:40",))
        self.assertEqual(ad.completed_orders, 250)

    def test_bad_bybit_price_returns_none(self):
        row = {"price": "bad", "minAmount": "100", "maxAmount": "200"}
        self.assertIsNone(parse_bybit_ad(row, trade_type="BUY", asset="USDT", fiat="RUB"))


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
        venue: str = "Binance P2P",
        fetched_at: float | None = None,
        payment_window_min: int | None = 15,
        advertiser: str | None = None,
    ) -> P2PAdvert:
        return P2PAdvert(
            venue=venue,
            trade_type=trade_type,
            asset="USDT",
            fiat="RUB",
            price=price,
            min_amount_fiat=min_fiat,
            max_amount_fiat=max_fiat,
            payment_methods=methods,
            advertiser=advertiser or f"{trade_type}-{price}",
            completed_orders=orders,
            completion_rate_pct=rate,
            is_merchant=merchant,
            fetched_at=time.time() if fetched_at is None else fetched_at,
            payment_window_min=payment_window_min,
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
        self.assertEqual(opportunities[0].shared_payment_methods, ("tinkoff",))

    def test_legacy_settlement_buffer_env_fallback(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_SETTLEMENT_BUFFER_PCT": "0.80"}, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0)],
                [self._ad("SELL", 103.0)],
                min_spread_pct=1.0,
            )
        self.assertEqual(len(opportunities), 1)
        self.assertAlmostEqual(opportunities[0].buffer_pct, 0.80)
        self.assertAlmostEqual(opportunities[0].net_spread_pct, 2.20)

    def test_fee_model_components_cross_venue(self):
        env = {
            "P2P_BANK_FEE_TINKOFF": "0.50",
            "P2P_CRYPTO_WITHDRAW_USDT": "1.0",
            "P2P_SLIPPAGE_PCT": "0.20",
        }
        with patch.dict(os.environ, env, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, venue="Binance P2P")],
                [self._ad("SELL", 103.0, venue="Bybit P2P")],
                min_spread_pct=1.0,
            )
        self.assertEqual(len(opportunities), 1)
        opportunity = opportunities[0]
        self.assertAlmostEqual(opportunity.bank_fee_pct, 0.50)
        self.assertAlmostEqual(opportunity.slippage_pct, 0.20)
        self.assertAlmostEqual(opportunity.crypto_withdraw_fee_usdt, 1.0)
        self.assertAlmostEqual(opportunity.crypto_withdraw_fee_pct, 0.10)
        self.assertAlmostEqual(opportunity.buffer_pct, 0.80)
        self.assertAlmostEqual(opportunity.net_spread_pct, 2.20)
        self.assertEqual(opportunity.cost_payment_method, "tinkoff")

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

    def test_canonical_payment_methods_overlap(self):
        self.assertEqual(canonical_payment_method("Тинькофф"), "tinkoff")
        opportunities = find_p2p_opportunities(
            [self._ad("BUY", 100.0, methods=("TinkoffNew",))],
            [self._ad("SELL", 103.0, methods=("Тинькофф",))],
            preferred_pay_types=("Tinkoff",),
        )
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].shared_payment_methods, ("tinkoff",))

    def test_short_payment_window_adds_warning(self):
        opportunities = find_p2p_opportunities(
            [self._ad("BUY", 100.0, orders=600, rate=99.0, payment_window_min=10)],
            [self._ad("SELL", 103.0, orders=600, rate=99.0, payment_window_min=15)],
        )
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0].risk_level, "MEDIUM")
        self.assertIn("короткое окно оплаты 10 мин", opportunities[0].warnings)

    def test_risk_adjusted_ranking_prefers_lower_risk(self):
        opportunities = find_p2p_opportunities(
            [
                self._ad("BUY", 100.0, orders=600, rate=99.0, advertiser="low-buy"),
                self._ad("BUY", 100.0, orders=60, rate=94.0, advertiser="high-buy"),
            ],
            [
                self._ad("SELL", 103.0, orders=600, rate=99.0, advertiser="low-sell"),
                self._ad("SELL", 105.0, orders=60, rate=94.0, advertiser="high-sell"),
            ],
            min_orders=50,
            min_completion_rate_pct=90,
            max_results=4,
        )
        self.assertGreater(len(opportunities), 1)
        self.assertEqual(opportunities[0].buy_ad.advertiser, "low-buy")
        self.assertEqual(opportunities[0].risk_level, "LOW")
        self.assertGreater(opportunities[0].score, opportunities[1].score)

    def test_dedup_by_advertiser_pair_after_ranking(self):
        opportunities = find_p2p_opportunities(
            [
                self._ad("BUY", 100.0, orders=600, rate=99.0, advertiser="maker-a"),
                self._ad("BUY", 100.5, orders=600, rate=99.0, advertiser="maker-a"),
            ],
            [
                self._ad("SELL", 104.0, orders=600, rate=99.0, advertiser="maker-b"),
                self._ad("SELL", 103.5, orders=600, rate=99.0, advertiser="maker-b"),
            ],
            max_results=5,
        )
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(
            (opportunities[0].buy_ad.advertiser, opportunities[0].sell_ad.advertiser),
            ("maker-a", "maker-b"),
        )

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
        self.assertIn("Binance P2P", text)
        self.assertIn("данные собраны", text)
        self.assertIn("Как читать", text)

    def test_report_warns_on_stale_data(self):
        old = time.time() - 180
        opportunities = find_p2p_opportunities(
            [self._ad("BUY", 100.0, fetched_at=old)],
            [self._ad("SELL", 103.0, fetched_at=old)],
        )
        with patch.dict(os.environ, {"P2P_OPPORTUNITY_TTL_SEC": "120"}, clear=True):
            text = format_p2p_report(
                opportunities,
                asset="USDT",
                fiat="RUB",
                pay_types=("TinkoffNew",),
            )
        self.assertIn("данные собраны", text)
        self.assertIn("данные старше TTL 120 сек", text)

    def test_empty_report_is_clear(self):
        text = format_p2p_report([], asset="USDT", fiat="RUB", pay_types=())
        self.assertIn("чистого арбитражного окна нет", text)


if __name__ == "__main__":
    unittest.main()
