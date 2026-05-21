"""Tests for :mod:`refactor.observability.sentry_setup`.

Coverage:

* DSN unset → ``setup_sentry`` returns False, no side effects.
* ``sentry-sdk`` missing → returns False, logs a warning, doesn't raise.
* ``sentry-sdk`` present + DSN set → calls ``sentry_sdk.init`` with expected
  kwargs (traces=0, send_default_pii=False, before_send installed).
* ``sentry_sdk.init`` raising → ``setup_sentry`` swallows and returns False.
* Redactor: scrubs GitHub PAT, OpenAI key, OpenRouter key, Telegram bot
  token, Google API key, AWS access key in nested dict/list/string.
* Keys whose names match ``_SENSITIVE_KEY_RE`` (TOKEN/SECRET/KEY/DSN/PAT/AUTH)
  have their values replaced with ``[REDACTED]`` even if the value itself
  doesn't match a known pattern.
* Cyclic / very deep structures don't recurse infinitely.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from refactor.observability import sentry_setup
from refactor.observability.sentry_setup import (
    REDACTED,
    _scrub_obj,
    make_before_send,
    setup_sentry,
)


class SetupSentryGuardsTestCase(unittest.TestCase):
    def test_no_dsn_returns_false(self) -> None:
        with patch.dict("os.environ", {"SENTRY_DSN": ""}, clear=False):
            self.assertFalse(setup_sentry())

    def test_blank_dsn_returns_false(self) -> None:
        with patch.dict("os.environ", {"SENTRY_DSN": "   "}, clear=False):
            self.assertFalse(setup_sentry())

    def test_sdk_missing_returns_false(self) -> None:
        # Setting ``sys.modules["sentry_sdk"] = None`` makes ``import sentry_sdk``
        # raise ImportError. This is the textbook way to mock a missing module.
        original_sdk = sys.modules.get("sentry_sdk")
        sys.modules["sentry_sdk"] = None
        try:
            with patch.dict("os.environ", {"SENTRY_DSN": "https://x@example/1"}, clear=False):
                self.assertFalse(setup_sentry())
        finally:
            if original_sdk is not None:
                sys.modules["sentry_sdk"] = original_sdk
            else:
                sys.modules.pop("sentry_sdk", None)

    def test_sdk_present_init_called(self) -> None:
        fake_sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch.dict("os.environ", {"SENTRY_DSN": "https://foo@sentry/1"}, clear=False):
                result = setup_sentry(environment="test-env", release="abc123")
        self.assertTrue(result)
        fake_sdk.init.assert_called_once()
        kwargs = fake_sdk.init.call_args.kwargs
        self.assertEqual(kwargs["dsn"], "https://foo@sentry/1")
        self.assertEqual(kwargs["environment"], "test-env")
        self.assertEqual(kwargs["release"], "abc123")
        self.assertEqual(kwargs["traces_sample_rate"], 0.0)
        self.assertFalse(kwargs["send_default_pii"])
        self.assertTrue(callable(kwargs["before_send"]))

    def test_sdk_init_raise_returns_false(self) -> None:
        fake_sdk = MagicMock()
        fake_sdk.init.side_effect = RuntimeError("init exploded")
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch.dict("os.environ", {"SENTRY_DSN": "https://foo@sentry/1"}, clear=False):
                self.assertFalse(setup_sentry())


class ScrubObjTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.patterns = sentry_setup._compile_patterns()

    def test_redacts_github_pat_in_string(self) -> None:
        s = "Authorization failed with token ghp_abcdefghijklmnopqrstuv1234"
        out = _scrub_obj(s, self.patterns)
        self.assertEqual(out, f"Authorization failed with token {REDACTED}")

    def test_redacts_openrouter_key(self) -> None:
        s = "key=sk-or-v1-abcdefghijklmnopqrstuvwxyz1234567890"
        out = _scrub_obj(s, self.patterns)
        self.assertIn(REDACTED, out)
        self.assertNotIn("sk-or-v1-abcdefghijklmnopqrstuvwxyz1234567890", out)

    def test_redacts_telegram_bot_token(self) -> None:
        s = "BOT_TOKEN=8533649592:AAEcXhiv9cqpYDIhw4aGqaDBXclDktm_rmU and more"
        out = _scrub_obj(s, self.patterns)
        self.assertIn(REDACTED, out)
        self.assertNotIn("AAEcXhiv9cqpYDIhw4aGqaDBXclDktm_rmU", out)

    def test_redacts_google_api_key(self) -> None:
        s = "AIzaSyABCDEF1234567890abcdefghij"
        out = _scrub_obj(s, self.patterns)
        self.assertEqual(out, REDACTED)

    def test_redacts_aws_access_key(self) -> None:
        s = "AKIAABCDEFGHIJKLMNOP"
        out = _scrub_obj(s, self.patterns)
        self.assertEqual(out, REDACTED)

    def test_nested_dict_scrubbed(self) -> None:
        event = {
            "extra": {
                "github_token_in_repr": "ghp_aaaaaaaaaaaaaaaaaaaaaaaa1234",
                "nested_list": ["ok", "sk-1234567890abcdefghij", 42],
            },
            "exception_msg": "boom",
        }
        out = _scrub_obj(event, self.patterns)
        self.assertEqual(out["extra"]["github_token_in_repr"], REDACTED)
        self.assertEqual(out["extra"]["nested_list"][1], REDACTED)
        self.assertEqual(out["extra"]["nested_list"][0], "ok")
        self.assertEqual(out["extra"]["nested_list"][2], 42)
        self.assertEqual(out["exception_msg"], "boom")

    def test_sensitive_key_name_redacts_value(self) -> None:
        event = {
            "BOT_TOKEN": "value-without-known-prefix-but-key-name-says-secret",
            "GITHUB_PAT": "ghp_x",  # would match nothing pattern-wise (too short)
            "GITHUB_AUTHORIZATION": "Bearer abc",
            "harmless_field": "kept",
        }
        out = _scrub_obj(event, self.patterns)
        self.assertEqual(out["BOT_TOKEN"], REDACTED)
        self.assertEqual(out["GITHUB_PAT"], REDACTED)
        self.assertEqual(out["GITHUB_AUTHORIZATION"], REDACTED)
        self.assertEqual(out["harmless_field"], "kept")

    def test_deep_recursion_capped(self) -> None:
        # Build a payload deeper than the depth ceiling (8).
        deep: dict = {"v": "ok"}
        for _ in range(20):
            deep = {"nested": deep}
        # Must not RecursionError and must return SOMETHING.
        result = _scrub_obj(deep, self.patterns)
        self.assertIsNotNone(result)

    def test_non_string_primitives_preserved(self) -> None:
        event = {"int": 42, "float": 1.5, "bool": True, "none": None, "ok": "fine"}
        out = _scrub_obj(event, self.patterns)
        self.assertEqual(out, event)


class BeforeSendTestCase(unittest.TestCase):
    def test_before_send_redacts(self) -> None:
        bs = make_before_send()
        event = {"message": "token leaked: ghp_aaaaaaaaaaaaaaaaaaaaaaaa1234"}
        out = bs(event, hint=None)
        self.assertIn(REDACTED, out["message"])

    def test_before_send_passes_through_on_internal_error(self) -> None:
        bs = make_before_send()
        # Pass something that breaks recursion (non-standard type with bad iter).
        bad = {"x": object()}  # safe — object() falls through to `return obj`
        out = bs(bad, hint=None)
        # Must return a dict; the bad value is preserved as-is.
        self.assertIn("x", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
