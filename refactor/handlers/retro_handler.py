"""/retro — 2-week (или N-day) аудит дайджестов: «были ли мы правы?».

Юзер: «скинуть анализы за 2 недели и спросить клауда были ли анализы правы».

Команда:
  /retro          → 14 дней (default)
  /retro 7        → последние 7 дней
  /retro 30       → последние 30 (capped at 60)

Поток:
  1. core.retro_analysis.collect_retro(days) тянет DIGEST_CACHE.md + цены и
     получает RetroSummary с hits/misses/flats per asset.
  2. build_retro_prompt() формирует структурированный prompt.
  3. AgentProvider.verifier() (gpt-oss / claude-style verifier) даёт аудит.
  4. format_retro_telegram() склеивает финальное сообщение.
"""

from __future__ import annotations

import logging

from aiogram.filters import Command
from aiogram.types import Message

logger = logging.getLogger(__name__)


def _parse_days(message: Message, default: int) -> int:
    parts = (message.text or "").split()
    if len(parts) <= 1:
        return default
    try:
        return max(1, int(parts[1]))
    except ValueError:
        return default


async def handle_retro_command(message: Message) -> None:
    from core.retro_analysis import (
        DEFAULT_RETRO_DAYS,
        MAX_RETRO_DAYS,
        build_retro_prompt,
        collect_retro,
        format_retro_telegram,
    )

    days = min(_parse_days(message, DEFAULT_RETRO_DAYS), MAX_RETRO_DAYS)
    period_label = f"{days} дней" if days != 14 else "2 недели"

    wait_msg = None
    try:
        wait_msg = await message.answer(
            f"📊 Запускаю retro-аудит за {period_label}…\n"
            "_Парсю DIGEST_CACHE.md → тяну исторические цены → агрегирую → "
            "прошу verifier-агента вынести вердикт. ~30-60 сек._",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    try:
        summary = await collect_retro(days=days)
    except Exception as exc:
        logger.exception("retro: collect_retro failed: %s", exc)
        await message.answer(f"⚠️ Не смог собрать retro: `{exc}`", parse_mode="Markdown")
        return

    if summary.days_analyzed == 0 or summary.total_calls == 0:
        await message.answer(
            f"📊 *Retro {period_label}*\n\n"
            f"Не нашёл дайджестов с оцениваемыми прогнозами за {period_label}. "
            "Скорее всего DIGEST_CACHE.md ещё не накопил данных или активы не "
            "распознаются.",
            parse_mode="Markdown",
        )
        return

    try:
        from ai_provider import AgentProvider

        prompt = build_retro_prompt(summary, period_label=period_label)
        sys_msg = (
            "Ты — risk officer и evaluator модели. Отвечай по-русски, "
            "по существу, без льстивой воды. Если hit-rate близок к 50% — "
            "честно говори что это noise, а не edge."
        )
        provider = AgentProvider()
        try:
            audit_text = await provider.verifier(
                prompt=prompt, system=sys_msg, temperature=0.3
            )
        except Exception as agent_err:
            logger.warning("retro: verifier failed, fallback to synth: %s", agent_err)
            audit_text = await provider.synth(
                prompt=prompt, system=sys_msg, temperature=0.3
            )
    except Exception as exc:
        logger.exception("retro: LLM call failed: %s", exc)
        audit_text = (
            "_Не смог получить вердикт от агента — показываю только цифры._\n"
            f"_Причина: `{exc}`_"
        )

    final = format_retro_telegram(summary, audit_text, period_label=period_label)
    # Telegram message limit ~4096 — если перевалит, режем по абзацам.
    if len(final) <= 3900:
        await message.answer(final, parse_mode="Markdown")
    else:
        chunks: list[str] = []
        buf = ""
        for line in final.split("\n"):
            if len(buf) + len(line) + 1 > 3900:
                chunks.append(buf)
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            chunks.append(buf)
        for i, c in enumerate(chunks):
            tag = "" if i == 0 else f"_({i + 1}/{len(chunks)})_\n"
            await message.answer(f"{tag}{c}", parse_mode="Markdown")

    if wait_msg is not None:
        try:
            await wait_msg.delete()
        except Exception:
            pass


def register_retro_handlers(dp) -> None:
    dp.message.register(handle_retro_command, Command("retro"))
