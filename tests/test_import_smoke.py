"""Smoke-тест импортов: ловит deploy-ломающие ImportError на старте.

Деплой падал, потому что main.py импортировал удалённый при чистке харама символ
(`sniping_callback_data`), а тесты этого не ловили — ни один тест не импортировал
main целиком. Этот тест импортирует ключевые модули верхнего уровня, чтобы любой
сломанный импорт падал в CI, а не на проде.
"""
import importlib

import pytest


@pytest.mark.parametrize("mod", [
    "links",
    "ui_kit",
    "halal_signals",
    "halal_alerts",
    "scheduler",
    "database",
])
def test_module_imports(mod):
    importlib.import_module(mod)


def test_main_imports():
    """main.py должен импортироваться целиком без ImportError (как при старте бота)."""
    importlib.import_module("main")


def test_halal_card_kb_structure():
    """_halal_card_kb строит валидную inline-клавиатуру: пик-кнопки + действия."""
    main = importlib.import_module("main")
    kb = main._halal_card_kb("trend", ["BTC", "ETH"])
    # Должна быть валидная разметка с ≥1 рядом действий.
    rows = kb.inline_keyboard
    assert len(rows) >= 1
    flat = [b for row in rows for b in row]
    texts = [b.text for b in flat]
    # Пик-кнопки на графики (URL).
    assert any("BTC" in t for t in texts)
    assert any(getattr(b, "url", None) for b in flat)
    # Ряд действий: обновить / алерты / навигация (callback_data).
    cbs = [getattr(b, "callback_data", None) for b in flat]
    assert "hsref:trend" in cbs
    assert "hsalert" in cbs
    assert "hsnav:stocks" in cbs


def test_halal_card_kb_no_picks():
    """Без пиков — только ряд действий, без падения."""
    main = importlib.import_module("main")
    kb = main._halal_card_kb("stocks", [])
    cbs = [getattr(b, "callback_data", None) for row in kb.inline_keyboard for b in row]
    assert "hsref:stocks" in cbs
    assert "hsnav:trend" in cbs
