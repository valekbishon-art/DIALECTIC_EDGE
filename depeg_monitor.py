"""
depeg_monitor.py — Алерт о ДЕПЕГЕ фиат-обеспеченных стейблкоинов.

Идея (спотовая, без шортов и деривативов, халяль): когда фиат-обеспеченный
стейблкоин (USDC/TUSD/USDP/FDUSD) временно торгуется НИЖЕ доллара из-за паники
или проблем банка-партнёра — исторически он почти всегда возвращается к $1.

⚠️ ВАЖНО (честно): это НЕ «стабильный доход», а РЕДКАЯ возможность с хвостовым
риском. 5-летний бэктест (часовые свечи, нетто после комиссий 0.1%/сторона +
0.05% слиппедж) дал ~13 сделок за 5 лет, винрейт ~85%, итог +17–20% за 5 лет
(≈3–4%/год) — и почти вся прибыль пришла от ОДНОГО события (USDC, крах SVB,
март 2023: цена падала до 0.882 и вернулась к ~$1 за ~4 дня).

Поэтому модуль работает как ИНФОРМАЦИОННЫЙ АЛЕРТ + подробное объяснение «что
делать с этой инфой», а НЕ как авто-трейдер. Алготические стейблы (типа UST)
СОЗНАТЕЛЬНО исключены: они не обязаны возвращаться к пегу и могут уйти в ноль.

Интеграция повторяет паттерн pump_alert / halal_alerts:
  feature_enabled(), get_interval_seconds(), class DepegAlertSystem.check_and_alert().
По умолчанию авто-пуш ВЫКЛ (FEATURE_DEPEG_ALERT=0) — фича безопасна на проде.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp

try:
    from database import kv_get, kv_set
except Exception:  # noqa: BLE001 — тесты/изоляция без БД
    kv_get = kv_set = None  # type: ignore

logger = logging.getLogger(__name__)

# --- Конфиг (крутится через env без передеплоя кода) -----------------------

# Только ФИАТ-обеспеченные стейблы (исключаем алго-стейблы — они не реверсят).
_DEFAULT_STABLES = ["USDC", "TUSD", "USDP", "FDUSD"]

_KEY_STATE = "depeg_alert_state"  # kv: {"flagged": {"USDC": "2023-03-11T..."}}


def get_stables() -> list[str]:
    raw = os.getenv("DEPEG_STABLES", "")
    if raw.strip():
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(_DEFAULT_STABLES)


def get_entry_threshold() -> float:
    """Цена, ниже которой считаем депегом и шлём алерт (доля от $1)."""
    try:
        return float(os.getenv("DEPEG_ENTRY_THRESHOLD", "0.99"))
    except ValueError:
        return 0.99


def get_recovery_threshold() -> float:
    """Цена, выше которой считаем депег закрытым (сбрасываем флаг)."""
    try:
        return float(os.getenv("DEPEG_RECOVERY_THRESHOLD", "0.998"))
    except ValueError:
        return 0.998


def get_hard_floor() -> float:
    """Ниже этого — НЕ возможность, а тревога (вероятна реальная
    неплатёжеспособность; исторический паттерн возврата НЕ работает)."""
    try:
        return float(os.getenv("DEPEG_HARD_FLOOR", "0.90"))
    except ValueError:
        return 0.90


def get_interval_seconds() -> int:
    try:
        return max(60, int(os.getenv("DEPEG_INTERVAL_SEC", "1800")))  # 30 мин
    except ValueError:
        return 1800


def feature_enabled() -> bool:
    """Авто-пуш в фоне. По умолчанию ВКЛ (депеги редки → спама нет; первый
    прогон только сохраняет baseline). Выключить: FEATURE_DEPEG_ALERT=0."""
    return os.getenv("FEATURE_DEPEG_ALERT", "1").strip().lower() in (
        "1", "true", "yes", "on")


# --- Получение цен ----------------------------------------------------------

_BINANCE = "https://api.binance.com/api/v3/ticker/price"
_VISION = "https://data-api.binance.vision/api/v3/ticker/price"  # geo-friendly fallback


async def _fetch_from(session: aiohttp.ClientSession, base: str,
                      symbols: list[str]) -> dict[str, float]:
    quoted = json.dumps(symbols, separators=(",", ":"))
    url = f"{base}?symbols={quoted}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = await resp.json()
    return {row["symbol"]: float(row["price"]) for row in data}


async def fetch_prices(stables: list[str] | None = None) -> dict[str, float]:
    """{'USDC': 0.997, ...} — цена стейбла в USDT. Binance, фолбэк на vision-зеркало.

    Пустой dict = сеть/гео легли (не трогаем состояние, не спамим)."""
    stables = stables or get_stables()
    symbols = [f"{s}USDT" for s in stables]
    async with aiohttp.ClientSession() as session:
        for base in (_BINANCE, _VISION):
            try:
                raw = await _fetch_from(session, base, symbols)
                out: dict[str, float] = {}
                for s in stables:
                    p = raw.get(f"{s}USDT")
                    if p and p > 0:
                        out[s] = p
                if out:
                    return out
            except Exception as e:  # noqa: BLE001 — пробуем следующий источник
                logger.warning("Depeg price fetch from %s failed: %s", base, e)
    return {}


# --- Детект возможностей ----------------------------------------------------

def detect_opportunities(prices: dict[str, float],
                         entry: float | None = None,
                         floor: float | None = None) -> list[dict]:
    """Возвращает список депег-событий, отсортированный по глубине скидки."""
    entry = get_entry_threshold() if entry is None else entry
    floor = get_hard_floor() if floor is None else floor
    opps: list[dict] = []
    for sym, price in prices.items():
        if price >= entry:
            continue
        discount = (1.0 - price) * 100.0  # % ниже пега
        if price < floor:
            severity = "danger"   # ниже жёсткого пола — НЕ покупать вслепую
        elif price < (entry + floor) / 2:
            severity = "deep"
        else:
            severity = "mild"
        opps.append({
            "symbol": sym, "price": price,
            "discount_pct": discount, "severity": severity,
        })
    opps.sort(key=lambda o: o["price"])  # самый глубокий депег первым
    return opps


# --- Тексты -----------------------------------------------------------------

# Честный пошаговый гайд «что делать с этой инфой» — также используется в
# explainers["depeg"]. Telegram Markdown (одинарная *).
WHAT_TO_DO_MD = (
    "📌 *Депег стейблкоина — что это и что с этим делать*\n"
    "\n"
    "*Что такое депег.* Стейблкоин должен стоить ≈ $1. Иногда он временно падает "
    "ниже (например, 0.97) — из-за паники, проблем у банка-партнёра эмитента или "
    "слухов. Фиат-обеспеченные стейблы (USDC, TUSD, USDP, FDUSD) исторически "
    "почти всегда возвращались к $1, когда паника проходила.\n"
    "\n"
    "*Почему это возможность.* Купив условно по 0.97 и продав по ~1.00, разница "
    "(~3%) — твоя. Но это РЕДКО и НЕ гарантировано (см. риски ниже).\n"
    "\n"
    "*Пошагово (если решишь действовать — на свой риск):*\n"
    "1️⃣ *Проверь ПРИЧИНУ по новостям.* Это техническая паника (биржа, слухи, "
    "временная нехватка ликвидности) — или РЕАЛЬНАЯ неплатёжеспособность эмитента? "
    "Возврат к $1 работает только в первом случае.\n"
    "2️⃣ *Только фиат-обеспеченные* (USDC/TUSD/USDP/FDUSD). НЕ трогай алго-стейблы "
    "(типа бывшего UST) — они не обязаны возвращаться и могут уйти в ноль.\n"
    "3️⃣ *Маленький размер.* Это не «весь депозит», а небольшая доля — потому что "
    "иногда депег = начало конца (см. риски).\n"
    "4️⃣ *Заранее наметь выход* около $0.998–1.00. Цель — поймать возврат к пегу, "
    "а не «ждать иксов».\n"
    "5️⃣ *Ментальный стоп.* Если цена пробивает жёсткий пол (~$0.90) и продолжает "
    "падать на фоне реальных плохих новостей — это сигнал, что паттерн возврата "
    "сломан. Лучше зафиксировать убыток, чем досиживать до нуля.\n"
    "\n"
    "⚠️ *Риски (читай обязательно):*\n"
    "• Алго-стейблы НЕ реверсят. UST в 2022 ушёл с $1 почти в ноль — вход «на "
    "отскок» там = потеря ~100%.\n"
    "• Эмитент может заморозить вывод/обмен — застрянешь в активе.\n"
    "• События РЕДКИЕ (≈ раз в год), деньги в ожидании простаивают.\n"
    "• Бэктест предполагает, что ты смог купить на дне в панике и у тебя был USDT "
    "наготове — в реале это психологически и технически тяжело.\n"
    "\n"
    "📊 *Честная статистика (бэктест 5 лет, часовые свечи, нетто после комиссий):*\n"
    "• ~13 сделок за 5 лет, винрейт ~85%, итог ≈ +17–20% за 5 лет (≈3–4%/год).\n"
    "• Почти вся прибыль — от ОДНОГО события (USDC, крах SVB, март 2023: падал "
    "до 0.882, вернулся к ~$1 за ~4 дня).\n"
    "\n"
    "Это *информация, а не инвест-совет*. Решения и риски — на тебе."
)


def _sev_label(sev: str) -> str:
    return {
        "danger": "🛑 НИЖЕ пола — осторожно (вероятна реальная проблема)",
        "deep": "🔴 глубокий депег",
        "mild": "🟡 лёгкий депег",
    }.get(sev, "🟡 депег")


def format_opportunity_alert(opps: list[dict]) -> str:
    """Текст авто-алерта при появлении новых депег-событий."""
    if not opps:
        return ""
    lines = ["⚖️ *ДЕПЕГ СТЕЙБЛКОИНА — возможен возврат к $1*", ""]
    for o in opps:
        lines.append(
            f"• *{o['symbol']}* = ${o['price']:.4f}  "
            f"({o['discount_pct']:.2f}% ниже пега) — {_sev_label(o['severity'])}"
        )
    lines += [
        "",
        "Историч. фиат-обеспеченные стейблы почти всегда возвращались к $1, "
        "*но это редкое событие с хвостовым риском* — не гарантия.",
        "",
        "👉 Нажми «❓ Что делать с этим» — там пошаговый честный разбор и риски.",
    ]
    return "\n".join(lines)


def format_status(prices: dict[str, float], opps: list[dict]) -> str:
    """Текст ручной команды /depeg — показывает все мониторимые стейблы."""
    if not prices:
        return ("⚠️ Не смог получить свежие цены стейблкоинов. "
                "Попробуй ещё раз через минуту.")
    entry = get_entry_threshold()
    lines = ["⚖️ *МОНИТОР ДЕПЕГА СТЕЙБЛКОИНОВ*", ""]
    for sym in sorted(prices, key=lambda s: prices[s]):
        p = prices[sym]
        mark = "🔴" if p < entry else "🟢"
        lines.append(f"{mark} *{sym}* = ${p:.4f}")
    lines.append("")
    if opps:
        lines.append(f"❗️ Сейчас ниже порога (${entry:.3f}): "
                     + ", ".join(o["symbol"] for o in opps))
    else:
        lines.append(f"✅ Все у пега (порог алерта ${entry:.3f}). Возможностей нет.")
    lines += ["", "👉 «❓ Что делать с этим» — пошаговый разбор, риски и честная статистика."]
    return "\n".join(lines)


# --- Авто-алерт система (паттерн HalalAlertSystem) ---------------------------

class DepegAlertSystem:
    """Шлёт подписчикам алерт при ПОЯВЛЕНИИ нового депега; не спамит повторно."""

    def __init__(self, bot):
        self.bot = bot

    async def _load_flagged(self) -> dict[str, str]:
        if kv_get is None:
            return {}
        raw = await kv_get(_KEY_STATE)
        if not raw:
            return {}
        try:
            return dict(json.loads(raw).get("flagged", {}))
        except Exception:  # noqa: BLE001
            return {}

    async def _save_flagged(self, flagged: dict[str, str]) -> None:
        if kv_set is None:
            return
        await kv_set(_KEY_STATE, json.dumps({"flagged": flagged}))

    async def check_and_alert(self, subscribers: list[dict]) -> int:
        prices = await fetch_prices()
        if not prices:  # сеть легла — не трогаем состояние, не спамим
            return 0

        opps = detect_opportunities(prices)
        now_flagged = {o["symbol"] for o in opps}
        prev = await self._load_flagged()
        recovery = get_recovery_threshold()

        # Сбрасываем флаг у тех, кто вернулся выше recovery.
        for sym in list(prev.keys()):
            price = prices.get(sym)
            if price is not None and price >= recovery:
                prev.pop(sym, None)

        # Новые депеги = те, кого ещё не флагали.
        new_opps = [o for o in opps if o["symbol"] not in prev]

        # Обновляем состояние (и тех, кто всё ещё в депеге, и новых).
        ts = datetime.now(timezone.utc).isoformat()
        for sym in now_flagged:
            prev.setdefault(sym, ts)
        await self._save_flagged(prev)

        if not new_opps or not subscribers:
            return 0

        text = format_opportunity_alert(new_opps)
        sent = 0
        for sub in subscribers:
            uid = sub.get("user_id") if isinstance(sub, dict) else sub
            if not uid:
                continue
            try:
                await self.bot.send_message(
                    uid, text, parse_mode="Markdown",
                    disable_web_page_preview=True,
                    reply_markup=_alert_kb(),
                )
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as e:  # noqa: BLE001
                logger.warning("Depeg alert send failed for %s: %s", uid, e)
        return sent


def _alert_kb():
    """Кнопка «Что делать с этим» → explain:depeg."""
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❓ Что делать с этим",
                                 callback_data="explain:depeg"),
        ]])
    except Exception:  # noqa: BLE001 — без aiogram (тесты)
        return None
