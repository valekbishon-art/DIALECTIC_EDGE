"""
halal_edge.py — ЕДИНЫЙ движок халяльного EDGE (живой сигнал + бэктест).

Тут лежит ОДНА функция сигнала `edge_signal()`, которую используют оба места:
  • research/halal_edge_backtest.py — гоняет её по истории (бэктест);
  • main.py `/edge` — зовёт её на сегодняшнем баре и «ведёт за руку».

Это и есть честность: то, что бот советует тебе сегодня, — ровно та же
логика, что показала результат в бэктесте. Никаких двойных стандартов.

Правила (жёстко): только спот, только лонг, без плеча, без шортов, без
деривативов. Источник преимущества:
  1) Dual momentum — монета входит, только если её тренд вверх (цена > SMA)
     И импульс > 0; из прошедших берём ТОП-K самых сильных.
  2) Inverse-vol веса + vol targeting портфеля (режем риск в шторм, потолок
     100% — без плеча).
  3) Краш-фильтр: BTC < SMA200 → весь капитал в стейбл.

Данные — дневные close с Yahoo (без ключа).
"""
from __future__ import annotations

import json
import math
import statistics
import time
import urllib.request
from datetime import datetime, timezone

# ── Юниверс и константы (общие для лайва и бэктеста) ──
UNIVERSE = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "AVAX", "LINK", "DOT", "LTC"]
CORE4 = ["BTC", "ETH", "SOL", "BNB"]

FEE = 0.001            # 0.1% на оборот (для бэктеста)
SMA_BTC = 200          # краш-фильтр режима всего рынка

DEFAULT_CFG = dict(
    sma_trend=150,                 # абсолютный тренд по монете (медленный = меньше пилы)
    mom_lb=(30, 90, 180),          # окна импульса (дни)
    top_k=4,                       # сколько монет держим
    vol_lb=30,                     # окно оценки волатильности
    vol_target_ann=None,           # vol targeting ОТКЛЮЧЁН: в крипто-быке режет ракеты
    weight_mode="mom",             # 'equal' | 'invvol' | 'mom' — вес по силе импульса
    rebal=7,                       # ребаланс раз в неделю (для бэктеста)
)
# ВАЖНЫЙ УРОК (бэктест на полном цикле 2021→2026): inverse-vol + vol targeting,
# которые отлично работают в акциях/макро, в КРИПТО проигрывают — они
# недовешивают самые волатильные монеты (SOL/AVAX), а именно они дают рост в
# быке. Реальный EDGE здесь = (1) краш-фильтр по BTC (уход в стейбл в медвежке)
# + (2) отбор и взвешивание по силе импульса (momentum). Это бьёт простой тренд
# и по доходности, и по Sharpe, устойчиво по сетке параметров.

_UA = {"User-Agent": "Mozilla/5.0"}
_YH_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}-USD?interval=1d&"


def fetch(sym: str, rng: str = "5y", period1: int | None = None) -> dict[str, float]:
    """Дневные close с Yahoo.

    range=max ДАУНСЕМПЛИТ до недельных — поэтому для длинной истории передавай
    period1 (unix-сек начала): тогда тянем явный диапазон period1..сейчас в 1d.
    """
    if period1 is not None:
        now = int(datetime.now(tz=timezone.utc).timestamp())
        url = _YH_BASE.format(sym=sym) + f"period1={period1}&period2={now}"
    else:
        url = _YH_BASE.format(sym=sym) + f"range={rng}"
    req = urllib.request.Request(url, headers=_UA)
    raw = urllib.request.urlopen(req, timeout=30).read()
    res = json.loads(raw)["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out: dict[str, float] = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        out[day] = float(c)
    return out


def sma(vals: list[float], n: int) -> float | None:
    return statistics.fmean(vals[-n:]) if len(vals) >= n else None


def max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd


def sharpe(daily: list[float]) -> float:
    if len(daily) < 2:
        return 0.0
    sd = statistics.pstdev(daily)
    return (statistics.fmean(daily) / sd) * math.sqrt(365) if sd else 0.0


def daily_rets(series: list[float | None], i: int, lb: int) -> list[float]:
    """Доходности за последние lb дней до индекса i включительно."""
    out: list[float] = []
    for j in range(i - lb + 1, i + 1):
        if j <= 0:
            continue
        p0, p1 = series[j - 1], series[j]
        if p0 and p1:
            out.append(p1 / p0 - 1.0)
    return out


def cov(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    return sum((a[k] - ma) * (b[k] - mb) for k in range(n)) / (n - 1)


def max_lookback(cfg: dict) -> int:
    return max(max(cfg["mom_lb"]), cfg["sma_trend"], SMA_BTC, cfg["vol_lb"])


# ───────────────────────── ЕДИНЫЙ сигнал EDGE ─────────────────────────
def edge_signal(series: dict[str, list[float | None]], i: int, cfg: dict) -> dict:
    """
    Чистый сигнал EDGE на баре i (используя цены ≤ i, без подглядывания).

    Возвращает dict:
      btc_on, regime ('risk_on'|'risk_off'),
      weights {sym: вес 0..1} (после vol-target, сумма может быть < 1),
      invested, cash, scale,
      picks [ {sym, weight, score, mom30, mom90, mom180} ] — по убыванию силы,
      btc_price, btc_sma.
    """
    coins = list(series.keys())
    sma_trend = cfg["sma_trend"]
    mom_lb = cfg["mom_lb"]
    top_k = cfg["top_k"]
    vol_lb = cfg["vol_lb"]
    weight_mode = cfg.get("weight_mode", "equal")
    vol_target_ann = cfg.get("vol_target_ann")
    vt_daily = (vol_target_ann / math.sqrt(365)) if vol_target_ann else None
    max_lb = max_lookback(cfg)

    btcser = series["BTC"]
    bwin = [p for p in btcser[: i + 1] if p is not None]
    btc_ma = sma(bwin, SMA_BTC)
    btc_price = btcser[i]
    btc_on = bool(btc_ma is not None and btc_price and btc_price > btc_ma)

    base = {
        "btc_on": btc_on,
        "regime": "risk_on" if btc_on else "risk_off",
        "weights": {}, "invested": 0.0, "cash": 1.0, "scale": 0.0,
        "picks": [], "btc_price": btc_price, "btc_sma": btc_ma,
    }
    if not btc_on:
        return base  # краш-фильтр: всё в стейбл

    # ── eligible: абсолютный тренд + положительный импульс ──
    cand: list[dict] = []
    for s in coins:
        ser = series[s]
        win = [p for p in ser[: i + 1] if p is not None]
        if len(win) < max_lb + 1:
            continue
        ma = sma(win, sma_trend)
        pr = ser[i]
        if not (pr and ma and pr > ma):
            continue
        moms = {}
        for lb in mom_lb:
            p0 = ser[i - lb] if i - lb >= 0 else None
            moms[lb] = (pr / p0 - 1.0) if (p0 and pr) else None
        vals = [v for v in moms.values() if v is not None]
        if not vals:
            continue
        score = statistics.fmean(vals)
        if score <= 0:
            continue
        cand.append({
            "sym": s, "score": score,
            "mom30": moms.get(mom_lb[0]), "mom90": moms.get(mom_lb[1] if len(mom_lb) > 1 else mom_lb[0]),
            "mom180": moms.get(mom_lb[-1]),
        })

    cand.sort(key=lambda x: x["score"], reverse=True)
    chosen = cand[:top_k]
    if not chosen:
        return base

    # ── базовые веса внутри корзины ──
    names = [c["sym"] for c in chosen]
    rets_map = {c["sym"]: daily_rets(series[c["sym"]], i, vol_lb) for c in chosen}
    if weight_mode == "invvol":
        # обратная волатильности — спокойным больше (в крипто-быке режет ракеты)
        vols = {}
        for c in chosen:
            r = rets_map[c["sym"]]
            sd = statistics.pstdev(r) if len(r) > 1 else 0.0
            vols[c["sym"]] = sd if sd > 1e-9 else 1e-9
        inv = {s: 1.0 / vols[s] for s in names}
        tot = sum(inv.values())
        raw_w = {s: inv[s] / tot for s in names}
    elif weight_mode == "mom":
        # пропорционально силе импульса — сильным больше
        scores = {c["sym"]: max(c["score"], 1e-9) for c in chosen}
        tot = sum(scores.values())
        raw_w = {s: scores[s] / tot for s in names}
    else:  # equal — равный вес (по умолчанию; лучше всего в крипто-цикле)
        raw_w = {s: 1.0 / len(names) for s in names}

    # ── опциональный vol targeting на уровне портфеля (cap = 1.0, без плеча) ──
    if vt_daily:
        port_var = 0.0
        for a in names:
            for b in names:
                port_var += raw_w[a] * raw_w[b] * cov(rets_map[a], rets_map[b])
        port_vol = math.sqrt(port_var) if port_var > 0 else 0.0
        scale = min(1.0, vt_daily / port_vol) if port_vol > 1e-9 else 1.0
    else:
        scale = 1.0
    weights = {s: raw_w[s] * scale for s in names}

    picks = []
    for c in chosen:
        picks.append({**c, "weight": weights[c["sym"]]})
    invested = sum(weights.values())
    base.update({
        "weights": weights, "invested": invested, "cash": 1.0 - invested,
        "scale": scale, "picks": picks,
    })
    return base


# ───────────────────────── живой план («за руку») ─────────────────────────
_CACHE: dict = {"ts": 0.0, "days": None, "series": None}
_TTL = 1800  # 30 минут


def load_live(cfg: dict | None = None, rng: str = "2y", force: bool = False) -> tuple[list[str], dict]:
    """Свежие дневные данные по юниверсу, выровненные по календарю BTC. С TTL-кэшем."""
    now = time.time()
    if not force and _CACHE["days"] and (now - _CACHE["ts"] < _TTL):
        return _CACHE["days"], _CACHE["series"]
    raw: dict[str, dict[str, float]] = {}
    for sym in UNIVERSE:
        try:
            raw[sym] = fetch(sym, rng)
        except Exception:  # noqa: BLE001
            raw[sym] = {}
    if not raw.get("BTC"):
        raise RuntimeError("нет данных по BTC")
    days = sorted(raw["BTC"].keys())
    series = {s: [raw[s].get(d) for d in days] for s in UNIVERSE if raw[s]}
    _CACHE.update(ts=now, days=days, series=series)
    return days, series


def live_plan(cfg: dict | None = None, deposit: float = 100.0) -> dict:
    """План EDGE на сегодня: режим, что покупать и в каких долях, сколько в стейбле."""
    cfg = cfg or DEFAULT_CFG
    days, series = load_live(cfg)
    i = len(days) - 1
    sig = edge_signal(series, i, cfg)
    sig["as_of"] = days[i]
    sig["deposit"] = deposit
    return sig


def render_plan_text(plan: dict, deposit: float = 100.0) -> str:
    """Пошаговый «ведём за руку» текст (RU, Telegram-Markdown сбалансирован)."""
    as_of = plan.get("as_of", "")
    money = lambda frac: f"${deposit * frac:,.0f}"          # всегда в долларах
    pc = lambda x: f"{x * 100:.0f}%"
    pm = lambda x: f"{x * 100:+.0f}%" if x is not None else "—"

    head = (
        f"🧭 *EDGE-план на сегодня* _(данные на {as_of})_\n"
        "Строго спот, только лонг, без плеча и шортов.\n"
    )

    if plan["regime"] == "risk_off":
        bp, bs = plan.get("btc_price"), plan.get("btc_sma")
        why = f" BTC ${bp:,.0f} ниже своего тренда (SMA200 ≈ ${bs:,.0f})." if (bp and bs) else ""
        return (
            head + "\n"
            "🔴 *Режим: РИСК-ОФФ — сидим в стейбле.*" + why + "\n\n"
            "*Что делать пошагово:*\n"
            "1️⃣ Весь капитал держим в стейбле (USDT/USDC).\n"
            "2️⃣ *Ничего не покупаем* — даже если «дёшево». В медвежке дёшево становится ещё дешевле.\n"
            "3️⃣ Никакого плеча, фьючерсов и шортов. Просто ждём.\n"
            "4️⃣ Загляни снова через неделю — как BTC вернётся выше тренда, EDGE сам даст список монет.\n\n"
            "_Это не страх, а дисциплина: именно уход в стейбл в медвежке и даёт преимущество._"
        )

    picks = plan.get("picks", [])
    cash = plan.get("cash", 0.0)
    intro = (f"на твой депозит {money(1.0)}" if deposit != 100.0 else "на каждые $100 депозита")
    lines = [
        head,
        "🟢 *Режим: РИСК-ОН — рынок в аптренде.*",
        f"EDGE отобрал *{len(picks)}* самых сильных монет. План {intro}:\n",
    ]
    for p in picks:
        lines.append(
            f"• *{p['sym']}* — {pc(p['weight'])} (~{money(p['weight'])}) · импульс 90д {pm(p.get('mom90'))}"
        )
    if cash > 0.005:
        lines.append(f"• 💵 *Стейбл (USDT)* — {pc(cash)} (~{money(cash)}) — подушка на тряску")
    lines.append(
        "\n*Что делать пошагово:*\n"
        "1️⃣ Купи эти монеты *спотом* в указанных долях (можно за 2–3 захода за пару дней).\n"
        "2️⃣ Держи и не дёргайся на ежедневном шуме.\n"
        "3️⃣ Раз в неделю жми /plan: если монета выпала из списка — продай её и переложись в новую.\n"
        "4️⃣ Если придёт *РИСК-ОФФ* — спокойно выходи в стейбл целиком.\n"
        "5️⃣ Только спот. Без плеча, фьючерсов и шортов.\n\n"
        "_Это история и алгоритм, не гарантия и не инвестсовет. Решение за тобой._"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    pl = live_plan()
    print("regime:", pl["regime"], "| invested:", round(pl["invested"], 3), "| cash:", round(pl["cash"], 3))
    for p in pl["picks"]:
        print(f"  {p['sym']:5} w={p['weight']*100:5.1f}%  score={p['score']*100:+6.1f}%")
    print("\n--- TEXT ---\n")
    print(render_plan_text(pl))
