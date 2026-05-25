from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from p2p_arbitrage import (
    DEFAULT_P2P_ASSETS,
    DEFAULT_P2P_FIATS,
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
    get_fiats,
    parse_binance_ad,
    parse_bybit_ad,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestP2PEnv(unittest.TestCase):
    def test_feature_default_on(self):
        # По умолчанию P2P-сканер включён — явная просьба владельца
        # (read-only мониторинг, денег не двигает).
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(feature_enabled())

    def test_feature_explicit_off(self):
        with patch.dict(os.environ, {"FEATURE_P2P_ARBITRAGE": "0"}, clear=True):
            self.assertFalse(feature_enabled())

    def test_feature_explicit_on(self):
        with patch.dict(os.environ, {"FEATURE_P2P_ARBITRAGE": "1"}, clear=True):
            self.assertTrue(feature_enabled())

    def test_alerts_default_on(self):
        # Алерты тоже ON по умолчанию: цель сканера — авто-нотификация.
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(alerts_enabled())

    def test_alerts_explicit_off(self):
        with patch.dict(os.environ, {"FEATURE_P2P_ARBITRAGE_ALERTS": "0"}, clear=True):
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

    def test_default_assets_cover_stables_btc_eth_alts(self):
        # Дефолт расширен на 10 активов: USDT/USDC/FDUSD/DAI (стейблы),
        # BTC/ETH/BNB (крупные), SOL/TRX/LTC (альты). Юзер явно попросил
        # «расширить p2p до всех валютных пар в мире».
        with patch.dict(os.environ, {}, clear=True):
            assets = get_assets()
            # Должны быть стейблы (USDT/USDC), большая крипта (BTC/ETH),
            # ликвидные альты (SOL/LTC) — это baseline coverage.
            for required in ("USDT", "USDC", "BTC", "ETH", "SOL", "LTC"):
                self.assertIn(required, assets, f"{required} missing from defaults")
            self.assertEqual(assets, DEFAULT_P2P_ASSETS)
            self.assertGreaterEqual(len(assets), 7)

    def test_default_fiats_cover_majors_and_cis(self):
        # Фиаты раскрыты до global coverage: CIS (RUB/UAH/KZT/BYN) + LATAM
        # (ARS/VES/COP/...) + ASIA (VND/THB/IDR/...) + MENA (TRY/AED/...) +
        # AFRICA (NGN/KES/...) + EUROPE + MAJORS. Юзер: «расширить p2p до
        # всех валютных пар в мире — кнопка ничего не показывает».
        with patch.dict(os.environ, {}, clear=True):
            fiats = get_fiats()
            # CIS-зонт + США/ЕС — обязательны (baseline для RU/KZ юзеров).
            for required in ("RUB", "USD", "EUR", "KZT", "UAH", "BYN"):
                self.assertIn(required, fiats, f"{required} missing from defaults")
            # Топ high-arb регионы должны быть включены (TRY/ARS/NGN/VND).
            for arb_market in ("TRY", "ARS", "NGN", "VND"):
                self.assertIn(arb_market, fiats, f"{arb_market} missing — high-arb market")
            self.assertEqual(fiats, DEFAULT_P2P_FIATS)
            self.assertGreaterEqual(len(fiats), 30, "global coverage <30 фиатов")

    def test_default_cartesian_product_yields_global_coverage(self):
        # Раньше было 42 пары (7×6). Теперь ~10 × ~55 = ~550 пар для global
        # scheduler-scan'а. Санити: чтобы юзер реально видел арб-окна по
        # миру, нужно ≥ 200 пар покрытия.
        with patch.dict(os.environ, {}, clear=True):
            assets = get_assets()
            fiats = get_fiats()
            self.assertGreaterEqual(len(assets) * len(fiats), 200)

    def test_assets_env_override_still_works(self):
        # Override через P2P_ARBITRAGE_ASSETS по-прежнему сужает список.
        with patch.dict(os.environ, {"P2P_ARBITRAGE_ASSETS": "USDT"}, clear=True):
            self.assertEqual(get_assets(), ("USDT",))

    def test_fiats_env_override_still_works(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_FIATS": "RUB"}, clear=True):
            self.assertEqual(get_fiats(), ("RUB",))

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

    def test_bybit_numeric_ids_map_to_canonical(self):
        self.assertEqual(canonical_payment_method("bybit:14"), "tinkoff")
        self.assertEqual(canonical_payment_method("bybit:18"), "sbp")
        self.assertEqual(canonical_payment_method("bybit:40"), "sber")
        self.assertEqual(canonical_payment_method("bybit:185"), "sber")
        self.assertEqual(canonical_payment_method("bybit:999"), "bybit:999")

    def test_cross_venue_overlap_binance_text_vs_bybit_numeric(self):
        binance_buy = self._ad("BUY", 100.0, methods=("TinkoffNew",), advertiser="binance-mkt")
        bybit_sell = self._ad("SELL", 103.0, methods=("bybit:14",), advertiser="bybit-mkt")
        opportunities = find_p2p_opportunities(
            [binance_buy],
            [bybit_sell],
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


class TestAccountQualitySignals(unittest.TestCase):
    """Tests for new userGrade / vipLevel / accountAgeDays fields (Soft #2)."""

    def test_binance_parses_user_grade_vip_account_age(self):
        # Account registered 365 days ago (in ms)
        register_ms = int((time.time() - 365 * 86400) * 1000)
        row = {
            "adv": {
                "advNo": "id",
                "price": "100",
                "minSingleTransAmount": "100",
                "maxSingleTransAmount": "10000",
                "tradeMethods": [{"identifier": "TinkoffNew"}],
            },
            "advertiser": {
                "nickName": "x",
                "userType": "merchant",
                "userGrade": "5",
                "vipLevel": "2",
                "registrationTime": register_ms,
                "monthFinishRate": 0.99,
                "monthOrderCount": "300",
            },
        }
        ad = parse_binance_ad(row, trade_type="BUY", asset="USDT", fiat="RUB")
        self.assertIsNotNone(ad)
        assert ad is not None
        self.assertEqual(ad.user_grade, 5)
        self.assertEqual(ad.vip_level, 2)
        # Should be very close to 365 days
        self.assertIsNotNone(ad.account_age_days)
        self.assertGreaterEqual(ad.account_age_days or 0, 364)
        self.assertLessEqual(ad.account_age_days or 0, 366)

    def test_binance_missing_quality_fields_returns_none(self):
        row = {
            "adv": {
                "advNo": "id",
                "price": "100",
                "minSingleTransAmount": "100",
                "maxSingleTransAmount": "10000",
                "tradeMethods": [],
            },
            "advertiser": {"nickName": "x", "userType": "merchant"},
        }
        ad = parse_binance_ad(row, trade_type="BUY", asset="USDT", fiat="RUB")
        self.assertIsNotNone(ad)
        assert ad is not None
        self.assertIsNone(ad.user_grade)
        self.assertIsNone(ad.vip_level)
        self.assertIsNone(ad.account_age_days)

    def test_bybit_parses_quality_fields(self):
        register_ms = int((time.time() - 200 * 86400) * 1000)
        row = {
            "id": "x",
            "price": "100",
            "minAmount": "100",
            "maxAmount": "10000",
            "tokenId": "USDT",
            "currencyId": "RUB",
            "payments": ["14"],
            "vipLevel": 3,
            "userGrade": 7,
            "registerTime": register_ms,
            "finishNum": 500,
            "recentExecuteRate": 99.5,
        }
        ad = parse_bybit_ad(row, trade_type="BUY", asset="USDT", fiat="RUB")
        self.assertIsNotNone(ad)
        assert ad is not None
        self.assertEqual(ad.user_grade, 7)
        self.assertEqual(ad.vip_level, 3)
        self.assertGreaterEqual(ad.account_age_days or 0, 199)


class TestRiskLevelQualitySignals(unittest.TestCase):
    """Tests for `_risk_level` warnings driven by new fields."""

    def _ad(self, **overrides) -> P2PAdvert:
        defaults = dict(
            venue="Binance P2P",
            trade_type="BUY",
            asset="USDT",
            fiat="RUB",
            price=100.0,
            min_amount_fiat=1000.0,
            max_amount_fiat=50_000.0,
            payment_methods=("tinkoff",),
            advertiser="x",
            completed_orders=600,
            completion_rate_pct=99.0,
            is_merchant=True,
            payment_window_min=20,
            user_grade=5,
            vip_level=2,
            account_age_days=365,
        )
        defaults.update(overrides)
        return P2PAdvert(**defaults)

    def test_low_account_age_emits_warning(self):
        from p2p_arbitrage import _risk_level

        with patch.dict(os.environ, {"P2P_RISK_MIN_ACCOUNT_AGE_DAYS": "30"}, clear=True):
            buy = self._ad(trade_type="BUY", account_age_days=5)
            sell = self._ad(trade_type="SELL", price=102.0)
            _level, warnings = _risk_level(buy, sell, ("tinkoff",))
            self.assertTrue(any("свежий аккаунт" in w for w in warnings))

    def test_low_user_grade_emits_warning(self):
        from p2p_arbitrage import _risk_level

        with patch.dict(os.environ, {"P2P_RISK_MIN_USER_GRADE": "3"}, clear=True):
            buy = self._ad(trade_type="BUY", user_grade=1)
            sell = self._ad(trade_type="SELL", price=102.0)
            _level, warnings = _risk_level(buy, sell, ("tinkoff",))
            self.assertTrue(any("userGrade" in w for w in warnings))

    def test_low_vip_level_emits_warning(self):
        from p2p_arbitrage import _risk_level

        with patch.dict(os.environ, {"P2P_RISK_MIN_VIP_LEVEL": "2"}, clear=True):
            buy = self._ad(trade_type="BUY", vip_level=0)
            sell = self._ad(trade_type="SELL", price=102.0)
            _level, warnings = _risk_level(buy, sell, ("tinkoff",))
            self.assertTrue(any("VIP" in w for w in warnings))

    def test_quality_checks_disabled_when_env_zero(self):
        from p2p_arbitrage import _risk_level

        env = {
            "P2P_RISK_MIN_ACCOUNT_AGE_DAYS": "0",
            "P2P_RISK_MIN_USER_GRADE": "0",
            "P2P_RISK_MIN_VIP_LEVEL": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            buy = self._ad(trade_type="BUY", account_age_days=1, user_grade=0, vip_level=0)
            sell = self._ad(trade_type="SELL", price=102.0, account_age_days=1, user_grade=0, vip_level=0)
            _level, warnings = _risk_level(buy, sell, ("tinkoff",))
            self.assertFalse(any("свежий" in w or "userGrade" in w or "VIP" in w for w in warnings))


class TestPaymentMethodAliasExtensions(unittest.TestCase):
    """Soft #4 — verify the extra Bybit ID mapping covers common RU rails."""

    def test_extended_aliases_present(self):
        from p2p_arbitrage import PAYMENT_METHOD_ALIASES

        extended = {
            "bybit:160": "vtb",
            "bybit:189": "alfabank",
            "bybit:264": "yoomoney",
            "bybit:381": "psb",
            "bybit:600": "akbars",
        }
        for key, value in extended.items():
            self.assertEqual(PAYMENT_METHOD_ALIASES[key], value, msg=key)

    def test_canonical_payment_method_uses_extension(self):
        self.assertEqual(canonical_payment_method("bybit:189"), "alfabank")
        self.assertEqual(canonical_payment_method("bybit:264"), "yoomoney")

    def test_unknown_bybit_id_passes_through(self):
        # Unknown ID stays prefixed as "bybit:<id>"
        result = canonical_payment_method("bybit:99999")
        self.assertEqual(result, "bybit:99999")


class TestM9CSmartFilters(unittest.TestCase):
    """M9-C smart filters: TIER-1 банки, min объём, freshness.

    Юзер просил «только TIER-1 банки, объём ≥ X, не больше Y минут с
    момента публикации ad'а». Эти фильтры включаются через env
    (`P2P_TIER1_ONLY=1`, `P2P_MIN_EXECUTABLE_FIAT=50000`,
    `P2P_MAX_AD_AGE_MIN=5`) — по умолчанию выключены чтобы не сломать
    legacy-сканер.
    """

    def _ad(
        self,
        trade_type: str,
        price: float,
        *,
        methods: tuple[str, ...] = ("TinkoffNew",),
        fetched_at: float | None = None,
        max_fiat: float = 100_000,
        min_fiat: float = 1_000,
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
            completed_orders=250,
            completion_rate_pct=98.0,
            is_merchant=True,
            fetched_at=time.time() if fetched_at is None else fetched_at,
            payment_window_min=15,
        )

    def test_tier1_only_off_keeps_unknown_banks(self):
        # P2P_TIER1_ONLY не выставлен → unknown bank проходит.
        with patch.dict(os.environ, {}, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, methods=("Akbars",))],
                [self._ad("SELL", 103.0, methods=("Akbars",))],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(len(opportunities), 1)

    def test_tier1_only_on_filters_unknown_banks(self):
        # P2P_TIER1_ONLY=1 → ad с akbars-only отсечён (akbars не TIER-1).
        with patch.dict(os.environ, {"P2P_TIER1_ONLY": "1"}, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, methods=("Akbars",))],
                [self._ad("SELL", 103.0, methods=("Akbars",))],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(opportunities, [])

    def test_tier1_only_keeps_sber_tinkoff(self):
        # TIER-1 ad'ы (sber + tinkoff) проходят даже с P2P_TIER1_ONLY=1.
        with patch.dict(os.environ, {"P2P_TIER1_ONLY": "1"}, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, methods=("TinkoffNew",))],
                [self._ad("SELL", 103.0, methods=("Sberbank",))],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            # Tinkoff и Sber оба TIER-1, но shared_payment_methods будет пусто
            # (нет общего метода) → opportunity всё равно строится из 2 ад'ов.
            # Поведение _payment_intersection: если preferred_pay_types пуст,
            # пустой intersect НЕ блочит, просто warning.
            self.assertEqual(len(opportunities), 1)

    def test_tier1_only_passes_mixed_methods(self):
        # Ad с TIER-1 + non-TIER методами проходит (есть хотя бы один TIER-1).
        with patch.dict(os.environ, {"P2P_TIER1_ONLY": "1"}, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, methods=("TinkoffNew", "Akbars"))],
                [self._ad("SELL", 103.0, methods=("Tinkoff", "RandomBank"))],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(len(opportunities), 1)

    def test_tier1_banks_env_override(self):
        # P2P_TIER1_BANKS=akbars → akbars становится TIER-1, проходит.
        env = {"P2P_TIER1_ONLY": "1", "P2P_TIER1_BANKS": "akbars"}
        with patch.dict(os.environ, env, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, methods=("Akbars",))],
                [self._ad("SELL", 103.0, methods=("Akbars",))],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(len(opportunities), 1)

    def test_min_executable_fiat_filters_small_opportunities(self):
        # max_amount_fiat=10k у обоих → executable=10k. min=50k → отсекается.
        env = {"P2P_MIN_EXECUTABLE_FIAT": "50000"}
        with patch.dict(os.environ, env, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, max_fiat=10_000)],
                [self._ad("SELL", 103.0, max_fiat=10_000)],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(opportunities, [])

    def test_min_executable_fiat_passes_large_opportunities(self):
        # 100k обоих → executable=100k. min=50k → проходит.
        env = {"P2P_MIN_EXECUTABLE_FIAT": "50000"}
        with patch.dict(os.environ, env, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, max_fiat=100_000)],
                [self._ad("SELL", 103.0, max_fiat=100_000)],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(len(opportunities), 1)

    def test_min_executable_fiat_default_off(self):
        # Дефолт 0 → мелкие сделки проходят.
        with patch.dict(os.environ, {}, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, max_fiat=5_000)],
                [self._ad("SELL", 103.0, max_fiat=5_000)],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(len(opportunities), 1)

    def test_max_ad_age_filters_stale_ads(self):
        # fetched_at 10 минут назад, max_age=5 → отсекается.
        stale_ts = time.time() - 600  # 10 минут
        env = {"P2P_MAX_AD_AGE_MIN": "5"}
        with patch.dict(os.environ, env, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, fetched_at=stale_ts)],
                [self._ad("SELL", 103.0, fetched_at=stale_ts)],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(opportunities, [])

    def test_max_ad_age_passes_fresh_ads(self):
        # fetched_at 1 минуту назад, max_age=5 → проходит.
        fresh_ts = time.time() - 60
        env = {"P2P_MAX_AD_AGE_MIN": "5"}
        with patch.dict(os.environ, env, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, fetched_at=fresh_ts)],
                [self._ad("SELL", 103.0, fetched_at=fresh_ts)],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(len(opportunities), 1)

    def test_max_ad_age_default_off(self):
        # Дефолт 0 → старые ad'ы проходят.
        old_ts = time.time() - 86400  # 1 день
        with patch.dict(os.environ, {}, clear=True):
            opportunities = find_p2p_opportunities(
                [self._ad("BUY", 100.0, fetched_at=old_ts)],
                [self._ad("SELL", 103.0, fetched_at=old_ts)],
                min_spread_pct=1.0,
                settlement_buffer_pct=0.35,
            )
            self.assertEqual(len(opportunities), 1)


if __name__ == "__main__":
    unittest.main()
