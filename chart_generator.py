"""
chart_generator.py — Генерация графиков для Dialectic Edge.

УЛУЧШЕНО v4:
- ИСПРАВЛЕН emoji фикс: убирает 📦🏭💰📈 из названий (isalnum + кириллица)
- Добавлено детальное логирование для диагностики
- Исправлен _parse_russia_items: обрабатывает " • Название" с пробелами
  и "Уверенность: ВЫСОКАЯ." с точкой в конце
- Добавлен FinBERT Sentiment бар
- Добавлен RSI BTC
- Заголовок показывает реальные модели из ai_provider.MODELS_USED
"""

import io
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)
logger.info("chart_generator v4 loaded — emoji fix active")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.gridspec import GridSpec
    MATPLOTLIB_OK = True
except ImportError:
    MATPLOTLIB_OK = False
    logger.warning("matplotlib не установлен — графики недоступны")


COLORS = {
    "bg":       "#0D1117",
    "surface":  "#161B22",
    "border":   "#30363D",
    "bull":     "#3FB950",
    "bear":     "#F85149",
    "neutral":  "#8B949E",
    "gold":     "#D4A520",
    "text":     "#C9D1D9",
    "subtext":  "#8B949E",
    "blue":     "#58A6FF",
}


def _setup_dark_style():
    plt.rcParams.update({
        "figure.facecolor":  COLORS["bg"],
        "axes.facecolor":    COLORS["surface"],
        "axes.edgecolor":    COLORS["border"],
        "axes.labelcolor":   COLORS["text"],
        "xtick.color":       COLORS["subtext"],
        "ytick.color":       COLORS["subtext"],
        "text.color":        COLORS["text"],
        "grid.color":        COLORS["border"],
        "grid.alpha":        0.5,
        "font.family":       "DejaVu Sans",
        "font.size":         10,
    })


def _to_bytes(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=COLORS["bg"])
    buf.seek(0)
    plt.close(fig)
    return buf


def _parse_scenarios(report: str) -> dict:
    scenarios = {"Базовый": 50, "Бычий": 25, "Медвежий": 25}
    patterns = [
        (r"БАЗОВЫЙ[^(]*\((\d+)%\)",  "Базовый"),
        (r"БЫЧИЙ[^(]*\((\d+)%\)",    "Бычий"),
        (r"МЕДВЕЖИЙ[^(]*\((\d+)%\)", "Медвежий"),
        (r"базовый[^(]*\((\d+)%\)",  "Базовый"),
        (r"бычий[^(]*\((\d+)%\)",    "Бычий"),
        (r"медвежий[^(]*\((\d+)%\)", "Медвежий"),
    ]
    for pattern, key in patterns:
        m = re.search(pattern, report, re.IGNORECASE)
        if m:
            scenarios[key] = int(m.group(1))
    return scenarios


def _keyword_bull_bear_ratio(report: str) -> tuple[float, float]:
    """Грубый подсчёт по словам (в полном тексте дебатов даёт сильный шум)."""
    bull_signals = [
        "бычий", "рост", "покупать", "long", "восстановлени",
        "позитивный", "сильный сигнал", "точка входа",
    ]
    bear_signals = [
        "медвежий", "падение", "продавать", "short", "риск",
        "давление", "коррекция", "стагфляци",
    ]
    text = report.lower()
    bull = sum(text.count(s) for s in bull_signals)
    bear = sum(text.count(s) for s in bear_signals)
    total = bull + bear or 1
    return round(bull / total * 100, 1), round(bear / total * 100, 1)


def _extract_synth_verdict(report: str) -> str | None:
    """
    Итог дебатов обычно в хвосте отчёта (Synth / судья).
    Ищем ВЕРДИКТ СУДЬИ или ИТОГОВЫЙ СИНТЕЗ.
    """
    text = (report or "").upper()

    # Проверяем бычий/медвежий уклон
    patterns = [
        r"ВЕРДИКТ\s+СУДЬИ[^\n]*?[:：]\s*\*?БЫЧИЙ(?:\s*\([^)]+\))?\*?",     # БЫЧИЙ
        r"ВЕРДИКТ\s+СУДЬИ[^\n]*?[:：]\s*\*?МЕДВЕЖИЙ(?:\s*\([^)]+\))?\*?",  # МЕДВЕЖИЙ
        r"ВЕРДИКТ\s+СУДЬИ[^\n]*?[:：]\s*\*?НЕЙТРАЛЬНЫЙ\s*\([^)]+\)\*?",    # НЕЙТРАЛЬНЫЙ (с уклоном...)
        r"ВЕРДИКТ\s+СУДЬИ[^\n]*?[:：]\s*\*?НЕЙТРАЛЬНЫЙ\*?",               # НЕЙТРАЛЬНЫЙ
        r"ИТОГОВЫЙ\s+СИНТЕЗ[^\n]*?[:：]\s*\*?БЫЧИЙ",                       # ИТОГОВЫЙ СИНТЕЗ БЫЧИЙ
        r"ИТОГОВЫЙ\s+СИНТЕЗ[^\n]*?[:：]\s*\*?МЕДВЕЖИЙ",                   # ИТОГОВЫЙ СИНТЕЗ МЕДВЕЖИЙ
        r"ИТОГОВЫЙ\s+СИНТЕЗ[^\n]*?[:：]\s*\*?НЕЙТРАЛЬНЫЙ",                # ИТОГОВЫЙ СИНТЕЗ НЕЙТРАЛЬНЫЙ
        r"ВЕРДИКТ[^\n]*?БЫЧИЙ",
        r"ВЕРДИКТ[^\n]*?МЕДВЕЖИЙ",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            matched = m.group(0)
            if "БЫЧИЙ" in matched:
                return "bull"
            elif "МЕДВЕЖИЙ" in matched:
                return "bear"
            elif "НЕЙТРАЛЬНЫЙ" in matched:
                return "neutral"

    return None


def _fear_greed_value(prices: dict | None) -> float | None:
    if not prices:
        return None
    try:
        macro = prices.get("MACRO") or {}
        fng = macro.get("fng") or {}
        v = fng.get("val")
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.replace(".", "").isdigit():
            return float(v)
    except Exception:
        pass
    return None


def _parse_bull_bear_score(report: str, prices: dict | None = None) -> tuple[float, float]:
    """
    Полоса «баланс аргументов»: не дословный подсчёт слов по всему логу дебатов,
    а опора на итоговый вердикт + корректировка по Fear & Greed (если есть).
    
    Поддерживает уклоны: "НЕЙТРАЛЬНЫЙ (с уклоном в бычий)" → bull=55%, bear=45%
    """
    kw_bull, kw_bear = _keyword_bull_bear_ratio(report)
    verdict = _extract_synth_verdict(report)
    fng = _fear_greed_value(prices)

    # Проверяем уклоны в тексте
    text = (report or "").upper()
    has_bull_bias = "УКЛОН В БЫЧИЙ" in text or "УКЛОН В БЫЧЬЮ" in text or "БЫЧИЙ УКЛОН" in text
    has_bear_bias = "УКЛОН В МЕДВЕЖИЙ" in text or "УКЛОН В МЕДВЕЖЬЮ" in text or "МЕДВЕЖИЙ УКЛОН" in text

    if verdict == "bear" and not has_bull_bias:
        bull, bear = 36.0, 64.0
    elif verdict == "bull" and not has_bear_bias:
        bull, bear = 64.0, 36.0
    elif verdict == "neutral" or verdict is None:
        # Учитываем уклоны
        if has_bull_bias:
            bull, bear = 55.0, 45.0
        elif has_bear_bias:
            bull, bear = 45.0, 55.0
        else:
            bull, bear = 48.0, 52.0
    else:
        bull, bear = kw_bull, kw_bear

    # Смешение с keyword-оценкой (вердикт важнее, но не игнорируем «толщину» споров)
    if verdict:
        bull = round(0.65 * bull + 0.35 * kw_bull, 1)
        bear = round(100.0 - bull, 1)

    # Fear & Greed: без явного вердикта — сильная коррекция; с вердиктом — лёгкое усиление «в ту сторону»
    if fng is not None:
        if verdict is None:
            if fng <= 20:
                bear = min(88.0, bear + 10.0)
                bull = round(100.0 - bear, 1)
            elif fng <= 35:
                bear = min(82.0, bear + 5.0)
                bull = round(100.0 - bear, 1)
            elif fng >= 75:
                bull = min(88.0, bull + 10.0)
                bear = round(100.0 - bull, 1)
            elif fng >= 60:
                bull = min(82.0, bull + 5.0)
                bear = round(100.0 - bull, 1)
        else:
            if verdict == "bear" and fng <= 25:
                bear = min(86.0, bear + 4.0)
                bull = round(100.0 - bear, 1)
            elif verdict == "bull" and fng >= 70:
                bull = min(86.0, bull + 4.0)
                bear = round(100.0 - bull, 1)

    return bull, bear


def _parse_finbert(report: str):
    m = re.search(
        r"FINBERT SENTIMENT:\s*([+-]?\d+\.\d+)\s*→\s*(\w+).*?Уверенность[^:]*:\s*(\w+)",
        report, re.IGNORECASE | re.DOTALL
    )
    if m:
        return {
            "score":      float(m.group(1)),
            "label":      m.group(2).upper(),
            "confidence": m.group(3).upper(),
        }
    return None


def _parse_russia_items(text: str, marker: str) -> list:
    """
    Парсит блоки возможностей/рисков из Russia Edge отчёта.
    Bullet: •, -, –, *; рейтинг: Уверенность/Вероятность ВЫСОКАЯ/СРЕДНЯЯ/НИЗКАЯ.
    Учитывает: вариацию эмодзи (FE0F), Markdown *вокруг* слова, «Уверенность — ВЫСОКАЯ».
    """
    text = (text or "").replace("\ufe0f", "")
    items      = []
    rating_map = {"ВЫСОКАЯ": 3, "СРЕДНЯЯ": 2, "НИЗКАЯ": 1}
    bullet_re  = re.compile(r"^[\s]*[•\-–*·▪]\s+", re.UNICODE)
    rating_re  = re.compile(
        r"(уверенность|вероятность)\s*[:：—–\-]\s*",
        re.IGNORECASE,
    )

    if marker == "🟢":
        start = text.find("🟢 ВОЗМОЖНОСТИ")
        if start == -1:
            start = text.find("🟢")
    else:
        start = text.find("🔴 РИСКИ")
        if start == -1:
            start = text.find("🔴")

    if start == -1:
        return items

    if marker == "🟢":
        end = text.find("🔴 РИСКИ", start + 1)
        if end == -1:
            end = text.find("🔴", start + 2)
        if end == -1:
            end = len(text)
    else:
        end = len(text)
        for em in ("🇷🇺 ИТОГ", "💡 ТОП-3", "⚖️ БАЛАНС", "🤝 Честно", "🟢 ВОЗМОЖНОСТИ"):
            pos = text.find(em, start + 5)
            if pos != -1 and pos < end:
                end = pos

    block = text[start:end]
    lines = block.split("\n")

    current_name = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        norm = re.sub(r"[*_`]", "", stripped)

        if bullet_re.match(norm) and len(norm) > 4:
            raw = bullet_re.sub("", norm).strip()
            raw = re.sub(r"[*_`]", "", raw)
            raw = "".join(
                c for c in raw
                if c.isalnum() or c in " ,:.()/+-%" or "\u0400" <= c <= "\u04FF"
            )
            raw = raw.strip()
            raw = re.sub(r"\s*\([^)]+\)\s*$", "", raw).strip()
            if raw:
                current_name = raw[:30]

        if not current_name:
            continue
        if not rating_re.search(norm):
            continue
        up = norm.upper()
        for key, val in rating_map.items():
            if key in up:
                items.append({"name": current_name, "rating": val})
                current_name = None
                break

    return items

def _stars_for_chart(stars: str) -> str:
    """Конвертирует emoji-звёзды в Unicode-символы которые matplotlib рендерит
    стандартным шрифтом (DejaVu Sans).
    
    `⭐` (U+2B50) → `★` (U+2605, Black Star) — есть в DejaVu Sans.
    `☆` (U+2606) — уже рендерится в DejaVu Sans, не трогаем.
    """
    if not stars:
        return ""
    return stars.replace("⭐", "★")


def generate_main_chart(report: str, prices: dict, stars: str, pct: int):
    if not MATPLOTLIB_OK:
        return None
    # Star fix: для matplotlib используем ★ (U+2605) вместо ⭐ (U+2B50)
    stars = _stars_for_chart(stars)
    if not prices:
        prices = {}

    try:
        _setup_dark_style()
        fig = plt.figure(figsize=(10, 6), facecolor=COLORS["bg"])

        now            = datetime.now().strftime("%d.%m.%Y %H:%M")
        bull_pct, bear_pct = _parse_bull_bear_score(report, prices)
        scenarios      = _parse_scenarios(report)
        finbert = prices.get("SENTIMENT")
        if not finbert:
            finbert = _parse_finbert(report)

        try:
            from ai_provider import get_models_summary
            models_str = get_models_summary()
        except Exception:
            models_str = ""

        grid_top = 0.80 if finbert else 0.88
        gs  = GridSpec(2, 2, figure=fig, hspace=0.45, wspace=0.35,
                       left=0.08, right=0.95, top=grid_top, bottom=0.08)

        fig.text(0.5, 0.96, "DIALECTIC EDGE — MARKET ANALYSIS",
                 ha="center", va="top", fontsize=13, fontweight="bold",
                 color=COLORS["gold"])
        fig.text(0.5, 0.915, f"{now}   |   Сигнал: {stars} ({pct}%)",
                 ha="center", va="top", fontsize=9, color=COLORS["subtext"])
        if finbert:
            fl = str(finbert.get("label", "")).upper()
            fc = str(finbert.get("confidence", "")).upper()
            fig.text(
                0.5, 0.875,
                f"FinBERT: {fl} · уверенность классификатора: {fc} — полоса «Уровень сигнала» = эта уверенность, "
                f"не прогноз «рынок вверх/вниз».",
                ha="center", va="top", fontsize=7.2, color=COLORS["subtext"],
            )

        ax1 = fig.add_subplot(gs[0, 0])
        ax1.set_title("Баланс аргументов", color=COLORS["text"], fontsize=10, pad=8)
        ax1.barh([""], [bull_pct], color=COLORS["bull"], height=0.5,
                 label=f"Bull {bull_pct:.0f}%")
        ax1.barh([""], [bear_pct], left=[bull_pct],
                 color=COLORS["bear"], height=0.5,
                 label=f"Bear {bear_pct:.0f}%")
        ax1.set_xlim(0, 100)
        ax1.set_xlabel("% аргументов", fontsize=8)
        ax1.axvline(50, color=COLORS["border"], linewidth=1, linestyle="--")
        ax1.text(bull_pct / 2, 0, f"{bull_pct:.0f}%",
                 ha="center", va="center", fontsize=9, color="white", fontweight="bold")
        ax1.text(bull_pct + bear_pct / 2, 0, f"{bear_pct:.0f}%",
                 ha="center", va="center", fontsize=9, color="white", fontweight="bold")
        ax1.set_yticks([])
        ax1.legend(loc="upper right", fontsize=7,
                   facecolor=COLORS["surface"], edgecolor=COLORS["border"],
                   labelcolor=COLORS["text"])
        ax1.text(
            0.5, -0.42,
            "Шкала: итог дебатов + Fear & Greed, не «кто чаще сказал рост/риск».",
            transform=ax1.transAxes, ha="center", va="top",
            fontsize=6, color=COLORS["subtext"],
        )

        ax2 = fig.add_subplot(gs[0, 1])
        ax2.set_title("Вероятность сценариев", color=COLORS["text"], fontsize=10, pad=8)
        labels     = list(scenarios.keys())
        sizes      = list(scenarios.values())
        colors_pie = [COLORS["neutral"], COLORS["bull"], COLORS["bear"]]
        wedges, texts, autotexts = ax2.pie(
            sizes, labels=labels, colors=colors_pie,
            autopct="%1.0f%%", startangle=90,
            wedgeprops={"edgecolor": COLORS["bg"], "linewidth": 2},
            pctdistance=0.75,
            textprops={"color": COLORS["text"], "fontsize": 8},
        )
        for at in autotexts:
            at.set_color("white")
            at.set_fontsize(8)
            at.set_fontweight("bold")

        ax3 = fig.add_subplot(gs[1, 0])
        ax3.set_title("Ключевые активы", color=COLORS["text"], fontsize=10, pad=8)
        ax3.axis("off")

        rows = []
        labels_map = [
            ("BTC",     "Bitcoin",   "$", ","),
            ("ETH",     "Ethereum",  "$", ","),
            ("SPX",     "S&P 500",   "",  ","),
            ("OIL_WTI", "Нефть WTI", "$", ".2f"),
            ("GOLD",    "Золото",    "$", ","),
        ]
        for key, name, prefix, fmt in labels_map:
            if key in prices:
                p  = prices[key]
                pr = p["price"]
                ch = p["change_24h"]
                p_str = f"{prefix}{pr:,.0f}" if fmt == "," else f"{prefix}{pr:,.2f}"
                arrow = "▲" if ch > 0 else "▼" if ch < 0 else "●"
                color = (COLORS["bull"] if ch > 0 else
                         COLORS["bear"] if ch < 0 else
                         COLORS["neutral"])
                rows.append((name, p_str, f"{arrow}{abs(ch):.2f}%", color))

        # Временная метка в заголовке СТолбца 24ч: раньше юзер видел 24ч без
        # таймстампа и думал что это дельта за календарный день — но это
        # скользящее 24ч-окно от момента запроса. Подпись снимает путаницу.
        _now_hm = datetime.now().strftime("%H:%M")
        y = 0.95
        ax3.text(0.0,  y, "Актив",  transform=ax3.transAxes, fontsize=8, color=COLORS["subtext"], fontweight="bold")
        ax3.text(0.45, y, "Цена",   transform=ax3.transAxes, fontsize=8, color=COLORS["subtext"], fontweight="bold")
        ax3.text(0.78, y, f"24ч на {_now_hm}", transform=ax3.transAxes, fontsize=8, color=COLORS["subtext"], fontweight="bold")
        ax3.plot([0, 1], [y - 0.04, y - 0.04], color=COLORS["border"],
                 linewidth=0.5, transform=ax3.transAxes, clip_on=False)

        for i, (name, price_str, chg_str, c) in enumerate(rows):
            yi = y - 0.16 - i * 0.16
            ax3.text(0.0,  yi, name,      transform=ax3.transAxes, fontsize=8.5, color=COLORS["text"])
            ax3.text(0.45, yi, price_str, transform=ax3.transAxes, fontsize=8.5, color=COLORS["text"])
            ax3.text(0.78, yi, chg_str,   transform=ax3.transAxes, fontsize=8.5, color=c, fontweight="bold")

        ax4 = fig.add_subplot(gs[1, 1])
        ax4.set_title("Индикаторы", color=COLORS["text"], fontsize=10, pad=8)
        ax4.set_xlim(0, 100)
        ax4.set_ylim(0, 4)
        ax4.axis("off")

        if finbert:
            sig_color = (COLORS["bull"] if finbert["label"] == "BULLISH" else
                         COLORS["bear"] if finbert["label"] == "BEARISH" else
                         COLORS["gold"])
        else:
            sig_color = (COLORS["bull"] if pct >= 60 else
                         COLORS["bear"] if pct <= 35 else
                         COLORS["gold"])
        ax4.barh([3.4], [pct],       height=0.3, color=sig_color)
        ax4.barh([3.4], [100 - pct], height=0.3, color=COLORS["border"], left=pct)
        sig_lbl = f"Уровень сигнала: {pct}%"
        if finbert:
            sig_lbl += (
                f"  (= {str(finbert.get('label', '')).upper()} "
                f"@ {str(finbert.get('confidence', '')).upper()})"
            )
        ax4.text(0, 3.75, sig_lbl, fontsize=8.5, color=COLORS["text"])

        macro = prices.get("MACRO", {})
        fng   = macro.get("fng", {}) if isinstance(macro, dict) else {}
        fv    = fng.get("val", "N/A")
        fs    = fng.get("status", "")
        if isinstance(fv, int):
            fng_color = (COLORS["bear"] if fv <= 25 else
                         COLORS["bull"] if fv >= 60 else
                         COLORS["gold"])
            ax4.barh([2.5], [fv],       height=0.3, color=fng_color)
            ax4.barh([2.5], [100 - fv], height=0.3, color=COLORS["border"], left=fv)
            ax4.text(0, 2.85, f"Fear & Greed: {fv}/100 ({fs})", fontsize=8.5, color=COLORS["text"])

        if finbert:
            score     = finbert["score"]
            label     = finbert["label"]
            conf      = finbert["confidence"]
            bar_val   = int((score + 1) / 2 * 100)
            sent_color = (COLORS["bull"] if label == "BULLISH" else
                          COLORS["bear"] if label == "BEARISH" else
                          COLORS["gold"])
            ax4.barh([1.6], [bar_val],       height=0.3, color=sent_color)
            ax4.barh([1.6], [100 - bar_val], height=0.3, color=COLORS["border"], left=bar_val)
            ax4.text(0, 1.95, f"FinBERT: {score:+.2f} {label} ({conf})",
                     fontsize=8.5, color=sent_color, fontweight="bold")

        if "VIX" in prices:
            vix_val   = prices["VIX"]["price"]
            vix_color = (COLORS["bear"] if vix_val > 30 else
                         COLORS["gold"] if vix_val > 20 else
                         COLORS["bull"])
            vix_label = ("Высокая" if vix_val > 30 else
                         "Умеренная" if vix_val > 20 else
                         "Низкая")
            ax4.text(0, 0.95, f"VIX: {vix_val:.2f} — {vix_label}",
                     fontsize=8.5, color=vix_color, fontweight="bold")

        if "BTC" in prices:
            # Реальный RSI из prices_dict (web_search.py:_compute_rsi). Раньше
            # регекс тащил желаемую цифру из любого места в тексте LLM, отсюда
            # прыжки RSI 14→60 за 1.5ч при движении цены на +0.12%.
            rsi_val = prices.get("BTC", {}).get("rsi_14d") if isinstance(prices.get("BTC"), dict) else None
            if rsi_val is None:
                # Fallback на старый regex — на случай если web_search почему-то
                # не прокинул rsi_14d. Строже регекс (RSI(14): X или RSI BTC: X),
                # чтобы не хватать цены/MA из соседних слов.
                rsi_m = re.search(
                    r"RSI\s*\(?\s*1[45]\s*\)?\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)",
                    report,
                    re.IGNORECASE,
                )
                if rsi_m:
                    try:
                        candidate = float(rsi_m.group(1))
                        if 0 <= candidate <= 100:
                            rsi_val = candidate
                    except ValueError:
                        pass
            if rsi_val is not None:
                rsi_color = (COLORS["bear"] if rsi_val > 70 else
                             COLORS["bull"] if rsi_val < 30 else
                             COLORS["text"])
                rsi_label = ("Перекуплен" if rsi_val > 70 else
                             "Перепродан" if rsi_val < 30 else
                             "Нейтрально")
                ax4.text(0, 0.35, f"RSI BTC(14d): {rsi_val:.1f} — {rsi_label}",
                         fontsize=8.5, color=rsi_color)

        fig.text(0.5, 0.01, "⚠️ Не является финансовым советом. AI-анализ. DYOR.",
                 ha="center", fontsize=7, color=COLORS["subtext"])

        return _to_bytes(fig)

    except Exception as e:
        logger.error(f"Chart error: {e}", exc_info=True)
        return None


def generate_russia_chart(russia_report: str):
    if not MATPLOTLIB_OK:
        return None

    try:
        _setup_dark_style()
        fig, axes = plt.subplots(1, 2, figsize=(10, 4), facecolor=COLORS["bg"])
        fig.suptitle("🇷🇺 RUSSIA EDGE — Анализ рисков и возможностей",
                     color=COLORS["gold"], fontsize=12, fontweight="bold", y=1.02)

        # Логируем для диагностики
        logger.info(f"Russia chart v4: report len={len(russia_report)}, "
                    f"has_green={'🟢' in russia_report}, has_red={'🔴' in russia_report}")

        opportunities = _parse_russia_items(russia_report, "🟢")
        risks         = _parse_russia_items(russia_report, "🔴")

        # Если 🟢 не нашёл — пробуем текстовый маркер
        if not opportunities:
            logger.warning("🟢 не найден — пробую текстовый маркер ВОЗМОЖНОСТИ")
            for alt_marker in ["ВОЗМОЖНОСТИ ДЛЯ РОССИЯН", "ВОЗМОЖНОСТИ:"]:
                if alt_marker in russia_report:
                    # Временно заменяем маркер
                    tmp = russia_report.replace(alt_marker, "🟢 " + alt_marker, 1)
                    opportunities = _parse_russia_items(tmp, "🟢")
                    if opportunities:
                        logger.info(f"Fallback маркер сработал: {len(opportunities)} items")
                        break

        if not risks:
            logger.warning("🔴 не найден — пробую текстовый маркер РИСКИ")
            for alt_marker in ["РИСКИ ДЛЯ РОССИЙСКОГО БИЗНЕСА", "РИСКИ:"]:
                if alt_marker in russia_report:
                    tmp = russia_report.replace(alt_marker, "🔴 " + alt_marker, 1)
                    risks = _parse_russia_items(tmp, "🔴")
                    if risks:
                        logger.info(f"Fallback риски сработал: {len(risks)} items")
                        break

        logger.info(f"Russia chart parsed: {len(opportunities)} opp, {len(risks)} risks")

        ax1 = axes[0]
        ax1.set_title("Возможности", color=COLORS["bull"], fontsize=10, pad=8)
        if opportunities:
            names   = [re.sub(r'[^\w\s\u0400-\u04FF:.,()-]', '', o["name"])[:22] for o in opportunities[:5]]
            ratings = [o["rating"] for o in opportunities[:5]]
            colors  = [COLORS["bull"] if r >= 3 else COLORS["gold"] for r in ratings]
            bars    = ax1.barh(range(len(names)), ratings, color=colors, height=0.6)
            ax1.set_yticks(range(len(names)))
            ax1.set_yticklabels(names, fontsize=8)
            ax1.set_xlim(0, 3.5)
            ax1.set_xlabel("Уверенность", fontsize=8)
            ax1.set_xticks([1, 2, 3])
            ax1.set_xticklabels(["НИЗКАЯ", "СРЕДНЯЯ", "ВЫСОКАЯ"], fontsize=7)
            for bar, r in zip(bars, ratings):
                ax1.text(bar.get_width() + 0.05,
                         bar.get_y() + bar.get_height()/2,
                         "*" * r, va="center", fontsize=8, color=COLORS["gold"])
        else:
            ax1.text(0.5, 0.5, "Данные\nне найдены",
                     ha="center", va="center", transform=ax1.transAxes,
                     color=COLORS["subtext"], fontsize=10)

        ax2 = axes[1]
        ax2.set_title("Риски", color=COLORS["bear"], fontsize=10, pad=8)
        if risks:
            names   = [re.sub(r'[^\w\s\u0400-\u04FF:.,()-]', '', r["name"])[:22] for r in risks[:5]]
            ratings = [r["rating"] for r in risks[:5]]
            colors  = [COLORS["bear"] if rv >= 3 else COLORS["gold"] for rv in ratings]
            bars    = ax2.barh(range(len(names)), ratings, color=colors, height=0.6)
            ax2.set_yticks(range(len(names)))
            ax2.set_yticklabels(names, fontsize=8)
            ax2.set_xlim(0, 3.5)
            ax2.set_xlabel("Вероятность", fontsize=8)
            ax2.set_xticks([1, 2, 3])
            ax2.set_xticklabels(["НИЗКАЯ", "СРЕДНЯЯ", "ВЫСОКАЯ"], fontsize=7)
            for bar, rv in zip(bars, ratings):
                ax2.text(bar.get_width() + 0.05,
                         bar.get_y() + bar.get_height()/2,
                         "⚠️" if rv >= 3 else "!", va="center",
                         fontsize=8, color=COLORS["bear"])
        else:
            ax2.text(0.5, 0.5, "Данные\nне найдены",
                     ha="center", va="center", transform=ax2.transAxes,
                     color=COLORS["subtext"], fontsize=10)

        for ax in axes:
            ax.invert_yaxis()

        plt.tight_layout()
        return _to_bytes(fig)

    except Exception as e:
        logger.error(f"Russia chart error: {e}", exc_info=True)
        return None


# ── Торговый план — PNG-таблица (PR #34) ────────────────────────────────────
# Чтобы юзеру не приходилось читать 11 строк подряд в Telegram, рендерим
# план таблицей. Layout: 2 секции (Крипта / Макро), 4 столбца на актив:
# название, текущая цена, LONG-trigger (выше), SHORT-trigger (ниже).
# Цветовая дифференциация: цена выше обеих MA — зелёный фон, ниже обеих —
# красный, sandwich — нейтральный. Так глаз сразу видит, где сейчас актив.

_TRADING_PLAN_PNG_GROUPS = [
    ("КРИПТО", [
        ("BTC",     "BTC"),
        ("ETH",     "ETH"),
        ("SOL",     "SOL"),
        ("BNB",     "BNB"),
        ("XRP",     "XRP"),
    ]),
    ("МАКРО", [
        ("SPX",     "S&P 500"),
        ("NDX",     "Nasdaq 100"),
        ("GOLD",    "Gold"),
        ("OIL_WTI", "WTI Oil"),
        ("DXY",     "DXY"),
        ("VIX",     "VIX"),
    ]),
]


def _fmt_plan_money(value) -> str:
    """Зеркало main._fmt_money_compact — адаптивная точность.

    Дублируем минимально, чтобы chart_generator не зависел от main.
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    abs_v = abs(v)
    if abs_v < 1:
        return f"{v:,.4f}"
    elif abs_v < 100:
        return f"{v:,.2f}"
    else:
        return f"{v:,.0f}"


def generate_trading_plan_png(prices: dict | None, plans: list[dict] | None = None):
    """Рендер торгового плана как PNG-таблицы для кнопки «Показать таблицу».

    Источник истины — `prices_dict` (MA50/MA200 из web_search.py). `plans`
    используется только чтобы понять, какие активы Synth включил в план.
    """
    if not MATPLOTLIB_OK:
        return None
    if not prices:
        return None

    _setup_dark_style()

    # Нормализация символов из plans — тот же набор алиасов, что в main.py.
    aliases = {
        "BITCOIN": "BTC", "BTCUSD": "BTC", "BTCUSDT": "BTC",
        "ETHEREUM": "ETH", "ETHUSD": "ETH", "ETHUSDT": "ETH",
        "SOLANA": "SOL", "SOLUSDT": "SOL",
        "BNBUSDT": "BNB", "XRPUSDT": "XRP",
        "S&P": "SPX", "S&P500": "SPX", "SP500": "SPX", "SPY": "SPX", "^GSPC": "SPX",
        "NASDAQ": "NDX", "QQQ": "NDX", "^NDX": "NDX",
        "XAU": "GOLD", "GLD": "GOLD", "XAUUSD": "GOLD",
        "OILWTI": "OIL_WTI", "WTI": "OIL_WTI", "USO": "OIL_WTI",
        "CL=F": "OIL_WTI", "OIL": "OIL_WTI",
        "DX-Y.NYB": "DXY", "^VIX": "VIX",
    }
    plan_symbols: set[str] = set()
    for plan in plans or []:
        raw = (plan.get("symbol") or plan.get("label") or "")
        sym = aliases.get(str(raw).upper().strip(), str(raw).upper().strip())
        if sym:
            plan_symbols.add(sym)

    rows: list[tuple[str, str, dict]] = []
    for group_title, assets in _TRADING_PLAN_PNG_GROUPS:
        any_in_group = False
        for key, label in assets:
            if plan_symbols and key not in plan_symbols:
                continue
            entry = prices.get(key)
            if not isinstance(entry, dict):
                continue
            if entry.get("price") is None or entry.get("ma50") is None or entry.get("ma200") is None:
                continue
            if not any_in_group:
                rows.append((group_title, "__header__", {}))
                any_in_group = True
            rows.append((group_title, label, entry))

    if not rows:
        return None

    # Высота: 0.55" на строку (заголовок группы + N активов), +1.2" на title.
    height = 1.4 + 0.55 * len(rows)
    fig, ax = plt.subplots(figsize=(10.5, height), facecolor=COLORS["bg"])
    ax.set_facecolor(COLORS["surface"])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    now_hm = datetime.now().strftime("%d.%m %H:%M UTC")
    ax.text(0.5, 0.98, "ТОРГОВЫЙ ПЛАН",
            transform=ax.transAxes, ha="center", va="top",
            fontsize=15, fontweight="bold", color=COLORS["gold"])
    ax.text(0.5, 0.945, f"{now_hm} · ждём пробоя MA50 / MA200",
            transform=ax.transAxes, ha="center", va="top",
            fontsize=8, color=COLORS["subtext"])

    # Шапка таблицы.
    header_y = 0.895
    ax.text(0.04, header_y, "Актив",       transform=ax.transAxes, fontsize=9, color=COLORS["subtext"], fontweight="bold")
    ax.text(0.30, header_y, "Цена",        transform=ax.transAxes, fontsize=9, color=COLORS["subtext"], fontweight="bold")
    ax.text(0.48, header_y, "▲ выше → LONG",  transform=ax.transAxes, fontsize=9, color=COLORS["bull"], fontweight="bold")
    ax.text(0.74, header_y, "▼ ниже → SHORT", transform=ax.transAxes, fontsize=9, color=COLORS["bear"], fontweight="bold")
    ax.plot([0.02, 0.98], [header_y - 0.02, header_y - 0.02], color=COLORS["border"], linewidth=0.8, transform=ax.transAxes)

    # Каждая строка занимает row_h в относительных координатах.
    row_h = (header_y - 0.04) / len(rows)
    for idx, (group, label, entry) in enumerate(rows):
        y = header_y - 0.04 - row_h * (idx + 0.5)
        if label == "__header__":
            ax.text(0.04, y, f"═══ {group} ═══",
                    transform=ax.transAxes, fontsize=10,
                    color=COLORS["blue"], fontweight="bold", va="center")
            continue
        price = float(entry["price"])
        ma50 = float(entry["ma50"]); ma200 = float(entry["ma200"])
        up_level, up_tag = (ma200, "MA200") if ma200 >= ma50 else (ma50, "MA50")
        dn_level, dn_tag = (ma50, "MA50") if ma200 >= ma50 else (ma200, "MA200")

        # Цветовая подсветка статуса: выше обоих — зелёный, ниже — красный.
        if price >= max(ma50, ma200):
            status_color = COLORS["bull"]
        elif price <= min(ma50, ma200):
            status_color = COLORS["bear"]
        else:
            status_color = COLORS["text"]

        ax.text(0.04, y, label, transform=ax.transAxes, fontsize=10,
                color=COLORS["text"], fontweight="bold", va="center")
        ax.text(0.30, y, f"${_fmt_plan_money(price)}",
                transform=ax.transAxes, fontsize=10,
                color=status_color, va="center")
        ax.text(0.48, y, f"${_fmt_plan_money(up_level)} ({up_tag})",
                transform=ax.transAxes, fontsize=10,
                color=COLORS["bull"], va="center")
        ax.text(0.74, y, f"${_fmt_plan_money(dn_level)} ({dn_tag})",
                transform=ax.transAxes, fontsize=10,
                color=COLORS["bear"], va="center")

    return _to_bytes(fig)


def is_available() -> bool:
    return MATPLOTLIB_OK


# ─────────────────────────── ПАМП-график (фича PUMP) ─────────────────────
def generate_pump_chart(asset, closes, *, price_from=None, price_to=None,
                        pump_pct=None, window_min=30):
    """Мини-график цены для памп-алерта (стиль сканера).

    Чёрная линия цены + пунктирные референсные уровни. Возвращает BytesIO (PNG)
    или None если matplotlib недоступен / данных нет. Non-fatal.
    """
    if not MATPLOTLIB_OK:
        return None
    pts = [float(c) for c in (closes or []) if c is not None]
    if len(pts) < 2:
        return None
    try:
        _setup_dark_style()
        fig, ax = plt.subplots(figsize=(8, 4))
        xs = list(range(len(pts)))
        up = (pts[-1] >= pts[0])
        line_color = COLORS["bull"] if up else COLORS["bear"]
        ax.plot(xs, pts, color=line_color, linewidth=2.0, zorder=3)
        ax.fill_between(xs, pts, min(pts), color=line_color, alpha=0.08, zorder=1)
        # якорь и текущая точка
        ax.scatter([xs[-1]], [pts[-1]], color=line_color, s=40, zorder=4)
        # референсные уровни (min / max / from)
        for lvl in (min(pts), max(pts)):
            ax.axhline(lvl, color=COLORS["border"], linestyle="--",
                       linewidth=0.8, alpha=0.7, zorder=2)
        if price_from is not None:
            ax.axhline(float(price_from), color=COLORS["blue"], linestyle=":",
                       linewidth=1.0, alpha=0.8, zorder=2)
        title = str(asset)
        if pump_pct is not None:
            title += f"   +{float(pump_pct):.2f}%  /  {int(window_min)}мин"
        ax.set_title(title, color=COLORS["text"], fontsize=13, fontweight="bold",
                     loc="left")
        if price_from is not None and price_to is not None:
            ax.set_xlabel(f"{price_from:.6g}  →  {price_to:.6g}",
                          color=COLORS["subtext"], fontsize=10)
        ax.set_xticks([])
        ax.margins(x=0.01)
        return _to_bytes(fig)
    except Exception as e:  # noqa: BLE001
        logger.warning("generate_pump_chart failed: %s", e)
        try:
            plt.close("all")
        except Exception:
            pass
        return None
