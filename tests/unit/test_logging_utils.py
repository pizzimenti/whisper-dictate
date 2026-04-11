"""Tests for the shared logging helper."""

from __future__ import annotations

import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kdictate.logging_utils import (
    attach_file_handler,
    configure_logging,
    get_propagating_child,
)


class LoggingUtilsTests(unittest.TestCase):
    """Verify logger setup is deterministic and does not duplicate handlers."""

    def test_configure_logging_installs_one_handler(self) -> None:
        stream = io.StringIO()
        logger_name = "kdictate.tests.logging_utils"
        logger = logging.getLogger(logger_name)
        original_handlers = list(logger.handlers)
        for handler in original_handlers:
            logger.removeHandler(handler)

        try:
            configured = configure_logging(logger_name, stream=stream)
            self.assertIs(configured, logger)
            self.assertEqual(len(configured.handlers), 1)
            self.assertFalse(configured.propagate)

            configured.info("hello world")
            output = stream.getvalue()
            self.assertIn(logger_name, output)
            self.assertIn("hello world", output)

            configure_logging(logger_name, stream=stream)
            self.assertEqual(len(configured.handlers), 1)
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            for handler in original_handlers:
                logger.addHandler(handler)


class GetPropagatingChildTests(unittest.TestCase):
    """Verify parent-with-children pattern funnels into one handler."""

    def setUp(self) -> None:
        self._parent_name = "kdictate.tests.propagating_parent"
        self._child_suffixes = ("alpha", "beta", "gamma")
        self._touched: list[logging.Logger] = []

    def tearDown(self) -> None:
        for logger in self._touched:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            logger.propagate = True

    def _track(self, logger: logging.Logger) -> logging.Logger:
        self._touched.append(logger)
        return logger

    def test_child_has_no_handlers_and_propagates(self) -> None:
        parent = self._track(logging.getLogger(self._parent_name))
        child = self._track(get_propagating_child(parent, "alpha"))

        self.assertEqual(child.name, f"{self._parent_name}.alpha")
        self.assertEqual(child.handlers, [])
        self.assertTrue(child.propagate)

    def test_existing_child_handlers_are_cleared(self) -> None:
        parent = self._track(logging.getLogger(self._parent_name))
        pre_existing = self._track(logging.getLogger(f"{self._parent_name}.beta"))
        pre_existing.addHandler(logging.NullHandler())
        pre_existing.propagate = False
        self.assertEqual(len(pre_existing.handlers), 1)

        reset = get_propagating_child(parent, "beta")

        self.assertIs(reset, pre_existing)
        self.assertEqual(reset.handlers, [])
        self.assertTrue(reset.propagate)

    def test_one_handler_receives_messages_from_all_children(self) -> None:
        # Set up a parent with a single capturing stream handler, and three
        # propagating children. Each child logs once. The capturing handler
        # should see exactly three records — proving messages funnel through
        # one handler instead of being duplicated by per-child handlers.
        parent = self._track(logging.getLogger(self._parent_name))
        for handler in list(parent.handlers):
            parent.removeHandler(handler)
        stream = io.StringIO()
        capture = logging.StreamHandler(stream)
        capture.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        parent.addHandler(capture)
        parent.setLevel(logging.INFO)
        parent.propagate = False

        children = [
            self._track(get_propagating_child(parent, suffix))
            for suffix in self._child_suffixes
        ]
        for child, suffix in zip(children, self._child_suffixes):
            child.setLevel(logging.INFO)
            child.info("hello from %s", suffix)

        lines = [line for line in stream.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 3)
        for line, suffix in zip(lines, self._child_suffixes):
            self.assertIn(f"{self._parent_name}.{suffix}", line)
            self.assertIn(f"hello from {suffix}", line)


class FileHandlerAttachmentTests(unittest.TestCase):
    """Cover the configure_logging(log_file=) and attach_file_handler paths."""

    def setUp(self) -> None:
        # Each test gets its own private XDG_STATE_HOME so we never write
        # into the real ~/.local/state/kdictate/ used by the production
        # daemon. The tempdir is cleaned in tearDown.
        self._tempdir = tempfile.TemporaryDirectory()
        self._xdg_state = Path(self._tempdir.name)
        self._env_patch = patch.dict(
            os.environ, {"XDG_STATE_HOME": str(self._xdg_state)}
        )
        self._env_patch.start()
        self._touched: list[logging.Logger] = []

    def tearDown(self) -> None:
        for logger in self._touched:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            logger.propagate = True
        self._env_patch.stop()
        self._tempdir.cleanup()

    def _track(self, logger: logging.Logger) -> logging.Logger:
        self._touched.append(logger)
        return logger

    def test_configure_logging_with_log_file_attaches_filehandler(self) -> None:
        # Reset any stale handlers carried over from a prior test run
        # in the same process so the post-condition is unambiguous —
        # "this logger has exactly one FileHandler" must be a
        # statement about THIS call, not a happy coincidence with
        # whatever prior state existed.
        stale = logging.getLogger("kdictate.tests.fh_attach")
        for handler in list(stale.handlers):
            stale.removeHandler(handler)
        self._track(stale)

        logger = self._track(
            configure_logging(
                "kdictate.tests.fh_attach",
                log_file="test.log",
            )
        )

        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        self.assertEqual(len(file_handlers), 1)

        target = self._xdg_state / "kdictate" / "test.log"
        self.assertEqual(file_handlers[0].baseFilename, str(target))
        # Emit a record and verify it actually lands in the file.
        logger.info("hello file")
        for handler in file_handlers:
            handler.flush()
        self.assertTrue(target.exists())
        self.assertIn("hello file", target.read_text(encoding="utf-8"))

    def test_attach_file_handler_is_idempotent(self) -> None:
        logger = self._track(logging.getLogger("kdictate.tests.fh_idempotent"))
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

        attach_file_handler(logger, "test.log")
        attach_file_handler(logger, "test.log")
        attach_file_handler(logger, "test.log")

        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        self.assertEqual(len(file_handlers), 1)

    def test_attach_file_handler_silently_skips_when_log_dir_unwritable(self) -> None:
        logger = self._track(logging.getLogger("kdictate.tests.fh_no_dir"))
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

        with patch(
            "kdictate.logging_utils._resolve_log_dir",
            return_value=None,
        ):
            attach_file_handler(logger, "test.log")

        file_handlers = [
            h for h in logger.handlers if isinstance(h, logging.FileHandler)
        ]
        self.assertEqual(file_handlers, [])
