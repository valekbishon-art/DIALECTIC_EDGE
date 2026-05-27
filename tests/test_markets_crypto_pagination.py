"""Tests for the /markets crypto-tab pagination.

15 crypto assets in one Telegram message is unreadable (and risks the
4096-character limit). This module covers:

  - `format_prices_for_agents(crypto_assets=...)` — filters to only the
    requested tickers, preserving CRYPTO_KEYS ordering.
  - `format_prices_section(crypto_assets=...)` — same, but for the crypto
    section only.
  - `build_markets_section_message(section="crypto", page=N)` —
    paginates 15 assets into 3 pages of 5, clamps out-of-range pages,
    populates `bundle["pagination"]`.
  - `_markets_section_keyboard(current="crypto", page=, pages_total=)` —
    renders the pagination row with prev/index/next callbacks when
    multiple pages exist.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from signals import MARKETS_CRYPTO_PAGE_SIZE, build_markets_section_message
from web_search import (
    CRYPTO_KEYS,
    format_prices_for_agents,
    format_prices_section,
)


def _crypto_fixture(price=100.0, ma50=95.0, ma200=110.0):
    return {
        "price": price,
        "change_24h": 1.2,
        "change_7d": 5.0,
        "change_30d": 8.0,
        "ma50": ma50,
        "ma200": ma200,
        "above_ma50": price > ma50,
        "above_ma200": price > ma200,
        "trend": "MIXED",
        "trend_emoji": "⚪",
        "source": "Binance",
    }


def _full_15_prices() -> dict:
    """Synthetic prices dict covering all 15 EXTENDED_CRYPTO_SYMBOLS."""
    return {k: _crypto_fixture(price=100.0 + i) for i, k in enumerate(CRYPTO_KEYS)}


class TestFormatPricesForAgentsCryptoFilter(unittest.TestCase):
    def test_unfiltered_renders_all_keys_present(self):
        prices = _full_15_prices()
        text = format_prices_for_agents(prices, for_user=True)
        # Все 15 ключей должны встретиться (`(BTC)`, `(ETH)`, ...).
        for k in CRYPTO_KEYS:
            self.assertIn(f"({k})", text, f"missing {k} in unfiltered output")

    def test_filter_to_subset_renders_only_those(self):
        prices = _full_15_prices()
        text = format_prices_for_agents(
            prices, for_user=True, crypto_assets=("BTC", "ETH"),
        )
        self.assertIn("(BTC)", text)
        self.assertIn("(ETH)", text)
        self.assertNotIn("(SOL)", text)
        self.assertNotIn("(DOGE)", text)

    def test_filter_preserves_canonical_order(self):
        prices = _full_15_prices()
        # Передаём в обратном порядке — должны выйти в порядке CRYPTO_KEYS.
        text = format_prices_for_agents(
            prices, for_user=True, crypto_assets=("ETH", "BTC"),
        )
        idx_btc = text.find("(BTC)")
        idx_eth = text.find("(ETH)")
        self.assertGreater(idx_btc, 0)
        self.assertGreater(idx_eth, 0)
        self.assertLess(idx_btc, idx_eth, "BTC должен идти раньше ETH")

    def test_filter_unknown_ticker_ignored(self):
        prices = _full_15_prices()
        text = format_prices_for_agents(
            prices, for_user=True, crypto_assets=("BTC", "FAKE_COIN"),
        )
        self.assertIn("(BTC)", text)
        self.assertNotIn("FAKE_COIN", text)

    def test_empty_filter_drops_all_assets(self):
        prices = _full_15_prices()
        text = format_prices_for_agents(
            prices, for_user=True, crypto_assets=(),
        )
        # Заголовок [КРИПТОРЫНОК] должен остаться.
        self.assertIn("[КРИПТОРЫНОК]", text)
        for k in CRYPTO_KEYS:
            self.assertNotIn(f"({k})", text)


class TestFormatPricesSectionCryptoFilter(unittest.TestCase):
    def test_section_crypto_with_subset(self):
        prices = _full_15_prices()
        text = format_prices_section(
            prices, section="crypto", crypto_assets=("BTC", "ETH", "SOL"),
        )
        self.assertIn("(BTC)", text)
        self.assertIn("(ETH)", text)
        self.assertIn("(SOL)", text)
        self.assertNotIn("(ADA)", text)
        self.assertNotIn("(DOGE)", text)


class TestBuildMarketsSectionMessagePagination(unittest.IsolatedAsyncioTestCase):
    async def _build(self, section: str, page: int = 0):
        # Мокаем тяжёлые async-fetcher'ы — нас интересует только
        # пагинация / bundle["pagination"], а не реальные данные.
        with patch(
            "signals.fetch_markets_bundle",
            return_value=asyncio.Future(),
        ), patch(
            "web_search.fetch_realtime_prices",
            return_value=asyncio.Future(),
        ):
            from signals import fetch_markets_bundle  # type: ignore
            from web_search import fetch_realtime_prices  # type: ignore

            fetch_markets_bundle.return_value.set_result({})
            fetch_realtime_prices.return_value.set_result(_full_15_prices())

            return await build_markets_section_message(
                github_repo="fake/repo", section=section, page=page,
            )

    async def test_pagination_for_crypto_three_pages(self):
        _, bundle = await self._build("crypto", page=0)
        pag = bundle["pagination"]
        self.assertEqual(pag["section"], "crypto")
        self.assertEqual(pag["page"], 0)
        self.assertEqual(pag["page_size"], MARKETS_CRYPTO_PAGE_SIZE)
        expected_pages = (len(CRYPTO_KEYS) + MARKETS_CRYPTO_PAGE_SIZE - 1) // MARKETS_CRYPTO_PAGE_SIZE
        self.assertEqual(pag["pages"], expected_pages)
        self.assertEqual(len(pag["assets"]), MARKETS_CRYPTO_PAGE_SIZE)
        # Первая страница начинается с BTC.
        self.assertEqual(pag["assets"][0], CRYPTO_KEYS[0])

    async def test_pagination_page_one_shifts_window(self):
        _, bundle = await self._build("crypto", page=1)
        pag = bundle["pagination"]
        self.assertEqual(pag["page"], 1)
        self.assertEqual(
            pag["assets"][0],
            CRYPTO_KEYS[MARKETS_CRYPTO_PAGE_SIZE],
        )

    async def test_pagination_page_beyond_range_is_clamped(self):
        # Юзер дёрнул page=99 — должны clamp'нуть к последней (pages-1).
        _, bundle = await self._build("crypto", page=99)
        pag = bundle["pagination"]
        expected_pages = (len(CRYPTO_KEYS) + MARKETS_CRYPTO_PAGE_SIZE - 1) // MARKETS_CRYPTO_PAGE_SIZE
        self.assertEqual(pag["page"], expected_pages - 1)

    async def test_pagination_negative_page_is_clamped_to_zero(self):
        _, bundle = await self._build("crypto", page=-5)
        self.assertEqual(bundle["pagination"]["page"], 0)

    async def test_non_crypto_section_has_empty_pagination(self):
        _, bundle = await self._build("macro")
        pag = bundle["pagination"]
        self.assertEqual(pag["section"], "macro")
        self.assertEqual(pag["pages"], 1)
        self.assertEqual(pag["page"], 0)
        self.assertEqual(pag["assets"], ())


try:
    import aiogram  # noqa: F401
    _HAS_AIOGRAM = True
except ImportError:
    _HAS_AIOGRAM = False


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestMarketsSectionKeyboardPagination(unittest.TestCase):
    def _make_kb(self, current: str, page: int, pages_total: int):
        # main.py — god-object с side-effects на импорте. Поэтому импортим
        # лениво и достаём конкретно функцию-builder клавиатуры.
        from main import _markets_section_keyboard

        return _markets_section_keyboard(
            is_enabled=False,
            current=current,
            user_id=123,
            page=page,
            pages_total=pages_total,
        )

    def _all_callbacks(self, kb) -> list[str]:
        return [btn.callback_data for row in kb.inline_keyboard for btn in row]

    def test_crypto_pagination_row_present_when_multiple_pages(self):
        kb = self._make_kb("crypto", page=0, pages_total=3)
        first_row_texts = [btn.text for btn in kb.inline_keyboard[0]]
        self.assertEqual(len(first_row_texts), 3)
        self.assertIn("Назад", first_row_texts[0])
        self.assertEqual(first_row_texts[1], "1 / 3")
        self.assertIn("Вперёд", first_row_texts[2])

    def test_crypto_pagination_row_absent_when_single_page(self):
        kb = self._make_kb("crypto", page=0, pages_total=1)
        # Первая строка должна быть НЕ пагинацией, а первой строкой
        # выбора секций (2 кнопки вместо 3).
        self.assertEqual(len(kb.inline_keyboard[0]), 2)

    def test_pagination_wraps_around_last_to_first(self):
        kb = self._make_kb("crypto", page=2, pages_total=3)
        callbacks = self._all_callbacks(kb)
        # На последней странице «Вперёд» → 0 (wrap), «Назад» → 1.
        self.assertIn("markets:section:crypto:0", callbacks)
        self.assertIn("markets:section:crypto:1", callbacks)
        self.assertEqual(kb.inline_keyboard[0][1].text, "3 / 3")

    def test_pagination_indicator_uses_noop_callback(self):
        kb = self._make_kb("crypto", page=1, pages_total=3)
        indicator = kb.inline_keyboard[0][1]
        self.assertEqual(indicator.callback_data, "noop")

    def test_non_crypto_section_no_pagination_row(self):
        kb = self._make_kb("macro", page=0, pages_total=1)
        first_row = kb.inline_keyboard[0]
        self.assertEqual(len(first_row), 2)
        for btn in first_row:
            self.assertNotIn("/", (btn.text or ""))

    def test_refresh_button_preserves_current_page_for_crypto(self):
        kb = self._make_kb("crypto", page=2, pages_total=3)
        callbacks = self._all_callbacks(kb)
        # «🔄 Обновить» должен указывать на ту же страницу 2.
        self.assertIn("markets:section:crypto:2", callbacks)


if __name__ == "__main__":
    unittest.main()
