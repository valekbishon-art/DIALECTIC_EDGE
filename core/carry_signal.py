"""core/carry_signal.py — общий модуль FUNDING CARRY edge.

Единственный edge, переживший всё бэктест-расследование (2020-2026): дельта-
нейтральный funding carry (лонг-спот + шорт-перп, сбор фандинга). НЕ прогноз
цены — структурная премия. Работает при годовом фандинге >=20-30%.

Этот модуль — единый источник правды для:
  • scripts/funding_scanner.py  (standalone-сканер + Telegram)
  • best_deal_alert.py          (секция «структурная сделка» в Лучшей сделке)
  • refactor/handlers/*         (строка фандинга в Анализе актива)

Live-источник: Binance fapi premiumIndex (без ключей). Геоблок (451) → VPN.
Без внешних зависимостей (urllib + json).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"

# Пороги из бэктеста (годовых, %).
PRIME = 30.0    # прайм-зона (бэктест: максимум edge)
STRONG = 20.0   # рабочая возможность
THIN = 8.0      # ниже — косты дельта-нейтрала съедают премию

# Ликвидный юниверс по умолчанию (= TRADABLE_ASSETS бота).
DEFAULT_ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "ADA", "DOGE", "AVAX", "LINK", "DOT",
    "TRX", "TON", "LTC", "NEAR", "SUI",
]

# Куда писать лог появления/исчезновения возможностей.
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "carry_opportunities.csv")


@dataclass(frozen=True)
class CarryOpportunity:
    symbol: str          # BTCUSDT
    asset: str           # BTC
    rate: float          # last funding rate за интервал
    interval_h: float    # часов в интервале фандинга (обычно 8)
    annual_pct: float    # аннуализированная ставка, %
    positive: bool       # True = лонги платят (лонг-спот+шорт-перп); False = обратный

    @property
    def play(self) -> str:
        if self.positive:
            return f"ЛОНГ спот {self.asset} + ШОРТ перп {self.symbol}"
        return f"ШОРТ спот {self.asset} + ЛОНГ перп {self.symbol}"


def _get(path: str):
    req = urllib.request.Request(FAPI + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def annualized(rate: float, interval_h: float) -> float:
    """Годовая ставка в % = rate за интервал × (число интервалов в году)."""
    if not interval_h:
        interval_h = 8.0
    return rate * (24.0 / interval_h) * 365.0 * 100.0


def fetch_funding() -> dict:
    """{SYMBOL: {'rate', 'interval_h', 'next'}} по всем USDT-перпам. {} при ошибке."""
    try:
        prem = _get("/fapi/v1/premiumIndex")
    except Exception as e:  # noqa: BLE001
        logger.warning("carry: premiumIndex failed: %s", e)
        return {}
    interval = {}
    try:
        for it in _get("/fapi/v1/fundingInfo"):
            interval[it["symbol"]] = float(it.get("fundingIntervalHours", 8))
    except Exception:  # noqa: BLE001
        pass
    out = {}
    for p in prem:
        sym = p.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            rate = float(p["lastFundingRate"])
        except (KeyError, ValueError, TypeError):
            continue
        out[sym] = {"rate": rate, "interval_h": interval.get(sym, 8.0),
                    "next": p.get("nextFundingTime")}
    return out


def scan_carry(assets: list[str] | None = None, threshold: float = STRONG,
               *, data: dict | None = None) -> list[CarryOpportunity]:
    """Список carry-возможностей >= threshold (или <= -threshold) годовых, по убыванию |ann|.

    assets=None → ликвидный юниверс DEFAULT_ASSETS. data — готовый fetch_funding()
    (чтобы не дёргать сеть дважды). Пустой список = возможностей нет (спим).
    """
    data = data if data is not None else fetch_funding()
    if not data:
        return []
    universe = DEFAULT_ASSETS if assets is None else assets
    opps: list[CarryOpportunity] = []
    for a in universe:
        sym = f"{a}USDT"
        d = data.get(sym)
        if not d:
            continue
        ann = annualized(d["rate"], d["interval_h"])
        if ann >= threshold or ann <= -threshold:
            opps.append(CarryOpportunity(
                symbol=sym, asset=a, rate=d["rate"], interval_h=d["interval_h"],
                annual_pct=ann, positive=ann > 0,
            ))
    opps.sort(key=lambda o: abs(o.annual_pct), reverse=True)
    return opps


DAPI = "https://dapi.binance.com"


def fetch_basis() -> list[CarryOpportunity]:
    """Live годовой базис квартальных COIN-M фьючей (cash-and-carry).

    Лонг спот + шорт квартал, держим до экспирации → PnL = базис на входе (locked-in,
    схождение гарантировано). Базис положителен ~100% времени (бэктест 2022-2026),
    чище funding carry. COIN-M квартальные есть только для мажоров (BTC/ETH/BNB).
    Возвращает list CarryOpportunity (annual_pct = годовой базис), по убыванию.
    """
    import datetime as _dt
    out: list[CarryOpportunity] = []
    try:
        prices = {p["symbol"]: float(p["price"]) for p in _get_dapi("/dapi/v1/ticker/price")}
        idx = {p["symbol"]: float(p["indexPrice"])
               for p in _get_dapi("/dapi/v1/premiumIndex") if "indexPrice" in p}
    except Exception as e:  # noqa: BLE001
        logger.warning("carry: basis fetch failed: %s", e)
        return []
    now = _dt.datetime.now(_dt.timezone.utc)
    for sym, pr in prices.items():
        if "_" not in sym or "PERP" in sym:
            continue
        base, exp = sym.split("_", 1)
        ip = idx.get(f"{base}_PERP") or idx.get(sym)
        if not ip or ip <= 0 or not exp.isdigit():
            continue
        try:
            ed = _dt.datetime.strptime(exp, "%y%m%d").replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
        dte = max((ed - now).days, 1)
        ann = (pr / ip - 1.0) * 365.0 / dte * 100.0
        asset = base[:-3] if base.endswith("USD") else base
        out.append(CarryOpportunity(symbol=sym, asset=asset, rate=pr / ip - 1.0,
                                    interval_h=float(dte * 24), annual_pct=ann, positive=ann > 0))
    out.sort(key=lambda o: o.annual_pct, reverse=True)
    return out


def _get_dapi(path: str):
    req = urllib.request.Request(DAPI + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def get_asset_funding_annualized(asset: str, *, data: dict | None = None) -> float | None:
    """Годовой фандинг одного актива (%), или None если нет данных. Для Анализа."""
    data = data if data is not None else fetch_funding()
    d = data.get(f"{asset.upper()}USDT")
    if not d:
        return None
    return annualized(d["rate"], d["interval_h"])


def fetch_new_listings(hours: int = 48) -> list[dict]:
    """Новые перпы Binance за последние `hours` часов. Для листинг-фейд алерта.

    Бэктест: медиана нового листинга падает ~10% за неделю (retail FOMO → слив),
    но среднее ≈0 (редкие 'луны' съедают шорт) → фейд ТОЛЬКО с жёстким риск-контролем.
    Возвращает [{'symbol','asset','onboard_ms','age_h'}], свежайшие первыми.
    """
    import time as _t
    try:
        req = urllib.request.Request(f"{FAPI}/fapi/v1/exchangeInfo",
                                     headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:  # noqa: BLE001
        logger.warning("carry: listings fetch failed: %s", e)
        return []
    now = _t.time() * 1000
    out = []
    for s in data.get("symbols", []):
        sym = s.get("symbol", "")
        od = s.get("onboardDate")
        if not sym.endswith("USDT") or not od:
            continue
        age_h = (now - int(od)) / 3.6e6
        if 0 <= age_h <= hours:
            out.append({"symbol": sym, "asset": sym[:-4],
                        "onboard_ms": int(od), "age_h": round(age_h, 1)})
    out.sort(key=lambda x: x["age_h"])
    return out


def carry_verdict(ann: float | None) -> str:
    """Короткий ярлык по годовому фандингу — для строки в Анализе."""
    if ann is None:
        return ""
    if ann >= PRIME:
        return f"🟢🟢 carry PRIME (+{ann:.0f}% год — лонг-спот+шорт-перп)"
    if ann >= STRONG:
        return f"🟢 carry-возможность (+{ann:.0f}% год — лонг-спот+шорт-перп)"
    if ann <= -STRONG:
        return f"🔵 обратный carry ({ann:.0f}% год — шорт-спот+лонг-перп)"
    if ann >= THIN:
        return f"⚪ фандинг +{ann:.0f}% год (тонко для carry)"
    if ann < 0:
        return f"🔴 фандинг {ann:.0f}% год (отрицательный)"
    return f"· фандинг +{ann:.0f}% год (carry спит)"


# ──────────────────────── LP-оптимизация аллокации (#4) ─────────────────────
# Текущая выдача — «равный объём по топ-3»: игнорирует и разницу в доходности,
# и косты дельта-нейтрала. Ниже — оптимизатор капитала по carry-возможностям.
#
# Задача (LP): max Σ wᵢ·net_annualᵢ  при  Σ wᵢ ≤ capital,  0 ≤ wᵢ ≤ capᵢ.
# Для бюджет+box-ограничений оптимум LP = жадное наполнение по убыванию net-
# доходности до лимита на актив (доказуемо; scipy не нужен — нулевые зависимости,
# как и весь модуль). cap ограничивает концентрацию (риск дельта-нейтрала: флип
# фандинга, проскальзывание/ликвидация ноги, делистинг спота). Косты round-trip
# аннуализируются и вычитаются — тонкие возможности отсекаются порогом.

# Дефолты костов дельта-нейтрала.
CARRY_ROUNDTRIP_COST_PCT = 0.30   # % оборота: открыть+закрыть спот+перп (~taker)
CARRY_HOLDING_DAYS = 30.0         # типичный holding до нормализации фандинга
CARRY_MAX_WEIGHT = 0.25           # лимит доли капитала на одну сделку (концентрация)


@dataclass(frozen=True)
class CarryAllocation:
    opp: CarryOpportunity
    capital_usd: float        # капитал на сделку (делится на 2 ноги равного нотионала)
    gross_annual_pct: float   # |annual| (до костов)
    net_annual_pct: float     # после аннуализированных round-trip костов
    weight: float             # доля портфеля (capital_usd / total_capital)

    @property
    def net_year_usd(self) -> float:
        # фандинг собирается с шорт-перп ноги (нотионал = capital_usd/2)
        return (self.capital_usd / 2.0) * self.net_annual_pct / 100.0


def _annualized_cost(roundtrip_cost_pct: float, holding_days: float) -> float:
    """Round-trip кост → годовых %. 0.30% за 30д ≈ 3.65%/год."""
    if holding_days <= 0:
        return 0.0
    return roundtrip_cost_pct / holding_days * 365.0


def optimize_carry_allocation(
    opps: list[CarryOpportunity],
    capital: float,
    *,
    max_weight: float = CARRY_MAX_WEIGHT,
    roundtrip_cost_pct: float = CARRY_ROUNDTRIP_COST_PCT,
    holding_days: float = CARRY_HOLDING_DAYS,
    min_net_annual: float = THIN,
    risk_caps: dict[str, float] | None = None,
) -> dict:
    """Оптимальная аллокация капитала по carry-возможностям.

    Возвращает dict:
      allocations: list[CarryAllocation]  — по убыванию net-доходности
      total_allocated, n_legs
      port_gross_annual_pct, port_net_annual_pct  — взвешенный портфельный yield
      port_net_year_usd, port_net_month_usd
      baseline_net_year_usd  — «равный объём по топ-3» (текущая логика)
      uplift_pct             — прирост net $/год оптимизатора над baseline
    """
    cost_annual = _annualized_cost(roundtrip_cost_pct, holding_days)
    # 1. net-доходность и фильтр по порогу.
    cands = []
    for o in opps:
        net = abs(o.annual_pct) - cost_annual
        if net >= min_net_annual:
            cands.append((o, net))
    cands.sort(key=lambda x: x[1], reverse=True)

    # 2. жадное наполнение (= LP-оптимум) с лимитом на актив.
    allocations: list[CarryAllocation] = []
    remaining = float(capital)
    for o, net in cands:
        if remaining <= 0:
            break
        cap_i = (risk_caps or {}).get(o.asset, max_weight) * capital
        w_usd = min(cap_i, remaining)
        if w_usd <= 0:
            continue
        allocations.append(CarryAllocation(
            opp=o, capital_usd=round(w_usd, 2),
            gross_annual_pct=abs(o.annual_pct),
            net_annual_pct=round(net, 2),
            weight=round(w_usd / capital, 4) if capital else 0.0,
        ))
        remaining -= w_usd

    total_alloc = sum(a.capital_usd for a in allocations)
    net_year = sum(a.net_year_usd for a in allocations)
    gross_year = sum((a.capital_usd / 2.0) * a.gross_annual_pct / 100.0
                     for a in allocations)
    # портфельный yield в % годовых (на задействованный капитал).
    port_net = (net_year / (total_alloc / 2.0) * 100.0) if total_alloc else 0.0
    port_gross = (gross_year / (total_alloc / 2.0) * 100.0) if total_alloc else 0.0

    # 3. baseline = «равный объём» по ТЕМ ЖЕ отобранным ногам на ТОМ ЖЕ
    #    задействованном капитале. Это честное сравнение: изолирует пользу
    #    yield-взвешивания против равного веса при одинаковом риск-бюджете.
    #    (Прирост скромный и растёт с разбросом доходностей и шириной лимита;
    #    при cap = 1/n_legs аллокации совпадают → uplift = 0.)
    base_net_year = _equal_weight_baseline(allocations, cost_annual)
    uplift = ((net_year - base_net_year) / base_net_year * 100.0) if base_net_year > 0 else 0.0

    return {
        "allocations": allocations,
        "total_allocated": round(total_alloc, 2),
        "n_legs": len(allocations),
        "port_gross_annual_pct": round(port_gross, 2),
        "port_net_annual_pct": round(port_net, 2),
        "port_net_year_usd": round(net_year, 2),
        "port_net_month_usd": round(net_year / 12.0, 2),
        "baseline_net_year_usd": round(base_net_year, 2),
        "uplift_pct": round(uplift, 1),
        "cost_annual_pct": round(cost_annual, 2),
    }


def _equal_weight_baseline(allocations: list, cost_annual: float) -> float:
    """Net $/год при равном объёме по тем же ногам и том же капитале.

    Берём задействованный оптимизатором капитал, делим поровну между
    выбранными ногами и считаем net $/год по их net-доходностям. Так uplift
    отражает чистый вклад yield-взвешивания (а не разницу в наборе/риске).
    """
    if not allocations:
        return 0.0
    deployed = sum(a.capital_usd for a in allocations)
    n = len(allocations)
    per = deployed / n
    total = 0.0
    for a in allocations:
        total += (per / 2.0) * a.net_annual_pct / 100.0
    return total


def format_carry_allocation_md(plan: dict, capital: float) -> str:
    """Markdown-секция оптимизированной аллокации carry."""
    allocs = plan.get("allocations") or []
    if not allocs:
        return ""
    lines = [
        "",
        "💎 *СТРУКТУРНАЯ СДЕЛКА (funding carry — оптимизированная аллокация):*",
    ]
    for a in allocs:
        o = a.opp
        leg = a.capital_usd / 2.0
        lines.append(
            f"• *{o.symbol}* {o.annual_pct:+.0f}% год (net {a.net_annual_pct:+.0f}%) — "
            f"{o.play}\n"
            f"  ${a.capital_usd:,.0f} ({a.weight*100:.0f}%) · по ${leg:,.0f}/ногу · "
            f"~${a.net_year_usd/12:,.0f}/мес net"
        )
    lines.append(
        f"_Портфель: net {plan['port_net_annual_pct']:.0f}%/год · "
        f"~${plan['port_net_month_usd']:,.0f}/мес · {plan['n_legs']} ног · "
        f"+{plan['uplift_pct']:.0f}% к равному объёму. "
        f"Дельта-нейтрал: цена захеджирована, собираешь фандинг. "
        f"Косты {plan['cost_annual_pct']:.0f}%/год учтены. Маржу/ликвидность проверь руками._"
    )
    return "\n".join(lines)


def format_carry_block_md(opps: list[CarryOpportunity], capital: float = 0.0) -> str:
    """Markdown-секция для Лучшей сделки. '' если возможностей нет."""
    if not opps:
        return ""
    lines = ["", "💎 *СТРУКТУРНАЯ СДЕЛКА (funding carry — единственный +edge в бэктесте):*"]
    for o in opps[:3]:
        lines.append(f"• *{o.symbol}* {o.annual_pct:+.0f}% годовых — {o.play} (равный объём)")
        if capital > 0:
            n = capital / 2.0
            lines.append(f"  под ${capital:,.0f}: по ${n:,.0f}/ногу · ~${n*abs(o.annual_pct)/100/12:,.0f}/мес гросс")
    lines.append("_Дельта-нейтрал: цена захеджирована, собираешь фандинг. "
                 "Выход когда нормализуется. Проверь ликвидность/маржу руками._")
    return "\n".join(lines)


def log_opportunities(opps: list[CarryOpportunity]) -> None:
    """Дописывает в CSV факт появления/исчезновения возможностей (для сверки edge).

    Пишет снимок текущих возможностей одной строкой на символ. Non-fatal.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            if new:
                f.write("ts_utc,symbol,annual_pct,rate,interval_h,side\n")
            for o in opps:
                side = "long_spot_short_perp" if o.positive else "short_spot_long_perp"
                f.write(f"{ts},{o.symbol},{o.annual_pct:.2f},{o.rate:.8f},{o.interval_h:.0f},{side}\n")
    except Exception:  # noqa: BLE001
        logger.debug("carry log_opportunities skipped", exc_info=True)


__all__ = [
    "CarryOpportunity", "scan_carry", "fetch_funding", "fetch_basis", "annualized",
    "get_asset_funding_annualized", "carry_verdict", "format_carry_block_md",
    "log_opportunities", "fetch_new_listings", "DEFAULT_ASSETS", "PRIME", "STRONG", "THIN",
    "CarryAllocation", "optimize_carry_allocation", "format_carry_allocation_md",
    "CARRY_MAX_WEIGHT", "CARRY_ROUNDTRIP_COST_PCT", "CARRY_HOLDING_DAYS",
]
