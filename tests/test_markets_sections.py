"""Тесты для нового section-selector /markets:
  • `format_prices_minimal()` — per-секционный минималистичный рендер
  • `build_markets_section_message()` — корректная dispatch'ация по секциям
  • `_markets_section_keyboard()` — клавиатура с пометкой активной секции

Юзер просил «меньше жмодци, минимализм»: проверяем, что в крипто-блоке
нет цветных эмодзи (🟢/🔴), нет «ТРЕНД:» строки и нет квант-блока, но
сохранены ▲/▼ MA-триггеры (это main signal для входа).
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from web_search import (
    _commod_lines_minimal,
    _crypto_lines_minimal,
    _indices_lines_minimal,
    _macro_lines_minimal,
    format_prices_minimal,
)


def _btc_fixture(price=78114.0, ma50=75437.0, ma200=81623.0, change=1.20):
    """Realistic BTC dict в формате `fetch_realtime_prices()`."""
    return {
        "price": price,
        "change_24h": change,
        "ma50": ma50,
        "ma200": ma200,
        "above_ma50": price > ma50,
        "above_ma200": price > ma200,
        "trend": "BEARISH",
        "trend_emoji": "🔴",
        "source": "Binance",
    }


def _eth_fixture():
    return {
        "price": 3124.0,
        "change_24h": -0.80,
        "ma50": 2980.0,
        "ma200": 3250.0,
        "above_ma50": True,
        "above_ma200": False,
        "trend": "MIXED",
        "trend_emoji": "⚪️",
        "source": "Binance",
    }


def _spx_fixture():
    return {
        "price": 5872.42,
        "change_24h": 0.40,
        "ma50": 5756.0,
        "ma200": 6012.0,
        "above_ma50": True,
        "above_ma200": False,
        "source": "Yahoo",
    }


def _oil_fixture():
    return {
        "price": 68.40,
        "change_24h": -0.30,
        "ma50": 65.20,
        "ma200": 72.10,
        "above_ma50": True,
        "above_ma200": False,
        "source": "Yahoo",
    }


class TestCryptoMinimal(unittest.TestCase):
    """`₿ BTC — $78,114` со стрелками ▲/▼ к MA-триггерам."""

    def test_btc_line_contains_icon_and_price(self):
        prices = {"BTC": _btc_fixture()}
        lines = _crypto_lines_minimal(prices)
        self.assertGreaterEqual(len(lines), 1)
        head = lines[0]
        self.assertIn("₿", head)
        self.assertIn("BTC", head)
        self.assertIn("$78,114", head)
        self.assertIn("24ч", head)

    def test_btc_has_ma_triggers(self):
        prices = {"BTC": _btc_fixture()}
        body = "\n".join(_crypto_lines_minimal(prices))
        self.assertIn("▲", body)
        self.assertIn("▼", body)
        self.assertIn("LONG", body)
        self.assertIn("SHORT", body)
        self.assertIn("MA200", body)
        self.assertIn("MA50", body)
        self.assertIn("$81,623", body)
        self.assertIn("$75,437", body)

    def test_minimal_has_no_color_emoji(self):
        """В минимальном рендере не должно быть 🟢/🔴 (это «шум»)."""
        prices = {"BTC": _btc_fixture(), "ETH": _eth_fixture()}
        body = "\n".join(_crypto_lines_minimal(prices))
        self.assertNotIn("🟢", body)
        self.assertNotIn("🔴", body)

    def test_minimal_has_no_trend_or_quant_blocks(self):
        """Тренд-строка и quant-блок убраны — фокус на цену + MA-триггер."""
        prices = {"BTC": _btc_fixture()}
        body = "\n".join(_crypto_lines_minimal(prices))
        self.assertNotIn("ТРЕНД", body)
        self.assertNotIn("Quant", body)
        self.assertNotIn("Объём", body)

    def test_minus_is_unicode(self):
        """`−0.80%` — юникод-минус, не ASCII `-`."""
        prices = {"ETH": _eth_fixture()}
        head = _crypto_lines_minimal(prices)[0]
        self.assertIn("−0.80%", head)

    def test_eth_uses_xi_icon(self):
        prices = {"ETH": _eth_fixture()}
        head = _crypto_lines_minimal(prices)[0]
        self.assertTrue(head.startswith("Ξ "))


class TestMacroMinimal(unittest.TestCase):
    """ФРС/CPI/F&G — короткие 3 строки."""

    def test_fed_cpi_fng_lines(self):
        prices = {
            "MACRO": {
                "fed_rate": 4.50,
                "cpi_raw": 308.42,
                "fng": {"val": 62, "status": "Greed", "change": 3},
            }
        }
        body = "\n".join(_macro_lines_minimal(prices))
        self.assertIn("ФРС", body)
        self.assertIn("4.5%", body)
        self.assertIn("CPI", body)
        self.assertIn("F&G", body)
        self.assertIn("62/100", body)
        self.assertIn("Greed", body)
        self.assertIn("↗", body)
        self.assertIn("+3", body)

    def test_missing_macro_returns_empty(self):
        self.assertEqual(_macro_lines_minimal({}), [])


class TestIndicesMinimal(unittest.TestCase):
    def test_spx_has_no_dollar_prefix(self):
        """Индексы — без `$`-префикса перед триггером (это не доллары)."""
        prices = {"SPX": _spx_fixture()}
        body = "\n".join(_indices_lines_minimal(prices))
        self.assertIn("S&P 500", body)
        self.assertIn("5,872.42", body)
        # Триггеры — без $-префикса
        self.assertIn("▲ выше 6,012", body)
        self.assertIn("▼ ниже 5,756", body)
        self.assertNotIn("$6,012", body)


class TestCommodMinimal(unittest.TestCase):
    def test_oil_unit_and_dollar_prefix(self):
        prices = {"OIL_WTI": _oil_fixture()}
        body = "\n".join(_commod_lines_minimal(prices))
        self.assertIn("Нефть WTI", body)
        self.assertIn("$/барр", body)
        # Триггеры — с $-префиксом
        self.assertIn("$72", body)
        self.assertIn("$65", body)


class TestFormatPricesMinimal(unittest.TestCase):
    def test_empty_returns_placeholder(self):
        self.assertEqual(
            format_prices_minimal({}),
            "Рыночные данные временно недоступны.",
        )

    def test_section_crypto_only(self):
        prices = {
            "BTC": _btc_fixture(),
            "MACRO": {"fed_rate": 4.5, "cpi_raw": 308, "fng": {"val": 50, "status": "n", "change": 0}},
            "SPX": _spx_fixture(),
        }
        out = format_prices_minimal(prices, section="crypto")
        self.assertIn("₿", out)
        self.assertNotIn("ФРС", out)
        self.assertNotIn("S&P 500", out)

    def test_section_macro_only(self):
        prices = {
            "BTC": _btc_fixture(),
            "MACRO": {"fed_rate": 4.5, "cpi_raw": 308, "fng": {"val": 50, "status": "n", "change": 0}},
        }
        out = format_prices_minimal(prices, section="macro")
        self.assertNotIn("₿", out)
        self.assertIn("ФРС", out)

    def test_section_all_has_titles(self):
        prices = {
            "BTC": _btc_fixture(),
            "MACRO": {"fed_rate": 4.5, "cpi_raw": 308, "fng": {"val": 50, "status": "n", "change": 0}},
            "SPX": _spx_fixture(),
            "OIL_WTI": _oil_fixture(),
        }
        out = format_prices_minimal(prices, section="all")
        self.assertIn("Крипта", out)
        self.assertIn("Макро", out)
        self.assertIn("Индексы", out)
        self.assertIn("Сырьё", out)

    def test_unknown_section_returns_empty_string(self):
        # Никакая ветка не сматчилась — пустая строка.
        out = format_prices_minimal({"BTC": _btc_fixture()}, section="unknown_xx")
        self.assertEqual(out, "")


class TestBuildMarketsSectionMessage(unittest.TestCase):
    """Интеграционные тесты для `build_markets_section_message()` с мок-fetcher'ами."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _mock_fetchers(self, *, prices=None, signals_msg="🔔 Сигналы пусты", section_extras=None):
        prices = prices or {"BTC": _btc_fixture()}
        bundle = {
            "binance_data": [],
            "signals": [],
            "verdict": None,
            "signals_message": signals_msg,
        }

        async def fake_prices():
            return prices

        async def fake_bundle(github_repo):
            return bundle

        return fake_prices, fake_bundle

    def test_summary_has_crypto_no_signals(self):
        """Summary = крипта с S/R, БЕЗ сигналов (сигналы в отдельной вкладке)."""
        fake_prices, fake_bundle = self._mock_fetchers(signals_msg="🔔 SIGNAL TEST")
        from signals import build_markets_section_message

        with patch("web_search.fetch_realtime_prices", new=fake_prices), \
             patch("signals.fetch_markets_bundle", new=fake_bundle):
            msgs, bundle = self._run(build_markets_section_message("o/r", section="summary"))

        self.assertEqual(bundle["section"], "summary")
        text = "\n\n".join(msgs)
        # Summary = крипта (рич-формат). Сигналы убраны — живут во вкладке
        # «📡 Сигналы» (section="signals"). Дублировать их перегружало
        # текст и ломало layout менюшки.
        self.assertIn("КРИПТОРЫНОК", text)
        self.assertIn("Bitcoin (BTC)", text)
        self.assertNotIn("SIGNAL TEST", text)

    def test_crypto_section_has_only_crypto(self):
        fake_prices, fake_bundle = self._mock_fetchers(signals_msg="DO_NOT_SHOW")
        from signals import build_markets_section_message

        with patch("web_search.fetch_realtime_prices", new=fake_prices), \
             patch("signals.fetch_markets_bundle", new=fake_bundle):
            msgs, _ = self._run(build_markets_section_message("o/r", section="crypto"))

        text = "\n\n".join(msgs)
        # Рич-формат: `[КРИПТОРЫНОК]` + полная строка по активу.
        self.assertIn("КРИПТОРЫНОК", text)
        self.assertIn("Bitcoin (BTC)", text)
        self.assertIn("$78,114", text)
        # ▲/▼ MA-триггеры остаются — это главный сигнал входа.
        self.assertIn("▲", text)
        self.assertIn("▼", text)
        # Чужие секции не появляются.
        self.assertNotIn("МАКРОЭКОНОМИКА", text)
        self.assertNotIn("ФОНДОВЫЕ ИНДЕКСЫ", text)
        self.assertNotIn("DO_NOT_SHOW", text)

    def test_crypto_section_preserves_rich_detail(self):
        """Юзер просил вернуть детальную инфу (24ч, MA-триггеры, ТРЕНД, …) —
        проверяем что fixture-данные действительно проявляются в выводе."""
        from signals import build_markets_section_message

        prices = {"BTC": _btc_fixture()}
        # `_btc_fixture()` имеет trend="BEARISH" / trend_emoji="🔴" → ТРЕНД-строка появится.
        fake_prices, fake_bundle = self._mock_fetchers(prices=prices)
        with patch("web_search.fetch_realtime_prices", new=fake_prices), \
             patch("signals.fetch_markets_bundle", new=fake_bundle):
            msgs, _ = self._run(build_markets_section_message("o/r", section="crypto"))
        text = "\n\n".join(msgs)
        # ТРЕНД-блок + MA-триггеры — это «детальная инфа» которую юзер хотел вернуть.
        self.assertIn("ТРЕНД", text)
        self.assertIn("MA50", text)
        self.assertIn("MA200", text)

    def test_signals_section_skips_prices_fetch(self):
        fake_prices, fake_bundle = self._mock_fetchers(signals_msg="🔔 ONLY_SIGNALS")
        from signals import build_markets_section_message

        with patch("web_search.fetch_realtime_prices", new=fake_prices), \
             patch("signals.fetch_markets_bundle", new=fake_bundle):
            msgs, _ = self._run(build_markets_section_message("o/r", section="signals"))

        text = "\n\n".join(msgs)
        self.assertIn("ONLY_SIGNALS", text)

    def test_invalid_section_falls_back_to_summary(self):
        fake_prices, fake_bundle = self._mock_fetchers()
        from signals import build_markets_section_message

        with patch("web_search.fetch_realtime_prices", new=fake_prices), \
             patch("signals.fetch_markets_bundle", new=fake_bundle):
            _, bundle = self._run(build_markets_section_message("o/r", section="bogus_section"))

        self.assertEqual(bundle["section"], "summary")

    def test_macro_section_has_only_macro(self):
        prices = {
            "BTC": _btc_fixture(),
            "MACRO": {"fed_rate": 4.5, "cpi_raw": 308, "fng": {"val": 50, "status": "n", "change": 0}},
        }
        fake_prices, fake_bundle = self._mock_fetchers(prices=prices)
        from signals import build_markets_section_message

        with patch("web_search.fetch_realtime_prices", new=fake_prices), \
             patch("signals.fetch_markets_bundle", new=fake_bundle):
            msgs, _ = self._run(build_markets_section_message("o/r", section="macro"))

        text = "\n\n".join(msgs)
        self.assertIn("ФРС", text)
        self.assertNotIn("₿", text)


try:
    import aiogram  # noqa: F401
    _HAS_AIOGRAM = True
except ImportError:
    _HAS_AIOGRAM = False


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestMarketsSectionKeyboard(unittest.TestCase):
    """Активная секция помечена точкой («• Крипта»). Это удобный hint
    для юзера: видно где сейчас, не надо щёлкать туда-сюда."""

    def test_keyboard_has_8_section_buttons(self):
        import main

        kb = main._markets_section_keyboard(is_enabled=False, current="summary")
        section_callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("markets:section:")
        ]
        # 8 секций + 1 «Обновить» = 9 кнопок секции
        self.assertEqual(len(section_callbacks), 9)
        # все 8 ключей должны присутствовать
        expected = {
            "markets:section:crypto",
            "markets:section:macro",
            "markets:section:indices",
            "markets:section:commod",
            "markets:section:cot",
            "markets:section:etf",
            "markets:section:signals",
            "markets:section:all",
        }
        self.assertTrue(expected.issubset(set(section_callbacks)))

    def test_current_section_has_bullet_marker(self):
        import main

        kb = main._markets_section_keyboard(is_enabled=False, current="crypto")
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        # «Крипта» должна быть помечена «• »
        self.assertTrue(any("• " in lab and "Крипта" in lab for lab in labels))
        # Другие секции — без точки
        macro_label = next(lab for lab in labels if "Макро" in lab)
        self.assertFalse(macro_label.startswith("• "))

    def test_bell_toggle_reflects_state(self):
        import main

        kb_off = main._markets_section_keyboard(is_enabled=False, current="summary")
        kb_on = main._markets_section_keyboard(is_enabled=True, current="summary")
        off_labels = [btn.text for row in kb_off.inline_keyboard for btn in row]
        on_labels = [btn.text for row in kb_on.inline_keyboard for btn in row]
        self.assertIn("🔔", off_labels)
        self.assertIn("🔕", on_labels)


@unittest.skipUnless(_HAS_AIOGRAM, "aiogram не установлен (CI: minimal deps)")
class TestMarketsPagination(unittest.TestCase):
    """Pagination: /markets теперь — одно сообщение которое юзер листает
    кнопками «◀ Назад / Вперёд ▶» вместо рассыпания на 3 портянки.
    """

    def test_no_pagination_row_when_single_page(self):
        import main

        kb = main._markets_section_keyboard(
            is_enabled=False, current="crypto", total_pages=1, current_page=0
        )
        page_callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("markets:page:")
        ]
        self.assertEqual(page_callbacks, [])

    def test_pagination_row_appears_for_multi_page(self):
        import main

        kb = main._markets_section_keyboard(
            is_enabled=False, current="crypto", total_pages=3, current_page=0
        )
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("Назад" in lab for lab in labels))
        self.assertTrue(any("Вперёд" in lab for lab in labels))
        self.assertIn("1/3", labels)

    def test_pagination_callbacks_point_to_correct_section(self):
        import main

        kb = main._markets_section_keyboard(
            is_enabled=False, current="macro", total_pages=3, current_page=1
        )
        page_callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("markets:page:")
        ]
        # «Назад» -> idx 0, «Вперёд» -> idx 2 (cur=1, total=3)
        self.assertIn("markets:page:macro:0", page_callbacks)
        self.assertIn("markets:page:macro:2", page_callbacks)

    def test_pagination_wraps_book_style(self):
        """На последней странице «Вперёд ▶» возвращает на стр 0 (циклично)."""
        import main

        kb = main._markets_section_keyboard(
            is_enabled=False, current="crypto", total_pages=3, current_page=2
        )
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("markets:page:")
        ]
        # cur=2, total=3 → prev=1, next=0 (wrap)
        self.assertIn("markets:page:crypto:1", callbacks)
        self.assertIn("markets:page:crypto:0", callbacks)

    def test_pagination_wraps_on_first_page(self):
        """На стр 0 «◀ Назад» возвращает на последнюю (циклично)."""
        import main

        kb = main._markets_section_keyboard(
            is_enabled=False, current="crypto", total_pages=4, current_page=0
        )
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("markets:page:")
        ]
        # cur=0 → prev=3 (wrap), next=1
        self.assertIn("markets:page:crypto:3", callbacks)
        self.assertIn("markets:page:crypto:1", callbacks)

    def test_pagination_indicator_shows_current_position(self):
        import main

        kb = main._markets_section_keyboard(
            is_enabled=False, current="crypto", total_pages=3, current_page=1
        )
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        # Индикатор «2/3» (cur=1 -> отображается как 1+1=2)
        self.assertIn("2/3", labels)

    def test_page_out_of_bounds_is_clamped(self):
        """page < 0 или > total-1 — должно показываться нормально."""
        import main

        kb_neg = main._markets_section_keyboard(
            is_enabled=False, current="crypto", total_pages=3, current_page=-1
        )
        kb_over = main._markets_section_keyboard(
            is_enabled=False, current="crypto", total_pages=3, current_page=99
        )
        # Не падает, индикатор внутри диапазона
        labels_neg = [btn.text for row in kb_neg.inline_keyboard for btn in row]
        labels_over = [btn.text for row in kb_over.inline_keyboard for btn in row]
        self.assertIn("1/3", labels_neg)
        self.assertIn("3/3", labels_over)


if __name__ == "__main__":
    unittest.main()
