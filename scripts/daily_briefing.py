"""scripts/daily_briefing.py — ЕЖЕДНЕВНЫЙ БРИФИНГ в Telegram, по шагам для новичка.

Шлёт пользователю ОДНО понятное сообщение: режим рынка + что делать сегодня
(carry-сделка с ТОЧНЫМИ шагами под депозит) + новые листинги (с предупреждением)
+ сигналы «ЗАКРЫВАЙ». Расчёт на то, что юзер ничего сам не считает и не читает
доки — бот ведёт за руку: «купи тут, плечо такое, продай там».

Запуск:
    python scripts/daily_briefing.py --capital 5000               # печать (тест)
    python scripts/daily_briefing.py --capital 5000 --send        # разово в Telegram
    python scripts/daily_briefing.py --capital 5000 --send --watch 21600   # каждые 6ч

Telegram: BOT_TOKEN + CARRY_ALERT_CHAT_IDS (или ALERT_ENGINE_CHAT_IDS) в env.
Состояние открытых carry хранится в carry_state.json (для сигналов ЗАКРЫВАЙ).
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.carry_briefing import build_briefing, close_alerts  # noqa: E402

STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "carry_state.json")


# ───────────────────────── Telegram ─────────────────────────
def send_telegram(text: str) -> bool:
    token = os.getenv("BOT_TOKEN", "").strip()
    raw = (os.getenv("CARRY_ALERT_CHAT_IDS", "") or os.getenv("ALERT_ENGINE_CHAT_IDS", "")
           or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE" or not raw:
        print("  [tg] нет BOT_TOKEN/CARRY_ALERT_CHAT_IDS в env — не отправлено.")
        return False
    ok = False
    for cid in [c.strip() for c in raw.split(",") if c.strip()]:
        try:
            body = urllib.parse.urlencode({
                "chat_id": cid, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true"}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
            urllib.request.urlopen(req, timeout=15).read()
            ok = True
        except Exception as e:  # noqa: BLE001
            print(f"  [tg] не доставлено {cid}: {str(e)[:60]}")
    return ok


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(s: dict) -> None:
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:  # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--send", action="store_true", help="отправить в Telegram")
    ap.add_argument("--watch", type=int, default=0, help="повтор каждые N секунд")
    args = ap.parse_args()

    while True:
        text, cur_open = build_briefing(args.capital)
        print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        state = _load_state()
        prev_open = state.get("open", {})
        closes = close_alerts(prev_open, cur_open)
        if args.send:
            send_telegram(text)
            for cm in closes:
                print("\n[ЗАКРЫВАЙ-алерт]\n" + cm.replace("<b>", "").replace("</b>", ""))
                send_telegram(cm)
        state["open"] = cur_open
        _save_state(state)
        if args.watch <= 0:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
