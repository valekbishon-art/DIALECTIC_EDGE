"""I/O для smart-money wallets — Etherscan API v2.

Etherscan v2 (https://api.etherscan.io/v2/api):
    Один API-ключ работает на 50+ EVM-чейнах. Параметр `chainid` обязательный.
    Для Ethereum mainnet chainid=1.

    Эндпоинты:
      module=block&action=getblocknobytime&timestamp=...&closest=before
        → block number в момент timestamp (или раньше).
      module=account&action=txlist&address=...&startblock=N&endblock=M
        → нормальные ETH-трансферы (без internal txs и без ERC20).
        Limit free tier: 10000 rows per call, sort=desc|asc.

Стратегия:
    Простая и rate-friendly:
      1. (опционально) Получить блок ~24h назад → start_block (1 call).
         Если key disabled или fail — используем эстиматор:
         start_block = current_block - (lookback_hours * 3600 / 12)  (12 sec/block).
      2. Для каждого кошелька — txlist с этим startblock и sort=desc, offset=1000.
         → 1 call per wallet.

    Итого: 1 + N calls. С N=8 → 9 calls. На free tier (5 calls/sec) — ~2 сек.

    Кошельки задаются константой `DEFAULT_SMART_MONEY_WALLETS` (см. ниже),
    либо через env-override `SMART_MONEY_WALLETS_ADDRESSES`.

Всё DI-based: HTTP-клиент инжектится в `fetch_smart_money_wallet_flows`;
тесты подменяют его моком, нет сетевых вызовов. Production-fabрика
`make_aiohttp_http_client(session)` отдаёт обёртку над aiohttp.

Не пересекается с smart_money.py (тот — futures/coinbase-premium/funding),
отдельный изолированный путь — на случай если ETHERSCAN_API_KEY отсутствует,
старый smart_money.py продолжает работать.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from market_indicators.smart_money_wallets import (
    DEFAULT_ALIGNMENT_THRESHOLD,
    DEFAULT_FLOW_THRESHOLD_ETH,
    DEFAULT_STRONG_FLOW_THRESHOLD_ETH,
    LABEL_ACCUMULATING,
    LABEL_DISTRIBUTING,
    LABEL_MIXED,
    LABEL_QUIET,
    LABEL_UNKNOWN,
    PER_WALLET_NOISE_FLOOR_ETH,
    SmartMoneyWalletsSignal,
    WalletNetFlow,
    aggregate_wallet_flows,
    compute_wallet_flow,
)

logger = logging.getLogger(__name__)

#: Callable интерфейс HTTP-клиента (как в regime_io / options_skew_io). Тесты замокивают.
HttpClient = Callable[..., Awaitable[Any]]


# ─── Wallet registry ─────────────────────────────────────────────────────────

#: Дефолтный набор публично-атрибутированных smart-money / market-maker
#: кошельков на Ethereum mainnet. Адреса должны быть в lowercase.
#:
#: Источники атрибуции (для аудита):
#:   - Arkham Intelligence (public attributions)
#:   - Etherscan name-tags (community-vetted)
#:   - Известные публикации (CoinDesk, The Block, Decrypt) с указанием адресов
#:
#: ВАЖНО: эти кошельки — известные **хот-кошельки** market-maker'ов и фондов.
#: Их балансы и активность публичны на on-chain. Мы НЕ trying to deanonymize
#: ничего нового; используем только то, что уже годами в public domain.
#:
#: Расширять список можно через env: SMART_MONEY_WALLETS_ADDRESSES=
#:   0x...:LabelA,0x...:LabelB
#: (формат: address:label через запятую).
DEFAULT_SMART_MONEY_WALLETS: tuple[tuple[str, str], ...] = (
    # Jump Trading — один из главных MM в крипте.
    ("0xf584f8728b874a6a5c7a8d4d387c9aae9172d621", "Jump Trading"),
    # Wintermute — крупнейший крипто-MM по объёму.
    ("0x0000006daea1723962647b7e189d311d757fb793", "Wintermute"),
    # Cumberland (DRW) — один из старейших институциональных MM.
    ("0x9f76437da9c2eef9d03ed694eb9d2cba0bc0bdb0", "Cumberland"),
    # Galaxy Digital — Mike Novogratz фонд.
    ("0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503", "Galaxy Digital"),
    # GSR Markets — глобальный MM.
    ("0xc4cb5793bd58bad06bf51fb37717b86b02cbe8a4", "GSR Markets"),
    # Amber Group — азиатский институциональный MM.
    ("0xb1adceddb2941033a090dd166a462fe1c2029484", "Amber Group"),
    # FalconX — prime broker.
    ("0x0a4c79ce84202b03e95b7a692e5d728d83c44c76", "FalconX"),
    # Flow Traders — европейский MM с большой ETH-экспозицией.
    ("0xbe0eb53f46cd790cd13851d5eff43d12404d33e8", "Flow Traders"),
)


def get_wallet_registry() -> tuple[tuple[str, str], ...]:
    """Получить актуальный wallet registry (default + override из env).

    Env override `SMART_MONEY_WALLETS_ADDRESSES`:
      Если задан — ПОЛНОСТЬЮ заменяет default registry (а не дополняет).
      Формат: `0xaddr1:Label1,0xaddr2:Label2,...`. Адрес автоматически
      приводится к lowercase, label trimmed.
      Пустой/мусорный override → silent fallback на default.
    """
    raw = os.environ.get("SMART_MONEY_WALLETS_ADDRESSES", "").strip()
    if not raw:
        return DEFAULT_SMART_MONEY_WALLETS
    parsed: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        addr, _, label = chunk.partition(":")
        addr = addr.strip().lower()
        label = label.strip()
        if not addr.startswith("0x") or len(addr) != 42:
            logger.warning("smart-money wallets override: invalid address %r — skip", addr)
            continue
        if not label:
            label = addr[:10]
        parsed.append((addr, label))
    if not parsed:
        logger.warning("SMART_MONEY_WALLETS_ADDRESSES has no valid entries — fallback to default")
        return DEFAULT_SMART_MONEY_WALLETS
    return tuple(parsed)


# ─── Etherscan v2 endpoints ──────────────────────────────────────────────────

ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
ETHERSCAN_CHAIN_ID_ETH = 1
ETHERSCAN_AVG_BLOCK_TIME_SEC = 12.0  # Ethereum POS post-Merge: ~12s/block


def _etherscan_block_by_time_args(
    *, chainid: int, timestamp_s: int, api_key: str,
) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": ETHERSCAN_V2_BASE,
        "params": {
            "chainid": chainid,
            "module": "block",
            "action": "getblocknobytime",
            "timestamp": int(timestamp_s),
            "closest": "before",
            "apikey": api_key,
        },
    }


def _etherscan_txlist_args(
    *,
    chainid: int,
    address: str,
    start_block: int,
    end_block: int,
    api_key: str,
    offset: int = 1000,
    sort: str = "desc",
) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": ETHERSCAN_V2_BASE,
        "params": {
            "chainid": chainid,
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": int(start_block),
            "endblock": int(end_block),
            "page": 1,
            "offset": int(offset),
            "sort": sort,
            "apikey": api_key,
        },
    }


def _parse_etherscan_block_number(payload: Any) -> int | None:
    """Etherscan возвращает {"status":"1","message":"OK","result":"25135488"}.

    На ошибку (e.g. timestamp в будущем) status="0" + message="No closest block".
    """
    try:
        if not isinstance(payload, dict):
            return None
        status = str(payload.get("status") or "")
        if status != "1":
            logger.warning(
                "etherscan getblocknobytime non-OK: status=%s msg=%s",
                status, payload.get("message"),
            )
            return None
        raw = payload.get("result")
        if raw is None:
            return None
        v = int(str(raw))
        return v if v > 0 else None
    except (TypeError, ValueError) as e:
        logger.warning("etherscan block-by-time parse failed: %s", e)
        return None


def _parse_etherscan_txlist(payload: Any) -> tuple[list[dict], bool]:
    """Распарсить ответ Etherscan txlist → (txs, truncated_flag).

    Etherscan возвращает либо:
      {"status":"1","message":"OK","result":[{...}, ...]}
      {"status":"0","message":"No transactions found","result":[]}
      {"status":"0","message":"NOTOK","result":"Error! Invalid..."}

    truncated_flag = True если len(result) >= max_offset (могли быть ещё txs).
    """
    if not isinstance(payload, dict):
        return [], False
    result = payload.get("result")
    if isinstance(result, str):
        # Ошибка типа "NOTOK" — result является строкой с описанием.
        logger.warning(
            "etherscan txlist non-list result: status=%s msg=%s result=%s",
            payload.get("status"), payload.get("message"), result[:120],
        )
        return [], False
    if not isinstance(result, list):
        return [], False
    return result, False  # truncated_flag вычислится в caller относительно offset


# ─── Top-level fetcher ───────────────────────────────────────────────────────


async def _fetch_start_block(
    http_client: HttpClient,
    *,
    chainid: int,
    timestamp_s: int,
    api_key: str,
    timeout: float = 8.0,
) -> int | None:
    args = _etherscan_block_by_time_args(
        chainid=chainid, timestamp_s=timestamp_s, api_key=api_key,
    )
    try:
        payload = await asyncio.wait_for(http_client(**args), timeout=timeout)
    except (asyncio.TimeoutError, RuntimeError) as e:
        logger.warning("etherscan block-by-time fetch failed: %s", e)
        return None
    return _parse_etherscan_block_number(payload)


async def _fetch_wallet_txs(
    http_client: HttpClient,
    *,
    chainid: int,
    address: str,
    start_block: int,
    end_block: int,
    api_key: str,
    offset: int = 1000,
    timeout: float = 8.0,
) -> tuple[list[dict], bool]:
    args = _etherscan_txlist_args(
        chainid=chainid,
        address=address,
        start_block=start_block,
        end_block=end_block,
        api_key=api_key,
        offset=offset,
    )
    try:
        payload = await asyncio.wait_for(http_client(**args), timeout=timeout)
    except (asyncio.TimeoutError, RuntimeError) as e:
        logger.warning("etherscan txlist fetch failed for %s: %s", address, e)
        return [], False
    txs, _ = _parse_etherscan_txlist(payload)
    truncated = len(txs) >= offset
    return txs, truncated


async def fetch_smart_money_wallet_flows(
    http_client: HttpClient | None = None,
    *,
    api_key: str | None = None,
    chainid: int = ETHERSCAN_CHAIN_ID_ETH,
    lookback_hours: int | None = None,
    wallets: tuple[tuple[str, str], ...] | None = None,
    timeout: float = 8.0,
    inter_call_delay_s: float = 0.0,
) -> SmartMoneyWalletsSignal:
    """Главная entry-point: дёрнуть Etherscan v2 и вернуть aggregate signal.

    Args:
        http_client:    DI HTTP-client (см. HttpClient typedef). Если None —
                        собирается production aiohttp-обёртка внутри (требует aiohttp).
        api_key:        ETHERSCAN_API_KEY. Если None — берётся из env. Если и в env
                        нет — возвращаем UNKNOWN signal (graceful disable).
        chainid:        Ethereum=1. Для multichain в будущем — оставлено параметром.
        lookback_hours: Окно агрегации. Если None — берётся из env / default 24.
        wallets:        Опциональный override registry. Если None — через get_wallet_registry().
        timeout:        per-call timeout.
        inter_call_delay_s: пауза между API-вызовами для уважения rate-limit
                            (free tier 5 calls/sec). Default=0 (никаких пауз).
                            При большом списке wallet'ов имеет смысл 0.2с.

    Returns:
        SmartMoneyWalletsSignal — пустой/UNKNOWN если что-то fail, иначе
        aggregated по классифицированным flows. Никогда не raise — graceful.
    """
    key = (api_key or os.environ.get("ETHERSCAN_API_KEY") or "").strip()
    lookback = lookback_hours if lookback_hours is not None else get_lookback_hours()
    registry = wallets if wallets is not None else get_wallet_registry()
    source = f"etherscan-v2-chainid-{chainid}"
    now_ms = int(time.time() * 1000)

    if not key:
        logger.info("[SMART-MONEY-WALLETS] no ETHERSCAN_API_KEY — feature disabled")
        return SmartMoneyWalletsSignal(
            n_wallets_tracked=len(registry),
            lookback_hours=lookback,
            timestamp_ms=now_ms,
            source=source,
            label=LABEL_UNKNOWN,
        )

    if http_client is None:
        try:
            import aiohttp
            session = aiohttp.ClientSession()
            http_client = make_aiohttp_http_client(session)
            owns_session = True
        except ImportError:
            logger.warning("[SMART-MONEY-WALLETS] aiohttp not available")
            return SmartMoneyWalletsSignal(
                n_wallets_tracked=len(registry),
                lookback_hours=lookback,
                timestamp_ms=now_ms,
                source=source,
                label=LABEL_UNKNOWN,
            )
    else:
        owns_session = False
        session = None

    try:
        # 1. Определить start_block (block ~lookback hours назад).
        since_ts = int(time.time()) - lookback * 3600
        start_block = await _fetch_start_block(
            http_client, chainid=chainid, timestamp_s=since_ts, api_key=key, timeout=timeout,
        )
        if start_block is None:
            # Fallback: эстимат по средней block-time.
            # Получить latest через extra-call дорого; полагаемся на endblock=99999999.
            logger.info(
                "[SMART-MONEY-WALLETS] block-by-time failed, falling back to startblock=0+txlist filter",
            )
            start_block = 0  # → tx list возвращает всё, фильтр идёт по timeStamp в compute_wallet_flow
        end_block = 99999999  # Etherscan-style «latest» max.

        # 2. Параллельно (с возможной паузой) тянем txlist для каждого wallet.
        per_wallet_flows: list[WalletNetFlow] = []
        for addr, label in registry:
            if inter_call_delay_s > 0:
                await asyncio.sleep(inter_call_delay_s)
            txs, truncated = await _fetch_wallet_txs(
                http_client,
                chainid=chainid,
                address=addr,
                start_block=start_block,
                end_block=end_block,
                api_key=key,
                timeout=timeout,
            )
            flow = compute_wallet_flow(
                address=addr,
                label=label,
                txs=txs,
                since_timestamp_s=since_ts,
                truncated=truncated,
            )
            per_wallet_flows.append(flow)
    finally:
        if owns_session and session is not None:
            await session.close()

    return aggregate_wallet_flows(
        per_wallet_flows,
        noise_floor_eth=get_noise_floor_eth(),
        flow_threshold_eth=get_flow_threshold_eth(),
        strong_flow_threshold_eth=get_strong_flow_threshold_eth(),
        alignment_threshold=get_alignment_threshold(),
        lookback_hours=lookback,
        timestamp_ms=now_ms,
        source=source,
    )


# ─── Score contribution & formatting ─────────────────────────────────────────


def smart_money_wallets_score_contribution(
    signal: SmartMoneyWalletsSignal,
) -> tuple[int, list[str], list[str]]:
    """Свести SmartMoneyWalletsSignal к (score_delta, bullish_reasons, bearish_reasons).

    Веса консервативные (±2 max) — это leading on-chain индикатор, шум возможен.
    Если is_strong_signal — даём ±2. Иначе ±1 для accumulating/distributing.
    QUIET / MIXED / UNKNOWN → 0.
    """
    if signal is None or signal.label in (LABEL_QUIET, LABEL_UNKNOWN, LABEL_MIXED):
        return (0, [], [])
    if signal.label == LABEL_ACCUMULATING:
        delta = 2 if signal.is_strong_signal else 1
        n = signal.accumulating_count
        reason = (
            f"smart-money: {n}/{signal.n_wallets_tracked} кошельков копят ETH "
            f"(+{signal.total_net_eth_flow:,.0f} ETH за {signal.lookback_hours}ч)"
        )
        return (delta, [reason], [])
    if signal.label == LABEL_DISTRIBUTING:
        delta = -2 if signal.is_strong_signal else -1
        n = signal.distributing_count
        reason = (
            f"smart-money: {n}/{signal.n_wallets_tracked} кошельков раздают ETH "
            f"({signal.total_net_eth_flow:,.0f} ETH за {signal.lookback_hours}ч)"
        )
        return (delta, [], [reason])
    return (0, [], [])


def format_smart_money_wallets_for_agents(signal: SmartMoneyWalletsSignal) -> str:
    """Markdown-блок для дебатёров. Безопасный: всегда возвращает строку."""
    if signal is None or signal.label == LABEL_UNKNOWN:
        return "🐋 **Smart-Money Wallets (on-chain):** нет данных (feature off / no key)"
    lines: list[str] = ["🐋 **Smart-Money Wallets (on-chain ETH flows):**"]
    lines.append(
        f"   • Window: последние {signal.lookback_hours}ч. "
        f"Wallets tracked: {signal.n_wallets_tracked}. "
        f"Source: {signal.source}"
    )
    label_emoji = {
        LABEL_ACCUMULATING: "🟢",
        LABEL_DISTRIBUTING: "🔴",
        LABEL_MIXED: "🟡",
        LABEL_QUIET: "⚪",
    }.get(signal.label, "❔")
    strong = " (strong)" if signal.is_strong_signal else ""
    lines.append(
        f"   • Aggregate: {label_emoji} **{signal.label.upper()}**{strong}  "
        f"net = {signal.total_net_eth_flow:+,.0f} ETH  "
        f"(received {signal.total_received_eth:,.0f}, sent {signal.total_sent_eth:,.0f})"
    )
    lines.append(
        f"   • Alignment: {signal.accumulating_count} buying / "
        f"{signal.distributing_count} selling / {signal.neutral_count} neutral "
        f"(ratio {signal.alignment_ratio:.2f})"
    )
    # Топ-3 wallets по абсолютному net flow.
    top = sorted(signal.wallets, key=lambda w: abs(w.net_eth_flow), reverse=True)[:3]
    if top:
        lines.append("   • Top moves:")
        for w in top:
            sign = "+" if w.net_eth_flow >= 0 else ""
            tag = " ⚠truncated" if w.truncated else ""
            lines.append(
                f"     – {w.label}: {sign}{w.net_eth_flow:,.0f} ETH "
                f"(in {w.received_eth:,.0f} / out {w.sent_eth:,.0f}, {w.tx_count} tx{tag})"
            )
    return "\n".join(lines)


# ─── Env parsers ─────────────────────────────────────────────────────────────


def feature_enabled() -> bool:
    """Проверить FEATURE_SMART_MONEY_WALLETS env-флаг. Default OFF."""
    return os.environ.get("FEATURE_SMART_MONEY_WALLETS", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _env_float(name: str, default: float, *, min_val: float, max_val: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("[SMART-MONEY-WALLETS] %s=%r not a float — using default %s", name, raw, default)
        return default
    if v < min_val or v > max_val:
        logger.warning(
            "[SMART-MONEY-WALLETS] %s=%s outside [%s, %s] — using default %s",
            name, v, min_val, max_val, default,
        )
        return default
    return v


def _env_int(name: str, default: int, *, min_val: int, max_val: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("[SMART-MONEY-WALLETS] %s=%r not an int — using default %s", name, raw, default)
        return default
    if v < min_val or v > max_val:
        logger.warning(
            "[SMART-MONEY-WALLETS] %s=%s outside [%s, %s] — using default %s",
            name, v, min_val, max_val, default,
        )
        return default
    return v


def get_lookback_hours() -> int:
    return _env_int("SMART_MONEY_WALLETS_LOOKBACK_HOURS", 24, min_val=1, max_val=168)


def get_noise_floor_eth() -> float:
    return _env_float(
        "SMART_MONEY_WALLETS_NOISE_FLOOR_ETH",
        PER_WALLET_NOISE_FLOOR_ETH, min_val=0.0, max_val=1e6,
    )


def get_flow_threshold_eth() -> float:
    return _env_float(
        "SMART_MONEY_WALLETS_FLOW_THRESHOLD_ETH",
        DEFAULT_FLOW_THRESHOLD_ETH, min_val=1.0, max_val=1e7,
    )


def get_strong_flow_threshold_eth() -> float:
    return _env_float(
        "SMART_MONEY_WALLETS_STRONG_FLOW_THRESHOLD_ETH",
        DEFAULT_STRONG_FLOW_THRESHOLD_ETH, min_val=1.0, max_val=1e7,
    )


def get_alignment_threshold() -> float:
    return _env_float(
        "SMART_MONEY_WALLETS_ALIGNMENT_THRESHOLD",
        DEFAULT_ALIGNMENT_THRESHOLD, min_val=0.0, max_val=1.0,
    )


def get_inter_call_delay_s() -> float:
    """Пауза между API-вызовами. Free tier Etherscan = 5/sec → 0.2s safe."""
    return _env_float(
        "SMART_MONEY_WALLETS_INTER_CALL_DELAY_S",
        0.0, min_val=0.0, max_val=5.0,
    )


# ─── Production aiohttp factory ──────────────────────────────────────────────


def make_aiohttp_http_client(session: Any) -> HttpClient:
    """Обёртка aiohttp.ClientSession под наш HttpClient interface.

    Сигнатура: http_client(method='GET'|'POST', url=..., params=..., timeout=...)
    → возвращает распарсенный JSON (dict | list).
    """

    async def _call(*, method: str, url: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        async with session.request(method, url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"etherscan HTTP {resp.status}")
            return await resp.json(content_type=None)

    return _call


# ─── Convenience: dataclass → dict для лог-export ────────────────────────────


def signal_to_dict(signal: SmartMoneyWalletsSignal) -> dict[str, Any]:
    """Дамп SmartMoneyWalletsSignal в dict для JSON-логов / digest export."""
    d = asdict(signal)
    return d
