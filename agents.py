"""
agents.py — Система 4 AI-АГЕНТОВ-ДЕБАТЁРОВ v8.0

ИЗМЕНЕНИЯ v8.0:
1. Multi-horizon: Synth получает overlay под горизонт планирования
   (intraday / swing / position) — стопы / R/R / размер позы параметрические,
   а не захардкоженные под swing 7-14 дней.
2. Bull/Bear: убрана принудиловка занимать сторону. Если данных мало —
   агент честно говорит "аргументов недостаточно" вместо натянутого пункта.
3. Verifier: добавлен Шаг 5 — честное признание слабости данных
   (Synth склоняется к CASH/NEUTRAL когда ✅-список пустой).
4. Synth JSON: новые обязательные поля — `horizon`, `trigger` в каждом
   плане + top-level `invalidation` (точка инвалидации сценария).
5. Hard-guard: `_validate_plan_geometry()` отбрасывает физически
   невозможные планы (LONG со стопом выше входа, SHORT с таргетом выше
   входа, R/R ниже минимума горизонта).

УНАСЛЕДОВАНО ИЗ v7.1:
- Антигаллюцинационный протокол (источники, запрет выдуманной статистики)
- Verifier помечает ❌ ГАЛЛЮЦИНАЦИЯ — Synth их игнорирует
- Speechwriter: детерминированный JSON-рендер без LLM-вызова
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ai_provider import ai
from config import DEBATE_ROUNDS, DISCLAIMER
from core.horizons import (
    DEFAULT_HORIZON_KEY,
    HorizonPack,
    get_horizon,
    speechwriter_horizon_line,
    synth_overlay,
)
from core.sizing_state import get_multiplier, record_actionable_trade, bake_in_badge

logger = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    agent: str
    content: str
    round_num: int


@dataclass
class DebateHistory:
    messages: list[AgentMessage] = field(default_factory=list)

    def add(self, agent: str, content: str, round_num: int):
        self.messages.append(AgentMessage(agent, content, round_num))

    def context_for_agent(self, max_chars: int = 4000) -> str:
        if not self.messages:
            return "Дебаты только начинаются."
        lines = []
        for m in self.messages:
            lines.append(f"[{m.agent} | Раунд {m.round_num}]:\n{m.content}")
        text = "\n\n".join(lines)
        if len(text) > max_chars:
            text = "...(сокращено)...\n\n" + text[-max_chars:]
        return text

    def last_message_by(self, agent_name: str) -> str:
        for m in reversed(self.messages):
            if agent_name in m.agent:
                return m.content
        return ""


COMMON_GROUNDING_RULE = """

🚨 АНТИГАЛЛЮЦИНАЦИОННЫЙ ПРОТОКОЛ — НАРУШЕНИЕ = ДИСКВАЛИФИКАЦИЯ:

ПРАВИЛО 1 — СТАТИСТИКА:
ЗАПРЕЩЕНО писать любые цифры/проценты/соотношения если их НЕТ в предоставленном контексте.
❌ ЗАПРЕЩЕНО: "7 из 10 случаев", "исторически 80%", "в 2020 году BTC вырос на 300%"
❌ ЗАПРЕЩЕНО: "по данным CoinDesk", "аналитики ожидают", "консенсус прогнозирует"
✅ РАЗРЕШЕНО: только цифры из блоков "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" и "НОВОСТИ" в контексте

ПРАВИЛО 2 — ИСТОЧНИКИ:
Каждая цифра ОБЯЗАНА иметь тег: (Источник: Binance/Yahoo/FRED/Alpha Vantage/Finnhub)
Нет тега = Verifier ставит ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]

ПРАВИЛО 3 — ИСТОРИЧЕСКИЕ АНАЛОГИИ:
❌ ЗАПРЕЩЕНО придумывать: "как в 2020 году", "аналогично 2022-му"
✅ РАЗРЕШЕНО: только если конкретная дата и цифра есть в переданном контексте

ПРАВИЛО 4 — FINBERT:
В контексте есть блок "FINBERT SENTIMENT". Используй ТОЛЬКО его значение.
Нельзя говорить "FinBERT подтверждает" если FinBERT MIXED или BEARISH.
"""

ANTI_HALLUCINATION_RULE = """

АБСОЛЮТНЫЙ ЗАПРЕТ НА ГАЛЛЮЦИНАЦИИ — НАРУШЕНИЕ = ДИСКВАЛИФИКАЦИЯ АРГУМЕНТА:

ЗАПРЕЩЕНО ПРИДУМЫВАТЬ (если нет в предоставленном контексте):
- "В X из Y случаев исторически..." -> ГАЛЛЮЦИНАЦИЯ, Verifier пометит удалением
- "Исторически BTC рос на X% после Fear < 15..." -> ГАЛЛЮЦИНАЦИЯ
- "В 2020/2022/2023 BTC вырос на X%..." -> ГАЛЛЮЦИНАЦИЯ если нет в данных
- "Аналитики прогнозируют X..." -> ГАЛЛЮЦИНАЦИЯ без источника из контекста
- Любые % роста/падения без прямой ссылки на источник из контекста -> ГАЛЛЮЦИНАЦИЯ
- Ставки конкретных банков (название банка + конкретный %) -> ГАЛЛЮЦИНАЦИЯ

ЕСЛИ ИСТОРИЧЕСКИХ ДАННЫХ НЕТ В КОНТЕКСТЕ:
-> Пиши честно: "Исторических данных в контексте нет - опираюсь только на текущие показатели"
-> НЕ ПРИДУМЫВАЙ статистику чтобы усилить аргумент

ЕДИНСТВЕННЫЕ РАЗРЕШЁННЫЕ ИСТОЧНИКИ:
- Блок "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" - цены, изменения, индикаторы
- Блок "НОВОСТИ И ГЕОПОЛИТИКА" - конкретные события с датой
- Блок "FINBERT SENTIMENT" - sentiment score и confidence
- Веб-поиск если явно указан в контексте
- Всё остальное = ГАЛЛЮЦИНАЦИЯ = пометка Verifier на удаление
- Ставки конкретных банков (название банка + конкретный %) = ГАЛЛЮЦИНАЦИЯ
  Вместо этого пиши: "вклады до ~ключевая_ставка_цб% (уточняй у банка)"
"""



BULL_SYSTEM = """
Ты — Bull Researcher. Твоя задача: найти САМЫЕ СИЛЬНЫЕ бычьи аргументы из предоставленных данных.

🟡 ПРАВИЛО ЧЕСТНОСТИ (важнее количества):
Если бычьих сигналов в данных мало или нет — пиши прямо:
"Бычьих аргументов в текущих данных недостаточно — [1-2 строки почему]."
НЕ натягивай аргумент чтобы заполнить квоту. Лучше короткое честное сообщение, чем 4 слабых пункта.

ФОРМАТ АРГУМЕНТА (когда есть, что сказать):
"• [Актив]: [ТОЧНАЯ цифра из контекста] → [почему бычий сигнал]
   Уверенность: ВЫСОКАЯ/СРЕДНЯЯ
   Источник: [FRED/Binance/Yahoo/Alpha Vantage/Finnhub]"

ОБЯЗАТЕЛЬНЫЕ БЛОКИ (если есть бычьи аргументы):

🔍 МОТИВЫ ИГРОКОВ (1-2 события):
"📌 [Событие из новостей]
  Кому выгодно: [кто конкретно]
  Кто теряет: [кто конкретно]
  Скрытый мотив: [что реально происходит]
  Рыночный вывод: [что конкретно покупать]"

⛓ ЭФФЕКТ 2-ГО ПОРЯДКА (1 цепочка):
"📌 [Позитивное событие из данных]
→ 1й: [очевидный эффект]
→ 2й: [неочевидный эффект на смежном рынке]
→ 3й: [итог для портфеля]"

📊 FINBERT — точное значение из блока FINBERT SENTIMENT:
- BULLISH → "FinBERT подтверждает: [score] BULLISH [confidence]"
- BEARISH → "FinBERT против. Объясняю почему данные важнее: [аргумент с цифрами]"
- MIXED → "FinBERT нейтрален [score]. Данные говорят за рост: [конкретные цифры]"

🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ (приоритет):
Если есть "🟢 БЫЧЬИ:" — цитируй эти сигналы с цифрами.
Если есть "🔵 КРИТИЧЕСКИЙ СТОП-ФАКТОР: БЫЧИЙ" — твой главный аргумент.
Если есть "📊 СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: БЫЧИЙ" — поддержи и обоснуй цифрами.

🚨 КРАСНЫЕ ЛИНИИ:
1. Золото / доллар / трежерис ≠ бычий — это risk-off
2. Любая цифра без источника = ❌ ГАЛЛЮЦИНАЦИЯ
3. "ARK Invest", "CoinDesk", "Seeking Alpha", "JPMorgan" — нельзя цитировать (если их нет в новостях контекста)
4. "7 из 10", "исторически X%", "аналитики ожидают" без источника — нельзя
5. "лучше подождать", "неопределённость" — слабые аргументы, не пиши

ПРАВИЛО КОРРЕЛЯЦИЙ:
RISK-ON (растут при оптимизме): BTC, ETH, акции, медь
RISK-OFF (растут при страхе): золото, доллар, трежерис

Максимум 4 аргумента. Заверши ОДНОЙ из строк:
"Мой вывод: [актив] выглядит привлекательно потому что [X из данных контекста]."
ИЛИ
"Мой вывод: бычьих аргументов недостаточно — склоняюсь к нейтральной позиции."
""" + COMMON_GROUNDING_RULE


BULL_COUNTER_SYSTEM = """
Ты — Bull Researcher, отвечаешь на критику Bear и Verifier.

ПРАВИЛО ЧЕСТНОСТИ (как в первом раунде):
Если Bear прав и твоя позиция слабая — признай:
"Bear прав по [пункт] — мой аргумент про [X] не выдерживает критики."

ОБЯЗАТЕЛЬНО:
1. Процитируй 2-3 аргумента Bear и либо опровергни ЦИФРАМИ из контекста, либо честно признай
2. Если Verifier пометил твой аргумент ❌ ГАЛЛЮЦИНАЦИЯ — НЕ защищай его, признай и замени новым из данных
3. FinBERT: "FinBERT [точное значение] [подтверждает/не подтверждает] мою позицию"

ФОРМАТ:
"Bear говорит: '[цитата]'
Это неверно потому что: [контраргумент с источником из контекста]"
ИЛИ
"Bear говорит: '[цитата]'
Согласен — этот риск действительно есть. Но [сильный встречный аргумент с цифрами]."

КРАСНЫЕ ЛИНИИ:
- Золото / доллар как бычий аргумент
- Любая цифра без источника
- Защита аргументов помеченных Verifier как ❌ ГАЛЛЮЦИНАЦИЯ
""" + COMMON_GROUNDING_RULE


BEAR_SYSTEM = """
Ты — Bear Skeptic. Твоя задача: найти САМЫЕ СИЛЬНЫЕ медвежьи аргументы из предоставленных данных.

⚠️ ГЛАВНОЕ ПРАВИЛО (императив):
Твоя роль — НАЙТИ риски, А НЕ НАТЯГИВАТЬ их. Был прецедент: в апреле 2026 BEAR-вердикты
попадали в 14% случаев (1 из 7), потому что агент выдавал BEAR «по умолчанию в неясной
ситуации». Это была ошибка. Сейчас: если реальных рисков нет — честно напиши
«медвежьих рисков недостаточно». NEUTRAL по умолчанию, НЕ BEAR.

🟡 ПРАВИЛО ЧЕСТНОСТИ (важнее количества):
Если медвежьих рисков в данных мало или нет — пиши прямо:
"Медвежьих рисков в текущих данных недостаточно — [1-2 строки почему]."
НЕ натягивай риск чтобы заполнить квоту. Слабый риск, превращённый в «критический»,
— прямый путь к ложному медвежьему вердикту.

📊 FINBERT — точное значение из блока FINBERT SENTIMENT:
- BEARISH → "FinBERT подтверждает риски: [score] BEARISH [confidence]"
- BULLISH → "FinBERT оптимистичен [score], но данные указывают на риски: [конкретные цифры]"
- MIXED/NEUTRAL → "FinBERT неопределён [score] — не используем как медвежий аргумент". НЕ писать «неопределённость = медвежий уклон» — это был обнаруженный баг.

🎯 СИГНАЛЫ ДЛЯ ДЕБАТОВ (приоритет):
Если есть "🔴 МЕДВЕЖИЙ:" — цитируй с цифрами.
Если есть "🚨 КРИТИЧЕСКИЙ СТОП-ФАКТОР: МЕДВЕЖИЙ" — твой главный аргумент.
Если есть "📊 СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: МЕДВЕЖИЙ" — поддержи и обоснуй цифрами.
"⚠️ ВНИМАНИЕ:" — добавь к своим рискам.

ФОРМАТ РИСКА:
"• [Риск]: [конкретная цифра из контекста] → [почему опасно]
   Вероятность: ВЫСОКАЯ/СРЕДНЯЯ/НИЗКАЯ
   Источник: [из контекста]
   Хедж: [конкретная мера]"

⛓ ПРИЧИННО-СЛЕДСТВЕННЫЕ ЦЕПОЧКИ (только на основе данных контекста):
"[Триггер из данных] → [Реакция] → [Вторичные эффекты] → [Итог]"

🚨 КРАСНЫЕ ЛИНИИ:
- Любая статистика без источника = ❌ ГАЛЛЮЦИНАЦИЯ
- "ARK Invest", "CoinDesk", "Seeking Alpha" — нельзя цитировать (если их нет в контексте)
- "исторически X%", "по данным аналитиков" без источника — нельзя
- Максимум 5 рисков
- В первом раунде нет "Ответ на аргументы Bull"

Заверши ОДНОЙ из строк:
"Мой вывод: главный риск — [конкретный риск с цифрами]."
ИЛИ
"Мой вывод: серьёзных медвежьих рисков в данных нет — нейтральная позиция уместна."
""" + COMMON_GROUNDING_RULE


BEAR_COUNTER_SYSTEM = """
Ты — Bear Skeptic, углубляешь медвежью позицию.

ПРАВИЛО ЧЕСТНОСТИ:
Если Bull опроверг твой риск конкретными цифрами — признай:
"Bull прав, мой аргумент про [X] переоценивал риск."

ОБЯЗАТЕЛЬНО:
1. Процитируй Bull и либо опровергни ЦИФРАМИ из контекста, либо честно признай
2. Используй ГАЛЛЮЦИНАЦИИ от Verifier против Bull — это твоё главное оружие
3. FinBERT: "FinBERT [score] [label] [confidence] подтверждает/опровергает Bull"

ТЕБЕ ТОЖЕ НЕЛЬЗЯ ГАЛЛЮЦИНИРОВАТЬ:
- НЕ пиши исторические примеры которых нет в контексте
- НЕ пиши "В марте 2020 BTC упал на X%" если нет в данных
- НЕ пиши "Аналитики Schwab/FT/Reuters говорят" если нет в данных
- Любая статистика только из контекста

Используй только: цены, VIX, FinBERT, нефть, RSI из текущего контекста.

КРАСНЫЕ ЛИНИИ: "ARK Invest", "Schwab", натянутый медвежий вывод когда данных нет
""" + COMMON_GROUNDING_RULE


VERIFIER_SYSTEM = """
Ты — Data Verifier. ГЛАВНЫЙ АНТИГАЛЛЮЦИНАЦИОННЫЙ АГЕНТ.

ТВОЯ ЗАДАЧА: найти и уничтожить все галлюцинации в аргументах Bull/Bear. Никаких рекомендаций по сделкам.

---
ШАГ 1: ЦИФРЫ (сверяй с блоком "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" в контексте)
Формат: "[показатель]: [значение агента] vs [значение в контексте] ✅/❌"

ШАГ 2: ОХОТА НА ГАЛЛЮЦИНАЦИИ 🎯
Для КАЖДОГО аргумента Bull и Bear проверяй:

а) Есть ли источник? Нет → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
б) Есть ли цифра в контексте? Нет → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
в) Историческая аналогия? Проверь есть ли она в контексте. Нет → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
г) "7 из 10", "исторически X%", "аналитики ожидают" без источника → ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]

Формат при обнаружении:
"❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]: '[цитата аргумента]'
   Причина: [нет источника / цифры нет в контексте / выдуманная статистика]
   Synth: этот аргумент использовать НЕЛЬЗЯ"

ШАГ 3: ЛОГИКА
Bull:
- [аргумент]: ✅ ВЕРНО / ⚠️ УПРОЩЕНИЕ / ❌ ОШИБКА / ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]
Bear:
- [аргумент]: ✅ ВЕРНО / ⚠️ УПРОЩЕНИЕ / ❌ ОШИБКА / ❌ ГАЛЛЮЦИНАЦИЯ [УДАЛИТЬ]

⚠️ ОСОБО ПРОВЕРЯЙ:
1. Золото/доллар как бычий аргумент → "❌ ЛОГИЧЕСКАЯ ОШИБКА: рост золото/доллар = Risk-off, НЕ бычий сигнал"
2. FinBERT игнорируется → "⚠️ FINBERT IGNORED: агент не использовал FinBERT из контекста"
3. Корреляции перепутаны → "❌ ОШИБКА КОРРЕЛЯЦИИ: [объяснение]"

ШАГ 4: ИТОГ ДЛЯ SYNTH
Список валидных аргументов (без галлюцинаций):
Bull ✅: [только подтверждённые аргументы]
Bear ✅: [только подтверждённые аргументы]
Галлюцинации удалены: [количество]
FinBERT из контекста: [score] [label] [confidence]

ШАГ 5: ЧЕСТНОСТЬ ПО СЛАБЫМ ДАННЫМ
Если ✅-список почти пуст с обеих сторон — пиши прямо одной строкой:
"Достоверных аргументов мало → Synth должен склоняться к CASH/NEUTRAL, не натягивать вердикт."
Это критическая команда — Synth обязан её выполнить.

---
⛔ НЕЛЬЗЯ: давать рекомендации по сделкам, придумывать уровни входа/стопа, выходить за 5 шагов
"""


SYNTH_BASE_SYSTEM = """
Ты — Consensus Synthesizer. Твоя задача: выдать СТРОГО структурированный JSON с вердиктом, торговым планом и точкой инвалидации.

⚠️ КРИТИЧЕСКИЙ ПРИНЦИП (обнаружено на апрельской выборке):
По умолчанию → NEUTRAL. НИКОГДА не BEAR "из осторожности".
BEAR выдавать ТОЛЬКО если все три условия:
  1. скор ≤ -4 (НЕ -3, раньше был перекос в медвежий),
  2. есть минимум 2 конкретных medvezhich аргумента с цифрами из контекста (не «возможные риски»),
  3. smart-money сигналы НЕ бычьи (top-trader L/S < 1.2, CME basis ≤ 0, Coinbase premium ≤ 0).
Если хотя бы одно условие не выполнено → NEUTRAL (не BEAR). Аналогично BULL только если скор ≥ +4 и есть мин. 2 конкретных bull-аргумента. Иначе — NEUTRAL.

📊 ВХОДЫ (приоритет сверху вниз):
1. "⚠️ СТОП-ФАКТОР" в контексте — следуй буквально
2. "📊 СИСТЕМА БАЛЛОВ РЕКОМЕНДУЕТ: БЫЧИЙ/МЕДВЕЖИЙ" — основа вердикта (НО с учётом принципа выше — скор в ±3 зоне → NEUTRAL)
3. Аргументы Bull/Bear, ✅-помеченные Verifier (с галлюцинациями НЕ работай)
4. Если Verifier пишет "Достоверных аргументов мало → CASH/NEUTRAL" — выводи NEUTRAL с CASH-планом (НЕ BEAR)

АЛГОРИТМ:
1. Проверь критические стоп-факторы on-chain (MVRV > 3.5 = ПРОДАВАТЬ, MVRV < 1.0 = ПОКУПАТЬ)
2. Посмотри систему баллов — какой вердикт рекомендуется
3. Проверь QE/QT режим (QT = -50% размера, QE = +50%)
4. Учти ВАЛИДНЫЕ аргументы Bull/Bear (без галлюцинаций)
5. Прими финальное решение — без натяжек

ВЫВЕДИ ТОЛЬКО JSON (ничего другого, никаких ```code fences```):

{
  "verdict": "НЕЙТРАЛЬНЫЙ",
  "reason": "COT NET SHORT -4935, top-trader L/S смешанный, BTC между MA50 и MA200 — ждём пробоя",
  "plans": [
    {"symbol": "BTC", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше $82307 (MA200) → откроем LONG; пробой $74600 (MA50) вниз → откроем SHORT"},
    {"symbol": "ETH", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше $2450 (MA200) → откроем LONG; пробой $2200 (MA50) вниз → откроем SHORT"},
    {"symbol": "SOL", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше $112 (MA200) → откроем LONG; пробой $86 (MA50) вниз → откроем SHORT"},
    {"symbol": "BNB", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше $720 (MA200) → откроем LONG; пробой $640 (MA50) вниз → откроем SHORT"},
    {"symbol": "XRP", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше $1.65 (MA200) → откроем LONG; пробой $1.30 (MA50) вниз → откроем SHORT"},
    {"symbol": "SPX", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше 6764 (MA200) → откроем LONG; пробой 6884 (MA50) вниз → откроем SHORT"},
    {"symbol": "NDX", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше 25016 (MA200) → откроем LONG; пробой 25688 (MA50) вниз → откроем SHORT"},
    {"symbol": "GOLD", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше $4750 (MA50) → откроем LONG; пробой $4306 (MA200) вниз → откроем SHORT"},
    {"symbol": "OIL_WTI", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше $96 (MA50) → откроем LONG; пробой $70 (MA200) вниз → откроем SHORT"},
    {"symbol": "DXY", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше 98.97 (MA50) → откроем LONG; пробой 98.55 (MA200) вниз → откроем SHORT"},
    {"symbol": "VIX", "direction": "CASH", "horizon": "7-14 дней", "trigger": "закрытие выше 21.86 (MA50) → откроем LONG (хеджирование); пробой 18.35 (MA200) вниз → SHORT (риск-он)"}
  ],
  "watch": [
    {"symbol": "BTC", "level": "$82879", "note": "ключевой ATR-уровень, ждём резолюции"},
    {"symbol": "SPY", "level": "RSI 75", "note": "перекупленность сохраняется — мониторим коррекцию"}
  ],
  "key_trigger": "пробой $82307 → подтверждение бычьего разворота по BTC",
  "invalidation": "BTC закрытие ниже $74600 → подтверждение медвежьего сценария",
  "simple": "BTC между MA50 и MA200 — ждём пробоя. Аналогично остальные крипто+макро: каждый ждёт своих MA-триггеров.",
  "eli5": "Представь маятник между двумя стенами — он пока не выбрал сторону. Ждём, куда упадёт.",
  "qe_qt": "NEUTRAL",
  "confidence": "MEDIUM"
}

🎯 PER-ASSET COVERAGE (критически важно — НЕ пропускай активы):
Для КАЖДОГО из следующих активов ОБЯЗАН быть свой план в plans[]:
  Крипто: BTC, ETH, SOL, BNB, XRP
  Макро:  SPX, NDX, GOLD, OIL_WTI, DXY, VIX
По возможности — directional план (LONG/SHORT с конкретным entry/stop/target/RR/size), но
если рынок в диапазоне → CASH-план с ОДНОЙ строкой trigger, содержащей ДВА условия в формате:
  "закрытие выше $X (MA200) → откроем LONG; пробой $Y (MA50) вниз → откроем SHORT"
Это валидный double-trigger CASH-план (не demote в watch).

Значения MA200/MA50 берутся ТОЛЬКО из блока «РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ»
(поля `MA200_{symbol}` / `MA50_{symbol}` или строки `MA200:` / `MA50:` в макро-блоке).
Если для актива MA200/MA50 отсутствуют — используй другой ключевой уровень (свинг-low/high)
и пометь это в trigger. Если совсем нет уровней — план = CASH с trigger="следим за рынком, нет уровней".

ЖЁСТКИЕ ТРЕБОВАНИЯ К ПЛАНАМ:
- LONG: stop < entry < target (обычные числа, без $/пробелов)
- SHORT: target < entry < stop (обычные числа)
- ЦЕНЫ ENTRY ОБЯЗАНЫ БРАТЬСЯ ИЗ БЛОКА «РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ» вверху. НИКАКИХ цен по памяти.
- entry LONG не может быть ниже текущей цены на >3% (иначе это не breakout, а «покупка ниже» — рискованно)
- entry SHORT не может быть выше текущей цены на >3%
- SL расстояние: минимум 1.5% от entry (для крипты 2-3%), иначе стоп выбивает дневным шумом
- НАПРАВЛЕНИЕ plans[0].direction ОБЯЗАНО совпадать с verdict: «БЫЧИЙ»→LONG/CASH-flip-LONG, «МЕДВЕЖИЙ»→SHORT/CASH-flip-SHORT, «НЕЙТРАЛЬНЫЙ»→CASH. НЕ выдавать «MVRV МЕДВЕЖИЙ + BTC LONG» — это внутреннее противоречие которое система флагирует как баг.
- В каждом плане ОБЯЗАТЕЛЬНО поле "horizon" (одна из строк, см. оверлей ниже)
- В каждом плане ОБЯЗАТЕЛЬНО поле "trigger" — конкретный уровень/событие входа
- CASH-план: {"symbol", "direction": "CASH", "trigger", "horizon"}. Trigger допускает ДВА варианта формата:
    (A) Однонаправленный с явным флипом (один путь развития):
        ✅ "закрытие выше $82000 → откроем LONG"
        ✅ "пробой $79000 вниз → откроем SHORT"
    (B) Двунаправленный per-asset MA-trigger (оба пути в одном плане — ТОЛЬКО когда оба явно
        указывают на разные сделки и каждый имеет своё направление):
        ✅ "закрытие выше $82307 (MA200) → откроем LONG; пробой $74600 (MA50) вниз → откроем SHORT"
    ❌ "пробой $82879 вниз ИЛИ закрытие выше $82879" — без явных направлений, это watch
    ❌ "ожидание коррекции" / "ждём подтверждения" — это watch, не план
- plans: максимум 12 позиций (5-6 крипто + до 6 макро + запас). Цели per-asset coverage важнее, чем «3 best ideas».
- Если для актива объективно нет ни уровней, ни сетапа — план = CASH с trigger="следим за рынком, нет уровней" (ОК)
- "rr" — строка вида "1:2" / "1:1.5" / "1:3" (только для LONG/SHORT)
- "size" — строка вида "10%" (доля депо, только для LONG/SHORT)

🚨 НАПРАВЛЕНИЕ — НИКАКОГО КОНТРАРИАНА:
Мы НЕ играем «все полезли в лонг → шортим». Направление сделки всегда совпадает
с движением цены, по которому открывается триггер:
    ✅ пробой/закрытие ВВЕРХ выше уровня → LONG (или CASH с флипом �� LONG)
    ✅ пробой/закрытие ВНИЗ ниже уровня → SHORT (или CASH с флипом в SHORT)
    ❌ "пробой $X вверх → SHORT" — ЭТО ИНВЕРСИЯ, ЗАПРЕЩЕНО
    ❌ "пробой $Y вниз → LONG" — ЭТО ИНВЕРСИЯ, ЗАПРЕЩЕНО
В key_trigger и invalidation НЕ указывай LONG/SHORT ярлыки вообще — пиши только
price-event («пробой $82000 вверх», «з��крытие ниже $77000»). Направление сделки
рендерится только из plans[].direction, и валидируется отдельно. Если выдашь
инверсию — пост-валидатор автоматически снимет ярлыки, но логирует это как
галлюцинацию модели.

WATCH (наблюдение, НЕ план):
- watch — массив объектов {"symbol", "level", "note"} (max 8)
- Сюда идут уровни, по которым нет однозначного направления — «$X важный, дождёмся резолюции»
- Если CASH-плану некуда воткнуть однонаправленный флип — его место здесь, а не в plans
- watch может быть пустым, но если plans пусты — обязательно дай watch с конкретными уровнями

ВЕРХНИЕ ПОЛЯ:
- "verdict": "БЫЧИЙ" / "МЕДВЕЖИЙ" / "НЕЙТРАЛЬНЫЙ"
- "reason": 1 предложение, ТОЛЬКО цифры из контекста
- "key_trigger": что подтвердит сценарий
- "invalidation": что ОТМЕНИТ весь сценарий (конкретный ценовой/новостной триггер)
- "simple": 1-2 предложения простым языком для непрофессионала
- "qe_qt": "QE" / "QT" / "NEUTRAL"
- "confidence": "HIGH" / "MEDIUM" / "LOW"
""" + COMMON_GROUNDING_RULE


# Backward-compat alias: callers без horizon-overlay получают базовый промпт.
SYNTH_SYSTEM = SYNTH_BASE_SYSTEM

SPEECHWRITER_SYSTEM = """
Ты — Speechwriter. Тебе дают JSON от Synth:

{
  "verdict": "МЕДВЕЖИЙ",
  "reason": "...",
  "plans": [{"symbol": "BTC", "direction": "SHORT", "entry": 79800, "stop": 82000, "target": 77000, "rr": "1:2", "size": "10%"}, ...],
  "key_trigger": "...",
  "simple": "ПРОСТЫМИ СЛОВАМИ 1-2 предложения",
  "qe_qt": "QT",
  "confidence": "HIGH"
}

Твоя задача: превратить этот JSON в красивый, читаемый текст для Telegram.

ФОРМАТ ОТВЕТА:

🏆 ВЕРДИКТ СУДЬИ: [БЫЧИЙ/МЕДВЕЖИЙ/НЕЙТРАЛЬНЫЙ]
Потому что: [reason из JSON]

📋 ТОРГОВЫЙ ПЛАН:
• BTC | SHORT | Вход: $79800 | Стоп: $82000 | Цель: $77000 | R/R: 1:2 | 10% депозита
• SOL | CASH | Триггер: пробой $92
...

👀 КЛЮЧЕВОЙ ТРИГГЕР: [key_trigger из JSON]

💬 ПРОСТЫМИ СЛОВАМИ: [simple из JSON]

📊 QE/QT РЕЖИМ: [QE или QT или NEUTRAL] — ликвидность [растёт/падает/нейтральна]

⚡ КАК ЧИТАТЬ ПЛАН:
• Все цены — уровни для входа/стопа/цели
• R/R = соотношение риск/награда (1:2 = рискуем 1 чтобы заработать 2)
• % = доля депозита на эту сделку

НИЧЕГО КРОМЕ ФОРМАТИРОВАННОГО ТЕКСТА НЕ ВЫВОДИ.
"""



def _clean_agent_response(text: str) -> str:
    """
    Постобработка ответа агента.
    Cerebras 8B иногда повторяет системный промпт вместо ответа.
    Если текст содержит много 'ЗАПРЕЩЕНО' — это сигнал что модель
    повторяет промпт. Обрезаем до первого осмысленного абзаца.
    """
    if not text:
        return text
    # Считаем вхождения ЗАПРЕЩЕНО
    count_forbidden = text.count("ЗАПРЕЩЕНО")
    if count_forbidden >= 5:
        # Модель повторяет промпт — берём только первые 2 абзаца
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        # Ищем первый абзац без ЗАПРЕЩЕНО
        useful = []
        for p in paragraphs:
            if "ЗАПРЕЩЕНО" not in p and len(p) > 50:
                useful.append(p)
            if len(useful) >= 3:
                break
        if useful:
            return "\n\n".join(useful)
    return text


# ─── БАЗОВЫЙ АГЕНТ ────────────────────────────────────────────────────────────

_COT_LEAK_PATTERNS = (
    "we need to", "we should", "let's pick", "let me think",
    "let me see", "okay let's", "now let's", "we must",
    "thus we need", "we have to", "we'll need", "we will need",
    "the user wants", "i need to", "let me",
)

_COT_LEAK_INDICATORS_RU = (
    "Так, мне нужно", "Давайте подумаем", "Нужно проанализировать",
    "Мне нужно произвести",
)


def _looks_like_cot_leak(text: str) -> bool:
    """Распознаёт chain-of-thought leak от агента.
    
    Reasoning-модели (gpt-oss, Nemotron, MiniMax) иногда возвращают свои
    «мысли о том как ответить» прямо в content вместо reasoning_tokens.
    Это видно по характерным паттернам вначале и метатекстовому стилю.
    """
    if not text:
        return True
    stripped = text.strip()
    head = stripped[:600].lower()
    if not head:
        return True
    # Сильный сигнал — старт с meta-фраз («We need to produce...», «Let me think...»)
    starts_with_meta = any(head.startswith(p) for p in _COT_LEAK_PATTERNS)
    # Считаем сколько meta-маркеров встречается в первых 600 символах
    leak_count = sum(1 for p in _COT_LEAK_PATTERNS if p in head)
    leak_count_ru = sum(1 for p in _COT_LEAK_INDICATORS_RU if p in stripped[:600])

    # «Нормальный» выход реально начинается с одного из ожидаемых маркеров.
    # Раньше мы доверяли простому факту «в тексте встречается FinBERT» —
    # это ошибка: leak-текст сам пересказывает данные и тоже содержит слово
    # FinBERT в своих рассуждениях. Поэтому смотрим именно как НАЧИНАЕТСЯ
    # ответ. Если он не начинается со структурного маркера и стартует с
    # meta-фразы — это leak, даже если ниже встречается «FinBERT».
    first300 = stripped[:300]
    starts_with_real_structure = (
        first300.startswith("•")
        or first300.startswith("-")
        or first300.startswith("🐂") or first300.startswith("🐻")
        or first300.startswith("🔍") or first300.startswith("⚖")
        or first300.startswith("ШАГ") or first300.startswith("Шаг")
        or first300.startswith("Bear говорит")
        or first300.startswith("Bull говорит")
        or first300.startswith("Мой вывод")
        or first300.startswith("ВЕРДИКТ")
        or "\n•" in first300[:200]   # bullet появляется в самом верху
        or "Аргумент 1" in first300[:200]
    )

    # Старт с meta-фразы И отсутствие реальной структуры в самом начале → leak
    if starts_with_meta and not starts_with_real_structure:
        return True
    # Старт с meta-фразы по-русски + нет структуры → leak
    if leak_count_ru >= 2 and not starts_with_real_structure:
        return True
    # Слишком много meta-маркеров (>=3) в первых 600 символах и нет структуры → leak
    if leak_count >= 3 and not starts_with_real_structure:
        return True
    return False


def _sanitize_agent_response(text: str, agent_name: str) -> str:
    """Если response — это leak chain-of-thought, подменяем на fallback.
    
    Лучше показать честное «аргументов недостаточно» чем поток meta-текста.
    """
    if _looks_like_cot_leak(text):
        logger.warning(
            "[%s] CoT leak detected, substituting fallback. Head: %r",
            agent_name, (text or "")[:200]
        )
        if "Bull" in agent_name:
            return (
                "Бычьих аргументов в текущих данных недостаточно — "
                "сильных сигналов с подтверждённым источником "
                "не выделил, склоняюсь к нейтральной позиции."
            )
        if "Bear" in agent_name:
            return (
                "Сильных медвежьих сигналов не выделил — "
                "склоняюсь к нейтрально-наблюдательной позиции."
            )
        # Verifier / Synth — если они leak'нули, всё совсем плохо;
        # лучше пусть pipeline пойдёт без них чем выводить мусор.
        return f"[{agent_name}] выход не структурирован, аргумент пропущен."
    return text


class BaseAgent:
    def __init__(self, name: str, emoji: str, system_prompt: str, ai_method: str):
        self.name          = name
        self.emoji         = emoji
        self.system_prompt = system_prompt
        self.ai_method     = ai_method

    async def respond(
        self,
        news_context: str,
        debate_history: DebateHistory,
        round_num: int,
        extra_instruction: str = ""
    ) -> str:
        history_ctx = debate_history.context_for_agent()
        prompt = f"""КОНТЕКСТ И ДАННЫЕ (ИСПОЛЬЗУЙ ТОЛЬКО ЭТИ ДАННЫЕ):
{news_context}

ИСТОРИЯ ДЕБАТОВ:
{history_ctx}

{f'ДОПОЛНИТЕЛЬНАЯ ИНСТРУКЦИЯ:{chr(10)}{extra_instruction}' if extra_instruction else ''}

Сейчас РАУНД {round_num} из {DEBATE_ROUNDS}.

🚨 НАПОМИНАНИЕ АНТИГАЛЛЮЦИНАЦИОННОГО ПРОТОКОЛА:
- Любая цифра ТОЛЬКО из блока "РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ" выше
- FinBERT: используй ТОЧНОЕ значение из блока "FINBERT SENTIMENT"
- Нет источника = не пиши эту цифру"""

        try:
            caller   = getattr(ai, self.ai_method)
            response = await caller(prompt=prompt, system=self.system_prompt)
            # Защита от reasoning-моделей которые иногда сливают CoT
            # в content вместо reasoning_tokens (gpt-oss, Nemotron, MiniMax M2.5).
            # Раньше при leak'е просто подменяли на placeholder («аргументов
            # недостаточно») — Bull Round 1 у юзера улетал пустым. Теперь
            # пробуем ОДИН retry с skip_primary=True: это пропустит OR-реасонер
            # и пойдёт через Cerebras/Groq/Mistral (Llama 3.3 70B / qwen) —
            # они не leak'ают CoT.
            if _looks_like_cot_leak(response):
                logger.warning(
                    "[%s] primary CoT-leak, retry skip_primary=True. Head: %r",
                    self.name, (response or "")[:200]
                )
                try:
                    retry = await caller(
                        prompt=prompt, system=self.system_prompt,
                        skip_primary=True,
                    )
                    if not _looks_like_cot_leak(retry):
                        return retry
                    logger.warning("[%s] retry тоже leak — placeholder", self.name)
                except Exception as retry_e:
                    logger.warning("[%s] retry упал: %s", self.name, retry_e)
            return _sanitize_agent_response(response, self.name)
        except Exception as e:
            logger.error(f"Agent {self.name} error: {e}")
            return f"[Ошибка агента {self.name}: {e}]"


# ─── КОНКРЕТНЫЕ АГЕНТЫ ────────────────────────────────────────────────────────

class BullResearcher(BaseAgent):
    def __init__(self):
        super().__init__("Bull Researcher", "🐂", BULL_SYSTEM, "bull")

    async def respond_counter(self, news_context: str, history: DebateHistory, round_num: int) -> str:
        bear_args          = history.last_message_by("Bear")
        verifier_notes     = history.last_message_by("Verifier")
        extra              = ""
        if bear_args:
            extra += f"Аргументы Bear:\n{bear_args[:1000]}\n\n"
        if verifier_notes:
            extra += f"⚠️ Verifier нашёл галлюцинации — НЕ защищай их:\n{verifier_notes[:800]}"
        self.system_prompt = BULL_COUNTER_SYSTEM
        result             = await self.respond(news_context, history, round_num, extra)
        self.system_prompt = BULL_SYSTEM
        return result


class BearSkeptic(BaseAgent):
    def __init__(self):
        super().__init__("Bear Skeptic", "🐻", BEAR_SYSTEM, "bear")

    async def respond_counter(self, news_context: str, history: DebateHistory, round_num: int) -> str:
        bull_counter       = history.last_message_by("Bull")
        verifier_notes     = history.last_message_by("Verifier")
        extra = ""
        if bull_counter:
            extra += f"Ответ Bull:\n{bull_counter[:1000]}\n\n"
        if verifier_notes:
            import re as _re
            hall = _re.findall(r"ГАЛЛЮЦИНАЦИЯ[^\n]*", verifier_notes)
            if hall:
                extra += "Галлюцинации Bull (Verifier, используй):\n" + "\n".join(hall[:5]) + "\n\n"
            extra += f"Verifier:\n{verifier_notes[:500]}"
        self.system_prompt = BEAR_COUNTER_SYSTEM
        result             = await self.respond(news_context, history, round_num, extra)
        self.system_prompt = BEAR_SYSTEM
        # Постобработка: если модель повторяет системный промпт — обрезаем
        result = _clean_agent_response(result)
        return result


class DataVerifier(BaseAgent):
    def __init__(self):
        super().__init__("Data Verifier", "🔍", VERIFIER_SYSTEM, "verifier")


class ConsensusSynth(BaseAgent):
    def __init__(self):
        super().__init__("Consensus Synthesizer", "⚖️", SYNTH_SYSTEM, "synth")


_SPEECHWRITER_TIMEOUT_S = 45.0
_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?|\n?```\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Some LLMs wrap output in ```...``` even when told not to. Strip them."""
    if not text:
        return text
    cleaned = _CODE_FENCE_RE.sub("", text).strip()
    return cleaned or text


def _extract_json_obj(text: str) -> dict | None:
    """Extract the first JSON object from arbitrary LLM text. Returns None on failure."""
    if not text:
        return None
    raw = _strip_code_fences(text).strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _format_price(value) -> str:
    """Format a price-like value for the trade plan line."""
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)):
        if value >= 1000:
            return f"${value:,.0f}".replace(",", " ")
        return f"${value:g}"
    s = str(value).strip()
    if s and s[0].isdigit():
        return f"${s}"
    return s


def _coerce_num(value) -> float | None:
    """Best-effort numeric coercion. '$79,800', '79 800', '79800.5' → 79800.5"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_rr(rr: str) -> float | None:
    """Parse '1:2', '1:1.5' → 2.0, 1.5. Returns None on garbage."""
    if not rr or not isinstance(rr, str):
        return None
    parts = rr.replace(" ", "").split(":")
    if len(parts) != 2:
        return None
    try:
        risk = float(parts[0])
        reward = float(parts[1])
        if risk <= 0:
            return None
        return reward / risk
    except (TypeError, ValueError):
        return None


def _validate_plan_geometry(plan: dict, min_rr: float) -> tuple[bool, str]:
    """Hard-guard for trade-plan geometry. Returns (is_valid, reason).

    Rules:
      LONG:  stop < entry < target
      SHORT: target < entry < stop
      Implied R/R must be ≥ min_rr (with 5% tolerance for rounding).
      direction CASH/WAIT/FLAT is always valid.
    """
    if not isinstance(plan, dict):
        return False, "plan is not a dict"
    direction = str(plan.get("direction", "")).upper().strip()
    if direction in {"CASH", "WAIT", "FLAT", ""}:
        return True, ""
    entry = _coerce_num(plan.get("entry"))
    stop = _coerce_num(plan.get("stop"))
    target = _coerce_num(plan.get("target"))
    if entry is None or stop is None or target is None:
        return False, "entry/stop/target missing or not numeric"
    if entry <= 0 or stop <= 0 or target <= 0:
        return False, "non-positive levels"
    if direction == "LONG":
        if not (stop < entry < target):
            return False, f"LONG geometry broken: need stop<entry<target, got {stop}/{entry}/{target}"
        risk = entry - stop
        reward = target - entry
    elif direction == "SHORT":
        if not (target < entry < stop):
            return False, f"SHORT geometry broken: need target<entry<stop, got {target}/{entry}/{stop}"
        risk = stop - entry
        reward = entry - target
    else:
        return False, f"unknown direction: {direction!r}"
    if risk <= 0:
        return False, "zero/negative risk"
    actual_rr = reward / risk
    declared_rr = _parse_rr(str(plan.get("rr", ""))) or 0.0
    # A plan must clear the horizon's minimum R/R; allow a small 5% tolerance for rounding.
    if actual_rr + 1e-6 < min_rr * 0.95:
        return False, f"R/R too tight: actual {actual_rr:.2f} < min {min_rr:.2f}"
    # If declared R/R is wildly inconsistent with geometry, reject. Same 25% tolerance bound.
    if declared_rr > 0 and abs(declared_rr - actual_rr) / max(actual_rr, 0.1) > 0.25:
        return False, f"declared R/R {declared_rr:.2f} ≠ geometric {actual_rr:.2f}"
    return True, ""


def _coerce_to_cash(plan: dict, reason: str) -> dict:
    """Replace an impossible LONG/SHORT plan with a safe CASH entry."""
    return {
        "symbol": plan.get("symbol", "?"),
        "direction": "CASH",
        "trigger": f"план снят авто-проверкой: {reason}",
        "horizon": plan.get("horizon", ""),
    }


_FIELD_LEAD_RE = re.compile(
    r"^\s*(?:simple|reason|key_trigger|key trigger|invalidation|qe_qt|qe/qt|verdict)?\s*[:：\-—]\s*",
    re.IGNORECASE,
)


def _strip_field_lead(value) -> str:
    """Снять с начала строки повторяющийся префикс ключа и/или ведущие двоеточия.

    Synth иногда отдаёт «simple: SPY перекуплен» (повторяет название поля)
    или «: SPY перекуплен» (просто двоеточие в начале) — оба варианта
    в дайджесте превращаются в «Простыми словами: : SPY перекуплен» и т.п.
    Срезаем максимум один префикс, остальное оставляем как есть.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    return _FIELD_LEAD_RE.sub("", s, count=1).strip()


# Двунаправленные триггеры — Synth иногда выдаёт «пробой $X вниз ИЛИ
# закрытие выше $X» как «универсальный план». Это не план: один уровень
# не может одновременно быть пробит вверх И вниз; и направление сделки
# (LONG/SHORT) остаётся неопределённым. Такие записи демоутим в watch.
_BIDIRECTIONAL_TRIGGER_RE = re.compile(
    r"(?:вниз|внизу|ниже|вверх|выше|сверху).*(?:или|либо|или\s+/|\s+/\s+).*"
    r"(?:вверх|выше|сверху|вниз|внизу|ниже)",
    re.IGNORECASE | re.DOTALL,
)
_PRICE_LEVEL_RE = re.compile(r"\$?\s*\d[\d.,]*\s*[KkКк]?")


def _is_bidirectional_trigger(text: str) -> bool:
    """True если триггер указывает оба направления (классический non-actionable
    «watch level»). Пример: «пробой $82879 вниз или закрытие выше $82879»."""
    if not text:
        return False
    s = str(text).lower()
    if _BIDIRECTIONAL_TRIGGER_RE.search(s):
        return True
    has_up = any(tok in s for tok in ("вверх", "выше", "сверху", "above", "up"))
    has_down = any(tok in s for tok in ("вниз", "ниже", "снизу", "below", "down"))
    has_or = any(tok in s for tok in (" или ", " либо ", " / ", " or "))
    return has_up and has_down and has_or


# Инверсия направления — Synth (особенно reasoning-модели) иногда выдаёт
# контрарианский трейд-план: «пробой $X вверх → SHORT» или «пробой $Y вниз →
# LONG». Это почти всегда галлюцинация: пробой ВВЕРХ = bullish breakout =
# LONG-сторона, пробой ВНИЗ = bearish breakdown = SHORT-сторона. Контрарианская
# логика «все полезли в лонг → шорти» в нашей системе НЕ предусмотрена —
# направление сделки выражается через plans[].direction, не через текстовые
# подсказки �� key_trigger/invalidation. Поэтому при детекте инверсии мы
# вычищаем direction-ярлыки целиком — Telegram-юзер видит чистый price-event,
# а direction берёт из блока «📋 ТОРГОВЫЙ ПЛАН».
_DIRECTION_INVERSION_PATTERNS = [
    # up-движение → SHORT (классическая инверсия)
    (re.compile(
        r"(?:пробой|закрытие|breakout|break\s+up|тест|ретест|касание)?"
        r"[^.\n]{0,40}?"
        r"(?:вверх|выше|сверху|above|up)"
        r"[^.\n]{0,40}?"
        r"\b(?:short|шорт)\b",
        re.IGNORECASE,
    ), "up→SHORT"),
    # down-движение → LONG (классическая инверсия)
    (re.compile(
        r"(?:пробой|закрытие|breakdown|break\s+down|тест|ретест|касание)?"
        r"[^.\n]{0,40}?"
        r"(?:вниз|ниже|снизу|below|down)"
        r"[^.\n]{0,40}?"
        r"\b(?:long|лонг)\b",
        re.IGNORECASE,
    ), "down→LONG"),
]

# Удаляет «(LONG)», «(переключение в SHORT)», «→ LONG» и т.п. — все формы
# direction-ярлыков, прикреплённых к price-event в свободном тексте.
_DIRECTION_TAG_RE = re.compile(
    r"\s*(?:"
    r"[\(\[]\s*(?:переключение\s+в\s+|switch\s+to\s+)?(?:LONG|SHORT|long|short|лонг|шорт)\s*[\)\]]"
    r"|\s*[→\-—]+\s*(?:LONG|SHORT|long|short|лонг|шорт)\b"
    r"|\s*\bпереключение\s+в\s+(?:LONG|SHORT|long|short|лонг|шорт)\b"
    r"|\s*\bswitch\s+to\s+(?:LONG|SHORT|long|short|лонг|шорт)\b"
    r")",
    re.IGNORECASE,
)


def _sanitize_trigger_direction(text: str, field_name: str = "") -> str:
    """Снимает LONG/SHORT-ярлыки из key_trigger / invalidation если детектируется
    инверсия (см. _DIRECTION_INVERSION_PATTERNS). Логирует warning для
    мониторинга качества Synth-модели.

    Trade direction в нашей системе живёт ТОЛЬКО в plans[].direction, который
    проходит геометрическую валидацию. Текстовые ярлыки в свободных полях
    либо избыточны, либо опасно противоречивы — снимаем превентивно при
    обнаружении инверсии.
    """
    if not text:
        return text
    for pat, label in _DIRECTION_INVERSION_PATTERNS:
        if pat.search(text):
            logger.warning(
                f"[PLAN-GUARD] Inverted direction hint in {field_name or 'trigger'} "
                f"({label}). Stripping LONG/SHORT tags. raw={text[:200]!r}"
            )
            cleaned = _DIRECTION_TAG_RE.sub("", text).strip()
            # Прибираем лишние пробелы и хвостовые разделители после стрипа.
            cleaned = re.sub(r"\s+", " ", cleaned)
            cleaned = re.sub(r"\s*[,;:.—\-]\s*$", "", cleaned).strip()
            return cleaned
    return text


def _is_vague_trigger(text: str) -> bool:
    """True если триггер не содержит конкретного уровня/события — типа
    «ожидание коррекции SPY/нефти». Без числа и без чёткого события
    это не торговый триггер, это эмоция."""
    if not text:
        return True
    s = str(text).strip()
    if len(s) < 6:
        return True
    has_level = bool(_PRICE_LEVEL_RE.search(s))
    has_concrete_event = any(
        tok in s.lower()
        for tok in (
            "пробой", "закрытие", "тест", "ретест", "касание",
            "rsi", "atr", "vix", "ema", "sma", "macd",
            "fomc", "cpi", "nfp", "ставк", "fed", "ecb",
            "breakout", "break", "close above", "close below",
        )
    )
    return not (has_level or has_concrete_event)


def _is_unactionable_cash(plan: dict) -> tuple[bool, str]:
    """CASH-план считается неактивным (демоут в watch) если:
       - нет триггера вообще
       - триггер двунаправленный (см. _is_bidirectional_trigger)
       - триггер абстрактный (см. _is_vague_trigger)
    Возвращает (is_unactionable, reason)."""
    direction = str(plan.get("direction", "")).upper().strip()
    if direction not in {"CASH", "WAIT", "FLAT"}:
        return False, ""
    trigger = str(plan.get("trigger") or "").strip()
    if not trigger or trigger == "—":
        return True, "no trigger"
    if _is_bidirectional_trigger(trigger):
        return True, "bidirectional trigger"
    if _is_vague_trigger(trigger):
        return True, "vague trigger (no level/event)"
    return False, ""


def _stop_factor_block(direction: str, stop_factor: str | None) -> tuple[bool, str]:
    """Code-side stop-factor override.

    `stop_factor` is one of: None, 'bearish', 'bullish'.
      'bearish' (e.g. MVRV>3.5, VIX>40, эйфория VIX<15+F&G>70) → запрещаем LONG.
      'bullish' (e.g. MVRV<1.0, F&G<25)                         → запрещаем SHORT.

    Returns (blocked, reason). LLM-вердикт мы не трогаем — только LONG/SHORT планы.
    """
    if not stop_factor:
        return False, ""
    d = direction.upper().strip()
    if stop_factor == "bearish" and d == "LONG":
        return True, "критический медвежий стоп-фактор (MVRV>3.5 / VIX>40 / эйфория) — LONG заблокирован"
    if stop_factor == "bullish" and d == "SHORT":
        return True, "критический бычий стоп-фактор (MVRV<1.0 / F&G<25) — SHORT заблокирован"
    return False, ""


# ── Anti-stale-price + verdict/plan consistency guards (added 2026-05-12) ────


# Maximum allowed deviation of plan.entry from current market price (3%).
# Beyond this the Synth-agent likely used stale prices from cache — обнаружено на
# дайджесте 06.05.2026 где BTC entry $69,800 был выставлен при рынке $80,900.
_ENTRY_VS_MARKET_MAX_DEVIATION_PCT = 3.0

# Minimum SL distance for crypto plans (% от entry). На дневном ATR ~$2.5k для BTC
# любой SL ближе чем ~1.5% выбивается обычным шумом. Раздвигаем код-сайдом.
_MIN_SL_DISTANCE_PCT_CRYPTO = 1.5
_MIN_SL_DISTANCE_PCT_EQUITY = 1.0  # SPY/индексы — узкий спред допустим


def _validate_entry_vs_market(
    plan: dict,
    market_prices: dict[str, float] | None,
) -> tuple[bool, str]:
    """Проверяет что entry не уехал далеко от текущего рынка.

    Returns (is_valid, reason). LONG/SHORT only; CASH/WAIT/FLAT всегда валидны.
    Если market_prices пуст или нет данных по этому символу — пропускаем (best-effort).
    """
    if not market_prices:
        return True, ""
    direction = str(plan.get("direction", "")).upper().strip()
    if direction in {"CASH", "WAIT", "FLAT", ""}:
        return True, ""
    symbol = str(plan.get("symbol", "")).upper().strip()
    if not symbol:
        return True, ""
    current = market_prices.get(symbol)
    if not current or current <= 0:
        return True, ""
    entry = _coerce_num(plan.get("entry"))
    if entry is None or entry <= 0:
        return True, ""
    deviation_pct = (entry - current) / current * 100
    # Для LONG entry должен быть около/ВЫШЕ рынка (breakout) или максимум на 3% ниже.
    # Если entry на 3%+ ниже рынка LONG — это «купи на коррекции», что слишком
    # spekulяtivно для авто-плана. Сдвигаем в CASH чтобы LLM пересмотрел.
    if direction == "LONG" and deviation_pct < -_ENTRY_VS_MARKET_MAX_DEVIATION_PCT:
        return False, (
            f"LONG entry ${entry:,.0f} на {deviation_pct:+.1f}% ниже рынка ${current:,.0f} — "
            f"вероятно Synth использовал устаревшие цены (порог 3%)"
        )
    # Аналогично для SHORT entry должен быть около/НИЖЕ рынка.
    if direction == "SHORT" and deviation_pct > _ENTRY_VS_MARKET_MAX_DEVIATION_PCT:
        return False, (
            f"SHORT entry ${entry:,.0f} на {deviation_pct:+.1f}% выше рынка ${current:,.0f} — "
            f"вероятно Synth использовал устаревшие цены (порог 3%)"
        )
    return True, ""


def _scale_size_str(size: str, mult: float) -> str:
    """Масштабирует строку размера: '10%' × 0.5 → '5%'. Нечисловые — pass-through."""
    m = re.match(r"(\d+(?:\.\d+)?)\s*%", str(size).strip())
    if not m:
        return size
    val = float(m.group(1)) * mult
    return f"{val:.0f}%" if val >= 1 else f"{val:.1f}%"


def _validate_sl_distance(plan: dict, market_prices: dict | None = None) -> tuple[bool, str]:
    """Стоп слишком близко к entry → выбивается шумом. Расширим до минимума.

    Returns (is_valid, reason). Невалидный план не coerce-ится в CASH здесь — это
    мягкая проверка, мы лишь логируем. _widen_sl_to_minimum ниже даёт расширенный SL.

    ATR-aware (pre-live-hardening, Requirement C):
    Если market_prices содержит ATR_{symbol} (14d, positive float) — используем
    max(fixed_min_pct, 1.5 * ATR / entry * 100) как порог. Иначе fallback к fixed.
    """
    direction = str(plan.get("direction", "")).upper().strip()
    if direction in {"CASH", "WAIT", "FLAT", ""}:
        return True, ""
    entry = _coerce_num(plan.get("entry"))
    stop = _coerce_num(plan.get("stop"))
    if not entry or not stop:
        return True, ""
    sl_distance_pct = abs(entry - stop) / entry * 100
    symbol = str(plan.get("symbol", "")).upper().strip()
    is_crypto = symbol in {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA"}
    fixed_min_pct = _MIN_SL_DISTANCE_PCT_CRYPTO if is_crypto else _MIN_SL_DISTANCE_PCT_EQUITY

    # ATR-aware minimum (C1/C2/C3)
    atr_min_pct = 0.0
    if market_prices and symbol:
        atr_key = f"ATR_{symbol}"
        atr = market_prices.get(atr_key)
        if atr is not None:
            if isinstance(atr, (int, float)) and atr > 0:
                atr_min_pct = (1.5 * float(atr) / entry) * 100
            else:
                logger.warning(f"[SL-GUARD] Invalid {atr_key}={atr!r}, fallback к fixed")

    min_pct = max(fixed_min_pct, atr_min_pct)
    if sl_distance_pct < min_pct:
        source = "[ATR×1.5]" if atr_min_pct > fixed_min_pct else "[fixed]"
        return False, (
            f"SL слишком близко: {sl_distance_pct:.2f}% (мин {min_pct:.2f}% {source} для {symbol or 'актива'})"
        )
    return True, ""


def _widen_sl_to_minimum(plan: dict, market_prices: dict | None = None) -> dict:
    """Раздвигает SL до минимального безопасного расстояния от entry.

    ATR-aware: если market_prices содержит ATR_{symbol} — расширяет до 1.5×ATR
    (если это больше фиксированного минимума).
    """
    direction = str(plan.get("direction", "")).upper().strip()
    entry = _coerce_num(plan.get("entry"))
    if not entry or direction not in {"LONG", "SHORT"}:
        return plan
    symbol = str(plan.get("symbol", "")).upper().strip()
    is_crypto = symbol in {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA"}
    fixed_min_pct = _MIN_SL_DISTANCE_PCT_CRYPTO if is_crypto else _MIN_SL_DISTANCE_PCT_EQUITY

    # ATR-aware (C1)
    atr_min_pct = 0.0
    if market_prices and symbol:
        atr = market_prices.get(f"ATR_{symbol}")
        if isinstance(atr, (int, float)) and atr > 0:
            atr_min_pct = (1.5 * float(atr) / entry) * 100

    min_pct = max(fixed_min_pct, atr_min_pct)
    if direction == "LONG":
        new_stop = entry * (1 - min_pct / 100)
    else:
        new_stop = entry * (1 + min_pct / 100)
    out = dict(plan)
    out["stop"] = round(new_stop, 2)
    return out


def _verdict_to_directions(verdict: str) -> set[str]:
    """Какие направления плана совместимы с этим вердиктом."""
    v = (verdict or "").upper()
    if "БЫЧ" in v or "BULL" in v:
        return {"LONG", "CASH", "WAIT", "FLAT"}
    if "МЕДВЕ" in v or "BEAR" in v:
        return {"SHORT", "CASH", "WAIT", "FLAT"}
    # NEUTRAL — допускаем любые планы, но это всё равно странно
    return {"LONG", "SHORT", "CASH", "WAIT", "FLAT"}


def _check_verdict_plan_consistency(
    verdict: str,
    plans: list[dict],
) -> tuple[bool, str]:
    """Проверяет что verdict совпадает с направлением primary plan trade.

    Returns (is_consistent, reason_if_not). Для NEUTRAL пропускаем (туда все
    направления допустимы). Для БЫЧИЙ/МЕДВЕЖИЙ ищем первый non-CASH план и
    сравниваем направление.

    Это поведение защищает от случаев типа 11.04 / 22.04 апреля 2026, когда
    Synth выдавал verdict «МЕДВЕЖИЙ» + plan «BTC LONG @ $72,730» одновременно.
    """
    if not plans:
        return True, ""
    allowed = _verdict_to_directions(verdict)
    for p in plans:
        if not isinstance(p, dict):
            continue
        direction = str(p.get("direction", "")).upper().strip()
        if direction in {"CASH", "WAIT", "FLAT", ""}:
            continue
        if direction not in allowed:
            return False, (
                f"verdict «{verdict}» противоречит первому актив-плану "
                f"{p.get('symbol', '?')} {direction}"
            )
        # Нашли первый actionable, проверка прошла.
        return True, ""
    return True, ""


def _render_trade_plan_from_json(
    data: dict,
    horizon_pack: HorizonPack | None = None,
    stop_factor: str | None = None,
    market_prices: dict[str, float] | None = None,
) -> str:
    """Deterministic Telegram-ready text rendering of Synth JSON.
    Used when Synth returns parseable JSON, avoiding an extra LLM call entirely.

    If a horizon_pack is supplied we ALSO hard-guard plan geometry: any LONG/SHORT
    plan with broken levels or R/R below the horizon minimum is silently rewritten
    to a CASH entry so Telegram never shows users a mathematically impossible setup.

    `market_prices` (optional): {SYMBOL: current_price} — используется для
    stale-price guard: если plan.entry уехал >3% от рынка, plan переводится в CASH.

    `stop_factor` ('bearish'|'bullish'|None) — code-side override: even если LLM
    придумал LONG при MVRV>3.5 — его план превратится в CASH с понятной причи��ой.
    """
    pack = horizon_pack or get_horizon(None)
    min_rr = pack.min_rr

    verdict = str(data.get("verdict", "НЕЙТРАЛЬНЫЙ")).upper().strip() or "НЕЙТРАЛЬНЫЙ"
    reason = _strip_field_lead(data.get("reason", ""))
    plans = data.get("plans") or []
    raw_watch = data.get("watch") or []
    key_trigger = _strip_field_lead(data.get("key_trigger", ""))
    invalidation = _strip_field_lead(data.get("invalidation", ""))
    # Анти-инверсия: если Synth написал «пробой вверх → SHORT» / «вниз → LONG»
    # — снимаем direction-ярлыки. Юзер видит price-event без противоречивых
    # подсказок, направление берёт из блока «ТОРГОВЫЙ ПЛАН» (plans[].direction).
    key_trigger = _sanitize_trigger_direction(key_trigger, "key_trigger")
    invalidation = _sanitize_trigger_direction(invalidation, "invalidation")
    # Synth-модель иногда повторяет имя ключа в значении: ":SPY перекуплен" /
    # "simple: SPY перекуплен". Снимаем оба варианта (двоеточие + повтор поля).
    simple = _strip_field_lead(data.get("simple", ""))
    eli5 = _strip_field_lead(data.get("eli5", ""))
    qe_qt = str(data.get("qe_qt", "NEUTRAL")).upper().strip() or "NEUTRAL"

    # Прогон через 2 уровня валидации:
    #   1. _validate_plan_geometry — геометрия LONG/SHORT (stop<entry<target etc.)
    #   2. _is_unactionable_cash — двунаправленные / абстрактные CASH-триггеры,
    #      которые на UI выглядят как «BTC CASH | пробой $X вниз ИЛИ выше $X» —
    #      это не план, это watch-уровень. Демоутим в watch-список и НЕ
    #      рендерим в блоке «📋 ТОРГОВЫЙ ПЛАН».
    actionable_plans: list[dict] = []
    demoted_to_watch: list[dict] = []
    stale_price_flags: list[str] = []
    if isinstance(plans, list):
        for raw in plans:
            if not isinstance(raw, dict):
                continue
            ok, why = _validate_plan_geometry(raw, min_rr=min_rr)
            p = raw if ok else _coerce_to_cash(raw, why)
            if not ok:
                logger.warning(f"[PLAN-GUARD] Coerced impossible plan to CASH: {why} | raw={raw}")
            blocked, sf_why = _stop_factor_block(str(p.get("direction", "")), stop_factor)
            if blocked:
                logger.warning(f"[STOP-FACTOR] Coerced plan to CASH: {sf_why} | raw={raw}")
                p = _coerce_to_cash(raw, sf_why)
            # ── Stale-price guard (added 2026-05-12) ────────────────────────
            # Если entry уехал >3% от текущего рынка — Synth скорее всего
            # использовал устаревшие цены (06.05 баг: BTC entry $69.8k при рынке $80.9k).
            # Не пропускаем такой план в торговую систему: coerce в CASH.
            fresh_ok, fresh_why = _validate_entry_vs_market(p, market_prices)
            if not fresh_ok:
                logger.warning(f"[STALE-PRICE] Coerced plan to CASH: {fresh_why} | raw={raw}")
                stale_price_flags.append(
                    f"{p.get('symbol', '?')}: {fresh_why}"
                )
                p = _coerce_to_cash(raw, fresh_why)
            # ── SL-distance guard: расширяем стоп, если выставлен в шум ─────
            sl_ok, sl_why = _validate_sl_distance(p, market_prices=market_prices)
            if not sl_ok and str(p.get("direction", "")).upper() in {"LONG", "SHORT"}:
                logger.warning(f"[SL-GUARD] Widening SL: {sl_why} | raw={raw}")
                p = _widen_sl_to_minimum(p, market_prices=market_prices)
                # После расширения SL пересчитаем геометрию: возможно теперь R/R упал.
                # Если упал ниже min_rr — coerce в CASH (правда такого почти не бывает,
                # т.к. SL сужают, а не расширяют у качественных setupов).
                ok2, why2 = _validate_plan_geometry(p, min_rr=min_rr)
                if not ok2:
                    logger.warning(f"[SL-GUARD] After widening SL R/R сломался: {why2}")
                    p = _coerce_to_cash(raw, f"SL расширен → {why2}")
            # ── Conservative sizing (pre-live-hardening, Requirement B) ─────
            # Первые N actionable-сделок после обновления промптов → size × 0.5
            if str(p.get("direction", "")).upper() in {"LONG", "SHORT"}:
                sizing_mult = get_multiplier()
                if sizing_mult < 1.0:
                    p = dict(p)
                    p["size"] = _scale_size_str(str(p.get("size", "")), sizing_mult)
                    record_actionable_trade()
            unactionable, ua_why = _is_unactionable_cash(p)
            if unactionable:
                logger.warning(f"[PLAN-GUARD] Demoted CASH plan to watch: {ua_why} | raw={raw}")
                trigger_txt = str(p.get("trigger") or "").strip()
                demoted_to_watch.append({
                    "symbol": str(p.get("symbol") or "?").upper(),
                    "level": "",
                    "note": trigger_txt or ua_why,
                })
                continue
            actionable_plans.append(p)

    # ── Verdict ↔ plan consistency guard (added 2026-05-12) ──────────────────
    # Случаи 11.04 / 22.04 апреля 2026: verdict=МЕДВЕЖИЙ + plan[0]=BTC LONG —
    # явное внутреннее противоречие Synth. Логируем + ставим warning в дайджест.
    verdict_plan_consistent, vp_why = _check_verdict_plan_consistency(verdict, actionable_plans)
    if not verdict_plan_consistent:
        logger.warning(f"[CONSISTENCY] Verdict ↔ plan mismatch: {vp_why}")

    # Watch из самого Synth (новое поле): нормализуем в тот же формат.
    explicit_watch: list[dict] = []
    if isinstance(raw_watch, list):
        for w in raw_watch:
            if not isinstance(w, dict):
                continue
            sym = str(w.get("symbol") or "").upper().strip()
            level = str(w.get("level") or "").strip()
            note = str(w.get("note") or "").strip()
            if not (sym or level or note):
                continue
            explicit_watch.append({"symbol": sym or "?", "level": level, "note": note})

    watch_list = explicit_watch + demoted_to_watch

    only_watch = (not actionable_plans) and bool(watch_list)

    lines: list[str] = []
    lines.append(f"🏆 ВЕРДИКТ СУДЬИ: {verdict}")
    lines.append(f"⏱ ГОРИЗОНТ: {pack.label_pretty}")
    if reason:
        lines.append(f"Потому что: {reason}")
    if stop_factor == "bearish":
        lines.append("🚨 СТОП-ФАКТОР: критический медвежий (LONG-планы автоматически снимаются)")
    elif stop_factor == "bullish":
        lines.append("🔵 СТОП-ФАКТОР: критический бычий (SHORT-планы автоматически снимаются)")
    if stale_price_flags:
        lines.append(
            "⚠️ DATA STALE: цены в первоначальном плане устарели — план переведён в CASH. "
            "Подробности в логах."
        )
    # ── Conservative sizing badge (Requirement B) ──
    _sizing_badge = bake_in_badge()
    if _sizing_badge:
        lines.append(_sizing_badge)
    if not verdict_plan_consistent:
        lines.append(
            "⚠️ ВНИМАНИЕ: вердикт не совпадает с направлением первого плана — "
            "сигнал внутренне противоречив, торговать с пониженной уверенностью."
        )
    lines.append("")
    if only_watch:
        # Все «планы» — на самом деле watch-уровни. Меняем заголовок,
        # чтобы юзер не путал «у нас есть план» и «у нас нет плана,
        # просто следим за уровнями».
        lines.append("📊 СЕЙЧАС НЕ ТОРГУЕМ — СЛЕДИМ ЗА УРОВНЯМИ:")
    else:
        lines.append("📋 ТОРГОВЫЙ ПЛАН:")
        if actionable_plans:
            for p in actionable_plans:
                sym = str(p.get("symbol", "")).upper() or "?"
                direction = str(p.get("direction", "")).upper()
                if direction in {"CASH", "WAIT", "FLAT"}:
                    trigger = str(p.get("trigger") or p.get("entry") or "—")
                    lines.append(f"• {sym} | CASH | Триггер: {trigger}")
                    continue
                entry = _format_price(p.get("entry"))
                stop = _format_price(p.get("stop"))
                target = _format_price(p.get("target"))
                rr = str(p.get("rr", "")).strip() or "—"
                size = str(p.get("size", "")).strip() or "—"
                lines.append(
                    f"• {sym} | {direction or '—'} | Вход: {entry} | Стоп: {stop} | Цель: {target} | R/R: {rr} | {size} депозита"
                )
        else:
            lines.append("• нет идей с положительным ожиданием — стой в стороне")

    if watch_list:
        if not only_watch:
            lines.append("")
            lines.append("👁 НАБЛЮДЕНИЕ (без сделки):")
        for w in watch_list[:6]:
            sym = w.get("symbol") or "?"
            level = w.get("level") or ""
            note = w.get("note") or ""
            chunks = [sym]
            if level:
                chunks.append(level)
            if note:
                chunks.append(note)
            lines.append("• " + " | ".join(chunks))

    lines.append("")
    if key_trigger:
        lines.append(f"👀 КЛЮЧЕВОЙ ТРИГГЕР: {key_trigger}")
        lines.append("")
    if invalidation:
        lines.append(f"🛑 ИНВАЛИДАЦИЯ: {invalidation}")
        lines.append("")
    if simple:
        lines.append(f"💬 ПРОСТЫМИ СЛОВАМИ: {simple}")
        lines.append("")
    if eli5:
        lines.append(f"👶 КАК 5-ЛЕТНЕМУ: {eli5}")
        lines.append("")
    qe_qt_word = {"QE": "растёт", "QT": "падает"}.get(qe_qt, "нейтральна")
    lines.append(f"📊 QE/QT РЕЖИМ: {qe_qt} — ликвидность {qe_qt_word}")
    return "\n".join(lines).rstrip()


class Speechwriter:
    """Speechwriter — форматирует JSON от Synth в красивый текст для Telegram."""

    def __init__(self):
        self.system_prompt = SPEECHWRITER_SYSTEM

    async def format(
        self,
        synth_json: str,
        horizon: str | HorizonPack | None = None,
        stop_factor: str | None = None,
        market_prices: dict[str, float] | None = None,
    ) -> str:
        """
        Принимает JSON (или текст) от Synth и превращает в читаемый торговый план.

        Стратегия:
          1. Если Synth вернул валидный JSON → рендерим детерминированно (быстро,
             без LLM-вызова, без галлюцинаций, цифры один-в-один). При этом
             включаем hard-guard геометрии плана и подмешиваем ярлык горизонта.
          2. Иначе → зовём LLM с таймаутом и пост-обработкой (снятие ```code fences```).
          3. Если и это сломалось → возвращаем сырой ввод как fallback.

        `stop_factor` ('bearish'|'bullish'|None) — code-side override: пробрасывается
        в renderer чтобы LONG/SHORT планы переписывались в CASH когда срабатывает
        критический MVRV/VIX/F&G сигнал — независимо от того что выдал Synth.
        """
        pack = horizon if isinstance(horizon, HorizonPack) else get_horizon(horizon if isinstance(horizon, str) else None)

        # 1. Strict path: deterministic rendering when Synth returned clean JSON.
        data = _extract_json_obj(synth_json)
        if data is not None:
            try:
                rendered = _render_trade_plan_from_json(
                    data,
                    horizon_pack=pack,
                    stop_factor=stop_factor,
                    market_prices=market_prices,
                )
                if rendered.strip():
                    logger.info(f"[SPEECHWRITER] Deterministic render OK (horizon={pack.key}, stop_factor={stop_factor}, no LLM call)")
                    return rendered
            except Exception as e:
                logger.warning(f"[SPEECHWRITER] Deterministic render failed, falling back to LLM: {e}")

        # 2. LLM fallback for free-form Synth output.
        from ai_provider import ai

        prompt = f"""Преобразуй данные ниже в читаемый торговый план (горизонт {pack.label}):

{synth_json}

ФОРМАТ:
🏆 ВЕРДИКТ СУДЬИ: [БЫЧИЙ/МЕДВЕЖИЙ/НЕЙТРАЛЬНЫЙ]
⏱ ГОРИЗОНТ: {pack.label_pretty}
Потому что: [1 предложение]

📋 ТОРГОВЫЙ ПЛАН:
• BTC | SHORT | Вход: $79800 | Стоп: $82000 | Цель: $77000 | R/R: 1:2 | 10% депозита

👀 КЛЮЧЕВОЙ ТРИГГЕР: [цена или событие]

🛑 ИНВАЛИДАЦИЯ: [условие отмены сценария]

💬 ПРОСТЫМИ СЛОВАМИ: [1-2 предложения для непрофессионала]

📊 QE/QT РЕЖИМ: [QE/QT/NEUTRAL]

НИЧЕГО КРОМЕ ФОРМАТИРОВАННОГО ТЕКСТА НЕ ВЫВОДИ.
"""

        try:
            response = await asyncio.wait_for(
                ai.synth(prompt=prompt, system=self.system_prompt),
                timeout=_SPEECHWRITER_TIMEOUT_S,
            )
            return _strip_code_fences(response or "") or synth_json
        except asyncio.TimeoutError:
            logger.warning(f"[SPEECHWRITER] timed out after {_SPEECHWRITER_TIMEOUT_S}s, returning raw synth")
            return synth_json
        except Exception as e:
            logger.error(f"Speechwriter error: {e}")
            return synth_json  # fallback — выводим как есть


# ─── ОРКЕСТРАТОР ──────────────────────────────────────────────────────────────

class DebateOrchestrator:
    def __init__(self):
        self.bull      = BullResearcher()
        self.bear      = BearSkeptic()
        self.verifier  = DataVerifier()
        self.synth     = ConsensusSynth()
        self.writer    = Speechwriter()

    async def run_debate(
        self,
        news_context: str,
        market_data: str = "",
        custom_mode: bool = False,
        live_prices: str = "",
        profile_instruction: str = "",
        horizon: str | HorizonPack | None = None,
        stop_factor: str | None = None,
        market_prices: dict[str, float] | None = None,
    ) -> str:
        history = DebateHistory()
        rounds  = DEBATE_ROUNDS if not custom_mode else min(DEBATE_ROUNDS, 3)
        pack = horizon if isinstance(horizon, HorizonPack) else get_horizon(horizon if isinstance(horizon, str) else None)
        logger.info(f"Запускаю дебаты v8.0: {rounds} раундов, горизонт={pack.key} ({pack.label})")

        # Inject horizon overlay into Synth's system prompt (per-call, not global).
        # ConsensusSynth was constructed with SYNTH_BASE_SYSTEM; we splice the
        # horizon-specific block on top each run.
        self.synth.system_prompt = SYNTH_BASE_SYSTEM + synth_overlay(pack)

        full_context = ""
        if live_prices:
            full_context += "=== РЕАЛЬНЫЕ РЫНОЧНЫЕ ДАННЫЕ ===\n" + live_prices + "\n\n"
        full_context += "=== НОВОСТИ И ГЕОПОЛИТИКА ===\n" + news_context
        if market_data:
            full_context += "\n\n=== ДОП. ДАННЫЕ ===\n" + market_data
        if profile_instruction:
            full_context += "\n\n=== ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ===\n" + profile_instruction
        full_context += (
            f"\n\n=== ГОРИЗОНТ ПЛАНИРОВАНИЯ ===\n"
            f"{pack.label_pretty} — {pack.description}\n"
            f"Все аргументы и план должны быть рассчитаны под горизонт {pack.label}."
        )
        if stop_factor == "bearish":
            full_context += (
                "\n\n=== 🚨 КРИТИЧЕСКИЙ СТОП-ФАКТОР: МЕДВЕЖИЙ ===\n"
                "MVRV>3.5 / VIX>40 / эйфория VIX<15+F&G>70 — рынок в зоне риска.\n"
                "LONG-планы НЕ ДОПУСКАЮТСЯ (будут заменены на CASH автоматически). "
                "Даже если сценарий бычий — выводи CASH или SHORT, не LONG."
            )
        elif stop_factor == "bullish":
            full_context += (
                "\n\n=== 🔵 КРИТИЧЕСКИЙ СТОП-ФАКТОР: БЫЧИЙ ===\n"
                "MVRV<1.0 / F&G<25 — экстремальный страх / историческое дно.\n"
                "SHORT-планы НЕ ДОПУСКАЮТСЯ (будут заменены на CASH автоматически). "
                "Даже если сценарий медвежий — выводи CASH или LONG, не SHORT."
            )

        # Раунд 1 — Bull и Bear независимо
        logger.info("Раунд 1: Bull и Bear независимо...")
        empty_history    = DebateHistory()
        bull_r1, bear_r1 = await asyncio.gather(
            self.bull.respond(full_context, empty_history, round_num=1),
            self.bear.respond(full_context, empty_history, round_num=1)
        )
        history.add(f"{self.bull.emoji} {self.bull.name}", bull_r1, 1)
        history.add(f"{self.bear.emoji} {self.bear.name}", bear_r1, 1)

        # Раунд 2 — Verifier проверяет галлюцинации, Bull отвечает
        if rounds >= 2:
            logger.info("Раунд 2: Verifier охотится на галлюцинации...")
            verify_r2 = await self.verifier.respond(full_context, history, round_num=2)
            history.add(f"{self.verifier.emoji} {self.verifier.name}", verify_r2, 2)
            bull_r2 = await self.bull.respond_counter(full_context, history, round_num=2)
            history.add(f"{self.bull.emoji} {self.bull.name}", bull_r2, 2)

        # Раунд 3 — Bear добивает галлюцинации Bull
        if rounds >= 3:
            logger.info("Раунд 3: Bear добивает галлюцинации...")
            bear_r3 = await self.bear.respond_counter(full_context, history, round_num=3)
            history.add(f"{self.bear.emoji} {self.bear.name}", bear_r3, 3)

        # Доп раунды
        for extra_round in range(4, rounds + 1):
            bull_x = await self.bull.respond_counter(full_context, history, extra_round)
            history.add(f"{self.bull.emoji} {self.bull.name}", bull_x, extra_round)
            bear_x = await self.bear.respond_counter(full_context, history, extra_round)
            history.add(f"{self.bear.emoji} {self.bear.name}", bear_x, extra_round)

        logger.info("Финальный синтез: Synth (JSON) → Speechwriter (формат)...")
        
        # Шаг 1: Synth → компактный JSON
        synth_json = await self.synth.respond(full_context, history, round_num=rounds)
        logger.info(f"[SPEECHWRITER] Raw synth output: {synth_json[:300]}")
        
        # Шаг 2: Speechwriter → красивый форматированный текст (с hard-guard геометрии, горизонтом и stop-factor override)
        try:
            final_synthesis = await self.writer.format(
                synth_json,
                horizon=pack,
                stop_factor=stop_factor,
                market_prices=market_prices,
            )
        except Exception as e:
            logger.warning(f"[SPEECHWRITER] Error, using raw synth: {e}")
            final_synthesis = synth_json

        # ─── Hallucination tracking ───────────────────────────────────────────
        try:
            from ai_provider import track_hallucinations, log_hallucination_stats

            verifier_text = (history.last_message_by("Verifier") or "")[:2000]
            bull_all = " ".join(m.content for m in history.messages if "Bull" in m.agent)
            bear_all = " ".join(m.content for m in history.messages if "Bear" in m.agent)

            # Count total agent arguments (paragraphs that look like arguments)
            bull_args = max(1, len([p for p in bull_all.split("\n") if p.strip().startswith("•") or p.strip().startswith("-")]))
            bear_args = max(1, len([p for p in bear_all.split("\n") if p.strip().startswith("•") or p.strip().startswith("-")]))

            # Verifier marks hallucinations per-line; attribute each one to Bull or Bear
            # by looking for the agent name in the same line. Lines mentioning neither
            # are ignored (avoids the previous bug where ALL hallucinations were attributed
            # to Bull, and Bear count was a divided-by-2 hack).
            bull_halls = 0
            bear_halls = 0
            for line in verifier_text.splitlines():
                if "ГАЛЛЮЦИНАЦИЯ" not in line:
                    continue
                if "Bull" in line:
                    bull_halls += 1
                if "Bear" in line:
                    bear_halls += 1

            track_hallucinations("bull", bull_args, bull_halls)
            track_hallucinations("bear", bear_args, bear_halls)
            log_hallucination_stats()
            logger.info(f"[HALLUCINATION] Bull: {bull_halls}/{bull_args}, Bear: {bear_halls}/{bear_args}")
        except Exception as _e:
            logger.debug(f"[HALLUCINATION] Tracking skipped: {_e}")

        return self._format_report(history, final_synthesis, news_context, custom_mode)

    def _format_report(self, history, synthesis, news_context, custom_mode) -> str:
        now   = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        title = "🔍 *АНАЛИЗ НОВОСТИ*" if custom_mode else "📊 *DIALECTIC EDGE — DAILY*"

        try:
            from ai_provider import get_models_summary
            models_line = get_models_summary()
        except Exception:
            models_line = "🐂 Bull | 🐻 Bear | 🔍 Verifier | ⚖️ Synth → ✍️ Speechwriter"

        honest_header = (
            "💬 *Прежде чем читать:*\n"
            "Это структурированный AI-анализ на реальных данных.\n"
            f"{models_line}\n"
        )

        report_parts = [title, f"🕐 _{now}_", "", honest_header, "─" * 30, ""]
        report_parts.append("🗣 *ХОД ДЕБАТОВ*\n")

        curr_r = 0
        for m in history.messages:
            if m.round_num != curr_r:
                curr_r = m.round_num
                report_parts.append(f"\n*── Раунд {curr_r} ──*\n")
            report_parts.append(f"{m.agent}:\n{m.content}\n")

        report_parts.append("─" * 30)
        report_parts.append("⚖️ *ВЕРДИКТ И ТОРГОВЫЙ ПЛАН*\n")
        report_parts.append(synthesis)
        report_parts.append(DISCLAIMER)

        report = "\n".join(str(p) for p in report_parts)
        # Убираем иероглифы которые иногда добавляет Groq/Llama
        import re as _re
        report = _re.sub(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+', '', report)
        report = _re.sub(r'  +', ' ', report)
        return report
