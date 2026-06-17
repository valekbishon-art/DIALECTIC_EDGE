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
