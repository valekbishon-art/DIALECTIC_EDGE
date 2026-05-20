"""Pure-math tests для market_indicators.stablecoin_flows.

Покрытие:
  * normalize_supply (decimals)
  * aggregate_supply (multi-chain, фильтрация по token, OOB-защита)
  * classify_flow_class (5 классов + unknown, пороги)
  * build_flow_signal (полный цикл)
  * detect_flow_event (4 события + None)
  * format_flow_summary
"""

from __future__ import annotations

import math
import unittest

from market_indicators.stablecoin_flows import (
    DEFAULT_MASSIVE_MINT_PCT,
    DEFAULT_MINT_PCT,
    SANITY_MAX_SUPPLY_USD,
    StablecoinFlowSignal,
    StablecoinSupplySnapshot,
    aggregate_supply,
    build_flow_signal,
    classify_flow_class,
    detect_flow_event,
    format_flow_summary,
    normalize_supply,
)


class NormalizeSupplyTestCase(unittest.TestCase):
    def test_six_decimals_usdt(self):
        # 60B USDT с decimals=6
        v = normalize_supply(raw_units=60_000_000_000 * 10**6, decimals=6)
        self.assertAlmostEqual(v, 60_000_000_000.0, delta=1.0)

    def test_eighteen_decimals_dai(self):
        v = normalize_supply(raw_units=5_000_000_000 * 10**18, decimals=18)
        self.assertAlmostEqual(v, 5_000_000_000.0, delta=1.0)

    def test_zero_decimals(self):
        v = normalize_supply(raw_units=123, decimals=0)
        self.assertEqual(v, 123.0)

    def test_negative_clamps_zero(self):
        v = normalize_supply(raw_units=-100, decimals=6)
        self.assertEqual(v, 0.0)

    def test_negative_decimals_raises(self):
        with self.assertRaises(ValueError):
            normalize_supply(raw_units=100, decimals=-1)


class AggregateSupplyTestCase(unittest.TestCase):
    def _snap(self, *, token, chain, units, decimals=6, ts=1000):
        return StablecoinSupplySnapshot(
            token=token, chain=chain, raw_supply_units=units,
            decimals=decimals, timestamp_ms=ts,
        )

    def test_sums_multiple_chains_for_same_token(self):
        snaps = [
            self._snap(token="USDT", chain="ethereum", units=60_000_000_000 * 10**6),
            self._snap(token="USDT", chain="tron",     units=80_000_000_000 * 10**6),
        ]
        total, chains = aggregate_supply(snaps, token="USDT")
        self.assertAlmostEqual(total, 140_000_000_000.0, delta=1.0)
        self.assertEqual(chains, ("ethereum", "tron"))

    def test_filters_other_tokens(self):
        snaps = [
            self._snap(token="USDT", chain="ethereum", units=10 * 10**6),
            self._snap(token="USDC", chain="ethereum", units=20 * 10**6),
        ]
        total, chains = aggregate_supply(snaps, token="USDT")
        self.assertEqual(total, 10.0)
        self.assertEqual(chains, ("ethereum",))

    def test_oob_skipped(self):
        # Огромный supply > 1Q USD должен пропускаться.
        snaps = [
            self._snap(token="USDT", chain="ethereum",
                       units=int(SANITY_MAX_SUPPLY_USD * 10) * 10**6),
            self._snap(token="USDT", chain="tron", units=10 * 10**6),
        ]
        total, chains = aggregate_supply(snaps, token="USDT")
        self.assertEqual(total, 10.0)
        self.assertEqual(chains, ("tron",))

    def test_empty_returns_zero(self):
        total, chains = aggregate_supply([], token="USDT")
        self.assertEqual(total, 0.0)
        self.assertEqual(chains, ())

    def test_case_insensitive_token(self):
        snaps = [self._snap(token="usdt", chain="ethereum", units=10 * 10**6)]
        total, _ = aggregate_supply(snaps, token="USDT")
        self.assertEqual(total, 10.0)


class ClassifyFlowClassTestCase(unittest.TestCase):
    def test_none_is_unknown(self):
        self.assertEqual(classify_flow_class(None), "unknown")

    def test_nan_is_unknown(self):
        self.assertEqual(classify_flow_class(float("nan")), "unknown")

    def test_inf_is_unknown(self):
        self.assertEqual(classify_flow_class(float("inf")), "unknown")

    def test_neutral(self):
        # 0.1% — внутри [-0.25%, +0.25%]
        self.assertEqual(classify_flow_class(0.001), "neutral")

    def test_mint(self):
        self.assertEqual(classify_flow_class(DEFAULT_MINT_PCT), "mint")
        self.assertEqual(classify_flow_class(DEFAULT_MINT_PCT * 2), "mint")

    def test_massive_mint(self):
        self.assertEqual(classify_flow_class(DEFAULT_MASSIVE_MINT_PCT), "massive_mint")
        self.assertEqual(classify_flow_class(0.05), "massive_mint")

    def test_redeem(self):
        self.assertEqual(classify_flow_class(-DEFAULT_MINT_PCT), "redeem")
        self.assertEqual(classify_flow_class(-DEFAULT_MINT_PCT * 2), "redeem")

    def test_massive_redeem(self):
        self.assertEqual(classify_flow_class(-DEFAULT_MASSIVE_MINT_PCT), "massive_redeem")
        self.assertEqual(classify_flow_class(-0.05), "massive_redeem")

    def test_custom_thresholds(self):
        self.assertEqual(
            classify_flow_class(0.005, mint_threshold=0.01, massive_mint_threshold=0.02),
            "neutral",
        )
        self.assertEqual(
            classify_flow_class(0.015, mint_threshold=0.01, massive_mint_threshold=0.02),
            "mint",
        )


class BuildFlowSignalTestCase(unittest.TestCase):
    def _snap(self, units, chain="ethereum"):
        return StablecoinSupplySnapshot(
            token="USDT", chain=chain, raw_supply_units=units,
            decimals=6, timestamp_ms=2000,
        )

    def test_basic_mint(self):
        # 60.5B vs 60B → +500M ≈ +0.826% → mint
        sig = build_flow_signal(
            token="USDT",
            current_snapshots=[self._snap(60_500_000_000 * 10**6)],
            previous_supply_usd=60_000_000_000.0,
            timestamp_ms=2000,
        )
        self.assertEqual(sig.token, "USDT")
        self.assertEqual(sig.timestamp_ms, 2000)
        self.assertAlmostEqual(sig.supply_total_usd, 60_500_000_000.0, delta=1.0)
        self.assertIsNotNone(sig.delta_24h_usd)
        self.assertAlmostEqual(sig.delta_24h_usd, 500_000_000.0, delta=1.0)  # type: ignore[arg-type]
        self.assertIsNotNone(sig.delta_pct_24h)
        # 500M / 60.5B ≈ 0.826%
        self.assertAlmostEqual(sig.delta_pct_24h, 500_000_000.0 / 60_500_000_000.0, places=6)
        self.assertEqual(sig.flow_class, "mint")
        self.assertEqual(sig.chains_used, ("ethereum",))

    def test_no_previous_yields_unknown(self):
        sig = build_flow_signal(
            token="USDT",
            current_snapshots=[self._snap(60_000_000_000 * 10**6)],
            previous_supply_usd=None,
            timestamp_ms=2000,
        )
        self.assertIsNone(sig.delta_24h_usd)
        self.assertIsNone(sig.delta_pct_24h)
        self.assertEqual(sig.flow_class, "unknown")

    def test_zero_previous_yields_unknown(self):
        sig = build_flow_signal(
            token="USDT",
            current_snapshots=[self._snap(60_000_000_000 * 10**6)],
            previous_supply_usd=0.0,
            timestamp_ms=2000,
        )
        self.assertIsNone(sig.delta_24h_usd)
        self.assertEqual(sig.flow_class, "unknown")

    def test_negative_delta_classifies_redeem(self):
        # -500M из 60B → -0.840% → redeem
        sig = build_flow_signal(
            token="USDT",
            current_snapshots=[self._snap(59_500_000_000 * 10**6)],
            previous_supply_usd=60_000_000_000.0,
            timestamp_ms=2000,
        )
        self.assertAlmostEqual(sig.delta_24h_usd, -500_000_000.0, delta=1.0)  # type: ignore[arg-type]
        self.assertEqual(sig.flow_class, "redeem")

    def test_massive_mint_with_2pct_change(self):
        # +1.2B / 60B = +2% → massive_mint
        sig = build_flow_signal(
            token="USDT",
            current_snapshots=[self._snap(61_200_000_000 * 10**6)],
            previous_supply_usd=60_000_000_000.0,
            timestamp_ms=2000,
        )
        self.assertEqual(sig.flow_class, "massive_mint")

    def test_chains_used_preserved(self):
        sig = build_flow_signal(
            token="USDT",
            current_snapshots=[
                self._snap(50_000_000_000 * 10**6, chain="ethereum"),
                self._snap(80_000_000_000 * 10**6, chain="tron"),
            ],
            previous_supply_usd=129_000_000_000.0,
            timestamp_ms=2000,
        )
        self.assertEqual(sig.chains_used, ("ethereum", "tron"))
        self.assertAlmostEqual(sig.supply_total_usd, 130_000_000_000.0, delta=1.0)

    def test_token_upper_normalized(self):
        sig = build_flow_signal(
            token="usdt",
            current_snapshots=[self._snap(10 * 10**6)],
            previous_supply_usd=None,
            timestamp_ms=2000,
        )
        self.assertEqual(sig.token, "USDT")


class DetectFlowEventTestCase(unittest.TestCase):
    def _sig(self, flow_class, ts=1000):
        return StablecoinFlowSignal(
            token="USDT", timestamp_ms=ts, supply_total_usd=100e9,
            delta_24h_usd=None, delta_pct_24h=None,
            flow_class=flow_class, chains_used=(),
        )

    def test_none_previous_returns_none(self):
        self.assertIsNone(detect_flow_event(current=self._sig("mint"), previous=None))

    def test_mint_burst_from_neutral(self):
        self.assertEqual(
            detect_flow_event(
                current=self._sig("mint"), previous=self._sig("neutral"),
            ),
            "mint_burst",
        )

    def test_mint_burst_from_redeem(self):
        self.assertEqual(
            detect_flow_event(
                current=self._sig("massive_mint"), previous=self._sig("redeem"),
            ),
            "mint_burst",
        )

    def test_mint_cooldown(self):
        self.assertEqual(
            detect_flow_event(
                current=self._sig("neutral"), previous=self._sig("massive_mint"),
            ),
            "mint_cooldown",
        )

    def test_redeem_burst(self):
        self.assertEqual(
            detect_flow_event(
                current=self._sig("massive_redeem"), previous=self._sig("neutral"),
            ),
            "redeem_burst",
        )

    def test_redeem_cooldown(self):
        self.assertEqual(
            detect_flow_event(
                current=self._sig("neutral"), previous=self._sig("redeem"),
            ),
            "redeem_cooldown",
        )

    def test_no_change_in_class_returns_none(self):
        self.assertIsNone(detect_flow_event(
            current=self._sig("neutral"), previous=self._sig("neutral"),
        ))

    def test_mint_to_massive_mint_no_event(self):
        # mint → massive_mint остаётся «в кластере mint», без события.
        self.assertIsNone(detect_flow_event(
            current=self._sig("massive_mint"), previous=self._sig("mint"),
        ))

    def test_unknown_in_either_returns_none(self):
        self.assertIsNone(detect_flow_event(
            current=self._sig("unknown"), previous=self._sig("mint"),
        ))
        self.assertIsNone(detect_flow_event(
            current=self._sig("mint"), previous=self._sig("unknown"),
        ))


class FormatFlowSummaryTestCase(unittest.TestCase):
    def _sig(self, **overrides):
        defaults = dict(
            token="USDT", timestamp_ms=1000, supply_total_usd=140e9,
            delta_24h_usd=500e6, delta_pct_24h=500e6 / 140e9,
            flow_class="mint", chains_used=("ethereum", "tron"),
        )
        defaults.update(overrides)
        return StablecoinFlowSignal(**defaults)

    def test_contains_token_supply_delta(self):
        s = format_flow_summary(self._sig())
        self.assertIn("USDT", s)
        self.assertIn("140.00B", s)
        self.assertIn("+500.0M", s)
        self.assertIn("mint", s)

    def test_event_appended(self):
        s = format_flow_summary(self._sig(), event="mint_burst")
        self.assertIn("event=mint_burst", s)

    def test_handles_none_delta(self):
        s = format_flow_summary(self._sig(
            delta_24h_usd=None, delta_pct_24h=None, flow_class="unknown",
        ))
        self.assertIn("delta24h=n/a", s)
        self.assertIn("unknown", s)

    def test_handles_nan_delta_pct(self):
        s = format_flow_summary(self._sig(delta_pct_24h=float("nan")))
        # nan pct → 'n/a' для %
        self.assertIn("(n/a)", s)

    def test_chains_listed(self):
        s = format_flow_summary(self._sig())
        self.assertIn("ethereum,tron", s)

    def test_supply_under_million(self):
        s = format_flow_summary(self._sig(supply_total_usd=500.0))
        # Должен показать число, не B/M
        self.assertNotIn(".B", s)


if __name__ == "__main__":
    unittest.main()
