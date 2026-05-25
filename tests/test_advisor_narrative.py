"""Tests for core.advisor_narrative (AI explanation для /advise)."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from core.advisor_narrative import (
    _build_market_snapshot,
    _build_plan_summary,
    _build_prompt,
    feature_enabled,
    generate_plan_narrative,
)
from refactor.providers.advisor_storage import StoredPlan


def _make_plan(**overrides) -> StoredPlan:
    base = dict(
        asset="BTC",
        action="BUY",
        direction="LONG",
        confidence_pct=72,
        entry_price=50000.0,
        stop_price=48000.0,
        risk_reward=2.5,
        tp_levels=[
            {"price": 52000.0, "r_multiple": 1.0, "close_pct": 30},
            {"price": 55000.0, "r_multiple": 2.5, "close_pct": 40},
            {"price": 58000.0, "r_multiple": 4.0, "close_pct": 30},
        ],
        position_usd=1000.0,
        horizon_human="3-7 дней",
        btc_overlay_note="",
        rationale=["Тренд UP", "RSI 55"],
    )
    base.update(overrides)
    return StoredPlan(**base)


class TestFeatureFlag(unittest.TestCase):
    def test_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feature_enabled())

    def test_explicit_on(self):
        with patch.dict(os.environ, {"FEATURE_ADVISOR_NARRATIVE": "1"}, clear=True):
            self.assertTrue(feature_enabled())


class TestPromptBuilders(unittest.TestCase):
    def test_plan_summary_includes_core_fields(self):
        plan = _make_plan()
        summary = _build_plan_summary(plan)
        self.assertIn("BTC", summary)
        self.assertIn("LONG", summary)
        self.assertIn("72%", summary)
        self.assertIn("50,000", summary)
        self.assertIn("48,000", summary)
        self.assertIn("TP1", summary)
        self.assertIn("R/R: 2.50", summary)
        self.assertIn("$1000", summary)

    def test_plan_summary_omits_none_fields(self):
        plan = _make_plan(
            entry_price=None, stop_price=None, risk_reward=None,
            tp_levels=[], position_usd=None,
        )
        summary = _build_plan_summary(plan)
        self.assertNotIn("Вход:", summary)
        self.assertNotIn("Стоп:", summary)
        self.assertNotIn("Тейки:", summary)
        self.assertNotIn("R/R:", summary)
        self.assertNotIn("Размер:", summary)

    def test_market_snapshot_with_context(self):
        ctx = {"btc_lean": "BULL", "btc_confidence_pct": 68, "trend": "UP", "rsi": 55, "fear_greed": 60}
        snap = _build_market_snapshot(ctx)
        self.assertIn("BTC outlook: BULL", snap)
        self.assertIn("уверенность 68%", snap)
        self.assertIn("Тренд актива: UP", snap)
        self.assertIn("RSI: 55", snap)
        self.assertIn("Fear&Greed: 60", snap)

    def test_market_snapshot_empty(self):
        self.assertIn("не передан", _build_market_snapshot(None))
        self.assertIn("не передан", _build_market_snapshot({}))

    def test_prompt_includes_structure_instructions(self):
        prompt = _build_prompt(_make_plan(), {"btc_lean": "BULL", "btc_confidence_pct": 70})
        self.assertIn("Контекст", prompt)
        self.assertIn("Почему этот сетап", prompt)
        self.assertIn("Риски", prompt)
        self.assertIn("На что смотреть", prompt)
        # Ensure plan + market sections are present
        self.assertIn("=== ПЛАН ===", prompt)
        self.assertIn("=== РЫНОК ===", prompt)


class TestGenerateNarrative(unittest.TestCase):
    def test_disabled_returns_none(self):
        with patch.dict(os.environ, {"FEATURE_ADVISOR_NARRATIVE": "0"}, clear=True):
            out = asyncio.run(generate_plan_narrative(_make_plan(), {}))
            self.assertIsNone(out)

    def test_returns_cached_narrative_without_ai_call(self):
        plan = _make_plan()
        plan.narrative = "Уже есть готовый текст."
        with patch.dict(os.environ, {"FEATURE_ADVISOR_NARRATIVE": "1"}, clear=True):
            mock_ai = AsyncMock()
            out = asyncio.run(generate_plan_narrative(
                plan, {}, agent_provider=mock_ai,
            ))
        self.assertEqual(out, "Уже есть готовый текст.")
        mock_ai.complete.assert_not_called()

    def test_calls_ai_when_no_cache(self):
        plan = _make_plan()
        mock_ai = AsyncMock()
        mock_ai.complete = AsyncMock(return_value="📍 Bull trend setup.")
        with patch.dict(os.environ, {"FEATURE_ADVISOR_NARRATIVE": "1"}, clear=True):
            out = asyncio.run(generate_plan_narrative(
                plan, {"btc_lean": "BULL", "btc_confidence_pct": 70},
                agent_provider=mock_ai,
            ))
        self.assertEqual(out, "📍 Bull trend setup.")
        mock_ai.complete.assert_called_once()

    def test_ai_failure_returns_none(self):
        plan = _make_plan()
        mock_ai = AsyncMock()
        mock_ai.complete = AsyncMock(side_effect=RuntimeError("API down"))
        with patch.dict(os.environ, {"FEATURE_ADVISOR_NARRATIVE": "1"}, clear=True):
            out = asyncio.run(generate_plan_narrative(
                plan, {}, agent_provider=mock_ai,
            ))
        self.assertIsNone(out)

    def test_empty_ai_response_returns_none(self):
        plan = _make_plan()
        mock_ai = AsyncMock()
        mock_ai.complete = AsyncMock(return_value="   ")
        with patch.dict(os.environ, {"FEATURE_ADVISOR_NARRATIVE": "1"}, clear=True):
            out = asyncio.run(generate_plan_narrative(
                plan, {}, agent_provider=mock_ai,
            ))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
