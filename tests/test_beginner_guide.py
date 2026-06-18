"""Task 8 (Andrey): гид «Я новичок» переписан под РЕАЛЬНЫЕ edge'и бота
(carry / кросс-арб / базис / edge-леджер), а НЕ под directional-торговлю
(spot/futures, MA50/200, swing-горизонты, BULLISH/BEARISH) — тот режим
доказанно убыточен и удалён. Тест запрещает регресс к directional-контенту.
"""
from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MD = REPO / "docs" / "BEGINNER_GUIDE.md"


class TestBeginnerGuideMarkdown(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MD.exists(), "docs/BEGINNER_GUIDE.md отсутствует")
        self.text = MD.read_text(encoding="utf-8")

    def test_covers_real_edges(self):
        low = self.text.lower()
        for term in ("дельта-нейтрал", "carry", "/arb", "/basis", "/signal",
                     "funding", "базис", "edge-леджер"):
            self.assertIn(term.lower(), low, f"в гиде нет «{term}»")

    def test_no_directional_artifacts(self):
        # Старый directional-контент не должен вернуться.
        for banned in ("MA50", "MA200", "BULLISH", "BEARISH", "Swing (7"):
            self.assertNotIn(banned, self.text, f"directional-пережиток «{banned}»")

    def test_honesty_disclaimer_present(self):
        low = self.text.lower()
        self.assertIn("не финансовый совет", low)
        # Честность про удалённый directional-режим.
        self.assertIn("убыточн", low)


class TestInlineGuideSource(unittest.TestCase):
    """Inline-выжимка в main.py тоже не должна содержать directional-пережитков."""

    def test_inline_parts_are_delta_neutral(self):
        src = (REPO / "main.py").read_text(encoding="utf-8")
        # Берём тело _send_newbie_guide.
        start = src.index("async def _send_newbie_guide")
        end = src.index("class _CallbackMessageProxy", start)
        body = src[start:end]
        # Гид должен продавать реальный edge — спот + следование тренду,
        # а не угадывание направления.
        for term in ("спот", "тренд", "SMA"):
            self.assertIn(term, body, f"в inline-гиде нет «{term}»")
        # И не должен тянуть исключённые из проекта механики/обещания направления.
        # (MA50/MA200 не баним — SMA50/SMA200 это легитимный трендовый фильтр.)
        for banned in ("BULLISH", "/carry", "/arb", "/basis", "funding"):
            self.assertNotIn(banned, body, f"в гиде остался исключённый «{banned}»")


if __name__ == "__main__":
    unittest.main()
