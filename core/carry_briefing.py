"""core/carry_briefing.py — сборка ежедневного брифинга (общий для CLI и планировщика).

Логика «что показать юзеру по шагам» живёт здесь, чтобы её переиспользовали:
  • scripts/daily_briefing.py  (ручной запуск / cron, отправка через HTTP)
  • scheduler.py               (авто-рассылка в боте через bot.send_message)

Формат сообщений — HTML parse_mode, пошагово для новичка (купи спот, шорт перп 1x,
выход по сигналу). Без внешних зависимостей.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.carry_signal import (STRONG, THIN, fetch_basis, fetch_funding,
                                fetch_new_listings, log_opportunities, scan_carry)
from core.regime_radar import format_regime_md, regime_now

logger = logging.getLogger(__name__)


def funding_trade_steps(asset: str, ann: float, capital: float, regime_size: float) -> str:
    """Пошаговая инструкция funding-carry под депозит. Для новичка."""
    deploy = capital * regime_size
    leg = deploy / 2.0
    monthly = leg * ann / 100.0 / 12.0
    sizenote = "" if regime_size >= 1.0 else f" (урезано до {int(regime_size*100)}% из-за режима)"
    return (
        f"💎 <b>СДЕЛКА СЕГОДНЯ: фандинг {asset} +{ann:.0f}% годовых</b>\n"
        f"Депозит ${capital:,.0f}. Делай РОВНО по шагам:\n\n"
        f"1️⃣ Binance → <b>Спот (Spot)</b>, пара {asset}/USDT.\n"
        f"   Купи {asset} на <b>${leg:,.0f}</b> (Buy, тип Market).\n\n"
        f"2️⃣ Binance → <b>Фьючерсы USDⓈ-M</b> → пара {asset}USDT.\n"
        f"   Плечо <b>1x</b> (ровно 1x!). Открой <b>ШОРТ (Sell)</b> на <b>${leg:,.0f}</b>, Market.\n\n"
        f"3️⃣ Готово. Цена тебе ПОФИГ — спот и шорт гасят друг друга.\n"
        f"   Капает фандинг 3 раза в день (03:00, 11:00, 19:00 МСК), ~<b>${monthly:,.0f}/мес</b>{sizenote}.\n\n"
        f"4️⃣ Держи. Упадёт фандинг ниже {THIN:.0f}% — пришлю «ЗАКРЫВАЙ {asset}».\n\n"
        f"5️⃣ По сигналу ЗАКРЫВАЙ: фьючерсы → закрой шорт (Buy/Close); спот → продай {asset}.\n\n"
        f"⚠️ Плечо только 1x. Закрывай ВСЕГДА обе ноги вместе."
    )


def listing_block(listings: list[dict]) -> str:
    if not listings:
        return ""
    lines = ["🆕 <b>НОВЫЕ ЛИСТИНГИ (осторожно!)</b>"]
    for lst in listings[:5]:
        lines.append(f"• <b>{lst['symbol']}</b> (листинг {lst['age_h']:.0f}ч назад)")
    lines.append(
        "\n❌ <b>НЕ покупай на хайпе.</b> Статистика 2020-2026: новый листинг чаще "
        "ПАДАЕТ (медиана −10% за неделю) — толпа закупается, потом слив.\n"
        "Хочешь играть (по желанию, рискованно):\n"
        "1️⃣ Фьючерсы USDⓈ-M → пара листинга → плечо 1x.\n"
        "2️⃣ ШОРТ на МАЛЕНЬКУЮ сумму (макс 2% депозита — изредка листинг улетает вверх).\n"
        "3️⃣ Стоп-лосс ОБЯЗАТЕЛЬНО +15% от входа.\n"
        "4️⃣ Цель: закрыть через 5-7 дней или при −10%.\n"
        "Лучше для новичка — <b>просто не трогать</b>.")
    return "\n".join(lines)


def build_briefing(capital: float) -> tuple[str, dict]:
    """(текст брифинга HTML, состояние открытых carry {asset: ann%})."""
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    parts = [f"🤖 <b>ДНЕВНОЙ БРИФИНГ</b> · {now}\n"]

    reg = regime_now()
    parts.append(format_regime_md(reg))
    size = reg.get("carry_size", 0.7) if reg else 0.7

    fund = fetch_funding()
    pos = [o for o in scan_carry(threshold=THIN, data=fund) if o.positive]
    open_state: dict = {}
    parts.append("\n" + "─" * 20)
    if pos:
        best = pos[0]
        if best.annual_pct >= STRONG:
            parts.append(funding_trade_steps(best.asset, best.annual_pct, capital, size))
        else:
            parts.append(
                f"⚪ <b>Жирных сделок нет</b> (рынок тихий). Лучшее: фандинг "
                f"<b>{best.asset} +{best.annual_pct:.0f}% годовых</b> — скромно.\n"
                f"Можешь поставить МАЛЕНЬКИЙ carry на {best.asset} (шаги те же, сумма меньше) "
                f"или подождать. По-крупному — когда увижу ≥{STRONG:.0f}% и пришлю 💎.")
        open_state = {o.asset: round(o.annual_pct, 1) for o in pos}
        try:
            log_opportunities(pos)
        except Exception:  # noqa: BLE001
            pass
    else:
        parts.append("💤 <b>Сегодня carry-сделок нет</b> — фандинг мал по всем активам. "
                     "Норма в тихом рынке. Сиди в USDT, жди сигнала.")

    try:
        bpos = [b for b in fetch_basis() if b.annual_pct >= 5]
        if bpos:
            bb = bpos[0]
            parts.append(f"\n📐 <b>Базис (для опытных):</b> {bb.symbol} +{bb.annual_pct:.0f}% год — "
                         f"лонг спот {bb.asset} + шорт этот квартальный фьюч (COIN-M). Чище, но сложнее.")
    except Exception:  # noqa: BLE001
        pass

    # КРОСС-БИРЖЕВОЙ арб — то, чего нет на одной бирже. Топ-спред, детали в /arb.
    try:
        from core.cross_exchange import fetch_all, find_spreads
        arb = find_spreads(fetch_all())
        if arb:
            a = arb[0]
            parts.append(f"\n🔀 <b>Кросс-биржевой арб:</b> {a.asset} спред {a.spread:.0f}% год "
                         f"(шорт {a.short_venue} / лонг {a.long_venue}). Жми /arb — детали по шагам.")
    except Exception:  # noqa: BLE001
        pass

    try:
        lb = listing_block(fetch_new_listings(48))
        if lb:
            parts.append("\n" + "─" * 20 + "\n" + lb)
    except Exception:  # noqa: BLE001
        pass

    parts.append("\n<i>Не финсовет. Carry — реальный edge (2020-2026), но не гарантия. "
                 "Плечо 1x, не рискуй больше чем готов потерять.</i>")
    return "\n".join(parts), open_state


def close_alerts(prev_open: dict, cur_open: dict) -> list[str]:
    """Сигналы ЗАКРЫВАЙ: актив был в carry, фандинг упал ниже THIN."""
    msgs = []
    for asset in prev_open:
        if asset not in cur_open:
            msgs.append(
                f"🔔 <b>ЗАКРЫВАЙ {asset}</b>\n"
                f"Фандинг {asset} упал ниже {THIN:.0f}% — carry больше не платит.\n"
                f"1️⃣ Фьючерсы → закрой ШОРТ {asset}USDT (Buy/Close Position).\n"
                f"2️⃣ Спот → продай {asset} в USDT.\n"
                f"Забирай собранный фандинг. 👍")
    return msgs


def arb_close_alerts(prev_open: dict, cur_open: dict, *, min_keep: float = 12.0) -> list[str]:
    """Сигналы ЗАКРЫВАЙ для кросс-арба: спред по активу схлопнулся ниже min_keep.

    prev_open/cur_open: {asset: spread_pct}. Если актив был открыт, а спред упал
    ниже порога (или исчез) → пора закрывать обе ноги (доход больше не покрывает
    косты двух бирж)."""
    msgs = []
    for asset, prev_spread in prev_open.items():
        cur = cur_open.get(asset)
        if cur is None or cur < min_keep:
            now_txt = "исчез" if cur is None else f"упал до {cur:.0f}%"
            msgs.append(
                f"🔔 <b>ЗАКРЫВАЙ АРБ {asset}</b>\n"
                f"Спред фандинга {now_txt} (был {prev_spread:.0f}%) — арб больше не платит.\n"
                f"1️⃣ Закрой ШОРТ перп {asset} на бирже с высоким фандингом.\n"
                f"2️⃣ Закрой ЛОНГ перп {asset} на второй бирже.\n"
                f"Закрывай ОБЕ ноги вместе. Забирай собранный спред. 👍")
    return msgs


__all__ = ["build_briefing", "close_alerts", "arb_close_alerts",
           "funding_trade_steps", "listing_block"]
