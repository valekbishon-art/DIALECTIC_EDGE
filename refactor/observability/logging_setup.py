"""Centralised logging setup with optional JSON output.

Why: previously ``main.py`` just called ``logging.basicConfig(level=INFO, ...)``
with a text format. That worked for local dev but on Railway:

* No control over level via env (always INFO; can't lower to DEBUG without
  redeploy).
* Plain text — hard to parse into a log aggregator if/when we plug one in.
* Third-party libraries (``aiohttp.access``, ``urllib3``, ``asyncio``,
  ``aiogram.event``) are very chatty at INFO level and drown out our own logs.

This module exposes :func:`setup_logging`, which:

1. Reads ``LOG_LEVEL`` env (default ``INFO``). Invalid values fall back to INFO.
2. Reads ``LOG_FORMAT`` env: ``json`` switches to a one-line JSON formatter,
   anything else keeps the human-readable text format (default).
3. Quiets a configurable set of noisy 3rd-party loggers (``LOG_QUIET_LIBS``).
4. Installs a handler on the *root* logger so all modules (signal_trader,
   agents, signals, refactor.*) emit consistently.

Safe to call multiple times — re-applies idempotently.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Iterable, Optional

DEFAULT_TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_QUIET_LIBS: tuple[str, ...] = (
    "aiohttp.access",
    "aiohttp.server",
    "urllib3",
    "asyncio",
    "aiogram.event",
    "aiogram.dispatcher",
)

# Attributes set by the stdlib LogRecord that we do NOT want to dump as "extra"
# fields in JSON output — they would just duplicate info.
_LOGRECORD_RESERVED = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName",
    "taskName",  # py3.12+
})


class JsonFormatter(logging.Formatter):
    """Render every record as one JSON line.

    Output shape:
        {"ts": "2025-05-21T12:34:56.789+00:00",
         "level": "INFO",
         "logger": "signal_trader",
         "msg": "loop tick done",
         ... any custom kwargs passed via logger.info(..., extra={...}) ...}

    Exceptions are flattened into the ``exc`` field.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: dict[str, object] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge any user-provided extras (anything not in the reserved set).
        for key, value in record.__dict__.items():
            if key in _LOGRECORD_RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value, default=str)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _normalise_level(raw: Optional[str], fallback: int = logging.INFO) -> int:
    if not raw:
        return fallback
    name = raw.strip().upper()
    if not name:
        return fallback
    level = logging.getLevelName(name)
    # ``getLevelName("FOOBAR")`` returns ``"Level FOOBAR"`` (str). Guard that.
    if isinstance(level, int):
        return level
    return fallback


def _split_csv(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def setup_logging(
    level: Optional[str] = None,
    *,
    fmt: Optional[str] = None,
    quiet_libs: Optional[Iterable[str]] = None,
    stream: Optional[object] = None,
) -> int:
    """Configure root logger and return the resolved numeric level.

    Args:
        level: Override for ``LOG_LEVEL`` env (case-insensitive: ``debug`` /
            ``INFO`` / ``Warning``). Invalid → INFO.
        fmt: Override for ``LOG_FORMAT`` env. ``"json"`` enables JSON,
            otherwise text.
        quiet_libs: Override for ``LOG_QUIET_LIBS`` env (csv). Each named
            logger gets its level raised to WARNING.
        stream: Stream for the StreamHandler (default ``sys.stderr``).
    """
    resolved_level = _normalise_level(level if level is not None else os.getenv("LOG_LEVEL"))
    resolved_fmt = (fmt if fmt is not None else os.getenv("LOG_FORMAT", "")).strip().lower()

    formatter: logging.Formatter
    if resolved_fmt == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(DEFAULT_TEXT_FORMAT)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)
    handler.setLevel(resolved_level)

    root = logging.getLogger()
    # Wipe whatever ``logging.basicConfig`` (or an earlier setup_logging call)
    # installed — we want a single, predictable handler.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    if quiet_libs is None:
        names = _split_csv(os.getenv("LOG_QUIET_LIBS")) or list(DEFAULT_QUIET_LIBS)
    else:
        names = [n for n in quiet_libs if n]
    for name in names:
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).debug(
        "logging configured: level=%s format=%s quiet=%s",
        logging.getLevelName(resolved_level), resolved_fmt or "text", ",".join(names),
    )
    return resolved_level
