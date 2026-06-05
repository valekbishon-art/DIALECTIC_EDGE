"""
dynamic_risk.py — Профессиональный риск-менеджмент.

- ATR-based стопы (не фиксированные %, а по реальной волатильности)
- Kelly Criterion для размера позиции (бутстрап из истории + персистент на диск)
- Max drawdown защита
- Correlation-adjusted sizing
- Position sizing по режиму рынка
- **Vol-targeting** (НОВОЕ): размер позиции обратно пропорционален реализованной
  волатильности — стратегия из institutional vol-targeted CTA-фондов.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskParams:
    """Параметры риска для одной сделки."""
    position_size_pct: float   # % капитала на сделку
    stop_loss_pct: float       # % стоп-лосс
    take_profit_pct: float     # % тейк-профит
    max_portfolio_risk: float  # Макс % капитала под риском
    kelly_fraction: float      # Доля Kelly (0.0-1.0)
    atr_stop_multiplier: float # ATR множитель для стопа
    max_drawdown_pct: float    # Остановка при просадке
    correlation_penalty: float # Штраф за коррелированные позиции


# Дефолтный файл для персистентности Kelly state между рестартами Railway.
DEFAULT_RISK_STATE_FILE = os.environ.get("RISK_STATE_FILE", "risk_state.json")

# Vol-targeting дефолты. target_vol_pct — «normalized» волатильность, на которую
# мы хотели бы видеть в позиции. BTC daily realized vol исторически ~3-5%, берём 3.0%
# как baseline. На high-vol-днях (напр. 8%) size уменьшится в ~2.7х;
# на quiet днях (1.5%) разрешаем до 2x.
VOL_TARGET_MIN_MULT = 0.35
VOL_TARGET_MAX_MULT = 2.0
VOL_TARGET_DEFAULT_PCT = 3.0


class DynamicRiskManager:
    """
    Профессиональный риск-менеджмент.
    
    Адаптирует размер позиции и стопы под:
    - Волатильность (ATR + vol-targeting)
    - Режим рынка
    - Историю винрейта (динамический Kelly на реальных данных)
    - Текущую просадку
    
    Персистит историю выигрышей/проигрышей на диск, чтобы Kelly переживал
    рестарты Railway.
    """

    def __init__(
        self,
        base_risk_pct: float = 0.02,       # 2% базовый риск
        kelly_fraction: float = 0.25,       # Quarter Kelly
        max_drawdown_pct: float = 0.25,     # Стоп при -25%
        atr_stop_multiplier: float = 2.0,   # 2x ATR стоп
        max_correlated_positions: int = 3,  # Макс коррелированных позиций
        target_vol_pct: float = VOL_TARGET_DEFAULT_PCT,  # Целевая реализ. вола-ть
        state_file: Optional[str] = None,   # Где персистить вин/лоссы
    ):
        self.base_risk_pct = base_risk_pct
        self.kelly_fraction = kelly_fraction
        self.max_drawdown_pct = max_drawdown_pct
        self.atr_stop_multiplier = atr_stop_multiplier
        self.max_correlated_positions = max_correlated_positions
        self.target_vol_pct = target_vol_pct
        self.state_file = state_file or DEFAULT_RISK_STATE_FILE

        # История для Kelly
        self._wins = 0
        self._losses = 0
        self._avg_win = 0.0
        self._avg_loss = 0.0
        self._total_pnl = 0.0
        self._peak_capital = 100.0
        self._current_capital = 100.0
        # Счётчик ПОДРЯД идущих проигрышей (сбрасывается на каждой победе).
        self._consecutive_losses = 0
        
        # Пытаемся восстановиться из state-файла. Не падаем если файла нет.
        self.load_state()

    def update_capital(self, capital: float):
        """Обновить текущий капитал."""
        self._current_capital = capital
        if capital > self._peak_capital:
            self._peak_capital = capital

    def record_trade(self, pnl_pct: float, is_win: bool, persist: bool = True):
        """Записать результат сделки для Kelly.
        
        Args:
            pnl_pct: PnL в %, положительный для побед, отрицательный для проигрышей.
            is_win: True если сделка выиграна (можно вывести из pnl_pct, но
                    некоторые консервативные стратегии считают <0.5R как loss).
            persist: Сохранить state на диск.
        """
        if is_win:
            self._wins += 1
            n = self._wins
            self._avg_win = self._avg_win + (abs(pnl_pct) - self._avg_win) / n
            self._consecutive_losses = 0
        else:
            self._losses += 1
            n = self._losses
            self._avg_loss = self._avg_loss + (abs(pnl_pct) - self._avg_loss) / n
            self._consecutive_losses += 1
        self._total_pnl += pnl_pct
        
        if persist:
            try:
                self.save_state()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"DynamicRiskManager.save_state failed: {e}")
    
    def bootstrap_from_history(
        self,
        wins: int,
        losses: int,
        avg_win_pct: float,
        avg_loss_pct: float,
        total_pnl_pct: float = 0.0,
    ) -> None:
        """Заполнить историю Kelly из внешнего источника (BACKTEST.md, session_manager).
        
        Не сбрасывает существующее состояние если оно уже больше предложенного
        (защита от перезаписи свежих данных стейлом).
        """
        if wins + losses < 1:
            return
        if (wins + losses) <= (self._wins + self._losses):
            logger.debug(
                f"bootstrap_from_history: skipping ({wins}W/{losses}L) — "
                f"in-memory state ({self._wins}W/{self._losses}L) уже больше"
            )
            return
        self._wins = wins
        self._losses = losses
        self._avg_win = abs(avg_win_pct) if avg_win_pct else 0.0
        self._avg_loss = abs(avg_loss_pct) if avg_loss_pct else 0.0
        self._total_pnl = total_pnl_pct
        logger.info(
            f"DynamicRiskManager bootstrapped: {wins}W/{losses}L "
            f"avg_win={self._avg_win:.2f}% avg_loss={self._avg_loss:.2f}%"
        )
    
    # ── Persistence ────────────────────────────────────────────────────────
    
    def state_to_dict(self) -> dict:
        return {
            "wins": self._wins,
            "losses": self._losses,
            "avg_win": self._avg_win,
            "avg_loss": self._avg_loss,
            "total_pnl": self._total_pnl,
            "peak_capital": self._peak_capital,
            "current_capital": self._current_capital,
            "consecutive_losses": self._consecutive_losses,
        }
    
    def state_from_dict(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self._wins = int(data.get("wins") or 0)
        self._losses = int(data.get("losses") or 0)
        self._avg_win = float(data.get("avg_win") or 0.0)
        self._avg_loss = float(data.get("avg_loss") or 0.0)
        self._total_pnl = float(data.get("total_pnl") or 0.0)
        self._peak_capital = float(data.get("peak_capital") or 100.0)
        self._current_capital = float(data.get("current_capital") or 100.0)
        self._consecutive_losses = int(data.get("consecutive_losses") or 0)
    
    def save_state(self) -> None:
        """Сохранить state на диск (атомарно через .tmp + rename)."""
        if not self.state_file:
            return
        path = Path(self.state_file)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.state_to_dict(), indent=2))
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"save_state error: {e}")
    
    def load_state(self) -> bool:
        """Загрузить state с диска. Returns True если успешно."""
        if not self.state_file:
            return False
        path = Path(self.state_file)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text())
            self.state_from_dict(data)
            logger.info(
                f"DynamicRiskManager loaded: {self._wins}W/{self._losses}L "
                f"avg_win={self._avg_win:.2f}% avg_loss={self._avg_loss:.2f}% "
                f"capital=${self._current_capital:.2f}"
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"load_state error: {e}")
            return False
    
    # ── Vol-targeting ───────────────────────────────────────────────────────
    
    def vol_targeting_multiplier(self, realized_vol_pct: float) -> float:
        """Множитель vol-targeting: target_vol / realized_vol, клампленный.
        
        Args:
            realized_vol_pct: реализованная волатильность в % (daily, например 3.5).
        Returns:
            multiplier в диапазоне [VOL_TARGET_MIN_MULT, VOL_TARGET_MAX_MULT].
            На high-vol днях возвращает <1, уменьшая позицию;
            на quiet днях >1, увеличивая.
        """
        if realized_vol_pct is None or realized_vol_pct <= 0:
            return 1.0
        mult = self.target_vol_pct / realized_vol_pct
        return max(VOL_TARGET_MIN_MULT, min(VOL_TARGET_MAX_MULT, mult))

    def kelly_percentage(self) -> float:
        """
        Kelly Criterion: f* = (bp - q) / b
        b = avg_win / avg_loss
        p = win_rate
        q = 1 - p
        """
        if self._wins + self._losses < 10:
            return self.base_risk_pct  # Недостаточно данных

        p = self._wins / (self._wins + self._losses)
        q = 1 - p

        if self._avg_loss == 0:
            return self.base_risk_pct

        b = self._avg_win / self._avg_loss
        kelly = (b * p - q) / b

        # Quarter Kelly для безопасности
        return max(0, kelly * self.kelly_fraction)

    def should_stop_trading(self) -> tuple[bool, str]:
        """Проверить, нужно ли остановить торговлю."""
        # Drawdown check
        if self._peak_capital > 0:
            drawdown = (self._peak_capital - self._current_capital) / self._peak_capital
            if drawdown >= self.max_drawdown_pct:
                return True, f"Max drawdown reached: {drawdown*100:.1f}%"

        # Losing streak check — по ПОДРЯД идущим проигрышам, а не «никогда не было побед»
        if self._consecutive_losses >= 5:
            return True, f"{self._consecutive_losses} consecutive losses"

        return False, ""

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_price: float,
        atr: float = 0,
        regime: str = "NEUTRAL",
        correlation_count: int = 0,
        realized_vol_pct: float = 0.0,
        direction: str = "BUY",
    ) -> dict:
        """
        Рассчитать размер позиции.
        
        Args:
            capital: текущий капитал.
            entry_price, stop_price: цены.
            atr: ATR в долларах (если 0 — используем дистанцию entry-stop).
            regime: рыночный режим (UPTREND/DOWNTREND/SIDEWAYS/HIGH_VOL/...).
            correlation_count: число коррелированных позиций уже в портфеле.
            realized_vol_pct: реализованная вола-ть в % (daily, для vol-targeting).
                              Если 0 — vol-targeting не применяется.
            direction: BUY или SELL. Для SELL стоп выше entry.
        
        Returns:
            {
                "quantity": float,            # Сколько единиц купить
                "position_value": float,      # $ стоимость
                "risk_amount": float,         # $ под риском
                "risk_pct": float,            # % капитала под риском
                "stop_price": float,          # Итоговый стоп
                "take_profit": float,         # Итоговый тейк
                "kelly_pct": float,           # Kelly рекомендация
                "regime_multiplier": float,   # Регимический мультипликатор
                "vol_multiplier": float,      # Vol-targeting мультипликатор
                "kelly_used": bool,           # Использован Kelly (а не base)
            }
        """
        if entry_price <= 0:
            return {"error": "invalid_entry"}

        # 1. ATR-based стоп
        if atr > 0:
            atr_stop_distance = atr * self.atr_stop_multiplier
            stop_distance = max(atr_stop_distance, abs(entry_price - stop_price))
        else:
            stop_distance = abs(entry_price - stop_price)

        if stop_distance == 0:
            stop_distance = entry_price * 0.02  # Fallback 2%

        # Стоп для BUY ниже, для SELL выше
        if (direction or "BUY").upper() in {"SELL", "SHORT"}:
            final_stop = entry_price + stop_distance
            take_profit = entry_price - (stop_distance * 1.5)
        else:
            final_stop = entry_price - stop_distance
            take_profit = entry_price + (stop_distance * 1.5)

        # 2. Динамический Kelly (на реальной истории если она есть, иначе base)
        kelly_pct = self.kelly_percentage()
        kelly_used = (self._wins + self._losses) >= 10
        # Берём min(kelly, 2x base) чтобы не превысить здравый смысл, но
        # также не падать ниже base если Kelly даёт меньше (база — пол).
        if kelly_used:
            # Нет положительного края (Kelly <= 0) → не торгуем вообще.
            if kelly_pct <= 0:
                return {
                    "quantity": 0.0,
                    "position_value": 0.0,
                    "risk_amount": 0.0,
                    "risk_pct": 0.0,
                    "stop_price": round(final_stop, 4),
                    "take_profit": round(take_profit, 4),
                    "kelly_pct": round(kelly_pct * 100, 2),
                    "regime_multiplier": 0.0,
                    "vol_multiplier": 0.0,
                    "kelly_used": True,
                    "skipped": True,
                    "skip_reason": "non_positive_kelly_edge",
                }
            risk_pct = min(kelly_pct, self.base_risk_pct * 2)
            risk_pct = max(risk_pct, self.base_risk_pct * 0.5)
        else:
            risk_pct = self.base_risk_pct

        # 3. Режим рынка
        regime_multipliers = {
            "UPTREND": 1.0,
            "DOWNTREND": 0.7,
            "SIDEWAYS": 0.4,
            "HIGH_VOL": 0.5,
        }
        regime_mult = regime_multipliers.get((regime or "").upper(), 0.7)
        risk_pct *= regime_mult

        # 4. Vol-targeting: если на дворе high realized vol, режем размер.
        vol_mult = self.vol_targeting_multiplier(realized_vol_pct)
        risk_pct *= vol_mult

        # 5. Корреляционный штраф
        if correlation_count >= self.max_correlated_positions:
            risk_pct *= 0.3  # Сильное снижение
        elif correlation_count > 0:
            risk_pct *= (1 - correlation_count * 0.15)

        # 6. Drawdown adjustment
        if self._peak_capital > 0:
            drawdown = (self._peak_capital - self._current_capital) / self._peak_capital
            if drawdown > 0.1:
                risk_pct *= max(0.3, 1 - drawdown * 2)

        # 7. Финальный расчёт
        risk_amount = capital * risk_pct
        quantity = risk_amount / stop_distance
        position_value = quantity * entry_price

        return {
            "quantity": round(quantity, 8),
            "position_value": round(position_value, 2),
            "risk_amount": round(risk_amount, 2),
            "risk_pct": round(risk_pct * 100, 2),
            "stop_price": round(final_stop, 4),
            "take_profit": round(take_profit, 4),
            "kelly_pct": round(kelly_pct * 100, 2),
            "regime_multiplier": regime_mult,
            "vol_multiplier": round(vol_mult, 3),
            "kelly_used": kelly_used,
        }

    def get_risk_summary(self) -> dict:
        """Сводка текущего состояния риска."""
        drawdown = 0.0
        if self._peak_capital > 0:
            drawdown = (self._peak_capital - self._current_capital) / self._peak_capital

        total_trades = self._wins + self._losses
        win_rate = self._wins / total_trades if total_trades > 0 else 0

        return {
            "current_capital": round(self._current_capital, 2),
            "peak_capital": round(self._peak_capital, 2),
            "drawdown_pct": round(drawdown * 100, 2),
            "total_trades": total_trades,
            "wins": self._wins,
            "losses": self._losses,
            "win_rate": round(win_rate * 100, 1),
            "avg_win": round(self._avg_win, 2),
            "avg_loss": round(self._avg_loss, 2),
            "kelly_pct": round(self.kelly_percentage() * 100, 2),
            "kelly_using_history": total_trades >= 10,
            "target_vol_pct": self.target_vol_pct,
            "total_pnl": round(self._total_pnl, 2),
            "should_stop": self.should_stop_trading()[0],
            "stop_reason": self.should_stop_trading()[1],
        }
