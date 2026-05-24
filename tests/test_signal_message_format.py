"""Тесты для `_fmt_signal_message` (рендер кнопки «Лучшая сделка»).

Закрывают форматы обеих веток:
  • setup есть → блоки «Почему эта сделка / Вход-Stop-Target / Риски».
  • setup нет → «Сидим» + топ-3 кандидата, БЕЗ старой фразы про
    «~90% дней / слив комиссий».
"""

from __future__ import annotations

import os
import re
import sys
import unittest

# main.py пытается импортить aiogram. Если его нет в окружении (unit-fast
# job) — пропускаем весь модуль.
try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

# Заглушки секретов чтобы main.py смог импортнуться без BOT_TOKEN.
os.environ.setdefault("BOT_TOKEN", "test:test")

from core.signal_scorer import AssetScore, ScoreBreakdown, SignalSetup  # noqa: E402


def _make_setup(score: int = 75) -> SignalSetup:
    """Sample SignalSetup, как его вернул бы rank_signals для SOL LONG."""
    return SignalSetup(
        asset="SOL",
        direction="LONG",
        entry=90.58,
        stop=86.23,
        target=99.28,
        stop_pct=-4.80,
        target_pct=9.60,
        rr_ratio=2.0,
        sigma_1d_pct=3.20,
        size_usd=30.75,
        score=score,
        reasons=[
            "UPTREND ✓ (цена выше MA50 +5.3%, MA200 +13.2%)",
            "TRENDING ✓ (H=0.61, score=0.72)",
            "VRT H0 отвергнут ✓ (VR=1.18, есть структура)",
            "Markov UP ✓ (P(next UP)=55%)",
            "raw score=0.72 → 11 pts",
        ],
    )


def _make_score(asset: str, total: int, direction: str = "LONG") -> AssetScore:
    """Lightweight AssetScore с нужным total — для проверки runner-up логики."""
    bd = ScoreBreakdown()
    # Trend-alignment даёт 30 если direction != NONE, остальное добиваем
    # raw_tradeable'ом (он clamp'ится в [0, 15] внутри ScoreBreakdown.total
    # через clamp(0..100), так что переполнение ОК — отдельно тестируется
    # в test_signal_scorer.py).
    bd.trend_alignment = 30 if direction != "NONE" else 0
    bd.raw_tradeable = max(0, total - bd.trend_alignment)
    reasons = (
        ["UPTREND ✓ (цена выше MA50)"] if direction != "NONE" else ["SIDEWAYS"]
    )
    return AssetScore(asset=asset, direction=direction, breakdown=bd, reasons=reasons)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestFmtSignalMessageSetupFound(unittest.TestCase):
    """Когда top != None — проверяем что появились новые блоки."""

    @classmethod
    def setUpClass(cls):
        # Импорт main лениво, чтобы не падать в unit-fast (нет aiogram).
        from main import _fmt_signal_message  # noqa: PLC0415

        cls._fmt = staticmethod(_fmt_signal_message)

    def _render(self, **kwargs) -> str:
        top = kwargs.get("top", _make_setup())
        scored = kwargs.get(
            "scored",
            [
                _make_score("SOL", 75),
                _make_score("ETH", 60),
                _make_score("BTC", 45),
            ],
        )
        return self._fmt(
            {
                "top": top,
                "scored": scored,
                "capital": 123.0,
                "min_score": 60,
            }
        )

    def test_header_present(self):
        msg = self._render()
        self.assertIn("АВТО-СИГНАЛ", msg)
        self.assertIn("SOL", msg)
        self.assertIn("LONG", msg)

    def test_why_this_trade_block(self):
        """Должен быть блок «Почему эта сделка» с отрывом от #2."""
        msg = self._render()
        self.assertIn("Почему эта сделка", msg)
        # Отрыв от #2 (ETH 60/100): 75 - 60 = 15 pts.
        self.assertIn("Отрыв от #2", msg)
        self.assertIn("ETH 60/100", msg)
        self.assertIn("+15 pts", msg)

    def test_rr_breakeven_winrate(self):
        """R/R = 2x → breakeven winrate = 100/3 ≈ 33%."""
        msg = self._render()
        self.assertIn("R/R = 2.0x", msg)
        self.assertIn("winrate ≥ 33%", msg)

    def test_entry_stop_target_block(self):
        msg = self._render()
        self.assertIn("Вход / Stop / Target", msg)
        self.assertIn("Entry:", msg)
        self.assertIn("$90.58", msg)
        self.assertIn("Stop:", msg)
        self.assertIn("$86.23", msg)
        self.assertIn("Target:", msg)
        self.assertIn("$99.28", msg)
        self.assertIn("если хит, выходим", msg)
        self.assertIn("фиксируем профит", msg)

    def test_risks_block_with_usd_amounts(self):
        msg = self._render()
        self.assertIn("Риски этой сделки", msg)
        # SL loss = 30.75 * 4.80 / 100 = $1.476 → отображается как $1.48
        self.assertIn("$1.48", msg)
        # TP gain = 30.75 * 9.60 / 100 = $2.952 → $2.95
        self.assertIn("$2.95", msg)
        # % от капитала
        self.assertIn("% от капитала", msg)
        # σ̂ запас от шума
        self.assertIn("σ запаса", msg)

    def test_no_legacy_90_percent_phrase(self):
        """Старая фраза про «~90% дней» не должна появляться даже если top есть."""
        msg = self._render()
        self.assertNotIn("90% дней", msg)
        self.assertNotIn("слив комиссий", msg)

    def test_no_runner_up_when_only_one_scored(self):
        msg = self._render(scored=[_make_score("SOL", 75)])
        self.assertIn("лучший среди 1 сканированных", msg)
        self.assertNotIn("Отрыв от #2", msg)

    def test_weak_marker_surfaced_in_risks(self):
        """Если в reasons есть «0 pts» — выносим в риски как слабое место."""
        top = _make_setup()
        top.reasons = list(top.reasons) + ["VRT не отвергает H0 (ряд похож на random walk)"]
        msg = self._render(top=top)
        self.assertIn("Слабое место", msg)
        self.assertIn("VRT не отвергает H0", msg)

    def test_disclaimer_still_present(self):
        msg = self._render()
        self.assertIn("suggestion, не приказ", msg)
        self.assertIn("Bybit", msg)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestFmtSignalMessageNoSetup(unittest.TestCase):
    """Когда top == None И preview_top == None — старая «лишняя инфа» удалена."""

    @classmethod
    def setUpClass(cls):
        from main import _fmt_signal_message  # noqa: PLC0415

        cls._fmt = staticmethod(_fmt_signal_message)

    def _render(self, scored=None) -> str:
        if scored is None:
            scored = [
                _make_score("VIX", 71, direction="LONG"),
                _make_score("GOLD", 53, direction="SHORT"),
                _make_score("ETH", 46, direction="SHORT"),
            ]
        return self._fmt(
            {
                "top": None,
                "preview_top": None,
                "scored": scored,
                "capital": 123.0,
                "min_score": 60,
            }
        )

    def test_sit_out_header(self):
        msg = self._render()
        self.assertIn("чистого setup нет", msg)
        self.assertIn("Сидим", msg)

    def test_top_3_still_shown(self):
        msg = self._render()
        self.assertIn("VIX", msg)
        self.assertIn("GOLD", msg)
        self.assertIn("ETH", msg)
        self.assertIn("71/100", msg)

    def test_no_90_percent_phrase(self):
        """Главная цель PR-4 — убрать «Это нормально — наша модель велит сидеть»."""
        msg = self._render()
        self.assertNotIn("Это нормально", msg)
        self.assertNotIn("90% дней", msg)
        self.assertNotIn("слив комиссий", msg)

    def test_markets_hint_present(self):
        msg = self._render()
        self.assertIn("/markets", msg)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestFmtSignalMessagePreview(unittest.TestCase):
    """Когда top is None НО preview_top != None — показываем preview-блок.

    Это новый код-путь (PR-5): пользователь жмёт «Лучшая сделка», score
    ниже порога — но мы всё равно показываем уровни SL/TP/«почему», чтобы
    кнопка не висела бесполезно.
    """

    @classmethod
    def setUpClass(cls):
        from main import _fmt_signal_message  # noqa: PLC0415

        cls._fmt = staticmethod(_fmt_signal_message)

    def _make_preview(self, score: int = 48, asset: str = "XRP") -> SignalSetup:
        return SignalSetup(
            asset=asset,
            direction="SHORT",
            entry=1.40,
            stop=1.47,
            target=1.26,
            stop_pct=5.00,
            target_pct=-10.00,
            rr_ratio=2.0,
            sigma_1d_pct=3.33,
            size_usd=30.75,
            score=score,
            reasons=[
                "DOWNTREND ✓ (vs MA50 -0.2%, MA200 -18.6%)",
                "MEAN_REVERTING — counter-trend, weak (5 pts)",
                "VRT не отвергает H0 (ряд похож на random walk)",
                "Markov FLAT (нейтрально 5 pts)",
                "raw score=0.31 → 5 pts",
            ],
        )

    def _render(self, **kwargs) -> str:
        preview = kwargs.get("preview_top", self._make_preview())
        scored = kwargs.get(
            "scored",
            [
                _make_score("VIX", 71, direction="LONG"),       # non-tradable выше
                _make_score("GOLD", 53, direction="SHORT"),     # non-tradable
                _make_score("XRP", 48, direction="SHORT"),      # tradable preview
                _make_score("BTC", 30, direction="NONE"),
            ],
        )
        return self._fmt(
            {
                "top": None,
                "preview_top": preview,
                "scored": scored,
                "capital": 123.0,
                "min_score": 60,
            }
        )

    def test_preview_header_present(self):
        """Должен быть жёлтый заголовок «ЛУЧШИЙ КАНДИДАТ», не «ТОП SETUP»."""
        msg = self._render()
        self.assertIn("ЛУЧШИЙ КАНДИДАТ", msg)
        self.assertIn("XRP", msg)
        self.assertIn("SHORT", msg)
        self.assertNotIn("ТОП SETUP", msg)

    def test_preview_marks_score_below_threshold(self):
        msg = self._render()
        # «(score 48/60 — ниже порога ...)»
        self.assertIn("48/60", msg)
        self.assertIn("ниже порога", msg)

    def test_preview_shows_sl_tp_levels(self):
        """Главное требование пользователя — SL/TP в «сидим»-режиме тоже видны."""
        msg = self._render()
        self.assertIn("Вход / Stop / Target", msg)
        self.assertIn("Entry:", msg)
        self.assertIn("$1.4", msg)            # entry / stop / target — округлены
        self.assertIn("Stop:", msg)
        self.assertIn("Target:", msg)

    def test_preview_shows_risks_in_usd(self):
        msg = self._render()
        self.assertIn("Риски этой сделки", msg)
        # SL loss = 30.75 * 5.00 / 100 = $1.5375 → $1.54
        self.assertIn("$1.54", msg)
        # TP gain = 30.75 * 10.00 / 100 = $3.075 → $3.07 or $3.08 (rounding)
        self.assertTrue("$3.07" in msg or "$3.08" in msg, msg=f"missing TP gain ≈$3.08: {msg}")

    def test_preview_warns_to_reduce_size(self):
        """В preview-режиме явное предупреждение «уменьши size»."""
        msg = self._render()
        self.assertIn("уменьши size", msg)

    def test_preview_explains_higher_non_tradable(self):
        """VIX 71 выше XRP 48 — но VIX не на споте Bybit, явно объясняем."""
        msg = self._render()
        self.assertIn("Выше по score", msg)
        self.assertIn("VIX", msg)
        self.assertIn("71/100", msg)
        self.assertIn("не торгуется", msg)

    def test_preview_keeps_disclaimer(self):
        msg = self._render()
        self.assertIn("suggestion, не приказ", msg)

    def test_preview_no_legacy_90_percent(self):
        msg = self._render()
        self.assertNotIn("90% дней", msg)
        self.assertNotIn("слив комиссий", msg)

    def test_preview_escapes_underscores_in_reasons(self):
        """Reason 'MEAN_REVERTING …' должен прийти как 'MEAN\\_REVERTING …'.

        Сырой `_` в Telegram MD V1 трактуется как italic — пара `_` съедала
        всё между ними. Юзер видел `MEANREVERTING` вместо `MEAN_REVERTING`.
        """
        msg = self._render()
        # Reason содержит MEAN_REVERTING — должно быть экранировано.
        self.assertIn(r"MEAN\_REVERTING", msg)
        # Старая (сломанная) форма не должна появляться нигде в выводе.
        self.assertNotIn(
            "MEAN_REVERTING — counter-trend",
            msg,
            msg=(
                "raw `MEAN_REVERTING` без бэкслэша ломает Telegram MD parsing "
                "(пара `_` съест всё в italic)"
            ),
        )

    def test_preview_no_raw_tradable_assets_constant(self):
        """`TRADABLE_ASSETS = ...` ломал MD V1 двумя `_`. Должно быть в backticks."""
        msg = self._render()
        self.assertNotIn("TRADABLE_ASSETS =", msg)
        # Новая форма — список tradable-активов в backtick-code-span'е.
        # Расширили список до 15 крипто-активов (BTC..SUI), внутри code-span'а
        # MD V1 не парсит разметку — слеши безопасны.
        self.assertIn("`BTC/ETH/SOL", msg)
        # SUI — последний из расширенной корзины, должен быть в списке.
        self.assertIn("SUI`", msg)


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestMdEscapeUnderscores(unittest.TestCase):
    """`_md_escape_underscores` — экранирование `_` для Telegram MD V1."""

    @classmethod
    def setUpClass(cls):
        from main import _md_escape_underscores  # noqa: PLC0415

        cls._escape = staticmethod(_md_escape_underscores)

    def test_replaces_underscores_with_backslash_underscore(self):
        self.assertEqual(self._escape("MEAN_REVERTING"), r"MEAN\_REVERTING")
        self.assertEqual(self._escape("RANDOM_WALK"), r"RANDOM\_WALK")

    def test_no_underscore_no_change(self):
        self.assertEqual(self._escape("UPTREND ✓ (vs MA50 +5.3%)"), "UPTREND ✓ (vs MA50 +5.3%)")

    def test_multiple_underscores(self):
        self.assertEqual(
            self._escape("FOO_BAR_BAZ"),
            r"FOO\_BAR\_BAZ",
        )

    def test_empty_string(self):
        self.assertEqual(self._escape(""), "")


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestSignalGlossaryText(unittest.TestCase):
    """`_signal_glossary_text` — текст под кнопкой «📖 Что значат эти слова?».

    Regression-тесты против `TelegramBadRequest: can't parse entities`,
    который ловили на проде когда в тексте оставались некэскейпленные
    markdown-метасимволы.  Telegram parse_mode=Markdown (V1) тривиально
    падает на нечётном количестве `_` / `*` / backtick — здесь
    проверяем, что текст структурно валиден.
    """

    @classmethod
    def setUpClass(cls):
        from main import _signal_glossary_text  # noqa: PLC0415

        cls._text = _signal_glossary_text()

    def test_within_telegram_size_limit(self):
        # Telegram hard limit — 4096 chars per message.  Глоссарий
        # должен помещаться без splitting'а.
        self.assertLess(len(self._text), 4096)

    def test_non_empty(self):
        self.assertGreater(len(self._text), 100)

    def test_stars_are_balanced(self):
        # `*bold*` — парное кол-во `*`, иначе MD V1 не закроет entity.
        self.assertEqual(self._text.count("*") % 2, 0)

    def test_backticks_are_balanced(self):
        # `code` — парное кол-во `` ` ``.
        self.assertEqual(self._text.count("`") % 2, 0)

    def test_no_unescaped_underscores_outside_code_spans(self):
        """Главный regression-тест.

        Прод падал на байте 4360 из-за неэкранированного `_` в
        `stop_pct`.  Парсер открыл italic, не нашёл закрывающий `_`
        → `Can't find end of the entity`.  Тут проверяем что вне
        backtick-code-span'ов ВСЕ `_` экранированы.
        """
        # Вырезаем содержимое всех `code` блоков (внутри MD V1 ничего
        # не парсит, там `_` безопасны).
        no_code = re.sub(r"`[^`]*`", "", self._text)
        # Считаем «голые» `_` — не предварённые `\`.
        bare = [
            i
            for i, ch in enumerate(no_code)
            if ch == "_" and (i == 0 or no_code[i - 1] != "\\")
        ]
        self.assertEqual(
            bare,
            [],
            f"Found {len(bare)} unescaped '_' outside code spans at chars "
            f"{bare[:5]} — Telegram MD V1 will fail to parse.",
        )

    def test_glossary_keyword_terms_present(self):
        # Минимальный smoke: основные термины упомянуты.
        for kw in ("LONG", "SHORT", "Entry", "Stop", "Target", "R/R"):
            self.assertIn(kw, self._text, f"Missing keyword: {kw}")


if __name__ == "__main__":
    unittest.main()
