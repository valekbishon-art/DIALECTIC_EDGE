"""core/basis_carry.py — CALENDAR BASIS CARRY (cash-and-carry), живой сигнал.

Второй edge, переживший бэктест-расследование (см. scripts/research_basis_carry):
дельта-нейтраль ЛОНГ спот + ШОРТ квартальный фьюч, держим ДО экспирации.

Почему это edge, а не прогноз: на экспирации фьюч сходится к споту (F→S)
ГАРАНТИРОВАННО. Значит запертый PnL = базис на входе, путь цены не важен.
Годовой базис на входе = реализованный carry. Положителен ~100% времени в
contango, ЖИВ в 2025-26 (где funding carry уснул). Скромный (single/low-double
% годовых), НО структурный и не обнуляется.

Live-источник: Binance USDⓈ-M квартальные (BTCUSDT_YYMMDD, в USDT → напрямую
сравнимы со спотом BTCUSDT) + спот. Без ключей. Дата экспирации парсится из
символа (YYMMDD), days_to_exp считается на лету.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0"}
FAPI = "https://fapi.binance.com"
SPOT = "https://api.binance.com"

# Пороги НЕТТО годового базиса (%, после костов). Торгуемая метрика — нетто:
# косты cash-and-carry фиксированы за round-trip, на коротком сроке аннуализиру-
# ются крупно и могут съесть весь gross. Поэтому сигналим и ранжируем по нетто.
THIN = 1.5      # ниже — не стоит возни (после костов почти ноль)
STRONG = 4.0    # рабочая возможность
PRIME = 7.0     # жирно для базиса

# Round-trip косты cash-and-carry, доля: открыть 2 ноги + закрыть/экспирация 2.
COST_ROUNDTRIP = 0.004

# Активы с ликвидными квартальными фьючами на Binance USDⓈ-M.
DEFAULT_ASSETS = ("BTC", "ETH")

# Окно фронт-квартала: контракт в этом коридоре дней до экспирации торгуем.
MIN_DAYS = 10     # ближе к экспирации — шум схождения, базис уже почти ноль
MAX_DAYS = 130    # дальше фронт-квартала — стейл/неликвид


@dataclass(frozen=True)
class BasisOpportunity:
    asset: str            # BTC
    contract: str         # BTCUSDT_260626
    spot: float
    future: float
    days_to_exp: int
    expiry: str           # 2026-06-26 (ISO)

    @property
    def annual_pct(self) -> float:
        """Годовой базис = реализованный carry если держать до экспирации."""
        if self.spot <= 0 or self.days_to_exp <= 0:
            return 0.0
        return (self.future / self.spot - 1.0) * 365.0 / self.days_to_exp * 100.0

    @property
    def net_annual_pct(self) -> float:
        """Нетто после костов (косты аннуализируются на срок удержания)."""
        return self.annual_pct - COST_ROUNDTRIP * 100.0 * 365.0 / max(self.days_to_exp, 1)


def _get(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _parse_expiry(contract: str) -> date | None:
    """BTCUSDT_260626 → date(2026, 6, 26). None если не квартальный символ."""
    if "_" not in contract:
        return None
    suffix = contract.rsplit("_", 1)[1]
    if len(suffix) != 6 or not suffix.isdigit():
        return None  # PERP/NEXT_QUARTER и пр. — пропускаем
    try:
        return date(2000 + int(suffix[:2]), int(suffix[2:4]), int(suffix[4:6]))
    except ValueError:
        return None


def fetch_spot(assets=DEFAULT_ASSETS) -> dict:
    """{ASSET: spot_usdt}."""
    out = {}
    try:
        for t in _get(f"{SPOT}/api/v3/ticker/price"):
            s = t.get("symbol", "")
            if s.endswith("USDT") and s[:-4] in assets:
                try:
                    out[s[:-4]] = float(t["price"])
                except (KeyError, ValueError):
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning("basis spot: %s", e)
    return out


def fetch_quarterly(assets=DEFAULT_ASSETS) -> list[tuple]:
    """[(asset, contract, future_price, expiry_date)] по квартальным USDⓈ-M."""
    out = []
    try:
        for t in _get(f"{FAPI}/fapi/v1/ticker/price"):
            sym = t.get("symbol", "")
            if "_" not in sym:
                continue  # бессрочные не нужны
            base = sym.split("_", 1)[0]
            if not base.endswith("USDT") or base[:-4] not in assets:
                continue
            exp = _parse_expiry(sym)
            if exp is None:
                continue
            try:
                out.append((base[:-4], sym, float(t["price"]), exp))
            except (KeyError, ValueError):
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("basis quarterly: %s", e)
    return out


def _today() -> date:
    return datetime.now(timezone.utc).date()


def find_basis(spot: dict, quarterly: list[tuple], *, min_net: float = THIN,
               today: date | None = None) -> list[BasisOpportunity]:
    """Basis-возможности с НЕТТО годовым базисом >= min_net.

    На каждый актив берём контракт с лучшим НЕТТО (после костов) в окне
    [MIN_DAYS, MAX_DAYS] — а не просто ближайший: дальний контракт часто
    выгоднее по нетто, т.к. фикс-косты размазываются на больший срок.
    """
    ref = today or _today()
    best: dict[str, BasisOpportunity] = {}
    for asset, contract, fut, exp in quarterly:
        s = spot.get(asset)
        if not s or s <= 0:
            continue
        dte = (exp - ref).days
        if dte < MIN_DAYS or dte > MAX_DAYS:
            continue
        opp = BasisOpportunity(asset=asset, contract=contract, spot=s, future=fut,
                               days_to_exp=dte, expiry=exp.isoformat())
        prev = best.get(asset)
        if prev is None or opp.net_annual_pct > prev.net_annual_pct:
            best[asset] = opp
    opps = [o for o in best.values() if o.net_annual_pct >= min_net]
    opps.sort(key=lambda o: o.net_annual_pct, reverse=True)
    return opps


def scan(min_net: float = THIN) -> list[BasisOpportunity]:
    return find_basis(fetch_spot(), fetch_quarterly(), min_net=min_net)


def format_basis_md(opps: list[BasisOpportunity], capital: float = 0.0) -> str:
    """Telegram HTML — пошагово для новичка. '' нет → подсказка-заглушка."""
    if not opps:
        return ("🗓 <b>CALENDAR BASIS CARRY</b>\n"
                "Сейчас фронт-базис тонкий (фьюч близко к споту) — после костов "
                "не стоит входа. Это норма в спокойном контанго. Базис расширяется "
                "в перегретом рынке (на хайпе фьюч уходит в премию) — тогда жди сигнал.")
    lines = ["🗓 <b>CALENDAR BASIS CARRY</b> (cash-and-carry, держим до экспирации)\n"]
    for o in opps[:3]:
        leg = f", на ${capital/2:,.0f}" if capital else ""
        lines.append(
            f"💠 <b>{o.asset}: {o.annual_pct:.1f}% годовых</b> "
            f"(нетто ~{o.net_annual_pct:.1f}%) · контракт {o.contract}\n"
            f"Спот ${o.spot:,.0f} / фьюч ${o.future:,.0f} · до экспирации {o.days_to_exp} дн ({o.expiry})\n"
            f"1️⃣ ЛОНГ спот {o.asset}{leg} (купить и держать)\n"
            f"2️⃣ ШОРТ квартальный фьюч {o.contract}{leg} (равный объём, 1x)\n"
            f"3️⃣ Держи до экспирации — фьюч сойдётся к споту, заберёшь базис. Путь цены не важен.\n")
    lines.append("✅ Базис на входе ЗАПЕРТ (схождение F→S на экспирации гарантировано). "
                 "Это структурный edge, не прогноз. Риск: фандинг по шорт-ноге и маржа — "
                 "держи буфер. Косты 4 сделок учтены в нетто.")
    return "\n".join(lines)


__all__ = ["BasisOpportunity", "fetch_spot", "fetch_quarterly", "find_basis",
           "scan", "format_basis_md", "THIN", "STRONG", "PRIME"]
