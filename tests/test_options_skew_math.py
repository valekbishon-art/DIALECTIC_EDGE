"""Unit-tests для market_indicators.options_skew (pure math)."""

from __future__ import annotations

import math
import unittest
from datetime import datetime

from market_indicators.options_skew import (
    OptionQuote,
    OptionsSkewSignal,
    RR_CALL_SKEW,
    RR_CALL_SKEW_EXTREME,
    RR_PUT_SKEW,
    RR_PUT_SKEW_EXTREME,
    SANITY_IV_MAX,
    SANITY_IV_MIN,
    TARGET_DELTA,
    _bucket_quotes,
    bs_d1,
    build_options_skew,
    call_delta,
    classify_skew_class,
    detect_skew_event,
    estimate_days_to_expiry,
    find_atm_iv,
    find_delta_target_iv,
    format_skew_summary,
    norm_cdf,
    parse_deribit_option_name,
    put_delta,
    risk_reversal_25d,
)


def _expiry_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _q(
    *, strike: float, kind: str, expiry: datetime, iv: float, currency: str = "BTC",
    underlying: float = 100_000.0,
) -> OptionQuote:
    """Хелпер: квота с шаблонным instrument_name."""
    day = f"{expiry.day}{expiry.strftime('%b').upper()}{expiry.strftime('%y')}"
    strike_str = f"{int(strike)}"
    name = f"{currency}-{day}-{strike_str}-{kind}"
    return OptionQuote(
        instrument_name=name,
        currency=currency,
        kind=kind,
        strike=strike,
        expiry_ms=_expiry_ms(expiry),
        mark_iv=iv,
        underlying_price=underlying,
    )


class ParseDeribitOptionNameTestCase(unittest.TestCase):
    def test_call(self):
        out = parse_deribit_option_name("BTC-26DEC25-100000-C")
        self.assertEqual(out["currency"], "BTC")
        self.assertEqual(out["kind"], "C")
        self.assertEqual(out["strike"], 100000.0)
        self.assertEqual(out["expiry"], datetime(2025, 12, 26, 8, 0, 0))

    def test_put_lowercase_normalized(self):
        out = parse_deribit_option_name("eth-7nov25-3500-p")
        self.assertEqual(out["currency"], "ETH")
        self.assertEqual(out["kind"], "P")
        self.assertEqual(out["strike"], 3500.0)

    def test_invalid_month_returns_none(self):
        self.assertIsNone(parse_deribit_option_name("BTC-26ABC25-100000-C"))

    def test_invalid_format_returns_none(self):
        self.assertIsNone(parse_deribit_option_name("BTCUSDT"))
        self.assertIsNone(parse_deribit_option_name(""))
        self.assertIsNone(parse_deribit_option_name(None))  # type: ignore[arg-type]


class EstimateDaysToExpiryTestCase(unittest.TestCase):
    def test_future(self):
        now = datetime(2026, 5, 19, 0, 0, 0)
        exp = datetime(2026, 5, 26, 8, 0, 0)
        # 7d + 8h ≈ 8d (ceil)
        self.assertEqual(estimate_days_to_expiry(expiry_date=exp, now=now), 8)

    def test_same_moment_zero(self):
        now = datetime(2026, 5, 19, 8, 0, 0)
        exp = datetime(2026, 5, 19, 8, 0, 0)
        self.assertEqual(estimate_days_to_expiry(expiry_date=exp, now=now), 0)

    def test_past_zero(self):
        now = datetime(2026, 5, 19, 8, 0, 0)
        exp = datetime(2026, 5, 18, 8, 0, 0)
        self.assertEqual(estimate_days_to_expiry(expiry_date=exp, now=now), 0)


class BlackScholesTestCase(unittest.TestCase):
    def test_norm_cdf_symmetry(self):
        self.assertAlmostEqual(norm_cdf(0.0), 0.5, places=6)
        self.assertAlmostEqual(norm_cdf(1.0) + norm_cdf(-1.0), 1.0, places=6)

    def test_d1_atm(self):
        # ATM (S=K) at r=0: d1 = 0.5 σ √T
        d1 = bs_d1(spot=100.0, strike=100.0, t_years=1.0, iv=0.5)
        self.assertAlmostEqual(d1, 0.25, places=6)

    def test_call_delta_atm_above_half(self):
        # ATM call delta слегка > 0.5 при r=0 (т.к. d1 > 0).
        d = call_delta(spot=100.0, strike=100.0, t_years=30 / 365, iv=0.65)
        self.assertGreater(d, 0.5)
        self.assertLess(d, 0.6)

    def test_put_delta_equals_call_minus_one(self):
        cd = call_delta(spot=100.0, strike=110.0, t_years=30 / 365, iv=0.5)
        pd = put_delta(spot=100.0, strike=110.0, t_years=30 / 365, iv=0.5)
        self.assertAlmostEqual(pd, cd - 1.0, places=6)

    def test_call_delta_deep_otm(self):
        d = call_delta(spot=100.0, strike=200.0, t_years=30 / 365, iv=0.5)
        self.assertLess(d, 0.05)

    def test_d1_invalid_args_raise(self):
        with self.assertRaises(ValueError):
            bs_d1(spot=-1.0, strike=100.0, t_years=1.0, iv=0.5)
        with self.assertRaises(ValueError):
            bs_d1(spot=100.0, strike=100.0, t_years=0.0, iv=0.5)
        with self.assertRaises(ValueError):
            bs_d1(spot=100.0, strike=100.0, t_years=1.0, iv=0.0)


class FindAtmIvTestCase(unittest.TestCase):
    def setUp(self):
        exp = datetime(2026, 6, 19, 8, 0, 0)
        self.quotes = [
            _q(strike=90_000, kind="C", expiry=exp, iv=0.72),
            _q(strike=100_000, kind="C", expiry=exp, iv=0.65),
            _q(strike=110_000, kind="C", expiry=exp, iv=0.66),
            _q(strike=90_000, kind="P", expiry=exp, iv=0.82),
            _q(strike=100_000, kind="P", expiry=exp, iv=0.66),
            _q(strike=110_000, kind="P", expiry=exp, iv=0.72),
        ]

    def test_returns_mean_atm(self):
        iv = find_atm_iv(quotes=self.quotes, spot=100_000.0)
        # Среднее ATM call (0.65) и put (0.66) = 0.655.
        self.assertAlmostEqual(iv, 0.655, places=4)

    def test_only_calls(self):
        only_calls = [q for q in self.quotes if q.kind == "C"]
        iv = find_atm_iv(quotes=only_calls, spot=100_000.0)
        self.assertAlmostEqual(iv, 0.65, places=4)

    def test_empty_quotes(self):
        self.assertIsNone(find_atm_iv(quotes=[], spot=100_000.0))

    def test_skips_iv_outside_sanity(self):
        exp = datetime(2026, 6, 19, 8, 0, 0)
        quotes = [
            _q(strike=100_000, kind="C", expiry=exp, iv=SANITY_IV_MAX + 1.0),
            _q(strike=100_000, kind="P", expiry=exp, iv=0.0),
        ]
        self.assertIsNone(find_atm_iv(quotes=quotes, spot=100_000.0))

    def test_sanity_bounds(self):
        # Граничные значения попадают.
        exp = datetime(2026, 6, 19, 8, 0, 0)
        quotes = [
            _q(strike=100_000, kind="C", expiry=exp, iv=SANITY_IV_MIN),
            _q(strike=100_000, kind="P", expiry=exp, iv=SANITY_IV_MAX),
        ]
        iv = find_atm_iv(quotes=quotes, spot=100_000.0)
        self.assertIsNotNone(iv)


class FindDeltaTargetIvTestCase(unittest.TestCase):
    def setUp(self):
        # ~30 days; "put_skew" book — OTM puts дороже OTM calls
        self.exp = datetime(2026, 6, 19, 8, 0, 0)
        self.t = 30 / 365
        self.quotes = [
            _q(strike=80_000, kind="P", expiry=self.exp, iv=0.95),  # ~25Δ put
            _q(strike=85_000, kind="P", expiry=self.exp, iv=0.90),
            _q(strike=90_000, kind="P", expiry=self.exp, iv=0.82),
            _q(strike=100_000, kind="P", expiry=self.exp, iv=0.66),
            _q(strike=100_000, kind="C", expiry=self.exp, iv=0.65),
            _q(strike=110_000, kind="C", expiry=self.exp, iv=0.66),
            _q(strike=115_000, kind="C", expiry=self.exp, iv=0.69),
            _q(strike=120_000, kind="C", expiry=self.exp, iv=0.75),  # ~25Δ call
        ]

    def test_call_25_delta_selects_otm_call(self):
        iv = find_delta_target_iv(
            quotes=self.quotes, spot=100_000.0, t_years=self.t,
            target_delta=TARGET_DELTA, kind="C",
        )
        self.assertIsNotNone(iv)
        # Должно выбрать OTM call (strike > 100k), не ATM.
        self.assertGreater(iv, 0.65)

    def test_put_neg_25_delta_selects_otm_put(self):
        iv = find_delta_target_iv(
            quotes=self.quotes, spot=100_000.0, t_years=self.t,
            target_delta=-TARGET_DELTA, kind="P",
        )
        self.assertIsNotNone(iv)
        # Должно выбрать OTM put, IV которой выше ATM (put_skew).
        self.assertGreater(iv, 0.70)

    def test_kind_filter(self):
        only_calls = [q for q in self.quotes if q.kind == "C"]
        iv = find_delta_target_iv(
            quotes=only_calls, spot=100_000.0, t_years=self.t,
            target_delta=-TARGET_DELTA, kind="P",
        )
        self.assertIsNone(iv)

    def test_invalid_spot(self):
        self.assertIsNone(find_delta_target_iv(
            quotes=self.quotes, spot=0.0, t_years=self.t,
            target_delta=TARGET_DELTA, kind="C",
        ))
        self.assertIsNone(find_delta_target_iv(
            quotes=self.quotes, spot=100_000.0, t_years=0.0,
            target_delta=TARGET_DELTA, kind="C",
        ))


class RiskReversalTestCase(unittest.TestCase):
    def test_put_skew_negative(self):
        exp = datetime(2026, 6, 19, 8, 0, 0)
        quotes = [
            _q(strike=80_000, kind="P", expiry=exp, iv=0.95),
            _q(strike=100_000, kind="P", expiry=exp, iv=0.66),
            _q(strike=100_000, kind="C", expiry=exp, iv=0.65),
            _q(strike=120_000, kind="C", expiry=exp, iv=0.72),
        ]
        rr = risk_reversal_25d(quotes=quotes, spot=100_000.0, t_years=30 / 365)
        self.assertIsNotNone(rr)
        # put_iv (0.95) > call_iv (0.72) → RR отрицательный.
        self.assertLess(rr, 0.0)

    def test_call_skew_positive(self):
        exp = datetime(2026, 6, 19, 8, 0, 0)
        quotes = [
            _q(strike=80_000, kind="P", expiry=exp, iv=0.55),
            _q(strike=100_000, kind="P", expiry=exp, iv=0.50),
            _q(strike=100_000, kind="C", expiry=exp, iv=0.50),
            _q(strike=120_000, kind="C", expiry=exp, iv=0.95),
        ]
        rr = risk_reversal_25d(quotes=quotes, spot=100_000.0, t_years=30 / 365)
        self.assertIsNotNone(rr)
        self.assertGreater(rr, 0.0)

    def test_returns_none_if_no_pair(self):
        exp = datetime(2026, 6, 19, 8, 0, 0)
        only_calls = [
            _q(strike=120_000, kind="C", expiry=exp, iv=0.7),
        ]
        rr = risk_reversal_25d(quotes=only_calls, spot=100_000.0, t_years=30 / 365)
        self.assertIsNone(rr)


class ClassifySkewClassTestCase(unittest.TestCase):
    def test_neutral(self):
        self.assertEqual(classify_skew_class(0.0), "neutral")
        self.assertEqual(classify_skew_class(0.01), "neutral")
        self.assertEqual(classify_skew_class(-0.01), "neutral")

    def test_put_skew(self):
        self.assertEqual(classify_skew_class(-0.03), "put_skew")
        self.assertEqual(classify_skew_class(RR_PUT_SKEW), "put_skew")

    def test_extreme_put_skew(self):
        self.assertEqual(classify_skew_class(-0.08), "extreme_put_skew")
        self.assertEqual(classify_skew_class(RR_PUT_SKEW_EXTREME), "extreme_put_skew")

    def test_call_skew(self):
        self.assertEqual(classify_skew_class(0.03), "call_skew")
        self.assertEqual(classify_skew_class(RR_CALL_SKEW), "call_skew")

    def test_extreme_call_skew(self):
        self.assertEqual(classify_skew_class(0.08), "extreme_call_skew")
        self.assertEqual(classify_skew_class(RR_CALL_SKEW_EXTREME), "extreme_call_skew")

    def test_unknown_for_none_or_nan(self):
        self.assertEqual(classify_skew_class(None), "unknown")
        self.assertEqual(classify_skew_class(float("nan")), "unknown")


class BucketQuotesTestCase(unittest.TestCase):
    def test_picks_expiry_with_most_quotes(self):
        now = datetime(2026, 5, 19, 8, 0, 0)
        exp_7d = datetime(2026, 5, 26, 8, 0, 0)
        exp_10d = datetime(2026, 5, 29, 8, 0, 0)
        # 7d → 2 quotes, 10d → 4 quotes → выбрать 10d
        quotes = [
            _q(strike=100_000, kind="C", expiry=exp_7d, iv=0.6),
            _q(strike=100_000, kind="P", expiry=exp_7d, iv=0.6),
            _q(strike=95_000, kind="C", expiry=exp_10d, iv=0.6),
            _q(strike=100_000, kind="C", expiry=exp_10d, iv=0.6),
            _q(strike=100_000, kind="P", expiry=exp_10d, iv=0.6),
            _q(strike=105_000, kind="P", expiry=exp_10d, iv=0.6),
        ]
        bucket, day = _bucket_quotes(
            quotes, days_min=3, days_max=14, now=now,
        )
        self.assertEqual(day, 10)
        self.assertEqual(len(bucket), 4)

    def test_empty_when_out_of_range(self):
        now = datetime(2026, 5, 19, 8, 0, 0)
        exp = datetime(2026, 5, 19, 12, 0, 0)
        quotes = [_q(strike=100_000, kind="C", expiry=exp, iv=0.6)]
        bucket, day = _bucket_quotes(
            quotes, days_min=3, days_max=14, now=now,
        )
        self.assertEqual(bucket, [])
        self.assertIsNone(day)


class BuildOptionsSkewTestCase(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 19, 8, 0, 0)
        exp_7d = datetime(2026, 5, 26, 8, 0, 0)
        exp_30d = datetime(2026, 6, 19, 8, 0, 0)
        self.quotes = [
            # near (7d): мягкий put_skew
            _q(strike=90_000, kind="P", expiry=exp_7d, iv=0.80),
            _q(strike=100_000, kind="C", expiry=exp_7d, iv=0.70),
            _q(strike=100_000, kind="P", expiry=exp_7d, iv=0.71),
            _q(strike=110_000, kind="C", expiry=exp_7d, iv=0.75),
            # far (30d): чуть слабее
            _q(strike=80_000, kind="P", expiry=exp_30d, iv=0.90),
            _q(strike=100_000, kind="C", expiry=exp_30d, iv=0.65),
            _q(strike=100_000, kind="P", expiry=exp_30d, iv=0.66),
            _q(strike=120_000, kind="C", expiry=exp_30d, iv=0.72),
        ]

    def test_builds_both_buckets(self):
        sig = build_options_skew(
            currency="BTC", quotes=self.quotes,
            timestamp_ms=_expiry_ms(self.now),
            underlying_price=100_000.0, now=self.now,
        )
        self.assertEqual(sig.currency, "BTC")
        self.assertEqual(sig.near_expiry_days, 7)
        self.assertEqual(sig.far_expiry_days, 31)
        self.assertIsNotNone(sig.near_atm_iv)
        self.assertIsNotNone(sig.far_atm_iv)
        self.assertIsNotNone(sig.near_rr_25d)
        self.assertIsNotNone(sig.far_rr_25d)
        # Near IV выше far IV → term backwardation
        self.assertGreater(sig.near_atm_iv, sig.far_atm_iv)
        self.assertIsNotNone(sig.atm_iv_term_slope)
        self.assertLess(sig.atm_iv_term_slope, 0)

    def test_no_far_quotes_partial_signal(self):
        # Только near (7d) — far пустой
        near_only = [q for q in self.quotes if estimate_days_to_expiry(
            expiry_date=datetime.utcfromtimestamp(q.expiry_ms / 1000.0),
            now=self.now,
        ) <= 14]
        sig = build_options_skew(
            currency="BTC", quotes=near_only,
            timestamp_ms=_expiry_ms(self.now),
            underlying_price=100_000.0, now=self.now,
        )
        self.assertIsNotNone(sig.near_atm_iv)
        self.assertIsNone(sig.far_atm_iv)
        self.assertIsNone(sig.atm_iv_term_slope)

    def test_filters_by_currency(self):
        # Подмешаем ETH quote — он не должен попасть.
        exp_30d = datetime(2026, 6, 19, 8, 0, 0)
        mixed = self.quotes + [
            _q(strike=3500, kind="C", expiry=exp_30d, iv=0.7, currency="ETH"),
        ]
        sig = build_options_skew(
            currency="BTC", quotes=mixed,
            timestamp_ms=_expiry_ms(self.now),
            underlying_price=100_000.0, now=self.now,
        )
        self.assertEqual(sig.currency, "BTC")

    def test_skew_class_picks_far_first(self):
        sig = build_options_skew(
            currency="BTC", quotes=self.quotes,
            timestamp_ms=_expiry_ms(self.now),
            underlying_price=100_000.0, now=self.now,
        )
        # Должно классифицировать по far_rr_25d, а не по near.
        self.assertIn(
            sig.skew_class,
            {"neutral", "put_skew", "extreme_put_skew",
             "call_skew", "extreme_call_skew"},
        )


class DetectSkewEventTestCase(unittest.TestCase):
    def _sig(self, rr: float | None) -> OptionsSkewSignal:
        return OptionsSkewSignal(
            currency="BTC", timestamp_ms=0, underlying_price=100_000.0,
            near_expiry_days=7, near_atm_iv=0.7, near_rr_25d=rr,
            far_expiry_days=30, far_atm_iv=0.65, far_rr_25d=rr,
            atm_iv_term_slope=-0.05,
            skew_class=classify_skew_class(rr),
        )

    def test_put_skew_onset(self):
        prev = self._sig(0.0)
        cur = self._sig(-0.05)
        self.assertEqual(detect_skew_event(current=cur, previous=prev), "put_skew_onset")

    def test_put_skew_recovery(self):
        prev = self._sig(-0.05)
        cur = self._sig(0.0)
        self.assertEqual(detect_skew_event(current=cur, previous=prev), "put_skew_recovery")

    def test_call_skew_onset(self):
        prev = self._sig(0.0)
        cur = self._sig(0.05)
        self.assertEqual(detect_skew_event(current=cur, previous=prev), "call_skew_onset")

    def test_call_skew_recovery(self):
        prev = self._sig(0.05)
        cur = self._sig(0.0)
        self.assertEqual(detect_skew_event(current=cur, previous=prev), "call_skew_recovery")

    def test_no_event_when_both_neutral(self):
        prev = self._sig(0.005)
        cur = self._sig(-0.005)
        self.assertIsNone(detect_skew_event(current=cur, previous=prev))

    def test_returns_none_if_previous_missing(self):
        cur = self._sig(-0.05)
        self.assertIsNone(detect_skew_event(current=cur, previous=None))

    def test_returns_none_if_rr_none(self):
        prev = self._sig(None)
        cur = self._sig(None)
        self.assertIsNone(detect_skew_event(current=cur, previous=prev))


class FormatSkewSummaryTestCase(unittest.TestCase):
    def test_format(self):
        sig = OptionsSkewSignal(
            currency="BTC", timestamp_ms=0, underlying_price=100_000.0,
            near_expiry_days=7, near_atm_iv=0.65, near_rr_25d=-0.03,
            far_expiry_days=30, far_atm_iv=0.60, far_rr_25d=-0.05,
            atm_iv_term_slope=-0.05, skew_class="extreme_put_skew",
        )
        text = format_skew_summary(sig, event="put_skew_onset")
        self.assertIn("BTC", text)
        self.assertIn("65.0%", text)
        self.assertIn("-3.00vp", text)
        self.assertIn("extreme_put_skew", text)
        self.assertIn("event=put_skew_onset", text)

    def test_format_handles_nones(self):
        sig = OptionsSkewSignal(
            currency="ETH", timestamp_ms=0, underlying_price=3500.0,
            near_expiry_days=None, near_atm_iv=None, near_rr_25d=None,
            far_expiry_days=None, far_atm_iv=None, far_rr_25d=None,
            atm_iv_term_slope=None, skew_class="unknown",
        )
        text = format_skew_summary(sig)
        self.assertIn("ETH", text)
        self.assertIn("n/a", text)
        self.assertIn("unknown", text)


if __name__ == "__main__":
    unittest.main()
