"""scripts/research_basis_carry.py — calendar basis carry (cash-and-carry).

Дельта-нейтраль: лонг спот + шорт квартальный фьюч, держим ДО экспирации.
Ключ: PnL = F_entry - S_entry = базис на входе, ЗАПЕРТ (схождение F→S на экспирации
гарантировано, путь цены не важен). Значит годовой базис на входе = реализованный
carry. Нужен лишь ряд базиса.

Источник: Binance Vision COIN-M квартальные контракты (BTCUSD_YYMMDD) vs спот.
Считаем фронт-квартальный годовой базис по дням, смотрим: жирен ли, переживает
ли косты, и — главное — ЖИВ ли в 2025-2026 (где всё остальное умерло).
"""
from __future__ import annotations

import io
import os
import sys
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.backtest_engine import DATA_DIR, load_candles  # noqa: E402

CM = "https://data.binance.vision/data/futures/cm/monthly/klines"
BULL = os.path.join(os.path.dirname(DATA_DIR), "backtest_bull")

# Квартальные экспирации (последняя пятница Mar/Jun/Sep/Dec), YYMMDD.
QUARTERLIES = [
    ("BTCUSD_230331", date(2023, 3, 31)), ("BTCUSD_230630", date(2023, 6, 30)),
    ("BTCUSD_230929", date(2023, 9, 29)), ("BTCUSD_231229", date(2023, 12, 29)),
    ("BTCUSD_240329", date(2024, 3, 29)), ("BTCUSD_240628", date(2024, 6, 28)),
    ("BTCUSD_240927", date(2024, 9, 27)), ("BTCUSD_241227", date(2024, 12, 27)),
    ("BTCUSD_250328", date(2025, 3, 28)), ("BTCUSD_250627", date(2025, 6, 27)),
    ("BTCUSD_250926", date(2025, 9, 26)), ("BTCUSD_251226", date(2025, 12, 26)),
    ("BTCUSD_260327", date(2026, 3, 27)),
]


def _months_before(exp: date, k: int = 4):
    """k месяцев, заканчивая месяцем экспирации (окно фронт-квартала)."""
    out = []
    y, m = exp.year, exp.month
    for _ in range(k):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    return out[::-1]


def fetch_contract_day_closes(contract: str, exp: date) -> dict:
    """{date_str: close} квартального контракта (фронт-окно ~4 мес до экспирации)."""
    out = {}
    for ym in _months_before(exp, 4):
        url = f"{CM}/{contract}/1d/{contract}-1d-{ym}.zip"
        for attempt in range(3):  # ретрай против троттлинга Vision
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=30).read()
                z = zipfile.ZipFile(io.BytesIO(raw))
                for ln in z.read(z.namelist()[0]).decode("utf-8", "ignore").splitlines():
                    p = ln.split(",")
                    if not p or not p[0].isdigit():
                        continue
                    d = datetime.fromtimestamp(int(p[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    try:
                        out[d] = float(p[4])  # close
                    except ValueError:
                        pass
                break  # успех
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break  # контракт не торговался в этот месяц — не ретраим
            except Exception:  # noqa: BLE001 — таймаут/троттл — ретраим
                import time
                time.sleep(0.5 * (attempt + 1))
    return out


def spot_closes() -> dict:
    out = {}
    for d in (BULL, DATA_DIR):
        try:
            for c in load_candles("BTC", data_dir=d):
                out[c.timestamp.strftime("%Y-%m-%d")] = c.close
        except Exception:  # noqa: BLE001
            pass
    return out


def main():
    spot = spot_closes()
    print(f"Спот BTC: {len(spot)} дней")
    # тянем контракты параллельно
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda ce: (ce[1], fetch_contract_day_closes(ce[0], ce[1])),
                              QUARTERLIES))
    # для каждого календарного дня — фронт-квартал (ближайшая экспирация с >=10 дн)
    # basis_ann = (F/S - 1) * 365 / days_to_exp
    by_day = {}  # date_str -> (annual_basis_pct, days_to_exp)
    for exp, closes in results:
        for d, f in closes.items():
            s = spot.get(d)
            if not s or s <= 0:
                continue
            dd = (exp - date.fromisoformat(d)).days
            if dd < 10 or dd > 100:        # фронт-квартал, без экспирационного шума
                continue
            ann = (f / s - 1.0) * 365.0 / dd * 100.0
            # если на день есть несколько контрактов — берём ближайший (фронт)
            prev = by_day.get(d)
            if prev is None or dd < prev[1]:
                by_day[d] = (ann, dd)

    if not by_day:
        print("Нет данных базиса.")
        return
    days = sorted(by_day)
    anns = [by_day[d][0] for d in days]
    print(f"Дней с фронт-базисом: {len(days)} ({days[0]}..{days[-1]})")
    print(f"Средний годовой базис: {sum(anns)/len(anns):+.2f}%  "
          f"(min {min(anns):+.1f} / max {max(anns):+.1f})")

    print("\n=== средний годовой базис по годам ===")
    for yr in ("2023", "2024", "2025", "2026"):
        seg = [by_day[d][0] for d in days if d[:4] == yr]
        if seg:
            pos = sum(1 for x in seg if x > 0)
            print(f"  {yr}: n={len(seg):3d}  ср.базис {sum(seg)/len(seg):+5.1f}%год  "
                  f"| дней>0: {100*pos//len(seg)}%  | дней>=5%год: {sum(1 for x in seg if x>=5)*100//len(seg)}%  "
                  f"| >=10%: {sum(1 for x in seg if x>=10)*100//len(seg)}%")

    # carry-стратегия: входим когда базис>=порог, ловим его (минус косты 4 ноги)
    print("\n=== carry: вход при годовом базисе>=порог (косты 0.10%/нога=0.4% round-trip аннуализир.) ===")
    for thr in (3, 5, 8, 12):
        elig = [(d, by_day[d][0], by_day[d][1]) for d in days if by_day[d][0] >= thr]
        if not elig:
            print(f"  порог {thr:2d}%: нет дней"); continue
        # реализованный нетто-годовой = базис минус аннуализированные косты round-trip
        nets = [ann - 0.4 * 365.0 / dd for (_, ann, dd) in elig]
        recent = [n for (d, _, _), n in zip(elig, nets) if d[:4] in ("2025", "2026")]
        print(f"  порог {thr:2d}%: дней-входа {len(elig):3d} | нетто годовых ~{sum(nets)/len(nets):+5.1f}% "
              f"| 2025-26 дней-входа: {len(recent)} (нетто {sum(recent)/len(recent):+.1f}% если есть)"
              if recent else
              f"  порог {thr:2d}%: дней-входа {len(elig):3d} | нетто годовых ~{sum(nets)/len(nets):+5.1f}% "
              f"| 2025-26: 0 дней-входа")


if __name__ == "__main__":
    main()
