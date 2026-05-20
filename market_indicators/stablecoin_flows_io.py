"""I/O для stablecoin flows.

Источники totalSupply:
  Ethereum:
    Etherscan v1/v2 API — module=stats&action=tokensupply&contractaddress=...
    Требует ETHERSCAN_API_KEY (free tier 5 calls/sec). Если ключа нет — loop
    логирует warning и выходит (graceful disable).
  Tron:
    Tronscan apilist — /api/token_trc20?contract=...
    Публичный, без ключа. Берём `total_supply_str` и `decimals` напрямую.

Всё DI-based: тесты подменяют HTTP-клиент моком, без сетевых вызовов.
Не пересекается с data_sources.py (тот ходит за gas price), отдельный путь.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

from market_indicators.stablecoin_flows import (
    StablecoinSupplySnapshot,
    TOKEN_DECIMALS,
)

logger = logging.getLogger(__name__)

#: Callable интерфейс HTTP-клиента (как в options_skew_io). Тесты замокивают.
HttpClient = Callable[..., Awaitable[Any]]


# ─── Token registry ─────────────────────────────────────────────────────────

#: Известные контракты по chain. Расширяй по необходимости — для каждого
#: нового токена нужно добавить (token, chain, contract_addr, decimals).
ERC20_CONTRACTS: dict[str, str] = {
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "FRAX": "0x853d955aCEf822Db058eb8505911ED77F175b99e",
}

TRC20_CONTRACTS: dict[str, str] = {
    # Tether USD on Tron (TRC20).
    "USDT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    # USDC on Tron — официальный TRC20 контракт.
    "USDC": "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8",
}


# ─── Etherscan ──────────────────────────────────────────────────────────────


def _etherscan_token_supply_args(*, contract: str, api_key: str) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": "https://api.etherscan.io/api",
        "params": {
            "module": "stats",
            "action": "tokensupply",
            "contractaddress": contract,
            "apikey": api_key,
        },
    }


def _parse_etherscan_token_supply(payload: Any) -> int | None:
    """Etherscan возвращает {"status":"1","message":"OK","result":"60500000000000000"}."""
    try:
        if not isinstance(payload, dict):
            return None
        status = str(payload.get("status") or "")
        if status != "1":
            logger.warning("etherscan token supply non-OK status: %s", payload.get("message"))
            return None
        raw = payload.get("result")
        if raw is None:
            return None
        v = int(str(raw))
        return v if v >= 0 else None
    except (TypeError, ValueError) as e:
        logger.warning("etherscan token supply parse failed: %s", e)
        return None


# ─── Tronscan ───────────────────────────────────────────────────────────────


def _tronscan_token_supply_args(contract: str) -> dict[str, Any]:
    return {
        "method": "GET",
        "url": "https://apilist.tronscanapi.com/api/token_trc20",
        "params": {"contract": contract, "showAll": "1"},
    }


def _parse_tronscan_token_supply(payload: Any) -> tuple[int, int] | None:
    """Возвращает (raw_supply_units, decimals) либо None.

    Tronscan отдаёт `data: [{"total_supply_str": "...", "decimals": "6"}]`.
    Иногда `total_supply` уже отнормализованный — используем `*_str` (raw).
    """
    try:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") or payload.get("trc20_tokens") or []
        if not data:
            return None
        row = data[0]
        if not isinstance(row, dict):
            return None
        raw_str = row.get("total_supply_str") or row.get("totalSupply") or row.get("total_supply")
        if raw_str is None:
            return None
        decimals_raw = row.get("decimals")
        decimals = int(decimals_raw) if decimals_raw is not None else 6
        raw_units = int(str(raw_str))
        if raw_units < 0:
            return None
        return (raw_units, decimals)
    except (TypeError, ValueError, IndexError) as e:
        logger.warning("tronscan token supply parse failed: %s", e)
        return None


# ─── End-to-end fetch ───────────────────────────────────────────────────────


async def _call_http(http_client: HttpClient, args: dict[str, Any]) -> Any:
    return await http_client(**args)


async def fetch_stablecoin_snapshots(
    *,
    token: str,
    http_client: HttpClient,
    etherscan_api_key: str | None,
    chains: tuple[str, ...] = ("ethereum", "tron"),
    now: datetime | None = None,
) -> list[StablecoinSupplySnapshot]:
    """Pull totalSupply на каждом chain для одного токена.

    Per-chain error isolation: одна биржа не блокирует loop.
    Возвращает список snapshot'ов (по одному на chain).
    """
    moment = now or datetime.utcnow()
    timestamp_ms = int(moment.timestamp() * 1000)
    token_upper = token.upper()
    decimals_default = TOKEN_DECIMALS.get(token_upper, 6)
    out: list[StablecoinSupplySnapshot] = []

    if "ethereum" in chains:
        contract = ERC20_CONTRACTS.get(token_upper)
        if contract and etherscan_api_key:
            try:
                payload = await _call_http(
                    http_client,
                    _etherscan_token_supply_args(
                        contract=contract, api_key=etherscan_api_key,
                    ),
                )
                raw_units = _parse_etherscan_token_supply(payload)
                if raw_units is not None and raw_units > 0:
                    out.append(StablecoinSupplySnapshot(
                        token=token_upper, chain="ethereum",
                        raw_supply_units=raw_units, decimals=decimals_default,
                        timestamp_ms=timestamp_ms,
                    ))
            except Exception as e:  # noqa: BLE001 — per-chain isolation
                logger.warning(
                    "stablecoin: etherscan supply (%s) failed: %s", token_upper, e,
                )
        elif contract and not etherscan_api_key:
            logger.debug(
                "stablecoin: ETHERSCAN_API_KEY не задан, ethereum chain пропущен",
            )

    if "tron" in chains:
        contract_trx = TRC20_CONTRACTS.get(token_upper)
        if contract_trx:
            try:
                payload = await _call_http(
                    http_client, _tronscan_token_supply_args(contract_trx),
                )
                parsed = _parse_tronscan_token_supply(payload)
                if parsed is not None:
                    raw_units, decimals = parsed
                    if raw_units > 0:
                        out.append(StablecoinSupplySnapshot(
                            token=token_upper, chain="tron",
                            raw_supply_units=raw_units, decimals=decimals,
                            timestamp_ms=timestamp_ms,
                        ))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "stablecoin: tronscan supply (%s) failed: %s", token_upper, e,
                )

    return out


# ─── Persistence ────────────────────────────────────────────────────────────


async def persist_supply_snapshots(
    snapshots: list[StablecoinSupplySnapshot],
) -> None:
    """Сохранить supply snapshots (по одному на chain)."""
    from database import save_stablecoin_supply_snapshot  # noqa: PLC0415
    for s in snapshots:
        await save_stablecoin_supply_snapshot(
            token=s.token, chain=s.chain,
            raw_supply_units_str=str(s.raw_supply_units),
            decimals=s.decimals,
            timestamp_ms=s.timestamp_ms,
        )


async def persist_flow_signal(signal) -> None:  # type: ignore[no-untyped-def]
    """Сохранить StablecoinFlowSignal в БД."""
    from database import save_stablecoin_flow_snapshot  # noqa: PLC0415
    await save_stablecoin_flow_snapshot(
        token=signal.token,
        timestamp_ms=signal.timestamp_ms,
        supply_total_usd=signal.supply_total_usd,
        delta_24h_usd=signal.delta_24h_usd,
        delta_pct_24h=signal.delta_pct_24h,
        flow_class=signal.flow_class,
        chains_csv=",".join(signal.chains_used),
    )


async def get_previous_supply_usd(
    *, token: str, hours_ago: float = 24.0,
) -> float | None:
    """Сумма supply'а по всем chains ≈ hours_ago назад. Берём ближайший
    snapshot per-chain в окне [hours_ago - 2ч, hours_ago + 2ч]; если нет —
    fallback на самый старый snapshot не моложе hours_ago.
    """
    from market_indicators.stablecoin_flows import normalize_supply  # noqa: PLC0415
    from database import get_supply_snapshot_at_or_before  # noqa: PLC0415
    chains = ("ethereum", "tron")
    total = 0.0
    found_any = False
    for chain in chains:
        row = await get_supply_snapshot_at_or_before(
            token=token, chain=chain, hours_ago=hours_ago,
        )
        if not row:
            continue
        try:
            raw_units = int(str(row["raw_supply_units_str"]))
            decimals = int(row["decimals"])
        except (KeyError, TypeError, ValueError):
            continue
        v = normalize_supply(raw_units=raw_units, decimals=decimals)
        if v > 0:
            total += v
            found_any = True
    return total if found_any else None


async def get_previous_flow_signal(*, token: str):  # type: ignore[no-untyped-def]
    """Последний сохранённый flow signal по токену (для event detection)."""
    from database import get_recent_stablecoin_flow_snapshots  # noqa: PLC0415
    from market_indicators.stablecoin_flows import StablecoinFlowSignal  # noqa: PLC0415
    rows = await get_recent_stablecoin_flow_snapshots(token=token, limit=5)
    for row in rows:
        try:
            return StablecoinFlowSignal(
                token=str(row["token"]),
                timestamp_ms=int(row["timestamp_ms"]),
                supply_total_usd=float(row.get("supply_total_usd") or 0.0),
                delta_24h_usd=(
                    float(row["delta_24h_usd"])
                    if row.get("delta_24h_usd") is not None else None
                ),
                delta_pct_24h=(
                    float(row["delta_pct_24h"])
                    if row.get("delta_pct_24h") is not None else None
                ),
                flow_class=str(row.get("flow_class") or "unknown"),
                chains_used=tuple(
                    str(row.get("chains_csv") or "").split(",")
                ) if row.get("chains_csv") else (),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return None


# ─── Env-flags ───────────────────────────────────────────────────────────────


def feature_enabled() -> bool:
    return os.getenv("FEATURE_STABLECOIN_FLOWS", "0").strip() in {"1", "true", "True", "yes"}


def get_tokens() -> tuple[str, ...]:
    raw = os.getenv("STABLECOIN_FLOWS_TOKENS", "USDT,USDC")
    parts = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return tuple(dict.fromkeys(parts)) if parts else ("USDT", "USDC")


def get_interval_seconds() -> int:
    try:
        return max(300, int(os.getenv("STABLECOIN_FLOWS_INTERVAL_SEC", "3600")))
    except (TypeError, ValueError):
        return 3600


def get_etherscan_api_key() -> str | None:
    key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    return key or None


# ─── Aiohttp factory (для scheduler — НЕ для тестов) ────────────────────────


async def make_aiohttp_http_client(session: Any) -> HttpClient:
    """Создать HttpClient над уже открытым aiohttp.ClientSession."""
    async def _call(*, method: str, url: str, params=None, json=None, timeout=8.0):
        import aiohttp  # noqa: PLC0415
        to = aiohttp.ClientTimeout(total=float(timeout))
        if method.upper() == "GET":
            async with session.get(url, params=params, timeout=to) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return await resp.json()
        elif method.upper() == "POST":
            async with session.post(url, params=params, json=json, timeout=to) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return await resp.json()
        else:
            raise ValueError(f"unsupported method: {method}")

    return _call
