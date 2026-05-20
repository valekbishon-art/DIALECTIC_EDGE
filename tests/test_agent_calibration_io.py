"""Unit-тесты для core/agent_calibration_io.py.

Покрывают:
  * `parse_probability` — извлечение P(up) из шумных ответов LLM:
      - чистый «0.65», «0,65», «65%», «65», «0.0», «1.0»;
      - текст с пояснениями «P(up) = 0.7 because...»;
      - невалид: «», «no probability», числа > 100;
      - clip в [0, 1].
  * `build_probability_prompt` — содержит asset, threshold, horizon, ref_price.
  * `feature_enabled` — env-flag parsing.
  * `extract_probability` — успешный путь + AI exception + parse failure.
  * `save_post_debate_predictions` — DI saver вызывается с правильными args,
    resolve_at = now + horizon, skip None probs.
  * `evaluate_pending_predictions`:
      - empty pending → resolved=0;
      - happy path: y=1 если price вырос на threshold, y=0 иначе;
      - skip когда price_fetcher вернул None;
      - failed когда resolver кидает.
  * `get_all_agent_stats` — агрегирует stats по ролям через injected loader.

Все внешние зависимости (DB, AI, market) подменены через DI — никаких
реальных HTTP/SQL вызовов. Stdlib-only — гоняются и в unit-fast, и в unit-full.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from core.agent_calibration_io import (
    AgentProbabilityRequest,
    EvaluationResult,
    build_probability_prompt,
    evaluate_pending_predictions,
    extract_probability,
    feature_enabled,
    get_all_agent_stats,
    parse_probability,
    save_post_debate_predictions,
)


def _run(coro):
    """Helper: запустить корутину в текущем event loop'е (unittest не async)."""
    return asyncio.run(coro)


class ParseProbabilityTestCase(unittest.TestCase):
    def test_clean_float(self) -> None:
        self.assertAlmostEqual(parse_probability("0.65"), 0.65)
        self.assertAlmostEqual(parse_probability("0.0"), 0.0)
        self.assertAlmostEqual(parse_probability("1.0"), 1.0)
        self.assertAlmostEqual(parse_probability(".7"), 0.7)

    def test_percent_format(self) -> None:
        # Простой percent.
        self.assertAlmostEqual(parse_probability("65%"), 0.65)
        self.assertAlmostEqual(parse_probability("65"), 0.65)
        self.assertAlmostEqual(parse_probability("100%"), 1.0)

    def test_noisy_text(self) -> None:
        # LLM выдаёт пояснение — берём первое число.
        self.assertAlmostEqual(parse_probability("P(up) = 0.72 because..."), 0.72)
        self.assertAlmostEqual(
            parse_probability("My estimate: 0.45 (medium conviction)"),
            0.45,
        )

    def test_empty_or_invalid(self) -> None:
        self.assertIsNone(parse_probability(""))
        self.assertIsNone(parse_probability("no number here"))
        self.assertIsNone(parse_probability(None))  # type: ignore[arg-type]

    def test_out_of_range_skipped(self) -> None:
        # Числа > 100 не имеют смысла.
        self.assertIsNone(parse_probability("12345"))
        # Но 50 — это процент, → 0.5.
        self.assertAlmostEqual(parse_probability("50"), 0.5)


class BuildProbabilityPromptTestCase(unittest.TestCase):
    def test_contains_required_fields(self) -> None:
        prompt = build_probability_prompt(
            agent_role="bull",
            asset="btc",
            threshold_pct=0.5,
            horizon_minutes=1440,
            ref_price=67_543.21,
        )
        self.assertIn("BTC", prompt)  # upper-cased
        self.assertIn("0.50", prompt)
        self.assertIn("24", prompt)  # 1440 / 60 = 24h
        self.assertIn("67543.21", prompt)  # ref price
        self.assertIn("Bull Researcher", prompt)  # переведённое название роли

    def test_unknown_role_falls_back_to_raw(self) -> None:
        prompt = build_probability_prompt(
            agent_role="speechwriter",
            asset="eth",
            threshold_pct=1.0,
            horizon_minutes=240,
            ref_price=3500.0,
        )
        # Если роль unknown — используем raw имя.
        self.assertIn("speechwriter", prompt.lower())


class FeatureEnabledTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("FEATURE_AGENT_CALIBRATION")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("FEATURE_AGENT_CALIBRATION", None)
        else:
            os.environ["FEATURE_AGENT_CALIBRATION"] = self._saved

    def test_default_off(self) -> None:
        os.environ.pop("FEATURE_AGENT_CALIBRATION", None)
        self.assertFalse(feature_enabled())

    def test_explicit_on_values(self) -> None:
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            os.environ["FEATURE_AGENT_CALIBRATION"] = val
            self.assertTrue(feature_enabled(), f"Expected enabled for {val!r}")

    def test_explicit_off_values(self) -> None:
        for val in ("0", "false", "no", "off", "garbage", ""):
            os.environ["FEATURE_AGENT_CALIBRATION"] = val
            self.assertFalse(feature_enabled(), f"Expected disabled for {val!r}")


class ExtractProbabilityTestCase(unittest.TestCase):
    def test_happy_path(self) -> None:
        ai_mock = AsyncMock(return_value="0.72")
        request = AgentProbabilityRequest(
            asset="BTC", agent_role="bull",
            horizon_minutes=1440, threshold_pct=0.5,
            ref_price=67_000.0,
        )
        p = _run(extract_probability(
            request=request,
            news_context="news...",
            debate_summary="debate...",
            ai_callable=ai_mock,
        ))
        self.assertAlmostEqual(p, 0.72)
        ai_mock.assert_called_once()

    def test_ai_failure_returns_none(self) -> None:
        ai_mock = AsyncMock(side_effect=RuntimeError("AI down"))
        request = AgentProbabilityRequest(
            asset="BTC", agent_role="bear",
            horizon_minutes=240, threshold_pct=1.0,
            ref_price=67_000.0,
        )
        p = _run(extract_probability(
            request=request,
            news_context="",
            debate_summary="",
            ai_callable=ai_mock,
        ))
        self.assertIsNone(p)

    def test_parse_failure_returns_none(self) -> None:
        ai_mock = AsyncMock(return_value="нет числа в ответе")
        request = AgentProbabilityRequest(
            asset="BTC", agent_role="synth",
            horizon_minutes=60, threshold_pct=0.5,
            ref_price=67_000.0,
        )
        p = _run(extract_probability(
            request=request,
            news_context="", debate_summary="",
            ai_callable=ai_mock,
        ))
        self.assertIsNone(p)


class SavePostDebatePredictionsTestCase(unittest.TestCase):
    def test_calls_saver_per_role(self) -> None:
        saver = AsyncMock(side_effect=[101, 102, 103])
        now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
        ids = _run(save_post_debate_predictions(
            asset="btc",
            ref_price=67_000.0,
            extracted={"bull": 0.7, "bear": 0.3, "synth": 0.55},
            debate_id="dbg-1",
            horizon_minutes=1440,
            threshold_pct=0.5,
            now=now,
            saver=saver,
        ))
        self.assertEqual(ids, [101, 102, 103])
        self.assertEqual(saver.call_count, 3)

        # Проверим что resolve_at = now + horizon.
        expected_resolve_at = (now + timedelta(minutes=1440)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        for call in saver.call_args_list:
            kwargs = call.kwargs
            self.assertEqual(kwargs["resolve_at"], expected_resolve_at)
            self.assertEqual(kwargs["asset"], "BTC")
            self.assertEqual(kwargs["horizon_minutes"], 1440)
            self.assertEqual(kwargs["threshold_pct"], 0.5)
            self.assertEqual(kwargs["ref_price"], 67_000.0)
            self.assertEqual(kwargs["debate_id"], "dbg-1")

    def test_skip_none_probabilities(self) -> None:
        saver = AsyncMock(return_value=42)
        ids = _run(save_post_debate_predictions(
            asset="ETH",
            ref_price=3500.0,
            extracted={"bull": 0.7, "bear": None, "synth": 0.5},  # type: ignore[arg-type]
            saver=saver,
        ))
        # Только 2 не-None.
        self.assertEqual(len(ids), 2)
        self.assertEqual(saver.call_count, 2)

    def test_empty_extracted(self) -> None:
        saver = AsyncMock()
        ids = _run(save_post_debate_predictions(
            asset="BTC", ref_price=67_000.0,
            extracted={}, saver=saver,
        ))
        self.assertEqual(ids, [])
        saver.assert_not_called()


class EvaluatePendingPredictionsTestCase(unittest.TestCase):
    def test_empty_pending(self) -> None:
        loader = AsyncMock(return_value=[])
        resolver = AsyncMock()
        price_fetcher = AsyncMock()
        result = _run(evaluate_pending_predictions(
            price_fetcher=price_fetcher,
            pending_loader=loader,
            resolver=resolver,
        ))
        self.assertEqual(result, EvaluationResult(0, 0, 0))
        resolver.assert_not_called()

    def test_happy_path_y_equals_1(self) -> None:
        # ref=100, current=102 → +2% (>= threshold 0.5) → y=1.
        # p_up = 0.8, brier = (0.8 - 1)^2 = 0.04
        rows = [{
            "id": 1, "asset": "BTC", "ref_price": 100.0,
            "p_up": 0.8, "threshold_pct": 0.5, "horizon_minutes": 60,
        }]
        loader = AsyncMock(return_value=rows)
        resolver = AsyncMock()
        price_fetcher = AsyncMock(return_value=102.0)

        result = _run(evaluate_pending_predictions(
            price_fetcher=price_fetcher,
            pending_loader=loader,
            resolver=resolver,
        ))
        self.assertEqual(result.resolved, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.failed, 0)
        resolver.assert_called_once_with(
            prediction_id=1,
            realized_price=102.0,
            realized_y=True,
            brier_score=unittest.mock.ANY,
        )
        # Проверяем что brier ≈ 0.04
        kwargs = resolver.call_args.kwargs
        self.assertAlmostEqual(kwargs["brier_score"], 0.04, places=6)

    def test_happy_path_y_equals_0(self) -> None:
        # ref=100, current=99.9 → -0.1% → не достиг threshold 0.5 → y=0.
        # p_up = 0.8, brier = (0.8 - 0)^2 = 0.64
        rows = [{
            "id": 2, "asset": "ETH", "ref_price": 100.0,
            "p_up": 0.8, "threshold_pct": 0.5, "horizon_minutes": 60,
        }]
        loader = AsyncMock(return_value=rows)
        resolver = AsyncMock()
        price_fetcher = AsyncMock(return_value=99.9)

        result = _run(evaluate_pending_predictions(
            price_fetcher=price_fetcher,
            pending_loader=loader,
            resolver=resolver,
        ))
        self.assertEqual(result.resolved, 1)
        kwargs = resolver.call_args.kwargs
        self.assertEqual(kwargs["realized_y"], False)
        self.assertAlmostEqual(kwargs["brier_score"], 0.64, places=6)

    def test_skip_when_price_none(self) -> None:
        rows = [{
            "id": 3, "asset": "DOGE", "ref_price": 0.1,
            "p_up": 0.5, "threshold_pct": 0.5, "horizon_minutes": 60,
        }]
        loader = AsyncMock(return_value=rows)
        resolver = AsyncMock()
        price_fetcher = AsyncMock(return_value=None)

        result = _run(evaluate_pending_predictions(
            price_fetcher=price_fetcher,
            pending_loader=loader,
            resolver=resolver,
        ))
        self.assertEqual(result.resolved, 0)
        self.assertEqual(result.skipped, 1)
        resolver.assert_not_called()

    def test_resolver_exception_counts_as_failed(self) -> None:
        rows = [{
            "id": 4, "asset": "BTC", "ref_price": 100.0,
            "p_up": 0.7, "threshold_pct": 0.5, "horizon_minutes": 60,
        }]
        loader = AsyncMock(return_value=rows)
        resolver = AsyncMock(side_effect=RuntimeError("DB error"))
        price_fetcher = AsyncMock(return_value=101.0)

        result = _run(evaluate_pending_predictions(
            price_fetcher=price_fetcher,
            pending_loader=loader,
            resolver=resolver,
        ))
        self.assertEqual(result.resolved, 0)
        self.assertEqual(result.failed, 1)

    def test_price_cache_avoids_duplicate_fetches(self) -> None:
        # Два прогноза на тот же актив — fetch цены только 1 раз.
        rows = [
            {
                "id": 5, "asset": "BTC", "ref_price": 100.0,
                "p_up": 0.7, "threshold_pct": 0.5, "horizon_minutes": 60,
            },
            {
                "id": 6, "asset": "BTC", "ref_price": 100.0,
                "p_up": 0.4, "threshold_pct": 0.5, "horizon_minutes": 60,
            },
        ]
        loader = AsyncMock(return_value=rows)
        resolver = AsyncMock()
        price_fetcher = AsyncMock(return_value=101.0)

        _run(evaluate_pending_predictions(
            price_fetcher=price_fetcher,
            pending_loader=loader,
            resolver=resolver,
        ))
        # Один вызов price_fetcher на «BTC», даже если 2 прогноза.
        self.assertEqual(price_fetcher.call_count, 1)
        self.assertEqual(resolver.call_count, 2)


class GetAllAgentStatsTestCase(unittest.TestCase):
    def test_aggregates_per_role(self) -> None:
        # Каждая роль возвращает свои rows из stub'а.
        rows_by_role = {
            "bull": [
                {"p_up": 1.0, "realized_y": 1}, {"p_up": 0.8, "realized_y": 1},
                {"p_up": 0.7, "realized_y": 1},
            ],
            "bear": [
                {"p_up": 0.3, "realized_y": 0}, {"p_up": 0.2, "realized_y": 0},
            ],
            "synth": [],
        }

        async def loader(*, agent_role, **kwargs):
            return rows_by_role.get(agent_role, [])

        stats = _run(get_all_agent_stats(
            roles=("bull", "bear", "synth"),
            history_loader=loader,
        ))
        self.assertEqual(stats["bull"].n_resolved, 3)
        self.assertEqual(stats["bear"].n_resolved, 2)
        self.assertEqual(stats["synth"].n_resolved, 0)
        # Bull прав → его weight выше Bear (т.к. Bear даёт p=0.3 на y=0, тоже неплохо).
        # Просто проверим что Bull > 0 при таких данных.
        self.assertGreater(stats["bull"].weight, 0.0)


if __name__ == "__main__":
    unittest.main()
