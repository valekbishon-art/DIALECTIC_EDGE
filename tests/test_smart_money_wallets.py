"""Unit tests для market_indicators/smart_money_wallets.py (pure math)."""

from __future__ import annotations

import unittest

from market_indicators.smart_money_wallets import (
    DEFAULT_ALIGNMENT_THRESHOLD,
    DEFAULT_FLOW_THRESHOLD_ETH,
    DEFAULT_STRONG_FLOW_THRESHOLD_ETH,
    ETH_DECIMALS,
    LABEL_ACCUMULATING,
    LABEL_DISTRIBUTING,
    LABEL_MIXED,
    LABEL_QUIET,
    LABEL_UNKNOWN,
    PER_WALLET_NOISE_FLOOR_ETH,
    WalletNetFlow,
    aggregate_wallet_flows,
    compute_wallet_flow,
    wei_to_eth,
)


def _wei(eth: float) -> str:
    """Helper: convert ETH float → wei decimal-string (как Etherscan возвращает)."""
    return str(int(round(eth * (10**ETH_DECIMALS))))


# ─── wei_to_eth ──────────────────────────────────────────────────────────────


class WeiToEthTestCase(unittest.TestCase):
    def test_basic_conversion(self):
        # 1 ETH = 10^18 wei.
        self.assertEqual(wei_to_eth(10**18), 1.0)

    def test_string_input(self):
        # Etherscan возвращает строки для big-int.
        self.assertEqual(wei_to_eth("1000000000000000000"), 1.0)

    def test_fractional(self):
        self.assertAlmostEqual(wei_to_eth(10**17), 0.1, places=10)

    def test_zero(self):
        self.assertEqual(wei_to_eth(0), 0.0)
        self.assertEqual(wei_to_eth("0"), 0.0)

    def test_negative_returns_zero(self):
        # Защита: balance не может быть отрицательным.
        self.assertEqual(wei_to_eth(-1000), 0.0)

    def test_invalid_input_returns_zero(self):
        self.assertEqual(wei_to_eth("abc"), 0.0)
        self.assertEqual(wei_to_eth(None), 0.0)
        self.assertEqual(wei_to_eth([]), 0.0)


# ─── compute_wallet_flow ─────────────────────────────────────────────────────


class ComputeWalletFlowTestCase(unittest.TestCase):
    ADDR = "0xabcdef0123456789abcdef0123456789abcdef01"
    OTHER = "0x1111111111111111111111111111111111111111"

    def _tx(self, *, from_addr: str, to_addr: str, value_eth: float, ts: int, isError: str = "0") -> dict:
        return {
            "from": from_addr,
            "to": to_addr,
            "value": _wei(value_eth),
            "timeStamp": str(ts),
            "isError": isError,
        }

    def test_inflow_only(self):
        txs = [
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=100.0, ts=2000),
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=50.0, ts=3000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertAlmostEqual(flow.received_eth, 150.0, places=6)
        self.assertEqual(flow.sent_eth, 0.0)
        self.assertAlmostEqual(flow.net_eth_flow, 150.0, places=6)
        self.assertEqual(flow.tx_count, 2)

    def test_outflow_only(self):
        txs = [
            self._tx(from_addr=self.ADDR, to_addr=self.OTHER, value_eth=80.0, ts=2000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertEqual(flow.received_eth, 0.0)
        self.assertAlmostEqual(flow.sent_eth, 80.0, places=6)
        self.assertAlmostEqual(flow.net_eth_flow, -80.0, places=6)
        self.assertEqual(flow.tx_count, 1)

    def test_mixed_in_and_out(self):
        txs = [
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=200.0, ts=2000),
            self._tx(from_addr=self.ADDR, to_addr=self.OTHER, value_eth=75.0, ts=2500),
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=25.0, ts=3000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertAlmostEqual(flow.received_eth, 225.0, places=6)
        self.assertAlmostEqual(flow.sent_eth, 75.0, places=6)
        self.assertAlmostEqual(flow.net_eth_flow, 150.0, places=6)
        self.assertEqual(flow.tx_count, 3)

    def test_filters_old_transactions(self):
        # tx ts=500 — раньше окна (since=1000) → исключаем.
        txs = [
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=999.0, ts=500),
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=10.0, ts=2000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertAlmostEqual(flow.received_eth, 10.0, places=6)
        self.assertEqual(flow.tx_count, 1)

    def test_filters_failed_transactions(self):
        # isError=1 → tx failed, value не передавался.
        txs = [
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=100.0, ts=2000, isError="1"),
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=50.0, ts=2500, isError="0"),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertAlmostEqual(flow.received_eth, 50.0, places=6)
        self.assertEqual(flow.tx_count, 1)

    def test_self_transaction(self):
        # from == to == address → net = 0, но count = 1.
        txs = [
            self._tx(from_addr=self.ADDR, to_addr=self.ADDR, value_eth=10.0, ts=2000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertAlmostEqual(flow.received_eth, 10.0, places=6)
        self.assertAlmostEqual(flow.sent_eth, 10.0, places=6)
        self.assertEqual(flow.net_eth_flow, 0.0)
        self.assertEqual(flow.tx_count, 1)

    def test_unrelated_transaction_skipped(self):
        # tx где address не at all (defensive — txlist по address не должен такое возвращать).
        txs = [
            self._tx(from_addr=self.OTHER, to_addr=self.OTHER, value_eth=100.0, ts=2000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertEqual(flow.received_eth, 0.0)
        self.assertEqual(flow.sent_eth, 0.0)
        self.assertEqual(flow.tx_count, 0)

    def test_address_case_insensitive(self):
        # ADDR в lower, tx с uppercase → должны match.
        txs = [
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR.upper(), value_eth=42.0, ts=2000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        self.assertAlmostEqual(flow.received_eth, 42.0, places=6)

    def test_malformed_tx_skipped(self):
        # Defensive: tx без timeStamp / без value / not a dict.
        txs = [
            "not a dict",
            {"from": self.OTHER, "to": self.ADDR, "value": "not_a_number", "timeStamp": "2000"},
            {"from": self.OTHER, "to": self.ADDR, "value": _wei(5.0), "timeStamp": "abc"},
            self._tx(from_addr=self.OTHER, to_addr=self.ADDR, value_eth=10.0, ts=2000),
        ]
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=txs, since_timestamp_s=1000,
        )
        # Только legitimate tx учитывается. Malformed value parses to 0 (count++ но flow 0).
        # Malformed timeStamp пропускается полностью.
        self.assertAlmostEqual(flow.received_eth, 10.0, places=6)

    def test_truncated_flag_passed_through(self):
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=[], since_timestamp_s=1000, truncated=True,
        )
        self.assertTrue(flow.truncated)

    def test_empty_txs(self):
        flow = compute_wallet_flow(
            address=self.ADDR, label="Test", txs=[], since_timestamp_s=1000,
        )
        self.assertEqual(flow.received_eth, 0.0)
        self.assertEqual(flow.sent_eth, 0.0)
        self.assertEqual(flow.net_eth_flow, 0.0)
        self.assertEqual(flow.tx_count, 0)


# ─── aggregate_wallet_flows ──────────────────────────────────────────────────


class AggregateWalletFlowsTestCase(unittest.TestCase):
    def _flow(self, label: str, net: float, count: int = 5) -> WalletNetFlow:
        recv = max(0.0, net)
        sent = max(0.0, -net)
        return WalletNetFlow(
            address="0x" + "0" * 40,
            label=label,
            received_eth=recv,
            sent_eth=sent,
            net_eth_flow=net,
            tx_count=count,
            truncated=False,
        )

    def test_empty_list_returns_unknown(self):
        sig = aggregate_wallet_flows([])
        self.assertEqual(sig.label, LABEL_UNKNOWN)
        self.assertEqual(sig.n_wallets_tracked, 0)
        self.assertEqual(sig.total_net_eth_flow, 0.0)
        self.assertFalse(sig.is_strong_signal)

    def test_all_quiet_below_noise_floor(self):
        # Все потоки ниже noise floor → QUIET.
        flows = [self._flow(f"w{i}", net=5.0) for i in range(5)]  # 5 ETH < 10 noise
        sig = aggregate_wallet_flows(flows)
        self.assertEqual(sig.label, LABEL_QUIET)
        self.assertEqual(sig.accumulating_count, 0)
        self.assertEqual(sig.distributing_count, 0)

    def test_accumulating_strong(self):
        # 8 wallets, все >> noise floor, все buying, sum >> strong threshold.
        flows = [self._flow(f"w{i}", net=1000.0) for i in range(8)]  # sum=8000 > 5000 strong
        sig = aggregate_wallet_flows(flows)
        self.assertEqual(sig.label, LABEL_ACCUMULATING)
        self.assertTrue(sig.is_strong_signal)
        self.assertEqual(sig.accumulating_count, 8)
        self.assertEqual(sig.alignment_ratio, 1.0)
        self.assertAlmostEqual(sig.total_net_eth_flow, 8000.0, places=6)

    def test_distributing_strong(self):
        flows = [self._flow(f"w{i}", net=-1500.0) for i in range(6)]  # sum=-9000
        sig = aggregate_wallet_flows(flows)
        self.assertEqual(sig.label, LABEL_DISTRIBUTING)
        self.assertTrue(sig.is_strong_signal)
        self.assertEqual(sig.distributing_count, 6)
        self.assertAlmostEqual(sig.total_net_eth_flow, -9000.0, places=6)

    def test_mixed_high_volume_no_alignment(self):
        # Половина копит, половина раздаёт — net около нуля, но volume big.
        flows = [
            self._flow("buyer1", net=2000.0),
            self._flow("buyer2", net=2000.0),
            self._flow("seller1", net=-2000.0),
            self._flow("seller2", net=-2000.0),
        ]
        sig = aggregate_wallet_flows(flows)
        # net = 0 → меньше flow_threshold (1000) → MIXED либо QUIET.
        # abs_net=0 < noise_floor → QUIET.
        self.assertIn(sig.label, [LABEL_QUIET, LABEL_MIXED])
        self.assertFalse(sig.is_strong_signal)

    def test_mixed_label_offset_flows(self):
        # Slight buying tilt, but no clear alignment, sum < flow_threshold.
        flows = [
            self._flow("buyer1", net=500.0),
            self._flow("buyer2", net=400.0),
            self._flow("seller1", net=-300.0),
        ]
        sig = aggregate_wallet_flows(flows)
        # net=+600 < 1000 flow_threshold → MIXED (>= noise floor).
        self.assertEqual(sig.label, LABEL_MIXED)
        self.assertFalse(sig.is_strong_signal)
        self.assertEqual(sig.accumulating_count, 2)
        self.assertEqual(sig.distributing_count, 1)

    def test_accumulating_weak_no_strong_flag(self):
        # Sum выше flow_threshold но ниже strong → ACCUMULATING без strong.
        flows = [self._flow(f"w{i}", net=400.0) for i in range(5)]  # sum=2000, > 1000 не > 5000
        sig = aggregate_wallet_flows(flows)
        self.assertEqual(sig.label, LABEL_ACCUMULATING)
        self.assertFalse(sig.is_strong_signal)

    def test_alignment_ratio_computation(self):
        # 6 wallets: 4 buy, 1 sell, 1 neutral → alignment = 4/6 = 0.667.
        flows = [
            self._flow("b1", net=100.0),
            self._flow("b2", net=200.0),
            self._flow("b3", net=300.0),
            self._flow("b4", net=400.0),
            self._flow("s1", net=-500.0),
            self._flow("n1", net=5.0),  # < noise floor
        ]
        sig = aggregate_wallet_flows(flows)
        self.assertAlmostEqual(sig.alignment_ratio, 4 / 6, places=4)
        self.assertEqual(sig.accumulating_count, 4)
        self.assertEqual(sig.distributing_count, 1)
        self.assertEqual(sig.neutral_count, 1)

    def test_metadata_propagation(self):
        flows = [self._flow("w1", net=100.0)]
        sig = aggregate_wallet_flows(
            flows,
            lookback_hours=48,
            timestamp_ms=1234567,
            source="test-source",
        )
        self.assertEqual(sig.lookback_hours, 48)
        self.assertEqual(sig.timestamp_ms, 1234567)
        self.assertEqual(sig.source, "test-source")
        self.assertEqual(sig.n_wallets_tracked, 1)

    def test_custom_thresholds_respected(self):
        flows = [self._flow(f"w{i}", net=50.0) for i in range(5)]  # sum=250
        sig_default = aggregate_wallet_flows(flows)
        # Default flow_threshold=1000 → MIXED (250 < 1000) at best.
        self.assertNotEqual(sig_default.label, LABEL_ACCUMULATING)
        sig_loose = aggregate_wallet_flows(
            flows, flow_threshold_eth=100.0, strong_flow_threshold_eth=200.0,
        )
        self.assertEqual(sig_loose.label, LABEL_ACCUMULATING)

    def test_lookback_hours_floor(self):
        # lookback=0 invalid → должен подняться до 1.
        sig = aggregate_wallet_flows([self._flow("w1", 100.0)], lookback_hours=0)
        self.assertEqual(sig.lookback_hours, 1)

    def test_zero_division_safety(self):
        # n=0 — handled через empty_list_returns_unknown; здесь — n=1 эджа.
        flows = [self._flow("only", net=0.0)]
        sig = aggregate_wallet_flows(flows)
        # net=0, тоже classified как QUIET.
        self.assertEqual(sig.label, LABEL_QUIET)
        self.assertEqual(sig.alignment_ratio, 0.0)  # max(0, 0) / 1 = 0

    def test_constants_sanity(self):
        # Регрессия: defaults осмысленны.
        self.assertGreater(DEFAULT_STRONG_FLOW_THRESHOLD_ETH, DEFAULT_FLOW_THRESHOLD_ETH)
        self.assertGreater(DEFAULT_FLOW_THRESHOLD_ETH, PER_WALLET_NOISE_FLOOR_ETH)
        self.assertTrue(0.0 < DEFAULT_ALIGNMENT_THRESHOLD <= 1.0)


if __name__ == "__main__":
    unittest.main()
