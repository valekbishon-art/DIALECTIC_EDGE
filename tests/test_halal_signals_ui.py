"""Тесты ui_kit + халяль-сигналов (чистая логика, без сети)."""
import asyncio
from unittest.mock import patch

import pytest

import ui_kit
import halal_signals as hs


# ─────────────────────────── ui_kit ───────────────────────────
def test_sparkline_basic():
    s = ui_kit.sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(s) == 8
    assert s[0] == "▁" and s[-1] == "█"


def test_sparkline_constant_and_empty():
    assert ui_kit.sparkline([]) == ""
    assert ui_kit.sparkline([5, 5, 5]) == "▄▄▄"
    assert ui_kit.sparkline([1, None, 2]).__len__() == 2  # None отброшен


def test_bar_clamps():
    assert ui_kit.bar(0.0, 10) == "░" * 10
    assert ui_kit.bar(1.0, 10) == "█" * 10
    assert ui_kit.bar(1.5, 10) == "█" * 10
    assert ui_kit.bar(-1, 10) == "░" * 10
    assert len(ui_kit.bar(0.37, 8)) == 8


def test_pct_uses_typographic_minus():
    assert ui_kit.pct(0.032) == "+3.2%"
    assert ui_kit.pct(-0.011) == "−1.1%"
    assert "−" in ui_kit.chip(-0.05)


def test_trend_arrow():
    assert ui_kit.trend_arrow(1) == "🟢▲"
    assert ui_kit.trend_arrow(-1) == "🔴▼"
    assert ui_kit.trend_arrow(0) == "⚪▬"


def test_rank_emoji():
    assert ui_kit.rank_emoji(1) == "🥇"
    assert ui_kit.rank_emoji(3) == "🥉"
    assert ui_kit.rank_emoji(4) == "④"
    assert ui_kit.rank_emoji(99) == "99."


def test_card_structure():
    c = ui_kit.card("Title", ["a", "b"], "foot")
    assert c.startswith("*Title*")
    assert "foot" in c


def test_no_religious_terms_in_ui_output():
    # Важно: вывод для юзера без религиозной терминологии.
    blob = (ui_kit.card("📈 Акции", ["row"], "foot") + hs.build_dca_plan(1000, 4, 5))
    for term in ("халя", "харам", "шариат", "ислам", "halal", "haram", "sharia"):
        assert term.lower() not in blob.lower()


# ─────────────────────────── halal_signals (pure) ───────────────────────────
def test_momentum():
    assert hs.momentum([10] * 120 + [12], 100) == pytest.approx(0.2, abs=1e-9)
    assert hs.momentum([1, 2, 3], 100) is None  # недостаточно данных


def test_trend_extension():
    up, ext = hs.trend_extension(list(range(1, 11)), 3)
    assert up is True and ext > 0
    up2, ext2 = hs.trend_extension([10, 9, 8, 7, 6, 5], 3)
    assert up2 is False and ext2 < 0
    assert hs.trend_extension([1, 2], 50) == (None, None)


def test_dca_plan():
    p = hs.build_dca_plan(1200, 4, 5)
    assert "$300.00" in p
    assert p.count("день") == 4
    # клампится
    assert hs.build_dca_plan(1000, 1).count("день") == 2
    assert hs.build_dca_plan(1000, 99).count("день") == 24


# ─────────────────────────── async builders (mock network) ───────────────────────────
def _fake_closes(symbol, rng="1y"):
    # восходящий ряд → всегда «в аптренде» с положительным моментумом
    return [float(i) for i in range(1, 200)]


def test_build_crypto_trend_card_mocked():
    async def _make(s, r="1y"):
        return _fake_closes(s)

    async def run():
        with patch.object(hs, "fetch_closes", side_effect=_make):
            return await hs.build_crypto_trend_card(sma=50, universe=["BTC", "ETH"])

    res = asyncio.new_event_loop().run_until_complete(run())
    out = res.text
    assert "Крипто-тренд" in out
    assert "BTC" in out and "ETH" in out
    # picks — топ-монеты в аптренде для inline-кнопок (могут быть пустыми).
    assert isinstance(res.picks, list)
    assert all(isinstance(s, str) for s in res.picks)
    for term in ("халя", "харам", "ислам", "halal", "haram"):
        assert term not in out.lower()


def test_build_stocks_card_mocked():
    async def _make(s, r="1y"):
        return _fake_closes(s)

    async def run():
        with patch.object(hs, "fetch_closes", side_effect=_make):
            return await hs.build_stocks_card(sma=50, top=3)

    res = asyncio.new_event_loop().run_until_complete(run())
    out = res.text
    assert "Акции" in out
    assert "SMA50" in out
    assert isinstance(res.picks, list)
    assert all(isinstance(s, str) for s in res.picks)
