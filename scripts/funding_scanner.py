"""scripts/funding_scanner.py — СКАНЕР FUNDING CARRY.

Единственный edge, переживший всё расследование (7+ тестов) — дельта-нейтральный
funding carry: ЛОНГ спот + ШОРТ перп, собираешь funding. Бэктест 2020-2026:
при годовом фандинге >=20-30% даёт +30%+ годовых, переживает косты, робастен
каждый торгующий год. Это НЕ прогноз цены (там edge нет) — это сбор премии.

Бот — не предсказатель, а СКАНЕР: тянет live funding по активам, считает
годовую ставку, и говорит ГДЕ сейчас открыта carry-возможность. Решение и
исполнение — руками (как в исходной цели проекта).

Запуск:
    python scripts/funding_scanner.py                 # 15 ликвидных активов
    python scripts/funding_scanner.py --all            # все USDT-перпы Binance
    python scripts/funding_scanner.py --threshold 30   # свой порог (% годовых)
    python scripts/funding_scanner.py --watch 300      # обновлять каждые 300 сек
    python scripts/funding_scanner.py --capital 5000   # расчёт размера позиции под $5000
    python scripts/funding_scanner.py --watch 600 --telegram   # пинг в Telegram при возможности

Источник: Binance fapi premiumIndex (live, без ключей). Геоблок? — VPN/прокси.
Telegram: читает BOT_TOKEN + CARRY_ALERT_CHAT_IDS (или ALERT_ENGINE_CHAT_IDS) из env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

# Общая carry-логика живёт в core/carry_signal — единый источник правды,
# который переиспользуют и Лучшая сделка, и Анализ в боте.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.carry_signal import (  # noqa: E402
    DEFAULT_ASSETS, PRIME, STRONG, THIN,
    annualized, fetch_basis, fetch_funding, log_opportunities, scan_carry,
)


def print_basis_section() -> None:
    """Секция calendar basis carry (квартальные COIN-M, чище funding carry)."""
    try:
        opps = fetch_basis()
    except Exception as e:  # noqa: BLE001
        print(f"  [basis] недоступно: {e}")
        return
    if not opps:
        return
    print("\n  CALENDAR BASIS (cash-and-carry, квартальные — базис в плюсе ~100% времени):")
    print("  " + "─" * 62)
    for o in opps:
        tagb = ("🟢 carry" if o.annual_pct >= 5 else "⚪ тонко" if o.annual_pct >= 2 else "· спим")
        print(f"  {o.symbol:<16}{o.annual_pct:>7.1f}% годовых   {tagb}")
    best = opps[0]
    if best.annual_pct >= 5:
        print(f"\n  💎 Базис-сделка: ЛОНГ спот {best.asset} + ШОРТ {best.symbol} → "
              f"~{best.annual_pct:.0f}% годовых (locked-in до экспирации)")


def position_plan(ann: float, capital: float) -> list[str]:
    """Строки с раскладкой размера дельта-нейтральной позиции под `capital` (USD).

    Дельта-нейтраль = равный объём: спот-нога = перп-нога = N. При перпе 1x
    маржа под шорт ≈ N, значит весь капитал C делится C = N(спот) + N(маржа) → N = C/2.
    Доход (гросс) = собранный фандинг на шорт-ноге = ann% от N.
    """
    n = capital / 2.0
    annual = n * ann / 100.0
    return [
        f"     РАЗМЕР под ${capital:,.0f}:  ЛОНГ спот ${n:,.0f}  +  ШОРТ перп ${n:,.0f} (1x, равный объём)",
        f"     доход (гросс): ~${annual/365:,.2f}/день · ~${annual/12:,.0f}/мес · ~${annual:,.0f}/год  (минус косты/спред)",
    ]


def send_telegram(text: str) -> bool:
    """Шлёт сообщение в Telegram. Токен/чаты из env. True если ушло хоть кому-то."""
    token = os.getenv("BOT_TOKEN", "").strip()
    raw = (os.getenv("CARRY_ALERT_CHAT_IDS", "")
           or os.getenv("ALERT_ENGINE_CHAT_IDS", "")
           or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or not raw:
        print("  [tg] нет BOT_TOKEN или CARRY_ALERT_CHAT_IDS в env — алерт пропущен.")
        return False
    ok = False
    for chunk in raw.split(","):
        cid = chunk.strip()
        if not cid:
            continue
        try:
            data = urllib.parse.urlencode({
                "chat_id": cid, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            urllib.request.urlopen(req, timeout=15).read()
            ok = True
        except Exception as e:  # noqa: BLE001
            print(f"  [tg] не доставлено в {cid}: {str(e)[:60]}")
    return ok


def tag(ann: float) -> str:
    if ann >= PRIME:
        return "🟢🟢 PRIME"
    if ann >= STRONG:
        return "🟢 CARRY"
    if ann <= -STRONG:
        return "🔵 REVERSE"   # отрицательный экстрим — обратный carry (шорт-спот+лонг-перп)
    if ann >= THIN:
        return "⚪ тонко"
    if ann < 0:
        return "🔴 отриц"
    return "·  спим"


def scan(assets: list[str] | None, threshold: float, capital: float = 0.0) -> list[tuple]:
    data = fetch_funding()
    if not data:
        print("Нет данных (геоблок fapi? нужен VPN).")
        return []
    if assets is None:
        rows = list(data.items())              # все USDT-перпы
    else:
        rows = [(f"{a}USDT", data[f"{a}USDT"]) for a in assets if f"{a}USDT" in data]

    scored = []
    for sym, d in rows:
        ann = annualized(d["rate"], d["interval_h"])
        scored.append((sym, d["rate"], d["interval_h"], ann))
    scored.sort(key=lambda x: x[3], reverse=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n  FUNDING CARRY SCANNER · {now} · порог {threshold:.0f}% годовых")
    print("  " + "─" * 62)
    print(f"  {'СИМВОЛ':<12}{'ставка/8ч':>11}{'годовых':>11}   статус")
    print("  " + "─" * 62)
    opps = []
    for sym, rate, ih, ann in scored:
        t = tag(ann)
        mark = "  "
        if ann >= threshold or ann <= -threshold:
            mark = "▶ "
            opps.append((sym, rate, ih, ann))
        # ставку нормируем к 8ч-эквиваленту для читаемости
        rate8 = rate * (8.0 / ih)
        print(f"{mark}{sym:<12}{rate8*100:>10.4f}%{ann:>10.1f}%   {t}")
    print("  " + "─" * 62)

    if not opps:
        print(f"\n  💤 Carry-возможностей >= {threshold:.0f}% годовых нет.")
        print("     Это норма в спокойном рынке. Стратегия СПИТ — ждём эйфории")
        print("     (фандинг разгоняется в манию, когда толпа лезет в лонг).")
        return []

    print(f"\n  ⚡ {len(opps)} ВОЗМОЖНОСТЬ(ЕЙ) — разбор:\n")
    for sym, rate, ih, ann in opps:
        asset = sym[:-4]
        if ann > 0:
            print(f"  🟢 {sym}: фандинг +{ann:.1f}% годовых (лонги платят шортам)")
            print(f"     ИГРА (дельта-нейтрал): ЛОНГ спот {asset} + ШОРТ перп {sym}, равный объём")
            print(f"     → собираешь ~{ann:.0f}% годовых, цена захеджирована (ноги гасятся)")
        else:
            print(f"  🔵 {sym}: фандинг {ann:.1f}% годовых (шорты платят лонгам)")
            print(f"     ОБРАТНАЯ ИГРА: ШОРТ спот {asset} + ЛОНГ перп {sym} (нужен borrow спота)")
            print(f"     → собираешь ~{abs(ann):.0f}% годовых; сложнее в исполнении")
        if capital > 0:
            for line in position_plan(abs(ann), capital):
                print(line)
        print(f"     ВЫХОД: когда годовой фандинг нормализуется (< {THIN:.0f}%) или перевернётся.")
        print()
    print("  ⚠️  Перед входом проверь руками: ликвидность спота И перпа, маржу под")
    print("      шорт-ногу, риск ликвидации при резком движении, спред/косты двух ног.")
    print("      Бэктест = верхняя оценка; реал съедает часть. Edge есть, грааля нет.")
    return opps


def main():
    ap = argparse.ArgumentParser(description="Сканер funding carry возможностей")
    ap.add_argument("--all", action="store_true", help="все USDT-перпы, не только ликвидный юниверс")
    ap.add_argument("--threshold", type=float, default=STRONG, help="порог годовых %% для алерта (деф 20)")
    ap.add_argument("--watch", type=int, default=0, help="обновлять каждые N секунд (0 = один раз)")
    ap.add_argument("--capital", type=float, default=0.0, help="депозит USD — посчитать размер позиции")
    ap.add_argument("--telegram", action="store_true", help="слать алерт в Telegram при новой возможности")
    args = ap.parse_args()
    assets = None if args.all else DEFAULT_ASSETS

    from core.carry_signal import CarryOpportunity, log_opportunities

    alerted: set[str] = set()  # дедуп: символы, по которым уже слали алерт
    while True:
        try:
            opps = scan(assets, args.threshold, args.capital)
            print_basis_section()
            if opps:
                # лог в carry_opportunities.csv — для сверки edge с реальностью
                log_opportunities([
                    CarryOpportunity(symbol=s, asset=s[:-4], rate=r, interval_h=ih,
                                     annual_pct=ann, positive=ann > 0)
                    for s, r, ih, ann in opps
                ])
            if args.telegram and opps:
                fresh = [o for o in opps if o[0] not in alerted]
                if fresh:
                    lines = ["⚡ <b>FUNDING CARRY</b> — возможность!"]
                    for sym, rate, ih, ann in fresh:
                        asset = sym[:-4]
                        side = (f"ЛОНГ спот {asset} + ШОРТ перп {sym}" if ann > 0
                                else f"ШОРТ спот {asset} + ЛОНГ перп {sym}")
                        lines.append(f"\n<b>{sym}</b>: {ann:+.0f}% годовых\n{side} (равный объём)")
                        if args.capital > 0:
                            n = args.capital / 2.0
                            lines.append(f"под ${args.capital:,.0f}: по ${n:,.0f} на ногу · ~${n*abs(ann)/100/12:,.0f}/мес гросс")
                    lines.append("\nВыход когда фандинг нормализуется. Проверь ликвидность/маржу руками.")
                    if send_telegram("\n".join(lines)):
                        print(f"\n  [tg] ✅ алерт отправлен ({len(fresh)} новых)")
                        alerted.update(o[0] for o in fresh)
                # сбрасываем дедуп по символам, которые ушли из возможностей (вернутся → снова алерт)
                alerted &= {o[0] for o in opps}
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка: {e}")
        if args.watch <= 0:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
