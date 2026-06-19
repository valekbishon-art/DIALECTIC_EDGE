"""СКАЧАТЬ РЕАЛЬНЫЕ ЦЕНЫ — запускать У СЕБЯ (где есть интернет).

Что делает: тянет дневные close по всему юниверсу с Yahoo (тем же fetch(),
что и бот) с 2020-04 и кладёт их в prices_cache.json.

Дальше: загрузи prices_cache.json в чат — Notion AI прогонит реальный
бэктест офлайн на этих данных.

Запуск:  python research/download_prices.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from halal_edge import UNIVERSE, fetch  # noqa: E402

PERIOD1 = int(datetime(2020, 4, 1, tzinfo=timezone.utc).timestamp())


def main() -> None:
    print("Качаю реальные дневные цены с Yahoo (с 2020-04)…")
    raw: dict[str, dict[str, float]] = {}
    for sym in UNIVERSE:
        try:
            raw[sym] = fetch(sym, period1=PERIOD1)
            d = sorted(raw[sym].keys())
            print(f"  {sym}: {len(d)} дней  ({d[0]}→{d[-1]})")
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: ошибка {e} — пропускаю")
            raw[sym] = {}

    if not raw.get("BTC"):
        raise SystemExit("Нет данных по BTC — проверь интернет/доступ к Yahoo.")

    days = sorted(raw["BTC"].keys())
    series = {s: [raw[s].get(d) for d in days] for s in UNIVERSE if raw[s]}

    out = ROOT / "prices_cache.json"
    out.write_text(
        json.dumps({"days": days, "series": series}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        f"\n✅ Сохранил {out.name}: {len(days)} дней, {len(series)} монет "
        f"({days[0]}→{days[-1]}).\nТеперь загрузи этот файл ({out}) в чат."
    )


if __name__ == "__main__":
    main()
