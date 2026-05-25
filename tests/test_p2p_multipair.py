"""Тесты multi-pair P2P scanner и region-group helpers.

Юзер: «расширить p2p до всех валютных пар в мире — кнопка ничего не
показывает выгоду». Эти тесты проверяют:
  • Region-groups (cis/latam/asia/mena/africa/europe/fiat_majors) — корректный resolve
  • P2P_ARBITRAGE_FIAT_GROUP env-override
  • Button-scope (get_button_scan_*) — sub-scope для быстрой выдачи
  • scan_all_pairs aggregation: opps собираются со всех пар и сортируются по spread desc
  • _has_explicit_pair: button-click vs /p2p USDT RUB
  • _filter_pay_types_for_fiat: RU banks-фильтр игнорируется в TRY/ARS/etc
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "test:test")

from p2p_arbitrage import (  # noqa: E402
    DEFAULT_P2P_FIATS,
    P2P_FIAT_GROUPS,
    P2PAdvert,
    P2POpportunity,
    get_assets,
    get_button_scan_assets,
    get_button_scan_fiats,
    get_fiats,
    get_scan_concurrency,
)

# Handler-импорты живут в aiogram-gated блоке: unit-fast CI job
# гоняется без aiogram → нужно skipUnless, как в test_p2p_arbitrage_handler.py.
try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:  # pragma: no cover
    HAS_AIOGRAM = False


def _make_advert(
    *,
    advertiser: str = "merchant",
    price: float = 100.0,
    trade_type: str = "BUY",
    venue: str = "Binance P2P",
    asset: str = "USDT",
    fiat: str = "RUB",
) -> P2PAdvert:
    return P2PAdvert(
        venue=venue,
        trade_type=trade_type,
        asset=asset,
        fiat=fiat,
        price=price,
        min_amount_fiat=1_000.0,
        max_amount_fiat=1_000_000.0,
        payment_methods=("sbp",),
        advertiser=advertiser,
        completed_orders=500,
        completion_rate_pct=99.0,
        is_merchant=True,
    )


def _make_opportunity(*, asset: str, fiat: str, spread_pct: float) -> P2POpportunity:
    """Прямой P2POpportunity для форматтер-тестов (не вызывает find_p2p_opportunities)."""
    buy_ad = _make_advert(advertiser="m1", price=100.0, trade_type="BUY", asset=asset, fiat=fiat)
    sell_ad = _make_advert(
        advertiser="m2", price=100.0 * (1 + spread_pct / 100), trade_type="SELL", asset=asset, fiat=fiat
    )
    return P2POpportunity(
        asset=asset,
        fiat=fiat,
        buy_ad=buy_ad,
        sell_ad=sell_ad,
        gross_spread_pct=spread_pct,
        buffer_pct=0.5,
        net_spread_pct=spread_pct - 0.5,
        executable_fiat=10_000.0,
        executable_asset=100.0,
        shared_payment_methods=("sbp",),
        risk_level="low",
    )


class TestRegionGroups(unittest.TestCase):
    """Region-groups покрывают все ключевые арб-регионы."""

    def test_cis_group_includes_ru_and_kz_currencies(self):
        cis = P2P_FIAT_GROUPS["cis"]
        for required in ("RUB", "UAH", "KZT", "BYN"):
            self.assertIn(required, cis, f"{required} missing from CIS group")

    def test_latam_includes_high_inflation_markets(self):
        latam = P2P_FIAT_GROUPS["latam"]
        # ARS (Argentina) и VES (Venezuela) — chronic inflation, P2P премия живёт.
        for required in ("ARS", "VES", "BRL", "MXN"):
            self.assertIn(required, latam, f"{required} missing from LATAM")

    def test_mena_includes_turkey(self):
        # TRY — крупнейший P2P-market в мире после CIS из-за инфляции.
        self.assertIn("TRY", P2P_FIAT_GROUPS["mena"])

    def test_africa_includes_nigeria(self):
        # NGN (Naira) — capital-control P2P-премия 5-15%.
        self.assertIn("NGN", P2P_FIAT_GROUPS["africa"])

    def test_asia_includes_capital_controlled_markets(self):
        asia = P2P_FIAT_GROUPS["asia"]
        for required in ("VND", "IDR", "INR", "THB"):
            self.assertIn(required, asia)

    def test_groups_are_disjoint_no_overlap(self):
        # Каждый фиат принадлежит ровно одной региональной группе (sanity).
        seen: dict[str, str] = {}
        for group_name, fiats in P2P_FIAT_GROUPS.items():
            for fiat in fiats:
                self.assertNotIn(
                    fiat, seen,
                    f"{fiat} duplicated: in {seen.get(fiat, '?')} and {group_name}",
                )
                seen[fiat] = group_name


class TestFiatGroupResolve(unittest.TestCase):
    """get_fiats() резолвит env с правильным приоритетом."""

    def test_explicit_fiats_override_wins(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_FIATS": "RUB,USD"}, clear=True):
            self.assertEqual(get_fiats(), ("RUB", "USD"))

    def test_fiat_group_env_picks_single_group(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_FIAT_GROUP": "latam"}, clear=True):
            self.assertEqual(get_fiats(), P2P_FIAT_GROUPS["latam"])

    def test_fiat_group_case_insensitive(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_FIAT_GROUP": "CIS"}, clear=True):
            self.assertEqual(get_fiats(), P2P_FIAT_GROUPS["cis"])

    def test_unknown_group_falls_back_to_default(self):
        with patch.dict(os.environ, {"P2P_ARBITRAGE_FIAT_GROUP": "nope"}, clear=True):
            self.assertEqual(get_fiats(), DEFAULT_P2P_FIATS)

    def test_explicit_fiats_beats_group(self):
        env = {
            "P2P_ARBITRAGE_FIATS": "RUB",
            "P2P_ARBITRAGE_FIAT_GROUP": "latam",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(get_fiats(), ("RUB",))


class TestButtonScope(unittest.TestCase):
    """Button-scope — sub-scope для быстрой выдачи (high-arb регионы only)."""

    def test_button_fiats_default_is_high_arb_regions(self):
        with patch.dict(os.environ, {}, clear=True):
            fiats = get_button_scan_fiats()
            # CIS + LATAM + MENA + AFRICA — high-arb potential. Без EU/MAJORS.
            for required in ("RUB", "TRY", "ARS", "NGN"):
                self.assertIn(required, fiats, f"{required} missing from button scope")
            # USD/EUR — мало арб, исключены из button-default.
            self.assertNotIn("USD", fiats)
            self.assertNotIn("EUR", fiats)

    def test_button_fiats_env_override(self):
        with patch.dict(os.environ, {"P2P_BUTTON_FIATS": "RUB,USD,TRY"}, clear=True):
            self.assertEqual(get_button_scan_fiats(), ("RUB", "USD", "TRY"))

    def test_button_assets_default_is_stables(self):
        # Default — стейблы где P2P премия концентрируется.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_button_scan_assets(), ("USDT", "USDC", "FDUSD"))

    def test_button_assets_env_override(self):
        with patch.dict(os.environ, {"P2P_BUTTON_ASSETS": "USDT,BTC"}, clear=True):
            self.assertEqual(get_button_scan_assets(), ("USDT", "BTC"))


class TestScanConcurrency(unittest.TestCase):
    def test_default_concurrency_is_5(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_scan_concurrency(), 5)

    def test_concurrency_env_override(self):
        with patch.dict(os.environ, {"P2P_SCAN_CONCURRENCY": "10"}, clear=True):
            self.assertEqual(get_scan_concurrency(), 10)

    def test_concurrency_clamped_to_1_20(self):
        # Защита от безумных значений.
        with patch.dict(os.environ, {"P2P_SCAN_CONCURRENCY": "100"}, clear=True):
            self.assertEqual(get_scan_concurrency(), 20)
        with patch.dict(os.environ, {"P2P_SCAN_CONCURRENCY": "0"}, clear=True):
            self.assertEqual(get_scan_concurrency(), 1)

    def test_concurrency_invalid_falls_back(self):
        with patch.dict(os.environ, {"P2P_SCAN_CONCURRENCY": "bogus"}, clear=True):
            self.assertEqual(get_scan_concurrency(), 5)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestHasExplicitPair(unittest.TestCase):
    """Различение button-click vs /p2p USDT RUB."""

    def test_button_text_without_args_is_button_mode(self):
        from refactor.handlers.p2p_arbitrage_handler import _has_explicit_pair

        # Persistent button шлёт текст «🧭 P2P арбитраж» — не начинается с /.
        # Только command-syntax `/p2p ...` считается explicit-pair mode.
        self.assertFalse(_has_explicit_pair("🧭 P2P арбитраж"))
        self.assertFalse(_has_explicit_pair("p2p"))
        self.assertFalse(_has_explicit_pair(""))
        self.assertFalse(_has_explicit_pair("/p2p"))

    def test_explicit_pair_triggers_single_pair_mode(self):
        from refactor.handlers.p2p_arbitrage_handler import _has_explicit_pair

        self.assertTrue(_has_explicit_pair("/p2p USDT"))
        self.assertTrue(_has_explicit_pair("/p2p USDT RUB"))
        self.assertTrue(_has_explicit_pair("/p2p BTC TRY"))
        self.assertTrue(_has_explicit_pair("/p2p FDUSD ARS"))


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestPayTypesFilterForFiat(unittest.TestCase):
    def test_ru_banks_kept_for_rub(self):
        from refactor.handlers.p2p_arbitrage_handler import _filter_pay_types_for_fiat

        self.assertEqual(
            _filter_pay_types_for_fiat(("sber", "tinkoff"), "RUB"),
            ("sber", "tinkoff"),
        )

    def test_ru_banks_dropped_for_try_ars(self):
        from refactor.handlers.p2p_arbitrage_handler import _filter_pay_types_for_fiat

        # В TRY/ARS Sber/Tinkoff не существуют — фильтр оставит 0 ads.
        # Graceful degradation: для не-RUB пар pay_types игнорируются.
        self.assertEqual(_filter_pay_types_for_fiat(("sber", "tinkoff"), "TRY"), ())
        self.assertEqual(_filter_pay_types_for_fiat(("sber",), "ARS"), ())

    def test_empty_pay_types_always_empty(self):
        from refactor.handlers.p2p_arbitrage_handler import _filter_pay_types_for_fiat

        self.assertEqual(_filter_pay_types_for_fiat((), "RUB"), ())
        self.assertEqual(_filter_pay_types_for_fiat((), "TRY"), ())


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestScanAllPairs(unittest.IsolatedAsyncioTestCase):
    """Multi-pair scan агрегирует opps со всех пар и сортирует по spread desc."""

    async def test_aggregates_opportunities_from_all_pairs(self):
        from refactor.handlers import p2p_arbitrage_handler

        # Изолируем агрегацию/сортировку от find_p2p_opportunities фильтров
        # (min_orders, completion_rate, etc) — мокаем оба слоя.
        spread_per_pair = {
            ("USDT", "RUB"): 0.5,
            ("USDT", "TRY"): 5.0,
            ("USDT", "ARS"): 12.0,
            ("USDC", "RUB"): 2.5,
            ("USDC", "TRY"): 1.5,
            ("USDC", "ARS"): 8.0,
        }

        async def fake_fetch(*, asset, fiat, pay_types=(), rows=20):
            # Pair tagged через advertiser-tag для последующей find-mock.
            buy = _make_advert(
                advertiser=f"{asset}|{fiat}", price=100.0, trade_type="BUY",
                asset=asset, fiat=fiat,
            )
            sell = _make_advert(
                advertiser=f"{asset}|{fiat}", price=200.0, trade_type="SELL",
                asset=asset, fiat=fiat,
            )
            return [buy], [sell], (), "Binance P2P"

        def fake_find(buy_ads, sell_ads, **kwargs):
            if not buy_ads or not sell_ads:
                return []
            tag = buy_ads[0].advertiser
            if "|" not in tag:
                return []
            asset, fiat = tag.split("|")
            spread = spread_per_pair.get((asset, fiat), 0.0)
            if spread <= 0:
                return []
            return [_make_opportunity(asset=asset, fiat=fiat, spread_pct=spread)]

        with patch.object(p2p_arbitrage_handler, "fetch_p2p_ads", side_effect=fake_fetch), \
             patch.object(p2p_arbitrage_handler, "find_p2p_opportunities", side_effect=fake_find):
            triples, errors, source = await p2p_arbitrage_handler.scan_all_pairs(
                assets=("USDT", "USDC"),
                fiats=("RUB", "TRY", "ARS"),
                concurrency=2,
                per_pair_timeout_sec=2.0,
            )

        # Все 6 пар дали opp. Топ — USDT/ARS (12%). Сортировка desc.
        self.assertEqual(len(triples), 6)
        top_asset, top_fiat, _ = triples[0]
        self.assertEqual((top_asset, top_fiat), ("USDT", "ARS"))
        spreads = [t[2].net_spread_pct for t in triples]
        self.assertEqual(spreads, sorted(spreads, reverse=True))

    async def test_per_pair_timeout_collected_as_error(self):
        from refactor.handlers import p2p_arbitrage_handler

        async def hanging_fetch(*, asset, fiat, pay_types=(), rows=20):
            await asyncio.sleep(10)
            return [], [], (), ""

        with patch.object(p2p_arbitrage_handler, "fetch_p2p_ads", side_effect=hanging_fetch):
            triples, errors, _ = await p2p_arbitrage_handler.scan_all_pairs(
                assets=("USDT",),
                fiats=("RUB",),
                concurrency=1,
                per_pair_timeout_sec=0.1,
            )

        self.assertEqual(triples, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("timeout", errors[0])

    async def test_fetch_exception_collected_as_error(self):
        from refactor.handlers import p2p_arbitrage_handler

        async def boom_fetch(*, asset, fiat, pay_types=(), rows=20):
            raise RuntimeError("network die")

        with patch.object(p2p_arbitrage_handler, "fetch_p2p_ads", side_effect=boom_fetch):
            triples, errors, _ = await p2p_arbitrage_handler.scan_all_pairs(
                assets=("USDT",),
                fiats=("RUB", "TRY"),
                concurrency=2,
                per_pair_timeout_sec=1.0,
            )

        self.assertEqual(triples, [])
        self.assertEqual(len(errors), 2)
        for err in errors:
            self.assertIn("fetch failed", err)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestFormatMultipairReport(unittest.TestCase):
    """Report formatting: топ-N + сводка + плейсхолдер при 0 results."""

    def test_empty_result_shows_helpful_message(self):
        from refactor.handlers.p2p_arbitrage_handler import _format_multipair_report

        text = _format_multipair_report(
            [],
            pair_count_scanned=60,
            errors=[],
            source="Binance + Bybit",
        )
        self.assertIn("60 пар", text)
        self.assertIn("Binance + Bybit", text)

    def test_top_n_opportunities_rendered_with_pair_labels(self):
        from refactor.handlers.p2p_arbitrage_handler import _format_multipair_report

        triples = [
            ("USDT", "ARS", _make_opportunity(asset="USDT", fiat="ARS", spread_pct=12.0)),
            ("USDT", "TRY", _make_opportunity(asset="USDT", fiat="TRY", spread_pct=5.0)),
            ("USDC", "NGN", _make_opportunity(asset="USDC", fiat="NGN", spread_pct=3.0)),
        ]
        text = _format_multipair_report(
            triples,
            pair_count_scanned=60,
            errors=[],
            source="Binance",
            top_n=10,
        )
        # Все пары должны быть упомянуты.
        self.assertIn("USDT/ARS", text)
        self.assertIn("USDT/TRY", text)
        self.assertIn("USDC/NGN", text)
        # Cnt в заголовке.
        self.assertIn("топ-3", text)

    def test_errors_shown_as_skipped_count(self):
        from refactor.handlers.p2p_arbitrage_handler import _format_multipair_report

        triples = [
            ("USDT", "TRY", _make_opportunity(asset="USDT", fiat="TRY", spread_pct=5.0)),
        ]
        text = _format_multipair_report(
            triples,
            pair_count_scanned=60,
            errors=["USDT/RUB: timeout 8s", "USDT/ARS: 429"],
            source="Binance",
        )
        self.assertIn("2 пар пропущено", text)


if __name__ == "__main__":
    unittest.main()
