"""core/cross_exchange.py — КРОСС-БИРЖЕВОЙ funding-арбитраж.

То, что НИ ОДНА биржа не покажет в своём UI: фандинг по одному активу разный на
разных биржах. Игра (market-neutral, без спота):
  ЛОНГ перп там где фандинг НИЗКИЙ/отрицательный  +  ШОРТ перп там где ВЫСОКИЙ
  → обе ноги перп на один актив → цена нейтральна (гасятся)
  → собираешь СПРЕД фандинга (annualized) = high − low.

Источник: публичные API 4 бирж (Binance, Bybit, Gate, Hyperliquid) — без ключей.
Аннуализация учитывает интервал фандинга (Binance/Bybit/Gate 8ч, Hyperliquid 1ч).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0"}
# Спред считаем только для ликвидных активов (иначе топ — мусорные мемы со
# стейл-фандингом ±300%, торговать нельзя).
MIN_SPREAD_ANNUAL = 12.0   # % годовых — ниже косты двух ног съедят
SANE_ABS_CAP = 200.0       # |ann| выше на ноге — выброс/стейл, отсекаем

# Ликвидный юниверс — топ перпов, торгуемые на нескольких биржах.
LIQUID_ASSETS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT",
    "TRX", "TON", "LTC", "NEAR", "SUI", "APT", "ARB", "OP", "MATIC", "POL",
    "INJ", "SEI", "TIA", "PEPE", "WIF", "BONK", "FIL", "ATOM", "UNI", "AAVE",
    "LDO", "RUNE", "FTM", "HBAR", "ICP", "ETC", "BCH", "XLM", "ALGO", "ENA",
    "JUP", "PYTH", "STX", "ORDI", "FET", "RENDER", "WLD", "TAO", "KAS", "ONDO",
}


@dataclass(frozen=True)
class ArbOpportunity:
    asset: str
    long_venue: str          # где фандинг низкий → ЛОНГ перп
    short_venue: str         # где высокий → ШОРТ перп
    long_ann: float
    short_ann: float

    @property
    def spread(self) -> float:
        return self.short_ann - self.long_ann


def _get(url: str, timeout: int = 12, post: dict | None = None):
    data = json.dumps(post).encode() if post is not None else None
    hdr = dict(UA)
    if post is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


# ───────────────────── фетчеры по биржам → {ASSET: annual_pct} ─────────────────
def funding_binance() -> dict:
    out = {}
    try:
        for p in _get("https://fapi.binance.com/fapi/v1/premiumIndex"):
            s = p.get("symbol", "")
            if not s.endswith("USDT"):
                continue
            try:
                out[s[:-4]] = float(p["lastFundingRate"]) * 3 * 365 * 100  # 8ч
            except (KeyError, ValueError, TypeError):
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("xexch binance: %s", e)
    return out


def funding_bybit() -> dict:
    out = {}
    try:
        d = _get("https://api.bybit.com/v5/market/tickers?category=linear")
        for it in d.get("result", {}).get("list", []):
            s = it.get("symbol", "")
            if not s.endswith("USDT"):
                continue
            fr = it.get("fundingRate")
            if fr in (None, ""):
                continue
            try:
                out[s[:-4]] = float(fr) * 3 * 365 * 100  # 8ч (деф)
            except ValueError:
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("xexch bybit: %s", e)
    return out


def funding_gate() -> dict:
    out = {}
    try:
        for c in _get("https://api.gateio.ws/api/v4/futures/usdt/contracts"):
            name = c.get("name", "")
            if not name.endswith("_USDT"):
                continue
            fr = c.get("funding_rate")
            if fr in (None, ""):
                continue
            iv = c.get("funding_interval", 28800) or 28800  # сек, деф 8ч
            try:
                ann = float(fr) * (86400.0 / float(iv)) * 365 * 100
                out[name[:-5]] = ann
            except (ValueError, ZeroDivisionError):
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("xexch gate: %s", e)
    return out


def funding_hyperliquid() -> dict:
    out = {}
    try:
        meta, ctxs = _get("https://api.hyperliquid.xyz/info", post={"type": "metaAndAssetCtxs"})
        universe = meta.get("universe", [])
        for i, ctx in enumerate(ctxs):
            if i >= len(universe):
                break
            name = universe[i].get("name", "")
            f = ctx.get("funding")
            if not name or f in (None, ""):
                continue
            try:
                out[name.upper()] = float(f) * 24 * 365 * 100  # 1ч фандинг!
            except ValueError:
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("xexch hyperliquid: %s", e)
    return out


VENUES = {
    "Binance": funding_binance,
    "Bybit": funding_bybit,
    "Gate": funding_gate,
    "Hyperliquid": funding_hyperliquid,
}


def fetch_all() -> dict:
    """{ASSET: {venue: annual_pct}} по всем биржам (параллельно)."""
    from concurrent.futures import ThreadPoolExecutor
    res = {}
    with ThreadPoolExecutor(max_workers=len(VENUES)) as ex:
        fut = {ex.submit(fn): name for name, fn in VENUES.items()}
        for f, name in [(f, fut[f]) for f in fut]:
            try:
                res[name] = f.result()
            except Exception:  # noqa: BLE001
                res[name] = {}
    by_asset: dict[str, dict] = {}
    for venue, table in res.items():
        for asset, ann in table.items():
            if abs(ann) > SANE_ABS_CAP:
                continue
            by_asset.setdefault(asset, {})[venue] = ann
    return by_asset


def find_spreads(by_asset: dict, min_spread: float = MIN_SPREAD_ANNUAL,
                 universe: set | None = None) -> list[ArbOpportunity]:
    """Список арб-возможностей (ликвидный актив на >=2 биржах, спред >= min_spread).

    universe=None → LIQUID_ASSETS (кредибильно/торгуемо). Передай set() или явный
    набор чтобы расширить (на свой риск — мусорные мемы дают фейк-спреды).
    """
    uni = LIQUID_ASSETS if universe is None else universe
    opps = []
    for asset, venues in by_asset.items():
        if uni and asset not in uni:
            continue
        if len(venues) < 2:
            continue
        hi_v = max(venues, key=lambda v: venues[v])
        lo_v = min(venues, key=lambda v: venues[v])
        spread = venues[hi_v] - venues[lo_v]
        if spread >= min_spread:
            opps.append(ArbOpportunity(asset=asset, long_venue=lo_v, short_venue=hi_v,
                                       long_ann=venues[lo_v], short_ann=venues[hi_v]))
    opps.sort(key=lambda o: o.spread, reverse=True)
    return opps


def format_arb_md(opps: list[ArbOpportunity], capital: float = 0.0) -> str:
    """Telegram HTML — пошагово для новичка. '' если возможностей нет."""
    if not opps:
        return ("🔀 <b>КРОСС-БИРЖЕВОЙ АРБ</b>\n"
                "Сейчас жирных спредов нет (фандинг по биржам сошёлся). "
                "Это норма в тихом рынке — спреды разлетаются в волатильность. Жди.")
    lines = ["🔀 <b>КРОСС-БИРЖЕВОЙ FUNDING-АРБ</b> (market-neutral, чего нет на одной бирже)\n"]
    for o in opps[:4]:
        lines.append(
            f"💠 <b>{o.asset}: спред {o.spread:.0f}% годовых</b>\n"
            f"Фандинг: {o.short_venue} {o.short_ann:+.0f}% / {o.long_venue} {o.long_ann:+.0f}%\n"
            f"1️⃣ <b>{o.short_venue}</b> (высокий фандинг): ШОРТ перп {o.asset}, плечо 1x"
            + (f", на ${capital/2:,.0f}" if capital else "") + "\n"
            f"2️⃣ <b>{o.long_venue}</b> (низкий): ЛОНГ перп {o.asset}, плечо 1x"
            + (f", на ${capital/2:,.0f} (равный объём!)" if capital else ", равный объём") + "\n"
            f"3️⃣ Цена пофиг (ноги гасятся) → собираешь ~{o.spread:.0f}% годовых разницы фандинга.\n"
            f"4️⃣ Держи пока спред не схлопнется. Следи за маржой на ОБЕИХ биржах.\n")
    lines.append("⚠️ Нужны аккаунты+депозит на обеих биржах. Закрывай обе ноги вместе. "
                 "Спред может схлопнуться быстро. Косты двух бирж учти.")
    lines.append("\n💡 Фиатный P2P-арбитраж (USDT по странам) — команда /p2p.")
    return "\n".join(lines)


def scan(min_spread: float = MIN_SPREAD_ANNUAL) -> list[ArbOpportunity]:
    return find_spreads(fetch_all(), min_spread)


__all__ = ["ArbOpportunity", "fetch_all", "find_spreads", "format_arb_md", "scan",
           "VENUES", "MIN_SPREAD_ANNUAL"]
