"""Concrete alert rules consumed by ``alert_engine.AlertEngine``."""

from refactor.services.alert_rules.btc_etf_outflow import BtcEtfOutflowRule
# LiquidationClusterRule удалён — ликвидации/деривативы (вне спот-режима).
from refactor.services.alert_rules.screener_anomaly import ScreenerAnomalyRule

__all__ = [
    "BtcEtfOutflowRule",
    "ScreenerAnomalyRule",
]
