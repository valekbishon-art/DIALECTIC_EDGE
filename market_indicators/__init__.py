"""
data_sources/
Модуль расширенных данных для AI-анализа.

Структура:
├── __init__.py          # Точка входа, экспорт основных функций
├── onchain.py           # On-chain метрики (MVRV, SOPR, Reserves, etc.)
├── macro_extended.py     # Расширенные макро данные (Yields, Balance Sheet)
├── scorer.py             # Система баллов для вердикта
└── aggregator.py         # Сборщик всех данных
"""

from .onchain import (
    fetch_onchain_metrics,
    format_onchain_for_agents,
    get_mvrv_signal,
    get_sopr_signal,
    get_exchange_reserves_signal,
)
from .macro_extended import (
    fetch_extended_macro,
    format_macro_extended_for_agents,
    get_yield_curve_signal,
    get_fed_balance_signal,
)
from .scorer import (
    calculate_market_score,
    get_critical_signals,
    format_scored_context_for_agents,
    format_signal_block_for_debates,
)
from .smart_money import (
    fetch_smart_money_signals,
    format_smart_money_for_agents,
    smart_money_score_contribution,
    SmartMoneySignals,
)
from .regime import (
    RegimeClassification,
    classify_regime,
    LABEL_CRISIS,
    LABEL_RANGING,
    LABEL_TRENDING,
    LABEL_UNKNOWN,
    LABEL_VOLATILE,
)
from .regime_io import (
    RegimeSignals,
    feature_enabled as regime_feature_enabled,
    fetch_regime_signals,
    format_regime_for_agents,
    regime_score_contribution,
)
from .smart_money_wallets import (
    SmartMoneyWalletsSignal,
    WalletNetFlow,
    aggregate_wallet_flows,
    compute_wallet_flow,
    LABEL_ACCUMULATING as SMW_LABEL_ACCUMULATING,
    LABEL_DISTRIBUTING as SMW_LABEL_DISTRIBUTING,
    LABEL_MIXED as SMW_LABEL_MIXED,
    LABEL_QUIET as SMW_LABEL_QUIET,
    LABEL_UNKNOWN as SMW_LABEL_UNKNOWN,
)
from .smart_money_wallets_io import (
    feature_enabled as smart_money_wallets_feature_enabled,
    fetch_smart_money_wallet_flows,
    format_smart_money_wallets_for_agents,
    smart_money_wallets_score_contribution,
)
from .aggregator import (
    build_enriched_context,
    enrich_prices_with_scores,
)

__all__ = [
    # On-chain
    "fetch_onchain_metrics",
    "format_onchain_for_agents",
    "get_mvrv_signal",
    "get_sopr_signal",
    "get_exchange_reserves_signal",
    # Macro extended
    "fetch_extended_macro",
    "format_macro_extended_for_agents",
    "get_yield_curve_signal",
    "get_fed_balance_signal",
    # Scoring
    "calculate_market_score",
    "get_critical_signals",
    "format_scored_context_for_agents",
    "format_signal_block_for_debates",
    # Smart-money
    "fetch_smart_money_signals",
    "format_smart_money_for_agents",
    "smart_money_score_contribution",
    "SmartMoneySignals",
    # Regime classifier (BOCPD)
    "RegimeClassification",
    "RegimeSignals",
    "classify_regime",
    "fetch_regime_signals",
    "format_regime_for_agents",
    "regime_feature_enabled",
    "regime_score_contribution",
    "LABEL_CRISIS",
    "LABEL_RANGING",
    "LABEL_TRENDING",
    "LABEL_UNKNOWN",
    "LABEL_VOLATILE",
    # Smart-money wallets (on-chain via Etherscan v2)
    "SmartMoneyWalletsSignal",
    "WalletNetFlow",
    "aggregate_wallet_flows",
    "compute_wallet_flow",
    "smart_money_wallets_feature_enabled",
    "fetch_smart_money_wallet_flows",
    "format_smart_money_wallets_for_agents",
    "smart_money_wallets_score_contribution",
    "SMW_LABEL_ACCUMULATING",
    "SMW_LABEL_DISTRIBUTING",
    "SMW_LABEL_MIXED",
    "SMW_LABEL_QUIET",
    "SMW_LABEL_UNKNOWN",
    # Aggregator
    "build_enriched_context",
    "enrich_prices_with_scores",
]
