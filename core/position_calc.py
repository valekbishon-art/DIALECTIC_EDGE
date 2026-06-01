"""core/position_calc.py — калькулятор дельта-нейтральной позиции (carry/арб).

Юзер вводит депозит → бот считает: размер каждой ноги, ожидаемый доход
(день/мес/год), и сколько «съедят» косты. Для carry (спот+перп на одной бирже)
и для кросс-арба (перп+перп на двух биржах). Без сети — чистая математика.
"""
from __future__ import annotations

from dataclasses import dataclass

# Реалистичные косты round-trip (вход+выход), доля. Carry = 2 ноги, арб = 2 ноги
# на 2 биржах. Берём консервативно по 0.1% на ногу-сторону.
COST_CARRY = 0.004   # спот+перп, вход+выход
COST_ARB = 0.006     # перп+перп на 2 биржах (выше — две площадки)


@dataclass(frozen=True)
class PositionPlan:
    capital: float
    annual_pct: float        # годовая ставка (фандинг или спред)
    leg_usd: float           # размер каждой ноги
    gross_year: float        # доход до костов, $/год
    cost_usd: float          # косты round-trip, $
    net_year: float          # доход после костов, $/год
    net_month: float
    net_day: float
    net_annual_pct: float    # чистая годовая доходность на капитал, %
    breakeven_days: float    # за сколько дней доход покроет косты


def calc_position(capital: float, annual_pct: float, *, kind: str = "carry") -> PositionPlan:
    """Расчёт дельта-нейтральной позиции.

    capital — депозит USD. annual_pct — годовая ставка (фандинг для carry,
    спред для арба), %. kind: 'carry' (спот+перп) | 'arb' (перп+перп 2 биржи).
    Дельта-нейтраль = равный объём, нога = capital/2.
    """
    leg = capital / 2.0
    cost_rate = COST_ARB if kind == "arb" else COST_CARRY
    gross_year = leg * abs(annual_pct) / 100.0
    cost_usd = leg * cost_rate
    net_year = gross_year - cost_usd
    # дней до покрытия костов: cost / (дневной гросс)
    daily_gross = gross_year / 365.0
    breakeven = (cost_usd / daily_gross) if daily_gross > 0 else float("inf")
    return PositionPlan(
        capital=capital, annual_pct=annual_pct, leg_usd=leg,
        gross_year=gross_year, cost_usd=cost_usd, net_year=net_year,
        net_month=net_year / 12.0, net_day=net_year / 365.0,
        net_annual_pct=(net_year / capital * 100.0) if capital else 0.0,
        breakeven_days=breakeven,
    )


def format_calc_md(plan: PositionPlan, *, kind: str = "carry", asset: str = "",
                   venues: str = "") -> str:
    """Telegram HTML — расклад позиции для новичка."""
    leg = plan.leg_usd
    if kind == "arb":
        head = f"🧮 <b>КАЛЬКУЛЯТОР: кросс-арб {asset}</b>"
        legs = (f"1️⃣ ШОРТ перп на бирже с высоким фандингом — <b>${leg:,.0f}</b> (1x)\n"
                f"2️⃣ ЛОНГ перп на бирже с низким — <b>${leg:,.0f}</b> (1x, равный объём)")
        if venues:
            legs += f"\n   ({venues})"
    else:
        head = f"🧮 <b>КАЛЬКУЛЯТОР: carry {asset}</b>"
        legs = (f"1️⃣ ЛОНГ спот {asset or 'актива'} — <b>${leg:,.0f}</b>\n"
                f"2️⃣ ШОРТ перп {asset or ''} — <b>${leg:,.0f}</b> (1x, равный объём)")
    be = ("∞" if plan.breakeven_days == float("inf")
          else f"~{plan.breakeven_days:.0f} дн")
    return (
        f"{head}\n"
        f"Депозит: <b>${plan.capital:,.0f}</b> · ставка <b>{plan.annual_pct:+.0f}% годовых</b>\n\n"
        f"{legs}\n\n"
        f"📈 <b>Доход (после костов):</b>\n"
        f"  • ${plan.net_day:,.2f}/день\n"
        f"  • ${plan.net_month:,.0f}/мес\n"
        f"  • ${plan.net_year:,.0f}/год = <b>{plan.net_annual_pct:.1f}%</b> на депозит\n"
        f"  • косты round-trip ≈ ${plan.cost_usd:,.0f} (окупятся за {be})\n\n"
        f"⚠️ Расчёт — верхняя оценка (гросс минус типовые косты). Реал: спред "
        f"стакана, маржа, проскальзывание. Плечо 1x. Дельта-нейтраль = цена не важна."
    )


__all__ = ["PositionPlan", "calc_position", "format_calc_md", "COST_CARRY", "COST_ARB"]
