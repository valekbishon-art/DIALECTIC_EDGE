"""Tests for :mod:`refactor.observability.logging_setup`.

Coverage:

* Level parsing: known names (case-insensitive), unknown → INFO fallback.
* JSON formatter emits one-line JSON with required keys.
* Exceptions are captured into ``exc`` field.
* Custom kwargs via ``extra={...}`` are merged into the JSON payload.
* Non-JSON-serialisable extras get ``repr``'d (no crash).
* ``setup_logging`` replaces existing handlers (no double output).
* Quiet libs list raises their level to WARNING.
* ``LOG_LEVEL`` / ``LOG_FORMAT`` env overrides are honoured.
"""
from __future__ import annotations

import io
import json
import logging
import unittest
from unittest.mock import patch

from refactor.observability.logging_setup import (
    DEFAULT_QUIET_LIBS,
    JsonFormatter,
    _normalise_level,
    setup_logging,
)


class NormaliseLevelTestCase(unittest.TestCase):
    def test_known_levels(self) -> None:
        for name, expected in [
            ("DEBUG", logging.DEBUG),
            ("debug", logging.DEBUG),
            ("INFO", logging.INFO),
            ("Warning", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("critical", logging.CRITICAL),
        ]:
            with self.subTest(name=name):
                self.assertEqual(_normalise_level(name), expected)

    def test_unknown_falls_back_to_info(self) -> None:
        self.assertEqual(_normalise_level("FOOBAR"), logging.INFO)
        self.assertEqual(_normalise_level("foobar"), logging.INFO)

    def test_empty_or_none_falls_back(self) -> None:
        self.assertEqual(_normalise_level(None), logging.INFO)
        self.assertEqual(_normalise_level(""), logging.INFO)
        self.assertEqual(_normalise_level("   "), logging.INFO)


class JsonFormatterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.formatter = JsonFormatter()

    def _make_record(
        self,
        msg: str = "hello",
        level: int = logging.INFO,
        *,
        args: tuple = (),
        exc_info=None,
        extra: dict | None = None,
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.module",
            level=level,
            pathname=__file__,
            lineno=42,
            msg=msg,
            args=args,
            exc_info=exc_info,
        )
        if extra:
            for k, v in extra.items():
                setattr(record, k, v)
        return record

    def test_basic_shape(self) -> None:
        record = self._make_record("hello world")
        out = self.formatter.format(record)
        payload = json.loads(out)
        self.assertEqual(payload["msg"], "hello world")
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["logger"], "test.module")
        self.assertIn("ts", payload)

    def test_formatted_message(self) -> None:
        record = self._make_record("user=%s done", args=("alice",))
        payload = json.loads(self.formatter.format(record))
        self.assertEqual(payload["msg"], "user=alice done")

    def test_exception_in_payload(self) -> None:
        try:
            raise ValueError("kaboom")
        except ValueError:
            import sys
            exc = sys.exc_info()
        record = self._make_record("crashed", level=logging.ERROR, exc_info=exc)
        payload = json.loads(self.formatter.format(record))
        self.assertIn("exc", payload)
        self.assertIn("ValueError", payload["exc"])
        self.assertIn("kaboom", payload["exc"])

    def test_extra_kwargs_merged(self) -> None:
        record = self._make_record("trade", extra={"symbol": "BTC", "qty": 0.5})
        payload = json.loads(self.formatter.format(record))
        self.assertEqual(payload["symbol"], "BTC")
        self.assertEqual(payload["qty"], 0.5)

    def test_non_serialisable_extra_falls_back_to_repr(self) -> None:
        class Weird:
            def __repr__(self) -> str:
                return "<Weird obj>"

        record = self._make_record("hi", extra={"obj": Weird()})
        payload = json.loads(self.formatter.format(record))
        self.assertEqual(payload["obj"], "<Weird obj>")

    def test_no_secret_leak_via_record_fields(self) -> None:
        """Reserved LogRecord attributes are not blindly copied into output.

        E.g. ``record.pathname`` is informative for stack traces but not
        meant to be replicated in the JSON payload (we have ``logger``).
        """
        record = self._make_record("hello")
        payload = json.loads(self.formatter.format(record))
        self.assertNotIn("pathname", payload)
        self.assertNotIn("args", payload)
        self.assertNotIn("levelname", payload)  # we use "level" not "levelname"


class SetupLoggingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_root_level = logging.getLogger().level
        self._saved_root_handlers = list(logging.getLogger().handlers)

    def tearDown(self) -> None:
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in self._saved_root_handlers:
            root.addHandler(h)
        root.setLevel(self._saved_root_level)

    def test_replaces_existing_handlers(self) -> None:
        root = logging.getLogger()
        # Pre-existing dummy handler — should be removed.
        dummy = logging.StreamHandler()
        root.addHandler(dummy)
        setup_logging("INFO", quiet_libs=())
        self.assertNotIn(dummy, root.handlers)
        self.assertEqual(len(root.handlers), 1)

    def test_level_applied_to_root(self) -> None:
        setup_logging("DEBUG", quiet_libs=())
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_unknown_level_falls_back(self) -> None:
        resolved = setup_logging("ridiculous", quiet_libs=())
        self.assertEqual(resolved, logging.INFO)
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_json_format_emits_json(self) -> None:
        stream = io.StringIO()
        setup_logging("INFO", fmt="json", quiet_libs=(), stream=stream)
        log = logging.getLogger("dialectic.test")
        log.info("hello %s", "world")
        for h in logging.getLogger().handlers:
            h.flush()
        line = stream.getvalue().strip()
        self.assertTrue(line, "no log line emitted")
        payload = json.loads(line)
        self.assertEqual(payload["msg"], "hello world")
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["logger"], "dialectic.test")

    def test_text_format_default(self) -> None:
        stream = io.StringIO()
        setup_logging("INFO", fmt="text", quiet_libs=(), stream=stream)
        logging.getLogger("dialectic.test").warning("plain text line")
        for h in logging.getLogger().handlers:
            h.flush()
        line = stream.getvalue().strip()
        self.assertIn("WARNING", line)
        self.assertIn("plain text line", line)
        # Should NOT be parseable as JSON.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(line)

    def test_quiet_libs_silenced(self) -> None:
        setup_logging("DEBUG", quiet_libs=("noisy.lib",))
        self.assertEqual(logging.getLogger("noisy.lib").level, logging.WARNING)

    def test_default_quiet_libs(self) -> None:
        setup_logging("INFO")
        for name in DEFAULT_QUIET_LIBS:
            with self.subTest(name=name):
                self.assertEqual(logging.getLogger(name).level, logging.WARNING)

    def test_env_overrides(self) -> None:
        with patch.dict(
            "os.environ",
            {"LOG_LEVEL": "ERROR", "LOG_FORMAT": "json", "LOG_QUIET_LIBS": "foo,bar"},
            clear=False,
        ):
            stream = io.StringIO()
            resolved = setup_logging(stream=stream)
        self.assertEqual(resolved, logging.ERROR)
        self.assertEqual(logging.getLogger().level, logging.ERROR)
        self.assertEqual(logging.getLogger("foo").level, logging.WARNING)
        self.assertEqual(logging.getLogger("bar").level, logging.WARNING)

    def test_idempotent(self) -> None:
        setup_logging("INFO", quiet_libs=())
        setup_logging("INFO", quiet_libs=())
        # After two calls, root should still have exactly one handler.
        self.assertEqual(len(logging.getLogger().handlers), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
