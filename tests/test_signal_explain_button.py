"""Тесты для кнопки «📖 Что значат эти слова?» под `/signal`.

Покрывают:
  • _signal_explain_keyboard — структура InlineKeyboardMarkup, callback_data
    с user_id, единственная кнопка.
  • _signal_glossary_text — содержит все термины из АВТО-СИГНАЛ сообщения
    (порог, R/R, SL/TP, σ̂, score breakdown components, LONG/SHORT и т.д.),
    влезает в Telegram-лимит 4096 символов.
  • handle_signal_explain_callback — UID-guard (чужой клик отвечает alert'ом,
    свой клик зовёт send_message с глоссарием).
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# main.py пытается импортить aiogram. Если его нет в окружении (unit-fast
# job) — пропускаем весь модуль.
try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

# Заглушки секретов чтобы main.py смог импортнуться без BOT_TOKEN.
os.environ.setdefault("BOT_TOKEN", "test:test")


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestSignalExplainKeyboard(unittest.TestCase):
    """Структура клавиатуры — единственная кнопка-глоссарий с UID в callback_data.

    Снайпинг-кнопка удалена (снайпинг листингов = спекуляция, исключён из проекта).
    """

    @classmethod
    def setUpClass(cls):
        from main import _signal_explain_keyboard  # noqa: PLC0415

        cls._kb = staticmethod(_signal_explain_keyboard)

    def test_returns_inline_keyboard_markup(self):
        from aiogram.types import InlineKeyboardMarkup  # noqa: PLC0415

        kb = self._kb(42)
        self.assertIsInstance(kb, InlineKeyboardMarkup)

    def test_single_row_one_button(self):
        kb = self._kb(42)
        self.assertEqual(len(kb.inline_keyboard), 1)
        self.assertEqual(len(kb.inline_keyboard[0]), 1)

    def test_button_text_human_readable(self):
        kb = self._kb(42)
        btn = kb.inline_keyboard[0][0]
        # Должна быть кнопка вида «📖 Что значат эти слова?» — book-emoji
        # и явный вопрос, чтобы юзер понял что это объяснение терминов.
        self.assertIn("📖", btn.text)
        self.assertTrue(
            any(word in btn.text.lower() for word in ("значат", "объясн", "поясн")),
            f"button text must mention explanation: {btn.text!r}",
        )

    def test_callback_data_carries_user_id(self):
        kb = self._kb(42)
        btn = kb.inline_keyboard[0][0]
        self.assertEqual(btn.callback_data, "sigexplain:42")

    def test_no_sniping_button(self):
        # Снайпинг-кнопка удалена — в клавиатуре её быть не должно.
        kb = self._kb(42, 250)
        for row in kb.inline_keyboard:
            for btn in row:
                self.assertNotIn("Снайпинг", btn.text)
                self.assertFalse((btn.callback_data or "").startswith("sniping:"))

    def test_callback_data_uid_isolation(self):
        # Разные UID → разные callback_data → бот сможет отделить чужой клик.
        kb_a = self._kb(100)
        kb_b = self._kb(200)
        self.assertNotEqual(
            kb_a.inline_keyboard[0][0].callback_data,
            kb_b.inline_keyboard[0][0].callback_data,
        )


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestSignalGlossaryText(unittest.TestCase):
    """Глоссарий должен покрывать каждый термин из АВТО-СИГНАЛ сообщения."""

    @classmethod
    def setUpClass(cls):
        from main import _signal_glossary_text  # noqa: PLC0415

        cls._txt = staticmethod(_signal_glossary_text)

    def test_returns_non_empty_string(self):
        self.assertIsInstance(self._txt(), str)
        self.assertGreater(len(self._txt()), 500)  # минимальный осмысленный объём

    def test_fits_telegram_message_limit(self):
        # Telegram cap = 4096 чарактеров на одно сообщение.
        self.assertLess(len(self._txt()), 4096)

    def test_explains_header_terms(self):
        text = self._txt()
        # «АВТО-СИГНАЛ» / «детерминированный scoring» / score 0-100.
        self.assertIn("АВТО-СИГНАЛ", text)
        self.assertTrue(
            any(s in text for s in ("формул", "математика", "scoring")),
            "должен объяснять что это математика, не LLM",
        )

    def test_explains_threshold(self):
        text = self._txt()
        # «Порог 60/100» — это core-понятие, нельзя пропустить.
        self.assertIn("60", text)
        self.assertIn("Порог", text)

    def test_explains_long_short(self):
        text = self._txt()
        self.assertIn("LONG", text)
        self.assertIn("SHORT", text)
        # Должно быть пояснение направлений в человеческом виде.
        self.assertTrue(
            any(s in text.lower() for s in ("растёт", "вырастет", "выше"))
            and any(s in text.lower() for s in ("упадёт", "падени", "ниже")),
            "должно объяснять что LONG=вверх, SHORT=вниз",
        )

    def test_explains_rr_ratio(self):
        text = self._txt()
        # «R/R 2:1» = (TP - entry) / (entry - SL). Объясняем «риск-к-прибыли».
        self.assertIn("R/R", text)
        self.assertTrue(
            any(s in text for s in ("Reward", "риск", "прибыль", "потер")),
            "должно объяснять что такое risk/reward",
        )

    def test_explains_entry_stop_target(self):
        text = self._txt()
        self.assertIn("Entry", text)
        self.assertIn("Stop", text)
        self.assertIn("Target", text)

    def test_explains_sigma(self):
        text = self._txt()
        # σ̂ = стандартное отклонение. Должно быть упомянуто и объяснено.
        self.assertIn("σ̂", text)
        self.assertTrue(
            any(s in text.lower() for s in ("стандартн", "отклонен", "колеблет"))
        )

    def test_explains_score_components(self):
        text = self._txt()
        # Score = trend(30) + complexity(20) + VRT(15) + Markov(15) + tradeable(20).
        # Юзер должен понимать что складывается в финальный score.
        self.assertIn("Trend", text)
        self.assertTrue(
            any(s in text for s in ("Complexity", "TRENDING", "MEAN")),
            "должен упоминать complexity",
        )
        self.assertIn("VRT", text)
        self.assertIn("Markov", text)

    def test_explains_not_an_order(self):
        text = self._txt()
        # Главное правило: «это suggestion, не приказ» — должно быть.
        self.assertTrue(
            any(s in text.lower() for s in ("suggestion", "не приказ", "подтверд"))
        )

    def test_underscores_safe_for_md_v1(self):
        """`_` в Telegram MD V1 трактуется как italic.

        Два валидных способа сделать `_` безопасным:
          1) Экранировать как `\\_` (parser игнорит markdown).
          2) Завернуть в `` `code` `` (внутри code-span'а MD V1
             ничего не парсит).

        Тут проверяем, что вне code-span'ов НЕТ голых `_`.  Это
        regression-guard против `TelegramBadRequest: can't parse
        entities` (баг словили на проде на байте 4360 — `stop_pct`
        стоял без бэктиков и без эскейпа).
        """
        import re  # noqa: PLC0415

        text = self._txt()
        # Вырезаем содержимое всех code-span'ов — внутри них `_`
        # безопасны и проверять их не нужно.
        no_code = re.sub(r"`[^`]*`", "", text)
        bare = [
            i
            for i, ch in enumerate(no_code)
            if ch == "_" and (i == 0 or no_code[i - 1] != "\\")
        ]
        self.assertEqual(
            bare,
            [],
            f"Found {len(bare)} bare '_' outside code spans at chars "
            f"{bare[:5]} — Telegram MD V1 will fail to parse.",
        )


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestSignalExplainCallback(unittest.TestCase):
    """UID-guard + send_message с глоссарием."""

    @classmethod
    def setUpClass(cls):
        from main import handle_signal_explain_callback  # noqa: PLC0415

        cls._handler = staticmethod(handle_signal_explain_callback)

    def _make_callback(self, *, data: str, from_user_id: int, chat_id: int = 999):
        cb = MagicMock()
        cb.data = data
        cb.answer = AsyncMock()
        cb.from_user = MagicMock()
        cb.from_user.id = from_user_id
        cb.message = MagicMock()
        cb.message.chat = MagicMock()
        cb.message.chat.id = chat_id
        return cb

    def test_uid_mismatch_shows_alert(self):
        cb = self._make_callback(data="sigexplain:42", from_user_id=99)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            cb.answer.assert_awaited_with(
                "Кнопка не с твоего аккаунта", show_alert=True
            )
            bot_mock.send_message.assert_not_awaited()

    def test_uid_match_sends_glossary(self):
        cb = self._make_callback(data="sigexplain:42", from_user_id=42)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            cb.answer.assert_awaited()
            bot_mock.send_message.assert_awaited_once()
            args, kwargs = bot_mock.send_message.call_args
            # chat_id-first arg + parse_mode=Markdown.
            self.assertEqual(args[0], 999)
            self.assertEqual(kwargs.get("parse_mode"), "Markdown")
            # Сам глоссарий должен содержать ключевые термины.
            body = args[1]
            self.assertIn("σ̂", body)
            self.assertIn("R/R", body)

    def test_malformed_callback_data_no_crash(self):
        cb = self._make_callback(data="sigexplain:notanint", from_user_id=42)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            # Должен ответить (закрыть spinner) но не отправлять глоссарий.
            cb.answer.assert_awaited()
            bot_mock.send_message.assert_not_awaited()

    def test_empty_callback_data_no_crash(self):
        cb = self._make_callback(data="", from_user_id=42)
        with patch("main.bot", new=MagicMock(send_message=AsyncMock())) as bot_mock:
            asyncio.run(self._handler(cb))
            cb.answer.assert_awaited()
            bot_mock.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
