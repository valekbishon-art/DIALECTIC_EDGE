"""core/track_record.py — сводка track-record carry/арб возможностей.

Читает carry_opportunities.csv (лог появления возможностей) и даёт честную
сводку: сколько окон поймали, средняя ставка, как часто рынок «жирный». Это
доказательство для продажи: 'за N дней бот нашёл X carry-окон в среднем Y%'.
БЕЗ обещаний прибыли — только факт что edge-окна были и измерены.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "carry_opportunities.csv")


def summarize(path: str = LOG_PATH) -> dict:
    """Сводка по логу возможностей. {} если лога нет/пуст."""
    if not os.path.exists(path):
        return {}
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({
                        "ts": r["ts_utc"], "symbol": r["symbol"],
                        "ann": float(r["annual_pct"]),
                    })
                except (KeyError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    days = {r["ts"][:10] for r in rows}
    anns = [r["ann"] for r in rows]
    by_sym: dict[str, int] = defaultdict(int)
    for r in rows:
        by_sym[r["symbol"]] += 1
    top = sorted(by_sym.items(), key=lambda x: x[1], reverse=True)[:5]
    return {
        "total_windows": len(rows),
        "days_tracked": len(days),
        "first_day": min(days), "last_day": max(days),
        "avg_annual": sum(anns) / len(anns),
        "max_annual": max(anns),
        "windows_per_day": len(rows) / max(len(days), 1),
        "top_assets": top,
    }


def format_track_md(s: dict) -> str:
    """Telegram HTML — track-record сводка."""
    if not s:
        return ("📊 <b>TRACK-RECORD</b>\n"
                "Пока пусто — бот ещё не залогировал carry/арб-окна. "
                "Дай поработать день-другой, потом покажу честную статистику.")
    top = " · ".join(f"{sym} ({n})" for sym, n in s["top_assets"])
    return (
        f"📊 <b>TRACK-RECORD ({s['days_tracked']} дн: {s['first_day']}…{s['last_day']})</b>\n\n"
        f"• Поймано carry/арб-окон: <b>{s['total_windows']}</b> "
        f"(~{s['windows_per_day']:.1f}/день)\n"
        f"• Средняя ставка окна: <b>{s['avg_annual']:.0f}% годовых</b>\n"
        f"• Максимум: {s['max_annual']:.0f}% годовых\n"
        f"• Чаще всего: {top}\n\n"
        f"<i>Это лог найденных edge-окон (не P&L). Честно: бот не обещает прибыль — "
        f"он показывает, что реальные carry/арб-возможности были и измерены.</i>"
    )


__all__ = ["summarize", "format_track_md", "LOG_PATH"]
