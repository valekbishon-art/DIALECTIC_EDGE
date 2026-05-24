"""/postmortem — просмотр каскадных post-mortem'ов.

Подкоманды:
    /postmortem               → последний post-mortem + ссылка на список
    /postmortem list          → таблица последних 10
    /postmortem <id>          → конкретный по id
    /postmortem YYYY-MM-DD    → последний за дату
"""

from __future__ import annotations

import logging
import re

from aiogram.filters import Command
from aiogram.types import Message

from market_indicators.cascade_post_mortem_io import (
    find_cascade_post_mortem_by_date,
    format_post_mortem_full,
    format_post_mortem_list,
    get_cascade_post_mortem_by_id,
    list_recent_cascade_post_mortems,
)

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^\d+$")


def _extract_arg(message: Message) -> str:
    """Возвращает первый аргумент команды (после `/postmortem `)."""
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


async def handle_postmortem_command(message: Message) -> None:
    arg = _extract_arg(message)

    try:
        # list
        if arg.lower() == "list":
            rows = await list_recent_cascade_post_mortems(limit=10)
            text = format_post_mortem_list(rows)
            await message.answer(text, parse_mode="Markdown")
            return

        # by id
        if _ID_RE.match(arg):
            row = await get_cascade_post_mortem_by_id(int(arg))
            if not row:
                await message.answer(f"_Post-mortem #{arg} не найден._", parse_mode="Markdown")
                return
            await message.answer(
                format_post_mortem_full(row),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        # by date
        if _DATE_RE.match(arg):
            row = await find_cascade_post_mortem_by_date(arg)
            if not row:
                await message.answer(
                    f"_За {arg} post-mortem'ов не было._", parse_mode="Markdown"
                )
                return
            await message.answer(
                format_post_mortem_full(row),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
            return

        # default — последний + хвост списка
        rows = await list_recent_cascade_post_mortems(limit=5)
        if not rows:
            await message.answer(
                "_Каскадных post-mortem'ов ещё не было._\n\n"
                "Включи `FEATURE_CASCADE_POST_MORTEM=1` и подожди следующего"
                " каскада ≥$500M.",
                parse_mode="Markdown",
            )
            return
        latest = await get_cascade_post_mortem_by_id(int(rows[0]["id"]))
        full = format_post_mortem_full(latest) if latest else ""
        await message.answer(full, parse_mode="Markdown", disable_web_page_preview=True)
        if len(rows) > 1:
            list_text = format_post_mortem_list(rows[1:])
            await message.answer(list_text, parse_mode="Markdown")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("/postmortem failed: %s", exc)
        await message.answer(
            f"⚠️ Ошибка при чтении post-mortem'ов: `{exc}`",
            parse_mode="Markdown",
        )


def register_postmortem_handlers(dp) -> None:
    dp.message.register(handle_postmortem_command, Command("postmortem"))
