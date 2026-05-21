from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")

try:
    import aiogram  # noqa: F401

    HAS_AIOGRAM = True
except Exception:
    HAS_AIOGRAM = False

if HAS_AIOGRAM:
    from refactor.handlers.p2p_arbitrage_handler import _parse_p2p_command


@unittest.skipUnless(HAS_AIOGRAM, "aiogram not installed (unit-fast job)")
class TestP2PCommandParsing(unittest.TestCase):
    def test_defaults(self):
        asset, fiat, pay_types = _parse_p2p_command("/p2p")
        self.assertEqual(asset, "USDT")
        self.assertEqual(fiat, "RUB")
        self.assertEqual(pay_types, ())

    def test_asset_fiat_and_payments(self):
        asset, fiat, pay_types = _parse_p2p_command("/p2p usdt rub TinkoffNew,RosBankNew")
        self.assertEqual(asset, "USDT")
        self.assertEqual(fiat, "RUB")
        self.assertEqual(pay_types, ("TinkoffNew", "RosBankNew"))


if __name__ == "__main__":
    unittest.main()
