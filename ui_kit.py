"""ui_kit.py — визуальные помощники для красивых сообщений бота.

Чистый stdlib, без сети. Юникод-спарклайны, бары силы, стрелки и форматирование
чисел. Используется командами /stocks, /trend и др. для аккуратных «карточек».

Принцип: НИКАКОЙ религиозной терминологии в выводе — только рыночные данные.
"""
from __future__ import annotations

from typing import Iterable, Sequence

# Восьмиуровневые блоки для спарклайнов (от низкого к высокому).
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_BAR_FULL = "█"
_BAR_EMPTY = "░"


def sparkline(values: Sequence[float]) -> str:
    """Юникод-спарклайн ряда значений (один символ на точку).

    Пустой/константный ряд → ровная линия. NaN/None отбрасываются.
    """
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return _SPARK_BLOCKS[3] * len(vals)
    span = hi - lo
    out = []
    for v in vals:
        idx = int((v - lo) / span * (len(_SPARK_BLOCKS) - 1) + 0.5)
        out.append(_SPARK_BLOCKS[max(0, min(len(_SPARK_BLOCKS) - 1, idx))])
    return "".join(out)


def bar(frac: float, width: int = 10) -> str:
    """Горизонтальный бар «силы» из блоков. frac клампится в [0, 1]."""
    frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
    filled = int(round(frac * width))
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


def trend_arrow(change: float) -> str:
    """Цветная стрелка по знаку изменения."""
    if change > 0:
        return "🟢▲"
    if change < 0:
        return "🔴▼"
    return "⚪▬"


def pct(change: float, signed: bool = True, decimals: int = 1) -> str:
    """Форматирует долю как проценты с явным знаком: 0.032 → «+3.2%»."""
    v = change * 100.0
    s = f"{v:+.{decimals}f}%" if signed else f"{v:.{decimals}f}%"
    # минус → типографский минус для ровного вида
    return s.replace("-", "−")


def money(x: float, decimals: int = 2) -> str:
    """$-форматирование с разделителями тысяч."""
    return "$" + f"{x:,.{decimals}f}".replace(",", " ")


def chip(change: float) -> str:
    """Компактный «чип»: стрелка + процент. 0.032 → «🟢▲ +3.2%»."""
    return f"{trend_arrow(change)} {pct(change)}"


def card(title: str, rows: Iterable[str], footer: str | None = None) -> str:
    """Собирает аккуратную карточку (Markdown) из заголовка, строк и подвала."""
    parts = [f"*{title}*", ""]
    parts.extend(rows)
    if footer:
        parts.extend(["", footer])
    return "\n".join(parts)


def rank_emoji(i: int) -> str:
    """Медальки для топ-3, кружок-цифры дальше (1-based индекс)."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    if i in medals:
        return medals[i]
    circled = "④⑤⑥⑦⑧⑨⑩"
    return circled[i - 4] if 4 <= i <= 10 else f"{i}."
