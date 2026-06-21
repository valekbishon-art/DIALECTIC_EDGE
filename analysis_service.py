"""
Analysis orchestration service used by handlers and Telegram commands.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

from agents import DebateOrchestrator
from core.horizons import DEFAULT_HORIZON_KEY, HorizonPack, get_horizon
# fetch_full_context из старого файла data_sources.py
from data_sources import fetch_full_context
from database import log_report
from github_export import get_previous_digest, push_digest_cache
from meta_analyst import get_meta_context
from news_fetcher import NewsFetcher
from report_sanitizer import sanitize_full_report
from sentiment import analyze_and_filter_async, format_for_agents
from storage import Storage
from config import DIGEST_SNAPSHOT_MAX_CHARS
from prompt_versions import get_digest_prompt_manifest
from tracker import save_predictions_from_report
from user_profile import build_profile_instruction, get_profile
from web_search import get_full_realtime_context, search_news_context

logger = logging.getLogger(__name__)

_fetcher = NewsFetcher()
_storage = Storage()
_orchestrator: DebateOrchestrator | None = None


def _get_orchestrator() -> DebateOrchestrator:
    """Lazy module-level singleton: avoids re-instantiating Bull/Bear/Verifier/Synth/Speechwriter on every request."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = DebateOrchestrator()
    return _orchestrator


def _as_float(value) -> float | None:
    """Coerce a prices_dict entry to a float for downstream comparators.

    `prices_dict` mixes shapes: numeric scalars, dicts like
    {"price": 17.3, "change_24h": ...}, dicts with a `"value"` key (e.g. F&G),
    and None. The market_indicators scoring code assumes plain numbers; passing
    a dict triggers `TypeError: '>' not supported between instances of 'dict'
    and 'int'`. This helper normalizes all of those into Optional[float].
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("price", "value"):
            inner = value.get(key)
            if isinstance(inner, (int, float)):
                return float(inner)
            if isinstance(inner, str):
                try:
                    return float(inner.replace(",", "").strip())
                except ValueError:
                    continue
        return None
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def build_digest_persist_metadata(
    *,
    custom_mode: bool,
    news_context: str,
    live_prices: str,
    profile: dict,
    sentiment_result,
    prices_dict: dict,
) -> tuple[dict, dict]:
    """
    Версии промптов/пайплайна + усечённый снимок входов модели на момент дайджеста.
    """
    from datetime import datetime, timezone

    def _clip(s: str, n: int) -> str:
        s = s or ""
        return s if len(s) <= n else s[: n - 3] + "..."

    lean_prices: dict = {}
    for k, v in (prices_dict or {}).items():
        if k == "SENTIMENT":
            lean_prices[k] = v
        elif isinstance(v, (int, float)):
            lean_prices[k] = float(v)
        elif isinstance(v, dict) and "price" in v:
            try:
                lean_prices[k] = float(v.get("price"))
            except (TypeError, ValueError):
                lean_prices[k] = str(v)[:120]

    prompt_versions = get_digest_prompt_manifest()
    snapshot = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "custom_mode": custom_mode,
        "profile_excerpt": {
            "risk": profile.get("risk"),
            "horizon": profile.get("horizon"),
            "markets": profile.get("markets"),
        },
        "sentiment": {
            "label": getattr(sentiment_result, "label", None),
            "score": getattr(sentiment_result, "score", None),
            "confidence": getattr(sentiment_result, "confidence", None),
        },
        "news_context_excerpt": _clip(news_context, min(DIGEST_SNAPSHOT_MAX_CHARS // 2, 8000)),
        "live_prices_excerpt": _clip(str(live_prices), 4000),
        "prices_dict_lean": lean_prices,
        "notes": "Полный news_context усечён; отчёт агентов хранится отдельно в predictions/дебатах.",
    }
    return prompt_versions, snapshot


async def run_full_analysis(
    user_id: int,
    custom_news: str = "",
    custom_mode: bool = False,
    horizon: str | HorizonPack | None = None,
) -> Tuple[str, Dict]:
    """
    Run the current production analysis pipeline and return full report + prices.

    `horizon` selects the planning timeframe (intraday/swing/position) and is
    propagated into Synth's prompt overlay, the deterministic plan renderer and
    the digest cache key. Defaults to swing for backward compatibility.
    """
    from database import get_predictions_summary

    pack = horizon if isinstance(horizon, HorizonPack) else get_horizon(horizon if isinstance(horizon, str) else None)
    logger.info(f"[ANALYSIS] run_full_analysis user={user_id} custom={custom_mode} horizon={pack.key}")

    tasks = [
        _fetcher.fetch_all(),
        fetch_full_context(),
        get_full_realtime_context(),
        get_profile(user_id),
        get_meta_context(),
        get_previous_digest(),
        get_predictions_summary(days=5),
    ]
    news, geo_context, realtime_result, profile, meta_context, prev_digest, predictions_summary = await asyncio.gather(
        *tasks, return_exceptions=True
    )

    if isinstance(news, Exception):
        logger.warning("news fetch failed: %s", news)
        news = ""
    if isinstance(geo_context, Exception):
        logger.warning("geo context failed: %s", geo_context)
        geo_context = ""
    if isinstance(profile, Exception):
        logger.warning("profile load failed: %s", profile)
        profile = {"risk": "moderate", "horizon": "swing", "markets": "all", "capital": "unknown"}
    if isinstance(meta_context, Exception):
        logger.warning("meta context failed: %s", meta_context)
        meta_context = ""
    if isinstance(prev_digest, Exception):
        logger.warning("previous digest failed: %s", prev_digest)
        prev_digest = ""
    if isinstance(predictions_summary, Exception):
        logger.warning("predictions summary failed: %s", predictions_summary)
        predictions_summary = ""

    if isinstance(realtime_result, Exception):
        logger.warning("realtime context failed: %s", realtime_result)
        prices_dict, live_prices = {}, ""
    elif isinstance(realtime_result, tuple) and len(realtime_result) == 2:
        prices_dict, live_prices = realtime_result
    else:
        prices_dict, live_prices = {}, ""

    profile_instruction = build_profile_instruction(profile)
    if custom_mode and custom_news:
        web_context = await search_news_context(custom_news)
        news_context = (
            f"ТЕМА АНАЛИЗА: {custom_news}\n\n"
            f"{web_context}\n\n{geo_context}\n\n{meta_context}"
        )
    else:
        news_context = f"{geo_context}\n\n=== НОВОСТИ ===\n{news}\n\n{meta_context}"

    if prev_digest and not custom_mode:
        news_context += f"\n\n{prev_digest}"
    
    if predictions_summary and not custom_mode:
        news_context += f"\n\n{predictions_summary}"

    # FIX: sentiment must be computed BEFORE on-chain enrichment because
    # build_enriched_context() reads sentiment_result.label.
    sentiment_result, confidence_instruction = await analyze_and_filter_async(
        news_context,
        str(live_prices),
    )
    sentiment_block = format_for_agents(sentiment_result, confidence_instruction)
    prices_dict = dict(prices_dict) if prices_dict else {}
    prices_dict["SENTIMENT"] = {
        "score": sentiment_result.score,
        "label": sentiment_result.label,
        "confidence": sentiment_result.confidence,
    }

    # ═══ ELITE DATA ENRICHMENT ═══
    # Добавляем данные уровня хедж-фондов: деривативы, макро, Fear&Greed
    if not custom_mode:
        try:
            from core.data_enricher import enrich_context, format_enriched_context
            elite_context = await enrich_context(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
            elite_block = format_enriched_context(elite_context)
            news_context += f"\n\n{elite_block}"
            logger.info("Elite data enrichment: OK")
        except Exception as e:
            logger.warning(f"Elite data enrichment failed: {e}")

    # ═══ ON-CHAIN + EXTENDED MACRO + SCORING ═══
    # Добавляем MVRV, SOPR, Fed Balance, Yields, Yield Curve, система баллов
    stop_factor: str | None = None  # 'bearish'|'bullish'|None — code-side override into renderer
    if not custom_mode:
        try:
            from market_indicators import build_enriched_context, enrich_prices_with_scores
            logger.info("[ANALYSIS] Building enriched context with market_indicators...")
            enriched_context_str, enriched_data = await build_enriched_context(
                vix=_as_float(prices_dict.get("VIX")),
                fear_greed=_as_float(prices_dict.get("FEAR_GREED")),
                sentiment_label=sentiment_result.label if sentiment_result else None,
                trend_btc=prices_dict.get("TREND_BTC"),
                rsi_btc=_as_float(prices_dict.get("RSI_BTC")),
                rsi_spy=_as_float(prices_dict.get("RSI_SPY")),
            )
            news_context += f"\n\n{enriched_context_str}"
            
            # Обогащаем prices_dict для отчёта и графиков
            if enriched_data:
                prices_dict = enrich_prices_with_scores(prices_dict, enriched_data.score, enriched_data)
            # Code-side stop-factor override: MVRV/VIX/F&G в экстремальных зонах →
            # запрещаем направленные планы, чтобы LLM не натянул LONG в эйфории /
            # SHORT на историческом дне. Bearish имеет приоритет — он опаснее.
            if enriched_data and enriched_data.score:
                if enriched_data.score.has_critical_bearish:
                    stop_factor = "bearish"
                elif enriched_data.score.has_critical_bullish:
                    stop_factor = "bullish"
                if stop_factor:
                    logger.warning(f"[STOP-FACTOR] active: {stop_factor} → directional plans will be coerced to CASH")

            logger.info(f"[ANALYSIS] On-chain + Macro + Scoring: OK (verdict={enriched_data.score.final_verdict}, score={enriched_data.score.total_score:+d}, stop_factor={stop_factor})")
            
            # Сохраняем в GitHub cache
            try:
                from github_export import save_market_cache
                _cache_task = asyncio.create_task(save_market_cache(
                    mvrv=enriched_data.onchain.mvrv if enriched_data.onchain else 0,
                    sopr=enriched_data.onchain.sopr if enriched_data.onchain else 0,
                    fed_balance=enriched_data.macro.fed_balance_billions if enriched_data.macro else 0,
                    qe_qt_mode=enriched_data.macro.qe_qt_mode if enriched_data.macro else "UNKNOWN",
                    yield_spread=enriched_data.macro.yield_spread if enriched_data.macro else 0,
                    hy_spread=enriched_data.macro.hy_spread if enriched_data.macro else 0,
                    vix=prices_dict.get("VIX", 0),
                    fear_greed=prices_dict.get("FEAR_GREED", 0),
                    total_score=enriched_data.score.total_score,
                    final_verdict=enriched_data.score.final_verdict,
                ))
                _cache_task.add_done_callback(
                    lambda t: t.exception() and logger.debug(f"[ANALYSIS] Cache task error: {t.exception()}")
                )
            except Exception as _e:
                logger.debug(f"[ANALYSIS] Cache save skipped: {_e}")
        except Exception as e:
            logger.warning(f"On-chain/macro/scoring failed: {e}")

    # Numeric prices for anti-stale-price guard in Speechwriter.
    numeric_market_prices: dict[str, float] = {}
    for _sym in ("BTC", "ETH", "SOL", "BNB", "XRP", "SPX", "NDX", "VIX", "GOLD", "OIL_WTI", "DXY"):
        _entry = prices_dict.get(_sym) if isinstance(prices_dict, dict) else None
        if isinstance(_entry, dict):
            _p = _entry.get("price")
            if isinstance(_p, (int, float)) and _p > 0:
                numeric_market_prices[_sym] = float(_p)
    if "SPX" in numeric_market_prices:
        numeric_market_prices.setdefault("SPY", numeric_market_prices["SPX"])
    if "GOLD" in numeric_market_prices:
        numeric_market_prices.setdefault("GLD", numeric_market_prices["GOLD"])
    if "OIL_WTI" in numeric_market_prices:
        numeric_market_prices.setdefault("WTI", numeric_market_prices["OIL_WTI"])
        numeric_market_prices.setdefault("USO", numeric_market_prices["OIL_WTI"])

    # ATR keys прокидываются отдельно (pre-live-hardening): web_search кладёт
    # их как top-level prices["ATR_BTC"] и т.д. — иначе ATR-aware SL guard
    # падает к fixed-fallback.
    for _sym in ("BTC", "ETH", "SOL", "BNB", "XRP"):
        _atr_key = f"ATR_{_sym}"
        _atr_val = prices_dict.get(_atr_key) if isinstance(prices_dict, dict) else None
        if isinstance(_atr_val, (int, float)) and _atr_val > 0:
            numeric_market_prices[_atr_key] = float(_atr_val)

    report = await _get_orchestrator().run_debate(
        news_context=news_context,
        live_prices=live_prices,
        profile_instruction=profile_instruction + sentiment_block,
        custom_mode=custom_mode,
        horizon=pack,
        stop_factor=stop_factor,
        market_prices=numeric_market_prices,
    )
    report, removed_lines = sanitize_full_report(report)
    if removed_lines:
        logger.info("sanitizer removed %s lines", removed_lines)

    conf_raw = sentiment_result.confidence
    conf_map = {"HIGH": 0.85, "MEDIUM": 0.55, "LOW": 0.25, "EXTREME": 0.95}
    if isinstance(conf_raw, str):
        conf_num = conf_map.get(conf_raw.upper(), 0.5)
    else:
        try:
            conf_num = float(conf_raw)
        except (TypeError, ValueError):
            conf_num = 0.5

    stars = max(1, min(5, round(conf_num * 5)))
    pct = int(conf_num * 100)
    separator = "─" * 30 + "\n"
    signal_line = (
        f"📶 *Уровень сигнала:* {'⭐' * stars}{'☆' * (5 - stars)} "
        f"({pct}% — уверенность FinBERT в тоне новостей)\n"
        f"_Это не гарантированное направление рынка._\n\n"
    )
    report = report.replace(separator, separator + signal_line, 1)

    source = custom_news[:300] if custom_mode else str(news)[:300]
    pv, snap = build_digest_persist_metadata(
        custom_mode=custom_mode,
        news_context=news_context,
        live_prices=str(live_prices),
        profile=profile if isinstance(profile, dict) else {},
        sentiment_result=sentiment_result,
        prices_dict=prices_dict,
    )
    await save_predictions_from_report(
        report,
        source_news=source,
        prompt_versions=pv,
        model_inputs_snapshot=snap,
    )
    await log_report(
        user_id,
        "analyze" if custom_mode else "daily",
        source,
        report[:500],
    )

    if not custom_mode:
        _storage.cache_report(report, prices_dict, owner_user_id=user_id, horizon=pack.key)
        try:
            date_str = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
            from main import parse_report_parts
            parts = parse_report_parts(report)
            full_debates = ""
            if parts.get("rounds"):
                blocks = []
                for i, r in enumerate(parts["rounds"], 1):
                    blocks.append(f"{'='*12} Раунд {i} {'='*12}\n\n{r}")
                full_debates = "\n\n".join(blocks)
            asyncio.create_task(push_digest_cache(report, date_str, full_debates))
        except Exception as exc:
            logger.warning("digest cache push failed: %s", exc)

    return report, prices_dict
