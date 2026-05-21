"""Pure P2P arbitrage math and formatting.

The module is intentionally exchange-agnostic and has no Telegram/HTTP imports.
Network adapters live in handlers/providers and feed normalized ``P2PAdvert``
rows here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


DEFAULT_P2P_ASSETS = ("USDT",)
DEFAULT_P2P_FIATS = ("RUB",)
DEFAULT_MIN_SPREAD_PCT = 1.0
DEFAULT_SETTLEMENT_BUFFER_PCT = 0.35
DEFAULT_MIN_COMPLETION_RATE_PCT = 90.0
DEFAULT_MIN_ORDERS = 50
DEFAULT_MAX_RESULTS = 5


@dataclass(frozen=True)
class P2PAdvert:
    venue: str
    trade_type: str
    asset: str
    fiat: str
    price: float
    min_amount_fiat: float
    max_amount_fiat: float
    available_asset: float | None = None
    payment_methods: tuple[str, ...] = ()
    advertiser: str = "unknown"
    completed_orders: int | None = None
    completion_rate_pct: float | None = None
    is_merchant: bool = False
    advert_id: str = ""

    @property
    def side_label(self) -> str:
        if self.trade_type.upper() == "BUY":
            return "buy USDT"
        if self.trade_type.upper() == "SELL":
            return "sell USDT"
        return self.trade_type.upper()


@dataclass(frozen=True)
class P2POpportunity:
    asset: str
    fiat: str
    buy_ad: P2PAdvert
    sell_ad: P2PAdvert
    gross_spread_pct: float
    buffer_pct: float
    net_spread_pct: float
    executable_fiat: float
    executable_asset: float
    shared_payment_methods: tuple[str, ...]
    risk_level: str
    warnings: tuple[str, ...] = ()

    @property
    def gross_profit_fiat(self) -> float:
        return self.executable_asset * (self.sell_ad.price - self.buy_ad.price)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    values = []
    seen = set()
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        key = item.upper()
        if key in seen:
            continue
        seen.add(key)
        values.append(item.upper())
    return tuple(values) or default


def _env_float(name: str, default: float, *, min_val: float, max_val: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < min_val or value > max_val:
        return default
    return value


def _env_int(name: str, default: int, *, min_val: int, max_val: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < min_val or value > max_val:
        return default
    return value


def feature_enabled() -> bool:
    return _env_bool("FEATURE_P2P_ARBITRAGE", False)


def get_assets() -> tuple[str, ...]:
    return _env_csv("P2P_ARBITRAGE_ASSETS", DEFAULT_P2P_ASSETS)


def get_fiats() -> tuple[str, ...]:
    return _env_csv("P2P_ARBITRAGE_FIATS", DEFAULT_P2P_FIATS)


def get_pay_types() -> tuple[str, ...]:
    raw = os.getenv("P2P_ARBITRAGE_PAY_TYPES", "").strip()
    if not raw:
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        item = normalize_payment_method(part)
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            values.append(item)
    return tuple(values)


def get_min_spread_pct() -> float:
    return _env_float("P2P_ARBITRAGE_MIN_SPREAD_PCT", DEFAULT_MIN_SPREAD_PCT, min_val=0.0, max_val=50.0)


def get_settlement_buffer_pct() -> float:
    return _env_float(
        "P2P_ARBITRAGE_SETTLEMENT_BUFFER_PCT",
        DEFAULT_SETTLEMENT_BUFFER_PCT,
        min_val=0.0,
        max_val=10.0,
    )


def get_min_completion_rate_pct() -> float:
    return _env_float(
        "P2P_ARBITRAGE_MIN_COMPLETION_RATE_PCT",
        DEFAULT_MIN_COMPLETION_RATE_PCT,
        min_val=0.0,
        max_val=100.0,
    )


def get_min_orders() -> int:
    return _env_int("P2P_ARBITRAGE_MIN_ORDERS", DEFAULT_MIN_ORDERS, min_val=0, max_val=100_000)


def get_max_results() -> int:
    return _env_int("P2P_ARBITRAGE_MAX_RESULTS", DEFAULT_MAX_RESULTS, min_val=1, max_val=20)


def merchant_only() -> bool:
    return _env_bool("P2P_ARBITRAGE_MERCHANT_ONLY", False)


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        text = str(value).replace(",", "").strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    num = _to_float(value)
    if num is None:
        return None
    return int(num)


def _completion_rate_pct(value: Any) -> float | None:
    rate = _to_float(value)
    if rate is None:
        return None
    if 0 <= rate <= 1:
        return rate * 100
    if 1 < rate <= 100:
        return rate
    return None


def normalize_payment_method(value: Any) -> str:
    return str(value or "").strip()


def parse_binance_ad(row: dict[str, Any], *, trade_type: str, asset: str, fiat: str) -> P2PAdvert | None:
    """Parse one Binance P2P advertisement row into a normalized advert."""
    try:
        adv = row.get("adv") or {}
        advertiser = row.get("advertiser") or {}
        price = _to_float(adv.get("price"))
        min_amount = _to_float(adv.get("minSingleTransAmount"))
        max_amount = _to_float(adv.get("maxSingleTransAmount"))
        if price is None or price <= 0 or min_amount is None or max_amount is None:
            return None
        methods: list[str] = []
        for method in adv.get("tradeMethods") or []:
            if not isinstance(method, dict):
                continue
            name = (
                method.get("identifier")
                or method.get("tradeMethodName")
                or method.get("name")
            )
            normalized = normalize_payment_method(name)
            if normalized:
                methods.append(normalized)
        user_type = str(advertiser.get("userType") or "").lower()
        return P2PAdvert(
            venue="Binance P2P",
            trade_type=trade_type.upper(),
            asset=asset.upper(),
            fiat=fiat.upper(),
            price=price,
            min_amount_fiat=max(0.0, min_amount),
            max_amount_fiat=max(0.0, max_amount),
            available_asset=_to_float(adv.get("surplusAmount")),
            payment_methods=tuple(dict.fromkeys(methods)),
            advertiser=str(advertiser.get("nickName") or "unknown"),
            completed_orders=_to_int(
                advertiser.get("monthOrderCount")
                or advertiser.get("monthFinishOrderCount")
                or advertiser.get("userStatsRet", {}).get("completedOrderNum")
            ),
            completion_rate_pct=_completion_rate_pct(
                advertiser.get("monthFinishRate")
                or advertiser.get("positiveRate")
                or advertiser.get("userStatsRet", {}).get("completionRate")
            ),
            is_merchant=user_type in {"merchant", "block"} or bool(advertiser.get("isMerchant")),
            advert_id=str(adv.get("advNo") or ""),
        )
    except Exception:
        return None


def passes_quality_filter(
    ad: P2PAdvert,
    *,
    min_completion_rate_pct: float,
    min_orders: int,
    merchant_required: bool,
) -> bool:
    if merchant_required and not ad.is_merchant:
        return False
    if min_orders and (ad.completed_orders or 0) < min_orders:
        return False
    if min_completion_rate_pct and (ad.completion_rate_pct or 0.0) < min_completion_rate_pct:
        return False
    return True


def _payment_intersection(
    buy_ad: P2PAdvert,
    sell_ad: P2PAdvert,
    preferred_pay_types: tuple[str, ...],
) -> tuple[str, ...]:
    buy = {p.lower(): p for p in buy_ad.payment_methods}
    sell = {p.lower(): p for p in sell_ad.payment_methods}
    if preferred_pay_types:
        preferred = {p.lower(): p for p in preferred_pay_types}
        keys = set(buy) & set(sell) & set(preferred)
    else:
        keys = set(buy) & set(sell)
    return tuple(sorted((buy.get(k) or sell.get(k) or k) for k in keys))


def _executable_fiat(buy_ad: P2PAdvert, sell_ad: P2PAdvert) -> float:
    max_fiat = min(buy_ad.max_amount_fiat, sell_ad.max_amount_fiat)
    if buy_ad.available_asset is not None:
        max_fiat = min(max_fiat, buy_ad.available_asset * buy_ad.price)
    if sell_ad.available_asset is not None:
        max_fiat = min(max_fiat, sell_ad.available_asset * sell_ad.price)
    min_fiat = max(buy_ad.min_amount_fiat, sell_ad.min_amount_fiat)
    return max_fiat if max_fiat >= min_fiat else 0.0


def _risk_level(buy_ad: P2PAdvert, sell_ad: P2PAdvert, shared_methods: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    warnings: list[str] = []
    min_orders = min(buy_ad.completed_orders or 0, sell_ad.completed_orders or 0)
    rates = [r for r in (buy_ad.completion_rate_pct, sell_ad.completion_rate_pct) if r is not None]
    min_rate = min(rates) if rates else 0.0
    if not shared_methods:
        warnings.append("нет общего payment method — проверь руками")
    if min_rate < 95:
        warnings.append("completion rate ниже 95%")
    if min_orders < 100:
        warnings.append("мало сделок у одной стороны")
    if not (buy_ad.is_merchant and sell_ad.is_merchant):
        warnings.append("не обе стороны merchant")
    if not warnings and min_rate >= 98 and min_orders >= 500:
        return "LOW", ()
    if len(warnings) <= 1 and min_rate >= 95 and min_orders >= 100:
        return "MEDIUM", tuple(warnings)
    return "HIGH", tuple(warnings)


def find_p2p_opportunities(
    buy_ads: list[P2PAdvert],
    sell_ads: list[P2PAdvert],
    *,
    min_spread_pct: float = DEFAULT_MIN_SPREAD_PCT,
    settlement_buffer_pct: float = DEFAULT_SETTLEMENT_BUFFER_PCT,
    min_completion_rate_pct: float = DEFAULT_MIN_COMPLETION_RATE_PCT,
    min_orders: int = DEFAULT_MIN_ORDERS,
    merchant_required: bool = False,
    preferred_pay_types: tuple[str, ...] = (),
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[P2POpportunity]:
    buys = [
        ad for ad in buy_ads
        if ad.trade_type == "BUY" and passes_quality_filter(
            ad,
            min_completion_rate_pct=min_completion_rate_pct,
            min_orders=min_orders,
            merchant_required=merchant_required,
        )
    ]
    sells = [
        ad for ad in sell_ads
        if ad.trade_type == "SELL" and passes_quality_filter(
            ad,
            min_completion_rate_pct=min_completion_rate_pct,
            min_orders=min_orders,
            merchant_required=merchant_required,
        )
    ]
    out: list[P2POpportunity] = []
    for buy_ad in buys:
        for sell_ad in sells:
            if buy_ad.asset != sell_ad.asset or buy_ad.fiat != sell_ad.fiat:
                continue
            if sell_ad.price <= buy_ad.price:
                continue
            shared_methods = _payment_intersection(buy_ad, sell_ad, preferred_pay_types)
            if preferred_pay_types and not shared_methods:
                continue
            executable_fiat = _executable_fiat(buy_ad, sell_ad)
            if executable_fiat <= 0:
                continue
            gross = ((sell_ad.price - buy_ad.price) / buy_ad.price) * 100
            net = gross - settlement_buffer_pct
            if net < min_spread_pct:
                continue
            risk, warnings = _risk_level(buy_ad, sell_ad, shared_methods)
            out.append(P2POpportunity(
                asset=buy_ad.asset,
                fiat=buy_ad.fiat,
                buy_ad=buy_ad,
                sell_ad=sell_ad,
                gross_spread_pct=gross,
                buffer_pct=settlement_buffer_pct,
                net_spread_pct=net,
                executable_fiat=executable_fiat,
                executable_asset=executable_fiat / buy_ad.price,
                shared_payment_methods=shared_methods,
                risk_level=risk,
                warnings=warnings,
            ))
    out.sort(key=lambda opp: (opp.net_spread_pct, opp.executable_fiat), reverse=True)
    return out[:max_results]


def format_p2p_report(
    opportunities: list[P2POpportunity],
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...],
    source: str = "Binance P2P",
    errors: tuple[str, ...] = (),
) -> str:
    title = f"*🧭 P2P arbitrage — {asset.upper()}/{fiat.upper()}*"
    pay_line = ", ".join(pay_types) if pay_types else "all methods"
    lines = [
        title,
        f"Source: `{source}` · payments: `{pay_line}`",
        "",
    ]
    if errors:
        lines.append("⚠️ Источник частично недоступен: " + "; ".join(errors[:3]))
        lines.append("")
    if not opportunities:
        lines.extend([
            "Сейчас чистого арбитражного окна нет.",
            "",
            "Важно: отсутствие сигнала — это нормально. P2P-спред быстро исчезает, а грязный spread без учёта лимитов/банка/рейтинга — шум.",
        ])
        return "\n".join(lines)

    lines.append("`net` = gross spread минус settlement/risk buffer. Это НЕ приказ к сделке.")
    lines.append("")
    for idx, opp in enumerate(opportunities, start=1):
        shared = ", ".join(opp.shared_payment_methods) if opp.shared_payment_methods else "manual check"
        buy = opp.buy_ad
        sell = opp.sell_ad
        lines.extend([
            f"*{idx}. Net {opp.net_spread_pct:+.2f}%* · gross `{opp.gross_spread_pct:+.2f}%` · risk `{opp.risk_level}`",
            f"   Buy: `{buy.price:.4f}` {opp.fiat} · {buy.advertiser} · orders `{buy.completed_orders or 0}` · done `{buy.completion_rate_pct or 0:.1f}%`",
            f"   Sell: `{sell.price:.4f}` {opp.fiat} · {sell.advertiser} · orders `{sell.completed_orders or 0}` · done `{sell.completion_rate_pct or 0:.1f}%`",
            f"   Size: up to `{opp.executable_fiat:,.0f}` {opp.fiat} ≈ `{opp.executable_asset:,.2f}` {opp.asset}; gross PnL ≈ `{opp.gross_profit_fiat:,.0f}` {opp.fiat}",
            f"   Payment overlap: `{shared}`",
        ])
        if opp.warnings:
            lines.append("   ⚠️ " + "; ".join(opp.warnings[:3]))
        lines.append("")

    lines.extend([
        "*Как читать:* важен только `net spread` после buffer + совпадение payment method + лимиты + рейтинг контрагентов.",
        "*Шум:* красивый gross spread без общего банка, маленький лимит, мало ордеров или completion ниже фильтра.",
        "*Риск:* не отправляй деньги без ручной проверки мерчанта, лимитов, реквизитов и актуальной цены в стакане.",
    ])
    return "\n".join(lines).rstrip()
