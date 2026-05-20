"""Stablecoin mint/redeem flows — чистая математика (stdlib only).

Зачем:
  USDT и USDC mints/redemptions on-chain — leading-indicator BTC на 24-72ч.
  Big mints (≫ $200M в сутки) = ликвидность втекает в крипту, обычно
  предшествует bullish-move. Big redemptions = выкуп → bearish pressure.
  Это работает потому что эмитенты минтят/жгут токены *по запросу институтов*,
  то есть на разнице со спотом — opportunistic capital flow.

Что НЕ делает (намеренно):
  * Не трогает signals.py / signal_trader.py / dynamic_risk.py / agents.py.
  * Не пишет в trading-tables. Своя таблица supply-snapshots и flow-snapshots.
  * Не использует numpy / pandas. Всё на stdlib — 2 числа в формуле.
  * Не делает netflows per-address (whale tracking) — это #6 roadmap'а.

Math:
  normalize_supply: raw_units на блокчейне (e.g. USDT-eth = 6 decimals) →
                    USD-эквивалент (float).
  delta_24h_usd:     supply_now_usd - supply_24h_ago_usd
                     (aggregated по всем chains для одного токена).
  flow_class:        пороги в долях % от total_supply (по умолчанию 0.25%
                     для mint/redeem, 1.0% для massive_mint/massive_redeem).
                     Использует %, а не абсолютные $$, чтобы автоматически
                     адаптироваться к росту общего саплая (USDT с 80B → 160B
                     за 2 года).

Конвенция знаков:
  delta_24h_usd > 0  → чистый mint  → bullish-leaning leading signal
  delta_24h_usd < 0  → чистый redeem → bearish-leaning leading signal

Внешние зависимости: только stdlib.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Стандартные decimals для основных стейблов.
TOKEN_DECIMALS = {
    "USDT": 6,
    "USDC": 6,
    "DAI": 18,
    "FRAX": 18,
    "TUSD": 18,
}

#: Пороги для классификации flow в долях от total supply (24h delta / supply).
#: 0.25% от $80B USDT = $200M — типичный «значимый» mint в 2024-25.
DEFAULT_MINT_PCT = 0.0025
DEFAULT_MASSIVE_MINT_PCT = 0.01

#: Лимит, выше которого raw_supply считаем глюком (защита от парсинга мусора).
SANITY_MAX_SUPPLY_USD = 1e15  # 1Q USD — невозможный supply на сегодня


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StablecoinSupplySnapshot:
    """Один срез totalSupply токена на одном чейне.

    `raw_supply_units` — int (на Ethereum для USDT это `totalSupply()` /
    10^decimals = USD). Храним как int для точности; нормализация при чтении.
    """

    token: str           # 'USDT' | 'USDC' | ...
    chain: str           # 'ethereum' | 'tron' | ...
    raw_supply_units: int  # totalSupply() в наименьших единицах
    decimals: int        # 6 для USDT/USDC, 18 для DAI/FRAX
    timestamp_ms: int    # когда снят snapshot


@dataclass(frozen=True)
class StablecoinFlowSignal:
    """Аггрегированный flow по токену (по всем chains).

    Все суммы в USD (нормализованные). delta_24h_usd может быть None если
    нет предыдущего снимка для сравнения.
    """

    token: str                       # 'USDT' | 'USDC' | ...
    timestamp_ms: int                # моменту current snapshot
    supply_total_usd: float          # сумма по всем chains *сейчас*
    delta_24h_usd: float | None      # change vs ~24h назад (None если нет prev)
    delta_pct_24h: float | None      # delta / supply (доля, не %)
    flow_class: str                  # см. classify_flow_class
    chains_used: tuple[str, ...] = ()


# ─── Normalization ──────────────────────────────────────────────────────────


def normalize_supply(*, raw_units: int, decimals: int) -> float:
    """Конверсия raw token supply (e.g. wei для erc20) в USD (float).

    raw = 80_000_000_000_000_000 (USDT-eth, decimals=6) → 80_000_000_000.0 USD.
    """
    if decimals < 0:
        raise ValueError(f"decimals must be >= 0, got {decimals}")
    if raw_units < 0:
        return 0.0
    if decimals == 0:
        return float(raw_units)
    return float(raw_units) / (10.0 ** decimals)


def aggregate_supply(
    snapshots: Sequence[StablecoinSupplySnapshot], *, token: str,
) -> tuple[float, tuple[str, ...]]:
    """Сумма supply (USD) по всем snapshots одного токена.

    Возвращает (total_usd, chains_used).
    """
    total = 0.0
    chains: list[str] = []
    for s in snapshots:
        if s.token.upper() != token.upper():
            continue
        v = normalize_supply(raw_units=s.raw_supply_units, decimals=s.decimals)
        if v <= 0 or not math.isfinite(v):
            continue
        if v > SANITY_MAX_SUPPLY_USD:
            logger.warning(
                "stablecoin supply OOB: %s/%s=%.2e — skip", s.token, s.chain, v,
            )
            continue
        total += v
        if s.chain not in chains:
            chains.append(s.chain)
    return (total, tuple(chains))


# ─── Flow classification ────────────────────────────────────────────────────


def classify_flow_class(
    delta_pct_24h: float | None,
    *,
    mint_threshold: float = DEFAULT_MINT_PCT,
    massive_mint_threshold: float = DEFAULT_MASSIVE_MINT_PCT,
) -> str:
    """Категория flow на основе delta_pct_24h (доля от текущего supply).

    'massive_mint' / 'mint' / 'neutral' / 'redeem' / 'massive_redeem' /
    'unknown' (если None или non-finite).
    """
    if delta_pct_24h is None or not math.isfinite(delta_pct_24h):
        return "unknown"
    d = float(delta_pct_24h)
    if d >= massive_mint_threshold:
        return "massive_mint"
    if d >= mint_threshold:
        return "mint"
    if d <= -massive_mint_threshold:
        return "massive_redeem"
    if d <= -mint_threshold:
        return "redeem"
    return "neutral"


# ─── Aggregation ─────────────────────────────────────────────────────────────


def build_flow_signal(
    *,
    token: str,
    current_snapshots: Sequence[StablecoinSupplySnapshot],
    previous_supply_usd: float | None,
    timestamp_ms: int,
    mint_threshold: float = DEFAULT_MINT_PCT,
    massive_mint_threshold: float = DEFAULT_MASSIVE_MINT_PCT,
) -> StablecoinFlowSignal:
    """Собрать StablecoinFlowSignal: суммировать current_snapshots по chains,
    сравнить с previous_supply_usd (если есть), классифицировать flow.

    previous_supply_usd ожидается из snapshot'а ~24ч назад (caller сам
    лукапит из БД).
    """
    total_usd, chains = aggregate_supply(current_snapshots, token=token)
    delta_usd: float | None = None
    delta_pct: float | None = None
    if previous_supply_usd is not None and previous_supply_usd > 0:
        delta_usd = total_usd - float(previous_supply_usd)
        if total_usd > 0:
            delta_pct = delta_usd / total_usd
    flow_class = classify_flow_class(
        delta_pct,
        mint_threshold=mint_threshold,
        massive_mint_threshold=massive_mint_threshold,
    )
    return StablecoinFlowSignal(
        token=token.upper(),
        timestamp_ms=int(timestamp_ms),
        supply_total_usd=total_usd,
        delta_24h_usd=delta_usd,
        delta_pct_24h=delta_pct,
        flow_class=flow_class,
        chains_used=chains,
    )


# ─── Event detection ─────────────────────────────────────────────────────────


def detect_flow_event(
    *,
    current: StablecoinFlowSignal,
    previous: StablecoinFlowSignal | None,
) -> str | None:
    """Сравнить current vs previous flow_class и вернуть тип события:

      * 'mint_burst'        — neutral/redeem → mint/massive_mint.
      * 'mint_cooldown'     — mint/massive_mint → neutral/redeem.
      * 'redeem_burst'      — neutral/mint → redeem/massive_redeem.
      * 'redeem_cooldown'   — redeem/massive_redeem → neutral/mint.
      * None — без смены режима или одна из точек unknown.
    """
    if previous is None:
        return None
    cur = current.flow_class
    prev = previous.flow_class
    if cur in {"unknown"} or prev in {"unknown"}:
        return None
    mint_states = {"mint", "massive_mint"}
    redeem_states = {"redeem", "massive_redeem"}
    cur_mint = cur in mint_states
    cur_redeem = cur in redeem_states
    prev_mint = prev in mint_states
    prev_redeem = prev in redeem_states
    if cur_mint and not prev_mint:
        return "mint_burst"
    if prev_mint and not cur_mint:
        return "mint_cooldown"
    if cur_redeem and not prev_redeem:
        return "redeem_burst"
    if prev_redeem and not cur_redeem:
        return "redeem_cooldown"
    return None


# ─── Formatters ──────────────────────────────────────────────────────────────


def format_flow_summary(
    signal: StablecoinFlowSignal, *, event: str | None = None,
) -> str:
    """Однострочный summary для логов."""

    def _usd(x: float | None) -> str:
        if x is None or not math.isfinite(x):
            return "n/a"
        a = abs(x)
        if a >= 1e9:
            return f"{x / 1e9:+.2f}B"
        if a >= 1e6:
            return f"{x / 1e6:+.1f}M"
        if a >= 1e3:
            return f"{x / 1e3:+.1f}K"
        return f"{x:+.0f}"

    def _supply(x: float) -> str:
        if x >= 1e9:
            return f"{x / 1e9:.2f}B"
        if x >= 1e6:
            return f"{x / 1e6:.1f}M"
        return f"{x:.0f}"

    pct = "n/a"
    if signal.delta_pct_24h is not None and math.isfinite(signal.delta_pct_24h):
        pct = f"{signal.delta_pct_24h * 100:+.3f}%"
    delta = _usd(signal.delta_24h_usd)
    supply = _supply(signal.supply_total_usd)
    chains = ",".join(signal.chains_used) if signal.chains_used else "n/a"
    tag = f" event={event}" if event else ""
    return (
        f"stablecoin-flow {signal.token} "
        f"supply={supply} delta24h={delta} ({pct}) "
        f"chains={chains} class={signal.flow_class}{tag}"
    )
