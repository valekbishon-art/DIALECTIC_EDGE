"""asset_filter — фильтр допустимости активов для спот-лонг трендовой системы.

Отбирает монеты по ЭКОНОМИКЕ токена (не по цене). Исключает:
  • протоколы на проценте/кредитовании (lending/yield) — доход-механика в основе ценности;
  • токены без утилити, цена = чистая спекуляция;
  • проекты гемблинга/казино и приватные миксеры.
Помечает PoS-сети флагом spot_only (держим спот, доход-стейкинг НЕ используем).

Экспортирует eligible_universe() -> list[str] для трендовой системы.
Вывод полностью нейтральный (экономические термины). Чистый stdlib.

    py asset_filter.py                 # таблица
    py asset_filter.py --only ELIGIBLE
    py asset_filter.py --universe      # список eligible_universe()
    py asset_filter.py --check LINK
"""
from __future__ import annotations
import argparse

# нейтральные ярлыки-причины (экономика токена)
R_PAY    = "utility: платёж/расчёт"
R_INFRA  = "utility: газ/инфраструктура сети"
R_ORACLE = "utility: оракул/данные-сервис"
R_STORE  = "utility: оплата хранения/вычислений"
R_STAKE  = "utility-сеть (spot-only: доход-стейкинг не используем)"
R_LEND   = "исключено: протокол кредитования/процента/yield"
R_MEME   = "исключено: нет утилити, цена = спекуляция"
R_GAMB   = "исключено: гемблинг/казино"
R_PRIV   = "исключено: приватный миксер"
R_EXCH   = "исключено: биржевой токен (ценность от деривативно-маржинальной площадки)"

# (TICKER, CATEGORY, reason, spot_only_flag)
#   CATEGORY in {"ELIGIBLE", "EXCLUDED"};  spot_only=True → PoS-сеть, доход-стейкинг не берём.
_ASSETS = [
    # платёжные / расчётные
    ("BTC", "ELIGIBLE", R_PAY,   False),
    ("LTC", "ELIGIBLE", R_PAY,   False),
    ("BCH", "ELIGIBLE", R_PAY,   False),
    ("XRP", "ELIGIBLE", R_PAY,   False),
    ("XLM", "ELIGIBLE", R_PAY,   False),
    # инфраструктура / газ (PoW или non-stake)
    ("ETC", "ELIGIBLE", R_INFRA, False),
    ("LINK","ELIGIBLE", R_ORACLE,False),
    ("FIL", "ELIGIBLE", R_STORE, False),
    ("VET", "ELIGIBLE", R_INFRA, False),
    ("ARB", "ELIGIBLE", R_INFRA, False),
    ("OP",  "ELIGIBLE", R_INFRA, False),
    ("RENDER","ELIGIBLE",R_STORE,False),
    # инфраструктура с PoS (spot-only)
    ("ETH", "ELIGIBLE", R_STAKE, True),
    ("SOL", "ELIGIBLE", R_STAKE, True),
    ("ADA", "ELIGIBLE", R_STAKE, True),
    ("AVAX","ELIGIBLE", R_STAKE, True),
    ("ATOM","ELIGIBLE", R_STAKE, True),
    ("DOT", "ELIGIBLE", R_STAKE, True),
    ("NEAR","ELIGIBLE", R_STAKE, True),
    ("ALGO","ELIGIBLE", R_STAKE, True),
    ("HBAR","ELIGIBLE", R_STAKE, True),
    ("TIA", "ELIGIBLE", R_STAKE, True),
    ("SUI", "ELIGIBLE", R_STAKE, True),
    ("APT", "ELIGIBLE", R_STAKE, True),
    ("SEI", "ELIGIBLE", R_STAKE, True),
    ("TRX", "ELIGIBLE", R_INFRA, True),
    ("ICP", "ELIGIBLE", R_INFRA, True),
    ("INJ", "ELIGIBLE", R_STAKE, True),
    ("FET", "ELIGIBLE", R_INFRA, True),
    # исключено: биржевой токен
    ("BNB", "EXCLUDED", R_EXCH, False),
    # исключено: протоколы кредитования/процента/yield
    ("AAVE","EXCLUDED", R_LEND, False),
    ("COMP","EXCLUDED", R_LEND, False),
    ("MKR", "EXCLUDED", R_LEND, False),
    ("CRV", "EXCLUDED", R_LEND, False),
    ("LDO", "EXCLUDED", R_LEND, False),
    ("CAKE","EXCLUDED", R_LEND, False),
    ("UNI", "EXCLUDED", R_LEND, False),
    # исключено: нет утилити, спекуляция
    ("DOGE","EXCLUDED", R_MEME, False),
    ("SHIB","EXCLUDED", R_MEME, False),
    ("PEPE","EXCLUDED", R_MEME, False),
    # исключено: гемблинг / миксеры
    ("FUN", "EXCLUDED", R_GAMB, False),
    ("ROLL","EXCLUDED", R_GAMB, False),
    ("XMR", "EXCLUDED", R_PRIV, False),
    ("TORN","EXCLUDED", R_PRIV, False),
]

CATEGORIES = ("ELIGIBLE", "EXCLUDED")


def all_assets():
    return [{"ticker": t, "category": c, "reason": r, "spot_only": s}
            for (t, c, r, s) in _ASSETS]


def classify(ticker):
    t = ticker.upper()
    for rec in all_assets():
        if rec["ticker"] == t:
            return rec
    return None


def eligible_universe(spot_only_ok=True):
    """list[str] — допустимый спот-юниверс. spot_only_ok=False → строго:
    только non-stake токены (платёжные/PoW/сервисные)."""
    out = []
    for rec in all_assets():
        if rec["category"] != "ELIGIBLE":
            continue
        if not spot_only_ok and rec["spot_only"]:
            continue
        out.append(rec["ticker"])
    return out


def _print_table(only=None):
    print("=" * 70)
    print(" ФИЛЬТР ДОПУСТИМОСТИ АКТИВОВ (по экономике токена)")
    print("=" * 70)
    icon = {"ELIGIBLE": "✓", "EXCLUDED": "✗"}
    for cat in ((only,) if only else CATEGORIES):
        recs = [r for r in all_assets() if r["category"] == cat]
        if not recs:
            continue
        print(f"\n{icon.get(cat,'')} {cat}  ({len(recs)})")
        print("-" * 70)
        for r in recs:
            star = " *" if r["spot_only"] else "  "
            print(f"  {r['ticker']:7s}{star} {r['reason']}")
    print("\n" + "-" * 70)
    print(" * = PoS-сеть: держим спот, доход-стейкинг не используем.")
    uni, strict = eligible_universe(), eligible_universe(spot_only_ok=False)
    print(f" eligible_universe()             -> {len(uni)}: {', '.join(uni)}")
    print(f" eligible_universe(strict)       -> {len(strict)}: {', '.join(strict)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Фильтр допустимости активов (экономика токена).")
    ap.add_argument("--only", choices=CATEGORIES)
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--strict", action="store_true", help="с --universe: исключить PoS-активы")
    ap.add_argument("--check", metavar="TICKER")
    a = ap.parse_args(argv)
    if a.check:
        rec = classify(a.check)
        if rec is None:
            print(f"{a.check.upper()}: нет в справочнике — по умолчанию EXCLUDED (осторожно).")
            return 1
        print(f"{rec['ticker']}: {rec['category']} — {rec['reason']}")
        return 0
    if a.universe:
        for t in eligible_universe(spot_only_ok=not a.strict):
            print(t)
        return 0
    _print_table(only=a.only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
