# ☪️ Вырезание харама — ветка `halal-only`

> Задача: убрать из проекта ВСЁ харамное (риба/гарар/майсир/шорт/плечо/деривативы), построить
> халяль-замены. Сделано на ветке `halal-only` — харам-версия цела в git, всё восстановимо.
> Обновлено: 2026-06-17. ⚠️ Не фетва; применён мейнстрим-фикх.

## ✅ Вырезано (standalone — чисто, без поломки ядра)

**Скрипты/ресёрч (entry-points, никто не импортит):** scripts/{fetch_funding, funding_scanner,
arb_scanner, pump_edge_search, pump_fade_backtest, pump_pnd_backtest, pump_pnd_confirm,
research_basis_carry, research_cointegration, research_stablecoin}, research/{carry_alloc_backtest,
cascade_event_study}.
**Тесты харам-модулей:** test_{pump_fade, carry_briefing, funding_term_math, funding_interval,
cross_exchange, cascade_post_mortem, carry_alloc, best_edge, basis_carry, arb_*}.
**Наши сессионные харам-артефакты:** все *_backtest.py по carry/basis/funding/cvd/oi/statarb/xexch/
pump + их вердикты (CVD/OI_RAMP/PUMP_*/STATARB/FUNDING_CARRY/XEXCH_CARRY/XS_REVERSAL .md) +
данные data/{funding, klines_taker, oi_5m}.

## ✅ Построены халяль-замены (спот, лонг, без плеча/шорта/процента)

| Харам-фича | Халяль-замена | Файл | Статус |
|------------|---------------|------|--------|
| Funding/carry/basis scanner | **Трендфолловинг** (спот, в активе>SMA50 иначе стейбл) | `halal_trend.py` | 🟢 боевой: сигнал сейчас + бэктест CAGR+14.7%/maxDD−52% (vs −83% buy-hold) |
| Кросс-биржевой funding-арб | **Спот-арб** (купи реальную монету дешевле/продай дороже, владение) | `scripts/spot_arb_scanner.py` | 🟢 работает; честно: ликвид-спред ~0, живёт на тонких парах |
| Базис бэктест (был) | — | — | удалён (харам) |

Оставлены халяльные: `halal_backtest.py`, `HALAL_STRATEGIES.md`, `scripts/{fetch_klines,
fetch_daily_klines}`, `scripts/research_smallcap_mom.py` (лонг-онли моментум), data/{klines_1m, daily}.

## ⚠️ Осталось: глубокое раз-вязывание ЯДРА (нужен тест-проход)

Харам-логика **вплетена в оркестраторы** — модули импортятся ~46 файлами (main.py, scheduler.py,
analysis_service.py, signals.py, signal_trader.py, market_indicators/aggregator). Слепо удалять
нельзя — сломается импортами, а бот тут не запустить для проверки. План (по приоритету):

**Харам-модули ядра к удалению + де-вайр их импортов:**
- `core/`: carry_signal, basis_carry, cross_exchange, carry_briefing, best_edge, pump_fade
- `market_indicators/`: funding_term_structure(+_io), liquidation_magnet(+_io), options_skew(+_io),
  cascade_post_mortem(+_io)  ← деривативы/фандинг/ликвидации
- root: best_deal_alert, pump_alert, pump_backtest, pump_scanner, (signal_trader/signals — содержат
  шорт/плечо-сигналы, нужна ревизия: оставить лонг, вырезать шорт)
- `refactor/handlers/`: funding_handler, pump_handler, sniping_handler (+ снять регистрацию)

**Шаги де-вайра (на ветке, по одному, с прогоном тестов после каждого):**
1. Снять регистрацию харам-хендлеров в `refactor/handlers/__init__.py` + удалить их файлы.
2. Вырезать харам-индикаторы из `market_indicators/aggregator.py` (убрать funding/liq/options/cascade
   из агрегации) + удалить модули.
3. Убрать импорты/вызовы carry/basis/cross_exchange/pump_fade из main.py, scheduler.py,
   analysis_service.py, signals.py (удалить команды/джобы/ветки, оставить спот-лонг-анализ).
4. Прогнать `pytest` → чинить упавшее → пока зелено.

**Пограничное (оставлено, требует твоего решения):** `p2p_arbitrage*` (спот-крипто↔фиат — возможно
халяль как купечество, но есть нюанс сарф), `smart_money*`/`whale_detector` (трекинг китов —
детекция, не харам сам по себе), `microstructure` (спот-поток ок).

## Откат / проверка
```bash
git checkout fix/arb-carry-antispam   # вернуться к харам-версии (всё цело)
git checkout halal-only               # халяльная ветка
py halal_trend.py                     # халяль-сигнал + бэктест
py scripts/spot_arb_scanner.py        # халяль спот-арб сканер
```
