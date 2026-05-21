"""
aggregator.py — Сборщик всех данных для AI-анализа

Собирает:
1. On-chain метрики (CoinGecko)
2. Расширенные макро данные (FRED)
3. Скоринг и баллы
4. Цены (из основного потока)

И формирует единый контекст для AI агентов.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .onchain import fetch_onchain_metrics, format_onchain_for_agents, OnChainMetrics
from .macro_extended import fetch_extended_macro, format_macro_extended_for_agents, MacroExtended
from .scorer import (
    calculate_market_score,
    get_critical_signals,
    format_scored_context_for_agents,
    format_signal_block_for_debates,
    MarketScore
)
from .smart_money import (
    fetch_smart_money_signals,
    format_smart_money_for_agents,
    smart_money_score_contribution,
    SmartMoneySignals,
)
from .regime_io import (
    feature_enabled as regime_feature_enabled,
    fetch_regime_signals,
    format_regime_for_agents,
    get_hazard_rate,
    get_klines_limit,
    get_label_window,
    get_vol_high_annualized,
    regime_score_contribution,
    RegimeSignals,
)

logger = logging.getLogger(__name__)


@dataclass
class EnrichedData:
    """Все собранные данные + скоринг"""
    onchain: OnChainMetrics = None
    macro: MacroExtended = None
    score: MarketScore = None
    smart_money: SmartMoneySignals = None
    # Regime classifier (BOCPD + label). Заполняется только при
    # FEATURE_REGIME_CLASSIFIER=1; иначе остаётся дефолтным (label=UNKNOWN).
    regime: RegimeSignals = None

    def __post_init__(self):
        if self.onchain is None:
            self.onchain = OnChainMetrics()
        if self.macro is None:
            self.macro = MacroExtended()
        if self.score is None:
            self.score = MarketScore()
        if self.smart_money is None:
            self.smart_money = SmartMoneySignals()
        if self.regime is None:
            self.regime = RegimeSignals()


async def build_enriched_context(
    prices: dict = None,
    vix: Optional[float] = None,
    fear_greed: Optional[float] = None,
    sentiment_label: Optional[str] = None,
    trend_btc: Optional[str] = None,
    rsi_btc: Optional[float] = None,
    rsi_spy: Optional[float] = None,
) -> tuple[str, EnrichedData]:
    """
    Собирает все данные и формирует контекст для AI.

    Returns: (context_string, EnrichedData)
    """
    logger.info("[AGGREGATOR] Starting enriched context build...")
    enriched = EnrichedData()

    # Параллельно собираем данные
    onchain_task = fetch_onchain_metrics()
    macro_task = fetch_extended_macro()
    smart_money_task = fetch_smart_money_signals()

    # Regime classifier — за фичефлагом (AGENTS.md: «Любая фича за
    # фичефлагом»). Если OFF — не дёргаем Binance лишний раз.
    regime_enabled = regime_feature_enabled()
    regime_task = (
        fetch_regime_signals(
            limit=get_klines_limit(),
            hazard_rate=get_hazard_rate(),
            label_window=get_label_window(),
            vol_high_annualized=get_vol_high_annualized(),
        )
        if regime_enabled
        else None
    )

    logger.info(
        "[AGGREGATOR] Fetching on-chain + macro + smart-money%s in parallel...",
        " + regime" if regime_enabled else "",
    )
    if regime_task is not None:
        onchain_data, macro_data, smart_money_data, regime_data = await asyncio.gather(
            onchain_task, macro_task, smart_money_task, regime_task
        )
        enriched.regime = regime_data
    else:
        onchain_data, macro_data, smart_money_data = await asyncio.gather(
            onchain_task, macro_task, smart_money_task
        )

    enriched.onchain = onchain_data
    enriched.macro = macro_data
    enriched.smart_money = smart_money_data
    logger.info(
        "[AGGREGATOR] On-chain + macro + smart-money%s fetched OK",
        " + regime" if regime_enabled else "",
    )

    # Проверяем критические стоп-факторы
    critical_bearish, critical_bullish = get_critical_signals(
        mvrv=enriched.onchain.mvrv if enriched.onchain else None,
        vix=vix,
        fear_greed=fear_greed,
    )
    logger.info(f"[AGGREGATOR] Critical signals — bearish={critical_bearish}, bullish={critical_bullish}")

    # Вычисляем скор
    score = calculate_market_score(
        # Макро
        vix=vix,
        yield_curve_spread=enriched.macro.yield_spread if enriched.macro else None,
        qe_qt_mode=enriched.macro.qe_qt_mode if enriched.macro else None,
        credit_spread=enriched.macro.hy_spread if enriched.macro else None,

        # Ончейн
        mvrv=enriched.onchain.mvrv if enriched.onchain else None,
        sopr=enriched.onchain.sopr if enriched.onchain else None,
        exchange_reserves_trend="down" if enriched.onchain and getattr(enriched.onchain, "reserves_signal", None) and "HODL" in enriched.onchain.reserves_signal else "up",

        # Технические
        rsi=rsi_btc,
        trend=trend_btc,

        # Сентимент
        fear_greed=fear_greed,
        sentiment=sentiment_label,
    )

    # Smart-money вклад в общий скор — применяем ДО финального вердикта,
    # чтобы verdict учитывал институциональные сигналы.
    sm_delta, sm_bull, sm_bear = smart_money_score_contribution(enriched.smart_money)
    if sm_delta != 0:
        score.total_score += sm_delta
        # Не льём в macro/onchain/tech/sentiment — смарт-мани отдельный класс,
        # но выводим причины в explanation через bullish/bearish_signals.
        score.bullish_signals.extend(sm_bull)
        score.bearish_signals.extend(sm_bear)
        logger.info(f"[AGGREGATOR] Smart-money score delta: {sm_delta:+d} (bull={len(sm_bull)} bear={len(sm_bear)})")

    # Regime classifier вклад — только если фича включена и был fetched.
    # Консервативные веса (±1 max) — собираем baseline перед раскруткой.
    if regime_enabled:
        rg_delta, rg_bull, rg_bear = regime_score_contribution(enriched.regime)
        if rg_delta != 0:
            score.total_score += rg_delta
        if rg_bull:
            score.bullish_signals.extend(rg_bull)
        if rg_bear:
            score.bearish_signals.extend(rg_bear)
        if rg_delta != 0 or rg_bull or rg_bear:
            logger.info(
                f"[AGGREGATOR] Regime score delta: {rg_delta:+d} (bull={len(rg_bull)} bear={len(rg_bear)})"
            )

        # Пересчитываем preliminary verdict после добавки smart-money
        if score.total_score >= 4:
            score.preliminary_verdict = "BULLISH"
        elif score.total_score <= -4:
            score.preliminary_verdict = "BEARISH"
        else:
            score.preliminary_verdict = "NEUTRAL"

    # Добавляем стоп-факторы (могут оверрайднуть verdict)
    score.has_critical_bearish = critical_bearish
    score.has_critical_bullish = critical_bullish

    # Финальный вердикт
    if critical_bearish:
        score.final_verdict = "BEARISH"
    elif critical_bullish:
        score.final_verdict = "BULLISH"
    else:
        score.final_verdict = score.preliminary_verdict

    enriched.score = score

    logger.info(f"[AGGREGATOR] Scoring — total={score.total_score}, macro={score.macro_score}, onchain={score.onchain_score}, tech={score.technical_score}, sentiment={score.sentiment_score}")
    logger.info(f"[AGGREGATOR] Verdict — preliminary={score.preliminary_verdict}, final={score.final_verdict}")

    # Формируем контекст
    context_parts = []

    # 1. On-chain метрики
    onchain_str = format_onchain_for_agents(enriched.onchain)
    context_parts.append(onchain_str)

    # 2. Расширенные макро
    macro_str = format_macro_extended_for_agents(enriched.macro)
    context_parts.append("")
    context_parts.append(macro_str)

    # 3. Система баллов
    scored_str = format_scored_context_for_agents(enriched.score)
    context_parts.append("")
    context_parts.append(scored_str)

    # 4. SMART-MONEY блок (институциональные сигналы)
    smart_money_str = format_smart_money_for_agents(enriched.smart_money)
    context_parts.append("")
    context_parts.append(smart_money_str)

    # 4b. РЕЖИМ РЫНКА (BOCPD-based regime classifier) — только если fetched.
    if regime_enabled and enriched.regime is not None:
        regime_str = format_regime_for_agents(enriched.regime)
        context_parts.append("")
        context_parts.append(regime_str)

    # 5. СИГНАЛ БЛОК для Bull/Bear дебатов
    signal_block = format_signal_block_for_debates(enriched.score, enriched.onchain, enriched.macro)
    context_parts.append("")
    context_parts.append(signal_block)

    # 5. Финальный вердикт для агентов
    context_parts.append("")
    verdict_emoji = "🟢" if score.final_verdict == "BULLISH" else "🔴" if score.final_verdict == "BEARISH" else "⚪"
    context_parts.append(f"{verdict_emoji} СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: **{score.final_verdict}**")

    if critical_bearish:
        context_parts.append("⚠️ СТОП-ФАКТОР: Критические условия указывают на медвежий рынок!")
    elif critical_bullish:
        context_parts.append("🔵 СТОП-ФАКТОР: Критические условия указывают на историческое дно!")

    logger.info(f"[AGGREGATOR] DONE — context length={len(context_parts)} parts, final_verdict={score.final_verdict}")
    return "\n".join(context_parts), enriched


def enrich_prices_with_scores(
    prices: dict,
    score: MarketScore,
    enriched: EnrichedData
) -> dict:
    """
    Обогащает словарь цен дополнительными метриками.
    Используется для передачи в шаблоны и отчёты.
    """
    enriched_prices = prices.copy()

    # Добавляем on-chain
    if enriched.onchain:
        enriched_prices["MVRV"] = enriched.onchain.mvrv
        enriched_prices["MVRV_SIGNAL"] = enriched.onchain.mvrv_signal
        enriched_prices["SOPR"] = enriched.onchain.sopr
        enriched_prices["SOPR_SIGNAL"] = enriched.onchain.sopr_signal

    # Добавляем макро
    if enriched.macro:
        enriched_prices["FED_BALANCE"] = enriched.macro.fed_balance_billions
        enriched_prices["FED_SIGNAL"] = enriched.macro.qe_qt_mode
        enriched_prices["YIELD_10Y"] = enriched.macro.yield_10y
        enriched_prices["YIELD_2Y"] = enriched.macro.yield_2y
        enriched_prices["YIELD_SPREAD"] = enriched.macro.yield_spread
        enriched_prices["CREDIT_SPREAD"] = enriched.macro.hy_spread

    # Добавляем скор
    if score:
        enriched_prices["MARKET_SCORE"] = score.total_score
        enriched_prices["MARKET_VERDICT"] = score.final_verdict
        enriched_prices["SCORE_MACRO"] = score.macro_score
        enriched_prices["SCORE_ONCHAIN"] = score.onchain_score
        enriched_prices["SCORE_TECHNICAL"] = score.technical_score
        enriched_prices["SCORE_SENTIMENT"] = score.sentiment_score

    # Smart-money
    if enriched.smart_money:
        sm = enriched.smart_money
        if sm.top_trader_ls_ratio is not None:
            enriched_prices["SM_TOP_TRADER_LS"] = sm.top_trader_ls_ratio
        if sm.top_trader_ls_per_symbol:
            enriched_prices["SM_TOP_TRADER_LS_PER_SYMBOL"] = dict(sm.top_trader_ls_per_symbol)
        if sm.coinbase_premium_pct is not None:
            enriched_prices["SM_COINBASE_PREMIUM"] = sm.coinbase_premium_pct
        if sm.cme_basis_pct is not None:
            enriched_prices["SM_CME_BASIS"] = sm.cme_basis_pct
        if sm.funding_avg_pct is not None:
            enriched_prices["SM_FUNDING_AVG"] = sm.funding_avg_pct
        if sm.funding_alignment:
            enriched_prices["SM_FUNDING_ALIGN"] = sm.funding_alignment

    return enriched_prices


# ─── Test ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def test():
        print("=== ТЕСТ АГРЕГАТОРА ===\n")

        # Пример данных из основного потока
        context, enriched = await build_enriched_context(
            vix=18.5,
            fear_greed=45,
            sentiment_label="NEUTRAL",
            trend_btc="UPTREND",
            rsi_btc=62,
        )

        print("=== СФОРМИРОВАННЫЙ КОНТЕКСТ ===")
        print(context)
        print()
        print("=== ENRICHED DATA ===")
        print(f"MVRV: {enriched.onchain.mvrv:.2f}")
        print(f"SOPR: {enriched.onchain.sopr:.3f}")
        print(f"Fed Balance: ${enriched.macro.fed_balance_billions:.0f}B")
        print(f"QE/QT: {enriched.macro.qe_qt_mode}")
        print(f"Yield Curve: {enriched.macro.yield_spread:.2f}%")
        print()
        print(f"FINAL VERDICT: {enriched.score.final_verdict}")
        print(f"Total Score: {enriched.score.total_score:+d}")

    asyncio.run(test())
