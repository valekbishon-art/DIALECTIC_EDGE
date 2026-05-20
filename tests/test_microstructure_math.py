"""Unit-тесты для market_indicators/microstructure.py (математика).

Покрывают:
  * `compute_depth_in_band` — bid/ask side, граничные случаи, пустой стакан.
  * `compute_depth_asymmetry` — корректные значения, NaN при нулевой depth.
  * `compute_quoted_spread_bps` — корректность, crossed book, нулевые входы.
  * `build_venue_snapshot` — happy-path и невалидные данные → None.
  * `compute_aggregate` — volume-weighted vs equal weights, partial flag,
    NaN-handling, пустой список.
  * `detect_liquidity_vacuum` — порог drop_pct, отсутствие baseline.
  * `classify_signal` — direction_bias (порог), vacuum, severity, partial.
  * `normalize_levels` — фильтрация мусора.

Stdlib-only — гоняется и в unit-fast, и в unit-full.
"""

from __future__ import annotations

import math
import unittest

from market_indicators.microstructure import (
    ASYMMETRY_NEUTRAL_THRESHOLD,
    DEFAULT_BAND_PCT,
    DEFAULT_MIN_VENUES_FOR_AGGREGATE,
    DEFAULT_VACUUM_DROP_PCT,
    AggregateMicrostructure,
    MicrostructureSignal,
    OrderbookLevel,
    VenueMicrostructure,
    build_venue_snapshot,
    classify_signal,
    compute_aggregate,
    compute_depth_asymmetry,
    compute_depth_in_band,
    compute_quoted_spread_bps,
    detect_liquidity_vacuum,
    normalize_levels,
)


# ─── compute_depth_in_band ───────────────────────────────────────────────────


class ComputeDepthInBandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Mid=100, band=1% → диапазон [99, 101].
        self.mid = 100.0
        self.bids = [
            OrderbookLevel(price=99.9, size=1.0),   # in
            OrderbookLevel(price=99.5, size=2.0),   # in (>= 99.0)
            OrderbookLevel(price=99.0, size=3.0),   # boundary (==99.0)
            OrderbookLevel(price=98.5, size=10.0),  # out (< 99.0)
        ]
        self.asks = [
            OrderbookLevel(price=100.1, size=1.0),  # in
            OrderbookLevel(price=100.5, size=2.0),  # in
            OrderbookLevel(price=101.0, size=3.0),  # boundary (== 101.0)
            OrderbookLevel(price=101.5, size=10.0),  # out
        ]

    def test_bid_depth_in_band(self) -> None:
        # 99.9*1 + 99.5*2 + 99.0*3 = 99.9 + 199.0 + 297.0 = 595.9
        usd = compute_depth_in_band(
            self.bids, mid_price=self.mid, band_pct=1.0, side="bid"
        )
        self.assertAlmostEqual(usd, 595.9, places=2)

    def test_ask_depth_in_band(self) -> None:
        # 100.1*1 + 100.5*2 + 101.0*3 = 100.1 + 201.0 + 303.0 = 604.1
        usd = compute_depth_in_band(
            self.asks, mid_price=self.mid, band_pct=1.0, side="ask"
        )
        self.assertAlmostEqual(usd, 604.1, places=2)

    def test_empty_levels(self) -> None:
        self.assertEqual(
            compute_depth_in_band([], mid_price=100.0, band_pct=0.5, side="bid"),
            0.0,
        )

    def test_zero_mid_returns_zero(self) -> None:
        self.assertEqual(
            compute_depth_in_band(self.bids, mid_price=0.0, band_pct=0.5, side="bid"),
            0.0,
        )

    def test_zero_band_returns_zero(self) -> None:
        self.assertEqual(
            compute_depth_in_band(self.bids, mid_price=100.0, band_pct=0.0, side="bid"),
            0.0,
        )

    def test_invalid_side_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_depth_in_band(self.bids, mid_price=100.0, band_pct=1.0, side="x")

    def test_narrow_band_filters_more(self) -> None:
        # band=0.5 → диапазон [99.5, 100.5]. Bid'ы: 99.9 + 99.5 = 199.4
        usd = compute_depth_in_band(
            self.bids, mid_price=self.mid, band_pct=0.5, side="bid"
        )
        # 99.9*1 + 99.5*2 = 99.9 + 199.0 = 298.9
        self.assertAlmostEqual(usd, 298.9, places=2)


# ─── compute_depth_asymmetry ─────────────────────────────────────────────────


class ComputeDepthAsymmetryTestCase(unittest.TestCase):
    def test_bid_heavier(self) -> None:
        # bid=600, ask=400 → (600-400)/1000 = 0.2
        self.assertAlmostEqual(compute_depth_asymmetry(600.0, 400.0), 0.2)

    def test_ask_heavier(self) -> None:
        self.assertAlmostEqual(compute_depth_asymmetry(400.0, 600.0), -0.2)

    def test_balanced(self) -> None:
        self.assertEqual(compute_depth_asymmetry(500.0, 500.0), 0.0)

    def test_zero_total_returns_nan(self) -> None:
        self.assertTrue(math.isnan(compute_depth_asymmetry(0.0, 0.0)))

    def test_bid_only(self) -> None:
        self.assertEqual(compute_depth_asymmetry(100.0, 0.0), 1.0)

    def test_ask_only(self) -> None:
        self.assertEqual(compute_depth_asymmetry(0.0, 100.0), -1.0)


# ─── compute_quoted_spread_bps ──────────────────────────────────────────────


class ComputeQuotedSpreadBpsTestCase(unittest.TestCase):
    def test_simple_spread(self) -> None:
        # bid=100, ask=100.1, mid=100.05 → (0.1/100.05)*10000 ≈ 9.995 bps
        spread = compute_quoted_spread_bps(100.0, 100.1, 100.05)
        self.assertAlmostEqual(spread, (0.1 / 100.05) * 10_000.0, places=4)

    def test_zero_inputs_return_nan(self) -> None:
        self.assertTrue(math.isnan(compute_quoted_spread_bps(0.0, 100.1, 100.0)))
        self.assertTrue(math.isnan(compute_quoted_spread_bps(100.0, 0.0, 100.0)))
        self.assertTrue(math.isnan(compute_quoted_spread_bps(100.0, 100.1, 0.0)))

    def test_crossed_book_returns_zero(self) -> None:
        # ask < bid → crossed; не падаем, возвращаем 0.
        self.assertEqual(compute_quoted_spread_bps(100.5, 100.0, 100.25), 0.0)


# ─── build_venue_snapshot ────────────────────────────────────────────────────


class BuildVenueSnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.bids = (
            OrderbookLevel(price=99.9, size=1.0),
            OrderbookLevel(price=99.5, size=2.0),
        )
        self.asks = (
            OrderbookLevel(price=100.1, size=1.0),
            OrderbookLevel(price=100.5, size=2.0),
        )

    def test_happy_path(self) -> None:
        snap = build_venue_snapshot(
            venue="binance", bids=self.bids, asks=self.asks,
            band_pct=1.0, timestamp_ms=12345,
        )
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.venue, "binance")
        self.assertAlmostEqual(snap.best_bid, 99.9)
        self.assertAlmostEqual(snap.best_ask, 100.1)
        self.assertAlmostEqual(snap.mid_price, 100.0)
        self.assertGreater(snap.bid_depth_usd, 0)
        self.assertGreater(snap.ask_depth_usd, 0)
        self.assertEqual(snap.timestamp_ms, 12345)

    def test_empty_bids_returns_none(self) -> None:
        self.assertIsNone(
            build_venue_snapshot(
                venue="x", bids=(), asks=self.asks, band_pct=1.0, timestamp_ms=1
            )
        )

    def test_empty_asks_returns_none(self) -> None:
        self.assertIsNone(
            build_venue_snapshot(
                venue="x", bids=self.bids, asks=(), band_pct=1.0, timestamp_ms=1
            )
        )

    def test_includes_volume_when_provided(self) -> None:
        snap = build_venue_snapshot(
            venue="binance", bids=self.bids, asks=self.asks,
            band_pct=1.0, timestamp_ms=1, volume_24h_usd=1_000_000.0,
        )
        assert snap is not None
        self.assertEqual(snap.volume_24h_usd, 1_000_000.0)


# ─── compute_aggregate ───────────────────────────────────────────────────────


class ComputeAggregateTestCase(unittest.TestCase):
    def _snap(
        self, venue: str, bid: float = 100.0, ask: float = 200.0,
        asym: float = 0.0, spread: float = 5.0, mid: float = 100.0,
        volume: float | None = None,
    ) -> VenueMicrostructure:
        return VenueMicrostructure(
            venue=venue, mid_price=mid, best_bid=mid - 0.1, best_ask=mid + 0.1,
            bid_depth_usd=bid, ask_depth_usd=ask, band_pct=0.5,
            quoted_spread_bps=spread, asymmetry=asym, timestamp_ms=1,
            volume_24h_usd=volume,
        )

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(compute_aggregate([], asset="BTC", timestamp_ms=1))

    def test_single_venue_partial(self) -> None:
        agg = compute_aggregate(
            [self._snap("binance", bid=100, ask=100)], asset="BTC", timestamp_ms=2,
        )
        assert agg is not None
        self.assertEqual(agg.venue_count, 1)
        self.assertTrue(agg.partial)
        self.assertEqual(agg.bid_depth_usd_total, 100.0)
        self.assertEqual(agg.ask_depth_usd_total, 100.0)

    def test_two_venues_not_partial(self) -> None:
        agg = compute_aggregate(
            [self._snap("binance", bid=100, ask=100), self._snap("bybit", bid=200, ask=300)],
            asset="BTC", timestamp_ms=3,
        )
        assert agg is not None
        self.assertEqual(agg.venue_count, 2)
        self.assertFalse(agg.partial)
        self.assertEqual(agg.bid_depth_usd_total, 300.0)
        self.assertEqual(agg.ask_depth_usd_total, 400.0)

    def test_equal_weights_when_no_volumes(self) -> None:
        agg = compute_aggregate(
            [
                self._snap("binance", asym=0.5, mid=100.0),
                self._snap("bybit", asym=-0.1, mid=101.0),
            ],
            asset="BTC", timestamp_ms=4,
        )
        assert agg is not None
        # asymmetry: (0.5 + -0.1) / 2 = 0.2
        self.assertAlmostEqual(agg.asymmetry_weighted, 0.2)
        # mid: (100 + 101) / 2 = 100.5
        self.assertAlmostEqual(agg.mid_price_weighted, 100.5)

    def test_volume_weighted_when_all_have_volumes(self) -> None:
        # bin volume=900M, bybit volume=100M → 90/10 split.
        snaps = [
            self._snap("binance", asym=0.5, volume=900_000_000.0),
            self._snap("bybit", asym=-0.1, volume=100_000_000.0),
        ]
        agg = compute_aggregate(snaps, asset="BTC", timestamp_ms=5)
        assert agg is not None
        # asymmetry: 0.9*0.5 + 0.1*(-0.1) = 0.45 - 0.01 = 0.44
        self.assertAlmostEqual(agg.asymmetry_weighted, 0.44, places=4)

    def test_partial_volume_falls_back_to_equal(self) -> None:
        snaps = [
            self._snap("binance", asym=0.5, volume=900_000_000.0),
            self._snap("bybit", asym=-0.1, volume=None),  # missing
        ]
        agg = compute_aggregate(snaps, asset="BTC", timestamp_ms=6)
        assert agg is not None
        # Fallback to equal weights → average asymmetry = 0.2.
        self.assertAlmostEqual(agg.asymmetry_weighted, 0.2)

    def test_nan_asymmetry_skipped(self) -> None:
        snaps = [
            self._snap("binance", asym=0.5),
            self._snap("bybit", asym=float("nan")),
        ]
        agg = compute_aggregate(snaps, asset="BTC", timestamp_ms=7)
        assert agg is not None
        # Только bn попадает в asymmetry weighted average → 0.5.
        self.assertAlmostEqual(agg.asymmetry_weighted, 0.5)

    def test_min_venues_threshold(self) -> None:
        agg = compute_aggregate(
            [self._snap("binance")], asset="BTC", timestamp_ms=8,
            min_venues=3,
        )
        assert agg is not None
        self.assertTrue(agg.partial)


# ─── detect_liquidity_vacuum ─────────────────────────────────────────────────


class DetectLiquidityVacuumTestCase(unittest.TestCase):
    def test_below_threshold_no_vacuum(self) -> None:
        # current=70% of baseline → drop 30% < 40% → no vacuum.
        vacuum, drop = detect_liquidity_vacuum(700.0, 1000.0, drop_pct=40.0)
        self.assertFalse(vacuum)
        self.assertAlmostEqual(drop or 0.0, 30.0, places=2)

    def test_above_threshold_vacuum(self) -> None:
        # current=50% of baseline → drop 50% >= 40% → vacuum.
        vacuum, drop = detect_liquidity_vacuum(500.0, 1000.0, drop_pct=40.0)
        self.assertTrue(vacuum)
        self.assertAlmostEqual(drop or 0.0, 50.0, places=2)

    def test_no_baseline_returns_false(self) -> None:
        vacuum, drop = detect_liquidity_vacuum(500.0, None)
        self.assertFalse(vacuum)
        self.assertIsNone(drop)

    def test_zero_baseline_returns_false(self) -> None:
        vacuum, drop = detect_liquidity_vacuum(500.0, 0.0)
        self.assertFalse(vacuum)
        self.assertIsNone(drop)

    def test_negative_current_returns_false(self) -> None:
        vacuum, drop = detect_liquidity_vacuum(-100.0, 1000.0)
        self.assertFalse(vacuum)


# ─── classify_signal ─────────────────────────────────────────────────────────


def _make_aggregate(
    *, asymmetry: float = 0.0, total_depth: float = 1000.0,
    spread: float = 5.0, venue_count: int = 3, partial: bool = False,
    venues: tuple[str, ...] = ("binance", "bybit", "okx"),
) -> AggregateMicrostructure:
    return AggregateMicrostructure(
        asset="BTC",
        mid_price_weighted=100.0,
        bid_depth_usd_total=total_depth / 2.0,
        ask_depth_usd_total=total_depth / 2.0,
        asymmetry_weighted=asymmetry,
        quoted_spread_bps_weighted=spread,
        venue_count=venue_count,
        partial=partial,
        timestamp_ms=1,
        venues=venues,
    )


class ClassifySignalTestCase(unittest.TestCase):
    def test_neutral_below_threshold(self) -> None:
        agg = _make_aggregate(asymmetry=0.05)
        sig = classify_signal(agg, baseline_depth_usd=None)
        self.assertEqual(sig.direction_bias, 0)
        self.assertFalse(sig.vacuum)

    def test_positive_bias_when_bid_heavy(self) -> None:
        agg = _make_aggregate(asymmetry=0.3)
        sig = classify_signal(agg, baseline_depth_usd=None)
        self.assertEqual(sig.direction_bias, 1)
        self.assertGreater(sig.severity, 0.0)

    def test_negative_bias_when_ask_heavy(self) -> None:
        agg = _make_aggregate(asymmetry=-0.3)
        sig = classify_signal(agg, baseline_depth_usd=None)
        self.assertEqual(sig.direction_bias, -1)

    def test_nan_asymmetry_means_neutral(self) -> None:
        agg = _make_aggregate(asymmetry=float("nan"))
        sig = classify_signal(agg, baseline_depth_usd=None)
        self.assertEqual(sig.direction_bias, 0)

    def test_vacuum_increases_severity(self) -> None:
        agg = _make_aggregate(asymmetry=0.0, total_depth=400.0)
        sig = classify_signal(agg, baseline_depth_usd=1000.0)  # 60% drop
        self.assertTrue(sig.vacuum)
        self.assertGreater(sig.severity, 0.0)

    def test_severity_clamped_to_one(self) -> None:
        agg = _make_aggregate(asymmetry=0.9, total_depth=1.0)
        sig = classify_signal(agg, baseline_depth_usd=10_000.0)
        self.assertLessEqual(sig.severity, 1.0)

    def test_partial_penalises_severity(self) -> None:
        agg_full = _make_aggregate(asymmetry=0.5, partial=False)
        agg_part = _make_aggregate(asymmetry=0.5, partial=True, venue_count=1, venues=("binance",))
        sig_full = classify_signal(agg_full, baseline_depth_usd=None)
        sig_part = classify_signal(agg_part, baseline_depth_usd=None)
        # partial умножает severity на 0.7 → меньше.
        self.assertLess(sig_part.severity, sig_full.severity)

    def test_signal_dataclass_passthrough(self) -> None:
        agg = _make_aggregate(asymmetry=0.2)
        sig = classify_signal(agg, baseline_depth_usd=None)
        self.assertIsInstance(sig, MicrostructureSignal)
        self.assertIs(sig.aggregate, agg)


# ─── normalize_levels ────────────────────────────────────────────────────────


class NormalizeLevelsTestCase(unittest.TestCase):
    def test_strips_invalid(self) -> None:
        raw = [
            (100.0, 1.0),
            (0.0, 5.0),    # zero price → drop
            (50.0, 0.0),   # zero size → drop
            (-1.0, 1.0),   # negative price → drop
            ("x", 1.0),    # non-numeric → drop
            (100.5, 2.0),  # ok
        ]
        out = normalize_levels(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual([(lvl.price, lvl.size) for lvl in out], [(100.0, 1.0), (100.5, 2.0)])

    def test_handles_strings(self) -> None:
        # API часто отдают строки — должны парситься.
        raw = [("100.0", "1.5"), ("99.5", "2.0")]
        out = normalize_levels(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].price, 100.0)
        self.assertEqual(out[1].size, 2.0)

    def test_empty_input(self) -> None:
        self.assertEqual(normalize_levels([]), ())

    def test_short_rows_dropped(self) -> None:
        # Row length < 2 — должны игнорироваться.
        raw = [(100.0,), (), (100.0, 1.0)]
        out = normalize_levels(raw)
        self.assertEqual(len(out), 1)


# ─── Sanity ──────────────────────────────────────────────────────────────────


class ConstantsTestCase(unittest.TestCase):
    def test_constants_reasonable(self) -> None:
        self.assertGreater(DEFAULT_BAND_PCT, 0)
        self.assertGreater(DEFAULT_VACUUM_DROP_PCT, 0)
        self.assertLess(DEFAULT_VACUUM_DROP_PCT, 100)
        self.assertGreaterEqual(DEFAULT_MIN_VENUES_FOR_AGGREGATE, 1)
        self.assertGreater(ASYMMETRY_NEUTRAL_THRESHOLD, 0)
        self.assertLess(ASYMMETRY_NEUTRAL_THRESHOLD, 1)


if __name__ == "__main__":
    unittest.main()
