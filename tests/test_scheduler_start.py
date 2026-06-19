"""Regression: scheduler.start() должен построить полный task-list без ошибок.

Деплой падал на NameError (`_alert_liq_enabled`), оставшемся в start() после
чистки харама. Импорт-smoke этого не ловил — ошибка в теле функции, не на импорте.
Здесь мы вызываем start() с пропатченным asyncio.gather (чтобы не уходить в
бесконечные loop'ы) — выполняется весь синхронный код сборки задач, и любая
undefined-name / NameError падает в тесте, а не на проде.
"""
import asyncio

import pytest

import scheduler as S


class _FakeBot:
    async def send_message(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_scheduler_start_builds_tasklist(monkeypatch):
    async def fake_gather(*coros, **kw):
        for c in coros:
            if asyncio.iscoroutine(c):
                c.close()  # не запускаем бесконечные loop'ы
        return []

    monkeypatch.setattr(S.asyncio, "gather", fake_gather)

    sch = S.Scheduler(_FakeBot(), lambda *a, **k: None, lambda *a, **k: None)
    # Не должно бросить NameError/AttributeError при сборке задач.
    await sch.start()
