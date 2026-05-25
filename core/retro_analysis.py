"""core/retro_analysis.py — 2-week retro: «Claude, were our calls right?».

Юзер: «скинуть анализы за 2 недели и спросить клауда были ли анализы правы».

Идея: тянем последние N дней дайджестов из DIGEST_CACHE.md, по каждому
запускаем существующий `run_post_mortem` (он уже знает как evaluate vs real
price), агрегируем outcomes per asset, формируем prompt для verifier-агента и
просим его честно сказать: «вот что ты говорил, вот что произошло — где ты
ошибся и что подкрутить».

Без сети на module-load. Импорты AgentProvider/PriceFetcher — ленивые.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_RETRO_DAYS = 14
MAX_RETRO_DAYS = 60  # защита от больших prompt'ов


@dataclass
class RetroDayResult:
    """Один день retro: что говорили + что вышло."""

    date: str
    entries: list  # list[PostMortemEntry] — заранее не импортируем, чтобы не плодить циклы


@dataclass
class RetroSummary:
    """Агрегированная статистика 2-week retro."""

    days_analyzed: int
    total_calls: int
    hits: int
    misses: int
    flats: int
    no_data: int
    by_asset: dict[str, dict[str, int]] = field(default_factory=dict)
    daily: list[RetroDayResult] = field(default_factory=list)

    @property
    def hit_rate(self) -> Optional[float]:
        graded = self.hits + self.misses
        if graded == 0:
            return None
        return self.hits / graded


_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})(?:\s+\d{2}:\d{2})?$")


def _parse_digest_date(raw: str) -> Optional[datetime]:
    m = _DATE_RE.match(raw.strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


async def collect_retro(
    days: int = DEFAULT_RETRO_DAYS,
    *,
    digest_cache_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> RetroSummary:
    """Парсит DIGEST_CACHE.md и оценивает каждый дайджест за последние N дней.

    Returns RetroSummary с агрегатом по hit/miss и per-asset breakdown.
    """
    days = max(1, min(days, MAX_RETRO_DAYS))
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).date()

    if digest_cache_path is None:
        digest_cache_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "DIGEST_CACHE.md")
        )

    try:
        with open(digest_cache_path, encoding="utf-8") as f:
            digest_text = f.read()
    except Exception as exc:
        logger.warning("retro: cannot read %s: %s", digest_cache_path, exc)
        return RetroSummary(
            days_analyzed=0,
            total_calls=0,
            hits=0,
            misses=0,
            flats=0,
            no_data=0,
        )

    # Ленивые импорты — не тянем yfinance/aiosqlite на module-load.
    from auto_tracker import DigestParser
    from core.post_mortem import run_post_mortem

    all_digests = DigestParser.extract_all_digests(digest_text)

    # Уникальные даты дайджестов за последние N дней.
    target_dates: list[str] = []
    seen: set[str] = set()
    for d in all_digests:
        raw = d.get("date", "")
        parsed = _parse_digest_date(raw)
        if parsed is None:
            continue
        if parsed.date() < cutoff:
            continue
        date_only = parsed.strftime("%d.%m.%Y")
        if date_only in seen:
            continue
        seen.add(date_only)
        target_dates.append(date_only)

    # Сортируем по дате ↑ (старые → новые) — Claude'у легче следить за хронологией.
    target_dates.sort(
        key=lambda s: _parse_digest_date(s) or datetime.min,
    )

    summary = RetroSummary(
        days_analyzed=0,
        total_calls=0,
        hits=0,
        misses=0,
        flats=0,
        no_data=0,
    )

    for date_str in target_dates:
        try:
            report = await run_post_mortem(
                target_date=date_str,
                digest_cache_text=digest_text,
                write_db=False,  # ретро не пишет в БД — это бот-калибровка уже сделала
                now=now,
            )
        except Exception as exc:
            logger.warning("retro: run_post_mortem(%s) failed: %s", date_str, exc)
            continue
        if report is None or not report.entries:
            continue
        summary.days_analyzed += 1
        summary.daily.append(RetroDayResult(date=date_str, entries=list(report.entries)))
        for e in report.entries:
            asset = e.asset
            outcome = e.outcome
            summary.total_calls += 1
            if outcome in ("hit", "neutral_correct"):
                summary.hits += 1
            elif outcome in ("miss", "neutral_missed"):
                summary.misses += 1
            elif outcome == "flat":
                summary.flats += 1
            else:  # no_data
                summary.no_data += 1
            by = summary.by_asset.setdefault(
                asset, {"hits": 0, "misses": 0, "flats": 0, "no_data": 0}
            )
            if outcome in ("hit", "neutral_correct"):
                by["hits"] += 1
            elif outcome in ("miss", "neutral_missed"):
                by["misses"] += 1
            elif outcome == "flat":
                by["flats"] += 1
            else:
                by["no_data"] += 1

    return summary


def _outcome_emoji(outcome: str) -> str:
    return {
        "hit": "✅",
        "miss": "❌",
        "flat": "⚪",
        "neutral_correct": "✅",
        "neutral_missed": "❌",
        "no_data": "⚠️",
    }.get(outcome, "•")


def build_retro_prompt(summary: RetroSummary, *, period_label: str) -> str:
    """Готовит prompt для verifier-агента (Claude/GPT-OSS).

    Стиль — risk-officer честный аудит, без льстит-маркетинга. Просим:
      • признать честные ошибки,
      • найти системные паттерны (если bull-bias на NEUTRAL рынке — указать),
      • дать конкретные actionable правки.
    """
    lines: list[str] = []
    lines.append(f"# Retro {period_label}: что говорили vs что произошло")
    lines.append("")
    lines.append("## Aggregate")
    lines.append(f"- Days analyzed: {summary.days_analyzed}")
    lines.append(f"- Total calls graded: {summary.total_calls}")
    hr = summary.hit_rate
    if hr is not None:
        lines.append(
            f"- Hits: {summary.hits} / Misses: {summary.misses} "
            f"/ Flats: {summary.flats} / No-data: {summary.no_data}  "
            f"(hit-rate {hr * 100:.1f}%)"
        )
    else:
        lines.append(
            f"- Hits: {summary.hits} / Misses: {summary.misses} "
            f"/ Flats: {summary.flats} / No-data: {summary.no_data}"
        )
    lines.append("")
    if summary.by_asset:
        lines.append("## By asset")
        for asset, d in sorted(summary.by_asset.items()):
            g = d["hits"] + d["misses"]
            rate = (d["hits"] / g * 100) if g else None
            rate_s = f" ({rate:.0f}%)" if rate is not None else ""
            lines.append(
                f"- {asset}: hits {d['hits']} / misses {d['misses']} "
                f"/ flats {d['flats']} / no_data {d['no_data']}{rate_s}"
            )
        lines.append("")

    lines.append("## Per-day")
    # Ограничим до последних 14 дней в prompt'е чтобы не раздуть payload
    days_in_prompt = summary.daily[-14:]
    for day in days_in_prompt:
        lines.append(f"### {day.date}")
        for e in day.entries:
            emoji = _outcome_emoji(e.outcome)
            ret = f"{e.return_pct:+.2f}%" if e.return_pct is not None else "—"
            lines.append(
                f"- {emoji} {e.asset} forecast={e.direction} "
                f"actual={ret} ({e.outcome}) | {e.explanation}"
            )
        lines.append("")

    lines.append("## Task")
    lines.append(
        "Ты — risk officer и evaluator. Не льсти боту. Прочитай выше и ответь "
        "на русском, разделами:"
    )
    lines.append(
        "1. **TL;DR** (2-3 строки): в целом анализы за период были скорее правильными "
        "или нет? hit-rate в контексте baseline 50/50."
    )
    lines.append(
        "2. **Где ошибся** (3-5 пунктов): конкретные паттерны промахов — bull-bias на "
        "падающем рынке, переоценка NEUTRAL когда был тренд, и т.п. Цитируй даты/активы."
    )
    lines.append(
        "3. **Что починить** (2-4 пункта): actionable правки в логику дайджеста "
        "или в формулировки. Никаких общих слов \"улучшить агентов\"."
    )
    lines.append(
        "4. **Дисклеймер юзеру** (1 строка): как бы ты сказал юзеру про track record "
        "так чтобы он не переоценивал бот."
    )
    return "\n".join(lines)


def format_retro_telegram(
    summary: RetroSummary, audit_text: str, *, period_label: str
) -> str:
    """Готовит финальный Telegram-ответ: цифры + Claude's audit + дисклеймер."""
    hr = summary.hit_rate
    hr_line = (
        f"*Hit-rate:* {hr * 100:.1f}%  ({summary.hits}/{summary.hits + summary.misses})"
        if hr is not None
        else "_Недостаточно данных для hit-rate_"
    )
    parts = [
        f"📊 *Retro {period_label} — честный аудит*",
        "",
        f"_Проанализировано дней:_ {summary.days_analyzed}  ·  "
        f"_всего call'ов:_ {summary.total_calls}",
        hr_line,
        "",
        audit_text.strip(),
        "",
        "⚠️ _Это самоаудит модели по реальным ценам. Не финансовый совет. "
        "Прошлые результаты не гарантируют будущих._",
    ]
    return "\n".join(parts)


__all__ = [
    "DEFAULT_RETRO_DAYS",
    "MAX_RETRO_DAYS",
    "RetroDayResult",
    "RetroSummary",
    "build_retro_prompt",
    "collect_retro",
    "format_retro_telegram",
]
