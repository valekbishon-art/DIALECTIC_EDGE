"""Optional Sentry SDK setup with a secret-redacting ``before_send`` filter.

Activation is **fully opt-in**:

* If ``SENTRY_DSN`` is empty or unset → :func:`setup_sentry` is a no-op.
* If the ``sentry_sdk`` package is not installed → no-op (log warning once).
* Otherwise: init with conservative defaults (errors only, no PII, no
  performance tracing) and install a redactor that scrubs API tokens out of
  exception messages, breadcrumbs, and stack frame locals.

Why a redactor: ``sentry_sdk`` by default sends repr() of locals at the
crash site. In our codebase, local variables routinely hold ``BOT_TOKEN``,
``GITHUB_TOKEN``, OpenRouter keys, etc. We must NOT ship them to a third
party. The redactor matches well-known secret prefixes (``ghp_``, ``sk-``,
``xoxb-``, …) and replaces any string containing them with ``"[REDACTED]"``.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Heuristic patterns for high-confidence secrets. Conservative on purpose:
# false positives (= a redaction) are cheap; false negatives (= a leak) are
# expensive. Tweak via ``SENTRY_SECRET_PATTERNS`` env (CSV of regex).
_DEFAULT_SECRET_PATTERNS: tuple[str, ...] = (
    r"ghp_[A-Za-z0-9]{16,}",         # GitHub classic PAT
    r"gho_[A-Za-z0-9]{16,}",         # GitHub OAuth
    r"github_pat_[A-Za-z0-9_]{20,}", # GitHub fine-grained PAT
    r"sk-[A-Za-z0-9_\-]{20,}",       # OpenAI / OpenRouter style
    r"sk-or-v1-[A-Za-z0-9]{20,}",    # OpenRouter explicit
    r"xoxb-[A-Za-z0-9\-]{20,}",      # Slack
    r"xapp-[A-Za-z0-9\-]{20,}",      # Slack app
    r"AIza[0-9A-Za-z_\-]{20,}",      # Google API
    r"AKIA[0-9A-Z]{16}",             # AWS access key
    # Telegram bot token: digits ":" 35-char alnum/_/- chunk.
    r"\d{6,12}:[A-Za-z0-9_\-]{30,}",
)

# Keys whose VALUES we redact unconditionally (e.g. variable names ending
# in ``_TOKEN``, ``_SECRET``, ``_KEY``, ``_DSN``, ``_PAT``).
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|_)(token|secret|api_?key|password|passwd|dsn|pat|auth(?:orization)?)(?:$|_)"
)

REDACTED = "[REDACTED]"


def _compile_patterns(extra_csv: Optional[str] = None) -> list[re.Pattern[str]]:
    patterns = list(_DEFAULT_SECRET_PATTERNS)
    if extra_csv:
        for raw in extra_csv.split(","):
            piece = raw.strip()
            if piece:
                patterns.append(piece)
    compiled: list[re.Pattern[str]] = []
    for pat in patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error as exc:
            logger.warning("sentry_setup: invalid pattern %r ignored: %s", pat, exc)
    return compiled


def _scrub_string(value: str, patterns: list[re.Pattern[str]]) -> str:
    out = value
    for pat in patterns:
        out = pat.sub(REDACTED, out)
    return out


def _scrub_obj(obj: Any, patterns: list[re.Pattern[str]], depth: int = 0) -> Any:
    """Recursively walk a dict/list/tuple/string, redacting secret-looking text.

    Stops at ``depth=8`` to avoid stack-overflow on cyclic payloads
    (sentry events are JSON-shaped; 8 is a generous ceiling).
    """
    if depth > 8:
        return obj
    if isinstance(obj, str):
        return _scrub_string(obj, patterns)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key_is_sensitive = isinstance(k, str) and bool(_SENSITIVE_KEY_RE.search(k))
            if key_is_sensitive:
                out[k] = REDACTED if v else v
            else:
                out[k] = _scrub_obj(v, patterns, depth + 1)
        return out
    if isinstance(obj, list):
        return [_scrub_obj(item, patterns, depth + 1) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_obj(item, patterns, depth + 1) for item in obj)
    return obj


def make_before_send(patterns: Optional[list[re.Pattern[str]]] = None) -> Callable[[dict, Any], dict]:
    """Build a ``before_send`` callable closing over the compiled patterns."""
    pats = patterns if patterns is not None else _compile_patterns(
        os.getenv("SENTRY_SECRET_PATTERNS")
    )

    def _before_send(event: dict, hint: Any) -> dict:  # noqa: ARG001 — hint required by sentry
        try:
            return _scrub_obj(event, pats)
        except Exception as exc:  # noqa: BLE001 — never break the SDK
            logger.warning("sentry_setup: scrub failed, sending raw event: %s", exc)
            return event

    return _before_send


def setup_sentry(
    dsn: Optional[str] = None,
    *,
    environment: Optional[str] = None,
    release: Optional[str] = None,
) -> bool:
    """Initialise Sentry if both DSN and SDK are available.

    Returns ``True`` on successful init, ``False`` otherwise. Never raises.

    Reads from env when args are ``None``:
        ``SENTRY_DSN`` — full DSN URL (required to enable)
        ``SENTRY_ENVIRONMENT`` — default ``production``
        ``SENTRY_RELEASE`` — git sha or version tag (optional)
    """
    resolved_dsn = (dsn if dsn is not None else os.getenv("SENTRY_DSN", "")).strip()
    if not resolved_dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        logger.warning(
            "sentry_setup: SENTRY_DSN is set but `sentry-sdk` is not installed — "
            "skipping. Add `sentry-sdk` to requirements.txt to enable."
        )
        return False

    resolved_env = environment or os.getenv("SENTRY_ENVIRONMENT") or "production"
    resolved_release = release or os.getenv("SENTRY_RELEASE") or None

    try:
        sentry_sdk.init(
            dsn=resolved_dsn,
            environment=resolved_env,
            release=resolved_release,
            traces_sample_rate=0.0,  # errors only, no perf overhead
            send_default_pii=False,
            attach_stacktrace=True,
            before_send=make_before_send(),
        )
    except Exception as exc:  # noqa: BLE001 — must never fatal the bot
        logger.warning("sentry_setup: init failed: %s", exc)
        return False

    logger.info(
        "sentry_setup: initialised (env=%s, release=%s)", resolved_env, resolved_release or "-",
    )
    return True
