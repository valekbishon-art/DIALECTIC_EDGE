"""Unit-тесты для debate-summary extraction и rendering.

Покрывают:
  * ``_split_debate_agent_speeches`` — парсинг блока «🗣 ХОД ДЕБАТОВ».
  * ``_summarize_agent_speech`` — сжатие реплик до ≤220 символов.
  * ``_shrink_text`` — обрезка по границе слова.
  * ``extract_debate_summary`` — end-to-end: report → {bull, bear, verifier}.
  * ``_format_debate_summary_block`` (main.py) — Telegram-рендер блока.

Stdlib-only — гоняются и в unit-fast, и в unit-full.
"""

from __future__ import annotations

import unittest


# ── Fixtures ─────────────────────────────────────────────────────────────

SAMPLE_REPORT = """\
# Отчёт за 27.05.2026

## 🗣 ХОД ДЕБАТОВ

── Раунд 1 ──

🐂 Bull Researcher: Институционалы жёстко лонгуют SOL (L/S 1.92) и XRP (1.69). \
Coinbase premium +0.3% подтверждает bid со стороны US-институтов. \
Мой вывод: бычий рынок подтверждается smart-money данными.

🐻 Bear Skeptic: Макро давит. Крупные спекулянты в шорте (COT –5270), а ФРС \
изымает ликвидность (QT –$15B). DXY растёт, давление продаж из США. \
Итог: медвежье давление от макрофакторов перевешивает.

── Раунд 2 ──

🐂 Bull Researcher: Несмотря на макро, on-chain показывает накопление. \
MVRV на низких уровнях, whale wallets растут.

🐻 Bear Skeptic: On-chain — lagging indicator. Price action показывает lower highs.

🔍 Verifier: Достоверных аргументов у обеих сторон хватает. Бык прав насчёт \
smart-money позиционирования, медведь прав по макро. Рынок зажат.

## ВЕРДИКТ И ТОРГОВЫЙ ПЛАН

Вердикт: NEUTRAL
Почему: рынок зажат между уровнями.
"""

SAMPLE_REPORT_NO_DEBATE = """\
# Анализ

Вердикт: BUY
Почему: всё растёт.
"""

SAMPLE_REPORT_ONLY_BULL_BEAR = """\
## 🗣 ХОД ДЕБАТОВ

── Раунд 1 ──

🐂 Bull Researcher: Позитивный тренд.

🐻 Bear Skeptic: Негативный тренд сохраняется, макро ухудшается серьёзно.

## ИТОГОВЫЙ СИНТЕЗ
"""


class TestSplitDebateAgentSpeeches(unittest.TestCase):
    """Тесты для _split_debate_agent_speeches."""

    def test_basic_split(self):
        from core.digest_context import _split_debate_agent_speeches

        speeches = _split_debate_agent_speeches(SAMPLE_REPORT)
        self.assertEqual(len(speeches["bull"]), 2, "Bull should have 2 rounds")
        self.assertEqual(len(speeches["bear"]), 2, "Bear should have 2 rounds")
        self.assertEqual(len(speeches["verifier"]), 1, "Verifier should have 1 round")

    def test_empty_input(self):
        from core.digest_context import _split_debate_agent_speeches

        result = _split_debate_agent_speeches("")
        self.assertEqual(result, {"bull": [], "bear": [], "verifier": []})

    def test_no_debate_section(self):
        from core.digest_context import _split_debate_agent_speeches

        result = _split_debate_agent_speeches(SAMPLE_REPORT_NO_DEBATE)
        self.assertEqual(result, {"bull": [], "bear": [], "verifier": []})

    def test_only_bull_bear(self):
        from core.digest_context import _split_debate_agent_speeches

        speeches = _split_debate_agent_speeches(SAMPLE_REPORT_ONLY_BULL_BEAR)
        self.assertTrue(len(speeches["bull"]) >= 1)
        self.assertTrue(len(speeches["bear"]) >= 1)
        self.assertEqual(len(speeches["verifier"]), 0)


class TestSummarizeAgentSpeech(unittest.TestCase):
    """Тесты для _summarize_agent_speech."""

    def test_short_text(self):
        from core.digest_context import _summarize_agent_speech

        result = _summarize_agent_speech("Бычий рынок подтверждён.")
        self.assertTrue(len(result) <= 220)
        self.assertTrue(len(result) > 0)

    def test_verdict_hint_extraction(self):
        from core.digest_context import _summarize_agent_speech

        text = (
            "Долгий анализ. Мой вывод: институционалы лонгуют, "
            "smart-money данные подтверждают бычий сценарий."
        )
        result = _summarize_agent_speech(text)
        self.assertIn("вывод", result.lower())

    def test_empty_input(self):
        from core.digest_context import _summarize_agent_speech

        self.assertEqual(_summarize_agent_speech(""), "")

    def test_max_chars_respected(self):
        from core.digest_context import _summarize_agent_speech

        long_text = "Слово " * 200
        result = _summarize_agent_speech(long_text, max_chars=100)
        self.assertTrue(len(result) <= 105)  # small margin for ellipsis


class TestShrinkText(unittest.TestCase):
    """Тесты для _shrink_text."""

    def test_short_passthrough(self):
        from core.digest_context import _shrink_text

        self.assertEqual(_shrink_text("Hello world", 100), "Hello world")

    def test_truncation_with_ellipsis(self):
        from core.digest_context import _shrink_text

        result = _shrink_text("This is a very long text that should be truncated", 20)
        self.assertTrue(result.endswith("…"))
        self.assertTrue(len(result) <= 25)

    def test_empty(self):
        from core.digest_context import _shrink_text

        self.assertEqual(_shrink_text("", 100), "")


class TestExtractDebateSummary(unittest.TestCase):
    """End-to-end тесты для extract_debate_summary."""

    def test_full_report(self):
        from core.digest_context import extract_debate_summary

        summary = extract_debate_summary(SAMPLE_REPORT)
        self.assertTrue(summary["bull"], "Bull summary should not be empty")
        self.assertTrue(summary["bear"], "Bear summary should not be empty")
        self.assertTrue(summary["verifier"], "Verifier summary should not be empty")
        for key in ("bull", "bear", "verifier"):
            self.assertTrue(len(summary[key]) <= 220, f"{key} too long")

    def test_no_debate(self):
        from core.digest_context import extract_debate_summary

        summary = extract_debate_summary(SAMPLE_REPORT_NO_DEBATE)
        self.assertEqual(summary, {"bull": "", "bear": "", "verifier": ""})

    def test_partial_agents(self):
        from core.digest_context import extract_debate_summary

        summary = extract_debate_summary(SAMPLE_REPORT_ONLY_BULL_BEAR)
        self.assertTrue(summary["bull"])
        self.assertTrue(summary["bear"])
        self.assertEqual(summary["verifier"], "")

    def test_empty_input(self):
        from core.digest_context import extract_debate_summary

        summary = extract_debate_summary("")
        self.assertEqual(summary, {"bull": "", "bear": "", "verifier": ""})


class TestFormatDebateSummaryBlock(unittest.TestCase):
    """Тесты для _format_debate_summary_block (main.py)."""

    def _get_func(self):
        import importlib
        import sys
        # Stub aiogram and other heavy deps so main.py can be partially imported
        # We only need the standalone function, not the full module.
        from core.digest_context import extract_debate_summary
        # Import the function directly from source to avoid full main.py import
        import types
        import re as _re

        src_path = "main.py"
        with open(src_path) as f:
            source = f.read()

        # Extract just the function source
        pattern = _re.compile(
            r"^def _format_debate_summary_block\(.*?\n(?=\ndef |\nclass |\Z)",
            _re.MULTILINE | _re.DOTALL,
        )
        match = pattern.search(source)
        if not match:
            self.fail("Could not find _format_debate_summary_block in main.py")

        func_source = match.group(0)
        ns: dict = {}
        exec(func_source, ns)
        return ns["_format_debate_summary_block"]

    def test_full_debate_block(self):
        func = self._get_func()
        result = func(
            debate_summary={"bull": "Лонг тезис", "bear": "Шорт тезис", "verifier": "Оба правы"},
            verdict_reason="Рынок зажат",
            plain_language="Сидим на заборе",
        )
        self.assertTrue(len(result) > 0)
        self.assertIn("🧠 *О чём спорил ИИ сегодня:*", result)
        self.assertIn("🐂 *Бык:*", " ".join(result))
        self.assertIn("🐻 *Медведь:*", " ".join(result))
        self.assertIn("🔍 *Скептик:*", " ".join(result))
        self.assertIn("⚖️ *Консенсус:*", " ".join(result))
        self.assertIn("💬 *Простыми словами:*", " ".join(result))

    def test_no_verifier(self):
        func = self._get_func()
        result = func(
            debate_summary={"bull": "Бычий тезис", "bear": "Медвежий тезис", "verifier": ""},
            verdict_reason="Нейтрально",
        )
        joined = " ".join(result)
        self.assertIn("🐂 *Бык:*", joined)
        self.assertIn("🐻 *Медведь:*", joined)
        self.assertNotIn("🔍 *Скептик:*", joined)

    def test_empty_debate_with_reason_and_plain(self):
        func = self._get_func()
        result = func(
            debate_summary=None,
            verdict_reason="Есть причина",
            plain_language="Есть простое объяснение",
        )
        self.assertTrue(len(result) > 0)
        self.assertIn("⚖️ *Консенсус:*", " ".join(result))

    def test_completely_empty(self):
        func = self._get_func()
        result = func(debate_summary=None, verdict_reason="", plain_language="")
        self.assertEqual(result, [])

    def test_only_bull(self):
        func = self._get_func()
        result = func(
            debate_summary={"bull": "Только бык", "bear": "", "verifier": ""},
        )
        joined = " ".join(result)
        self.assertIn("🐂 *Бык:*", joined)
        self.assertNotIn("🐻 *Медведь:*", joined)


class TestBuildDigestContextDebateSummary(unittest.TestCase):
    """Проверяем что build_digest_context включает debate_summary."""

    @classmethod
    def _import_build_digest_context(cls):
        """Import build_digest_context bypassing heavy core/__init__.py."""
        import importlib
        return importlib.import_module("core.digest_context").build_digest_context

    def test_debate_summary_in_context(self):
        build_digest_context = self._import_build_digest_context()
        ctx = build_digest_context(SAMPLE_REPORT)
        self.assertIn("debate_summary", ctx)
        ds = ctx["debate_summary"]
        self.assertIsInstance(ds, dict)
        self.assertIn("bull", ds)
        self.assertIn("bear", ds)
        self.assertIn("verifier", ds)

    def test_empty_report_debate_summary(self):
        build_digest_context = self._import_build_digest_context()
        ctx = build_digest_context("")
        ds = ctx["debate_summary"]
        self.assertEqual(ds, {"bull": "", "bear": "", "verifier": ""})


if __name__ == "__main__":
    unittest.main()
