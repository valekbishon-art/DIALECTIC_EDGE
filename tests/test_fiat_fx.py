"""Tests для market_indicators.fiat_fx — spot FX anchor для P2P outlier-фильтра."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from market_indicators import fiat_fx


class TestFiatFXBasic(unittest.TestCase):
    def setUp(self) -> None:
        fiat_fx.reset_cache()
        fiat_fx.set_test_mode(True)  # disable network в тестах

    def tearDown(self) -> None:
        fiat_fx.set_test_mode(False)
        fiat_fx.reset_cache()

    def test_usd_returns_one(self):
        self.assertEqual(fiat_fx.get_usd_fiat_rate("USD"), 1.0)

    def test_empty_returns_none(self):
        self.assertIsNone(fiat_fx.get_usd_fiat_rate(""))

    def test_unknown_fiat_returns_none(self):
        # Незнакомая валюта без peg fallback + remote отключён → None.
        self.assertIsNone(fiat_fx.get_usd_fiat_rate("XXX"))

    def test_peg_fallback_when_remote_disabled(self):
        # SAR peg = 3.75 (hardcoded fallback) — должен сработать без HTTP.
        self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("SAR"), 3.75)
        self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("AED"), 3.6725)
        self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("HKD"), 7.80, places=2)

    def test_lowercase_works(self):
        self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("sar"), 3.75)

    def test_cache_hit_within_ttl(self):
        # 1-й вызов кэширует, 2-й читает из кэша.
        rate1 = fiat_fx.get_usd_fiat_rate("SAR")
        rate2 = fiat_fx.get_usd_fiat_rate("SAR")
        self.assertEqual(rate1, rate2)


class TestRemoteFetch(unittest.TestCase):
    """Проверка handling'а HTTP ответов через мок urlopen."""

    def setUp(self) -> None:
        fiat_fx.reset_cache()
        fiat_fx.set_test_mode(False)  # включаем remote
        os.environ["P2P_FX_DISABLE_REMOTE"] = "0"

    def tearDown(self) -> None:
        fiat_fx.set_test_mode(False)
        fiat_fx.reset_cache()
        os.environ.pop("P2P_FX_DISABLE_REMOTE", None)

    def test_remote_disabled_via_env(self):
        os.environ["P2P_FX_DISABLE_REMOTE"] = "1"
        # SAR должен вернуть peg fallback, ILS (нет peg) — None.
        self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("SAR"), 3.75)
        self.assertIsNone(fiat_fx.get_usd_fiat_rate("ILS"))

    def test_remote_success_caches_all_rates(self):
        # Мок urlopen возвращает валидный JSON с парой курсов.
        import io
        import json

        fake_payload = {
            "result": "success",
            "rates": {"ILS": 2.9, "BYN": 3.27, "VES": 530.5},
        }
        fake_response = io.BytesIO(json.dumps(fake_payload).encode())
        fake_response.__enter__ = lambda self_: self_  # type: ignore[attr-defined]
        fake_response.__exit__ = lambda *_: None  # type: ignore[attr-defined]

        with patch("market_indicators.fiat_fx.urllib.request.urlopen", return_value=fake_response):
            self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("ILS"), 2.9)
        # Сразу читаем второй курс — он должен быть в кэше (нет нового fetch).
        self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("BYN"), 3.27)

    def test_remote_network_error_falls_back_to_peg(self):
        from urllib.error import URLError

        with patch(
            "market_indicators.fiat_fx.urllib.request.urlopen",
            side_effect=URLError("DNS fail"),
        ):
            # SAR имеет peg fallback → 3.75 даже при network fail.
            self.assertAlmostEqual(fiat_fx.get_usd_fiat_rate("SAR"), 3.75)
            # ILS — peg нет → None.
            self.assertIsNone(fiat_fx.get_usd_fiat_rate("ILS"))

    def test_remote_bad_payload_returns_none(self):
        import io

        bad_response = io.BytesIO(b"not json")
        bad_response.__enter__ = lambda self_: self_  # type: ignore[attr-defined]
        bad_response.__exit__ = lambda *_: None  # type: ignore[attr-defined]

        with patch("market_indicators.fiat_fx.urllib.request.urlopen", return_value=bad_response):
            self.assertIsNone(fiat_fx.get_usd_fiat_rate("ILS"))


class TestStableDetection(unittest.TestCase):
    def test_usdt_usdc_are_stables(self):
        self.assertTrue(fiat_fx.is_usd_pegged_stable("USDT"))
        self.assertTrue(fiat_fx.is_usd_pegged_stable("USDC"))
        self.assertTrue(fiat_fx.is_usd_pegged_stable("DAI"))
        self.assertTrue(fiat_fx.is_usd_pegged_stable("usdc"))

    def test_btc_eth_are_not_stables(self):
        self.assertFalse(fiat_fx.is_usd_pegged_stable("BTC"))
        self.assertFalse(fiat_fx.is_usd_pegged_stable("ETH"))
        self.assertFalse(fiat_fx.is_usd_pegged_stable(""))


class TestMarketAnchorForPair(unittest.TestCase):
    def setUp(self) -> None:
        fiat_fx.reset_cache()
        fiat_fx.set_test_mode(True)

    def tearDown(self) -> None:
        fiat_fx.set_test_mode(False)
        fiat_fx.reset_cache()

    def test_stable_with_peg_fiat_returns_peg(self):
        # USDC/SAR → SAR peg 3.75 (USDC ≈ 1 USD).
        self.assertAlmostEqual(fiat_fx.market_anchor_for_pair("USDC", "SAR"), 3.75)

    def test_stable_with_unknown_fiat_returns_none(self):
        # USDC/XXX → fiat нет ни в кэше ни в peg → None.
        self.assertIsNone(fiat_fx.market_anchor_for_pair("USDC", "XXX"))

    def test_non_stable_returns_none(self):
        # BTC/SAR — BTC не stable, spot lookup пропускается → None
        # (caller должен fallback'нуть на median).
        self.assertIsNone(fiat_fx.market_anchor_for_pair("BTC", "SAR"))


if __name__ == "__main__":
    unittest.main()
