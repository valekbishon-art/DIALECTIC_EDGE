"""Smart-money on-chain wallet flows — чистая математика (stdlib only).

Идея:
    Известные публично-атрибутированные «smart money» / market-maker /
    institutional кошельки на Ethereum дают сигнал по тому, **накапливают**
    они ETH (bullish leading indicator) или **раздают** (bearish).

    В отличие от ритейла (фьючерсный long/short ratio, sentiment), smart-money
    кошельки оперируют **на спот-рынке** и **большими размерами**. Их netflow
    за 24-48 часов часто опережает ценовое движение на 6-24ч.

Сигналы:
    1. **Aggregate net ETH flow** — сумма net ETH inflow по N кошелькам
       за lookback_hours. >0 = накопление, <0 = распределение.
    2. **Wallet alignment** — сколько % кошельков выровнены в одну сторону
       (все покупают / все продают / разделены). Alignment > 0.75 в одну
       сторону = сильный сигнал.
    3. **Wallet activity** — общее количество транзакций. Низкая = «затишье»,
       высокая = «активность» (полезно как мета-флаг для дебатёров).

Что НЕ делает (намеренно):
    * Не трогает торговую логику в signal_trader.py / signals.py / agents.py.
    * Не использует numpy / pandas — pure stdlib, попадает в `unit-fast` CI.
    * Не персистит state — net flow считается из tx history (без snapshot DB).
    * Не пытается классифицировать **куда** уходят средства (exchange vs DeFi).
      Это next-iteration; сейчас базовый balance-delta сигнал.

Конвенция знаков:
    net_eth_flow > 0  → кошелёк ПОЛУЧИЛ больше ETH чем отправил → accumulating
    net_eth_flow < 0  → кошелёк ОТПРАВИЛ больше ETH чем получил → distributing

    Агрегация: sign(sum(per-wallet net flows)). Bullish если smart money копит.

Внешние зависимости: только stdlib.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ─── Константы ───────────────────────────────────────────────────────────────

#: Ethereum native ETH имеет 18 decimals.
ETH_DECIMALS = 18

#: Пороги классификации aggregate flow в ETH (а не в долях supply,
#: т.к. supply ETH ~120M = постоянен, в отличие от стейблов). 1000 ETH ≈ $3M
#: в моменте; 5000 ETH ≈ $15M = «значимый» smart-money сигнал.
DEFAULT_FLOW_THRESHOLD_ETH = 1000.0
DEFAULT_STRONG_FLOW_THRESHOLD_ETH = 5000.0

#: Порог alignment — какая доля кошельков должна быть синхронна для
#: «strong alignment». 0.75 = 6 из 8 кошельков идут в одну сторону.
DEFAULT_ALIGNMENT_THRESHOLD = 0.75

#: Минимальный net flow per-wallet (в ETH), ниже которого считаем «дрейф» /
#: газ-перевод, а не реальный сигнал. 10 ETH ≈ $30K — отсекает мелочь.
PER_WALLET_NOISE_FLOOR_ETH = 10.0

#: Labels агрегата.
LABEL_ACCUMULATING = "accumulating"  # smart money net buying
LABEL_DISTRIBUTING = "distributing"  # smart money net selling
LABEL_MIXED = "mixed"                # signals offset each other
LABEL_QUIET = "quiet"                # all flows below noise floor
LABEL_UNKNOWN = "unknown"            # not enough data


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WalletNetFlow:
    """Net ETH flow одного кошелька за lookback-period.

    `net_eth_flow` = received_eth - sent_eth (ETH-номинированный, NOT raw wei).
    Положительный = кошелёк накапливает; отрицательный = распределяет.

    `tx_count` — суммарное число tx где address фигурирует как from или to.
    Помогает дебатёрам отличить тихий «дрейф» от активной перетряски.

    `truncated` — флаг, что мы вытянули максимум tx-ов от API и могли пропустить
    более ранние из lookback-окна. Для high-frequency wallets полезно знать.
    """

    address: str               # 0x... lowercase
    label: str                 # human-readable: "Jump Trading", "Wintermute", ...
    received_eth: float        # сумма ETH полученного за окно
    sent_eth: float            # сумма ETH отправленного за окно (включая gas? нет — только value)
    net_eth_flow: float        # received - sent
    tx_count: int              # количество tx в окне
    truncated: bool = False    # True если API вернул >= max_offset txs (могли пропустить старые)


@dataclass
class SmartMoneyWalletsSignal:
    """Aggregate smart-money signal по всем отслеживаемым кошелькам."""

    # Per-wallet данные (для трассировки и format).
    wallets: list[WalletNetFlow] = field(default_factory=list)

    # Aggregate netflow.
    total_net_eth_flow: float = 0.0  # сумма net flows всех wallets
    total_received_eth: float = 0.0
    total_sent_eth: float = 0.0

    # Alignment metrics.
    accumulating_count: int = 0      # сколько wallets с net flow > noise floor
    distributing_count: int = 0      # сколько wallets с net flow < -noise floor
    neutral_count: int = 0           # сколько wallets с |net flow| <= noise floor
    alignment_ratio: float = 0.0     # max(accum, distr) / total — 0..1

    # Classification.
    label: str = LABEL_UNKNOWN       # accumulating / distributing / mixed / quiet / unknown
    is_strong_signal: bool = False   # alignment >= threshold И |flow| >= strong_threshold

    # Meta.
    n_wallets_tracked: int = 0
    lookback_hours: int = 0
    timestamp_ms: int = 0
    source: str = ""                 # "etherscan-v2-chainid-1"


# ─── Math helpers ────────────────────────────────────────────────────────────


def wei_to_eth(wei: int | str) -> float:
    """Конвертировать wei (int или decimal-string) в ETH (float).

    Etherscan API возвращает balance/value как строку (числа > 2^53 не лезут
    в JS Number). Парсим строго через int(str(.)).
    """
    try:
        units = int(str(wei))
    except (TypeError, ValueError) as e:
        logger.warning("wei_to_eth parse failed: %r → %s", wei, e)
        return 0.0
    if units < 0:
        return 0.0
    return units / (10**ETH_DECIMALS)


def compute_wallet_flow(
    *,
    address: str,
    label: str,
    txs: list[dict],
    since_timestamp_s: int,
    truncated: bool = False,
) -> WalletNetFlow:
    """Из списка Etherscan tx-объектов посчитать net ETH flow одного кошелька.

    Каждый tx имеет поля:
      - `from`  (lowercase address): кто отправил
      - `to`    (lowercase address): кто получил
      - `value` (decimal string в wei): сумма
      - `timeStamp` (decimal string seconds since epoch)
      - `isError` ("0" или "1"): "1" = failed tx — value не передавался

    Логика:
      * Только tx с timeStamp >= since_timestamp_s.
      * Только tx с isError == "0" (успешные).
      * Если from == address → outflow (sent_eth += value).
      * Если to == address → inflow (received_eth += value).
      * Self-tx (from == to == address) → симметрично оба, net = 0.
      * value=0 (contract-only call) → tx_count считается, но flow=0.
    """
    addr = address.lower().strip()
    received = 0.0
    sent = 0.0
    count = 0
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        # Skip failed tx (no ETH actually moved).
        if str(tx.get("isError", "0")) == "1":
            continue
        try:
            ts = int(str(tx.get("timeStamp", "0")))
        except (TypeError, ValueError):
            continue
        if ts < since_timestamp_s:
            continue
        from_addr = str(tx.get("from", "")).lower()
        to_addr = str(tx.get("to", "")).lower()
        if from_addr != addr and to_addr != addr:
            # Дефенсив: txlist по address не должен возвращать чужие, но мало ли.
            continue
        try:
            value_wei = int(str(tx.get("value", "0")))
        except (TypeError, ValueError):
            value_wei = 0
        eth = value_wei / (10**ETH_DECIMALS)
        if from_addr == addr:
            sent += eth
        if to_addr == addr:
            received += eth
        count += 1
    net = received - sent
    return WalletNetFlow(
        address=addr,
        label=label,
        received_eth=received,
        sent_eth=sent,
        net_eth_flow=net,
        tx_count=count,
        truncated=truncated,
    )


# ─── Aggregation & classification ───────────────────────────────────────────


def aggregate_wallet_flows(
    wallets: list[WalletNetFlow],
    *,
    noise_floor_eth: float = PER_WALLET_NOISE_FLOOR_ETH,
    flow_threshold_eth: float = DEFAULT_FLOW_THRESHOLD_ETH,
    strong_flow_threshold_eth: float = DEFAULT_STRONG_FLOW_THRESHOLD_ETH,
    alignment_threshold: float = DEFAULT_ALIGNMENT_THRESHOLD,
    lookback_hours: int = 24,
    timestamp_ms: int = 0,
    source: str = "",
) -> SmartMoneyWalletsSignal:
    """Превратить per-wallet net flows в агрегированный signal с классификацией.

    Логика label:
      * `unknown`        — нет данных (пустой список или все wallets без txs)
      * `quiet`          — все net flows ниже noise_floor
      * `accumulating`   — sum > flow_threshold AND alignment в сторону buy
      * `distributing`   — sum < -flow_threshold AND alignment в сторону sell
      * `mixed`          — иначе (сильные потоки, но нет alignment)

    `is_strong_signal` = (alignment_ratio >= alignment_threshold) AND
                         (|total_net_eth_flow| >= strong_flow_threshold_eth)
    """
    sig = SmartMoneyWalletsSignal(
        wallets=list(wallets),
        n_wallets_tracked=len(wallets),
        lookback_hours=max(1, int(lookback_hours)),
        timestamp_ms=int(timestamp_ms),
        source=source,
    )
    if not wallets:
        sig.label = LABEL_UNKNOWN
        return sig

    total_recv = 0.0
    total_sent = 0.0
    accumulating = 0
    distributing = 0
    neutral = 0
    for w in wallets:
        total_recv += w.received_eth
        total_sent += w.sent_eth
        if w.net_eth_flow > noise_floor_eth:
            accumulating += 1
        elif w.net_eth_flow < -noise_floor_eth:
            distributing += 1
        else:
            neutral += 1

    total_net = total_recv - total_sent
    sig.total_received_eth = total_recv
    sig.total_sent_eth = total_sent
    sig.total_net_eth_flow = total_net
    sig.accumulating_count = accumulating
    sig.distributing_count = distributing
    sig.neutral_count = neutral
    n = max(1, len(wallets))
    sig.alignment_ratio = max(accumulating, distributing) / n

    # Classification.
    abs_net = abs(total_net)
    if accumulating == 0 and distributing == 0:
        sig.label = LABEL_QUIET
    elif abs_net < flow_threshold_eth:
        sig.label = LABEL_QUIET if abs_net < noise_floor_eth else LABEL_MIXED
    else:
        if total_net > 0 and accumulating >= distributing:
            sig.label = LABEL_ACCUMULATING
        elif total_net < 0 and distributing >= accumulating:
            sig.label = LABEL_DISTRIBUTING
        else:
            sig.label = LABEL_MIXED

    sig.is_strong_signal = (
        sig.alignment_ratio >= alignment_threshold
        and abs_net >= strong_flow_threshold_eth
        and sig.label in (LABEL_ACCUMULATING, LABEL_DISTRIBUTING)
    )
    return sig
