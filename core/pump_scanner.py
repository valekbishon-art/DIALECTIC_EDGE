"""core.pump_scanner — тонкий ре-экспорт корневого pump_scanner.

Единственный источник истины — корневой ``pump_scanner.py`` (его же гоняет CI
и юнит-тесты). Бот, хендлеры и авто-рассылка исторически импортируют
``core.pump_scanner`` — поэтому здесь просто реэкспортируем публичный API,
включая новый предиктивный (опережающий) слой.
"""
from __future__ import annotations

from pump_scanner import (  # noqa: F401
    EXCHANGE_TRADE_URL,
    PumpConfig,
    PumpMetrics,
    PumpSignal,
    classify_signal,
    early_pump_score,
    evaluate_pump,
    format_pump_alert,
    max_rise_pct,
    merge_universes,
    momentum_acceleration,
    passes_static_filters,
    pct_change,
    scan_pumps,
    trade_url,
    volume_ramp,
    volume_ratio,
    window_anchor_price,
    window_pump_pct,
)

__all__ = [
    "PumpConfig", "PumpMetrics", "PumpSignal",
    "pct_change", "window_pump_pct", "window_anchor_price", "volume_ratio",
    "max_rise_pct", "passes_static_filters",
    "momentum_acceleration", "volume_ramp", "early_pump_score",
    "classify_signal",
    "evaluate_pump", "format_pump_alert", "merge_universes", "trade_url",
    "scan_pumps", "EXCHANGE_TRADE_URL",
]
