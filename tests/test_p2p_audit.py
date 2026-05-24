"""Unit tests for p2p_audit (pure-math module)."""

from __future__ import annotations

import time
import unittest

from p2p_arbitrage import P2PAdvert, P2POpportunity
from p2p_audit import (
    STATUS_AMPLIFIED,
    STATUS_CONFIRMED,
    STATUS_DECAYED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_VANISHED,
    OpportunityAuditRecord,
    compute_realised_spread,
    format_audit_summary,
    make_opportunity_audit_record,
    recommend_threshold_adjustment,
)


def _ad(
    *,
    venue: str = "Binance P2P",
    trade_type: str = "BUY",
    price: float = 100.0,
    asset: str = "USDT",
    fiat: str = "RUB",
) -> P2PAdvert:
    return P2PAdvert(
        venue=venue,
        trade_type=trade_type,
        asset=asset,
        fiat=fiat,
        price=price,
        min_amount_fiat=1000.0,
        max_amount_fiat=50_000.0,
        is_merchant=True,
        completed_orders=200,
        completion_rate_pct=99.0,
    )


def _opp(buy: P2PAdvert, sell: P2PAdvert, *, gross: float, net: float) -> P2POpportunity:
    return P2POpportunity(
        asset=buy.asset,
        fiat=buy.fiat,
        buy_ad=buy,
        sell_ad=sell,
        gross_spread_pct=gross,
        buffer_pct=gross - net,
        net_spread_pct=net,
        executable_fiat=10_000.0,
        executable_asset=100.0,
        shared_payment_methods=("sber",),
        risk_level="LOW",
    )


def _record(
    *,
    key: str = "k",
    asset: str = "USDT",
    fiat: str = "RUB",
    buy: float = 100.0,
    sell: float = 102.0,
    gross: float = 2.0,
    net: float = 1.5,
    risk: str = "LOW",
    shown_ms: int | None = None,
    status: str = STATUS_PENDING,
    realised_at_ms: int | None = None,
    realised_spread: float | None = None,
) -> OpportunityAuditRecord:
    return OpportunityAuditRecord(
        opportunity_key=key,
        asset=asset,
        fiat=fiat,
        venue_buy="Binance P2P",
        venue_sell="Bybit P2P",
        buy_price=buy,
        sell_price=sell,
        gross_spread_pct=gross,
        net_spread_pct=net,
        risk_level=risk,
        shown_at_ms=shown_ms or int(time.time() * 1000),
        realised_at_ms=realised_at_ms,
        realised_spread_pct=realised_spread,
        status=status,
    )


class TestMakeAuditRecord(unittest.TestCase):
    def test_pending_status_by_default(self):
        buy = _ad(trade_type="BUY", price=100.0)
        sell = _ad(venue="Bybit P2P", trade_type="SELL", price=102.0)
        opportunity = _opp(buy, sell, gross=2.0, net=1.5)
        rec = make_opportunity_audit_record(opportunity, opportunity_key="x", shown_at_ms=42)
        self.assertEqual(rec.status, STATUS_PENDING)
        self.assertEqual(rec.shown_at_ms, 42)
        self.assertEqual(rec.asset, "USDT")
        self.assertEqual(rec.fiat, "RUB")
        self.assertEqual(rec.buy_price, 100.0)
        self.assertEqual(rec.sell_price, 102.0)
        self.assertEqual(rec.net_spread_pct, 1.5)
        self.assertEqual(rec.risk_level, "LOW")
        self.assertFalse(rec.is_resolved)


class TestRecordProperties(unittest.TestCase):
    def test_realised_delta_pct_positive(self):
        rec = _record(net=2.0, realised_spread=2.4)
        self.assertAlmostEqual(rec.realised_delta_pct, 20.0)

    def test_realised_delta_pct_negative(self):
        rec = _record(net=2.0, realised_spread=1.5)
        self.assertAlmostEqual(rec.realised_delta_pct, -25.0)

    def test_realised_delta_pct_none_when_missing(self):
        rec = _record(realised_spread=None)
        self.assertIsNone(rec.realised_delta_pct)

    def test_realised_delta_pct_none_when_net_nonpositive(self):
        rec = _record(net=0.0, realised_spread=0.5)
        self.assertIsNone(rec.realised_delta_pct)

    def test_is_resolved_for_terminal_statuses(self):
        for s in (STATUS_CONFIRMED, STATUS_AMPLIFIED, STATUS_DECAYED, STATUS_VANISHED, STATUS_EXPIRED):
            self.assertTrue(_record(status=s).is_resolved, msg=s)
        self.assertFalse(_record(status=STATUS_PENDING).is_resolved)


class TestComputeRealisedSpread(unittest.TestCase):
    def test_vanished_when_no_buy(self):
        rec = _record(buy=100.0, sell=102.0, net=1.5)
        result = compute_realised_spread(
            rec,
            current_buy_ads=[],
            current_sell_ads=[_ad(trade_type="SELL", price=102.0)],
        )
        self.assertEqual(result.status, STATUS_VANISHED)
        self.assertIsNone(result.realised_spread_pct)

    def test_vanished_when_no_sell(self):
        rec = _record()
        result = compute_realised_spread(
            rec,
            current_buy_ads=[_ad(trade_type="BUY", price=100.0)],
            current_sell_ads=[],
        )
        self.assertEqual(result.status, STATUS_VANISHED)

    def test_decayed_when_sell_below_buy(self):
        rec = _record(buy=100.0, sell=102.0, gross=2.0, net=1.5)
        # New ads where buy went UP past the sell → spread collapsed.
        # Need price_tolerance_pct wide enough to match the moved SELL (102 → 100.1
        # is ~2% drift), so use 3%.
        result = compute_realised_spread(
            rec,
            current_buy_ads=[_ad(trade_type="BUY", price=100.3)],
            current_sell_ads=[_ad(trade_type="SELL", price=100.1)],
            price_tolerance_pct=3.0,
        )
        self.assertEqual(result.status, STATUS_DECAYED)
        self.assertEqual(result.realised_spread_pct, 0.0)

    def test_confirmed_when_within_decay_band(self):
        rec = _record(buy=100.0, sell=102.0, gross=2.0, net=1.5)
        # Realised gross 2.0 - buffer 0.5 = 1.5 net, perfectly matches.
        result = compute_realised_spread(
            rec,
            current_buy_ads=[_ad(trade_type="BUY", price=100.0)],
            current_sell_ads=[_ad(trade_type="SELL", price=102.0)],
            price_tolerance_pct=0.5,
            decay_threshold_pct=25.0,
        )
        self.assertEqual(result.status, STATUS_CONFIRMED)
        self.assertAlmostEqual(result.realised_spread_pct or 0.0, 1.5, places=2)

    def test_amplified_when_spread_widened(self):
        rec = _record(buy=100.0, sell=102.0, gross=2.0, net=1.5)
        # New SELL went to 103 → gross 3% → net 2.5% (delta +66%, > 25% band)
        result = compute_realised_spread(
            rec,
            current_buy_ads=[_ad(trade_type="BUY", price=100.0)],
            current_sell_ads=[_ad(trade_type="SELL", price=103.0)],
            price_tolerance_pct=2.0,
            decay_threshold_pct=25.0,
        )
        self.assertEqual(result.status, STATUS_AMPLIFIED)
        self.assertGreater(result.realised_spread_pct or 0.0, rec.net_spread_pct)

    def test_decayed_when_spread_shrank(self):
        rec = _record(buy=100.0, sell=102.0, gross=2.0, net=1.5)
        # New SELL went to 100.5 → gross 0.5% → net 0% (within tol but tiny)
        result = compute_realised_spread(
            rec,
            current_buy_ads=[_ad(trade_type="BUY", price=100.0)],
            current_sell_ads=[_ad(trade_type="SELL", price=100.5)],
            price_tolerance_pct=2.0,
            decay_threshold_pct=25.0,
        )
        self.assertEqual(result.status, STATUS_DECAYED)

    def test_tolerance_excludes_far_prices(self):
        rec = _record(buy=100.0, sell=102.0)
        # Only candidate is 5% off — excluded
        result = compute_realised_spread(
            rec,
            current_buy_ads=[_ad(trade_type="BUY", price=95.0)],
            current_sell_ads=[_ad(trade_type="SELL", price=102.0)],
            price_tolerance_pct=0.5,
        )
        self.assertEqual(result.status, STATUS_VANISHED)

    def test_asset_fiat_must_match(self):
        rec = _record(buy=100.0, sell=102.0, asset="USDT", fiat="RUB")
        result = compute_realised_spread(
            rec,
            current_buy_ads=[_ad(trade_type="BUY", price=100.0, asset="USDT", fiat="USD")],
            current_sell_ads=[_ad(trade_type="SELL", price=102.0, asset="USDT", fiat="USD")],
            price_tolerance_pct=1.0,
        )
        self.assertEqual(result.status, STATUS_VANISHED)


class TestRecommendThresholdAdjustment(unittest.TestCase):
    def test_hold_when_few_samples(self):
        recs = [_record(status=STATUS_CONFIRMED) for _ in range(3)]
        rec = recommend_threshold_adjustment(recs, min_samples=10)
        self.assertEqual(rec.direction, "hold")
        self.assertEqual(rec.sample_size, 3)
        self.assertEqual(rec.delta_pct, 0.0)

    def test_raise_when_majority_decayed(self):
        recs = [_record(status=STATUS_DECAYED) for _ in range(7)]
        recs += [_record(status=STATUS_CONFIRMED) for _ in range(3)]
        rec = recommend_threshold_adjustment(recs, min_samples=5, delta_pct=0.1)
        self.assertEqual(rec.direction, "raise")
        self.assertEqual(rec.delta_pct, 0.1)
        self.assertEqual(rec.sample_size, 10)
        self.assertGreater(rec.decay_rate, 0.5)

    def test_lower_when_majority_amplified(self):
        recs = [_record(status=STATUS_AMPLIFIED) for _ in range(8)]
        recs += [_record(status=STATUS_CONFIRMED) for _ in range(2)]
        rec = recommend_threshold_adjustment(recs, min_samples=5, delta_pct=0.1)
        self.assertEqual(rec.direction, "lower")
        self.assertEqual(rec.delta_pct, 0.1)

    def test_hold_when_balanced(self):
        recs = [_record(status=STATUS_CONFIRMED) for _ in range(8)]
        recs += [_record(status=STATUS_DECAYED) for _ in range(1)]
        recs += [_record(status=STATUS_AMPLIFIED) for _ in range(1)]
        rec = recommend_threshold_adjustment(recs, min_samples=5, delta_pct=0.1)
        self.assertEqual(rec.direction, "hold")

    def test_ignores_pending(self):
        recs = [_record(status=STATUS_PENDING) for _ in range(20)]
        rec = recommend_threshold_adjustment(recs, min_samples=5)
        self.assertEqual(rec.direction, "hold")
        self.assertEqual(rec.sample_size, 0)

    def test_vanished_counts_as_decay(self):
        recs = [_record(status=STATUS_VANISHED) for _ in range(6)]
        recs += [_record(status=STATUS_CONFIRMED) for _ in range(4)]
        rec = recommend_threshold_adjustment(recs, min_samples=5)
        self.assertEqual(rec.direction, "raise")


class TestFormatAuditSummary(unittest.TestCase):
    def test_empty_records(self):
        out = format_audit_summary([])
        self.assertIn("P2P self-audit", out)
        self.assertIn("Нет записей", out)

    def test_counts_appear(self):
        recs = [_record(status=STATUS_CONFIRMED, realised_spread=1.5) for _ in range(3)]
        recs.append(_record(status=STATUS_DECAYED, realised_spread=0.5))
        recs.append(_record(status=STATUS_PENDING))
        out = format_audit_summary(recs)
        self.assertIn("confirmed", out)
        self.assertIn("decayed", out)
        self.assertIn("ожидает", out)
        self.assertIn("4", out)  # 4 resolved

    def test_recommendation_rendered(self):
        from p2p_audit import ThresholdAdjustmentRecommendation

        recs = [_record(status=STATUS_CONFIRMED, realised_spread=1.5) for _ in range(5)]
        rec = ThresholdAdjustmentRecommendation(
            direction="raise",
            delta_pct=0.1,
            reason="много decay",
            sample_size=5,
            decay_rate=0.6,
            amplify_rate=0.0,
        )
        out = format_audit_summary(recs, recommendation=rec)
        self.assertIn("RAISE", out)
        self.assertIn("0.10", out)

    def test_includes_recent_resolved(self):
        recs = [
            _record(status=STATUS_CONFIRMED, realised_spread=1.5, asset="USDT", fiat="RUB"),
            _record(status=STATUS_DECAYED, realised_spread=0.4, asset="USDT", fiat="EUR"),
        ]
        out = format_audit_summary(recs)
        self.assertIn("Последние резолвы", out)
        self.assertIn("USDT/RUB", out)
        self.assertIn("USDT/EUR", out)


if __name__ == "__main__":
    unittest.main()
