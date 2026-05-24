"""Generic alert engine: rules + cooldown + severity formatting.

A small framework that lets each alert source (anomaly screener, ETF flow
tracker, liquidation cluster watcher, etc.) be implemented as an independent
`AlertRule`. The engine evaluates rules, deduplicates emitted cards against a
`JsonAlertStore`-style store and returns the cards ready to be sent.

Design goals:
- stdlib-only (HTTP and aiohttp belong to individual rules, not the engine).
- side-effect free: the engine returns cards, the caller (scheduler) sends.
- per-rule cooldown driven by ``AlertCard.dedup_key`` so multiple cards from a
  single rule (e.g. one per symbol) cool down independently.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Protocol, Sequence

logger = logging.getLogger(__name__)


VALID_SEVERITIES = ("INFO", "WARN", "CRIT")
SEVERITY_EMOJI = {"INFO": "🟢", "WARN": "🟡", "CRIT": "🔴"}


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(float(raw))
        except (ValueError, TypeError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = float(raw)
        except (ValueError, TypeError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def feature_enabled() -> bool:
    """Parent feature flag for the whole engine."""
    return _env_flag("FEATURE_ALERT_ENGINE", False)


def get_interval_sec() -> int:
    return _env_int("ALERT_ENGINE_INTERVAL_SEC", 300, minimum=30, maximum=3600)


def get_chat_ids(fallback: Sequence[int]) -> tuple[int, ...]:
    raw = os.getenv("ALERT_ENGINE_CHAT_IDS", "")
    if not raw.strip():
        return tuple(int(x) for x in fallback)
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            continue
    return tuple(out)


@dataclass(frozen=True)
class AlertCard:
    """A single alert emitted by a rule, ready to be sent."""

    rule_id: str
    severity: str
    title: str
    body: str
    dedup_key: str
    fetched_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(
                f"invalid severity {self.severity!r}; expected one of {VALID_SEVERITIES}"
            )

    @property
    def emoji(self) -> str:
        return SEVERITY_EMOJI.get(self.severity, "🟢")


class AlertRule(Protocol):
    """Protocol every concrete alert rule must implement."""

    rule_id: str
    cooldown_sec: int

    async def check(self) -> Sequence[AlertCard]:  # pragma: no cover - protocol stub
        ...


class _StoreLike(Protocol):
    def should_alert(self, key: str, cooldown_sec: int) -> bool: ...

    def record_alert(self, key: str) -> None: ...


@dataclass
class AlertEngine:
    """Evaluates a set of rules and filters their cards through a cooldown store."""

    rules: list[AlertRule]
    store: _StoreLike

    async def evaluate_all(
        self,
        *,
        now: float | None = None,
        rule_timeout_sec: float = 20.0,
    ) -> list[AlertCard]:
        """Run all rules concurrently and return cards that pass cooldown.

        ``now`` is unused right now (cooldowns are computed by the store), but
        exposed for future testability around time travel.
        """
        if not self.rules:
            return []

        results: list[Sequence[AlertCard] | BaseException] = await asyncio.gather(
            *(self._run_rule(r, rule_timeout_sec) for r in self.rules),
            return_exceptions=True,
        )

        out: list[AlertCard] = []
        for rule, res in zip(self.rules, results):
            if isinstance(res, BaseException):
                logger.warning("alert rule %s failed: %s", rule.rule_id, res)
                continue
            for card in res:
                key = f"{card.rule_id}:{card.dedup_key}"
                if not self.store.should_alert(key, rule.cooldown_sec):
                    continue
                out.append(card)
                self.store.record_alert(key)
        return out

    async def _run_rule(
        self, rule: AlertRule, timeout_sec: float
    ) -> Sequence[AlertCard]:
        try:
            return await asyncio.wait_for(rule.check(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.warning(
                "alert rule %s timed out after %.1fs", rule.rule_id, timeout_sec
            )
            return ()


def format_alert_card(card: AlertCard, *, age_now: float | None = None) -> str:
    """Render an AlertCard as a Telegram Markdown message."""
    now = age_now if age_now is not None else time.time()
    age_sec = max(0, int(now - card.fetched_at))
    if age_sec < 60:
        age = f"{age_sec}s"
    elif age_sec < 3600:
        age = f"{age_sec // 60}m"
    else:
        age = f"{age_sec // 3600}h"

    return (
        f"{card.emoji} *{card.severity}* — *{card.title}*\n\n"
        f"{card.body}\n\n"
        f"_rule: `{card.rule_id}` · age: {age}_"
    )


@dataclass
class CallableRule:
    """Adapter that turns an async function into an `AlertRule`.

    Useful for tests and for wiring small rules without subclassing.
    """

    rule_id: str
    cooldown_sec: int
    fn: Callable[[], Awaitable[Iterable[AlertCard]]]

    async def check(self) -> Sequence[AlertCard]:
        cards = await self.fn()
        return tuple(cards)
