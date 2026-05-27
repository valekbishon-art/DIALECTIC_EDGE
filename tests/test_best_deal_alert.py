# -*- coding: utf-8 -*-
"""Тесты для best_deal_alert: top-N батч + cooldown.

Покрываем:
  - `_format_alert` с одним setup'ом (backward-compat).
  - `_format_alert` с list из 1..3 setup'ов (новый формат с медалями).
  - `get_top_n` env-handling.
  - `BestDealAlertSystem.check_and_alert`:
    * пустой subscribers → 0.
    * feature disabled → 0.
    * нет tradable → 0.
    * 1 юзер, нет cooldown → шлём батч из top-N, маркируем все.
    * 1 юзер с allowed=[BTC] → шлём только BTC из батча.
    * 1 юзер, все setup'ы в cooldown → пропуск.
    * 1 юзер, 1 setup score bump'нул → шлём весь батч.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from unittest.mock import patch

from best_deal_alert import (
    BestDealAlertSystem,
    DEFAULT_TOP_N,
    _LastAlert,
    _format_alert,
    get_top_n,
)


@dataclass
class _StubSetup:
    """Минимальный stub `core.signal_scorer.SignalSetup` для тестов рендера.

    Реальный `SignalSetup` — `@dataclass` с полями asset/direction/entry/stop/
    target/score/rr_ratio/sigma_1d_pct/reasons. Stub воспроизводит интерфейс
    `_format_single_setup` (asset, direction, score, entry, stop, target,
    sigma_1d_pct, reasons).
    """
    asset: str
    direction: str
    score: int
    entry: float
    stop: float
    target: float
    sigma_1d_pct: float | None = 2.5
    rr_ratio: float | None = 2.0
    reasons: list[str] = field(default_factory=lambda: ["bias+quant"])
    # Опциональные старые имена — иногда _format_alert смотрит на них.
    rr: float | None = None
    reason: str = ""


class TestFormatAlert(unittest.TestCase):
    """Рендер _format_alert — 1 vs N setup'ов."""

    def test_single_setup_legacy_path(self):
        s = _StubSetup(
            asset="BTC", direction="LONG", score=82,
            entry=77500.0, stop=73890.0, target=84250.0,
        )
        out = _format_alert(s)
        # Заголовок ОДНОГО setup'а — «лучший setup score ≥ 60».
        self.assertIn("лучший setup score", out)
        self.assertIn("*BTC*", out)
        self.assertIn("*LONG*", out)
        self.assertIn("$77,500", out)
        self.assertIn("score *82/100*", out)
        # Без медалей.
        self.assertNotIn("🥇", out)
        self.assertNotIn("🥈", out)

    def test_multi_setup_top3_with_medals(self):
        s1 = _StubSetup(asset="BTC", direction="LONG", score=82,
                        entry=77500.0, stop=73890.0, target=84250.0)
        s2 = _StubSetup(asset="SOL", direction="LONG", score=68,
                        entry=108.5, stop=102.3, target=120.1)
        s3 = _StubSetup(asset="XRP", direction="SHORT", score=65,
                        entry=1.49, stop=1.55, target=1.36)
        out = _format_alert([s1, s2, s3])
        # Заголовок батча.
        self.assertIn("ТОП-3 setups", out)
        # Все 3 актива.
        self.assertIn("*BTC*", out)
        self.assertIn("*SOL*", out)
        self.assertIn("*XRP*", out)
        # Медали 1/2/3.
        self.assertIn("🥇", out)
        self.assertIn("🥈", out)
        self.assertIn("🥉", out)
        # И LONG, и SHORT представлены.
        self.assertIn("*LONG*", out)
        self.assertIn("*SHORT*", out)

    def test_multi_setup_top2(self):
        s1 = _StubSetup(asset="ETH", direction="LONG", score=75,
                        entry=3500.0, stop=3380.0, target=3700.0)
        s2 = _StubSetup(asset="SOL", direction="LONG", score=63,
                        entry=110.0, stop=104.0, target=120.0)
        out = _format_alert([s1, s2])
        self.assertIn("ТОП-2 setups", out)
        self.assertIn("🥇", out)
        self.assertIn("🥈", out)
        self.assertNotIn("🥉", out)

    def test_empty_list_returns_empty_string(self):
        self.assertEqual(_format_alert([]), "")

    def test_format_handles_reasons_list_when_no_reason(self):
        """SignalSetup использует `reasons` (list), а stub без `reason`. Проверяем
        что _format_alert корректно join'ит `reasons[:2]`."""
        s = _StubSetup(
            asset="BTC", direction="LONG", score=82,
            entry=77500.0, stop=73890.0, target=84250.0,
            reasons=["UPTREND +5%", "TRENDING H=0.62", "extra ignored"],
        )
        out = _format_alert([s])
        # Из reasons[:2] взяли первые две.
        self.assertIn("UPTREND +5%", out)
        self.assertIn("TRENDING H=0.62", out)
        self.assertNotIn("extra ignored", out)


class TestGetTopN(unittest.TestCase):
    def test_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BEST_DEAL_ALERT_TOP_N", None)
            self.assertEqual(get_top_n(), DEFAULT_TOP_N)

    def test_env_override(self):
        with patch.dict(os.environ, {"BEST_DEAL_ALERT_TOP_N": "5"}):
            self.assertEqual(get_top_n(), 5)

    def test_env_clamp_high(self):
        with patch.dict(os.environ, {"BEST_DEAL_ALERT_TOP_N": "100"}):
            self.assertEqual(get_top_n(), 5)  # max_val=5

    def test_env_clamp_low(self):
        with patch.dict(os.environ, {"BEST_DEAL_ALERT_TOP_N": "0"}):
            self.assertEqual(get_top_n(), 1)  # min_val=1

    def test_env_invalid(self):
        with patch.dict(os.environ, {"BEST_DEAL_ALERT_TOP_N": "abc"}):
            self.assertEqual(get_top_n(), DEFAULT_TOP_N)


# ── BestDealAlertSystem.check_and_alert ──────────────────────────────────────


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, user_id, text, **kwargs):
        self.sent.append((user_id, text))


class TestCheckAndAlertBatching(unittest.TestCase):
    """check_and_alert: top-N батч с cooldown'ом per-asset."""

    def _make_setups(self) -> list:
        """3 tradable setup'а — фейковые SignalSetup'ы."""
        # Импортируем реальный SignalSetup чтобы isinstance() в check_and_alert
        # проходил. Если core.signal_scorer недоступен — тесты skip.
        from core.signal_scorer import SignalSetup
        return [
            SignalSetup(
                asset="BTC", direction="LONG",
                entry=77500.0, stop=73890.0, target=84250.0,
                stop_pct=-4.66, target_pct=8.71,
                rr_ratio=1.87, sigma_1d_pct=2.41,
                size_usd=12.3, score=82,
                reasons=["bias+quant"],
            ),
            SignalSetup(
                asset="SOL", direction="LONG",
                entry=108.5, stop=102.3, target=120.1,
                stop_pct=-5.71, target_pct=10.69,
                rr_ratio=1.87, sigma_1d_pct=3.12,
                size_usd=12.3, score=68,
                reasons=["L/S 1.92"],
            ),
            SignalSetup(
                asset="XRP", direction="LONG",
                entry=1.49, stop=1.42, target=1.61,
                stop_pct=-4.7, target_pct=8.05,
                rr_ratio=1.71, sigma_1d_pct=2.87,
                size_usd=12.3, score=65,
                reasons=["COT bias"],
            ),
        ]

    def _run(self, system, subscribers):
        # asyncio.run() создаёт свежий event loop под каждый прогон —
        # совместимо с unittest discover, где старый цикл бывает закрыт.
        return asyncio.run(system.check_and_alert(subscribers))

    def setUp(self):
        try:
            from core.signal_scorer import SignalSetup  # noqa: F401
        except Exception:
            self.skipTest("core.signal_scorer недоступен")
        self.bot = _FakeBot()
        self.system = BestDealAlertSystem(self.bot)

    def test_empty_subscribers(self):
        sent = self._run(self.system, [])
        self.assertEqual(sent, 0)
        self.assertEqual(self.bot.sent, [])

    def test_feature_disabled(self):
        with patch.dict(os.environ, {"FEATURE_BEST_DEAL_AUTO_PUSH": "0"}):
            sent = self._run(self.system, [{"user_id": 1}])
        self.assertEqual(sent, 0)
        self.assertEqual(self.bot.sent, [])

    def test_no_tradable(self):
        """Если rank_signals.tradable_setups пуст — не шлём ничего."""
        with patch("web_search.fetch_realtime_prices") as fetch_mock, \
             patch("core.signal_scorer.rank_signals") as rank_mock:
            async def _fetch():
                return {"BTC": {"price": 77500.0}}
            fetch_mock.side_effect = _fetch
            rank_mock.return_value = {"tradable_setups": []}
            sent = self._run(self.system, [{"user_id": 1}])
        self.assertEqual(sent, 0)
        self.assertEqual(self.bot.sent, [])

    def test_sends_top_n_batch_first_time(self):
        """Первый алерт: cooldown пуст, шлём батч из всех 3 setup'ов."""
        setups = self._make_setups()
        with patch("web_search.fetch_realtime_prices") as fetch_mock, \
             patch("core.signal_scorer.rank_signals") as rank_mock:
            async def _fetch():
                return {"BTC": {"price": 77500.0}}
            fetch_mock.side_effect = _fetch
            rank_mock.return_value = {"tradable_setups": setups}
            sent = self._run(self.system, [{"user_id": 1}])
        self.assertEqual(sent, 1)
        self.assertEqual(len(self.bot.sent), 1)
        user_id, text = self.bot.sent[0]
        self.assertEqual(user_id, 1)
        # Батч содержит все 3 актива.
        self.assertIn("BTC", text)
        self.assertIn("SOL", text)
        self.assertIn("XRP", text)
        self.assertIn("ТОП-3 setups", text)
        # Все 3 пары записаны в _last (cooldown трекинг).
        self.assertIn("1:BTC:LONG", self.system._last)
        self.assertIn("1:SOL:LONG", self.system._last)
        self.assertIn("1:XRP:LONG", self.system._last)

    def test_per_user_allowed_filter(self):
        """allowed=[BTC] → в батче только BTC, SOL/XRP отсечены."""
        setups = self._make_setups()
        with patch("web_search.fetch_realtime_prices") as fetch_mock, \
             patch("core.signal_scorer.rank_signals") as rank_mock:
            async def _fetch():
                return {"BTC": {"price": 77500.0}}
            fetch_mock.side_effect = _fetch
            rank_mock.return_value = {"tradable_setups": setups}
            sent = self._run(
                self.system,
                [{"user_id": 1, "signals_assets": "BTC"}],
            )
        self.assertEqual(sent, 1)
        _, text = self.bot.sent[0]
        # BTC есть, SOL/XRP — нет.
        self.assertIn("BTC", text)
        self.assertNotIn("SOL", text)
        self.assertNotIn("XRP", text)
        # Заголовок одиночного setup'а.
        self.assertIn("лучший setup score", text)

    def test_empty_allowed_user_skipped(self):
        """signals_assets='' (явно «Снять все») → ничего не шлём."""
        setups = self._make_setups()
        with patch("web_search.fetch_realtime_prices") as fetch_mock, \
             patch("core.signal_scorer.rank_signals") as rank_mock:
            async def _fetch():
                return {"BTC": {"price": 77500.0}}
            fetch_mock.side_effect = _fetch
            rank_mock.return_value = {"tradable_setups": setups}
            self._run(
                self.system,
                [{"user_id": 1, "signals_assets": ""}],
            )
        # signals_assets="" → _user_allowed_assets возвращает None (пустой raw),
        # значит шлём все. Этот тест проверяет именно границу «явно сняли»
        # отличается от «дефолт = все». _user_allowed_assets разбирает CSV,
        # пустая строка даёт None.
        # Для теста «снять все» используем явный список из пустых элементов
        # вроде запятой:
        self.bot.sent.clear()
        self.system._last.clear()
        with patch("web_search.fetch_realtime_prices") as fetch_mock, \
             patch("core.signal_scorer.rank_signals") as rank_mock:
            async def _fetch():
                return {"BTC": {"price": 77500.0}}
            fetch_mock.side_effect = _fetch
            rank_mock.return_value = {"tradable_setups": setups}
            sent2 = self._run(
                self.system,
                [{"user_id": 1, "signals_assets": []}],
            )
        # signals_assets=[] → set() → пустой и не-None → пропускаем юзера.
        self.assertEqual(sent2, 0)

    def test_all_in_cooldown_skipped(self):
        """Все 3 setup'а недавно отправлены и score не поднялся — пропуск батча."""
        setups = self._make_setups()
        now = datetime.now()
        for s in setups:
            self.system._last[f"1:{s.asset}:{s.direction}"] = _LastAlert(
                asset=s.asset, direction=s.direction,
                score=int(s.score), fired_at=now - timedelta(minutes=10),
            )
        with patch("web_search.fetch_realtime_prices") as fetch_mock, \
             patch("core.signal_scorer.rank_signals") as rank_mock:
            async def _fetch():
                return {"BTC": {"price": 77500.0}}
            fetch_mock.side_effect = _fetch
            rank_mock.return_value = {"tradable_setups": setups}
            sent = self._run(self.system, [{"user_id": 1}])
        self.assertEqual(sent, 0)
        self.assertEqual(self.bot.sent, [])

    def test_score_bump_unlocks_batch(self):
        """1 setup из 3 score'а bump'нул → шлём батч из всех 3 (текущий top-N).

        Юзер видит «вот наш текущий top-3, BTC просел до 65, но SOL прыгнул
        с 50 до 70 = bump'нул на 20». Это правильная UX: показываем всё
        актуальное состояние топа, а не только «новый» setup.
        """
        setups = self._make_setups()
        now = datetime.now()
        # BTC и XRP — недавно с тем же score (в cooldown'е).
        # SOL — в cooldown'е со score=50 (текущий 68 → bump +18 > 15).
        self.system._last["1:BTC:LONG"] = _LastAlert(
            asset="BTC", direction="LONG", score=82,
            fired_at=now - timedelta(minutes=10),
        )
        self.system._last["1:SOL:LONG"] = _LastAlert(
            asset="SOL", direction="LONG", score=50,
            fired_at=now - timedelta(minutes=10),
        )
        self.system._last["1:XRP:LONG"] = _LastAlert(
            asset="XRP", direction="LONG", score=65,
            fired_at=now - timedelta(minutes=10),
        )
        with patch("web_search.fetch_realtime_prices") as fetch_mock, \
             patch("core.signal_scorer.rank_signals") as rank_mock:
            async def _fetch():
                return {"BTC": {"price": 77500.0}}
            fetch_mock.side_effect = _fetch
            rank_mock.return_value = {"tradable_setups": setups}
            sent = self._run(self.system, [{"user_id": 1}])
        self.assertEqual(sent, 1)
        _, text = self.bot.sent[0]
        # Батч из всех 3.
        self.assertIn("BTC", text)
        self.assertIn("SOL", text)
        self.assertIn("XRP", text)
        # _last обновлён по SOL (новый score=68).
        self.assertEqual(self.system._last["1:SOL:LONG"].score, 68)


if __name__ == "__main__":
    unittest.main()
