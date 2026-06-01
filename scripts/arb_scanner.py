"""scripts/arb_scanner.py — КРОСС-БИРЖЕВОЙ funding-арб сканер (CLI).

Лонг перп где фандинг низкий + шорт где высокий → собираешь спред, цена нейтральна.
То, что одна биржа в своём UI не покажет. 4 биржи: Binance/Bybit/Gate/Hyperliquid.

Запуск:
    python scripts/arb_scanner.py                  # ликвидные, спред >=12%
    python scripts/arb_scanner.py --min 20         # свой порог спреда
    python scripts/arb_scanner.py --capital 500    # + размер позиции
    python scripts/arb_scanner.py --all            # все активы (вкл мусор — на свой риск)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.cross_exchange import fetch_all, find_spreads, format_arb_md  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=12.0, help="мин спред %% годовых")
    ap.add_argument("--capital", type=float, default=0.0)
    ap.add_argument("--all", action="store_true", help="все активы (вкл неликвид)")
    args = ap.parse_args()

    by_asset = fetch_all()
    print(f"Активов с фандингом на >=2 биржах: "
          f"{sum(1 for v in by_asset.values() if len(v) >= 2)}")
    universe = set() if args.all else None
    opps = find_spreads(by_asset, min_spread=args.min, universe=universe)
    print(format_arb_md(opps, capital=args.capital)
          .replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))


if __name__ == "__main__":
    main()
