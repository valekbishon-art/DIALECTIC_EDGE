# -*- coding: utf-8 -*-
"""Regression-tests для SL/TP блока в `/markets`.

Покрывают:
  1. `_sl_tp_lines` рендерит две строки (LONG + SHORT) c корректными
     уровнями `price × (1 ± k·σ̂)` и tick-rounding.
  2. Graceful-degradation: нет σ̂ / нет цены / нулевая σ̂ → пусто.
  3. `format_prices_for_agents` пропускает блок через интеграционно —
     строка реально видна в выводе `/markets`.
  4. Tick-size для XRP — 0.0001 (Bybit Spot, 4 знака), для BTC — 0.01.
  5. R/R = 2:1 (TP_dist = 2·SL_dist в абсолюте).
  6. Помощь `/help markets` ссылается на новую SL/TP-строку.

Эти тесты — фундамент: они описывают «что должен видеть юзер в
`/markets` при наличии σ̂». Любая регрессия формата или формулы их
сломает до проверки на пользователе.

Замечание: справку из `main.py` мы достаём через AST (как
`tests/test_help_markets_markdown.py`), не `import main` — CI не ставит
aiogram / matplotlib / FinBERT.
"""

from __future__ import annotations

import ast
import os
import unittest

from web_search import _fmt_pct, _sl_tp_lines, format_prices_for_agents


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _extract_markets_help_text() -> str:
    """Достаёт значение `return` из `_markets_help_text()` без import main.py.
    Идентичный приём — см. `tests/test_help_markets_markdown.py`."""
    src_path = os.path.join(REPO_ROOT, "main.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_markets_help_text":
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    return ast.literal_eval(stmt.value)
    raise AssertionError("_markets_help_text() not found in main.py")


class TestFmtPct(unittest.TestCase):
    """`_fmt_pct(value)` — печатает `+5.0%` / `−2.5%` (U+2212 минус)."""

    def test_positive_uses_plus(self):
        self.assertEqual(_fmt_pct(4.95), "+5.0%")

    def test_negative_uses_unicode_minus(self):
        # Юникодный минус (U+2212), а не ASCII '-' — Markdown будет
        # ровно выровнен с `−2.18%` из строки изменений.
        self.assertEqual(_fmt_pct(-2.475), "−2.5%")
        self.assertNotIn("-", _fmt_pct(-2.475))


class TestSlTpLines(unittest.TestCase):
    """`_sl_tp_lines(p, asset)` — LONG/SHORT уровни от σ̂."""

    def test_empty_when_no_sigma(self):
        self.assertEqual(_sl_tp_lines({"price": 100.0}, "BTC"), [])

    def test_empty_when_zero_sigma(self):
        self.assertEqual(
            _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": 0.0}, "BTC"),
            [],
        )

    def test_empty_when_negative_sigma(self):
        # Защита от мусорных данных — отрицательная σ̂ не имеет смысла.
        self.assertEqual(
            _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": -1.0}, "BTC"),
            [],
        )

    def test_empty_when_no_price(self):
        self.assertEqual(_sl_tp_lines({"vol_sigma_1d_pct": 1.5}, "BTC"), [])

    def test_empty_when_zero_price(self):
        self.assertEqual(
            _sl_tp_lines({"price": 0.0, "vol_sigma_1d_pct": 1.5}, "BTC"),
            [],
        )

    def test_btc_renders_two_lines(self):
        # BTC: price=79,118, σ̂=1.65% → SL=1.5×=2.475%, TP=3×=4.95%
        # LONG SL  = 79118 × (1-0.02475) = 77,159.83 → tick 0.01 → 77,159.83
        # LONG TP  = 79118 × (1+0.0495)  = 83,034.34 → 83,034.34
        # SHORT TP = 79118 × (1-0.0495)  = 75,201.66
        # SHORT SL = 79118 × (1+0.02475) = 81,076.17
        lines = _sl_tp_lines({"price": 79118.0, "vol_sigma_1d_pct": 1.65}, "BTC")
        self.assertEqual(len(lines), 2)
        long_line, short_line = lines

        self.assertIn("LONG", long_line)
        self.assertIn("TP $83,034", long_line)
        self.assertIn("SL $77,160", long_line)
        self.assertIn("(+5.0%)", long_line)
        # Юникодный минус (U+2212), не ASCII '-'
        self.assertIn("(−2.5%)", long_line)
        self.assertIn("R/R 2:1", long_line)

        self.assertIn("SHORT", short_line)
        self.assertIn("TP $75,202", short_line)
        self.assertIn("SL $81,076", short_line)
        self.assertIn("(−5.0%)", short_line)
        self.assertIn("(+2.5%)", short_line)
        self.assertIn("R/R 2:1", short_line)

    def test_xrp_tick_rounding_to_4_decimals(self):
        # XRP на Bybit Spot — 4 знака после точки (tick=0.0001).
        # Раньше был 0.1 и это ломало превью: при σ̂≥3% все три
        # уровня (entry/SL/TP) схлопывались в одно число → R/R=0.0x.
        # Ныне при price=1.46, σ̂=2.5% → SL=3.75%, TP=7.5%:
        # LONG TP = 1.46 × 1.075 = 1.5695   (tick=0.0001 → 1.5695)
        # LONG SL = 1.46 × 0.9625 = 1.4052   (tick=0.0001 → 1.4052)
        # SHORT TP = 1.46 × 0.925 = 1.3505   (tick=0.0001 → 1.3505)
        # SHORT SL = 1.46 × 1.0375 = 1.5148  (tick=0.0001 → 1.5148)
        # `_fmt_money_compact` сжимает цены 1 ≤ v < 100 до 2 знаков — это
        # формат показа, не round (в ордер уходит полный 4-десятичный tick).
        lines = _sl_tp_lines({"price": 1.46, "vol_sigma_1d_pct": 2.5}, "XRP")
        self.assertEqual(len(lines), 2)
        long_line, short_line = lines
        # 1.5695 → `1.57`, 1.4052 → `1.41` (2-десятичный показ).
        self.assertIn("$1.57", long_line)
        self.assertIn("$1.41", long_line)
        # 1.3505 → `1.35`, 1.5148 → `1.51`.
        self.assertIn("$1.35", short_line)
        self.assertIn("$1.51", short_line)
        # Самое главное: SL ≠ entry (раньше 1.46 → 1.5 → выбоина).
        # И в выводе не должно быть равных уровней рядом.
        self.assertNotIn("$1.4 ", long_line)  # 1-decimal artefact
        self.assertNotIn("$1.5 ", long_line)

    def test_rr_ratio_is_two(self):
        # Геометрия: TP_dist = 2 × SL_dist для R/R = 2:1.
        # Проверяем через парсинг % из строки.
        lines = _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": 2.0}, "BTC")
        # σ̂=2% → SL=3%, TP=6%
        self.assertIn("(+6.0%)", lines[0])
        self.assertIn("(−3.0%)", lines[0])
        self.assertIn("(−6.0%)", lines[1])
        self.assertIn("(+3.0%)", lines[1])

    def test_unknown_asset_falls_back_to_default_tick(self):
        # Актив не в `ASSET_TICK_SIZE` (например, DOGE) — fallback 0.0001.
        lines = _sl_tp_lines(
            {"price": 0.15, "vol_sigma_1d_pct": 3.0},
            "DOGE",
        )
        # Не падаем, рендерим 2 строки. Точное значение не важно — важно
        # что не падаем и формат сохраняется.
        self.assertEqual(len(lines), 2)
        self.assertIn("LONG", lines[0])
        self.assertIn("SHORT", lines[1])

    def test_indent_is_four_spaces_by_default(self):
        lines = _sl_tp_lines({"price": 100.0, "vol_sigma_1d_pct": 2.0}, "BTC")
        for line in lines:
            self.assertTrue(line.startswith("    🎯"))


class TestFormatPricesRendersSlTp(unittest.TestCase):
    """Интеграционно: `/markets` показывает SL/TP блок."""

    def _btc(self) -> dict:
        # Минимальный валидный prices-dict для BTC с σ̂.
        return {
            "BTC": {
                "price": 79118.0,
                "change_24h": -2.18,
                "change_7d": -1.9,
                "change_30d": 5.3,
                "source": "Binance",
                "trend": "SIDEWAYS",
                "trend_emoji": "↔️",
                "ma50": 75222.0,
                "ma200": 81788.0,
                "above_ma50": True,
                "above_ma200": False,
                "complexity_hint": "MEAN_REVERTING",
                "hurst": 0.45,
                "tradeable_score": 0.49,
                "vol_sigma_1d_pct": 1.65,
                "vol_sigma_annual_pct": 32.0,
            }
        }

    def test_crypto_block_includes_long_and_short_sl_tp(self):
        out = format_prices_for_agents(self._btc())
        self.assertIn("🎯 LONG", out)
        self.assertIn("🎯 SHORT", out)
        self.assertIn("R/R 2:1", out)
        # Точные числа от прод-кейса (79,118 × 1.65%).
        self.assertIn("TP $83,034", out)
        self.assertIn("SL $77,160", out)

    def test_user_facing_is_spot_only_no_short(self):
        # for_user=True (карточка /markets) — только спот/лонг, без SHORT.
        # Шорт/деривативы вне проекта; в пользовательском выводе их быть не должно.
        out = format_prices_for_agents(self._btc(), for_user=True)
        # Спот-уровень входа есть, число то же.
        self.assertIn("🎯 Спот", out)
        self.assertIn("TP $83,034", out)
        self.assertIn("R/R 2:1", out)
        # А SHORT — нигде: ни SL/TP-строкой, ни в MA-триггерах, ни в quant.
        self.assertNotIn("SHORT", out)
        self.assertNotIn("🎯 LONG", out)
        # MA-триггеры переформулированы по споту.
        self.assertIn("→ покупка спот", out)
        self.assertIn("→ выход в стейбл", out)

    def test_sl_tp_follows_ma_triggers(self):
        # Визуальный порядок: цена → MA-триггеры → SL/TP → тренд → quant.
        # Это специально — юзер сначала видит «при пробое», потом «если
        # входим сейчас», потом контекст.
        out = format_prices_for_agents(self._btc())
        idx_triggers = out.index("▲ выше")
        idx_sltp = out.index("🎯 LONG")
        idx_trend = out.index("ТРЕНД:")
        self.assertLess(idx_triggers, idx_sltp)
        self.assertLess(idx_sltp, idx_trend)

    def test_sl_tp_skipped_when_no_sigma(self):
        # Короткий ряд — нет σ̂ → SL/TP блок просто пропадает (не падаем).
        prices = self._btc()
        prices["BTC"].pop("vol_sigma_1d_pct")
        prices["BTC"].pop("vol_sigma_annual_pct")
        out = format_prices_for_agents(prices)
        # Базовая строка с ценой и MA-триггеры — есть.
        self.assertIn("Bitcoin (BTC)", out)
        self.assertIn("▲ выше", out)
        # А блока SL/TP — нет.
        self.assertNotIn("🎯 LONG", out)
        self.assertNotIn("🎯 SHORT", out)

    def test_works_for_all_crypto_symbols(self):
        # Все 5 крипты-символов получают SL/TP при наличии σ̂.
        prices = {}
        for sym in ("BTC", "ETH", "SOL", "BNB", "XRP"):
            prices[sym] = {
                "price": 100.0,
                "change_24h": 0.0,
                "source": "Binance",
                "trend": "SIDEWAYS",
                "trend_emoji": "↔️",
                "vol_sigma_1d_pct": 2.0,
            }
        out = format_prices_for_agents(prices)
        # 5 LONG + 5 SHORT = 10 строк с 🎯
        self.assertEqual(out.count("🎯 LONG"), 5)
        self.assertEqual(out.count("🎯 SHORT"), 5)


class TestHelpDocumentsSlTp(unittest.TestCase):
    """`/help markets` теперь упоминает SL/TP блок — без шпаргалки юзер
    смотрит на новые цифры и не понимает что это."""

    @classmethod
    def setUpClass(cls):
        cls.text = _extract_markets_help_text()

    def test_help_mentions_sl_tp_section(self):
        # Заголовок секции по новой нумерации.
        self.assertIn("SL / TP от текущей цены", self.text)
        # Формула, чтобы юзер мог считать сам.
        self.assertIn("1.5", self.text)
        self.assertIn("σ̂", self.text)
        self.assertIn("R/R", self.text)
        # Telegram-лимит 4096 символов — справка должна укладываться.
        self.assertLessEqual(len(self.text), 4096)


if __name__ == "__main__":
    unittest.main()
