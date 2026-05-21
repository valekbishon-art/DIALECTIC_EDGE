"""Unit tests для market_indicators/smart_money_wallets_io.py (I/O + mocked HTTP)."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from market_indicators.smart_money_wallets import (
    ETH_DECIMALS,
    LABEL_ACCUMULATING,
    LABEL_DISTRIBUTING,
    LABEL_MIXED,
    LABEL_QUIET,
    LABEL_UNKNOWN,
    SmartMoneyWalletsSignal,
    WalletNetFlow,
)
from market_indicators.smart_money_wallets_io import (
    DEFAULT_SMART_MONEY_WALLETS,
    ETHERSCAN_CHAIN_ID_ETH,
    ETHERSCAN_V2_BASE,
    _etherscan_block_by_time_args,
    _etherscan_txlist_args,
    _parse_etherscan_block_number,
    _parse_etherscan_txlist,
    feature_enabled,
    fetch_smart_money_wallet_flows,
    format_smart_money_wallets_for_agents,
    get_alignment_threshold,
    get_flow_threshold_eth,
    get_inter_call_delay_s,
    get_lookback_hours,
    get_noise_floor_eth,
    get_strong_flow_threshold_eth,
    get_wallet_registry,
    signal_to_dict,
    smart_money_wallets_score_contribution,
)


def _wei(eth: float) -> str:
    return str(int(round(eth * (10**ETH_DECIMALS))))


def _async_run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ─── Etherscan args constructors ─────────────────────────────────────────────


class EtherscanArgsTestCase(unittest.TestCase):
    def test_block_by_time_args_shape(self):
        args = _etherscan_block_by_time_args(chainid=1, timestamp_s=1700000000, api_key="KEY")
        self.assertEqual(args["method"], "GET")
        self.assertEqual(args["url"], ETHERSCAN_V2_BASE)
        p = args["params"]
        self.assertEqual(p["chainid"], 1)
        self.assertEqual(p["module"], "block")
        self.assertEqual(p["action"], "getblocknobytime")
        self.assertEqual(p["timestamp"], 1700000000)
        self.assertEqual(p["closest"], "before")
        self.assertEqual(p["apikey"], "KEY")

    def test_txlist_args_shape(self):
        args = _etherscan_txlist_args(
            chainid=1,
            address="0xabc",
            start_block=100,
            end_block=200,
            api_key="KEY",
            offset=500,
            sort="asc",
        )
        p = args["params"]
        self.assertEqual(p["module"], "account")
        self.assertEqual(p["action"], "txlist")
        self.assertEqual(p["address"], "0xabc")
        self.assertEqual(p["startblock"], 100)
        self.assertEqual(p["endblock"], 200)
        self.assertEqual(p["offset"], 500)
        self.assertEqual(p["sort"], "asc")


# ─── Etherscan response parsing ──────────────────────────────────────────────


class ParseBlockNumberTestCase(unittest.TestCase):
    def test_valid_response(self):
        payload = {"status": "1", "message": "OK", "result": "25135488"}
        self.assertEqual(_parse_etherscan_block_number(payload), 25135488)

    def test_non_ok_status(self):
        payload = {"status": "0", "message": "No closest block", "result": ""}
        self.assertIsNone(_parse_etherscan_block_number(payload))

    def test_missing_result(self):
        payload = {"status": "1", "message": "OK"}
        self.assertIsNone(_parse_etherscan_block_number(payload))

    def test_invalid_result(self):
        payload = {"status": "1", "message": "OK", "result": "not_a_number"}
        self.assertIsNone(_parse_etherscan_block_number(payload))

    def test_negative_result(self):
        payload = {"status": "1", "message": "OK", "result": "-1"}
        self.assertIsNone(_parse_etherscan_block_number(payload))

    def test_non_dict_payload(self):
        self.assertIsNone(_parse_etherscan_block_number("not a dict"))
        self.assertIsNone(_parse_etherscan_block_number(None))


class ParseTxlistTestCase(unittest.TestCase):
    def test_valid_list(self):
        payload = {
            "status": "1",
            "message": "OK",
            "result": [{"from": "0xa", "to": "0xb", "value": "100"}],
        }
        txs, truncated = _parse_etherscan_txlist(payload)
        self.assertEqual(len(txs), 1)
        self.assertFalse(truncated)

    def test_empty_list(self):
        payload = {"status": "0", "message": "No transactions found", "result": []}
        txs, _ = _parse_etherscan_txlist(payload)
        self.assertEqual(txs, [])

    def test_error_result_string(self):
        payload = {"status": "0", "message": "NOTOK", "result": "Error! Invalid address"}
        txs, _ = _parse_etherscan_txlist(payload)
        self.assertEqual(txs, [])

    def test_non_dict(self):
        txs, _ = _parse_etherscan_txlist("garbage")
        self.assertEqual(txs, [])

    def test_unexpected_result_type(self):
        payload = {"status": "1", "result": {"foo": "bar"}}
        txs, _ = _parse_etherscan_txlist(payload)
        self.assertEqual(txs, [])


# ─── Wallet registry ─────────────────────────────────────────────────────────


class WalletRegistryTestCase(unittest.TestCase):
    def test_default_registry_non_empty(self):
        reg = DEFAULT_SMART_MONEY_WALLETS
        self.assertGreater(len(reg), 0)
        for addr, label in reg:
            self.assertTrue(addr.startswith("0x"))
            self.assertEqual(len(addr), 42)
            self.assertEqual(addr, addr.lower(), f"address {addr} not lowercase")
            self.assertTrue(label)

    def test_no_env_override_uses_default(self):
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_ADDRESSES": ""}, clear=False):
            self.assertEqual(get_wallet_registry(), DEFAULT_SMART_MONEY_WALLETS)

    def test_env_override_replaces_default(self):
        override = "0x1111111111111111111111111111111111111111:Alpha,0x2222222222222222222222222222222222222222:Beta"
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_ADDRESSES": override}, clear=False):
            reg = get_wallet_registry()
            self.assertEqual(len(reg), 2)
            self.assertEqual(reg[0], ("0x1111111111111111111111111111111111111111", "Alpha"))
            self.assertEqual(reg[1], ("0x2222222222222222222222222222222222222222", "Beta"))

    def test_env_override_skips_invalid_addresses(self):
        # First entry invalid (not 0x, wrong length); second valid → only second survives.
        override = "bogus:X,0x1111111111111111111111111111111111111111:Real"
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_ADDRESSES": override}, clear=False):
            reg = get_wallet_registry()
            self.assertEqual(len(reg), 1)
            self.assertEqual(reg[0][1], "Real")

    def test_env_override_empty_label_uses_short_address(self):
        override = "0x1111111111111111111111111111111111111111:"
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_ADDRESSES": override}, clear=False):
            reg = get_wallet_registry()
            self.assertEqual(reg[0][1], "0x11111111")

    def test_env_override_all_invalid_falls_back(self):
        override = "garbage,more_garbage:nope"
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_ADDRESSES": override}, clear=False):
            reg = get_wallet_registry()
            self.assertEqual(reg, DEFAULT_SMART_MONEY_WALLETS)

    def test_env_override_uppercase_normalized(self):
        override = "0xABCDABCDABCDABCDABCDABCDABCDABCDABCDABCD:Tag"
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_ADDRESSES": override}, clear=False):
            reg = get_wallet_registry()
            self.assertEqual(reg[0][0], "0xabcdabcdabcdabcdabcdabcdabcdabcdabcdabcd")


# ─── fetch_smart_money_wallet_flows (end-to-end with mocked HTTP) ────────────


class FetchSmartMoneyWalletFlowsTestCase(unittest.TestCase):
    def setUp(self):
        # Limit registry to 2 wallets для скорости тестов.
        self.test_wallets = (
            ("0x1111111111111111111111111111111111111111", "Alpha"),
            ("0x2222222222222222222222222222222222222222", "Beta"),
        )

    def _make_mock_http_client(self, *, block_response: dict | None = None, tx_responses_by_addr: dict | None = None):
        """Создаёт callable, который мокает Etherscan API.

        block_response — что вернуть для action=getblocknobytime.
        tx_responses_by_addr — {address.lower(): {"status":...,"result":[...]} } для action=txlist.
        """
        block_response = block_response or {"status": "1", "result": "20000000"}
        tx_responses_by_addr = tx_responses_by_addr or {}

        async def _client(*, method: str, url: str, params: dict, **_):
            action = params.get("action", "")
            if action == "getblocknobytime":
                return block_response
            if action == "txlist":
                addr = params.get("address", "").lower()
                return tx_responses_by_addr.get(addr, {"status": "0", "result": []})
            raise AssertionError(f"unexpected action: {action}")

        return _client

    def test_no_api_key_returns_unknown(self):
        with patch.dict(os.environ, {"ETHERSCAN_API_KEY": ""}, clear=False):
            sig = _async_run(fetch_smart_money_wallet_flows(
                http_client=self._make_mock_http_client(),
                api_key="",
                wallets=self.test_wallets,
            ))
        self.assertEqual(sig.label, LABEL_UNKNOWN)
        self.assertEqual(sig.n_wallets_tracked, 2)

    def test_end_to_end_accumulating(self):
        # Both wallets net buying — should result in ACCUMULATING.
        import time as _t
        now = int(_t.time())
        tx_responses = {
            "0x1111111111111111111111111111111111111111": {
                "status": "1",
                "result": [
                    {
                        "from": "0xother", "to": "0x1111111111111111111111111111111111111111",
                        "value": _wei(3000), "timeStamp": str(now - 3600), "isError": "0",
                    },
                ],
            },
            "0x2222222222222222222222222222222222222222": {
                "status": "1",
                "result": [
                    {
                        "from": "0xother", "to": "0x2222222222222222222222222222222222222222",
                        "value": _wei(4000), "timeStamp": str(now - 1800), "isError": "0",
                    },
                ],
            },
        }
        sig = _async_run(fetch_smart_money_wallet_flows(
            http_client=self._make_mock_http_client(tx_responses_by_addr=tx_responses),
            api_key="DUMMY_KEY",
            wallets=self.test_wallets,
            lookback_hours=24,
        ))
        self.assertEqual(sig.label, LABEL_ACCUMULATING)
        self.assertTrue(sig.is_strong_signal)
        self.assertAlmostEqual(sig.total_net_eth_flow, 7000.0, places=1)
        self.assertEqual(sig.n_wallets_tracked, 2)

    def test_end_to_end_distributing(self):
        import time as _t
        now = int(_t.time())
        tx_responses = {
            "0x1111111111111111111111111111111111111111": {
                "status": "1",
                "result": [
                    {
                        "from": "0x1111111111111111111111111111111111111111", "to": "0xexchange",
                        "value": _wei(2500), "timeStamp": str(now - 3600), "isError": "0",
                    },
                ],
            },
            "0x2222222222222222222222222222222222222222": {
                "status": "1",
                "result": [
                    {
                        "from": "0x2222222222222222222222222222222222222222", "to": "0xexchange",
                        "value": _wei(3500), "timeStamp": str(now - 1800), "isError": "0",
                    },
                ],
            },
        }
        sig = _async_run(fetch_smart_money_wallet_flows(
            http_client=self._make_mock_http_client(tx_responses_by_addr=tx_responses),
            api_key="DUMMY_KEY",
            wallets=self.test_wallets,
        ))
        self.assertEqual(sig.label, LABEL_DISTRIBUTING)
        self.assertTrue(sig.is_strong_signal)
        self.assertLess(sig.total_net_eth_flow, 0)

    def test_block_fetch_failure_falls_back_to_zero(self):
        # block_response status=0 → start_block=0, должен работать через timeStamp фильтр.
        sig = _async_run(fetch_smart_money_wallet_flows(
            http_client=self._make_mock_http_client(
                block_response={"status": "0", "message": "Error", "result": ""},
                tx_responses_by_addr={},
            ),
            api_key="DUMMY_KEY",
            wallets=self.test_wallets,
        ))
        # No txs → QUIET либо UNKNOWN, но не raise.
        self.assertIn(sig.label, [LABEL_QUIET, LABEL_UNKNOWN])

    def test_http_timeout_on_block_call_handled(self):
        async def _timeout_client(*, method, url, params, **_):
            if params.get("action") == "getblocknobytime":
                raise asyncio.TimeoutError()
            return {"status": "0", "result": []}

        sig = _async_run(fetch_smart_money_wallet_flows(
            http_client=_timeout_client,
            api_key="DUMMY_KEY",
            wallets=self.test_wallets,
            timeout=0.5,
        ))
        # Should not raise; fall back.
        self.assertIsNotNone(sig)

    def test_http_error_on_txlist_handled(self):
        async def _err_client(*, method, url, params, **_):
            if params.get("action") == "getblocknobytime":
                return {"status": "1", "result": "20000000"}
            raise RuntimeError("network down")

        sig = _async_run(fetch_smart_money_wallet_flows(
            http_client=_err_client,
            api_key="DUMMY_KEY",
            wallets=self.test_wallets,
        ))
        # Should not raise; all wallets return empty → QUIET.
        self.assertIn(sig.label, [LABEL_QUIET, LABEL_UNKNOWN])

    def test_timestamp_set(self):
        sig = _async_run(fetch_smart_money_wallet_flows(
            http_client=self._make_mock_http_client(),
            api_key="DUMMY_KEY",
            wallets=self.test_wallets,
        ))
        self.assertGreater(sig.timestamp_ms, 0)
        self.assertEqual(sig.source, "etherscan-v2-chainid-1")


# ─── Score contribution ──────────────────────────────────────────────────────


class ScoreContributionTestCase(unittest.TestCase):
    def _sig(self, label: str, *, strong: bool = False, n_total: int = 8,
             n_accum: int = 0, n_distr: int = 0, net: float = 0.0) -> SmartMoneyWalletsSignal:
        return SmartMoneyWalletsSignal(
            n_wallets_tracked=n_total,
            accumulating_count=n_accum,
            distributing_count=n_distr,
            total_net_eth_flow=net,
            label=label,
            is_strong_signal=strong,
            lookback_hours=24,
        )

    def test_unknown_zero(self):
        delta, bull, bear = smart_money_wallets_score_contribution(self._sig(LABEL_UNKNOWN))
        self.assertEqual(delta, 0)
        self.assertEqual(bull, [])
        self.assertEqual(bear, [])

    def test_quiet_zero(self):
        delta, bull, bear = smart_money_wallets_score_contribution(self._sig(LABEL_QUIET))
        self.assertEqual(delta, 0)

    def test_mixed_zero(self):
        delta, bull, bear = smart_money_wallets_score_contribution(self._sig(LABEL_MIXED))
        self.assertEqual(delta, 0)

    def test_accumulating_weak_plus_one(self):
        sig = self._sig(LABEL_ACCUMULATING, strong=False, n_accum=4, net=2000.0)
        delta, bull, bear = smart_money_wallets_score_contribution(sig)
        self.assertEqual(delta, 1)
        self.assertEqual(len(bull), 1)
        self.assertEqual(bear, [])
        self.assertIn("копят", bull[0])

    def test_accumulating_strong_plus_two(self):
        sig = self._sig(LABEL_ACCUMULATING, strong=True, n_accum=7, net=8000.0)
        delta, bull, bear = smart_money_wallets_score_contribution(sig)
        self.assertEqual(delta, 2)
        self.assertEqual(len(bull), 1)

    def test_distributing_weak_minus_one(self):
        sig = self._sig(LABEL_DISTRIBUTING, strong=False, n_distr=5, net=-2000.0)
        delta, bull, bear = smart_money_wallets_score_contribution(sig)
        self.assertEqual(delta, -1)
        self.assertEqual(len(bear), 1)
        self.assertIn("раздают", bear[0])

    def test_distributing_strong_minus_two(self):
        sig = self._sig(LABEL_DISTRIBUTING, strong=True, n_distr=6, net=-8000.0)
        delta, bull, bear = smart_money_wallets_score_contribution(sig)
        self.assertEqual(delta, -2)

    def test_none_signal_handled(self):
        delta, bull, bear = smart_money_wallets_score_contribution(None)
        self.assertEqual(delta, 0)


# ─── Format for agents ───────────────────────────────────────────────────────


class FormatForAgentsTestCase(unittest.TestCase):
    def test_unknown_signal_returns_no_data_string(self):
        sig = SmartMoneyWalletsSignal(label=LABEL_UNKNOWN)
        text = format_smart_money_wallets_for_agents(sig)
        self.assertIn("нет данных", text)

    def test_accumulating_signal_text(self):
        sig = SmartMoneyWalletsSignal(
            wallets=[
                WalletNetFlow(
                    address="0xa", label="Wintermute", received_eth=1000.0,
                    sent_eth=200.0, net_eth_flow=800.0, tx_count=10,
                ),
            ],
            total_net_eth_flow=800.0,
            total_received_eth=1000.0,
            total_sent_eth=200.0,
            accumulating_count=1,
            n_wallets_tracked=1,
            alignment_ratio=1.0,
            label=LABEL_ACCUMULATING,
            is_strong_signal=False,
            lookback_hours=24,
            source="etherscan-v2-chainid-1",
        )
        text = format_smart_money_wallets_for_agents(sig)
        self.assertIn("ACCUMULATING", text)
        self.assertIn("Wintermute", text)
        self.assertIn("+800", text)

    def test_strong_flag_shown(self):
        sig = SmartMoneyWalletsSignal(
            wallets=[],
            label=LABEL_ACCUMULATING,
            is_strong_signal=True,
            n_wallets_tracked=8,
            lookback_hours=24,
        )
        text = format_smart_money_wallets_for_agents(sig)
        self.assertIn("strong", text)

    def test_none_signal_handled(self):
        text = format_smart_money_wallets_for_agents(None)
        self.assertIsInstance(text, str)


# ─── Feature flag & env parsers ──────────────────────────────────────────────


class FeatureFlagTestCase(unittest.TestCase):
    def test_default_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FEATURE_SMART_MONEY_WALLETS", None)
            self.assertFalse(feature_enabled())

    def test_on(self):
        for v in ["1", "true", "TRUE", "yes", "on"]:
            with patch.dict(os.environ, {"FEATURE_SMART_MONEY_WALLETS": v}, clear=False):
                self.assertTrue(feature_enabled(), f"failed for {v}")

    def test_off(self):
        for v in ["0", "false", "no", "off", ""]:
            with patch.dict(os.environ, {"FEATURE_SMART_MONEY_WALLETS": v}, clear=False):
                self.assertFalse(feature_enabled(), f"failed for {v}")


class EnvParserTestCase(unittest.TestCase):
    def test_lookback_hours_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SMART_MONEY_WALLETS_LOOKBACK_HOURS", None)
            self.assertEqual(get_lookback_hours(), 24)

    def test_lookback_hours_custom(self):
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_LOOKBACK_HOURS": "48"}, clear=False):
            self.assertEqual(get_lookback_hours(), 48)

    def test_lookback_hours_out_of_range_uses_default(self):
        for v in ["0", "200", "-5", "not_a_number"]:
            with patch.dict(os.environ, {"SMART_MONEY_WALLETS_LOOKBACK_HOURS": v}, clear=False):
                self.assertEqual(get_lookback_hours(), 24, f"failed for {v}")

    def test_noise_floor_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SMART_MONEY_WALLETS_NOISE_FLOOR_ETH", None)
            self.assertEqual(get_noise_floor_eth(), 10.0)

    def test_flow_threshold_custom(self):
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_FLOW_THRESHOLD_ETH": "2500"}, clear=False):
            self.assertEqual(get_flow_threshold_eth(), 2500.0)

    def test_strong_flow_threshold_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SMART_MONEY_WALLETS_STRONG_FLOW_THRESHOLD_ETH", None)
            self.assertEqual(get_strong_flow_threshold_eth(), 5000.0)

    def test_alignment_threshold_bounds(self):
        with patch.dict(os.environ, {"SMART_MONEY_WALLETS_ALIGNMENT_THRESHOLD": "1.5"}, clear=False):
            # 1.5 > 1.0 max → fallback to default 0.75
            self.assertEqual(get_alignment_threshold(), 0.75)

    def test_inter_call_delay_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SMART_MONEY_WALLETS_INTER_CALL_DELAY_S", None)
            self.assertEqual(get_inter_call_delay_s(), 0.0)


# ─── signal_to_dict ──────────────────────────────────────────────────────────


class SignalToDictTestCase(unittest.TestCase):
    def test_round_trip(self):
        sig = SmartMoneyWalletsSignal(
            wallets=[WalletNetFlow(
                address="0xa", label="X", received_eth=1.0,
                sent_eth=0.0, net_eth_flow=1.0, tx_count=1,
            )],
            total_net_eth_flow=1.0,
            label=LABEL_ACCUMULATING,
        )
        d = signal_to_dict(sig)
        self.assertEqual(d["label"], LABEL_ACCUMULATING)
        self.assertEqual(d["total_net_eth_flow"], 1.0)
        self.assertEqual(len(d["wallets"]), 1)
        self.assertEqual(d["wallets"][0]["label"], "X")


if __name__ == "__main__":
    unittest.main()
