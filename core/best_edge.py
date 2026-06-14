"""«Лучшая сделка сейчас» — единый picker лучшего ЖИВОГО delta-neutral edge.

Раньше /signal (кнопка «🎯 Лучшая сделка») выдавал directional LONG/SHORT
price-bet через core.signal_scorer. Бэктест 2020-26 показал, что такие
сигналы робастно убыточны на дневках — поэтому directional УБРАН.

Теперь «Лучшая сделка» = ОДНА с максимальным net annualized % из трёх
проверенных delta-neutral источников (никакого угадывания направления цены):

  • carry — funding carry           (core.carry_signal.scan_carry)
  • arb   — кросс-биржевой спред     (core.cross_exchange.scan)
  • basis — calendar basis           (core.basis_carry.scan)

Если ни один источник не даёт сделку выше порога — честно говорим «сегодня
дельта-нейтрального edge нет, сидим в стейблах». Никаких выдуманных сетапов.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BestEdge:
    kind: str       # "carry" | "arb" | "basis"
    asset: str
    net_apr: float  # годовых, % (net там где источник даёт net)
    headline: str   # ноги сделки одной строкой
    detail: str     # доп. контекст (срок / венчи / фандинг)
    command: str    # куда идти за полным планом: /carry /arb /basis


# ── нормализация каждого источника в BestEdge ────────────────────────────────
def _best_carry(opps: list) -> Optional[BestEdge]:
    if not opps:
        return None
    o = max(opps, key=lambda x: abs(getattr(x, "annual_pct", 0.0)))
    apr = abs(float(getattr(o, "annual_pct", 0.0)))
    if apr <= 0:
        return None
    return BestEdge(
        kind="carry",
        asset=getattr(o, "asset", "?"),
        net_apr=apr,
        headline=getattr(o, "play", f"carry {getattr(o, 'asset', '?')}"),
        detail=f"фандинг-carry, дельта-нейтрально (~{apr:.0f}% годовых)",
        command="/carry",
    )


def _best_arb(opps: list) -> Optional[BestEdge]:
    if not opps:
        return None

    def _net(x) -> float:
        fn = getattr(x, "net_spread", None)
        if callable(fn):
            try:
                return float(fn())
            except Exception:
                pass
        return float(getattr(x, "spread", 0.0))

    o = max(opps, key=_net)
    apr = _net(o)
    if apr <= 0:
        return None
    asset = getattr(o, "asset", "?")
    long_v = getattr(o, "long_venue", "?")
    short_v = getattr(o, "short_venue", "?")
    return BestEdge(
        kind="arb",
        asset=asset,
        net_apr=apr,
        headline=f"ЛОНГ перп {asset} на {long_v} + ШОРТ перп на {short_v}",
        detail=f"кросс-биржевой funding-спред (~{apr:.0f}% годовых net)",
        command="/arb",
    )


def _best_basis(opps: list) -> Optional[BestEdge]:
    if not opps:
        return None
    o = max(opps, key=lambda x: float(getattr(x, "net_annual_pct", 0.0)))
    apr = float(getattr(o, "net_annual_pct", 0.0))
    if apr <= 0:
        return None
    asset = getattr(o, "asset", "?")
    contract = getattr(o, "contract", "?")
    days = int(getattr(o, "days_to_exp", 0) or 0)
    return BestEdge(
        kind="basis",
        asset=asset,
        net_apr=apr,
        headline=f"ЛОНГ спот {asset} + ШОРТ фьюч {contract}",
        detail=f"calendar basis, держим {days}д до экспирации (~{apr:.0f}% годовых net)",
        command="/basis",
    )


def pick_best_edge(
    *, carry_opps: list, arb_opps: list, basis_opps: list,
) -> Optional[BestEdge]:
    """Чистый picker: из топов трёх источников берёт максимум по net_apr."""
    cands = [
        _best_carry(carry_opps),
        _best_arb(arb_opps),
        _best_basis(basis_opps),
    ]
    cands = [c for c in cands if c is not None]
    if not cands:
        return None
    return max(cands, key=lambda c: c.net_apr)


async def scan_best_edge() -> Optional[BestEdge]:
    """Live-скан всех трёх источников (параллельно, с толерантностью к сбоям).

    Каждый scan — синхронный HTTP, гоним в thread. Если источник упал
    (биржа недоступна/гео-блок) — просто пропускаем его, не роняем picker.
    """
    async def _safe(fn) -> list:
        try:
            return await asyncio.to_thread(fn) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("best_edge source failed (%s): %s", getattr(fn, "__name__", fn), e)
            return []

    def _carry() -> list:
        from core.carry_signal import THIN, fetch_funding, scan_carry
        return scan_carry(threshold=THIN, data=fetch_funding())

    def _arb() -> list:
        from core.cross_exchange import scan
        return scan()

    def _basis() -> list:
        from core.basis_carry import scan
        return scan()

    carry_opps, arb_opps, basis_opps = await asyncio.gather(
        _safe(_carry), _safe(_arb), _safe(_basis),
    )
    return pick_best_edge(
        carry_opps=carry_opps, arb_opps=arb_opps, basis_opps=basis_opps,
    )


def format_best_edge(edge: Optional[BestEdge], capital: float = 0.0) -> str:
    """Telegram-сообщение (Markdown). edge=None → честное «сегодня сидим»."""
    if edge is None:
        return (
            "🎯 *Лучшая сделка сейчас*\n\n"
            "Сегодня дельта-нейтрального edge выше порога костов нет — "
            "ни в carry, ни в кросс-арбе, ни в базисе.\n\n"
            "Это нормально: лучше сидеть в стейблах, чем платить комиссии за "
            "тонкую премию. Проверь вручную: /carry · /arb · /basis."
        )

    kind_label = {
        "carry": "Funding carry",
        "arb": "Кросс-биржевой арбитраж",
        "basis": "Calendar basis",
    }.get(edge.kind, edge.kind)

    lines = [
        "🎯 *Лучшая сделка сейчас*",
        "_Лучший живой delta-neutral edge из carry/арб/базис_",
        "",
        f"🏆 *{kind_label}* — *{edge.asset}*",
        f"📈 *~{edge.net_apr:.0f}% годовых* (дельта-нейтрально)",
        "",
        f"*Как:* {edge.headline}",
        f"_{edge.detail}_",
    ]
    if capital and capital > 0:
        est = capital * edge.net_apr / 100.0
        lines.append(f"_На ${capital:,.0f} это ≈ ${est:,.0f}/год при текущей ставке._"
                     .replace(",", " "))
    lines += [
        "",
        f"➡️ Полный план по шагам: {edge.command}",
        "",
        "_Не угадываем направление цены — ставим дельта-нейтрально на ставку/спред. "
        "Это информация, не финансовый совет._",
    ]
    return "\n".join(lines)


__all__ = ["BestEdge", "pick_best_edge", "scan_best_edge", "format_best_edge"]
