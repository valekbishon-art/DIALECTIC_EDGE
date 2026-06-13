"""
Admin commands backed by current production database statistics.
"""

from __future__ import annotations

import logging
import platform
import sys
from typing import List

from aiogram.types import Message

from config import ADMIN_IDS as CONFIG_ADMIN_IDS
from database import get_admin_stats, get_feedback_stats, get_track_record

logger = logging.getLogger(__name__)

ADMIN_IDS: set[int] = set(CONFIG_ADMIN_IDS)


def register_admin(admin_id: int) -> None:
    ADMIN_IDS.add(admin_id)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


class AdminHandler:
    async def format_stats(self) -> str:
        stats = await get_admin_stats()
        feedback = await get_feedback_stats()
        track = await get_track_record()
        tr_stats = track["stats"]
        wins = tr_stats.get("wins") or 0
        losses = tr_stats.get("losses") or 0
        winrate = (wins / (wins + losses) * 100) if (wins + losses) else 0
        return (
            "<b>📊 Статистика бота</b>\n\n"
            f"<b>Пользователи:</b> {stats['total_users']} | активных 7д: {stats['active_week']}\n"
            f"<b>Подписчики:</b> {stats['subscribers']}\n"
            f"<b>Отчёты:</b> {stats['total_reports']}\n"
            f"<b>Фидбек:</b> +{feedback.get('positive', 0)} / -{feedback.get('negative', 0)}\n"
            f"<b>Track Record:</b> {tr_stats.get('total', 0)} прогнозов | {winrate:.0f}% winrate"
        )

    async def format_health_check(self) -> str:
        stats = await get_admin_stats()
        
        checks = []
        overall = "✅"
        
        # DB check
        try:
            from database import get_pending_predictions
            pending = await get_pending_predictions()
            checks.append("🟢 БД SQLite — OK")
        except Exception as e:
            checks.append(f"🔴 БД SQLite — ERROR: {e}")
            overall = "🔴"
        
        # GitHub connection check
        try:
            from github_export import _github_get, DIGEST_CACHE_FILE, DATA_BRANCH
            content, sha = await _github_get(DIGEST_CACHE_FILE, branch=DATA_BRANCH)
            if not content:
                content, sha = await _github_get(DIGEST_CACHE_FILE)
            if content:
                checks.append("🟢 GitHub connection — OK")
            else:
                checks.append("🟡 GitHub — пустой контент")
        except Exception as e:
            checks.append(f"🔴 GitHub — ERROR: {e}")
            overall = "🔴"
        
        # Last digest check
        import re
        from datetime import datetime, timedelta
        last_digest = "неизвестно"
        try:
            from github_export import _github_get, DIGEST_CACHE_FILE, DATA_BRANCH
            content, _ = await _github_get(DIGEST_CACHE_FILE, branch=DATA_BRANCH)
            if not content:
                content, _ = await _github_get(DIGEST_CACHE_FILE)
            if content:
                time_m = re.search(r"## (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", content)
                if time_m:
                    last_digest = time_m.group(1)
        except:
            pass
        
        checks_text = "\n".join(checks)
        
        return (
            f"{overall} *Health Check*\n\n"
            f"{checks_text}\n\n"
            f"📋 Последний дайджест: {last_digest}\n"
            f"👥 Пользователей: {stats['total_users']} | Активных 7д: {stats['active_week']}\n"
            f"📊 Отчётов: {stats['total_reports']} | Подписчиков: {stats['subscribers']}\n"
            f"🟢 Статус бота: online"
        )

    def get_recent_logs(self) -> str:
        return (
            "<b>📋 Логи</b>\n\n"
            "Локальный refactor-слой не читает файл логов напрямую.\n"
            "Используй stdout/stderr Railway или лог-файл процесса."
        )

    def format_system_info(self) -> str:
        return (
            "<b>🖥️ Системная информация</b>\n\n"
            f"<b>Python:</b> {sys.version.split()[0]}\n"
            f"<b>Platform:</b> {platform.system()} {platform.release()}\n"
            f"<b>Admins loaded:</b> {len(ADMIN_IDS)}"
        )


_admin_handler = AdminHandler()


def get_admin_handler() -> AdminHandler:
    return _admin_handler


async def check_admin(message: Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда доступна только администраторам")
        return False
    return True


async def handle_stats_command(message: Message) -> None:
    if not await check_admin(message):
        return
    await message.answer(await get_admin_handler().format_stats(), parse_mode="HTML")


async def handle_health_command(message: Message) -> None:
    if not await check_admin(message):
        return
    await message.answer(await get_admin_handler().format_health_check(), parse_mode="HTML")


async def handle_logs_command(message: Message) -> None:
    if not await check_admin(message):
        return
    await message.answer(get_admin_handler().get_recent_logs(), parse_mode="HTML")


async def handle_sysinfo_command(message: Message) -> None:
    if not await check_admin(message):
        return
    await message.answer(get_admin_handler().format_system_info(), parse_mode="HTML")


async def format_edge_stats() -> str:
    """Edge-леджер: общий win-rate live-сигналов + win-rate по каждому условию
    формального сертификата (#2/#5). Показывает, какие условия реально несут edge.
    """
    from database import edge_overall_stats, edge_condition_stats

    overall = await edge_overall_stats()
    resolved = overall.get("resolved") or 0
    pending = overall.get("pending") or 0
    if not resolved:
        return (
            "<b>📐 Edge-леджер</b>\n\n"
            f"Резолвнутых сигналов пока нет (pending: {pending}).\n"
            "Включи <code>FEATURE_EDGE_LEDGER=1</code> и подожди, пока сигналы "
            "отыграют горизонт — тогда появится win-rate по условиям."
        )
    tp = overall.get("tp") or 0
    sl = overall.get("sl") or 0
    expired = overall.get("expired") or 0
    avg_pnl = overall.get("avg_pnl") or 0.0
    total_pnl = overall.get("total_pnl") or 0.0
    win_rate = (tp / resolved * 100.0) if resolved else 0.0

    lines = [
        "<b>📐 Edge-леджер (live)</b>\n",
        f"Резолвнуто: <b>{resolved}</b> | pending: {pending}",
        f"TP: {tp} | SL: {sl} | expired: {expired}",
        f"Win-rate (TP): <b>{win_rate:.0f}%</b>",
        f"Avg PnL: {avg_pnl:+.2f}% | Total: {total_pnl:+.2f}%\n",
        "<b>Win-rate по условиям сертификата:</b>",
    ]
    conds = await edge_condition_stats(min_n=1)
    if not conds:
        lines.append("  (нет данных по условиям)")
    else:
        for c in conds:
            lines.append(
                f"  • <code>{c['condition']}</code>: "
                f"{c['win_rate']:.0f}% (n={c['n']}, avg {c['avg_pnl']:+.2f}%)"
            )
        lines.append(
            "\n<i>Условия вверху списка несут больше edge; "
            "внизу — кандидаты на ужесточение/выпил.</i>"
        )
    return "\n".join(lines)


async def handle_edge_command(message: Message) -> None:
    if not await check_admin(message):
        return
    try:
        await message.answer(await format_edge_stats(), parse_mode="HTML")
    except Exception as e:
        logger.exception("edge stats error")
        await message.answer(f"Ошибка edge-статистики: {e}")


def setup_admins(admin_list: List[int]) -> None:
    for admin_id in admin_list:
        register_admin(admin_id)
