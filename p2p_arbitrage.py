"""Pure P2P arbitrage math and formatting.

The module is intentionally exchange-agnostic and has no Telegram/HTTP imports.
Network adapters live in handlers/providers and feed normalized ``P2PAdvert``
rows here.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_P2P_ASSETS = ("USDT",)
DEFAULT_P2P_FIATS = ("RUB",)
DEFAULT_MIN_SPREAD_PCT = 1.0
DEFAULT_SETTLEMENT_BUFFER_PCT = 0.35
DEFAULT_BANK_FEE_PCT = 0.0
DEFAULT_CRYPTO_WITHDRAW_FEE_USDT = 1.0
DEFAULT_SLIPPAGE_PCT = 0.25
DEFAULT_OPPORTUNITY_TTL_SEC = 120
DEFAULT_PAYMENT_WINDOW_WARN_MIN = 15
DEFAULT_MIN_COMPLETION_RATE_PCT = 90.0
DEFAULT_MIN_ORDERS = 50
DEFAULT_MAX_RESULTS = 5
DEFAULT_ALERT_INTERVAL_SEC = 1800
DEFAULT_ALERT_COOLDOWN_SEC = 7200
DEFAULT_RISK_LOW_MIN_RATE_PCT = 98.0
DEFAULT_RISK_LOW_MIN_ORDERS = 500
DEFAULT_RISK_MEDIUM_MIN_RATE_PCT = 95.0
DEFAULT_RISK_MEDIUM_MIN_ORDERS = 100
DEFAULT_RISK_WEIGHT_LOW = 1.0
DEFAULT_RISK_WEIGHT_MEDIUM = 0.7
DEFAULT_RISK_WEIGHT_HIGH = 0.4

PAYMENT_METHOD_ALIASES = {
    "tinkoffnew": "tinkoff",
    "tinkoff": "tinkoff",
    "tbank": "tinkoff",
    "tink": "tinkoff",
    "тинькофф": "tinkoff",
    "тбанк": "tinkoff",
    "сбер": "sber",
    "сбербанк": "sber",
    "sber": "sber",
    "sberbank": "sber",
    "sberbanknew": "sber",
    "rosbank": "rosbank",
    "rosbanknew": "rosbank",
    "росбанк": "rosbank",
    "raiffeisen": "raiffeisen",
    "raiffeisenbank": "raiffeisen",
    "raiffeisenbanknew": "raiffeisen",
    "райффайзен": "raiffeisen",
    "sbp": "sbp",
    "сбп": "sbp",
}


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
    fetched_at: float = 0.0
    payment_window_min: int | None = None

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
    bank_fee_pct: float = 0.0
    crypto_withdraw_fee_usdt: float = 0.0
    crypto_withdraw_fee_pct: float = 0.0
    slippage_pct: float = 0.0
    cost_payment_method: str = ""
    score: float = 0.0

    @property
    def gross_profit_fiat(self) -> float:
        return self.executable_asset * (self.sell_ad.price - self.buy_ad.price)


@dataclass(frozen=True)
class P2PCostBreakdown:
    buffer_pct: float
    bank_fee_pct: float = 0.0
    crypto_withdraw_fee_usdt: float = 0.0
    crypto_withdraw_fee_pct: float = 0.0
    slippage_pct: float = 0.0
    payment_method: str = ""


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


def _env_float_optional(name: str, *, min_val: float, max_val: float) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value < min_val or value > max_val:
        return None
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


def alerts_enabled() -> bool:
    return _env_bool("FEATURE_P2P_ARBITRAGE_ALERTS", False)


def bybit_enabled() -> bool:
    return _env_bool("FEATURE_P2P_BYBIT", True)


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


def _fee_env_configured() -> bool:
    for name in os.environ:
        if name in {"P2P_CRYPTO_WITHDRAW_USDT", "P2P_SLIPPAGE_PCT"}:
            return True
        if name.startswith("P2P_BANK_FEE_"):
            return True
    return False


def _env_key_suffix(value: str) -> str:
    chars = [
        char if char.isascii() and char.isalnum() else "_"
        for char in str(value or "").upper()
    ]
    return "_".join(part for part in "".join(chars).split("_") if part)


def _bank_fee_env_candidates(payment_method: str) -> tuple[str, ...]:
    suffix = _env_key_suffix(payment_method)
    if not suffix:
        return ()
    candidates = [suffix]
    if suffix.endswith("NEW") and len(suffix) > 3:
        candidates.append(suffix[:-3])
    if suffix.endswith("_NEW") and len(suffix) > 4:
        candidates.append(suffix[:-4])
    if suffix.startswith("SBER"):
        candidates.append("SBER")
    if suffix.startswith(("TINKOFF", "T_BANK", "TBANK")):
        candidates.append("TINKOFF")
    return tuple(dict.fromkeys(candidates))


def get_default_bank_fee_pct() -> float:
    for name in ("P2P_BANK_FEE_DEFAULT", "P2P_BANK_FEE_DEFAULT_PCT"):
        value = _env_float_optional(name, min_val=0.0, max_val=10.0)
        if value is not None:
            return value
    return DEFAULT_BANK_FEE_PCT


def get_bank_fee_pct(payment_method: str) -> float:
    for candidate in _bank_fee_env_candidates(payment_method):
        for name in (f"P2P_BANK_FEE_{candidate}", f"P2P_BANK_FEE_{candidate}_PCT"):
            value = _env_float_optional(name, min_val=0.0, max_val=10.0)
            if value is not None:
                return value
    return get_default_bank_fee_pct()


def get_crypto_withdraw_fee_usdt() -> float:
    return _env_float(
        "P2P_CRYPTO_WITHDRAW_USDT",
        DEFAULT_CRYPTO_WITHDRAW_FEE_USDT,
        min_val=0.0,
        max_val=1_000.0,
    )


def get_slippage_pct() -> float:
    return _env_float("P2P_SLIPPAGE_PCT", DEFAULT_SLIPPAGE_PCT, min_val=0.0, max_val=10.0)


def get_opportunity_ttl_sec() -> int:
    return _env_int("P2P_OPPORTUNITY_TTL_SEC", DEFAULT_OPPORTUNITY_TTL_SEC, min_val=1, max_val=3_600)


def get_payment_window_warn_min() -> int:
    return _env_int("P2P_PAYMENT_WINDOW_WARN_MIN", DEFAULT_PAYMENT_WINDOW_WARN_MIN, min_val=1, max_val=120)


def get_risk_low_min_rate_pct() -> float:
    return _env_float(
        "P2P_RISK_LOW_MIN_RATE",
        DEFAULT_RISK_LOW_MIN_RATE_PCT,
        min_val=0.0,
        max_val=100.0,
    )


def get_risk_low_min_orders() -> int:
    return _env_int("P2P_RISK_LOW_MIN_ORDERS", DEFAULT_RISK_LOW_MIN_ORDERS, min_val=0, max_val=1_000_000)


def get_risk_medium_min_rate_pct() -> float:
    return _env_float(
        "P2P_RISK_MEDIUM_MIN_RATE",
        DEFAULT_RISK_MEDIUM_MIN_RATE_PCT,
        min_val=0.0,
        max_val=100.0,
    )


def get_risk_medium_min_orders() -> int:
    return _env_int(
        "P2P_RISK_MEDIUM_MIN_ORDERS",
        DEFAULT_RISK_MEDIUM_MIN_ORDERS,
        min_val=0,
        max_val=1_000_000,
    )


def get_risk_weight(risk_level: str) -> float:
    defaults = {
        "LOW": DEFAULT_RISK_WEIGHT_LOW,
        "MEDIUM": DEFAULT_RISK_WEIGHT_MEDIUM,
        "HIGH": DEFAULT_RISK_WEIGHT_HIGH,
    }
    level = str(risk_level or "").upper()
    return _env_float(
        f"P2P_RISK_WEIGHT_{level}",
        defaults.get(level, DEFAULT_RISK_WEIGHT_HIGH),
        min_val=0.0,
        max_val=1.0,
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


def get_alert_interval_sec() -> int:
    return _env_int(
        "P2P_ARBITRAGE_ALERT_INTERVAL_SEC",
        DEFAULT_ALERT_INTERVAL_SEC,
        min_val=300,
        max_val=86_400,
    )


def get_alert_cooldown_sec() -> int:
    return _env_int(
        "P2P_ARBITRAGE_ALERT_COOLDOWN_SEC",
        DEFAULT_ALERT_COOLDOWN_SEC,
        min_val=300,
        max_val=86_400,
    )


def get_alert_chat_ids(admin_ids: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv("P2P_ARBITRAGE_ALERT_CHAT_IDS", "").strip()
    ids: list[int] = []
    if raw:
        for part in raw.split(","):
            item = part.strip()
            if not item:
                continue
            try:
                ids.append(int(item))
            except ValueError:
                continue
    if not ids:
        ids = [int(x) for x in admin_ids if int(x) > 0]
    return tuple(dict.fromkeys(ids))


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


def _escape_md(text: Any) -> str:
    """Escape common Markdown characters to avoid formatting injection."""
    s = str(text or "")
    if not s:
        return ""
    # escape backslash first
    s = s.replace("\\", "\\\\")
    for ch in ('*', '_', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!'):
        s = s.replace(ch, f"\\{ch}")
    return s


def _payment_alias_key(value: Any) -> str:
    text = normalize_payment_method(value).casefold()
    for char in (" ", "_", "-", "."):
        text = text.replace(char, "")
    return text


def canonical_payment_method(value: Any) -> str:
    normalized = normalize_payment_method(value)
    if not normalized:
        return ""
    return PAYMENT_METHOD_ALIASES.get(_payment_alias_key(normalized), normalized)


def _normalize_bybit_payment_method(value: Any) -> str:
    def _with_provider_prefix(normalized: str) -> str:
        if normalized and normalized.isdigit():
            return f"bybit:{normalized}"
        return normalized

    if isinstance(value, dict):
        for key in (
            "identifier",
            "paymentName",
            "paymentType",
            "paymentMethodName",
            "name",
            "id",
        ):
            normalized = normalize_payment_method(value.get(key))
            if normalized:
                return _with_provider_prefix(normalized)
        config = value.get("paymentConfigVo") or value.get("paymentConfig") or {}
        if isinstance(config, dict):
            for key in ("paymentName", "paymentType", "name", "id"):
                normalized = normalize_payment_method(config.get(key))
                if normalized:
                    return _with_provider_prefix(normalized)
        return ""
    normalized = normalize_payment_method(value)
    return _with_provider_prefix(normalized)


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
            fetched_at=time.time(),
            payment_window_min=_to_int(adv.get("payTimeLimit")),
        )
    except Exception:
        return None


def parse_bybit_ad(row: dict[str, Any], *, trade_type: str, asset: str, fiat: str) -> P2PAdvert | None:
    """Parse one Bybit P2P advertisement row into a normalized advert."""
    try:
        price = _to_float(row.get("price"))
        min_amount = _to_float(row.get("minAmount"))
        max_amount = _to_float(row.get("maxAmount"))
        if price is None or price <= 0 or min_amount is None or max_amount is None:
            return None

        methods: list[str] = []
        for method in row.get("payments") or row.get("paymentMethods") or []:
            normalized = _normalize_bybit_payment_method(method)
            if normalized:
                methods.append(normalized)

        available_asset = _to_float(row.get("lastQuantity"))
        if available_asset is None:
            quantity = _to_float(row.get("quantity"))
            frozen = _to_float(row.get("frozenQuantity")) or 0.0
            if quantity is not None:
                available_asset = max(0.0, quantity - frozen)

        auth_tags = row.get("authTag") or []
        user_type = str(row.get("userType") or "").upper()
        is_merchant = (
            bool(auth_tags)
            or user_type in {"MERCHANT", "BUSINESS", "ORG", "ORGANIZATION"}
            or bool(row.get("makerContact"))
        )
        return P2PAdvert(
            venue="Bybit P2P",
            trade_type=trade_type.upper(),
            asset=str(row.get("tokenId") or asset).upper(),
            fiat=str(row.get("currencyId") or fiat).upper(),
            price=price,
            min_amount_fiat=max(0.0, min_amount),
            max_amount_fiat=max(0.0, max_amount),
            available_asset=available_asset,
            payment_methods=tuple(dict.fromkeys(methods)),
            advertiser=str(row.get("nickName") or row.get("userMaskId") or "unknown"),
            completed_orders=_to_int(row.get("finishNum") or row.get("orderNum") or row.get("recentOrderNum")),
            completion_rate_pct=_completion_rate_pct(
                row.get("recentExecuteRate")
                or (row.get("tradingPreferenceSet") or {}).get("completeRateDay30")
            ),
            is_merchant=is_merchant,
            advert_id=str(row.get("id") or ""),
            fetched_at=time.time(),
            payment_window_min=_to_int(row.get("paymentPeriod")),
        )
    except Exception:
        return None


def okx_enabled() -> bool:
    return _env_bool("FEATURE_P2P_OKX", True)


def parse_okx_ad(row: dict[str, Any], *, trade_type: str, asset: str, fiat: str) -> P2PAdvert | None:
    """Parse a (best-effort) OKX P2P advertisement row into a normalized advert.

    OKX API shapes vary; this parser is defensive and extracts common fields.
    """
    try:
        # common price/min/max keys used by various providers
        price = _to_float(
            row.get("price")
            or row.get("unitPrice")
            or row.get("advPrice")
            or row.get("payAmount")
        )
        min_amount = _to_float(row.get("minAmount") or row.get("minSingleTransAmount") or row.get("minLimit"))
        max_amount = _to_float(row.get("maxAmount") or row.get("maxSingleTransAmount") or row.get("maxLimit"))
        if price is None or price <= 0 or min_amount is None or max_amount is None:
            return None

        methods: list[str] = []
        for method in row.get("paymentMethods") or row.get("payments") or row.get("tradeMethods") or []:
            if isinstance(method, dict):
                name = (
                    method.get("name")
                    or method.get("paymentName")
                    or method.get("bankName")
                    or method.get("identifier")
                )
                normalized = normalize_payment_method(name)
                if normalized:
                    methods.append(normalized)
            else:
                normalized = normalize_payment_method(method)
                if normalized:
                    methods.append(normalized)

        available_asset = _to_float(row.get("surplusAmount") or row.get("available") or row.get("lastQuantity") or row.get("quantity"))
        advertiser = str(
            row.get("nickName")
            or row.get("userName")
            or row.get("advertiser")
            or row.get("seller")
            or "unknown"
        )
        completed_orders = _to_int(row.get("finishNum") or row.get("orderNum") or row.get("tradeQty"))
        completion_rate_pct = _completion_rate_pct(row.get("completeRate") or row.get("completionRate") or row.get("recentExecuteRate"))
        is_merchant = bool(row.get("isMerchant") or row.get("merchant") or row.get("userType") in ("MERCHANT", "BUSINESS"))
        advert_id = str(row.get("adId") or row.get("id") or "")

        return P2PAdvert(
            venue="OKX P2P",
            trade_type=trade_type.upper(),
            asset=str(row.get("token") or asset).upper(),
            fiat=str(row.get("fiat") or fiat).upper(),
            price=price,
            min_amount_fiat=max(0.0, min_amount),
            max_amount_fiat=max(0.0, max_amount),
            available_asset=available_asset,
            payment_methods=tuple(dict.fromkeys(methods)),
            advertiser=advertiser,
            completed_orders=completed_orders,
            completion_rate_pct=completion_rate_pct,
            is_merchant=is_merchant,
            advert_id=advert_id,
            fetched_at=time.time(),
            payment_window_min=_to_int(row.get("paymentLimit") or row.get("paymentPeriod")),
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
    # Build maps from canonical key -> canonical method string for both sides.
    buy_map: dict[str, str] = {}
    for method in buy_ad.payment_methods:
        canonical = canonical_payment_method(method)
        if not canonical:
            continue
        key = canonical.casefold()
        if key not in buy_map:
            buy_map[key] = canonical

    sell_map: dict[str, str] = {}
    for method in sell_ad.payment_methods:
        canonical = canonical_payment_method(method)
        if not canonical:
            continue
        key = canonical.casefold()
        if key not in sell_map:
            sell_map[key] = canonical

    if preferred_pay_types:
        pref_map: dict[str, str] = {}
        for method in preferred_pay_types:
            canonical = canonical_payment_method(method)
            if not canonical:
                continue
            key = canonical.casefold()
            if key not in pref_map:
                pref_map[key] = canonical
        keys = set(buy_map) & set(sell_map) & set(pref_map)
    else:
        keys = set(buy_map) & set(sell_map)

    return tuple(buy_map[k] for k in sorted(keys))


def _executable_fiat(buy_ad: P2PAdvert, sell_ad: P2PAdvert) -> float:
    max_fiat = min(buy_ad.max_amount_fiat, sell_ad.max_amount_fiat)
    if buy_ad.available_asset is not None:
        max_fiat = min(max_fiat, buy_ad.available_asset * buy_ad.price)
    if sell_ad.available_asset is not None:
        max_fiat = min(max_fiat, sell_ad.available_asset * sell_ad.price)
    min_fiat = max(buy_ad.min_amount_fiat, sell_ad.min_amount_fiat)
    return max_fiat if max_fiat >= min_fiat else 0.0


def _bank_fee_for_methods(shared_methods: tuple[str, ...]) -> tuple[float, str]:
    if not shared_methods:
        return get_default_bank_fee_pct(), ""
    fees = [(get_bank_fee_pct(method), method) for method in shared_methods]
    return min(fees, key=lambda item: item[0])


def _opportunity_cost_breakdown(
    buy_ad: P2PAdvert,
    sell_ad: P2PAdvert,
    shared_methods: tuple[str, ...],
    *,
    executable_asset: float,
    legacy_buffer_pct: float,
) -> P2PCostBreakdown:
    if not _fee_env_configured():
        return P2PCostBreakdown(buffer_pct=max(0.0, legacy_buffer_pct))

    bank_fee_pct, payment_method = _bank_fee_for_methods(shared_methods)
    slippage_pct = get_slippage_pct()
    withdraw_fee_usdt = get_crypto_withdraw_fee_usdt() if buy_ad.venue != sell_ad.venue else 0.0
    withdraw_fee_pct = (withdraw_fee_usdt / executable_asset) * 100 if executable_asset > 0 else 0.0
    buffer_pct = bank_fee_pct + slippage_pct + withdraw_fee_pct
    return P2PCostBreakdown(
        buffer_pct=buffer_pct,
        bank_fee_pct=bank_fee_pct,
        crypto_withdraw_fee_usdt=withdraw_fee_usdt,
        crypto_withdraw_fee_pct=withdraw_fee_pct,
        slippage_pct=slippage_pct,
        payment_method=payment_method,
    )


def _risk_level(
    buy_ad: P2PAdvert,
    sell_ad: P2PAdvert,
    shared_methods: tuple[str, ...],
    *,
    executable_fiat: float | None = None,
) -> tuple[str, tuple[str, ...]]:
    warnings: list[str] = []
    min_orders = min(buy_ad.completed_orders or 0, sell_ad.completed_orders or 0)
    rates = [r for r in (buy_ad.completion_rate_pct, sell_ad.completion_rate_pct) if r is not None]
    min_rate = min(rates) if rates else 0.0
    windows = [
        window for window in (buy_ad.payment_window_min, sell_ad.payment_window_min)
        if window is not None and window > 0
    ]
    min_payment_window = min(windows) if windows else None
    if not shared_methods:
        warnings.append("нет общего payment method — проверь руками")
    if min_rate < get_risk_medium_min_rate_pct():
        warnings.append(f"completion rate ниже {get_risk_medium_min_rate_pct():.0f}%")
    if min_orders < get_risk_medium_min_orders():
        warnings.append("мало сделок у одной стороны")
    if not (buy_ad.is_merchant and sell_ad.is_merchant):
        warnings.append("не обе стороны merchant")
    if min_payment_window is not None and min_payment_window < get_payment_window_warn_min():
        warnings.append(f"короткое окно оплаты {min_payment_window} мин")
    # Russia-specific check: big RUB transfers may trigger 115-FZ requirements
    try:
        fiat_upper = (buy_ad.fiat or "").upper()
    except Exception:
        fiat_upper = ""
    if fiat_upper == "RUB" and executable_fiat is not None and executable_fiat > 600_000:
        warnings.append("высокий объём (>600k RUB) — возможны ограничения 115-ФЗ")
    # Unknown bank card detection
    all_methods = tuple(buy_ad.payment_methods) + tuple(sell_ad.payment_methods)
    for m in all_methods:
        nm = normalize_payment_method(m).casefold()
        if "карта" in nm or "card" in nm:
            key = _payment_alias_key(m)
            if key and key not in PAYMENT_METHOD_ALIASES:
                warnings.append("payment method содержит карту неизвестного банка — проверь реквизиты")
                break
    if (
        not warnings
        and min_rate >= get_risk_low_min_rate_pct()
        and min_orders >= get_risk_low_min_orders()
    ):
        return "LOW", ()
    if (
        len(warnings) <= 1
        and min_rate >= get_risk_medium_min_rate_pct()
        and min_orders >= get_risk_medium_min_orders()
    ):
        return "MEDIUM", tuple(warnings)
    return "HIGH", tuple(warnings)


def find_p2p_opportunities(
    buy_ads: list[P2PAdvert],
    sell_ads: list[P2PAdvert],
    *,
    min_spread_pct: float = DEFAULT_MIN_SPREAD_PCT,
    settlement_buffer_pct: float | None = None,
    min_completion_rate_pct: float = DEFAULT_MIN_COMPLETION_RATE_PCT,
    min_orders: int = DEFAULT_MIN_ORDERS,
    merchant_required: bool = False,
    preferred_pay_types: tuple[str, ...] = (),
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[P2POpportunity]:
    legacy_buffer_pct = get_settlement_buffer_pct() if settlement_buffer_pct is None else settlement_buffer_pct
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
            executable_asset = executable_fiat / buy_ad.price
            costs = _opportunity_cost_breakdown(
                buy_ad,
                sell_ad,
                shared_methods,
                executable_asset=executable_asset,
                legacy_buffer_pct=legacy_buffer_pct,
            )
            net = gross - costs.buffer_pct
            if net < min_spread_pct:
                continue
            risk, warnings = _risk_level(buy_ad, sell_ad, shared_methods, executable_fiat=executable_fiat)
            score = net * get_risk_weight(risk)
            out.append(P2POpportunity(
                asset=buy_ad.asset,
                fiat=buy_ad.fiat,
                buy_ad=buy_ad,
                sell_ad=sell_ad,
                gross_spread_pct=gross,
                buffer_pct=costs.buffer_pct,
                net_spread_pct=net,
                executable_fiat=executable_fiat,
                executable_asset=executable_asset,
                shared_payment_methods=shared_methods,
                risk_level=risk,
                warnings=warnings,
                bank_fee_pct=costs.bank_fee_pct,
                crypto_withdraw_fee_usdt=costs.crypto_withdraw_fee_usdt,
                crypto_withdraw_fee_pct=costs.crypto_withdraw_fee_pct,
                slippage_pct=costs.slippage_pct,
                cost_payment_method=costs.payment_method,
                score=score,
            ))
    out.sort(key=lambda opp: (opp.score, opp.net_spread_pct, opp.executable_fiat), reverse=True)
    deduped: list[P2POpportunity] = []
    seen_advertisers: set[tuple[str, str]] = set()
    for opportunity in out:
        advertiser_pair = (
            opportunity.buy_ad.advertiser.strip().casefold(),
            opportunity.sell_ad.advertiser.strip().casefold(),
        )
        if advertiser_pair in seen_advertisers:
            continue
        seen_advertisers.add(advertiser_pair)
        deduped.append(opportunity)
        if len(deduped) >= max_results:
            break
    return deduped


def opportunity_key(opp: P2POpportunity) -> str:
    """Produce a deterministic key for deduping/alert cooldowns for an opportunity."""
    buy = opp.buy_ad
    sell = opp.sell_ad
    buy_id = buy.advert_id or buy.advertiser
    sell_id = sell.advert_id or sell.advertiser
    methods = "-".join(sorted(m.casefold() for m in opp.shared_payment_methods)) if opp.shared_payment_methods else "manual"
    return f"{opp.asset}|{opp.fiat}|{buy.venue}:{buy_id}|{sell.venue}:{sell_id}|{methods}|net:{opp.net_spread_pct:.4f}|size:{int(opp.executable_fiat)}"


def _opportunity_age_sec(opp: P2POpportunity, *, now: float) -> int | None:
    timestamps = [
        fetched_at for fetched_at in (opp.buy_ad.fetched_at, opp.sell_ad.fetched_at)
        if fetched_at and fetched_at > 0
    ]
    if not timestamps:
        return None
    return int(max(0.0, now - min(timestamps)))


def _cost_line(opp: P2POpportunity) -> str:
    if opp.bank_fee_pct or opp.slippage_pct or opp.crypto_withdraw_fee_pct:
        method = f" via `{_escape_md(opp.cost_payment_method)}`" if opp.cost_payment_method else ""
        return (
            f"   Costs: buffer `{opp.buffer_pct:.2f}%` = bank `{opp.bank_fee_pct:.2f}%`{method}"
            f" + slippage `{opp.slippage_pct:.2f}%`"
            f" + withdraw `{opp.crypto_withdraw_fee_usdt:.2f}` USDT (`{opp.crypto_withdraw_fee_pct:.2f}%`)"
        )
    return f"   Costs: settlement buffer `{opp.buffer_pct:.2f}%`"


def _payment_window_text(ad: P2PAdvert) -> str:
    if ad.payment_window_min is None:
        return "pay `n/a`"
    return f"pay `{ad.payment_window_min}m`"


def format_p2p_report(
    opportunities: list[P2POpportunity],
    *,
    asset: str,
    fiat: str,
    pay_types: tuple[str, ...],
    source: str = "Binance P2P",
    errors: tuple[str, ...] = (),
) -> str:
    now = time.time()
    opportunity_ttl_sec = get_opportunity_ttl_sec()
    title = f"*🧭 P2P arbitrage — {asset.upper()}/{fiat.upper()}*"
    pay_line = ", ".join(pay_types) if pay_types else "all methods"
    pay_line_safe = _escape_md(pay_line)
    lines = [
        title,
        f"Source: `{_escape_md(source)}` · payments: `{pay_line_safe}`",
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
        shared_safe = _escape_md(shared)
        buy = opp.buy_ad
        sell = opp.sell_ad
        age_sec = _opportunity_age_sec(opp, now=now)
        age_text = f"данные собраны {age_sec} сек назад" if age_sec is not None else "данные без timestamp"
        report_warnings = list(opp.warnings)
        if age_sec is not None and age_sec > opportunity_ttl_sec:
            report_warnings.append(f"данные старше TTL {opportunity_ttl_sec} сек")
        buy_safe = _escape_md(buy.advertiser)
        sell_safe = _escape_md(sell.advertiser)
        lines.extend([
            f"*{idx}. Net {opp.net_spread_pct:+.2f}%* · gross `{opp.gross_spread_pct:+.2f}%` · score `{opp.score:.2f}` · risk `{opp.risk_level}` · {age_text}",
            f"   Buy: `{buy.price:.4f}` {opp.fiat} · {buy.venue} · {buy_safe} · orders `{buy.completed_orders or 0}` · done `{buy.completion_rate_pct or 0:.1f}%` · {_payment_window_text(buy)}",
            f"   Sell: `{sell.price:.4f}` {opp.fiat} · {sell.venue} · {sell_safe} · orders `{sell.completed_orders or 0}` · done `{sell.completion_rate_pct or 0:.1f}%` · {_payment_window_text(sell)}",
            f"   Size: up to `{opp.executable_fiat:,.0f}` {opp.fiat} ≈ `{opp.executable_asset:,.2f}` {opp.asset}; gross PnL ≈ `{opp.gross_profit_fiat:,.0f}` {opp.fiat}",
            _cost_line(opp),
            f"   Payment overlap: `{shared_safe}`",
        ])
        if report_warnings:
            lines.append("   ⚠️ " + "; ".join(report_warnings[:4]))
        lines.append("")

    lines.extend([
        "*Как читать:* важен только `net spread` после buffer + совпадение payment method + лимиты + рейтинг контрагентов.",
        "*Шум:* красивый gross spread без общего банка, маленький лимит, мало ордеров или completion ниже фильтра.",
        "*Риск:* не отправляй деньги без ручной проверки мерчанта, лимитов, реквизитов и актуальной цены в стакане.",
    ])
    return "\n".join(lines).rstrip()
