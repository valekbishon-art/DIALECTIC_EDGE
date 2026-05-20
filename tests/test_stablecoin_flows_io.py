"""I/O-tests для market_indicators.stablecoin_flows_io.

DI-моки HTTP-клиента: никаких сетевых вызовов. Все Etherscan / Tronscan
ответы — заранее подготовленные dict'ы.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from market_indicators.stablecoin_flows_io import (
    ERC20_CONTRACTS,
    TRC20_CONTRACTS,
    _etherscan_token_supply_args,
    _parse_etherscan_token_supply,
    _parse_tronscan_token_supply,
    _tronscan_token_supply_args,
    feature_enabled,
    fetch_stablecoin_snapshots,
    get_etherscan_api_key,
    get_interval_seconds,
    get_tokens,
)


def _run(coro):
    return asyncio.run(coro)


# ─── _etherscan_token_supply_args ────────────────────────────────────────────


class EtherscanArgsTestCase(unittest.TestCase):
    def test_contains_required_params(self):
        args = _etherscan_token_supply_args(contract="0xabc", api_key="KEY")
        self.assertEqual(args["method"], "GET")
        self.assertIn("api.etherscan.io", args["url"])
        self.assertEqual(args["params"]["module"], "stats")
        self.assertEqual(args["params"]["action"], "tokensupply")
        self.assertEqual(args["params"]["contractaddress"], "0xabc")
        self.assertEqual(args["params"]["apikey"], "KEY")


# ─── _parse_etherscan_token_supply ───────────────────────────────────────────


class EtherscanParseTestCase(unittest.TestCase):
    def test_happy_path(self):
        payload = {"status": "1", "message": "OK", "result": "60500000000000000"}
        out = _parse_etherscan_token_supply(payload)
        self.assertEqual(out, 60_500_000_000_000_000)

    def test_status_zero_returns_none(self):
        payload = {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
        self.assertIsNone(_parse_etherscan_token_supply(payload))

    def test_missing_result(self):
        self.assertIsNone(_parse_etherscan_token_supply({"status": "1", "message": "OK"}))

    def test_non_dict_returns_none(self):
        self.assertIsNone(_parse_etherscan_token_supply([]))  # type: ignore[arg-type]
        self.assertIsNone(_parse_etherscan_token_supply(None))
        self.assertIsNone(_parse_etherscan_token_supply("text"))  # type: ignore[arg-type]

    def test_non_integer_result(self):
        # Etherscan для tokensupply иногда отдаёт hex для других endpoints.
        # Здесь должен вернуть None, не упасть.
        self.assertIsNone(_parse_etherscan_token_supply(
            {"status": "1", "result": "not_a_number"},
        ))

    def test_negative_supply_rejected(self):
        # Etherscan не отдаёт отрицательные supplies, но защита от мусора.
        self.assertIsNone(_parse_etherscan_token_supply(
            {"status": "1", "result": "-100"},
        ))


# ─── _tronscan_token_supply_args ─────────────────────────────────────────────


class TronscanArgsTestCase(unittest.TestCase):
    def test_contains_contract_param(self):
        args = _tronscan_token_supply_args("TR7N...")
        self.assertEqual(args["method"], "GET")
        self.assertIn("tronscanapi.com", args["url"])
        self.assertEqual(args["params"]["contract"], "TR7N...")


# ─── _parse_tronscan_token_supply ────────────────────────────────────────────


class TronscanParseTestCase(unittest.TestCase):
    def test_happy_path(self):
        payload = {
            "data": [{
                "name": "Tether USD",
                "symbol": "USDT",
                "total_supply_str": "80000000000000000",
                "decimals": "6",
            }],
        }
        out = _parse_tronscan_token_supply(payload)
        self.assertEqual(out, (80_000_000_000_000_000, 6))

    def test_handles_decimals_int(self):
        payload = {
            "data": [{
                "total_supply_str": "1234",
                "decimals": 6,
            }],
        }
        out = _parse_tronscan_token_supply(payload)
        self.assertEqual(out, (1234, 6))

    def test_fallback_to_total_supply_field(self):
        payload = {
            "data": [{"total_supply": "5000", "decimals": "6"}],
        }
        out = _parse_tronscan_token_supply(payload)
        self.assertEqual(out, (5000, 6))

    def test_empty_data_returns_none(self):
        self.assertIsNone(_parse_tronscan_token_supply({"data": []}))

    def test_missing_data_returns_none(self):
        self.assertIsNone(_parse_tronscan_token_supply({}))

    def test_non_dict_returns_none(self):
        self.assertIsNone(_parse_tronscan_token_supply(None))
        self.assertIsNone(_parse_tronscan_token_supply([]))  # type: ignore[arg-type]

    def test_missing_supply_returns_none(self):
        self.assertIsNone(_parse_tronscan_token_supply({"data": [{"decimals": "6"}]}))

    def test_negative_supply_rejected(self):
        self.assertIsNone(_parse_tronscan_token_supply(
            {"data": [{"total_supply_str": "-10", "decimals": "6"}]},
        ))

    def test_decimals_default_six_if_missing(self):
        payload = {"data": [{"total_supply_str": "1000"}]}
        out = _parse_tronscan_token_supply(payload)
        self.assertEqual(out, (1000, 6))

    def test_alternate_field_trc20_tokens(self):
        payload = {
            "trc20_tokens": [{
                "total_supply_str": "999",
                "decimals": "6",
            }],
        }
        out = _parse_tronscan_token_supply(payload)
        self.assertEqual(out, (999, 6))


# ─── fetch_stablecoin_snapshots (end-to-end с DI-моком) ─────────────────────


class FetchSnapshotsTestCase(unittest.TestCase):
    def _make_http_mock(self, *, etherscan_response=None, tronscan_response=None,
                        etherscan_raises=False, tronscan_raises=False):
        calls = []

        async def http(*, method, url, params=None, json=None, timeout=8.0):
            calls.append({"method": method, "url": url, "params": params})
            if "etherscan.io" in url:
                if etherscan_raises:
                    raise RuntimeError("etherscan boom")
                return etherscan_response
            if "tronscanapi.com" in url:
                if tronscan_raises:
                    raise RuntimeError("tronscan boom")
                return tronscan_response
            raise AssertionError(f"unexpected URL: {url}")

        return http, calls

    def test_both_chains_happy(self):
        http, calls = self._make_http_mock(
            etherscan_response={"status": "1", "result": str(60_000_000_000 * 10**6)},
            tronscan_response={"data": [{
                "total_supply_str": str(80_000_000_000 * 10**6),
                "decimals": "6",
            }]},
        )
        snaps = _run(fetch_stablecoin_snapshots(
            token="USDT", http_client=http, etherscan_api_key="KEY",
            now=datetime(2025, 1, 1, 12, 0, 0),
        ))
        self.assertEqual(len(snaps), 2)
        chains = {s.chain for s in snaps}
        self.assertEqual(chains, {"ethereum", "tron"})
        for s in snaps:
            self.assertEqual(s.token, "USDT")
            self.assertEqual(s.decimals, 6)
            self.assertGreater(s.timestamp_ms, 0)
        # Оба URL должны были вызваться.
        self.assertEqual(len(calls), 2)

    def test_no_etherscan_key_skips_ethereum(self):
        http, calls = self._make_http_mock(
            tronscan_response={"data": [{
                "total_supply_str": str(80_000_000_000 * 10**6),
                "decimals": "6",
            }]},
        )
        snaps = _run(fetch_stablecoin_snapshots(
            token="USDT", http_client=http, etherscan_api_key=None,
        ))
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].chain, "tron")
        # Etherscan не вызывался.
        self.assertEqual(len(calls), 1)
        self.assertIn("tronscanapi.com", calls[0]["url"])

    def test_etherscan_raises_does_not_break_tron(self):
        http, calls = self._make_http_mock(
            etherscan_raises=True,
            tronscan_response={"data": [{
                "total_supply_str": str(80_000_000_000 * 10**6),
                "decimals": "6",
            }]},
        )
        snaps = _run(fetch_stablecoin_snapshots(
            token="USDT", http_client=http, etherscan_api_key="KEY",
        ))
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].chain, "tron")

    def test_tronscan_raises_does_not_break_etherscan(self):
        http, calls = self._make_http_mock(
            etherscan_response={"status": "1", "result": str(60_000_000_000 * 10**6)},
            tronscan_raises=True,
        )
        snaps = _run(fetch_stablecoin_snapshots(
            token="USDT", http_client=http, etherscan_api_key="KEY",
        ))
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].chain, "ethereum")

    def test_unknown_token_returns_empty(self):
        http, _calls = self._make_http_mock()
        snaps = _run(fetch_stablecoin_snapshots(
            token="ZZZ", http_client=http, etherscan_api_key="KEY",
        ))
        self.assertEqual(snaps, [])

    def test_chains_filter_only_ethereum(self):
        http, calls = self._make_http_mock(
            etherscan_response={"status": "1", "result": str(60_000_000_000 * 10**6)},
        )
        snaps = _run(fetch_stablecoin_snapshots(
            token="USDT", http_client=http, etherscan_api_key="KEY",
            chains=("ethereum",),
        ))
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].chain, "ethereum")
        # Tron не должен вызываться.
        self.assertEqual(len(calls), 1)

    def test_zero_supply_skipped(self):
        http, _calls = self._make_http_mock(
            etherscan_response={"status": "1", "result": "0"},
        )
        snaps = _run(fetch_stablecoin_snapshots(
            token="USDT", http_client=http, etherscan_api_key="KEY",
            chains=("ethereum",),
        ))
        self.assertEqual(snaps, [])

    def test_now_param_drives_timestamp(self):
        http, _calls = self._make_http_mock(
            etherscan_response={"status": "1", "result": str(60_000_000_000 * 10**6)},
        )
        moment = datetime(2025, 6, 1, 0, 0, 0)
        expected_ms = int(moment.timestamp() * 1000)
        snaps = _run(fetch_stablecoin_snapshots(
            token="USDT", http_client=http, etherscan_api_key="KEY",
            chains=("ethereum",), now=moment,
        ))
        self.assertEqual(snaps[0].timestamp_ms, expected_ms)


# ─── env-flag helpers ────────────────────────────────────────────────────────


class EnvFlagsTestCase(unittest.TestCase):
    def setUp(self):
        # Сохраняем оригинальные значения
        self._saved = {
            k: os.environ.get(k)
            for k in [
                "FEATURE_STABLECOIN_FLOWS",
                "STABLECOIN_FLOWS_TOKENS",
                "STABLECOIN_FLOWS_INTERVAL_SEC",
                "ETHERSCAN_API_KEY",
            ]
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_feature_enabled_default_off(self):
        os.environ.pop("FEATURE_STABLECOIN_FLOWS", None)
        self.assertFalse(feature_enabled())

    def test_feature_enabled_truthy(self):
        for val in ("1", "true", "yes"):
            os.environ["FEATURE_STABLECOIN_FLOWS"] = val
            self.assertTrue(feature_enabled())

    def test_feature_enabled_falsy(self):
        for val in ("0", "false", "no", ""):
            os.environ["FEATURE_STABLECOIN_FLOWS"] = val
            self.assertFalse(feature_enabled())

    def test_get_tokens_default(self):
        os.environ.pop("STABLECOIN_FLOWS_TOKENS", None)
        self.assertEqual(get_tokens(), ("USDT", "USDC"))

    def test_get_tokens_custom(self):
        os.environ["STABLECOIN_FLOWS_TOKENS"] = "DAI,FRAX,usdt"
        # Должно быть upper-cased + dedup сохранён порядок
        self.assertEqual(get_tokens(), ("DAI", "FRAX", "USDT"))

    def test_get_tokens_empty_string_falls_back_to_default(self):
        os.environ["STABLECOIN_FLOWS_TOKENS"] = ""
        self.assertEqual(get_tokens(), ("USDT", "USDC"))

    def test_interval_seconds_default(self):
        os.environ.pop("STABLECOIN_FLOWS_INTERVAL_SEC", None)
        self.assertEqual(get_interval_seconds(), 3600)

    def test_interval_seconds_clamped_min(self):
        os.environ["STABLECOIN_FLOWS_INTERVAL_SEC"] = "10"
        self.assertEqual(get_interval_seconds(), 300)

    def test_interval_seconds_custom(self):
        os.environ["STABLECOIN_FLOWS_INTERVAL_SEC"] = "1800"
        self.assertEqual(get_interval_seconds(), 1800)

    def test_interval_seconds_garbage_falls_back(self):
        os.environ["STABLECOIN_FLOWS_INTERVAL_SEC"] = "garbage"
        self.assertEqual(get_interval_seconds(), 3600)

    def test_etherscan_api_key_empty_returns_none(self):
        os.environ.pop("ETHERSCAN_API_KEY", None)
        self.assertIsNone(get_etherscan_api_key())

    def test_etherscan_api_key_whitespace_stripped(self):
        os.environ["ETHERSCAN_API_KEY"] = "  "
        self.assertIsNone(get_etherscan_api_key())

    def test_etherscan_api_key_set(self):
        os.environ["ETHERSCAN_API_KEY"] = "MYKEY"
        self.assertEqual(get_etherscan_api_key(), "MYKEY")


# ─── Registry consistency ───────────────────────────────────────────────────


class ContractsRegistryTestCase(unittest.TestCase):
    def test_usdt_eth_address_canonical(self):
        # Тестируем известный USDT erc20 контракт (case-insensitive).
        self.assertEqual(
            ERC20_CONTRACTS["USDT"].lower(),
            "0xdac17f958d2ee523a2206206994597c13d831ec7",
        )

    def test_usdc_eth_address_canonical(self):
        self.assertEqual(
            ERC20_CONTRACTS["USDC"].lower(),
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        )

    def test_usdt_tron_address_starts_with_t(self):
        # TRC20 адреса начинаются с T (base58)
        self.assertTrue(TRC20_CONTRACTS["USDT"].startswith("T"))


if __name__ == "__main__":
    unittest.main()
