"""Pure P2P arbitrage math and formatting.

The module is intentionally exchange-agnostic and has no Telegram/HTTP imports.
Network adapters live in handlers/providers and feed normalized ``P2PAdvert``
rows here.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_UNKNOWN_BYBIT_ID_LOG_LIMIT = 50
_unknown_bybit_ids_logged: set[str] = set()


# По умолчанию сканер смотрит ШИРОКО — юзер: «расширить p2p до всех валютных
# пар в мире хз, а то щас вообще смысла нет в этой кнопке, нужно хоть что-то
# показывала бы хоть какую-нибудь выгоду». Поэтому ассеты = все ликвидные
# стейблы + крупная крипта + хайвол альты; фиаты = global coverage по
# регионам где P2P-spread исторически выше эффективности orderbook'а
# (TRY/ARS/VES/NGN — chronic inflation, P2P премия 2-5%; INR/IDR/VND —
# capital controls; UAH/KZT — постсоветские рейлы).
#
# Override через P2P_ARBITRAGE_ASSETS / P2P_ARBITRAGE_FIATS если нужно
# сузить или сместить фокус. Сканер graceful-degrade'ит если венчур не
# торгует пару — просто пропустит её без ошибки.
DEFAULT_P2P_ASSETS: tuple[str, ...] = (
    "USDT",   # доминантный стейбл, ~80% P2P volume
    "USDC",   # второй стейбл, более регуляторный
    "FDUSD",  # First Digital — растёт на Binance
    "BTC",    # самая ликвидная крипта на P2P
    "ETH",    # вторая по ликвидности
    "BNB",    # дешёвый transfer fee → удобно для арб-loops
    "SOL",    # высоковолатильная альта
    "TRX",    # дешёвый USDT-сеть-кошелёк (TRC20)
    "DAI",    # MakerDAO стейбл
    "LTC",    # legacy высокая ликвидность
)
# Региональные группы фиатов — для P2P_ARBITRAGE_FIAT_GROUP=cis|latam|asia|...
# DEFAULT_P2P_FIATS = объединение всех (global coverage). Юзер можно
# переключить на одну регион-группу через env, чтобы сузить скан.
P2P_FIAT_GROUPS: dict[str, tuple[str, ...]] = {
    # СНГ + соседи: исторически высокая P2P премия (контроль капитала,
    # санкции, спрос на USDT). Юзер работает с Казахстаном.
    "cis": ("RUB", "UAH", "KZT", "BYN", "AMD", "GEL", "AZN", "UZS"),
    # Латинская Америка: chronic inflation (ARS 100%+, VES hyperinflation)
    # → P2P премия 5-15% реальна. COP/MXN/PEN/CLP/BRL — крупные рынки.
    "latam": ("ARS", "VES", "COP", "MXN", "PEN", "CLP", "BRL", "BOB"),
    # Азия: capital controls (CNY/INR) + huge volume markets (IDR/VND/THB).
    "asia": ("VND", "THB", "IDR", "INR", "PKR", "PHP", "MYR", "BDT"),
    # MENA + Турция: TRY — крупнейший P2P-market в мире после CIS,
    # инфляция 60-80%. AED/SAR — стабильные dollar-pegs (мало арба).
    "mena": ("TRY", "AED", "SAR", "EGP", "ILS", "MAD", "TND", "LBP"),
    # Африка: NGN (Naira) — huge P2P премия из-за CBN-ограничений;
    # KES/GHS/ZAR/UGX — растущие рынки.
    "africa": ("NGN", "KES", "GHS", "ZAR", "UGX", "TZS"),
    # Европа (без EUR): малая P2P премия в развитых, но PLN/TRY-граница
    # иногда даёт окна. GBP/CHF/SEK/NOK — для completeness.
    "europe": ("GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON"),
    # Big reserve currencies (USD/EUR/JPY/etc) — мало арба, но baseline.
    "fiat_majors": ("USD", "EUR", "JPY", "CNY", "HKD", "SGD", "KRW", "TWD"),
}
# По умолчанию объединяем все группы (~50 фиатов) — даёт «global coverage».
# Юзер может сузить через P2P_ARBITRAGE_FIAT_GROUP=cis (только CIS) или
# P2P_ARBITRAGE_FIATS=RUB,USD (полный override).
def _build_default_fiats() -> tuple[str, ...]:
    """Дедуплицированное объединение всех групп для global-mode скана."""
    seen: set[str] = set()
    result: list[str] = []
    for group_key in ("cis", "latam", "asia", "mena", "africa", "europe", "fiat_majors"):
        for fiat in P2P_FIAT_GROUPS.get(group_key, ()):
            if fiat not in seen:
                seen.add(fiat)
                result.append(fiat)
    return tuple(result)


DEFAULT_P2P_FIATS: tuple[str, ...] = _build_default_fiats()
# Inter-pair дроссель: 10×~50=500 пар × 2 venue × 2 side = 2000 HTTP-запросов
# на полный скан. С semaphore=5 параллелизма и 0.15s sleep получаем ~60s
# walltime на full-global scan — это окей для scheduler-loop'а раз в 30 мин.
# Для on-demand button scan мы режем scope (см. get_button_scan_fiats).
DEFAULT_SCAN_THROTTLE_SEC = 0.15
# Параллелизм multi-pair скана — semaphore для async asyncio.gather. Чем
# выше, тем быстрее, но риск 429. Default 5 — компромисс между скоростью и
# rate limits Binance/Bybit P2P API (~10 req/sec/IP).
DEFAULT_SCAN_CONCURRENCY = 5
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

# M9-C P2P smart filters — крупные надёжные банки CIS-региона. Используются
# фильтром «только TIER-1» (`P2P_TIER1_ONLY=1`) когда юзеру важна гарантия
# что counterparty не использует серый/мелкий банк (риск отказов, AML, и
# просто долгого вывода). Override через `P2P_TIER1_BANKS=sber,tinkoff,...`.
#
# Список включает топ-10 RU-банков по активам + крупные kz/ua рейлы, плюс
# универсальные fast-payment системы (СБП в РФ, Kaspi Gold в Казахстане).
# Не включает: yoomoney/qiwi/mts_bank/akbars (мелкие или payment-only),
# vtb/psb/sovcombank (часто санкционные / лимиты на P2P).
DEFAULT_TIER1_BANKS: tuple[str, ...] = (
    "sber",        # Сбер РФ
    "tinkoff",     # Т-Банк РФ
    "alfabank",    # Альфа-Банк РФ
    "raiffeisen",  # Райффайзен РФ (RBA)
    "sbp",         # Система Быстрых Платежей (СБП) РФ
    "kaspi",       # Kaspi Bank KZ (по умолчанию для kz)
    "monobank",    # Монобанк UA
    "privat24",    # ПриватБанк UA
)
DEFAULT_MIN_EXECUTABLE_FIAT = 0.0
DEFAULT_MAX_AD_AGE_MIN = 0  # 0 = выкл

# M9-D / M9-E outlier protection — пользователь словил кейс USDT/ILS buy 2.77
# при рыночной медиане ~3.7 (отклонение -25%), что породило фейковый спред 35%.
#
# M9-D (PR #35) пытался опираться на median ОДНОЙ стороны (BUY или SELL), но это
# не работает в реальных P2P-маркетах:
#   • ILS: SELL-side полон wishlist-ads (мерчанты пробуют продать USDC за 3.75
#     ILS при реальном spot 2.90 = +29% премия). BUY median = реальный spot.
#   • SAR: BUY-side полон wishlist-ads (мерчанты предлагают купить USDC за 3.61
#     ILS при peg 3.75 = -3.7% дисконт от пега). SELL median = реальный spot.
# Поэтому one-side median анкор работает только в половине случаев.
#
# M9-E: переходим на ВНЕШНИЙ spot anchor через ``market_indicators.fiat_fx``
# (open.er-api.com + hardcoded fallback для USD-пегов). Для USDT/USDC anchor =
# spot USD-fiat rate. Для BTC/ETH/etc — outlier-фильтр пропускается (нет данных).
# Это даёт правильный якорь во ВСЕХ случаях.
#
# Параметры:
#   • OUTLIER_BAND_PCT — режем adverts, чья цена отклоняется от spot anchor
#     больше чем на N%. Default 15% покрывает нормальный P2P-шум (1-3%) + ARS-
#     style premium (5-10%) и при этом дропает обнальные wishlist'ы.
#   • MAX_SPREAD_PCT — hard cap на net_spread_pct. После PR #35 был 20%, но
#     пользователь словил кейс с 19% спредом на USDC/SAR / USDC/VES где обе
#     стороны под cap'ом, но реальный арб всё равно невозможен (USD-пеги).
#     Снижаем дефолт до 15% — реальный арб даже в VES/ARS редко sustained > 12%.
#   • DEDUP_PRICE_BUCKET_PCT — bucket size для стрикт-dedup'а opportunities.
DEFAULT_OUTLIER_BAND_PCT = 15.0
# Stable-coin assets (USDT/USDC/FDUSD/etc.) дают anchor = реальный USD→fiat
# forex-курс через ``market_indicators.fiat_fx``. Для них любое отклонение
# > 7% — это либо wishlist-ad, либо ad с экзотическим методом оплаты,
# который реально не исполнится. 15% band слишком широкий: пропускает
# ad'ы типа `15.21 MXN @ XchangeGlobal` при споте `17.28` (отклонение
# 12%) — после фикса инверсии Bybit (PR #38) такой ad всё равно сядет
# в SELL-пул и фантомного арба не даст, но он искажает median, anchor
# fallback и засоряет /p2p single-pair view.
#
# Не-stable активы (BTC/ETH/etc.) anchor не получают (``compute_market_anchor``
# возвращает None), для них outlier-фильтр пропускается целиком — поэтому
# DEFAULT_OUTLIER_BAND_PCT для них фактически безразличен, оставляем 15.0
# как страховку на случай если в будущем мы добавим crypto-spot anchors.
DEFAULT_OUTLIER_BAND_STABLE_PCT = 7.0
DEFAULT_MAX_SPREAD_PCT = 15.0
DEFAULT_DEDUP_PRICE_BUCKET_PCT = 0.5
DEFAULT_OUTLIER_MIN_SAMPLES = 3

# Активы, для которых spot anchor берётся из forex (USD-stable peg). См.
# ``market_indicators.fiat_fx.market_anchor_for_pair`` — там их явный whitelist.
STABLE_USD_ASSETS = frozenset({"USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD"})

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
    # Bybit P2P numeric payment IDs → canonical RU bank slugs.
    # Source: Bybit P2P public API (`paymentTypeList`). Best-effort mapping for
    # the most common RU rails; unknown IDs fall through to "bybit:<id>".
    "bybit:14": "tinkoff",
    "bybit:75": "tinkoff",
    "bybit:582": "tinkoff",
    "bybit:18": "sbp",
    "bybit:585": "sbp",
    "bybit:377": "sbp",
    "bybit:40": "sber",
    "bybit:185": "sber",
    "bybit:584": "sber",
    "bybit:64": "raiffeisen",
    "bybit:27": "raiffeisen",
    "bybit:62": "rosbank",
    "bybit:90": "cash",
    # Расширения по мере появления неизвестных IDs (best-effort, без
    # официального справочника — корректируется логом
    # ``unknown bybit payment id`` в продакшене).
    "bybit:160": "vtb",
    "bybit:171": "mts_bank",
    "bybit:172": "otkritie",
    "bybit:189": "alfabank",
    "bybit:230": "ozonbank",
    "bybit:264": "yoomoney",
    "bybit:267": "qiwi",
    "bybit:283": "tinkoff",
    "bybit:299": "sbp",
    "bybit:333": "sber",
    "bybit:381": "psb",
    "bybit:382": "uralsib",
    "bybit:600": "akbars",
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
    # Дополнительные signals качества аккаунта продавца:
    user_grade: int | None = None
    """Binance ``userGrade`` (0..7) — внутренний скоринг площадки."""
    vip_level: int | None = None
    """Bybit/Binance VIP level (0..N) — обычно отражает объёмы."""
    account_age_days: int | None = None
    """Возраст аккаунта продавца в днях (если venue отдаёт registerTime)."""

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
    # M9-D: рыночные медианы по той же стороне, той же паре — нужны
    # для outlier-detection в отчёте («buy на 25% ниже медианы =
    # подозрительно»). None если медиану не считали (legacy code-path
    # или мало sample'ов).
    median_buy_price: float | None = None
    median_sell_price: float | None = None

    @property
    def gross_profit_fiat(self) -> float:
        return self.executable_asset * (self.sell_ad.price - self.buy_ad.price)

    @property
    def buy_vs_median_pct(self) -> float | None:
        if self.median_buy_price is None or self.median_buy_price <= 0:
            return None
        return (self.buy_ad.price - self.median_buy_price) / self.median_buy_price * 100.0

    @property
    def sell_vs_median_pct(self) -> float | None:
        if self.median_sell_price is None or self.median_sell_price <= 0:
            return None
        return (self.sell_ad.price - self.median_sell_price) / self.median_sell_price * 100.0


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
    # По умолчанию ON — владелец явно попросил «P2P должен быть автоматически
    # включён». Это отступление от общего правила «фичефлаги по умолчанию OFF»
    # из AGENTS.md, но соответствует UX-намерению (read-only мониторинг,
    # денег не двигает).
    return _env_bool("FEATURE_P2P_ARBITRAGE", True)


def alerts_enabled() -> bool:
    # Алерты тоже включены по умолчанию: цель сканера — присылать
    # пользователю прибыльные окна автоматически.
    return _env_bool("FEATURE_P2P_ARBITRAGE_ALERTS", True)


def bybit_enabled() -> bool:
    return _env_bool("FEATURE_P2P_BYBIT", True)


def get_assets() -> tuple[str, ...]:
    return _env_csv("P2P_ARBITRAGE_ASSETS", DEFAULT_P2P_ASSETS)


def get_fiats() -> tuple[str, ...]:
    """Активный список фиатов для сканирования.

    Приоритет резолва:
      1. ``P2P_ARBITRAGE_FIATS=RUB,USD,...`` — полный явный override (всегда выигрывает)
      2. ``P2P_ARBITRAGE_FIAT_GROUP=cis|latam|asia|mena|africa|europe|fiat_majors`` — одна region-группа
      3. ``DEFAULT_P2P_FIATS`` — global coverage (~50 фиатов из всех групп)
    """
    raw = os.getenv("P2P_ARBITRAGE_FIATS", "").strip()
    if raw:
        return _env_csv("P2P_ARBITRAGE_FIATS", DEFAULT_P2P_FIATS)

    group = os.getenv("P2P_ARBITRAGE_FIAT_GROUP", "").strip().lower()
    if group and group in P2P_FIAT_GROUPS:
        return P2P_FIAT_GROUPS[group]

    return DEFAULT_P2P_FIATS


def get_button_scan_fiats() -> tuple[str, ...]:
    """Sub-список фиатов для on-demand button scan'а (быстрая выдача).

    Полный global scan (~50 фиатов × 10 ассетов = 500 пар) занимает ~60s
    walltime даже с semaphore=5. Это окей для scheduler-loop'а раз в 30 мин,
    но плохо для button-click (юзер ожидает результат за 5-15s).

    Поэтому button по умолчанию сужается до:
      • топ-арб-фиатов (CIS + LATAM + TRY/NGN) = 20 фиатов
      • стейблов (USDT/USDC/FDUSD) — где P2P премия живёт = 3 ассета
      = ~60 пар × 4 HTTP/пара = 240 HTTP-запросов / button click ≈ 8-12s
    Override через ``P2P_BUTTON_FIATS=RUB,USD,TRY,...``.
    """
    raw = os.getenv("P2P_BUTTON_FIATS", "").strip()
    if raw:
        return _env_csv("P2P_BUTTON_FIATS", DEFAULT_P2P_FIATS)
    # Default: high-arb-potential регионы (без low-arb fiat_majors/europe).
    seen: set[str] = set()
    out: list[str] = []
    for grp in ("cis", "latam", "mena", "africa"):
        for f in P2P_FIAT_GROUPS.get(grp, ()):
            if f not in seen:
                seen.add(f)
                out.append(f)
    return tuple(out)


def get_button_scan_assets() -> tuple[str, ...]:
    """Sub-список ассетов для on-demand button scan'а.

    Стейблы (USDT/USDC/FDUSD) — концентрируют 95% P2P-объёма, поэтому
    button по умолчанию сужается до них для быстрой выдачи. Override
    через ``P2P_BUTTON_ASSETS=USDT,USDC,BTC,...``.
    """
    raw = os.getenv("P2P_BUTTON_ASSETS", "").strip()
    if raw:
        return _env_csv("P2P_BUTTON_ASSETS", DEFAULT_P2P_ASSETS)
    return ("USDT", "USDC", "FDUSD")


def get_scan_concurrency() -> int:
    """Semaphore-параллелизм multi-pair скана. Default 5 (см. DEFAULT_SCAN_CONCURRENCY)."""
    raw = os.getenv("P2P_SCAN_CONCURRENCY", "").strip()
    if not raw:
        return DEFAULT_SCAN_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SCAN_CONCURRENCY
    return max(1, min(20, value))


def get_scan_throttle_sec() -> float:
    # Сколько секунд спать между парами в Cartesian-product скане.
    # 0 = без дросселя (риск 429 при широких defaults).
    return _env_float(
        "P2P_ARBITRAGE_SCAN_THROTTLE_SEC",
        DEFAULT_SCAN_THROTTLE_SEC,
        min_val=0.0,
        max_val=10.0,
    )


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


def get_risk_min_user_grade() -> int:
    """Minimum acceptable Binance ``userGrade``; 0 = check disabled."""
    return _env_int("P2P_RISK_MIN_USER_GRADE", 0, min_val=0, max_val=20)


def get_risk_min_vip_level() -> int:
    """Minimum acceptable VIP level; 0 = check disabled."""
    return _env_int("P2P_RISK_MIN_VIP_LEVEL", 0, min_val=0, max_val=50)


def get_risk_min_account_age_days() -> int:
    """Minimum account age (days); newer accounts get a warning. 0 = disabled."""
    return _env_int("P2P_RISK_MIN_ACCOUNT_AGE_DAYS", 30, min_val=0, max_val=3650)


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


def get_tier1_only() -> bool:
    """M9-C: фильтр «только TIER-1 банки».

    Если `P2P_TIER1_ONLY=1` — оставляем только объявления, где хотя бы
    один payment method входит в TIER-1 список (см. DEFAULT_TIER1_BANKS
    или override `P2P_TIER1_BANKS=sber,tinkoff,...`).
    """
    return _env_bool("P2P_TIER1_ONLY", False)


def get_tier1_banks() -> tuple[str, ...]:
    """Возвращает canonical list TIER-1 банков (override через env)."""
    raw = os.getenv("P2P_TIER1_BANKS", "").strip()
    if not raw:
        return DEFAULT_TIER1_BANKS
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or DEFAULT_TIER1_BANKS


def get_min_executable_fiat() -> float:
    """M9-C: минимальный объём opportunity в fiat. 0 = без ограничения.

    Например, для RUB можно поставить 50000 чтобы видеть только сделки
    где исполнимый объём ≥ 50k RUB (отсекаем мелочь).
    """
    return _env_float(
        "P2P_MIN_EXECUTABLE_FIAT",
        DEFAULT_MIN_EXECUTABLE_FIAT,
        min_val=0.0,
        max_val=1e12,
    )


def get_outlier_band_pct() -> float:
    """M9-D: max отклонение цены ad'а от медианы той же стороны (BUY/SELL)
    той же пары. 0 = выкл outlier-фильтр (legacy behavior).

    Default 15% достаточно широк чтобы не задевать нормальный P2P-шум
    (1-3% разброс), но дроп'ает dead/typo-ads типа USDT/ILS buy 2.77
    при медиане 3.7 (-25% отклонение).

    Для USDT/USDC/прочих USD-stable активов используется отдельный, более
    узкий band — см. ``get_outlier_band_stable_pct`` и
    ``get_outlier_band_for_asset``.
    """
    return _env_float(
        "P2P_OUTLIER_BAND_PCT",
        DEFAULT_OUTLIER_BAND_PCT,
        min_val=0.0,
        max_val=100.0,
    )


def get_outlier_band_stable_pct() -> float:
    """Узкий band для stable-coin пар (USDT/USDC/FDUSD/TUSD/DAI/BUSD).

    Spot anchor для них = реальный USD→fiat курс с open.er-api.com, поэтому
    любое отклонение > ~7% — это уже не «шум», а wishlist-ads или экзотические
    payment-метод ads которые не исполнятся как заявлено. См. подробный
    рационал в комментарии у DEFAULT_OUTLIER_BAND_STABLE_PCT.
    """
    return _env_float(
        "P2P_OUTLIER_BAND_STABLE_PCT",
        DEFAULT_OUTLIER_BAND_STABLE_PCT,
        min_val=0.0,
        max_val=100.0,
    )


def get_outlier_band_for_asset(asset: str | None) -> float:
    """Возвращает band для конкретного asset'а: stable → 7%, остальные → 15%."""
    if asset and asset.upper() in STABLE_USD_ASSETS:
        return get_outlier_band_stable_pct()
    return get_outlier_band_pct()


def get_max_spread_pct() -> float:
    """M9-D: hard cap на ``net_spread_pct``. Opps выше этого — мусор/шум
    (нереальный арб), не показываем. 0 = выкл cap (legacy).

    Реальный P2P-арб даже в гиперинфляционных фиатах редко превышает 15%
    (VES/ARS premium на USDT 5-12% sustained). 20% дефолтом — комфорт.
    """
    return _env_float(
        "P2P_MAX_SPREAD_PCT",
        DEFAULT_MAX_SPREAD_PCT,
        min_val=0.0,
        max_val=1000.0,
    )


def get_dedup_price_bucket_pct() -> float:
    """M9-D: bucket size для price-aware dedup'а. Две opps схлопываются
    в одну если buy/sell цены лежат в пределах ±N% друг от друга
    (даже если advertiser-пары разные).

    Default 0.5% — реальные «копии» одного и того же окна обычно идут
    с дельтой ≤ 0.3% по цене (разные мерчанты, тот же эффективный рынок).
    0 = выкл price-bucket dedup (только advertiser-pair как раньше).
    """
    return _env_float(
        "P2P_DEDUP_PRICE_BUCKET_PCT",
        DEFAULT_DEDUP_PRICE_BUCKET_PCT,
        min_val=0.0,
        max_val=10.0,
    )


def get_max_ad_age_minutes() -> int:
    """M9-C: max возраст объявления в минутах. 0 = без ограничения.

    Использует `fetched_at` который сканер ставит на момент HTTP fetch.
    Например, 5 минут = только свежак (быстро затухает после relist'а).
    """
    return _env_int(
        "P2P_MAX_AD_AGE_MIN",
        DEFAULT_MAX_AD_AGE_MIN,
        min_val=0,
        max_val=1440,
    )


def is_tier1_payment_method(method: str) -> bool:
    """True если canonical payment-method входит в TIER-1 список."""
    if not method:
        return False
    return method.strip().lower() in get_tier1_banks()


def ad_has_tier1_method(ad: P2PAdvert) -> bool:
    """True если хотя бы один payment method у объявления — TIER-1."""
    for m in ad.payment_methods:
        if is_tier1_payment_method(canonical_payment_method(m)):
            return True
    return False


def ad_is_fresh(ad: P2PAdvert, *, now: float | None = None) -> bool:
    """True если ad свежее лимита `P2P_MAX_AD_AGE_MIN`.

    Если лимит = 0 или `fetched_at` не выставлен — всегда True (фильтр
    выключен / нет данных для оценки).
    """
    max_age = get_max_ad_age_minutes()
    if max_age <= 0:
        return True
    if not ad.fetched_at or ad.fetched_at <= 0:
        return True
    now = now if now is not None else time.time()
    age_sec = now - ad.fetched_at
    return age_sec <= max_age * 60


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


def _account_age_days_from_register_ms(value: Any, *, now: float | None = None) -> int | None:
    """Compute ``account_age_days`` from a `registerTime` Unix-ms timestamp.

    Площадки отдают это поле в миллисекундах. Защищаемся от 0 / отрицательных
    значений и от слишком далёкого будущего (clock skew).
    """
    if value is None:
        return None
    raw = _to_float(value)
    if raw is None or raw <= 0:
        return None
    now_ts = now if now is not None else time.time()
    # Treat large numbers as ms, small as seconds (heuristic: > 10**11 ⇒ ms)
    if raw > 1e11:
        seconds = raw / 1000.0
    else:
        seconds = raw
    age_sec = now_ts - seconds
    if age_sec < 0:
        return None
    return int(age_sec // 86400)


def _normalize_user_grade(value: Any) -> int | None:
    """Binance `userGrade` приходит 0..7; clamp в этот диапазон."""
    num = _to_int(value)
    if num is None:
        return None
    if num < 0 or num > 20:
        return None
    return num


def _normalize_vip_level(value: Any) -> int | None:
    """Bybit/Binance VIP level — clamp в [0, 50]."""
    num = _to_int(value)
    if num is None:
        return None
    if num < 0 or num > 50:
        return None
    return num


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


def _log_unknown_bybit_id(prefixed: str) -> None:
    """Rate-limited warning: surface IDs not in ``PAYMENT_METHOD_ALIASES``.

    Storing each unknown ID once per process — we only need to learn the
    *existence* of the ID, not count usage. Прод-логи затем позволяют
    дополнить mapping в следующем коммите.
    """
    if not prefixed.startswith("bybit:"):
        return
    key = _payment_alias_key(prefixed)
    if not key or key in PAYMENT_METHOD_ALIASES:
        return
    if prefixed in _unknown_bybit_ids_logged:
        return
    if len(_unknown_bybit_ids_logged) >= _UNKNOWN_BYBIT_ID_LOG_LIMIT:
        return
    _unknown_bybit_ids_logged.add(prefixed)
    logger.warning(
        "p2p arbitrage: unknown bybit payment id %s — add it to PAYMENT_METHOD_ALIASES",
        prefixed,
    )


def _normalize_bybit_payment_method(value: Any) -> str:
    def _with_provider_prefix(normalized: str) -> str:
        if normalized and normalized.isdigit():
            prefixed = f"bybit:{normalized}"
            _log_unknown_bybit_id(prefixed)
            return prefixed
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
        user_stats = advertiser.get("userStatsRet") or {}
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
                or user_stats.get("completedOrderNum")
            ),
            completion_rate_pct=_completion_rate_pct(
                advertiser.get("monthFinishRate")
                or advertiser.get("positiveRate")
                or user_stats.get("completionRate")
            ),
            is_merchant=user_type in {"merchant", "block"} or bool(advertiser.get("isMerchant")),
            advert_id=str(adv.get("advNo") or ""),
            fetched_at=time.time(),
            payment_window_min=_to_int(adv.get("payTimeLimit")),
            user_grade=_normalize_user_grade(advertiser.get("userGrade")),
            vip_level=_normalize_vip_level(advertiser.get("vipLevel")),
            account_age_days=_account_age_days_from_register_ms(
                advertiser.get("registrationTime")
                or advertiser.get("registerTime")
                or user_stats.get("registerTime")
            ),
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
        trading_preference = row.get("tradingPreferenceSet") or {}
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
                or trading_preference.get("completeRateDay30")
            ),
            is_merchant=is_merchant,
            advert_id=str(row.get("id") or ""),
            fetched_at=time.time(),
            payment_window_min=_to_int(row.get("paymentPeriod")),
            user_grade=_normalize_user_grade(row.get("userGrade")),
            vip_level=_normalize_vip_level(row.get("vipLevel") or row.get("vaLevel")),
            account_age_days=_account_age_days_from_register_ms(
                row.get("registerTime")
                or row.get("userCreateTime")
                or row.get("accountCreateDate")
            ),
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
            user_grade=_normalize_user_grade(row.get("userGrade")),
            vip_level=_normalize_vip_level(row.get("vipLevel")),
            account_age_days=_account_age_days_from_register_ms(
                row.get("registerTime") or row.get("registrationTime")
            ),
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
    # Account quality signals (Soft #2 — userGrade / vipLevel / accountAge).
    # Скам-эффект: прокачанный monthFinishRate 30-дневный обнуляется свежим
    # аккаунтом, у которого ещё нет никаких других сигналов.
    min_user_grade = get_risk_min_user_grade()
    if min_user_grade > 0:
        grades = [g for g in (buy_ad.user_grade, sell_ad.user_grade) if g is not None]
        if grades and min(grades) < min_user_grade:
            warnings.append(f"низкий userGrade ({min(grades)} < {min_user_grade})")
    min_vip = get_risk_min_vip_level()
    if min_vip > 0:
        vips = [v for v in (buy_ad.vip_level, sell_ad.vip_level) if v is not None]
        if vips and min(vips) < min_vip:
            warnings.append(f"низкий VIP-level ({min(vips)} < {min_vip})")
    min_age = get_risk_min_account_age_days()
    if min_age > 0:
        ages = [a for a in (buy_ad.account_age_days, sell_ad.account_age_days) if a is not None]
        if ages and min(ages) < min_age:
            warnings.append(f"свежий аккаунт ({min(ages)} дн. < {min_age})")
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


def _median_price(prices: list[float]) -> float | None:
    """Median цены или None если samples < ``DEFAULT_OUTLIER_MIN_SAMPLES``.

    Минимум sample'ов важен: на 1-2 ad'ах медиана неинформативна (любой
    случайный outlier станет «нормой»). Поэтому ниже порога — возвращаем
    None, и outlier-фильтр graceful-skip'ает (пропускает все ads без фильтра).
    """
    valid = [p for p in prices if isinstance(p, (int, float)) and p > 0]
    if len(valid) < DEFAULT_OUTLIER_MIN_SAMPLES:
        return None
    valid.sort()
    n = len(valid)
    mid = n // 2
    if n % 2 == 1:
        return float(valid[mid])
    return (float(valid[mid - 1]) + float(valid[mid])) / 2.0


def _is_within_band(price: float, median: float, band_pct: float) -> bool:
    """True если ``price`` лежит в ``±band_pct%`` от ``median``.

    band_pct=0 → always True (фильтр выключен).
    median<=0 или price<=0 → False (нечего сравнивать).
    """
    if band_pct <= 0:
        return True
    if median <= 0 or price <= 0:
        return False
    deviation = abs(price - median) / median * 100.0
    return deviation <= band_pct


def filter_outliers(
    ads: list[P2PAdvert],
    *,
    band_pct: float | None = None,
) -> tuple[list[P2PAdvert], dict[tuple[str, str, str], float]]:
    """LEGACY (M9-D, PR #35): группирует ads по ``(asset, fiat, side)``, считает
    median по той же стороне, дропает ads с отклонением > ``band_pct%``.

    Сохранён для обратной совместимости тестов. **В новом коде используется
    spot-anchored фильтрация через ``compute_market_anchor`` + проверку каждого
    ad'а** — это устраняет одностороннюю bias-проблему (см. M9-E в шапке файла).

    Защита: если в группе < 3 sample'ов, медиана неопределена, ad проходит без
    фильтра. Возвращает ``(filtered_ads, medians)``.
    """
    band = get_outlier_band_pct() if band_pct is None else band_pct
    if band <= 0:
        return ads, {}

    groups: dict[tuple[str, str, str], list[float]] = {}
    for ad in ads:
        key = (ad.asset.upper(), ad.fiat.upper(), ad.trade_type.upper())
        groups.setdefault(key, []).append(float(ad.price))

    medians: dict[tuple[str, str, str], float] = {}
    for key, prices in groups.items():
        m = _median_price(prices)
        if m is not None:
            medians[key] = m

    filtered: list[P2PAdvert] = []
    for ad in ads:
        key = (ad.asset.upper(), ad.fiat.upper(), ad.trade_type.upper())
        median = medians.get(key)
        if median is None:
            filtered.append(ad)
            continue
        if _is_within_band(float(ad.price), median, band):
            filtered.append(ad)
    return filtered, medians


def compute_market_anchor(
    asset: str,
    fiat: str,
    all_ads: list[P2PAdvert],
    *,
    anchor_override: float | None = None,
) -> float | None:
    """Вычисляет market anchor для пары (asset, fiat).

    Приоритет:
      1. ``anchor_override`` — для DI / тестов (явное значение).
      2. External FX spot rate из ``market_indicators.fiat_fx`` (USD-stable
         assets only). Это **правильный** якорь — настоящий forex-курс,
         независимый от P2P-distortions с обеих сторон.
      3. Combined median (BUY+SELL вместе) — fallback когда spot недоступен и
         в пуле минимум 3 ad'а (как degraded хеджирование от полной потери
         outlier-защиты). Не идеален, но лучше чем ничего.
      4. None — fallback'и не сработали, фильтр пропускается для этой пары.
    """
    if anchor_override is not None and anchor_override > 0:
        return float(anchor_override)

    try:
        from market_indicators.fiat_fx import market_anchor_for_pair  # noqa: PLC0415
        spot = market_anchor_for_pair(asset, fiat)
        if spot is not None and spot > 0:
            return float(spot)
    except Exception as exc:
        _logger().warning("p2p outlier: fiat_fx anchor lookup failed: %s", exc)

    prices = [float(ad.price) for ad in all_ads if ad.price and ad.price > 0]
    return _median_price(prices)


def _logger() -> logging.Logger:
    return logging.getLogger(__name__)


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
    outlier_band_pct: float | None = None,
    max_spread_pct: float | None = None,
    dedup_price_bucket_pct: float | None = None,
) -> list[P2POpportunity]:
    legacy_buffer_pct = get_settlement_buffer_pct() if settlement_buffer_pct is None else settlement_buffer_pct

    # M9-C smart filters: TIER-1 банки + max age объявления. Применяются ДО
    # quality filter чтобы не считать R-score для очевидно нерелевантных
    # ad'ов. min_executable_fiat применяется ПОСЛЕ расчёта executable_fiat
    # (нужны оба ada чтобы знать общий объём).
    tier1_only = get_tier1_only()
    now_ts = time.time()

    def _passes_m9c_filters(ad: P2PAdvert) -> bool:
        if tier1_only and not ad_has_tier1_method(ad):
            return False
        if not ad_is_fresh(ad, now=now_ts):
            return False
        return True

    buys = [
        ad for ad in buy_ads
        if ad.trade_type == "BUY"
        and _passes_m9c_filters(ad)
        and passes_quality_filter(
            ad,
            min_completion_rate_pct=min_completion_rate_pct,
            min_orders=min_orders,
            merchant_required=merchant_required,
        )
    ]
    sells = [
        ad for ad in sell_ads
        if ad.trade_type == "SELL"
        and _passes_m9c_filters(ad)
        and passes_quality_filter(
            ad,
            min_completion_rate_pct=min_completion_rate_pct,
            min_orders=min_orders,
            merchant_required=merchant_required,
        )
    ]

    # M9-E: spot-anchored outlier filter. Якорь — реальный forex-курс из
    # ``market_indicators.fiat_fx`` (open.er-api.com + peg fallback). Для
    # USDT/USDC: 1 stable ≈ 1 USD, поэтому anchor = USD-fiat spot rate.
    # Для других asset'ов (BTC/ETH/etc) — fallback на combined median или
    # отсутствие фильтра (см. ``compute_market_anchor``).
    #
    # Per-asset band: USD-stables (USDT/USDC/FDUSD/etc.) идут с узким 7%
    # band'ом потому что у них есть точный forex anchor. Остальные активы
    # получают дефолтный 15% band (см. ``get_outlier_band_for_asset``).
    sample = next(iter(buy_ads + sell_ads), None)
    if outlier_band_pct is not None:
        band = outlier_band_pct
    elif sample is not None:
        band = get_outlier_band_for_asset(sample.asset)
    else:
        band = get_outlier_band_pct()
    market_anchor: float | None = None
    if band > 0:
        if sample is not None:
            market_anchor = compute_market_anchor(
                sample.asset,
                sample.fiat,
                list(buy_ads) + list(sell_ads),
            )

        if market_anchor is not None and market_anchor > 0:
            def _passes_outlier(ad: P2PAdvert) -> bool:
                if ad.price is None or ad.price <= 0:
                    return False
                deviation = abs(float(ad.price) - market_anchor) / market_anchor * 100.0
                return deviation <= band

            buys = [ad for ad in buys if _passes_outlier(ad)]
            sells = [ad for ad in sells if _passes_outlier(ad)]
        # Если anchor=None → outlier-фильтр пропускается (graceful degradation).
        # Max-spread cap ниже всё равно подстрахует от 30%+ фейков.

    min_exec_fiat = get_min_executable_fiat()
    cap_spread = get_max_spread_pct() if max_spread_pct is None else max_spread_pct
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
            # M9-C: фильтр минимального объёма (env P2P_MIN_EXECUTABLE_FIAT).
            # 0 = выкл. Применяется к executable_fiat (общий потолок объёма
            # сделки), а не к min_amount_fiat — чтобы исключать сделки с
            # потолком < N RUB/USD/EUR, а не сделки где min ≥ N.
            if min_exec_fiat > 0 and executable_fiat < min_exec_fiat:
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
            # M9-D: hard cap на сумасшедшие спреды. Если net > P2P_MAX_SPREAD_PCT
            # (default 20%) — это с очень высокой вероятностью шум (dead/typo
            # ad, mismatched payment-stream, проигнорированный outlier фильтр).
            # Реальный P2P-арб > 20% — единичный кейс (только VES-hyperinflation
            # flash). Дропаем чтобы не показывать пользователю «фейковый» 35%-арб.
            if cap_spread > 0 and net > cap_spread:
                continue
            risk, warnings = _risk_level(buy_ad, sell_ad, shared_methods, executable_fiat=executable_fiat)
            score = net * get_risk_weight(risk)
            # M9-E: оба поля держат ЕДИНЫЙ market anchor (spot FX). Это
            # позволяет в отчёте показать «vs market 3.75» вместо двух разных
            # медиан, и сравнение buy/sell-цены относительно ОДНОЙ точки.
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
                median_buy_price=market_anchor,
                median_sell_price=market_anchor,
            ))
    out.sort(key=lambda opp: (opp.score, opp.net_spread_pct, opp.executable_fiat), reverse=True)

    # M9-D: stricter dedup — учитываем 3 вектора одинаковости:
    #   1. та же advertiser-пара (legacy);
    #   2. тот же advertiser хотя бы с одной стороны + price-bucket другой
    #      стороны совпадает — один мерчант, разные ad'ы на одинаковую цену;
    #   3. price-bucket пара совпадает целиком — разные мерчанты, но
    #      идентичный по цене сетап (тот же рыночный сигнал, бесполезно
    #      дублировать в отчёте).
    # Bucket size настраивается env'ой; 0 = price-aware dedup выключен.
    bucket_pct = (
        get_dedup_price_bucket_pct() if dedup_price_bucket_pct is None else dedup_price_bucket_pct
    )

    def _bucket(price: float) -> int:
        if bucket_pct <= 0 or price <= 0:
            return 0
        # Bucket index: цена / (bucket_pct%-of-price). Цены в одном бакете
        # отличаются ≤ bucket_pct% друг от друга по rounding'у к ближайшему.
        scale = 100.0 / bucket_pct
        return int(round(price * scale))

    deduped: list[P2POpportunity] = []
    seen_advertisers: set[tuple[str, str]] = set()
    seen_price_buckets: set[tuple[str, str, int, int]] = set()
    seen_buy_adv_sell_bucket: set[tuple[str, str, str, int]] = set()
    seen_sell_adv_buy_bucket: set[tuple[str, str, int, str]] = set()
    for opportunity in out:
        buy_adv = opportunity.buy_ad.advertiser.strip().casefold()
        sell_adv = opportunity.sell_ad.advertiser.strip().casefold()
        advertiser_pair = (buy_adv, sell_adv)
        if advertiser_pair in seen_advertisers:
            continue
        buy_b = _bucket(opportunity.buy_ad.price)
        sell_b = _bucket(opportunity.sell_ad.price)
        asset_fiat = (opportunity.asset.upper(), opportunity.fiat.upper())
        if bucket_pct > 0:
            price_pair_key = asset_fiat + (buy_b, sell_b)
            if price_pair_key in seen_price_buckets:
                continue
            buy_collision = asset_fiat + (buy_adv, sell_b)
            if buy_collision in seen_buy_adv_sell_bucket:
                continue
            sell_collision = asset_fiat + (buy_b, sell_adv)
            if sell_collision in seen_sell_adv_buy_bucket:
                continue
            seen_price_buckets.add(price_pair_key)
            seen_buy_adv_sell_bucket.add(buy_collision)
            seen_sell_adv_buy_bucket.add(sell_collision)
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


def _median_annotation(
    price: float,
    anchor: float | None,
    *,
    side: str,
    asset: str | None = None,
) -> str:
    """Возвращает суффикс типа ` · vs market 3.7500 (-25.0%) ⚠️ outlier` или пустую строку.

    M9-E: ``anchor`` = реальный forex-курс (open.er-api.com), а не median
    одной из сторон — это устраняет проблему PR #35, где BUY-side median
    смещался wishlist-ads и не ловил настоящих outliers.

    Включает emoji-предупреждение если отклонение > 10% (или per-asset
    band'а, если он уже),  — это явный сигнал что ad либо dead, либо typo,
    либо payment-stream странный.
    """
    if anchor is None or anchor <= 0 or price <= 0:
        return ""
    delta_pct = (price - anchor) / anchor * 100.0
    band = get_outlier_band_for_asset(asset) if asset else get_outlier_band_pct()
    warn = " ⚠️ outlier" if band > 0 and abs(delta_pct) > min(10.0, band) else ""
    return f" · vs market `{anchor:.4f}` ({delta_pct:+.1f}%){warn}"


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
        buy_med_suffix = _median_annotation(buy.price, opp.median_buy_price, side="BUY", asset=buy.asset)
        sell_med_suffix = _median_annotation(sell.price, opp.median_sell_price, side="SELL", asset=sell.asset)
        lines.extend([
            f"*{idx}. Net {opp.net_spread_pct:+.2f}%* · gross `{opp.gross_spread_pct:+.2f}%` · score `{opp.score:.2f}` · risk `{opp.risk_level}` · {age_text}",
            f"   Buy: `{buy.price:.4f}` {opp.fiat} · {buy.venue} · {buy_safe} · orders `{buy.completed_orders or 0}` · done `{buy.completion_rate_pct or 0:.1f}%` · {_payment_window_text(buy)}{buy_med_suffix}",
            f"   Sell: `{sell.price:.4f}` {opp.fiat} · {sell.venue} · {sell_safe} · orders `{sell.completed_orders or 0}` · done `{sell.completion_rate_pct or 0:.1f}%` · {_payment_window_text(sell)}{sell_med_suffix}",
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
